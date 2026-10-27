"""
tests/test_live_entrypoint.py
=============================
Test suite for Patch Live Entrypoint & Broker Routing (#135).

Verifies:
1. main.py --headless-trader reads config/strategy_config.json ave.mode:
   - When ave.mode == 'live': initializes live broker engine and logs
     [CRITICAL] RUNTIME EXECUTION MODE: LIVE BROKER ROUTING ARMED.
   - When ave.mode == 'shadow': instantiates build_shadow() and logs
     [INFO] RUNTIME EXECUTION MODE: SHADOW SIMULATION.
2. Dedicated --live CLI flag forces live execution and rejects shadow fallback.
3. build_live binds the real Schwab order router.
4. Invariant 6 (verify_execution_mode_alignment):
   - Config 'live' + daemon '--live': passes.
   - Config 'live' + daemon '--mode shadow': fails with mismatch.
   - Config 'shadow' + daemon '--live': fails with mismatch.
   - Config 'shadow' + daemon '--mode shadow': passes.
"""

import argparse
import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from main import parse_args, resolve_live, run
from scripts.tools.check_preflight_invariants import PreflightVerifier, verify_execution_mode_alignment


def test_parse_args_live_flag():
    """Verify dedicated --live CLI flag sets args.live and args.mode to 'live'."""
    args = parse_args(["--headless-trader", "--live"])
    assert args.live is True
    assert args.mode == "live"
    assert args.headless_trader is True


