"""
data/multi_rate_sync.py
=======================
Multi-Rate Telemetry Synchronizer for SchwabEngine Dual-Sleeve Architecture (#90).

Coordinates three decoupled telemetry ingestion planes with distinct frequency and latency profiles:
1. Hot Plane (Sub-50ms / Streaming):
   - Ingests streaming Schwab LEVELONE_EQUITIES quotes (or REST market data).
   - Computes mid-price, volume-weighted microprice, and L1 Order Book Imbalance (OBI).
   - Enforces quote-age watermarking (stale quote rejection).
   - Dynamically marks both the 1x Robo-Core sleeve C(t) and 3x Tactical Satellite sleeve S(t)
     to market, producing continuous live Net Liquidation Value N_live(t) without waiting for REST.
   - Tracks live instantaneous and rolling return r_core(t) of the Robo-Core sleeve for Strategy D
     drawdown recovery acceleration.

2. Warm Plane (On-Fill + 60s Periodic Reconciliation & Mid-Session Hot Boot):
   - Ingests Schwab /trader/v1/accounts payloads.
   - Categorizes positions into 1x Robo-Core C(t) and 3x Tactical Satellite S(t).
   - Robust Mid-Session Hot Boot: Syncs live broker positions, cash, and balances at any mid-day
     point without requiring a 09:30 AM baseline or 09:45 AM pre-market cash reference (S0).
   - Reconciles broker holdings against internal inventory and updates sleeve weights.

3. Cold Plane (Event-Driven / Macroeconomic Ingestion):
   - Ingests FRED daily High Yield OAS (BAMLH0A0HYM2) and Economic Surprise Z-scores (Z_s).
   - Ingests Gemini NLP Dovish/Hawkish sentiment polarity S_nlp(t0).
   - Applies continuous exponential time decay e^(-lambda * dt) to sentiment scores.
   - Fuses daily macro anchors with live intraday proxies into feature vector M(t) for the HMM Meta-Governor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import logging
import math
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("SchwabEngine.MultiRateSync")

# Default core basket proxies (1x beta) and tactical satellite universe (3x LETFs)
DEFAULT_CORE_SYMBOLS: Set[str] = {"SPY", "QQQ", "IWM", "HYG", "IEF"}
DEFAULT_SATELLITE_SYMBOLS: Set[str] = {"SOXL", "TQQQ", "TNA"}
DEFAULT_SWEEP_SYMBOLS: Set[str] = {"SWVXX"}

# Staleness tolerance: ticks older than 5.0 seconds are flagged as stale
DEFAULT_STALE_TOLERANCE_SEC: float = 5.0

# Sentiment exponential half-life (e.g. 14,400 seconds = 4 hours)
DEFAULT_SENTIMENT_LAMBDA: float = math.log(2.0) / 14400.0

# Credit proxy symbols and sensitivity: HYG's effective spread duration is ~3.5y, so a 1 bp move in
# HY OAS moves the HYG/IEF price ratio by roughly 0.035% (3.5e-4 in log terms).
CREDIT_RISK_SYMBOL: str = "HYG"
CREDIT_SAFE_SYMBOL: str = "IEF"
HYG_IEF_LOG_RATIO_PER_BP: float = 3.5e-4


@dataclass
class HotQuoteUpdate:
    """Microstructure metrics derived from a single Level 1 equity quote."""
    symbol: str
    bid: float
    ask: float
    last: float
    bid_size: int
    ask_size: int
    mid_price: float
    microprice: float
    order_book_imbalance: float
    volume: int
    quote_epoch_ms: int
    received_timestamp: float
    is_stale: bool


@dataclass
class SleevePosition:
    """Position representation within a portfolio sleeve."""
    symbol: str
    quantity: float
    average_cost: float
    current_price: float
    market_value: float
    unrealized_pnl: float
    last_update_ts: float
    sleeve: str  # "CORE", "SATELLITE", or "SWEEP"


@dataclass
class DualSleeveState:
    """Mark-to-market valuation and allocation across both sleeves."""
    core_market_value: float
    satellite_market_value: float
    cash_buffer: float
    net_liquidation_value: float
    core_weight: float
    satellite_weight: float
    core_return_instantaneous: float
    core_return_cumulative: float
    is_hot_boot_complete: bool
    boot_timestamp: float
    last_mark_timestamp: float
    positions: Dict[str, SleevePosition] = field(default_factory=dict)


@dataclass
class ColdMacroState:
    """Macroeconomic and sentiment telemetry state."""
    fred_oas_bps: float = 320.0
    economic_surprise_z_score: float = 0.0
    sentiment_polarity_raw: float = 0.0
    sentiment_timestamp: float = 0.0
    sentiment_decayed: float = 0.0
    intraday_credit_proxy_bps: float = 320.0
    hybrid_credit_spread_bps: float = 320.0
    macro_regime: str = "REGIME_A"
    market_bias: str = "BULLISH"
    last_macro_update_ts: float = 0.0


class MultiRateStateSynchronizer:
    """
    Coordinates multi-rate telemetry streams across Hot, Warm, and Cold planes
    for scale-invariant, dual-sleeve portfolio execution.
    """

    def __init__(
        self,
        core_symbols: Optional[Set[str]] = None,
        satellite_symbols: Optional[Set[str]] = None,
        stale_tolerance_sec: float = DEFAULT_STALE_TOLERANCE_SEC,
        sentiment_decay_lambda: float = DEFAULT_SENTIMENT_LAMBDA,
    ) -> None:
        self._lock = threading.RLock()
        self.core_symbols: Set[str] = {s.upper() for s in (core_symbols or DEFAULT_CORE_SYMBOLS)}
        self.satellite_symbols: Set[str] = {s.upper() for s in (satellite_symbols or DEFAULT_SATELLITE_SYMBOLS)}
        self.stale_tolerance_sec: float = stale_tolerance_sec
        self.sentiment_decay_lambda: float = sentiment_decay_lambda

        # --- Hot Plane State ---
        self._latest_quotes: Dict[str, HotQuoteUpdate] = {}
        self._core_anchor_prices: Dict[str, float] = {}
        self._initial_core_market_value: Optional[float] = None

        # --- Warm Plane State (Dual-Sleeve Ledger) ---
        self._positions: Dict[str, SleevePosition] = {}
        self._cash_buffer: float = 0.0
        self._broker_nlv: float = 0.0
        self._is_hot_boot_complete: bool = False
        self._boot_timestamp: float = 0.0
        self._last_warm_sync_ts: float = 0.0

        # --- Cold Plane State ---
        self._macro_state: ColdMacroState = ColdMacroState()

        # --- Live HYG/IEF credit proxy state ---
        self._tick_cache: Dict[str, Dict[str, float]] = {}
        self._credit_ref_ratio: Optional[float] = None
        self._credit_ref_source: str = "UNSET"
        self._credit_live_ratio: Optional[float] = None
        self._credit_proxy_is_live: bool = False

    # =========================================================================
    # 1. HOT PLANE (Sub-50ms / Level 1 Streaming Ingestion)
    # =========================================================================

    def on_level1_quote(self, payload: Dict[str, Any]) -> HotQuoteUpdate:
        """
        Processes streaming or REST Level 1 quote payload.
        Computes mid-price, volume-weighted microprice, and L1 Order Book Imbalance (OBI).
        Continuously marks portfolio positions to market.
        """
        now = time.time()

        # Parse symbol and quote components across Schwab Streaming & REST formats
        symbol, bid, ask, last, bid_size, ask_size, volume, quote_epoch_ms = self._extract_quote_fields(payload)
        symbol = symbol.upper()

        # Enforce quote-age watermarking
        quote_age_sec = (now - (quote_epoch_ms / 1000.0)) if quote_epoch_ms > 0 else 0.0
        is_stale = quote_age_sec > self.stale_tolerance_sec if quote_epoch_ms > 0 else False

        # Mid-price
        if bid > 0.0 and ask > 0.0:
            mid_price = (bid + ask) / 2.0
        elif last > 0.0:
            mid_price = last
        else:
            mid_price = max(bid, ask, 0.0)

        # Volume-Weighted Microprice & Order Book Imbalance (OBI)
        total_size = bid_size + ask_size
        if total_size > 0 and bid > 0.0 and ask > 0.0:
            microprice = (bid * ask_size + ask * bid_size) / float(total_size)
            obi = (bid_size - ask_size) / float(total_size)
        else:
            microprice = mid_price if mid_price > 0.0 else last
            obi = 0.0

        update = HotQuoteUpdate(
            symbol=symbol,
            bid=round(bid, 4),
            ask=round(ask, 4),
            last=round(last, 4),
            bid_size=bid_size,
            ask_size=ask_size,
            mid_price=round(mid_price, 4),
            microprice=round(microprice, 4),
            order_book_imbalance=round(obi, 4),
            volume=volume,
            quote_epoch_ms=quote_epoch_ms,
            received_timestamp=now,
            is_stale=is_stale,
        )

        with self._lock:
            self._latest_quotes[symbol] = update

            # Update live mark-to-market if we hold this position
            if symbol in self._positions:
                pos = self._positions[symbol]
                new_price = microprice if microprice > 0.0 else mid_price
                if new_price > 0.0:
                    pos.current_price = new_price
                    pos.market_value = pos.quantity * new_price
                    pos.unrealized_pnl = (new_price - pos.average_cost) * pos.quantity
                    pos.last_update_ts = now

            # Anchor Core reference prices if not yet set
            if symbol in self.core_symbols and symbol not in self._core_anchor_prices and microprice > 0.0:
                self._core_anchor_prices[symbol] = microprice

            # Re-derive the intraday credit proxy whenever either leg of HYG/IEF ticks
            if symbol in (CREDIT_RISK_SYMBOL, CREDIT_SAFE_SYMBOL):
                self._update_credit_proxy_locked()

        return update

    def on_streamer_tick(self, symbol: str, fields: Dict[str, Any]) -> HotQuoteUpdate:
        """
        Adapter for ``data.streamer.SchwabStreamer.on_tick(symbol, fields)``.

        Schwab LEVELONE_EQUITIES frames are delta updates (only changed fields are present), so the
        fields are merged into a per-symbol cache before the full quote is evaluated. The streamer
        frames carry no quote timestamp, so receipt time is used as the quote epoch.
        """
        sym = str(symbol).upper()
        with self._lock:
            cache = self._tick_cache.setdefault(sym, {})
            for key in ("bid_price", "ask_price", "last_price", "bid_size", "ask_size", "total_volume"):
                val = fields.get(key)
                if val is not None:
                    try:
                        cache[key] = float(val)
                    except (TypeError, ValueError):
                        continue
            merged = dict(cache)
        payload = {
            "symbol": sym,
            "bidPrice": merged.get("bid_price", 0.0),
            "askPrice": merged.get("ask_price", 0.0),
            "lastPrice": merged.get("last_price", 0.0),
            "bidSize": int(merged.get("bid_size", 0)),
            "askSize": int(merged.get("ask_size", 0)),
            "totalVolume": int(merged.get("total_volume", 0)),
            "quoteTime": int(time.time() * 1000),
        }
        return self.on_level1_quote(payload)

    def get_latest_quote(self, symbol: str) -> Optional[HotQuoteUpdate]:
        """Returns the most recent hot-plane quote for ``symbol`` (None if never ticked)."""
        with self._lock:
            return self._latest_quotes.get(symbol.upper())

    def set_credit_reference(self, hyg_close: float, ief_close: float, source: str = "PRIOR_CLOSE") -> None:
        """
        Sets the HYG/IEF reference ratio against which the intraday credit proxy is measured
        (normally the prior-session close, so the proxy reflects the day's credit move).
        """
        if hyg_close <= 0.0 or ief_close <= 0.0:
            return
        with self._lock:
            self._credit_ref_ratio = hyg_close / ief_close
            self._credit_ref_source = source
            self._update_credit_proxy_locked()

    def get_credit_proxy_state(self) -> Dict[str, Any]:
        """Snapshot of the live credit proxy inputs/outputs for telemetry."""
        with self._lock:
            return {
                "hyg_ief_ratio": self._credit_live_ratio,
                "reference_ratio": self._credit_ref_ratio,
                "reference_source": self._credit_ref_source,
                "proxy_is_live": self._credit_proxy_is_live,
                "fred_oas_bps": self._macro_state.fred_oas_bps,
                "intraday_credit_proxy_bps": round(self._macro_state.intraday_credit_proxy_bps, 2),
                "hybrid_credit_spread_bps": round(self._macro_state.hybrid_credit_spread_bps, 2),
            }

    def _update_credit_proxy_locked(self) -> None:
        """
        Intraday credit spread proxy from the live HYG/IEF ratio:

            proxy_bps = OAS_fred - ln(r_live / r_ref) / HYG_IEF_LOG_RATIO_PER_BP

        A rising HYG/IEF ratio means credit is tightening, so the proxy falls below the FRED anchor.
        If no reference close was supplied, the first observed ratio becomes the reference.
        """
        hyg = self._latest_quotes.get(CREDIT_RISK_SYMBOL)
        ief = self._latest_quotes.get(CREDIT_SAFE_SYMBOL)
        if hyg is None or ief is None:
            return
        px_h = hyg.mid_price if hyg.mid_price > 0.0 else hyg.last
        px_i = ief.mid_price if ief.mid_price > 0.0 else ief.last
        if px_h <= 0.0 or px_i <= 0.0:
            return
        ratio = px_h / px_i
        self._credit_live_ratio = ratio
        if self._credit_ref_ratio is None:
            self._credit_ref_ratio = ratio
            self._credit_ref_source = "FIRST_LIVE_TICK"
        delta_bps = -math.log(ratio / self._credit_ref_ratio) / HYG_IEF_LOG_RATIO_PER_BP
        self._macro_state.intraday_credit_proxy_bps = self._macro_state.fred_oas_bps + delta_bps
        self._credit_proxy_is_live = True
        self._recalculate_hybrid_credit_spread_locked()

    def _extract_quote_fields(self, payload: Dict[str, Any]) -> Tuple[str, float, float, float, int, int, int, int]:
        """Normalizes diverse Schwab Streaming and REST payload schemas."""
        symbol = ""
        bid = 0.0
        ask = 0.0
        last = 0.0
        bid_size = 0
        ask_size = 0
        volume = 0
        quote_epoch_ms = 0

        # Case A: Streaming LEVELONE_EQUITIES payload
        if "content" in payload and isinstance(payload["content"], list) and len(payload["content"]) > 0:
            c = payload["content"][0]
            symbol = str(c.get("key") or c.get("0") or "")
            bid = float(c.get("bidPrice") or c.get("1") or 0.0)
            ask = float(c.get("askPrice") or c.get("2") or 0.0)
            last = float(c.get("lastPrice") or c.get("3") or 0.0)
            bid_size = int(c.get("bidSize") or c.get("4") or 0)
            ask_size = int(c.get("askSize") or c.get("5") or 0)
            volume = int(c.get("totalVolume") or c.get("8") or 0)
            quote_epoch_ms = int(c.get("quoteTime") or c.get("24") or payload.get("timestamp") or 0)

        # Case B: REST Market Data quote spec
        elif "quote" in payload and "symbol" in payload:
            symbol = str(payload["symbol"])
            q = payload["quote"]
            bid = float(q.get("bidPrice", 0.0))
            ask = float(q.get("askPrice", 0.0))
            last = float(q.get("lastPrice", 0.0))
            bid_size = int(q.get("bidSize", 0))
            ask_size = int(q.get("askSize", 0))
            volume = int(q.get("totalVolume", 0))
            quote_epoch_ms = int(q.get("quoteTime", 0))

        # Case C: Flattened dictionary
        elif "symbol" in payload:
            symbol = str(payload["symbol"])
            bid = float(payload.get("bidPrice", payload.get("bid", 0.0)))
            ask = float(payload.get("askPrice", payload.get("ask", 0.0)))
            last = float(payload.get("lastPrice", payload.get("last", 0.0)))
            bid_size = int(payload.get("bidSize", payload.get("bid_size", 0)))
            ask_size = int(payload.get("askSize", payload.get("ask_size", 0)))
            volume = int(payload.get("totalVolume", payload.get("volume", 0)))
            quote_epoch_ms = int(payload.get("quoteTime", payload.get("timestamp", 0)))

        return symbol, bid, ask, last, bid_size, ask_size, volume, quote_epoch_ms

    # =========================================================================
    # 2. WARM PLANE (Account Sync & Robust Mid-Session Hot Boot)
    # =========================================================================

    def on_account_sync(self, account_payload: Dict[str, Any], is_hot_boot: bool = False) -> DualSleeveState:
        """
        Ingests Schwab /trader/v1/accounts response.
        Reconciles positions into 1x Robo-Core and 3x Tactical Satellite sleeves.

        Hot Boot Reconciliation Logic:
        If booting mid-session (or if baseline has not been initialized), dynamically
        samples live broker liquidation value and mark-to-market prices as the reference
        state (S0 and N0) immediately, bypassing the requirement for 09:30 pre-market baseline.
        """
        now = time.time()
        with self._lock:
            sec = account_payload.get("securitiesAccount", account_payload)
            cur = sec.get("currentBalances", {})
            agg = account_payload.get("aggregatedBalance", {})

            # 1. Parse liquidation value and cash
            nlv = float(cur.get("liquidationValue") or agg.get("currentLiquidationValue") or agg.get("liquidationValue") or 0.0)
            cash = float(cur.get("cashAvailableForTrading") or cur.get("availableFundsNonMarginableTrade") or cur.get("cashBalance") or 0.0)

            self._broker_nlv = nlv
            self._cash_buffer = cash

            # 2. Parse and categorize positions
            raw_positions = sec.get("positions", []) or []
            active_symbols: Set[str] = set()

            for raw in raw_positions:
                inst = raw.get("instrument", {})
                sym = str(inst.get("symbol", "")).upper()
                if not sym:
                    continue

                active_symbols.add(sym)
                long_qty = float(raw.get("longQuantity", 0.0))
                short_qty = float(raw.get("shortQuantity", 0.0))
                qty = long_qty - short_qty

                avg_price = float(raw.get("averagePrice", 0.0))
                mkt_val = float(raw.get("marketValue", 0.0))

                # Determine initial current price
                current_price = avg_price
                if sym in self._latest_quotes:
                    q = self._latest_quotes[sym]
                    if q.microprice > 0.0:
                        current_price = q.microprice
                elif qty != 0.0 and mkt_val > 0.0:
                    current_price = mkt_val / abs(qty)

                # Assign sleeve classification
                if sym in self.satellite_symbols:
                    sleeve_type = "SATELLITE"
                elif sym in DEFAULT_SWEEP_SYMBOLS:
                    sleeve_type = "SWEEP"
                else:
                    # Tickers outside satellite are mapped to 1x Core (e.g. SPY, QQQ, broad market)
                    sleeve_type = "CORE"

                unrealized = (current_price - avg_price) * qty if avg_price > 0 else 0.0

                self._positions[sym] = SleevePosition(
                    symbol=sym,
                    quantity=qty,
                    average_cost=avg_price,
                    current_price=current_price,
                    market_value=qty * current_price if current_price > 0 else mkt_val,
                    unrealized_pnl=unrealized,
                    last_update_ts=now,
                    sleeve=sleeve_type,
                )

                # Set core anchor price if missing
                if sleeve_type == "CORE" and sym not in self._core_anchor_prices and current_price > 0.0:
                    self._core_anchor_prices[sym] = current_price

            # Remove closed positions
            closed_symbols = set(self._positions.keys()) - active_symbols
            for cs in closed_symbols:
                del self._positions[cs]

            # 3. Mid-Session Hot Boot Initialization
            if is_hot_boot or not self._is_hot_boot_complete:
                self._is_hot_boot_complete = True
                self._boot_timestamp = now

                # Calculate initial core baseline market value for return tracking
                core_val = sum(p.market_value for p in self._positions.values() if p.sleeve == "CORE")
                self._initial_core_market_value = core_val if core_val > 0.0 else 1.0

                logger.info(
                    "Hot boot reconciliation complete at %s EDT: Broker NLV=$%.2f, Cash=$%.2f, Positions=%d, CoreVal=$%.2f",
                    datetime.now(timezone.utc).isoformat(),
                    nlv,
                    cash,
                    len(self._positions),
                    core_val,
                )

            self._last_warm_sync_ts = now
            return self._compute_dual_sleeve_state_locked(now)

    # =========================================================================
    # 3. COLD PLANE (FRED OAS, Macro Surprise, and Decayed Gemini NLP)
    # =========================================================================

    def on_fred_macro(self, payload: Dict[str, Any]) -> ColdMacroState:
        """
        Ingests FRED macroeconomic payload (BAMLH0A0HYM2 High Yield OAS).
        Updates baseline credit spread and economic surprise metrics.
        """
        now = time.time()
        with self._lock:
            # Parse observations
            obs = payload.get("observations", [])
            spread_bps = 320.0
            z_score = 0.0

            if obs and isinstance(obs, list):
                latest = obs[0]
                if "spread_basis_points" in latest:
                    spread_bps = float(latest["spread_basis_points"])
                elif "value" in latest:
                    spread_bps = float(latest["value"]) * 100.0
                z_score = float(latest.get("z_score_20d", 0.0))

            # Direct overrides if present
            if "economic_surprise_z_score" in payload:
                z_score = float(payload["economic_surprise_z_score"])
            if "intraday_credit_proxy_bps" in payload and not self._credit_proxy_is_live:
                self._macro_state.intraday_credit_proxy_bps = float(payload["intraday_credit_proxy_bps"])

            self._macro_state.fred_oas_bps = spread_bps
            self._macro_state.economic_surprise_z_score = z_score
            self._macro_state.last_macro_update_ts = now
            if self._credit_proxy_is_live:
                self._update_credit_proxy_locked()

            self._recalculate_hybrid_credit_spread_locked()
            return self._macro_state

    def on_gemini_nlp(self, payload: Dict[str, Any]) -> ColdMacroState:
        """
        Ingests Gemini NLP Sentiment payload.
        Records raw polarity S_nlp(t0) and evaluates exponential time decay.
        """
        now = time.time()
        with self._lock:
            scoring = payload.get("dovish_hawkish_scoring", {})
            net_polarity = float(scoring.get("net_dovish_polarity", 0.0))
            if net_polarity == 0.0 and ("dovish_score" in scoring and "hawkish_score" in scoring):
                net_polarity = float(scoring["dovish_score"]) - float(scoring["hawkish_score"])

            decision = payload.get("macro_decision_payload", {})
            regime = str(decision.get("macro_regime", "REGIME_A"))
            bias = str(decision.get("market_bias", "BULLISH"))

            self._macro_state.sentiment_polarity_raw = net_polarity
            self._macro_state.sentiment_timestamp = now
            self._macro_state.sentiment_decayed = net_polarity
            self._macro_state.macro_regime = regime
            self._macro_state.market_bias = bias
            self._macro_state.last_macro_update_ts = now

            return self._macro_state

    def get_macro_feature_vector(self) -> Tuple[float, float, float]:
        """
        Returns feature vector M(t) = [Z_s(t), S_nlp(t), CS_hybrid(t)] for the HMM Meta-Governor.
        Applies continuous exponential time decay e^(-lambda * dt) to sentiment polarity.
        """
        now = time.time()
        with self._lock:
            # 1. Economic surprise Z-score
            z_s = self._macro_state.economic_surprise_z_score

            # 2. Continuous exponential decay of sentiment polarity
            if self._macro_state.sentiment_timestamp > 0.0:
                dt_sec = max(0.0, now - self._macro_state.sentiment_timestamp)
                decay_factor = math.exp(-self.sentiment_decay_lambda * dt_sec)
                s_nlp = self._macro_state.sentiment_polarity_raw * decay_factor
                self._macro_state.sentiment_decayed = s_nlp
            else:
                s_nlp = 0.0

            # 3. Hybrid Credit Spread (OAS anchor + live proxy)
            self._recalculate_hybrid_credit_spread_locked()
            cs_hybrid = self._macro_state.hybrid_credit_spread_bps

            return (round(z_s, 4), round(s_nlp, 4), round(cs_hybrid, 2))

    def _recalculate_hybrid_credit_spread_locked(self) -> None:
        """Fuses daily FRED OAS anchor with intraday proxy (HYG/IEF or reported bps)."""
        oas = self._macro_state.fred_oas_bps
        intraday = self._macro_state.intraday_credit_proxy_bps
        # 70% daily FRED anchor + 30% live intraday credit proxy
        hybrid = 0.70 * oas + 0.30 * intraday
        self._macro_state.hybrid_credit_spread_bps = hybrid

    # =========================================================================
    # 4. UNIFIED DUAL-SLEEVE VALUATION & RETURN TRACKING
    # =========================================================================

    def get_live_portfolio_state(self) -> DualSleeveState:
        """Computes thread-safe, continuous mark-to-market across both sleeves."""
        with self._lock:
            return self._compute_dual_sleeve_state_locked(time.time())

    def _compute_dual_sleeve_state_locked(self, now: float) -> DualSleeveState:
        """Internal computation of Dual-Sleeve mark-to-market and 1x Core return."""
        core_mv = 0.0
        sat_mv = 0.0
        sweep_mv = 0.0

        for sym, pos in self._positions.items():
            if pos.sleeve == "CORE":
                core_mv += pos.market_value
            elif pos.sleeve == "SATELLITE":
                sat_mv += pos.market_value
            elif pos.sleeve == "SWEEP":
                sweep_mv += pos.market_value

        total_cash = self._cash_buffer + sweep_mv
        live_nlv = core_mv + sat_mv + total_cash
        if live_nlv <= 0.0:
            live_nlv = self._broker_nlv if self._broker_nlv > 0.0 else 1.0

        w_core = core_mv / live_nlv
        w_sat = sat_mv / live_nlv

        # Compute Core return r_core(t) relative to boot/anchor baseline
        r_core_cum = 0.0
        if self._initial_core_market_value and self._initial_core_market_value > 0.0:
            r_core_cum = (core_mv - self._initial_core_market_value) / self._initial_core_market_value

        # Instantaneous weighted return across Core components
        r_core_inst = 0.0
        core_positions = [p for p in self._positions.values() if p.sleeve == "CORE" and p.market_value > 0.0]
        if core_positions and core_mv > 0.0:
            for cp in core_positions:
                anchor = self._core_anchor_prices.get(cp.symbol, cp.average_cost)
                if anchor > 0.0:
                    asset_ret = (cp.current_price - anchor) / anchor
                    asset_weight = cp.market_value / core_mv
                    r_core_inst += asset_weight * asset_ret

        return DualSleeveState(
            core_market_value=round(core_mv, 2),
            satellite_market_value=round(sat_mv, 2),
            cash_buffer=round(total_cash, 2),
            net_liquidation_value=round(live_nlv, 2),
            core_weight=round(w_core, 4),
            satellite_weight=round(w_sat, 4),
            core_return_instantaneous=round(r_core_inst, 6),
            core_return_cumulative=round(r_core_cum, 6),
            is_hot_boot_complete=self._is_hot_boot_complete,
            boot_timestamp=self._boot_timestamp,
            last_mark_timestamp=now,
            positions=dict(self._positions),
        )

    def is_synchronized(self) -> bool:
        """Returns True if the synchronizer has completed initial hot-boot reconciliation."""
        with self._lock:
            return self._is_hot_boot_complete
