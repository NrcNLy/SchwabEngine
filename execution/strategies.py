"""
execution/strategies.py
=======================
Quantitative Strategy and Regime Engine for the Schwab Day-Trading Engine.

Architecture
------------
CandleBar           — Immutable 1-minute OHLCV bar.
VWAPSample          — A single 60-second VWAP snapshot used for slope regression.
RegimeMetrics       — Point-in-time snapshot of all four telemetry metrics.
TradeSignal         — A fully-specified, sized entry signal ready for order routing.
_BarBuilder         — Mutable in-progress bar accumulator (internal).
SymbolState         — All per-symbol running state (bars, VWAP, ORB, regime, RSI).

Computation helpers (module-level, pure functions):
    _compute_true_ranges     → 1-period ATR values for each bar
    _compute_ci              → 14-period Choppiness Index
    _compute_atr14           → 14-period Average True Range (dollars)
    _compute_natr            → Normalized ATR (ATR / close × 100)
    _compute_rsi             → 14-period Wilder's RSI
    _compute_vwap_and_bands  → Session VWAP + 2.2σ upper/lower bands
    _vwap_slope_degrees      → OLS slope over 15-sample window (in degrees)
    _compute_rvol            → RVOL vs. historical 20-day per-minute average
    quarter_kelly_size       → Floor(10.00 / |entry - stop|)

RegimeClassifier   — Classifies MarketRegime from a RegimeMetrics snapshot.
StrategyEngine     — Orchestrates all of the above; primary interface is on_tick().

Metric specification (from research doc):
    VWAP Slope: sampled every 60s, 15m rolling regression window,
                positive slope (> 15°) confirms accumulation.
    NATR:       14-period window on 1m bars.
    RVOL:       Active candle vs 20-day historical average for that minute.
    CI:         14-period lookback.

Regime thresholds (from dev plan + research):
    A (Trend Expansion):  CI < 38.2,  VWAP Slope > 0°, RVOL ≥ 1.5  → 15m ORB
    B (Mean-Reversion):   38.2 ≤ CI ≤ 61.8,            RVOL < 1.0  → VWAP MR
    C (High-Noise Chop):  CI > 61.8                                 → HALT

Configuration sources (config.yaml):
    regime.ci_trend_threshold        → 38.2
    regime.ci_chop_threshold         → 61.8
    regime.rvol_breakout_min         → 1.5
    regime.rvol_compression_max      → 1.0
    regime.vwap_slope_min_degrees    → 15.0
    regime.evaluation_interval_sec   → 180  (3 minutes)
    regime.natr_period               → 14
    regime.ci_period                 → 14
    strategies.orb_15m.entry_rvol_min         → 1.5
    strategies.orb_15m.risk_reward_target     → 2.5
    strategies.vwap_mean_reversion.vwap_band_sigma      → 2.2
    strategies.vwap_mean_reversion.rsi_oversold_threshold → 28
    risk.max_loss_per_trade          → 10.00
"""

from __future__ import annotations

import logging
import math
import statistics
import threading
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from enum import Enum
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

import pytz

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_EDT                  = pytz.timezone("America/New_York")
_SESSION_OPEN         = (9, 30, 0)    # (hour, minute, second) EDT
_ORB_END              = (9, 45, 0)    # 15m Opening Range ends
_SESSION_CLOSE        = (16, 0, 0)
_BARS_NEEDED          = 15            # Minimum closed bars for CI / RSI calculations
_VWAP_SAMPLE_INTERVAL = 60           # seconds between VWAP slope samples
_SIGNAL_COOLDOWN_SEC  = 300          # Minimum gap between signals for same symbol/strategy
_MIN_SIGNAL_QUANTITY  = 1            # Floor — never signal for 0 shares


# ===========================================================================
# Enums
# ===========================================================================

class MarketRegime(Enum):
    """Current market microstructure classification."""
    TREND_EXPANSION   = "A"    # CI < 38.2  → 15m ORB active
    MEAN_REVERSION    = "B"    # 38.2 ≤ CI ≤ 61.8 → VWAP MR active
    HIGH_NOISE_CHOP   = "C"    # CI > 61.8 → HALT all entries
    INSUFFICIENT_DATA = "D"    # Not enough bars yet to classify


# ===========================================================================
# Data Classes
# ===========================================================================

@dataclass(frozen=True)
class CandleBar:
    """Immutable closed 1-minute OHLCV bar."""
    timestamp: datetime     # Bar open time (EDT, minute-truncated)
    open:      float
    high:      float
    low:       float
    close:     float
    volume:    int          # Volume during THIS bar (not cumulative session volume)


@dataclass(frozen=True)
class VWAPSample:
    """A 60-second VWAP snapshot used for the 15-minute slope regression."""
    elapsed_sec: float      # Seconds since session open (X-axis for regression)
    vwap:        float      # VWAP value at this sample moment


@dataclass(frozen=True)
class RegimeMetrics:
    """
    Point-in-time snapshot of all four telemetry metrics plus derived values.
    Computed every 3 minutes and stored on SymbolState.
    """
    timestamp:        datetime
    symbol:           str
    ci:               float     # Choppiness Index (14-period)
    natr:             float     # Normalized ATR (14-period, %)
    rvol:             float     # Relative Volume vs. historical average
    vwap_slope_deg:   float     # OLS slope of VWAP samples (degrees)
    atr14_dollars:    float     # Raw ATR in dollars (used for stop sizing)
    vwap:             float     # Current session VWAP
    vwap_upper_2_2:   float     # VWAP + 2.2σ upper band
    vwap_lower_2_2:   float     # VWAP - 2.2σ lower band
    rsi14:            float     # 14-period Wilder RSI
    regime:           MarketRegime


@dataclass(frozen=True)
class TradeSignal:
    """
    Fully-specified entry signal ready for routing to the order manager.

    Quarter-Kelly sizing formula (from spec):
        quantity = math.floor(max_risk / abs(entry_price - stop_price))
        quantity = max(quantity, 0)  # Zero is invalid — signal suppressed upstream
    """
    timestamp:    datetime
    symbol:       str
    strategy:     str        # "15m_ORB" or "VWAP_MR"
    direction:    str        # Always "LONG" (leveraged ETFs, long-only)
    entry_price:  Decimal    # Limit price for order submission
    stop_price:   Decimal    # Tier-1 initial stop reference
    target_price: Decimal    # Profit target
    quantity:     int        # Floored whole-integer share count
    max_risk_usd: Decimal    # Capped at $10.00 per spec
    regime:       MarketRegime
    metrics:      RegimeMetrics


# ===========================================================================
# Internal: Mutable bar accumulator
# ===========================================================================

