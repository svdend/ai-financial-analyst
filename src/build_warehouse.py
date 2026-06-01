"""DuckDB analytics warehouse with provenance-aware views.

Loads the ``{ticker}_financials.parquet`` produced by :mod:`src.ingest_edgar`
into a local DuckDB file and creates six analytical views:

* ``v_canonical_facts``       — deduplicated facts, best filing wins per period
* ``v_income_statement_quarterly`` — IS pivoted wide with provenance triples
* ``v_balance_sheet_quarterly``    — BS pivoted wide with provenance triples
* ``v_cash_flow_quarterly``        — CF pivoted wide with provenance triples
* ``v_key_metrics``                — margins, YoY/QoQ growth rates, FCF
* ``v_restatement_details``        — rows where a /A filing materially restates
* ``v_missing_coverage``           — (line_item, quarter) combinations with no data
* ``v_data_quality``               — one-row summary: has_physical_inventory,
                                     has_restatement, missing_quarters, ...

``v_variance_facts`` is intentionally deferred to Prompt 7.5 — it depends on
forecast parquets that do not exist until Prompts 5/6.

CLI::

    python -m src.build_warehouse
    python -m src.build_warehouse --ticker CRWD
    python -m src.build_warehouse --rebuild                  # prompts before destroying
    python -m src.build_warehouse --rebuild --force          # skips prompt
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import duckdb
import yaml

logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parents[1]
_PROCESSED_DIR = _REPO_ROOT / "data" / "processed"
_CONFIG_PATH = _REPO_ROOT / "config" / "company.yaml"

# ── SQL: canonical fact selection ─────────────────────────────────────────────
# Priority order for form_type: 10-K/A (4) > 10-Q/A (3) > 10-K (2) > 10-Q (1) > other (0).
# Within the same form priority, the most-recently-filed entry wins.
# Routine 10-Q → 10-K finalization is NOT a restatement — different form types for
# the same period silently coexist and the 10-K supersedes.

_SQL_CANONICAL = """
CREATE OR REPLACE VIEW v_canonical_facts AS
SELECT
    ticker, line_item, concept_used, period_end, period_type,
    fiscal_year, fiscal_period, value, unit,
    accession_no, fact_id, filing_url, form_type, filed_date, frame
FROM (
    SELECT *,
        CASE form_type
            WHEN '10-K/A' THEN 4
            WHEN '10-Q/A' THEN 3
            WHEN '10-K'   THEN 2
            WHEN '10-Q'   THEN 1
            ELSE 0
        END AS _form_priority
    FROM raw_financials
)
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY line_item, period_end, period_type, frame
    ORDER BY _form_priority DESC, filed_date DESC
) = 1
"""

# ── SQL: income statement (quarterly + annual) ────────────────────────────────

_SQL_INCOME_STATEMENT = """
CREATE OR REPLACE VIEW v_income_statement_quarterly AS
SELECT
    period_end,
    fiscal_year,
    fiscal_period,
    period_type,
    -- Revenue
    MAX(CASE WHEN line_item = 'Revenue' THEN value        END) AS Revenue,
    MAX(CASE WHEN line_item = 'Revenue' THEN fact_id      END) AS revenue_fact_id,
    MAX(CASE WHEN line_item = 'Revenue' THEN accession_no END) AS revenue_accession,
    MAX(CASE WHEN line_item = 'Revenue' THEN filing_url   END) AS revenue_filing_url,
    -- CostOfRevenue
    MAX(CASE WHEN line_item = 'CostOfRevenue' THEN value        END) AS CostOfRevenue,
    MAX(CASE WHEN line_item = 'CostOfRevenue' THEN fact_id      END) AS cost_of_revenue_fact_id,
    MAX(CASE WHEN line_item = 'CostOfRevenue' THEN accession_no END) AS cost_of_revenue_accession,
    MAX(CASE WHEN line_item = 'CostOfRevenue' THEN filing_url   END) AS cost_of_revenue_filing_url,
    -- GrossProfit
    MAX(CASE WHEN line_item = 'GrossProfit' THEN value        END) AS GrossProfit,
    MAX(CASE WHEN line_item = 'GrossProfit' THEN fact_id      END) AS gross_profit_fact_id,
    MAX(CASE WHEN line_item = 'GrossProfit' THEN accession_no END) AS gross_profit_accession,
    MAX(CASE WHEN line_item = 'GrossProfit' THEN filing_url   END) AS gross_profit_filing_url,
    -- OperatingExpenses
    MAX(CASE WHEN line_item = 'OperatingExpenses' THEN value        END) AS OperatingExpenses,
    MAX(CASE WHEN line_item = 'OperatingExpenses' THEN fact_id      END) AS opex_fact_id,
    MAX(CASE WHEN line_item = 'OperatingExpenses' THEN accession_no END) AS opex_accession,
    MAX(CASE WHEN line_item = 'OperatingExpenses' THEN filing_url   END) AS opex_filing_url,
    -- OperatingIncome
    MAX(CASE WHEN line_item = 'OperatingIncome' THEN value        END) AS OperatingIncome,
    MAX(CASE WHEN line_item = 'OperatingIncome' THEN fact_id      END) AS operating_income_fact_id,
    MAX(CASE WHEN line_item = 'OperatingIncome' THEN accession_no END) AS operating_income_accession,
    MAX(CASE WHEN line_item = 'OperatingIncome' THEN filing_url   END) AS operating_income_filing_url,
    -- NetIncome
    MAX(CASE WHEN line_item = 'NetIncome' THEN value        END) AS NetIncome,
    MAX(CASE WHEN line_item = 'NetIncome' THEN fact_id      END) AS net_income_fact_id,
    MAX(CASE WHEN line_item = 'NetIncome' THEN accession_no END) AS net_income_accession,
    MAX(CASE WHEN line_item = 'NetIncome' THEN filing_url   END) AS net_income_filing_url
