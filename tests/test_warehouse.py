"""Tests for src/build_warehouse.py.

Builds a DuckDB warehouse from fixture parquets and verifies all views,
including the has_physical_inventory and has_restatement flags.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import duckdb
import pandas as pd
import pytest
import yaml

from src.build_warehouse import build, query_summary
from src.ingest_edgar import ingest

# ── Fixture helpers ────────────────────────────────────────────────────────────

_FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict[str, Any]:
    with (_FIXTURES / name).open() as fh:
        return json.load(fh)  # type: ignore[no-any-return]


def _build_parquet(
    fixture_name: str,
    ticker: str,
    cik_int: int,
    tmp_path: Path,
) -> Path:
    """Ingest fixture → parquet in tmp_path; return parquet path."""
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

    return tmp_path / f"{ticker}_financials.parquet"


def _build_warehouse(
    fixture_name: str,
    ticker: str,
    cik_int: int,
    tmp_path: Path,
) -> Path:
    """Ingest fixture → parquet → DuckDB; return .duckdb path."""
    _build_parquet(fixture_name, ticker, cik_int, tmp_path)
    config_path = tmp_path / "company.yaml"

    with (
        patch("src.build_warehouse._CONFIG_PATH", config_path),
        patch("src.build_warehouse._PROCESSED_DIR", tmp_path),
    ):
        return build(ticker=ticker)


# ── has_physical_inventory ────────────────────────────────────────────────────


def test_has_physical_inventory_true_for_panw(tmp_path: Path) -> None:
    """PANW fixture has InventoryNet → has_physical_inventory must be TRUE."""
    db_path = _build_warehouse("panw_companyfacts.json", "PANW", 1327567, tmp_path)
    summary = query_summary(db_path)
    assert bool(summary["has_physical_inventory"]) is True


def test_has_physical_inventory_false_for_crwd(tmp_path: Path) -> None:
    """CRWD fixture has no Inventory → has_physical_inventory must be FALSE."""
    db_path = _build_warehouse("crwd_companyfacts.json", "CRWD", 1517396, tmp_path)
    summary = query_summary(db_path)
    assert bool(summary["has_physical_inventory"]) is False


def test_has_physical_inventory_false_for_snow(tmp_path: Path) -> None:
    """SNOW fixture has no Inventory → has_physical_inventory must be FALSE."""
    db_path = _build_warehouse("snow_companyfacts.json", "SNOW", 1640147, tmp_path)
    summary = query_summary(db_path)
    assert bool(summary["has_physical_inventory"]) is False


# ── has_restatement ───────────────────────────────────────────────────────────


def test_has_restatement_false_for_clean_data(tmp_path: Path) -> None:
    """PANW fixture has no /A filings → has_restatement must be FALSE."""
    db_path = _build_warehouse("panw_companyfacts.json", "PANW", 1327567, tmp_path)
    summary = query_summary(db_path)
    assert bool(summary["has_restatement"]) is False


def test_has_restatement_true_for_amendment_fixture(tmp_path: Path) -> None:
    """Restatement fixture has a 10-K/A that materially differs → TRUE."""
    db_path = _build_warehouse("restatement_companyfacts.json", "TEST", 9999999, tmp_path)
    summary = query_summary(db_path)
    assert bool(summary["has_restatement"]) is True


def test_restatement_details_populated(tmp_path: Path) -> None:
    """v_restatement_details should have rows for the amended period."""
    db_path = _build_warehouse("restatement_companyfacts.json", "TEST", 9999999, tmp_path)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute("SELECT * FROM v_restatement_details").fetchdf()
    finally:
        con.close()
    assert len(rows) > 0
    assert "amending_accession_no" in rows.columns
    # Amendment value is 1.05B vs original 1.0B → 5% diff (above 0.1% threshold)
    assert (rows["rel_diff"] > 0.001).all()


# ── View structure and content ────────────────────────────────────────────────


def test_income_statement_view_has_revenue(tmp_path: Path) -> None:
    """v_income_statement_quarterly must have non-null Revenue rows for PANW."""
    db_path = _build_warehouse("panw_companyfacts.json", "PANW", 1327567, tmp_path)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        df = con.execute(
            "SELECT * FROM v_income_statement_quarterly WHERE Revenue IS NOT NULL"
        ).fetchdf()
    finally:
        con.close()
    assert len(df) > 0
    assert "revenue_accession" in df.columns
    assert "revenue_fact_id" in df.columns
    assert "revenue_filing_url" in df.columns


def test_income_statement_provenance_populated(tmp_path: Path) -> None:
    """Revenue provenance columns must be non-null wherever Revenue is not null."""
    db_path = _build_warehouse("panw_companyfacts.json", "PANW", 1327567, tmp_path)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        df = con.execute(
            "SELECT revenue_fact_id, revenue_accession, revenue_filing_url "
            "FROM v_income_statement_quarterly WHERE Revenue IS NOT NULL"
        ).fetchdf()
    finally:
        con.close()
    for col in ("revenue_fact_id", "revenue_accession", "revenue_filing_url"):
        assert df[col].notna().all(), f"Null provenance in {col}"


def test_balance_sheet_inventory_null_for_saas(tmp_path: Path) -> None:
    """Pure-SaaS company (CRWD) should have all-NULL Inventory column in BS view."""
    db_path = _build_warehouse("crwd_companyfacts.json", "CRWD", 1517396, tmp_path)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        df = con.execute(
            "SELECT Inventory FROM v_balance_sheet_quarterly WHERE Inventory IS NOT NULL"
        ).fetchdf()
    finally:
        con.close()
    assert len(df) == 0, "CRWD should have no Inventory rows"


def test_canonical_facts_deduplicates(tmp_path: Path) -> None:
    """v_canonical_facts should return exactly one row per (line_item, period_end, frame)."""
    db_path = _build_warehouse("panw_companyfacts.json", "PANW", 1327567, tmp_path)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        dup_check = con.execute(
            """
            SELECT line_item, period_end, period_type, frame, COUNT(*) AS cnt
            FROM v_canonical_facts
            GROUP BY line_item, period_end, period_type, frame
            HAVING cnt > 1
            """
        ).fetchdf()
    finally:
        con.close()
    assert len(dup_check) == 0, f"Duplicate canonical facts found:\n{dup_check}"


def test_warehouse_exposes_new_line_items(tmp_path: Path) -> None:
    """New Week-2 line items must flow through v_canonical_facts.

    Builds a synthetic warehouse from a parquet that includes one row per new
    line_item (LongTermDebt, ShortTermDebt, SharesOutstanding, DilutedShares,
    CurrentAssets, CurrentLiabilities, RPO) and verifies each appears in the
    canonical view with the correct unit.
    """
    new_items: list[tuple[str, str, str]] = [
        ("LongTermDebt", "LongTermDebtNoncurrent", "USD"),
        ("ShortTermDebt", "LongTermDebtCurrent", "USD"),
        ("SharesOutstanding", "EntityCommonStockSharesOutstanding", "shares"),
        ("DilutedShares", "WeightedAverageNumberOfDilutedSharesOutstanding", "shares"),
        ("CurrentAssets", "AssetsCurrent", "USD"),
        ("CurrentLiabilities", "LiabilitiesCurrent", "USD"),
        ("RPO", "RevenueRemainingPerformanceObligation", "USD"),
    ]
    rows: list[dict[str, Any]] = []
    for line_item, concept, unit in new_items:
        for fy, fp, end in _QUARTERS:
            rows.append(
                {
                    "ticker": "TEST",
                    "line_item": line_item,
                    "concept_used": concept,
                    "period_end": end,
                    "period_type": "Q",
                    "fiscal_year": fy,
                    "fiscal_period": fp,
                    "value": 1_000_000.0,
                    "unit": unit,
                    "accession_no": f"0000000000-{fy % 100:02d}-000001",
                    "fact_id": f"{line_item}-{fy}-{fp}",
                    "filing_url": f"https://example.test/{line_item}/",
                    "form_type": "10-Q",
                    "filed_date": end,
                    "frame": f"CY{fy}{fp}",
                }
            )

    db_path = _build_synthetic_warehouse(rows, tmp_path)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        df = con.execute("SELECT line_item, unit FROM v_canonical_facts").fetchdf()
    finally:
        con.close()
    surfaced = set(df["line_item"].unique())
    for line_item, _concept, expected_unit in new_items:
        assert (
            line_item in surfaced
        ), f"{line_item} missing from v_canonical_facts; got {sorted(surfaced)}"
        units = set(df.loc[df["line_item"] == line_item, "unit"].unique())
        assert units == {
            expected_unit
        }, f"{line_item} should have unit {expected_unit!r}, got {units}"


def test_key_metrics_quarterly_only(tmp_path: Path) -> None:
    """v_key_metrics must contain only quarterly rows."""
    db_path = _build_warehouse("panw_companyfacts.json", "PANW", 1327567, tmp_path)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        df = con.execute("SELECT DISTINCT period_type FROM v_key_metrics").fetchdf()
    finally:
        con.close()
    assert set(df["period_type"].tolist()) == {"Q"}


def test_data_quality_row_count(tmp_path: Path) -> None:
    """v_data_quality returns exactly one summary row."""
    db_path = _build_warehouse("panw_companyfacts.json", "PANW", 1327567, tmp_path)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        df = con.execute("SELECT * FROM v_data_quality").fetchdf()
    finally:
        con.close()
    assert len(df) == 1


# ── missing_quarters scalar ───────────────────────────────────────────────────
# These tests bypass the EDGAR JSON path and write a raw_financials parquet
# directly so each scenario can pin a specific (line_item, quarter) gap.


_LOAD_BEARING = ("Revenue", "OperatingIncome", "OperatingCashFlow", "NetIncome")
_QUARTERS = (
    (2024, "Q1", "2024-01-31"),
    (2024, "Q2", "2024-04-30"),
    (2024, "Q3", "2024-07-31"),
    (2024, "Q4", "2024-10-31"),
)


def _row(line_item: str, fy: int, fp: str, period_end: str, val: float) -> dict[str, Any]:
    """Build a single synthetic raw_financials row."""
    accn = f"0000000000-{fy % 100:02d}-000001"
    return {
        "ticker": "TEST",
        "line_item": line_item,
        "concept_used": line_item,
        "period_end": period_end,
        "period_type": "Q",
        "fiscal_year": fy,
        "fiscal_period": fp,
        "value": val,
        "unit": "USD",
        "accession_no": accn,
        "fact_id": f"{line_item}-{fy}-{fp}",
        "filing_url": f"https://example.test/{accn}/",
        "form_type": "10-Q",
        "filed_date": period_end,
        "frame": f"CY{fy}{fp}",
    }


def _build_synthetic_warehouse(rows: list[dict[str, Any]], tmp_path: Path) -> Path:
    """Write rows to a parquet + minimal company.yaml, then build the warehouse."""
    df = pd.DataFrame(rows)
    parquet_path = tmp_path / "TEST_financials.parquet"
    df.to_parquet(parquet_path, index=False)

    config_path = tmp_path / "company.yaml"
    with config_path.open("w") as fh:
        yaml.dump(
            {
                "cik": "0000000000",
                "cik_int": 0,
                "ticker": "TEST",
                "name": "Test",
                "fiscal_year_end_month": 10,
                "fiscal_year_end_day": 31,
                "sector_etf": "XLK",
            },
            fh,
        )

    with (
        patch("src.build_warehouse._CONFIG_PATH", config_path),
        patch("src.build_warehouse._PROCESSED_DIR", tmp_path),
    ):
        return build(ticker="TEST")


def _missing_quarters(db_path: Path) -> str | None:
    """Return missing_quarters scalar from v_data_quality."""
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        row = con.execute("SELECT missing_quarters FROM v_data_quality").fetchone()
    finally:
        con.close()
    assert row is not None
    return row[0]


def test_missing_quarters_empty_when_complete(tmp_path: Path) -> None:
    """All load-bearing line items present for every quarter → missing_quarters empty."""
    rows = [
        _row(li, fy, fp, end, 1_000_000.0) for li in _LOAD_BEARING for (fy, fp, end) in _QUARTERS
    ]
    db_path = _build_synthetic_warehouse(rows, tmp_path)
    missing = _missing_quarters(db_path)
    # Empty string OR NULL — both are handled correctly by the refusal branch.
    assert not missing, f"expected empty/NULL, got {missing!r}"


def test_missing_quarters_flags_revenue_gap(tmp_path: Path) -> None:
    """Revenue missing for FY2024Q2 → missing_quarters contains 'FY2024Q2'."""
    rows = [
        _row(li, fy, fp, end, 1_000_000.0)
        for li in _LOAD_BEARING
        for (fy, fp, end) in _QUARTERS
        if not (li == "Revenue" and fp == "Q2")
    ]
    db_path = _build_synthetic_warehouse(rows, tmp_path)
    missing = _missing_quarters(db_path)
    assert (
        missing is not None and "FY2024Q2" in missing
    ), f"expected FY2024Q2 in missing_quarters, got {missing!r}"


def test_missing_quarters_ignores_inventory_gap(tmp_path: Path) -> None:
    """Only Inventory missing → missing_quarters empty (Inventory is excluded)."""
    rows = [
        _row(li, fy, fp, end, 1_000_000.0) for li in _LOAD_BEARING for (fy, fp, end) in _QUARTERS
    ]
    # Inventory present for three quarters, missing for Q2 — must NOT be flagged.
    rows.extend(
        _row("Inventory", fy, fp, end, 50_000.0) for (fy, fp, end) in _QUARTERS if fp != "Q2"
    )
    db_path = _build_synthetic_warehouse(rows, tmp_path)
    missing = _missing_quarters(db_path)
    assert not missing, f"expected empty/NULL (Inventory excluded), got {missing!r}"


# ── v_fcf_bridge ──────────────────────────────────────────────────────────────


def _bridge_rows(
    fy: int,
    fp: str,
    period_end: str,
    *,
    net_income: float,
    depreciation: float,
    sbc: float,
    ocf: float,
    capex: float,
) -> list[dict[str, Any]]:
    """Synthetic raw_financials rows for one quarter, sufficient for v_fcf_bridge.

    Working capital is implicit: ocf - net_income - depreciation - sbc.  The
    plug is what the view computes, so callers control it indirectly via OCF.
    """
    return [
        _row("NetIncome", fy, fp, period_end, net_income),
        _row("Depreciation", fy, fp, period_end, depreciation),
        _row("StockBasedCompensation", fy, fp, period_end, sbc),
        _row("OperatingCashFlow", fy, fp, period_end, ocf),
        _row("CapEx", fy, fp, period_end, capex),
    ]


def test_fcf_bridge_view_emits_six_components(tmp_path: Path) -> None:
    """v_fcf_bridge must emit one row per bridge component for each quarter."""
    rows = _bridge_rows(
        2024,
        "Q1",
        "2024-01-31",
        net_income=100.0,
        depreciation=50.0,
        sbc=80.0,
        ocf=300.0,
        capex=40.0,
    )
    db_path = _build_synthetic_warehouse(rows, tmp_path)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        df = con.execute(
            "SELECT * FROM v_fcf_bridge ORDER BY period_end, component_order"
        ).fetchdf()
    finally:
        con.close()
    components = df["component"].tolist()
    assert components == [
        "NetIncome",
        "Depreciation",
        "StockBasedCompensation",
        "WorkingCapitalAndOther",
        "OperatingCashFlow",
        "CapEx",
        "FreeCashFlow",
    ], f"unexpected component order: {components}"


def test_fcf_bridge_reconciles_to_free_cash_flow(tmp_path: Path) -> None:
    """Sum of NI + D&A + SBC + ΔWC equals OCF; OCF − CapEx equals FCF.

    The plug ('WorkingCapitalAndOther') is computed as OCF − NI − D&A − SBC,
    so the additive bars from NetIncome through WorkingCapitalAndOther must
    sum to OCF *exactly* by construction.  The terminal FCF must equal
    OCF − CapEx (CapEx is reported positive on the cash-flow statement and
    is sign-flipped in the bridge).
    """
    rows = _bridge_rows(
        2024,
        "Q1",
        "2024-01-31",
        net_income=100.0,
        depreciation=50.0,
        sbc=80.0,
        ocf=300.0,
        capex=40.0,
    )
    db_path = _build_synthetic_warehouse(rows, tmp_path)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        df = con.execute(
            "SELECT component, value FROM v_fcf_bridge "
            "WHERE period_end = '2024-01-31' ORDER BY component_order"
        ).fetchdf()
    finally:
        con.close()
    by_component = dict(zip(df["component"], df["value"], strict=True))

    # Plug is the residual that makes NI + D&A + SBC + ΔWC = OCF.
    expected_wc = 300.0 - 100.0 - 50.0 - 80.0  # = 70
    assert by_component["WorkingCapitalAndOther"] == pytest.approx(expected_wc)

    # OCF subtotal equals the sum of the four additive bars.
    additive_sum = (
        by_component["NetIncome"]
        + by_component["Depreciation"]
        + by_component["StockBasedCompensation"]
        + by_component["WorkingCapitalAndOther"]
    )
    assert additive_sum == pytest.approx(by_component["OperatingCashFlow"])

    # CapEx is sign-flipped (cash *out*) in the bridge.
    assert by_component["CapEx"] == pytest.approx(-40.0)

    # FCF terminal equals OCF + signed CapEx.
    assert by_component["FreeCashFlow"] == pytest.approx(
        by_component["OperatingCashFlow"] + by_component["CapEx"]
    )


def test_fcf_bridge_carries_provenance_for_filed_components(tmp_path: Path) -> None:
    """Each filed component (NI, D&A, SBC, OCF, CapEx) must carry accession_no.

    The plug 'WorkingCapitalAndOther' is derived (no single source filing) so
    its accession_no is NULL.  This is intentional and documented — the
    Tableau spec surfaces the four contributing accessions in the tooltip.
    """
    rows = _bridge_rows(
        2024,
        "Q1",
        "2024-01-31",
        net_income=100.0,
        depreciation=50.0,
        sbc=80.0,
        ocf=300.0,
        capex=40.0,
    )
    db_path = _build_synthetic_warehouse(rows, tmp_path)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        df = con.execute(
            "SELECT component, accession_no FROM v_fcf_bridge "
            "WHERE period_end = '2024-01-31' ORDER BY component_order"
        ).fetchdf()
    finally:
        con.close()
    accn_by_component = dict(zip(df["component"], df["accession_no"], strict=True))
    filed_components = [
        "NetIncome",
        "Depreciation",
        "StockBasedCompensation",
        "OperatingCashFlow",
        "CapEx",
        "FreeCashFlow",
    ]
    for c in filed_components:
        assert pd.notna(accn_by_component[c]), f"component {c} must carry a non-null accession_no"
    # The plug is the only derived bar without a single source accession.
    assert pd.isna(accn_by_component["WorkingCapitalAndOther"])


def test_fcf_bridge_skips_quarter_missing_required_component(tmp_path: Path) -> None:
    """A quarter missing any of NI/D&A/SBC/OCF/CapEx should not produce bridge rows.

    The bridge is meaningless if any component is absent — we'd be filling in
    zeros that look like real $0 contributions.  Skip the quarter entirely and
    let v_missing_coverage flag the gap.
    """
    # Q1 has every component.  Q2 is missing CapEx.
    q1_rows = _bridge_rows(
        2024,
        "Q1",
        "2024-01-31",
        net_income=100.0,
        depreciation=50.0,
        sbc=80.0,
        ocf=300.0,
        capex=40.0,
    )
    q2_rows = [
        _row("NetIncome", 2024, "Q2", "2024-04-30", 110.0),
        _row("Depreciation", 2024, "Q2", "2024-04-30", 55.0),
        _row("StockBasedCompensation", 2024, "Q2", "2024-04-30", 85.0),
        _row("OperatingCashFlow", 2024, "Q2", "2024-04-30", 320.0),
        # CapEx intentionally missing.
    ]
    db_path = _build_synthetic_warehouse(q1_rows + q2_rows, tmp_path)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        df = con.execute(
            "SELECT DISTINCT period_end FROM v_fcf_bridge ORDER BY period_end"
        ).fetchdf()
    finally:
        con.close()
    period_ends = [str(pe) for pe in df["period_end"].tolist()]
    assert period_ends == [
        "2024-01-31"
    ], f"Q2 should be skipped (missing CapEx), got period_ends={period_ends}"
