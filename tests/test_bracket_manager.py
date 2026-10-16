"""
tests/test_bracket_manager.py
=============================
Unit Test Verification for Multi-Tranche Profit Scaling & Virtual Ratchet Bracket Manager (#62).

Test 1: Verify Tranche 1 execution automatically ratchets remaining stop to breakeven.
Test 2: Verify price reversal after Tranche 1 exits remaining shares at breakeven with positive net PnL.
Test 3: Verify 15:50 EDT sweep flushes partial positions cleanly.
Additional tests:
- Tranche 2 (+4.5%) locks stop to Tranche 1 target price (+2.5%) for Runner.
- Runner trails dynamic Yang-Zhang envelope.
- Initial catastrophic stop protects full size before Tranche 1.
- SQLite trades.db schema migration and tranche progression recording.
- RiskEngine bracket lifecycle integration.
"""

from datetime import datetime
from zoneinfo import ZoneInfo
import pytest

from execution.bracket_manager import MultiTrancheBracket, ActionType
from execution.risk_manager import RiskEngine
from core.trade_store import SQLiteTradeStore

_EDT = ZoneInfo("America/New_York")


def test_tranche_1_ratchet_to_breakeven():
    """
    Test 1: Verify Tranche 1 execution automatically ratchets remaining stop to breakeven.
    - Initial Stop: Entry - 1.8% ($98.20 on $100.00 entry).
    - Tranche 1 Trigger: Entry + 2.5% ($102.50).
    - Stop on remaining 65% of shares moves to Breakeven (Entry + $0.02 = $100.02).
    - Trade marked as 'Risk-Free' and 'breakeven_latched'.
    """
    entry_price = 100.00
    total_shares = 100
    bracket = MultiTrancheBracket(
        symbol="SOXL",
        total_shares=total_shares,
        entry_price=entry_price,
        s0_reference=entry_price,
    )

    # Initial state assertions
    assert bracket.remaining_shares == 100
    assert bracket.initial_stop_price == 98.20
    assert bracket.current_stop_price == 98.20
    assert bracket.breakeven_stop_price == 100.02
    assert bracket.tranche_1_filled is False
    assert bracket.tranche_2_filled is False
    assert bracket.breakeven_latched is False
    assert bracket.is_risk_free is False

    # Shares allocation check: 35% / 35% / 30%
    assert bracket.tranches[1]["shares"] == 35
    assert bracket.tranches[1]["target_price"] == 102.50
    assert bracket.tranches[2]["shares"] == 35
    assert bracket.tranches[2]["target_price"] == 104.50
    assert bracket.tranches[3]["shares"] == 30

    # Advance price below Tranche 1 (no trigger)
    t1 = datetime(2026, 10, 15, 10, 0, 0, tzinfo=_EDT)
    actions = bracket.update_state(101.50, t1)
    assert len(actions) == 0
    assert bracket.tranche_1_filled is False
    assert bracket.current_stop_price == 98.20

    # Trigger Tranche 1 Fill (+2.5% = $102.50)
    t2 = datetime(2026, 10, 15, 10, 15, 0, tzinfo=_EDT)
    actions = bracket.update_state(102.50, t2)

    # Assertions on state transitions
    assert bracket.tranche_1_filled is True
    assert bracket.breakeven_latched is True
    assert bracket.is_risk_free is True
    assert bracket.remaining_shares == 65
    assert bracket.current_stop_price == 100.02  # Ratcheted to breakeven + $0.02

    # Assert actions emitted: SELL Tranche 1 and UPDATE_STOP
    sell_actions = [a for a in actions if a.action_type == ActionType.SELL.value]
    stop_actions = [a for a in actions if a.action_type == ActionType.UPDATE_STOP.value]

    assert len(sell_actions) == 1
    assert sell_actions[0].shares == 35
    assert sell_actions[0].price == 102.50
    assert sell_actions[0].reason == "TRANCHE_1_TARGET"
    assert sell_actions[0].tranche_id == 1

    assert len(stop_actions) == 1
    assert stop_actions[0].stop_price == 100.02
    assert stop_actions[0].shares == 65
    assert stop_actions[0].reason == "RATCHET_BREAKEVEN"


