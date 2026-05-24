"""Export pipeline artifacts to Tableau-ready CSVs (and optional Hyper extract).

Generates five files in ``/dashboard/tableau_data/``:

* ``fact_financials.csv``  — long-format quarterly actuals with provenance columns
* ``fact_forecasts.csv``   — combined Prophet + AutoARIMA + Lasso forecasts
* ``dim_date.csv``         — date dimension (fiscal + calendar)
* ``dim_metric.csv``       — metric metadata (label, category, unit)
* ``dim_filing.csv``       — one row per accession_no (provenance dimension)

A Tableau Hyper extract is also attempted; if ``tableauhyperapi`` is not
installed or fails, the step is skipped cleanly with a warning.

Star schema for Tableau::

    fact_financials ─┬─ dim_date    (period_end = date_key)
    fact_forecasts   ├─ dim_metric  (line_item = metric_key)
                     └─ dim_filing  (accession_no = filing_key)

CLI::

    python -m src.export_for_tableau
    python -m src.export_for_tableau --ticker PANW
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
import yaml

logger = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONFIG_PATH = _REPO_ROOT / "config" / "company.yaml"
_PROCESSED_DIR = _REPO_ROOT / "data" / "processed"
_MODELS_DIR = _REPO_ROOT / "models"
_TABLEAU_DIR = _REPO_ROOT / "dashboard" / "tableau_data"
_DASHBOARD_DIR = _REPO_ROOT / "dashboard"

# ── Metric metadata ────────────────────────────────────────────────────────────
# Controls dim_metric.csv — human labels and category groupings for Tableau.
_METRIC_META: list[dict[str, str]] = [
    # Income Statement
    {"line_item": "Revenue", "label": "Revenue", "category": "Income Statement", "unit": "USD"},
    {
        "line_item": "CostOfRevenue",
        "label": "Cost of Revenue",
        "category": "Income Statement",
        "unit": "USD",
    },
    {
        "line_item": "GrossProfit",
        "label": "Gross Profit",
        "category": "Income Statement",
        "unit": "USD",
    },
    {
        "line_item": "OperatingExpenses",
        "label": "Operating Expenses",
        "category": "Income Statement",
        "unit": "USD",
    },
    {
        "line_item": "OperatingIncome",
        "label": "Operating Income",
        "category": "Income Statement",
        "unit": "USD",
    },
    {
        "line_item": "NetIncome",
        "label": "Net Income",
        "category": "Income Statement",
        "unit": "USD",
    },
    # Balance Sheet
    {
        "line_item": "Cash",
        "label": "Cash & Equivalents",
        "category": "Balance Sheet",
        "unit": "USD",
    },
    {
        "line_item": "AccountsReceivable",
        "label": "Accounts Receivable",
        "category": "Balance Sheet",
        "unit": "USD",
    },
    {"line_item": "Inventory", "label": "Inventory", "category": "Balance Sheet", "unit": "USD"},
    {
        "line_item": "AccountsPayable",
        "label": "Accounts Payable",
        "category": "Balance Sheet",
        "unit": "USD",
    },
    {
        "line_item": "DeferredRevenue",
        "label": "Deferred Revenue",
        "category": "Balance Sheet",
        "unit": "USD",
    },
    {
        "line_item": "TotalAssets",
        "label": "Total Assets",
        "category": "Balance Sheet",
        "unit": "USD",
    },
    {
        "line_item": "TotalLiabilities",
        "label": "Total Liabilities",
        "category": "Balance Sheet",
        "unit": "USD",
    },
    {
        "line_item": "TotalEquity",
        "label": "Total Equity",
        "category": "Balance Sheet",
        "unit": "USD",
    },
    # Cash Flow
    {
        "line_item": "OperatingCashFlow",
        "label": "Operating Cash Flow",
        "category": "Cash Flow",
        "unit": "USD",
    },
    {"line_item": "CapEx", "label": "Capital Expenditures", "category": "Cash Flow", "unit": "USD"},
    {
        "line_item": "FreeCashFlow",
        "label": "Free Cash Flow",
        "category": "Cash Flow",
        "unit": "USD",
    },
    {
        "line_item": "StockBasedCompensation",
        "label": "Stock-Based Compensation",
        "category": "Cash Flow",
        "unit": "USD",
    },
    # Margins and growth rates are NOT exported as fact rows — they are
    # computed inside Tableau as calculated fields from the sourced rows
    # above (see Tableau_Setup.md §5).  This keeps every fact row tied to a
    # single SEC accession_no instead of carrying derived rows with no
    # provenance.
]


# ── Helper: snap to fiscal quarter-end ────────────────────────────────────────


def _snap_to_fiscal_quarter_end(ts: pd.Timestamp, fy_end_month: int) -> pd.Timestamp:
    """Snap a timestamp forward to its enclosing fiscal quarter-end (last day of month).

    Forecast notebooks emit a mix of conventions: Prophet/AutoARIMA produce
    quarter-start dates, Lasso produces quarter-end. Tableau joins on
    fact_financials.period_end (always a real fiscal quarter-end like 2026-04-30
    for a July fiscal year) require every forecast row to share that convention.
    """
    ts = pd.to_datetime(ts)
    qe_months = sorted({((fy_end_month - 3 * i - 1) % 12) + 1 for i in range(4)})
    year, month = ts.year, ts.month
    target = next((m for m in qe_months if m >= month), None)
    if target is None:
        target = qe_months[0]
        year += 1
    return pd.Timestamp(year=year, month=target, day=1) + pd.offsets.MonthEnd(0)


# ── Helper: fiscal period → calendar quarter ──────────────────────────────────


def _fiscal_to_calendar_quarter(
    fiscal_year: int,
    fiscal_period: str,
    fy_end_month: int,
) -> tuple[int, int]:
    """Map fiscal quarter to (calendar_year, calendar_quarter).

    Args:
        fiscal_year:    EDGAR fiscal year (e.g. 2024).
        fiscal_period:  EDGAR fiscal period string (e.g. 'Q1', 'Q4').
        fy_end_month:   Month in which the fiscal year ends (1–12).

    Returns:
        (calendar_year, calendar_quarter) tuple.
    """
    quarter_num = int(fiscal_period[-1]) if fiscal_period.startswith("Q") else 0
    if quarter_num == 0:
        return fiscal_year, 4

    # Offset Q1 start from FY start month
    fy_start_month = (fy_end_month % 12) + 1
    cal_month = (fy_start_month + (quarter_num - 1) * 3 - 1) % 12 + 1
    cal_year = fiscal_year
    # If the quarter start crosses a calendar year boundary
    raw_month = fy_start_month + (quarter_num - 1) * 3
    if raw_month > 12:
        cal_year += raw_month // 13
    cal_quarter = (cal_month - 1) // 3 + 1
    return cal_year, cal_quarter


# ── Export functions ───────────────────────────────────────────────────────────


def _export_fact_financials(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Long-format actuals with provenance columns.

    Deduplication notes
    -------------------
    Two distinct sources of duplication must be resolved before the CSV is
    Tableau-safe (Tableau's default AVG aggregation silently halves doubled
    values):

    1. **Multi-fiscal-year comparatives.**  A 10-Q for FY2026-Q1 carries the
       prior-year same-quarter row as a comparative — same ``period_end`` but
       a *different* ``fiscal_year`` / ``fiscal_period`` and often a different
       ``frame`` than the original FY2025-Q1 filing.  ``v_canonical_facts``
       partitions on ``frame`` so both rows survive.  We collapse them here
       using a form-priority ordering (10-K/A > 10-Q/A > 10-K > 10-Q) with
       latest ``filed_date`` as tiebreaker — the same priority used inside
       ``v_canonical_facts``, but partitioning only on
       ``(ticker, line_item, period_end)`` so each calendar period_end yields
       at most one row in the export.

    2. **YTD vs standalone within the same filing.**  XBRL also reports a
       3-month standalone value and a YTD cumulative value for the same
       concept and period_end (e.g. a Q2 10-Q includes both the Q2 standalone
       and the H1 YTD revenue figure).  Both rows pass the ``period_type='Q'``
       filter and share the same fiscal triple.  We resolve those by keeping
       the row with the minimum absolute value per (ticker, line_item,
       period_end, fiscal_year, fiscal_period) — the standalone quarterly
       value is always <= the YTD cumulative for income-statement and
       cash-flow items, and balance-sheet items (point-in-time) are identical
       across contexts.
    """
    # Pull from v_canonical_facts and collapse multi-fiscal-year comparatives
    # to one canonical row per (ticker, line_item, period_end) using form
    # priority + most-recent filed_date.  This is what makes the export
    # Tableau-safe: every (line_item, period_end) pair appears exactly once.
    df = con.execute("""
        SELECT
            ticker,
            line_item,
            period_end,
            period_type,
            fiscal_year,
            fiscal_period,
            value,
            unit,
            concept_used,
            accession_no,
            fact_id,
            filing_url,
            form_type,
            filed_date
        FROM v_canonical_facts
        WHERE period_type = 'Q'
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY ticker, line_item, period_end
            ORDER BY
                CASE form_type
                    WHEN '10-K/A' THEN 4
                    WHEN '10-Q/A' THEN 3
                    WHEN '10-K'   THEN 2
                    WHEN '10-Q'   THEN 1
                    ELSE 0
                END DESC,
                filed_date DESC
        ) = 1
        ORDER BY line_item, fiscal_year, fiscal_period
    """).fetchdf()

    # Drop YTD duplicates: sort by abs(value) ascending, keep first per group.
    # (After the form-priority collapse above the surviving rows still need this
    # pass because YTD/standalone duplication happens within a single filing.)
    dedup_key = ["ticker", "line_item", "period_end", "fiscal_year", "fiscal_period"]
    if len(df) > 0:
        n_before = len(df)
        df["_abs_value"] = df["value"].abs()
        df = (
            df.sort_values("_abs_value")
            .drop_duplicates(subset=dedup_key, keep="first")
            .drop(columns=["_abs_value"])
        )
        n_removed = n_before - len(df)
        if n_removed > 0:
            logger.info(
                "  Removed %d YTD duplicate rows (kept standalone quarterly values)", n_removed
            )

    return df.sort_values(["line_item", "fiscal_year", "fiscal_period"]).reset_index(drop=True)


