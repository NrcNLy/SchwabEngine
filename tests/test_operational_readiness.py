"""
tests/test_operational_readiness.py
===================================
Workstream 4 (#124): Operational Readiness, Cold Plane & Preflight Test Suite.

Verifies:
1. ControlSignalPayload schema contract, field validation, and clamping.
2. select_cold_plane_model model tier routing by time-of-day.
3. write_control_signal_atomic and read_control_signal atomic persistence, corruption guards, and staleness handling.
4. ColdPlaneWorker heuristic fallback and custom LLM provider execution.
5. AsymmetricHMMGovernor.ingest_cold_plane_signal prior blending and circuit breaker override.
6. PreflightVerifier 5-point invariant checks across settled cash, satellites, OAuth gate, rate limiter, and SQLite WAL.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time as dt_time, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Dict
from zoneinfo import ZoneInfo
import pytest

from core.auth import OAuthTokenManager, SecurityVault
from core.cold_plane_worker import (
    ColdPlaneWorker,
    ControlSignalPayload,
    read_control_signal,
    select_cold_plane_model,
    write_control_signal_atomic,
)
from core.rate_limiter import AsyncTokenBucketRateLimiter
from core.t1_ledger import T1CashLedger
from scripts.tools.check_preflight_invariants import PreflightVerifier
from strategies.regime_governor import AsymmetricHMMGovernor

EDT = ZoneInfo("America/New_York")


# ==============================================================================
# 1. ControlSignalPayload Validation Tests
# ==============================================================================

def test_control_signal_payload_valid_instantiation():
    payload = ControlSignalPayload(
        timestamp_utc="2026-10-26T13:15:00Z",
        model_version="gemini-4-argon",
        dominant_prior="Goldilocks",
        prior_probabilities={"goldilocks": 0.70, "uncertainty": 0.20, "macro_shock": 0.10},
        credit_spread_z_score=0.25,
        nlp_sentiment_score=0.55,
        satellite_target_weight=0.25,
        circuit_breaker_override=False,
    )
    assert payload.model_version == "gemini-4-argon"
    assert payload.dominant_prior == "Goldilocks"
    assert payload.prior_probabilities["goldilocks"] == 0.70
    assert payload.nlp_sentiment_score == 0.55
    assert payload.satellite_target_weight == 0.25
    assert not payload.circuit_breaker_override

    # Round-trip serialization
    d = payload.to_dict()
    reconstructed = ControlSignalPayload.from_dict(d)
    assert reconstructed == payload


def test_control_signal_payload_invalid_probabilities_sum():
    with pytest.raises(ValueError, match="deviates from 1.0"):
        ControlSignalPayload(
            timestamp_utc="2026-10-26T13:15:00Z",
            model_version="gemini-3.8-flash",
            dominant_prior="Uncertainty",
            prior_probabilities={"goldilocks": 0.50, "uncertainty": 0.20, "macro_shock": 0.10},  # Sum = 0.80
            credit_spread_z_score=0.0,
            nlp_sentiment_score=0.0,
            satellite_target_weight=0.15,
            circuit_breaker_override=False,
        )


def test_control_signal_payload_invalid_probabilities_keys():
    with pytest.raises(ValueError, match="must contain exactly keys"):
        ControlSignalPayload(
            timestamp_utc="2026-10-26T13:15:00Z",
            model_version="gemini-3.8-flash",
            dominant_prior="Uncertainty",
            prior_probabilities={"goldilocks": 0.50, "uncertainty": 0.50, "extra": 0.0},
            credit_spread_z_score=0.0,
            nlp_sentiment_score=0.0,
            satellite_target_weight=0.15,
            circuit_breaker_override=False,
        )


def test_control_signal_payload_invalid_model_and_regime():
    # Invalid model
    with pytest.raises(ValueError, match="Invalid model_version"):
        ControlSignalPayload(
            timestamp_utc="2026-10-26T13:15:00Z",
            model_version="gpt-4o",
            dominant_prior="Goldilocks",
            prior_probabilities={"goldilocks": 0.70, "uncertainty": 0.20, "macro_shock": 0.10},
            credit_spread_z_score=0.0,
            nlp_sentiment_score=0.0,
            satellite_target_weight=0.15,
            circuit_breaker_override=False,
        )

    # Invalid regime
    with pytest.raises(ValueError, match="Invalid dominant_prior"):
        ControlSignalPayload(
            timestamp_utc="2026-10-26T13:15:00Z",
            model_version="gemini-4-argon",
            dominant_prior="Hyperinflation",
            prior_probabilities={"goldilocks": 0.70, "uncertainty": 0.20, "macro_shock": 0.10},
            credit_spread_z_score=0.0,
            nlp_sentiment_score=0.0,
            satellite_target_weight=0.15,
            circuit_breaker_override=False,
        )


def test_control_signal_payload_clamping():
    payload = ControlSignalPayload(
        timestamp_utc="2026-10-26T13:15:00Z",
        model_version="gemini-4-argon",
        dominant_prior="Goldilocks",
        prior_probabilities={"goldilocks": 1.0, "uncertainty": 0.0, "macro_shock": 0.0},
        credit_spread_z_score=0.0,
        nlp_sentiment_score=1.85,  # Exceeds +1.0
        satellite_target_weight=0.60,  # Exceeds 0.35
        circuit_breaker_override=False,
    )
    assert payload.nlp_sentiment_score == 1.0
    assert payload.satellite_target_weight == 0.35

    payload_low = ControlSignalPayload(
        timestamp_utc="2026-10-26T13:15:00Z",
        model_version="gemini-4-argon",
        dominant_prior="Macro Shock",
        prior_probabilities={"goldilocks": 0.0, "uncertainty": 0.0, "macro_shock": 1.0},
        credit_spread_z_score=3.5,
        nlp_sentiment_score=-2.5,  # Below -1.0
        satellite_target_weight=-0.10,  # Below 0.0
        circuit_breaker_override=True,
    )
    assert payload_low.nlp_sentiment_score == -1.0
    assert payload_low.satellite_target_weight == 0.0


# ==============================================================================
# 2. Model Tier Router Tests
# ==============================================================================

def test_select_cold_plane_model_routing():
    # 09:15 EDT -> Pre-Market Gate -> gemini-4-argon
    dt_gate = datetime(2026, 10, 26, 9, 15, 0, tzinfo=EDT)
    assert select_cold_plane_model(dt_gate) == "gemini-4-argon"

    # 09:10 EDT boundary -> gemini-4-argon
    dt_gate_start = datetime(2026, 10, 26, 9, 10, 0, tzinfo=EDT)
    assert select_cold_plane_model(dt_gate_start) == "gemini-4-argon"

    # 09:25 EDT boundary -> gemini-4-argon
    dt_gate_end = datetime(2026, 10, 26, 9, 25, 0, tzinfo=EDT)
    assert select_cold_plane_model(dt_gate_end) == "gemini-4-argon"

    # 11:00 EDT intraday -> gemini-3.8-flash
    dt_intraday = datetime(2026, 10, 26, 11, 0, 0, tzinfo=EDT)
    assert select_cold_plane_model(dt_intraday) == "gemini-3.8-flash"

    # 15:55 EDT EOD synthesis -> gemini-4-argon
    dt_eod = datetime(2026, 10, 26, 15, 55, 0, tzinfo=EDT)
    assert select_cold_plane_model(dt_eod) == "gemini-4-argon"

    # 15:58 EDT EOD synthesis -> gemini-4-argon
    dt_eod_late = datetime(2026, 10, 26, 15, 58, 0, tzinfo=EDT)
    assert select_cold_plane_model(dt_eod_late) == "gemini-4-argon"


# ==============================================================================
# 3. Atomic File Drop Persistence & Staleness Guards
# ==============================================================================

def test_write_and_read_control_signal_atomic(tmp_path: Path):
    target_file = tmp_path / "test_signal.json"
    now_utc = datetime.now(timezone.utc)
    ts_str = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    payload = ControlSignalPayload(
        timestamp_utc=ts_str,
        model_version="gemini-4-argon",
        dominant_prior="Goldilocks",
        prior_probabilities={"goldilocks": 0.80, "uncertainty": 0.15, "macro_shock": 0.05},
        credit_spread_z_score=-0.2,
        nlp_sentiment_score=0.45,
        satellite_target_weight=0.30,
        circuit_breaker_override=False,
    )

    written_path = write_control_signal_atomic(payload, target_file)
    assert written_path == target_file
    assert target_file.exists()

    # Read back cleanly
    read_payload = read_control_signal(target_file, max_age_seconds=3600.0, now_utc=now_utc)
    assert read_payload is not None
    assert read_payload.dominant_prior == "Goldilocks"
    assert read_payload.prior_probabilities["goldilocks"] == 0.80


def test_read_control_signal_staleness_rejection(tmp_path: Path):
    target_file = tmp_path / "stale_signal.json"
    stale_time = datetime.now(timezone.utc) - timedelta(seconds=3601.0)
    ts_str = stale_time.strftime("%Y-%m-%dT%H:%M:%SZ")

    payload = ControlSignalPayload(
        timestamp_utc=ts_str,
        model_version="gemini-3.8-flash",
        dominant_prior="Uncertainty",
        prior_probabilities={"goldilocks": 0.20, "uncertainty": 0.60, "macro_shock": 0.20},
        credit_spread_z_score=0.0,
        nlp_sentiment_score=0.0,
        satellite_target_weight=0.15,
        circuit_breaker_override=False,
    )
    write_control_signal_atomic(payload, target_file)

    # Exceeds max_age_seconds=3600.0
    read_payload = read_control_signal(target_file, max_age_seconds=3600.0)
    assert read_payload is None


def test_read_control_signal_missing_and_corrupt(tmp_path: Path):
    missing_file = tmp_path / "non_existent.json"
    assert read_control_signal(missing_file) is None

    corrupt_file = tmp_path / "corrupt.json"
    corrupt_file.write_text("{malformed_json: true, broken...", encoding="utf-8")
    assert read_control_signal(corrupt_file) is None


# ==============================================================================
# 4. ColdPlaneWorker Synthesis Cycle Tests
# ==============================================================================

def test_cold_plane_worker_heuristic_fallback(tmp_path: Path):
    async def _run():
        signal_file = tmp_path / "heuristic_signal.json"
        worker = ColdPlaneWorker(signal_path=signal_file)

        # Scenario: high credit spread stress -> triggers Macro Shock
        context = {
            "credit_spread_z_score": 2.6,
            "qqq_velocity_15m": -0.018,
            "nlp_sentiment_score": -0.4,
        }
        payload = await worker.execute_synthesis_cycle(
            market_context=context,
            now_et=datetime(2026, 10, 26, 9, 15, 0, tzinfo=EDT),
        )
        assert payload.model_version == "gemini-4-argon"
        assert payload.dominant_prior == "Macro Shock"
        assert payload.circuit_breaker_override is True
        assert payload.satellite_target_weight == 0.0
        assert signal_file.exists()

    asyncio.run(_run())


def test_cold_plane_worker_custom_llm_provider(tmp_path: Path):
    async def _run():
        signal_file = tmp_path / "llm_signal.json"

        async def mock_llm_provider(model: str, context: Dict[str, Any]) -> Dict[str, Any]:
            return {
                "timestamp_utc": "2026-10-26T15:00:00Z",
                "model_version": model,
                "dominant_prior": "Goldilocks",
                "prior_probabilities": {"goldilocks": 0.85, "uncertainty": 0.10, "macro_shock": 0.05},
                "credit_spread_z_score": -0.5,
                "nlp_sentiment_score": 0.8,
                "satellite_target_weight": 0.35,
                "circuit_breaker_override": False,
            }

        worker = ColdPlaneWorker(llm_provider=mock_llm_provider, signal_path=signal_file)
        payload = await worker.execute_synthesis_cycle(
            market_context={"dummy": True},
            now_et=datetime(2026, 10, 26, 11, 0, 0, tzinfo=EDT),
        )
        assert payload.model_version == "gemini-3.8-flash"
        assert payload.dominant_prior == "Goldilocks"
        assert payload.satellite_target_weight == 0.35
        assert not payload.circuit_breaker_override

    asyncio.run(_run())


# ==============================================================================
# 5. Hot-Loop Ingestion & Prior Blending in AsymmetricHMMGovernor
# ==============================================================================

def test_governor_ingest_cold_plane_signal_blend(tmp_path: Path):
    db_path = tmp_path / "test_journal.db"
    signal_path = tmp_path / "control_signal.json"

    # Start governor in pure Goldilocks
    governor = AsymmetricHMMGovernor(initial_pi=[1.0, 0.0, 0.0], journal_db_path=db_path)
    assert governor.get_dominant_regime() == "Goldilocks"

    # Cold plane publishes an Uncertainty signal
    payload = ControlSignalPayload(
        timestamp_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        model_version="gemini-3.8-flash",
        dominant_prior="Uncertainty",
        prior_probabilities={"goldilocks": 0.10, "uncertainty": 0.80, "macro_shock": 0.10},
        credit_spread_z_score=0.8,
        nlp_sentiment_score=-0.1,
        satellite_target_weight=0.10,
        circuit_breaker_override=False,
    )
    write_control_signal_atomic(payload, signal_path)

    # Ingest with blend_weight = 0.50
    # Expected Goldilocks: 0.5 * 1.0 + 0.5 * 0.10 = 0.55
    # Expected Uncertainty: 0.5 * 0.0 + 0.5 * 0.80 = 0.40
    # Expected Shock: 0.5 * 0.0 + 0.5 * 0.10 = 0.05
    ingested = governor.ingest_cold_plane_signal(signal_path=signal_path, blend_weight=0.50)
    assert ingested is not None
    assert round(governor.pi_goldilocks, 2) == 0.55
    assert round(governor.pi_uncertainty, 2) == 0.40
    assert round(governor.pi_shock, 2) == 0.05


def test_governor_ingest_circuit_breaker_override(tmp_path: Path):
    db_path = tmp_path / "test_journal_cb.db"
    signal_path = tmp_path / "control_signal_cb.json"

    governor = AsymmetricHMMGovernor(initial_pi=[0.9, 0.1, 0.0], journal_db_path=db_path)
    assert governor.calculate_target_satellite_weight() > 0.0

    payload = ControlSignalPayload(
        timestamp_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        model_version="gemini-4-argon",
        dominant_prior="Macro Shock",
        prior_probabilities={"goldilocks": 0.0, "uncertainty": 0.1, "macro_shock": 0.9},
        credit_spread_z_score=3.2,
        nlp_sentiment_score=-0.8,
        satellite_target_weight=0.0,
        circuit_breaker_override=True,
    )
    write_control_signal_atomic(payload, signal_path)

    ingested = governor.ingest_cold_plane_signal(signal_path=signal_path)
    assert ingested is not None
    assert governor.tail_risk_override_active is True
    assert governor.pi == [0.0, 0.0, 1.0]
    assert governor.get_dominant_regime() == "Macro Shock"
    assert governor.calculate_target_satellite_weight() == 0.0

    # Verify audit telemetry logged to SQLite
    transitions = governor.fetch_recent_transitions(limit=5)
    assert len(transitions) >= 1
    assert transitions[0]["new_regime"] == "Macro Shock"
    assert transitions[0]["tail_risk_override"] == 1


# ==============================================================================
# 6. PreflightVerifier Invariant Tests
# ==============================================================================

def test_preflight_verifier_all_checks_pass(tmp_path: Path):
    async def _run():
        report_file = tmp_path / "preflight_report.json"
        journal_file = tmp_path / "journal.db"
        vault_file = tmp_path / "schwab_tokens_vault.json"

        # Valid test vault with unexpired refresh token
        passphrase = "test-passphrase-32bytes-padding"
        vault = SecurityVault(passphrase=passphrase, iterations=1000, vault_path=vault_file)
        vault.save({
            "access_token": "valid_access",
            "refresh_token": "valid_refresh",
            "refresh_token_issued_at": datetime.now(timezone.utc).timestamp(),
            "persisted_at": datetime.now(timezone.utc).isoformat(),
            "expires_in": 1800,
        })

        ledger = T1CashLedger(settled_cash=Decimal("4400.00"), unsettled_cash=Decimal("0.00"), reserved_cash=Decimal("0.00"))
        token_manager = OAuthTokenManager(vault=vault, initial_ttl_sec=3600.0)
        rate_limiter = AsyncTokenBucketRateLimiter(burst_capacity=120.0, sustained_rate=1.0, daily_ceiling=3500)
        positions = {"SPY": 10, "QQQ": 5, "SOXL": 0, "TQQQ": 0, "TNA": 0}

        verifier = PreflightVerifier(
            ledger=ledger,
            token_manager=token_manager,
            rate_limiter=rate_limiter,
            positions=positions,
            journal_db_path=journal_file,
            report_path=report_file,
        )

        import os
        orig_pass = os.environ.get("VAULT_PASSPHRASE")
        os.environ["VAULT_PASSPHRASE"] = passphrase
        try:
            report = await verifier.run_all_checks(dry_run=True, baseline_nlv=4400.0)
            assert report["overall_passed"] is True
            assert all(chk["passed"] for chk in report["checks"].values())
            assert report_file.exists()
        finally:
            if orig_pass is not None:
                os.environ["VAULT_PASSPHRASE"] = orig_pass
            else:
                os.environ.pop("VAULT_PASSPHRASE", None)

    asyncio.run(_run())


def test_preflight_verifier_fails_on_unsettled_cash(tmp_path: Path):
    async def _run():
        report_file = tmp_path / "preflight_fail_unsettled.json"
        journal_file = tmp_path / "journal.db"

        # Unsettled cash > 0 (T+1 rollover did not clear)
        ledger = T1CashLedger(settled_cash=Decimal("3000.00"), unsettled_cash=Decimal("1400.00"), reserved_cash=Decimal("0.00"))
        verifier = PreflightVerifier(
            ledger=ledger,
            journal_db_path=journal_file,
            report_path=report_file,
        )

        report = await verifier.run_all_checks(dry_run=True)
        assert report["overall_passed"] is False
        assert report["checks"]["t1_ledger_invariants"]["passed"] is False
        assert "Unsettled cash" in report["checks"]["t1_ledger_invariants"]["detail"]

    asyncio.run(_run())


def test_preflight_verifier_fails_on_stranded_satellites(tmp_path: Path):
    async def _run():
        report_file = tmp_path / "preflight_fail_satellites.json"
        journal_file = tmp_path / "journal.db"

        # Stranded SOXL and TQQQ positions overnight
        positions = {"SPY": 10, "SOXL": 50, "TQQQ": 20, "TNA": 0}
        verifier = PreflightVerifier(
            positions=positions,
            journal_db_path=journal_file,
            report_path=report_file,
        )

        report = await verifier.run_all_checks(dry_run=True)
        assert report["overall_passed"] is False
        assert report["checks"]["zero_stranded_satellites"]["passed"] is False
        assert "Stranded overnight" in report["checks"]["zero_stranded_satellites"]["detail"]

    asyncio.run(_run())


def test_preflight_verifier_fails_on_locked_oauth(tmp_path: Path):
    async def _run():
        report_file = tmp_path / "preflight_fail_oauth.json"
        journal_file = tmp_path / "journal.db"

        token_manager = OAuthTokenManager()
        token_manager.auth_circuit_open = True
        token_manager.state = "AUTH_LOCKED"

        verifier = PreflightVerifier(
            token_manager=token_manager,
            journal_db_path=journal_file,
            report_path=report_file,
        )

        report = await verifier.run_all_checks(dry_run=True)
        assert report["overall_passed"] is False
        assert report["checks"]["oauth_pre_market_gate"]["passed"] is False
        assert "AUTH_LOCKED" in report["checks"]["oauth_pre_market_gate"]["detail"]

    asyncio.run(_run())


def test_preflight_verifier_fails_closed_on_expired_disk_token(tmp_path: Path):
    """
    Workstream (#127): Invariant check must fail closed if token file on disk has expired refresh token,
    even when dry-run is specified.
    """
    async def _run():
        report_file = tmp_path / "preflight_fail_expired_disk.json"
        journal_file = tmp_path / "journal.db"
        vault_file = tmp_path / "schwab_tokens_vault.json"

        # Create expired vault
        passphrase = "test-passphrase-32bytes-padding"
        vault = SecurityVault(passphrase=passphrase, iterations=1000, vault_path=vault_file)
        vault.save({
            "access_token": "expired_access",
            "refresh_token": "expired_refresh",
            "refresh_token_issued_at": 1000000000.0,  # Expired timestamp
            "persisted_at": "2020-01-01T00:00:00Z",
            "expires_in": 1800,
        })

        tm = OAuthTokenManager(vault=vault)

        verifier = PreflightVerifier(
            token_manager=tm,
            journal_db_path=journal_file,
            report_path=report_file,
        )

        # Set environment for passphrase and run
        import os
        orig_pass = os.environ.get("VAULT_PASSPHRASE")
        os.environ["VAULT_PASSPHRASE"] = passphrase
        try:
            report = await verifier.run_all_checks(dry_run=True)
            assert report["overall_passed"] is False
            assert report["checks"]["oauth_pre_market_gate"]["passed"] is False
            assert "Refresh token has expired" in report["checks"]["oauth_pre_market_gate"]["detail"]
        finally:
            if orig_pass is not None:
                os.environ["VAULT_PASSPHRASE"] = orig_pass
            else:
                os.environ.pop("VAULT_PASSPHRASE", None)

    asyncio.run(_run())


def test_preflight_verifier_fails_on_cli_nlv_discrepancy(tmp_path: Path):
    """
    Workstream (#127): Any discrepancy between CLI --nlv and configured baseline fails the gate.
    """
    async def _run():
        report_file = tmp_path / "preflight_fail_nlv.json"
        journal_file = tmp_path / "journal.db"

        verifier = PreflightVerifier(
            journal_db_path=journal_file,
            report_path=report_file,
        )

        # Discrepancy: pass 4400.00 while configured baseline is 4400.90
        report = await verifier.run_all_checks(dry_run=True, baseline_nlv=Decimal("4400.90"), cli_nlv=Decimal("4400.00"))
        assert report["overall_passed"] is False
        assert report["checks"]["t1_ledger_invariants"]["passed"] is False
        assert "CLI NLV discrepancy" in report["checks"]["t1_ledger_invariants"]["detail"]

    asyncio.run(_run())
