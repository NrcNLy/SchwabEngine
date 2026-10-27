"""
tests/test_shadow.py
====================
Unit tests for shadow execution runtime (core/shadow.py).
Enforces EOD Satellite Liquidation Parity (#132):
- Evaluates 15:50:00 EDT mandatory liquidation gate for satellite positions (SOXL, TQQQ, TNA).
- Verifies full liquidation to 0 shares, proceeds credited to settled cash, and telemetry emission.
- Verifies post-15:50 hold invariants preventing re-entry into leveraged satellites.
"""

from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo
import pytest

from core.shadow import (
    ShadowRebalancer,
    ShadowTelemetrySink,
    session_telemetry_path,
)
from data.multi_rate_sync import DEFAULT_SATELLITE_SYMBOLS, MultiRateStateSynchronizer
from execution.risk_manager import MetaGovernorHMM
from strategies.regime_governor import AsymmetricHMMGovernor

EDT = ZoneInfo("America/New_York")

MOCK_TARGETS = {
    "regime_targets": {
        "Goldilocks": {"target": 0.25, "ntz_band": [0.21, 0.29]},
        "Uncertainty": {"target": 0.10, "ntz_band": [0.08, 0.12]},
        "Shock": {"target": 0.00, "ntz_band": [0.00, 0.00]},
    },
    "routing_vector": {"SPY": 0.4, "QQQ": 0.3, "IWM": 0.1, "HYG": 0.1, "IEF": 0.1},
    "tau_band": 0.03,
}

MOCK_QUOTES = {
    "SPY": 500.0,
    "QQQ": 400.0,
    "IWM": 200.0,
    "HYG": 75.0,
    "IEF": 90.0,
    "SOXL": 50.0,
    "TQQQ": 60.0,
    "TNA": 40.0,
}


def _seed_quotes(sync: MultiRateStateSynchronizer) -> None:
    for sym, px in MOCK_QUOTES.items():
        sync.on_level1_quote({
            "symbol": sym,
            "quote": {"askPrice": px, "bidPrice": px, "lastPrice": px, "totalVolume": 1000},
        })


def test_shadow_eod_mandatory_satellite_liquidation(tmp_path: Path):
    """
    Enforce EOD Satellite Liquidation Parity (#132):
    An evaluation at 15:51 EDT forces total satellite liquidation to 0 shares
    and returns cash to baseline.
    """
    log_file = tmp_path / "session_telemetry_261026.jsonl"
    sync = MultiRateStateSynchronizer()
    sink = ShadowTelemetrySink(log_file)
    hmm = MetaGovernorHMM(initial_pi=[1.0, 0.0, 0.0])
    governor = AsymmetricHMMGovernor(initial_pi=[1.0, 0.0, 0.0])

    rebalancer = ShadowRebalancer(
        sync=sync,
        sink=sink,
        hmm=hmm,
        targets=MOCK_TARGETS,
        governor=governor,
    )
    _seed_quotes(sync)

    # Initial portfolio state: $5,000 cash + $5,000 in satellites = $10,000 NLV baseline
    sync.on_account_sync({
        "securitiesAccount": {
            "currentBalances": {"liquidationValue": 10000.0, "cashAvailableForTrading": 5000.0},
            "positions": [
                {"instrument": {"symbol": "SOXL"}, "longQuantity": 50.0, "averagePrice": 50.0, "marketValue": 2500.0},
                {"instrument": {"symbol": "TQQQ"}, "longQuantity": 25.0, "averagePrice": 60.0, "marketValue": 1500.0},
                {"instrument": {"symbol": "TNA"}, "longQuantity": 25.0, "averagePrice": 40.0, "marketValue": 1000.0},
            ],
        }
    })

    # Evaluate at 15:51 EDT (after the 15:50 mandatory gate)
    now_eod = datetime(2026, 10, 26, 15, 51, 0, tzinfo=EDT)
    summary = rebalancer.evaluate(now=now_eod)

    # 1. Assert decision and targets
    assert summary["decision"] == "EOD_MANDATORY_LIQUIDATION"
    assert summary["target_satellite_weight"] == 0.0
    assert summary["post_satellite_weight"] == 0.0
    assert summary["orders"] == 3

    # 2. Assert cash returned to baseline ($10,000)
    assert summary["post_cash"] == 10000.0
    assert rebalancer._cash == 10000.0
    live_state = sync.get_live_portfolio_state()
    assert float(live_state.cash_buffer) == 10000.0
    assert float(live_state.satellite_weight) == 0.0

    # 3. Assert total satellite shares forced to 0
    for sym in DEFAULT_SATELLITE_SYMBOLS:
        assert sym not in rebalancer._book or rebalancer._book[sym][0] == 0

    for sym in ("SOXL", "TQQQ", "TNA"):
        pos = live_state.positions.get(sym)
        assert pos is None or pos.quantity == 0

    # 4. Verify orders emitted into the telemetry sink
    assert sink.path.exists()
    lines = [json.loads(line) for line in sink.path.read_text(encoding="utf-8").strip().splitlines()]
    order_events = [ev for ev in lines if ev["event"] == "SHADOW_REBALANCE_ORDER"]
    assert len(order_events) == 3
    sold_symbols = {o["symbol"]: (o["shares"], o["side"], o["sleeve"]) for o in order_events}
    assert sold_symbols["SOXL"] == (50, "SELL", "SATELLITE")
    assert sold_symbols["TQQQ"] == (25, "SELL", "SATELLITE")
    assert sold_symbols["TNA"] == (25, "SELL", "SATELLITE")


