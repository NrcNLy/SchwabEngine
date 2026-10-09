"""
tests/test_baselines_store.py
=============================
Tests for SQLiteBaselinesStore (market_baselines.db):
1. SQLite WAL mode and schema initialization.
2. 1-minute OHLCV bar upsertion and deduplication.
3. 20-day ADV minute profile calculation and roundtrip.
4. Concurrency under read/write threads.
"""

from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import pytest

from core.baselines_store import SQLiteBaselinesStore


@pytest.fixture
def temp_db(tmp_path):
    db_file = tmp_path / "test_market_baselines.db"
    return SQLiteBaselinesStore(db_path=db_file)


def test_baselines_store_initialization_and_wal(temp_db):
    conn = sqlite3.connect(temp_db.db_path)
    try:
        cur = conn.execute("PRAGMA journal_mode;")
        journal_mode = cur.fetchone()[0]
        assert journal_mode.lower() == "wal"

        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = {row[0] for row in cur.fetchall()}
        assert "bars_1m" in tables
        assert "adv_minute_profiles" in tables
        assert "symbol_baselines" in tables
    finally:
        conn.close()


def test_bars_upsert_and_retrieval(temp_db):
    sym = "SOXL"
    bars = [
        {"timestamp": 1728414000000, "open": 150.0, "high": 152.0, "low": 149.5, "close": 151.5, "volume": 1000},
        {"timestamp": 1728414060000, "open": 151.5, "high": 153.0, "low": 151.0, "close": 152.8, "volume": 1500},
    ]
    inserted = temp_db.upsert_bars(sym, bars)
    assert inserted == 2

    latest_ts = temp_db.get_latest_bar_timestamp(sym)
    assert latest_ts == 1728414060000

    retrieved = temp_db.get_bars(sym)
    assert len(retrieved) == 2
    assert retrieved[0]["close"] == 151.5
    assert retrieved[1]["close"] == 152.8

    # Deduplication test: re-insert updated volume for same timestamp
    updated_bars = [
        {"timestamp": 1728414060000, "open": 151.5, "high": 153.0, "low": 151.0, "close": 152.8, "volume": 2000},
    ]
    temp_db.upsert_bars(sym, updated_bars)
    retrieved_after = temp_db.get_bars(sym)
    assert len(retrieved_after) == 2
    assert retrieved_after[1]["volume"] == 2000


def test_adv_minute_profiles_roundtrip(temp_db):
    sym = "TQQQ"
    profile = {570: 50000.0, 571: 45000.0, 960: 120000.0}
    temp_db.save_adv_profile(sym, profile, sample_days=20)

    loaded = temp_db.get_adv_profile(sym)
    assert len(loaded) == 3
    assert loaded[570] == 50000.0
    assert loaded[571] == 45000.0
    assert loaded[960] == 120000.0


def test_symbol_baselines_roundtrip(temp_db):
    sym = "TNA"
    temp_db.save_symbol_baseline(
        symbol=sym,
        yz_volatility_20d=0.045,
        median_spread_cents=2.5,
        adv_20d_shares=4500000.0,
    )
    b = temp_db.get_symbol_baseline(sym)
    assert b is not None
    assert b["symbol"] == "TNA"
    assert b["yz_volatility_20d"] == 0.045
    assert b["median_spread_cents"] == 2.5
    assert b["adv_20d_shares"] == 4500000.0


def test_concurrent_read_write(temp_db):
    sym = "SOXL"
    errors = []

    def writer():
        for i in range(50):
            try:
                temp_db.upsert_bars(sym, [{
                    "timestamp": 1728414000000 + i * 60000,
                    "open": 100.0 + i,
                    "high": 101.0 + i,
                    "low": 99.0 + i,
                    "close": 100.5 + i,
                    "volume": 1000 + i,
                }])
            except Exception as e:
                errors.append(e)

    def reader():
        for _ in range(50):
            try:
                _ = temp_db.get_bars(sym)
            except Exception as e:
                errors.append(e)

    threads = [
        threading.Thread(target=writer),
        threading.Thread(target=reader),
        threading.Thread(target=reader),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
