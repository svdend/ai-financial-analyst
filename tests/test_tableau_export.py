"""Tests for src/export_for_tableau.py.

Uses the same DuckDB fixture-build helpers as test_warehouse.py.
All tests run offline — no FRED network calls are made
(ETF return is not exercised here).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pandas as pd
import pytest
import yaml

from src.build_warehouse import build as build_warehouse
from src.export_for_tableau import (
    _calendar_to_fiscal_quarter,
    _cash_flow_ytd_to_standalone,
    _derive_free_cash_flow,
    _export_dim_filing,
    _export_dim_metric,
    _export_fact_financials,
    _export_fact_forecasts,
    _fiscal_to_calendar_quarter,
    export,
)
from src.ingest_edgar import ingest

_FIXTURES = Path(__file__).parent / "fixtures"


# ── Shared helpers ─────────────────────────────────────────────────────────────


def _load_fixture(name: str) -> dict[str, Any]:
    with (_FIXTURES / name).open() as fh:
        return json.load(fh)  # type: ignore[no-any-return]


def _build_warehouse_tmp(
    fixture_name: str,
    ticker: str,
    cik_int: int,
    tmp_path: Path,
) -> Path:
    """Ingest fixture → parquet → DuckDB; return .duckdb path."""
    facts = _load_fixture(fixture_name)
    config: dict[str, Any] = {
        "cik": str(cik_int).zfill(10),
        "cik_int": cik_int,
        "ticker": ticker,
        "name": f"Test {ticker}",
        "fiscal_year_end_month": 7,
        "fiscal_year_end_day": 31,
        "sector_etf": "XLK",
    }
    config_path = tmp_path / "company.yaml"
    with config_path.open("w") as fh:
        yaml.dump(config, fh)

    with (
        patch("src.ingest_edgar._CONFIG_PATH", config_path),
        patch("src.ingest_edgar._DATA_DIR", tmp_path),
    ):
        ingest(ticker=ticker, years=10, facts_json=facts)

    with (
        patch("src.build_warehouse._CONFIG_PATH", config_path),
        patch("src.build_warehouse._PROCESSED_DIR", tmp_path),
    ):
        return build_warehouse(ticker=ticker)


# ── _fiscal_to_calendar_quarter ───────────────────────────────────────────────


def test_fiscal_to_calendar_quarter_panw_q1() -> None:
    """PANW FY ends July (month 7). FY2024 Q1 is Aug–Oct 2023 → cal 2023 Q4."""
    cal_year, cal_q = _fiscal_to_calendar_quarter(2024, "Q1", fy_end_month=7)
    assert (cal_year, cal_q) == (2023, 4)


def test_fiscal_to_calendar_quarter_panw_q2() -> None:
    """PANW FY ends July: FY2025 Q2 ends Jan 2025 → cal 2025 Q1."""
    cal_year, cal_q = _fiscal_to_calendar_quarter(2025, "Q2", fy_end_month=7)
    assert (cal_year, cal_q) == (2025, 1)


def test_fiscal_to_calendar_quarter_q4() -> None:
    """FY ending December: Q4 should be calendar Q4."""
    cal_year, cal_q = _fiscal_to_calendar_quarter(2024, "Q4", fy_end_month=12)
    assert (cal_year, cal_q) == (2024, 4)


# ── _calendar_to_fiscal_quarter ───────────────────────────────────────────────


def test_calendar_to_fiscal_quarter_panw_q2() -> None:
    """PANW July fiscal year: 2025-01-31 (Jan) is FY2025 Q2."""
    fy, fp = _calendar_to_fiscal_quarter(pd.Timestamp("2025-01-31"), fy_end_month=7)
    assert (fy, fp) == (2025, "Q2")


def test_calendar_to_fiscal_quarter_panw_q4_end_of_fy() -> None:
    """PANW July fiscal year: 2025-07-31 closes FY2025 Q4."""
    fy, fp = _calendar_to_fiscal_quarter(pd.Timestamp("2025-07-31"), fy_end_month=7)
    assert (fy, fp) == (2025, "Q4")


def test_calendar_to_fiscal_quarter_panw_q1_after_fy_rollover() -> None:
    """PANW July fiscal year: 2025-10-31 (Oct, after July rollover) is FY2026 Q1."""
    fy, fp = _calendar_to_fiscal_quarter(pd.Timestamp("2025-10-31"), fy_end_month=7)
    assert (fy, fp) == (2026, "Q1")


def test_calendar_to_fiscal_quarter_calendar_fy() -> None:
    """December fiscal year: 2024-09-30 is FY2024 Q3, 2024-12-31 is FY2024 Q4."""
    assert _calendar_to_fiscal_quarter(pd.Timestamp("2024-09-30"), fy_end_month=12) == (
        2024,
        "Q3",
    )
    assert _calendar_to_fiscal_quarter(pd.Timestamp("2024-12-31"), fy_end_month=12) == (
        2024,
        "Q4",
    )


def test_calendar_to_fiscal_quarter_apple_september_fy() -> None:
    """Apple September fiscal year: late-Sept close is FY Q4, June close is Q3."""
    assert _calendar_to_fiscal_quarter(pd.Timestamp("2025-09-27"), fy_end_month=9) == (
        2025,
        "Q4",
    )
    assert _calendar_to_fiscal_quarter(pd.Timestamp("2025-06-28"), fy_end_month=9) == (
        2025,
        "Q3",
    )


def test_calendar_to_fiscal_quarter_round_trip() -> None:
    """For every (fy, Qn, fy_end_month), the two helpers must round-trip cleanly.

    Constructs the fiscal-quarter-end date deterministically from
    ``fy_end_month`` and ``Qn`` (Q1 ends 9 months before fy_end, Q4 ends at
    fy_end), then feeds that ``period_end`` through ``_calendar_to_fiscal_quarter``
    and asserts we recover the original (fy, Qn).

    Also checks that ``_fiscal_to_calendar_quarter`` returns the calendar
    quarter that *contains* that fiscal-quarter-end date.
    """
    for fy_end_month in (3, 7, 9, 12):
        for fy_in in (2024, 2025):
            for q_in in ("Q1", "Q2", "Q3", "Q4"):
                quarter_num = int(q_in[1])
                end_month_raw = fy_end_month + (quarter_num - 4) * 3
                end_year = fy_in if end_month_raw > 0 else fy_in - 1
                end_month = ((end_month_raw - 1) % 12) + 1
                pe = pd.Timestamp(year=end_year, month=end_month, day=1) + pd.offsets.MonthEnd(0)

                fy_out, q_out = _calendar_to_fiscal_quarter(pe, fy_end_month)
                assert (fy_out, q_out) == (fy_in, q_in), (
                    f"round-trip failed: fy_end_month={fy_end_month} "
                    f"fy={fy_in} q={q_in} → pe={pe.date()} → ({fy_out},{q_out})"
                )

                cal_year, cal_q = _fiscal_to_calendar_quarter(fy_in, q_in, fy_end_month)
                expected_cal_q = (end_month - 1) // 3 + 1
                assert (cal_year, cal_q) == (end_year, expected_cal_q), (
                    f"_fiscal_to_calendar_quarter mismatch: fy_end_month={fy_end_month} "
                    f"fy={fy_in} q={q_in} → got ({cal_year},{cal_q}), "
                    f"expected ({end_year},{expected_cal_q})"
                )


# ── _export_dim_metric ────────────────────────────────────────────────────────


def test_dim_metric_required_columns() -> None:
    """dim_metric must contain line_item, label, category, unit."""
    df = _export_dim_metric()
    for col in ("line_item", "label", "category", "unit"):
        assert col in df.columns, f"Missing column: {col}"


def test_dim_metric_has_revenue_row() -> None:
    """Revenue must be present in dim_metric."""
    df = _export_dim_metric()
    assert "Revenue" in df["line_item"].values


def test_dim_metric_no_duplicate_line_items() -> None:
    """Each line_item appears at most once in dim_metric."""
    df = _export_dim_metric()
    assert df["line_item"].nunique() == len(df)


def test_dim_metric_covers_exported_actual_line_items() -> None:
    """Metadata should exist for every actual line item the exporter emits."""
    df = _export_dim_metric()
    expected = {
        "ResearchAndDevelopment",
        "InvestingCashFlow",
        "FinancingCashFlow",
        "Depreciation",
        "TreasuryStockRepurchases",
    }
    assert expected <= set(df["line_item"])


# ── _export_dim_filing ────────────────────────────────────────────────────────


def test_dim_filing_one_row_per_accession(tmp_path: Path) -> None:
    """dim_filing must have no duplicate accession_no values."""
    db_path = _build_warehouse_tmp("panw_companyfacts.json", "PANW", 1327567, tmp_path)
    import duckdb

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        df_fin = _export_fact_financials(con)
    finally:
        con.close()
    df_filing = _export_dim_filing(df_fin)
    assert df_filing["accession_no"].nunique() == len(df_filing)


def test_dim_filing_has_filing_url(tmp_path: Path) -> None:
    """All dim_filing rows must have a non-null filing_url."""
    db_path = _build_warehouse_tmp("panw_companyfacts.json", "PANW", 1327567, tmp_path)
    import duckdb

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        df_fin = _export_fact_financials(con)
    finally:
        con.close()
    df_filing = _export_dim_filing(df_fin)
    assert df_filing["filing_url"].notna().all()


# ── _export_fact_financials ────────────────────────────────────────────────────


def test_fact_financials_has_provenance_columns(tmp_path: Path) -> None:
    """fact_financials must carry accession_no and filing_url columns."""
    db_path = _build_warehouse_tmp("panw_companyfacts.json", "PANW", 1327567, tmp_path)
    import duckdb

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        df = _export_fact_financials(con)
    finally:
        con.close()
    assert "accession_no" in df.columns
    assert "filing_url" in df.columns


def test_fact_financials_contains_revenue(tmp_path: Path) -> None:
    """fact_financials must have Revenue rows for PANW."""
    db_path = _build_warehouse_tmp("panw_companyfacts.json", "PANW", 1327567, tmp_path)
    import duckdb

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        df = _export_fact_financials(con)
    finally:
        con.close()
    assert "Revenue" in df["line_item"].values


def test_fact_financials_excludes_derived_metrics(tmp_path: Path) -> None:
    """fact_financials must NOT contain derived metrics — they are Tableau calc fields.

    Margins and growth rates have no single source accession_no, so exporting
    them as fact rows breaks the README's "every point traces to a filing"
    claim.  They live in Tableau_Setup.md §5 as calculated fields computed
    from the sourced Revenue/GrossProfit/OperatingIncome/etc. rows.
    """
    db_path = _build_warehouse_tmp("panw_companyfacts.json", "PANW", 1327567, tmp_path)
    import duckdb

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        df = _export_fact_financials(con)
    finally:
        con.close()
    derived = {
        "gross_margin_pct",
        "operating_margin_pct",
        "net_margin_pct",
        "fcf_margin_pct",
        "revenue_yoy_growth",
        "revenue_qoq_growth",
    }
    leaked = derived & set(df["line_item"].values)
    assert not leaked, f"Derived metrics leaked into fact_financials: {leaked}"


def test_fact_financials_every_row_has_accession(tmp_path: Path) -> None:
    """Every row in fact_financials must carry a non-null accession_no.

    Load-bearing invariant for the README claim that every Tableau mark
    traces to an SEC filing.  Derived rows (margins, growth) used to violate
    this — they are now Tableau calc fields, not fact rows.
    """
    db_path = _build_warehouse_tmp("panw_companyfacts.json", "PANW", 1327567, tmp_path)
    import duckdb

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        df = _export_fact_financials(con)
    finally:
        con.close()
    missing = df[df["accession_no"].isna()]
    assert missing.empty, (
        "fact_financials rows without accession_no:\n"
        f"{missing[['line_item', 'period_end', 'fiscal_year']].to_string()}"
    )


def test_fact_financials_no_ytd_duplicates(tmp_path: Path) -> None:
    """fact_financials must have at most one row per (line_item, period_end, fiscal_period).

    SEC XBRL filings report both 3-month standalone and YTD cumulative values
    for the same concept and period_end.  The export must deduplicate these,
    keeping quarter-framed standalone values when present.
    """
    db_path = _build_warehouse_tmp("panw_companyfacts.json", "PANW", 1327567, tmp_path)
    import duckdb

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        df = _export_fact_financials(con)
    finally:
        con.close()

    dupes = df.duplicated(
        subset=["line_item", "period_end", "fiscal_year", "fiscal_period"], keep=False
    )
    dup_rows = df[dupes][["line_item", "period_end", "fiscal_period", "value"]]
    assert (
        len(dup_rows) == 0
    ), f"YTD duplicates still present after deduplication:\n{dup_rows.to_string()}"


def test_fact_financials_unique_per_period_end(tmp_path: Path) -> None:
    """fact_financials must have at most one row per (ticker, line_item, period_end).

    Load-bearing invariant for Tableau — duplicates cause the default AVG
    aggregation to silently halve values and break growth calculations.
    Covers both actuals and derived metrics (gross_margin_pct etc.).
    """
    db_path = _build_warehouse_tmp("panw_companyfacts.json", "PANW", 1327567, tmp_path)
    import duckdb

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        df = _export_fact_financials(con)
    finally:
        con.close()

    dupes = df.duplicated(subset=["ticker", "line_item", "period_end"], keep=False)
    dup_rows = df[dupes][["line_item", "period_end", "fiscal_year", "fiscal_period", "value"]]
    assert (
        len(dup_rows) == 0
    ), f"Duplicate (ticker, line_item, period_end) rows in export:\n{dup_rows.to_string()}"


def _two_cashflow_rows() -> pd.DataFrame:
    """Minimal standalone OCF + CapEx frame for one period, sharing a filing."""
    base = {
        "ticker": "PANW",
        "period_end": "2026-01-31",
        "period_type": "Q",
        "fiscal_year": 2026,
        "fiscal_period": "Q2",
        "unit": "USD",
        "accession_no": "0001327567-26-000001",
        "filing_url": "https://example.com/10q",
        "form_type": "10-Q",
        "filed_date": "2026-02-18",
        "frame": "CY2026Q1",
    }
    return pd.DataFrame(
        [
            {
                **base,
                "line_item": "OperatingCashFlow",
                "value": 554_000_000.0,
                "concept_used": "NetCashProvidedByUsedInOperatingActivities",
                "fact_id": "f1",
            },
            {
                **base,
                "line_item": "CapEx",
                "value": 170_000_000.0,
                "concept_used": "PaymentsToAcquireProductiveAssets",
                "fact_id": "f2",
            },
        ]
    )


def test_derive_free_cash_flow_equals_ocf_minus_capex() -> None:
    """FreeCashFlow row = OperatingCashFlow - CapEx for the shared period_end."""
    out = _derive_free_cash_flow(_two_cashflow_rows())
    fcf = out[out["line_item"] == "FreeCashFlow"]
    assert len(fcf) == 1
    assert fcf.iloc[0]["value"] == pytest.approx(554_000_000.0 - 170_000_000.0)


def test_derive_free_cash_flow_inherits_filing_provenance() -> None:
    """Derived FCF inherits the filing's accession_no / filing_url (no orphan rows)."""
    out = _derive_free_cash_flow(_two_cashflow_rows())
    fcf = out[out["line_item"] == "FreeCashFlow"].iloc[0]
    assert fcf["accession_no"] == "0001327567-26-000001"
    assert fcf["filing_url"] == "https://example.com/10q"
    assert fcf["fiscal_year"] == 2026
    assert fcf["fiscal_period"] == "Q2"


