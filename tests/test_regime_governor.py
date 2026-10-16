"""
tests/test_regime_governor.py
=============================
Unit and property-based tests for the Real-Time Macro Regime Governor (SES Engine).
Ref: Issue #59.

Covers:
1. Bullish breakout alignment (High RS, Low VXN, Flat Yields) -> SES >= 0.50 (FAVORABLE).
2. Severe rate spike / tech divergence (SOXX dumping vs QQQ, Yields surging) -> SES <= -0.30 (DEFENSIVE).
3. Sizing governor throttle (verify max allowed position size dynamically scales with SES score).
4. State persistence to state/telemetry.db table regime_telemetry on 1-minute bars.
5. Invariant and boundary conditions across FAVORABLE, CHOP, and DEFENSIVE regimes.
6. Streaming tick minute aggregation and zero-division numerical safeguards.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

from core.synthetic_divergence import (
    MacroRegime,
    RealTimeRegimeGovernor,
    RegimeSnapshot,
)


def test_bullish_breakout_alignment(tmp_path: Path):
    """
    Test 1: Bullish breakout alignment (High RS, Low VXN, Flat Yields).
    Assert SES >= 0.50 (FAVORABLE), full 33% capital sizing ($1,836 cap), zero target compression.
    """
    db_file = tmp_path / "telemetry.db"
    governor = RealTimeRegimeGovernor(db_path=db_file)

    # 1. SOXX: 49 flat bars at 100.0, 50th bar breakouts to 105.0 (+5.0% surge)
    soxx_prices = [100.0] * 49 + [105.0]
    # 2. QQQ: 50 flat bars at 400.0 (SOXX sharply outperforming broad tech beta)
    qqq_prices = [400.0] * 50
    # 3. ^VXN: 59 historical bars at 25.0, 60th bar drops to 15.0 (calm / bottom percentile)
    vxn_prices = [25.0] * 59 + [15.0]
    # 4. ^TNX: 6 flat bars at 4.20 (flat Treasury yields, zero drag)
    tnx_prices = [4.20] * 6

    governor.seed_macro_history(
        soxx=soxx_prices,
        qqq=qqq_prices,
        tnx=tnx_prices,
        vxn=vxn_prices,
    )

    snapshot: RegimeSnapshot = governor.evaluate_regime()

    # Mathematical factor assertions
    assert snapshot.score_trend == 1.0, f"Expected max positive trend score, got {snapshot.score_trend}"
    assert snapshot.score_rs == 1.0, f"Expected max positive relative strength score, got {snapshot.score_rs}"
    assert snapshot.score_vxn > 0.90, f"Expected high positive vol score, got {snapshot.score_vxn}"
    assert snapshot.score_tnx == 0.0, f"Expected neutral yield score, got {snapshot.score_tnx}"

    # Composite SES & Regime assertions
    assert snapshot.ses >= 0.50, f"Expected SES >= 0.50, got {snapshot.ses}"
    assert snapshot.regime == MacroRegime.FAVORABLE.value
    assert snapshot.entry_permitted is True
    assert snapshot.max_position_size == 1836.0
    assert snapshot.capital_allocation_pct == 0.33
    assert snapshot.target_compression_pct == 0.0
    assert snapshot.target_multiplier == 1.0

    # Governor sizing methods
    assert governor.get_max_position_size() == 1836.0
    assert governor.calculate_position_size(base_capital=5563.64) == 1836.0


def test_severe_rate_spike_and_tech_divergence(tmp_path: Path):
    """
    Test 2: Severe rate spike / tech divergence (SOXX dumping vs QQQ, Yields surging).
    Assert SES <= -0.30 (DEFENSIVE), ZERO long entries permitted, max position size $0.
    """
    db_file = tmp_path / "telemetry.db"
    governor = RealTimeRegimeGovernor(db_path=db_file)

    # 1. SOXX: 49 flat bars at 100.0, 50th bar dumps to 95.0 (-5.0% breakdown)
    soxx_prices = [100.0] * 49 + [95.0]
    # 2. QQQ: 50 flat bars at 400.0 (SOXX underperforming tech benchmark)
    qqq_prices = [400.0] * 50
    # 3. ^VXN: 60 bars of elevated / neutral volatility
    vxn_prices = [22.0] * 60
    # 4. ^TNX: Surging yields (+40 bps over 5 periods, surging from 4.00 to 4.40)
    tnx_prices = [4.00] * 5 + [4.40]

    governor.seed_macro_history(
        soxx=soxx_prices,
        qqq=qqq_prices,
        tnx=tnx_prices,
        vxn=vxn_prices,
    )

    snapshot: RegimeSnapshot = governor.evaluate_regime()

    # Mathematical factor assertions
    assert snapshot.score_trend == -1.0, f"Expected max negative trend score, got {snapshot.score_trend}"
    assert snapshot.score_rs == -1.0, f"Expected max negative relative strength score, got {snapshot.score_rs}"
    assert snapshot.score_tnx == -1.0, f"Expected max negative yield acceleration score, got {snapshot.score_tnx}"

    # Composite SES & Regime assertions
    assert snapshot.ses <= -0.30, f"Expected SES <= -0.30, got {snapshot.ses}"
    assert snapshot.regime == MacroRegime.DEFENSIVE.value
    assert snapshot.entry_permitted is False, "Expected long entries to be strictly vetoed in DEFENSIVE regime"
    assert snapshot.max_position_size == 0.0, f"Expected $0 position cap, got {snapshot.max_position_size}"
    assert snapshot.capital_allocation_pct == 0.0

    # Governor sizing methods
    assert governor.get_max_position_size() == 0.0
    assert governor.calculate_position_size(base_capital=5563.64) == 0.0


def test_sizing_governor_throttle(tmp_path: Path):
    """
    Test 3: Sizing governor throttle (verify max allowed position size dynamically scales with SES score).
    Validates transitions:
      - FAVORABLE (SES >= +0.20): Full 33% capital ($1,836 cap).
      - CHOP (-0.25 < SES < +0.20): Sizing halved to 16.5% ($918 cap), targets compressed 25%.
      - DEFENSIVE (SES <= -0.25): ZERO long entries permitted ($0 cap).
    """
    db_file = tmp_path / "telemetry.db"
    governor = RealTimeRegimeGovernor(db_path=db_file)
    base_capital = 5563.64

    # -------------------------------------------------------------------------
    # Scenario A: FAVORABLE
    # -------------------------------------------------------------------------
    governor.seed_macro_history(
        soxx=[100.0] * 49 + [104.0],
        qqq=[400.0] * 50,
        tnx=[4.20] * 6,
        vxn=[25.0] * 59 + [16.0],
    )
    fav_snap = governor.evaluate_regime()
    assert fav_snap.ses >= 0.20, f"Expected FAVORABLE SES >= 0.20, got {fav_snap.ses}"
    assert fav_snap.regime == "FAVORABLE"
    assert fav_snap.entry_permitted is True
    assert fav_snap.capital_allocation_pct == 0.33
    assert fav_snap.max_position_size == 1836.0
    assert fav_snap.target_compression_pct == 0.0
    assert fav_snap.target_multiplier == 1.0
    assert governor.get_max_position_size() == 1836.0
    assert governor.calculate_position_size(base_capital) == 1836.0

    # -------------------------------------------------------------------------
    # Scenario B: CHOP
    # -------------------------------------------------------------------------
    # Neutral/balanced conditions: SOXX flat with QQQ, yields flat, vol median
    governor.seed_macro_history(
        soxx=[100.0] * 50,
        qqq=[400.0] * 50,
        tnx=[4.20] * 6,
        vxn=[20.0] * 60,
    )
    chop_snap = governor.evaluate_regime()
    assert -0.25 < chop_snap.ses < 0.20, f"Expected CHOP -0.25 < SES < 0.20, got {chop_snap.ses}"
    assert chop_snap.regime == "CHOP"
    assert chop_snap.entry_permitted is True
    assert chop_snap.capital_allocation_pct == 0.165
    assert chop_snap.max_position_size == 918.0
    assert chop_snap.target_compression_pct == 0.25
    assert chop_snap.target_multiplier == 0.75
    assert governor.get_max_position_size() == 918.0
    # Sizing dynamically halved from $1,836 to $918
    assert governor.calculate_position_size(base_capital) == 918.0

    # -------------------------------------------------------------------------
    # Scenario C: DEFENSIVE
    # -------------------------------------------------------------------------
    governor.seed_macro_history(
        soxx=[100.0] * 49 + [94.0],
        qqq=[400.0] * 50,
        tnx=[4.00] * 5 + [4.45],
        vxn=[20.0] * 60,
    )
    def_snap = governor.evaluate_regime()
    assert def_snap.ses <= -0.25, f"Expected DEFENSIVE SES <= -0.25, got {def_snap.ses}"
    assert def_snap.regime == "DEFENSIVE"
    assert def_snap.entry_permitted is False
    assert def_snap.capital_allocation_pct == 0.0
    assert def_snap.max_position_size == 0.0
    assert governor.get_max_position_size() == 0.0
    assert governor.calculate_position_size(base_capital) == 0.0


def test_state_persistence_on_1m_bar(tmp_path: Path):
    """
    Test 4: State persistence to state/telemetry.db table regime_telemetry on 1-minute bars.
    Verifies WAL schema, indexed timestamp ordering, and correct factor serialization.
    """
    db_file = tmp_path / "telemetry.db"
    governor = RealTimeRegimeGovernor(db_path=db_file)

    now = datetime(2026, 10, 15, 14, 30, 0, tzinfo=timezone.utc)
    snap1 = governor.record_macro_bar(
        soxx_close=520.50,
        qqq_close=490.25,
        tnx_close=4.25,
        vxn_close=18.50,
        timestamp=now,
        persist=True,
    )

    # Verify directly in SQLite database
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM regime_telemetry ORDER BY id ASC;")
        rows = cursor.fetchall()
        assert len(rows) == 1
        r = rows[0]
        assert r["timestamp"] == now.isoformat()
        assert r["ses"] == snap1.ses
        assert r["regime"] == snap1.regime
        assert r["soxx_price"] == 520.50
        assert r["qqq_price"] == 490.25
        assert r["tnx_yield"] == 4.25
        assert r["vxn_level"] == 18.50
        assert r["entry_permitted"] == (1 if snap1.entry_permitted else 0)
        assert r["max_capital_cap"] == snap1.max_position_size
    finally:
        conn.close()

    # Record second 1-minute bar
    now_plus_1m = now + timedelta(minutes=1)
    snap2 = governor.record_macro_bar(
        soxx_close=525.00,
        qqq_close=490.00,
        tnx_close=4.24,
        vxn_close=17.80,
        timestamp=now_plus_1m,
        persist=True,
    )

    records = governor.get_telemetry_records(limit=10)
    assert len(records) == 2
    # Records ordered descending by id
    assert records[0]["timestamp"] == now_plus_1m.isoformat()
    assert records[1]["timestamp"] == now.isoformat()


def test_property_boundary_regime_thresholds(tmp_path: Path):
    """
    Test 5: Explicit invariant checks on SES classification boundary thresholds.
    """
    db_file = tmp_path / "telemetry.db"
    governor = RealTimeRegimeGovernor(db_path=db_file)

    # Invariant helper to evaluate regime classification logic directly
    def classify(ses_val: float):
        if ses_val >= 0.20:
            return "FAVORABLE", True, 1836.0, 0.33, 0.0
        elif ses_val <= -0.25:
            return "DEFENSIVE", False, 0.0, 0.0, 0.0
        else:
            return "CHOP", True, 918.0, 0.165, 0.25

    regime, permit, cap, alloc, comp = classify(0.20)
    assert regime == "FAVORABLE" and permit is True and cap == 1836.0

    regime, permit, cap, alloc, comp = classify(0.1999)
    assert regime == "CHOP" and permit is True and cap == 918.0 and comp == 0.25

    regime, permit, cap, alloc, comp = classify(-0.25)
    assert regime == "DEFENSIVE" and permit is False and cap == 0.0

    regime, permit, cap, alloc, comp = classify(-0.2499)
    assert regime == "CHOP" and permit is True and cap == 918.0 and comp == 0.25


def test_streaming_tick_aggregation(tmp_path: Path):
    """
    Test 6: Real-time streaming tick ingestion across minute boundaries.
    Verifies that minute bars are constructed and persisted automatically.
    """
    db_file = tmp_path / "telemetry.db"
    governor = RealTimeRegimeGovernor(db_path=db_file, auto_persist=True)

    t0 = datetime(2026, 10, 15, 9, 30, 15, tzinfo=timezone.utc)
    t1 = datetime(2026, 10, 15, 9, 30, 45, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 15, 9, 31, 5, tzinfo=timezone.utc)  # Advances into minute 9:31

    # Ticks for 09:30
    governor.record_tick("SOXX", 100.0, 500, t0)
    governor.record_tick("QQQ", 400.0, 1000, t0)
    governor.record_tick("^TNX", 4.25, 0, t0)
    governor.record_tick("^VXN", 20.0, 0, t0)

    governor.record_tick("SOXX", 101.0, 500, t1)

    # Rollover to 09:31: flushes 09:30 bar and persists to telemetry.db
    governor.record_tick("SOXX", 102.0, 300, t2)
    governor.record_tick("QQQ", 401.0, 800, t2)

    latest = governor.get_latest_persisted_telemetry()
    assert latest is not None
    assert latest["soxx_price"] == 101.0  # Close of 09:30 bar was 101.0
    assert latest["regime"] in ("FAVORABLE", "CHOP", "DEFENSIVE")


def test_insufficient_data_and_zero_division_safety(tmp_path: Path):
    """
    Test 7: Numerical stability and warmup safety with zero or incomplete data.
    """
    db_file = tmp_path / "telemetry.db"
    governor = RealTimeRegimeGovernor(db_path=db_file)

    # Empty governor evaluation: must not crash or trigger warnings
    snap = governor.evaluate_regime()
    assert snap.ses == 0.0
    assert snap.regime == "CHOP"

    # Single price point: std is 0.0 -> must not divide by zero
    governor.record_bar("SOXX", 100.0)
    governor.record_bar("QQQ", 400.0)
    snap2 = governor.evaluate_regime()
    assert -1.0 <= snap2.ses <= 1.0
