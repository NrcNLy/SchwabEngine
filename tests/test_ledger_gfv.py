from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from core.ledger import BrokerBalances, SettlementLedger

D = Decimal
EDT = ZoneInfo("America/New_York")
FRIDAY_NOON = datetime(2026, 10, 2, 12, 0, tzinfo=EDT)


def ledger(settled="1000", **kw) -> SettlementLedger:
    return SettlementLedger(D(settled), **kw)


def test_entries_are_funded_from_settled_cash_only():
    lg = ledger("1000")
    lg.record_sell("SOXL", 3, "100", when=FRIDAY_NOON)  # $300 unsettled until Monday
    assert lg.unsettled_total == D("300.00")

    # 995 > settled(1000) - buffer(10): refused even though NLV is 1300
    ok, _ = lg.check_order_allowed(D("995"))
    assert ok is False

    assert lg.allocate_capital("TQQQ", D("150")) is True
    assert lg.settled == D("850.00")
    assert lg.unsettled_total == D("300.00"), "allocation must never consume Bucket 2"


def test_unsettled_funds_cannot_fund_an_order_larger_than_settled():
    lg = ledger("100")
    lg.record_sell("TNA", 10, "50", when=FRIDAY_NOON)  # $500 unsettled
    assert lg.allocate_capital("TNA", D("200")) is False
    assert lg.settled == D("100.00")


def test_single_ticker_cap_is_20pct_of_nlv_and_not_hardcoded():
    lg = ledger("3747.50")
    assert lg.nlv == D("3747.50")
    assert lg.max_single_exposure == D("749.50")
    ok, why = lg.check_order_allowed(D("750"))
    assert ok is False and "cap" in why
    assert lg.check_order_allowed(D("749.50"))[0] is True

    small = ledger("1000")
    assert small.max_single_exposure == D("200.00")


def test_rollover_only_on_or_after_settle_date():
    lg = ledger("0")
    lg.record_sell("SOXL", 2, "50", when=FRIDAY_NOON)  # settles Monday 2026-10-05
    assert lg.rollover(date(2026, 10, 2)) == D("0")
    assert lg.rollover(date(2026, 10, 4)) == D("0")
    assert lg.unsettled_total == D("100.00") and lg.settled == D("0.00")
    assert lg.rollover(date(2026, 10, 5)) == D("100.00")
    assert lg.settled == D("100.00") and lg.unsettled_total == D("0.00")


def test_first_sync_is_authoritative_then_lower_of_wins():
    lg = ledger("0", data_source="LIVE_SCHWAB")
    lg.sync_from_broker(BrokerBalances(liquidation_value=D("600"), settled_cash=D("500")))
    assert lg.settled == D("500.00") and lg.synced

    drift = lg.sync_from_broker(BrokerBalances(liquidation_value=D("550"), settled_cash=D("450")))
    assert drift == D("50.00")
    assert lg.settled == D("450.00"), "ledger must adopt the LOWER broker figure"
    assert lg.gfv_risk_flag is True


def test_swvxx_and_ach_do_not_increase_settled_cash():
    lg = ledger("0", data_source="LIVE_SCHWAB")
    lg.sync_from_broker(BrokerBalances(
        liquidation_value=D("5000"), settled_cash=D("300"), swvxx_value=D("4000"), positions_value=D("0")))
    lg.set_pending_ach(D("1000"))
    assert lg.settled == D("300.00")
    assert lg.swvxx_balance == D("4000.00")
    assert lg.check_order_allowed(D("295"))[0] is False  # 300 - 10 buffer
    assert lg.check_order_allowed(D("100"))[0] is True


def test_order_ceiling_provider_is_enforced_and_fails_closed():
    lg = ledger("1000")
    lg.set_order_ceiling_provider(lambda: D("50"))
    assert lg.check_order_allowed(D("60"))[0] is False
    assert lg.check_order_allowed(D("40"))[0] is True

    def broken():
        raise RuntimeError("policy store unreadable")

    lg.set_order_ceiling_provider(broken)
    ok, why = lg.check_order_allowed(D("10"))
    assert ok is False and "unavailable" in why

    lg.set_order_ceiling_provider(None)
    assert lg.check_order_allowed(D("10"))[0] is True


def test_buy_then_sell_round_trip_books_realized_pnl_and_unsettled_proceeds():
    lg = ledger("1000")
    assert lg.allocate_capital("SOXL", D("150"))
    lg.confirm_buy("SOXL", 3, "50", simulated=True, when=FRIDAY_NOON)
    assert lg.settled == D("850.00")
    lg.mark_price("SOXL", "55")
    assert lg.unrealized_pnl() == D("15.00")
    proceeds = lg.record_sell("SOXL", 3, "55", simulated=True, when=FRIDAY_NOON)
    assert proceeds == D("165.00")
    assert lg.realized_pnl_today(date(2026, 10, 2)) == D("15.00")
    assert lg.unsettled_total == D("165.00")
    assert lg.positions == {}


