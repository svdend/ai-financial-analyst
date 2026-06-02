"""Forecast persistence helpers — vintage-stamped parquet writes.

Forecast notebooks (``notebooks/02_baseline_forecast.ipynb``,
``notebooks/03_macro_regularized_forecast.ipynb``) call
:func:`write_forecast_parquet` to persist their outputs. Every row is stamped
with ``forecast_run_date`` — a UTC ISO date string (``YYYY-MM-DD``) recording
when the model was trained, *not* when the parquet is later read or exported.

Why a string column instead of a datetime: keeps Tableau CSV diffs tight (one
day-granular value per vintage) and avoids tz/serialisation drift between
parquet and CSV round-trips. Day-precision is the right grain for the
"forecasts you've made vs how they landed" scorecard — sub-day vintages would
just be noise.

One vintage per parquet file: every row in a single write shares the same
``forecast_run_date``. Re-running a notebook overwrites the parquet with a new
vintage; the append-only ``fact_forecasts.csv`` accumulates the history.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

FORECAST_RUN_DATE_COL = "forecast_run_date"


def _today_iso() -> str:
    """Return today's UTC date as ``YYYY-MM-DD``.

    Indirection exists so tests can monkey-patch the clock without reaching
    into ``datetime`` globally.
    """
    return datetime.now(UTC).date().isoformat()


def stamp_forecast_run_date(
    df: pd.DataFrame,
    run_date: str | date | None = None,
) -> pd.DataFrame:
    """Return a copy of *df* with a ``forecast_run_date`` column.

    Args:
        df: Forecast DataFrame (must already have ``model``, ``period_end``,
            ``yhat``, etc.).
        run_date: Optional override. If a :class:`datetime.date`, it is
            ISO-formatted; if a string, it is used verbatim (caller is
            responsible for ``YYYY-MM-DD`` shape). Defaults to today UTC.

    Returns:
        New DataFrame with ``forecast_run_date`` set on every row.
    """
    if run_date is None:
        stamp = _today_iso()
    elif isinstance(run_date, date):
        stamp = run_date.isoformat()
    else:
        stamp = str(run_date)

    out = df.copy()
    out[FORECAST_RUN_DATE_COL] = stamp
    return out


def write_forecast_parquet(
    df: pd.DataFrame,
    out_path: Path,
    run_date: str | date | None = None,
) -> Path:
    """Stamp *df* with ``forecast_run_date`` and write it to *out_path*.

    The ``forecast_run_date`` column captures provenance — the date the model
    trained — and is the join key for the future forecast-vs-actuals scorecard.

    Args:
        df: Forecast DataFrame to persist.
        out_path: Destination ``.parquet`` path. Parent directory must exist.
        run_date: Optional vintage override (see :func:`stamp_forecast_run_date`).

    Returns:
        The *out_path* that was written, for chaining.
    """
    stamped = stamp_forecast_run_date(df, run_date=run_date)
    stamped.to_parquet(out_path, index=False)
    return out_path
