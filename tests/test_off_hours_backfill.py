"""
tests/test_off_hours_backfill.py
================================
Tests for workers/off_hours_backfill.py:
1. Daily API quota circuit breaker (abort >= 2,500 calls).
2. Rate limiter 30 RPM minimum delay (>= 2.0s).
3. Bar processing and ADV minute profile updates.
"""

from unittest.mock import MagicMock
import pytest

from core.baselines_store import SQLiteBaselinesStore
from workers.off_hours_backfill import (
    OffHoursBackfillWorker,
    QuotaCeilingExceededError,
)


def test_quota_ceiling_guard_aborts_at_2500(tmp_path):
    store = SQLiteBaselinesStore(db_path=tmp_path / "test_mb.db")
    worker = OffHoursBackfillWorker(store=store, quota_threshold=2500)

    mock_limiter = MagicMock()
    mock_limiter.daily_calls_used = 2500

    with pytest.raises(QuotaCeilingExceededError, match="Historical backfill ABORTED"):
        worker.check_quota(limiter=mock_limiter)


def test_quota_passes_below_2500(tmp_path):
    store = SQLiteBaselinesStore(db_path=tmp_path / "test_mb.db")
    worker = OffHoursBackfillWorker(store=store, quota_threshold=2500)

    mock_limiter = MagicMock()
    mock_limiter.daily_calls_used = 2499

    calls = worker.check_quota(limiter=mock_limiter)
    assert calls == 2499


def test_process_symbol_bars_and_adv_profile(tmp_path):
    store = SQLiteBaselinesStore(db_path=tmp_path / "test_mb.db")
    worker = OffHoursBackfillWorker(store=store, symbols=["SOXL"])

    candles = [
        # 09:30 EDT = 13:30 UTC = mod 570
        {"datetime": 1728480600000, "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 50000},
        # 09:31 EDT = 13:31 UTC = mod 571
        {"datetime": 1728480660000, "open": 101.0, "high": 103.0, "low": 100.5, "close": 102.0, "volume": 35000},
    ]

    inserted = worker.process_symbol_bars("SOXL", candles)
    assert inserted == 2

    adv = worker.update_adv_profiles("SOXL")
    assert len(adv) == 2
    assert adv[570] == 50000.0
    assert adv[571] == 35000.0


def test_30_rpm_rate_delay_configured():
    worker = OffHoursBackfillWorker()
    assert worker.rate_delay >= 2.0
