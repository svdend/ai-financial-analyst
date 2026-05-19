"""Tests for src/ingest_edgar.py.

Uses recorded fixture files in tests/fixtures/ to avoid live EDGAR requests.
The fixtures mirror the real companyfacts JSON structure but with a minimal
subset of facts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from src.ingest_edgar import (
    CONCEPT_SYNONYMS,
    _fact_id,
    _filing_url,
    _period_type,
    ingest,
)

_FIXTURES = Path(__file__).parent / "fixtures"
_PROVENANCE_COLS = ("concept_used", "accession_no", "fact_id", "filing_url", "form_type")


# ── Fixture loading helpers ────────────────────────────────────────────────────


def _load_fixture(name: str) -> dict[str, Any]:
    with (_FIXTURES / name).open() as fh:
        return json.load(fh)  # type: ignore[no-any-return]


# ── Unit tests: pure helpers ───────────────────────────────────────────────────


def test_fact_id_stable() -> None:
    """_fact_id must produce the same 16-char hex for the same inputs."""
    fid = _fact_id(
        "PANW",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "2024-07-31",
        "FY",
        "0001327567-24-000030",
    )
    assert len(fid) == 16
    assert fid == _fact_id(
        "PANW",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "2024-07-31",
        "FY",
        "0001327567-24-000030",
    )


def test_fact_id_differs_on_period() -> None:
    """Different period_end must produce different fact IDs."""
    fid_a = _fact_id("PANW", "Revenues", "2024-07-31", "FY", "acc-1")
    fid_b = _fact_id("PANW", "Revenues", "2023-07-31", "FY", "acc-1")
    assert fid_a != fid_b


def test_filing_url_structure() -> None:
    """Filing URL must point to EDGAR Archives with dashes removed from accession."""
    url = _filing_url(1327567, "0001327567-24-000030")
    assert url == "https://www.sec.gov/Archives/edgar/data/1327567/000132756724000030/"
    assert "-" not in url.split("/")[-2]  # no dashes in the path segment


def test_period_type_fy() -> None:
    assert _period_type("FY") == "FY"


def test_period_type_quarterly() -> None:
    for fp in ("Q1", "Q2", "Q3", "Q4"):
        assert _period_type(fp) == "Q"


def test_concept_synonyms_ordered_structure() -> None:
    """Revenue synonyms: ExcludingAssessedTax must come before IncludingAssessedTax.

    This ordering ensures PANW (which reports Excluding) is chosen first, so
    the test 'PANW resolves Revenue via ...Excluding...' can pass deterministically.
    """
    rev_syns = CONCEPT_SYNONYMS["Revenue"]
    excl_idx = rev_syns.index("RevenueFromContractWithCustomerExcludingAssessedTax")
    incl_idx = rev_syns.index("RevenueFromContractWithCustomerIncludingAssessedTax")
    assert excl_idx < incl_idx, "ExcludingAssessedTax must precede IncludingAssessedTax"


# ── Integration tests: fixture-based ingestion ────────────────────────────────


def _run_ingest(fixture_name: str, ticker: str, cik_int: int, tmp_path: Path) -> pd.DataFrame:
    """Run ingest() against a fixture, pointing config/output at tmp_path."""
    facts = _load_fixture(fixture_name)
    # Patch config so ingest() uses the fixture's CIK
    import yaml  # noqa: PLC0415 — conditional import for test helper only

    config = {
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

    from unittest.mock import patch  # noqa: PLC0415

    with (
        patch("src.ingest_edgar._CONFIG_PATH", config_path),
        patch("src.ingest_edgar._DATA_DIR", tmp_path),
    ):
        return ingest(ticker=ticker, years=10, facts_json=facts)


@pytest.mark.parametrize(
    "fixture,ticker,cik_int",
    [
        ("panw_companyfacts.json", "PANW", 1327567),
        ("crwd_companyfacts.json", "CRWD", 1517396),
        ("snow_companyfacts.json", "SNOW", 1640147),
    ],
)
def test_provenance_columns_populated(
    fixture: str, ticker: str, cik_int: int, tmp_path: Path
) -> None:
    """Every output row must have all provenance columns non-empty."""
    df = _run_ingest(fixture, ticker, cik_int, tmp_path)
    for col in _PROVENANCE_COLS:
        assert col in df.columns, f"Missing column: {col}"
        nulls = df[col].isna() | (df[col].astype(str).str.strip() == "")
        assert not nulls.any(), f"Empty values in '{col}' for {ticker}"


def test_panw_revenue_concept() -> None:
    """PANW should resolve Revenue via ExcludingAssessedTax."""
    facts = _load_fixture("panw_companyfacts.json")
    # ExcludingAssessedTax is present in the PANW fixture; IncludingAssessedTax is not.
    from src.ingest_edgar import _extract_line_item  # noqa: PLC0415

    rows = _extract_line_item(
        facts, "Revenue", "PANW", 1327567, CONCEPT_SYNONYMS["Revenue"], min_fiscal_year=2020
    )
    assert rows, "Expected Revenue rows for PANW"
    used = {r["concept_used"] for r in rows}
    assert used == {"RevenueFromContractWithCustomerExcludingAssessedTax"}


def test_crwd_revenue_concept() -> None:
    """CRWD should resolve Revenue via IncludingAssessedTax (Excluding not in fixture)."""
    facts = _load_fixture("crwd_companyfacts.json")
    from src.ingest_edgar import _extract_line_item  # noqa: PLC0415

    rows = _extract_line_item(
        facts, "Revenue", "CRWD", 1517396, CONCEPT_SYNONYMS["Revenue"], min_fiscal_year=2020
    )
    assert rows, "Expected Revenue rows for CRWD"
    used = {r["concept_used"] for r in rows}
    assert used == {"RevenueFromContractWithCustomerIncludingAssessedTax"}


@pytest.mark.parametrize(
    "fixture,ticker,cik_int",
    [
        ("panw_companyfacts.json", "PANW", 1327567),
        ("crwd_companyfacts.json", "CRWD", 1517396),
        ("snow_companyfacts.json", "SNOW", 1640147),
    ],
)
def test_revenue_at_least_8_quarters(
    fixture: str, ticker: str, cik_int: int, tmp_path: Path
) -> None:
    """Revenue should resolve to ≥8 quarterly entries per company fixture."""
    df = _run_ingest(fixture, ticker, cik_int, tmp_path)
    rev = df[(df["line_item"] == "Revenue") & (df["period_type"] == "Q")]
    assert len(rev) >= 8, f"{ticker}: expected ≥8 Revenue quarters, got {len(rev)}"


def test_snow_has_no_inventory(tmp_path: Path) -> None:
    """SNOW fixture has no InventoryNet concept — Inventory line item must be absent."""
    df = _run_ingest("snow_companyfacts.json", "SNOW", 1640147, tmp_path)
    assert "Inventory" not in df["line_item"].values


def test_crwd_has_no_inventory(tmp_path: Path) -> None:
    """CRWD fixture has no InventoryNet concept — Inventory line item must be absent."""
    df = _run_ingest("crwd_companyfacts.json", "CRWD", 1517396, tmp_path)
    assert "Inventory" not in df["line_item"].values


def test_panw_has_inventory(tmp_path: Path) -> None:
    """PANW fixture has InventoryNet — Inventory line item must be present."""
    df = _run_ingest("panw_companyfacts.json", "PANW", 1327567, tmp_path)
    assert "Inventory" in df["line_item"].values


def test_output_schema_columns(tmp_path: Path) -> None:
    """Output DataFrame must contain all required schema columns."""
    df = _run_ingest("panw_companyfacts.json", "PANW", 1327567, tmp_path)
    required = {
        "ticker",
        "line_item",
        "concept_used",
        "period_end",
        "period_type",
        "fiscal_year",
        "fiscal_period",
        "value",
        "unit",
        "accession_no",
        "fact_id",
        "filing_url",
        "form_type",
        "filed_date",
        "frame",
    }
    assert required.issubset(set(df.columns))


def test_years_filter(tmp_path: Path) -> None:
    """--years 2 should retain only the 2 most-recent fiscal years."""
    facts = _load_fixture("panw_companyfacts.json")
    import yaml  # noqa: PLC0415

    config = {
        "cik": "0001327567",
        "cik_int": 1327567,
        "ticker": "PANW",
        "name": "Palo Alto Networks Inc",
        "fiscal_year_end_month": 7,
        "fiscal_year_end_day": 31,
        "sector_etf": "XLK",
    }
    config_path = tmp_path / "company.yaml"
    with config_path.open("w") as fh:
        yaml.dump(config, fh)

    from unittest.mock import patch  # noqa: PLC0415

    with (
        patch("src.ingest_edgar._CONFIG_PATH", config_path),
        patch("src.ingest_edgar._DATA_DIR", tmp_path),
    ):
        df = ingest(ticker="PANW", years=2, facts_json=facts)

    max_fy = df["fiscal_year"].max()
    assert df["fiscal_year"].min() >= max_fy - 1
