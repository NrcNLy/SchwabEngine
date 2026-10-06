from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo
import pytest

from core.ledger import BrokerBalances, ReconciliationLockError, SettlementLedger

D = Decimal
EDT = ZoneInfo("America/New_York")
FRIDAY_NOON = datetime(2026, 10, 2, 12, 0, tzinfo=EDT)


def test_three_bucket_invariants():
    lg = SettlementLedger(D("1500.00"))
    assert lg.settled_cash == D("1500.00")
    assert lg.unsettled_cash == D("0.00")
    assert lg.locked_cash == D("0.00")

    # Allocate $300 for SOXL
    assert lg.allocate_capital("SOXL", D("300.00")) is True
    assert lg.settled_cash == D("1200.00")
    assert lg.locked_cash == D("300.00")

    # Confirm buy fills at $280 ($20 returns to settled cash)
    pos = lg.confirm_buy("SOXL", 2, "140", simulated=True, when=FRIDAY_NOON)
    assert lg.settled_cash == D("1220.00")
    assert lg.locked_cash == D("0.00")
    assert pos.funded_with_settled is True

    # Check GFV safety
    safe, msg = pos.is_gfv_safe_to_sell()
    assert safe is True

    # Record sell -> proceeds $300 enter unsettled_cash (Bucket 2)
    proceeds = lg.record_sell("SOXL", 2, "150", simulated=True, when=FRIDAY_NOON)
    assert proceeds == D("300.00")
    assert lg.settled_cash == D("1220.00")
    assert lg.unsettled_cash == D("300.00")
    assert lg.locked_cash == D("0.00")


def test_strict_reconciliation_lock_live_mode():
    lg = SettlementLedger(D("1000.00"))
    lg.adopt_position("TQQQ", 5, "50", simulated=False, opened_at=FRIDAY_NOON)

    # Live sell without execution payload must fail closed
    with pytest.raises(ReconciliationLockError):
        lg.record_sell("TQQQ", 5, "52", simulated=False, when=FRIDAY_NOON)

    # Live sell with unconfirmed payload must fail closed
    with pytest.raises(ReconciliationLockError):
        lg.record_sell(
            "TQQQ", 5, "52", simulated=False, when=FRIDAY_NOON,
            execution_payload={"status": "WORKING", "filledQuantity": 0}
        )

    # Live sell with definitive filled payload succeeds
    filled_doc = {
        "status": "FILLED",
        "filledQuantity": 5,
        "orderActivityCollection": [{"executionLegs": [{"legId": 1, "quantity": 5, "price": 52.0}]}],
    }
    proceeds = lg.record_sell(
        "TQQQ", 5, "52", simulated=False, when=FRIDAY_NOON,
        execution_payload=filled_doc
    )
    assert proceeds == D("260.00")
    assert lg.unsettled_cash == D("260.00")


def test_gfv_blocked_and_overnight_hold_transition():
    lg = SettlementLedger(D("500.00"))
    pos = lg.adopt_position(
        "TNA", 10, "30", simulated=False, opened_at=FRIDAY_NOON,
        funded_with_settled=False, settle_date=date(2026, 10, 5)
    )

    # Intraday sale blocked
    can_sell, veto = lg.can_sell_position("TNA", today=date(2026, 10, 2))
    assert can_sell is False
    assert "GFV VETO" in veto

    # Weekend / pre-settlement blocked
    can_sell_wk, _ = lg.can_sell_position("TNA", today=date(2026, 10, 4))
    assert can_sell_wk is False

    # Settlement day (Monday) unlocked
    can_sell_mon, _ = lg.can_sell_position("TNA", today=date(2026, 10, 5))
    assert can_sell_mon is True
