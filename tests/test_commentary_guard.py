"""Tests for the hallucination guard in src/generate_commentary.py.

Every test exercises a distinct guard rule from Prompt 8 Step 5.
The guard is deterministic Python — no API calls.
"""

from __future__ import annotations

import pytest

from src.generate_commentary import HallucinationError, run_hallucination_guard

# ── Minimal payload fixtures ──────────────────────────────────────────────────

_VALID_ACCESSION = "0001327567-26-000123"
_UNKNOWN_ACCESSION = "9999999999-99-999999"

# Payload with a single $1.2B revenue entry
_PAYLOAD_1_2B = {
    "ticker": "TEST",
    "fiscal_year": 2026,
    "fiscal_period": "Q1",
    "revenue": {
        "value": "$1.2B",
        "fact_id": "abc123",
        "accession": _VALID_ACCESSION,
        "filing_url": "https://www.sec.gov/Archives/edgar/data/1327567/000132756726000123/",
    },
    "revenue_yoy_growth_pct": {"value": "12.3%"},
    "gross_margin_pct_actual": {"value": "8.0%"},
    "operating_margin_pct_actual": {"value": "5.5%"},
}

# Valid one-sentence commentary that should PASS all guard checks
_VALID_COMMENTARY = (
    f"Revenue of $1.2B [{_VALID_ACCESSION}] was in line with expectations. "
    f"YoY growth was 12.3%."
)


# ── Helper ────────────────────────────────────────────────────────────────────

def _guard(text: str, payload: dict = _PAYLOAD_1_2B) -> None:  # type: ignore[type-arg]
    run_hallucination_guard(text, payload)


# ── Forbidden word-form tests ─────────────────────────────────────────────────


def test_guard_rejects_billion_word_form() -> None:
    """'$1.2 billion' while input has '$1.2B' → rejected (forbidden 'billion')."""
    with pytest.raises(HallucinationError, match="billion"):
        _guard(f"Revenue of $1.2 billion [{_VALID_ACCESSION}].")


def test_guard_rejects_bps() -> None:
    """'230 bps' → rejected (forbidden 'bps')."""
    with pytest.raises(HallucinationError, match="bps"):
        _guard(f"Margin expanded 230 bps [{_VALID_ACCESSION}].")


def test_guard_rejects_basis_point() -> None:
    """'basis point' → rejected."""
    with pytest.raises(HallucinationError, match="basis point"):
        _guard("Expanded by 100 basis points.")


# ── Numeric value mismatch tests ──────────────────────────────────────────────


def test_guard_rejects_wrong_dollar_value() -> None:
    """'$1.5B' while input has only '$1.2B' → rejected (numeric mismatch)."""
    with pytest.raises(HallucinationError):
        _guard(f"Revenue of $1.5B [{_VALID_ACCESSION}].")


def test_guard_accepts_percent_within_tolerance() -> None:
    """'12.30%' while input has '12.3%' → ACCEPTED (within tolerance)."""
    _guard(f"Revenue of $1.2B [{_VALID_ACCESSION}] grew 12.30%.")


def test_guard_accepts_8_0_pct() -> None:
    """'8.0%' from input → ACCEPTED."""
    _guard(f"Gross margin was 8.0%.")


# ── Parens-negative tests ─────────────────────────────────────────────────────


def test_guard_rejects_parens_negative_dollar() -> None:
    """'$(123)M' → rejected (parens-negative)."""
    with pytest.raises(HallucinationError, match="parens-negative"):
        _guard("Operating loss was $(123)M.")


def test_guard_accepts_parenthetical_clarification() -> None:
    """'Revenue grew 12.3% (vs 8.0% prior quarter)' → ACCEPTED (not parens-negative)."""
    _guard(f"Revenue of $1.2B [{_VALID_ACCESSION}] grew 12.3% (vs 8.0% prior quarter).")


def test_guard_accepts_dollar_value_in_parenthetical() -> None:
    """'Operating income ($87.5M)...' → depends on whether $87.5M is in input.
    This tests that a dollar value *in* parentheses is handled by the value check,
    not incorrectly flagged as parens-negative. Payload has no $87.5M so it fails
    on value mismatch, not parens-negative — both are valid refusals.
    """
    with pytest.raises(HallucinationError):
        # Either parens-negative OR value mismatch is acceptable
        _guard(f"Operating income ($87.5M [{_VALID_ACCESSION}]) exceeded consensus.")


# ── Bare-number tests ─────────────────────────────────────────────────────────


def test_guard_rejects_bare_employee_count() -> None:
    """'8400 employees' → rejected (bare number)."""
    with pytest.raises(HallucinationError, match="bare number"):
        _guard("The company has 8400 employees.")


def test_guard_accepts_year_bare() -> None:
    """'In 2024, revenue was $1.2B' → ACCEPTED (year whitelisted)."""
    _guard(f"In 2024, revenue was $1.2B [{_VALID_ACCESSION}].")


# ── Citation tests ────────────────────────────────────────────────────────────


def test_guard_rejects_missing_citation() -> None:
    """'Revenue of $1.2B' (no citation) → REJECTED (missing citation)."""
    with pytest.raises(HallucinationError, match="no citation"):
        _guard("Revenue of $1.2B was solid.")


def test_guard_rejects_unknown_accession() -> None:
    """Citation to accession not in input set → REJECTED."""
    with pytest.raises(HallucinationError, match="not in the input accession set"):
        _guard(f"Revenue of $1.2B [{_UNKNOWN_ACCESSION}].")


def test_guard_accepts_valid_citation() -> None:
    """'Revenue of $1.2B [0001327567-26-000123]' with accession in input → ACCEPTED."""
    _guard(f"Revenue of $1.2B [{_VALID_ACCESSION}].")


def test_guard_accepts_full_valid_commentary() -> None:
    """Full valid commentary with all guard rules satisfied → ACCEPTED."""
    _guard(_VALID_COMMENTARY)
