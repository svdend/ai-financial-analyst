"""Tests for generate_commentary.py refusal logic (Step 2).

Verifies that the pipeline refuses (raises RefusalError) for:
- has_restatement=TRUE
- missing_quarters covering the variance window

No API calls are made in these tests — refusal happens before Step 4.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.generate_commentary import RefusalError, _check_refusals

# ── Fixtures ──────────────────────────────────────────────────────────────────

_VALID_VARIANCE_ROW = {
    "fiscal_year": 2026,
    "fiscal_period": "Q1",
    "latest_period_end": "2026-01-31",
    "revenue_actual": 1_200_000_000.0,
    "revenue_actual_accession": "0001327567-26-000123",
    "revenue_actual_fact_id": "abc123",
    "revenue_actual_filing_url": "https://www.sec.gov/Archives/edgar/data/1327567/",
}

_CLEAN_QUALITY_ROW = {
    "has_restatement": False,
    "has_going_concern_doubt": False,
    "has_material_weakness": False,
    "missing_quarters": None,
    "has_physical_inventory": True,
}


# ── Restatement refusal ───────────────────────────────────────────────────────


def test_refusal_on_restatement_true() -> None:
    """has_restatement=TRUE → pipeline refuses with RefusalError."""
    quality = {**_CLEAN_QUALITY_ROW, "has_restatement": True}
    with pytest.raises(RefusalError, match="REFUSED"):
        _check_refusals(_VALID_VARIANCE_ROW, quality)


def test_refusal_on_restatement_true_does_not_call_api(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """generate() refuses on has_restatement=TRUE without instantiating anthropic.Anthropic."""
    import anthropic

    from src import generate_commentary as gc

    constructor_calls: list[None] = []

    def _refuse_construct(*_args: object, **_kwargs: object) -> None:
        constructor_calls.append(None)
        raise AssertionError("anthropic.Anthropic() must not be called on refusal")

    monkeypatch.setattr(anthropic, "Anthropic", _refuse_construct)
    monkeypatch.setattr(
        gc,
        "_pull_variance_data",
        lambda _db: (_VALID_VARIANCE_ROW, {**_CLEAN_QUALITY_ROW, "has_restatement": True}),
    )

    fake_config = tmp_path / "company.yaml"
    fake_config.write_text("ticker: TEST\n")
    monkeypatch.setattr(gc, "_CONFIG_PATH", fake_config)

    with pytest.raises(RefusalError, match="has_restatement"):
        gc.generate(ticker="TEST", db_path=tmp_path / "ignored.duckdb")

    assert not constructor_calls, "anthropic.Anthropic must NOT be constructed on refusal"


# ── Missing-quarters refusal ──────────────────────────────────────────────────


def test_refusal_on_going_concern_doubt() -> None:
    """has_going_concern_doubt=TRUE → pipeline refuses with RefusalError."""
    quality = {**_CLEAN_QUALITY_ROW, "has_going_concern_doubt": True}
    with pytest.raises(RefusalError, match="going-concern"):
        _check_refusals(_VALID_VARIANCE_ROW, quality)


def test_refusal_on_material_weakness() -> None:
    """has_material_weakness=TRUE → pipeline refuses with RefusalError."""
    quality = {**_CLEAN_QUALITY_ROW, "has_material_weakness": True}
    with pytest.raises(RefusalError, match="material weakness"):
        _check_refusals(_VALID_VARIANCE_ROW, quality)


def test_refusal_on_missing_quarters() -> None:
    """missing_quarters non-empty → pipeline refuses with RefusalError."""
    quality = {**_CLEAN_QUALITY_ROW, "missing_quarters": "FY2025Q2,FY2025Q3"}
    with pytest.raises(RefusalError, match="REFUSED"):
        _check_refusals(_VALID_VARIANCE_ROW, quality)


def test_no_refusal_when_missing_quarters_is_none() -> None:
    """missing_quarters=None (clean) → no RefusalError raised."""
    _check_refusals(_VALID_VARIANCE_ROW, _CLEAN_QUALITY_ROW)


def test_no_refusal_when_missing_quarters_is_empty_string() -> None:
    """missing_quarters='' (clean) → no RefusalError raised."""
    quality = {**_CLEAN_QUALITY_ROW, "missing_quarters": ""}
    _check_refusals(_VALID_VARIANCE_ROW, quality)


# ── Fiscal year boundary refusal ──────────────────────────────────────────────


def test_refusal_on_null_fiscal_year() -> None:
    """fiscal_year=None → RefusalError about fiscal-year boundary."""
    variance = {**_VALID_VARIANCE_ROW, "fiscal_year": None}
    with pytest.raises(RefusalError, match="REFUSED"):
        _check_refusals(variance, _CLEAN_QUALITY_ROW)


# ── No refusal when data is clean ─────────────────────────────────────────────


def test_no_refusal_on_clean_data() -> None:
    """Clean variance + quality rows → no refusal raised."""
    _check_refusals(_VALID_VARIANCE_ROW, _CLEAN_QUALITY_ROW)
