"""
tests/test_microstructure.py
============================
Unit, contract, and benchmark tests for Phase 2 High-Frequency Microstructure Engine:
- data/book.py (BookSnapshot, parse_book_frame, is_valid)
- indicators/of_imbalance.py (MLOFIEngine, CKS events, spoof clipping, multi-level weights)
- indicators/vpin.py (VPINEngine, BVC normal CDF, volume clock, toxicity, persistence)
- indicators/lead_lag.py (LeadLagTracker, yield spike, KRE weakness, SOXL equity leaders)
- execution/risk_manager.py (evaluate_microstructure_gate, differentiated strategy policies)
- indicators/microstructure.py (MicrostructureHub, shadow vs enforce, latency benchmark)
"""

from __future__ import annotations

import math
import time
from typing import Dict, Any

import pytest

from data.book import BookSnapshot, is_valid, parse_book_frame
from indicators.lead_lag import LeadLagReading, LeadLagTracker
from indicators.microstructure import MicroSnapshot, MicrostructureHub
from indicators.of_imbalance import MLOFIEngine, MLOFIReading
from indicators.vpin import VPINEngine, VPINReading, norm_cdf
from execution.risk_manager import MicroDecision, evaluate_microstructure_gate
from core.session import TradingPhase


# ===========================================================================
# 1. Book Parsing & Validation (data/book.py)
# ===========================================================================

def test_parse_book_frame_valid():
    content = {
        "key": "TQQQ",
        "1": 1728000000000,
        "2": [  # bids
            {"0": 75.50, "1": 500, "2": 3},
            {"0": 75.49, "1": 1200, "2": 5},
        ],
        "3": [  # asks
            {"0": 75.52, "1": 800, "2": 4},
            {"0": 75.53, "1": 1500, "2": 6},
        ],
    }
    snap = parse_book_frame(content, venue="NASDAQ_BOOK")
    assert snap is not None
    assert snap.symbol == "TQQQ"
    assert snap.venue == "NASDAQ_BOOK"
    assert is_valid(snap)
    assert len(snap.bids) == 2
    assert len(snap.asks) == 2
    assert snap.bids[0][0] == 75.50
    assert snap.asks[0][0] == 75.52
    assert snap.spread == pytest.approx(0.02)
    assert snap.mid == pytest.approx(75.51)


def test_parse_book_frame_invalid_crossed():
    # Bid >= Ask is invalid (crossed)
    content = {
        "key": "TQQQ",
        "2": [{"0": 75.60, "1": 100}],
        "3": [{"0": 75.50, "1": 100}],
    }
    snap = parse_book_frame(content, venue="NASDAQ_BOOK")
    assert snap is not None
    assert not is_valid(snap)


def test_parse_book_frame_empty_or_malformed():
    assert parse_book_frame({}) is None
    assert parse_book_frame({"key": ""}) is None
    assert parse_book_frame({"key": "TQQQ", "2": [], "3": []}) is None


# ===========================================================================
# 2. Multi-Level Order Flow Imbalance (indicators/of_imbalance.py)
# ===========================================================================

def test_mlofi_cks_event_handling():
    engine = MLOFIEngine("TQQQ", levels=2, windows_ms=(1000, 5000), min_events=1, unanimous_min_levels=1)

    # Initial snapshot: Bid 75.50 x 100, Ask 75.52 x 100
    t0 = 1000000
    snap0 = BookSnapshot(
        symbol="TQQQ", ts_ms=t0,
        bids=((75.50, 100.0), (75.49, 100.0)),
        asks=((75.52, 100.0), (75.53, 100.0)),
    )
    engine.on_book(snap0)
    assert not engine.reading(t0).ready  # Need at least 1 event

    # Second snapshot: Bid price unchanged, size increased from 100 to 300 (+200 at level 0)
    # Ask price unchanged, size unchanged
    t1 = t0 + 100
    snap1 = BookSnapshot(
        symbol="TQQQ", ts_ms=t1,
        bids=((75.50, 300.0), (75.49, 100.0)),
        asks=((75.52, 100.0), (75.53, 100.0)),
    )
    engine.on_book(snap1)

    r = engine.reading(t1)
    assert r.ready
    # Positive delta at level 0
    assert r.raw["1s"] > 0
    assert r.norm["1s"] > 0


