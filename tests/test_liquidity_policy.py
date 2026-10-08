from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from core.liquidity_policy import (
    InflowSchedule,
    LiquidityPolicy,
    add_business_days,
    business_days_between,
    compute_buying_power,
    effective_inflow,
    load_policy,
    next_inflow_date,
    save_policy,
)

D = Decimal
MONDAY = date(2026, 10, 5)


def bp(**kw):
    base = dict(
        nlv=D("1000"), settled_cash=D("1000"), unsettled_cash=D("0"),
        policy=LiquidityPolicy(), today=MONDAY,
    )
    base.update(kw)
    return compute_buying_power(**base)


def test_defaults_match_user_decisions():
    p = LiquidityPolicy()
    assert p.inflow.enabled and p.inflow.amount == D("250.00")
    assert p.inflow.weekday == "FRIDAY" and p.inflow.lag_business_days == 3
    assert p.inflow.confidence == D("0.80")
    assert p.sweep.mode == "ADVISORY" and p.sweep.symbol == "SWVXX"
    assert p.risk_gate.high_prob_posterior_min == D("0.40") and p.risk_gate.high_prob_regime == "A"
    assert p.risk_gate.single_ticker_cap_pct == D("0.33")


def test_business_day_math_skips_weekends_and_holidays():
    friday = date(2026, 10, 2)
    assert add_business_days(friday, 1) == date(2026, 10, 5)
    assert add_business_days(date(2026, 10, 9), 3) == date(2026, 10, 14)
    assert business_days_between(friday, date(2026, 10, 5)) == 1
    # Thanksgiving 2026-11-26 is a market holiday
    assert add_business_days(date(2026, 11, 25), 1) == date(2026, 11, 27)


def test_next_inflow_date_and_haircut():
    assert next_inflow_date(MONDAY, "FRIDAY") == date(2026, 10, 9)
    sched = InflowSchedule()
    assert effective_inflow(sched, MONDAY) == D("200.00")  # 250 * 0.80
    assert effective_inflow(InflowSchedule(enabled=False), MONDAY) == D("0.00")


def test_inflow_validation_rejects_weekend_and_bad_confidence():
    with pytest.raises(ValidationError):
        InflowSchedule(weekday="SATURDAY")
    with pytest.raises(ValidationError):
        InflowSchedule(confidence=D("1.5"))
    with pytest.raises(ValidationError):
        InflowSchedule(amount=D("-1"))


@pytest.mark.parametrize("nlv,expected", [("3747.50", "1236.68"), ("1000", "330.00"), ("5000", "1650.00")])
def test_single_ticker_cap_is_exactly_33pct_of_nlv(nlv, expected):
    out = bp(nlv=D(nlv), settled_cash=D(nlv))
    assert out.single_ticker_cap == D(expected)
    assert out.max_order_notional <= D(expected)


def test_buying_power_never_exceeds_settled_cash():
    for settled in ("0", "5", "40", "250", "999.99"):
        out = bp(settled_cash=D(settled), swvxx_settled=D("9999"), pending_ach=D("9999"),
                 external_backstop=D("9999"), unsettled_cash=D("9999"),
                 regime="A", posterior=0.9)
        assert out.tactical_float <= D(settled)
        assert out.max_order_notional <= max(D(settled) - D("10"), D("0"))


def test_unsettled_swvxx_ach_and_backstop_do_not_change_buying_power():
    clean = bp()
    loaded = bp(unsettled_cash=D("5000"), swvxx_settled=D("5000"), swvxx_in_flight=D("5000"),
                pending_ach=D("5000"), external_backstop=D("5000"))
    assert loaded.max_order_notional == clean.max_order_notional
    assert loaded.tactical_float == clean.tactical_float
    assert loaded.hard_reserve == D("10000.00")  # unsettled + SWVXX in flight, locked


@pytest.mark.parametrize(
    "regime,posterior,expected",
    [("A", 0.60, True), ("A", 0.40, True), ("A", 0.399, False), ("B", 0.95, False), ("C", 0.95, False),
     (None, 0.95, False), ("A", None, False)],
)
def test_soft_reserve_gate(regime, posterior, expected):
    out = bp(regime=regime, posterior=posterior)
    assert out.high_probability is expected
    if expected:
        assert out.tactical_float == out.base_float + out.soft_draw_available
    else:
        assert out.tactical_float == out.base_float


def test_inflow_only_lowers_target_never_adds_cash():
    on = bp()
    off = bp(policy=LiquidityPolicy(inflow=InflowSchedule(enabled=False)))
    assert on.soft_reserve_target_effective < off.soft_reserve_target_effective
    assert on.inflow_offset <= on.soft_reserve_target * LiquidityPolicy().inflow.offset_cap_fraction
    # anticipated cash is not part of settled cash, so it cannot raise buying power beyond settled
    assert on.tactical_float <= on.settled_cash


def test_sweep_is_advisory_and_never_routed():
    for mode in ("OFF", "ADVISORY"):
        pol = LiquidityPolicy()
        pol.sweep.mode = mode
        for settled, swvxx in (("1000", "0"), ("100", "800"), ("40", "0")):
            out = bp(policy=pol, settled_cash=D(settled), swvxx_settled=D(swvxx))
            assert out.sweep.routed is False
    out = bp(settled_cash=D("1000"))
    assert out.sweep.action == "SWEEP_IN" and out.sweep.settles_on == date(2026, 10, 6)
    redeem = bp(settled_cash=D("100"), swvxx_settled=D("800"))
    assert redeem.sweep.action == "REDEEM_FOR_NEXT_SESSION"
    assert redeem.sweep.place_by == MONDAY and redeem.sweep.settles_on == date(2026, 10, 6)


def test_policy_persistence_roundtrip_and_invalid_file_fallback(tmp_path):
    path = tmp_path / "policy.json"
    assert load_policy(path) == LiquidityPolicy()
    pol = LiquidityPolicy()
    pol.inflow.amount = D("400.00")
    pol.inflow.weekday = "THURSDAY"
    save_policy(pol, path)
    again = load_policy(path)
    assert again.inflow.amount == D("400.00") and again.inflow.weekday == "THURSDAY"
    path.write_text('{"inflow": {"weekday": "SUNDAY"}}', encoding="utf-8")
    assert load_policy(path) == LiquidityPolicy()