class _BarBuilder:
    """
    Accumulates streaming tick data into a 1-minute OHLCV bar.

    The Schwab LEVELONE_EQUITIES stream delivers total_volume as a cumulative
    session counter (field 8). Per-bar volume is computed as the delta between
    the cumulative volume at bar open vs. bar close.
    """

    __slots__ = (
        "minute", "open", "high", "low", "close",
        "start_volume", "end_volume",
    )

    def __init__(
        self,
        minute:       datetime,
        first_price:  float,
        start_volume: int,
    ) -> None:
        self.minute       = minute        # EDT minute-truncated timestamp
        self.open         = first_price
        self.high         = first_price
        self.low          = first_price
        self.close        = first_price
        self.start_volume = start_volume
        self.end_volume   = start_volume

    def update(self, price: float, total_volume: int) -> None:
        if price > self.high:
            self.high = price
        if price < self.low:
            self.low = price
        self.close      = price
        self.end_volume = max(self.end_volume, total_volume)

    def to_candle(self) -> CandleBar:
        volume = max(0, self.end_volume - self.start_volume)
        return CandleBar(
            timestamp=self.minute,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=volume,
        )


# ===========================================================================
# Per-symbol state container
# ===========================================================================

class SymbolState:
    """
    All mutable per-symbol state required for regime detection and strategy logic.

    Ring buffers (deque with maxlen) provide automatic eviction of stale data.
    Thread safety is handled at the StrategyEngine level (per-symbol lock).
    """

    def __init__(self, symbol: str) -> None:
        self.symbol: str = symbol

        # === 1-minute OHLCV bar history ===
        # maxlen=30: enough for CI(14) + RSI(14) with headroom
        self.bars: Deque[CandleBar] = deque(maxlen=30)

        # === VWAP slope samples (60s cadence, 15-min rolling window) ===
        self.vwap_samples: Deque[VWAPSample] = deque(maxlen=15)

        # === In-progress 1m bar builder ===
        self.current_bar: Optional[_BarBuilder] = None

        # === Session-level VWAP accumulators (reset at 09:30 EDT) ===
        # Using Welford-style weighted accumulation for numerical stability
        self.session_cum_pv:   float = 0.0   # Σ(price × volume)
        self.session_cum_vol:  float = 0.0   # Σ(volume)
        self.session_cum_pv_sq: float = 0.0  # Σ(volume × price²) for variance

        # === ORB (Opening Range Breakout) state ===
        self.orb_tracking_high: float = 0.0
        self.orb_tracking_low:  float = float("inf")
        self.orb_high:          Optional[float] = None    # Finalized at 09:45
        self.orb_low:           Optional[float] = None
        self.orb_finalized:     bool = False
        self.orb_signal_fired:  bool = False              # One ORB signal per day

        # === Regime state ===
        self.current_regime:    MarketRegime = MarketRegime.INSUFFICIENT_DATA
        self.last_regime_eval:  Optional[datetime] = None
        self.last_metrics:      Optional[RegimeMetrics] = None

        # === Latest tick values (updated every tick) ===
        self.last_bid:          float = 0.0
        self.last_ask:          float = 0.0
        self.last_price:        float = 0.0
        self.last_total_volume: int = 0

        # === VWAP slope sample timer ===
        self.last_vwap_sample:  Optional[datetime] = None
        self.session_open_time: Optional[datetime] = None

        # === Historical RVOL data ===
        # Key = minute-of-day (0-1439), Value = 20-day avg volume for that minute
        self.historical_minute_volumes: Dict[int, float] = {}

        # === Signal deduplication ===
        # { strategy_name: last_signal_datetime }
        self.last_signal_times: Dict[str, datetime] = {}


# ===========================================================================
# Pure computation helpers
# ===========================================================================

def _compute_true_ranges(bars: List[CandleBar]) -> List[float]:
    """
    Compute 1-period True Range for each bar (except the first, which has no prev_close).

    TR = max(high - low, |high - prev_close|, |low - prev_close|)

    Args:
        bars: Ordered list of CandleBar (oldest first).

    Returns:
        List of TR values, length = len(bars) - 1.
    """
    trs = []
    for i in range(1, len(bars)):
        prev_close = bars[i - 1].close
        b = bars[i]
        tr = max(
            b.high - b.low,
            abs(b.high - prev_close),
            abs(b.low  - prev_close),
        )
        trs.append(tr)
    return trs


def _compute_atr14(bars: List[CandleBar]) -> Optional[float]:
    """
    Compute the simple 14-period Average True Range in dollars.
    Requires at least 15 bars (14 TR values).
    """
    if len(bars) < 15:
        return None
    trs = _compute_true_ranges(bars[-15:])  # 14 TR values from 15 bars
    return sum(trs) / len(trs)


def _compute_ci(bars: List[CandleBar], period: int = 14) -> Optional[float]:
    """
    Choppiness Index (CI) — measures market trendiness vs. choppiness.

    Formula:
        CI(n) = 100 × log10(Σ ATR₁ over n periods / (HH_n − LL_n)) / log10(n)

    Range: 100 (maximum chop) → 0 (perfect trend).
    Spec thresholds: < 38.2 = Trend, > 61.8 = Chop.

    Requires period+1 bars (need prev_close for each ATR computation).
    """
    needed = period + 1
    if len(bars) < needed:
        return None

    window = list(bars[-needed:])
    trs    = _compute_true_ranges(window)  # len = period

    atr_sum    = sum(trs)
    highest_hi = max(b.high for b in window)
    lowest_lo  = min(b.low  for b in window)

    price_range = highest_hi - lowest_lo
    if price_range <= 0 or atr_sum <= 0:
        return None

    ci = 100.0 * math.log10(atr_sum / price_range) / math.log10(period)
    return ci


def _compute_natr(bars: List[CandleBar], period: int = 14) -> Optional[float]:
    """
    Normalized ATR — ATR expressed as a percentage of closing price.

    Formula: NATR = (ATR14 / close) × 100

    Requires 15 bars.
    """
    if len(bars) < 15:
        return None
    atr = _compute_atr14(bars)
    if atr is None:
        return None
    last_close = bars[-1].close
    if last_close <= 0:
        return None
    return (atr / last_close) * 100.0


def _compute_rsi(bars: List[CandleBar], period: int = 14) -> Optional[float]:
    """
    14-period Wilder's RSI.

    Seeded with a simple average of the first `period` gains/losses, then
    propagated with Wilder's smoothing:
        avg_gain_t = (avg_gain_{t-1} × (period-1) + gain_t) / period

    Oversold threshold per spec: RSI < 28 (not the standard 30).
    Requires period+1 bars (period price changes).
    """
    needed = period + 1
    if len(bars) < needed:
        return None

    closes = [b.close for b in bars[-needed:]]

    gains  = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(0.0, diff))
        losses.append(max(0.0, -diff))

    # Seed: simple average of first `period` values
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Wilder's smoothing for any remaining bars (covers look-ahead bars)
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0.0:
        return 100.0  # All up-moves
    rs  = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi


