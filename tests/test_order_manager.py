from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo
import pytest

from core.ledger import SettlementLedger
from execution.order_manager import AccountFirewallError, GoodFaithViolationBlockedError, OrderManager

D = Decimal
EDT = ZoneInfo("America/New_York")
FRIDAY_NOON = datetime(2026, 10, 2, 12, 0, tzinfo=EDT)


class FakeResponse:
    def __init__(self, headers=None, status_code=201):
        self.headers = headers or {"Location": "https://api.schwabapi.com/trader/v1/accounts/xyz123/orders/998877"}
        self.status_code = status_code


@pytest.fixture
def mock_rest():
    rest = MagicMock()
    rest.get_account_numbers.return_value = [
        {"accountNumber": "987654015", "hashValue": "HASH_015_SAFE"}
    ]
    rest.place_order.return_value = FakeResponse()
    return rest


@pytest.fixture
def cfg():
    return {
        "account": {
            "required_suffix": "015",
            "blocked_keywords": ["ROBO", "INTELLIGENT", "IRA", "PORTFOLIO_MANAGED"],
        }
    }


def test_firewall_passes_for_valid_account(mock_rest, cfg):
    lg = SettlementLedger(D("1000"))
    om = OrderManager(mock_rest, lg, cfg)
    om.initialize_firewall()
    assert om.account_hash == "HASH_015_SAFE"


def test_firewall_blocks_when_no_suffix_match(mock_rest, cfg):
    mock_rest.get_account_numbers.return_value = [
        {"accountNumber": "123456789", "hashValue": "HASH_789"}
    ]
    lg = SettlementLedger(D("1000"))
    om = OrderManager(mock_rest, lg, cfg)
    with pytest.raises(AccountFirewallError, match="no account ending in '015'"):
        om.initialize_firewall()


def test_firewall_blocks_when_account_contains_blocked_keyword(mock_rest, cfg):
    mock_rest.get_account_numbers.return_value = [
        {"accountNumber": "IRA987654015", "hashValue": "HASH_015_IRA"}
    ]
    lg = SettlementLedger(D("1000"))
    om = OrderManager(mock_rest, lg, cfg)
    with pytest.raises(AccountFirewallError, match="contains blocked keyword 'IRA'"):
        om.initialize_firewall()


def test_place_limit_buy_formats_price_and_submits_without_reason_bug(mock_rest, cfg):
    lg = SettlementLedger(D("1000"))
    om = OrderManager(mock_rest, lg, cfg)
    om.initialize_firewall()

    order_id = om.place_limit_buy("SOXL", 10, 25.50)
    assert order_id == "998877"
    mock_rest.place_order.assert_called_once()
    call_args = mock_rest.place_order.call_args
    assert call_args[0][0] == "HASH_015_SAFE"
    body = call_args[0][1]
    assert body["orderType"] == "LIMIT"
    assert body["price"] == "25.50"
    assert body["orderLegCollection"][0]["quantity"] == 10
    assert call_args[1]["action"] == "LIMIT_BUY"
    assert call_args[1]["is_essential"] is False


def test_execute_market_sell_allows_settled_funded_position(mock_rest, cfg):
    lg = SettlementLedger(D("1000"))
    om = OrderManager(mock_rest, lg, cfg)
    om.initialize_firewall()

    # Create position funded with settled cash
    lg.allocate_capital("SOXL", D("200"))
    lg.confirm_buy("SOXL", 2, "100", simulated=True, funded_with_settled=True, when=FRIDAY_NOON)

    # Market sell should proceed smoothly
    order_id = om.execute_market_sell("SOXL", 2, reason="TIER1_STOP")
    assert order_id == "998877"
    mock_rest.place_order.assert_called_once()


def test_execute_market_sell_allows_intraday_exit_under_margin_framework(mock_rest, cfg):
    lg = SettlementLedger(D("1000"))
    om = OrderManager(mock_rest, lg, cfg)
    om.initialize_firewall()

    # Adopt position (GFV exempt under margin rules)
    from core.ledger import next_business_day, today_et
    tomorrow = next_business_day(today_et())
    lg.adopt_position(
        "TNA", 10, "50", simulated=False, opened_at=FRIDAY_NOON,
        funded_with_settled=False, settle_date=tomorrow,
    )

    # Intraday sale succeeds under FINRA 4210 margin framework without GFV blocks
    order_id = om.execute_market_sell("TNA", 10, reason="TIER1_STOP")
    assert order_id == "998877"
    assert mock_rest.place_order.called


def test_execute_market_sell_allows_eod_mandatory_flatten_under_margin_framework(mock_rest, cfg):
    from core.ledger import next_business_day, today_et
    lg = SettlementLedger(D("1000"))
    om = OrderManager(mock_rest, lg, cfg)
    om.initialize_firewall()

    tomorrow = next_business_day(today_et())
    lg.adopt_position(
        "SOXL", 5, "30", simulated=False, opened_at=FRIDAY_NOON,
        funded_with_settled=False, settle_date=tomorrow,
    )

    # EOD mandatory flatten succeeds without GFV lock
    order_id = om.execute_market_sell("SOXL", 5, reason="MANDATORY_FLATTEN")
    assert order_id == "998877"
    assert mock_rest.place_order.called


def test_execute_market_sell_defensive_veto_if_ledger_blocks(mock_rest, cfg):
    mock_ledger = MagicMock()
    mock_ledger.can_sell_position.return_value = (False, "GFV VETO: defensive block")
    om = OrderManager(mock_rest, mock_ledger, cfg)
    om.initialize_firewall()
    mock_telemetry = MagicMock()
    om.telemetry = mock_telemetry

    with pytest.raises(GoodFaithViolationBlockedError, match="GFV VETO"):
        om.execute_market_sell("TNA", 10, reason="TIER1_STOP")
    mock_rest.place_order.assert_not_called()
    mock_telemetry.record_event.assert_called_once()
