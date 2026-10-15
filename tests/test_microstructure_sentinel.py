"""
tests/test_microstructure_sentinel.py
======================================
Unit tests for Synthetic Microstructure Sentinel & Opening Cross Buffer (#54).
"""

from __future__ import annotations

import pytest
from execution.microstructure_sentinel import MicrostructureSentinel, SentinelVerdict


def test_opening_liquidity_sweep_suppressed():
    """
    Test 1: Opening liquidity sweep (price drops to $99.50 at 09:30:05,
    recovers to $100.50 at 09:30:45) -> Verify exit is SUPPRESSED.
    """
    # s0_reference = 103.60 -> soft_floor = 103.60 * (1 - 0.037) = 99.7668 (~$99.77)
    sentinel = MicrostructureSentinel(
        symbol="SOXL",
        s0_reference=103.60,
        soft_floor_pct=0.037,
        hard_catastrophic_pct=0.060,
        opening_buffer_seconds=90,
        vwap_window_seconds=30,
    )

    # 1. 09:30:05 EDT: Opening cross liquidity sweep drops to $99.50
    verdict1 = sentinel.evaluate_stop(
        current_time_edt="09:30:05",
        current_price=99.50,
        current_volume=5000,
    )
    assert not verdict1.should_exit, "Opening noise should NOT trigger exit"
    assert verdict1.reason == "OPENING_AUCTION_NOISE"
    assert verdict1.exit_type == "NONE"
    assert verdict1.current_vwap == pytest.approx(99.50, rel=1e-3)

    # Intermediate ticks during opening auction
    verdict_mid = sentinel.evaluate_stop(
        current_time_edt="09:30:20",
        current_price=99.65,
        current_volume=3000,
    )
    assert not verdict_mid.should_exit
    assert verdict_mid.reason == "OPENING_AUCTION_NOISE"

    # 2. 09:30:45 EDT: Price recovers above soft floor to $100.50
    verdict2 = sentinel.evaluate_stop(
        current_time_edt="09:30:45",
        current_price=100.50,
        current_volume=10000,
    )
    assert not verdict2.should_exit, "Recovered price should NOT trigger exit"
    assert verdict2.reason == "HOLD"
    assert verdict2.exit_type == "NONE"


def test_catastrophic_gap_down_emergency_sweep():
    """
    Test 2: Catastrophic gap down (price drops to $93.00 at 09:30:02) ->
    Verify immediate EMERGENCY_HARD_SWEEP triggers.
    """
    # s0_reference = 103.60 -> hard_catastrophic floor = 103.60 * 0.94 = 97.384 (~$97.38)
    sentinel = MicrostructureSentinel(
        symbol="SOXL",
        s0_reference=103.60,
        soft_floor_pct=0.037,
        hard_catastrophic_pct=0.060,
        opening_buffer_seconds=90,
        vwap_window_seconds=30,
    )

    # 09:30:02 EDT: Immediate crash below catastrophic floor to $93.00
    verdict = sentinel.evaluate_stop(
        current_time_edt="09:30:02",
        current_price=93.00,
        current_volume=15000,
    )
    assert verdict.should_exit, "Catastrophic breach must trigger exit immediately"
    assert verdict.reason == "EMERGENCY_HARD_SWEEP"
    assert verdict.exit_type == "EMERGENCY_HARD_SWEEP"


def test_sustained_post_open_breakdown_limit_pegged():
    """
    Test 3: Sustained post-open breakdown (price drops to $99.20 at 09:35:00
    and VWAP holds below floor) -> Verify LIMIT_PEGGED exit triggers.
    """
    sentinel = MicrostructureSentinel(
        symbol="SOXL",
        s0_reference=103.60,
        soft_floor_pct=0.037,
        hard_catastrophic_pct=0.060,
        opening_buffer_seconds=90,
        vwap_window_seconds=30,
    )

    # 09:34:40 to 09:35:00: Series of depressed ticks post-open
    sentinel.evaluate_stop(
        current_time_edt="09:34:40",
        current_price=99.25,
        current_volume=2000,
        bid_price=99.24,
    )
    sentinel.evaluate_stop(
        current_time_edt="09:34:50",
        current_price=99.22,
        current_volume=2500,
        bid_price=99.21,
    )
    verdict = sentinel.evaluate_stop(
        current_time_edt="09:35:00",
        current_price=99.20,
        current_volume=3000,
        bid_price=99.18,
    )

    assert verdict.should_exit, "Post-open sustained breakdown must trigger exit"
    assert verdict.exit_type == "LIMIT_PEGGED"
    assert verdict.reason == "SOFT_FLOOR_BREACH"
    assert verdict.current_vwap < sentinel.soft_floor
    # Pegged limit price should be bid - $0.02
    assert verdict.pegged_limit_price == pytest.approx(99.16, abs=1e-2)


def test_sustained_depression_in_opening_buffer_triggers_exit():
    """
    Opening buffer allows true breakdowns to exit if depressed for >= 45s continuously.
    """
    sentinel = MicrostructureSentinel(
        symbol="SOXL",
        s0_reference=103.60,
        soft_floor_pct=0.037,
        hard_catastrophic_pct=0.060,
    )

    # 09:30:05: drops below soft floor (99.77)
    v1 = sentinel.evaluate_stop("09:30:05", 99.40, 1000)
    assert not v1.should_exit
    assert v1.reason == "OPENING_AUCTION_NOISE"

    # 09:30:40 (35 seconds in): still suppressed
    v2 = sentinel.evaluate_stop("09:30:40", 99.30, 1000)
    assert not v2.should_exit
    assert v2.reason == "OPENING_AUCTION_NOISE"

    # 09:30:51 (46 seconds continuously depressed): triggers exit!
    v3 = sentinel.evaluate_stop("09:30:51", 99.25, 1000)
    assert v3.should_exit
    assert v3.reason == "SUSTAINED_SOFT_BREACH"
    assert v3.exit_type == "LIMIT_PEGGED"


def test_consecutive_15s_closes_trigger_post_open():
    """
    Post-open exit can trigger on 2 consecutive 15-second closes < soft floor.
    """
    sentinel = MicrostructureSentinel(
        symbol="SOXL",
        s0_reference=103.60,
        soft_floor_pct=0.037,
        hard_catastrophic_pct=0.060,
    )

    # Bar 1 close at 09:32:00
    sentinel.evaluate_stop("09:32:00", 99.30, 100)
    # Bar 2 close at 09:32:15
    sentinel.evaluate_stop("09:32:15", 99.25, 100)
    # Bar 3 at 09:32:30
    verdict = sentinel.evaluate_stop("09:32:30", 99.20, 100)

    assert verdict.should_exit
    assert verdict.exit_type == "LIMIT_PEGGED"


def test_verdict_unpacking_and_properties():
    """
    Verify SentinelVerdict tuple-like unpacking and properties.
    """
    sentinel = MicrostructureSentinel(symbol="SOXL", s0_reference=100.0)
    verdict = sentinel.evaluate_stop("09:35:00", 101.0, 100)
    should_exit, reason, exit_type, vwap = verdict
    assert not should_exit
    assert reason == "HOLD"
    assert exit_type == "NONE"
    assert vwap == pytest.approx(101.0)
    assert sentinel.get_pegged_limit_price(101.00) == 100.98