FROM v_canonical_facts
GROUP BY period_end, fiscal_year, fiscal_period, period_type
ORDER BY fiscal_year, fiscal_period
"""

# ── SQL: balance sheet ────────────────────────────────────────────────────────

_SQL_BALANCE_SHEET = """
CREATE OR REPLACE VIEW v_balance_sheet_quarterly AS
SELECT
    period_end,
    fiscal_year,
    fiscal_period,
    period_type,
    -- Cash
    MAX(CASE WHEN line_item = 'Cash' THEN value        END) AS Cash,
    MAX(CASE WHEN line_item = 'Cash' THEN fact_id      END) AS cash_fact_id,
    MAX(CASE WHEN line_item = 'Cash' THEN accession_no END) AS cash_accession,
    MAX(CASE WHEN line_item = 'Cash' THEN filing_url   END) AS cash_filing_url,
    -- AccountsReceivable
    MAX(CASE WHEN line_item = 'AccountsReceivable' THEN value        END) AS AccountsReceivable,
    MAX(CASE WHEN line_item = 'AccountsReceivable' THEN fact_id      END) AS ar_fact_id,
    MAX(CASE WHEN line_item = 'AccountsReceivable' THEN accession_no END) AS ar_accession,
    MAX(CASE WHEN line_item = 'AccountsReceivable' THEN filing_url   END) AS ar_filing_url,
    -- Inventory (NULL for pure-SaaS; has_physical_inventory flag derived from this)
    MAX(CASE WHEN line_item = 'Inventory' THEN value        END) AS Inventory,
    MAX(CASE WHEN line_item = 'Inventory' THEN fact_id      END) AS inventory_fact_id,
    MAX(CASE WHEN line_item = 'Inventory' THEN accession_no END) AS inventory_accession,
    MAX(CASE WHEN line_item = 'Inventory' THEN filing_url   END) AS inventory_filing_url,
    -- AccountsPayable
    MAX(CASE WHEN line_item = 'AccountsPayable' THEN value        END) AS AccountsPayable,
    MAX(CASE WHEN line_item = 'AccountsPayable' THEN fact_id      END) AS ap_fact_id,
    MAX(CASE WHEN line_item = 'AccountsPayable' THEN accession_no END) AS ap_accession,
    MAX(CASE WHEN line_item = 'AccountsPayable' THEN filing_url   END) AS ap_filing_url,
    -- DeferredRevenue
    MAX(CASE WHEN line_item = 'DeferredRevenue' THEN value        END) AS DeferredRevenue,
    MAX(CASE WHEN line_item = 'DeferredRevenue' THEN fact_id      END) AS deferred_rev_fact_id,
    MAX(CASE WHEN line_item = 'DeferredRevenue' THEN accession_no END) AS deferred_rev_accession,
    MAX(CASE WHEN line_item = 'DeferredRevenue' THEN filing_url   END) AS deferred_rev_filing_url,
    -- TotalAssets
    MAX(CASE WHEN line_item = 'TotalAssets' THEN value        END) AS TotalAssets,
    MAX(CASE WHEN line_item = 'TotalAssets' THEN fact_id      END) AS total_assets_fact_id,
    MAX(CASE WHEN line_item = 'TotalAssets' THEN accession_no END) AS total_assets_accession,
    MAX(CASE WHEN line_item = 'TotalAssets' THEN filing_url   END) AS total_assets_filing_url,
    -- TotalLiabilities
    MAX(CASE WHEN line_item = 'TotalLiabilities' THEN value        END) AS TotalLiabilities,
    MAX(CASE WHEN line_item = 'TotalLiabilities' THEN fact_id      END) AS total_liab_fact_id,
    MAX(CASE WHEN line_item = 'TotalLiabilities' THEN accession_no END) AS total_liab_accession,
    MAX(CASE WHEN line_item = 'TotalLiabilities' THEN filing_url   END) AS total_liab_filing_url,
    -- TotalEquity
    MAX(CASE WHEN line_item = 'TotalEquity' THEN value        END) AS TotalEquity,
    MAX(CASE WHEN line_item = 'TotalEquity' THEN fact_id      END) AS total_equity_fact_id,
    MAX(CASE WHEN line_item = 'TotalEquity' THEN accession_no END) AS total_equity_accession,
    MAX(CASE WHEN line_item = 'TotalEquity' THEN filing_url   END) AS total_equity_filing_url
