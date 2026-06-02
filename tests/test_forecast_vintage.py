"""Tests for forecast vintage stamping (src/build_forecasts.py).

The sibling tests in ``test_tableau_export.py`` cover how
``_export_fact_forecasts`` propagates ``forecast_run_date`` and how
``fact_forecasts.csv`` accumulates vintaged snapshots.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pandas as pd

from src.build_forecasts import (
    FORECAST_RUN_DATE_COL,
    stamp_forecast_run_date,
    write_forecast_parquet,
)

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _stub_forecast_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "model": ["prophet", "prophet"],
            "period_end": pd.to_datetime(["2026-09-30", "2026-12-31"]),
            "yhat": [1.0e9, 1.1e9],
            "yhat_lower_80": [0.9e9, 1.0e9],
            "yhat_upper_80": [1.1e9, 1.2e9],
            "yhat_lower_95": [0.8e9, 0.9e9],
            "yhat_upper_95": [1.2e9, 1.3e9],
        }
    )


def test_stamp_forecast_run_date_uses_today_utc_iso_when_unspecified() -> None:
    df = stamp_forecast_run_date(_stub_forecast_df())
    assert FORECAST_RUN_DATE_COL in df.columns
    stamps = df[FORECAST_RUN_DATE_COL].unique()
    assert len(stamps) == 1, "one vintage stamp per write — all rows share it"
    assert _ISO_DATE_RE.match(stamps[0]), f"not ISO YYYY-MM-DD: {stamps[0]!r}"


def test_stamp_forecast_run_date_accepts_explicit_date_object() -> None:
    df = stamp_forecast_run_date(_stub_forecast_df(), run_date=date(2026, 1, 15))
    assert (df[FORECAST_RUN_DATE_COL] == "2026-01-15").all()


def test_stamp_forecast_run_date_accepts_explicit_iso_string() -> None:
    df = stamp_forecast_run_date(_stub_forecast_df(), run_date="2026-06-01")
    assert (df[FORECAST_RUN_DATE_COL] == "2026-06-01").all()


def test_stamp_forecast_run_date_does_not_mutate_input() -> None:
    df = _stub_forecast_df()
    original_cols = list(df.columns)
    stamp_forecast_run_date(df, run_date="2026-06-01")
    assert list(df.columns) == original_cols, "input frame must not gain columns"


def test_forecast_run_date_stamped_at_parquet_write_time(tmp_path: Path) -> None:
    """The parquet round-trip preserves the ISO-string vintage stamp."""
    out_path = tmp_path / "TEST_baseline_forecasts.parquet"
    write_forecast_parquet(_stub_forecast_df(), out_path, run_date="2026-06-01")

    assert out_path.exists()
    round_trip = pd.read_parquet(out_path)
    assert FORECAST_RUN_DATE_COL in round_trip.columns
    assert (round_trip[FORECAST_RUN_DATE_COL] == "2026-06-01").all()
    # The non-stamp columns survive too.
    assert (round_trip["model"] == "prophet").all()
    assert len(round_trip) == 2
