"""Tests for src.prompts.load_prompt.

Verifies the prompt-template loader strips HTML-comment headers, caches
results, raises a clear error when a template is missing, and that the
production prompt ``commentary_system_v1`` contains the contract phrases
that downstream guards rely on.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import src.prompts as prompts_pkg
from src.prompts import load_prompt


@pytest.fixture(autouse=True)
def _clear_prompt_cache() -> None:
    """Reset the module-level prompt cache between tests.

    Several tests assert behaviour on first load (header stripping, missing
    file). A pre-warmed cache from another test would short-circuit the read.
    """
    prompts_pkg._PROMPT_CACHE.clear()


def test_load_prompt_strips_header(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Leading HTML comment + blank lines are stripped from the returned body."""
    md = tmp_path / "fixture_prompt.md"
    md.write_text(
        "<!--\nfixture_prompt v1 — used in tests.\n-->\n\nBody line one.\nBody line two.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(prompts_pkg, "_PACKAGE_DIR", tmp_path)

    body = load_prompt("fixture_prompt")

    assert body.startswith("Body line one.")
    assert body.endswith("Body line two.")
    assert "<!--" not in body
    assert "-->" not in body
    # No leading whitespace bled through from the blank line after -->.
    assert body[0] != "\n"


def test_load_prompt_caches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Repeated calls for the same name read the file at most once."""
    md = tmp_path / "cached_prompt.md"
    md.write_text("Cached body.", encoding="utf-8")
    monkeypatch.setattr(prompts_pkg, "_PACKAGE_DIR", tmp_path)

    first = load_prompt("cached_prompt")

    real_read_text = Path.read_text
    with patch.object(Path, "read_text", autospec=True) as mock_read:
        mock_read.side_effect = real_read_text
        second = load_prompt("cached_prompt")
        third = load_prompt("cached_prompt")

    assert first == second == third == "Cached body."
    assert mock_read.call_count == 0


def test_load_prompt_raises_on_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A clear ``FileNotFoundError`` is raised when no matching .md exists."""
    monkeypatch.setattr(prompts_pkg, "_PACKAGE_DIR", tmp_path)

    with pytest.raises(FileNotFoundError, match="does_not_exist"):
        load_prompt("does_not_exist")


def test_commentary_system_v1_loads() -> None:
    """The shipped commentary prompt loads and carries its key contract clauses.

    The substrings asserted here are stable behavioural guarantees — fact_id
    citation, the ban on speculation, and the verbatim-numbers rule — not
    cosmetic phrasing.
    """
    prompt = load_prompt("commentary_system_v1")

    assert prompt
    assert "accession_no" in prompt
    assert "VERBATIM" in prompt
    assert "Never speculate" in prompt