def test_price_reversal_breakeven_positive_net_pnl():
    """
    Test 2: Verify price reversal after Tranche 1 exits remaining shares at breakeven with positive net PnL.
    - Tranche 1 sells 35 shares @ $102.50 -> Realized PnL: +$87.50.
    - Stop on remaining 65 shares is at $100.02.
    - Price reverses down to $100.02.
    - Breakeven stop triggers, exiting remaining 65 shares @ $100.02 -> Realized PnL: +$1.30.
    - Total Net PnL = $87.50 + $1.30 = +$88.80 > 0.
    """
    bracket = MultiTrancheBracket(
        symbol="SOXL",
        total_shares=100,
        entry_price=100.00,
    )

    t1 = datetime(2026, 10, 15, 10, 5, 0, tzinfo=_EDT)
    # Fill Tranche 1
    actions1 = bracket.update_state(102.50, t1)
    assert bracket.tranche_1_filled is True
    assert bracket.remaining_shares == 65
    assert bracket.realized_pnl == pytest.approx(87.50, rel=1e-3)

    # Price reverses down to Breakeven Stop ($100.02)
    t2 = datetime(2026, 10, 15, 10, 20, 0, tzinfo=_EDT)
    actions2 = bracket.update_state(100.02, t2)

    assert len(actions2) == 1
    exit_action = actions2[0]
    assert exit_action.action_type == ActionType.SELL.value
    assert exit_action.shares == 65
    assert exit_action.price == 100.02
    assert exit_action.reason == "BREAKEVEN_STOP"

    # All shares are exited
    assert bracket.remaining_shares == 0
    assert bracket.is_closed is True

    # Total Net PnL is strictly positive!
    expected_pnl = 35 * (102.50 - 100.00) + 65 * (100.02 - 100.00)
    assert bracket.realized_pnl == pytest.approx(expected_pnl, rel=1e-3)
    assert bracket.realized_pnl > 0.0
    assert bracket.realized_pnl == pytest.approx(88.80, rel=1e-2)


def test_terminal_flush_at_1550_edt_partial():
    """
    Test 3: Verify 15:50 EDT sweep flushes partial positions cleanly.
    - Position partially scaled out at Tranche 1 (65 shares remaining).
    - Clock hits 15:50:00 EDT.
    - Terminal flush liquidates remaining 65 shares to guarantee 100% cash clearance.
    """
    bracket = MultiTrancheBracket(
        symbol="TQQQ",
        total_shares=100,
        entry_price=50.00,
    )

    t1 = datetime(2026, 10, 15, 11, 0, 0, tzinfo=_EDT)
    bracket.update_state(51.25, t1)  # Entry + 2.5% on $50.00 = $51.25
    assert bracket.tranche_1_filled is True
    assert bracket.remaining_shares == 65

    # 15:50:00 EDT Terminal Flush
    t_flush = datetime(2026, 10, 15, 15, 50, 0, tzinfo=_EDT)
    actions = bracket.update_state(50.80, t_flush)

    assert len(actions) == 1
    flush_act = actions[0]
    assert flush_act.action_type == ActionType.SELL.value
    assert flush_act.shares == 65
    assert flush_act.price == 50.80
    assert flush_act.reason == "TERMINAL_FLUSH"

    assert bracket.remaining_shares == 0
    assert bracket.terminal_flushed is True
    assert bracket.is_closed is True


def test_terminal_flush_unfilled_position():
    """
    Verify 15:50 EDT sweep cleanly flushes an entire position if no tranches filled.
    """
    bracket = MultiTrancheBracket(
        symbol="TNA",
        total_shares=50,
        entry_price=40.00,
    )
    assert bracket.remaining_shares == 50

    t_flush = datetime(2026, 10, 15, 15, 50, 1, tzinfo=_EDT)
    actions = bracket.update_state(39.80, t_flush)

    assert len(actions) == 1
    assert actions[0].shares == 50
    assert actions[0].reason == "TERMINAL_FLUSH"
    assert bracket.remaining_shares == 0
    assert bracket.terminal_flushed is True


def test_tranche_2_locks_runner_stop():
    """
    Verify Tranche 2 Fill (+4.5%):
    - Sells Tranche 2 (35 shares).
    - Locks trailing stop on Runner (Tranche 3) to at least Tranche 1 target price (+2.5%).
    - When price reverses to Tranche 1 target price, Runner exits with locked gains.
    """
    bracket = MultiTrancheBracket(
        symbol="SOXL",
        total_shares=100,
        entry_price=100.00,
    )

    t1 = datetime(2026, 10, 15, 10, 0, 0, tzinfo=_EDT)
    bracket.update_state(102.50, t1)
    assert bracket.tranche_1_filled is True
    assert bracket.current_stop_price == 100.02

    # Trigger Tranche 2 Fill (+4.5% = $104.50)
    t2 = datetime(2026, 10, 15, 10, 30, 0, tzinfo=_EDT)
    actions2 = bracket.update_state(104.50, t2)

    assert bracket.tranche_2_filled is True
    assert bracket.remaining_shares == 30  # Tranche 3 Runner remaining
    # Stop on Runner locked to at least Tranche 1 target price ($102.50)
    assert bracket.current_stop_price == 102.50

    stop_actions = [a for a in actions2 if a.action_type == ActionType.UPDATE_STOP.value]
    assert len(stop_actions) == 1
    assert stop_actions[0].stop_price == 102.50
    assert stop_actions[0].reason == "LOCK_RUNNER_STOP"

    # Price reverses to $102.50
    t3 = datetime(2026, 10, 15, 11, 0, 0, tzinfo=_EDT)
    actions3 = bracket.update_state(102.50, t3)

    assert len(actions3) == 1
    assert actions3[0].action_type == ActionType.SELL.value
    assert actions3[0].shares == 30
    assert actions3[0].price == 102.50
    assert actions3[0].reason == "RUNNER_TRAILING_STOP"
    assert bracket.remaining_shares == 0

    # Total Net PnL calculation:
    # T1: 35 * 2.50 = 87.50
    # T2: 35 * 4.50 = 157.50
    # T3: 30 * 2.50 = 75.00
    # Total = 320.00
    assert bracket.realized_pnl == pytest.approx(320.00, rel=1e-2)


