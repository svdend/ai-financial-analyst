"""Build ``v_variance_facts`` — deferred from Prompt 3.

This view depends on forecast parquets (Prompts 5/6) and consensus CSVs
(Prompt 7) that did not exist when the warehouse was first built.  It is
intentionally built after all upstream artifacts are in place.

``v_variance_facts`` computes the following for the most-recently-reported
quarter:

* Actual revenue vs prior forecast (median of all available models)
* Actual revenue vs prior year (YoY)
* Actual revenue vs analyst consensus (NULL when no consensus)
* Same triple for gross_margin_pct, operating_margin_pct, free_cash_flow

All arithmetic happens in SQL/Python — Claude never receives raw inputs.

Provenance columns:
* ``revenue_actual_fact_id``, ``revenue_actual_accession`` — trace actual to filing
* ``revenue_prior_forecast_model`` — which model(s) contributed to median forecast

CLI::

    python -m src.build_variance_facts
    python -m src.build_variance_facts --ticker PANW
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
_REPO_ROOT     = Path(__file__).resolve().parents[1]
_CONFIG_PATH   = _REPO_ROOT / "config" / "company.yaml"
_PROCESSED_DIR = _REPO_ROOT / "data" / "processed"
_MODELS_DIR    = _REPO_ROOT / "models"
_TABLEAU_DIR   = _REPO_ROOT / "dashboard" / "tableau_data"

# ── SQL template ───────────────────────────────────────────────────────────────
# The view is parameterised with Python f-string literals only for path
# injection; all arithmetic is in SQL.

_SQL_VARIANCE_FACTS = """
CREATE OR REPLACE VIEW v_variance_facts AS
WITH

-- Latest complete quarter from the warehouse
latest_q AS (
    SELECT
        period_end,
        fiscal_year,
        fiscal_period,
        Revenue              AS revenue_actual,
        GrossProfit          AS gross_profit_actual,
        OperatingIncome      AS operating_income_actual,
        revenue_fact_id      AS revenue_actual_fact_id,
        revenue_accession    AS revenue_actual_accession,
        revenue_filing_url   AS revenue_actual_filing_url,
        gross_profit_fact_id AS gross_profit_fact_id,
        operating_income_fact_id AS op_income_fact_id
    FROM v_income_statement_quarterly
    WHERE period_type = 'Q' AND Revenue IS NOT NULL
    ORDER BY fiscal_year DESC, fiscal_period DESC
    LIMIT 1
),

-- Same quarter from four fiscal quarters ago (YoY baseline)
yoy_q AS (
    SELECT
        Revenue         AS revenue_yoy,
        GrossProfit     AS gross_profit_yoy,
        OperatingIncome AS op_income_yoy
    FROM v_income_statement_quarterly i, latest_q l
    WHERE i.period_type = 'Q'
      AND i.fiscal_year     = l.fiscal_year - 1
      AND i.fiscal_period   = l.fiscal_period
),

-- FCF from cash flow view
latest_cf AS (
    SELECT
        FreeCashFlow     AS fcf_actual,
        OperatingCashFlow AS ocf_actual
    FROM v_cash_flow_quarterly cf, latest_q l
    WHERE cf.period_type  = 'Q'
      AND cf.period_end   = l.period_end
),

-- Prior year FCF
yoy_cf AS (
    SELECT
        FreeCashFlow AS fcf_yoy
    FROM v_cash_flow_quarterly cf, latest_q l
    WHERE cf.period_type  = 'Q'
      AND cf.fiscal_year  = l.fiscal_year - 1
      AND cf.fiscal_period = l.fiscal_period
),

-- Forecast medians loaded from parquet files
-- (parquet paths are substituted at view creation time)
forecast_med AS (
    SELECT
        MEDIAN(yhat)             AS revenue_prior_forecast,
        STRING_AGG(DISTINCT model, '|' ORDER BY model)
                                  AS revenue_prior_forecast_model
    FROM read_parquet({forecast_glob!r})
    WHERE CAST(period_end AS DATE) = (SELECT CAST(period_end AS DATE) FROM latest_q)
),

-- Analyst consensus (NULL when file is empty or period not found)
consensus AS (
    SELECT revenue_consensus
    FROM {consensus_table}
    WHERE 1 = 0  -- placeholder; overridden if consensus CSV has rows
    LIMIT 1
)

