"""
tests/test_escalated_router.py
==============================
Unit & property tests for Workstream 2 (#123):
Time-and-Fill Escalated Microprice Router (15:50–15:55 EDT).

Validates:
1. Price Truncation Invariant: All limit prices emitted by `EscalatedMicropriceRouter`
   are truncated to 2 decimal places without floating-point rounding (`f"{math.floor(px * 100) / 100:.2f}"`).
2. Stage A (tau < 0.40): Volume-weighted microprice and 8.0s polling cadence.
3. Stage B (0.40 <= tau < 0.80): Quadratic spread concession toward bid proportional to
   unfilled inventory (1 - R_fill) and 4.0s polling cadence.
4. Stage C (0.80 <= tau <= 1.00): Marketable aggressive sweep crossing bid with a hard
   50 bps slippage floor and 1.5s polling cadence.
5. Strict Schwab parameter compliance: `duration="DAY"`, `session="NORMAL"`.
6. Cancel-replace >= $0.01 price divergence gate and non-blocking asyncio loop routed
   through `AsyncTokenBucketRateLimiter`.
"""

from __future__ import annotations

import asyncio
import math
from typing import List
from unittest.mock import MagicMock

import pytest

from core.rate_limiter import AsyncTokenBucketRateLimiter
from execution.escalated_router import (
    EscalatedMicropriceRouter,
    EscalationStage,
    QuoteSnapshot,
    format_truncated_limit_price,
)


@pytest.mark.parametrize(
    "raw_px,expected_str",
    [
        (28.559, "28.55"),
        (28.559999, "28.55"),
        (28.551, "28.55"),
        (28.550, "28.55"),
        (41.999, "41.99"),
        (10.009, "10.00"),
        (53.1287, "53.12"),
    ],
)
def test_price_truncation_without_floating_point_rounding(raw_px: float, expected_str: str) -> None:
    """
    Verification Specification (#123 Section 6.1):
    Assert all limit prices emitted by `EscalatedMicropriceRouter` are truncated to
    2 decimal places without floating-point rounding (`f"{math.floor(px * 100) / 100:.2f}"`).
    """
    truncated = format_truncated_limit_price(raw_px)
    assert truncated == expected_str
    assert len(truncated.split(".")[1]) == 2

    router = EscalatedMicropriceRouter()
    payload = router.build_schwab_sell_limit_payload("SOXL", 139, raw_px)
    assert payload["price"] == expected_str
    assert payload["duration"] == "DAY"
    assert payload["session"] == "NORMAL"
    assert payload["orderType"] == "LIMIT"


def test_stage_a_passive_microprice_and_cadence() -> None:
    """Validate Stage A (tau < 0.40) volume-weighted microprice and 8.0s cadence."""
    router = EscalatedMicropriceRouter()
    # P_bid = 28.50, P_ask = 28.60, Q_bid = 300, Q_ask = 100
    # P_micro = (28.50 * 100 + 28.60 * 300) / 400 = 28.575 -> truncated to "28.57"
    step = router.evaluate_step(
        tau=0.20,
        filled_qty=0,
        target_qty=100,
        bid_price=28.50,
        ask_price=28.60,
        bid_size=300.0,
        ask_size=100.0,
    )
    assert step.stage == EscalationStage.STAGE_A_PASSIVE_MICROPRICE
    assert step.microprice == pytest.approx(28.575)
    assert step.truncated_price_str == "28.57"
    assert step.polling_cadence_sec == 8.0


def test_stage_b_quadratic_spread_concession_and_cadence() -> None:
    """
    Validate Stage B (0.40 <= tau < 0.80):
    P_limit(t) = P_micro(t) - S(t) * (1 - R_fill(t)) * ((tau(t) - 0.40) / 0.40)^2
    and 4.0s polling cadence.
    """
    router = EscalatedMicropriceRouter()
    # P_bid = 30.00, P_ask = 30.20 (S = 0.20), equal sizes -> P_micro = 30.10
    # tau = 0.60 -> ((0.60 - 0.40) / 0.40)^2 = 0.25
    # R_fill = 20 / 100 = 0.20 -> (1 - R_fill) = 0.80
    # concession = 0.20 * 0.80 * 0.25 = 0.04 -> raw_px = 30.10 - 0.04 = 30.06
    step = router.evaluate_step(
        tau=0.60,
        filled_qty=20,
        target_qty=100,
        bid_price=30.00,
        ask_price=30.20,
        bid_size=100.0,
        ask_size=100.0,
    )
    assert step.stage == EscalationStage.STAGE_B_SPREAD_CONCESSION
    assert step.raw_limit_price == pytest.approx(30.06)
    assert step.truncated_price_str == "30.06"
    assert step.polling_cadence_sec == 4.0


