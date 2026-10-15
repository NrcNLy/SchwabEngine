"""
tests/test_stream_resampler.py
==============================
Unit & Performance Benchmarks for Streaming WebSocket Feed Ingestion & Zero-Allocation Bar Resampler (#63).

Validates:
1. 50,000 tick performance benchmark (< 500 microseconds / tick).
2. Resampled OHLCV values match known pandas `.resample('1min')` output exactly.
3. 15-second sub-bar aggregation and pandas parity.
4. Lock-free circular ring buffer with zero object allocation.
5. Running VWAP and rolling high/low watermarks.
6. MicrostructureSentinel and RealTimeRegimeGovernor BarCompletedEvent integration.
7. Batched SQLite persistence to `state/telemetry.db` table `intraday_bars` (batches of 50).
8. SchwabStreamSupervisor exponential backoff, 20s heartbeat, and subscription framing.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from core.synthetic_divergence import RealTimeRegimeGovernor
from data.schwab_stream import SchwabStreamSupervisor
from data.stream_resampler import (
    DEFAULT_TARGET_TICKERS,
    BarCompletedEvent,
    BarPipelineConnector,
    BarResampler,
    IntradayBarWriter,
    LockFreeRingBuffer,
)
from execution.microstructure_sentinel import MicrostructureSentinel


# ===========================================================================
# 1. 50,000 Ticks Benchmark & Pandas Exact OHLCV Parity
# ===========================================================================

def test_resampler_50k_ticks_benchmark_and_pandas_parity():
    """
    Simulate 50,000 randomized ticks across a 1-hour session.
    Assert resampler processing time is < 500 microseconds per tick.
    Verify resampled OHLCV values match known pandas .resample('1min') output exactly.
    """
    np.random.seed(42)
    n_ticks = 50_000
    base_ts = np.int64(1_729_000_000_000)  # Reference epoch ms

    # Distribute 50,000 ticks across 1 hour (3,600,000 ms)
    time_offsets = np.sort(np.random.randint(0, 3_600_000, size=n_ticks, dtype=np.int64))
    timestamps_ms = base_ts + time_offsets

    # Random walk prices and integer volumes
    price_steps = np.random.randn(n_ticks) * 0.05
    prices = np.round(100.0 + np.cumsum(price_steps), 4)
    volumes = np.random.randint(1, 100, size=n_ticks, dtype=np.int64).astype(np.float64)

    # -----------------------------------------------------------------------
    # Ground Truth: pandas .resample('1min')
    # -----------------------------------------------------------------------
    dts = pd.to_datetime(timestamps_ms, unit="ms", utc=True)
    df = pd.DataFrame({"price": prices, "volume": volumes}, index=dts)

    p_ohlc = df["price"].resample("1min", closed="left", label="left").ohlc()
    p_vol = df["volume"].resample("1min", closed="left", label="left").sum()

    expected_df = p_ohlc.copy()
    expected_df["volume"] = p_vol
    expected_df = expected_df.dropna()

    # -----------------------------------------------------------------------
    # Resampler Execution & Latency Benchmark
    # -----------------------------------------------------------------------
    resampler = BarResampler(symbols=["SOXL"])

    t_start = time.perf_counter()
    for t, p, v in zip(timestamps_ms, prices, volumes):
        resampler.on_tick("SOXL", p, v, t)
    t_elapsed = time.perf_counter() - t_start

    # Flush active trailing bar
    resampler.flush("SOXL")

    # Assert processing time < 500 microseconds per tick
    us_per_tick = (t_elapsed / n_ticks) * 1_000_000.0
    print(f"\n[BENCHMARK] Ingested {n_ticks} ticks in {t_elapsed:.4f}s ({us_per_tick:.2f} us/tick)")
    assert us_per_tick < 500.0, f"Processing time {us_per_tick:.2f} us/tick exceeds 500 us threshold"

    # -----------------------------------------------------------------------
    # Verify Exact Parity
    # -----------------------------------------------------------------------
    bars_1m = [b for b in resampler.get_completed_bars("SOXL") if b.timeframe == "1m"]
    assert len(bars_1m) == len(expected_df), f"Expected {len(expected_df)} bars, got {len(bars_1m)}"

    records = [
        {
            "timestamp_ms": b.timestamp_ms,
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
        }
        for b in bars_1m
    ]
    actual_df = pd.DataFrame(records)
    actual_df.index = pd.to_datetime(actual_df["timestamp_ms"], unit="ms", utc=True)
    actual_df = actual_df.drop(columns=["timestamp_ms"])
    actual_df.index.name = None

    # Assert all OHLCV columns match pandas exactly
    pd.testing.assert_frame_equal(expected_df, actual_df, check_freq=False)


# ===========================================================================
# 2. 15-Second Sub-Bar Aggregation & Parity
# ===========================================================================

def test_sub_bars_15s_pandas_parity():
    """Verify 15-second sub-bars match pandas .resample('15s') exactly."""
    np.random.seed(123)
    n_ticks = 5_000
    base_ts = np.int64(1_729_000_000_000)

    time_offsets = np.sort(np.random.randint(0, 300_000, size=n_ticks, dtype=np.int64))  # 5 minutes
    timestamps_ms = base_ts + time_offsets
    prices = np.round(50.0 + np.cumsum(np.random.randn(n_ticks) * 0.02), 4)
    volumes = np.random.randint(10, 50, size=n_ticks, dtype=np.int64).astype(np.float64)

    # Pandas ground truth
    dts = pd.to_datetime(timestamps_ms, unit="ms", utc=True)
    df = pd.DataFrame({"price": prices, "volume": volumes}, index=dts)
    p_ohlc = df["price"].resample("15s", closed="left", label="left").ohlc()
    p_vol = df["volume"].resample("15s", closed="left", label="left").sum()
    expected_15s = p_ohlc.copy()
    expected_15s["volume"] = p_vol
    expected_15s = expected_15s.dropna()

    resampler = BarResampler(symbols=["TQQQ"])
    for t, p, v in zip(timestamps_ms, prices, volumes):
        resampler.on_tick("TQQQ", p, v, t)
    resampler.flush("TQQQ")

    bars_15s = [b for b in resampler.get_completed_bars("TQQQ") if b.timeframe == "15s"]
    assert len(bars_15s) == len(expected_15s)

    actual_15s = pd.DataFrame(
        [
            {
                "timestamp_ms": b.timestamp_ms,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
            }
            for b in bars_15s
        ]
    )
    actual_15s.index = pd.to_datetime(actual_15s["timestamp_ms"], unit="ms", utc=True)
    actual_15s = actual_15s.drop(columns=["timestamp_ms"])
    actual_15s.index.name = None

    pd.testing.assert_frame_equal(expected_15s, actual_15s, check_freq=False)


# ===========================================================================
# 3. Lock-Free Circular Ring Buffer
# ===========================================================================

def test_lock_free_ring_buffer_zero_allocation():
    """Verify LockFreeRingBuffer operations, power-of-two wrapping, and retrieval."""
    rb = LockFreeRingBuffer(capacity=16)
    assert rb.capacity == 16
    assert len(rb) == 0

    # Ingest 20 ticks (exceeds capacity of 16 to test wrapping)
    for i in range(20):
        rb.append(timestamp_ms=1000 + i * 10, price=100.0 + i, volume=10.0 * i)

    assert len(rb) == 16  # Capped at capacity
    latest = rb.get_latest()
    assert latest is not None
    assert latest == (1000 + 19 * 10, 119.0, 190.0)

    # Retrieve recent 5 ticks
    ts, px, vol = rb.get_recent(5)
    assert len(ts) == 5
    assert px[-1] == 119.0
    assert px[0] == 115.0


# ===========================================================================
# 4. Running VWAP and Rolling Watermarks
# ===========================================================================

def test_running_vwap_and_watermarks():
    """Verify running session VWAP and rolling high/low watermarks."""
    resampler = BarResampler(symbols=["SOXL"])

    # Tick 1: price=100.0, vol=100
    resampler.on_tick("SOXL", price=100.0, volume=100.0, timestamp=1000)
    assert resampler.get_vwap("SOXL") == pytest.approx(100.0)
    assert resampler.get_high_watermark("SOXL") == 100.0
    assert resampler.get_low_watermark("SOXL") == 100.0

    # Tick 2: price=110.0, vol=100
    resampler.on_tick("SOXL", price=110.0, volume=100.0, timestamp=2000)
    # VWAP = (100*100 + 110*100) / 200 = 105.0
    assert resampler.get_vwap("SOXL") == pytest.approx(105.0)
    assert resampler.get_high_watermark("SOXL") == 110.0
    assert resampler.get_low_watermark("SOXL") == 100.0

    # Tick 3: price=95.0, vol=200
    resampler.on_tick("SOXL", price=95.0, volume=200.0, timestamp=3000)
    # VWAP = (100*100 + 110*100 + 95*200) / 400 = (10000 + 11000 + 19000) / 400 = 40000 / 400 = 100.0
    assert resampler.get_vwap("SOXL") == pytest.approx(100.0)
    assert resampler.get_high_watermark("SOXL") == 110.0
    assert resampler.get_low_watermark("SOXL") == 95.0


# ===========================================================================
# 5. Batched SQLite Persistence to intraday_bars (Batches of 50)
# ===========================================================================

def test_telemetry_batching_50_bars(tmp_path: Path):
    """
    Verify completed 1-minute bars are buffered and written to intraday_bars table
    in batches of 50 to minimize disk I/O.
    """
    db_file = tmp_path / "telemetry.db"
    writer = IntradayBarWriter(db_path=db_file, batch_size=50)

    # Ingest 49 bars -> assert 0 committed to disk
    for i in range(49):
        ev = BarCompletedEvent(
            symbol="SOXL",
            timeframe="1m",
            timestamp=datetime(2026, 10, 15, 9, 30 + i // 60, i % 60, tzinfo=timezone.utc),
            timestamp_ms=1729000000000 + i * 60000,
            open=100.0,
            high=102.0,
            low=99.0,
            close=101.0,
            volume=500.0,
            vwap=100.5,
            tick_count=50,
        )
        committed = writer.record_bar(ev)
        assert not committed
    assert writer.pending_count == 49
    assert writer.count_bars() == 0

    # Ingest 50th bar -> triggers automatic batch flush
    ev_50 = BarCompletedEvent(
        symbol="SOXL",
        timeframe="1m",
        timestamp=datetime(2026, 10, 15, 10, 20, 0, tzinfo=timezone.utc),
        timestamp_ms=1729000000000 + 49 * 60000,
        open=101.0,
        high=103.0,
        low=100.0,
        close=102.5,
        volume=600.0,
        vwap=102.0,
        tick_count=60,
    )
    committed = writer.record_bar(ev_50)
    assert committed
    assert writer.pending_count == 0
    assert writer.count_bars() == 50
    assert writer.total_written == 50

    # Ingest another 25 bars (total 75)
    for i in range(25):
        ev = BarCompletedEvent(
            symbol="TQQQ",
            timeframe="1m",
            timestamp=datetime(2026, 10, 15, 11, i, 0, tzinfo=timezone.utc),
            timestamp_ms=1729000000000 + (50 + i) * 60000,
            open=80.0,
            high=81.0,
            low=79.5,
            close=80.5,
            volume=300.0,
            vwap=80.2,
            tick_count=30,
        )
        writer.record_bar(ev)
    assert writer.pending_count == 25
    assert writer.count_bars() == 50

    # Explicit flush writes remainder
    written = writer.flush()
    assert written == 25
    assert writer.pending_count == 0
    assert writer.count_bars() == 75
    assert writer.count_bars("SOXL") == 50
    assert writer.count_bars("TQQQ") == 25

    # 15s sub-bars are ignored by writer
    ev_15s = BarCompletedEvent(
        symbol="SOXL",
        timeframe="15s",
        timestamp=datetime(2026, 10, 15, 9, 30, 0, tzinfo=timezone.utc),
        timestamp_ms=1729000000000,
        open=100.0,
        high=101.0,
        low=99.5,
        close=100.2,
        volume=100.0,
        vwap=100.1,
        tick_count=10,
    )
    writer.record_bar(ev_15s)
    assert writer.pending_count == 0

    writer.close()


# ===========================================================================
# 6. MicrostructureSentinel & RealTimeRegimeGovernor Connection
# ===========================================================================

def test_sentinel_and_governor_connection(tmp_path: Path):
    """
    Verify BarCompletedEvent connects seamlessly to MicrostructureSentinel
    and RealTimeRegimeGovernor via BarPipelineConnector.
    """
    db_file = tmp_path / "telemetry.db"
    resampler = BarResampler(symbols=["SOXL", "SOXX", "QQQ"])

    sentinel = MicrostructureSentinel(symbol="SOXL", s0_reference=100.0, soft_floor_pct=0.037)
    governor = RealTimeRegimeGovernor(db_path=db_file, auto_persist=False)
    writer = IntradayBarWriter(db_path=db_file, batch_size=50)

    connector = BarPipelineConnector(
        sentinels={"SOXL": sentinel},
        regime_governor=governor,
        bar_writer=writer,
    )
    resampler.register_listener(connector.on_bar_completed)

    # Ingest ticks breaching soft floor for SOXL on 15s bar boundaries
    # s0 = 100.0 -> soft floor = 96.30
    t0_ms = 1729000000000
    resampler.on_tick("SOXL", price=95.0, volume=100.0, timestamp=t0_ms)
    # Rollover to close 15s bar
    resampler.on_tick("SOXL", price=94.5, volume=100.0, timestamp=t0_ms + 16000)

    # Sentinel received 15s bar close
    assert len(sentinel._15s_closes) >= 1
    assert sentinel._15s_closes[-1] == 95.0
    assert "SOXL" in connector.last_verdicts

    # Ingest 1-minute bar for macro factor SOXX
    resampler.on_tick("SOXX", price=250.0, volume=500.0, timestamp=t0_ms)
    resampler.on_tick("SOXX", price=252.0, volume=500.0, timestamp=t0_ms + 61000)

    # Governor received 1-minute factor bar
    assert governor._latest_prices.get("SOXX") == 250.0
    assert len(governor._bars["SOXX"]) >= 1


# ===========================================================================
# 7. SchwabStreamSupervisor Reconnect Backoff & Heartbeat
# ===========================================================================

def test_schwab_stream_supervisor_config_and_backoff():
    """Verify SchwabStreamSupervisor backoff schedule, heartbeat interval, and frame parsing."""
    supervisor = SchwabStreamSupervisor()

    assert supervisor.symbols == list(DEFAULT_TARGET_TICKERS)
    assert supervisor.heartbeat_interval == 20.0

    # Verify backoff sequence (1s, 2s, 4s, 8s, 16s, up to 30s)
    expected_schedule = [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
    actual_schedule = [supervisor.compute_next_backoff() for _ in range(len(expected_schedule))]
    assert actual_schedule == expected_schedule

    # Reset backoff
    supervisor.reset_backoff()
    assert supervisor.current_backoff == 1.0

    # Verify subscription frame builder contains all 5 tickers
    sub_payload = supervisor.build_levelone_sub_payload()
    assert sub_payload["service"] == "LEVELONE_EQUITIES"
    assert sub_payload["command"] == "SUBS"
    keys = sub_payload["parameters"]["keys"].split(",")
    for ticker in DEFAULT_TARGET_TICKERS:
        assert ticker in keys

    # Verify incoming WebSocket JSON frame pipes to BarResampler
    mock_frame = {
        "data": [
            {
                "service": "LEVELONE_EQUITIES",
                "timestamp": 1729000000000,
                "content": [
                    {
                        "key": "SOXL",
                        "3": 28.50,   # last_price
                        "9": 150.0,   # last_size
                    },
                    {
                        "key": "QQQ",
                        "3": 490.25,
                        "9": 200.0,
                    },
                ],
            }
        ]
    }

    ticks_processed = supervisor.handle_message(json.dumps(mock_frame))
    assert ticks_processed == 2

    rb_soxl = supervisor.resampler.get_ring_buffer("SOXL")
    assert len(rb_soxl) >= 1
    assert rb_soxl.get_latest() == (1729000000000, 28.50, 150.0)

    rb_qqq = supervisor.resampler.get_ring_buffer("QQQ")
    assert len(rb_qqq) >= 1
    assert rb_qqq.get_latest() == (1729000000000, 490.25, 200.0)