def test_derive_free_cash_flow_skips_periods_missing_a_leg() -> None:
    """No FCF row is emitted for a period that lacks either OCF or CapEx."""
    rows = _two_cashflow_rows()
    ocf_only = rows[rows["line_item"] == "OperatingCashFlow"]
    out = _derive_free_cash_flow(ocf_only)
    assert "FreeCashFlow" not in out["line_item"].values


def test_fact_financials_fiscal_labels_self_consistent_with_period_end(tmp_path: Path) -> None:
    """Every exported row's (fiscal_year, fiscal_period) matches its period_end.

    Comparative rows carried in newer 10-Qs inherit the new filing's labels
    via ``v_canonical_facts``. The export must recompute fiscal labels from
    ``period_end`` + ``fy_end_month`` so each calendar period_end carries its
    *own* fiscal label, and no two distinct period_ends share a fiscal pair.
    Regression catch for ai-financial-analyst-bau.
    """
    db_path = _build_warehouse_tmp("panw_companyfacts.json", "PANW", 1327567, tmp_path)
    import duckdb

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        df = _export_fact_financials(con, fy_end_month=7)
    finally:
        con.close()

    # Every row's labels must match what the helper computes from its period_end.
    for _, row in df.iterrows():
        expected_fy, expected_fp = _calendar_to_fiscal_quarter(row["period_end"], 7)
        assert (int(row["fiscal_year"]), row["fiscal_period"]) == (expected_fy, expected_fp), (
            f"fiscal label drift: period_end={row['period_end']} "
            f"line_item={row['line_item']} got=({row['fiscal_year']}, {row['fiscal_period']}) "
            f"expected=({expected_fy}, {expected_fp})"
        )

    # Within any single line_item, distinct period_ends must carry distinct
    # fiscal pairs — the user-visible symptom the reviewer flagged.
    for _line_item, grp in df.groupby("line_item"):
        pair_to_periods = grp.groupby(["fiscal_year", "fiscal_period"])["period_end"].nunique()
        assert (pair_to_periods <= 1).all(), (
            "fiscal labels duplicated across distinct period_ends:\n"
            f"{pair_to_periods[pair_to_periods > 1]}"
        )


