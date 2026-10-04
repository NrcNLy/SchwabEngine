"""
core/liquidity_models.py
========================
Data models for credit reporting, promotional debt management, 
and portfolio collateral invariants.
"""

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


class BureauType(str, Enum):
    EXPERIAN = "EXPERIAN"
    TRANSUNION = "TRANSUNION"
    EQUIFAX = "EQUIFAX"


class Tradeline(BaseModel):
    account_name: str
    masked_account_number: str
    credit_limit: Decimal
    current_balance: Decimal
    monthly_payment: Optional[Decimal] = Decimal("0.00")
    date_opened: Optional[date] = None
    last_reported: Optional[date] = None
    is_promotional: bool = False
    promotional_expiration: Optional[date] = None


class CreditReportSnapshot(BaseModel):
    bureau: BureauType
    report_date: date
    total_revolving_limit: Decimal
    total_revolving_balance: Decimal
    aggregate_utilization_pct: float
    hard_inquiries_count: int
    tradelines: List[Tradeline]
    detected_discrepancies: List[str] = Field(default_factory=list)


class PromotionalDebt(BaseModel):
    id: str
    institution: str
    total_balance: Decimal
    promotional_apr: Decimal = Decimal("0.00")
    expiration_date: date
    minimum_monthly_payment: Decimal = Decimal("0.00")
    days_remaining: int = 0
    is_manual: bool = False
    notes: Optional[str] = None

    def recalculate_days_remaining(self, as_of: Optional[date] = None) -> int:
        target = as_of or date.today()
        delta = (self.expiration_date - target).days
        self.days_remaining = max(0, delta)
        return self.days_remaining

    def merge_with_tradeline(self, tradeline: Tradeline) -> "PromotionalDebt":
        """
        Enriches/merges manual promotional debt terms (APR and expiration date)
        with live bureau tradeline balances and reported limits.
        """
        self.total_balance = tradeline.current_balance
        if tradeline.monthly_payment and tradeline.monthly_payment > Decimal("0.00"):
            self.minimum_monthly_payment = tradeline.monthly_payment
        self.recalculate_days_remaining()
        return self


class CollateralInvariantState(BaseModel):
    timestamp: date
    settled_cash: Decimal
    unsettled_cash: Decimal
    external_liquid_backstop: Decimal = Decimal("0.00")
    total_liquid_backstop: Decimal
    active_promotional_debt: Decimal
    net_collateral_buffer: Decimal
    is_solvent: bool
    risk_multiplier: float

    @classmethod
    def calculate(
        cls,
        settled_cash: Decimal,
        unsettled_cash: Decimal,
        active_promotional_debt: Decimal,
        external_liquid_backstop: Decimal = Decimal("0.00"),
        as_of: Optional[date] = None,
        nearest_days_to_reset: Optional[int] = None,
    ) -> "CollateralInvariantState":
        """
        Calculates the Net Collateral Invariant and resulting Risk Multiplier.
        Formula:
          Total Liquid Backstop = settled_cash + unsettled_cash + external_liquid_backstop
          Net Collateral Buffer = Total Liquid Backstop - active_promotional_debt
        """
        ts = as_of or date.today()
        total_backstop = settled_cash + unsettled_cash + external_liquid_backstop
        buffer = total_backstop - active_promotional_debt
        is_solvent = buffer >= Decimal("0.00")

        # Multiplier evaluation
        if not is_solvent or (nearest_days_to_reset is not None and nearest_days_to_reset < 30):
            multiplier = 0.0
        elif nearest_days_to_reset is not None and nearest_days_to_reset <= 90:
            # Linear scaling from 1.0 down to 0.5 as days approach 30
            # multiplier = 0.5 + 0.5 * ((days - 30) / (90 - 30))
            clamped_days = max(30, min(90, nearest_days_to_reset))
            multiplier = round(0.5 + 0.5 * ((clamped_days - 30) / 60.0), 4)
            if buffer < Decimal("1000.00"):
                multiplier = min(multiplier, 0.5)
        else:
            multiplier = 1.0 if buffer >= Decimal("1000.00") else 0.8

        return cls(
            timestamp=ts,
            settled_cash=settled_cash,
            unsettled_cash=unsettled_cash,
            external_liquid_backstop=external_liquid_backstop,
            total_liquid_backstop=total_backstop,
            active_promotional_debt=active_promotional_debt,
            net_collateral_buffer=buffer,
            is_solvent=is_solvent,
            risk_multiplier=multiplier,
        )


class MacroLiquidityEvent(BaseModel):
    timestamp: str
    state: CollateralInvariantState
    snapshot: Optional[CreditReportSnapshot] = None
    promotional_debts: List[PromotionalDebt] = Field(default_factory=list)
