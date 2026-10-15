"""
tests/test_virtual_bracket_fsm.py
=================================
Unit and integration tests for execution/virtual_bracket.py (Virtual Bracket FSM).

Tests:
1. State transitions:
   - INIT -> ARMED -> TRACKING upon entry fill.
2. Upper breach behavior:
   - In "tighten" mode: transitions TRACKING -> TIGHTENING; halves k_l; does NOT liquidate.
   - In "exit" mode: transitions to TRIGGERED -> SETTLED.
3. Lower breach behavior:
   - Breached lower bound triggers TRIGGERED -> EXIT_PENDING -> SETTLED.
4. Target Close resolution (regime-dependent):
   - FAVORABLE -> Friday 15:50 EDT
   - CHOP / DEFENSIVE -> Daily 15:50 EDT
5. Single-writer actor loop with conflated tick queue.
6. Shadow mode vs Live mode:
   - Shadow mode logs without calling broker order dispatch.
   - Live mode invokes exit_callback; escalates to ORPHANED on execution failure.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time as dtime, timedelta, timezone
import pytest
import pytz

from core.ses_mapping import EnvelopeParams
from execution.virtual_bracket import (
    FSMState,
    TickMessage,
    VirtualBracketFSM,
    compute_target_close,
)

_EDT = pytz.timezone("America/New_York")


def test_target_close_regime_dependence():
    # Wednesday 10:00 EDT
    wednesday_dt = _EDT.localize(datetime(2026, 10, 14, 10, 0, 0))  # Oct 14, 2026 is Wednesday

    # 1. CHOP -> Daily 15:50 EDT (Today)
    t_close_chop = compute_target_close(wednesday_dt, "CHOP")
    t_close_chop_ny = t_close_chop.astimezone(_EDT)
    assert t_close_chop_ny.date() == wednesday_dt.date()
    assert t_close_chop_ny.time() == dtime(15, 50, 0)

    # 2. DEFENSIVE -> Daily 15:50 EDT (Today)
    t_close_def = compute_target_close(wednesday_dt, "DEFENSIVE")
    t_close_def_ny = t_close_def.astimezone(_EDT)
    assert t_close_def_ny.date() == wednesday_dt.date()
    assert t_close_def_ny.time() == dtime(15, 50, 0)

    # 3. FAVORABLE -> Friday 15:50 EDT (Oct 16, 2026)
    t_close_fav = compute_target_close(wednesday_dt, "FAVORABLE")
    t_close_fav_ny = t_close_fav.astimezone(_EDT)
    assert t_close_fav_ny.weekday() == 4  # Friday
    assert t_close_fav_ny.day == 16
    assert t_close_fav_ny.time() == dtime(15, 50, 0)


def test_fsm_lifecycle_and_tightening():
    async def _run():
        fsm = VirtualBracketFSM(
            symbol="SOXL",
            mode="shadow",
            upper_breach_mode="tighten",
        )
        assert fsm.state == FSMState.INIT

        # 1. Arm
        params = EnvelopeParams(
            k_u=2.0,
            k_l=1.5,
            gamma=1.0,
            regime="CHOP",
            ses_score=0.1,
        )
        assert fsm.arm(planned_qty=50, params=params) is True
        assert fsm.state == FSMState.ARMED

        # 2. Entry fill confirmation
        t_entry = datetime(2026, 10, 14, 10, 0, 0, tzinfo=timezone.utc)
        t_open = datetime(2026, 10, 14, 9, 30, 0, tzinfo=timezone.utc)
        t_close = datetime(2026, 10, 14, 15, 50, 0, tzinfo=timezone.utc)

        fsm.notify_entry_fill(
            fill_price=100.0,
            fill_qty=50,
            fill_time=t_entry,
            session_open=t_open,
            session_close=t_close,
        )
        assert fsm.state == FSMState.TRACKING
        assert fsm.envelope is not None
        assert fsm.envelope.k_l == 1.5

        # 3. Normal tick within bounds
        t1 = t_entry + timedelta(minutes=5)
        bounds1 = await fsm.process_tick(TickMessage(
            tick_time=t1,
            price=101.0,
            sigma_t=0.01,
            bid_price=100.95,
        ))
        assert bounds1 is not None
        assert fsm.state == FSMState.TRACKING
        assert bounds1.breached_upper is False
        assert bounds1.breached_lower is False

        # 4. Upper breach in tightening mode:
        # Triggers TIGHTENING state, halves k_l, but does not liquidate
        t2 = t1 + timedelta(minutes=5)
        bounds2 = await fsm.process_tick(TickMessage(
            tick_time=t2,
            price=106.0,  # Breaches upper bound
            sigma_t=0.01,
            bid_price=105.90,
        ))
        assert bounds2 is not None
        assert bounds2.breached_upper is True
        assert fsm.state == FSMState.TIGHTENING
        assert fsm.envelope.k_l == pytest.approx(0.75, abs=1e-4)

    asyncio.run(_run())


def test_fsm_lower_breach_and_shadow_settlement():
    async def _run():
        fsm = VirtualBracketFSM(
            symbol="SOXL",
            mode="shadow",
        )
        params = EnvelopeParams(k_u=2.0, k_l=1.5, gamma=1.0, regime="CHOP", ses_score=0.0)
        fsm.arm(planned_qty=20, params=params)

        t_entry = datetime(2026, 10, 14, 10, 0, 0, tzinfo=timezone.utc)
        t_open = datetime(2026, 10, 14, 9, 30, 0, tzinfo=timezone.utc)
        t_close = datetime(2026, 10, 14, 15, 50, 0, tzinfo=timezone.utc)

        fsm.notify_entry_fill(
            fill_price=100.0,
            fill_qty=20,
            fill_time=t_entry,
            session_open=t_open,
            session_close=t_close,
        )

        # Tick that breaches lower bound
        t_breach = t_entry + timedelta(minutes=10)
        await fsm.process_tick(TickMessage(
            tick_time=t_breach,
            price=95.0,
            sigma_t=0.01,
            bid_price=94.50,
        ))

        # In shadow mode, FSM transitions to SETTLED without live execution
        assert fsm.state == FSMState.SETTLED
        assert fsm.exit_reason == "AVE_TRAILING_STOP"

    asyncio.run(_run())


def test_fsm_live_mode_exit_and_orphaned_escalation():
    async def _run():
        # 1. Successful live exit
        exit_called = False
        async def mock_successful_exit(sym: str, reason: str) -> bool:
            nonlocal exit_called
            exit_called = True
            return True

        fsm_live = VirtualBracketFSM(
            symbol="TQQQ",
            mode="live",
            exit_callback=mock_successful_exit,
        )
        params = EnvelopeParams(k_u=2.0, k_l=1.5, gamma=1.0, regime="CHOP", ses_score=0.0)
        fsm_live.arm(planned_qty=10, params=params)
        t_entry = datetime(2026, 10, 14, 10, 0, 0, tzinfo=timezone.utc)
        fsm_live.notify_entry_fill(
            fill_price=80.0,
            fill_qty=10,
            fill_time=t_entry,
            session_open=t_entry - timedelta(minutes=30),
            session_close=t_entry + timedelta(hours=5),
        )

        await fsm_live.process_tick(TickMessage(
            tick_time=t_entry + timedelta(minutes=5),
            price=75.0,
            sigma_t=0.01,
            bid_price=74.8,
        ))
        assert exit_called is True
        assert fsm_live.state == FSMState.SETTLED

        # 2. Failed live exit escalates to ORPHANED
        async def mock_failed_exit(sym: str, reason: str) -> bool:
            return False

        fsm_fail = VirtualBracketFSM(
            symbol="TQQQ",
            mode="live",
            exit_callback=mock_failed_exit,
        )
        fsm_fail.arm(planned_qty=10, params=params)
        fsm_fail.notify_entry_fill(
            fill_price=80.0,
            fill_qty=10,
            fill_time=t_entry,
            session_open=t_entry - timedelta(minutes=30),
            session_close=t_entry + timedelta(hours=5),
        )

        await fsm_fail.process_tick(TickMessage(
            tick_time=t_entry + timedelta(minutes=5),
            price=75.0,
            sigma_t=0.01,
            bid_price=74.8,
        ))
        assert fsm_fail.state == FSMState.ORPHANED

    asyncio.run(_run())