def test_fact_financials_collapses_multi_fiscal_year_comparatives(tmp_path: Path) -> None:
    """fact_financials must collapse multi-fiscal-year comparatives to one row.

    A 10-Q for FY2026-Q1 carries the prior-year same-quarter row as a
    comparative — same ``period_end`` but a different ``fiscal_year`` and
    often a different ``frame`` than the original FY2025-Q1 filing.  Both
    rows survive ``v_canonical_facts`` (which partitions on ``frame``) and
    Tableau's default AVG aggregation would silently halve the value.

    The export must collapse these to one canonical row per period_end using
    form-priority + latest filed_date, so growth calculations are correct.
    """
    import duckdb

    from src.build_warehouse import _SQL_CANONICAL

    db_path = tmp_path / "synthetic.duckdb"
    con = duckdb.connect(str(db_path))
    try:
        con.execute("""
            CREATE TABLE raw_financials (
                ticker        VARCHAR,
                line_item     VARCHAR,
                concept_used  VARCHAR,
                period_end    DATE,
                period_type   VARCHAR,
                fiscal_year   INTEGER,
                fiscal_period VARCHAR,
                value         DOUBLE,
                unit          VARCHAR,
                accession_no  VARCHAR,
                fact_id       VARCHAR,
                filing_url    VARCHAR,
                form_type     VARCHAR,
                filed_date    DATE,
                frame         VARCHAR
            )
        """)
        # Two rows for the same (ticker, line_item, period_end) — the original
        # FY2025-Q1 10-Q row and the comparative carried in the FY2026-Q1 10-Q.
        # Different fiscal_year + different frame would let both survive the
        # v_canonical_facts QUALIFY otherwise.  The newer 10-Q (latest
        # filed_date) must win.
        rows = [
            (
                "TEST",
                "Revenue",
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                "2024-10-31",
                "Q",
                2025,
                "Q1",
                2_138_800_000.0,
                "USD",
                "0000000000-24-000001",
                "fact-2025",
                "https://example/2025",
                "10-Q",
                "2024-11-21",
                "CY2024Q4",
            ),
            (
                "TEST",
                "Revenue",
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                "2024-10-31",
                "Q",
                2026,
                "Q1",
                2_139_000_000.0,
                "USD",
                "0000000000-25-000001",
                "fact-2026",
                "https://example/2026",
                "10-Q",
                "2025-11-20",
                "",
            ),
        ]
        con.executemany(
            "INSERT INTO raw_financials VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        con.execute(_SQL_CANONICAL)

        # PANW-like July fiscal year end so 2024-10-31 maps to FY2025 Q1.
        df = _export_fact_financials(con, fy_end_month=7)
    finally:
        con.close()

    # Exactly one row per (ticker, line_item, period_end) — the load-bearing
    # invariant for Tableau's AVG aggregation.
    dupes = df.duplicated(subset=["ticker", "line_item", "period_end"], keep=False)
    assert not dupes.any(), (
        "Multi-fiscal-year comparative duplicates not collapsed; got:\n"
        f"{df[dupes][['ticker', 'line_item', 'period_end', 'fiscal_year', 'value', 'filed_date']].to_string()}"
    )

    # The newer filing (FY2026-Q1 10-Q, filed 2025-11-20) wins the value pick.
    revenue = df[(df["line_item"] == "Revenue") & (df["period_end"].astype(str) == "2024-10-31")]
    assert len(revenue) == 1
    assert revenue.iloc[0]["accession_no"] == "0000000000-25-000001"

    # Fiscal labels are recomputed from period_end + fy_end_month, NOT inherited
    # from the newer filing's stamped (2026, Q1).  For PANW's July fiscal year,
    # 2024-10-31 lives in FY2025 Q1.  This is the regression catch for ai-financial-analyst-bau.
    assert int(revenue.iloc[0]["fiscal_year"]) == 2025
    assert revenue.iloc[0]["fiscal_period"] == "Q1"


def test_fact_financials_ytd_tie_resolved_to_standalone(tmp_path: Path) -> None:
    """When form_type and filed_date tie, the smaller (standalone) value wins.

    A single 10-Q reports both the 3-month standalone and the YTD cumulative
    value for the same period_end, with the same form_type and filed_date but
    different ``frame`` (e.g. ``CY2024Q4`` vs empty).  The QUALIFY's row pick
    must break that tie deterministically with ``ABS(value) ASC`` so the
    standalone value wins — otherwise DuckDB occasionally surfaces the YTD
    cumulative and the export silently doubles the period's value.
    """
    import duckdb

    from src.build_warehouse import _SQL_CANONICAL

    db_path = tmp_path / "synthetic_ytd.duckdb"
    con = duckdb.connect(str(db_path))
    try:
        con.execute("""
            CREATE TABLE raw_financials (
                ticker        VARCHAR,
                line_item     VARCHAR,
                concept_used  VARCHAR,
                period_end    DATE,
                period_type   VARCHAR,
                fiscal_year   INTEGER,
                fiscal_period VARCHAR,
                value         DOUBLE,
                unit          VARCHAR,
                accession_no  VARCHAR,
                fact_id       VARCHAR,
                filing_url    VARCHAR,
                form_type     VARCHAR,
                filed_date    DATE,
                frame         VARCHAR
            )
        """)
        # Same fiscal triple, same form/filed_date, different frame: the
        # YTD H1 cumulative (4.396B) and the Q2 standalone (2.257B).
        # Modeled after PANW 2025-01-31 in the live warehouse.
        rows = [
            (
                "TEST",
                "Revenue",
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                "2025-01-31",
                "Q",
                2026,
                "Q2",
                4_396_000_000.0,  # YTD H1
                "USD",
                "0000000000-26-000001",
                "fact-ytd",
                "https://example/ytd",
                "10-Q",
                "2026-02-18",
                "",
            ),
            (
                "TEST",
                "Revenue",
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                "2025-01-31",
                "Q",
                2026,
                "Q2",
                2_257_000_000.0,  # Q2 standalone
                "USD",
                "0000000000-26-000001",
                "fact-standalone",
                "https://example/standalone",
                "10-Q",
                "2026-02-18",
                "CY2024Q4",
            ),
        ]
        con.executemany(
            "INSERT INTO raw_financials VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        con.execute(_SQL_CANONICAL)

        # Run the export multiple times — without the ABS(value) tiebreaker,
        # DuckDB's row pick on ties is undefined; one of these runs would
        # surface the YTD value.  With the tiebreaker, every run is identical.
        results = [_export_fact_financials(con) for _ in range(5)]
    finally:
        con.close()

    for df in results:
        revenue = df[df["line_item"] == "Revenue"]
        assert len(revenue) == 1
        assert revenue.iloc[0]["value"] == 2_257_000_000.0, (
            "YTD value won the QUALIFY race; frame-priority tiebreaker must "
            f"pick the quarter-framed (standalone) row.\n{revenue.to_string()}"
        )
        assert revenue.iloc[0]["fact_id"] == "fact-standalone"


def test_fact_financials_ytd_tie_negative_standalone(tmp_path: Path) -> None:
    """Standalone wins even when its absolute value is LARGER than YTD's.

    A magnitude-only tiebreaker (``ABS(value) ASC``) silently picks the wrong
    row when the company's quarterly result crosses zero.  Example: Q1
    OperatingIncome = +$200M (profit), Q2 OperatingIncome = -$300M (loss),
    YTD H1 = -$100M.  ``ABS(-300M) > ABS(-100M)`` so the YTD row would win
    on magnitude — and the export would understate the Q2 loss by $200M.

    Frame priority (quarter-framed beats year/empty-framed) is sign-invariant
    and is what the QUALIFY uses now.  This test pins that contract.
    """
    import duckdb

    from src.build_warehouse import _SQL_CANONICAL

    db_path = tmp_path / "synthetic_negative.duckdb"
    con = duckdb.connect(str(db_path))
    try:
        con.execute("""
            CREATE TABLE raw_financials (
                ticker        VARCHAR,
                line_item     VARCHAR,
                concept_used  VARCHAR,
                period_end    DATE,
                period_type   VARCHAR,
                fiscal_year   INTEGER,
                fiscal_period VARCHAR,
                value         DOUBLE,
                unit          VARCHAR,
                accession_no  VARCHAR,
                fact_id       VARCHAR,
                filing_url    VARCHAR,
                form_type     VARCHAR,
                filed_date    DATE,
                frame         VARCHAR
            )
        """)
        # Q2 standalone deeper loss than YTD H1 (because Q1 was a profit).
        # ABS(standalone) > ABS(ytd) — magnitude tiebreaker would pick YTD.
        rows = [
            (
                "TEST",
                "OperatingIncome",
                "OperatingIncomeLoss",
                "2025-01-31",
                "Q",
                2026,
                "Q2",
                -100_000_000.0,  # YTD H1 (Q1 +200M, Q2 -300M => H1 -100M)
                "USD",
                "0000000000-26-000001",
                "fact-ytd-loss",
                "https://example/ytd",
                "10-Q",
                "2026-02-18",
                "",
            ),
            (
                "TEST",
                "OperatingIncome",
                "OperatingIncomeLoss",
                "2025-01-31",
                "Q",
                2026,
                "Q2",
                -300_000_000.0,  # Q2 standalone (the deeper loss — correct)
                "USD",
                "0000000000-26-000001",
                "fact-standalone-loss",
                "https://example/standalone",
                "10-Q",
                "2026-02-18",
                "CY2024Q4",
            ),
        ]
        con.executemany(
            "INSERT INTO raw_financials VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        con.execute(_SQL_CANONICAL)

        df = _export_fact_financials(con)
    finally:
        con.close()

    op = df[df["line_item"] == "OperatingIncome"]
    assert len(op) == 1
    assert op.iloc[0]["value"] == -300_000_000.0, (
        "YTD value won — magnitude-only tiebreaker is unsafe on sign-flipping "
        f"line items.  Frame priority must pick the quarter-framed row.\n{op.to_string()}"
    )
    assert op.iloc[0]["fact_id"] == "fact-standalone-loss"


def test_fact_financials_differences_ytd_cash_flow_rows(tmp_path: Path) -> None:
    """Cash-flow Q2/Q3 rows without quarter frames are YTD and must be differenced.

    PANW-like 10-Q cash-flow statements usually expose Q2 and Q3 OCF/CapEx as
    fiscal-year-to-date values only.  Tableau margins divide by standalone
    quarterly Revenue, so the export must convert those cumulative rows to
    standalone quarter values before publishing.
    """
    import duckdb

    from src.build_warehouse import _SQL_CANONICAL

    db_path = tmp_path / "synthetic_cash_flow_ytd.duckdb"
    con = duckdb.connect(str(db_path))
    try:
        con.execute("""
            CREATE TABLE raw_financials (
                ticker        VARCHAR,
                line_item     VARCHAR,
                concept_used  VARCHAR,
                period_end    DATE,
                period_type   VARCHAR,
                fiscal_year   INTEGER,
                fiscal_period VARCHAR,
                value         DOUBLE,
                unit          VARCHAR,
                accession_no  VARCHAR,
                fact_id       VARCHAR,
                filing_url    VARCHAR,
                form_type     VARCHAR,
                filed_date    DATE,
                frame         VARCHAR
            )
        """)
        rows = [
            (
                "TEST",
                "OperatingCashFlow",
                "NetCashProvidedByUsedInOperatingActivities",
                "2024-10-31",
                "Q",
                2025,
                "Q1",
                100.0,
                "USD",
                "0000000000-25-000001",
                "ocf-q1",
                "https://example/q1",
                "10-Q",
                "2024-11-20",
                "CY2024Q3",
            ),
            (
                "TEST",
                "OperatingCashFlow",
                "NetCashProvidedByUsedInOperatingActivities",
                "2025-01-31",
                "Q",
                2025,
                "Q2",
                260.0,
                "USD",
                "0000000000-25-000002",
                "ocf-h1-ytd",
                "https://example/q2",
                "10-Q",
                "2025-02-20",
                "",
            ),
            (
                "TEST",
                "OperatingCashFlow",
                "NetCashProvidedByUsedInOperatingActivities",
                "2025-04-30",
                "Q",
                2025,
                "Q3",
                450.0,
                "USD",
                "0000000000-25-000003",
                "ocf-9m-ytd",
                "https://example/q3",
                "10-Q",
                "2025-05-20",
                "",
            ),
            (
                "TEST",
                "CapEx",
                "PaymentsToAcquireProductiveAssets",
                "2024-10-31",
                "Q",
                2025,
                "Q1",
                10.0,
                "USD",
                "0000000000-25-000001",
                "capex-q1",
                "https://example/q1",
                "10-Q",
                "2024-11-20",
                "CY2024Q3",
            ),
            (
                "TEST",
                "CapEx",
                "PaymentsToAcquireProductiveAssets",
                "2025-01-31",
                "Q",
                2025,
                "Q2",
                35.0,
                "USD",
                "0000000000-25-000002",
                "capex-h1-ytd",
                "https://example/q2",
                "10-Q",
                "2025-02-20",
                "",
            ),
            (
                "TEST",
                "CapEx",
                "PaymentsToAcquireProductiveAssets",
                "2025-04-30",
                "Q",
                2025,
                "Q3",
                70.0,
                "USD",
                "0000000000-25-000003",
                "capex-9m-ytd",
                "https://example/q3",
                "10-Q",
                "2025-05-20",
                "",
            ),
        ]
        con.executemany(
            "INSERT INTO raw_financials VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        con.execute(_SQL_CANONICAL)

        df = _export_fact_financials(con, fy_end_month=7)
    finally:
        con.close()

    values = {
        (row["line_item"], str(pd.to_datetime(row["period_end"]).date())): row["value"]
        for _, row in df.iterrows()
    }
    assert values[("OperatingCashFlow", "2024-10-31")] == 100.0
    assert values[("OperatingCashFlow", "2025-01-31")] == 160.0
    assert values[("OperatingCashFlow", "2025-04-30")] == 190.0
    assert values[("CapEx", "2024-10-31")] == 10.0
    assert values[("CapEx", "2025-01-31")] == 25.0
    assert values[("CapEx", "2025-04-30")] == 35.0


def test_fact_financials_keeps_quarter_framed_cash_flow_rows(tmp_path: Path) -> None:
    """Quarter-framed cash-flow rows are already standalone and must not be differenced."""
    import duckdb

    from src.build_warehouse import _SQL_CANONICAL

    db_path = tmp_path / "synthetic_cash_flow_standalone.duckdb"
    con = duckdb.connect(str(db_path))
    try:
        con.execute("""
            CREATE TABLE raw_financials (
                ticker        VARCHAR,
                line_item     VARCHAR,
                concept_used  VARCHAR,
                period_end    DATE,
                period_type   VARCHAR,
                fiscal_year   INTEGER,
                fiscal_period VARCHAR,
                value         DOUBLE,
                unit          VARCHAR,
                accession_no  VARCHAR,
                fact_id       VARCHAR,
                filing_url    VARCHAR,
                form_type     VARCHAR,
                filed_date    DATE,
                frame         VARCHAR
            )
        """)
        rows = [
            (
                "TEST",
                "OperatingCashFlow",
                "NetCashProvidedByUsedInOperatingActivities",
                "2024-10-31",
                "Q",
                2025,
                "Q1",
                100.0,
                "USD",
                "0000000000-25-000001",
                "ocf-q1",
                "https://example/q1",
                "10-Q",
                "2024-11-20",
                "CY2024Q3",
            ),
            (
                "TEST",
                "OperatingCashFlow",
                "NetCashProvidedByUsedInOperatingActivities",
                "2025-01-31",
                "Q",
                2025,
                "Q2",
                160.0,
                "USD",
                "0000000000-25-000002",
                "ocf-q2-standalone",
                "https://example/q2",
                "10-Q",
                "2025-02-20",
                "CY2024Q4",
            ),
        ]
        con.executemany(
            "INSERT INTO raw_financials VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        con.execute(_SQL_CANONICAL)

        df = _export_fact_financials(con, fy_end_month=7)
    finally:
        con.close()

    q2 = df[
        (df["line_item"] == "OperatingCashFlow")
        & (pd.to_datetime(df["period_end"]).dt.date.astype(str) == "2025-01-31")
    ]
    assert len(q2) == 1
    assert q2.iloc[0]["value"] == 160.0


def test_fact_financials_warns_when_cash_flow_baseline_missing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A cumulative Q3 cash-flow row without Q2 should not be silently published."""
    import duckdb

    from src.build_warehouse import _SQL_CANONICAL

    db_path = tmp_path / "synthetic_cash_flow_missing_baseline.duckdb"
    con = duckdb.connect(str(db_path))
    try:
        con.execute("""
            CREATE TABLE raw_financials (
                ticker        VARCHAR,
                line_item     VARCHAR,
                concept_used  VARCHAR,
                period_end    DATE,
                period_type   VARCHAR,
                fiscal_year   INTEGER,
                fiscal_period VARCHAR,
                value         DOUBLE,
                unit          VARCHAR,
                accession_no  VARCHAR,
                fact_id       VARCHAR,
                filing_url    VARCHAR,
                form_type     VARCHAR,
                filed_date    DATE,
                frame         VARCHAR
            )
        """)
        rows = [
            (
                "TEST",
                "OperatingCashFlow",
                "NetCashProvidedByUsedInOperatingActivities",
                "2025-04-30",
                "Q",
                2025,
                "Q3",
                450.0,
                "USD",
                "0000000000-25-000003",
                "ocf-9m-ytd",
                "https://example/q3",
                "10-Q",
                "2025-05-20",
                "",
            )
        ]
        con.executemany(
            "INSERT INTO raw_financials VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        con.execute(_SQL_CANONICAL)

        df = _export_fact_financials(con, fy_end_month=7)
    finally:
        con.close()

    q3 = df[(df["line_item"] == "OperatingCashFlow") & (df["fiscal_period"] == "Q3")]
    assert q3.iloc[0]["value"] == 450.0
    assert "missing prior quarter baseline" in caplog.text


def test_fact_financials_cash_flow_differences_within_each_fiscal_year(
    tmp_path: Path,
) -> None:
    """Cumulative cash-flow rows must be differenced within each fiscal year only.

    PANW-style ``fy_end_month=7``: a single calendar year contains rows from two
    different fiscal years (FY2025 Q3 with period_end 2025-04-30 and FY2026 Q1
    with period_end 2025-10-31). Differencing must NOT cross fiscal-year groups
    — FY2026 Q1 is its own standalone-equals-YTD value, not the YTD-minus-prior
    of FY2025 Q3.
    """
    import duckdb

    from src.build_warehouse import _SQL_CANONICAL

    db_path = tmp_path / "synthetic_cash_flow_cross_fy.duckdb"
    con = duckdb.connect(str(db_path))
    try:
        con.execute("""
            CREATE TABLE raw_financials (
                ticker        VARCHAR,
                line_item     VARCHAR,
                concept_used  VARCHAR,
                period_end    DATE,
                period_type   VARCHAR,
                fiscal_year   INTEGER,
                fiscal_period VARCHAR,
                value         DOUBLE,
                unit          VARCHAR,
                accession_no  VARCHAR,
                fact_id       VARCHAR,
                filing_url    VARCHAR,
                form_type     VARCHAR,
                filed_date    DATE,
                frame         VARCHAR
            )
        """)
        rows = [
            (
                "TEST",
                "OperatingCashFlow",
                "NetCashProvidedByUsedInOperatingActivities",
                "2024-10-31",
                "Q",
                2025,
                "Q1",
                100.0,
                "USD",
                "0000000000-25-000001",
                "ocf-fy25-q1",
                "https://example/fy25-q1",
                "10-Q",
                "2024-11-20",
                "",
            ),
            (
                "TEST",
                "OperatingCashFlow",
                "NetCashProvidedByUsedInOperatingActivities",
                "2025-01-31",
                "Q",
                2025,
                "Q2",
                250.0,
                "USD",
                "0000000000-25-000002",
                "ocf-fy25-q2-ytd",
                "https://example/fy25-q2",
                "10-Q",
                "2025-02-20",
                "",
            ),
            (
                "TEST",
                "OperatingCashFlow",
                "NetCashProvidedByUsedInOperatingActivities",
                "2025-04-30",
                "Q",
                2025,
                "Q3",
                420.0,
                "USD",
                "0000000000-25-000003",
                "ocf-fy25-q3-ytd",
                "https://example/fy25-q3",
                "10-Q",
                "2025-05-20",
                "",
            ),
            (
                "TEST",
                "OperatingCashFlow",
                "NetCashProvidedByUsedInOperatingActivities",
                "2025-10-31",
                "Q",
                2026,
                "Q1",
                130.0,
                "USD",
                "0000000000-26-000001",
                "ocf-fy26-q1",
                "https://example/fy26-q1",
                "10-Q",
                "2025-11-20",
                "",
            ),
            (
                "TEST",
                "OperatingCashFlow",
                "NetCashProvidedByUsedInOperatingActivities",
                "2026-01-31",
                "Q",
                2026,
                "Q2",
                290.0,
                "USD",
                "0000000000-26-000002",
                "ocf-fy26-q2-ytd",
                "https://example/fy26-q2",
                "10-Q",
                "2026-02-20",
                "",
            ),
        ]
        con.executemany(
            "INSERT INTO raw_financials VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        con.execute(_SQL_CANONICAL)

        df = _export_fact_financials(con, fy_end_month=7)
    finally:
        con.close()

    values = {
        (row["line_item"], str(pd.to_datetime(row["period_end"]).date())): row["value"]
        for _, row in df.iterrows()
    }
    # FY2025 Q1 — Q1 always equals YTD, unchanged.
    assert values[("OperatingCashFlow", "2024-10-31")] == 100.0
    # FY2025 Q2 — 250 (YTD) − 100 (Q1).
    assert values[("OperatingCashFlow", "2025-01-31")] == 150.0
    # FY2025 Q3 — 420 (YTD) − 250 (Q2 YTD).
    assert values[("OperatingCashFlow", "2025-04-30")] == 170.0
    # FY2026 Q1 — load-bearing assertion: must NOT subtract FY2025 Q3's 420.
    assert values[("OperatingCashFlow", "2025-10-31")] == 130.0
    # FY2026 Q2 — 290 (YTD) − 130 (FY2026 Q1).
    assert values[("OperatingCashFlow", "2026-01-31")] == 160.0


def test_fact_financials_warns_when_cash_flow_q4_baseline_missing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Q4 with no Q2/Q3 must not be silently published as a standalone diff.

    The earlier baseline-missing test only exercised the Q3 branch; this test
    exercises Q4 specifically because Q4 is the most consequential gap (12-month
    YTD silently labeled as a 3-month standalone is a 4× overstatement).
    """
    import duckdb

    from src.build_warehouse import _SQL_CANONICAL

    db_path = tmp_path / "synthetic_cash_flow_q4_missing_baseline.duckdb"
    con = duckdb.connect(str(db_path))
    try:
        con.execute("""
            CREATE TABLE raw_financials (
                ticker        VARCHAR,
                line_item     VARCHAR,
                concept_used  VARCHAR,
                period_end    DATE,
                period_type   VARCHAR,
                fiscal_year   INTEGER,
                fiscal_period VARCHAR,
                value         DOUBLE,
                unit          VARCHAR,
                accession_no  VARCHAR,
                fact_id       VARCHAR,
                filing_url    VARCHAR,
                form_type     VARCHAR,
                filed_date    DATE,
                frame         VARCHAR
            )
        """)
        rows = [
            (
                "TEST",
                "OperatingCashFlow",
                "NetCashProvidedByUsedInOperatingActivities",
                "2024-10-31",
                "Q",
                2025,
                "Q1",
                100.0,
                "USD",
                "0000000000-25-000001",
                "ocf-q1",
                "https://example/q1",
                "10-Q",
                "2024-11-20",
                "CY2024Q3",
            ),
            (
                "TEST",
                "OperatingCashFlow",
                "NetCashProvidedByUsedInOperatingActivities",
                "2025-07-31",
                "Q",
                2025,
                "Q4",
                600.0,
                "USD",
                "0000000000-25-000004",
                "ocf-fy-cumulative",
                "https://example/q4",
                "10-K",
                "2025-09-20",
                "",
            ),
        ]
        con.executemany(
            "INSERT INTO raw_financials VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        con.execute(_SQL_CANONICAL)

        df = _export_fact_financials(con, fy_end_month=7)
    finally:
        con.close()

    q4 = df[(df["line_item"] == "OperatingCashFlow") & (df["fiscal_period"] == "Q4")]
    assert len(q4) == 1
    assert q4.iloc[0]["value"] == 600.0
    assert "missing prior quarter baseline" in caplog.text
    assert "Q4" in caplog.text


def test_cash_flow_ytd_to_standalone_warns_on_duplicate_quarters(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two rows for the same (ticker, line_item, fiscal_year, q) must trigger a warning.

    Upstream dedup in ``_export_fact_financials`` normally strips duplicates
    before this function is called — so we exercise the warning branch by
    invoking ``_cash_flow_ytd_to_standalone`` directly with a synthetic frame.
    """
    import logging

    df = pd.DataFrame(
        [
            {
                "ticker": "TEST",
                "line_item": "OperatingCashFlow",
                "period_type": "Q",
                "fiscal_year": 2025,
                "fiscal_period": "Q1",
                "value": 80.0,
                "frame": "CY2024Q3",
            },
            {
                "ticker": "TEST",
                "line_item": "OperatingCashFlow",
                "period_type": "Q",
                "fiscal_year": 2025,
                "fiscal_period": "Q2",
                "value": 250.0,
                "frame": "",
            },
            {
                "ticker": "TEST",
                "line_item": "OperatingCashFlow",
                "period_type": "Q",
                "fiscal_year": 2025,
                "fiscal_period": "Q2",
                "value": 260.0,
                "frame": "",
            },
        ]
    )

    with caplog.at_level(logging.WARNING, logger="src.export_for_tableau"):
        out = _cash_flow_ytd_to_standalone(df)

    assert len(out) == len(df), "row count must be preserved"
    assert "duplicate cash-flow fiscal quarters" in caplog.text
    assert "OperatingCashFlow" in caplog.text
    assert "FY2025" in caplog.text
    assert "Q2" in caplog.text


# ── _export_fact_forecasts ────────────────────────────────────────────────────


def test_fact_forecasts_empty_stub_when_no_parquets(tmp_path: Path) -> None:
    """When no forecast parquets exist, fact_forecasts returns empty stub."""
    with patch("src.export_for_tableau._MODELS_DIR", tmp_path):
        df = _export_fact_forecasts("TESTONLY")
    assert len(df) == 0
    assert "model" in df.columns


def test_fact_forecasts_loads_parquet(tmp_path: Path) -> None:
    """fact_forecasts loads rows from a forecast parquet when present."""
    stub = pd.DataFrame(
        {
            "model": ["prophet"] * 4,
            "period_end": pd.date_range("2025-10-31", periods=4, freq="QE"),
            "yhat": [1e9, 1.1e9, 1.2e9, 1.3e9],
            "yhat_lower_80": [0.9e9] * 4,
            "yhat_upper_80": [1.1e9] * 4,
            "yhat_lower_95": [0.8e9] * 4,
            "yhat_upper_95": [1.2e9] * 4,
        }
    )
    stub.to_parquet(tmp_path / "TEST_baseline_forecasts.parquet", index=False)

    with patch("src.export_for_tableau._MODELS_DIR", tmp_path):
        df = _export_fact_forecasts("TEST")
    assert len(df) == 4
    assert (df["model"] == "prophet").all()


def test_fact_forecasts_dates_snapped_to_fiscal_quarter_end(tmp_path: Path) -> None:
    """Mixed-convention forecast dates snap to PANW fiscal quarter-end (Jan/Apr/Jul/Oct 31).

    Inputs intentionally mix:
      - quarter-start dates (autoarima, prophet) like 2026-04-01
      - quarter-end dates (lasso) like 2026-04-30
      - mid-quarter dates (2026-12-15) that should snap to the next fiscal QE
    All must land on a PANW fiscal quarter-end so they join 1:1 against dim_date.
    """
    stub = pd.DataFrame(
        {
            "model": ["autoarima", "prophet", "lasso", "lasso"],
            "period_end": pd.to_datetime(["2026-04-01", "2026-07-01", "2026-04-30", "2026-12-15"]),
            "yhat": [1e9] * 4,
            "yhat_lower_80": [0.9e9] * 4,
            "yhat_upper_80": [1.1e9] * 4,
            "yhat_lower_95": [0.8e9] * 4,
            "yhat_upper_95": [1.2e9] * 4,
        }
    )
    stub.to_parquet(tmp_path / "TEST_baseline_forecasts.parquet", index=False)

    with patch("src.export_for_tableau._MODELS_DIR", tmp_path):
        df = _export_fact_forecasts("TEST", fy_end_month=7)

    assert (df["line_item"] == "Revenue").all()
    expected = pd.to_datetime(["2026-04-30", "2026-07-31", "2026-04-30", "2027-01-31"])
    assert sorted(df["period_end"].tolist()) == sorted(expected.tolist())


def test_fact_forecasts_join_to_dim_date_is_one_to_one(tmp_path: Path) -> None:
    """Every row in fact_forecasts.csv must join exactly one row in dim_date.csv."""
    db_path = _build_warehouse_tmp("panw_companyfacts.json", "PANW", 1327567, tmp_path)
    tableau_dir = tmp_path / "tableau_data"
    config_path = tmp_path / "company.yaml"

    models_dir = tmp_path / "models"
    models_dir.mkdir()
    stub = pd.DataFrame(
        {
            "model": ["autoarima", "autoarima", "prophet", "prophet", "lasso", "lasso"],
            "period_end": pd.to_datetime(
                [
                    "2026-04-01",
                    "2026-07-01",
                    "2026-07-01",
                    "2026-10-01",
                    "2026-04-30",
                    "2026-07-31",
                ]
            ),
            "yhat": [1e9] * 6,
            "yhat_lower_80": [0.9e9] * 6,
            "yhat_upper_80": [1.1e9] * 6,
            "yhat_lower_95": [0.8e9] * 6,
            "yhat_upper_95": [1.2e9] * 6,
        }
    )
    stub.to_parquet(models_dir / "PANW_baseline_forecasts.parquet", index=False)

    with (
        patch("src.export_for_tableau._CONFIG_PATH", config_path),
        patch("src.export_for_tableau._PROCESSED_DIR", tmp_path),
        patch("src.export_for_tableau._MODELS_DIR", models_dir),
        patch("src.export_for_tableau._TABLEAU_DIR", tableau_dir),
        patch("src.export_for_tableau._DASHBOARD_DIR", tmp_path),
    ):
        export(ticker="PANW")

    fcst = pd.read_csv(tableau_dir / "fact_forecasts.csv")
    dim = pd.read_csv(tableau_dir / "dim_date.csv")
    merged = fcst.merge(dim, left_on="period_end", right_on="date_key", how="left", indicator=True)
    unmatched = merged[merged["_merge"] != "both"]
    assert unmatched.empty, (
        f"Forecast rows did not join to dim_date: "
        f"{unmatched[['model', 'period_end']].to_dict('records')}"
    )
    assert (fcst["line_item"] == "Revenue").all()


# ── Full export integration ────────────────────────────────────────────────────


def test_export_writes_all_csv_files(tmp_path: Path) -> None:
    """Full export() should write all six CSV files."""
    db_path = _build_warehouse_tmp("panw_companyfacts.json", "PANW", 1327567, tmp_path)
    tableau_dir = tmp_path / "tableau_data"
    config_path = tmp_path / "company.yaml"

    with (
        patch("src.export_for_tableau._CONFIG_PATH", config_path),
        patch("src.export_for_tableau._PROCESSED_DIR", tmp_path),
        patch("src.export_for_tableau._MODELS_DIR", tmp_path / "models"),
        patch("src.export_for_tableau._TABLEAU_DIR", tableau_dir),
        patch("src.export_for_tableau._DASHBOARD_DIR", tmp_path),
    ):
        paths = export(ticker="PANW")

    for name in (
        "fact_financials",
        "fact_forecasts",
        "dim_date",
        "dim_metric",
        "dim_filing",
    ):
        assert name in paths, f"Missing output: {name}"
        assert paths[name].exists(), f"File not written: {paths[name]}"


def test_export_fact_financials_accession_non_empty(tmp_path: Path) -> None:
    """fact_financials.csv must have at least some rows with non-null accession_no."""
    db_path = _build_warehouse_tmp("panw_companyfacts.json", "PANW", 1327567, tmp_path)
    tableau_dir = tmp_path / "tableau_data"
    config_path = tmp_path / "company.yaml"

    with (
        patch("src.export_for_tableau._CONFIG_PATH", config_path),
        patch("src.export_for_tableau._PROCESSED_DIR", tmp_path),
        patch("src.export_for_tableau._MODELS_DIR", tmp_path / "models"),
        patch("src.export_for_tableau._TABLEAU_DIR", tableau_dir),
        patch("src.export_for_tableau._DASHBOARD_DIR", tmp_path),
    ):
        export(ticker="PANW")

    df = pd.read_csv(tableau_dir / "fact_financials.csv")
    accession_rows = df[df["accession_no"].notna()]
    assert len(accession_rows) > 0, "No rows with accession_no in fact_financials.csv"