FROM v_canonical_facts
GROUP BY period_end, fiscal_year, fiscal_period, period_type
ORDER BY fiscal_year, fiscal_period
"""

# ── SQL: cash flow statement ──────────────────────────────────────────────────

_SQL_CASH_FLOW = """
CREATE OR REPLACE VIEW v_cash_flow_quarterly AS
SELECT
    period_end,
    fiscal_year,
    fiscal_period,
    period_type,
    -- OperatingCashFlow
    MAX(CASE WHEN line_item = 'OperatingCashFlow' THEN value        END) AS OperatingCashFlow,
    MAX(CASE WHEN line_item = 'OperatingCashFlow' THEN fact_id      END) AS ocf_fact_id,
    MAX(CASE WHEN line_item = 'OperatingCashFlow' THEN accession_no END) AS ocf_accession,
    MAX(CASE WHEN line_item = 'OperatingCashFlow' THEN filing_url   END) AS ocf_filing_url,
    -- InvestingCashFlow
    MAX(CASE WHEN line_item = 'InvestingCashFlow' THEN value        END) AS InvestingCashFlow,
    MAX(CASE WHEN line_item = 'InvestingCashFlow' THEN fact_id      END) AS icf_fact_id,
    MAX(CASE WHEN line_item = 'InvestingCashFlow' THEN accession_no END) AS icf_accession,
    MAX(CASE WHEN line_item = 'InvestingCashFlow' THEN filing_url   END) AS icf_filing_url,
    -- FinancingCashFlow
    MAX(CASE WHEN line_item = 'FinancingCashFlow' THEN value        END) AS FinancingCashFlow,
    MAX(CASE WHEN line_item = 'FinancingCashFlow' THEN fact_id      END) AS fcf_fact_id,
    MAX(CASE WHEN line_item = 'FinancingCashFlow' THEN accession_no END) AS fcf_accession,
    MAX(CASE WHEN line_item = 'FinancingCashFlow' THEN filing_url   END) AS fcf_filing_url,
    -- CapEx
    MAX(CASE WHEN line_item = 'CapEx' THEN value        END) AS CapEx,
    MAX(CASE WHEN line_item = 'CapEx' THEN fact_id      END) AS capex_fact_id,
    MAX(CASE WHEN line_item = 'CapEx' THEN accession_no END) AS capex_accession,
    MAX(CASE WHEN line_item = 'CapEx' THEN filing_url   END) AS capex_filing_url,
    -- Depreciation
    MAX(CASE WHEN line_item = 'Depreciation' THEN value        END) AS Depreciation,
    MAX(CASE WHEN line_item = 'Depreciation' THEN fact_id      END) AS depreciation_fact_id,
    MAX(CASE WHEN line_item = 'Depreciation' THEN accession_no END) AS depreciation_accession,
    MAX(CASE WHEN line_item = 'Depreciation' THEN filing_url   END) AS depreciation_filing_url,
    -- StockBasedCompensation
    MAX(CASE WHEN line_item = 'StockBasedCompensation' THEN value        END) AS StockBasedCompensation,
    MAX(CASE WHEN line_item = 'StockBasedCompensation' THEN fact_id      END) AS sbc_fact_id,
    MAX(CASE WHEN line_item = 'StockBasedCompensation' THEN accession_no END) AS sbc_accession,
    MAX(CASE WHEN line_item = 'StockBasedCompensation' THEN filing_url   END) AS sbc_filing_url,
    -- TreasuryStockRepurchases
    MAX(CASE WHEN line_item = 'TreasuryStockRepurchases' THEN value        END) AS TreasuryStockRepurchases,
    MAX(CASE WHEN line_item = 'TreasuryStockRepurchases' THEN fact_id      END) AS buybacks_fact_id,
    MAX(CASE WHEN line_item = 'TreasuryStockRepurchases' THEN accession_no END) AS buybacks_accession,
    MAX(CASE WHEN line_item = 'TreasuryStockRepurchases' THEN filing_url   END) AS buybacks_filing_url,
    -- FreeCashFlow: computed (OCF - CapEx); NULL when either component is missing
    MAX(CASE WHEN line_item = 'OperatingCashFlow'  THEN value END) -
    MAX(CASE WHEN line_item = 'CapEx'              THEN value END) AS FreeCashFlow
