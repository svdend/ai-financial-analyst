"""Tests for the guard-retry loop in src/generate_commentary.py.

Exercises:
- A first-attempt guard violation followed by a clean second attempt → succeeds.
- max_guard_retries=0 → the first violation propagates (no retries).
- max_guard_retries=2 with three calls → final clean attempt wins.

The pipeline is exercised end-to-end except for:
- DuckDB/_pull_variance_data is monkeypatched to return a hand-built fixture.
- _check_refusals is bypassed via that fixture's clean quality_row.
- _call_claude is patched to return a sequence of canned strings.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from src import generate_commentary as gc

_VALID_ACCESSION = "0001327567-26-000123"


def _fake_pull(_db_path: Path) -> tuple[dict, dict]:  # type: ignore[type-arg]
    """Return a minimal but realistic (variance_row, quality_row) tuple."""
    variance_row = {
        "fiscal_year": 2026,
        "fiscal_period": "Q1",
        "latest_period_end": "2026-01-31",
        "revenue_actual": 2_300_000_000,
        "revenue_actual_accession": _VALID_ACCESSION,
        "revenue_actual_filing_url": (
            "https://www.sec.gov/Archives/edgar/data/1327567/000132756726000123/"
        ),
        "revenue_actual_fact_id": "abc123",
        "revenue_yoy": 2_000_000_000,
        "revenue_yoy_growth_pct": 0.15,
        "revenue_prior_forecast": 2_250_000_000,
        "revenue_prior_forecast_model": "Prophet",
        "revenue_variance_vs_forecast": 50_000_000,
        "revenue_variance_pct_vs_forecast": 0.022,
        "gross_margin_pct_actual": 0.74,
        "gross_margin_pct_yoy": 0.71,
        "gross_margin_pct_yoy_delta": 0.03,
        "operating_margin_pct_actual": 0.08,
        "operating_margin_pct_yoy": 0.05,
        "operating_margin_pct_yoy_delta": 0.03,
        "fcf_actual": 1_000_000_000,
        "fcf_yoy": 800_000_000,
        "fcf_yoy_growth_pct": 0.25,
    }
    quality_row = {
        "has_restatement": False,
        "has_physical_inventory": True,
        "missing_quarters": "",
    }
    return variance_row, quality_row


_BAD_COMMENTARY_BILLION = f"Revenue of $2.3 billion [{_VALID_ACCESSION}] grew 15.0%."
_GOOD_COMMENTARY = f"Revenue of $2.3B [{_VALID_ACCESSION}] grew 15.0%."


def _save_path(ticker: str, _text: str) -> Path:
    """Stub for _save_commentary that returns a deterministic path without I/O."""
    return Path(f"/tmp/{ticker}_commentary.md")


@pytest.fixture
def _pipeline_patches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the warehouse pull and on-disk save so the test runs in-memory."""
    monkeypatch.setattr(gc, "_pull_variance_data", _fake_pull)
    monkeypatch.setattr(gc, "_save_commentary", lambda ticker, text: tmp_path / f"{ticker}.md")


def test_guard_retry_recovers_on_second_attempt(_pipeline_patches: None) -> None:
    """First call returns 'billion' (forbidden); second call returns valid output."""
    call_log: list[str] = []

    def fake_call(payload: dict, *, retry_feedback: str | None = None) -> str:  # type: ignore[type-arg]
        call_log.append(retry_feedback or "<initial>")
        # First attempt: forbidden 'billion' word. Second attempt: clean.
        return _BAD_COMMENTARY_BILLION if len(call_log) == 1 else _GOOD_COMMENTARY

    with patch.object(gc, "_call_claude", side_effect=fake_call):
        result = gc.generate(ticker="PANW", dry_run=False, max_guard_retries=1)

    assert result is not None
    assert len(call_log) == 2, f"expected 2 calls, got {len(call_log)}"
    assert call_log[0] == "<initial>"
    assert (
        "billion" in call_log[1]
    ), "Retry should pass the violation message back as corrective feedback."


def test_guard_retry_disabled_propagates_first_violation(_pipeline_patches: None) -> None:
    """max_guard_retries=0 → no retry; HallucinationError surfaces on first violation."""
    call_log: list[str] = []

    def fake_call(payload: dict, *, retry_feedback: str | None = None) -> str:  # type: ignore[type-arg]
        call_log.append("called")
        return _BAD_COMMENTARY_BILLION

    with patch.object(gc, "_call_claude", side_effect=fake_call):
        with pytest.raises(gc.HallucinationError, match="billion"):
            gc.generate(ticker="PANW", dry_run=False, max_guard_retries=0)

    assert len(call_log) == 1, "Must not retry when max_guard_retries=0"


def test_guard_retry_exhausts_then_raises(_pipeline_patches: None) -> None:
    """max_guard_retries=2 with all-bad output → raises after 3 total attempts."""
    call_log: list[str] = []

    def fake_call(payload: dict, *, retry_feedback: str | None = None) -> str:  # type: ignore[type-arg]
        call_log.append("called")
        return _BAD_COMMENTARY_BILLION

    with patch.object(gc, "_call_claude", side_effect=fake_call):
        with pytest.raises(gc.HallucinationError):
            gc.generate(ticker="PANW", dry_run=False, max_guard_retries=2)

    assert len(call_log) == 3, f"expected 3 attempts, got {len(call_log)}"