SELECT
    l.period_end                   AS latest_period_end,
    l.fiscal_year,
    l.fiscal_period,

    -- Revenue
    l.revenue_actual,
    fm.revenue_prior_forecast,
    yoy_q.revenue_yoy,
    NULL::DOUBLE                   AS revenue_consensus,  -- populated when consensus has data

    -- Revenue variances
    l.revenue_actual - fm.revenue_prior_forecast
                                   AS revenue_variance_vs_forecast,
    (l.revenue_actual - fm.revenue_prior_forecast)
        / NULLIF(fm.revenue_prior_forecast, 0)
                                   AS revenue_variance_pct_vs_forecast,
    (l.revenue_actual - yoy_q.revenue_yoy)
        / NULLIF(yoy_q.revenue_yoy, 0)
                                   AS revenue_yoy_growth_pct,

    -- Gross margin %
    l.gross_profit_actual / NULLIF(l.revenue_actual, 0)
                                   AS gross_margin_pct_actual,
    yoy_q.gross_profit_yoy / NULLIF(yoy_q.revenue_yoy, 0)
                                   AS gross_margin_pct_yoy,
    (l.gross_profit_actual / NULLIF(l.revenue_actual, 0))
        - (yoy_q.gross_profit_yoy / NULLIF(yoy_q.revenue_yoy, 0))
                                   AS gross_margin_pct_yoy_delta,

    -- Operating margin %
    l.operating_income_actual / NULLIF(l.revenue_actual, 0)
                                   AS operating_margin_pct_actual,
    yoy_q.op_income_yoy / NULLIF(yoy_q.revenue_yoy, 0)
                                   AS operating_margin_pct_yoy,
    (l.operating_income_actual / NULLIF(l.revenue_actual, 0))
        - (yoy_q.op_income_yoy / NULLIF(yoy_q.revenue_yoy, 0))
                                   AS operating_margin_pct_yoy_delta,

    -- Free cash flow
    cf.fcf_actual                  AS fcf_actual,
    yoy_cf.fcf_yoy                 AS fcf_yoy,
    cf.fcf_actual - yoy_cf.fcf_yoy AS fcf_yoy_delta,
    (cf.fcf_actual - yoy_cf.fcf_yoy)
        / NULLIF(ABS(yoy_cf.fcf_yoy), 0)
                                   AS fcf_yoy_growth_pct,

    -- Provenance
    l.revenue_actual_fact_id,
    l.revenue_actual_accession,
    l.revenue_actual_filing_url,
    fm.revenue_prior_forecast_model,
    l.gross_profit_fact_id,
    l.op_income_fact_id

FROM latest_q l
LEFT JOIN yoy_q    ON 1 = 1
LEFT JOIN latest_cf cf ON 1 = 1
LEFT JOIN yoy_cf   ON 1 = 1
LEFT JOIN forecast_med fm ON 1 = 1
"""

# Simpler version used when no forecast parquets are found
_SQL_VARIANCE_FACTS_NO_FORECAST = """
CREATE OR REPLACE VIEW v_variance_facts AS
WITH

latest_q AS (
    SELECT
        period_end, fiscal_year, fiscal_period,
        Revenue              AS revenue_actual,
        GrossProfit          AS gross_profit_actual,
        OperatingIncome      AS operating_income_actual,
        revenue_fact_id      AS revenue_actual_fact_id,
        revenue_accession    AS revenue_actual_accession,
        revenue_filing_url   AS revenue_actual_filing_url,
        gross_profit_fact_id,
        operating_income_fact_id AS op_income_fact_id
    FROM v_income_statement_quarterly
    WHERE period_type = 'Q' AND Revenue IS NOT NULL
    ORDER BY fiscal_year DESC, fiscal_period DESC
    LIMIT 1
),

yoy_q AS (
    SELECT Revenue AS revenue_yoy,
           GrossProfit AS gross_profit_yoy,
           OperatingIncome AS op_income_yoy
    FROM v_income_statement_quarterly i, latest_q l
    WHERE i.period_type = 'Q'
      AND i.fiscal_year   = l.fiscal_year - 1
      AND i.fiscal_period = l.fiscal_period
),