def _compute_vwap_and_bands(
    cum_pv: float,
    cum_vol: float,
    cum_pv_sq: float,
    sigma: float = 2.2,
) -> Tuple[float, float, float]:
    """
    Compute session VWAP, upper band, and lower band.

    VWAP = Σ(price × volume) / Σ(volume)

    Volume-weighted variance (for band calculation):
        var = Σ(volume × price²) / Σ(volume)  −  VWAP²
            = cum_pv_sq / cum_vol  −  VWAP²
    std_dev = √var
    upper   = VWAP + sigma × std_dev
    lower   = VWAP − sigma × std_dev

    Args:
        cum_pv:    Cumulative Σ(price × volume)
        cum_vol:   Cumulative Σ(volume)
        cum_pv_sq: Cumulative Σ(volume × price²)
        sigma:     Band width in standard deviations (spec: 2.2)

    Returns:
        (vwap, upper_band, lower_band)
    """
    if cum_vol <= 0:
        return 0.0, 0.0, 0.0

    vwap    = cum_pv / cum_vol
    var_raw = (cum_pv_sq / cum_vol) - (vwap * vwap)
    variance = max(0.0, var_raw)  # Clamp floating-point underflow
    std_dev  = math.sqrt(variance)

    return vwap, vwap + sigma * std_dev, vwap - sigma * std_dev


def _vwap_slope_degrees(samples: List[VWAPSample]) -> float:
    """
    Compute the OLS (Ordinary Least Squares) slope of VWAP over the 15-minute
    rolling sample window and convert to degrees.

    Spec: VWAP sampled every 60s over a 15m regression window.
          Positive slope > 15° confirms accumulation (Regime A).

    Args:
        samples: List of VWAPSample sorted by elapsed_sec ascending.

    Returns:
        Slope in degrees. 0.0 if fewer than 2 samples.
    """
    n = len(samples)
    if n < 2:
        return 0.0

    xs = [s.elapsed_sec for s in samples]
    ys = [s.vwap        for s in samples]

    mean_x = sum(xs) / n
    mean_y = sum(ys) / n

    num   = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
    denom = sum((xs[i] - mean_x) ** 2                for i in range(n))

    if denom == 0.0:
        return 0.0

    # slope is in price/second; scale to price/minute for a meaningful angle
    slope_per_min = (num / denom) * 60.0
    return math.degrees(math.atan(slope_per_min))


def _compute_rvol(
    current_bar_volume: int,
    minute_of_day: int,
    historical_volumes: Dict[int, float],
    session_bars: List[CandleBar],
) -> float:
    """
    Relative Volume — current bar volume vs. 20-day historical average for
    this same minute of the day.

    Spec: RVOL > 1.5 confirms breakouts (Regime A); RVOL < 1.0 for Regime B.

    Fallback: If no historical data exists for this minute, use the session's
              own per-minute average volume as the baseline.

    Args:
        current_bar_volume:  Volume of the bar being evaluated.
        minute_of_day:       Current minute of day (0-1439).
        historical_volumes:  Preloaded 20-day average volumes per minute.
        session_bars:        All bars closed this session (for fallback).

    Returns:
        RVOL ratio. Returns 1.0 (neutral) if baseline is zero.
    """
    # Primary: use 20-day historical average for this specific minute
    baseline = historical_volumes.get(minute_of_day)

    if baseline is None or baseline <= 0:
        # Fallback: session average volume per closed bar
        if session_bars:
            vols = [b.volume for b in session_bars if b.volume > 0]
            baseline = (sum(vols) / len(vols)) if vols else 0.0
        else:
            baseline = 0.0

    if baseline <= 0:
        return 1.0  # Neutral — no baseline available

    return current_bar_volume / baseline


def quarter_kelly_size(
    entry_price: float,
    stop_price:  float,
    max_risk:    float = 10.00,
) -> int:
    """
    Quarter-Kelly position sizing — caps maximum dollar risk per trade.

    Spec: Quantity = Floor(max_risk / |Entry_Price − Stop_Price|)
          All quantities floored to whole integers (math.floor).
          Zero-quantity signals are suppressed by the caller.

    Args:
        entry_price: Planned entry price per share.
        stop_price:  Initial stop-loss price per share.
        max_risk:    Maximum acceptable dollar risk (default $10.00).

    Returns:
        Integer share quantity (floored). May be 0 if |entry - stop| > max_risk.
    """
    risk_per_share = abs(entry_price - stop_price)
    if risk_per_share <= 0:
        return 0
    return math.floor(max_risk / risk_per_share)


# ===========================================================================
# Regime Classifier
# ===========================================================================

class RegimeClassifier:
    """
    Stateless classifier: maps a RegimeMetrics snapshot to a MarketRegime.

    Classification rules (from dev plan + research doc):
        Regime A — Trend Expansion:
            CI < 38.2  AND  VWAP slope > 0°  AND  RVOL ≥ 1.5
        Regime B — Mean-Reversion (Range-Bound Compression):
            38.2 ≤ CI ≤ 61.8  AND  RVOL < 1.0
        Regime C — High-Noise Chop:
            CI > 61.8  (trading deactivated regardless of other metrics)

    If Regime A CI conditions are met but secondary confirmations are missing
    (positive slope / RVOL), the regime falls through to B evaluation.
    If no regime matches cleanly, the most conservative applicable regime is used.
    """

    def __init__(self, cfg: dict) -> None:
        reg_cfg = cfg.get("regime", {})
        self.ci_trend: float = float(reg_cfg.get("ci_trend_threshold", 38.2))
        self.ci_chop:  float = float(reg_cfg.get("ci_chop_threshold",  61.8))
        self.rvol_min: float = float(reg_cfg.get("rvol_breakout_min",  1.5))
        self.rvol_max: float = float(reg_cfg.get("rvol_compression_max", 1.0))
        self.slope_min_deg: float = float(reg_cfg.get("vwap_slope_min_degrees", 15.0))

    def classify(self, metrics: RegimeMetrics) -> MarketRegime:
        """Map metrics to a MarketRegime."""

        # Regime C: always check chop first — halts trading unconditionally
        if metrics.ci > self.ci_chop:
            return MarketRegime.HIGH_NOISE_CHOP

        # Regime A: Trend Expansion — CI AND slope AND RVOL confirmations
        if (metrics.ci < self.ci_trend
                and metrics.vwap_slope_deg > 0.0
                and metrics.rvol >= self.rvol_min):
            return MarketRegime.TREND_EXPANSION

        # Regime B: Mean-Reversion — CI in mid-band AND low RVOL
        if (self.ci_trend <= metrics.ci <= self.ci_chop
                and metrics.rvol < self.rvol_max):
            return MarketRegime.MEAN_REVERSION

        # CI < 38.2 but secondary conditions not met → default to B
        # (avoid trading a breakout without volume confirmation)
        if metrics.ci < self.ci_trend:
            logger.debug(
                "Regime: CI=%.1f < %.1f (Trend) but slope=%.1f° / RVOL=%.2f "
                "fail secondaries → defaulting to MEAN_REVERSION.",
                metrics.ci, self.ci_trend, metrics.vwap_slope_deg, metrics.rvol,
            )
            return MarketRegime.MEAN_REVERSION

        # Mid-band CI but RVOL not clearly low → conservative Chop
        return MarketRegime.HIGH_NOISE_CHOP


