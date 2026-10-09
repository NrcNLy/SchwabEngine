"""
tests/test_preopen_hardening_patches.py
=======================================
Verification suite for the October 9, 2026 Pre-Open Quantitative Hardening Patches:
1. RVOL Opening Drive Bias Neutralization (2.20 hurdle during 09:30-10:15 EDT).
2. Tech Beta Allocation Clamp ({"SOXL", "TQQQ", "TECL", "FNGU"} half-slot, 2 active limit, portfolio beta <= 6.00).
3. Yang-Zhang Volatility Stop Recalibration (15-minute horizon scalar ~0.01235, ~1.2% to 2.2% stops).
4. Hurst Exponent Trailing Stop Inversion Fix & Emergency Defensive Exit.
5. 5-Second 09:30 VWMP Anchor Buffer with Spread Filter.
"""

from decimal import Decimal
import math
from datetime import datetime, time as dtime, timezone
from zoneinfo import ZoneInfo
import pytest

from core.ledger import ManagedPosition
from core.synthetic_divergence import SyntheticDivergenceEngine
from core.universe_manager import (
    UniverseManager,
    HIGH_CORRELATION_SET,
    PORTFOLIO_BETA_CEILING,
    TICKER_BETAS,
)
from execution.risk_manager import RiskEngine
from execution.strategies import (
    CandleBar,
    MarketRegime,
    RegimeMetrics,
    StrategyEngine,
    TradeSignal,
    _compute_ema,
    check_defensive_exit,
    update_trailing_stop,
)

D = Decimal
NY_TZ = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Helpers & Mocks
# ---------------------------------------------------------------------------

class MockLedger:
    def __init__(self, cash=3773.04, buffer=10.0, max_single=1245.10):
        self.settled_cash = Decimal(str(cash))
        self.cash_buffer = Decimal(str(buffer))
        self.max_single_exposure = Decimal(str(max_single))
        self.positions = {}

    def check_order_allowed(self, cost):
        cost_d = Decimal(str(cost))
        avail = self.settled_cash - self.cash_buffer
        if cost_d > avail:
            return False, "Insufficient settled funds"
        return True, "OK"


class MockMask:
    def is_symbol_tradeable(self, symbol):
        return True


def _make_dummy_metrics(symbol: str, rvol: float = 2.0, slope: float = 15.0) -> RegimeMetrics:
    now = datetime(2026, 10, 9, 10, 0, 0, tzinfo=NY_TZ)
    return RegimeMetrics(
        timestamp=now,
        symbol=symbol,
        ci=30.0,
        natr=2.5,
        rvol=rvol,
        vwap_slope_deg=slope,
        atr14_dollars=1.50,
        vwap=100.0,
        vwap_upper_2_2=103.0,
        vwap_lower_2_2=97.0,
        rsi14=55.0,
        regime=MarketRegime.TREND_EXPANSION,
    )


# ===========================================================================
# 1. Neutralize RVOL Opening Drive Bias
# ===========================================================================