FROM v_canonical_facts
GROUP BY period_end, fiscal_year, fiscal_period, period_type
ORDER BY fiscal_year, fiscal_period
"""

# ── SQL: FCF waterfall bridge ─────────────────────────────────────────────────
# One row per (period_end, component) for the Net Income → FCF waterfall.
# The four "additive" components (NI, D&A, SBC, ΔWorking-Capital-and-Other)
# sum exactly to OperatingCashFlow by construction — the WorkingCapitalAndOther
# bar is computed as the residual OCF − NI − D&A − SBC, which absorbs both
# real working-capital movements and any other non-cash reconciliation items
# (asset impairments, deferred-tax adjustments, etc.) without requiring us to
# ingest the full XBRL working-capital reconciliation. CapEx is sign-flipped
# (cash *out*) so the running total walks down to FreeCashFlow.
#
# Quarters missing any of the five filed components (NI, D&A, SBC, OCF, CapEx)
# are excluded outright — partial bridges are misleading. v_missing_coverage
# already surfaces the underlying gap.
#
# Schema:
#   ticker, period_end, fiscal_year, fiscal_period, period_type,
#   component, component_order, component_role, value,
#   accession_no, fact_id, filing_url
# component_role ∈ {start, add, subtract, subtotal, end}; the plug carries
# accession_no=NULL because no single filing sources it.

_SQL_FCF_BRIDGE = """
CREATE OR REPLACE VIEW v_fcf_bridge AS
WITH q AS (
    SELECT
        ticker,
        period_end,
        fiscal_year,
        fiscal_period,
        period_type,
        MAX(CASE WHEN line_item = 'NetIncome'              THEN value END)        AS NetIncome,
        MAX(CASE WHEN line_item = 'NetIncome'              THEN accession_no END) AS ni_accn,
        MAX(CASE WHEN line_item = 'NetIncome'              THEN fact_id END)      AS ni_fact_id,
        MAX(CASE WHEN line_item = 'NetIncome'              THEN filing_url END)   AS ni_url,
        MAX(CASE WHEN line_item = 'Depreciation'           THEN value END)        AS Depreciation,
        MAX(CASE WHEN line_item = 'Depreciation'           THEN accession_no END) AS da_accn,
        MAX(CASE WHEN line_item = 'Depreciation'           THEN fact_id END)      AS da_fact_id,
        MAX(CASE WHEN line_item = 'Depreciation'           THEN filing_url END)   AS da_url,
        MAX(CASE WHEN line_item = 'StockBasedCompensation' THEN value END)        AS SBC,
        MAX(CASE WHEN line_item = 'StockBasedCompensation' THEN accession_no END) AS sbc_accn,
        MAX(CASE WHEN line_item = 'StockBasedCompensation' THEN fact_id END)      AS sbc_fact_id,
        MAX(CASE WHEN line_item = 'StockBasedCompensation' THEN filing_url END)   AS sbc_url,
        MAX(CASE WHEN line_item = 'OperatingCashFlow'      THEN value END)        AS OCF,
        MAX(CASE WHEN line_item = 'OperatingCashFlow'      THEN accession_no END) AS ocf_accn,
        MAX(CASE WHEN line_item = 'OperatingCashFlow'      THEN fact_id END)      AS ocf_fact_id,
        MAX(CASE WHEN line_item = 'OperatingCashFlow'      THEN filing_url END)   AS ocf_url,
        MAX(CASE WHEN line_item = 'CapEx'                  THEN value END)        AS CapEx,
        MAX(CASE WHEN line_item = 'CapEx'                  THEN accession_no END) AS capex_accn,
        MAX(CASE WHEN line_item = 'CapEx'                  THEN fact_id END)      AS capex_fact_id,
        MAX(CASE WHEN line_item = 'CapEx'                  THEN filing_url END)   AS capex_url
    FROM v_canonical_facts
    WHERE period_type = 'Q'
    GROUP BY ticker, period_end, fiscal_year, fiscal_period, period_type
),
complete AS (
    -- Skip quarters missing any filed component — partial bridges mislead.
    SELECT * FROM q
    WHERE NetIncome     IS NOT NULL
      AND Depreciation  IS NOT NULL
      AND SBC           IS NOT NULL
      AND OCF           IS NOT NULL
      AND CapEx         IS NOT NULL
)
SELECT ticker, period_end, fiscal_year, fiscal_period, period_type,
       'NetIncome'              AS component, 1 AS component_order, 'start'    AS component_role,
       NetIncome                AS value, ni_accn AS accession_no, ni_fact_id AS fact_id, ni_url AS filing_url
