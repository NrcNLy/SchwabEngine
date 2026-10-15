"""
data/stream_resampler.py
========================
High-throughput, zero-allocation real-time market data resampler and streaming aggregator.

Features:
- Lock-free circular ring buffers for target tickers (SOXL, TQQQ, TNA, SOXX, QQQ).
- Zero-allocation per tick in steady-state via pre-allocated NumPy contiguous arrays.
- Dual-horizon aggregation: 15-second sub-bars and 1-minute primary bars.
- Running session Volume-Weighted Average Price (VWAP) and rolling high/low watermarks.
- Asynchronous boundary closure dispatch via BarCompletedEvent.
- Batched SQLite persistence to `state/telemetry.db` table `intraday_bars` (batch size 50).
- Bi-directional connector for MicrostructureSentinel virtual stops and RealTimeRegimeGovernor.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union
from zoneinfo import ZoneInfo

import numpy as np

from core.paths import TELEMETRY_DB_PATH

logger = logging.getLogger(__name__)

_EDT_TZ = ZoneInfo("America/New_York")
DEFAULT_TARGET_TICKERS: Tuple[str, ...] = ("SOXL", "TQQQ", "TNA", "SOXX", "QQQ")


# ===========================================================================
# 1. BarCompletedEvent
# ===========================================================================

@dataclass(frozen=True, slots=True)
class BarCompletedEvent:
    """
    Immutable representation of an aggregated OHLCV bar emitted upon interval boundary closure.
    """
    symbol: str
    timeframe: str               # "15s" or "1m"
    timestamp: datetime          # Interval start timestamp (datetime)
    timestamp_ms: int            # Interval start timestamp in epoch milliseconds
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float
    tick_count: int
    high_watermark: float = 0.0  # Session high watermark up to this bar
    low_watermark: float = 0.0   # Session low watermark up to this bar

    def to_dict(self) -> Dict[str, Any]:
        """Convert event to serializable dictionary."""
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "timestamp": self.timestamp.isoformat() if hasattr(self.timestamp, "isoformat") else str(self.timestamp),
            "timestamp_ms": self.timestamp_ms,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "vwap": self.vwap,
            "tick_count": self.tick_count,
            "high_watermark": self.high_watermark,
            "low_watermark": self.low_watermark,
        }

    def to_db_tuple(self) -> Tuple[Any, ...]:
        """Format row tuple for SQLite intraday_bars insertion."""
        ts_str = self.timestamp.isoformat() if hasattr(self.timestamp, "isoformat") else str(self.timestamp)
        return (
            self.symbol,
            self.timeframe,
            ts_str,
            self.timestamp_ms,
            float(self.open),
            float(self.high),
            float(self.low),
            float(self.close),
            float(self.volume),
            float(self.vwap),
            int(self.tick_count),
        )


# ===========================================================================
# 2. LockFreeRingBuffer
# ===========================================================================

class LockFreeRingBuffer:
    """
    Lock-free, pre-allocated circular ring buffer for raw ticks: (timestamp_ms, price, volume).
    Uses a power-of-two capacity for zero-allocation bitwise modulo wrapping:
        idx = head & (capacity - 1)
    """

    def __init__(self, capacity: int = 65536) -> None:
        self.capacity = 1 << (capacity - 1).bit_length()
        self.mask = self.capacity - 1
        self._timestamps = np.zeros(self.capacity, dtype=np.int64)
        self._prices = np.zeros(self.capacity, dtype=np.float64)
        self._volumes = np.zeros(self.capacity, dtype=np.float64)
        self._head = 0
        self._count = 0

    def append(self, timestamp_ms: int, price: float, volume: float) -> None:
        """Append tick in-place with zero object allocation."""
        idx = self._head & self.mask
        self._timestamps[idx] = timestamp_ms
        self._prices[idx] = price
        self._volumes[idx] = volume
        self._head += 1
        if self._count < self.capacity:
            self._count += 1

    def get_latest(self) -> Optional[Tuple[int, float, float]]:
        """Return the most recently appended tick (timestamp_ms, price, volume)."""
        if self._count == 0:
            return None
        idx = (self._head - 1) & self.mask
        return int(self._timestamps[idx]), float(self._prices[idx]), float(self._volumes[idx])

    def get_recent(self, n: int) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Return the n most recent ticks as contiguous numpy arrays."""
        k = min(n, self._count)
        if k == 0:
            return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float64), np.empty(0, dtype=np.float64)
        indices = np.arange(self._head - k, self._head, dtype=np.int64) & self.mask
        return self._timestamps[indices], self._prices[indices], self._volumes[indices]

    def clear(self) -> None:
        """Reset the ring buffer pointers."""
        self._head = 0
        self._count = 0

    def __len__(self) -> int:
        return self._count


