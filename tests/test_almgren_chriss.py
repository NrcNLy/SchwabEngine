"""
tests/test_almgren_chriss.py
============================
Unit and mock tests for Almgren-Chriss Optimal Execution Router
and Adaptive Limit Pegging (#61).
"""

from __future__ import annotations

import time
from decimal import Decimal
from unittest.mock import MagicMock, patch
import pytest

from execution.almgren_chriss import (
    AdaptiveExecutionRouter,
    ExecutionSummary,
    calculate_optimal_trajectory,
    calculate_slice_trades,
    optimal_liquidation_trajectory,
    permanent_market_impact,
    temporary_market_impact,
)
from execution.order_manager import OrderManager


class FakeResponse:
    def __init__(self, headers=None, status_code=201):
        self.headers = headers or {"Location": "https://api.schwabapi.com/trader/v1/accounts/xyz123/orders/ORD-101"}
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Test 1: Optimal Trajectory Generation Ensures Zero Shares at Interval T
# ---------------------------------------------------------------------------

def test_optimal_trajectory_generation_zero_shares_at_interval_t():
    """
    Test 1:
    - Optimal trajectory generation ensures zero shares remain at interval T.
    - Slices sum exactly to total_shares.
    - Monotonically non-increasing remaining inventory curve.
    - Permanent and temporary market impact mathematical models evaluate correctly.
    """
    total_shares = 1000
    intervals = 10

    # 1. Almgren-Chriss Trajectory Calculation
    trajectory = optimal_liquidation_trajectory(
        total_shares=total_shares,
        intervals=intervals,
        risk_aversion=1e-4,
        sigma=0.02,
        eta=2.5e-5,
        tau=1.0,
    )

    # Must have length T + 1 (representing intervals 0, 1, ..., T)
    assert len(trajectory) == intervals + 1
    # Boundary conditions
    assert trajectory[0] == total_shares
    assert trajectory[intervals] == 0
    assert trajectory[-1] == 0  # Zero shares remain at interval T!

    # Monotonicity check
    for j in range(1, len(trajectory)):
        assert trajectory[j] <= trajectory[j - 1], f"Trajectory not monotonically decreasing at {j}"
        assert trajectory[j] >= 0

    # 2. Slice Trades summation
    trades = calculate_slice_trades(
        total_shares=total_shares,
        intervals=intervals,
        risk_aversion=1e-4,
        sigma=0.02,
        eta=2.5e-5,
    )
    assert len(trades) == intervals
    assert sum(trades) == total_shares
    assert all(t >= 0 for t in trades)

    # 3. High risk-aversion front-loads execution compared to risk-neutral (TWAP)
    twap_traj = optimal_liquidation_trajectory(
        total_shares=total_shares,
        intervals=intervals,
        risk_aversion=0.0,  # Pure TWAP
        sigma=0.02,
        eta=2.5e-5,
    )
    assert twap_traj[0] == total_shares
    assert twap_traj[-1] == 0
    # Front-loaded curve should have lower remaining inventory in early/mid intervals
    assert trajectory[intervals // 2] <= twap_traj[intervals // 2]

    # 4. Single interval edge case
    single_interval = optimal_liquidation_trajectory(total_shares=500, intervals=1)
    assert single_interval == [500, 0]

    # 5. Market impact model evaluation
    perm = permanent_market_impact(n=100.0, tau=5.0, gamma=2.5e-6)
    expected_perm = 2.5e-6 * (100.0 / 5.0)
    assert pytest.approx(perm, rel=1e-6) == expected_perm

    temp = temporary_market_impact(n=100.0, tau=5.0, eta=2.5e-5, spread=0.04)
    expected_temp = (2.5e-5 * (100.0 / 5.0)) + (0.04 / 2.0)
    assert pytest.approx(temp, rel=1e-6) == expected_temp


# ---------------------------------------------------------------------------
# Test 2: Adaptive Limit Pegging Handles Partial Fills and Adjusts Cancel-Replace
# ---------------------------------------------------------------------------

def test_adaptive_limit_pegging_partial_fills_and_adjusts_cancel_replace():
    """
    Test 2:
    - Adaptive limit pegging handles partial fills and adjusts cancel-replace price.
    - Slice 1 is placed at Best Bid - (0.01 * Spread).
    - Simulates partial fill after timeout: order is cancelled.
    - Updated quote is sampled: replacement order is placed at updated peg price.
    - Final state confirms 100% liquidated with attributed fill price, slippage, and duration.
    """
    mock_rest = MagicMock()
    mock_rest.get_account_numbers.return_value = [{"accountNumber": "123456015", "hashValue": "HASH_015_SAFE"}]

    # Dynamic market quotes based on cancellation state:
    # Before cancel: Bid = 100.00, Ask = 100.50 (spread = 0.50). Peg = 100.00 - (0.01 * 0.50) = 100.00
    # After cancel: Bid = 99.80, Ask = 100.00 (spread = 0.20). Peg = 99.80 - (0.01 * 0.20) = 99.80
    def mock_quotes(symbols):
        if mock_rest.cancel_order.called:
            return {"SOXL": {"quote": {"bidPrice": 99.80, "askPrice": 100.00, "totalVolume": 10000}}}
        return {"SOXL": {"quote": {"bidPrice": 100.00, "askPrice": 100.50, "totalVolume": 10000}}}

    mock_rest.get_quotes.side_effect = mock_quotes

    # Place order sequence:
    # 1st order: ORD-001 (50 shares target)
    # 2nd order: ORD-002 (30 shares remainder replacement)
    mock_rest.place_order.side_effect = [
        FakeResponse(headers={"Location": "https://api.schwabapi.com/trader/v1/accounts/xyz/orders/ORD-001"}),
        FakeResponse(headers={"Location": "https://api.schwabapi.com/trader/v1/accounts/xyz/orders/ORD-002"}),
    ]

    # Order status polling sequence:
    # 1. ORD-001 working status check during 3s window -> returns partial fill (20 filled, 30 working)
    # 2. ORD-001 post-cancel status check -> confirms 20 filled, canceled
    # 3. ORD-002 status check -> confirms remaining 30 filled @ 99.80
    def mock_get_order(account_hash, order_id):
        if order_id == "ORD-001":
            return {
                "orderId": "ORD-001",
                "status": "WORKING",
                "filledQuantity": 20,
                "price": 100.00,
                "orderActivityCollection": [
                    {
                        "activityType": "EXECUTION",
                        "executionLegs": [{"quantity": 20, "price": 100.00}],
                    }
                ],
            }
        elif order_id == "ORD-002":
            return {
                "orderId": "ORD-002",
                "status": "FILLED",
                "filledQuantity": 30,
                "price": 99.80,
                "orderActivityCollection": [
                    {
                        "activityType": "EXECUTION",
                        "executionLegs": [{"quantity": 30, "price": 99.80}],
                    }
                ],
            }
        return {}

    mock_rest.get_order.side_effect = mock_get_order
    mock_rest.cancel_order.return_value = True

    # Use fast timeouts and mock sleep to run instantly
    with patch("time.sleep", return_value=None):
        router = AdaptiveExecutionRouter(
            rest_client=mock_rest,
            max_slippage_bps=30,  # 30 bps
            slice_interval_seconds=0,
            cancel_replace_timeout=1,
            account_hash="HASH_015_SAFE",
        )

        # Force a single 50-share tranche for deterministic cancel-replace testing
        with patch.object(router, "_partition_into_tranches", return_value=[50]):
            summary: ExecutionSummary = router.liquidate_optimal(
                symbol="SOXL",
                total_shares=50,
                urgency="NORMAL",
            )

    # Verifications
    assert summary.total_shares == 50
    assert summary.filled_shares == 50
    assert summary.status == "FILLED"
    assert "ORD-001" in summary.order_ids
    assert "ORD-002" in summary.order_ids

    # Verify cancel_order was called on ORD-001
    mock_rest.cancel_order.assert_called_once_with("HASH_015_SAFE", "ORD-001")

    # Verify replacement order ORD-002 was submitted with updated price 99.80 for 30 shares
    assert mock_rest.place_order.call_count == 2
    second_call_body = mock_rest.place_order.call_args_list[1][0][1]
    assert second_call_body["orderType"] == "LIMIT"
    assert second_call_body["price"] == "99.80"
    assert second_call_body["orderLegCollection"][0]["quantity"] == 30

    # Average fill price: (20 * 100.00 + 30 * 99.80) / 50 = (2000 + 2994) / 50 = 4994 / 50 = 99.88
    assert pytest.approx(summary.average_fill_price, rel=1e-4) == 99.88
    assert summary.arrival_price == 100.00

    # Slippage: (100.00 - 99.88) / 100.00 * 10000 = 12 bps
    assert pytest.approx(summary.slippage_bps, rel=1e-2) == 12.0
    assert summary.execution_duration > 0.0


# ---------------------------------------------------------------------------
# Test 3: Emergency Urgency Mode Bypasses Slicing and Fires Protective IOC
# ---------------------------------------------------------------------------

def test_emergency_urgency_mode_bypasses_slicing_and_fires_single_protective_ioc():
    """
    Test 3:
    - Emergency urgency mode bypasses slicing and fires single protective IOC block.
    - Routes single Immediate-Or-Cancel (IOC) marketable limit at Bid - $0.10.
    - Zero cancellations or slicing loops.
    """
    mock_rest = MagicMock()
    mock_rest.get_account_numbers.return_value = [{"accountNumber": "987654015", "hashValue": "HASH_015_SAFE"}]

    # Quote: Bid = 80.00, Ask = 80.05
    mock_rest.get_quotes.return_value = {
        "TQQQ": {
            "quote": {
                "bidPrice": 80.00,
                "askPrice": 80.05,
                "totalVolume": 500000,
            }
        }
    }

    mock_rest.place_order.return_value = FakeResponse(
        headers={"Location": "https://api.schwabapi.com/trader/v1/accounts/xyz/orders/IOC-777"}
    )
    mock_rest.get_order.return_value = {
        "orderId": "IOC-777",
        "status": "FILLED",
        "filledQuantity": 250,
        "price": 79.90,
    }

    with patch("time.sleep", return_value=None):
        router = AdaptiveExecutionRouter(
            rest_client=mock_rest,
            account_hash="HASH_015_SAFE",
        )

        summary = router.liquidate_optimal(
            symbol="TQQQ",
            total_shares=250,
            urgency="EMERGENCY_CATASTROPHIC",
        )

    # Slicing bypassed: exactly ONE order placed
    mock_rest.place_order.assert_called_once()
    mock_rest.cancel_order.assert_not_called()

    call_body = mock_rest.place_order.call_args[0][1]
    assert call_body["orderType"] == "LIMIT"
    assert call_body["duration"] == "IMMEDIATE_OR_CANCEL"
    # Price = Bid - $0.10 = 80.00 - 0.10 = 79.90
    assert call_body["price"] == "79.90"
    assert call_body["orderLegCollection"][0]["quantity"] == 250
    assert call_body["orderLegCollection"][0]["instruction"] == "SELL"

    # Execution summary metrics
    assert summary.urgency == "EMERGENCY_CATASTROPHIC"
    assert summary.total_shares == 250
    assert summary.filled_shares == 250
    assert summary.average_fill_price == 79.90
    assert summary.order_ids == ["IOC-777"]
    assert summary.status == "FILLED"


# ---------------------------------------------------------------------------
# Test 4: Terminal Flush 15:50 EDT Falls Back to IOC if Unfilled After 60s
# ---------------------------------------------------------------------------

def test_terminal_flush_falls_back_to_ioc_after_timeout():
    """
    Test 4:
    - 15:50 EDT terminal cash flush triggers with TERMINAL_FLUSH.
    - If unfilled by 60 seconds / 15:52 EDT, router triggers immediate IOC fallback.
    """
    mock_rest = MagicMock()
    mock_rest.get_account_numbers.return_value = [{"accountNumber": "987654015", "hashValue": "HASH_015_SAFE"}]

    mock_rest.get_quotes.return_value = {
        "TNA": {"quote": {"bidPrice": 40.00, "askPrice": 40.04, "totalVolume": 100000}}
    }

    # Slice order fails to fill within 60s window
    mock_rest.place_order.side_effect = [
        FakeResponse(headers={"Location": "https://api.schwabapi.com/trader/v1/accounts/xyz/orders/SLICE-001"}),
        FakeResponse(headers={"Location": "https://api.schwabapi.com/trader/v1/accounts/xyz/orders/IOC-FALLBACK"}),
    ]

    def mock_get_order(account_hash, order_id):
        if order_id == "SLICE-001":
            if mock_rest.cancel_order.called:
                return {"orderId": "SLICE-001", "status": "CANCELED", "filledQuantity": 0, "price": 40.00}
            return {"orderId": "SLICE-001", "status": "WORKING", "filledQuantity": 0, "price": 40.00}
        elif order_id == "IOC-FALLBACK":
            return {"orderId": "IOC-FALLBACK", "status": "FILLED", "filledQuantity": 100, "price": 39.90}
        return {}

    mock_rest.get_order.side_effect = mock_get_order

    # Advance monotonic time past 60s once cancel is called
    t = [100.0]
    def mock_time():
        if mock_rest.cancel_order.called:
            t[0] += 70.0
        else:
            t[0] += 2.0
        return t[0]

    with patch("time.monotonic", side_effect=mock_time), patch("time.sleep", return_value=None):
        router = AdaptiveExecutionRouter(
            rest_client=mock_rest,
            account_hash="HASH_015_SAFE",
            cancel_replace_timeout=1,
        )

        with patch.object(router, "_partition_into_tranches", return_value=[100]):
            summary = router.liquidate_optimal(
                symbol="TNA",
                total_shares=100,
                urgency="TERMINAL_FLUSH",
            )

    assert summary.status == "FILLED"
    assert summary.filled_shares == 100
    assert "IOC-FALLBACK" in summary.order_ids

    # Verify fallback IOC order was placed at Bid - $0.10 (39.90)
    last_call_body = mock_rest.place_order.call_args_list[-1][0][1]
    assert last_call_body["duration"] == "IMMEDIATE_OR_CANCEL"
    assert last_call_body["price"] == "39.90"
    assert last_call_body["orderLegCollection"][0]["quantity"] == 100


# ---------------------------------------------------------------------------
# Test 5: Integration with OrderManager
# ---------------------------------------------------------------------------

def test_order_manager_integration_routes_via_adaptive_router():
    """
    Test 5:
    - OrderManager.execute_market_sell delegates to AdaptiveExecutionRouter.
    - OrderManager.execute_terminal_flush triggers urgency='TERMINAL_FLUSH'.
    """
    from core.ledger import SettlementLedger
    mock_rest = MagicMock()
    mock_rest.get_account_numbers.return_value = [{"accountNumber": "987654015", "hashValue": "HASH_015_SAFE"}]
    mock_rest.place_order.return_value = FakeResponse()

    cfg = {"account": {"required_suffix": "015", "blocked_keywords": ["ROBO"]}}
    lg = SettlementLedger(Decimal("1000"))
    om = OrderManager(mock_rest, lg, cfg)
    om.initialize_firewall()

    # Position funded with settled cash (cost $200 <= $330 cap)
    lg.allocate_capital("SOXL", Decimal("200"))
    lg.confirm_buy("SOXL", 20, "10.00", simulated=True, funded_with_settled=True)

    with patch.object(om, "liquidate_optimal") as mock_liquidate:
        mock_liquidate.return_value = ExecutionSummary(
            symbol="SOXL",
            total_shares=20,
            filled_shares=20,
            average_fill_price=10.00,
            arrival_price=10.00,
            slippage_bps=0.0,
            execution_duration=0.5,
            order_ids=["OM-ORDER-123"],
            urgency="TERMINAL_FLUSH",
            status="FILLED",
        )

        # 1. execute_terminal_flush
        summary = om.execute_terminal_flush("SOXL", 20)
        assert summary.status == "FILLED"
        mock_liquidate.assert_called_with(symbol="SOXL", total_shares=20, urgency="TERMINAL_FLUSH", reason="TERMINAL_FLUSH")

        # 2. execute_market_sell with EOD_FLAT maps to TERMINAL_FLUSH
        oid = om.execute_market_sell("SOXL", 20, reason="EOD_FLAT")
        assert oid == "OM-ORDER-123"

        # 3. execute_market_sell with EMERGENCY maps to EMERGENCY_CATASTROPHIC
        oid_emerg = om.execute_market_sell("SOXL", 20, reason="EMERGENCY_CATASTROPHIC")
        assert oid_emerg == "OM-ORDER-123"