def test_three_bucket_capital_states():
    lg = ledger("1000")
    assert lg.settled_cash == D("1000.00")
    assert lg.unsettled_cash == D("0.00")
    assert lg.locked_cash == D("0.00")

    # Reserve capital for SOXL
    assert lg.allocate_capital("SOXL", D("200")) is True
    assert lg.settled_cash == D("800.00")
    assert lg.unsettled_cash == D("0.00")
    assert lg.locked_cash == D("200.00")

    # Fill SOXL at 190 (surplus 10 returned to settled)
    pos = lg.confirm_buy("SOXL", 2, "95", simulated=True, when=FRIDAY_NOON)
    assert lg.settled_cash == D("810.00")
    assert lg.unsettled_cash == D("0.00")
    assert lg.locked_cash == D("0.00")
    assert pos.funded_with_settled is True

    # Sell SOXL -> proceeds enter unsettled_cash
    proceeds = lg.record_sell("SOXL", 2, "100", simulated=True, when=FRIDAY_NOON)
    assert proceeds == D("200.00")
    assert lg.settled_cash == D("810.00")
    assert lg.unsettled_cash == D("200.00")
    assert lg.locked_cash == D("0.00")


def test_gfv_position_settled_vs_unsettled_funding():
    lg = ledger("1000")
    # 1. Position bought with settled funds is GFV safe on same day
    lg.allocate_capital("SOXL", D("100"))
    pos_settled = lg.confirm_buy("SOXL", 2, "50", simulated=True, when=FRIDAY_NOON, funded_with_settled=True)
    assert pos_settled.funded_with_settled is True
    safe, msg = pos_settled.is_gfv_safe_to_sell(today=date(2026, 10, 2))
    assert safe is True
    can_sell, _ = lg.can_sell_position("SOXL", today=date(2026, 10, 2))
    assert can_sell is True
    assert lg.evaluate_gfv_compliance("SOXL", today=date(2026, 10, 2)) is True

    # 2. Position bought with unsettled funds is BLOCKED on trade date
    pos_unsettled = lg.adopt_position(
        "TNA", 10, "40", simulated=False, opened_at=FRIDAY_NOON,
        funded_with_settled=False, settle_date=date(2026, 10, 5)
    )
    assert pos_unsettled.funded_with_settled is False
    assert pos_unsettled.settle_date == date(2026, 10, 5)

    # Friday (trade date) -> BLOCKED
    safe_fri, msg_fri = pos_unsettled.is_gfv_safe_to_sell(today=date(2026, 10, 2))
    assert safe_fri is False
    assert "GFV VETO" in msg_fri
    can_sell_fri, veto_fri = lg.can_sell_position("TNA", today=date(2026, 10, 2))
    assert can_sell_fri is False
    assert "GFV VETO" in veto_fri
    assert lg.evaluate_gfv_compliance("TNA", today=date(2026, 10, 2)) is False

    # Monday (settlement date) -> ALLOWED
    safe_mon, msg_mon = pos_unsettled.is_gfv_safe_to_sell(today=date(2026, 10, 5))
    assert safe_mon is True
    assert "settled" in msg_mon.lower()
    can_sell_mon, _ = lg.can_sell_position("TNA", today=date(2026, 10, 5))
    assert can_sell_mon is True
    assert lg.evaluate_gfv_compliance("TNA", today=date(2026, 10, 5)) is True


def test_strict_reconciliation_lock_live_sell_requires_execution_payload():
    import pytest
    from core.ledger import ReconciliationLockError

    lg = ledger("1000")
    lg.adopt_position("SOXL", 10, "50", simulated=False, opened_at=FRIDAY_NOON)

    # 1. Calling record_sell with simulated=False and no execution_payload MUST raise ReconciliationLockError
    with pytest.raises(ReconciliationLockError, match="Definitive completed execution payload from Schwab API is required"):
        lg.record_sell("SOXL", 10, "55", simulated=False, when=FRIDAY_NOON)

    # 2. Calling record_sell with unconfirmed / non-terminal payload MUST raise ReconciliationLockError
    pending_payload = {
        "status": "QUEUED",
        "filledQuantity": 0,
        "orderActivityCollection": [],
    }
    with pytest.raises(ReconciliationLockError, match="Execution payload for SOXL is not filled"):
        lg.record_sell("SOXL", 10, "55", simulated=False, when=FRIDAY_NOON, execution_payload=pending_payload)

    # 3. Calling record_sell with definitive FILLED payload succeeds and credits unsettled cash
    filled_payload = {
        "status": "FILLED",
        "filledQuantity": 10,
        "orderActivityCollection": [
            {"executionLegs": [{"legId": 1, "quantity": 10, "price": 55.0}]}
        ],
    }
    proceeds = lg.record_sell("SOXL", 10, "55", simulated=False, when=FRIDAY_NOON, execution_payload=filled_payload)
    assert proceeds == D("550.00")
    assert lg.unsettled_cash == D("550.00")
    assert "SOXL" not in lg.positions

