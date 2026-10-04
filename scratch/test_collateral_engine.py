"""
scratch/test_collateral_engine.py
=================================
Automated unit test suite for core/collateral_engine.py.
Verifies:
1. Standard operation (days > 90 and buffer >= $1,000 -> multiplier = 1.0)
2. Impending payoff linear scaling (30 <= days <= 90 -> multiplier between 0.5 and 1.0)
3. Payoff critical cutoff (days < 30 -> multiplier = 0.0)
4. Collateral deficit (buffer < 0 -> COLLATERAL_DEFICIT_WARNING, is_solvent = False, multiplier = 0.0)
5. External liquid backstop integration
6. Merging of manual promotional debts with bureau tradelines
"""

import sys
from pathlib import Path
from decimal import Decimal
from datetime import date, timedelta

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.liquidity_models import (
    BureauType,
    Tradeline,
    CreditReportSnapshot,
    PromotionalDebt,
    CollateralInvariantState,
)
from core.collateral_engine import CollateralEngine


def run_tests():
    print("=== Running CollateralEngine Unit Tests ===")
    today = date(2026, 10, 4)

    # Test 1: Standard operation without promotional debt
    print("\n--- Test 1: Standard Operation (No Promo Debt) ---")
    engine = CollateralEngine()
    state = engine.evaluate_invariant(
        settled_cash=Decimal("2000.00"),
        unsettled_cash=Decimal("500.00"),
        as_of=today
    )
    assert state.is_solvent is True, "Expected solvent state"
    assert state.net_collateral_buffer == Decimal("2500.00"), f"Expected 2500 buffer, got {state.net_collateral_buffer}"
    assert state.risk_multiplier == 1.0, f"Expected 1.0 multiplier, got {state.risk_multiplier}"
    print("[PASS] Test 1 passed: Multiplier=1.0, Buffer=$2500.00")

    # Test 2: Standard operation with Promo Debt > 90 days out and buffer >= $1000
    print("\n--- Test 2: Standard Operation (>90d Promo Debt, Buffer >= $1,000) ---")
    engine2 = CollateralEngine(external_liquid_backstop=Decimal("1500.00"))
    debt_95d = PromotionalDebt(
        id="promo_citi_01",
        institution="Citi Simplicity",
        total_balance=Decimal("2000.00"),
        expiration_date=today + timedelta(days=95),
        is_manual=True
    )
    engine2.add_or_update_promotional_debt(debt_95d)
    state2 = engine2.evaluate_invariant(
        settled_cash=Decimal("1000.00"),
        unsettled_cash=Decimal("500.00"),
        as_of=today
    )
    # Total backstop = 1000 + 500 + 1500 = 3000. Buffer = 3000 - 2000 = 1000
    assert state2.total_liquid_backstop == Decimal("3000.00")
    assert state2.net_collateral_buffer == Decimal("1000.00")
    assert state2.is_solvent is True
    assert state2.risk_multiplier == 1.0, f"Expected 1.0 multiplier, got {state2.risk_multiplier}"
    print("[PASS] Test 2 passed: Multiplier=1.0 with $1500 external backstop and >90d reset")

    # Test 3: Impending Payoff Linear Scaling (60 days out)
    print("\n--- Test 3: Impending Payoff Linear Scaling (60d reset) ---")
    engine3 = CollateralEngine()
    debt_60d = PromotionalDebt(
        id="promo_discover_01",
        institution="Discover IT",
        total_balance=Decimal("500.00"),
        expiration_date=today + timedelta(days=60),
        is_manual=True
    )
    engine3.add_or_update_promotional_debt(debt_60d)
    state3 = engine3.evaluate_invariant(
        settled_cash=Decimal("1500.00"),
        unsettled_cash=Decimal("200.00"),
        as_of=today
    )
    # Formula: 0.5 + 0.5 * ((60 - 30) / 60) = 0.5 + 0.25 = 0.75
    expected_mult = 0.75
    assert abs(state3.risk_multiplier - expected_mult) < 0.001, f"Expected {expected_mult}, got {state3.risk_multiplier}"
    print(f"[PASS] Test 3 passed: Multiplier correctly scaled to {state3.risk_multiplier:.4f} at 60 days")

    # Test 4: Critical Reset Cutoff (< 30 days)
    print("\n--- Test 4: Critical Reset Cutoff (<30d reset) ---")
    engine4 = CollateralEngine()
    debt_20d = PromotionalDebt(
        id="promo_chase_01",
        institution="Chase Slate",
        total_balance=Decimal("800.00"),
        expiration_date=today + timedelta(days=20),
        is_manual=True
    )
    engine4.add_or_update_promotional_debt(debt_20d)
    state4 = engine4.evaluate_invariant(
        settled_cash=Decimal("2000.00"),
        unsettled_cash=Decimal("200.00"),
        as_of=today
    )
    assert state4.risk_multiplier == 0.0, f"Expected 0.0, got {state4.risk_multiplier}"
    print("[PASS] Test 4 passed: Multiplier clamped to 0.0 when days < 30")

    # Test 5: Collateral Deficit Warning (Buffer < 0)
    print("\n--- Test 5: Collateral Deficit Warning (Buffer < 0) ---")
    engine5 = CollateralEngine()
    debt_heavy = PromotionalDebt(
        id="promo_bofa_01",
        institution="Bank of America",
        total_balance=Decimal("5000.00"),
        expiration_date=today + timedelta(days=120),
        is_manual=True
    )
    engine5.add_or_update_promotional_debt(debt_heavy)
    state5 = engine5.evaluate_invariant(
        settled_cash=Decimal("1000.00"),
        unsettled_cash=Decimal("200.00"),
        as_of=today
    )
    # Total backstop = 1200. Debt = 5000. Buffer = -3800
    assert state5.net_collateral_buffer == Decimal("-3800.00")
    assert state5.is_solvent is False
    assert state5.risk_multiplier == 0.0, f"Expected 0.0, got {state5.risk_multiplier}"
    print(f"[PASS] Test 5 passed: Insolvent detected, Buffer=-$3800.00, Multiplier=0.0")

    # Test 6: Manual Promo Debt merged with Bureau Tradeline
    print("\n--- Test 6: Merge Manual Promo Terms with Bureau Tradeline ---")
    engine6 = CollateralEngine()
    manual_debt = PromotionalDebt(
        id="promo_citi_manual",
        institution="Citi Simplicity",
        total_balance=Decimal("3000.00"),
        promotional_apr=Decimal("0.00"),
        expiration_date=today + timedelta(days=75),
        is_manual=True
    )
    engine6.add_or_update_promotional_debt(manual_debt)

    # Simulated credit report update with updated balance $2,450.00
    snapshot = CreditReportSnapshot(
        bureau=BureauType.EXPERIAN,
        report_date=today,
        total_revolving_limit=Decimal("15000.00"),
        total_revolving_balance=Decimal("2450.00"),
        aggregate_utilization_pct=16.33,
        hard_inquiries_count=1,
        tradelines=[
            Tradeline(
                account_name="CITI SIMPLICITY CARD",
                masked_account_number="************4321",
                credit_limit=Decimal("10000.00"),
                current_balance=Decimal("2450.00"),
                monthly_payment=Decimal("50.00"),
                is_promotional=False # Bureau doesn't know it's promo
            )
        ]
    )
    engine6.merge_credit_report(snapshot)
    merged_debt = engine6.promotional_debts["promo_citi_manual"]
    assert merged_debt.total_balance == Decimal("2450.00"), f"Expected updated balance 2450.00, got {merged_debt.total_balance}"
    assert merged_debt.minimum_monthly_payment == Decimal("50.00")
    assert merged_debt.days_remaining == 75
    print("[PASS] Test 6 passed: Successfully merged manual promo APR/expiration with bureau updated balance")

    print("\n>>> ALL COLLATERAL ENGINE UNIT TESTS PASSED SUCCESSFULLY! <<<\n")


if __name__ == "__main__":
    run_tests()
