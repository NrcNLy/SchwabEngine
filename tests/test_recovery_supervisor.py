"""
tests/test_recovery_supervisor.py
=================================
Verification tests for SOXL Autonomous Recovery Pipeline (#47).
Phase 4 Local Offline Verification:
1. Pure math compute_bracket & ratchet verification.
2. QuoteGate tick validation & sanity clamps.
3. OrderManager is_essential recovery reasons verification (F5 fix).
4. LiveEngine recovery hook & adoption stop clamp (F2 & F3 fix).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, time as dtime
from decimal import Decimal
import logging
from typing import Any, Dict, Optional

import pytest
import pytz

from core.engine import EngineSettings, LiveEngine
from core.ledger import BrokerBalances, SettlementLedger
from core.runtime import EngineContext
from execution.order_manager import OrderManager
from execution.recovery_supervisor import (
    Bracket,
    QuoteGate,
    RecoveryConfig,
    RecoveryMachineState,
    RecoverySupervisor,
    compute_bracket,
    ratchet,
)
from tests.test_live_engine import FakeOrderManager, FakeStrategy, FakeSync

D = Decimal
_EDT = pytz.timezone("America/New_York")


def make_test_engine(loop: asyncio.AbstractEventLoop):
    ctx = EngineContext({}, live=True)
    ledger = SettlementLedger(D("0"), data_source="LIVE_SCHWAB")
    ledger.sync_from_broker(BrokerBalances(liquidation_value=D("3650.00"), settled_cash=D("1786.27")))
    ctx.ledgers["active"] = ledger
    ctx.broker_sync = FakeSync()
    om = FakeOrderManager()
    settings = EngineSettings(
        symbols=["SOXL"], eod=dtime(15, 50), flat_deadline=dtime(15, 55),
        tier2_stop_pct=D("0.04"), fill_timeout_s=0.2, fill_poll_s=0.01,
    )
    engine = LiveEngine(ctx, {}, ledger, settings, om, FakeStrategy(), loop)
    engine.ignore_session = True
    return engine, ctx, ledger, om


def test_compute_bracket_bounds():
    """Verify dynamic bracket U(t) clamping and L(t) anchoring."""
    cfg = RecoveryConfig(floor=99.75, trail_pct=0.025, u_min=0.05, u_max=0.08)
    basis = 110.42

    # Case 1: S0 = 103.80 -> U = 110.42 (breakeven in [108.99, 112.10])
    b1 = compute_bracket(s0=103.80, basis=basis, cfg=cfg)
    assert b1.s0 == 103.80
    assert b1.m == 103.80
    assert b1.upper == 110.42
    assert b1.lower == 101.20  # 103.80 * (1 - 0.025) = 101.205 -> 101.20
    assert b1.lower >= cfg.floor

    # Case 2: S0 = 101.50 -> Breakeven 110.42 exceeds +8.0% ($109.62). Clamped to 109.62.
    b2 = compute_bracket(s0=101.50, basis=basis, cfg=cfg)
    assert b2.upper == 109.62
    assert b2.lower == 99.75  # max(101.50 * 0.975 = 98.96, floor 99.75) = 99.75

    # Case 2b: S0 within 0.3% of floor raises FloorProximity
    from execution.recovery_supervisor import FloorProximity
    with pytest.raises(FloorProximity):
        compute_bracket(s0=100.00, basis=basis, cfg=cfg)

    # Case 3: S0 = 106.00 -> Breakeven 110.42 is below +5.0% ($111.30). Clamped to 111.30.
    b3 = compute_bracket(s0=106.00, basis=basis, cfg=cfg)
    assert b3.upper == 111.30
    assert b3.lower == 103.35


def test_ratchet_monotonicity():
    """Verify ratchet strictly raises M(t) and L(t), never lowering them."""
    cfg = RecoveryConfig(floor=99.75, trail_pct=0.025, u_min=0.05, u_max=0.08)
    b0 = Bracket(s0=103.80, m=103.80, upper=110.42, lower=101.21, u_pct=0.0638)

    # Tick higher at 106.00
    b1 = ratchet(b0, last=106.00, bid=105.95, cfg=cfg)
    assert b1.m == 106.00
    assert b1.lower == 103.35  # 106.00 * 0.975

    # Tick lower at 104.00 -> M and lower should remain unchanged
    b2 = ratchet(b1, last=104.00, bid=103.95, cfg=cfg)
    assert b2.m == 106.00
    assert b2.lower == 103.35


def test_quote_gate_validation():
    """Verify QuoteGate rejects crossed markets, excessive spreads, and crazy jumps."""
    gate = QuoteGate("SOXL", reference_price=103.60)
    now = datetime.now(timezone.utc)

    # Valid tick
    q1 = gate.validate_tick(price=103.80, bid=103.75, ask=103.85, tick_time=now)
    assert q1 is not None
    assert q1.mid == 103.80

    # Inverted crossed quote
    assert gate.validate_tick(price=103.80, bid=105.00, ask=103.00, tick_time=now) is None

    # Spread > 0.5% ($0.52 on $103.80)
    assert gate.validate_tick(price=103.80, bid=103.00, ask=104.50, tick_time=now) is None

    # Unrealistic jump (>6% jump from 103.80 -> 115.00)
    assert gate.validate_tick(price=115.00, bid=114.95, ask=115.05, tick_time=now) is None


def test_order_manager_is_essential_includes_recovery():
    """Verify F5 fix: all recovery and flush exit reasons are marked essential."""
    recovery_reasons = [
        "RECOVERY_FLOOR",
        "RECOVERY_TRAIL",
        "RECOVERY_TARGET",
        "RECOVERY_EOD",
        "EOD_FLAT",
        "IPC_EMERGENCY_LIQUIDATE",
        "MANDATORY_FLATTEN",
        "EMERGENCY",
        "API_EMERGENCY",
    ]
    for r in recovery_reasons:
        assert OrderManager.is_essential(r) is True, f"{r} must be essential to bypass daily quota."


def test_f3_fixed_hurst_h_and_pos_unbound():
    """Verify F3 fix: on_tick evaluates cleanly without NameError."""
    async def scenario():
        loop = asyncio.get_running_loop()
        engine, ctx, ledger, om = make_test_engine(loop)

        # Adopt position
        ledger.adopt_position(
            symbol="SOXL",
            quantity=18,
            entry_price=110.42,
            current_price=103.60,
            stop_price=99.75,
            target_price=118.00,
            regime="A",
            simulated=False,
        )

        from execution.risk_manager import RiskEngine
        ctx.risk_manager = RiskEngine()

        # Tick at $103.50 should evaluate without raising NameError
        engine.on_tick("SOXL", {
            "last_price": 103.50,
            "bid_price": 103.45,
            "ask_price": 103.55,
            "total_volume": 1000,
        })
        # Assert no exceptions occurred
        assert True

    asyncio.run(scenario())


def test_f2_recovery_locked_hook_in_engine():
    """Verify F2 fix: positions marked recovery_locked route to recovery supervisor."""
    async def scenario():
        loop = asyncio.get_running_loop()
        engine, ctx, ledger, om = make_test_engine(loop)

        # Adopt position with recovery_locked = True
        ledger.adopt_position(
            symbol="SOXL",
            quantity=18,
            entry_price=110.42,
            current_price=103.60,
            stop_price=99.75,
            target_price=None,
            regime="RECOVERY",
            simulated=False,
            recovery_locked=True,
        )

        class MockRecovery:
            def __init__(self):
                self.ticks = []
            def on_tick(self, symbol, price, bid_price, ask_price, volume=0, **kwargs):
                self.ticks.append((symbol, price, bid_price, ask_price))

        mock_rec = MockRecovery()
        engine.recovery = mock_rec

        engine.on_tick("SOXL", {
            "last_price": 103.50,
            "bid_price": 103.45,
            "ask_price": 103.55,
            "total_volume": 1000,
        })

        assert len(mock_rec.ticks) == 1
        assert mock_rec.ticks[0] == ("SOXL", 103.50, 103.45, 103.55)
        # Ensure engine did NOT place any premature orders
        assert len(om.calls) == 0

    asyncio.run(scenario())
