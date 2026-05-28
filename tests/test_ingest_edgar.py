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


# ── Concept-selection logic (most-quarterly-coverage wins) ─────────────────────


def _synthetic_facts(concept_facts: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Build a minimal companyfacts JSON with the given concept → USD facts mapping."""
    return {
        "facts": {
            "us-gaap": {
                concept: {"units": {"USD": facts}} for concept, facts in concept_facts.items()
            }
        }
    }


def _q_fact(
    period_end: str, fy: int, fp: str, val: float, accn: str = "0000000-00-000001"
) -> dict[str, Any]:
    """One synthetic quarterly fact row."""
    return {
        "end": period_end,
        "fy": fy,
        "fp": fp,
        "val": val,
        "accn": accn,
        "form": "10-Q",
        "filed": "2025-01-01",
    }


def test_concept_selection_picks_highest_coverage() -> None:
    """When two concepts both have data, pick the one with more quarterly facts.

    Mirrors the real-world PANW CapEx case: pre-2022 reported under
    PaymentsToAcquirePropertyPlantAndEquipment (2 facts), post-2022 under
    PaymentsToAcquireProductiveAssets (many facts).  The bug we're guarding
    against: 'first non-empty wins' would pick the 2-fact concept and silently
    drop the rest of history.
    """
    from src.ingest_edgar import _extract_line_item  # noqa: PLC0415

    facts = _synthetic_facts(
        {
            "PaymentsToAcquirePropertyPlantAndEquipment": [
                _q_fact("2022-01-31", 2022, "Q2", 39_500_000),
                _q_fact("2022-04-30", 2022, "Q3", 39_500_000),
            ],
            "PaymentsToAcquireProductiveAssets": [
                _q_fact("2024-10-31", 2025, "Q1", 84_000_000),
                _q_fact("2025-01-31", 2025, "Q2", 92_000_000),
                _q_fact("2025-04-30", 2025, "Q3", 159_900_000),
                _q_fact("2025-10-31", 2026, "Q1", 84_000_000),
                _q_fact("2026-01-31", 2026, "Q2", 254_000_000),
            ],
        }
    )
    rows = _extract_line_item(
        facts, "CapEx", "PANW", 1327567, CONCEPT_SYNONYMS["CapEx"], min_fiscal_year=2020
    )
    used = {r["concept_used"] for r in rows}
    assert used == {
        "PaymentsToAcquireProductiveAssets"
    }, f"Expected the higher-coverage concept to win; got {used}"
    assert len(rows) == 5


def test_concept_selection_breaks_ties_by_list_order() -> None:
    """When two concepts have equal coverage, the one listed first in
    CONCEPT_SYNONYMS wins (deterministic tiebreak)."""
    from src.ingest_edgar import _extract_line_item  # noqa: PLC0415

    # Build two concepts with identical fact counts (3 quarters each).
    facts = _synthetic_facts(
        {
            "PaymentsToAcquirePropertyPlantAndEquipment": [
                _q_fact("2022-01-31", 2022, "Q2", 1_000),
                _q_fact("2022-04-30", 2022, "Q3", 1_000),
                _q_fact("2022-07-31", 2022, "Q4", 1_000),
            ],
            "PaymentsToAcquireProductiveAssets": [
                _q_fact("2023-01-31", 2023, "Q2", 2_000),
                _q_fact("2023-04-30", 2023, "Q3", 2_000),
                _q_fact("2023-07-31", 2023, "Q4", 2_000),
            ],
        }
    )
    rows = _extract_line_item(
        facts, "CapEx", "ANY", 1, CONCEPT_SYNONYMS["CapEx"], min_fiscal_year=2020
    )
    # CONCEPT_SYNONYMS lists PaymentsToAcquirePropertyPlantAndEquipment first.
    used = {r["concept_used"] for r in rows}
    assert used == {"PaymentsToAcquirePropertyPlantAndEquipment"}


def test_concept_selection_warns_on_concept_switch(caplog: pytest.LogCaptureFixture) -> None:
    """When two concepts both have substantive coverage (>2 quarters each),
    log a warning so concept-switch events aren't silently masked."""
    import logging  # noqa: PLC0415

    from src.ingest_edgar import _extract_line_item  # noqa: PLC0415

    facts = _synthetic_facts(
        {
            "PaymentsToAcquirePropertyPlantAndEquipment": [
                _q_fact("2021-01-31", 2021, "Q2", 1_000),
                _q_fact("2021-04-30", 2021, "Q3", 1_000),
                _q_fact("2021-07-31", 2021, "Q4", 1_000),
            ],
            "PaymentsToAcquireProductiveAssets": [
                _q_fact("2024-01-31", 2024, "Q2", 2_000),
                _q_fact("2024-04-30", 2024, "Q3", 2_000),
                _q_fact("2024-07-31", 2024, "Q4", 2_000),
                _q_fact("2024-10-31", 2025, "Q1", 2_000),
            ],
        }
    )
    with caplog.at_level(logging.WARNING, logger="src.ingest_edgar"):
        rows = _extract_line_item(
            facts, "CapEx", "ANY", 1, CONCEPT_SYNONYMS["CapEx"], min_fiscal_year=2020
        )

    assert rows, "Expected rows from the higher-coverage concept"
    assert any(
        "concept-switch detected" in rec.message and "CapEx" in rec.message
        for rec in caplog.records
    ), f"Expected concept-switch warning; got: {[r.message for r in caplog.records]}"


def test_concept_selection_prefers_recent_over_legacy() -> None:
    """Recency-aware: a discontinued concept with more lifetime facts must NOT
    outvote the current concept whose facts fall in the keep window.

    Real-world case: PANW reported Revenue under SalesRevenueNet through FY2018
    (130 facts), then switched to RevenueFromContractWithCustomerExcludingAssessedTax
    from FY2019 onwards (113 facts within keep window).  A naïve max-lifetime-
    coverage rule would pick the obsolete concept; the in-window rule picks
    the current concept correctly.
    """
    from src.ingest_edgar import _extract_line_item  # noqa: PLC0415

    facts = _synthetic_facts(
        {
            # Legacy concept: 8 quarterly facts but all in FY2015-FY2018
            "SalesRevenueNet": [
                _q_fact(f"{y}-{m}-30", y, fp, 1_000_000)
                for y in (2015, 2016, 2017, 2018)
                for fp, m in (("Q1", "10"), ("Q2", "01"))
            ],
            # Current concept: 4 quarterly facts in FY2024
            "RevenueFromContractWithCustomerExcludingAssessedTax": [
                _q_fact("2024-01-31", 2024, "Q2", 2_000_000),
                _q_fact("2024-04-30", 2024, "Q3", 2_000_000),
                _q_fact("2024-07-31", 2024, "Q4", 2_000_000),
                _q_fact("2024-10-31", 2025, "Q1", 2_000_000),
            ],
        }
    )
    rows = _extract_line_item(
        facts,
        "Revenue",
        "PANW",
        1327567,
        ["SalesRevenueNet", "RevenueFromContractWithCustomerExcludingAssessedTax"],
        min_fiscal_year=2020,
    )
    used = {r["concept_used"] for r in rows}
    assert used == {
        "RevenueFromContractWithCustomerExcludingAssessedTax"
    }, f"Expected the in-window concept to win over the legacy concept; got {used}"


def test_concept_selection_ignores_empty_concept() -> None:
    """A concept entry that exists but has zero quarterly facts (e.g. annual-
    only or stub) is ignored — should not block selection of a real concept."""
    from src.ingest_edgar import _extract_line_item  # noqa: PLC0415

    facts = _synthetic_facts(
        {
            "PaymentsToAcquirePropertyPlantAndEquipment": [
                # FY-only fact, not quarterly — should not be counted as coverage
                _q_fact("2022-07-31", 2022, "FY", 100_000),
            ],
            "PaymentsToAcquireProductiveAssets": [
                _q_fact("2025-01-31", 2025, "Q2", 92_000_000),
                _q_fact("2025-04-30", 2025, "Q3", 159_900_000),
            ],
        }
    )
    rows = _extract_line_item(
        facts, "CapEx", "ANY", 1, CONCEPT_SYNONYMS["CapEx"], min_fiscal_year=2020
    )
    # The first concept has FY-only coverage (0 quarterly); should pick the
    # second concept, but the FY row in the first concept should ALSO survive
    # if it's in _VALID_FP — actually _VALID_FP includes FY, so the first
    # concept has 1 valid fact total but 0 *quarterly* facts.  The selection
    # rule scores by quarterly count, so the second concept wins.
    used = {r["concept_used"] for r in rows}
    assert used == {"PaymentsToAcquireProductiveAssets"}


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