FROM complete
UNION ALL
SELECT ticker, period_end, fiscal_year, fiscal_period, period_type,
       'Depreciation'           AS component, 2, 'add',
       Depreciation, da_accn, da_fact_id, da_url
FROM complete
UNION ALL
SELECT ticker, period_end, fiscal_year, fiscal_period, period_type,
       'StockBasedCompensation' AS component, 3, 'add',
       SBC, sbc_accn, sbc_fact_id, sbc_url
FROM complete
UNION ALL
SELECT ticker, period_end, fiscal_year, fiscal_period, period_type,
       'WorkingCapitalAndOther' AS component, 4, 'add',
       OCF - NetIncome - Depreciation - SBC AS value,
       NULL AS accession_no, NULL AS fact_id, NULL AS filing_url
FROM complete
UNION ALL
SELECT ticker, period_end, fiscal_year, fiscal_period, period_type,
       'OperatingCashFlow'      AS component, 5, 'subtotal',
       OCF, ocf_accn, ocf_fact_id, ocf_url
FROM complete
UNION ALL
SELECT ticker, period_end, fiscal_year, fiscal_period, period_type,
       'CapEx'                  AS component, 6, 'subtract',
       -CapEx AS value, capex_accn, capex_fact_id, capex_url
FROM complete
UNION ALL
SELECT ticker, period_end, fiscal_year, fiscal_period, period_type,
       'FreeCashFlow'           AS component, 7, 'end',
       OCF - CapEx AS value, ocf_accn, ocf_fact_id, ocf_url