def _export_fact_forecasts(ticker: str, fy_end_month: int = 12) -> pd.DataFrame:
    """Combine all model forecast parquets.

    Snaps every period_end to the enclosing fiscal quarter-end so the resulting
    rows join 1:1 against dim_date (and against fact_financials' real reporting
    period_end). Adds a constant line_item='Revenue' column so forecasts can be
    joined to dim_metric the same way fact_financials rows are.
    """
    frames: list[pd.DataFrame] = []
    for fname in (
        f"{ticker}_baseline_forecasts.parquet",
        f"{ticker}_macro_forecast.parquet",
    ):
        path = _MODELS_DIR / fname
        if path.exists():
            df = pd.read_parquet(path)
            df["ticker"] = ticker
            frames.append(df)
            logger.info("  Loaded %s (%d rows)", fname, len(df))
        else:
            logger.warning("  Forecast not found: %s — skipping", path)

    if not frames:
        logger.warning("No forecast parquets found; fact_forecasts.csv will be empty stub.")
        return pd.DataFrame(
            columns=[
                "ticker",
                "model",
                "line_item",
                "period_end",
                "yhat",
                "yhat_lower_80",
                "yhat_upper_80",
                "yhat_lower_95",
                "yhat_upper_95",
            ]
        )

    combined = pd.concat(frames, ignore_index=True)
    combined["period_end"] = pd.to_datetime(combined["period_end"]).map(
        lambda ts: _snap_to_fiscal_quarter_end(ts, fy_end_month)
    )
    combined["line_item"] = "Revenue"
    return combined.sort_values(["model", "period_end"]).reset_index(drop=True)


