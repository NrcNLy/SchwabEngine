"""
core/telemetry.py
=================
Deterministic Historical Event Telemetry with SQLite WAL Storage.

Provides an immutable, append-only event store for system, risk, and order telemetry:
- Pydantic TelemetryEvent model for strict validation.
- SQLite WAL journal mode (PRAGMA journal_mode=WAL;) for concurrent readers and writer.
- High-concurrency PRAGMAs: synchronous=NORMAL, busy_timeout=5000.
- Composite index on (aggregate_id, timestamp) for rapid UI polling and hydration.
- Non-blocking async fetch methods for FastAPI route integration.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sqlite3
import threading
from typing import Any, Dict, List, Optional, Union
import uuid

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TELEMETRY_DB = REPO_ROOT / "data" / "schwab_telemetry.db"


class TelemetryEvent(BaseModel):
    """
    Immutable event entity representing a domain state transition or operational telemetry.
    """
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    aggregate_id: str
    event_type: str
    payload: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("event_id", mode="before")
    @classmethod
    def validate_event_id(cls, v: Any) -> str:
        if not v:
            return str(uuid.uuid4())
        return str(v)

    @field_validator("aggregate_id", "event_type")
    @classmethod
    def validate_non_empty_str(cls, v: Any) -> str:
        s = str(v).strip()
        if not s:
            raise ValueError("Field cannot be empty")
        return s


class SQLiteWALEventStore:
    """
    Append-only telemetry store backed by SQLite with Write-Ahead Logging (WAL) enabled.
    Supports concurrent readers alongside single-writer loops with 5000ms busy timeout.
    """

    def __init__(self, db_path: Union[str, Path] = DEFAULT_TELEMETRY_DB):
        self.db_path = str(db_path)
        self._is_memory = self.db_path == ":memory:"
        if not self._is_memory:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=5.0,
            check_same_thread=False,
            isolation_level=None,  # autocommit
        )
        conn.row_factory = sqlite3.Row
        # Apply high-concurrency PRAGMAs on every connection
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS event_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id TEXT UNIQUE NOT NULL,
                        timestamp TEXT NOT NULL,
                        aggregate_id TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        payload TEXT NOT NULL
                    );
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_event_log_agg_ts
                    ON event_log (aggregate_id, timestamp);
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_event_log_ts
                    ON event_log (timestamp);
                """)
            finally:
                conn.close()

    def append_event(self, event: TelemetryEvent) -> None:
        """
        Appends an event to the immutable SQLite WAL log.
        """
        ts_str = event.timestamp.isoformat()
        payload_str = json.dumps(event.payload, default=str)
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute(
                    """
                    INSERT INTO event_log (event_id, timestamp, aggregate_id, event_type, payload)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (event.event_id, ts_str, event.aggregate_id, event.event_type, payload_str),
                )
            finally:
                conn.close()

    def record_event(
        self,
        aggregate_id: str,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
        timestamp: Optional[datetime] = None,
        event_id: Optional[str] = None,
    ) -> TelemetryEvent:
        """
        Convenience builder to instantiate and persist a TelemetryEvent.
        """
        evt = TelemetryEvent(
            event_id=event_id or str(uuid.uuid4()),
            timestamp=timestamp or datetime.now(timezone.utc),
            aggregate_id=aggregate_id,
            event_type=event_type,
            payload=payload or {},
        )
        self.append_event(evt)
        return evt

    def fetch_historical_events(
        self,
        aggregate_id: Optional[str] = None,
        limit: int = 200,
        reverse: bool = True,
    ) -> List[TelemetryEvent]:
        """
        Fetches historical events from SQLite WAL database.
        If reverse is True, returns newest events first (DESC order).
        """
        conn = self._get_connection()
        try:
            lim = max(1, min(int(limit), 5000))
            if aggregate_id:
                cur = conn.execute(
                    """
                    SELECT event_id, timestamp, aggregate_id, event_type, payload
                    FROM event_log
                    WHERE aggregate_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (aggregate_id, lim),
                )
            else:
                cur = conn.execute(
                    """
                    SELECT event_id, timestamp, aggregate_id, event_type, payload
                    FROM event_log
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (lim,),
                )
            rows = cur.fetchall()
            events: List[TelemetryEvent] = []
            for row in rows:
                raw_payload = row["payload"]
                try:
                    payload_dict = json.loads(raw_payload) if isinstance(raw_payload, str) else dict(raw_payload)
                except Exception:
                    payload_dict = {}
                events.append(
                    TelemetryEvent(
                        event_id=row["event_id"],
                        timestamp=row["timestamp"],
                        aggregate_id=row["aggregate_id"],
                        event_type=row["event_type"],
                        payload=payload_dict,
                    )
                )
            if not reverse:
                events.reverse()
            return events
        finally:
            conn.close()

    async def fetch_historical_events_async(
        self,
        aggregate_id: Optional[str] = None,
        limit: int = 200,
        reverse: bool = True,
    ) -> List[TelemetryEvent]:
        """
        Non-blocking asynchronous wrapper running SQLite queries on a worker thread
        to ensure FastAPI reads do not block the high-frequency tick loop's writer.
        """
        return await asyncio.to_thread(self.fetch_historical_events, aggregate_id, limit, reverse)

    def get_journal_mode(self) -> str:
        """Returns the active SQLite journal mode."""
        conn = self._get_connection()
        try:
            cur = conn.execute("PRAGMA journal_mode;")
            row = cur.fetchone()
            return str(row[0]).lower() if row else ""
        finally:
            conn.close()

    def count_events(self, aggregate_id: Optional[str] = None) -> int:
        """Returns total count of recorded events."""
        conn = self._get_connection()
        try:
            if aggregate_id:
                cur = conn.execute("SELECT COUNT(*) FROM event_log WHERE aggregate_id = ?", (aggregate_id,))
            else:
                cur = conn.execute("SELECT COUNT(*) FROM event_log")
            row = cur.fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
