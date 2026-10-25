"""
tests/test_t1_ledger.py
=======================
Unit & property tests for Workstream 3 (#123):
Regulatory Cash Ledger & T+1 Settlement State Machine (SEC Rule 15c6-1).

Validates:
1. Three-bucket capital segregation: C_total(t) = C_settled(t) + C_unsettled(t) + C_reserved(t).
2. Strict Decimal quantization using ROUND_HALF_UP to 0.01 USD.
3. Pre-Trade GFV Firewall (can_submit_order): explicit "GFV REJECTION" when BUY notional
   exceeds MaxAllowableBuyNotional(t) = max(0.00, C_settled(t) - C_reserved(t) - (NLV(t) * CashFloorPct)).
4. Intraday sale proceeds credited exclusively to C_unsettled(t) (never spendable for same-day buys).
5. Overnight T+1 clearing transition (00:00:00 EDT): C_settled(T+1) <- C_settled(T) + C_unsettled(T), C_unsettled(T+1) <- 0.00.
6. Thread-safe RLock access across concurrent threads.
7. OrderManager.place_limit_buy and RiskEngine.can_submit_order GFV blocking.
"""

from __future__ import annotations

import threading
from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
import pytz

from core.ledger import (
    GFVRejectionError,
    RegulatoryCashLedger,
    SettlementLedger,
    T1CashLedger,
    quantize_usd,
)
from execution.order_manager import OrderManager
from execution.risk_manager import RiskEngine

_EDT = pytz.timezone("America/New_York")


def test_decimal_quantization_round_half_up() -> None:
    """Assert all cash arithmetic uses Decimal with ROUND_HALF_UP to 0.01."""
    assert quantize_usd("100.005") == Decimal("100.01")
    assert quantize_usd("100.004") == Decimal("100.00")
    assert quantize_usd(4400.125) == Decimal("4400.13")


def test_three_bucket_capital_segregation_and_intraday_sale() -> None:
    """
    Assert C_total(t) = C_settled(t) + C_unsettled(t) + C_reserved(t).
    Selling a position intraday credits C_unsettled(t) exclusively and cannot fund a new BUY.
    """
    ledger = T1CashLedger(settled_cash="1000.00", nlv="4400.00")
    assert ledger.settled_cash == Decimal("1000.00")
    assert ledger.unsettled_cash == Decimal("0.00")
    assert ledger.reserved_cash == Decimal("0.00")
    assert ledger.total_cash == Decimal("1000.00")

    # Reserve $400 for a working buy order
    ledger.reserve_for_buy_order("ORD-1", "400.00", symbol="SOXL")
    assert ledger.settled_cash == Decimal("600.00")
    assert ledger.reserved_cash == Decimal("400.00")
    assert ledger.total_cash == Decimal("1000.00")

    # Fill the buy order at $395.00 ($5.00 price improvement refunded to settled)
    ledger.record_buy_fill("SOXL", 10, "39.50", order_id="ORD-1")
    assert ledger.settled_cash == Decimal("605.00")
    assert ledger.reserved_cash == Decimal("0.00")
    assert ledger.unsettled_cash == Decimal("0.00")

    # Sell the 10 shares intraday at $42.00 ($420.00 proceeds -> C_unsettled)
    proceeds = ledger.record_sell_fill("SOXL", 10, "42.00")
    assert proceeds == Decimal("420.00")
    assert ledger.settled_cash == Decimal("605.00")
    assert ledger.unsettled_cash == Decimal("420.00")
    assert ledger.total_cash == Decimal("1025.00")

    # Attempting to buy $700.00 (which requires tapping the $420 unsettled proceeds) MUST be rejected!
    allowed, reason = ledger.can_submit_order("BUY", "700.00", symbol="TQQQ")
    assert allowed is False
    assert "GFV REJECTION" in reason
    assert "C_unsettled=$420.00" in reason


def test_gfv_rejection_respects_cash_floor_and_reserved() -> None:
    """
    Validate MaxAllowableBuyNotional(t) = max(0.00, C_settled(t) - C_reserved(t) - (NLV(t) * CashFloorPct)).
    """
    ledger = T1CashLedger(
        settled_cash="1000.00",
        unsettled_cash="500.00",
        reserved_cash="200.00",
        nlv="4400.00",
        cash_floor_pct="0.10",  # 10% of $4,400 = $440.00 floor
    )
    # Note: when initialized with settled=1000 and reserved=200 directly,
    # MaxAllowable = max(0, 1000 - 200 - 440) = $360.00
    max_buy = ledger.compute_max_allowable_buy_notional()
    assert max_buy == Decimal("360.00")

    ok_within, msg_within = ledger.can_submit_order("BUY", "360.00", symbol="SPY")
    assert ok_within is True
    assert "OK" in msg_within

    ok_exceed, msg_exceed = ledger.can_submit_order("BUY", "360.01", symbol="SPY")
    assert ok_exceed is False
    assert "GFV REJECTION" in msg_exceed

    with pytest.raises(GFVRejectionError, match="GFV REJECTION"):
        ledger.reserve_for_buy_order("ORD-GFV", "360.01", symbol="SPY")


