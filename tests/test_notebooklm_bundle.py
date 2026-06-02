"""Tests for src/build_notebooklm_bundle.py.

Covers reviewer-facing invariants:
  - The bundle's Excel summary reads the file actually produced by
    src/build_excel_model.py (filename agreement).
  - When a real PDF is downloaded for a filing, no sibling .txt placeholder
    is left on disk for NotebookLM to ingest.
  - Forecast summary renders a real markdown table with no "Missing
    optional dependency 'tabulate'" stub leaking through.
  - The commentary path is gated on ``ANTHROPIC_API_KEY``: present →
    regenerate live; absent → fall back to the committed SAMPLE bundle.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src import build_excel_model, build_notebooklm_bundle
from src.build_notebooklm_bundle import (
    _build_excel_model_summary,
    _build_forecast_summary,
    _build_historical_financials,
    _download_sec_filing,
)


def test_excel_summary_filename_matches_excel_writer(tmp_path: Path) -> None:
    """The bundle reads the same filename build_excel_model writes.

    Read the canonical filename pattern from build_excel_model and assert
    _build_excel_model_summary recognizes a file at that path.
    """
    ticker = "TEST"
    expected_name = f"{ticker}_3Statement_Model.xlsx"
    excel_source = Path(build_excel_model.__file__).read_text(encoding="utf-8")
    assert expected_name.replace(ticker, "{resolved_ticker}") in excel_source.replace(
        '"', ""
    ) or expected_name.replace(ticker, "{ticker}") in excel_source.replace('"', ""), (
        "build_excel_model no longer writes the expected filename pattern; "
        "update build_notebooklm_bundle._build_excel_model_summary to match."
    )

    excel_path = tmp_path / expected_name
    excel_path.write_bytes(b"fake xlsx")
    with patch.object(build_notebooklm_bundle, "_DASHBOARD_DIR", tmp_path):
        summary = _build_excel_model_summary(ticker)

    assert (
        "not found" not in summary
    ), f"Bundle could not find the Excel file at the canonical path; summary said:\n{summary}"
    assert expected_name in summary


def test_download_sec_filing_removes_stale_txt_placeholder(tmp_path: Path) -> None:
    """A successful PDF download wipes any sibling .txt placeholder.

    NotebookLM ingests every file in the bundle; leaving a "Filing not in PDF
    format" placeholder next to the real PDF causes confusing citations.
    """
    pdf_dest = tmp_path / "02_latest_10K.pdf"
    stale_txt = pdf_dest.with_suffix(".txt")
    stale_txt.write_text("Filing 10-K not in PDF format.\n", encoding="utf-8")
    assert stale_txt.exists()

    fake_subs: dict[str, Any] = {
        "filings": {
            "recent": {
                "form": ["10-K"],
                "accessionNumber": ["0000000000-00-000000"],
                "primaryDocument": ["something.pdf"],
            }
        }
    }

    class _FakeResp:
        status_code = 200
        content = b"%PDF-1.4 fake pdf bytes"

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return fake_subs

    def _fake_get(url: str, **_kwargs: Any) -> _FakeResp:
        return _FakeResp()

    with patch("src.build_notebooklm_bundle.requests.get", _fake_get):
        ok = _download_sec_filing(1327567, "10-K", pdf_dest)

    assert ok is True
    assert pdf_dest.exists()
    assert not stale_txt.exists(), (
        f"Stale .txt placeholder was not removed; bundle would include "
        f"both {pdf_dest.name} and {stale_txt.name}."
    )


def test_download_sec_filing_removes_stale_pdf_when_filing_is_htm(tmp_path: Path) -> None:
    """A non-PDF filing wipes any sibling .pdf left over from a prior run.

    NotebookLM ingests every file in the bundle; leaving an old PDF next to
    the new placeholder causes confusing dual citations.
    """
    pdf_dest = tmp_path / "02_latest_10K.pdf"
    stale_pdf = pdf_dest
    stale_pdf.write_bytes(b"%PDF-1.4 stale pdf bytes from prior run")
    assert stale_pdf.exists()

    fake_subs: dict[str, Any] = {
        "filings": {
            "recent": {
                "form": ["10-K"],
                "accessionNumber": ["0000000000-00-000000"],
                "primaryDocument": ["panw-20250731.htm"],
            }
        }
    }

    class _FakeResp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return fake_subs

    def _fake_get(url: str, **_kwargs: Any) -> _FakeResp:
        return _FakeResp()

    with patch("src.build_notebooklm_bundle.requests.get", _fake_get):
        ok = _download_sec_filing(1327567, "10-K", pdf_dest)

    assert ok is True
    placeholder = pdf_dest.with_suffix(".txt")
    assert placeholder.exists(), "Placeholder .txt should be written for non-PDF filing"
    assert not stale_pdf.exists(), (
        f"Stale .pdf was not removed; bundle would include both "
        f"{stale_pdf.name} and {placeholder.name}."
    )


def test_sample_commentary_renamed_and_banner_added(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sample commentary is bundled as 07_exec_commentary.md with an in-file banner.

    The bundle output filename is always ``07_exec_commentary.md`` whether the
    source is sample or live; the SAMPLE marker carries through as an in-file
    banner only. This avoids two files for NotebookLM to pick between, which
    confused reviewers in the original SAMPLE-suffix scheme.

    With ``ANTHROPIC_API_KEY`` unset, the live-regen branch is skipped and the
    most-recent SAMPLE file from ``dashboard/`` is used as-is.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    dashboard_dir = tmp_path / "dashboard"
    dashboard_dir.mkdir()
    sample_src = dashboard_dir / "TEST_exec_commentary_SAMPLE.md"
    sample_src.write_text(
        "# TEST Commentary (Sample)\nRevenue $1.0B [0000000000-00-000000]\n",
        encoding="utf-8",
    )
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "ticker: TEST\ncik: '0000000000'\ncik_int: 0\nname: Test Co\n"
        "fiscal_year_end_month: 12\nfiscal_year_end_day: 31\n",
        encoding="utf-8",
    )
    # Pre-create stale 07_exec_commentary_SAMPLE.md to confirm cleanup of the
    # legacy suffix path.
    stale = bundle_dir / "07_exec_commentary_SAMPLE.md"
    stale.write_text("# stale legacy-suffix commentary\n", encoding="utf-8")

    # Stub network + heavy build steps so we only exercise the commentary path.
    def _no_op(*_args: Any, **_kwargs: Any) -> bool:
        return True

    def _no_op_path(b_dir: Path) -> Path:
        out = b_dir / "_stub.html"
        out.write_text("stub", encoding="utf-8")
        return out

    with (
        patch.object(build_notebooklm_bundle, "_DASHBOARD_DIR", dashboard_dir),
        patch.object(build_notebooklm_bundle, "_BUNDLE_DIR", bundle_dir),
        patch.object(build_notebooklm_bundle, "_CONFIG_PATH", config_path),
        patch.object(build_notebooklm_bundle, "_PROCESSED_DIR", tmp_path),
        patch.object(build_notebooklm_bundle, "_MODELS_DIR", tmp_path),
        patch.object(build_notebooklm_bundle, "_download_sec_filing", _no_op),
        patch.object(build_notebooklm_bundle, "_generate_test_report", _no_op_path),
        patch.object(build_notebooklm_bundle, "_generate_eval_report", _no_op_path),
    ):
        written = build_notebooklm_bundle.build(ticker="TEST")

    live_dest = bundle_dir / "07_exec_commentary.md"
    legacy_dest = bundle_dir / "07_exec_commentary_SAMPLE.md"
    assert live_dest.exists(), "Bundle commentary should always be 07_exec_commentary.md"
    assert not legacy_dest.exists(), "Stale 07_exec_commentary_SAMPLE.md was not cleaned up"
    body = live_dest.read_text(encoding="utf-8")
    assert "SAMPLE — illustrative only" in body, "Banner not injected into sample commentary"
    assert written["07_exec_commentary"] == live_dest

    readme = (bundle_dir / "README_FOR_NOTEBOOKLM.md").read_text(encoding="utf-8")
    assert "07_exec_commentary.md" in readme
    assert (
        "live commentary required" in readme.lower()
    ), "README should suppress the provenance demo prompt for sample commentary"


# ── Live-vs-SAMPLE gating on ANTHROPIC_API_KEY ────────────────────────────────


def _stub_bundle_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, Path]:
    """Set up a hermetic bundle environment and return (dashboard, bundle, config)."""
    dashboard_dir = tmp_path / "dashboard"
    dashboard_dir.mkdir()
    sample_src = dashboard_dir / "TEST_exec_commentary_SAMPLE.md"
    sample_src.write_text(
        "# TEST Commentary (Sample)\nRevenue $1.0B [0000000000-00-000000]\n",
        encoding="utf-8",
    )
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "ticker: TEST\ncik: '0000000000'\ncik_int: 0\nname: Test Co\n"
        "fiscal_year_end_month: 12\nfiscal_year_end_day: 31\n",
        encoding="utf-8",
    )

    def _no_op(*_args: Any, **_kwargs: Any) -> bool:
        return True

    def _no_op_path(b_dir: Path) -> Path:
        out = b_dir / "_stub.html"
        out.write_text("stub", encoding="utf-8")
        return out

    monkeypatch.setattr(build_notebooklm_bundle, "_DASHBOARD_DIR", dashboard_dir)
    monkeypatch.setattr(build_notebooklm_bundle, "_BUNDLE_DIR", bundle_dir)
    monkeypatch.setattr(build_notebooklm_bundle, "_CONFIG_PATH", config_path)
    monkeypatch.setattr(build_notebooklm_bundle, "_PROCESSED_DIR", tmp_path)
    monkeypatch.setattr(build_notebooklm_bundle, "_MODELS_DIR", tmp_path)
    monkeypatch.setattr(build_notebooklm_bundle, "_download_sec_filing", _no_op)
    monkeypatch.setattr(build_notebooklm_bundle, "_generate_test_report", _no_op_path)
    monkeypatch.setattr(build_notebooklm_bundle, "_generate_eval_report", _no_op_path)

    return dashboard_dir, bundle_dir, config_path


def test_bundle_uses_sample_when_no_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No ANTHROPIC_API_KEY → bundle ships the committed SAMPLE content as 07_exec_commentary.md.

    The fallback is intentional: this machine has Claude subscription only, no
    API key, and ``make demo`` must still produce a complete bundle. We assert
    that the bundle's 07_exec_commentary.md content matches the SAMPLE source
    (banner aside) and that no live-generator code path was invoked.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    dashboard_dir, bundle_dir, _ = _stub_bundle_env(tmp_path, monkeypatch)
    sample_src = dashboard_dir / "TEST_exec_commentary_SAMPLE.md"
    sample_text = sample_src.read_text(encoding="utf-8")

    # Spy on generate_commentary.generate so we can confirm it was NOT called.
    from src import generate_commentary as gc

    spy = MagicMock()
    monkeypatch.setattr(gc, "generate", spy)

    with caplog.at_level(logging.INFO, logger=build_notebooklm_bundle.logger.name):
        written = build_notebooklm_bundle.build(ticker="TEST")

    bundled = bundle_dir / "07_exec_commentary.md"
    assert bundled.exists()
    assert written["07_exec_commentary"] == bundled
    body = bundled.read_text(encoding="utf-8")
    # SAMPLE source content must appear verbatim in the bundle (banner is prepended).
    assert sample_text in body, "Sample source content was not preserved in the bundle"
    spy.assert_not_called()


def test_bundle_calls_live_generator_when_api_key_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ANTHROPIC_API_KEY present → live commentary generator is invoked, no real API call.

    We mock ``generate_commentary.generate`` so no Anthropic API request is
    made and a fake live commentary is materialised in ``dashboard/`` for the
    bundler to pick up.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
    dashboard_dir, bundle_dir, _ = _stub_bundle_env(tmp_path, monkeypatch)

    # Mock the live generator: instead of calling the real Anthropic API, write
    # a fake live-commentary file to the dashboard dir under the canonical
    # naming pattern that ``_find_latest_commentary`` already understands.
    from src import generate_commentary as gc

    def _fake_generate(ticker: str | None = None, **_kwargs: Any) -> Path:
        live_path = dashboard_dir / f"{ticker}_exec_commentary_20260601.md"
        live_path.write_text(
            f"# {ticker} Live Commentary\nRevenue $9.9B [9999999999-99-999999]\n",
            encoding="utf-8",
        )
        return live_path

    spy = MagicMock(side_effect=_fake_generate)
    monkeypatch.setattr(gc, "generate", spy)

    written = build_notebooklm_bundle.build(ticker="TEST")

    spy.assert_called_once()
    # Confirm dry_run is False — i.e. we actually requested a live regen.
    _, kwargs = spy.call_args
    assert kwargs.get("dry_run") is False, "Live regen must be invoked with dry_run=False"

    bundled = bundle_dir / "07_exec_commentary.md"
    assert bundled.exists()
    assert written["07_exec_commentary"] == bundled
    body = bundled.read_text(encoding="utf-8")
    # Live content (not the SAMPLE) is what landed in the bundle.
    assert "Live Commentary" in body
    assert (
        "SAMPLE — illustrative only" not in body
    ), "Live commentary should not carry the SAMPLE banner"


def test_print_upload_instructions_includes_path_and_url(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI footer must surface both the bundle path and the manual upload URL.

    The pipeline writes the bundle but does not upload it — NotebookLM has no
    public API. The operator step is to drag the bundle's files into
    notebooklm.google.com. README/HOW_TO_DEMO point at this; the runtime
    output must back them up so the demo isn't a copy-paste game with the
    docs.
    """
    from src.build_notebooklm_bundle import _print_upload_instructions

    files = {
        "company_overview": tmp_path / "01_overview.md",
        "exec_commentary": tmp_path / "07_exec_commentary.md",
    }
    for path in files.values():
        path.write_text("stub", encoding="utf-8")

    _print_upload_instructions(tmp_path, files)
    captured = capsys.readouterr().out

    # Absolute bundle path so the operator can copy-paste into Finder/explorer.
    assert (
        str(tmp_path.resolve()) in captured
    ), "Upload instructions must include the absolute bundle path."
    # Manual upload destination — NotebookLM has no API.
    assert "https://notebooklm.google.com" in captured, (
        "Upload instructions must point at notebooklm.google.com so the "
        "operator knows where to drop the bundle."
    )
    # Honest framing: this is a manual step, not an automated one.
    assert "manual" in captured.lower(), (
        "The footer must call out that upload is a manual operator step "
        "(NotebookLM has no public API) so docs and runtime stay aligned."
    )