# ===========================================================================
# 3. Bar Aggregation State
# ===========================================================================

class _BarState:
    """Internal lightweight aggregator for a single bar interval."""
    __slots__ = (
        "interval_id", "open", "high", "low", "close",
        "volume", "pv", "tick_count", "is_active"
    )

    def __init__(self) -> None:
        self.interval_id: int = -1
        self.open: float = 0.0
        self.high: float = 0.0
        self.low: float = 0.0
        self.close: float = 0.0
        self.volume: float = 0.0
        self.pv: float = 0.0
        self.tick_count: int = 0
        self.is_active: bool = False

    def reset(self) -> None:
        self.interval_id = -1
        self.open = 0.0
        self.high = 0.0
        self.low = 0.0
        self.close = 0.0
        self.volume = 0.0
        self.pv = 0.0
        self.tick_count = 0
        self.is_active = False


class _SymbolState:
    """Aggregator state for a single symbol across sub-bars and primary bars."""
    __slots__ = (
        "symbol", "ring_buffer", "sub_bar", "primary_bar",
        "cum_pv", "cum_vol", "high_watermark", "low_watermark",
        "last_price", "last_timestamp_ms"
    )

    def __init__(self, symbol: str, buffer_capacity: int = 65536) -> None:
        self.symbol: str = symbol.upper()
        self.ring_buffer: LockFreeRingBuffer = LockFreeRingBuffer(buffer_capacity)
        self.sub_bar: _BarState = _BarState()
        self.primary_bar: _BarState = _BarState()
        self.cum_pv: float = 0.0
        self.cum_vol: float = 0.0
        self.high_watermark: float = -float("inf")
        self.low_watermark: float = float("inf")
        self.last_price: float = 0.0
        self.last_timestamp_ms: int = 0

    @property
    def vwap(self) -> float:
        if self.cum_vol > 0.0:
            return self.cum_pv / self.cum_vol
        return self.last_price

    def reset(self) -> None:
        self.ring_buffer.clear()
        self.sub_bar.reset()
        self.primary_bar.reset()
        self.cum_pv = 0.0
        self.cum_vol = 0.0
        self.high_watermark = -float("inf")
        self.low_watermark = float("inf")
        self.last_price = 0.0
        self.last_timestamp_ms = 0


# ===========================================================================
# 4. BarResampler
# ===========================================================================

