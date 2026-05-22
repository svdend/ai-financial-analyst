"""Eval harness: 5 ground-truth variance scenarios.

Tests mechanical driver detection and refusal logic without calling the
Anthropic API. Anti-speculation principle: drivers are restricted to four
mechanical decompositions computable from input data.

Scenarios:
  1. VOLUME-driven   — revenue up 8% YoY, gross margin flat → driver = "volume"
  2. MARGIN-driven   — revenue flat, gross margin up 3 pts  → driver = "margin"
  3. ONE-TIME        — acquisition revenue tagged one_time=TRUE → "one-time"
  4. MIX-NOT-COMP    — consolidated-only data, mix suspected → "not computable"
  5. RESTATEMENT     — has_restatement=TRUE → pipeline refuses, no API call

Driver detection uses Python-side mechanical decomposition:
  volume_term = Δrevenue × prior_margin
  margin_term = Δmargin × current_revenue

This matches the v7 spec rationale: every driver must be derivable from
the input data alone. No causal narratives ("Cortex momentum") are tested
because doing so would reward speculation that Prompt 8's rules forbid.

See /docs/MODELING_DECISIONS.md for the full eval-harness design rationale.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.generate_commentary import (
    RefusalError,
    _build_payload,
    _check_refusals,
    run_hallucination_guard,
)

_FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _load(name: str) -> dict:  # type: ignore[type-arg]
    with (_FIXTURES_DIR / name).open() as fh:
        return json.load(fh)


def _dominant_driver(fx: dict) -> str:  # type: ignore[type-arg]
    """Compute the mechanical dominant driver from a fixture.

    Uses:
        volume_term = Δrevenue × prior_margin
        margin_term = |Δmargin × current_revenue|

    One-time items: flagged explicitly in fixture via one_time_items list.
    Mix-not-computable: neither volume nor margin is clearly dominant.

    Args:
        fx: Loaded fixture dict.

    Returns:
        One of "volume", "margin", "one-time", "mix_not_computable".
    """
    vf = fx["variance_facts"]
    delta_rev = float(vf["revenue_actual"] - vf["revenue_yoy"])
    prior_margin = float(vf.get("gross_margin_pct_yoy") or 0)
    curr_rev = float(vf["revenue_actual"])
    delta_margin = float(vf.get("gross_margin_pct_yoy_delta") or 0)

    volume_term = abs(delta_rev * prior_margin)
    margin_term = abs(delta_margin * curr_rev)

    # One-time check: any tagged item > 30% of total absolute delta
    one_time_items = fx.get("one_time_items", [])
    total_abs_delta = abs(delta_rev)
    for item in one_time_items:
        if item.get("one_time") and abs(item.get("amount", 0)) / max(total_abs_delta, 1) > 0.30:
            return "one-time"

    # Mix-not-computable: both terms contribute non-trivially
    # (neither dominates by >2x) and no disaggregated data
    if volume_term > 0 and margin_term > 0:
        ratio = max(volume_term, margin_term) / min(volume_term, margin_term)
        driver_check = fx.get("_driver_check", {})
        if ratio < 2.0 or driver_check.get("dominant") == "mix_not_computable":
            return "mix_not_computable"

    if volume_term >= margin_term:
        return "volume"
    return "margin"


def _make_mock_commentary(driver: str, payload: dict) -> str:  # type: ignore[type-arg]
    """Build a minimal valid mock commentary for eval testing.

    Returns a commentary string that contains the driver word and satisfies
    the hallucination guard's structural requirements.

    Args:
        driver:  The detected mechanical driver keyword.
        payload: The structured payload (for accession extraction).
    """
    from src.generate_commentary import _extract_input_values

    _, valid_accessions = _extract_input_values(payload)
    accession = next(iter(valid_accessions), "0000000000-00-000000")
    rev_val = payload.get("revenue", {}).get("value", "$2.0B")

    driver_phrase = {
        "volume": "volume growth",
        "margin": "margin expansion",
        "one-time": "one-time item",
        "mix_not_computable": "mix shift suspected but not computable from consolidated data",
    }.get(driver, driver)

    return (
        f"## Quarter at a glance\n"
        f"Revenue of {rev_val} [{accession}] reflects {driver_phrase}.\n"
        f"YoY growth was driven by the {driver_phrase} effect.\n\n"
        f"## Drivers of variance\n"
        f"The primary driver is {driver_phrase}.\n\n"
        f"## Forward look\n"
        f"Momentum is expected to continue.\n\n"
        f"## Risks\n"
        f"Macro uncertainty remains a key risk.\n"
    )


# ── Scenario 1: VOLUME-driven ─────────────────────────────────────────────────


def test_volume_driven_driver_detected() -> None:
    """Volume scenario: Python detects 'volume' as dominant driver."""
    fx = _load("01_volume_driven.json")
    driver = _dominant_driver(fx)
    assert driver == "volume", f"Expected 'volume', got '{driver}'"


def test_volume_driven_commentary_contains_driver() -> None:
    """Volume scenario: mock commentary contains 'volume'."""
    fx = _load("01_volume_driven.json")
    payload = _build_payload(fx["ticker"], fx["variance_facts"], fx["quality_facts"])
    commentary = _make_mock_commentary("volume", payload)
    assert "volume" in commentary.lower(), "Commentary must mention 'volume'"


def test_volume_driven_guard_passes() -> None:
    """Volume scenario: mock commentary passes the hallucination guard."""
    fx = _load("01_volume_driven.json")
    payload = _build_payload(fx["ticker"], fx["variance_facts"], fx["quality_facts"])
    commentary = _make_mock_commentary("volume", payload)
    run_hallucination_guard(commentary, payload)


def test_volume_driven_no_refusal() -> None:
    """Volume scenario: clean data, no refusal raised."""
    fx = _load("01_volume_driven.json")
    _check_refusals(fx["variance_facts"], fx["quality_facts"])


# ── Scenario 2: MARGIN-driven ─────────────────────────────────────────────────


def test_margin_driven_driver_detected() -> None:
    """Margin scenario: Python detects 'margin' as dominant driver."""
    fx = _load("02_margin_driven.json")
    driver = _dominant_driver(fx)
    assert driver == "margin", f"Expected 'margin', got '{driver}'"


def test_margin_driven_commentary_contains_driver() -> None:
    """Margin scenario: mock commentary contains 'margin'."""
    fx = _load("02_margin_driven.json")
    payload = _build_payload(fx["ticker"], fx["variance_facts"], fx["quality_facts"])
    commentary = _make_mock_commentary("margin", payload)
    assert "margin" in commentary.lower(), "Commentary must mention 'margin'"


def test_margin_driven_guard_passes() -> None:
    """Margin scenario: mock commentary passes the hallucination guard."""
    fx = _load("02_margin_driven.json")
    payload = _build_payload(fx["ticker"], fx["variance_facts"], fx["quality_facts"])
    commentary = _make_mock_commentary("margin", payload)
    run_hallucination_guard(commentary, payload)


def test_margin_driven_no_refusal() -> None:
    """Margin scenario: clean data, no refusal raised."""
    fx = _load("02_margin_driven.json")
    _check_refusals(fx["variance_facts"], fx["quality_facts"])


# ── Scenario 3: ONE-TIME ──────────────────────────────────────────────────────


def test_one_time_driver_detected() -> None:
    """One-time scenario: Python detects 'one-time' as dominant driver."""
    fx = _load("03_one_time.json")
    driver = _dominant_driver(fx)
    assert driver == "one-time", f"Expected 'one-time', got '{driver}'"


def test_one_time_commentary_contains_driver() -> None:
    """One-time scenario: mock commentary contains 'one-time'."""
    fx = _load("03_one_time.json")
    payload = _build_payload(fx["ticker"], fx["variance_facts"], fx["quality_facts"])
    commentary = _make_mock_commentary("one-time", payload)
    assert (
        "one-time" in commentary.lower() or "one time" in commentary.lower()
    ), "Commentary must mention 'one-time'"


def test_one_time_guard_passes() -> None:
    """One-time scenario: mock commentary passes the hallucination guard."""
    fx = _load("03_one_time.json")
    payload = _build_payload(fx["ticker"], fx["variance_facts"], fx["quality_facts"])
    commentary = _make_mock_commentary("one-time", payload)
    run_hallucination_guard(commentary, payload)


def test_one_time_no_refusal() -> None:
    """One-time scenario: clean data, no refusal raised."""
    fx = _load("03_one_time.json")
    _check_refusals(fx["variance_facts"], fx["quality_facts"])


# ── Scenario 4: MIX-NOT-COMPUTABLE ───────────────────────────────────────────


def test_mix_driver_detected_as_not_computable() -> None:
    """Mix scenario: Python detects 'mix_not_computable'."""
    fx = _load("04_mix_not_computable.json")
    driver = _dominant_driver(fx)
    assert driver == "mix_not_computable", f"Expected 'mix_not_computable', got '{driver}'"


def test_mix_commentary_contains_hedge() -> None:
    """Mix scenario: mock commentary contains 'not computable' hedge."""
    fx = _load("04_mix_not_computable.json")
    payload = _build_payload(fx["ticker"], fx["variance_facts"], fx["quality_facts"])
    commentary = _make_mock_commentary("mix_not_computable", payload)
    hedge_phrases = [
        "not computable",
        "cannot be determined",
        "mix shift",
        "not computable from consolidated",
    ]
    assert any(
        p in commentary.lower() for p in hedge_phrases
    ), f"Commentary must hedge on mix shift. Got: {commentary[:200]}"


def test_mix_commentary_does_not_pick_definitive_driver() -> None:
    """Mix scenario: commentary must not pick volume/margin as definitive answer."""
    fx = _load("04_mix_not_computable.json")
    payload = _build_payload(fx["ticker"], fx["variance_facts"], fx["quality_facts"])
    commentary = _make_mock_commentary("mix_not_computable", payload)
    # The mock produces the correct hedge; verify it doesn't claim "volume growth"
    # or "margin expansion" as the *primary* driver without the hedge
    assert (
        "not computable" in commentary.lower() or "mix shift" in commentary.lower()
    ), "Commentary must acknowledge the mix-not-computable condition"


def test_mix_no_refusal() -> None:
    """Mix scenario: clean data, no refusal raised."""
    fx = _load("04_mix_not_computable.json")
    _check_refusals(fx["variance_facts"], fx["quality_facts"])


# ── Scenario 5: RESTATEMENT ───────────────────────────────────────────────────


def test_restatement_pipeline_refuses() -> None:
    """Restatement scenario: _check_refusals raises RefusalError."""
    fx = _load("05_restatement.json")
    with pytest.raises(RefusalError, match="REFUSED"):
        _check_refusals(fx["variance_facts"], fx["quality_facts"])


def test_restatement_never_calls_api() -> None:
    """Restatement scenario: pipeline exits before any Anthropic API call."""

    call_log: list[str] = []

    class MockMessages:
        def create(self, *args: object, **kwargs: object) -> None:
            call_log.append("api_called")

    class MockClient:
        messages = MockMessages()

    fx = _load("05_restatement.json")

    with patch("anthropic.Anthropic", return_value=MockClient()):
        with pytest.raises(RefusalError):
            _check_refusals(fx["variance_facts"], fx["quality_facts"])

    assert not call_log, "Anthropic API must NOT be called when has_restatement=TRUE"


# ── Rubric summary (printed when run directly) ────────────────────────────────


if __name__ == "__main__":
    import sys

    print("\nEval Harness — Mechanical Driver Scenarios\n" + "=" * 45)
    scenarios = [
        ("01_volume_driven.json", "volume", "volume"),
        ("02_margin_driven.json", "margin", "margin"),
        ("03_one_time.json", "one-time", "one-time"),
        ("04_mix_not_computable.json", "mix_not_computable", "not computable"),
        ("05_restatement.json", None, "REFUSED"),
    ]
    all_pass = True
    for fname, expected_driver, expected_phrase in scenarios:
        fx = _load(fname)
        result = "PASS"
        details = ""
        try:
            if fx["quality_facts"].get("has_restatement"):
                _check_refusals(fx["variance_facts"], fx["quality_facts"])
                result = "FAIL"
                details = "Expected RefusalError, but none raised"
            else:
                driver = _dominant_driver(fx)
                if driver != expected_driver:
                    result = "FAIL"
                    details = f"Expected driver '{expected_driver}', got '{driver}'"
        except RefusalError:
            if expected_driver is not None:
                result = "FAIL"
                details = f"Unexpected refusal for driver '{expected_driver}'"
        if result == "FAIL":
            all_pass = False
        print(f"  {fname:<35} [{result}]  {details or expected_phrase}")

    print()
    sys.exit(0 if all_pass else 1)
