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


def test_default_posterior_unlocks_the_high_probability_gate():
    """Documents that with threshold 0.40, the static default record (63/138 approx 0.4565) unlocks the soft reserve."""
    eng = RiskEngine()
    posterior = eng.posterior_win_rate()
    assert posterior == pytest.approx(63 / 138)
    assert posterior >= float(LiquidityPolicy().risk_gate.high_prob_posterior_min)
    assert eng.posterior_win_rate(wins=80, executions=100) > 0.40


# ---------------------------------------------------------------------------
# FINRA Rule 4210 Intraday Margin Framework Tests (#80)
# ---------------------------------------------------------------------------

def test_margin_eligibility_and_sub_2k_equity_lock():
    """
    FINRA Rule 4210: Account must maintain a minimum of $2,000 equity for margin eligibility.
    Below $2,000, margin borrowing is blocked and buying power is strictly 1x cash.
    """
    # 1. Sub-$2,000 account ($1,500 equity)
    eng_sub = RiskEngine(account_base=1500.0)
    assert eng_sub.is_margin_eligible is False
    assert eng_sub.account_equity == 1500.0
    assert eng_sub.intraday_buying_power == 1500.0  # 1x leverage only
    assert eng_sub.overnight_buying_power == 1500.0

    # Pre-trade check blocks orders requiring margin leverage
    ok, reason = eng_sub.validate_pre_trade_margin("SOXL", 50, 40.0)  # $2,000 > $1,500
    assert ok is False
    assert "Margin ineligible" in reason and "$2,000.00 minimum" in reason

    # Cash-covered order passes
    ok_cash, _ = eng_sub.validate_pre_trade_margin("SOXL", 25, 40.0)  # $1,000 <= $1,500
    assert ok_cash is True

    # 2. Qualified account ($10,000 equity)
    eng_qual = RiskEngine(account_base=10000.0)
    assert eng_qual.is_margin_eligible is True
    # 4x Intraday Buying Power = $40,000
    assert eng_qual.intraday_buying_power == 40000.0
    # 2x Reg T Overnight Buying Power = $20,000
    assert eng_qual.overnight_buying_power == 20000.0


def test_dual_buying_power_and_realtime_tick_pipeline():
    """
    Verifies dynamic real-time recalculation of Intraday Margin Buying Power
    (25% req / 4x leverage) and Reg T Overnight Buying Power (50% req / 2x leverage)
    as active tick data moves open position valuations.
    """
    eng = RiskEngine(account_base=10000.0)
    # Open 100 shares of SOXL at $50 ($5,000 position value)
    eng.update_position("SOXL", quantity=100, current_price=50.0, entry_price=50.0)

    assert eng.total_position_value == 5000.0
    # Maintenance requirement = $5,000 * 0.25 = $1,250
    # Margin excess = $10,000 - $1,250 = $8,750
    # Intraday BP = $8,750 * 4 = $35,000
    assert eng.intraday_buying_power == 35000.0
    # Reg T Overnight BP = ($10,000 - $5,000 * 0.50) * 2 = $7,500 * 2 = $15,000
    assert eng.overnight_buying_power == 15000.0

    # Real-time tick update: SOXL surges to $60 (+ $1,000 unrealized gain)
    eng.update_tick("SOXL", 60.0)
    assert eng.positions["SOXL"]["current_price"] == 60.0
    assert eng.total_position_value == 6000.0
    assert eng.account_equity == 11000.0  # $10,000 + $1,000
    # New maintenance req = $6,000 * 0.25 = $1,500
    # Margin excess = $11,000 - $1,500 = $9,500
    # Intraday BP = $9,500 * 4 = $38,000
    assert eng.intraday_buying_power == 38000.0
    # Overnight BP = ($11,000 - $6,000 * 0.50) * 2 = $8,000 * 2 = $16,000
    assert eng.overnight_buying_power == 16000.0


