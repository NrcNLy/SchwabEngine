"""
core/collateral_engine.py
=========================
Passive Net Collateral Invariant Engine for portfolio solvency 
and archival document float management. Strictly decoupled from sizing.
"""

import logging
from datetime import date
from decimal import Decimal
from typing import List, Optional, Dict
from core.liquidity_models import (
    CollateralInvariantState, 
    PromotionalDebt, 
    UnifiedDocumentSnapshot,
    DocumentLineItem
)

logger = logging.getLogger("collateral_engine")


class CollateralEngine:
    """
    Evaluates portfolio solvency against outstanding promotional debt float.
    Operates in passive record-keeping mode.
    """

    def __init__(
        self, 
        external_liquid_backstop: Decimal = Decimal("0.00")
    ):
        self.external_liquid_backstop = external_liquid_backstop
        self.promotional_debts: Dict[str, PromotionalDebt] = {}
        self.latest_snapshot: Optional[UnifiedDocumentSnapshot] = None
        self._current_state: Optional[CollateralInvariantState] = None

    def add_or_update_promotional_debt(self, debt: PromotionalDebt) -> None:
        """Adds or updates a promotional debt record (manual or parsed)."""
        debt.recalculate_days_remaining()
        self.promotional_debts[debt.id] = debt
        logger.info(
            f"Updated PromotionalDebt: [{debt.institution}] Balance: ${debt.total_balance:.2f}, "
            f"Expires: {debt.expiration_date} ({debt.days_remaining}d remaining)"
        )

    def merge_document_snapshot(self, snapshot: UnifiedDocumentSnapshot) -> None:
        """
        Merges newly ingested document snapshot line items.
        Updates balances of existing promotional debts matching institution or account name.
        """
        self.latest_snapshot = snapshot
        for item in snapshot.line_items:
            # Check if there is an existing promotional debt matching institution/account
            matched = False
            for debt_id, debt in self.promotional_debts.items():
                if debt.institution.lower() in item.account_name.lower() or \
                   item.account_name.lower() in debt.institution.lower():
                    debt.total_balance = item.current_balance or Decimal("0.00")
                    if item.monthly_payment and item.monthly_payment > Decimal("0.00"):
                        debt.minimum_monthly_payment = item.monthly_payment
                    debt.recalculate_days_remaining()
                    matched = True
                    logger.info(f"Merged line item into PromotionalDebt [{debt_id}]: Balance=${debt.total_balance}")
                    break

            # If line item is explicitly flagged promotional and not yet tracked
            if not matched and item.is_promotional:
                mask = item.masked_account_number[-4:] if item.masked_account_number else "0000"
                new_debt = PromotionalDebt(
                    id=f"promo_{item.account_name.lower().replace(' ', '_')}_{mask}",
                    institution=item.account_name,
                    total_balance=item.current_balance or Decimal("0.00"),
                    expiration_date=item.promotional_expiration or date.today(),
                    minimum_monthly_payment=item.monthly_payment or Decimal("0.00"),
                    is_manual=False,
                    notes=f"Auto-discovered from {snapshot.document_class.value}"
                )
                self.add_or_update_promotional_debt(new_debt)

    def get_total_active_promotional_debt(self) -> Decimal:
        return sum((d.total_balance for d in self.promotional_debts.values()), Decimal("0.00"))

    def evaluate_invariant(
        self,
        settled_cash: Decimal,
        unsettled_cash: Decimal,
        as_of: Optional[date] = None,
    ) -> CollateralInvariantState:
        """
        Evaluates the passive Net Collateral Buffer.
        """
        total_promo_debt = self.get_total_active_promotional_debt()

        state = CollateralInvariantState.calculate(
            settled_cash=settled_cash,
            unsettled_cash=unsettled_cash,
            active_promotional_debt=total_promo_debt,
            external_liquid_backstop=self.external_liquid_backstop,
            as_of=as_of,
        )

        if not state.is_solvent:
            logger.info(
                f"[PASSIVE COLLATERAL AUDIT] Net Collateral Buffer is negative: "
                f"${state.net_collateral_buffer:.2f}. Total Backstop: ${state.total_liquid_backstop:.2f}, "
                f"Active Promo Debt: ${state.active_promotional_debt:.2f}."
            )

        self._current_state = state
        return state

    @property
    def current_state(self) -> Optional[CollateralInvariantState]:
        return self._current_state