FROM complete
ORDER BY period_end, component_order
"""

# ── SQL: key metrics ──────────────────────────────────────────────────────────
# Quarterly view only (period_type = 'Q').
# YoY growth uses LAG(4) over ordered quarters; QoQ uses LAG(1).
# Window ordering on (fiscal_year, fiscal_period) works because fiscal_period
# values Q1/Q2/Q3/Q4 sort alphabetically in the correct chronological order.

_SQL_KEY_METRICS = """
CREATE OR REPLACE VIEW v_key_metrics AS
SELECT
    q.period_end,
    q.period_type,
    q.fiscal_year,
    q.fiscal_period,
    q.Revenue,
    q.GrossProfit,
    q.OperatingIncome,
    q.NetIncome,
    cf.OperatingCashFlow,
    cf.CapEx,
    cf.FreeCashFlow,
    -- Margins (NULL when denominator is zero or missing)
    q.GrossProfit     / NULLIF(q.Revenue, 0) AS gross_margin_pct,
    q.OperatingIncome / NULLIF(q.Revenue, 0) AS operating_margin_pct,
    q.NetIncome       / NULLIF(q.Revenue, 0) AS net_margin_pct,
    cf.FreeCashFlow   / NULLIF(q.Revenue, 0) AS fcf_margin_pct,
    -- QoQ revenue growth
    (q.Revenue - LAG(q.Revenue)    OVER w) / NULLIF(LAG(q.Revenue)    OVER w, 0)
        AS revenue_qoq_growth,
    -- YoY revenue growth (same quarter prior year)
    (q.Revenue - LAG(q.Revenue, 4) OVER w) / NULLIF(LAG(q.Revenue, 4) OVER w, 0)
        AS revenue_yoy_growth
FROM v_income_statement_quarterly q
LEFT JOIN v_cash_flow_quarterly cf
    ON  q.period_end     = cf.period_end
    AND q.fiscal_year    = cf.fiscal_year
    AND q.fiscal_period  = cf.fiscal_period
WHERE q.period_type = 'Q'
WINDOW w AS (ORDER BY q.fiscal_year, q.fiscal_period)
ORDER BY q.fiscal_year, q.fiscal_period
"""

# ── SQL: restatement detection ────────────────────────────────────────────────
# A restatement is defined STRICTLY as a /A amendment (10-K/A or 10-Q/A) whose
# value materially differs from the most-recent prior same-base-form filing
# (10-K/A vs 10-K; 10-Q/A vs 10-Q).
#
# NOT a restatement:
#   - Routine 10-Q preliminary → 10-K final (different form types, same period)
#   - Floating-point noise (threshold: >$10,000 absolute OR >0.1% relative)
#   - Entries with different `frame` values for the same period_end (these are
#     different facts: instantaneous BS vs duration IS)

_SQL_RESTATEMENT_DETAILS = """
CREATE OR REPLACE VIEW v_restatement_details AS
WITH prio AS (
    SELECT *,
        regexp_replace(form_type, '/A$', '') AS _base_form
    FROM raw_financials
),
amendments AS (SELECT * FROM prio WHERE form_type LIKE '%/A'),
originals  AS (SELECT * FROM prio WHERE form_type NOT LIKE '%/A')
SELECT
    a.line_item,
    a.period_end,
    a.period_type,
    a.frame,
    a.fiscal_year,
    a.fiscal_period,
    a.value          AS amended_value,
    a.accession_no   AS amending_accession_no,
    a.filed_date     AS amendment_date,
    o.value          AS original_value,
    o.accession_no   AS original_accession_no,
    o.filed_date     AS original_date,
    ABS(a.value - o.value)                              AS abs_diff,
    ABS(a.value - o.value) / NULLIF(ABS(o.value), 0)   AS rel_diff
FROM amendments a
JOIN originals o
    ON  a.line_item    = o.line_item
    AND a.period_end   = o.period_end
    AND a.period_type  = o.period_type
    AND a.frame        = o.frame
    AND a._base_form   = o.form_type
    AND o.filed_date   < a.filed_date
WHERE
    ABS(a.value - o.value) > 10000
    OR ABS(a.value - o.value) / NULLIF(ABS(o.value), 0) > 0.001
