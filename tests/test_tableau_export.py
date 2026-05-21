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
