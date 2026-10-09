"""
core/liquidity_models.py
========================
Data models for automated document classification, manual liquidity targets,
and passive portfolio collateral tracking.
"""

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


class DocumentClass(str, Enum):
    CREDIT_REPORT = "CREDIT_REPORT"
    BANK_STATEMENT = "BANK_STATEMENT"
    CREDIT_CARD_STATEMENT = "CREDIT_CARD_STATEMENT"
    PAYSTUB = "PAYSTUB"
    STUDENT_LOAN_STATEMENT = "STUDENT_LOAN_STATEMENT"
    TAX_DOCUMENT = "TAX_DOCUMENT"
    MISCELLANEOUS_FINANCIAL = "MISCELLANEOUS_FINANCIAL"


class DocumentLineItem(BaseModel):
    account_name: str
    masked_account_number: Optional[str] = None
    credit_limit: Optional[Decimal] = Decimal("0.00")
    current_balance: Optional[Decimal] = Decimal("0.00")
    monthly_payment: Optional[Decimal] = Decimal("0.00")
    date_opened: Optional[date] = None
    last_reported: Optional[date] = None
    is_promotional: bool = False
    promotional_expiration: Optional[date] = None
    notes: Optional[str] = None


class UnifiedDocumentSnapshot(BaseModel):
    document_class: DocumentClass
    institution_or_bureau: str
    report_date: date
    total_revolving_limit: Optional[Decimal] = Decimal("0.00")
    total_revolving_balance: Optional[Decimal] = Decimal("0.00")
    aggregate_utilization_pct: Optional[float] = 0.0
    hard_inquiries_count: Optional[int] = 0
    line_items: List[DocumentLineItem] = Field(default_factory=list)
    detected_discrepancies: List[str] = Field(default_factory=list)

    @classmethod
    def from_legacy(cls, legacy: Dict[str, Any]) -> "UnifiedDocumentSnapshot":
        """Adapts the pre-Directive-28 CreditReportSnapshot dict shape."""
        items = [DocumentLineItem(**{k: v for k, v in t.items() if k in DocumentLineItem.model_fields})
                 for t in legacy.get("tradelines", [])]
        return cls(
            document_class=DocumentClass.CREDIT_REPORT,
            institution_or_bureau=str(legacy.get("bureau", "UNKNOWN")),
            report_date=legacy.get("report_date", date.today().isoformat()),
            total_revolving_limit=legacy.get("total_revolving_limit", Decimal("0.00")),
            total_revolving_balance=legacy.get("total_revolving_balance", Decimal("0.00")),
            aggregate_utilization_pct=legacy.get("aggregate_utilization_pct", 0.0),
            hard_inquiries_count=legacy.get("hard_inquiries_count", 0),
            line_items=items,
            detected_discrepancies=list(legacy.get("detected_discrepancies", [])),
        )


def extract_snapshot(data: Dict[str, Any]) -> Optional[UnifiedDocumentSnapshot]:
    """
    Pulls a UnifiedDocumentSnapshot out of a macro_liquidity.json payload.
    Prefers the unified "snapshot" key; falls back to the legacy "credit_report" dict.
    Returns None (never raises) if neither is present or parseable.
    """
    try:
        if data.get("snapshot"):
            return UnifiedDocumentSnapshot(**data["snapshot"])
        if data.get("credit_report"):
            return UnifiedDocumentSnapshot.from_legacy(data["credit_report"])
    except Exception:
        return None
    return None


class LiquidityTarget(BaseModel):
    target_id: str
    label: str
    target_amount: Decimal
    target_date: date
    is_active: bool = True
    created_at: date


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


class CollateralInvariantState(BaseModel):
    timestamp: date
    settled_cash: Decimal
    unsettled_cash: Decimal
    open_positions_market_value: Decimal = Decimal("0.00")
    external_liquid_backstop: Decimal = Decimal("0.00")
    total_liquid_backstop: Decimal
    active_promotional_debt: Decimal
    net_collateral_buffer: Decimal
    is_solvent: bool

    @classmethod
    def calculate(
        cls,
        settled_cash: Decimal,
        unsettled_cash: Decimal,
        active_promotional_debt: Decimal,
        open_positions_market_value: Decimal = Decimal("0.00"),
        external_liquid_backstop: Decimal = Decimal("0.00"),
        as_of: Optional[date] = None,
    ) -> "CollateralInvariantState":
        """
        Calculates the passive Net Collateral Invariant.
        Formula:
          true_equity = settled_cash + open_positions_market_value
          Total Liquid Backstop = true_equity + external_liquid_backstop
          Net Collateral Buffer = Total Liquid Backstop - active_promotional_debt
        """
        ts = as_of or date.today()
        true_equity = settled_cash + open_positions_market_value
        total_backstop = true_equity + external_liquid_backstop
        buffer = total_backstop - active_promotional_debt
        is_solvent = buffer >= Decimal("0.00")

        return cls(
            timestamp=ts,
            settled_cash=settled_cash,
            unsettled_cash=unsettled_cash,
            open_positions_market_value=open_positions_market_value,
            external_liquid_backstop=external_liquid_backstop,
            total_liquid_backstop=total_backstop,
            active_promotional_debt=active_promotional_debt,
            net_collateral_buffer=buffer,
            is_solvent=is_solvent,
        )


class MacroLiquidityEvent(BaseModel):
    timestamp: str
    state: CollateralInvariantState
    snapshot: Optional[UnifiedDocumentSnapshot] = None
    promotional_debts: List[PromotionalDebt] = Field(default_factory=list)
    liquidity_targets: List[LiquidityTarget] = Field(default_factory=list)
