"""
tests/test_dynamic_stops.py
===========================
Unit tests for Phase 1 Quantitative Risk updates:
1. Corrected Intraday Yang-Zhang Volatility Time Scalar (intraday_fraction = 1/390, min floor 0.5%).
2. Hurst Exponent (R/S) Regime Filter (vectorized compute_hurst_rs).
3. Tri-state Stop Ratchet Regime Gating:
   - H <= 0.50 (mean-reverting chop): Freeze ratchet, maintain existing stop.
   - H > 0.55 (directional persistence): Permit ratchet upward with excursions.
   - 0.50 < H <= 0.55 (random walk): Retain previous ratchet state.
4. YangZhangEstimator streaming Hurst integration.
"""

from decimal import Decimal
import math
import numpy as np
import pytest

from core.ledger import ManagedPosition
from execution.risk_manager import RiskEngine
from indicators.volatility import (
    OHLCBar,
    YangZhangEstimator,
    compute_hurst_rs,
)

D = Decimal


# ---------------------------------------------------------------------------
# 1. Hurst Exponent (R/S) Calculation Tests
# ---------------------------------------------------------------------------

def test_compute_hurst_rs_mean_reverting():
    """
    Synthetic alternating / mean-reverting series must produce H <= 0.50.
    """
    np.random.seed(42)
    # Alternating returns represent microstructural consolidation / oscillation
    mr_series = np.tile([0.015, -0.015], 25) + np.random.normal(0, 0.001, 50)
    h_mr = compute_hurst_rs(mr_series)
    assert h_mr <= 0.50, f"Expected H <= 0.50 for mean-reverting series, got {h_mr:.4f}"


def test_compute_hurst_rs_trending():
    """
    Synthetic persistent trending series must produce H > 0.55.
    """
    np.random.seed(42)
    # Strongly persistent momentum drift
    trend_series = np.array([0.005 + 0.0005 * i for i in range(50)]) + np.random.normal(0, 0.0005, 50)
    h_trend = compute_hurst_rs(trend_series)
    assert h_trend > 0.55, f"Expected H > 0.55 for persistent trending series, got {h_trend:.4f}"


def test_compute_hurst_rs_edge_cases():
    """
    Handles insufficient data (<10 points) or zero variance gracefully with neutral 0.50.
    """
    assert compute_hurst_rs([]) == 0.50
    assert compute_hurst_rs([0.01, -0.01, 0.02]) == 0.50
    assert compute_hurst_rs(np.zeros(30)) == 0.50


# ---------------------------------------------------------------------------
# 2. Intraday Annualization Scalar & Mathematical Floor
# ---------------------------------------------------------------------------

def test_corrected_intraday_time_factor_and_floor():
    """
    Verifies that the intraday time factor correctly scales daily volatility
    using intraday_fraction = 1.0 / 390.0, and enforces the 0.5% mathematical floor.
    """
    risk = RiskEngine()
    hwm = 100.0
    intraday_fraction = 1.0 / 390.0

    # Expected time factor: sqrt((1/390) / 252)
    expected_time_factor = math.sqrt(intraday_fraction / 252.0)
    assert math.isclose(expected_time_factor, 0.00318995, rel_tol=1e-4)

    # Low volatility case: yz_vol = 0.02, k_stop = 2.0
    # Raw distance: 100 * 2.0 * 0.02 * 0.00319 = 0.01276
    # Floor: 100 * 0.005 = 0.50
    # Mathematical floor must clamp the buffer so it cannot collapse into the bid-ask spread
    dist = risk.calculate_yang_zhang_stop_distance(
        high_water_mark=hwm,
        yz_vol=0.02,
        k_stop=2.0,
        intraday_fraction=intraday_fraction,
        min_stop_distance_pct=0.005,
    )
    assert dist == 0.50, f"Expected floor of 0.50, got {dist}"

    # High volatility expansion case (e.g. 3x ETF extreme intraday spike): yz_vol = 0.85
    # Raw distance: 100 * 2.0 * 0.85 * 0.06299 (if daily scalar) vs intraday
    dist_high_vol = risk.calculate_yang_zhang_stop_distance(
        high_water_mark=hwm,
        yz_vol=0.02,
        k_stop=2.0,
        dt_days=1.0,  # legacy 1.0 day scalar
        min_stop_distance_pct=0.005,
    )
    # Legacy unfloored distance was 100 * 2 * 0.02 * sqrt(1/252) = 0.252 -> clamped to 0.50
    assert dist_high_vol >= 0.50


# ---------------------------------------------------------------------------
# 3. Tri-State Stop Ratchet Regime Gating Tests
# ---------------------------------------------------------------------------

