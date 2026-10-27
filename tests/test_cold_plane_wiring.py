"""
tests/test_cold_plane_wiring.py
===============================
Workstream Remediation (#130):
1. Disambiguated Control Signal Namespaces (state/ipc_control.json vs state/macro_prior.json).
2. Cold Plane Ingestion into ShadowEngine (ShadowRebalancer & AsymmetricHMMGovernor).
3. Dynamic Session Telemetry Log Rotation across ET calendar days.
4. ColdPlaneWorker module entrypoint execution.
"""

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from core.cold_plane_worker import (
    ColdPlaneWorker,
    ControlSignalPayload,
    read_control_signal,
    run_cold_plane_cycle,
    write_control_signal_atomic,
)
from core.ipc import (
    acknowledge_control_signal,
    read_control_signal as read_ipc_signal,
    write_control_signal as write_ipc_signal,
)
from core.paths import IPC_CONTROL_FILE, MACRO_PRIOR_FILE
from core.shadow import (
    ShadowRebalancer,
    ShadowTelemetrySink,
    session_telemetry_path,
)
from data.multi_rate_sync import MultiRateStateSynchronizer
from execution.risk_manager import MetaGovernorHMM
from strategies.regime_governor import AsymmetricHMMGovernor

EDT = ZoneInfo("America/New_York")


def test_control_signal_namespace_disambiguation(tmp_path: Path):
    """Trader IPC must target ipc_control.json while Cold Plane targets macro_prior.json."""
    ipc_file = tmp_path / "ipc_control.json"
    macro_file = tmp_path / "macro_prior.json"

    # 1. Write Trader IPC signal
    ipc_payload = write_ipc_signal("RESUME", reason="Unit test", file_path=ipc_file)
    assert ipc_payload["action"] == "RESUME"
    assert ipc_file.exists()

    read_ipc = read_ipc_signal(ipc_file)
    assert read_ipc is not None
    assert read_ipc["action"] == "RESUME"
    assert read_ipc["acknowledged_by_trader"] is False

    ack_ipc = acknowledge_control_signal(ipc_file)
    assert ack_ipc["acknowledged_by_trader"] is True

    # 2. Write Cold Plane Macro Prior signal
    now_utc_str = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
    macro_payload = ControlSignalPayload(
        timestamp_utc=now_utc_str,
        model_version="gemini-4-argon",
        dominant_prior="Goldilocks",
        prior_probabilities={"goldilocks": 0.80, "uncertainty": 0.15, "macro_shock": 0.05},
        credit_spread_z_score=-0.4,
        nlp_sentiment_score=0.3,
        satellite_target_weight=0.25,
        circuit_breaker_override=False,
    )
    write_control_signal_atomic(macro_payload, target_path=macro_file)
    assert macro_file.exists()

    read_macro = read_control_signal(target_path=macro_file, max_age_seconds=3600.0)
    assert read_macro is not None
    assert read_macro.dominant_prior == "Goldilocks"
    assert read_macro.model_version == "gemini-4-argon"

    # Verify no collision between schemas
    assert "action" not in macro_payload.to_dict()
    assert "model_version" not in ipc_payload


def test_dynamic_session_log_rotation(tmp_path: Path):
    """ShadowTelemetrySink rolls files dynamically across ET calendar date changes."""
    project_root = tmp_path / "test_project"
    project_root.mkdir()

    # Pass the project root into ShadowTelemetrySink
    sink = ShadowTelemetrySink(project_root)

    # Day 1: October 25
    dt_day1 = datetime(2026, 10, 25, 23, 59, 50, tzinfo=EDT)
    expected_path_day1 = session_telemetry_path(project_root, now=dt_day1)
    assert expected_path_day1.name == "session_telemetry_261025.jsonl"

    # Day 2: October 26
    dt_day2 = datetime(2026, 10, 26, 0, 0, 10, tzinfo=EDT)
    expected_path_day2 = session_telemetry_path(project_root, now=dt_day2)
    assert expected_path_day2.name == "session_telemetry_261026.jsonl"

    assert expected_path_day1 != expected_path_day2