def test_bundle_log_message_distinguishes_sample_from_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The bundle logs an honest, distinct message for each branch.

    Pin the exact fallback line so a future refactor can't silently swap it
    for something less informative.
    """
    expected_fallback = (
        "ANTHROPIC_API_KEY not set — bundling SAMPLE commentary. "
        "To regenerate live, set the key and re-run `make demo` "
        "(or `make commentary LIVE=1`)."
    )

    # Branch A — no key.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _stub_bundle_env(tmp_path, monkeypatch)
    with caplog.at_level(logging.INFO, logger=build_notebooklm_bundle.logger.name):
        build_notebooklm_bundle.build(ticker="TEST")
    fallback_messages = [r.getMessage() for r in caplog.records]
    assert any(
        expected_fallback in msg for msg in fallback_messages
    ), f"Expected fallback log line not emitted. Got:\n{fallback_messages}"
    assert not any("Live commentary regenerated" in msg for msg in fallback_messages)

    # Branch B — key present, mocked generator.
    caplog.clear()
    tmp2 = tmp_path / "round2"
    tmp2.mkdir()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
    dashboard_dir, _, _ = _stub_bundle_env(tmp2, monkeypatch)

    from src import generate_commentary as gc

    def _fake_generate(ticker: str | None = None, **_kwargs: Any) -> Path:
        p = dashboard_dir / f"{ticker}_exec_commentary_20260601.md"
        p.write_text(f"# {ticker} Live\nRevenue $1.0B [0000000000-00-000000]\n", encoding="utf-8")
        return p

    monkeypatch.setattr(gc, "generate", MagicMock(side_effect=_fake_generate))
    with caplog.at_level(logging.INFO, logger=build_notebooklm_bundle.logger.name):
        build_notebooklm_bundle.build(ticker="TEST")
    live_messages = [r.getMessage() for r in caplog.records]
    assert any(
        "Live commentary regenerated" in msg for msg in live_messages
    ), f"Expected live-regen log line not emitted. Got:\n{live_messages}"
    assert not any(expected_fallback in msg for msg in live_messages)


def test_historical_financials_uses_canonical_export(tmp_path: Path) -> None:
    """04_historical_financials.csv inherits the canonical export contract.

    The bundle's CSV-builder must reuse ``_export_fact_financials`` so that
    every (ticker, line_item, period_end) appears at most once and every row
    carries a non-null accession_no.  Previously the bundle ran its own SQL
    against ``v_canonical_facts`` and silently emitted YTD-vs-standalone
    duplicates plus rows with missing provenance.
    """
    import json

    import yaml

    from src.build_warehouse import build as build_warehouse
    from src.ingest_edgar import ingest

    fixtures_dir = Path(__file__).parent / "fixtures"
    with (fixtures_dir / "panw_companyfacts.json").open() as fh:
        facts = json.load(fh)

    config: dict[str, Any] = {
        "cik": "0001327567",
        "cik_int": 1327567,
        "ticker": "PANW",
        "name": "Test PANW",
        "fiscal_year_end_month": 7,
        "fiscal_year_end_day": 31,
        "sector_etf": "XLK",
    }
    config_path = tmp_path / "company.yaml"
    with config_path.open("w") as fh:
        yaml.dump(config, fh)

    with (
        patch("src.ingest_edgar._CONFIG_PATH", config_path),
        patch("src.ingest_edgar._DATA_DIR", tmp_path),
    ):
        ingest(ticker="PANW", years=10, facts_json=facts)

    with (
        patch("src.build_warehouse._CONFIG_PATH", config_path),
        patch("src.build_warehouse._PROCESSED_DIR", tmp_path),
    ):
        build_warehouse(ticker="PANW")

    with patch.object(build_notebooklm_bundle, "_PROCESSED_DIR", tmp_path):
        df = _build_historical_financials("PANW", fy_end_month=7)

    assert df is not None and len(df) > 0, "Bundle CSV should not be empty for PANW fixture"

    # Invariant 1: no duplicates per period_end (would have halved Tableau values).
    dupes = df.duplicated(subset=["period_end"]).sum()
    assert dupes == 0, f"Bundle CSV has {dupes} duplicate period_end rows"

    # Invariant 2: every row carries provenance.
    missing = df["accession_no"].isna().sum()
    assert missing == 0, f"Bundle CSV has {missing} rows with null accession_no"

    # Invariant 3: standalone (not YTD) values.  Q2 standalone Revenue for PANW
    # 2025-01-31 is ~2.257B; the YTD H1 cumulative is ~4.396B.  If the YTD row
    # ever wins the QUALIFY race in _export_fact_financials, this catches it.
    q2 = df[df["period_end"] == "2025-01-31"]
    if len(q2) > 0:
        rev = float(q2.iloc[0]["Revenue"])
        assert rev < 3.0e9, (
            f"Revenue for 2025-01-31 was {rev:.3e}; expected ~2.257B (Q2 standalone), "
            "not the ~4.396B YTD H1 cumulative."
        )

    # Invariant 4: distinct period_ends carry distinct (fiscal_year, fiscal_period)
    # pairs — comparative rows must not inherit the newer filing's labels.
    # ai-financial-analyst-bau regression catch.
    pair_to_periods = df.groupby(["fiscal_year", "fiscal_period"])["period_end"].nunique()
    assert (pair_to_periods <= 1).all(), (
        "Bundle CSV has fiscal labels duplicated across distinct period_ends:\n"
        f"{pair_to_periods[pair_to_periods > 1]}"
    )

    # Specific PANW check: 2025-01-31 must be FY2025 Q2 (July fiscal year),
    # NOT FY2026 Q2 inherited from the FY2026 Q2 10-Q's comparative row.
    if len(q2) > 0:
        assert (int(q2.iloc[0]["fiscal_year"]), q2.iloc[0]["fiscal_period"]) == (2025, "Q2"), (
            f"period_end=2025-01-31 should be (2025, Q2) for PANW July FY; got "
            f"({q2.iloc[0]['fiscal_year']}, {q2.iloc[0]['fiscal_period']})"
        )


def test_forecast_summary_renders_table_without_tabulate(tmp_path: Path) -> None:
    """05_forecast_summary.md must render a real table, not the tabulate stub.

    df.to_markdown() requires the optional ``tabulate`` package. When it
    isn't installed pandas inserts the literal string
        "Missing optional dependency 'tabulate'"
    into the output. NotebookLM ingests that as a citation source, which is
    worse than no table at all. We render the table ourselves.
    """
    parquet = tmp_path / "TEST_baseline_forecasts.parquet"
    pd.DataFrame(
        {
            "model": ["prophet", "autoarima"],
            "period_end": pd.to_datetime(["2026-04-30", "2026-07-31"]),
            "yhat": [1_000_000_000.0, 1_100_000_000.0],
            "yhat_lower_80": [9.0e8, 1.0e9],
            "yhat_upper_80": [1.1e9, 1.2e9],
        }
    ).to_parquet(parquet, index=False)

    with patch.object(build_notebooklm_bundle, "_MODELS_DIR", tmp_path):
        md = _build_forecast_summary("TEST")

    assert (
        "Missing optional dependency" not in md
    ), f"Forecast summary leaked the tabulate-missing stub:\n{md}"
    # The header row of the rendered table must be present.
    assert (
        "| model | period_end | yhat | yhat_lower_80 | yhat_upper_80 |" in md
    ), f"Expected markdown table header not found in output:\n{md}"


# ── Verification & hygiene prompts (gap #4) ───────────────────────────────────


def test_readme_contains_verification_and_hygiene_prompts() -> None:
    """README ships the audit-grade verification prompts the bundle is built for.

    The bundle's whole reason to exist is provenance — every fact row carries
    accession_no + filing_url. The README's prompt guide should turn that into
    questions a reviewer can run: provenance audit, contradiction-finding,
    unsupported-claim flagging, and negative-space ('what's missing').
    """
    readme = build_notebooklm_bundle._build_notebooklm_readme("TEST", is_sample=False)

    assert "## Verification & hygiene" in readme, (
        "README must contain a top-level Verification & hygiene section "
        "alongside Financial analysis and Methodology."
    )
    # Provenance audit prompt — every cited number with source/period/source-type.
    assert "every cited number" in readme.lower()
    assert "source type" in readme.lower()
    # Contradiction-finding prompt — transcript vs 10-Q.
    assert "contradictions" in readme.lower()
    assert "8-K" in readme or "earnings release" in readme.lower(), (
        "Contradiction prompt should mention the 8-K/earnings release as one "
        "of the sources to compare against the 10-Q/10-K."
    )
    # Unsupported-claims prompt — flag claims with no source.
    assert "unsupported" in readme.lower()
    # Negative-space / what-would-confirm-or-refute prompt.
    assert "confirm or refute" in readme.lower() or "missing" in readme.lower()
    # Time-anchor prompt — bundles are snapshots; questions about latest must
    # be grounded against the data-as-of marker.
    assert "as of" in readme.lower() or "data through" in readme.lower()


def test_readme_calls_out_enterprise_api_path() -> None:
    """The 'no public API' line must mention the Enterprise API future path.

    Consumer NotebookLM has no public API; NotebookLM Enterprise on Google
    Cloud does. The bundle README should not assert the bare 'no public API'
    claim without the Enterprise caveat.
    """
    readme = build_notebooklm_bundle._build_notebooklm_readme("TEST", is_sample=False)

    assert (
        "Enterprise" in readme
    ), "README must reference NotebookLM Enterprise as the future automated path."


# ── Earnings 8-K download (gap #1 slice) ──────────────────────────────────────


def _fake_subs_with_8ks(
    items_per_8k: list[str],
    primary_doc: str = "ex991-earningspress.pdf",
) -> dict[str, Any]:
    """Build a fake EDGAR submissions JSON containing a list of 8-Ks.

    Each 8-K's ``items`` cell carries the comma-separated Item codes the
    filer reported (e.g. ``"2.02,9.01"`` for an earnings release with
    exhibits). The function under test must filter to 8-Ks whose ``items``
    contains ``2.02`` and pick the most recent.
    """
    return {
        "filings": {
            "recent": {
                "form": ["8-K"] * len(items_per_8k),
                "accessionNumber": [f"0001234567-25-{i:06d}" for i in range(len(items_per_8k))],
                "primaryDocument": [primary_doc] * len(items_per_8k),
                "items": items_per_8k,
            }
        }
    }


def test_download_earnings_8k_picks_most_recent_with_item_2_02(tmp_path: Path) -> None:
    """The picker must prefer the most recent 8-K with Item 2.02 in items.

    EDGAR's submissions API returns filings in reverse-chronological order,
    so index 0 is the most recent. We construct three 8-Ks: index 0 is a
    non-earnings 8-K (Item 8.01 — Other Events), index 1 is an earnings 8-K
    (Item 2.02), index 2 is a stale earnings 8-K. The picker must select
    index 1 (newest 2.02) by URL.
    """
    from src.build_notebooklm_bundle import _download_earnings_8k

    fake_subs = _fake_subs_with_8ks(
        items_per_8k=[
            "8.01",  # most recent: non-earnings — must NOT be picked
            "2.02,9.01",  # earnings release with exhibits — should win
            "2.02",  # older earnings release
        ]
    )
    captured_urls: list[str] = []

    class _FakeResp:
        status_code = 200
        content = b"%PDF-1.4 fake earnings release"

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return fake_subs

    def _fake_get(url: str, **_kwargs: Any) -> _FakeResp:
        captured_urls.append(url)
        return _FakeResp()

    dest = tmp_path / "03b_latest_earnings_8K.pdf"
    with patch("src.build_notebooklm_bundle.requests.get", _fake_get):
        ok = _download_earnings_8k(1327567, dest)

    assert ok is True
    assert dest.exists()
    # Two GETs: the submissions API + the primary doc download.
    doc_urls = [u for u in captured_urls if "/Archives/edgar/" in u]
    assert len(doc_urls) == 1, f"Expected one Archives GET, got: {captured_urls}"
    # The accession in the URL is the index-1 filing's accession with dashes
    # stripped: 0001234567-25-000001 → 000123456725000001.
    assert "000123456725000001" in doc_urls[0], (
        f"Picker should have downloaded the index-1 (newest 2.02) filing, "
        f"but URL was: {doc_urls[0]}"
    )


def test_download_earnings_8k_falls_back_to_txt_for_non_pdf(tmp_path: Path) -> None:
    """Non-PDF earnings releases produce a .txt placeholder, not a broken PDF.

    Mirrors the _download_sec_filing fallback contract: if the primary
    document isn't a PDF, write a .txt placeholder pointing at the EDGAR
    index page so NotebookLM still has *something* to ingest, and return
    True (the bundle is still complete from the caller's perspective).
    """
    from src.build_notebooklm_bundle import _download_earnings_8k

    fake_subs = _fake_subs_with_8ks(
        items_per_8k=["2.02"],
        primary_doc="ex991-earningspress.htm",
    )

    class _FakeResp:
        status_code = 200
        content = b""

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return fake_subs

    def _fake_get(url: str, **_kwargs: Any) -> _FakeResp:
        return _FakeResp()

    dest = tmp_path / "03b_latest_earnings_8K.pdf"
    with patch("src.build_notebooklm_bundle.requests.get", _fake_get):
        ok = _download_earnings_8k(1327567, dest)

    placeholder = dest.with_suffix(".txt")
    assert ok is True
    assert placeholder.exists()
    assert not dest.exists(), (
        "Non-PDF filings must NOT leave an empty .pdf next to the placeholder; "
        "NotebookLM would ingest both."
    )


def test_build_includes_earnings_8k_in_returned_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``build()`` must publish the 03b earnings-8K entry in its result dict.

    This locks the wiring contract: the new download function is actually
    called from ``build`` and the result is registered under the
    ``03b_latest_earnings_8K`` label. Without this, the function could be
    written but never invoked, and tests would still pass module-level.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    dashboard_dir = tmp_path / "dashboard"
    dashboard_dir.mkdir()
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "ticker: TEST\ncik: '0000000000'\ncik_int: 0\nname: Test Co\n"
        "fiscal_year_end_month: 12\nfiscal_year_end_day: 31\n",
        encoding="utf-8",
    )

    def _no_op(*_args: Any, **_kwargs: Any) -> bool:
        return True

    def _no_op_path(b_dir: Path) -> Path:
        out = b_dir / "_stub.html"
        out.write_text("stub", encoding="utf-8")
        return out

    earnings_8k_calls: list[tuple[int, Path]] = []

    def _record_earnings_8k(cik_int: int, dest: Path) -> bool:
        earnings_8k_calls.append((cik_int, dest))
        # Simulate a successful PDF download by writing the file.
        dest.write_bytes(b"%PDF-1.4 fake earnings 8-K")
        return True

    with (
        patch.object(build_notebooklm_bundle, "_DASHBOARD_DIR", dashboard_dir),
        patch.object(build_notebooklm_bundle, "_BUNDLE_DIR", bundle_dir),
        patch.object(build_notebooklm_bundle, "_CONFIG_PATH", config_path),
        patch.object(build_notebooklm_bundle, "_PROCESSED_DIR", tmp_path),
        patch.object(build_notebooklm_bundle, "_MODELS_DIR", tmp_path),
        patch.object(build_notebooklm_bundle, "_download_sec_filing", _no_op),
        patch.object(build_notebooklm_bundle, "_download_earnings_8k", _record_earnings_8k),
        patch.object(build_notebooklm_bundle, "_generate_test_report", _no_op_path),
        patch.object(build_notebooklm_bundle, "_generate_eval_report", _no_op_path),
    ):
        written = build_notebooklm_bundle.build(ticker="TEST")

    assert "03b_latest_earnings_8K" in written, (
        f"build() did not register the earnings 8-K file; written keys: "
        f"{sorted(written.keys())}"
    )
    earnings_path = written["03b_latest_earnings_8K"]
    assert earnings_path.exists()
    assert earnings_path.name == "03b_latest_earnings_8K.pdf"
    # Confirm _download_earnings_8k was actually called from build().
    assert len(earnings_8k_calls) == 1
    assert earnings_8k_calls[0][1].name == "03b_latest_earnings_8K.pdf"


def test_download_earnings_8k_returns_false_when_no_2_02_present(tmp_path: Path) -> None:
    """If no recent 8-K carries Item 2.02, the picker reports failure cleanly.

    The bundle build must not crash — it should log a warning and continue,
    same contract as ``_download_sec_filing`` when no matching form is found.
    """
    from src.build_notebooklm_bundle import _download_earnings_8k

    fake_subs = _fake_subs_with_8ks(items_per_8k=["8.01", "5.02", "1.01"])

    class _FakeResp:
        status_code = 200
        content = b""

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return fake_subs

    def _fake_get(url: str, **_kwargs: Any) -> _FakeResp:
        return _FakeResp()

    dest = tmp_path / "03b_latest_earnings_8K.pdf"
    with patch("src.build_notebooklm_bundle.requests.get", _fake_get):
        ok = _download_earnings_8k(1327567, dest)

    assert ok is False
    assert not dest.exists()
    assert not dest.with_suffix(".txt").exists()
