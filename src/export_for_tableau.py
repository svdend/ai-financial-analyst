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

_CUMULATIVE_CASH_FLOW_ITEMS: frozenset[str] = frozenset(
    {
        "OperatingCashFlow",
        "InvestingCashFlow",
        "FinancingCashFlow",
        "CapEx",
        "Depreciation",
        "StockBasedCompensation",
        "TreasuryStockRepurchases",
    }
)
# Balance-sheet items are reported as instantaneous (point-in-time) facts.
# A 10-K filing's FY-end balance row IS the Q4 closing balance — there is no
# separate Q4 10-K fact for these, unlike flow items where Q4 standalone is
# computed by subtraction (FY − Q1 − Q2 − Q3). The export promotes the FY-end
# balance row to (period_type='Q', fiscal_period='Q4') so Sheet 9 has an
# unbroken series across Q4→Q1 transitions.
_BALANCE_SHEET_LINE_ITEMS: frozenset[str] = frozenset(
    {
        "Cash",
        "AccountsReceivable",
        "Inventory",
        "AccountsPayable",
        "DeferredRevenue",
        "TotalAssets",
        "TotalLiabilities",
        "TotalEquity",
    }
)
_QUARTER_FRAME_RE = r"^CY\d{4}Q[1-4]$"
_FCF_DERIVED_CONCEPT = "OperatingCashFlow - CapEx (derived)"

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
    {
        "line_item": "ResearchAndDevelopment",
        "label": "Research & Development",
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
    {
        "line_item": "InvestingCashFlow",
        "label": "Investing Cash Flow",
        "category": "Cash Flow",
        "unit": "USD",
    },
    {
        "line_item": "FinancingCashFlow",
        "label": "Financing Cash Flow",
        "category": "Cash Flow",
        "unit": "USD",
    },
    {"line_item": "CapEx", "label": "Capital Expenditures", "category": "Cash Flow", "unit": "USD"},
    {
        "line_item": "Depreciation",
        "label": "Depreciation & Amortization",
        "category": "Cash Flow",
        "unit": "USD",
    },
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
    {
        "line_item": "TreasuryStockRepurchases",
        "label": "Treasury Stock Repurchases",
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

    Uses the EDGAR convention that ``fiscal_year`` is named for the calendar
    year in which the fiscal year ends (PANW Aug2024–Jul2025 = FY2025). The
    returned calendar tuple corresponds to the *quarter-end* — i.e. for
    PANW FY2025 Q1 (Aug–Oct 2024) this returns ``(2024, 4)``.

    Args:
        fiscal_year:    EDGAR fiscal year (e.g. 2024).
        fiscal_period:  EDGAR fiscal period string (e.g. 'Q1', 'Q4').
        fy_end_month:   Month in which the fiscal year ends (1–12).

    Returns:
        (calendar_year, calendar_quarter) tuple corresponding to the
        calendar quarter that contains the fiscal quarter's *end* date.
    """
    quarter_num = int(fiscal_period[-1]) if fiscal_period.startswith("Q") else 0
    end_month_raw = fy_end_month if quarter_num == 0 else fy_end_month + (quarter_num - 4) * 3
    end_year = fiscal_year if end_month_raw > 0 else fiscal_year - 1
    end_month = ((end_month_raw - 1) % 12) + 1
    cal_quarter = (end_month - 1) // 3 + 1
    return end_year, cal_quarter


def _calendar_to_fiscal_quarter(
    period_end: pd.Timestamp,
    fy_end_month: int,
) -> tuple[int, str]:
    """Map a calendar quarter-end date to (fiscal_year, fiscal_period).

    Inverse of :func:`_fiscal_to_calendar_quarter`. Used to recompute fiscal
    labels from ``period_end`` after the multi-fiscal-year comparative
    collapse: a comparative row carried in a newer 10-Q inherits the *new*
    filing's ``fiscal_year``/``fiscal_period`` from ``v_canonical_facts`` even
    though its ``period_end`` belongs to the prior year. Recomputing from
    ``period_end`` keeps the labels self-consistent.

    Convention: ``fiscal_year`` is named for the calendar year in which it
    ends (PANW's fiscal year ending July 2025 is FY2025; Q2 of FY2025 ends
    Jan 2025).

    Args:
        period_end:    Calendar quarter-end date (e.g. ``2025-01-31``).
        fy_end_month:  Month in which the fiscal year ends (1–12).

    Returns:
        ``(fiscal_year, fiscal_period)`` tuple, e.g. ``(2025, "Q2")``.
    """
    ts = pd.to_datetime(period_end)
    months_before_fy_end = (fy_end_month - ts.month) % 12
    quarter = 4 - months_before_fy_end // 3
    fiscal_year = ts.year if ts.month <= fy_end_month else ts.year + 1
    return fiscal_year, f"Q{quarter}"


def _cash_flow_ytd_to_standalone(df: pd.DataFrame) -> pd.DataFrame:
    """Convert cumulative cash-flow duration facts into standalone quarters.

    SEC cash-flow statements usually report Q2 and Q3 facts as fiscal-year-to-date
    amounts, and those rows often have no quarter frame. Income-statement facts
    commonly include quarter-framed standalone rows, so duplicate pruning is
    enough there. For cash-flow duration metrics, explicitly difference each
    non-quarter-framed Q2/Q3/Q4 row against the previous fiscal quarter's raw
    cumulative value. Q1 remains unchanged because YTD equals standalone.

    Implementation is a self-merge on ``(ticker, line_item, fiscal_year, q-1)``:
    the prior-quarter raw value is attached as a vectorized ``_prev_raw`` column,
    and the new ``value`` is computed in a single ``out.loc[mask, "value"] = ...``
    assignment. Duplicate ``(group, quarter)`` rows are collapsed by
    ``groupby().max()`` for the baseline lookup, which is deterministic and
    sign-preserving (the upstream dedup pipeline normally strips duplicates
    before this function sees them; this branch only fires on synthetic or
    pathological inputs and emits a warning).
    """
    if df.empty:
        return df

    out = df.copy()
    frame_col = (
        out["frame"] if "frame" in out.columns else pd.Series([""] * len(out), index=out.index)
    )
    quarter_num = out["fiscal_period"].astype(str).str.extract(r"Q([1-4])", expand=False)
    out["_quarter_num"] = pd.to_numeric(quarter_num, errors="coerce")
    out["_is_quarter_framed"] = frame_col.fillna("").str.match(_QUARTER_FRAME_RE).fillna(False)
    out["_raw_value"] = out["value"]

    cumulative_mask = (
        out["line_item"].isin(_CUMULATIVE_CASH_FLOW_ITEMS)
        & out["period_type"].eq("Q")
        & out["_quarter_num"].notna()
    )
    if not cumulative_mask.any():
        return out.drop(columns=["_quarter_num", "_is_quarter_framed", "_raw_value"])

    cumulative = out.loc[cumulative_mask]
    group_keys = ["ticker", "line_item", "fiscal_year", "_quarter_num"]

    duplicates = (
        cumulative.loc[cumulative.duplicated(subset=group_keys, keep=False), group_keys]
        .drop_duplicates()
        .sort_values(group_keys)
    )
    for (_ticker, line_item, fiscal_year), grp in duplicates.groupby(
        ["ticker", "line_item", "fiscal_year"], sort=False
    ):
        quarters = ", ".join(f"Q{int(q)}" for q in grp["_quarter_num"].tolist())
        logger.warning(
            "  %s FY%s: duplicate cash-flow fiscal quarters in Tableau export: %s",
            line_item,
            fiscal_year,
            quarters,
        )

    baseline = (
        cumulative.groupby(group_keys, sort=False)["_raw_value"]
        .max()
        .rename("_prev_raw")
        .reset_index()
        .rename(columns={"_quarter_num": "_baseline_q"})
    )
    out["_baseline_q"] = out["_quarter_num"] - 1
    out = out.merge(
        baseline,
        on=["ticker", "line_item", "fiscal_year", "_baseline_q"],
        how="left",
    )

    update_mask = (
        out["line_item"].isin(_CUMULATIVE_CASH_FLOW_ITEMS)
        & out["period_type"].eq("Q")
        & out["_quarter_num"].notna()
        & (out["_quarter_num"] > 1)
        & (~out["_is_quarter_framed"].astype(bool))
    )
    has_baseline = update_mask & out["_prev_raw"].notna()
    out.loc[has_baseline, "value"] = (
        out.loc[has_baseline, "_raw_value"] - out.loc[has_baseline, "_prev_raw"]
    )

    missing = out.loc[
        update_mask & out["_prev_raw"].isna(), ["line_item", "fiscal_year", "_quarter_num"]
    ]
    for _, row in missing.iterrows():
        logger.warning(
            "  %s FY%s Q%d: cannot convert cumulative cash-flow row to standalone; "
            "missing prior quarter baseline",
            row["line_item"],
            row["fiscal_year"],
            int(row["_quarter_num"]),
        )

    return out.drop(
        columns=["_quarter_num", "_is_quarter_framed", "_raw_value", "_baseline_q", "_prev_raw"]
    )


def _derive_free_cash_flow(df: pd.DataFrame) -> pd.DataFrame:
    """Append derived FreeCashFlow rows (OperatingCashFlow − CapEx).

    Free cash flow is not a single XBRL concept; it is the standard SaaS
    definition computed from the standalone ``OperatingCashFlow`` and ``CapEx``
    rows of the *same* filing.  Each derived row inherits the OperatingCashFlow
    row's provenance (accession_no, filing_url, fiscal labels), so the "every
    fact row traces to an SEC filing" invariant still holds.  Periods missing
    either leg are skipped — no FCF row is fabricated without both inputs.
    """
    ocf = df[df["line_item"] == "OperatingCashFlow"]
    capex = df[df["line_item"] == "CapEx"][["ticker", "period_end", "value"]].rename(
        columns={"value": "_capex"}
    )
    if ocf.empty or capex.empty:
        return df

    merged = ocf.merge(capex, on=["ticker", "period_end"], how="inner")
    if merged.empty:
        return df

    fcf = merged.assign(
        line_item="FreeCashFlow",
        value=merged["value"] - merged["_capex"],
        concept_used=_FCF_DERIVED_CONCEPT,
        fact_id=merged["accession_no"]
        .astype(str)
        .str.cat(merged["period_end"].astype(str), sep=":FreeCashFlow:"),
    ).drop(columns=["_capex"])[df.columns]

    return pd.concat([df, fcf], ignore_index=True)


# ── Export functions ───────────────────────────────────────────────────────────


def _export_fact_financials(
    con: duckdb.DuckDBPyConnection,
    fy_end_month: int = 12,
) -> pd.DataFrame:
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
       filter and share the same fiscal triple but differ on ``frame`` — the
       standalone row carries a quarter-frame (``CY####Q#``) while the YTD row
       carries an empty frame.  The SQL QUALIFY breaks the
       form-priority/filed-date tie by preferring quarter-framed rows
       (``CASE WHEN frame LIKE 'CY%Q%' THEN 0 ELSE 1 END ASC``), which is
       semantic and sign-invariant — magnitude-based tiebreakers fail on any
       quarter where the standalone value crosses zero (e.g. a Q2 loss
       deeper than the YTD H1 loss because Q1 was a profit).  ``ABS(value)
       ASC`` remains as a last-resort tiebreaker for rows that share frame
       category.  The trailing pandas pass on (ticker, line_item, period_end,
       fiscal_year, fiscal_period) using the same frame-then-magnitude
       priority is kept as a defense-in-depth guard for shapes the SQL
       collapse can't see.

    3. **Comparative-row label inheritance.**  ``v_canonical_facts`` keeps
       whichever fiscal_year/fiscal_period was stamped on the surviving row.
       For comparative rows carried in a newer 10-Q, that's the *new*
       filing's labels — so a prior-year ``period_end`` (e.g. 2025-01-31)
       wrongly inherits the new filing's ``fiscal_year=2026, Q2``.  After
       the dedup we recompute both labels from ``period_end`` +
       ``fy_end_month`` via :func:`_calendar_to_fiscal_quarter`, so labels
       are always self-consistent with the calendar period.
    """
    # Pull from v_canonical_facts and collapse multi-fiscal-year comparatives
    # to one canonical row per (ticker, line_item, period_end) using form
    # priority + most-recent filed_date.  This is what makes the export
    # Tableau-safe: every (line_item, period_end) pair appears exactly once.
    #
    # Balance-sheet line items are reported as instantaneous facts; the 10-K
    # FY-end balance IS the Q4 closing balance (no separate Q4 10-K filing
    # for these).  Allow FY-period rows through the filter, but only for the
    # balance-sheet line items — flow items derive Q4 standalone by
    # subtraction (FY − Q1 − Q2 − Q3) elsewhere, so admitting FY rows for
    # them would surface the FY total at a Q4 period_end.
    balance_sheet_sql_list = ", ".join(f"'{li}'" for li in sorted(_BALANCE_SHEET_LINE_ITEMS))
    df = con.execute(f"""
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
            filed_date,
            frame
        FROM v_canonical_facts
        WHERE period_type = 'Q'
           OR (period_type = 'FY' AND line_item IN ({balance_sheet_sql_list}))
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
                filed_date DESC,
                CASE WHEN frame LIKE 'CY%Q%' THEN 0 ELSE 1 END ASC,
                ABS(value) ASC
        ) = 1
        ORDER BY line_item, fiscal_year, fiscal_period
    """).fetchdf()

    # Promote FY-period balance-sheet rows to period_type='Q'.  After this
    # relabel, the rest of the pipeline (which expects 'Q') treats them as
    # Q4 closing balances; the fiscal_period is recomputed from period_end
    # below and lands on 'Q4' for FY-end dates.  Defensive: only the
    # whitelist of balance-sheet line items can have period_type='FY' here
    # (the SQL filter guarantees it), but the explicit predicate keeps the
    # invariant readable.
    if len(df) > 0:
        fy_balance_mask = df["period_type"].eq("FY") & df["line_item"].isin(
            _BALANCE_SHEET_LINE_ITEMS
        )
        if fy_balance_mask.any():
            df.loc[fy_balance_mask, "period_type"] = "Q"

    # Drop YTD duplicates with frame-aware priority: prefer quarter-framed
    # rows (CY####Q#) over empty/year-framed rows, then fall back to smaller
    # absolute value.  Magnitude alone is unsafe — for a quarter where the
    # standalone value crosses zero (e.g. Q2 loss deeper than YTD H1 loss
    # because Q1 was a profit), the YTD row has the smaller absolute value
    # and would wrongly win.  Frame is a semantic, sign-invariant signal.
    dedup_key = ["ticker", "line_item", "period_end", "fiscal_year", "fiscal_period"]
    if len(df) > 0:
        n_before = len(df)
        frame_col = df["frame"] if "frame" in df.columns else pd.Series([""] * len(df))
        df["_frame_priority"] = (
            frame_col.fillna("").str.match(r"^CY\d+Q\d+$").map({True: 0, False: 1})
        )
        df["_abs_value"] = df["value"].abs()
        df = (
            df.sort_values(["_frame_priority", "_abs_value"])
            .drop_duplicates(subset=dedup_key, keep="first")
            .drop(columns=["_frame_priority", "_abs_value"])
        )
        n_removed = n_before - len(df)
        if n_removed > 0:
            logger.info(
                "  Removed %d YTD duplicate rows (kept standalone quarterly values)", n_removed
            )

    # Recompute fiscal_year/fiscal_period from period_end so comparative rows
    # carry their *own* fiscal labels rather than the labels stamped by the
    # newer filing that won the QUALIFY pick.  See dedup note (3).
    if len(df) > 0:
        fiscal_pairs = [_calendar_to_fiscal_quarter(pe, fy_end_month) for pe in df["period_end"]]
        df["fiscal_year"] = [fy for fy, _fp in fiscal_pairs]
        df["fiscal_period"] = [fp for _fy, fp in fiscal_pairs]

    df = _cash_flow_ytd_to_standalone(df)
    df = _derive_free_cash_flow(df)

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
            con.catalog.create_schema_if_not_exists("Extract")
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
                        period_end_ts = pd.to_datetime(
                            row.get("period_end"),
                            errors="coerce",
                        )
                        period_end = None if pd.isna(period_end_ts) else period_end_ts.date()
                        ins.add_row(
                            [
                                str(row.get("ticker", "")),
                                str(row.get("line_item", "")),
                                period_end,
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


_TABLEAU_SETUP_TEMPLATE = _DASHBOARD_DIR / "_Tableau_Setup_template.md"


def _write_tableau_setup_md(output_dir: Path, ticker: str) -> None:
    """Render Tableau_Setup.md from the checked-in template.

    The template at ``dashboard/_Tableau_Setup_template.md`` is the source of
    truth for this doc; this function reads it, substitutes ``{ticker}`` (the
    only allowed placeholder), and writes the rendered file to ``output_dir``.

    Why a template file instead of an embedded f-string: docs PRs that edit
    the rendered Tableau_Setup.md were silently overwritten on the next
    ``make dashboard`` run when the source lived inside this Python file.
    With the template extracted, docs edits are pure markdown PRs and the
    export script becomes a thin renderer.
    """
    template = _TABLEAU_SETUP_TEMPLATE.read_text(encoding="utf-8")
    content = template.format(ticker=ticker)
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
        df_fin = _export_fact_financials(con, fy_end_month=fy_end_month)
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