def test_shadow_rebalancer_cold_plane_wiring_blend_and_override(tmp_path: Path):
    """
    ShadowRebalancer must ingest macro_prior.json via AsymmetricHMMGovernor,
    shift target weights, adjust NTZ bands, and trigger REBALANCE_OUTSIDE_NTZ on shock.
    """
    signal_file = tmp_path / "macro_prior.json"
    journal_file = tmp_path / "trading_journal.db"
    log_file = tmp_path / "session_telemetry_261026.jsonl"

    sync = MultiRateStateSynchronizer()
    sink = ShadowTelemetrySink(log_file)
    hmm = MetaGovernorHMM(initial_pi=[1.0, 0.0, 0.0])
    governor = AsymmetricHMMGovernor(initial_pi=[1.0, 0.0, 0.0], journal_db_path=journal_file)

    targets = {
        "regime_targets": {
            "Goldilocks": {"target": 0.25, "ntz_band": [0.21, 0.29]},
            "Uncertainty": {"target": 0.10, "ntz_band": [0.08, 0.12]},
            "Shock": {"target": 0.00, "ntz_band": [0.00, 0.00]},
        },
        "routing_vector": {"SPY": 0.4, "QQQ": 0.3, "IWM": 0.1, "HYG": 0.1, "IEF": 0.1},
        "tau_band": 0.03,
    }

    rebalancer = ShadowRebalancer(
        sync=sync,
        sink=sink,
        hmm=hmm,
        targets=targets,
        governor=governor,
        macro_prior_path=signal_file,
    )

    # Seed mock quotes into synchronizer
    quotes = {
        "SPY": 500.0, "QQQ": 400.0, "IWM": 200.0, "HYG": 75.0, "IEF": 90.0,
        "SOXL": 50.0, "TQQQ": 60.0, "TNA": 40.0,
    }
    for sym, px in quotes.items():
        sync.on_level1_quote({
            "symbol": sym,
            "quote": {"askPrice": px, "bidPrice": px, "lastPrice": px, "totalVolume": 1000},
        })

    # Hot boot synchronizer with $10,000 cash
    sync.on_account_sync({
        "securitiesAccount": {
            "currentBalances": {"liquidationValue": 10000.0, "cashAvailableForTrading": 10000.0},
            "positions": [],
        }
    })

    # Cycle 1: No cold signal yet -> INITIAL_DEPLOYMENT to Goldilocks target 0.25 (evaluated midday 11:00 EDT)
    midday = datetime(2026, 10, 26, 11, 0, 0, tzinfo=EDT)
    summary1 = rebalancer.evaluate(now=midday)
    assert summary1["decision"] == "INITIAL_DEPLOYMENT"
    assert summary1["dominant_regime"] == "Goldilocks"
    assert summary1["target_satellite_weight"] == 0.25
    assert summary1["ntz_band"] == [0.21, 0.29]

    # Cycle 2: Same state inside NTZ -> HOLD_INSIDE_NTZ
    summary2 = rebalancer.evaluate(now=midday)
    assert summary2["decision"] == "HOLD_INSIDE_NTZ"

    # Step 3: Publish Cold Plane Macro Shock with circuit breaker override
    now_str = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
    shock_payload = ControlSignalPayload(
        timestamp_utc=now_str,
        model_version="gemini-4-argon",
        dominant_prior="Macro Shock",
        prior_probabilities={"goldilocks": 0.0, "uncertainty": 0.05, "macro_shock": 0.95},
        credit_spread_z_score=3.2,
        nlp_sentiment_score=-0.75,
        satellite_target_weight=0.0,
        circuit_breaker_override=True,
    )
    write_control_signal_atomic(shock_payload, signal_file)

    # Cycle 3: Ingests cold plane signal -> triggers emergency de-risking
    summary3 = rebalancer.evaluate(now=midday)
    assert summary3["dominant_regime"] == "Shock"
    assert summary3["target_satellite_weight"] == 0.0
    assert summary3["ntz_band"] == [0.0, 0.0]
    assert summary3["decision"] == "REBALANCE_OUTSIDE_NTZ"
    assert summary3["cold_plane_prior"] == "Macro Shock"
    assert summary3["cold_plane_cb_override"] is True


def test_cold_plane_worker_entrypoint(tmp_path: Path):
    """Test execution of run_cold_plane_cycle."""
    signal_file = tmp_path / "macro_prior.json"
    payload = asyncio.run(run_cold_plane_cycle(signal_path=signal_file))
    assert payload is not None
    assert payload.dominant_prior in ("Goldilocks", "Uncertainty", "Macro Shock")
    assert signal_file.exists()
