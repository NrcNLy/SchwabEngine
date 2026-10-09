from __future__ import annotations

from datetime import date
from decimal import Decimal
import pytest

from core.collateral_engine import CollateralEngine
from core.liquidity_models import CollateralInvariantState, PromotionalDebt


def test_macro_liquidity_invariant_no_double_counting():
    """
    Verifies that gross closed trade turnover ($4,301.17) is treated as recycled capital
    and is NEVER added to settled cash ($3,772.10) to create a phantom $8,073.27 backstop.
    """
    settled_cash = Decimal("3772.10")
    cumulative_unsettled_turnover = Decimal("4301.17")
    promo_debt = Decimal("2500.00")

    state = CollateralInvariantState.calculate(
        settled_cash=settled_cash,
        unsettled_cash=cumulative_unsettled_turnover,
        active_promotional_debt=promo_debt,
        open_positions_market_value=Decimal("0.00"),
        external_liquid_backstop=Decimal("0.00"),
    )

    # Invariant: Total Liquid Backstop must reflect true account equity ($3,772.10), NOT $8,073.27
    assert state.total_liquid_backstop == Decimal("3772.10")
    assert state.total_liquid_backstop != Decimal("8073.27")
    assert state.net_collateral_buffer == Decimal("3772.10") - promo_debt
    assert state.is_solvent is True


def test_macro_liquidity_invariant_with_open_positions_and_external_backstop():
    """
    Verifies Total Liquid Backstop strictly equals:
    settled_cash + open_positions_market_value + external_liquid_backstop
    """
    engine = CollateralEngine(external_liquid_backstop=Decimal("1000.00"))
    engine.add_or_update_promotional_debt(
        PromotionalDebt(
            id="promo_card_1",
            institution="Bank A",
            total_balance=Decimal("4000.00"),
            expiration_date=date(2027, 1, 1),
        )
    )

    settled_cash = Decimal("3772.10")
    open_positions_value = Decimal("500.00")
    unsettled_sales = Decimal("4301.17")

    state = engine.evaluate_invariant(
        settled_cash=settled_cash,
        unsettled_cash=unsettled_sales,
        open_positions_market_value=open_positions_value,
    )

    expected_backstop = settled_cash + open_positions_value + Decimal("1000.00")
    assert state.total_liquid_backstop == expected_backstop
    assert state.net_collateral_buffer == expected_backstop - Decimal("4000.00")
    assert state.is_solvent is True