def test_mean_reverting_regime_locks_stop_ratchet():
    """
    H <= 0.50 (mean-reverting chop): Freeze the trailing stop ratchet.
    Maintain existing stop level and prevent upward creep during microstructural consolidation.
    """
    risk = RiskEngine()
    pos = ManagedPosition(
        symbol="TNA",
        quantity=10,
        entry_price=D("100.00"),
        last_price=D("100.00"),
        stop_price=D("96.00"),
        simulated=False,
    )

    # Initial state
    assert pos.stop_price == D("96.00")
    assert pos.high_water_mark == D("100.00")

    # Price excursions to 105.00 during mean-reverting chop (H = 0.35 <= 0.50)
    new_stop, ratcheted = risk.evaluate_trailing_stop(
        pos,
        current_price=105.00,
        yz_vol=0.02,
        k_stop=2.0,
        hurst_exponent=0.35,
    )

    # Stop ratchet must be FROZEN
    assert ratcheted is False, "Ratchet should not have triggered during mean-reverting chop"
    assert pos.stop_price == D("96.00"), "Stop price must remain pinned at initial 96.00"
    assert pos.ratchet_permitted is False

    # Pullback to 98.00: position remains alive because stop did not creep up to 104.50!
    assert pos.stop_price < D("98.00")


def test_persistent_trending_regime_allows_stop_ratchet():
    """
    H > 0.55 (directional persistence): Permit the dynamic stop to ratchet upward with excursions.
    """
    risk = RiskEngine()
    pos = ManagedPosition(
        symbol="TQQQ",
        quantity=10,
        entry_price=D("100.00"),
        last_price=D("100.00"),
        stop_price=D("96.00"),
        simulated=False,
    )

    # Price excursions to 105.00 during persistent trend (H = 0.72 > 0.55)
    new_stop, ratcheted = risk.evaluate_trailing_stop(
        pos,
        current_price=105.00,
        yz_vol=0.02,
        k_stop=2.0,
        hurst_exponent=0.72,
    )

    # Stop ratchet must be PERMITTED
    assert ratcheted is True, "Ratchet should advance upward during persistent trending regime"
    assert pos.stop_price > D("96.00"), f"Stop price {pos.stop_price} should have ratcheted above 96.00"
    assert pos.ratchet_permitted is True
    assert pos.high_water_mark == D("105.00")


def test_random_walk_retains_prior_ratchet_state():
    """
    0.50 < H <= 0.55 (random walk): Retain previous ratchet state.
    """
    risk = RiskEngine()
    pos = ManagedPosition(
        symbol="SOXL",
        quantity=10,
        entry_price=D("100.00"),
        last_price=D("100.00"),
        stop_price=D("96.00"),
        simulated=False,
    )

    # Step 1: Mean-reverting regime freezes the ratchet
    risk.evaluate_trailing_stop(pos, 102.00, yz_vol=0.02, hurst_exponent=0.40)
    assert pos.ratchet_permitted is False
    assert pos.stop_price == D("96.00")

    # Step 2: Random walk regime (H = 0.52) retains the frozen state
    new_stop, ratcheted = risk.evaluate_trailing_stop(pos, 104.00, yz_vol=0.02, hurst_exponent=0.52)
    assert ratcheted is False
    assert pos.ratchet_permitted is False
    assert pos.stop_price == D("96.00")

    # Step 3: Directional breakout (H = 0.65) unfreezes and permits ratcheting
    new_stop2, ratcheted2 = risk.evaluate_trailing_stop(pos, 106.00, yz_vol=0.02, hurst_exponent=0.65)
    assert ratcheted2 is True
    assert pos.ratchet_permitted is True
    assert pos.stop_price > D("96.00")
    stop_level = pos.stop_price

    # Step 4: Random walk regime (H = 0.53) retains the permitted state
    new_stop3, ratcheted3 = risk.evaluate_trailing_stop(pos, 108.00, yz_vol=0.02, hurst_exponent=0.53)
    assert ratcheted3 is True
    assert pos.ratchet_permitted is True
    assert pos.stop_price > stop_level


# ---------------------------------------------------------------------------
# 4. YangZhangEstimator Streaming Hurst Integration
# ---------------------------------------------------------------------------

def test_yang_zhang_estimator_current_hurst():
    """
    Verifies that YangZhangEstimator accumulates bars and computes current_hurst().
    """
    est = YangZhangEstimator(window_size=30)
    assert est.current_hurst() is None  # < 10 bars

    # Add 15 bars of trending data
    for i in range(15):
        p = 100.0 + i * 0.5
        est.add_bar(open_p=p - 0.1, high_p=p + 0.3, low_p=p - 0.2, close_p=p)

    h_val = est.current_hurst()
    assert h_val is not None
    assert 0.0 <= h_val <= 1.0