QUALIFY ROW_NUMBER() OVER (
    -- For each amendment, compare against the most-recent original
    PARTITION BY a.line_item, a.period_end, a.period_type, a.frame, a.accession_no
    ORDER BY o.filed_date DESC
) = 1
"""

# ── SQL: missing coverage ─────────────────────────────────────────────────────

_SQL_MISSING_COVERAGE = """
CREATE OR REPLACE VIEW v_missing_coverage AS
WITH all_quarters AS (
    SELECT DISTINCT fiscal_year, fiscal_period, period_end
    FROM raw_financials
    WHERE period_type = 'Q'
),
all_line_items AS (
    SELECT DISTINCT line_item FROM raw_financials
),
expected AS (
    SELECT q.fiscal_year, q.fiscal_period, q.period_end, li.line_item
    FROM all_quarters q CROSS JOIN all_line_items li
),
actual AS (
    SELECT line_item, fiscal_year, fiscal_period, period_end
    FROM raw_financials
    WHERE period_type = 'Q'
    GROUP BY line_item, fiscal_year, fiscal_period, period_end
)
SELECT e.line_item, e.fiscal_year, e.fiscal_period, e.period_end
FROM expected e
LEFT JOIN actual a
    USING (line_item, fiscal_year, fiscal_period, period_end)
WHERE a.line_item IS NULL
ORDER BY e.line_item, e.fiscal_year, e.fiscal_period
"""

# ── SQL: data quality summary ─────────────────────────────────────────────────
# One-row view with scalar flags.  Used by downstream steps to decide whether
# to proceed (e.g. generate_commentary.py refuses if has_restatement is TRUE).

_SQL_DATA_QUALITY = """
CREATE OR REPLACE VIEW v_data_quality AS
SELECT
    (SELECT COUNT(*)                 FROM raw_financials)                              AS total_facts,
    (SELECT COUNT(DISTINCT line_item) FROM raw_financials)                             AS distinct_line_items,
    (SELECT COUNT(DISTINCT fiscal_year) FROM raw_financials)                           AS distinct_fiscal_years,
    (SELECT MIN(fiscal_year)         FROM raw_financials)                              AS min_fiscal_year,
    (SELECT MAX(fiscal_year)         FROM raw_financials)                              AS max_fiscal_year,
    (SELECT COUNT(*) > 0 FROM raw_financials
     WHERE line_item = 'Inventory' AND value IS NOT NULL)                              AS has_physical_inventory,
    (SELECT COUNT(*) > 0 FROM v_restatement_details)                                  AS has_restatement,
    (SELECT COUNT(*) > 0 FROM raw_financials
     WHERE line_item = 'GoingConcernDoubt' AND value IS NOT NULL)                      AS has_going_concern_doubt,
    (SELECT COUNT(*) > 0 FROM raw_financials
     WHERE line_item = 'MaterialWeakness' AND value IS NOT NULL)                       AS has_material_weakness,
    -- missing_quarters: comma-separated 'FY<year><Qn>' for any (line_item, quarter)
    -- gap among the load-bearing line items.  Restricted to Revenue / OperatingIncome
    -- / OperatingCashFlow / NetIncome so legitimately-missing items (e.g. Inventory
    -- on a pure-SaaS company) do not trigger downstream refusals.
    (SELECT STRING_AGG(DISTINCT 'FY' || fiscal_year || fiscal_period, ','
                       ORDER BY 'FY' || fiscal_year || fiscal_period)
     FROM v_missing_coverage
     WHERE line_item IN ('Revenue', 'OperatingIncome',
                         'OperatingCashFlow', 'NetIncome'))                            AS missing_quarters
