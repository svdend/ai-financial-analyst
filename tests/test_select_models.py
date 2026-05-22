"""Tests for src/select_models.py.

Covers:
- Network/auth failures fall through to the policy fallback IDs.
- Policy-parsing bugs (missing keys) propagate as real errors instead of
  silently masking via the fallback path.
- The lru_cache is cleared between tests by the autouse conftest fixture.
"""

from __future__ import annotations

from unittest.mock import patch

import anthropic
import pytest

from src.select_models import select_models


def test_falls_back_on_anthropic_api_error() -> None:
    """Network/auth/rate-limit errors → fallback IDs from model_selection.yaml."""
    with patch("anthropic.Anthropic") as mock_client:
        mock_client.return_value.models.list.side_effect = anthropic.APIConnectionError(
            request=None  # type: ignore[arg-type]
        )
        result = select_models()

    # Fallback values from config/model_selection.yaml
    assert "planner" in result
    assert "narrator" in result
    assert "opus" in result["planner"]
    assert "sonnet" in result["narrator"]


def test_picks_highest_generation_when_discovery_succeeds() -> None:
    """When /v1/models returns matching opus + sonnet IDs, pick highest version."""

    class _M:
        def __init__(self, mid: str) -> None:
            self.id = mid

    fake_models = [
        _M("claude-opus-4-1-20250805"),
        _M("claude-opus-4-6-20260101"),
        _M("claude-sonnet-4-6-20260101"),
        _M("claude-sonnet-4-5-20250914"),
        _M("claude-haiku-4-5-20251001"),
    ]
    with patch("anthropic.Anthropic") as mock_client:
        mock_client.return_value.models.list.return_value = fake_models
        result = select_models()

    assert result["planner"] == "claude-opus-4-6-20260101"
    assert result["narrator"] == "claude-sonnet-4-6-20260101"


def test_policy_parse_error_propagates(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Bad model_selection.yaml → real error, not a silent fallback."""
    bad_yaml = tmp_path / "model_selection.yaml"
    bad_yaml.write_text("selection:\n  # missing planner / narrator keys\n")

    monkeypatch.setattr("src.select_models._CONFIG_PATH", bad_yaml)
    select_models.cache_clear()  # ensure we re-read the patched path

    with pytest.raises((KeyError, TypeError)):
        select_models()
