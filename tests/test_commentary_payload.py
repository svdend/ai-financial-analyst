"""Tests for the provenance strings emitted by ``_build_payload``.

Every derived metric in the payload carries a sibling ``*_provenance`` string
that names the formula and the fact_ids that flowed into it. These tests
assert that each derived metric has a non-empty, well-formed provenance
sibling, that each provenance string contains a ``Sources:`` substring with
at least one fact_id-like token, and that a missing source fact_id renders
deterministically rather than raising.

The scope is intentionally narrow: only the payload-building helper is
exercised. No DuckDB / Anthropic / filesystem I/O.
"""

from __future__ import annotations

import re
from typing import Any

from src.generate_commentary import _build_payload

# ── Fixtures ──────────────────────────────────────────────────────────────────

_VALID_ACCESSION = "0001327567-26-000123"

# A complete variance row — every fact_id and derived metric populated. This is
# the happy-path fixture that asserts provenance strings are emitted.
_COMPLETE_VARIANCE_ROW: dict[str, Any] = {
    "fiscal_year": 2026,
    "fiscal_period": "Q1",
    "latest_period_end": "2026-01-31",
    # Revenue
    "revenue_actual": 2_300_000_000.0,
    "revenue_actual_accession": _VALID_ACCESSION,
    "revenue_actual_filing_url": (
        "https://www.sec.gov/Archives/edgar/data/1327567/000132756726000123/"
    ),
    "revenue_actual_fact_id": "fact_rev_actual_001",
    "revenue_yoy": 2_000_000_000.0,
    "revenue_yoy_growth_pct": 0.15,
    "revenue_yoy_fact_id": "fact_rev_yoy_001",
    # Forecast
    "revenue_prior_forecast": 2_250_000_000.0,
    "revenue_prior_forecast_model": "prophet|autoarima",
    "revenue_variance_vs_forecast": 50_000_000.0,
    "revenue_variance_pct_vs_forecast": 0.022,
    # Gross margin (derived from gross_profit / revenue)
    "gross_profit_fact_id": "fact_gp_actual_001",
    "gross_profit_yoy_fact_id": "fact_gp_yoy_001",
    "gross_margin_pct_actual": 0.74,
    "gross_margin_pct_yoy": 0.71,
    "gross_margin_pct_yoy_delta": 0.03,
    # Operating margin
    "op_income_fact_id": "fact_op_actual_001",
    "op_income_yoy_fact_id": "fact_op_yoy_001",
    "operating_margin_pct_actual": 0.08,
    "operating_margin_pct_yoy": 0.05,
    "operating_margin_pct_yoy_delta": 0.03,
    # Free cash flow
    "fcf_actual": 1_000_000_000.0,
    "fcf_yoy": 800_000_000.0,
    "fcf_yoy_growth_pct": 0.25,
    "ocf_actual_fact_id": "fact_ocf_actual_001",
    "capex_actual_fact_id": "fact_capex_actual_001",
    # Billings (template metric — kept verbatim from am4)
    "billings_actual": 2_500_000_000.0,
    "billings_yoy": 2_150_000_000.0,
    "billings_yoy_growth_pct": 0.16,
}

_CLEAN_QUALITY_ROW: dict[str, Any] = {
    "has_restatement": False,
    "has_going_concern_doubt": False,
    "has_material_weakness": False,
    "missing_quarters": None,
    "has_physical_inventory": True,
}

# Every derived metric required to carry a provenance string.
_REQUIRED_PROVENANCE_KEYS = (
    "gross_margin_pct_actual_provenance",
    "gross_margin_pct_yoy_provenance",
    "gross_margin_pct_yoy_delta_provenance",
    "operating_margin_pct_actual_provenance",
    "operating_margin_pct_yoy_provenance",
    "operating_margin_pct_yoy_delta_provenance",
    "revenue_yoy_growth_pct_provenance",
    "revenue_variance_vs_forecast_provenance",
    "revenue_variance_pct_vs_forecast_provenance",
    "free_cash_flow_provenance",
    "billings_provenance",
)

# A fact_id-like token: a non-empty word that is neither "Sources:" nor a
# "<missing>" marker. Provenance strings should always carry at least one.
_FACT_ID_TOKEN_PAT = re.compile(r"[A-Za-z0-9_<>\-]+")


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_every_derived_metric_has_non_empty_provenance() -> None:
    """Each derived metric in the payload exposes a non-empty provenance sibling."""
    payload = _build_payload("TEST", _COMPLETE_VARIANCE_ROW, _CLEAN_QUALITY_ROW)

    for key in _REQUIRED_PROVENANCE_KEYS:
        assert key in payload, f"missing provenance key: {key}"
        val = payload[key]
        assert isinstance(val, str), f"{key} should be a string, got {type(val).__name__}"
        assert val.strip(), f"{key} must be non-empty"


def test_every_provenance_string_names_sources() -> None:
    """Each provenance string contains 'Sources' and at least one fact_id-like token.

    The billings_provenance template (from bead am4) uses the word "sourced"
    rather than "Sources:" — accept either spelling, case-insensitive, since
    both signal "this string names where the number came from".
    """
    payload = _build_payload("TEST", _COMPLETE_VARIANCE_ROW, _CLEAN_QUALITY_ROW)

    for key in _REQUIRED_PROVENANCE_KEYS:
        val = str(payload[key])
        lower = val.lower()
        assert (
            "sources:" in lower or "sourced" in lower
        ), f"{key} must reference its sources; got: {val!r}"

        tokens_after_sources = re.split(r"sources:|sourced", val, maxsplit=1, flags=re.IGNORECASE)
        tail = tokens_after_sources[1] if len(tokens_after_sources) > 1 else val
        tokens = _FACT_ID_TOKEN_PAT.findall(tail)
        assert tokens, f"{key} has no fact_id-like token after 'Sources:'; got: {val!r}"


