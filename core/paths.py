"""
core/paths.py
=============
Single source of truth for where runtime state is persisted.

Everything the engine writes at run time lives under ``STATE_DIR`` (default ``./state``,
override with the ``ENGINE_STATE_DIR`` environment variable). In Docker this directory is a
mounted volume, so liquidity policy, ingested-document state and NLV anchors survive
rebuilds. (It must be a DIRECTORY mount: atomic ``os.replace`` onto a bind-mounted single
file fails with EBUSY.)
"""

from __future__ import annotations

import os
from pathlib import Path

def _resolve_state_dir() -> Path:
    env_val = os.environ.get("ENGINE_STATE_DIR")
    if env_val:
        return Path(env_val)
    vm_state = Path("/home/nicho/schwab_state")
    if vm_state.is_dir():
        return vm_state
    return Path("state")


STATE_DIR = _resolve_state_dir()

# ---- Consolidated project layout (static assets live in the repository tree) -------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DOCS_DIR = PROJECT_ROOT / "docs"
DATA_DIR = PROJECT_ROOT / "data"
EXPORTS_DIR = DATA_DIR / "exports"
FIXTURES_DIR = DATA_DIR / "fixtures"
REMOTE_VM_STATE_DIR = PROJECT_ROOT / "state" / "remote_vm_state"

STRATEGY_CONFIG_FILE = CONFIG_DIR / "strategy_config.json"
TELEMETRY_FIXTURE_FILE = FIXTURES_DIR / "schwab_telemetry_micro_sequence.json"

MACRO_STATE_FILE = STATE_DIR / "macro_liquidity.json"
POLICY_FILE = STATE_DIR / "liquidity_policy.json"
ANCHOR_ACTIVE_FILE = STATE_DIR / "nlv_anchor_active.json"
ANCHOR_SANDBOX_FILE = STATE_DIR / "nlv_anchor_sandbox.json"
DOCUMENTS_DIR = STATE_DIR / "documents"
PROCESSED_HASHES_FILE = DOCUMENTS_DIR / "processed_hashes.json"
TRADES_DB_PATH = STATE_DIR / "trades.db"
TRADING_JOURNAL_DB_PATH = STATE_DIR / "trading_journal.db"
BASELINES_DB_PATH = STATE_DIR / "market_baselines.db"
TELEMETRY_DB_PATH = STATE_DIR / "telemetry.db"
CONTROL_SIGNAL_FILE = STATE_DIR / "control_signal.json"
DYNAMIC_POLICY_FILE = STATE_DIR / "dynamic_policy.json"
DAILY_QUOTA_FILE = STATE_DIR / "daily_quota.json"
RECOVERY_PLAN_FILE = STATE_DIR / "recovery_plan.json"
RECOVERY_STATE_FILE = STATE_DIR / "recovery_state.json"
RECOVERY_STATUS_FILE = STATE_DIR / "recovery_status.json"
TRADER_IPC_SOCKET = Path(os.environ.get("TRADER_IPC_SOCKET", "/tmp/schwab_trader.sock"))
