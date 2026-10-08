"""
tests/test_temporal_stops.py
============================
Automated Test Suite for Temporal Session Gating and Yang-Zhang Dynamic Volatility Trailing Stops.

Tests:
1. test_temporal_session_transitions: Verifies TradingPhase across simulated timestamps.
2. test_midday_freeze_blocks_entries: Verifies entry rejection during MIDDAY_FREEZE (11:30 - 14:00 EDT).
3. test_power_hour_fractional_sizing: Verifies 50% sizing multiplier during POWER_HOUR.
4. test_yang_zhang_formula_fixtures: Verifies calculation of Yang-Zhang variance against mathematical fixture.
5. test_yang_zhang_fallback_on_sparse_bars: Verifies non-crashing graceful fallback when < 5 bars.
6. test_trailing_stop_ratchet_monotonicity: Verifies stop trails upwards on new highs and never loosens on drawdowns.
7. test_gfv_and_cap_invariants_preserved: Verifies T+1 cash account invariants are maintained.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time as dtime
from decimal import Decimal
from typing import List

import pytest
import pytz

from core.engine import EngineSettings, LiveEngine, SimEngine
from core.ledger import BrokerBalances, ManagedPosition, SettlementLedger
from core.liquidity_policy import LiquidityPolicy, compute_buying_power
from core.runtime import EngineContext
from core.session import TradingPhase, get_session_phase, is_entry_permitted
from execution.risk_manager import RiskEngine
from indicators.volatility import OHLCBar, YangZhangEstimator, compute_yang_zhang_volatility

_EDT = pytz.timezone("America/New_York")
D = Decimal


# ---------------------------------------------------------------------------
# 1. Temporal Session Transitions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "dt_time,expected_phase,expected_permitted,expected_mult",
    [
        (dtime(8, 0, 0), TradingPhase.OFFLINE, False, 0.0),
        (dtime(8, 35, 0), TradingPhase.PRE_MARKET, False, 0.0),
        (dtime(9, 29, 59), TradingPhase.PRE_MARKET, False, 0.0),
        (dtime(9, 30, 0), TradingPhase.MORNING_DRIVE, True, 1.0),
        (dtime(10, 15, 0), TradingPhase.MORNING_DRIVE, True, 1.0),
        (dtime(10, 30, 0), TradingPhase.MID_MORNING, True, 1.0),
        (dtime(11, 29, 59), TradingPhase.MID_MORNING, True, 1.0),
        (dtime(11, 30, 0), TradingPhase.MIDDAY_FREEZE, False, 0.0),
        (dtime(12, 30, 0), TradingPhase.MIDDAY_FREEZE, False, 0.0),
        (dtime(13, 59, 59), TradingPhase.MIDDAY_FREEZE, False, 0.0),
        (dtime(14, 0, 0), TradingPhase.POWER_HOUR, True, 0.50),
        (dtime(15, 0, 0), TradingPhase.POWER_HOUR, True, 0.50),
        (dtime(15, 34, 59), TradingPhase.POWER_HOUR, True, 0.50),
        (dtime(15, 35, 0), TradingPhase.PRE_CLOSE, False, 0.0),
        (dtime(15, 49, 59), TradingPhase.PRE_CLOSE, False, 0.0),
        (dtime(15, 50, 0), TradingPhase.MANDATORY_FLATTEN, False, 0.0),
        (dtime(15, 54, 59), TradingPhase.MANDATORY_FLATTEN, False, 0.0),
        (dtime(15, 55, 0), TradingPhase.POST_CLOSE_REFLECTION, False, 0.0),
        (dtime(16, 30, 0), TradingPhase.POST_CLOSE_REFLECTION, False, 0.0),
        (dtime(17, 0, 1), TradingPhase.OFFLINE, False, 0.0),
    ],
)
def test_temporal_session_transitions(dt_time, expected_phase, expected_permitted, expected_mult):
    # A standard Monday (business day)
    simulated_dt = datetime(2026, 10, 5, dt_time.hour, dt_time.minute, dt_time.second, tzinfo=_EDT)
    phase = get_session_phase(simulated_dt)
    assert phase == expected_phase

    permitted, mult, _ = is_entry_permitted(phase)
    assert permitted == expected_permitted
    assert mult == pytest.approx(expected_mult)


def test_weekend_evaluates_to_offline():
    # Saturday
    saturday_dt = datetime(2026, 10, 3, 10, 0, 0, tzinfo=_EDT)
    phase = get_session_phase(saturday_dt)
    assert phase == TradingPhase.OFFLINE
    permitted, _, _ = is_entry_permitted(phase)
    assert permitted is False


# ---------------------------------------------------------------------------
# 2. Midday Freeze Blocks Entries
# ---------------------------------------------------------------------------

def test_midday_freeze_blocks_entries():
    ctx = EngineContext({}, live=True)
    ledger = SettlementLedger(D("0"), data_source="LIVE_SCHWAB")
    ledger.sync_from_broker(BrokerBalances(liquidation_value=D("5000"), settled_cash=D("5000")))
    ctx.ledgers["active"] = ledger

    settings = EngineSettings(
        symbols=["TQQQ", "SOXL", "TNA"],
        eod=dtime(15, 50),
        flat_deadline=dtime(15, 55),
        tier2_stop_pct=D("0.04"),
    )

    class DummyOM:
        pass

    class DummyStrategy:
        def set_signal_callback(self, cb): pass

    engine = LiveEngine(ctx, {}, ledger, settings, DummyOM(), DummyStrategy(), None)
    engine.ignore_session = False

    # Simulate 12:30 EDT (MIDDAY_FREEZE)
    freeze_time = datetime(2026, 10, 5, 12, 30, 0, tzinfo=_EDT)
    import core.engine
    orig_now = core.engine.now_et
    core.engine.now_et = lambda: freeze_time
    try:
        reason = engine.entry_block_reason("SOXL")
        assert reason is not None
        assert "MIDDAY_FREEZE" in reason
    finally:
        core.engine.now_et = orig_now


# ---------------------------------------------------------------------------
# 3. Power Hour Fractional Sizing
# ---------------------------------------------------------------------------

def test_power_hour_fractional_sizing():
    power_hour_time = datetime(2026, 10, 5, 14, 30, 0, tzinfo=_EDT)
    phase = get_session_phase(power_hour_time)
    assert phase == TradingPhase.POWER_HOUR

    permitted, mult, _ = is_entry_permitted(phase)
    assert permitted is True
    assert mult == 0.50

    # Verify scaling application
    full_shares = 10
    power_hour_shares = math.floor(full_shares * mult)
    assert power_hour_shares == 5


# ---------------------------------------------------------------------------
# 4. Yang-Zhang Volatility Formula Fixtures
# ---------------------------------------------------------------------------

def test_yang_zhang_formula_fixtures():
    # Synthetic bars with known price variance
    bars = [
        OHLCBar(open=100.0, high=102.0, low=99.0, close=101.0),
        OHLCBar(open=101.5, high=103.0, low=100.5, close=102.0),
        OHLCBar(open=101.8, high=104.0, low=101.0, close=103.5),
        OHLCBar(open=103.0, high=105.0, low=102.5, close=104.0),
        OHLCBar(open=104.2, high=106.0, low=103.0, close=105.5),
        OHLCBar(open=105.0, high=107.5, low=104.0, close=106.0),
        OHLCBar(open=105.5, high=108.0, low=105.0, close=107.0),
        OHLCBar(open=107.0, high=109.0, low=106.0, close=108.5),
    ]

    vol = compute_yang_zhang_volatility(bars)
    assert vol > 0.0
    # Volatility for these ~1-2% moves should be in realistic bounds (0.005 - 0.05)
    assert 0.005 <= vol <= 0.05


def test_yang_zhang_fallback_on_sparse_bars():
    # 0 bars
    assert compute_yang_zhang_volatility([]) == 0.0

    # 1 bar
    assert compute_yang_zhang_volatility([OHLCBar(100.0, 101.0, 99.0, 100.5)]) == 0.0

    # 3 bars (fewer than 5)
    sparse = [
        OHLCBar(100.0, 101.0, 99.0, 100.5),
        OHLCBar(100.5, 102.0, 100.0, 101.5),
        OHLCBar(101.5, 103.0, 101.0, 102.0),
    ]
    vol_sparse = compute_yang_zhang_volatility(sparse)
    assert vol_sparse > 0.0

    # Estimator streaming class works without error
    estimator = YangZhangEstimator(window_size=30)
    for b in sparse:
        v = estimator.add_bar(b.open, b.high, b.low, b.close)
    assert estimator.current_volatility() == vol_sparse


# ---------------------------------------------------------------------------
# 5. Trailing Stop Ratchet Monotonicity
# ---------------------------------------------------------------------------

def test_trailing_stop_ratchet_monotonicity():
    risk = RiskEngine()
    pos = ManagedPosition(
        symbol="TQQQ",
        quantity=10,
        entry_price=D("100.00"),
        last_price=D("100.00"),
        stop_price=D("96.00"),
        simulated=False,
    )

    # Initial state
    assert pos.high_water_mark == D("100.00")
    assert pos.stop_price == D("96.00")

    # Price rises to 105.00: stop should ratchet upward
    # Volatility ~ 0.02, k_stop = 2.0 -> distance ~ 105 * (2.0 * 0.02 * sqrt(1/252)) ~ 105 * 0.00252 = ~0.26 => min buffer
    yz_vol = 0.02
    new_stop, ratcheted = risk.evaluate_trailing_stop(pos, 105.00, yz_vol, k_stop=2.0)
    assert ratcheted is True
    assert pos.high_water_mark == D("105.00")
    assert pos.stop_price > D("96.00")
    stop_after_rise = pos.stop_price

    # Price drops to 102.00: stop MUST NOT loosen (monotonicity check)
    new_stop2, ratcheted2 = risk.evaluate_trailing_stop(pos, 102.00, yz_vol, k_stop=2.0)
    assert ratcheted2 is False
    assert pos.high_water_mark == D("105.00")  # HWM stays pinned at 105.00
    assert pos.stop_price == stop_after_rise   # stop does not drop


# ---------------------------------------------------------------------------
# 6. GFV and Single-Ticker Cap Retention
# ---------------------------------------------------------------------------

def test_gfv_and_cap_invariants_preserved():
    policy = LiquidityPolicy()
    bp = compute_buying_power(
        nlv=D("3750.00"),
        settled_cash=D("2450.00"),
        unsettled_cash=D("500.00"),
        policy=policy,
        today=date(2026, 10, 5),
    )

    # Invariant I1 & I4: unsettled cash never enters buying power
    assert bp.hard_reserve == D("500.00")
    assert bp.tactical_float <= D("2450.00")

    # Invariant: single ticker cap is exactly 33% of NLV
    assert bp.single_ticker_cap == D("1237.50")
    assert bp.max_order_notional <= D("1237.50")