class BarResampler:
    """
    Zero-allocation real-time bar resampler for streaming ticks.
    Aggregates raw streaming ticks into:
      - 15-second sub-bars (timeframe="15s")
      - 1-minute primary bars (timeframe="1m")
    Maintains running VWAP and rolling high/low watermarks.
    Emits BarCompletedEvent asynchronously upon boundary closure.
    """

    SUB_BAR_MS: int = 15_000      # 15 seconds
    PRIMARY_BAR_MS: int = 60_000  # 1 minute

    def __init__(
        self,
        symbols: Optional[Sequence[str]] = None,
        buffer_capacity: int = 65536,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        self.buffer_capacity = int(buffer_capacity)
        self.loop = loop
        self._symbols_lock = threading.RLock()
        self._listeners: List[Callable[[BarCompletedEvent], Any]] = []
        self._completed_bars: List[BarCompletedEvent] = []
        self._queue: Optional[asyncio.Queue[BarCompletedEvent]] = None

        syms = list(symbols) if symbols is not None else list(DEFAULT_TARGET_TICKERS)
        self._states: Dict[str, _SymbolState] = {
            s.upper(): _SymbolState(s, self.buffer_capacity) for s in syms
        }

    def _get_or_create_symbol_state(self, symbol: str) -> _SymbolState:
        sym = symbol.upper()
        state = self._states.get(sym)
        if state is None:
            with self._symbols_lock:
                state = self._states.get(sym)
                if state is None:
                    state = _SymbolState(sym, self.buffer_capacity)
                    self._states[sym] = state
        return state

    def register_listener(self, callback: Callable[[BarCompletedEvent], Any]) -> None:
        """Register a callback to receive BarCompletedEvent instances upon closure."""
        if callback not in self._listeners:
            self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[BarCompletedEvent], Any]) -> None:
        """Unregister a previously registered callback."""
        if callback in self._listeners:
            self._listeners.remove(callback)

    @property
    def queue(self) -> asyncio.Queue[BarCompletedEvent]:
        """Lazy-initialize an asyncio.Queue for async consumers."""
        if self._queue is None:
            self._queue = asyncio.Queue()
        return self._queue

    def get_vwap(self, symbol: str) -> float:
        """Get running session volume-weighted average price for a symbol."""
        sym = symbol.upper()
        state = self._states.get(sym)
        if state is not None:
            return state.vwap
        return 0.0

    def get_high_watermark(self, symbol: str) -> float:
        """Get session high watermark price for a symbol."""
        sym = symbol.upper()
        state = self._states.get(sym)
        if state is not None and state.high_watermark != -float("inf"):
            return state.high_watermark
        return 0.0

    def get_low_watermark(self, symbol: str) -> float:
        """Get session low watermark price for a symbol."""
        sym = symbol.upper()
        state = self._states.get(sym)
        if state is not None and state.low_watermark != float("inf"):
            return state.low_watermark
        return 0.0

    def get_ring_buffer(self, symbol: str) -> LockFreeRingBuffer:
        """Access the lock-free ring buffer for a target ticker."""
        return self._get_or_create_symbol_state(symbol).ring_buffer

    # ------------------------------------------------------------------
    # Ingestion & Aggregation Hot-Path
    # ------------------------------------------------------------------

    def on_tick(
        self,
        symbol: str,
        price: float,
        volume: float = 0.0,
        timestamp: Any = None,
    ) -> List[BarCompletedEvent]:
        """
        Hot-path tick ingestion.
        Accepts: (symbol, price, volume, timestamp) or alternative order (symbol, timestamp_ms, price, volume).

        Aggregates into:
          - 15-second sub-bars
          - 1-minute primary bars
        Updates running VWAP and rolling high/low watermarks.
        Emits BarCompletedEvent asynchronously on boundary closure.
        Returns list of newly completed bars on this tick.
        """
        # Smart argument detection in case (symbol, timestamp_ms, price, volume) was passed
        if isinstance(price, (int, float)) and price > 1e11:
            # Caller passed (symbol, timestamp_ms, price, volume)
            ts_input = price
            px = float(volume)
            vol = float(timestamp if timestamp is not None else 0.0)
        else:
            ts_input = timestamp
            px = float(price)
            vol = float(volume) if volume is not None else 0.0

        # Fast timestamp normalization to integer epoch milliseconds
        if ts_input is None:
            ts_ms = int(time.time() * 1000)
        elif isinstance(ts_input, (int, float, np.integer, np.floating)):
            val = float(ts_input)
            ts_ms = int(val * 1000) if val < 1e11 else int(val)
        elif isinstance(ts_input, datetime):
            ts_ms = int(ts_input.timestamp() * 1000)
        elif isinstance(ts_input, str):
            try:
                dt = datetime.fromisoformat(ts_input)
                ts_ms = int(dt.timestamp() * 1000)
            except Exception:
                ts_ms = int(time.time() * 1000)
        else:
            ts_ms = int(time.time() * 1000)

        state = self._get_or_create_symbol_state(symbol)

        # 1. Update lock-free ring buffer (zero-allocation)
        state.ring_buffer.append(ts_ms, px, vol)

        # 2. Update watermarks & running VWAP
        state.last_price = px
        state.last_timestamp_ms = ts_ms
        if px > state.high_watermark:
            state.high_watermark = px
        if px < state.low_watermark:
            state.low_watermark = px

        state.cum_pv += px * vol
        state.cum_vol += vol

        completed: List[BarCompletedEvent] = []

        # 3. Aggregate 15-second sub-bar
        sub_id = ts_ms // self.SUB_BAR_MS
        b_sub = state.sub_bar
        if sub_id != b_sub.interval_id:
            if b_sub.is_active:
                event = self._close_bar(state, b_sub, "15s", self.SUB_BAR_MS)
                completed.append(event)
            # Initialize new sub-bar
            b_sub.interval_id = sub_id
            b_sub.open = px
            b_sub.high = px
            b_sub.low = px
            b_sub.close = px
            b_sub.volume = vol
            b_sub.pv = px * vol
            b_sub.tick_count = 1
            b_sub.is_active = True
        else:
            if px > b_sub.high:
                b_sub.high = px
            elif px < b_sub.low:
                b_sub.low = px
            b_sub.close = px
            b_sub.volume += vol
            b_sub.pv += px * vol
            b_sub.tick_count += 1

        # 4. Aggregate 1-minute primary bar
        pri_id = ts_ms // self.PRIMARY_BAR_MS
        b_pri = state.primary_bar
        if pri_id != b_pri.interval_id:
            if b_pri.is_active:
                event = self._close_bar(state, b_pri, "1m", self.PRIMARY_BAR_MS)
                completed.append(event)
            # Initialize new primary bar
            b_pri.interval_id = pri_id
            b_pri.open = px
            b_pri.high = px
            b_pri.low = px
            b_pri.close = px
            b_pri.volume = vol
            b_pri.pv = px * vol
            b_pri.tick_count = 1
            b_pri.is_active = True
        else:
            if px > b_pri.high:
                b_pri.high = px
            elif px < b_pri.low:
                b_pri.low = px
            b_pri.close = px
            b_pri.volume += vol
            b_pri.pv += px * vol
            b_pri.tick_count += 1

        # 5. Emit completed bars asynchronously
        for event in completed:
            self._completed_bars.append(event)
            self._emit_event_async(event)

        return completed

    def ingest_tick(self, symbol: str, timestamp_ms: int, price: float, volume: float) -> List[BarCompletedEvent]:
        """Convenience alias for raw tick tuple ingestion."""
        return self.on_tick(symbol=symbol, price=price, volume=volume, timestamp=timestamp_ms)

    def _close_bar(
        self,
        state: _SymbolState,
        b: _BarState,
        timeframe: str,
        interval_ms: int,
    ) -> BarCompletedEvent:
        bar_start_ms = b.interval_id * interval_ms
        bar_dt = datetime.fromtimestamp(bar_start_ms / 1000.0, tz=timezone.utc)
        bar_vwap = b.pv / b.volume if b.volume > 0.0 else b.close

        event = BarCompletedEvent(
            symbol=state.symbol,
            timeframe=timeframe,
            timestamp=bar_dt,
            timestamp_ms=bar_start_ms,
            open=b.open,
            high=b.high,
            low=b.low,
            close=b.close,
            volume=b.volume,
            vwap=bar_vwap,
            tick_count=b.tick_count,
            high_watermark=state.high_watermark,
            low_watermark=state.low_watermark,
        )
        return event

    def _emit_event_async(self, event: BarCompletedEvent) -> None:
        """Dispatch event asynchronously to registered listeners and queue."""
        if self._queue is not None:
            try:
                self._queue.put_nowait(event)
            except Exception:
                pass

        if not self._listeners:
            return

        # Determine running loop
        loop = self.loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

        for listener in self._listeners:
            try:
                if inspect.iscoroutinefunction(listener):
                    if loop is not None and loop.is_running():
                        loop.create_task(listener(event))
                    else:
                        asyncio.run(listener(event))
                else:
                    listener(event)
            except Exception as exc:
                logger.exception("Error in BarCompletedEvent listener: %s", exc)

    def flush(self, symbol: Optional[str] = None) -> List[BarCompletedEvent]:
        """
        Force-close any active in-flight sub-bars and primary bars.
        Useful at session boundaries, backtests, or benchmark conclusions.
        """
        targets = [self._states[symbol.upper()]] if symbol and symbol.upper() in self._states else list(self._states.values())
        flushed: List[BarCompletedEvent] = []

        for st in targets:
            if st.sub_bar.is_active:
                event = self._close_bar(st, st.sub_bar, "15s", self.SUB_BAR_MS)
                st.sub_bar.reset()
                flushed.append(event)
                self._completed_bars.append(event)
                self._emit_event_async(event)

            if st.primary_bar.is_active:
                event = self._close_bar(st, st.primary_bar, "1m", self.PRIMARY_BAR_MS)
                st.primary_bar.reset()
                flushed.append(event)
                self._completed_bars.append(event)
                self._emit_event_async(event)

        return flushed

    def reset(self, symbol: Optional[str] = None) -> None:
        """Reset aggregators and ring buffers."""
        targets = [self._states[symbol.upper()]] if symbol and symbol.upper() in self._states else list(self._states.values())
        for st in targets:
            st.reset()
        if symbol is None:
            self._completed_bars.clear()

    def get_completed_bars(self, symbol: Optional[str] = None, timeframe: Optional[str] = None) -> List[BarCompletedEvent]:
        """Return history of completed bars emitted during this session."""
        bars = self._completed_bars
        if symbol:
            s_up = symbol.upper()
            bars = [b for b in bars if b.symbol == s_up]
        if timeframe:
            bars = [b for b in bars if b.timeframe == timeframe]
        return list(bars)