def test_orb_morning_drive_rvol_hurdle_blocks_1_80_and_accepts_2_25():
    """
    Between 09:30 and 10:15 EDT, ORB entry must require rvol >= 2.20.
    RVOL of 1.80 is rejected; RVOL of 2.25 is accepted.
    After 10:15 EDT, RVOL hurdle relaxes to entry_rvol_min (1.50).
    """
    cfg = {
        "regime": {"evaluation_interval_sec": 180},
        "risk": {"max_loss_per_trade": 10.00},
        "strategies": {
            "orb_15m": {
                "entry_rvol_min": 1.50,
                "orb_morning_rvol_hurdle": 2.20,
                "risk_reward_target": 2.5,
            },
            "vwap_mean_reversion": {"vwap_band_sigma": 2.2, "rsi_oversold_threshold": 28},
        },
    }
    ledger = MockLedger()
    mask = MockMask()
    engine = StrategyEngine(cfg=cfg, ledger=ledger, mask=mask)

    # Register SOXL
    engine.register_symbol("SOXL")
    state, lock = engine._states["SOXL"]
    with lock:
        state.orb_high = 40.00
        state.orb_low = 38.00
        state.orb_finalized = True
        state.orb_signal_fired = False
        state.last_price = 40.50
        state.current_regime = MarketRegime.TREND_EXPANSION

    # During Morning Drive at 09:50 EDT with RVOL = 1.80 (< 2.20)
    t_morning = datetime(2026, 10, 9, 9, 50, 0, tzinfo=NY_TZ)
    with lock:
        state.last_metrics = _make_dummy_metrics("SOXL", rvol=1.80, slope=10.0)
    sig_rejected = engine._check_orb_signal(state, t_morning)
    assert sig_rejected is None, "ORB signal should be suppressed when RVOL 1.80 < 2.20 morning hurdle"

    # During Morning Drive at 09:50 EDT with RVOL = 2.25 (>= 2.20)
    with lock:
        state.last_metrics = _make_dummy_metrics("SOXL", rvol=2.25, slope=10.0)
    sig_accepted = engine._check_orb_signal(state, t_morning)
    assert sig_accepted is not None, "ORB signal should fire when RVOL 2.25 >= 2.20 morning hurdle"
    assert sig_accepted.symbol == "SOXL"

    # After Morning Drive at 10:20 EDT with RVOL = 1.80 (>= 1.50 baseline)
    t_after_morning = datetime(2026, 10, 9, 10, 20, 0, tzinfo=NY_TZ)
    with lock:
        state.last_metrics = _make_dummy_metrics("SOXL", rvol=1.80, slope=10.0)
    sig_afternoon = engine._check_orb_signal(state, t_after_morning)
    assert sig_afternoon is not None, "ORB signal should fire after 10:15 EDT when RVOL 1.80 >= 1.50"


# ===========================================================================
# 2. Tech Beta Allocation Clamp & Portfolio Beta Ceiling
# ===========================================================================

def test_tech_beta_allocation_clamp_and_portfolio_beta_ceiling():
    """
    1. First tech signal gets normal allocation.
    2. Second tech signal clamped to half-slot (avail_cash * 0.165).
    3. Third tech signal rejected because 2 slots already active.
    4. Non-tech signal rejected if total deployed beta exceeds 6.00.
    """
    um = UniverseManager()
    um.set_momentum_score("SOXL", 0.90)
    um.set_momentum_score("TQQQ", 0.85)
    um.set_momentum_score("TECL", 0.80)
    um.set_momentum_score("TNA",  0.70)

    # Initial ledger with $3,773.04 settled cash, $10.00 buffer -> $3,763.04 available
    avail_cash = 3773.04 - 10.00
    ledger = MockLedger(cash=3773.04, buffer=10.00, max_single=1245.10)

    dummy_m = _make_dummy_metrics("SOXL")

    sig_soxl = TradeSignal(
        timestamp=datetime.now(timezone.utc),
        symbol="SOXL",
        strategy="15m_ORB",
        direction="LONG",
        entry_price=Decimal("40.00"),
        stop_price=Decimal("38.00"),
        target_price=Decimal("45.00"),
        quantity=30,  # $1,200 notional (within 33% full slot)
        max_risk_usd=Decimal("10.00"),
        regime=MarketRegime.TREND_EXPANSION,
        metrics=dummy_m,
    )

    sig_tqqq = TradeSignal(
        timestamp=datetime.now(timezone.utc),
        symbol="TQQQ",
        strategy="15m_ORB",
        direction="LONG",
        entry_price=Decimal("50.00"),
        stop_price=Decimal("48.00"),
        target_price=Decimal("55.00"),
        quantity=24,  # $1,200 notional
        max_risk_usd=Decimal("10.00"),
        regime=MarketRegime.TREND_EXPANSION,
        metrics=dummy_m,
    )

    sig_tecl = TradeSignal(
        timestamp=datetime.now(timezone.utc),
        symbol="TECL",
        strategy="15m_ORB",
        direction="LONG",
        entry_price=Decimal("60.00"),
        stop_price=Decimal("58.00"),
        target_price=Decimal("65.00"),
        quantity=15,
        max_risk_usd=Decimal("10.00"),
        regime=MarketRegime.TREND_EXPANSION,
        metrics=dummy_m,
    )

    sig_tna = TradeSignal(
        timestamp=datetime.now(timezone.utc),
        symbol="TNA",
        strategy="15m_ORB",
        direction="LONG",
        entry_price=Decimal("40.00"),
        stop_price=Decimal("38.00"),
        target_price=Decimal("45.00"),
        quantity=15,
        max_risk_usd=Decimal("10.00"),
        regime=MarketRegime.TREND_EXPANSION,
        metrics=dummy_m,
    )

    signals = [sig_soxl, sig_tqqq, sig_tecl, sig_tna]
    dispatched = um.dispatch_candidate_signals(signals, ledger)

    # SOXL was approved first with full size
    assert dispatched[0].symbol == "SOXL"
    assert dispatched[0].quantity == 30

    # TQQQ was subsequent in tech set: clamped to half-slot
    # max half slot notional = 3763.04 * 0.165 = $620.90
    # max shares = int(620.90 / 50.00) = 12 shares
    assert dispatched[1].symbol == "TQQQ"
    assert dispatched[1].quantity == 12
    assert float(dispatched[1].entry_price * dispatched[1].quantity) <= (avail_cash * 0.165)

    # TECL (3rd tech signal) was rejected because 2 slots already active in tech set
    approved_syms = [s.symbol for s in dispatched]
    assert "TECL" not in approved_syms

    # TNA (non-tech) was rejected because deployed beta (SOXL 3.0 + TQQQ 3.0 = 6.0) + TNA 3.0 = 9.0 > 6.00
    assert "TNA" not in approved_syms
    assert len(dispatched) == 2