def test_shadow_eod_gate_temporal_boundary(tmp_path: Path):
    """
    Verifies that the gate activates precisely at 15:50:00 EDT and does not trigger before.
    Subsequent cycles after liquidation hold inside NTZ with target 0.0.
    """
    log_file = tmp_path / "session_telemetry_261026.jsonl"
    sync = MultiRateStateSynchronizer()
    sink = ShadowTelemetrySink(log_file)
    hmm = MetaGovernorHMM(initial_pi=[1.0, 0.0, 0.0])
    governor = AsymmetricHMMGovernor(initial_pi=[1.0, 0.0, 0.0])

    rebalancer = ShadowRebalancer(
        sync=sync,
        sink=sink,
        hmm=hmm,
        targets=MOCK_TARGETS,
        governor=governor,
    )
    _seed_quotes(sync)

    sync.on_account_sync({
        "securitiesAccount": {
            "currentBalances": {"liquidationValue": 10000.0, "cashAvailableForTrading": 5000.0},
            "positions": [
                {"instrument": {"symbol": "SOXL"}, "longQuantity": 50.0, "averagePrice": 50.0, "marketValue": 2500.0},
            ],
        }
    })

    # 1. 15:49:59 EDT (1 second prior to gate): Must NOT trigger mandatory liquidation
    now_pre = datetime(2026, 10, 26, 15, 49, 59, tzinfo=EDT)
    summary_pre = rebalancer.evaluate(now=now_pre)
    assert summary_pre["decision"] != "EOD_MANDATORY_LIQUIDATION"
    assert rebalancer._book["SOXL"][0] > 0
    held_before = rebalancer._book["SOXL"][0]

    # 2. 15:50:00 EDT (exact gate onset): Must trigger EOD_MANDATORY_LIQUIDATION
    now_at = datetime(2026, 10, 26, 15, 50, 0, tzinfo=EDT)
    summary_at = rebalancer.evaluate(now=now_at)
    assert summary_at["decision"] == "EOD_MANDATORY_LIQUIDATION"
    assert summary_at["orders"] == 3  # All 3 satellites deployed in step 1 are liquidated
    assert summary_at["target_satellite_weight"] == 0.0
    assert summary_at["post_satellite_weight"] == 0.0
    for sym in DEFAULT_SATELLITE_SYMBOLS:
        assert sym not in rebalancer._book

    # 3. 15:52:00 EDT (post-liquidation subsequent cycle):
    # Core held, satellites 0 -> hold inside NTZ, do not enter satellites
    now_post = datetime(2026, 10, 26, 15, 52, 0, tzinfo=EDT)
    summary_post = rebalancer.evaluate(now=now_post)
    assert summary_post["decision"] == "HOLD_INSIDE_NTZ"
    assert summary_post["target_satellite_weight"] == 0.0
    assert summary_post["orders"] == 0


def test_shadow_eod_gate_mixed_core_and_satellite(tmp_path: Path):
    """
    Verifies that EOD liquidation flattens only satellite positions, leaves core positions intact,
    and returns satellite proceeds to cash.
    """
    log_file = tmp_path / "session_telemetry_261026.jsonl"
    sync = MultiRateStateSynchronizer()
    sink = ShadowTelemetrySink(log_file)
    hmm = MetaGovernorHMM(initial_pi=[1.0, 0.0, 0.0])
    governor = AsymmetricHMMGovernor(initial_pi=[1.0, 0.0, 0.0])

    rebalancer = ShadowRebalancer(
        sync=sync,
        sink=sink,
        hmm=hmm,
        targets=MOCK_TARGETS,
        governor=governor,
    )
    _seed_quotes(sync)

    # Core: 10 SPY ($5,000)
    # Satellite: 50 SOXL ($2,500)
    # Cash: $2,500
    # NLV: $10,000
    sync.on_account_sync({
        "securitiesAccount": {
            "currentBalances": {"liquidationValue": 10000.0, "cashAvailableForTrading": 2500.0},
            "positions": [
                {"instrument": {"symbol": "SPY"}, "longQuantity": 10.0, "averagePrice": 500.0, "marketValue": 5000.0},
                {"instrument": {"symbol": "SOXL"}, "longQuantity": 50.0, "averagePrice": 50.0, "marketValue": 2500.0},
            ],
        }
    })

    now_eod = datetime(2026, 10, 26, 15, 51, 0, tzinfo=EDT)
    summary = rebalancer.evaluate(now=now_eod)

    assert summary["decision"] == "EOD_MANDATORY_LIQUIDATION"
    assert summary["orders"] == 1
    # SPY is retained (10 shares @ 500 = $5,000)
    assert rebalancer._book.get("SPY") == (10, 500.0)
    # SOXL is liquidated
    assert "SOXL" not in rebalancer._book
    # Cash: $2,500 + $2,500 (SOXL proceeds) = $5,000
    assert rebalancer._cash == 5000.0
    assert summary["post_cash"] == 5000.0
    assert summary["post_satellite_weight"] == 0.0