"""

# Ordered list of all views to create — order matters (dependencies first).
_VIEWS: list[tuple[str, str]] = [
    ("v_canonical_facts", _SQL_CANONICAL),
    ("v_income_statement_quarterly", _SQL_INCOME_STATEMENT),
    ("v_balance_sheet_quarterly", _SQL_BALANCE_SHEET),
    ("v_cash_flow_quarterly", _SQL_CASH_FLOW),
    ("v_fcf_bridge", _SQL_FCF_BRIDGE),
    ("v_key_metrics", _SQL_KEY_METRICS),
    ("v_restatement_details", _SQL_RESTATEMENT_DETAILS),
    ("v_missing_coverage", _SQL_MISSING_COVERAGE),
    ("v_data_quality", _SQL_DATA_QUALITY),
]


# ── Public API ────────────────────────────────────────────────────────────────


def build(
    ticker: str | None = None,
    rebuild: bool = False,
    force: bool = False,
) -> Path:
    """Build the DuckDB warehouse for a company.

    Loads ``{ticker}_financials.parquet`` into a ``raw_financials`` table,
    then creates all analytical views.

    Args:
        ticker:  Ticker symbol; if ``None``, reads from ``config/company.yaml``.
        rebuild: If ``True``, drop and recreate the entire database.
        force:   Skip confirmation prompt when *rebuild* is ``True``.

    Returns:
        Path to the ``.duckdb`` file.

    Raises:
        FileNotFoundError: If the parquet file does not exist.
        RuntimeError:      If *rebuild* is requested but *force* is not set
                           and the user declines the confirmation.
    """
    # ── Resolve ticker and paths ──────────────────────────────────────────────
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"Config not found: {_CONFIG_PATH}. Run: python -m src.company_resolver <TICKER>"
        )

    with _CONFIG_PATH.open() as fh:
        config: dict[str, Any] = yaml.safe_load(fh)

    resolved_ticker = (ticker or str(config["ticker"])).upper().strip()

    parquet_path = _PROCESSED_DIR / f"{resolved_ticker}_financials.parquet"
    if not parquet_path.exists():
        raise FileNotFoundError(
            f"Parquet not found: {parquet_path}. "
            "Run: python -m src.ingest_edgar --ticker {resolved_ticker}"
        )

    db_path = _PROCESSED_DIR / f"{resolved_ticker}.duckdb"

    # ── Rebuild guard ─────────────────────────────────────────────────────────
    if rebuild and db_path.exists():
        if not force:
            answer = (
                input(f"\nWARNING: --rebuild will destroy {db_path}.\nContinue? [y/N] ")
                .strip()
                .lower()
            )
            if answer not in ("y", "yes"):
                raise RuntimeError("Rebuild cancelled by user.")
        db_path.unlink()
        logger.info("Existing warehouse deleted: %s", db_path)

    # ── Connect and load ──────────────────────────────────────────────────────
    logger.info("Opening warehouse: %s", db_path)
    con = duckdb.connect(str(db_path))

    try:
        con.execute("DROP TABLE IF EXISTS raw_financials")
        con.execute(f"CREATE TABLE raw_financials AS SELECT * FROM read_parquet('{parquet_path}')")

        row_count: int = con.execute("SELECT COUNT(*) FROM raw_financials").fetchone()[0]  # type: ignore[index]
        logger.info("raw_financials: %d rows loaded", row_count)

        # ── Create views ──────────────────────────────────────────────────────
        for view_name, sql in _VIEWS:
            con.execute(sql)
            view_rows: int = con.execute(f"SELECT COUNT(*) FROM {view_name}").fetchone()[0]  # type: ignore[index]
            logger.info("  %-35s  %d rows", view_name, view_rows)

    finally:
        con.close()

    return db_path


def query_summary(db_path: Path) -> dict[str, Any]:
    """Return the ``v_data_quality`` summary row as a dict.

    Convenience function used by the CLI and by tests.
    """
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        row = con.execute("SELECT * FROM v_data_quality").fetchdf()
        if row.empty:
            return {}
        return dict(row.iloc[0])
    finally:
        con.close()


# ── CLI entry-point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    parser = argparse.ArgumentParser(
        description="Build the DuckDB analytics warehouse.",
        epilog="Creates data/processed/{TICKER}.duckdb with all analytical views.",
    )
    parser.add_argument(
        "--ticker",
        metavar="TICKER",
        default=None,
        help="Ticker symbol. Defaults to config/company.yaml.",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Drop and recreate the entire database (prompts for confirmation).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation prompt when used with --rebuild.",
    )
    args = parser.parse_args()

    try:
        path = build(ticker=args.ticker, rebuild=args.rebuild, force=args.force)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    summary = query_summary(path)
    print(f"\nWarehouse: {path}")
    print("\n── v_data_quality summary ──────────────────────────────────────")
    for key, value in summary.items():
        print(f"  {key:<30} {value}")

    # Print restatement details if any
    if summary.get("has_restatement"):
        con = duckdb.connect(str(path), read_only=True)
        try:
            restated = con.execute("SELECT * FROM v_restatement_details").fetchdf()
        finally:
            con.close()
        print("\n── Restatements detected ───────────────────────────────────────")
        print(restated.to_string(index=False))
    else:
        print("\nNo restatements detected in this dataset.")