# ===========================================================================
# 3. Recalibrate Volatility Stop Scaling
# ===========================================================================

def test_volatility_stop_scaling_dynamic_yield():
    """
    Validates that:
    1. Default 15-minute horizon scalar math.sqrt((15/390)/252) is ~0.01235.
    2. Dynamic stop distance yields ~1.2% to 2.2% for TQQQ, TNA, and SOXL.
    3. The static 50 bps floor does not dominate normal volatility.
    """
    risk = RiskEngine()

    scalar = math.sqrt((15.0 / 390.0) / 252.0)
    assert math.isclose(scalar, 0.012354, rel_tol=1e-3)

    # TQQQ: daily vol ~ 0.0285, k_stop = 1.8
    hwm = 100.0
    dist_tqqq = risk.calculate_yang_zhang_stop_distance(hwm, yz_vol=0.0285, k_stop=1.8)
    pct_tqqq = (dist_tqqq / hwm) * 100.0
    assert 1.0 <= pct_tqqq <= 1.4, f"TQQQ stop % should yield ~1.0-1.4%, got {pct_tqqq:.2f}%"

    # TNA: daily vol ~ 0.0390, k_stop = 2.2
    dist_tna = risk.calculate_yang_zhang_stop_distance(hwm, yz_vol=0.0390, k_stop=2.2)
    pct_tna = (dist_tna / hwm) * 100.0
    assert 1.5 <= pct_tna <= 1.9, f"TNA stop % should yield ~1.5-1.9%, got {pct_tna:.2f}%"

    # SOXL: daily vol ~ 0.0520, k_stop = 2.0
    dist_soxl = risk.calculate_yang_zhang_stop_distance(hwm, yz_vol=0.0520, k_stop=2.0)
    pct_soxl = (dist_soxl / hwm) * 100.0
    assert 1.9 <= pct_soxl <= 2.3, f"SOXL stop % should yield ~1.9-2.3%, got {pct_soxl:.2f}%"

    # All stops exceed the static 50 bps (0.5%) floor
    assert dist_tqqq > 0.50
    assert dist_tna > 0.50
    assert dist_soxl > 0.50


# ===========================================================================
# 4. Fix Hurst Trailing Stop Inversion & Defensive Exit
# ===========================================================================

def test_trailing_stop_never_blocked_by_low_hurst():
    """
    Monotonic upward ratchets of the stop price must NEVER be blocked by hurst <= 0.50.
    """
    pos = ManagedPosition(
        symbol="TQQQ",
        quantity=10,
        entry_price=D("100.00"),
        last_price=D("100.00"),
        stop_price=D("96.00"),
        simulated=False,
    )

    # Price excursions to 105.00 during low Hurst (H = 0.35 <= 0.50)
    # Stop distance = 1.50
    new_stop, ratcheted = update_trailing_stop(pos, current_price=105.00, stop_distance=1.50, hurst_exponent=0.35)
    assert ratcheted is True, "Monotonic upward ratchet must not be blocked by low Hurst"
    assert new_stop == 103.50
    assert pos.stop_price == D("103.50")

    # Price drops to 102.00: stop must not loosen
    new_stop2, ratcheted2 = update_trailing_stop(pos, current_price=102.00, stop_distance=1.50, hurst_exponent=0.35)
    assert ratcheted2 is False
    assert pos.stop_price == D("103.50")


