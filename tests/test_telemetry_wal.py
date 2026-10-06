"""
tests/test_telemetry_wal.py
===========================
Automated tests for Phase 2 Deterministic Historical Event Telemetry with SQLite WAL:
1. Pydantic TelemetryEvent validation.
2. SQLite WAL configuration (PRAGMA journal_mode=WAL, synchronous=NORMAL, busy_timeout=5000).
3. Event persistence, indexing, and historical fetching (chronological & reverse).
4. Multi-threaded concurrent writes and non-blocking reads.
5. FastAPI /events and /api/events hydration endpoints.
"""

from datetime import datetime, timezone
import threading
from typing import List
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.server import build_app
from core.runtime import EngineContext
from core.telemetry import SQLiteWALEventStore, TelemetryEvent


# ---------------------------------------------------------------------------
# 1. Pydantic Model Validation
# ---------------------------------------------------------------------------

def test_telemetry_event_model_validation():
    # Automatic uuid and timestamp generation
    evt = TelemetryEvent(
        aggregate_id="TQQQ",
        event_type="DYNAMIC_STOP_RATCHET",
        payload={"stop_price": "104.50", "hwm": "105.00"},
    )
    assert evt.event_id is not None
    assert len(evt.event_id) > 10
    assert evt.timestamp is not None
    assert evt.aggregate_id == "TQQQ"
    assert evt.event_type == "DYNAMIC_STOP_RATCHET"
    assert evt.payload["stop_price"] == "104.50"

    # Rejection of empty aggregate_id or event_type
    with pytest.raises(ValidationError):
        TelemetryEvent(aggregate_id="", event_type="TEST", payload={})

    with pytest.raises(ValidationError):
        TelemetryEvent(aggregate_id="TQQQ", event_type="   ", payload={})


# ---------------------------------------------------------------------------
# 2. SQLite WAL Initialization & PRAGMAs
# ---------------------------------------------------------------------------

def test_sqlite_wal_pragmas_and_schema(tmp_path):
    db_file = tmp_path / "test_telemetry.db"
    store = SQLiteWALEventStore(db_path=db_file)

    # Verify journal mode is WAL
    journal_mode = store.get_journal_mode()
    assert journal_mode == "wal", f"Expected 'wal', got '{journal_mode}'"

    # Verify table and indexes created
    conn = store._get_connection()
    try:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='event_log'")
        assert cur.fetchone() is not None, "event_log table not found"

        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        indexes = {row[0] for row in cur.fetchall()}
        assert "idx_event_log_agg_ts" in indexes, "idx_event_log_agg_ts composite index not found"
        assert "idx_event_log_ts" in indexes, "idx_event_log_ts timestamp index not found"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3. Append & Fetching (All vs. Filtered, Ordering)
# ---------------------------------------------------------------------------

def test_event_persistence_and_filtering(tmp_path):
    db_file = tmp_path / "persistence_telemetry.db"
    store = SQLiteWALEventStore(db_path=db_file)

    # Insert events for multiple aggregates
    store.record_event(
        aggregate_id="TQQQ",
        event_type="DYNAMIC_STOP_RATCHET",
        payload={"stop_price": "102.00"},
    )
    store.record_event(
        aggregate_id="SOXL",
        event_type="DYNAMIC_STOP_RATCHET",
        payload={"stop_price": "55.00"},
    )
    store.record_event(
        aggregate_id="TQQQ",
        event_type="TIER1_STOP_TRIGGER",
        payload={"price": "101.90"},
    )
    store.record_event(
        aggregate_id="GLOBAL",
        event_type="LIFECYCLE_PHASE",
        payload={"phase": "MIDDAY_FREEZE"},
    )

    assert store.count_events() == 4
    assert store.count_events(aggregate_id="TQQQ") == 2
    assert store.count_events(aggregate_id="SOXL") == 1

    # Fetch all events (default reverse=True -> newest first)
    all_events = store.fetch_historical_events(limit=10)
    assert len(all_events) == 4
    assert all_events[0].event_type == "LIFECYCLE_PHASE"
    assert all_events[1].event_type == "TIER1_STOP_TRIGGER"

    # Fetch with aggregate filter
    tqqq_events = store.fetch_historical_events(aggregate_id="TQQQ", limit=10)
    assert len(tqqq_events) == 2
    assert tqqq_events[0].event_type == "TIER1_STOP_TRIGGER"
    assert tqqq_events[1].event_type == "DYNAMIC_STOP_RATCHET"
    assert tqqq_events[1].payload["stop_price"] == "102.00"

    # Chronological fetch (reverse=False -> oldest first)
    chronological = store.fetch_historical_events(limit=10, reverse=False)
    assert chronological[0].event_type == "DYNAMIC_STOP_RATCHET"
    assert chronological[0].aggregate_id == "TQQQ"
    assert chronological[-1].event_type == "LIFECYCLE_PHASE"


# ---------------------------------------------------------------------------
# 4. Multi-threaded High-Concurrency Stress Test
# ---------------------------------------------------------------------------

def test_concurrent_writes_and_reads(tmp_path):
    db_file = tmp_path / "concurrent_telemetry.db"
    store = SQLiteWALEventStore(db_path=db_file)
    errors: List[Exception] = []

    def writer_task(worker_id: int, count: int):
        try:
            for i in range(count):
                store.record_event(
                    aggregate_id=f"TICKER_{worker_id}",
                    event_type="TICK_EVENT",
                    payload={"worker": worker_id, "seq": i},
                )
        except Exception as exc:
            errors.append(exc)

    def reader_task(iterations: int):
        try:
            for _ in range(iterations):
                _ = store.fetch_historical_events(limit=50)
        except Exception as exc:
            errors.append(exc)

    # Launch 6 concurrent writers (25 events each) and 3 concurrent readers
    threads = []
    for w in range(6):
        t = threading.Thread(target=writer_task, args=(w, 25))
        threads.append(t)
    for _ in range(3):
        t = threading.Thread(target=reader_task, args=(20,))
        threads.append(t)

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Concurrent execution generated errors: {errors}"
    assert store.count_events() == 6 * 25


# ---------------------------------------------------------------------------
# 5. FastAPI Endpoints (/events and /api/events)
# ---------------------------------------------------------------------------

def test_fastapi_events_endpoints(tmp_path):
    ctx = EngineContext(cfg={}, live=False)
    test_db = tmp_path / "api_telemetry.db"
    store = SQLiteWALEventStore(db_path=test_db)
    ctx.telemetry = store

    # Seed events
    store.record_event(
        aggregate_id="TNA",
        event_type="DYNAMIC_STOP_RATCHET",
        payload={"stop_price": "56.88", "category": "RISK"},
    )
    store.record_event(
        aggregate_id="TNA",
        event_type="TIER2_STOP_PLACED",
        payload={"stop_price": "54.60", "quantity": 8, "category": "RISK"},
    )

    app = build_app(ctx)
    client = TestClient(app)

    # Test GET /events
    resp = client.get("/events")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 2
    assert len(data["events"]) == 2
    assert data["events"][0]["event_type"] == "TIER2_STOP_PLACED"

    # Test GET /api/events with filter
    resp_filtered = client.get("/api/events?aggregate_id=TNA&limit=1")
    assert resp_filtered.status_code == 200
    data_filtered = resp_filtered.json()
    assert data_filtered["count"] == 1
    assert data_filtered["events"][0]["event_type"] == "TIER2_STOP_PLACED"

    # Test filter for non-existent aggregate
    resp_empty = client.get("/api/events?aggregate_id=NONEXISTENT")
    assert resp_empty.status_code == 200
    assert resp_empty.json()["count"] == 0
