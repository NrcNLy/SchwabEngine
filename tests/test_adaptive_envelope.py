"""
tests/test_adaptive_envelope.py
===============================
Unit tests for execution/adaptive_envelope.py (Adaptive Volatility Envelope Executor).

Tests:
1. Quadratic squeeze factor: lambda(tau) = 1.0 - tau^2
2. Yang-Zhang variance bar ingestion and volatility updates
3. Dynamic take-profit decay and upper breach modes
4. Dynamic trailing stop ratcheting with supremum tracking
5. Asymmetric breach detection (bid vs last)
6. Serialization and deserialization roundtrip
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import pytest

from execution.adaptive_envelope import AdaptiveEnvelopeExecutor, BreachType
from indicators.volatility import OHLCBar


@pytest.fixture
def session_times():
    t_open = datetime(2026, 10, 16, 9, 30, 0, tzinfo=timezone.utc)
    t_close = datetime(2026, 10, 16, 16, 0, 0, tzinfo=timezone.utc)
    return t_open, t_close


def test_quadratic_squeeze_factor(session_times):
    t_open, t_close = session_times
    env = AdaptiveEnvelopeExecutor(
        symbol="SOXL",
        entry_price=100.0,
        entry_time=t_open,
        session_open=t_open,
        session_close=t_close,
        gamma=2.0,  # Quadratic
    )

    # tau = 0.0 -> lambda = 1.0
    assert env.compute_tau(t_open) == pytest.approx(0.0)
    assert env.compute_squeeze_factor(t_open) == pytest.approx(1.0)

    # tau = 0.5 (midday) -> lambda = 1 - 0.5^2 = 0.75
    total_sec = (t_close - t_open).total_seconds()
    t_mid = t_open + timedelta(seconds=total_sec * 0.5)
    assert env.compute_tau(t_mid) == pytest.approx(0.5)
    assert env.compute_squeeze_factor(t_mid) == pytest.approx(0.75)

    # tau = 1.0 -> lambda = 0.0
    assert env.compute_tau(t_close) == pytest.approx(1.0)
    assert env.compute_squeeze_factor(t_close) == pytest.approx(0.0)


def test_yang_zhang_bar_ingestion(session_times):
    t_open, t_close = session_times
    env = AdaptiveEnvelopeExecutor(
        symbol="SOXL",
        entry_price=100.0,
        entry_time=t_open,
        session_open=t_open,
        session_close=t_close,
        baseline_sigma=0.015,
    )
    assert env.current_sigma_yz == 0.015

    # Ingest OHLC bars
    bars = [
        OHLCBar(open=100.0, high=102.0, low=99.5, close=101.5),
        OHLCBar(open=101.5, high=103.0, low=101.0, close=102.5),
        OHLCBar(open=102.5, high=104.0, low=102.0, close=103.5),
        OHLCBar(open=103.5, high=104.5, low=102.5, close=103.0),
        OHLCBar(open=103.0, high=105.0, low=102.8, close=104.5),
    ]
    for b in bars:
        vol = env.add_bar(b)
        assert vol > 0.0

    assert env.current_sigma_yz > 0.0


def test_take_profit_decay_and_tightening_mode(session_times):
    t_open, t_close = session_times
    env = AdaptiveEnvelopeExecutor(
        symbol="SOXL",
        entry_price=100.0,
        entry_time=t_open,
        session_open=t_open,
        session_close=t_close,
        k_u=1.5,
        k_l=1.5,
        upper_breach_mode="tighten",
        baseline_sigma=0.02,
    )

    t1 = t_open + timedelta(minutes=15)
    state1 = env.evaluate_tick(t1, current_price=101.0, bid_price=100.95)
    assert state1.breach == BreachType.NONE
    assert not env.is_tightened

    # Touch upper bound (take-profit target)
    state2 = env.evaluate_tick(t1 + timedelta(seconds=1), current_price=108.0, bid_price=107.95)
    # In tightening mode, upper touch tightens k_l without immediate hard exit
    assert env.is_tightened is True
    assert env.k_l == pytest.approx(0.75, abs=1e-4)


def test_asymmetric_breach_and_trailing_stop(session_times):
    t_open, t_close = session_times
    env = AdaptiveEnvelopeExecutor(
        symbol="SOXL",
        entry_price=100.0,
        entry_time=t_open,
        session_open=t_open,
        session_close=t_close,
        k_u=1.5,
        k_l=1.5,
        baseline_sigma=0.02,
    )

    # Ratchet supremum to 105.0
    t1 = t_open + timedelta(minutes=30)
    env.evaluate_tick(t1, current_price=105.0, bid_price=104.95)
    assert env.supremum == 105.0

    # Last trade is at 104.0, but bid drops below lower bound
    # Lower bound at t1: 105 * (1 - 1.5 * 0.02 * lam)
    # With quadratic squeeze at t1 (~0.077 elapsed): lam ~ 0.994
    # lower_bound ~ 105 * (1 - 0.0298) ~ 101.87
    state_drop = env.evaluate_tick(t1 + timedelta(seconds=5), current_price=103.0, bid_price=101.50)
    assert state_drop.breach == BreachType.LOWER_TRAILING_STOP


def test_serialization_roundtrip(session_times):
    t_open, t_close = session_times
    env = AdaptiveEnvelopeExecutor(
        symbol="SOXL",
        entry_price=100.0,
        entry_time=t_open,
        session_open=t_open,
        session_close=t_close,
        k_u=1.8,
        k_l=1.2,
        gamma=2.0,
    )
    env.evaluate_tick(t_open + timedelta(minutes=20), current_price=103.50, bid_price=103.45)
    env.apply_tightening()

    data = env.to_dict()
    assert data["symbol"] == "SOXL"
    assert data["supremum"] == 103.50
    assert data["is_tightened"] is True

    restored = AdaptiveEnvelopeExecutor.from_dict(data)
    assert restored.symbol == "SOXL"
    assert restored.supremum == 103.50
    assert restored.is_tightened is True
    assert restored.k_l == env.k_l