# ===========================================================================
# 5. Batched SQLite Intraday Bar Persistence
# ===========================================================================

class IntradayBarWriter:
    """
    Batched SQLite writer for completed 1-minute bars to table `intraday_bars` in `state/telemetry.db`.
    Buffers bars in-memory and executes batched writes of 50 to minimize disk I/O.
    """

    BATCH_SIZE: int = 50

    def __init__(
        self,
        db_path: Optional[Union[str, Path]] = None,
        batch_size: int = 50,
    ) -> None:
        self.db_path = str(db_path if db_path is not None else TELEMETRY_DB_PATH)
        self.batch_size = int(batch_size)
        self._batch: List[BarCompletedEvent] = []
        self._lock = threading.RLock()
        self._total_written: int = 0
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        if self.db_path != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _init_db(self) -> None:
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS intraday_bars (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        symbol TEXT NOT NULL,
                        timeframe TEXT NOT NULL,
                        timestamp TEXT NOT NULL,
                        timestamp_ms INTEGER NOT NULL,
                        open REAL NOT NULL,
                        high REAL NOT NULL,
                        low REAL NOT NULL,
                        close REAL NOT NULL,
                        volume REAL NOT NULL,
                        vwap REAL NOT NULL,
                        tick_count INTEGER NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_intraday_bars_sym_ts
                    ON intraday_bars (symbol, timestamp);
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_intraday_bars_ts
                    ON intraday_bars (timestamp);
                    """
                )
                conn.commit()
            finally:
                conn.close()

    def record_bar(self, bar: BarCompletedEvent) -> bool:
        """
        Record a completed bar. If it's a 1-minute primary bar, queue it for batched disk persistence.
        Returns True if a batch write occurred.
        """
        if bar.timeframe != "1m":
            return False

        with self._lock:
            self._batch.append(bar)
            if len(self._batch) >= self.batch_size:
                self.flush()
                return True
        return False

    def on_bar_completed(self, bar: BarCompletedEvent) -> None:
        """Callback handler matching BarResampler listener protocol."""
        self.record_bar(bar)

    def flush(self) -> int:
        """Flush any pending buffered bars to SQLite table intraday_bars in a single transaction."""
        with self._lock:
            if not self._batch:
                return 0
            to_write = list(self._batch)
            self._batch.clear()

        rows = [b.to_db_tuple() for b in to_write]
        conn = self._get_connection()
        try:
            with conn:
                conn.executemany(
                    """
                    INSERT INTO intraday_bars (
                        symbol, timeframe, timestamp, timestamp_ms,
                        open, high, low, close, volume, vwap, tick_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    rows,
                )
            self._total_written += len(rows)
            return len(rows)
        finally:
            conn.close()

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._batch)

    @property
    def total_written(self) -> int:
        return self._total_written

    def count_bars(self, symbol: Optional[str] = None) -> int:
        """Query count of persisted bars from intraday_bars table."""
        conn = self._get_connection()
        try:
            if symbol:
                cur = conn.execute("SELECT COUNT(*) FROM intraday_bars WHERE symbol = ?", (symbol.upper(),))
            else:
                cur = conn.execute("SELECT COUNT(*) FROM intraday_bars")
            row = cur.fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    def close(self) -> None:
        """Flush pending bars and close resources."""
        self.flush()


