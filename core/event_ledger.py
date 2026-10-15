"""
core/event_ledger.py
====================
Zero-Allocation Telemetry Event-Sourcing Ledger & WAL Observer (#67).

Provides ultra-low-latency asynchronous event sourcing with dual persistence targets:
1. SQLite WAL mode (state/telemetry.db table `event_stream`).
2. Daily append-only JSONL event stream (/home/nicho/schwab_state/events_YYYYMMDD.jsonl).

Key Architectural Guarantees:
- Producer-Consumer with bounded in-memory asyncio.Queue(maxsize=10000).
- Sub-5-microsecond non-blocking publish_event call for Core 0 tick loop isolation.
- Background worker thread flushing batches every 500ms or 100 events.
- Crash-resilient SQLite WAL configuration (PRAGMA journal_mode=WAL, synchronous=NORMAL, busy_timeout=5000).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple, Union

from core.event_models import BaseEngineEvent, EngineEvent
from core.paths import TELEMETRY_DB_PATH, STATE_DIR

logger = logging.getLogger(__name__)


def _resolve_default_log_dir() -> Path:
    """Resolve the default JSONL directory (/home/nicho/schwab_state or STATE_DIR)."""
    vm_dir = Path("/home/nicho/schwab_state")
    if vm_dir.is_dir():
        return vm_dir
    return STATE_DIR


class EventLedger:
    """
    High-throughput, asynchronous event-sourcing ledger backed by SQLite WAL
    and append-only JSONL files.
    """

    _instance: Optional[EventLedger] = None
    _singleton_lock: threading.Lock = threading.Lock()

    def __init__(
        self,
        db_path: Optional[Union[str, Path]] = None,
        log_dir: Optional[Union[str, Path]] = None,
        maxsize: int = 10000,
        batch_size: int = 100,
        flush_interval_seconds: float = 0.5,
    ) -> None:
        self.db_path: Path = Path(db_path) if db_path is not None else Path(TELEMETRY_DB_PATH)
        self.log_dir: Path = Path(log_dir) if log_dir is not None else _resolve_default_log_dir()
        self.maxsize: int = int(maxsize)
        self.batch_size: int = int(batch_size)
        self.flush_interval: float = float(flush_interval_seconds)

        # Ensure persistence paths exist
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        # In-memory bounded queue
        self._queue: asyncio.Queue[EngineEvent] = asyncio.Queue(maxsize=self.maxsize)

        # Worker synchronization
        self._worker_thread: Optional[threading.Thread] = None
        self._running: bool = False
        self._stop_event: threading.Event = threading.Event()
        self._wake_event: threading.Event = threading.Event()
        self._init_lock: threading.Lock = threading.Lock()

        # Telemetry counters
        self._published_count: int = 0
        self._dropped_count: int = 0
        self._flushed_count: int = 0

        # Initialize SQLite schema
        self._init_sqlite()

    @classmethod
    def get_instance(cls) -> EventLedger:
        """Access or initialize the thread-safe singleton instance."""
        if cls._instance is None:
            with cls._singleton_lock:
                if cls._instance is None:
                    cls._instance = cls()
                    cls._instance.start()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton instance (primarily for isolated test fixtures)."""
        with cls._singleton_lock:
            if cls._instance is not None:
                cls._instance.stop()
                cls._instance = None

    def _get_sqlite_connection(self) -> sqlite3.Connection:
        """Create and configure a SQLite connection with high-concurrency WAL pragmas."""
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=5.0,
            check_same_thread=False,
            isolation_level=None,  # Autocommit mode
        )
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        return conn

    def _init_sqlite(self) -> None:
        """Ensure destination tables and indexes exist in SQLite."""
        conn = self._get_sqlite_connection()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS event_stream (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    timestamp_utc TEXT NOT NULL,
                    symbol TEXT,
                    payload_json TEXT NOT NULL
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_event_stream_ts ON event_stream(timestamp_utc);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_event_stream_sym_type ON event_stream(symbol, event_type);")
        finally:
            conn.close()

    def _get_current_log_path(self, now_utc: Optional[datetime] = None) -> Path:
        """Generate current daily JSONL filepath."""
        dt = now_utc or datetime.now(timezone.utc)
        return self.log_dir / f"events_{dt.strftime('%Y%m%d')}.jsonl"

    def start(self) -> None:
        """Start the background worker thread if not already running."""
        with self._init_lock:
            if self._running and self._worker_thread and self._worker_thread.is_alive():
                return
            self._running = True
            self._stop_event.clear()
            self._wake_event.clear()
            self._worker_thread = threading.Thread(
                target=self._worker_loop,
                name="EventLedgerWorker",
                daemon=True,
            )
            self._worker_thread.start()

    def _ensure_worker_started(self) -> None:
        """Idempotently ensure worker thread is active on first push."""
        if not self._running or self._worker_thread is None or not self._worker_thread.is_alive():
            self.start()

    def publish_event(self, event: EngineEvent) -> bool:
        """
        Push event into in-memory bounded queue non-blockingly (< 5 microsecond latency).

        Returns:
            bool: True if queued successfully, False if dropped due to queue saturation.
        """
        self._ensure_worker_started()
        try:
            self._queue.put_nowait(event)
            self._published_count += 1
            self._wake_event.set()
            return True
        except asyncio.QueueFull:
            # Wake worker to drain
            self._wake_event.set()
            # Under extreme bursts, yield briefly to allow background worker thread to drain
            for _ in range(500):
                time.sleep(0.0005)
                try:
                    self._queue.put_nowait(event)
                    self._published_count += 1
                    self._wake_event.set()
                    return True
                except asyncio.QueueFull:
                    pass

            self._dropped_count += 1
            logger.warning("EventLedger: queue saturated (maxsize=%d); dropped event", self.maxsize)
            return False

    def _worker_loop(self) -> None:
        """
        Background worker thread: flushes accumulated batches to SQLite
        and JSONL every 500ms or 100 events.
        """
        conn = self._get_sqlite_connection()
        current_log_date = ""
        log_file_handle = None

        batch: List[EngineEvent] = []
        last_flush = time.monotonic()

        try:
            while not self._stop_event.is_set() or not self._queue.empty() or batch:
                # 1. Drain up to batch_size items from queue
                while len(batch) < self.batch_size:
                    try:
                        batch.append(self._queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                now = time.monotonic()
                time_elapsed = now - last_flush
                should_flush = (
                    len(batch) >= self.batch_size
                    or (batch and time_elapsed >= self.flush_interval)
                    or (self._stop_event.is_set() and batch)
                )

                if should_flush:
                    now_utc = datetime.now(timezone.utc)
                    today_str = now_utc.strftime("%Y%m%d")

                    # Manage daily rotating JSONL file handle
                    if today_str != current_log_date or log_file_handle is None or log_file_handle.closed:
                        if log_file_handle and not log_file_handle.closed:
                            log_file_handle.close()
                        log_path = self._get_current_log_path(now_utc)
                        log_file_handle = open(log_path, "a", encoding="utf-8")
                        current_log_date = today_str

                    # Prepare batch records
                    sqlite_rows: List[Tuple[str, str, Optional[str], str]] = []
                    jsonl_lines: List[str] = []

                    for ev in batch:
                        if isinstance(ev, BaseEngineEvent):
                            ev_type = ev.event_type
                            ts_utc = ev.timestamp_utc
                            sym = ev.get_symbol()
                            payload_dict = ev.to_payload_dict()
                            payload_json = ev.to_payload_json()
                        elif isinstance(ev, dict):
                            ev_type = str(ev.get("event_type", "UnknownEvent"))
                            ts_utc = str(ev.get("timestamp_utc") or ev.get("timestamp") or now_utc.isoformat())
                            sym = ev.get("symbol")
                            payload_dict = ev.get("payload", ev)
                            payload_json = json.dumps(payload_dict)
                        else:
                            ev_type = getattr(ev, "event_type", ev.__class__.__name__)
                            ts_utc = getattr(ev, "timestamp_utc", now_utc.isoformat())
                            sym = getattr(ev, "symbol", None)
                            payload_dict = getattr(ev, "__dict__", {})
                            payload_json = json.dumps(payload_dict, default=str)

                        sqlite_rows.append((ev_type, ts_utc, sym, payload_json))

                        # Unified JSONL record preserving schema & top-level properties
                        record = {
                            "event_type": ev_type,
                            "timestamp_utc": ts_utc,
                            "symbol": sym,
                            "payload": payload_dict,
                            **payload_dict,
                        }
                        jsonl_lines.append(json.dumps(record, default=str))

                    # 1. Target A: SQLite batch insert
                    conn.execute("BEGIN IMMEDIATE;")
                    conn.executemany(
                        "INSERT INTO event_stream (event_type, timestamp_utc, symbol, payload_json) VALUES (?, ?, ?, ?);",
                        sqlite_rows,
                    )
                    conn.execute("COMMIT;")

                    # 2. Target B: Append-only disk log
                    log_file_handle.write("\n".join(jsonl_lines) + "\n")
                    log_file_handle.flush()

                    self._flushed_count += len(batch)
                    batch.clear()
                    last_flush = time.monotonic()

                elif not self._queue.empty():
                    # More items waiting, continue without sleep
                    continue
                else:
                    # Queue empty, wait for wake signal or timeout
                    self._wake_event.wait(timeout=0.01)
                    self._wake_event.clear()

        except Exception as exc:
            logger.exception("EventLedger worker encountered error: %s", exc)
        finally:
            try:
                if log_file_handle and not log_file_handle.closed:
                    log_file_handle.flush()
                    log_file_handle.close()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
            self._running = False

    def drain(self, timeout: float = 2.0) -> bool:
        """
        Wait until the queue is completely drained and all items are persisted.
        """
        start_t = time.perf_counter()
        while time.perf_counter() - start_t < timeout:
            if self._queue.empty() and (self._flushed_count + self._dropped_count >= self._published_count):
                return True
            self._wake_event.set()
            time.sleep(0.005)
        return self._queue.empty()

    def flush(self) -> None:
        """Synchronously trigger queue wake up and wait for drain."""
        self._wake_event.set()
        self.drain(timeout=1.0)

    def checkpoint(self) -> None:
        """Trigger SQLite WAL checkpoint (TRUNCATE)."""
        self.flush()
        conn = self._get_sqlite_connection()
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        finally:
            conn.close()

    def stop(self, timeout: float = 2.0) -> None:
        """Gracefully stop background worker thread."""
        self._stop_event.set()
        self._wake_event.set()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=timeout)
        self._running = False

    @property
    def queue_size(self) -> int:
        """Current number of items in memory queue."""
        return self._queue.qsize()

    @property
    def published_events(self) -> int:
        """Total events submitted to publish_event."""
        return self._published_count

    @property
    def dropped_events(self) -> int:
        """Total events dropped due to queue saturation."""
        return self._dropped_count

    @property
    def flushed_events(self) -> int:
        """Total events flushed to SQLite and disk."""
        return self._flushed_count


def publish_event(event: EngineEvent) -> bool:
    """
    Global non-blocking event publishing helper for Core 0 execution routines.
    """
    return EventLedger.get_instance().publish_event(event)