def test_pre_trade_blocks_intraday_margin_deficit():
    """
    Pre-trade execution checks must actively block any order that would create
    or exacerbate an intraday margin deficit.
    """
    eng = RiskEngine(account_base=5000.0)
    # Intraday buying power = $20,000 (4x)
    assert eng.intraday_buying_power == 20000.0

    # Order exceeding intraday buying power is blocked
    ok, msg = eng.validate_pre_trade_margin("TQQQ", 500, 50.0)  # $25,000 > $20,000
    assert ok is False
    assert "exceeds real-time Intraday Margin Buying Power" in msg

    # Order within intraday buying power is allowed
    ok_valid, msg_valid = eng.validate_pre_trade_margin("TQQQ", 300, 50.0)  # $15,000 <= $20,000
    assert ok_valid is True
    assert msg_valid == "ok"

    # Simulate existing deficit condition: position value exceeds 4x equity
    eng.positions["TQQQ"] = {"quantity": 500, "current_price": 50.0, "entry_price": 50.0}
    eng.account_equity = 4000.0  # Req = 25,000 * 0.25 = 6,250 > 4,000 -> Deficit = $2,250
    eng.recalculate_margin_buying_power()
    assert eng.intraday_margin_deficit == 2250.0
    assert eng.intraday_buying_power == 0.0

    # Any new buy order while in deficit must be blocked to prevent exacerbation
    ok_def, msg_def = eng.validate_pre_trade_margin("SOXL", 1, 20.0)
    assert ok_def is False
    assert "intraday margin deficit" in msg_def


def test_instantaneous_capital_reallocation_on_governor_normal_regime():
    """
    Ensure the RiskEngine permits instantaneous capital reallocation back into equities
    the moment governor.py volatility trigger signals a 'Normal' regime, without applying
    a 24-hour settlement waiting period.
    """
    eng = RiskEngine(account_base=10000.0)

    # 1. High-volatility defensive signal from governor throttles sizing
    eng.update_governor_regime(regime="DEFENSIVE", volatility_multiplier=2.0)
    assert eng.regime == "DEFENSIVE"
    assert eng.macro_risk_multiplier == 0.50

    # 2. Volatility trigger normalizes: governor signals 'Normal' (vol_mult=1.0)
    eng.update_governor_regime(regime="NORMAL", volatility_multiplier=1.0)
    assert eng.regime == "NORMAL"
    assert eng.volatility_multiplier == 1.0
    assert eng.macro_risk_multiplier == 1.0
    assert eng.instantaneous_reallocation_permitted is True

    # Immediate capital allocation permitted up to full 4x intraday margin ($40,000)
    assert eng.intraday_buying_power == 40000.0
    ok, _ = eng.validate_pre_trade_margin("SOXL", 200, 50.0)  # $10,000 entry
    assert ok is True


def test_eod_overnight_liquidation_sequence_prior_to_8pm():
    """
    Automated end-of-day sequence scheduled prior to 8:00 p.m. ET cutoff:
    - Calculates delta between total open position value and allowable Reg T overnight limit (2x equity).
    - Triggers alerts and executes automated liquidation of excess leveraged positions
      down to standard overnight limits to prevent margin calls.
    """
    eng = RiskEngine(account_base=10000.0)
    # Reg T Overnight Limit = 2x $10,000 = $20,000
    # Open positions totaling $28,000 (leveraged via intraday margin up to 4x):
    # SOXL: 300 shares @ $60 = $18,000
    # TQQQ: 200 shares @ $50 = $10,000
    eng.update_position("SOXL", 300, 60.0)
    eng.update_position("TQQQ", 200, 50.0)
    assert eng.total_position_value == 28000.0

    delta, total_val, allowable_limit = eng.calculate_overnight_excess()
    assert total_val == 28000.0
    assert allowable_limit == 20000.0
    # Delta = $28,000 - $20,000 = $8,000 excess
    assert delta == 8000.0

    # Plan liquidation: sorts positions by value descending (SOXL is $18k, TQQQ is $10k)
    # Liquidating $8,000 from SOXL: ceil(8000 / 60) = 134 shares ($8,040 reduction)
    planned_orders = eng.plan_eod_overnight_liquidation()
    assert len(planned_orders) >= 1
    assert planned_orders[0]["symbol"] == "SOXL"
    assert planned_orders[0]["shares_to_sell"] == 134
    assert planned_orders[0]["notional_reduction"] == 134 * 60.0

    # Execute automated liquidation
    res = eng.execute_eod_overnight_liquidation()
    assert res["alert_triggered"] is True
    assert res["delta"] == 8000.0
    assert len(res["orders_executed"]) >= 1

    # After liquidation: total position value is reduced within the $20,000 limit
    new_delta, new_total, _ = eng.calculate_overnight_excess()
    assert new_delta == 0.0
    assert new_total <= allowable_limit