def test_stage_c_marketable_aggressive_sweep_with_50bps_floor() -> None:
    """
    Validate Stage C (0.80 <= tau <= 1.00):
    P_limit(t) = max(P_bid(t) - 0.02, P_bid(t) * (1 - MaxSlippage))
    where MaxSlippage = 50 bps (0.0050), polling cadence = 1.5s.
    """
    router = EscalatedMicropriceRouter()
    # Case 1: P_bid = 30.00 -> P_bid - 0.02 = 29.98, P_bid * 0.995 = 29.85 -> max is 29.98
    step_high_px = router.evaluate_step(
        tau=0.85,
        filled_qty=50,
        target_qty=100,
        bid_price=30.00,
        ask_price=30.08,
    )
    assert step_high_px.stage == EscalationStage.STAGE_C_AGGRESSIVE_SWEEP
    assert step_high_px.raw_limit_price == pytest.approx(29.98)
    assert step_high_px.truncated_price_str == "29.98"
    assert step_high_px.polling_cadence_sec == 1.5

    # Case 2: Low-priced asset P_bid = 3.00 -> P_bid - 0.02 = 2.98, P_bid * 0.995 = 2.985 -> max is 2.985 -> "2.98"
    step_low_px = router.evaluate_step(
        tau=0.90,
        filled_qty=50,
        target_qty=100,
        bid_price=3.00,
        ask_price=3.05,
    )
    assert step_low_px.raw_limit_price == pytest.approx(2.985)
    assert step_low_px.truncated_price_str == "2.98"


def test_cancel_replace_only_on_one_cent_or_greater_divergence() -> None:
    """Verify cancel-replace is suppressed if new limit price differs by < $0.01."""
    assert EscalatedMicropriceRouter.should_cancel_replace(28.55, 28.55) is False
    assert EscalatedMicropriceRouter.should_cancel_replace(28.55, 28.554) is False
    assert EscalatedMicropriceRouter.should_cancel_replace(28.55, 28.54) is True
    assert EscalatedMicropriceRouter.should_cancel_replace(28.55, 28.56) is True


def test_async_liquidate_position_end_to_end() -> None:
    """
    Validate full Stage A -> Stage B -> Stage C progression inside the asyncio loop,
    verifying cancel-replace gating, rate-limiter usage, and slippage < 50 bps.
    """
    async def _run() -> None:
        limiter = AsyncTokenBucketRateLimiter(burst_capacity=120.0, sustained_rate=100.0, daily_ceiling=3500)
        rest_client = MagicMock()
        order_seq = {"n": 0}

        def _place_order(acct_hash: str, payload: dict) -> dict:
            order_seq["n"] += 1
            assert payload["duration"] == "DAY"
            assert payload["session"] == "NORMAL"
            return {"orderId": f"ORD-{order_seq['n']}"}

        rest_client.place_order.side_effect = _place_order
        rest_client.cancel_order.return_value = True

        router = EscalatedMicropriceRouter(
            rest_client=rest_client,
            account_hash="HASH4015",
            rate_limiter=limiter,
            window_duration_seconds=300.0,
        )

        virtual_clock = {"t": 0.0}
        cadences_recorded: List[float] = []

        async def fake_sleep(sec: float) -> None:
            cadences_recorded.append(sec)
            virtual_clock["t"] += 65.0  # advance across Stage A -> Stage B -> Stage C

        def quote_provider(sym: str, tau: float) -> QuoteSnapshot:
            return QuoteSnapshot(
                symbol=sym,
                bid_price=28.50,
                ask_price=28.56,
                bid_size=200.0,
                ask_size=100.0,
            )

        def sim_fill(step, rem_qty: int, quote: QuoteSnapshot):
            if step.stage == EscalationStage.STAGE_A_PASSIVE_MICROPRICE:
                return (39, step.truncated_price) if rem_qty == 139 else (0, step.truncated_price)
            if step.stage == EscalationStage.STAGE_B_SPREAD_CONCESSION:
                return (50, step.truncated_price) if rem_qty == 100 else (0, step.truncated_price)
            if step.stage == EscalationStage.STAGE_C_AGGRESSIVE_SWEEP:
                return (rem_qty, step.truncated_price)
            return (0, step.truncated_price)

        report = await router.liquidate_position(
            symbol="SOXL",
            target_quantity=139,
            quote_provider=quote_provider,
            clock_fn=lambda: virtual_clock["t"],
            sleep_fn=fake_sleep,
            simulated_fill_fn=sim_fill,
        )

        assert report.completed is True
        assert report.filled_quantity == 139
        assert report.remaining_quantity == 0
        assert report.slippage_bps <= 50.0
        assert "STAGE_A" in report.stages_visited
        assert "STAGE_B" in report.stages_visited
        assert "STAGE_C" in report.stages_visited
        assert 8.0 in cadences_recorded
        assert 4.0 in cadences_recorded
        assert limiter.daily_count >= 1

    asyncio.run(_run())