def _export_dim_date(
    df_financials: pd.DataFrame,
    df_forecasts: pd.DataFrame,
    fy_end_month: int,
) -> pd.DataFrame:
    """Date dimension covering all periods in actuals + forecasts."""
    all_rows: list[dict[str, Any]] = []

    for df in (df_financials, df_forecasts):
        if "fiscal_year" not in df.columns or "fiscal_period" not in df.columns:
            continue
        for _, row in (
            df[["period_end", "fiscal_year", "fiscal_period"]].drop_duplicates().iterrows()
        ):
            fp = str(row.get("fiscal_period", ""))
            fy = int(row.get("fiscal_year", 0)) if pd.notna(row.get("fiscal_year")) else 0
            if not fp.startswith("Q") or fy == 0:
                continue
            cal_year, cal_q = _fiscal_to_calendar_quarter(fy, fp, fy_end_month)
            all_rows.append(
                {
                    "date_key": str(row["period_end"])[:10],
                    "period_end": row["period_end"],
                    "fiscal_year": fy,
                    "fiscal_quarter": fp,
                    "calendar_year": cal_year,
                    "calendar_quarter": cal_q,
                }
            )

    # Forecast periods (no fiscal_year in fact_forecasts — derive from date)
    if "period_end" in df_forecasts.columns and "fiscal_year" not in df_forecasts.columns:
        for pe in df_forecasts["period_end"].dropna().unique():
            pe_dt = pd.to_datetime(pe)
            all_rows.append(
                {
                    "date_key": str(pe_dt.date()),
                    "period_end": pe_dt,
                    "fiscal_year": pe_dt.year,
                    "fiscal_quarter": f"Q{(pe_dt.month - 1) // 3 + 1}",
                    "calendar_year": pe_dt.year,
                    "calendar_quarter": (pe_dt.month - 1) // 3 + 1,
                }
            )

    if not all_rows:
        return pd.DataFrame(
            columns=[
                "date_key",
                "period_end",
                "fiscal_year",
                "fiscal_quarter",
                "calendar_year",
                "calendar_quarter",
            ]
        )

    dim = pd.DataFrame(all_rows).drop_duplicates(subset=["date_key"])
    return dim.sort_values("date_key").reset_index(drop=True)