def test_emergency_defensive_exit_triggers_on_low_hurst_and_ema5_cross():
    """
    If hurst < 0.45 and 1-minute close crosses below 5-period EMA,
    check_defensive_exit must return True.
    """
    now = datetime(2026, 10, 9, 10, 0, 0, tzinfo=NY_TZ)
    # 5 bars trending around 100.00, then 6th bar closes below 5-EMA
    bars = [
        CandleBar(timestamp=now, open=100.0, high=101.0, low=99.5, close=100.5, volume=100),
        CandleBar(timestamp=now, open=100.5, high=101.5, low=100.0, close=101.0, volume=100),
        CandleBar(timestamp=now, open=101.0, high=102.0, low=100.5, close=101.5, volume=100),
        CandleBar(timestamp=now, open=101.5, high=102.0, low=101.0, close=101.8, volume=100),
        CandleBar(timestamp=now, open=101.8, high=102.0, low=101.2, close=101.9, volume=100),
    ]
    # At this point, closes are rising: 100.5 to 101.9
    # Now bar 6 drops sharply below EMA5
    drop_bar = CandleBar(timestamp=now, open=101.9, high=102.0, low=98.0, close=98.5, volume=500)
    bars_with_drop = bars + [drop_bar]

    # In persistent trend (H = 0.65), defensive exit is NOT active
    assert check_defensive_exit(bars_with_drop, hurst=0.65) is False

    # In mean-reverting chop (H = 0.40 < 0.45), defensive exit triggers
    assert check_defensive_exit(bars_with_drop, hurst=0.40) is True


# ===========================================================================
# 5. 5-Second 09:30 VWMP Anchor Buffer with Spread Filter
# ===========================================================================

def test_vwmp_anchor_buffer_and_spread_filter():
    """
    1. During 09:30:00 <= t < 09:30:05, ticks are buffered and anchors remain None.
    2. At t >= 09:30:05, VWMP is calculated from buffered ticks.
    3. Ticks with spread > 2x baseline spread are filtered out.
    4. Synthetic timestamps (< 1_000_000_000) preserve immediate single-tick latching.
    """
    engine = SyntheticDivergenceEngine()
    engine.set_baseline_spread("SOXL", 0.02)  # baseline spread $0.02 -> max allowed $0.04

    pair = engine.get_pair("SOXL_SOXS")
    assert pair.p_bull_0 is None

    # Ticks within 5-second buffer (09:30:00 - 09:30:04)
    # Tick 1: price 40.00, vol 100, spread 0.02 (valid)
    t1 = datetime(2026, 10, 9, 9, 30, 0, tzinfo=NY_TZ)
    engine.record_tick("SOXL", price=40.00, volume=100, timestamp=t1, spread=0.02)

    # Tick 2: price 41.00, vol 500, spread 0.08 (exceeds 2x baseline 0.04 -> filtered out!)
    t2 = datetime(2026, 10, 9, 9, 30, 2, tzinfo=NY_TZ)
    engine.record_tick("SOXL", price=41.00, volume=500, timestamp=t2, spread=0.08)

    # Tick 3: price 40.20, vol 300, spread 0.03 (valid)
    t3 = datetime(2026, 10, 9, 9, 30, 4, tzinfo=NY_TZ)
    engine.record_tick("SOXL", price=40.20, volume=300, timestamp=t3, spread=0.03)

    # During 09:30:00 - 09:30:04, anchors must remain None
    assert pair.p_bull_0 is None

    # Tick 4 at 09:30:05 triggers VWMP calculation
    # Valid ticks: 40.00 (vol 100), 40.20 (vol 300) -> total vol 400. VWMP = 40.20.
    t5 = datetime(2026, 10, 9, 9, 30, 5, tzinfo=NY_TZ)
    engine.record_tick("SOXL", price=40.20, volume=10, timestamp=t5, spread=0.02)

    assert pair.p_bull_0 == 40.20

    # Backward compatibility: synthetic timestamps (< 1_000_000_000) latch immediately
    engine_mock = SyntheticDivergenceEngine()
    engine_mock.record_tick("SOXL", price=50.00, volume=100, timestamp=500)
    pair_mock = engine_mock.get_pair("SOXL_SOXS")
    assert pair_mock.p_bull_0 == 50.00
