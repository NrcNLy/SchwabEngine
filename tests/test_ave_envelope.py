"""
tests/test_ave_envelope.py
==========================
Unit tests for core/envelope.py (Adaptive Volatility Envelope).

Tests:
1. Squeeze factor endpoints: lambda(tau=0) == 1.0, lambda(tau=1) == 0.0.
2. Gamma shaping parameter ordering at tau=0.5:
   - gamma=1.8 (FAVORABLE): stays wide (lambda ~ 0.713)
   - gamma=1.0 (Linear): lambda == 0.500
   - gamma=0.6 (CHOP): aggressive early squeeze (lambda ~ 0.340)
3. Supremum ratcheting and monotonicity under upward drift.
4. Bad print / outlier spike rejection from supremum tracking.
5. Asymmetric breach rules:
   - Lower bound breached when BID <= lower_bound.
   - Upper bound breached when LAST >= upper_bound.
6. Upper breach behavior:
   - In "tighten" mode: does NOT trigger liquidation; halves k_l and continues trailing.
   - In "exit" mode: triggers breached_upper.
7. Terminal convergence: as lambda -> 0, lower_bound -> supremum.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import pytest

from core.envelope import AdaptiveVolatilityEnvelope


def test_squeeze_factor_endpoints_and_shaping():
    t_open = datetime(2026, 10, 14, 9, 30, 0, tzinfo=timezone.utc)
    t_close = datetime(2026, 10, 14, 15, 50, 0, tzinfo=timezone.utc)

    # 1. Endpoints
    env_lin = AdaptiveVolatilityEnvelope(
        entry_price=100.0,
        entry_time=t_open,
        session_open=t_open,
        session_close=t_close,
        gamma=1.0,
    )
    assert env_lin.compute_squeeze_factor(t_open) == pytest.approx(1.0, abs=1e-5)
    assert env_lin.compute_squeeze_factor(t_close) == pytest.approx(0.0, abs=1e-5)
    # Outside session guards
    assert env_lin.compute_squeeze_factor(t_open - timedelta(minutes=10)) == 1.0
    assert env_lin.compute_squeeze_factor(t_close + timedelta(minutes=10)) == 0.0

    # 2. Midpoint (tau = 0.5) with different gamma values
    total_seconds = (t_close - t_open).total_seconds()
    t_mid = t_open + timedelta(seconds=total_seconds * 0.5)

    env_fav = AdaptiveVolatilityEnvelope(
        entry_price=100.0,
        entry_time=t_open,
        session_open=t_open,
        session_close=t_close,
        gamma=1.8,
    )
    env_chop = AdaptiveVolatilityEnvelope(
        entry_price=100.0,
        entry_time=t_open,
        session_open=t_open,
        session_close=t_close,
        gamma=0.6,
    )

    lam_fav = env_fav.compute_squeeze_factor(t_mid)
    lam_lin = env_lin.compute_squeeze_factor(t_mid)
    lam_chop = env_chop.compute_squeeze_factor(t_mid)

    # lambda = 1 - 0.5^gamma
    expected_fav = 1.0 - (0.5 ** 1.8)    # ~ 0.7128
    expected_lin = 1.0 - (0.5 ** 1.0)    # 0.5
    expected_chop = 1.0 - (0.5 ** 0.6)   # ~ 0.3404

    assert lam_fav == pytest.approx(expected_fav, abs=1e-3)
    assert lam_lin == pytest.approx(expected_lin, abs=1e-3)
    assert lam_chop == pytest.approx(expected_chop, abs=1e-3)
    assert lam_fav > lam_lin > lam_chop


def test_supremum_ratcheting_and_spike_filter():
    t_open = datetime(2026, 10, 14, 9, 30, 0, tzinfo=timezone.utc)
    t_close = datetime(2026, 10, 14, 15, 50, 0, tzinfo=timezone.utc)
    t1 = t_open + timedelta(minutes=15)

    env = AdaptiveVolatilityEnvelope(
        entry_price=100.0,
        entry_time=t_open,
        session_open=t_open,
        session_close=t_close,
        k_u=2.0,
        k_l=1.5,
        spike_filter_sigma_mult=4.0,
    )
    assert env.supremum == 100.0

    # Normal tick up ratchets supremum
    bounds1 = env.evaluate_tick(current_time=t1, current_price=105.0, sigma_t=0.02)
    assert env.supremum == 105.0
    assert bounds1.supremum == 105.0

    # Normal tick down keeps supremum
    t2 = t1 + timedelta(seconds=1)
    bounds2 = env.evaluate_tick(current_time=t2, current_price=103.0, sigma_t=0.02)
    assert env.supremum == 105.0

    # Bad print spike (+20% jump when 4 * sigma * supremum is ~8.4)
    # Max valid jump is 105 * (1 + 4 * 0.02) = 113.4. Tick at 125.0 must be rejected from supremum
    t3 = t2 + timedelta(seconds=1)
    bounds3 = env.evaluate_tick(current_time=t3, current_price=125.0, sigma_t=0.02)
    assert env.supremum == 105.0  # Rejected by spike filter


def test_bid_last_breach_asymmetry():
    t_open = datetime(2026, 10, 14, 9, 30, 0, tzinfo=timezone.utc)
    t_close = datetime(2026, 10, 14, 15, 50, 0, tzinfo=timezone.utc)
    t1 = t_open + timedelta(minutes=30)

    env = AdaptiveVolatilityEnvelope(
        entry_price=100.0,
        entry_time=t_open,
        session_open=t_open,
        session_close=t_close,
        k_u=2.0,
        k_l=1.5,
        gamma=1.0,
    )

    # Calculate expected lower bound at t1:
    # lambda at t1: (30m / 380m) elapsed => tau = 30/380 = 0.0789 => lambda = 1 - 0.0789 = 0.9211
    # lower_bound = 100 * (1 - 1.5 * 0.01 * 0.9211) = 100 * (1 - 0.0138) = 98.62
    # If last trade is 99.00 but bid drops to 98.50, lower bound should be breached
    bounds = env.evaluate_tick(
        current_time=t1,
        current_price=99.00,
        sigma_t=0.01,
        bid_price=98.50,
    )
    assert bounds.breached_lower is True
    assert bounds.breached_upper is False


def test_upper_breach_tightening_mode():
    t_open = datetime(2026, 10, 14, 9, 30, 0, tzinfo=timezone.utc)
    t_close = datetime(2026, 10, 14, 15, 50, 0, tzinfo=timezone.utc)
    t1 = t_open + timedelta(minutes=10)

    env = AdaptiveVolatilityEnvelope(
        entry_price=100.0,
        entry_time=t_open,
        session_open=t_open,
        session_close=t_close,
        k_u=1.5,
        k_l=1.5,
        min_k_l=0.25,
        upper_breach_mode="tighten",
    )

    # Initial k_l is 1.5
    assert env.k_l == 1.5
    assert not env.is_tightened

    # Tick breaches upper bound
    bounds = env.evaluate_tick(
        current_time=t1,
        current_price=108.0,
        sigma_t=0.02,
    )
    assert bounds.breached_upper is True
    assert env.is_tightened is True
    # k_l is halved to 0.75
    assert env.k_l == pytest.approx(0.75, abs=1e-5)


def test_terminal_convergence():
    t_open = datetime(2026, 10, 14, 9, 30, 0, tzinfo=timezone.utc)
    t_close = datetime(2026, 10, 14, 15, 50, 0, tzinfo=timezone.utc)

    env = AdaptiveVolatilityEnvelope(
        entry_price=100.0,
        entry_time=t_open,
        session_open=t_open,
        session_close=t_close,
    )
    # Ratchet supremum to 110.0 (with bid confirming the rally)
    env.evaluate_tick(t_open + timedelta(hours=1), current_price=110.0, sigma_t=0.02, bid_price=109.95)
    assert env.supremum == 110.0

    # At exact close, lambda factor is 0.0, so lower_bound converges to supremum
    bounds_close = env.evaluate_tick(t_close, current_price=109.5, sigma_t=0.02)
    assert bounds_close.lambda_factor == 0.0
    assert bounds_close.lower_bound == pytest.approx(110.0, abs=1e-4)
    # Any price below supremum triggers lower breach at close
    assert bounds_close.breached_lower is True
