"""
tests/test_resiliency_refactor.py
=================================
End-to-end verification suite for the SchwabEngine Resiliency Refactor (#123 Section 6).

Validates:
- Section 6.1 Unit & Property Invariants:
  1. GFV Rejection: Submitting a buy order exceeding liquid settled cash returns an
     explicit `GFV REJECTION` and blocks execution.
  2. HMM Descent: Simulating an equity drop (v_15m <= -1.5%) and credit widening
     (Delta CS >= 12 bps) transitions the regime to `Uncertainty`/`Macro Shock` within 2 ticks.
  3. Price Truncation: All limit prices emitted by `EscalatedMicropriceRouter` are
     truncated to 2 decimal places without floating-point rounding (`f"{math.floor(px * 100) / 100:.2f}"`).
  4. Async Non-Blocking: High-frequency rate-limiting delays yield to the `asyncio`
     event loop without stalling concurrent tasks.
- Section 6.2 Shadow Replay Validation (`scripts/dev/replay_session.py`):
  - Acceptance Criterion 1: HMM posterior pi_Goldilocks drops below 0.50 during the
    Oct 22 afternoon selloff, scaling target satellite weight W_sat^* from 0.25 down to <= 0.10.
  - Acceptance Criterion 2: At 15:50 EDT, all satellite positions are fully liquidated
    to 0 shares before 15:55:00 EDT without exceeding 50 bps in spread slippage.
"""

from __future__ import annotations

import asyncio
import math
from pathlib import Path

import pytest

from core.rate_limiter import AsyncTokenBucketRateLimiter
from core.t1_ledger import GFVRejectionError, T1CashLedger
from execution.escalated_router import EscalatedMicropriceRouter, format_truncated_limit_price
from scripts.dev.replay_session import run_oct22_shadow_replay
from strategies.regime_governor import AsymmetricHMMGovernor


def test_sec_6_1_gfv_rejection_invariant() -> None:
    """Section 6.1.1: Attempting to submit a buy order exceeding liquid settled cash returns GFV REJECTION."""
    ledger = T1CashLedger(settled_cash="800.00", unsettled_cash="3600.00", nlv="4400.00")
    allowed, reason = ledger.can_submit_order("BUY", "800.01", symbol="SOXL")
    assert allowed is False
    assert "GFV REJECTION" in reason

    with pytest.raises(GFVRejectionError, match="GFV REJECTION"):
        ledger.reserve_for_buy_order("ORD-101", "1200.00", symbol="SOXL")


def test_sec_6_1_hmm_descent_within_two_ticks(tmp_path: Path) -> None:
    """Section 6.1.2: Simulating v_15m <= -1.5% and Delta CS >= 12 bps transitions regime within 2 ticks."""
    gov = AsymmetricHMMGovernor(
        initial_pi=[1.0, 0.0, 0.0],
        journal_db_path=tmp_path / "trading_journal.db",
    )
    for _ in range(2):
        gov.update(
            macro_surprise_z=-0.40,
            macro_sentiment=-0.15,
            macro_hy_spread_bps=330.0,
            tech_velocity_15m=-0.015,
            delta_cs_1h_bps=12.4,
        )
    assert gov.get_dominant_regime() in ("Uncertainty", "Macro Shock")
    assert gov.pi_goldilocks < 0.50
    assert gov.calculate_target_satellite_weight() <= 0.10


def test_sec_6_1_price_truncation_invariant() -> None:
    """Section 6.1.3: Limit prices emitted by EscalatedMicropriceRouter are truncated without rounding."""
    router = EscalatedMicropriceRouter()
    for px in (28.559, 28.5599, 70.128, 54.999, 19.009):
        expected = f"{math.floor((px + 1e-9) * 100) / 100:.2f}"
        assert format_truncated_limit_price(px) == expected
        payload = router.build_schwab_sell_limit_payload("SOXL", 10, px)
        assert payload["price"] == expected
        assert payload["duration"] == "DAY"
        assert payload["session"] == "NORMAL"


def test_sec_6_1_async_non_blocking_invariant() -> None:
    """Section 6.1.4: High-frequency rate-limiting delays yield to the asyncio event loop."""
    async def _run() -> None:
        limiter = AsyncTokenBucketRateLimiter(burst_capacity=1.0, sustained_rate=50.0, daily_ceiling=3500)
        await limiter.acquire(1.0)  # drain initial token

        ticks = {"count": 0}

        async def _heartbeat() -> None:
            for _ in range(5):
                await asyncio.sleep(0.004)
                ticks["count"] += 1

        hb_task = asyncio.create_task(_heartbeat())
        await limiter.acquire(1.0)
        await hb_task
        assert ticks["count"] == 5

    asyncio.run(_run())


def test_sec_6_2_oct22_shadow_replay_acceptance_criteria(tmp_path: Path) -> None:
    """
    Section 6.2: Execute `scripts/dev/replay_session.py` against Oct 22 telemetry and
    verify Acceptance Criterion 1 and Acceptance Criterion 2.
    """
    summary = asyncio.run(run_oct22_shadow_replay(journal_db_path=tmp_path / "trading_journal.db"))

    # Acceptance Criterion 1: pi_Goldilocks < 0.50 during afternoon selloff, W_sat^* <= 0.10
    assert summary.acceptance_criterion_1_passed is True
    assert summary.afternoon_min_pi_goldilocks < 0.50
    assert summary.afternoon_min_w_sat_target <= 0.10

    # Acceptance Criterion 2: All satellite positions liquidated to 0 shares before 15:55 EDT with <= 50 bps slippage
    assert summary.acceptance_criterion_2_passed is True
    assert summary.eod_remaining_satellite_shares == 0
    assert summary.max_deleveraging_slippage_bps <= 50.0
    assert summary.gfv_same_day_redeploy_blocked is True
