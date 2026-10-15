"""
execution/microstructure_sentinel.py
====================================
Synthetic Microstructure Sentinel & Opening Cross Buffer (#54).

Eliminates premature stop-outs and raw exchange-side stop slippage during the
09:30:00–09:31:30 EDT market open (as occurred on SOXL Order 1008303563072).

Two-Tier Virtual Stop-Loss Architecture:
----------------------------------------
- Tier 1: Hard Catastrophic Floor (S <= S0 * (1 - hard_catastrophic_pct), default -6.0%):
  Breached at ANY millisecond regardless of time -> triggers immediate EMERGENCY_HARD_SWEEP
  (routes IOC market sell).
- Tier 2: Soft Synthetic Floor (S <= S0 * (1 - soft_floor_pct), default -3.7%):
  * Before 09:31:30 EDT (Opening Cross Buffer):
    Requires rolling VWAP < soft floor AND price to remain continuously depressed for >= 45s.
    Otherwise classified as OPENING_AUCTION_NOISE and exit is suppressed.
  * After 09:31:30 EDT (Regular Session):
    Triggers exit if rolling 30s VWAP < soft floor OR 2 consecutive 15-second closes < soft floor.
    Exit type: LIMIT_PEGGED (bid - $0.02) to minimize spread slippage.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any, Deque, Dict, Optional, Tuple, Union
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_EDT_TZ = ZoneInfo("America/New_York")
_MARKET_OPEN_SECONDS = 9 * 3600 + 30 * 60  # 34200.0 (09:30:00 EDT)


@dataclass
class SentinelVerdict:
    """Verdict returned by MicrostructureSentinel.evaluate_stop."""

    should_exit: bool
    reason: str
    exit_type: str
    current_vwap: float
    pegged_limit_price: Optional[float] = None

    def __iter__(self):
        yield self.should_exit
        yield self.reason
        yield self.exit_type
        yield self.current_vwap


class MicrostructureSentinel:
    """
    In-memory virtual microstructure sentinel providing opening cross noise buffering
    and two-tier virtual stop loss enforcement.
    """

    _active_sentinels: Dict[str, MicrostructureSentinel] = {}

    @classmethod
    def get_active_sentinels(cls) -> Dict[str, MicrostructureSentinel]:
        """Return a dictionary of currently registered active sentinel instances keyed by uppercase symbol."""
        return dict(cls._active_sentinels)

    @classmethod
    def register_sentinel(cls, sentinel: MicrostructureSentinel) -> None:
        """Register a sentinel instance."""
        cls._active_sentinels[sentinel.symbol.upper()] = sentinel

    @classmethod
    def unregister_sentinel(cls, symbol: str) -> None:
        """Unregister a sentinel instance by symbol."""
        cls._active_sentinels.pop(symbol.upper(), None)

    @classmethod
    def clear_active_sentinels(cls) -> None:
        """Clear all registered sentinel instances (e.g. for testing or session reset)."""
        cls._active_sentinels.clear()

    def __init__(
        self,
        symbol: str,
        s0_reference: float,
        soft_floor_pct: float = 0.037,
        hard_catastrophic_pct: float = 0.060,
        opening_buffer_seconds: int = 90,
        vwap_window_seconds: int = 30,
    ) -> None:
        """
        Args:
            symbol: Ticker symbol (e.g. "SOXL", "TQQQ", "TNA").
            s0_reference: Sampling price at 09:45 or position entry price.
            soft_floor_pct: Soft floor loss percentage (default 0.037 / 3.7%).
            hard_catastrophic_pct: Hard catastrophic floor loss percentage (default 0.060 / 6.0%).
            opening_buffer_seconds: Duration of opening cross buffer in seconds (default 90).
            vwap_window_seconds: Rolling VWAP window in seconds (default 30).
        """
        if float(s0_reference) <= 0:
            raise ValueError(f"s0_reference must be positive, got {s0_reference}")

        self.symbol: str = str(symbol).upper()
        self.s0_reference: float = float(s0_reference)
        self.soft_floor_pct: float = float(soft_floor_pct)
        self.hard_catastrophic_pct: float = float(hard_catastrophic_pct)
        self.opening_buffer_seconds: int = int(opening_buffer_seconds)
        self.vwap_window_seconds: int = int(vwap_window_seconds)

        # Dynamic envelope bounds tracking (#68)
        self.high_watermark: float = self.s0_reference
        self.target_pct: float = 0.04
        self._trail_stop_override: Optional[float] = None
        self._catastrophic_floor_override: Optional[float] = None
        self._target_override: Optional[float] = None

        # Rolling tick state: deque of (timestamp_seconds: float, price: float, volume: float)
        # Maintained for the last 60 seconds
        self._ticks: Deque[Tuple[float, float, float]] = deque()

        # Continuous price depression tracking
        self._depressed_since: Optional[float] = None

        # 15-second bar close tracking
        self._current_15s_interval: Optional[int] = None
        self._current_15s_close: Optional[float] = None
        self._15s_closes: Deque[float] = deque(maxlen=20)

        # Cached VWAP
        self._last_vwap: float = self.s0_reference

        # Auto-register in global active sentinels registry
        self.register_sentinel(self)

    @property
    def soft_floor(self) -> float:
        """Synthetic soft floor price level."""
        return self.s0_reference * (1.0 - self.soft_floor_pct)

    @property
    def hard_floor(self) -> float:
        """Catastrophic hardware floor price level."""
        return self.s0_reference * (1.0 - self.hard_catastrophic_pct)

    @property
    def trail_stop(self) -> float:
        """Dynamic ratchet trailing stop bound."""
        if self._trail_stop_override is not None:
            return round(self._trail_stop_override, 2)
        return round(max(self.soft_floor, self.high_watermark * (1.0 - self.soft_floor_pct)), 2)

    @trail_stop.setter
    def trail_stop(self, val: float) -> None:
        self._trail_stop_override = float(val)

    @property
    def catastrophic_floor(self) -> float:
        """Hard catastrophic floor price bound."""
        if self._catastrophic_floor_override is not None:
            return round(self._catastrophic_floor_override, 2)
        return round(self.hard_floor, 2)

    @catastrophic_floor.setter
    def catastrophic_floor(self, val: float) -> None:
        self._catastrophic_floor_override = float(val)

    @property
    def target(self) -> float:
        """Dynamic upper target price bound."""
        if self._target_override is not None:
            return round(self._target_override, 2)
        return round(self.s0_reference * (1.0 + self.target_pct), 2)

    @target.setter
    def target(self, val: float) -> None:
        self._target_override = float(val)

    def get_envelope_bounds(self) -> Dict[str, float]:
        """
        Pull active dynamic envelope bounds:
        (Upper Target, Ratchet Trail, Catastrophic Floor, High Watermark, S0).
        """
        return {
            "s0": round(self.s0_reference, 2),
            "high_watermark": round(self.high_watermark, 2),
            "trail_stop": round(self.trail_stop, 2),
            "catastrophic_floor": round(self.catastrophic_floor, 2),
            "target": round(self.target, 2),
        }

    @property
    def current_vwap(self) -> float:
        """Most recently computed rolling VWAP."""
        return self._last_vwap

    def get_pegged_limit_price(self, bid_price: float) -> float:
        """
        Calculate pegged limit exit price (bid - $0.02) to minimize spread slippage.
        """
        return round(float(bid_price) - 0.02, 2)

    def update_s0(self, new_s0: float) -> None:
        """Update reference price s0 (e.g. at 09:45 recalibration)."""
        if float(new_s0) <= 0:
            raise ValueError(f"s0 must be positive, got {new_s0}")
        self.s0_reference = float(new_s0)
        if self.high_watermark < self.s0_reference:
            self.high_watermark = self.s0_reference

    def ratchet(
        self,
        high_watermark: float,
        dynamic_trail_price: float,
        floor_price: Optional[float] = None,
    ) -> None:
        """
        Manually trigger or update a sentinel stop ratchet and publish SentinelRatchetEvent.
        """
        self.high_watermark = max(self.high_watermark, float(high_watermark))
        fl = float(floor_price) if floor_price is not None else self.soft_floor
        try:
            from core.event_ledger import publish_event
            from core.event_models import SentinelRatchetEvent
            publish_event(
                SentinelRatchetEvent(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    symbol=self.symbol,
                    high_watermark=self.high_watermark,
                    dynamic_trail_price=float(dynamic_trail_price),
                    floor_price=round(fl, 2),
                )
            )
        except Exception:
            pass


    def reset(self) -> None:
        """Clear rolling ticks, timers, and bar states."""
        self._ticks.clear()
        self._depressed_since = None
        self._current_15s_interval = None
        self._current_15s_close = None
        self._15s_closes.clear()
        self._last_vwap = self.s0_reference
        self.high_watermark = self.s0_reference
        self._trail_stop_override = None
        self._catastrophic_floor_override = None
        self._target_override = None

    def on_bar_completed(self, bar: Any) -> Optional[SentinelVerdict]:
        """
        Connects BarCompletedEvent from BarResampler to MicrostructureSentinel (#63).
        Updates rolling 15s bar closes, rolling VWAP, and triggers evaluate_stop on 15s boundaries.
        """
        bar_sym = getattr(bar, "symbol", "")
        if bar_sym and bar_sym.upper() != self.symbol:
            return None

        timeframe = getattr(bar, "timeframe", "")
        px = float(getattr(bar, "close", 0.0))
        vol = float(getattr(bar, "volume", 0.0))
        vwap = float(getattr(bar, "vwap", px))
        ts = getattr(bar, "timestamp", None)

        if timeframe == "15s":
            self._15s_closes.append(px)
            self._last_vwap = vwap
            return self.evaluate_stop(
                current_time_edt=ts if ts is not None else datetime.now(_EDT_TZ),
                current_price=px,
                current_volume=vol,
                bid_price=px,
            )
        elif timeframe == "1m":
            self._last_vwap = vwap
        return None

    def _normalize_time(self, current_time_edt: Any) -> Tuple[datetime, float, float]:
        """
        Normalize input timestamp into:
        (dt_edt: datetime, t_seconds: float, seconds_of_day: float)
        All in America/New_York (EDT) timezone.
        """
        if isinstance(current_time_edt, str):
            s = current_time_edt.strip()
            if "T" in s or "-" in s:
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=_EDT_TZ)
                else:
                    dt = dt.astimezone(_EDT_TZ)
            else:
                parts = s.split(":")
                h = int(parts[0])
                m = int(parts[1])
                sec_val = float(parts[2])
                sec_int = int(sec_val)
                usec = int(round((sec_val - sec_int) * 1_000_000))
                # Deterministic base trading date
                dt = datetime(2026, 10, 15, h, m, sec_int, usec, tzinfo=_EDT_TZ)
        elif isinstance(current_time_edt, datetime):
            if current_time_edt.tzinfo is None:
                dt = current_time_edt.replace(tzinfo=_EDT_TZ)
            else:
                dt = current_time_edt.astimezone(_EDT_TZ)
        elif isinstance(current_time_edt, time):
            dt = datetime.combine(date(2026, 10, 15), current_time_edt, tzinfo=_EDT_TZ)
        elif isinstance(current_time_edt, (int, float)):
            val = float(current_time_edt)
            if val > 1_000_000_000:
                dt = datetime.fromtimestamp(val, tz=_EDT_TZ)
            else:
                if val < 3600:
                    sec_of_day = _MARKET_OPEN_SECONDS + val
                else:
                    sec_of_day = val
                h = int(sec_of_day // 3600)
                m = int((sec_of_day % 3600) // 60)
                s_rem = sec_of_day % 60
                dt = datetime(2026, 10, 15, h, m, int(s_rem), int((s_rem % 1) * 1_000_000), tzinfo=_EDT_TZ)
        else:
            raise TypeError(f"Unsupported time type: {type(current_time_edt)} ({current_time_edt})")

        t_seconds = dt.timestamp()
        seconds_of_day = dt.hour * 3600.0 + dt.minute * 60.0 + dt.second + dt.microsecond / 1_000_000.0
        return dt, t_seconds, seconds_of_day

    def evaluate_stop(
        self,
        current_time_edt: Any,
        current_price: float,
        current_volume: float = 0.0,
        bid_price: Optional[float] = None,
    ) -> SentinelVerdict:
        """
        Evaluate virtual microstructure stop loss logic.

        Args:
            current_time_edt: Timestamp (datetime, str 'HH:MM:SS', time, or epoch).
            current_price: Last trade price S.
            current_volume: Trade volume for tick.
            bid_price: Optional current best bid (used for LIMIT_PEGGED price calculation).

        Returns:
            SentinelVerdict: (should_exit, reason, exit_type, current_vwap)
        """
        px = float(current_price)
        if px > self.high_watermark:
            self.high_watermark = px
            dynamic_trail = round(self.high_watermark * (1.0 - self.soft_floor_pct), 2)
            try:
                from core.event_ledger import publish_event
                from core.event_models import SentinelRatchetEvent
                publish_event(
                    SentinelRatchetEvent(
                        timestamp=str(current_time_edt),
                        symbol=self.symbol,
                        high_watermark=self.high_watermark,
                        dynamic_trail_price=dynamic_trail,
                        floor_price=round(self.soft_floor, 2),
                    )
                )
            except Exception:
                pass
        vol = float(current_volume) if current_volume is not None else 0.0

        bid = float(bid_price) if bid_price is not None else px

        _, t_seconds, seconds_of_day = self._normalize_time(current_time_edt)

        # ------------------------------------------------------------------
        # 1. Update rolling ticks deque (60-second window)
        # ------------------------------------------------------------------
        self._ticks.append((t_seconds, px, vol))
        while self._ticks and self._ticks[0][0] < t_seconds - 60.0:
            self._ticks.popleft()

        # ------------------------------------------------------------------
        # 2. Compute rolling 30-second VWAP: sum(p * v) / sum(v)
        # ------------------------------------------------------------------
        cutoff_vwap = t_seconds - self.vwap_window_seconds
        window_ticks = [tk for tk in self._ticks if tk[0] >= cutoff_vwap]

        sum_pv = sum(tk[1] * tk[2] for tk in window_ticks)
        sum_v = sum(tk[2] for tk in window_ticks)

        if sum_v > 0:
            current_vwap = sum_pv / sum_v
        elif window_ticks:
            current_vwap = sum(tk[1] for tk in window_ticks) / len(window_ticks)
        else:
            current_vwap = px

        self._last_vwap = current_vwap

        # ------------------------------------------------------------------
        # 3. Update 15-second bar closes
        # ------------------------------------------------------------------
        interval_id = int(t_seconds // 15.0)
        if self._current_15s_interval is None:
            self._current_15s_interval = interval_id
            self._current_15s_close = px
        elif interval_id == self._current_15s_interval:
            self._current_15s_close = px
        else:
            if self._current_15s_close is not None:
                self._15s_closes.append(self._current_15s_close)
            skipped = interval_id - self._current_15s_interval - 1
            if skipped > 0 and self._current_15s_close is not None:
                for _ in range(min(skipped, 5)):
                    self._15s_closes.append(self._current_15s_close)
            self._current_15s_interval = interval_id
            self._current_15s_close = px

        two_consecutive_15s_below = False
        if len(self._15s_closes) >= 2:
            if self._15s_closes[-1] < self.soft_floor and self._15s_closes[-2] < self.soft_floor:
                two_consecutive_15s_below = True
        if not two_consecutive_15s_below and len(self._15s_closes) >= 1 and self._current_15s_close is not None:
            if self._15s_closes[-1] < self.soft_floor and self._current_15s_close < self.soft_floor:
                two_consecutive_15s_below = True

        # ------------------------------------------------------------------
        # 4. Continuous price depression timer
        # ------------------------------------------------------------------
        if px <= self.soft_floor:
            if self._depressed_since is None:
                self._depressed_since = t_seconds
            depressed_duration = t_seconds - self._depressed_since
        else:
            self._depressed_since = None
            depressed_duration = 0.0

        # ------------------------------------------------------------------
        # Tier 1: Hard Catastrophic Floor (S <= S0 * (1 - hard_catastrophic_pct))
        # Breached at ANY millisecond regardless of time -> immediate EMERGENCY_HARD_SWEEP
        # ------------------------------------------------------------------
        if px <= self.hard_floor:
            return SentinelVerdict(
                should_exit=True,
                reason="EMERGENCY_HARD_SWEEP",
                exit_type="EMERGENCY_HARD_SWEEP",
                current_vwap=current_vwap,
                pegged_limit_price=None,
            )

        # ------------------------------------------------------------------
        # Tier 2: Soft Synthetic Floor (S <= S0 * (1 - soft_floor_pct))
        # ------------------------------------------------------------------
        buffer_end_seconds = _MARKET_OPEN_SECONDS + self.opening_buffer_seconds

        if seconds_of_day < buffer_end_seconds:
            # Inside Opening Cross Buffer (09:30:00 to 09:31:30 EDT)
            if px <= self.soft_floor:
                # Require rolling VWAP < soft floor AND continuous depression >= 45s
                if current_vwap < self.soft_floor and depressed_duration >= 45.0:
                    return SentinelVerdict(
                        should_exit=True,
                        reason="SUSTAINED_SOFT_BREACH",
                        exit_type="LIMIT_PEGGED",
                        current_vwap=current_vwap,
                        pegged_limit_price=self.get_pegged_limit_price(bid),
                    )
                else:
                    # Classify as OPENING_AUCTION_NOISE and suppress exit
                    return SentinelVerdict(
                        should_exit=False,
                        reason="OPENING_AUCTION_NOISE",
                        exit_type="NONE",
                        current_vwap=current_vwap,
                        pegged_limit_price=None,
                    )
            else:
                return SentinelVerdict(
                    should_exit=False,
                    reason="HOLD",
                    exit_type="NONE",
                    current_vwap=current_vwap,
                    pegged_limit_price=None,
                )
        else:
            # Post-Open Regular Session (after 09:31:30 EDT)
            # Trigger exit if rolling 30-second VWAP < soft floor OR 2 consecutive 15-second closes < soft floor
            if current_vwap < self.soft_floor or two_consecutive_15s_below:
                return SentinelVerdict(
                    should_exit=True,
                    reason="SOFT_FLOOR_BREACH",
                    exit_type="LIMIT_PEGGED",
                    current_vwap=current_vwap,
                    pegged_limit_price=self.get_pegged_limit_price(bid),
                )
            else:
                return SentinelVerdict(
                    should_exit=False,
                    reason="HOLD",
                    exit_type="NONE",
                    current_vwap=current_vwap,
                    pegged_limit_price=None,
                )