def test_mlofi_l1_fallback():
    engine = MLOFIEngine("TQQQ", levels=1, windows_ms=(1000,), min_events=1)
    t0 = 1000
    engine.on_l1(bid=75.50, bid_sz=100, ask=75.52, ask_sz=100, ts_ms=t0)
    # Ask size drops to 50 (positive OFI: ask size decrease -> buying pressure)
    engine.on_l1(bid=75.50, bid_sz=100, ask=75.52, ask_sz=50, ts_ms=t0 + 100)

    r = engine.reading(t0 + 100)
    assert r.ready
    assert r.raw["1s"] > 0
    assert r.norm["1s"] > 0


def test_mlofi_spoof_clip():
    engine = MLOFIEngine("TQQQ", levels=1, windows_ms=(1000,), spoof_clip_mult=3.0, min_events=1)
    t = 1000
    # Warm up EWMA with 25 small normal updates
    for i in range(25):
        engine.on_l1(bid=75.50, bid_sz=100 + (i % 5), ask=75.52, ask_sz=100 + (i % 5), ts_ms=t)
        t += 10
    # Huge phantom flash order of 100,000 size (must be winsorized/clipped)
    engine.on_l1(bid=75.50, bid_sz=100000, ask=75.52, ask_sz=100, ts_ms=t + 10)
    r = engine.reading(t + 10)
    assert r.ready
    # Clipped delta is at most 3.0 * EWMA (< 500 shares)
    assert r.raw["1s"] < 500


# ===========================================================================
# 3. Directional VPIN & Volume Clock (indicators/vpin.py)
# ===========================================================================

def test_norm_cdf():
    assert norm_cdf(0.0) == pytest.approx(0.5, abs=1e-5)
    assert norm_cdf(-3.0) < 0.01
    assert norm_cdf(3.0) > 0.99


def test_vpin_volume_clock_and_bucketing():
    engine = VPINEngine(
        "TQQQ", bucket_divisor=10, n_buckets=5, min_buckets=3,
        sigma_min_buckets=2, percentile_lookback=20, percentile_min_history=5, toxic_percentile=0.90
    )
    # Set ADV to 100,000 shares -> bucket volume = 100,000 / 10 = 10,000
    engine.set_adv(100000)
    assert engine.bucket_volume == 10000

    # Stream trades to fill buckets with price movements
    now = 1000
    for i in range(12):
        # alternate prices to establish sigma and classify buckets
        px = 75.00 + (0.10 if i % 2 == 0 else -0.10)
        engine.on_trade(px, 10000, now)
        now += 10

    reading = engine.reading()
    assert reading.ready
    assert reading.buckets >= 3
    assert 0.0 <= reading.vpin <= 1.0


def test_vpin_persistence_and_restore():
    engine = VPINEngine("TQQQ", bucket_divisor=10, n_buckets=5, min_buckets=2, sigma_min_buckets=2)
    engine.set_adv(50000)
    engine.on_trade(75.00, 5000, 1000)
    engine.on_trade(75.50, 10000, 2000)
    engine.on_trade(75.20, 10000, 3000)
    engine.on_trade(75.80, 10000, 4000)

    state = engine.snapshot_state()
    assert "buckets" in state
    assert len(state["buckets"]) >= 1

    restored = VPINEngine("TQQQ", bucket_divisor=10, n_buckets=5, min_buckets=2, sigma_min_buckets=2)
    assert restored.restore_state(state)
    assert list(restored._buckets) == list(engine._buckets)
    assert restored._sig_mean == engine._sig_mean
    assert restored._sig_var == engine._sig_var


# ===========================================================================
# 4. Cross-Asset Lead-Lag Telemetry (indicators/lead_lag.py)
# ===========================================================================

def test_lead_lag_yield_spike_veto():
    tracker = LeadLagTracker(
        cfg={"tqqq": {"yield_symbol": "$TNX", "window_s": 2, "spike_z": 2.0}},
        mlofi_cfg={},
    )
    # Feed 50 baseline ticks across 50 seconds (with 2s window) to warm up rate EWMA (min 30 samples)
    t = 100000
    rate = 4.20
    for i in range(50):
        tracker.on_rate_tick("$TNX", rate + (0.001 if i % 2 == 0 else -0.001), t)
        t += 1000

    # Rate spikes by +0.10 (10 basis points)
    t += 1000
    tracker.on_rate_tick("$TNX", rate + 0.10, t)

    reading = tracker.bias("TQQQ", t)
    assert reading.available
    assert reading.veto_long  # 10Y yield spike triggers veto_long


