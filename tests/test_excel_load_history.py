"""Tests for ``_load_history`` window selection and ordering contract.

Regression test for the bug where ``_load_history`` selected the *earliest*
``_N_HIST`` quarters in the warehouse (``ORDER BY ... ASC LIMIT N``) instead of
the *latest* — building the model on stale data for issuers whose history
extends well beyond the model's 12-quarter window (e.g. PANW, FY2017+).

The fix wraps the per-statement query in a subquery that orders DESC + LIMIT
(grabbing the latest N), then re-sorts ASC on the outer query so the docstring's
"oldest-first" return contract still holds for downstream consumers
(``_compute_base_assumptions``, ``_forecast_periods``).
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
import pytest

from src.build_excel_model import _N_HIST, _load_history

# ── Fixture warehouse ──────────────────────────────────────────────────────────

# 16 quarters of synthetic data: 2021Q1 .. 2024Q4. The latest 12 are
# 2022Q1 .. 2024Q4; the earliest 12 are 2021Q1 .. 2023Q4. The test asserts
# we get the former (post-fix), not the latter (pre-fix).
_QUARTERS: list[tuple[int, str, str]] = [
    (year, f"Q{q}", f"{year}-{3 * q:02d}-{28 if q != 1 else 31}")
    for year in (2021, 2022, 2023, 2024)
    for q in (1, 2, 3, 4)
]
assert len(_QUARTERS) == 16


def _build_synthetic_warehouse(db_path: Path) -> None:
    """Create the four views ``_load_history`` reads, with 16 quarters of data.

    We bypass the full ``raw_financials`` → ``v_canonical_facts`` pipeline and
    create the views directly as tables. ``_load_history`` only cares about the
    columns it (or its ``sources`` UNION ALL) selects, so we include only those.
    """
    con = duckdb.connect(str(db_path))
    try:
        rows: list[dict[str, object]] = []
        for i, (fy, fp, pe) in enumerate(_QUARTERS):
            rows.append(
                {
                    "period_end": pe,
                    "fiscal_year": fy,
                    "fiscal_period": fp,
                    "period_type": "Q",
                    # IS columns referenced by sources UNION ALL
                    "Revenue": 1_000_000_000 + i * 50_000_000,
                    "revenue_accession": f"acc-rev-{i}",
                    "revenue_filing_url": f"https://example/rev-{i}",
                    "OperatingIncome": 200_000_000 + i * 10_000_000,
                    "operating_income_accession": f"acc-oi-{i}",
                    "operating_income_filing_url": f"https://example/oi-{i}",
                    "NetIncome": 150_000_000 + i * 8_000_000,
                    "net_income_accession": f"acc-ni-{i}",
                    "net_income_filing_url": f"https://example/ni-{i}",
                }
            )
        is_df = pd.DataFrame(rows)
        con.register("is_df", is_df)
        con.execute("CREATE TABLE v_income_statement_quarterly AS SELECT * FROM is_df")

        bs_rows: list[dict[str, object]] = []
        for i, (fy, fp, pe) in enumerate(_QUARTERS):
            bs_rows.append(
                {
                    "period_end": pe,
                    "fiscal_year": fy,
                    "fiscal_period": fp,
                    "period_type": "Q",
                    "Cash": 5_000_000_000 + i * 100_000_000,
                    "cash_accession": f"acc-cash-{i}",
                    "cash_filing_url": f"https://example/cash-{i}",
                    "AccountsReceivable": 600_000_000 + i * 20_000_000,
                    "ar_accession": f"acc-ar-{i}",
                    "ar_filing_url": f"https://example/ar-{i}",
                    "TotalAssets": 10_000_000_000 + i * 200_000_000,
                    "TotalLiabilities": 4_000_000_000 + i * 80_000_000,
                    "TotalEquity": 6_000_000_000 + i * 120_000_000,
                }
            )
        bs_df = pd.DataFrame(bs_rows)
        con.register("bs_df", bs_df)
        con.execute("CREATE TABLE v_balance_sheet_quarterly AS SELECT * FROM bs_df")

        cf_rows: list[dict[str, object]] = []
        for i, (fy, fp, pe) in enumerate(_QUARTERS):
            cf_rows.append(
                {
                    "period_end": pe,
                    "fiscal_year": fy,
                    "fiscal_period": fp,
                    "period_type": "Q",
                    "OperatingCashFlow": 300_000_000 + i * 12_000_000,
                    "ocf_accession": f"acc-ocf-{i}",
                    "ocf_filing_url": f"https://example/ocf-{i}",
                }
            )
        cf_df = pd.DataFrame(cf_rows)
        con.register("cf_df", cf_df)
        con.execute("CREATE TABLE v_cash_flow_quarterly AS SELECT * FROM cf_df")

        # v_data_quality: one-row table with the flag _load_history reads.
        con.execute("CREATE TABLE v_data_quality AS SELECT FALSE AS has_physical_inventory")
    finally:
        con.close()


@pytest.fixture()
def synthetic_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "synthetic.duckdb"
    _build_synthetic_warehouse(db_path)
    return db_path


# ── Constants for assertions ───────────────────────────────────────────────────

# Pre-fix bug would return 2021Q1..2023Q4 (earliest 12). Post-fix returns
# 2022Q1..2024Q4 (latest 12).
_LATEST_12: list[tuple[int, str]] = [(fy, fp) for fy, fp, _ in _QUARTERS[-_N_HIST:]]
_EARLIEST_12: list[tuple[int, str]] = [(fy, fp) for fy, fp, _ in _QUARTERS[:_N_HIST]]


def test_n_hist_is_twelve() -> None:
    """Sanity: the module-level constant is the value the test was authored against."""
    assert _N_HIST == 12


def test_load_history_returns_latest_n_quarters(synthetic_db: Path) -> None:
    """All three frames must contain the *latest* 12 quarters, not the earliest."""
    hist_is, hist_bs, hist_cf, _has_inv, _sources = _load_history(synthetic_db)

    for name, df in (("hist_is", hist_is), ("hist_bs", hist_bs), ("hist_cf", hist_cf)):
        periods = list(zip(df["fiscal_year"].tolist(), df["fiscal_period"].tolist(), strict=True))
        assert (
            periods == _LATEST_12
        ), f"{name} returned {periods}; expected latest 12 {_LATEST_12} (not earliest 12)"
        assert (
            periods != _EARLIEST_12
        ), f"{name} returned the earliest 12 quarters — pre-fix bug regressed"


def test_load_history_returns_oldest_first(synthetic_db: Path) -> None:
    """Docstring contract: returned rows are oldest-first."""
    hist_is, hist_bs, hist_cf, _has_inv, _sources = _load_history(synthetic_db)

    for name, df in (("hist_is", hist_is), ("hist_bs", hist_bs), ("hist_cf", hist_cf)):
        first = (df["fiscal_year"].iloc[0], df["fiscal_period"].iloc[0])
        last = (df["fiscal_year"].iloc[-1], df["fiscal_period"].iloc[-1])
        assert first == (2022, "Q1"), f"{name} first row {first} != 2022Q1"
        assert last == (2024, "Q4"), f"{name} last row {last} != 2024Q4"

        # Strictly increasing by (fiscal_year, fiscal_period) — Q1<Q2<Q3<Q4 lex-sorts correctly.
        keys = list(zip(df["fiscal_year"].tolist(), df["fiscal_period"].tolist(), strict=True))
        assert keys == sorted(keys), f"{name} rows are not oldest-first: {keys}"


def test_load_history_three_frames_aligned(synthetic_db: Path) -> None:
    """All three frames return _N_HIST rows with matching periods."""
    hist_is, hist_bs, hist_cf, _has_inv, _sources = _load_history(synthetic_db)

    assert len(hist_is) == _N_HIST
    assert len(hist_bs) == _N_HIST
    assert len(hist_cf) == _N_HIST

    is_periods = list(zip(hist_is["fiscal_year"], hist_is["fiscal_period"], strict=True))
    bs_periods = list(zip(hist_bs["fiscal_year"], hist_bs["fiscal_period"], strict=True))
    cf_periods = list(zip(hist_cf["fiscal_year"], hist_cf["fiscal_period"], strict=True))

    assert is_periods == bs_periods == cf_periods
