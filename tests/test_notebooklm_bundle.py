"""Tests for src/build_notebooklm_bundle.py.

Covers two reviewer-facing invariants:
  - The bundle's Excel summary reads the file actually produced by
    src/build_excel_model.py (filename agreement).
  - When a real PDF is downloaded for a filing, no sibling .txt placeholder
    is left on disk for NotebookLM to ingest.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

from src import build_excel_model, build_notebooklm_bundle
from src.build_notebooklm_bundle import _build_excel_model_summary, _download_sec_filing


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

    assert "not found" not in summary, (
        "Bundle could not find the Excel file at the canonical path; " f"summary said:\n{summary}"
    )
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