def test_lead_lag_kre_breakout_invalidation():
    tracker = LeadLagTracker(
        cfg={"tna": {"leader": "KRE", "kre_neg": 0.05, "kre_ret_floor": 0.003, "ret_window_s": 100}},
        mlofi_cfg={"min_events": 2},
    )
    t0 = 100000
    # Initial quote and price 70,000 ms ago
    tracker.on_leader_l1("KRE", 55.00, 100, 55.02, 100, 55.00, t0)

    # 70 seconds later, KRE drops by 0.5% (to 54.70) with negative order flow (ask drops, bids weaken)
    t1 = t0 + 70000
    tracker.on_leader_l1("KRE", 54.68, 50, 54.70, 300, 54.70, t1)
    tracker.on_leader_l1("KRE", 54.66, 30, 54.68, 400, 54.68, t1 + 500)

    reading = tracker.bias("TNA", t1 + 500)
    assert reading.available
    assert reading.invalidate_breakout


# ===========================================================================
# 5. Microstructure Entry Gate Evaluation (execution/risk_manager.py)
# ===========================================================================

@pytest.fixture
def base_config():
    return {
        "enabled": True,
        "mode": "shadow",
        "mlofi": {
            "neg_deadband": 0.05,
            "hard_floor": -0.50,
            "confirm_threshold": 0.10,
            "neutral_band": 0.05,
            "accum_min": 0.10,
        },
        "gate": {
            "lead_confirm": 0.10,
            "lead_unavailable_policy": "mlofi_only",
        },
    }


def test_gate_disabled_allows(base_config):
    base_config["enabled"] = False
    snap = MicroSnapshot(
        symbol="TQQQ", now_ms=1000,
        mlofi=MLOFIReading(symbol="TQQQ", ready=False, depth_levels_available=0, raw={}, norm={}, level_contrib=(), unanimous_positive=False, unanimous_negative=False, crossed_events=0, invalid_events=0, events_in_longest_window=0),
        vpin=VPINReading(symbol="TQQQ", ready=False, buckets=0, bucket_volume=0, vpin=0, vpin_dir=0, percentile=0, percentile_ready=False, toxic=False),
        lead=LeadLagReading(target="TQQQ", available=False, bias=0, veto_long=False, invalidate_breakout=False, anticipatory=False, components={}, stale_sources=()),
        depth_source="NONE",
    )
    dec = evaluate_microstructure_gate(snap, "TQQQ", "15m_ORB", TradingPhase.MORNING_DRIVE, base_config)
    assert dec.action == "ALLOW"
    assert dec.code == "DISABLED"


def test_gate_yield_spike_veto(base_config):
    snap = MicroSnapshot(
        symbol="TQQQ", now_ms=1000,
        mlofi=MLOFIReading(symbol="TQQQ", ready=True, depth_levels_available=5, raw={}, norm={"5s": 0.20, "1s": 0.20}, level_contrib=(), unanimous_positive=True, unanimous_negative=False, crossed_events=0, invalid_events=0, events_in_longest_window=100),
        vpin=VPINReading(symbol="TQQQ", ready=True, buckets=10, bucket_volume=1000, vpin=0.2, vpin_dir=0.1, percentile=0.5, percentile_ready=True, toxic=False),
        lead=LeadLagReading(target="TQQQ", available=True, bias=0.2, veto_long=True, invalidate_breakout=False, anticipatory=False, components={}, stale_sources=()),
        depth_source="BOOK",
    )
    dec = evaluate_microstructure_gate(snap, "TQQQ", "15m_ORB", TradingPhase.MORNING_DRIVE, base_config)
    assert dec.suppress
    assert dec.code == "YIELD_SPIKE"


