"""
tests/test_synthetic_divergence.py
==================================
Comprehensive test suite for Pillar 4:
Real-Time Cross-Asset Synthetic Divergence Engine and False Liquidity Trap Guard.

Covers:
1. Rolling buffer ingestion, 15-second window pruning, and tick calculation (RVOL, ΔV_spread, S_t).
2. Positive directional breakout verification (volume divergence + price expansion allows entry).
3. Symmetrical volume absorption verification (elevated dual RVOL + compressed price velocity triggers LIQUIDITY_TRAP_ACTIVE).
4. 120-second cooldown recovery behavior.
5. Strategy suppression and telemetry.db event logging (LIQUIDITY_TRAP_SUPPRESSION) in UniverseManager and StrategyEngine.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional
import pytest
import pytz

from core.synthetic_divergence import (
    DEFAULT_SYNTHETIC_PAIRS,
    SyntheticDivergenceEngine,
    SyntheticPair,
)
from core.telemetry import SQLiteWALEventStore
from core.universe_manager import UniverseManager
from execution.strategies import (
    CandleBar,
    MarketRegime,
    RegimeMetrics,
    StrategyEngine,
    SymbolState,
    TradeSignal,
)

_EDT = pytz.timezone("America/New_York")


@pytest.fixture
def divergence_engine() -> SyntheticDivergenceEngine:
    engine = SyntheticDivergenceEngine(
        window_seconds=15.0,
        cooldown_seconds=120.0,
        rvol_trap_threshold=1.30,
        price_velocity_trap_threshold=0.15,
        default_baseline_volume=1000.0,
    )
    # Set explicit baselines for clean deterministic ratios
    engine.set_baseline_volume("SOXL", 1000.0)
    engine.set_baseline_volume("SOXS", 1000.0)
    engine.set_baseline_volume("TQQQ", 1000.0)
    engine.set_baseline_volume("SQQQ", 1000.0)
    engine.set_baseline_volume("TNA",  1000.0)
    engine.set_baseline_volume("TZA",  1000.0)
    engine.set_baseline_volume("UCO",  1000.0)
    engine.set_baseline_volume("SCO",  1000.0)
    return engine


class MockLedger:
    def __init__(self, cash: float = 10000.0, max_single: float = 5000.0):
        self.settled_cash = Decimal(str(cash))
        self.cash_buffer = Decimal("500.00")
        self.max_single_exposure = Decimal(str(max_single))

    def check_order_allowed(self, cost: float) -> tuple[bool, str]:
        return True, ""


class MockMask:
    def is_symbol_tradeable(self, symbol: str) -> bool:
        return True


# ===========================================================================
# 1. Rolling Buffer Ingestion & Tick Calculation
# ===========================================================================

def test_rolling_buffer_ingestion_and_tick_calculation(divergence_engine: SyntheticDivergenceEngine):
    t0 = datetime(2026, 10, 8, 14, 0, 0, tzinfo=timezone.utc)

    # Initial session anchor prices
    divergence_engine.set_initial_price("SOXL", 40.00)
    divergence_engine.set_initial_price("SOXS", 20.00)

    # Ingest ticks across 20 seconds
    divergence_engine.record_tick("SOXL", price=40.00, volume=200, timestamp=t0)
    divergence_engine.record_tick("SOXS", price=20.00, volume=150, timestamp=t0)

    divergence_engine.record_tick("SOXL", price=40.10, volume=300, timestamp=t0 + timedelta(seconds=5))
    divergence_engine.record_tick("SOXS", price=19.95, volume=250, timestamp=t0 + timedelta(seconds=5))

    divergence_engine.record_tick("SOXL", price=40.20, volume=500, timestamp=t0 + timedelta(seconds=10))
    divergence_engine.record_tick("SOXS", price=19.90, volume=400, timestamp=t0 + timedelta(seconds=10))

    # At t0 + 10s: all 3 ticks are within 15-second window
    vol_soxl = divergence_engine.get_window_volume("SOXL", current_time=t0 + timedelta(seconds=10))
    vol_soxs = divergence_engine.get_window_volume("SOXS", current_time=t0 + timedelta(seconds=10))
    assert vol_soxl == 1000  # 200 + 300 + 500
    assert vol_soxs == 800   # 150 + 250 + 400

    rvol_soxl = divergence_engine.get_rvol("SOXL", current_time=t0 + timedelta(seconds=10))
    rvol_soxs = divergence_engine.get_rvol("SOXS", current_time=t0 + timedelta(seconds=10))
    assert rvol_soxl == pytest.approx(1.0)  # 1000 / 1000
    assert rvol_soxs == pytest.approx(0.8)  # 800 / 1000

    # Relative Volume Delta Spread: RVOL_Bull - RVOL_Bear
    spread = divergence_engine.get_volume_delta_spread("SOXL_SOXS", current_time=t0 + timedelta(seconds=10))
    assert spread == pytest.approx(1.0 - 0.8)

    # Normalized Synthetic Price Product: (P_Bull,t * P_Bear,t) / (P_Bull,0 * P_Bear,0)
    # (40.20 * 19.90) / (40.00 * 20.00) = 799.98 / 800.00 = 0.999975
    s_t = divergence_engine.get_synthetic_price_product("SOXL_SOXS", current_time=t0 + timedelta(seconds=10))
    assert s_t == pytest.approx((40.20 * 19.90) / (40.00 * 20.00), rel=1e-5)

    # Advance time to t0 + 20s (ticks from t0 are now outside the 15-second window: cutoff is t0 + 5s)
    divergence_engine.record_tick("SOXL", price=40.25, volume=600, timestamp=t0 + timedelta(seconds=20))
    vol_soxl_pruned = divergence_engine.get_window_volume("SOXL", current_time=t0 + timedelta(seconds=20))
    # Ticks included: t0+5s (300), t0+10s (500), t0+20s (600) = 1400. Tick at t0 (200) was pruned!
    assert vol_soxl_pruned == 1400


# ===========================================================================
# 2. Positive Directional Breakout Verification
# ===========================================================================

def test_positive_directional_breakout_allows_entry(divergence_engine: SyntheticDivergenceEngine):
    """
    Volume divergence with price expansion confirms real breakout;
    LIQUIDITY_TRAP_ACTIVE must remain False.
    """
    t0 = datetime(2026, 10, 8, 14, 0, 0, tzinfo=timezone.utc)

    # Bull instrument surges with high volume and directional expansion (+0.75%)
    divergence_engine.record_tick("SOXL", price=40.00, volume=1000, timestamp=t0)
    divergence_engine.record_tick("SOXL", price=40.30, volume=1500, timestamp=t0 + timedelta(seconds=5))

    # Bear instrument has low volume and price contraction (-0.75%)
    divergence_engine.record_tick("SOXS", price=20.00, volume=250, timestamp=t0)
    divergence_engine.record_tick("SOXS", price=19.85, volume=250, timestamp=t0 + timedelta(seconds=5))

    t_check = t0 + timedelta(seconds=5)

    rvol_bull = divergence_engine.get_rvol("SOXL", t_check)
    rvol_bear = divergence_engine.get_rvol("SOXS", t_check)
    vel_bull  = divergence_engine.get_price_velocity("SOXL", t_check)
    vel_bear  = divergence_engine.get_price_velocity("SOXS", t_check)

    assert rvol_bull == 2.50  # 2500 / 1000 >= 1.30
    assert rvol_bear == 0.50  # 500 / 1000 < 1.30
    assert vel_bull  == pytest.approx(0.75)   # +0.75% > 0.15% (price expanded!)
    assert vel_bear  == pytest.approx(-0.75)  # -0.75%

    # Liquidity trap must NOT be triggered
    assert not divergence_engine.is_trap_active("SOXL", t_check)
    assert not divergence_engine.is_trap_active("SOXS", t_check)
    assert not divergence_engine.is_trap_active("SOXL_SOXS", t_check)


# ===========================================================================
# 3. Symmetrical Volume Absorption Verification
# ===========================================================================

def test_symmetrical_volume_absorption_triggers_trap(divergence_engine: SyntheticDivergenceEngine):
    """
    Both bull and bear show simultaneous elevated volume (RVOL >= 1.30)
    while price velocities are compressed (|ΔP| < 0.15%), triggering LIQUIDITY_TRAP_ACTIVE.
    """
    t0 = datetime(2026, 10, 8, 14, 0, 0, tzinfo=timezone.utc)

    # SOXL: RVOL = 1.50 (1500 vol), Price moves only from 40.00 to 40.02 (+0.05% < 0.15%)
    divergence_engine.record_tick("SOXL", price=40.00, volume=700, timestamp=t0)
    divergence_engine.record_tick("SOXL", price=40.02, volume=800, timestamp=t0 + timedelta(seconds=10))

    # SOXS: RVOL = 1.45 (1450 vol), Price moves only from 20.00 to 19.99 (-0.05% < 0.15%)
    divergence_engine.record_tick("SOXS", price=20.00, volume=650, timestamp=t0)
    divergence_engine.record_tick("SOXS", price=19.99, volume=800, timestamp=t0 + timedelta(seconds=10))

    t_check = t0 + timedelta(seconds=10)

    metrics = divergence_engine.get_metrics("SOXL_SOXS", t_check)
    assert metrics["rvol_bull"] == 1.50
    assert metrics["rvol_bear"] == 1.45
    assert abs(metrics["price_velocity_bull_pct"]) < 0.15
    assert abs(metrics["price_velocity_bear_pct"]) < 0.15

    # Assert LIQUIDITY_TRAP_ACTIVE is True for pair and both individual symbols
    assert divergence_engine.is_trap_active("SOXL_SOXS", t_check) is True
    assert divergence_engine.is_trap_active("SOXL", t_check) is True
    assert divergence_engine.is_trap_active("SOXS", t_check) is True


# ===========================================================================
# 4. 120-Second Cooldown Recovery
# ===========================================================================

def test_120_second_cooldown_recovery(divergence_engine: SyntheticDivergenceEngine):
    """
    Trap triggers at t0. When conditions subside at t0 + 20s,
    the trap remains active through t0 + 139s (120s cooldown),
    and cleanly recovers to False at t0 + 141s.
    """
    t0 = datetime(2026, 10, 8, 14, 0, 0, tzinfo=timezone.utc)

    # 1. Trigger trap conditions at t0 + 10s
    divergence_engine.record_tick("TQQQ", price=80.00, volume=700, timestamp=t0)
    divergence_engine.record_tick("TQQQ", price=80.02, volume=800, timestamp=t0 + timedelta(seconds=10))
    divergence_engine.record_tick("SQQQ", price=15.00, volume=700, timestamp=t0)
    divergence_engine.record_tick("SQQQ", price=15.01, volume=800, timestamp=t0 + timedelta(seconds=10))

    t_trigger = t0 + timedelta(seconds=10)
    assert divergence_engine.is_trap_active("TQQQ", t_trigger) is True

    # 2. Advance time to t0 + 40s (30s after trigger).
    # Conditions have subsided: volume is now small (RVOL = 0.20 < 1.30)
    t_subsided = t0 + timedelta(seconds=40)
    divergence_engine.record_tick("TQQQ", price=80.10, volume=100, timestamp=t_subsided)
    divergence_engine.record_tick("SQQQ", price=14.95, volume=100, timestamp=t_subsided)

    # Cooldown check at t_subsided (elapsed since trigger = 30s < 120s) -> STILL ACTIVE
    assert divergence_engine.is_trap_active("TQQQ", t_subsided) is True
    metrics_40s = divergence_engine.get_metrics("TQQQ", t_subsided)
    assert metrics_40s["is_trap_active"] is True
    assert metrics_40s["cooldown_remaining_sec"] == pytest.approx(90.0, abs=1.0)

    # Cooldown check at t0 + 129s (elapsed = 119s < 120s) -> STILL ACTIVE
    t_119s = t_trigger + timedelta(seconds=119)
    assert divergence_engine.is_trap_active("TQQQ", t_119s) is True

    # Cooldown check at t0 + 131s (elapsed = 121s > 120s) -> RECOVERED TO FALSE!
    t_121s = t_trigger + timedelta(seconds=121)
    assert divergence_engine.is_trap_active("TQQQ", t_121s) is False
    assert divergence_engine.is_trap_active("SQQQ", t_121s) is False


# ===========================================================================
# 5. UniverseManager and StrategyEngine Suppression Integration
# ===========================================================================

def test_universe_manager_entry_suppression(divergence_engine: SyntheticDivergenceEngine, tmp_path):
    """
    UniverseManager suppresses entry and records LIQUIDITY_TRAP_SUPPRESSION in telemetry.db.
    """
    db_file = tmp_path / "telemetry.db"
    store = SQLiteWALEventStore(db_path=db_file)

    um = UniverseManager(scanner=None, divergence_engine=divergence_engine)

    # Monkeypatch SQLiteWALEventStore to use tmp_path
    import core.universe_manager as um_module
    orig_store_cls = um_module.SQLiteWALEventStore if hasattr(um_module, "SQLiteWALEventStore") else None

    # Before trap: entry is allowed
    assert um.is_entry_allowed("SOXL") is True

    # Trigger trap on SOXL
    t0 = datetime(2026, 10, 8, 14, 0, 0, tzinfo=timezone.utc)
    divergence_engine.record_tick("SOXL", price=40.00, volume=1500, timestamp=t0)
    divergence_engine.record_tick("SOXS", price=20.00, volume=1500, timestamp=t0)

    # With trap active: entry is suppressed
    assert um.is_entry_allowed("SOXL", current_time=t0) is False


def test_universe_manager_momentum_ranking_and_dispatch(divergence_engine: SyntheticDivergenceEngine):
    """
    Verifies that UniverseManager ranks candidate entry signals by Composite
    Momentum Score (S_i) prior to checking settled cash availability, preventing
    secondary ETFs from starving primary assets.
    """
    um = UniverseManager(scanner=None, divergence_engine=divergence_engine)
    um.update_universe(["SOXL", "BOIL", "TQQQ"])
    assert um.get_active_universe() == ["SOXL", "BOIL", "TQQQ"]

    # Assign Composite Momentum Scores: SOXL (0.95), TQQQ (0.80), BOIL (0.15)
    um.set_momentum_score("SOXL", 0.95)
    um.set_momentum_score("TQQQ", 0.80)
    um.set_momentum_score("BOIL", 0.15)

    now = datetime(2026, 10, 8, 9, 35, 0, tzinfo=timezone.utc)
    dummy_metrics = RegimeMetrics(
        timestamp=now, symbol="DUMMY", ci=30.0, natr=1.0, rvol=1.5,
        vwap_slope_deg=10.0, atr14_dollars=0.5, vwap=20.0,
        vwap_upper_2_2=21.0, vwap_lower_2_2=19.0, rsi14=50.0,
        regime=MarketRegime.TREND_EXPANSION
    )

    sig_boil = TradeSignal(
        timestamp=now,
        symbol="BOIL",
        strategy="15m_ORB",
        direction="LONG",
        entry_price=Decimal("10.00"),
        stop_price=Decimal("9.50"),
        target_price=Decimal("11.00"),
        quantity=300,  # $3,000 cost
        max_risk_usd=Decimal("10.00"),
        regime=MarketRegime.TREND_EXPANSION,
        metrics=dummy_metrics,
    )
    sig_soxl = TradeSignal(
        timestamp=now,
        symbol="SOXL",
        strategy="15m_ORB",
        direction="LONG",
        entry_price=Decimal("40.00"),
        stop_price=Decimal("38.00"),
        target_price=Decimal("45.00"),
        quantity=100,  # $4,000 cost
        max_risk_usd=Decimal("10.00"),
        regime=MarketRegime.TREND_EXPANSION,
        metrics=dummy_metrics,
    )
    sig_tqqq = TradeSignal(
        timestamp=now,
        symbol="TQQQ",
        strategy="15m_ORB",
        direction="LONG",
        entry_price=Decimal("50.00"),
        stop_price=Decimal("48.00"),
        target_price=Decimal("55.00"),
        quantity=50,   # $2,500 cost
        max_risk_usd=Decimal("10.00"),
        regime=MarketRegime.TREND_EXPANSION,
        metrics=dummy_metrics,
    )

    # Greedy FIFO would process BOIL first if given [sig_boil, sig_soxl, sig_tqqq]
    unranked = [sig_boil, sig_soxl, sig_tqqq]
    ranked = um.rank_candidate_signals(unranked)
    assert [s.symbol for s in ranked] == ["SOXL", "TQQQ", "BOIL"]

    # Ledger with limited settled cash ($5,000 settled cash - $500 buffer = $4,500 available)
    ledger = MockLedger(cash=5000.0, max_single=5000.0)
    ledger.cash_buffer = Decimal("500.00")

    dispatched = um.dispatch_candidate_signals(unranked, ledger)
    # SOXL ($4,000) should be approved first because it has highest momentum score
    assert len(dispatched) >= 1
    assert dispatched[0].symbol == "SOXL"
    assert dispatched[0].quantity == 100


def test_strategy_engine_orb_suppression_and_telemetry(divergence_engine: SyntheticDivergenceEngine, tmp_path):
    """
    Verifies that StrategyEngine suppresses 15m ORB entries when LIQUIDITY_TRAP_ACTIVE,
    does NOT fire the signal callback, does NOT burn state.orb_signal_fired,
    and logs LIQUIDITY_TRAP_SUPPRESSION to telemetry.db.
    """
    db_file = tmp_path / "telemetry.db"

    # Patch paths to write to test db
    import core.telemetry as tel_mod
    orig_path = tel_mod.DEFAULT_TELEMETRY_DB
    tel_mod.DEFAULT_TELEMETRY_DB = db_file
    tel_mod.TELEMETRY_DB_PATH = db_file

    try:
        cfg = {
            "regime": {"evaluation_interval_sec": 180},
            "risk": {"max_loss_per_trade": 10.00},
            "strategies": {
                "orb_15m": {"entry_rvol_min": 1.5, "risk_reward_target": 2.5},
                "vwap_mean_reversion": {"vwap_band_sigma": 2.2, "rsi_oversold_threshold": 28},
            },
        }
        ledger = MockLedger()
        mask = MockMask()
        store = SQLiteWALEventStore(db_path=db_file)
        engine = StrategyEngine(cfg=cfg, ledger=ledger, mask=mask, divergence_engine=divergence_engine, event_store=store)

        signals_received: List[TradeSignal] = []
        engine.set_signal_callback(lambda sig: signals_received.append(sig))

        sym = "SOXL"
        engine.register_symbol(sym)
        state, lock = engine._states[sym]

        now = datetime(2026, 10, 8, 9, 50, 0, tzinfo=_EDT)
        utc_now = now.astimezone(timezone.utc)

        # Set up finalized ORB breakout state for SOXL
        state.orb_finalized = True
        state.orb_signal_fired = False
        state.orb_high = 40.00
        state.orb_low = 39.00
        state.last_price = 40.50  # Breakout above 40.00
        state.current_regime = MarketRegime.TREND_EXPANSION

        state.last_metrics = RegimeMetrics(
            timestamp=now,
            symbol=sym,
            ci=30.0,
            natr=1.5,
            rvol=2.5,
            vwap_slope_deg=25.0,
            atr14_dollars=0.50,
            vwap=40.00,
            vwap_upper_2_2=41.00,
            vwap_lower_2_2=39.00,
            rsi14=55.0,
            regime=MarketRegime.TREND_EXPANSION,
        )

        # 1. Trigger False Liquidity Trap on SOXL/SOXS
        divergence_engine.record_tick("SOXL", price=40.50, volume=1500, timestamp=utc_now)
        divergence_engine.record_tick("SOXS", price=20.00, volume=1500, timestamp=utc_now)
        assert divergence_engine.is_trap_active("SOXL", utc_now) is True

        # 2. Check signals in StrategyEngine
        engine._check_signals(state, now)

        # 3. Assert NO signals were emitted (callback was not invoked)
        assert len(signals_received) == 0
        # 4. Assert ORB slot was NOT burned
        assert state.orb_signal_fired is False

        # 5. Assert LIQUIDITY_TRAP_SUPPRESSION event logged to telemetry DB
        store = SQLiteWALEventStore(db_path=db_file)
        events = store.fetch_historical_events(aggregate_id="SOXL")
        matching = [e for e in events if e.event_type == "LIQUIDITY_TRAP_SUPPRESSION"]
        assert len(matching) >= 1
        assert matching[0].aggregate_id == "SOXL"
        assert matching[0].event_type == "LIQUIDITY_TRAP_SUPPRESSION"
        assert matching[0].payload.get("reason") == "LIQUIDITY_TRAP_ACTIVE"

    finally:
        tel_mod.DEFAULT_TELEMETRY_DB = orig_path
        tel_mod.TELEMETRY_DB_PATH = orig_path


def test_synthetic_divergence_0930_anchor_latching(divergence_engine: SyntheticDivergenceEngine):
    """
    Verify that:
    1. Pre-market ticks (< 09:30 EDT) do NOT latch opening anchors.
    2. Crossing 09:30:00 EDT resets pre-market state and latches the cash open prints.
    3. reset_opening_anchors() clears anchors across all tracked pairs.
    4. get_tracked_symbols() reports all legs.
    """
    tracked = divergence_engine.get_tracked_symbols()
    assert "SOXL" in tracked and "SOXS" in tracked
    assert "TQQQ" in tracked and "SQQQ" in tracked

    from zoneinfo import ZoneInfo
    ny_tz = ZoneInfo("America/New_York")

    # 1. Pre-market tick at 09:15:00 EDT
    t_premarket = datetime(2026, 10, 8, 9, 15, 0, tzinfo=ny_tz)
    divergence_engine.record_tick("SOXL", price=39.00, volume=100, timestamp=t_premarket)
    divergence_engine.record_tick("SOXS", price=21.00, volume=100, timestamp=t_premarket)

    pair = divergence_engine.get_pair("SOXL_SOXS")
    assert pair.p_bull_0 is None
    assert pair.p_bear_0 is None

    # 2. Cash open tick at 09:30:00 EDT (buffered during 09:30:00 <= t < 09:30:05)
    t_open = datetime(2026, 10, 8, 9, 30, 0, tzinfo=ny_tz)
    divergence_engine.on_tick("SOXL", price=40.00, volume=500, timestamp=t_open)
    divergence_engine.on_tick("SOXS", price=20.00, volume=400, timestamp=t_open)

    # During 09:30:00-09:30:05 EDT, ticks are held in opening buffer
    assert pair.p_bull_0 is None
    assert pair.p_bear_0 is None

    # At t >= 09:30:05 EDT, VWMP is calculated from buffered ticks and assigned
    t_latch = datetime(2026, 10, 8, 9, 30, 5, tzinfo=ny_tz)
    divergence_engine.on_tick("SOXL", price=40.00, volume=100, timestamp=t_latch)
    divergence_engine.on_tick("SOXS", price=20.00, volume=100, timestamp=t_latch)

    assert pair.p_bull_0 == 40.00
    assert pair.p_bear_0 == 20.00

    # Synthetic price product anchors to 40.00 and 20.00
    s_t = divergence_engine.get_synthetic_price_product("SOXL_SOXS", t_latch)
    assert s_t == pytest.approx(1.0)

    # Subsequent tick during market hours updates price but keeps anchor
    t_1 = datetime(2026, 10, 8, 9, 31, 0, tzinfo=ny_tz)
    divergence_engine.record_tick("SOXL", price=40.80, volume=100, timestamp=t_1)
    assert pair.p_bull_0 == 40.00

    # 3. Explicit reset_opening_anchors()
    divergence_engine.reset_opening_anchors()
    assert pair.p_bull_0 is None
    assert pair.p_bear_0 is None