def test_runner_yang_zhang_dynamic_envelope_ratchet():
    """
    Verify Runner dynamic Yang-Zhang volatility trailing envelope ratchets stop upward.
    """
    bracket = MultiTrancheBracket(
        symbol="SOXL",
        total_shares=100,
        entry_price=100.00,
    )
    t1 = datetime(2026, 10, 15, 10, 0, 0, tzinfo=_EDT)
    bracket.update_state(102.50, t1)
    t2 = datetime(2026, 10, 15, 10, 15, 0, tzinfo=_EDT)
    bracket.update_state(104.50, t2)
    assert bracket.current_stop_price == 102.50

    # High price excursion with moderate YZ volatility
    t3 = datetime(2026, 10, 15, 10, 45, 0, tzinfo=_EDT)
    actions = bracket.update_state(108.00, t3, yz_vol=0.012)
    # Stop should ratchet upward above $102.50
    assert bracket.high_water_mark == 108.00
    assert bracket.current_stop_price > 102.50


def test_initial_stop_loss_trigger():
    """
    Verify initial stop loss triggers at Entry - 1.8% when price declines immediately.
    """
    bracket = MultiTrancheBracket(
        symbol="SOXL",
        total_shares=100,
        entry_price=100.00,
    )
    assert bracket.current_stop_price == 98.20

    t1 = datetime(2026, 10, 15, 9, 35, 0, tzinfo=_EDT)
    actions = bracket.update_state(98.20, t1)

    assert len(actions) == 1
    act = actions[0]
    assert act.action_type == ActionType.SELL.value
    assert act.shares == 100
    assert act.price == 98.20
    assert act.reason == "INITIAL_STOP"
    assert bracket.remaining_shares == 0
    assert bracket.realized_pnl == pytest.approx(-180.00, rel=1e-2)


def test_trades_db_schema_migration_and_progression(tmp_path):
    """
    Verify state/trades.db schema includes tranche_1_filled, tranche_2_filled, breakeven_latched
    and records progression cleanly.
    """
    db_path = tmp_path / "test_trades.db"
    store = SQLiteTradeStore(db_path=db_path)

    # Record trade
    now = datetime.now(_EDT)
    tid = store.record_trade(
        symbol="SOXL",
        side="BUY",
        quantity=100,
        price=100.0,
        cost_basis=10000.0,
        timestamp=now,
        simulated=False,
        tranche_1_filled=False,
        tranche_2_filled=False,
        breakeven_latched=False,
    )

    trades = store.fetch_trades_today()
    assert len(trades) == 1
    assert trades[0]["tranche_1_filled"] == 0
    assert trades[0]["tranche_2_filled"] == 0
    assert trades[0]["breakeven_latched"] == 0

    # Record tranche progression
    store.record_tranche_progression(
        symbol="SOXL",
        tranche_1_filled=True,
        tranche_2_filled=False,
        breakeven_latched=True,
        trade_id=tid,
    )

    updated_trades = store.fetch_trades_today()
    assert len(updated_trades) == 1
    assert updated_trades[0]["tranche_1_filled"] == 1
    assert updated_trades[0]["tranche_2_filled"] == 0
    assert updated_trades[0]["breakeven_latched"] == 1


def test_risk_engine_bracket_integration(tmp_path):
    """
    Verify RiskEngine creates, updates, and tracks MultiTrancheBracket instances.
    """
    db_path = tmp_path / "test_trades_risk.db"
    store = SQLiteTradeStore(db_path=db_path)
    risk_engine = RiskEngine()

    now = datetime.now(_EDT)
    store.record_trade(
        symbol="SOXL",
        side="BUY",
        quantity=100,
        price=100.0,
        cost_basis=10000.0,
        timestamp=now,
        simulated=False,
    )

    bracket = risk_engine.create_breakout_bracket(
        symbol="SOXL",
        total_shares=100,
        entry_price=100.0,
        trade_store=store,
    )

    assert bracket is not None
    assert risk_engine.get_bracket("SOXL") is bracket

    # Update bracket state through RiskEngine
    t1 = datetime(2026, 10, 15, 10, 15, 0, tzinfo=_EDT)
    actions = risk_engine.update_bracket_state(
        "SOXL",
        102.50,
        t1,
        trade_store=store,
    )

    assert len(actions) == 2
    assert bracket.tranche_1_filled is True
    assert bracket.breakeven_latched is True

    # Verify db reflects progression
    trades = store.fetch_trades_today()
    assert trades[0]["tranche_1_filled"] == 1
    assert trades[0]["breakeven_latched"] == 1