def test_gate_differentiated_policy_orb_vs_vwap_mr(base_config):
    # Flow is mildly negative: MLOFI(5s) = -0.08 (worse than -0.05 deadband, but above -0.50 hard floor)
    snap = MicroSnapshot(
        symbol="SOXL", now_ms=1000,
        mlofi=MLOFIReading(symbol="SOXL", ready=True, depth_levels_available=5, raw={}, norm={"5s": -0.08, "1s": -0.08}, level_contrib=(), unanimous_positive=False, unanimous_negative=True, crossed_events=0, invalid_events=0, events_in_longest_window=100),
        vpin=VPINReading(symbol="SOXL", ready=True, buckets=10, bucket_volume=1000, vpin=0.2, vpin_dir=-0.1, percentile=0.5, percentile_ready=True, toxic=False),
        lead=LeadLagReading(target="SOXL", available=True, bias=-0.05, veto_long=False, invalidate_breakout=False, anticipatory=False, components={}, stale_sources=()),
        depth_source="BOOK",
    )

    # 1. ORB strategy MUST BE SUPPRESSED because MLOFI < -0.05
    dec_orb = evaluate_microstructure_gate(snap, "SOXL", "15m_ORB", TradingPhase.MID_MORNING, base_config)
    assert dec_orb.suppress
    assert dec_orb.code == "MLOFI_NEG"

    # 2. VWAP_MR strategy MUST BE ALLOWED because MLOFI (-0.08) > hard floor (-0.50) and VPIN is non-toxic
    dec_mr = evaluate_microstructure_gate(snap, "SOXL", "VWAP_MR", TradingPhase.MID_MORNING, base_config)
    assert not dec_mr.suppress
    assert dec_mr.action == "ALLOW"
    assert dec_mr.code == "OK"


def test_gate_vpin_toxic_veto(base_config):
    snap = MicroSnapshot(
        symbol="TQQQ", now_ms=1000,
        mlofi=MLOFIReading(symbol="TQQQ", ready=True, depth_levels_available=5, raw={}, norm={"5s": 0.01, "1s": 0.01}, level_contrib=(), unanimous_positive=False, unanimous_negative=False, crossed_events=0, invalid_events=0, events_in_longest_window=50),
        vpin=VPINReading(symbol="TQQQ", ready=True, buckets=15, bucket_volume=1000, vpin=0.75, vpin_dir=-0.5, percentile=0.95, percentile_ready=True, toxic=True),
        lead=LeadLagReading(target="TQQQ", available=True, bias=0.0, veto_long=False, invalidate_breakout=False, anticipatory=False, components={}, stale_sources=()),
        depth_source="BOOK",
    )
    dec = evaluate_microstructure_gate(snap, "TQQQ", "VWAP_MR", TradingPhase.MID_MORNING, base_config)
    assert dec.suppress
    assert dec.code == "VPIN_TOXIC"
    assert dec.regime_hint == "C"


# ===========================================================================
# 6. MicrostructureHub (indicators/microstructure.py) & Latency Benchmark
# ===========================================================================

def test_hub_shadow_mode_blocks_is_false():
    cfg = {
        "engine": {"symbols": ["TQQQ", "SOXL", "TNA"]},
        "microstructure": {
            "enabled": True,
            "mode": "shadow",
            "mlofi": {"neg_deadband": 0.05},
        },
    }
    hub = MicrostructureHub.from_config(cfg)
    assert hub is not None
    assert hub.mode == "shadow"
    assert not hub.enforce

    # Simulate a toxic decision
    decision = MicroDecision("SUPPRESS", "VPIN_TOXIC", "simulated toxicity")
    assert not hub.blocks(decision)  # In shadow mode, blocks() is always False!


def test_hub_enforce_mode_blocks_is_true():
    cfg = {
        "engine": {"symbols": ["TQQQ", "SOXL", "TNA"]},
        "microstructure": {
            "enabled": True,
            "mode": "enforce",
            "mlofi": {"neg_deadband": 0.05},
        },
    }
    hub = MicrostructureHub.from_config(cfg)
    assert hub is not None
    assert hub.mode == "enforce"
    assert hub.enforce

    decision = MicroDecision("SUPPRESS", "VPIN_TOXIC", "simulated toxicity")
    assert hub.blocks(decision)  # In enforce mode, blocks() is True!


def test_hub_latency_benchmark():
    cfg = {
        "engine": {"symbols": ["TQQQ", "SOXL", "TNA"]},
        "microstructure": {
            "enabled": True,
            "mode": "shadow",
        },
    }
    hub = MicrostructureHub.from_config(cfg)
    assert hub is not None

    # Benchmark 1,000 tick updates
    t0 = time.perf_counter()
    for i in range(1000):
        hub.on_tick("TQQQ", {
            "bid_price": 75.50 + (i % 10) * 0.01,
            "ask_price": 75.52 + (i % 10) * 0.01,
            "bid_size": 100 + (i % 50),
            "ask_size": 100 + (i % 50),
            "last_price": 75.51,
            "total_volume": 100000 + i * 100,
        })
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    avg_us = (elapsed_ms / 1000.0) * 1000.0

    # P99 latency target < 1000 µs (1ms), expected < 50 µs
    assert avg_us < 1000.0, f"Average tick ingest latency too high: {avg_us:.2f} µs"
