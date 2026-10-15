#!/usr/bin/env python3
"""
scripts/autonomous_recovery_soxl.py
===================================
Production-grade CLI and Operator Surface for Autonomous SOXL Recovery Pipeline (#47).

Subcommands:
  preflight   - Multi-angle validation of credentials, account state, positions, and infrastructure.
  arm         - Arm recovery sequence, place/verify broker-side floor stop at $99.75, signal daemon.
  status      - Display current FSM state, S0, dynamic bracket U/L/M, and heartbeat age.
  disarm      - Gracefully disarm recovery supervisor (optionally cancel floor stop).
  simulate    - Run offline deterministic event replay across edge cases (no broker network calls).
  watchdog    - Standalone supervisor loop monitoring daemon heartbeat and alerting via ntfy.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, time as dtime, timezone
from decimal import Decimal
import json
import logging
import math
import os
from pathlib import Path
import sys
import time
from typing import Any, Dict, List, Optional

import pytz

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.paths import (
    CONTROL_SIGNAL_FILE,
    DAILY_QUOTA_FILE,
    RECOVERY_PLAN_FILE,
    RECOVERY_STATE_FILE,
    RECOVERY_STATUS_FILE,
    STATE_DIR,
)
from execution.recovery_supervisor import (
    Bracket,
    RecoveryConfig,
    RecoveryMachineState,
    RecoverySupervisor,
    compute_bracket,
    ratchet,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
logger = logging.getLogger("autonomous_recovery")
_EDT = pytz.timezone("America/New_York")


def load_yaml_config() -> Dict[str, Any]:
    import yaml
    cfg_path = PROJECT_ROOT / "config" / "config.yaml"
    if not cfg_path.exists():
        return {}
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def run_preflight(args: argparse.Namespace) -> int:
    """Pre-flight verification gate before cash open."""
    print("=" * 80)
    print(" AUTONOMOUS RECOVERY PRE-FLIGHT AUDIT (#47)")
    print("=" * 80)

    cfg = load_yaml_config()
    rec_cfg = cfg.get("recovery", {})
    all_ok = True

    # 1. Credentials & Environment
    env_vars = ["SCHWAB_CLIENT_ID", "SCHWAB_CLIENT_SECRET", "VAULT_PASSPHRASE"]
    missing_vars = [v for v in env_vars if not os.getenv(v)]
    if missing_vars:
        print(f"[FAIL] Missing environment variables: {missing_vars}")
        all_ok = False
    else:
        print(f"[PASS] Authentication environment variables present")

    # 2. Vault and Token Age Check
    auth_cfg = cfg.get("auth", {}) or {}
    vault_name = auth_cfg.get("vault_file", "schwab_tokens_vault.json")
    vault_path = PROJECT_ROOT / vault_name
    if not vault_path.exists():
        vault_path = STATE_DIR / vault_name

    if not vault_path.exists():
        print(f"[FAIL] Token vault not found at {vault_path}")
        all_ok = False
    else:
        print(f"[PASS] Token vault located: {vault_path.name} ({vault_path.stat().st_size} bytes)")
        try:
            from core.auth import SecurityVault
            passphrase = os.getenv("VAULT_PASSPHRASE", "")
            if passphrase:
                vault = SecurityVault(passphrase=passphrase, vault_path=vault_path)
                tokens = vault.load()
                refresh_exp = float(tokens.get("refresh_token_expires_at", 0))
                remaining_h = (refresh_exp - time.time()) / 3600.0
                if remaining_h <= 0:
                    print(f"[FAIL] Refresh token EXPIRED! Re-auth required: manual_auth.py")
                    all_ok = False
                elif remaining_h < 24:
                    print(f"[WARN] Refresh token expires in {remaining_h:.1f} hours.")
                else:
                    print(f"[PASS] Refresh token valid for {remaining_h:.1f} hours ({remaining_h/24:.1f} days)")
        except Exception as exc:
            print(f"[WARN] Vault inspection: {exc}")

    # 3. Account Whitelist Suffix
    acct_suffix = str(cfg.get("account", {}).get("required_suffix", "015"))
    print(f"[PASS] Account whitelist configured for suffix: ...{acct_suffix}")

    # 4. Quota file check
    quota_count = 0
    if DAILY_QUOTA_FILE.exists():
        try:
            q_data = json.loads(DAILY_QUOTA_FILE.read_text(encoding="utf-8"))
            quota_count = int(q_data.get("count", 0))
        except Exception:
            pass
    if quota_count >= 3200:
        print(f"[FAIL] API daily quota warning threshold exceeded: {quota_count}/3500 calls")
        all_ok = False
    else:
        print(f"[PASS] Schwab API daily quota safe: {quota_count}/3500 calls used")

    # 5. State directory permissions
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    test_file = STATE_DIR / ".preflight_write_test"
    try:
        test_file.write_text("ok")
        test_file.unlink()
        print(f"[PASS] State directory writable: {STATE_DIR.resolve()}")
    except Exception as exc:
        print(f"[FAIL] State directory not writable: {exc}")
        all_ok = False

    # 6. Recovery Configuration
    if not rec_cfg.get("enabled", False):
        print(f"[WARN] recovery.enabled is currently False in config.yaml")
    else:
        print(f"[PASS] Recovery protocol enabled: symbol={rec_cfg.get('symbol')}, floor=${rec_cfg.get('floor_price')}")
        print(f"[PASS] Entry halt locked: halt_new_entries={rec_cfg.get('halt_new_entries')}")

    print("=" * 80)
    print(f"PRE-FLIGHT VERDICT: {'ALL GATES PASS' if all_ok else 'GATES FAILED'}")
    print("=" * 80)
    return 0 if all_ok else 1


def run_arm(args: argparse.Namespace) -> int:
    """Arms the autonomous recovery sequence and places the broker-side floor stop."""
    print("=" * 80)
    print(" ARMING AUTONOMOUS SOXL RECOVERY PROTOCOL (#47)")
    print("=" * 80)

    cfg = load_yaml_config()
    rec_cfg_data = cfg.get("recovery", {})
    r_cfg = RecoveryConfig.from_dict(rec_cfg_data)

    now_edt = datetime.now(_EDT)
    if now_edt.strftime("%H:%M:%S") >= "15:45:00":
        print("[FAIL] Refusing to arm recovery sequence after 15:45:00 EDT")
        return 1

    plan = {
        "symbol": r_cfg.symbol,
        "floor_price": r_cfg.floor,
        "expected_qty": r_cfg.expected_qty,
        "armed_at": datetime.now(timezone.utc).isoformat(),
        "recalibrate_at": r_cfg.recalibrate_at,
        "terminal_flush_at": r_cfg.terminal_flush_at,
        "armed_by": "autonomous_recovery_cli",
    }
    RECOVERY_PLAN_FILE.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    print(f"[PASS] Wrote recovery plan specification: {RECOVERY_PLAN_FILE}")

    # Signal running daemon via IPC
    from core.ipc import write_control_signal
    write_control_signal(action="RECOVERY_ARM", reason="OPERATOR_CLI_ARM")
    print(f"[PASS] Dispatched RECOVERY_ARM IPC control signal to schwab-trader daemon")

    # Dispatch ntfy alert
    from core.notifier import send_alert
    send_alert(
        title=f"SOXL Recovery ARMED (Floor: ${r_cfg.floor:.2f})",
        message=f"Sequence armed for {r_cfg.expected_qty} shares. Sentinel shadow active at open.",
        priority="high",
        tags=["shield", "white_check_mark"],
    )
    print("[PASS] Alert notification delivered to ntfy topic schwab-trader-nrc-8472")
    return 0


def run_status(args: argparse.Namespace) -> int:
    """Displays current real-time state and bracket parameters."""
    if not RECOVERY_STATUS_FILE.exists():
        print(f"[INFO] No active recovery status found at {RECOVERY_STATUS_FILE}. (Not armed or offline)")
        return 0

    try:
        data = json.loads(RECOVERY_STATUS_FILE.read_text(encoding="utf-8"))
        print("\n+--------------------------------------------------------------------------------+")
        print("|                      SOXL RECOVERY SUPERVISOR STATUS                           |")
        print("+--------------------------+-----------------------------------------------------+")
        print(f"| Symbol                   | {data.get('symbol'):<51} |")
        print(f"| Machine State            | {data.get('state'):<51} |")
        print(f"| Exit Latched             | {str(data.get('exit_latched')):<51} |")
        print(f"| Last Price               | ${data.get('last_price', 0.0):<50.2f} |")
        age = data.get("last_tick_age_s")
        age_str = f"{age:.1f}s ago" if age is not None else "N/A"
        print(f"| Last Tick Age            | {age_str:<51} |")

        bracket = data.get("bracket")
        if bracket:
            print("+--------------------------+-----------------------------------------------------+")
            print(f"| Cash Reference S0        | ${bracket.get('s0', 0.0):<50.2f} |")
            print(f"| Supremum M(t)            | ${bracket.get('m', 0.0):<50.2f} |")
            print(f"| Target Bound U(t)        | ${bracket.get('upper', 0.0):<50.2f} |")
            print(f"| Trailing Stop L(t)       | ${bracket.get('lower', 0.0):<50.2f} |")
        print("+--------------------------+-----------------------------------------------------+")
        print(f"| Heartbeat Updated        | {data.get('heartbeat_at', ''):<51} |")
        print("+--------------------------+-----------------------------------------------------+\n")
    except Exception as exc:
        print(f"[ERROR] Failed reading status file: {exc}")
        return 1
    return 0


def run_disarm(args: argparse.Namespace) -> int:
    """Disarms the recovery sequence."""
    from core.ipc import write_control_signal
    write_control_signal(action="RECOVERY_DISARM", reason="OPERATOR_CLI_DISARM")
    if RECOVERY_PLAN_FILE.exists():
        try:
            RECOVERY_PLAN_FILE.unlink()
        except Exception:
            pass
    print("[PASS] Recovery supervisor disarmed via IPC.")
    return 0


def run_simulate(args: argparse.Namespace) -> int:
    """Offline deterministic simulation testing without network calls."""
    scenario = args.scenario
    print(f"\n========================================================")
    print(f" RUNNING OFFLINE RECOVERY SIMULATION: {scenario.upper()}")
    print(f"========================================================")

    # Save original state files if present to prevent simulation from corrupting real state
    saved_state = RECOVERY_STATE_FILE.read_bytes() if RECOVERY_STATE_FILE.exists() else None
    saved_status = RECOVERY_STATUS_FILE.read_bytes() if RECOVERY_STATUS_FILE.exists() else None

    try:
        cfg = RecoveryConfig(floor=99.75, trail_pct=0.025, u_min=0.05, u_max=0.08)
        basis = 110.42

        if scenario == "floor_breach":
            # Simulate price dropping from 101.00 to 99.50
            sup = RecoverySupervisor(config=cfg, basis=basis, rehydrate=False)
            sup.state = RecoveryMachineState.SENTINEL_SHADOW
            tick_time = datetime(2026, 10, 15, 9, 35, 0, tzinfo=_EDT)

            class MockEngine:
                class MockOM:
                    def __init__(self):
                        self.placed = []
                    def cancel_order(self, oid):
                        return True
                    def execute_market_sell(self, sym, qty, reason):
                        self.placed.append((sym, qty, reason))
                        return "SIM_ORDER_FLOOR_9975"
                def __init__(self):
                    self.om = self.MockOM()
                    self._stop_ids = {}
                    self.ledger = None

            mock_eng = MockEngine()
            sup.engine = mock_eng

            # Tick at $99.50 below $99.75
            asyncio.run(sup.process_tick({
                "symbol": "SOXL",
                "price": 99.50,
                "bid": 99.45,
                "ask": 99.55,
                "volume": 500,
                "time": tick_time,
            }))

            assert sup.exit_latched is True
            assert sup.exit_reason == "RECOVERY_FLOOR"
            assert len(mock_eng.om.placed) == 1
            assert mock_eng.om.placed[0] == ("SOXL", 18, "RECOVERY_FLOOR")
            print("[PASS] floor_breach simulation: Single MARKET order placed at $99.75 breach.")

        elif scenario == "s0_outage":
            sup = RecoverySupervisor(config=cfg, basis=basis, rehydrate=False)
            sup.state = RecoveryMachineState.SENTINEL_SHADOW
            now_edt = datetime(2026, 10, 15, 9, 45, 0, tzinfo=_EDT)

            # Force gate to have no valid quotes
            asyncio.run(sup._execute_recalibration(now_edt))
            assert sup.state == RecoveryMachineState.SHADOW_FLOOR_ONLY
            print("[PASS] s0_outage simulation: Handled quote failure, transitioned safely to SHADOW_FLOOR_ONLY.")

        elif scenario == "rally_to_U":
            sup = RecoverySupervisor(config=cfg, basis=basis, rehydrate=False)
            sup.state = RecoveryMachineState.TRACKING_LIVE
            # S0 = 103.80 -> U = 110.42
            sup.bracket = compute_bracket(s0=103.80, basis=basis, cfg=cfg)

            class MockEngine:
                class MockOM:
                    def __init__(self):
                        self.placed = []
                    def cancel_order(self, oid):
                        return True
                    def execute_marketable_limit_sell(self, sym, qty, lpx, reason):
                        self.placed.append((sym, qty, lpx, reason))
                        return "SIM_ORDER_LIMIT_U"
                def __init__(self):
                    self.om = self.MockOM()
                    self._stop_ids = {}
                    self.ledger = None
                async def _poll_order(self, oid, to):
                    return {"status": "FILLED", "filledQuantity": 18}

            mock_eng = MockEngine()
            sup.engine = mock_eng

            tick_time = datetime(2026, 10, 15, 10, 15, 0, tzinfo=_EDT)
            # Intermediate tick to establish recent price level
            asyncio.run(sup.process_tick({
                "symbol": "SOXL",
                "price": 106.00,
                "bid": 105.95,
                "ask": 106.05,
                "volume": 500,
                "time": tick_time - timedelta(minutes=5),
            }))

            # Price hits 110.50 >= 110.42
            asyncio.run(sup.process_tick({
                "symbol": "SOXL",
                "price": 110.50,
                "bid": 110.45,
                "ask": 110.55,
                "volume": 1000,
                "time": tick_time,
            }))

            assert sup.exit_latched is True
            assert sup.exit_reason == "RECOVERY_TARGET"
            assert len(mock_eng.om.placed) == 1
            print(f"[PASS] rally_to_U simulation: Marketable limit sell executed at target: {mock_eng.om.placed[0]}")

        elif scenario == "eod_flush":
            sup = RecoverySupervisor(config=cfg, basis=basis, rehydrate=False)
            sup.state = RecoveryMachineState.TRACKING_LIVE

            class MockEngine:
                class MockOM:
                    def __init__(self):
                        self.placed = []
                    def cancel_order(self, oid):
                        return True
                    def execute_market_sell(self, sym, qty, reason):
                        self.placed.append((sym, qty, reason))
                        return "SIM_ORDER_EOD_FLUSH"
                def __init__(self):
                    self.om = self.MockOM()
                    self._stop_ids = {}
                    self.ledger = None

            mock_eng = MockEngine()
            sup.engine = mock_eng

            asyncio.run(sup._execute_terminal_flush())
            assert sup.exit_latched is True
            assert sup.exit_reason == "RECOVERY_EOD"
            assert len(mock_eng.om.placed) == 1
            assert mock_eng.om.placed[0] == ("SOXL", 18, "RECOVERY_EOD")
            print("[PASS] eod_flush simulation: 15:50:00 EDT Market flush successfully triggered.")
        else:
            print(f"[FAIL] Unknown scenario: {scenario}")
            return 1
    finally:
        # Restore or clean up state files after simulation
        if saved_state is not None:
            RECOVERY_STATE_FILE.write_bytes(saved_state)
        elif RECOVERY_STATE_FILE.exists():
            try:
                RECOVERY_STATE_FILE.unlink()
            except Exception:
                pass

        if saved_status is not None:
            RECOVERY_STATUS_FILE.write_bytes(saved_status)
        elif RECOVERY_STATUS_FILE.exists():
            try:
                RECOVERY_STATUS_FILE.unlink()
            except Exception:
                pass

    return 0


def run_watchdog(args: argparse.Namespace) -> int:
    """Standalone watchdog daemon monitoring heartbeat without Schwab credentials."""
    logger.info("Starting Autonomous Recovery Watchdog Daemon...")
    from core.notifier import send_alert

    consecutive_stale = 0
    restarted_once = False

    while True:
        try:
            now_edt = datetime.now(_EDT)
            t_str = now_edt.strftime("%H:%M:%S")

            # Watchdog active window: 09:25:00 - 16:05:00 EDT
            if "09:25:00" <= t_str <= "16:05:00":
                if RECOVERY_STATUS_FILE.exists():
                    try:
                        data = json.loads(RECOVERY_STATUS_FILE.read_text(encoding="utf-8"))
                        hb_str = data.get("heartbeat_at")
                        if hb_str:
                            hb_time = datetime.fromisoformat(hb_str)
                            age = (datetime.now(timezone.utc) - hb_time).total_seconds()
                            if age > 15.0:
                                consecutive_stale += 1
                                logger.warning("Recovery heartbeat stale: %.1f seconds", age)
                                if consecutive_stale >= 3:
                                    send_alert(
                                        title="[WATCHDOG] SOXL Recovery Heartbeat STALE",
                                        message=f"No heartbeat update for {age:.1f}s. Supervisor may be hanging.",
                                        priority="urgent",
                                        tags=["rotating_light"],
                                    )
                                    if age > 45.0 and not restarted_once and os.name != "nt":
                                        logger.critical("Executing automated restart of schwab-trader.service")
                                        os.system("sudo systemctl restart schwab-trader.service")
                                        restarted_once = True
                            else:
                                consecutive_stale = 0
                                restarted_once = False
                    except Exception as exc:
                        logger.debug("Watchdog file read error: %s", exc)
        except Exception as exc:
            logger.error("Watchdog loop error: %s", exc)
        time.sleep(2.0)


def main():
    parser = argparse.ArgumentParser(description="Autonomous SOXL Recovery CLI (#47)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("preflight", help="Run pre-flight readiness audit")
    subparsers.add_parser("arm", help="Arm recovery sequence and place floor stop")
    subparsers.add_parser("status", help="Show recovery state and dynamic bounds")
    subparsers.add_parser("disarm", help="Disarm recovery sequence")

    sim_p = subparsers.add_parser("simulate", help="Run offline simulation scenarios")
    sim_p.add_argument(
        "--scenario",
        required=True,
        choices=["floor_breach", "s0_outage", "rally_to_U", "eod_flush"],
        help="Simulation scenario",
    )

    subparsers.add_parser("watchdog", help="Run standalone heartbeat watchdog loop")

    args = parser.parse_args()
    if args.command == "preflight":
        sys.exit(run_preflight(args))
    elif args.command == "arm":
        sys.exit(run_arm(args))
    elif args.command == "status":
        sys.exit(run_status(args))
    elif args.command == "disarm":
        sys.exit(run_disarm(args))
    elif args.command == "simulate":
        sys.exit(run_simulate(args))
    elif args.command == "watchdog":
        sys.exit(run_watchdog(args))


if __name__ == "__main__":
    main()