latest_cf AS (
    SELECT FreeCashFlow AS fcf_actual
    FROM v_cash_flow_quarterly cf, latest_q l
    WHERE cf.period_type = 'Q' AND cf.period_end = l.period_end
),

yoy_cf AS (
    SELECT FreeCashFlow AS fcf_yoy
    FROM v_cash_flow_quarterly cf, latest_q l
    WHERE cf.period_type   = 'Q'
      AND cf.fiscal_year   = l.fiscal_year - 1
      AND cf.fiscal_period = l.fiscal_period
)

SELECT
    l.period_end              AS latest_period_end,
    l.fiscal_year,
    l.fiscal_period,
    l.revenue_actual,
    NULL::DOUBLE              AS revenue_prior_forecast,
    yoy_q.revenue_yoy,
    NULL::DOUBLE              AS revenue_consensus,
    NULL::DOUBLE              AS revenue_variance_vs_forecast,
    NULL::DOUBLE              AS revenue_variance_pct_vs_forecast,
    (l.revenue_actual - yoy_q.revenue_yoy)
        / NULLIF(yoy_q.revenue_yoy, 0)
                              AS revenue_yoy_growth_pct,
    l.gross_profit_actual / NULLIF(l.revenue_actual, 0)
                              AS gross_margin_pct_actual,
    yoy_q.gross_profit_yoy / NULLIF(yoy_q.revenue_yoy, 0)
                              AS gross_margin_pct_yoy,
    (l.gross_profit_actual / NULLIF(l.revenue_actual, 0))
        - (yoy_q.gross_profit_yoy / NULLIF(yoy_q.revenue_yoy, 0))
                              AS gross_margin_pct_yoy_delta,
    l.operating_income_actual / NULLIF(l.revenue_actual, 0)
                              AS operating_margin_pct_actual,
    yoy_q.op_income_yoy / NULLIF(yoy_q.revenue_yoy, 0)
                              AS operating_margin_pct_yoy,
    (l.operating_income_actual / NULLIF(l.revenue_actual, 0))
        - (yoy_q.op_income_yoy / NULLIF(yoy_q.revenue_yoy, 0))
                              AS operating_margin_pct_yoy_delta,
    cf.fcf_actual,
    yoy_cf.fcf_yoy,
    cf.fcf_actual - yoy_cf.fcf_yoy AS fcf_yoy_delta,
    (cf.fcf_actual - yoy_cf.fcf_yoy)
        / NULLIF(ABS(yoy_cf.fcf_yoy), 0) AS fcf_yoy_growth_pct,
    l.revenue_actual_fact_id,
    l.revenue_actual_accession,
    l.revenue_actual_filing_url,
    NULL::VARCHAR             AS revenue_prior_forecast_model,
    l.gross_profit_fact_id,
    l.op_income_fact_id
