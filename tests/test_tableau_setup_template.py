"""Tests for the Tableau_Setup.md template renderer.

The renderer reads ``dashboard/_Tableau_Setup_template.md`` and substitutes
``{ticker}``.  These tests pin two contracts:

  1. The committed ``dashboard/Tableau_Setup.md`` is byte-for-byte the result
     of rendering the template with the project's configured ticker — so docs
     PRs editing the rendered file alone (without touching the template) will
     fail this test, surfacing the drift.

  2. ``str.format`` rejects unknown placeholders — so a future template edit
     that introduces ``{foo}`` without wiring it through the renderer will
     fail loudly instead of silently emitting a literal ``{foo}``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.export_for_tableau import _write_tableau_setup_md

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATE_PATH = _REPO_ROOT / "dashboard" / "_Tableau_Setup_template.md"
_RENDERED_PATH = _REPO_ROOT / "dashboard" / "Tableau_Setup.md"


def test_template_file_exists() -> None:
    """The template is the source of truth — must be checked in."""
    assert _TEMPLATE_PATH.exists(), (
        f"Template missing: {_TEMPLATE_PATH}.  Restore it before running "
        "the export — the renderer has no fallback."
    )


def test_committed_tableau_setup_matches_template_render(tmp_path: Path) -> None:
    """Committed Tableau_Setup.md must equal the template rendered with the project ticker.

    Rebuild the rendered file from the template and compare byte-for-byte.
    A failure means either the rendered file was edited without re-running
    the export, or the template itself was edited without regenerating the
    artifact.  The fix is ``make dashboard`` (or call _write_tableau_setup_md
    directly) and commit the regenerated file alongside the template change.
    """
    _write_tableau_setup_md(tmp_path, "PANW")
    fresh = (tmp_path / "Tableau_Setup.md").read_text(encoding="utf-8")
    committed = _RENDERED_PATH.read_text(encoding="utf-8")
    assert fresh == committed, (
        "dashboard/Tableau_Setup.md is out of sync with the template.\n"
        "Run 'make dashboard' (or _write_tableau_setup_md directly) and "
        "commit the regenerated file."
    )


def test_renderer_substitutes_ticker(tmp_path: Path) -> None:
    """{ticker} placeholders are substituted; no curly braces remain in the output."""
    _write_tableau_setup_md(tmp_path, "ACME")
    body = (tmp_path / "Tableau_Setup.md").read_text(encoding="utf-8")
    assert "ACME" in body, "ticker substitution did not run"
    assert "{ticker}" not in body, "an unsubstituted {ticker} placeholder leaked through"
    # First H1 carries the ticker; Sheet 2 picks it up too.
    assert body.startswith("# Tableau Setup — ACME Financial Model")
    assert "### Sheet 2: ACME Margins %" in body


def test_renderer_rejects_unknown_placeholder(tmp_path: Path) -> None:
    """A future template edit that introduces {foo} without renderer support must fail loudly.

    str.format raises KeyError on unknown placeholders, which is the desired
    failure mode: a silent literal ``{foo}`` in the rendered doc would be
    embarrassing.  Patch the template path to a fixture that uses {bogus}
    and confirm the renderer raises.
    """
    bogus_template = tmp_path / "_template.md"
    bogus_template.write_text("# {ticker} - {bogus}\n", encoding="utf-8")
    from src import export_for_tableau

    original = export_for_tableau._TABLEAU_SETUP_TEMPLATE
    export_for_tableau._TABLEAU_SETUP_TEMPLATE = bogus_template
    try:
        with pytest.raises(KeyError, match="bogus"):
            _write_tableau_setup_md(tmp_path, "PANW")
    finally:
        export_for_tableau._TABLEAU_SETUP_TEMPLATE = original