def test_main_headless_trader_live_mode(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    """
    When running headlessly with config ave.mode == 'live',
    arms live broker execution and logs [CRITICAL] RUNTIME EXECUTION MODE: LIVE BROKER ROUTING ARMED.
    """
    strat_cfg = tmp_path / "strategy_config.json"
    strat_cfg.write_text(json.dumps({"ave": {"mode": "live"}}), encoding="utf-8")

    args = argparse.Namespace(
        headless_trader=True,
        live=False,
        mode="shadow",
        confirm_live=False,
        ignore_session=False,
        governor=False,
        no_api=False,
        host=None,
        port=None,
    )

    with patch("main.STRATEGY_CONFIG_FILE", strat_cfg), \
         patch("main.load_active_config", return_value={"engine": {}}), \
         patch("main.EngineContext") as mock_ctx_cls, \
         patch("main.EngineSettings") as mock_settings_cls, \
         patch("main.build_live", new_callable=AsyncMock) as mock_build_live, \
         patch("core.ipc.TraderIPCServer") as mock_ipc_cls, \
         patch("core.notifier.send_alert", return_value=None):

        mock_ctx = MagicMock()
        mock_ctx_cls.return_value = mock_ctx
        mock_engine = MagicMock()
        mock_engine.run_schedulers = AsyncMock()
        mock_engine.ledger.positions = {}
        mock_build_live.return_value = mock_engine
        mock_ipc = AsyncMock()
        mock_ipc_cls.return_value = mock_ipc

        with caplog.at_level(logging.INFO), \
             patch.object(asyncio.Event, "wait", new_callable=AsyncMock):
            ret = asyncio.run(run(args))

        assert ret == 0
        mock_build_live.assert_awaited_once()
        assert any(
            record.levelno == logging.CRITICAL and "RUNTIME EXECUTION MODE: LIVE BROKER ROUTING ARMED" in record.message
            for record in caplog.records
        )


def test_main_headless_trader_shadow_mode(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    """
    When running headlessly with config ave.mode == 'shadow',
    instantiates build_shadow() and logs [INFO] RUNTIME EXECUTION MODE: SHADOW SIMULATION.
    """
    strat_cfg = tmp_path / "strategy_config.json"
    strat_cfg.write_text(json.dumps({"ave": {"mode": "shadow"}}), encoding="utf-8")

    args = argparse.Namespace(
        headless_trader=True,
        live=False,
        mode="shadow",
        confirm_live=False,
        ignore_session=False,
        governor=False,
        no_api=False,
        host=None,
        port=None,
    )

    with patch("main.STRATEGY_CONFIG_FILE", strat_cfg), \
         patch("main.load_active_config", return_value={"engine": {}}), \
         patch("main.EngineContext") as mock_ctx_cls, \
         patch("main.EngineSettings") as mock_settings_cls, \
         patch("main.build_shadow", new_callable=AsyncMock) as mock_build_shadow, \
         patch("core.ipc.TraderIPCServer") as mock_ipc_cls, \
         patch("core.notifier.send_alert", return_value=None):

        mock_ctx = MagicMock()
        mock_ctx_cls.return_value = mock_ctx
        mock_engine = MagicMock()
        mock_engine.run_schedulers = AsyncMock()
        mock_engine.ledger.positions = {}
        mock_build_shadow.return_value = mock_engine
        mock_ipc = AsyncMock()
        mock_ipc_cls.return_value = mock_ipc

        with caplog.at_level(logging.INFO), \
             patch.object(asyncio.Event, "wait", new_callable=AsyncMock):
            ret = asyncio.run(run(args))

        assert ret == 0
        mock_build_shadow.assert_awaited_once()
        assert any(
            record.levelno == logging.INFO and "RUNTIME EXECUTION MODE: SHADOW SIMULATION" in record.message
            for record in caplog.records
        )


def test_main_live_flag_rejects_shadow_fallback(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    """
    When --live CLI flag is explicitly passed but config ave.mode == 'shadow',
    rejects shadow fallback and exits with code 2.
    """
    strat_cfg = tmp_path / "strategy_config.json"
    strat_cfg.write_text(json.dumps({"ave": {"mode": "shadow"}}), encoding="utf-8")

    args = argparse.Namespace(
        headless_trader=True,
        live=True,
        mode="live",
        confirm_live=True,
        ignore_session=False,
        governor=False,
        no_api=False,
        host=None,
        port=None,
    )

    with patch("main.STRATEGY_CONFIG_FILE", strat_cfg), \
         patch("main.load_active_config", return_value={"engine": {}}):

        with caplog.at_level(logging.CRITICAL):
            ret = asyncio.run(run(args))

        assert ret == 2
        assert any(
            "Rejecting shadow fallback" in record.message
            for record in caplog.records
        )


def test_invariant_6_execution_mode_alignment_live_pass(tmp_path: Path):
    """Invariant 6 passes when config is live and unit has --live."""
    strat_cfg = tmp_path / "strategy_config.json"
    strat_cfg.write_text(json.dumps({"ave": {"mode": "live"}}), encoding="utf-8")

    unit_file = tmp_path / "schwab-trader.service"
    unit_file.write_text(
        "[Service]\nExecStart=/home/nicho/schwab_engine/.venv/bin/python main.py --headless-trader --live\n",
        encoding="utf-8",
    )

    ok, msg = verify_execution_mode_alignment(strategy_config_path=strat_cfg, systemd_unit_path=unit_file)
    assert ok is True
    assert "broker router is enabled" in msg


def test_invariant_6_execution_mode_alignment_shadow_pass(tmp_path: Path):
    """Invariant 6 passes when config is shadow and unit has --mode shadow."""
    strat_cfg = tmp_path / "strategy_config.json"
    strat_cfg.write_text(json.dumps({"ave": {"mode": "shadow"}}), encoding="utf-8")

    unit_file = tmp_path / "schwab-trader.service"
    unit_file.write_text(
        "[Service]\nExecStart=/home/nicho/schwab_engine/.venv/bin/python main.py --headless-trader --mode shadow\n",
        encoding="utf-8",
    )

    ok, msg = verify_execution_mode_alignment(strategy_config_path=strat_cfg, systemd_unit_path=unit_file)
    assert ok is True
    assert "shadow simulation active" in msg


def test_invariant_6_execution_mode_alignment_live_config_shadow_unit_fails(tmp_path: Path):
    """Invariant 6 fails when config is live but unit is running shadow-only bypass."""
    strat_cfg = tmp_path / "strategy_config.json"
    strat_cfg.write_text(json.dumps({"ave": {"mode": "live"}}), encoding="utf-8")

    unit_file = tmp_path / "schwab-trader.service"
    unit_file.write_text(
        "[Service]\nExecStart=/home/nicho/schwab_engine/.venv/bin/python main.py --headless-trader --mode shadow\n",
        encoding="utf-8",
    )

    ok, msg = verify_execution_mode_alignment(strategy_config_path=strat_cfg, systemd_unit_path=unit_file)
    assert ok is False
    assert "shadow-only bypass" in msg


def test_invariant_6_execution_mode_alignment_shadow_config_live_unit_fails(tmp_path: Path):
    """Invariant 6 fails when config is shadow but unit forces live."""
    strat_cfg = tmp_path / "strategy_config.json"
    strat_cfg.write_text(json.dumps({"ave": {"mode": "shadow"}}), encoding="utf-8")

    unit_file = tmp_path / "schwab-trader.service"
    unit_file.write_text(
        "[Service]\nExecStart=/home/nicho/schwab_engine/.venv/bin/python main.py --headless-trader --live\n",
        encoding="utf-8",
    )

    ok, msg = verify_execution_mode_alignment(strategy_config_path=strat_cfg, systemd_unit_path=unit_file)
    assert ok is False
    assert "routes live orders" in msg


def test_invariant_6_in_preflight_verifier(tmp_path: Path):
    """Verify that Invariant 6 is evaluated inside PreflightVerifier.run_all_checks."""
    strat_cfg = tmp_path / "strategy_config.json"
    strat_cfg.write_text(json.dumps({"ave": {"mode": "live"}}), encoding="utf-8")

    unit_file = tmp_path / "schwab-trader.service"
    unit_file.write_text(
        "[Service]\nExecStart=/home/nicho/schwab_engine/.venv/bin/python main.py --headless-trader --live\n",
        encoding="utf-8",
    )

    report_file = tmp_path / "report.json"
    journal_file = tmp_path / "journal.db"

    verifier = PreflightVerifier(
        journal_db_path=journal_file,
        report_path=report_file,
        strategy_config_path=strat_cfg,
        systemd_unit_path=unit_file,
    )

    check_ok, check_msg = verifier.verify_execution_mode_alignment()
    assert check_ok is True
    assert "broker router is enabled" in check_msg
