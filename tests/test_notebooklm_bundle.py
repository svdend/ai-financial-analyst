"""Tests for src/build_notebooklm_bundle.py.

Covers reviewer-facing invariants:
  - The bundle's Excel summary reads the file actually produced by
    src/build_excel_model.py (filename agreement).
  - When a real PDF is downloaded for a filing, no sibling .txt placeholder
    is left on disk for NotebookLM to ingest.
  - Forecast summary renders a real markdown table with no "Missing
    optional dependency 'tabulate'" stub leaking through.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pandas as pd

from src import build_excel_model, build_notebooklm_bundle
from src.build_notebooklm_bundle import (
    _build_excel_model_summary,
    _build_forecast_summary,
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
