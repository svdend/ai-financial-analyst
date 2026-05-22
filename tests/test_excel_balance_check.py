"""BalanceCheck integration test for the Excel three-statement forecaster.

The README and docs/MODELING_DECISIONS.md §6 advertise the v6→v7 BalanceCheck
fix as a representative win. This test asserts the actual invariant:

    BalanceCheck = TotalAssets − (TotalLiabilities + TotalEquity) ≈ 0

over every forecast quarter × scenario for synthetic inputs.

We exercise the pure-Python forecaster (``_forecast_periods``) directly with
hand-built historical DataFrames so the test does NOT depend on the full
warehouse pipeline or openpyxl. This deliberately mirrors the v7 design
decision to verify BalanceCheck in Python rather than via openpyxl
data_only=True (which silently passes on freshly-generated workbooks; that's
the v6 bug).
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.build_excel_model import (
    _compute_base_assumptions,
    _forecast_periods,
    _make_scenarios,
)


def _hist_is() -> pd.DataFrame:
    """Synthetic income statement: 5 quarters, ~10% QoQ revenue growth, 75% GM."""
    revenue = [1_600_000_000, 1_760_000_000, 1_936_000_000, 2_129_600_000, 2_342_560_000]
    cogs = [r * 0.25 for r in revenue]
    gp = [r - c for r, c in zip(revenue, cogs, strict=True)]
    opex = [400_000_000 * (1.05**i) for i in range(5)]
    op_income = [g - o for g, o in zip(gp, opex, strict=True)]
    net_income = [oi * 0.85 for oi in op_income]  # ~15% effective tax
    return pd.DataFrame(
        {
            "Revenue": revenue,
            "CostOfRevenue": cogs,
            "GrossProfit": gp,
            "OperatingExpenses": opex,
            "OperatingIncome": op_income,
            "NetIncome": net_income,
        }
    )


def _hist_bs(has_inv: bool) -> pd.DataFrame:
    """Synthetic balance sheet that satisfies the accounting identity at t-1.

    Carefully constructed so TotalAssets = TotalLiabilities + TotalEquity at the
    final historical quarter (which is what the forecaster initialises from).
    """
    cash = 5_000_000_000
    ar = 600_000_000  # ~DSO 23 days at $2.34B/qtr
    inv = 200_000_000 if has_inv else 0
    other_assets = 4_000_000_000  # PP&E + intangibles + other (held flat)
    total_assets = cash + ar + inv + other_assets

    ap = 150_000_000
    deferred_rev = 1_500_000_000
    other_liab = 2_000_000_000  # debt + long-term lease etc (held flat)
    total_liab = ap + deferred_rev + other_liab
    total_equity = total_assets - total_liab  # forces identity to balance

    return pd.DataFrame(
        [
            {
                "Cash": cash,
                "AccountsReceivable": ar,
                "Inventory": inv,
                "TotalAssets": total_assets,
                "AccountsPayable": ap,
                "DeferredRevenue": deferred_rev,
                "TotalLiabilities": total_liab,
                "TotalEquity": total_equity,
            }
        ]
    )


def _hist_cf() -> pd.DataFrame:
    """Synthetic CF: depreciation, SBC, buybacks, capex held simple."""
    return pd.DataFrame(
        [
            {
                "Depreciation": 50_000_000,
                "StockBasedCompensation": 200_000_000,
                "TreasuryStockRepurchases": 100_000_000,
                "CapEx": 60_000_000,
            }
        ]
    )


# ── Tests ─────────────────────────────────────────────────────────────────────

_BC_TOLERANCE = 1_000_000.0  # $1M, matches the threshold asserted in build()


@pytest.mark.parametrize("has_inv", [True, False], ids=["with_inventory", "no_inventory"])
def test_balance_check_holds_across_all_forecast_quarters(has_inv: bool) -> None:
    """For every forecast quarter × scenario, |BalanceCheck| < $1M."""
    hist_is, hist_bs, hist_cf = _hist_is(), _hist_bs(has_inv), _hist_cf()
    base = _compute_base_assumptions(hist_is, hist_bs, hist_cf, has_inv)
    scenarios = _make_scenarios(base)

    failures: list[tuple[str, int, float]] = []
    for scenario_name, assumptions in scenarios.items():
        periods = _forecast_periods(hist_is, hist_bs, hist_cf, assumptions, has_inv)
        for q, p in enumerate(periods, start=1):
            bc = p["BalanceCheck"]
            if abs(bc) > _BC_TOLERANCE:
                failures.append((scenario_name, q, bc))

    assert not failures, (
        f"BalanceCheck violated tolerance ${_BC_TOLERANCE:,.0f} in "
        f"{len(failures)} period(s): {failures[:5]}"
    )


def test_balance_check_recomputed_from_components() -> None:
    """Sanity: re-derive BalanceCheck from TotalAssets/Liab/Equity columns and confirm match.

    This is the v7 verification approach: re-compute in Python against the same
    arrays rather than reading back from openpyxl data_only=True (which would
    return None on a fresh workbook — the v6 silent-failure mode).
    """
    has_inv = True
    hist_is, hist_bs, hist_cf = _hist_is(), _hist_bs(has_inv), _hist_cf()
    base = _compute_base_assumptions(hist_is, hist_bs, hist_cf, has_inv)
    periods = _forecast_periods(hist_is, hist_bs, hist_cf, base, has_inv)

    for q, p in enumerate(periods, start=1):
        recomputed = p["TotalAssets"] - p["TotalLiabilities"] - p["TotalEquity"]
        assert abs(recomputed - p["BalanceCheck"]) < 0.01, (
            f"q{q}: stored BalanceCheck {p['BalanceCheck']:.4f} differs from "
            f"recomputed {recomputed:.4f}"
        )


def test_cash_is_the_balancing_item() -> None:
    """Cash_t = Cash_{t-1} + OCF + InvestingCF + FinancingCF (verbatim from docs §6)."""
    has_inv = False
    hist_is, hist_bs, hist_cf = _hist_is(), _hist_bs(has_inv), _hist_cf()
    base = _compute_base_assumptions(hist_is, hist_bs, hist_cf, has_inv)
    periods = _forecast_periods(hist_is, hist_bs, hist_cf, base, has_inv)

    for q, p in enumerate(periods, start=1):
        expected_end = p["BegCash"] + p["OCF"] + p["InvestingCF"] + p["FinancingCF"]
        assert (
            abs(p["EndCash"] - expected_end) < 0.01
        ), f"q{q}: EndCash {p['EndCash']:.2f} != BegCash + ΔCF {expected_end:.2f}"
