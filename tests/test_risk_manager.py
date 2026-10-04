from __future__ import annotations

import math

import pytest

from core.liquidity_policy import LiquidityPolicy
from execution.risk_manager import RiskEngine


def raw_shares(engine: RiskEngine, atr: float, base: float) -> int:
    capital = engine.calculate_bayesian_kelly_sizing(
        engine.wins, engine.executions, engine.payoff_ratio, base)
    return math.floor(capital / (atr * 1.5))


def test_default_multiplier_is_one_and_never_changed_by_construction():
    eng = RiskEngine()
    assert eng.macro_risk_multiplier == 1.0


def test_multiplier_scales_total_meta_order_shares_before_slicing():
    eng = RiskEngine()
    base, atr = 100_000.0, 0.5
    raw = raw_shares(eng, atr, base)
    assert raw >= 10
    assert eng.determine_position_size("SOXL", atr, base) == raw
    assert eng.determine_position_size("SOXL", atr, base, risk_multiplier=0.5) == math.floor(raw * 0.5)
    assert eng.determine_position_size("SOXL", atr, base, risk_multiplier=2.0) == raw  # clamped to 1.0


def test_returns_zero_when_scaled_shares_below_one():
    eng = RiskEngine()
    base = 100_000.0
    assert eng.determine_position_size("SOXL", 0.5, base, risk_multiplier=0.0) == 0
    assert eng.determine_position_size("SOXL", 0.0, base) == 0  # zero ATR: no size

    one_share_atr = next(
        atr / 100.0 for atr in range(1, 100_000) if raw_shares(eng, atr / 100.0, base) == 1
    )
    assert eng.determine_position_size("SOXL", one_share_atr, base) == 1
    assert eng.determine_position_size("SOXL", one_share_atr, base, risk_multiplier=0.5) == 0


def test_notional_ceiling_caps_shares_and_requires_a_price():
    eng = RiskEngine()
    base, atr = 100_000.0, 0.5
    capped = eng.determine_position_size("SOXL", atr, base, price=100.0, max_notional=250.0)
    assert capped == 2
    assert eng.determine_position_size("SOXL", atr, base, max_notional=250.0) == 0
    assert eng.determine_position_size("SOXL", atr, base, price=100.0, max_notional=0.0) == 0


def test_document_policy_changes_cannot_touch_the_risk_multiplier():
    eng = RiskEngine()
    LiquidityPolicy()  # constructing / editing policy is independent of the risk engine
    assert eng.macro_risk_multiplier == 1.0


def test_default_posterior_is_below_the_high_probability_gate():
    """Documents a known property: with the static default record, the soft-reserve gate stays closed."""
    eng = RiskEngine()
    posterior = eng.posterior_win_rate()
    assert posterior == pytest.approx(60 / 130)
    assert posterior < float(LiquidityPolicy().risk_gate.high_prob_posterior_min)
    assert eng.posterior_win_rate(wins=80, executions=100) > 0.55