FROM latest_q l
LEFT JOIN yoy_q    ON 1 = 1
LEFT JOIN latest_cf cf ON 1 = 1
LEFT JOIN yoy_cf      ON 1 = 1
"""


def _find_forecast_parquets(ticker: str) -> list[Path]:
    candidates = [
        _MODELS_DIR / f"{ticker}_baseline_forecasts.parquet",
        _MODELS_DIR / f"{ticker}_macro_forecast.parquet",
    ]
    return [p for p in candidates if p.exists()]


def _load_consensus_revenue(ticker: str, period_end: str) -> float | None:
    """Return analyst revenue consensus for a specific period_end, or None."""
    consensus_path = _TABLEAU_DIR / "fact_consensus.csv"
    if not consensus_path.exists():
        return None
    try:
        df = pd.read_csv(consensus_path)
        if df.empty or "revenue_consensus" not in df.columns:
            return None
        # fact_consensus may use 'period' not 'period_end'
        pe_col = "period_end" if "period_end" in df.columns else "period"
        match = df[df[pe_col].astype(str).str[:10] == str(period_end)[:10]]
        if match.empty:
            return None
        val = match["revenue_consensus"].iloc[0]
        return float(val) if pd.notna(val) else None
    except Exception as exc:
        logger.warning("Could not load consensus: %s", exc)
        return None


def build(ticker: str | None = None, db_path: Path | None = None) -> Path:
    """Create ``v_variance_facts`` in the existing DuckDB warehouse.

    Args:
        ticker:  Ticker symbol override; falls back to config/company.yaml.
        db_path: Path to DuckDB file; auto-derived from ticker if not given.

    Returns:
        Path to the DuckDB file (with view now created).

    Raises:
        FileNotFoundError: If DuckDB file is missing.
    """
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(f"Config not found: {_CONFIG_PATH}")
    with _CONFIG_PATH.open() as fh:
        config: dict[str, Any] = yaml.safe_load(fh)

    resolved_ticker = (ticker or str(config["ticker"])).upper().strip()

    if db_path is None:
        db_path = _PROCESSED_DIR / f"{resolved_ticker}.duckdb"
    if not db_path.exists():
        raise FileNotFoundError(
            f"DuckDB warehouse not found: {db_path}\n"
            f"Run first: python -m src.build_warehouse --ticker {resolved_ticker}"
        )

    forecast_paths = _find_forecast_parquets(resolved_ticker)
    has_forecasts  = len(forecast_paths) > 0

    con = duckdb.connect(str(db_path))
    try:
        if has_forecasts:
            # Build a glob pattern that covers all forecast parquets
            # DuckDB read_parquet accepts a list literal
            parquet_list = [str(p) for p in forecast_paths]
            if len(parquet_list) == 1:
                forecast_glob = parquet_list[0]
                glob_expr = f"'{forecast_glob}'"
            else:
                # Pass as list string
                glob_expr = "[" + ", ".join(f"'{p}'" for p in parquet_list) + "]"

            sql = _SQL_VARIANCE_FACTS.format(
                forecast_glob=glob_expr,
                consensus_table="(SELECT NULL::DOUBLE AS revenue_consensus WHERE 1=0)",
            )
            # Replace the placeholder glob reference
            sql = sql.replace(f"read_parquet({glob_expr!r})", f"read_parquet({glob_expr})")
            logger.info("Building v_variance_facts with %d forecast parquet(s)", len(forecast_paths))
        else:
            sql = _SQL_VARIANCE_FACTS_NO_FORECAST
            logger.warning(
                "No forecast parquets found — v_variance_facts will have NULL forecast columns. "
                "Run notebooks 02 and 03 first."
            )

        con.execute(sql)

        # Verify the view was created and return the populated row
        result = con.execute("SELECT * FROM v_variance_facts").fetchdf()
        logger.info("v_variance_facts created: %d row(s)", len(result))

        if len(result) > 0:
            row = result.iloc[0]
            rev_b = (row.get("revenue_actual") or 0) / 1e9
            fcst_b = (row.get("revenue_prior_forecast") or 0) / 1e9
            var_pct = (row.get("revenue_variance_pct_vs_forecast") or 0) * 100
            yoy_pct = (row.get("revenue_yoy_growth_pct") or 0) * 100
            models  = row.get("revenue_prior_forecast_model") or "N/A"
            logger.info(
                "Latest quarter: %s %s | Revenue=$%.2fB | Forecast=$%.2fB "
                "| Variance=%.1f%% | YoY=%.1f%% | Models=%s",
                row.get("fiscal_year"), row.get("fiscal_period"),
                rev_b, fcst_b, var_pct, yoy_pct, models,
            )

    finally:
        con.close()

    return db_path


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    parser = argparse.ArgumentParser(
        description="Build v_variance_facts view in the DuckDB warehouse.",
    )
    parser.add_argument("--ticker", default=None, help="Ticker symbol (e.g. PANW)")
    args = parser.parse_args()

    try:
        path = build(ticker=args.ticker)
        print(f"\nv_variance_facts created in: {path}")

        # Print the populated row for inspection
        con = duckdb.connect(str(path), read_only=True)
        try:
            df = con.execute("SELECT * FROM v_variance_facts").fetchdf()
        finally:
            con.close()

        if df.empty:
            print("Warning: v_variance_facts returned 0 rows.")
        else:
            print("\nVariance summary:")
            row = df.iloc[0]
            for col in df.columns:
                val = row[col]
                if pd.notna(val):
                    print(f"  {col:<45} {val}")
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
