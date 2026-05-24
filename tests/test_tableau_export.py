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
import yaml

from src.build_warehouse import build as build_warehouse
from src.export_for_tableau import (
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
    """PANW FY ends July (month 7). Q1 starts August → cal Q3."""
    cal_year, cal_q = _fiscal_to_calendar_quarter(2024, "Q1", fy_end_month=7)
    assert cal_q in (3, 4)  # August–October maps to Q3 or Q4


def test_fiscal_to_calendar_quarter_q4() -> None:
    """FY ending December: Q4 should be calendar Q4."""
    cal_year, cal_q = _fiscal_to_calendar_quarter(2024, "Q4", fy_end_month=12)
    assert cal_q == 4
    assert cal_year == 2024


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


def test_fact_financials_derived_metrics_present(tmp_path: Path) -> None:
    """fact_financials should include derived metrics (operating_margin_pct or revenue_yoy_growth)."""
    db_path = _build_warehouse_tmp("panw_companyfacts.json", "PANW", 1327567, tmp_path)
    import duckdb

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        df = _export_fact_financials(con)
    finally:
        con.close()
    derived = {"operating_margin_pct", "revenue_yoy_growth", "revenue_qoq_growth"}
    found = derived & set(df["line_item"].values)
    assert (
        found
    ), f"No derived metrics found in fact_financials; got line_items: {df['line_item'].unique()}"


def test_fact_financials_no_ytd_duplicates(tmp_path: Path) -> None:
    """fact_financials must have at most one row per (line_item, period_end, fiscal_period).

    SEC XBRL filings report both 3-month standalone and YTD cumulative values
    for the same concept and period_end.  The export must deduplicate these,
    keeping only the standalone quarterly value (minimum absolute value).
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
        # _export_fact_financials also reads v_key_metrics for derived metrics;
        # an empty stub is enough — the dedup invariant is what we assert.
        con.execute("""
            CREATE VIEW v_key_metrics AS
            SELECT
                CAST(NULL AS DATE)    AS period_end,
                CAST(NULL AS INTEGER) AS fiscal_year,
                CAST(NULL AS VARCHAR) AS fiscal_period,
                CAST(NULL AS DOUBLE)  AS gross_margin_pct,
                CAST(NULL AS DOUBLE)  AS operating_margin_pct,
                CAST(NULL AS DOUBLE)  AS net_margin_pct,
                CAST(NULL AS DOUBLE)  AS fcf_margin_pct,
                CAST(NULL AS DOUBLE)  AS revenue_yoy_growth,
                CAST(NULL AS DOUBLE)  AS revenue_qoq_growth
            WHERE FALSE
        """)

        df = _export_fact_financials(con)
    finally:
        con.close()

    # Exactly one row per (ticker, line_item, period_end) — the load-bearing
    # invariant for Tableau's AVG aggregation.
    dupes = df.duplicated(subset=["ticker", "line_item", "period_end"], keep=False)
    assert not dupes.any(), (
        "Multi-fiscal-year comparative duplicates not collapsed; got:\n"
        f"{df[dupes][['ticker', 'line_item', 'period_end', 'fiscal_year', 'value', 'filed_date']].to_string()}"
    )

    # The newer filing (FY2026-Q1 10-Q, filed 2025-11-20) must win.
    revenue = df[(df["line_item"] == "Revenue") & (df["period_end"].astype(str) == "2024-10-31")]
    assert len(revenue) == 1
    assert int(revenue.iloc[0]["fiscal_year"]) == 2026
    assert revenue.iloc[0]["accession_no"] == "0000000000-25-000001"


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
    ):
        export(ticker="PANW")

    df = pd.read_csv(tableau_dir / "fact_financials.csv")
    accession_rows = df[df["accession_no"].notna()]
    assert len(accession_rows) > 0, "No rows with accession_no in fact_financials.csv"
