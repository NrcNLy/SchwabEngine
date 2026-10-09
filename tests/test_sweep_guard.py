from __future__ import annotations

from datetime import date
from decimal import Decimal
import pytest

from core.liquidity_policy import (
    LiquidityPolicy,
    SweepPolicy,
    _advise_sweep,
    compute_buying_power,
)

D = Decimal
MONDAY = date(2026, 10, 5)


def test_sub_25k_account_sweep_disabled():
    """
    Verifies that cash accounts below $25,000 (such as the ~$3,770 account)
    never generate a SWEEP_IN recommendation, preserving 100% operational float.
    """
    pol = LiquidityPolicy()
    nlv = D("3770.00")
    settled = D("3776.00")

    adv = _advise_sweep(
        policy=pol,
        nlv=nlv,
        settled=settled,
        target_eff=D("100.00"),
        swvxx_settled=D("0.00"),
        today=MONDAY,
    )

    assert adv.action == "NONE"
    assert "below the $25000.00 threshold" in adv.rationale
    assert "overnight money market sweep disabled" in adv.rationale


def test_operational_float_floor_choke_prevention():
    """
    Verifies that even if an account has >$25k equity, if settled cash is below
    the $3,500 operational float floor, sweep is blocked to protect morning sizing.
    """
    pol = LiquidityPolicy()
    nlv = D("26000.00")
    settled = D("3200.00")  # below $3,500 floor

    adv = _advise_sweep(
        policy=pol,
        nlv=nlv,
        settled=settled,
        target_eff=D("100.00"),
        swvxx_settled=D("0.00"),
        today=MONDAY,
    )

    assert adv.action == "NONE"
    assert "below the $3500.00 operational float floor" in adv.rationale


def test_qualified_account_sweep_evaluation():
    """
    Verifies that a high-equity account with surplus cash beyond the float floor
    and soft reserve can generate a SWEEP_IN advisory.
    """
    pol = LiquidityPolicy()
    nlv = D("50000.00")
    settled = D("35000.00")

    adv = _advise_sweep(
        policy=pol,
        nlv=nlv,
        settled=settled,
        target_eff=D("100.00"),
        swvxx_settled=D("0.00"),
        today=MONDAY,
    )

    # float_floor = max(50000 * 0.40, 3500) = 20000
    # keep = 20000 + 100 = 20100
    # idle = 35000 - 20100 = 14900
    assert adv.action == "SWEEP_IN"
    assert adv.amount == D("14900.00")