def _export_dim_metric() -> pd.DataFrame:
    return pd.DataFrame(_METRIC_META)


def _export_dim_filing(df_financials: pd.DataFrame) -> pd.DataFrame:
    """One row per accession_no with filing metadata."""
    required = {"accession_no", "filing_url", "filed_date", "form_type"}
    if not required.issubset(df_financials.columns):
        return pd.DataFrame(columns=list(required))

    dim = (
        df_financials[["accession_no", "filing_url", "filed_date", "form_type"]]
        .dropna(subset=["accession_no"])
        .drop_duplicates(subset=["accession_no"])
        .sort_values("accession_no")
        .reset_index(drop=True)
    )
    return dim


def _try_write_hyper(tableau_dir: Path, ticker: str) -> None:
    """Attempt to write a Tableau Hyper extract; skip cleanly if unavailable."""
    try:
        from tableauhyperapi import (  # noqa: PLC0415
            Connection,
            CreateMode,
            HyperProcess,
            Inserter,
            SqlType,
            TableDefinition,
            TableName,
            Telemetry,
        )
    except ImportError:
        logger.info("tableauhyperapi not installed — skipping Hyper extract.")
        return

    hyper_path = tableau_dir / f"{ticker}_financials.hyper"
    try:
        with (
            HyperProcess(Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hp,
            Connection(hp.endpoint, str(hyper_path), CreateMode.CREATE_AND_REPLACE) as con,
        ):
            schema = TableDefinition(
                TableName("Extract", "fact_financials"),
                [
                    TableDefinition.Column("ticker", SqlType.text()),
                    TableDefinition.Column("line_item", SqlType.text()),
                    TableDefinition.Column("period_end", SqlType.date()),
                    TableDefinition.Column("fiscal_year", SqlType.int()),
                    TableDefinition.Column("fiscal_period", SqlType.text()),
                    TableDefinition.Column("value", SqlType.double()),
                    TableDefinition.Column("accession_no", SqlType.text()),
                    TableDefinition.Column("filing_url", SqlType.text()),
                ],
            )
            con.catalog.create_table(schema)

            fin_csv = tableau_dir / "fact_financials.csv"
            if fin_csv.exists():
                df = pd.read_csv(fin_csv)
                with Inserter(con, schema) as ins:
                    for _, row in df.iterrows():
                        ins.add_row(
                            [
                                str(row.get("ticker", "")),
                                str(row.get("line_item", "")),
                                str(row.get("period_end", ""))[:10],
                                int(row.get("fiscal_year", 0))
                                if pd.notna(row.get("fiscal_year"))
                                else 0,
                                str(row.get("fiscal_period", "")),
                                float(row.get("value", 0.0)) if pd.notna(row.get("value")) else 0.0,
                                str(row.get("accession_no", "") or ""),
                                str(row.get("filing_url", "") or ""),
                            ]
                        )
                    ins.execute()

        logger.info("Hyper extract written: %s", hyper_path)

    except Exception as exc:
        logger.warning("Hyper extract failed: %s — continuing without it.", exc)


def _write_tableau_setup_md(output_dir: Path, ticker: str) -> None:
    """Write Tableau_Setup.md with connection instructions.

    Kept in sync with dashboard/Tableau_Setup.md (the ticker-agnostic version
    of the same content).  See bead a2d for the planned template extraction.
    """
    content = f"""# Tableau Setup — {ticker} Financial Model

> **⚠️ WARNING: Tableau Public publishes data WORLD-READABLE and Google-INDEXABLE.**
> **The SEC data here is already public, but if you ever extend this project to**
> **non-public sources, do NOT publish to Tableau Public.**

---

## 1. File Overview

The `/dashboard/tableau_data/` folder contains five files:

| File | Description |
|---|---|
| `fact_financials.csv` | Long-format quarterly actuals (all line items) with provenance |
| `fact_forecasts.csv` | Prophet + AutoARIMA + Lasso forecasts with 80%/95% CIs |
| `dim_date.csv` | Date dimension: fiscal year/quarter + calendar year/quarter |
| `dim_metric.csv` | Metric metadata: label, category, unit |
| `dim_filing.csv` | One row per `accession_no` with `filing_url`, `form_type`, `filed_date` |

---

## 2. Star Schema

Connect all tables in Tableau using these join keys:

```
fact_financials ──── dim_date    on  fact_financials.period_end = dim_date.date_key
fact_financials ──── dim_metric  on  fact_financials.line_item  = dim_metric.line_item
fact_financials ──── dim_filing  on  fact_financials.accession_no = dim_filing.accession_no
fact_forecasts  ──── dim_date    on  fact_forecasts.period_end  = dim_date.date_key
```

`dim_filing` is the **provenance dimension** — every mark on a Tableau viz can
carry a tooltip linking to the source SEC filing.

---

## 3. Connecting in Tableau Desktop / Tableau Public

1. Open Tableau Desktop or Tableau Public.
2. **Connect → Text File** → select `fact_financials.csv`.
3. Add remaining files via **Data Source** tab → drag each CSV to the canvas.
4. Create the joins as described above.

> The CSVs are the authoritative source — open all five via Tableau's Text
> File connector. The `.hyper` extract bundled in `tableau_data/` is a
> **`fact_financials`-only** convenience extract for faster scrolling on
> large quarter ranges; it is **not** a substitute for the `dim_*` and
> `fact_forecasts` CSVs. If you connect to the `.hyper` you will be missing
> the date/metric/filing dimensions and all forecast rows.

---

## 4. Recommended Worksheets

The three sheets below match the **currently published v1 dashboard**. Each
is built from `fact_financials` joined to the dimension tables.

### Sheet 1: Revenue Actuals
- Rows: `SUM([value])` filtered to `line_item = 'Revenue'`, scaled to $B
- Columns: `period_end` (continuous, quarterly)
- Marks: Line + circle for individual quarter marks
- Filters: `line_item = 'Revenue'`
- **"Source" tooltip** on every mark, joined via `dim_filing`:
  ```
  Accession: <accession_no>
  Filed: <filed_date>
  Form: <form_type>
  ATTR([filing_url])  ← wire up as a URL action (see §6)
  ```
- Click-through provenance is direct here — every Revenue mark is a single
  XBRL fact with a single accession.

### Sheet 2: Margins %
- Rows: gross margin %, operating margin %, net margin % (three measures
  on the same axis, or three rows)
- Columns: `period_end` (continuous, quarterly)
- Marks: Line, one colour per margin type
- Calculated fields (see §5):
  ```
  Gross Margin %     = SUM([GrossProfit])  / SUM([Revenue])
  Operating Margin % = SUM([OperatingIncome]) / SUM([Revenue])
  Net Margin %       = SUM([NetIncome])    / SUM([Revenue])
  ```
- Format axis as percentage; reference lines optional.
- Provenance: each margin computes from two source rows (numerator and
  denominator) — show both accession_no values in the mark's tooltip.

### Sheet 3: Revenue Growth
- Rows: YoY revenue growth %
- Columns: `period_end` (continuous, quarterly)
- Marks: Bar (positive/negative colour split) or Line
- Calculated field:
  ```
  YoY Revenue Growth =
    (SUM([Revenue]) - LOOKUP(SUM([Revenue]), -4))
    / ABS(LOOKUP(SUM([Revenue]), -4))
  ```
- Filter out the first four quarters (no prior-year comparable).
- Provenance: each growth value comes from two Revenue rows (current quarter
  + same quarter prior year) — both accession_no values are tooltip-able.

### Future work (v2 — not yet published)

The pipeline already produces `fact_forecasts.csv` and `v_variance_facts`,
but the following four sheets have not been built into the published
workbook. They are tracked as v2 dashboard work:

- **Actual vs Forecast** — dual-axis line: actuals from `fact_financials` +
  forecast bands (80% / 95% CIs) from `fact_forecasts`, three-model ensemble
  (Prophet / AutoARIMA / Lasso).
- **Variance Drivers** — bar chart of `revenue_variance_vs_forecast` per
  quarter, coloured by driver type (volume / margin / mix / one-time) from
  `v_variance_facts`.
- **Forecast Accuracy** — MAE and MAPE per expanding-window CV fold,
  grouped by model, with a 10% MAPE reference line.
- **Scenario Toggle** — parameter-driven Base / Bull / Bear filter on
  `fact_forecasts`, showing revenue forecast with CI bands.

---

## 5. Calculated Fields (margins, growth, variance)

Margins and growth rates are **not** materialized as fact rows in
`fact_financials.csv` — they have no single source accession and would break
the "every mark traces to a filing" claim. Compute them in Tableau as
calculated fields from the sourced rows. Provenance flows naturally: each
input row carries its own `accession_no`, so a margin or growth tooltip can
list the source filings used.

```
// Margins (use SUMIF-style expressions over the long-format fact_financials)
Gross Margin %     = SUM(IF [line_item]='GrossProfit'      THEN [value] END)
                   / SUM(IF [line_item]='Revenue'          THEN [value] END)

Operating Margin % = SUM(IF [line_item]='OperatingIncome'  THEN [value] END)
                   / SUM(IF [line_item]='Revenue'          THEN [value] END)

Net Margin %       = SUM(IF [line_item]='NetIncome'        THEN [value] END)
                   / SUM(IF [line_item]='Revenue'          THEN [value] END)

FCF Margin %       = SUM(IF [line_item]='FreeCashFlow'     THEN [value] END)
                   / SUM(IF [line_item]='Revenue'          THEN [value] END)

// Growth (period_end on Columns, sorted ascending, line_item filtered to Revenue)
YoY Revenue Growth = (SUM([value]) - LOOKUP(SUM([value]), -4))
                     / ABS(LOOKUP(SUM([value]), -4))

QoQ Revenue Growth = (SUM([value]) - LOOKUP(SUM([value]), -1))
                     / ABS(LOOKUP(SUM([value]), -1))

// Variance + accuracy (against fact_forecasts)
Revenue Variance % = ([Revenue Actual] - [Revenue Forecast]) / ABS([Revenue Forecast])
MAPE per fold      = ABS(([Revenue Actual] - [Revenue Forecast]) / [Revenue Actual])
```

---

## 6. Provenance Tooltip Setup

On any worksheet showing actuals:
1. In the **Tooltip** editor, add:
   ```
   Source filing: <accession_no>
   Filed: <filed_date>  |  Form: <form_type>
   Click to open: <URL action>
   ```
2. Create a **URL Action**: Dashboard → Actions → URL
   - URL: `<filing_url>`
   - Run on: Hover or Click

This is what makes the model interview-defensible: every data point is one click
away from its source SEC filing.

---

## 7. Publishing to Tableau Public

1. Sign in to Tableau Public (free account).
2. File → Save to Tableau Public.
3. Copy the published URL and embed it in the project README.

**Reminder**: Once published, data is world-readable. The SEC EDGAR data used
here is already public, so this is appropriate. Do not publish if you add
any non-public data sources.
"""
    (output_dir / "Tableau_Setup.md").write_text(content)
    logger.info("Written Tableau_Setup.md")


# ── Main export function ───────────────────────────────────────────────────────


def export(ticker: str | None = None) -> dict[str, Path]:
    """Export all Tableau artifacts for the given ticker.

    Args:
        ticker: Ticker symbol override; falls back to config/company.yaml.

    Returns:
        Dict mapping artifact name → output path.

    Raises:
        FileNotFoundError: If DuckDB warehouse is missing.
    """
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(f"Config not found: {_CONFIG_PATH}")
    with _CONFIG_PATH.open() as fh:
        config: dict[str, Any] = yaml.safe_load(fh)

    resolved_ticker = (ticker or str(config["ticker"])).upper().strip()
    fy_end_month = int(config.get("fiscal_year_end_month", 12))

    db_path = _PROCESSED_DIR / f"{resolved_ticker}.duckdb"
    if not db_path.exists():
        raise FileNotFoundError(
            f"DuckDB warehouse not found: {db_path}\n"
            f"Run first: python -m src.build_warehouse --ticker {resolved_ticker}"
        )

    _TABLEAU_DIR.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, Path] = {}

    # ── fact_financials ────────────────────────────────────────────────────────
    logger.info("Exporting fact_financials...")
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        df_fin = _export_fact_financials(con)
    finally:
        con.close()
    p = _TABLEAU_DIR / "fact_financials.csv"
    df_fin.to_csv(p, index=False)
    outputs["fact_financials"] = p
    logger.info("  %d rows → %s", len(df_fin), p)

    # ── fact_forecasts ─────────────────────────────────────────────────────────
    logger.info("Exporting fact_forecasts...")
    df_fcst = _export_fact_forecasts(resolved_ticker, fy_end_month=fy_end_month)
    p = _TABLEAU_DIR / "fact_forecasts.csv"
    df_fcst.to_csv(p, index=False)
    outputs["fact_forecasts"] = p
    logger.info("  %d rows → %s", len(df_fcst), p)

    # ── dim_date ───────────────────────────────────────────────────────────────
    logger.info("Exporting dim_date...")
    df_date = _export_dim_date(df_fin, df_fcst, fy_end_month)
    p = _TABLEAU_DIR / "dim_date.csv"
    df_date.to_csv(p, index=False)
    outputs["dim_date"] = p
    logger.info("  %d rows → %s", len(df_date), p)

    # ── dim_metric ─────────────────────────────────────────────────────────────
    logger.info("Exporting dim_metric...")
    df_metric = _export_dim_metric()
    p = _TABLEAU_DIR / "dim_metric.csv"
    df_metric.to_csv(p, index=False)
    outputs["dim_metric"] = p
    logger.info("  %d rows → %s", len(df_metric), p)

    # ── dim_filing ─────────────────────────────────────────────────────────────
    logger.info("Exporting dim_filing...")
    df_filing = _export_dim_filing(df_fin)
    p = _TABLEAU_DIR / "dim_filing.csv"
    df_filing.to_csv(p, index=False)
    outputs["dim_filing"] = p
    logger.info("  %d rows → %s", len(df_filing), p)

    # ── Hyper extract (optional) ───────────────────────────────────────────────
    _try_write_hyper(_TABLEAU_DIR, resolved_ticker)

    # ── Tableau_Setup.md ───────────────────────────────────────────────────────
    _write_tableau_setup_md(_DASHBOARD_DIR, resolved_ticker)

    return outputs


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    parser = argparse.ArgumentParser(
        description="Export Tableau-ready CSVs from the DuckDB warehouse.",
    )
    parser.add_argument("--ticker", default=None, help="Ticker symbol (e.g. PANW)")
    args = parser.parse_args()

    try:
        paths = export(ticker=args.ticker)
        print("\nExported files:")
        for name, path in paths.items():
            print(f"  {name:20s} → {path}")
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
