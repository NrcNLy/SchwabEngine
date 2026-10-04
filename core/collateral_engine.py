"""
core/collateral_engine.py
=========================
Net Collateral Invariant Engine for dynamic portfolio solvency, 
promotional debt float management, and Almgren-Chriss risk throttling.
"""

import logging
from datetime import date
from decimal import Decimal
from typing import List, Optional, Dict, Tuple
from core.liquidity_models import (
    CollateralInvariantState, 
    PromotionalDebt, 
    CreditReportSnapshot,
    Tradeline
)

logger = logging.getLogger("collateral_engine")


class CollateralEngine:
    """
    Evaluates portfolio solvency against outstanding promotional debt float
    and derives the macro risk multiplier to throttle or halt new entry orders.
    """

    def __init__(
        self, 
        external_liquid_backstop: Decimal = Decimal("0.00")
    ):
        self.external_liquid_backstop = external_liquid_backstop
        self.promotional_debts: Dict[str, PromotionalDebt] = {}
        self.latest_snapshot: Optional[CreditReportSnapshot] = None
        self._current_state: Optional[CollateralInvariantState] = None

    def add_or_update_promotional_debt(self, debt: PromotionalDebt) -> None:
        """Adds or updates a promotional debt record (manual or parsed)."""
        debt.recalculate_days_remaining()
        self.promotional_debts[debt.id] = debt
        logger.info(
            f"Updated PromotionalDebt: [{debt.institution}] Balance: ${debt.total_balance:.2f}, "
            f"Expires: {debt.expiration_date} ({debt.days_remaining}d remaining)"
        )

    def merge_credit_report(self, snapshot: CreditReportSnapshot) -> None:
        """
        Merges newly ingested credit bureau snapshot into active tradelines.
        Updates balances of existing promotional debts matching institution or account name.
        """
        self.latest_snapshot = snapshot
        for tradeline in snapshot.tradelines:
            # Check if there is an existing promotional debt matching institution/account
            matched = False
            for debt_id, debt in self.promotional_debts.items():
                if debt.institution.lower() in tradeline.account_name.lower() or \
                   tradeline.account_name.lower() in debt.institution.lower():
                    debt.merge_with_tradeline(tradeline)
                    matched = True
                    logger.info(f"Merged bureau tradeline into PromotionalDebt [{debt_id}]: Balance=${debt.total_balance}")
                    break

            # If tradeline is explicitly flagged promotional and not yet tracked
            if not matched and tradeline.is_promotional:
                new_debt = PromotionalDebt(
                    id=f"promo_{tradeline.account_name.lower().replace(' ', '_')}_{tradeline.masked_account_number[-4:]}",
                    institution=tradeline.account_name,
                    total_balance=tradeline.current_balance,
                    expiration_date=tradeline.promotional_expiration or date.today(),
                    minimum_monthly_payment=tradeline.monthly_payment or Decimal("0.00"),
                    is_manual=False,
                    notes=f"Auto-discovered from {snapshot.bureau.value} report"
                )
                self.add_or_update_promotional_debt(new_debt)

    def get_total_active_promotional_debt(self) -> Decimal:
        return sum((d.total_balance for d in self.promotional_debts.values()), Decimal("0.00"))

    def get_nearest_days_to_reset(self, as_of: Optional[date] = None) -> Optional[int]:
        if not self.promotional_debts:
            return None
        days_list = [d.recalculate_days_remaining(as_of) for d in self.promotional_debts.values()]
        return min(days_list) if days_list else None

    def evaluate_invariant(
        self,
        settled_cash: Decimal,
        unsettled_cash: Decimal,
        as_of: Optional[date] = None,
    ) -> CollateralInvariantState:
        """
        Evaluates the Net Collateral Buffer and updates internal state.
        Formula:
          Total Liquid Backstop = settled_cash + unsettled_cash + external_liquid_backstop
          Net Collateral Buffer = Total Liquid Backstop - active_promotional_debt
        """
        total_promo_debt = self.get_total_active_promotional_debt()
        nearest_days = self.get_nearest_days_to_reset(as_of)

        state = CollateralInvariantState.calculate(
            settled_cash=settled_cash,
            unsettled_cash=unsettled_cash,
            active_promotional_debt=total_promo_debt,
            external_liquid_backstop=self.external_liquid_backstop,
            as_of=as_of,
            nearest_days_to_reset=nearest_days,
        )

        if not state.is_solvent:
            logger.warning(
                f"[COLLATERAL_DEFICIT_WARNING] Net Collateral Buffer is negative: "
                f"${state.net_collateral_buffer:.2f}. Total Backstop: ${state.total_liquid_backstop:.2f}, "
                f"Active Promo Debt: ${state.active_promotional_debt:.2f}. Order sizing throttled to 0.0."
            )
        elif nearest_days is not None and nearest_days < 30:
            logger.warning(
                f"[CRITICAL_PROMO_RESET] Nearest promotional debt reset is {nearest_days} days away (<30d). "
                f"Halting new algorithmic entries to accumulate settled cash."
            )
        elif nearest_days is not None and nearest_days <= 90:
            logger.info(
                f"[IMPENDING_PAYOFF_THROTTLING] Nearest reset in {nearest_days} days. "
                f"Risk multiplier throttled to {state.risk_multiplier:.4f}."
            )

        self._current_state = state
        return state

    @property
    def current_state(self) -> Optional[CollateralInvariantState]:
        return self._current_state