def test_overnight_t1_clearing_transition_at_midnight_edt() -> None:
    """
    Validate 00:00:00 EDT T+1 rollover:
    C_settled(T+1) <- C_settled(T) + C_unsettled(T)
    C_unsettled(T+1) <- 0.00
    """
    day1_dt = _EDT.localize(datetime(2026, 10, 22, 15, 52, 0))
    ledger = T1CashLedger(settled_cash="412.50", nlv="4400.00")
    ledger.record_sell_fill("SOXL", 139, "28.50", when=day1_dt)

    expected_unsettled = Decimal("3961.50")
    assert ledger.settled_cash == Decimal("412.50")
    assert ledger.unsettled_cash == expected_unsettled

    # Before midnight clearing, cannot redeploy the $3,961.50 sale proceeds
    allowed_intraday, reason_intraday = ledger.can_submit_order("BUY", "1500.00", symbol="SOXL")
    assert allowed_intraday is False
    assert "GFV REJECTION" in reason_intraday

    # At 00:00:00 EDT on T+1 (2026-10-23), overnight clearing sweeps C_unsettled -> C_settled
    midnight_t1 = _EDT.localize(datetime(2026, 10, 23, 0, 0, 0))
    swept = ledger.process_t1_overnight_clearing(current_dt=midnight_t1)
    assert swept == expected_unsettled
    assert ledger.settled_cash == Decimal("4374.00")
    assert ledger.unsettled_cash == Decimal("0.00")

    # Now the $1,500.00 buy order is permitted
    allowed_t1, _ = ledger.can_submit_order("BUY", "1500.00", symbol="SOXL")
    assert allowed_t1 is True


def test_order_manager_and_risk_engine_enforce_gfv_rejection() -> None:
    """
    Verify that OrderManager.place_limit_buy and RiskEngine.can_submit_order block
    any buy order exceeding liquid settled cash with an explicit GFV REJECTION.
    """
    t1_ledger = RegulatoryCashLedger(settled_cash="500.00", unsettled_cash="2500.00", nlv="4400.00")
    rest_client = MagicMock()
    rest_client.get_account_numbers.return_value = [
        {"accountNumber": "12344015", "hashValue": "HASH4015"}
    ]
    om = OrderManager(rest_client=rest_client, ledger=t1_ledger, cfg={"account": {"required_suffix": "015"}})
    om.initialize_firewall()

    # Attempting to buy 25 shares @ $25.00 = $625.00 > $500.00 settled cash -> raises GFVRejectionError
    with pytest.raises(GFVRejectionError, match="GFV REJECTION"):
        om.place_limit_buy("SOXL", quantity=25, limit_price=25.00)
    rest_client.place_order.assert_not_called()

    # RiskEngine also exposes can_submit_order via its T1CashLedger
    risk = RiskEngine(account_base=500.0)
    allowed, reason = risk.can_submit_order("BUY", 600.0, symbol="TQQQ")
    assert allowed is False
    assert "GFV REJECTION" in reason

    # SettlementLedger also supports can_submit_order
    sl = SettlementLedger(baseline_settled="500.00")
    sl_ok, sl_reason = sl.can_submit_order("BUY", "600.00", symbol="SOXL")
    assert sl_ok is False
    assert "GFV REJECTION" in sl_reason


def test_thread_safe_concurrent_ledger_mutations() -> None:
    """Verify RLock thread-safety under concurrent reservations and releases."""
    ledger = T1CashLedger(settled_cash="10000.00", nlv="10000.00")
    errors = []

    def worker(idx: int) -> None:
        try:
            oid = f"ORD-{idx}"
            ledger.reserve_for_buy_order(oid, "50.00", symbol="SPY")
            ledger.release_buy_reservation(oid)
            ledger.record_sell_fill("SPY", 1, "10.00")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert ledger.settled_cash == Decimal("10000.00")
    assert ledger.reserved_cash == Decimal("0.00")
    assert ledger.unsettled_cash == Decimal("400.00")