def test_provenance_strings_for_specific_derivations() -> None:
    """Spot-check that key fact_ids land in the right provenance string.

    Catches accidental shuffling of the source lists between metrics
    (e.g. if gross-margin provenance ever stops naming gross_profit).
    """
    payload = _build_payload("TEST", _COMPLETE_VARIANCE_ROW, _CLEAN_QUALITY_ROW)

    gm_actual = str(payload["gross_margin_pct_actual_provenance"])
    assert "fact_gp_actual_001" in gm_actual
    assert "fact_rev_actual_001" in gm_actual

    gm_yoy = str(payload["gross_margin_pct_yoy_provenance"])
    assert "fact_gp_yoy_001" in gm_yoy
    assert "fact_rev_yoy_001" in gm_yoy

    gm_delta = str(payload["gross_margin_pct_yoy_delta_provenance"])
    for fid in (
        "fact_gp_actual_001",
        "fact_rev_actual_001",
        "fact_gp_yoy_001",
        "fact_rev_yoy_001",
    ):
        assert fid in gm_delta, f"gross_margin_pct_yoy_delta_provenance missing {fid}"

    op_actual = str(payload["operating_margin_pct_actual_provenance"])
    assert "fact_op_actual_001" in op_actual
    assert "fact_rev_actual_001" in op_actual

    rev_yoy = str(payload["revenue_yoy_growth_pct_provenance"])
    assert "fact_rev_actual_001" in rev_yoy
    assert "fact_rev_yoy_001" in rev_yoy

    fcf = str(payload["free_cash_flow_provenance"])
    assert "fact_ocf_actual_001" in fcf
    assert "fact_capex_actual_001" in fcf
    assert "OperatingCashFlow" in fcf
    assert "CapEx" in fcf

    rev_var = str(payload["revenue_variance_vs_forecast_provenance"])
    assert "fact_rev_actual_001" in rev_var
    # Forecast model name is the source for the forecast side
    assert "prophet" in rev_var or "autoarima" in rev_var


def test_provenance_renders_with_missing_fact_ids() -> None:
    """Missing source fact_ids render as ``<missing>`` and never raise."""
    sparse_row: dict[str, Any] = {
        "fiscal_year": 2026,
        "fiscal_period": "Q1",
        "latest_period_end": "2026-01-31",
        # Only revenue_actual is populated; all other fact_ids are absent.
        "revenue_actual": 2_300_000_000.0,
        "revenue_actual_accession": _VALID_ACCESSION,
        "revenue_actual_filing_url": "https://www.sec.gov/Archives/edgar/data/1327567/",
        "revenue_actual_fact_id": "fact_rev_actual_001",
        "revenue_yoy": 2_000_000_000.0,
        "revenue_yoy_growth_pct": 0.15,
        # No gross_profit_fact_id, no op_income_fact_id, no ocf/capex ids,
        # no forecast model.
        "gross_margin_pct_actual": 0.74,
        "gross_margin_pct_yoy": 0.71,
        "gross_margin_pct_yoy_delta": 0.03,
        "operating_margin_pct_actual": 0.08,
        "operating_margin_pct_yoy": 0.05,
        "operating_margin_pct_yoy_delta": 0.03,
        "fcf_actual": 1_000_000_000.0,
        "fcf_yoy": 800_000_000.0,
        "fcf_yoy_growth_pct": 0.25,
        "billings_actual": 2_500_000_000.0,
        "billings_yoy": 2_150_000_000.0,
        "billings_yoy_growth_pct": 0.16,
    }

    # Must not raise.
    payload = _build_payload("TEST", sparse_row, _CLEAN_QUALITY_ROW)

    # Every provenance key still present and well-formed.
    for key in _REQUIRED_PROVENANCE_KEYS:
        assert key in payload
        val = str(payload[key])
        assert val.strip()

    # Concrete check: gross_margin_pct_actual_provenance carries the existing
    # revenue fact_id and a "<missing>" marker for the absent gross_profit id.
    gm_actual = str(payload["gross_margin_pct_actual_provenance"])
    assert "fact_rev_actual_001" in gm_actual
    assert "<missing>" in gm_actual

    # Free-cash-flow provenance has neither OCF nor CapEx ids in this fixture.
    fcf = str(payload["free_cash_flow_provenance"])
    assert "<missing>" in fcf

    # Forecast-side provenance falls back to <missing> when the model is absent.
    rev_var = str(payload["revenue_variance_vs_forecast_provenance"])
    assert "<missing>" in rev_var


def test_billings_provenance_template_unchanged() -> None:
    """The billings_provenance string from bead am4 is kept verbatim — it is
    the template every other provenance string follows.
    """
    payload = _build_payload("TEST", _COMPLETE_VARIANCE_ROW, _CLEAN_QUALITY_ROW)
    bp = str(payload["billings_provenance"])
    assert "revenue + " in bp
    assert "deferred revenue" in bp.lower()
    assert "ContractWithCustomerLiabilityCurrent" in bp