# ===========================================================================
# Strategy Engine
# ===========================================================================

class StrategyEngine:
    """
    Orchestrates regime detection, VWAP/ORB tracking, and trade signal generation.

    Lifecycle
    ---------
    1. Instantiate with cfg, ledger, mask.
    2. Call preload_historical(symbol, rest_client) for each symbol to load
       20-day per-minute volume baselines for accurate RVOL computation.
    3. Register a signal callback via set_signal_callback(fn).
    4. Wire on_tick into the WebSocket streamer's on_tick attribute.
    5. Signals are emitted via the callback; in Phase 6 this routes to order_manager.

    Thread Safety
    -------------
    Each symbol has its own threading.Lock. The on_tick method is designed
    to be called from the streamer's asyncio event loop (via a thread-safe
    wrapper) without blocking.
    """

    def __init__(
        self,
        cfg: dict,
        ledger,
        mask,
        divergence_engine: Optional[Any] = None,
        event_store: Optional[Any] = None,
    ) -> None:
        """
        Args:
            cfg:    Top-level config dict.
            ledger: ComplianceLedger (for max order value check).
            mask:   UniverseExclusionMask (for symbol tradeability check).
            divergence_engine: Optional SyntheticDivergenceEngine for false liquidity trap guard.
            event_store: Optional SQLiteWALEventStore instance for event telemetry.
        """
        self._cfg    = cfg
        self._ledger = ledger
        self._mask   = mask
        self._divergence_engine = divergence_engine
        self._event_store = event_store

        reg_cfg   = cfg.get("regime", {})
        risk_cfg  = cfg.get("risk",   {})
        strat_cfg = cfg.get("strategies", {})
        orb_cfg   = strat_cfg.get("orb_15m", {})
        mr_cfg    = strat_cfg.get("vwap_mean_reversion", {})

        # Regime evaluation cadence
        self._eval_interval_sec: int = int(reg_cfg.get("evaluation_interval_sec", 180))
        self._natr_period:       int = int(reg_cfg.get("natr_period", 14))
        self._ci_period:         int = int(reg_cfg.get("ci_period",   14))

        # Strategy parameters
        self._orb_rvol_min:   float = float(orb_cfg.get("entry_rvol_min",   1.5))
        self._orb_rr_target:  float = float(orb_cfg.get("risk_reward_target", 2.5))
        self._mr_sigma:       float = float(mr_cfg.get("vwap_band_sigma", 2.2))
        self._mr_rsi_thresh:  float = float(mr_cfg.get("rsi_oversold_threshold", 28))

        # Risk
        self._max_risk: float = float(risk_cfg.get("max_loss_per_trade", 10.00))

        self._classifier = RegimeClassifier(cfg)

        # Per-symbol state  { symbol: (SymbolState, Lock) }
        self._states: Dict[str, Tuple[SymbolState, threading.Lock]] = {}

        # Signal callback: fn(signal: TradeSignal) -> None
        self._signal_cb: Optional[Callable[[TradeSignal], None]] = None

        # Pre-signal filter: fn(symbol: str, strategy: str) -> bool
        self._pre_filter: Optional[Callable[[str, str], bool]] = None

        logger.info(
            "StrategyEngine initialised — eval_interval=%ds, "
            "max_risk=$%.2f, ORB RR=%.1f:1, MR sigma=%.1fσ RSI<%d",
            self._eval_interval_sec, self._max_risk,
            self._orb_rr_target, self._mr_sigma, self._mr_rsi_thresh,
        )

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def set_divergence_engine(self, engine: Any) -> None:
        """Register the SyntheticDivergenceEngine for false liquidity trap suppression."""
        self._divergence_engine = engine
        logger.info("StrategyEngine: synthetic divergence engine registered.")

    def set_event_store(self, store: Any) -> None:
        """Register the SQLiteWALEventStore instance for event telemetry."""
        self._event_store = store

    def _record_trap_suppression(self, symbol: str, strategy: str) -> None:
        """Log a LIQUIDITY_TRAP_SUPPRESSION event to telemetry.db."""
        try:
            if self._event_store is not None:
                store = self._event_store
            else:
                from core.telemetry import SQLiteWALEventStore
                store = SQLiteWALEventStore()
            store.record_event(
                aggregate_id=symbol.upper(),
                event_type="LIQUIDITY_TRAP_SUPPRESSION",
                payload={
                    "symbol": symbol.upper(),
                    "strategy": strategy,
                    "reason": "LIQUIDITY_TRAP_ACTIVE",
                }
            )
        except Exception as exc:
            logger.warning("StrategyEngine: failed to log trap suppression event: %s", exc)

    def set_signal_callback(self, fn: Callable[[TradeSignal], None]) -> None:
        """Register the callback that receives generated TradeSignal objects."""
        self._signal_cb = fn
        logger.info("StrategyEngine: signal callback registered.")

    def set_pre_signal_filter(self, fn: Callable[[str, str], bool]) -> None:
        """Register a pre-signal filter that can veto signals before state is mutated."""
        self._pre_filter = fn
        logger.info("StrategyEngine: pre-signal filter registered.")

    def register_symbol(self, symbol: str) -> None:
        """Add a symbol to the engine's tracking universe if not already present."""
        sym = symbol.upper()
        if sym not in self._states:
            self._states[sym] = (SymbolState(sym), threading.Lock())
            logger.info("StrategyEngine: registered symbol %s.", sym)

    # ------------------------------------------------------------------
    # Historical RVOL preload
    # ------------------------------------------------------------------

    def preload_historical(self, symbol: str, rest_client) -> None:
        """
        Fetch 20 days of 1-minute bar data from the REST API and compute
        the average volume for each minute-of-day (0-1439).

        This populates SymbolState.historical_minute_volumes, enabling
        accurate RVOL calculations against a proper 20-day baseline.

        Args:
            symbol:      Ticker string.
            rest_client: Initialised SchwabRestClient.
        """
        sym = symbol.upper()
        if sym not in self._states:
            self.register_symbol(sym)

        state, lock = self._states[sym]

        try:
            from core.baselines_store import SQLiteBaselinesStore
            bstore = SQLiteBaselinesStore()
            cached_adv = bstore.get_adv_profile(sym)
            if cached_adv:
                with lock:
                    state.historical_minute_volumes = cached_adv
                logger.info(
                    "StrategyEngine: %s RVOL baseline loaded from SQLiteBaselinesStore — %d distinct minutes.",
                    sym, len(cached_adv),
                )
                return
        except Exception as exc:
            logger.debug("SQLite baseline check skipped for %s: %s", sym, exc)

        try:
            if rest_client is None:
                logger.warning("StrategyEngine: no rest_client or cached baselines for %s.", sym)
                return
            logger.info(
                "StrategyEngine: preloading 10-day 1m history for %s…", sym
            )
            history = rest_client.get_price_history(
                symbol=sym,
                period_type="day",
                period=10,
                frequency_type="minute",
                frequency=1,
                need_extended_hours_data=False,
            )

            candles = history.get("candles", [])
            if not candles:
                logger.warning(
                    "StrategyEngine: no historical candles for %s — RVOL will use "
                    "session average fallback.", sym
                )
                return

            # Aggregate volume by minute-of-day across all 20 days
            minute_volumes: Dict[int, List[int]] = {}
            for c in candles:
                dt_utc = datetime.utcfromtimestamp(c["datetime"] / 1000).replace(
                    tzinfo=pytz.utc
                )
                dt_edt = dt_utc.astimezone(_EDT)
                mod     = dt_edt.hour * 60 + dt_edt.minute  # 0-based minute of day
                vol     = int(c.get("volume", 0))
                minute_volumes.setdefault(mod, []).append(vol)

            with lock:
                state.historical_minute_volumes = {
                    mod: sum(vols) / len(vols)
                    for mod, vols in minute_volumes.items()
                    if vols
                }

            logger.info(
                "StrategyEngine: %s RVOL baseline loaded — %d distinct minutes "
                "from %d candles.",
                sym, len(state.historical_minute_volumes), len(candles),
            )

        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "StrategyEngine: failed to preload history for %s: %s — "
                "falling back to session average for RVOL.", sym, exc,
            )

    # ------------------------------------------------------------------
    # Primary tick handler
    # ------------------------------------------------------------------

    def on_tick(self, symbol: str, fields: dict) -> None:
        """
        Process a Level 1 tick from the WebSocket streamer.

        Called from the streamer's on_tick callback (runs in the streamer
        background thread). Non-blocking by design — heavy computation is
        gated by the 3-minute regime evaluation interval.

        Args:
            symbol: Ticker string.
            fields: Dict of field_name → value from LEVELONE_EQUITIES stream.
                    Keys: "bid_price", "ask_price", "last_price",
                          "total_volume", "high_price", "low_price"
        """
        sym = symbol.upper()

        # Auto-register new symbols seen on the stream
        if sym not in self._states:
            self.register_symbol(sym)

        state, lock = self._states[sym]

        # Discard ticks for masked symbols (IRC §1091 compliance)
        if not self._mask.is_symbol_tradeable(sym):
            return

        now = datetime.now(_EDT)

        # Feed tick into synthetic divergence engine if present
        if self._divergence_engine is not None:
            try:
                self._divergence_engine.on_tick(sym, fields, now)
            except Exception as exc:
                logger.debug("StrategyEngine: divergence engine on_tick failed: %s", exc)

        # Only process during regular session hours
        session_open  = now.replace(hour=9,  minute=30, second=0,  microsecond=0)
        session_close = now.replace(hour=16, minute=0,  second=0,  microsecond=0)
        if not (session_open <= now < session_close):
            return

        with lock:
            self._ingest_tick(state, fields, now)

    # ------------------------------------------------------------------
    # Tick ingestion (inside lock)
    # ------------------------------------------------------------------

    def _ingest_tick(
        self,
        state: SymbolState,
        fields: dict,
        now: datetime,
    ) -> None:
        """Update state with new tick data, close bars, and trigger evaluations."""

        # --- Extract tick fields ---
        last_price   = float(fields.get("last_price",   state.last_price   or 0))
        bid_price    = float(fields.get("bid_price",    state.last_bid     or 0))
        ask_price    = float(fields.get("ask_price",    state.last_ask     or 0))
        total_volume = int(  fields.get("total_volume", state.last_total_volume or 0))

        if last_price <= 0:
            return  # Ignore malformed ticks

        # Persist latest quote
        state.last_price        = last_price
        state.last_bid          = bid_price
        state.last_ask          = ask_price
        state.last_total_volume = total_volume

        # --- Session initialisation ---
        session_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        if state.session_open_time != session_open:
            self._reset_session(state, now, session_open, last_price, total_volume)
            return  # Skip further processing on the very first tick of the session

        # --- Update ORB tracking (09:30 – 09:44:59) ---
        orb_end = now.replace(hour=9, minute=45, second=0, microsecond=0)
        if now < orb_end and not state.orb_finalized:
            state.orb_tracking_high = max(state.orb_tracking_high, last_price)
            state.orb_tracking_low  = min(state.orb_tracking_low,  last_price)

        elif now >= orb_end and not state.orb_finalized:
            # Lock in the 15-minute Opening Range
            state.orb_high      = state.orb_tracking_high
            state.orb_low       = state.orb_tracking_low
            state.orb_finalized = True
            logger.info(
                "StrategyEngine: %s ORB finalized — High=%.2f Low=%.2f",
                state.symbol, state.orb_high, state.orb_low,
            )

        # --- 1-minute bar assembly ---
        minute_ts = now.replace(second=0, microsecond=0)

        if state.current_bar is None:
            # Start first bar
            state.current_bar = _BarBuilder(
                minute=minute_ts,
                first_price=last_price,
                start_volume=total_volume,
            )
        elif minute_ts > state.current_bar.minute:
            # Minute boundary crossed → close the completed bar
            closed = state.current_bar.to_candle()
            state.bars.append(closed)
            self._update_vwap_accumulators(state, closed)
            self._maybe_sample_vwap(state, now, session_open)
            self._maybe_evaluate_regime(state, now)

            # Open the new bar
            state.current_bar = _BarBuilder(
                minute=minute_ts,
                first_price=last_price,
                start_volume=total_volume,
            )
        else:
            # Update in-progress bar
            state.current_bar.update(last_price, total_volume)

        # --- Real-time strategy checks on every tick (uses last evaluated regime) ---
        if state.last_metrics is not None:
            self._check_signals(state, now)

    # ------------------------------------------------------------------
    # Session reset (new trading day)
    # ------------------------------------------------------------------

    def _reset_session(
        self,
        state: SymbolState,
        now:    datetime,
        session_open: datetime,
        first_price: float,
        start_volume: int,
    ) -> None:
        """Reset all session-scoped state at each new market open."""
        logger.info(
            "StrategyEngine: %s — new session detected, resetting state.", state.symbol
        )
        state.session_open_time  = session_open
        state.session_cum_pv     = 0.0
        state.session_cum_vol    = 0.0
        state.session_cum_pv_sq  = 0.0
        state.orb_tracking_high  = first_price
        state.orb_tracking_low   = first_price
        state.orb_high           = None
        state.orb_low            = None
        state.orb_finalized      = False
        state.orb_signal_fired   = False
        state.vwap_samples.clear()
        state.last_vwap_sample   = None
        state.current_bar        = _BarBuilder(
            minute=now.replace(second=0, microsecond=0),
            first_price=first_price,
            start_volume=start_volume,
        )
        state.last_signal_times  = {}
        state.current_regime     = MarketRegime.INSUFFICIENT_DATA
        state.last_metrics       = None

    # ------------------------------------------------------------------
    # VWAP accumulator update
    # ------------------------------------------------------------------

    def _update_vwap_accumulators(
        self,
        state: SymbolState,
        bar: CandleBar,
    ) -> None:
        """
        Update session VWAP accumulators using the typical price of a closed bar.
        Typical price = (high + low + close) / 3 (standard VWAP convention).
        """
        if bar.volume <= 0:
            return
        typical = (bar.high + bar.low + bar.close) / 3.0
        state.session_cum_pv    += typical * bar.volume
        state.session_cum_vol   += bar.volume
        state.session_cum_pv_sq += bar.volume * (typical ** 2)

    # ------------------------------------------------------------------
    # VWAP slope sampling (every 60 seconds)
    # ------------------------------------------------------------------

    def _maybe_sample_vwap(
        self,
        state: SymbolState,
        now: datetime,
        session_open: datetime,
    ) -> None:
        """
        Append a VWAP sample every 60 seconds for the slope regression window.
        Spec: sampled every 60s over a rolling 15m (15-sample) window.
        """
        if state.session_cum_vol <= 0:
            return

        # Check 60-second cadence
        if state.last_vwap_sample is not None:
            elapsed_since_last = (now - state.last_vwap_sample).total_seconds()
            if elapsed_since_last < _VWAP_SAMPLE_INTERVAL:
                return

        vwap, _, _ = _compute_vwap_and_bands(
            state.session_cum_pv,
            state.session_cum_vol,
            state.session_cum_pv_sq,
            sigma=self._mr_sigma,
        )

        elapsed_total = (now - session_open).total_seconds()
        state.vwap_samples.append(VWAPSample(elapsed_sec=elapsed_total, vwap=vwap))
        state.last_vwap_sample = now

    # ------------------------------------------------------------------
    # Regime evaluation (every 3 minutes, triggered on bar close)
    # ------------------------------------------------------------------

    def _maybe_evaluate_regime(
        self,
        state: SymbolState,
        now: datetime,
    ) -> None:
        """Gate the regime evaluation to run at most once every 3 minutes."""
        if state.last_regime_eval is not None:
            elapsed = (now - state.last_regime_eval).total_seconds()
            if elapsed < self._eval_interval_sec:
                return

        if len(state.bars) < _BARS_NEEDED:
            return   # Not enough data yet

        self._evaluate_regime(state, now)

    def _evaluate_regime(
        self,
        state: SymbolState,
        now: datetime,
    ) -> None:
        """
        Compute all four metrics and classify the current market regime.
        Updates state.last_metrics and state.current_regime.
        """
        bars = list(state.bars)

        ci   = _compute_ci(bars, self._ci_period)
        natr = _compute_natr(bars, self._natr_period)
        atr  = _compute_atr14(bars)
        rsi  = _compute_rsi(bars)

        if ci is None:
            logger.debug("StrategyEngine: %s — insufficient bars for CI.", state.symbol)
            return

        # VWAP and bands
        vwap, upper, lower = _compute_vwap_and_bands(
            state.session_cum_pv,
            state.session_cum_vol,
            state.session_cum_pv_sq,
            sigma=self._mr_sigma,
        )

        # VWAP slope
        slope_deg = _vwap_slope_degrees(list(state.vwap_samples))

        # RVOL — use the most recently closed bar's volume
        last_bar = bars[-1]
        mod      = last_bar.timestamp.hour * 60 + last_bar.timestamp.minute
        rvol     = _compute_rvol(
            current_bar_volume=last_bar.volume,
            minute_of_day=mod,
            historical_volumes=state.historical_minute_volumes,
            session_bars=bars,
        )

        metrics = RegimeMetrics(
            timestamp=now,
            symbol=state.symbol,
            ci=ci,
            natr=natr  if natr is not None else 0.0,
            rvol=rvol,
            vwap_slope_deg=slope_deg,
            atr14_dollars=atr if atr is not None else 0.0,
            vwap=vwap,
            vwap_upper_2_2=upper,
            vwap_lower_2_2=lower,
            rsi14=rsi if rsi is not None else 50.0,
            regime=MarketRegime.INSUFFICIENT_DATA,  # placeholder; overwritten below
        )

        regime  = self._classifier.classify(metrics)

        # Reconstruct with the actual regime (frozen dataclass)
        metrics = RegimeMetrics(
            **{**metrics.__dict__, "regime": regime}
        )

        prev_regime = state.current_regime
        state.last_metrics   = metrics
        state.current_regime = regime
        state.last_regime_eval = now

        if regime != prev_regime:
            logger.info(
                "REGIME CHANGE | %s | %s → %s | CI=%.1f RVOL=%.2f "
                "slope=%.1f° NATR=%.2f%% RSI=%.1f",
                state.symbol,
                prev_regime.name, regime.name,
                ci, rvol, slope_deg,
                metrics.natr, metrics.rsi14,
            )
        else:
            logger.debug(
                "REGIME HOLD   | %s | %s | CI=%.1f RVOL=%.2f slope=%.1f°",
                state.symbol, regime.name, ci, rvol, slope_deg,
            )

    # ------------------------------------------------------------------
    # Signal checking (every tick, guarded by regime and cooldown)
    # ------------------------------------------------------------------

    def _check_signals(
        self,
        state: SymbolState,
        now: datetime,
    ) -> None:
        """
        Evaluate strategy entry conditions for the current tick.
        Dispatches to the appropriate strategy based on active regime.
        """
        regime = state.current_regime

        if regime == MarketRegime.HIGH_NOISE_CHOP:
            return   # Spec: CI > 61.8 → trading deactivated

        if regime == MarketRegime.INSUFFICIENT_DATA:
            return

        signal: Optional[TradeSignal] = None

        if regime == MarketRegime.TREND_EXPANSION:
            signal = self._check_orb_signal(state, now)

        elif regime == MarketRegime.MEAN_REVERSION:
            signal = self._check_vwap_mr_signal(state, now)

        if signal is not None and signal.quantity >= _MIN_SIGNAL_QUANTITY:
            # Check Synthetic Divergence False Liquidity Trap Guard prior to confirming entry
            if self._divergence_engine and self._divergence_engine.is_trap_active(signal.symbol, now):
                logger.warning(
                    "StrategyEngine: %s %s signal suppressed — LIQUIDITY_TRAP_ACTIVE on synthetic pair.",
                    signal.symbol, signal.strategy
                )
                self._record_trap_suppression(signal.symbol, signal.strategy)
                return

            # Clamp position size against ledger's max single-ticker cap and available buying power
            max_shares_by_cap = int(float(self._ledger.max_single_exposure) // float(signal.entry_price))
            max_shares_by_cash = int((float(self._ledger.settled_cash) - float(self._ledger.cash_buffer)) // float(signal.entry_price))
            max_allowed = min(max_shares_by_cap, max_shares_by_cash)
            clamped_quantity = min(signal.quantity, max_allowed)
            
            if clamped_quantity <= 0:
                logger.warning(
                    "StrategyEngine: %s signal suppressed — 0 shares fit cap ($%.2f) or cash ($%.2f).",
                    signal.symbol, float(self._ledger.max_single_exposure), float(self._ledger.settled_cash) - float(self._ledger.cash_buffer)
                )
                from core.notifier import send_alert
                import threading
                threading.Thread(
                    target=send_alert,
                    args=(
                        f"[SUPPRESSED] {signal.symbol} Order Rejected",
                        f"Zero shares fit within the single-ticker cap (${float(self._ledger.max_single_exposure):.2f}) or available buying power.",
                        "high",
                        "warning,no_entry_sign"
                    ),
                    daemon=True
                ).start()
                return

            signal = replace(signal, quantity=clamped_quantity)
            # Verify compliance ledger approves the order cost
            order_cost = signal.entry_price * signal.quantity
            allowed, reason = self._ledger.check_order_allowed(order_cost)
            if not allowed:
                logger.warning(
                    "StrategyEngine: signal for %s suppressed by ledger: %s",
                    signal.symbol, reason,
                )
                from core.notifier import send_alert
                import threading
                threading.Thread(
                    target=send_alert,
                    args=(
                        f"[SUPPRESSED] {signal.symbol} Order Rejected",
                        f"Reason: {reason}",
                        "high",
                        "warning,no_entry_sign"
                    ),
                    daemon=True
                ).start()
                return

            # Fix Premature Flag Consumption: Only consume the ORB slot if it passed the ledger gate
            if signal.strategy == "15m_ORB":
                state.orb_signal_fired = True

            state.last_signal_times[signal.strategy] = now
            
            risk_amt = float(signal.entry_price - signal.stop_price) * signal.quantity
            total_notional = float(signal.entry_price) * signal.quantity
            logger.info(
                "SIGNAL %-10s | %s %s × %d @ $%.2f | stop=$%.2f target=$%.2f | "
                "risk=$%.2f",
                signal.strategy, signal.symbol, signal.direction,
                signal.quantity, signal.entry_price,
                signal.stop_price, signal.target_price,
                risk_amt,
            )

            if self._signal_cb:
                try:
                    self._signal_cb(signal)
                    
                    from core.notifier import send_alert
                    import threading
                    threading.Thread(
                        target=send_alert,
                        args=(
                            f"[ROUTED] {signal.direction} {signal.quantity} {signal.symbol} @ ~${float(signal.entry_price):.2f}",
                            f"Strategy: {signal.strategy}\nStop Distance: ${float(signal.entry_price - signal.stop_price):.2f}\nRisk Capital: ${risk_amt:.2f}\nTotal Notional: ${total_notional:.2f}",
                            "default",
                            "rocket,moneybag"
                        ),
                        daemon=True
                    ).start()
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "StrategyEngine: signal_callback raised: %s", exc
                    )

    # ------------------------------------------------------------------
    # Strategy 1 — 15-Minute Opening Range Breakout
    # ------------------------------------------------------------------

    def _check_orb_signal(
        self,
        state: SymbolState,
        now: datetime,
    ) -> Optional[TradeSignal]:
        """
        15m ORB entry conditions (Regime A — Trend Expansion):
            1. ORB has been finalized (09:45 AM has passed).
            2. No prior ORB signal fired today.
            3. Last price breaks above ORB high.
            4. RVOL ≥ 1.5× (volume confirmation per spec).
            5. Positive VWAP slope (confirmed accumulation).
            6. Signal cooldown not active.

        Stop:   ORB low.
        Target: entry + 2.5 × (entry − ORB low).   [2.5:1 R/R from spec]
        Size:   Floor(10.00 / (entry − stop)).
        """
        if not state.orb_finalized or state.orb_signal_fired:
            return None

        if state.orb_high is None or state.orb_low is None:
            return None

        # Entry is slightly above the ORB high to confirm the breakout
        entry = state.last_price
        if entry <= state.orb_high:
            return None   # Price has not broken above ORB high

        metrics = state.last_metrics
        if metrics is None:
            return None

        # Secondary confirmations
        if metrics.rvol < self._orb_rvol_min:
            logger.debug(
                "StrategyEngine: %s ORB entry suppressed — RVOL=%.2f < %.2f",
                state.symbol, metrics.rvol, self._orb_rvol_min,
            )
            return None

        if metrics.vwap_slope_deg <= 0.0:
            logger.debug(
                "StrategyEngine: %s ORB entry suppressed — VWAP slope=%.1f° ≤ 0°",
                state.symbol, metrics.vwap_slope_deg,
            )
            return None

        # Signal cooldown check
        if self._in_cooldown(state, "15m_ORB", now):
            return None

        stop_price   = state.orb_low
        risk_per_shr = entry - stop_price
        if risk_per_shr <= 0:
            return None   # ORB low ≥ current price — data anomaly

        target_price = entry + self._orb_rr_target * risk_per_shr
        quantity     = quarter_kelly_size(entry, stop_price, self._max_risk)

        if quantity < _MIN_SIGNAL_QUANTITY:
            logger.debug(
                "StrategyEngine: %s ORB quantity=0 (risk/shr=$%.4f > $%.2f cap) "
                "— signal suppressed.",
                state.symbol, risk_per_shr, self._max_risk,
            )
            return None

        # Check Synthetic Divergence False Liquidity Trap Guard prior to confirming ORB entry
        if self._divergence_engine and self._divergence_engine.is_trap_active(state.symbol, now):
            logger.warning(
                "StrategyEngine: %s 15m_ORB entry suppressed — LIQUIDITY_TRAP_ACTIVE on synthetic pair.",
                state.symbol
            )
            self._record_trap_suppression(state.symbol, "15m_ORB")
            return None

        # Pre-signal gate check (e.g. microstructure veto before burning ORB or starting cooldown)
        if self._pre_filter and not self._pre_filter(state.symbol, "15m_ORB"):
            return None

        return TradeSignal(
            timestamp=now,
            symbol=state.symbol,
            strategy="15m_ORB",
            direction="LONG",
            entry_price=Decimal(str(round(entry,       2))),
            stop_price= Decimal(str(round(stop_price,  2))),
            target_price=Decimal(str(round(target_price, 2))),
            quantity=quantity,
            max_risk_usd=Decimal(str(self._max_risk)),
            regime=state.current_regime,
            metrics=metrics,
        )

    # ------------------------------------------------------------------
    # Strategy 2 — VWAP Mean-Reversion
    # ------------------------------------------------------------------

    def _check_vwap_mr_signal(
        self,
        state: SymbolState,
        now: datetime,
    ) -> Optional[TradeSignal]:
        """
        VWAP Mean-Reversion entry conditions (Regime B — Range-Bound Compression):
            1. Bid price hits or crosses the lower 2.2σ VWAP band.
            2. RSI(14) < 28  (oversold per spec).
            3. Signal cooldown not active.

        Stop:   Entry − 1× ATR14 (dollar ATR-based stop below the lower band).
        Target: Session VWAP midline.
        Size:   Floor(10.00 / (entry − stop)).
        """
        metrics = state.last_metrics
        if metrics is None or metrics.vwap <= 0:
            return None

        # Use bid price for a realistic fill on the lower band touch
        bid = state.last_bid if state.last_bid > 0 else state.last_price
        if bid <= 0:
            return None

        # Condition 1: bid at or below lower 2.2σ band
        if bid > metrics.vwap_lower_2_2:
            return None

        # Condition 2: RSI < 28 (oversold confirmation per spec)
        if metrics.rsi14 >= self._mr_rsi_thresh:
            logger.debug(
                "StrategyEngine: %s VWAP MR suppressed — RSI=%.1f ≥ %d",
                state.symbol, metrics.rsi14, self._mr_rsi_thresh,
            )
            return None

        # Signal cooldown
        if self._in_cooldown(state, "VWAP_MR", now):
            return None

        # Entry: use the current bid (conservative — we buy at the lower band)
        entry = bid

        # Stop: 1× ATR below entry — natural volatility-based stop
        atr_stop_dist = metrics.atr14_dollars
        if atr_stop_dist <= 0:
            logger.debug(
                "StrategyEngine: %s VWAP MR suppressed — ATR=0 (no stop size).",
                state.symbol,
            )
            return None
        stop_price  = entry - atr_stop_dist

        # Target: VWAP midline
        target_price = metrics.vwap

        if target_price <= entry:
            # Price already above VWAP — no mean-reversion room
            return None

        quantity = quarter_kelly_size(entry, stop_price, self._max_risk)
        if quantity < _MIN_SIGNAL_QUANTITY:
            logger.debug(
                "StrategyEngine: %s VWAP MR quantity=0 (ATR=$%.4f > $%.2f cap) "
                "— signal suppressed.",
                state.symbol, atr_stop_dist, self._max_risk,
            )
            return None

        # Pre-signal gate check (e.g. microstructure veto before triggering cooldown)
        if self._pre_filter and not self._pre_filter(state.symbol, "VWAP_MR"):
            return None

        return TradeSignal(
            timestamp=now,
            symbol=state.symbol,
            strategy="VWAP_MR",
            direction="LONG",
            entry_price= Decimal(str(round(entry,        2))),
            stop_price=  Decimal(str(round(stop_price,   2))),
            target_price=Decimal(str(round(target_price, 2))),
            quantity=quantity,
            max_risk_usd=Decimal(str(self._max_risk)),
            regime=state.current_regime,
            metrics=metrics,
        )

    # ------------------------------------------------------------------
    # Signal cooldown helper
    # ------------------------------------------------------------------

    def _in_cooldown(
        self,
        state:    SymbolState,
        strategy: str,
        now:      datetime,
    ) -> bool:
        """
        Return True if a signal for this symbol+strategy was emitted
        within the last _SIGNAL_COOLDOWN_SEC seconds.
        """
        last = state.last_signal_times.get(strategy)
        if last is None:
            return False
        return (now - last).total_seconds() < _SIGNAL_COOLDOWN_SEC

    # ------------------------------------------------------------------
    # Public accessors
    # ------------------------------------------------------------------

    def get_regime(self, symbol: str) -> MarketRegime:
        """Return the current regime for a symbol (thread-safe)."""
        sym = symbol.upper()
        if sym not in self._states:
            return MarketRegime.INSUFFICIENT_DATA
        state, lock = self._states[sym]
        with lock:
            return state.current_regime

    def get_metrics(self, symbol: str) -> Optional[RegimeMetrics]:
        """Return the last computed RegimeMetrics for a symbol."""
        sym = symbol.upper()
        if sym not in self._states:
            return None
        state, lock = self._states[sym]
        with lock:
            return state.last_metrics


# ===========================================================================
# Factory
# ===========================================================================

def build_from_config(cfg: dict, ledger, mask, divergence_engine=None) -> StrategyEngine:
    """
    Construct a StrategyEngine from the loaded config.yaml dict.

    Args:
        cfg:    Top-level config dict.
        ledger: ComplianceLedger instance.
        mask:   UniverseExclusionMask instance.
        divergence_engine: Optional SyntheticDivergenceEngine instance.

    Returns:
        Configured StrategyEngine (symbols not yet registered — call
        register_symbol() or preload_historical() per symbol before starting).
    """
    return StrategyEngine(cfg=cfg, ledger=ledger, mask=mask, divergence_engine=divergence_engine)