# ===========================================================================
# 6. BarPipelineConnector: MicrostructureSentinel & RealTimeRegimeGovernor
# ===========================================================================

class BarPipelineConnector:
    """
    Connects BarCompletedEvent emissions from BarResampler to:
      1. MicrostructureSentinel instances (keyed by symbol)
      2. RealTimeRegimeGovernor
      3. IntradayBarWriter (telemetry.db SQLite table intraday_bars)
    """

    def __init__(
        self,
        sentinels: Optional[Dict[str, Any]] = None,
        regime_governor: Optional[Any] = None,
        bar_writer: Optional[IntradayBarWriter] = None,
    ) -> None:
        self.sentinels: Dict[str, Any] = sentinels if sentinels is not None else {}
        self.regime_governor = regime_governor
        self.bar_writer = bar_writer
        self.last_verdicts: Dict[str, Any] = {}
        self._lock = threading.RLock()

    def register_sentinel(self, symbol: str, sentinel: Any) -> None:
        """Register a MicrostructureSentinel instance for a symbol."""
        with self._lock:
            self.sentinels[symbol.upper()] = sentinel

    def set_governor(self, governor: Any) -> None:
        """Set the RealTimeRegimeGovernor instance."""
        self.regime_governor = governor

    def set_bar_writer(self, writer: IntradayBarWriter) -> None:
        """Set the IntradayBarWriter instance."""
        self.bar_writer = writer

    def on_bar_completed(self, bar: BarCompletedEvent) -> None:
        """
        Dispatch completed bar to sentinel, regime governor, and bar writer.
        """
        sym = bar.symbol.upper()

        # 1. Dispatch to MicrostructureSentinel
        with self._lock:
            sentinel = self.sentinels.get(sym)

        if sentinel is not None:
            if hasattr(sentinel, "on_bar_completed"):
                verdict = sentinel.on_bar_completed(bar)
                if verdict is not None:
                    self.last_verdicts[sym] = verdict
            elif bar.timeframe == "15s":
                verdict = sentinel.evaluate_stop(
                    current_time_edt=bar.timestamp,
                    current_price=bar.close,
                    current_volume=bar.volume,
                    bid_price=bar.close,
                )
                self.last_verdicts[sym] = verdict

        # 2. Dispatch to RealTimeRegimeGovernor on 1-minute bars
        if self.regime_governor is not None and bar.timeframe == "1m":
            if hasattr(self.regime_governor, "on_bar_completed"):
                self.regime_governor.on_bar_completed(bar)
            elif hasattr(self.regime_governor, "record_bar"):
                canon = getattr(self.regime_governor, "canonical_symbol", lambda s: s)(sym)
                factors = getattr(self.regime_governor, "FACTORS", ())
                if canon in factors:
                    self.regime_governor.record_bar(
                        symbol=canon,
                        close=bar.close,
                        timestamp=bar.timestamp,
                        volume=int(bar.volume),
                    )

        # 3. Dispatch to IntradayBarWriter for 1-minute bar batching
        if self.bar_writer is not None and bar.timeframe == "1m":
            self.bar_writer.record_bar(bar)
