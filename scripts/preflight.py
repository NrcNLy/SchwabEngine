#!/usr/bin/env python3
"""
scripts/preflight.py
====================
Production Preflight Validator & Invariant Latch Gatekeeper (#60).
Schwab Automated Day-Trading Engine.

Runs prior to every market open (scheduled daily Mon-Fri at 09:15:00 EDT / 13:15:00 UTC)
to verify account cash settlement, broker connectivity, database health, daemon states,
CPU pinning, and risk calibrations before permitting live execution.

12 Preflight Invariant Checks:
------------------------------
 1. Broker Authentication & REST Connect: Live handshake with Charles Schwab REST API.
 2. Account ...4015 Balance Extraction: Query target securitiesAccount and verify balance parsing.
 3. Zero Unhandled Broker Error Codes: 0 HTTP 4xx/5xx or unhandled broker codes, circuit breaker CLOSED.
 4. Cash Withdrawal & NLV Readout: Read cashAvailableForWithdrawal and currentLiquidationValue.
 5. Zero GFV Risk Liquidity Invariant: Verify max_position_size_usd does not exceed settled funds when unsettledCash > 0.
 6. Pre-Market Flat Inventory: Assert openPositionsCount == 0 prior to 09:30 EDT.
 7. SQLite Storage Integrity Check: Verify PRAGMA integrity_check returns 'ok' on trades.db and telemetry.db.
 8. SQLite Storage WAL Journal Mode: Verify PRAGMA journal_mode returns 'wal' on trades.db and telemetry.db.
 9. Trading Daemon State: Assert schwab-trader.service is active or enabled.
10. Web Gateway Health: Assert schwab-web.service responds HTTP 200 on port 8080.
11. Daemon CPU Affinity Pinning: Assert Core 0 for engine daemon and Core 1 for web gateway.
12. Risk & Recovery Latch Calibration: Verify max_position_size_pct <= 0.33, max_account_risk_pct <= 0.03,
    and recovery.halt_new_entries == True if recovery state is not DONE.

Latch Action:
-------------
If ANY invariant check fails (exit code 1):
  - Automatically locks trading.engine_active: false in configuration and strategy state.
  - Immediately dispatches an emergency alert to ntfy (schwab-trader-nrc-8472).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

# Setup project root and Python path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Load .env file
env_file = PROJECT_ROOT / ".env"
if env_file.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(env_file)
    except Exception:
        pass
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("preflight")

from core.paths import (
    STATE_DIR,
    TRADES_DB_PATH,
    TELEMETRY_DB_PATH,
    RECOVERY_STATE_FILE,
    RECOVERY_STATUS_FILE,
)
from core.runtime import load_config
from core.auth import SecurityVault, SchwabAuthManager
from data.rest_client import SchwabRestClient
from core.notifier import send_alert

DEFAULT_NTFY_TOPIC = "schwab-trader-nrc-8472"
TARGET_ACCOUNT_SUFFIX = "4015"
NY_TZ = ZoneInfo("America/New_York")


# ==============================================================================
# Audit Invariant Result Model & Reporter
# ==============================================================================

@dataclass
class InvariantResult:
    index: int
    name: str
    passed: bool
    evidence: str
    failure_reason: str = ""


class PreflightAudit:
    def __init__(self) -> None:
        self.results: List[InvariantResult] = []

    def record(self, index: int, name: str, passed: bool, evidence: str, failure_reason: str = "") -> bool:
        res = InvariantResult(index, name, passed, evidence, failure_reason)
        self.results.append(res)
        return passed

    @property
    def total_count(self) -> int:
        return len(self.results)

    @property
    def pass_count(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def fail_count(self) -> int:
        return sum(1 for r in self.results if not r.passed)

    @property
    def all_passed(self) -> bool:
        return self.fail_count == 0

    def render_table(self, execution_context: str) -> str:
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        now_edt = datetime.now(NY_TZ).strftime("%Y-%m-%d %H:%M:%S EDT")
        
        sep = "=" * 116
        div = "-" * 116
        lines = [
            sep,
            f"                     SCHWAB TRADING ENGINE — PREFLIGHT INVARIANT AUDIT & LATCH GATEKEEPER",
            f" Timestamp (EDT): {now_edt} | Account: ...{TARGET_ACCOUNT_SUFFIX} | Host: {execution_context} | Target: Market Open",
            sep,
            f"{'ID':<4} {'INVARIANT CHECK NAME':<38} {'STATUS':<8} {'TELEMETRY / INVARIANT EVIDENCE':<62}",
            div,
        ]

        for r in self.results:
            status_str = "PASS" if r.passed else "FAIL"
            evidence_str = r.evidence if r.passed else f"ERR: {r.failure_reason}"
            if len(evidence_str) > 62:
                evidence_str = evidence_str[:59] + "..."
            lines.append(f"{r.index:02d}   {r.name:<38} {status_str:<8} {evidence_str:<62}")

        lines.append(div)
        verdict = (
            "CLEARANCE GRANTED — ALL 12 INVARIANTS LATCHED (SAFE FOR MARKET OPEN)"
            if self.all_passed
            else "CLEARANCE DENIED — INVARIANT BREACH (LOCKING TRADING.ENGINE_ACTIVE)"
        )
        status_tag = "[PASS]" if self.all_passed else "[FAIL]"
        lines.append(f"TOTAL CHECKS: {self.total_count} | PASSED: {self.pass_count} | FAILED: {self.fail_count}")
        lines.append(f"VERDICT: {status_tag} {verdict}")
        lines.append(sep)
        return "\n".join(lines)


# ==============================================================================
# Invariant Latch Lockout & Alert Dispatcher
# ==============================================================================

def engage_invariant_latch(failures: List[InvariantResult], topic: str = DEFAULT_NTFY_TOPIC) -> None:
    """
    Invoked when any preflight invariant fails.
    Automatically locks trading.engine_active: false in config/config.yaml and strategy_config.json,
    and dispatches high-priority emergency alerts via ntfy.
    """
    logger.critical("PREFLIGHT INVARIANT FAILURE DETECTED: Engaging Invariant Latch")

    reasons = [f"#{r.index} {r.name}: {r.failure_reason or r.evidence}" for r in failures]
    reasons_fmt = "\n".join(f" - {r}" for r in reasons)

    # 1. Update config/config.yaml
    cfg_paths = [
        PROJECT_ROOT / "config" / "config.yaml",
        Path("/home/nicho/schwab_engine/config/config.yaml"),
    ]
    for p in cfg_paths:
        try:
            if p.exists():
                text = p.read_text(encoding="utf-8")
                if "engine_active:" in text:
                    text = re.sub(r"engine_active:\s*(true|True|false|False)", "engine_active: false", text)
                elif "trading:" in text:
                    text = text.replace("trading:\n", "trading:\n  engine_active: false\n")
                else:
                    text = "trading:\n  engine_active: false\n\n" + text
                p.write_text(text, encoding="utf-8")
                logger.info("Latched trading.engine_active: false in %s", p)
        except Exception as exc:
            logger.error("Failed updating %s during latch: %s", p, exc)

    # 2. Update strategy_config.json
    strat_paths = [
        PROJECT_ROOT / "config" / "strategy_config.json",
        STATE_DIR / "strategy_config.json",
        Path("/home/nicho/schwab_state/strategy_config.json"),
    ]
    for p in strat_paths:
        try:
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                data["hard_lockout_active"] = True
                data["emergency_flatten"] = True
                data["engine_active"] = False
                data["_latched_at"] = datetime.now(timezone.utc).isoformat()
                data["_latch_reason"] = "PREFLIGHT_INVARIANT_FAILURE"
                p.write_text(json.dumps(data, indent=2), encoding="utf-8")
                logger.info("Latched hard_lockout_active: true in %s", p)
        except Exception as exc:
            logger.error("Failed updating %s during latch: %s", p, exc)

    # 3. Dispatch Emergency ntfy Alert
    alert_title = "🚨 [PREFLIGHT INVARIANT LATCH] Engine Locked FALSE"
    alert_msg = (
        f"Preflight Invariant Breached on schwab-trader!\n\n"
        f"Failures ({len(failures)} total):\n{reasons_fmt}\n\n"
        f"trading.engine_active has been locked FALSE. Live orders suppressed."
    )
    send_alert(
        title=alert_title,
        message=alert_msg,
        priority="urgent",
        tags=["rotating_light", "no_entry", "fire"],
        topic=topic,
    )


# ==============================================================================
# Invariant Evaluation Engine
# ==============================================================================

def run_preflight_checks(audit: PreflightAudit, cfg: Dict[str, Any]) -> None:
    """Executes all 12 invariant checks sequentially."""
    
    # Context holders across checks
    rest_client: Optional[SchwabRestClient] = None
    target_hash: Optional[str] = None
    account_data: Optional[Dict[str, Any]] = None
    cur_liq_val: float = 0.0
    cash_avail_withdr: float = 0.0
    unsettled_cash: float = 0.0
    raw_positions: List[Dict[str, Any]] = []
    broker_error_count: int = 0
    circuit_breaker_open: bool = False

    auth_cfg = cfg.get("auth", {})
    acct_cfg = cfg.get("account", {})
    risk_cfg = cfg.get("risk", {})
    target_suffix = str(acct_cfg.get("required_suffix", "015"))

    # --------------------------------------------------------------------------
    # CHECK 1: Check Authentication & Broker State (REST Handshake)
    # --------------------------------------------------------------------------
    chk_1_name = "Broker Authentication & REST Connect"
    try:
        cid = os.getenv("SCHWAB_CLIENT_ID")
        sec = os.getenv("SCHWAB_CLIENT_SECRET")
        pw = os.getenv("VAULT_PASSPHRASE")
        if not cid or not sec or not pw:
            raise ValueError("Missing Schwab credentials (.env SCHWAB_CLIENT_ID, SCHWAB_CLIENT_SECRET, VAULT_PASSPHRASE)")

        vault_name = auth_cfg.get("vault_file", "schwab_tokens_vault.json")
        candidates = [
            PROJECT_ROOT / vault_name,
            STATE_DIR / vault_name,
            Path("/home/nicho/schwab_state") / vault_name,
            Path(vault_name),
        ]
        vault_path = next((p for p in candidates if p.exists() and p.stat().st_size > 0), None)
        if not vault_path:
            raise FileNotFoundError(f"Token vault {vault_name} not found")

        vault = SecurityVault(
            passphrase=pw,
            iterations=int(auth_cfg.get("pbkdf2_iterations", 600000)),
            vault_path=vault_path,
        )
        auth = SchwabAuthManager(cid, sec, vault, cfg)
        auth.load_tokens()

        # Check circuit breaker
        circuit_breaker_open = getattr(auth, "auth_circuit_open", False)
        if circuit_breaker_open:
            raise RuntimeError("Authentication circuit breaker is OPEN (AUTH_LOCKED)")

        rest_client = SchwabRestClient.from_config(cfg, auth)
        account_numbers = rest_client.get_account_numbers()
        if not account_numbers or not isinstance(account_numbers, list):
            raise RuntimeError(f"Unexpected account numbers response: {account_numbers}")

        audit.record(
            1, chk_1_name, True,
            f"OAuth token active; REST handshake OK ({len(account_numbers)} accounts linked)"
        )
    except Exception as exc:
        broker_error_count += 1
        audit.record(1, chk_1_name, False, "", str(exc))

    # --------------------------------------------------------------------------
    # CHECK 2: Assert Account ...4015 Balance Extraction Succeeds
    # --------------------------------------------------------------------------
    chk_2_name = "Account ...4015 Balance Extraction"
    if rest_client is not None and not audit.results[-1].passed is False:
        try:
            for entry in account_numbers:
                acc_no = str(entry.get("accountNumber", ""))
                if acc_no.endswith("4015") or acc_no.endswith(target_suffix):
                    target_hash = entry.get("hashValue")
                    break

            if not target_hash:
                raise RuntimeError(f"Could not locate account with suffix 4015 or {target_suffix}")

            acct_url = f"{rest_client._trader_base}/accounts/{target_hash}"
            resp = rest_client._get(acct_url, params={"fields": "positions"})
            if resp.status_code != 200:
                broker_error_count += 1
                raise RuntimeError(f"Broker returned status {resp.status_code}")

            account_data = resp.json()
            sec_acct = account_data.get("securitiesAccount", account_data)
            cur_bal = sec_acct.get("currentBalances", {})
            raw_positions = sec_acct.get("positions", []) or []

            if not cur_bal:
                raise RuntimeError("Empty currentBalances returned for account ...4015")

            audit.record(
                2, chk_2_name, True,
                f"Securities account located, hash {target_hash[:8]}...{target_hash[-6:]} extracted"
            )
        except Exception as exc:
            broker_error_count += 1
            audit.record(2, chk_2_name, False, "", str(exc))
    else:
        audit.record(2, chk_2_name, False, "", "Skipped due to REST connection failure")

    # --------------------------------------------------------------------------
    # CHECK 3: Verify Zero Unhandled Broker Error Codes
    # --------------------------------------------------------------------------
    chk_3_name = "Zero Unhandled Broker Error Codes"
    no_errors = (broker_error_count == 0) and not circuit_breaker_open
    if no_errors:
        audit.record(
            3, chk_3_name, True,
            "0 unhandled broker error codes (HTTP 200 OK, auth circuit breaker CLOSED)"
        )
    else:
        audit.record(
            3, chk_3_name, False, "",
            f"{broker_error_count} broker error(s) detected, circuit_open={circuit_breaker_open}"
        )

    # --------------------------------------------------------------------------
    # CHECK 4: Read cashAvailableForWithdrawal & currentLiquidationValue
    # --------------------------------------------------------------------------
    chk_4_name = "Cash Withdrawal & NLV Readout"
    if account_data is not None:
        try:
            sec_acct = account_data.get("securitiesAccount", account_data)
            cur_bal = sec_acct.get("currentBalances", {})
            agg_bal = account_data.get("aggregatedBalance", {})

            liq_val_raw = cur_bal.get("liquidationValue")
            if liq_val_raw is None:
                liq_val_raw = cur_bal.get("currentLiquidationValue")
            if liq_val_raw is None:
                liq_val_raw = agg_bal.get("currentLiquidationValue")
            cur_liq_val = float(liq_val_raw) if liq_val_raw is not None else 0.0

            cash_avail_raw = cur_bal.get("cashAvailableForWithdrawal")
            cash_avail_withdr = float(cash_avail_raw) if cash_avail_raw is not None else 0.0

            unsettled_raw = cur_bal.get("unsettledCash")
            unsettled_cash = float(unsettled_raw) if unsettled_raw is not None else 0.0

            if cur_liq_val <= 0:
                raise ValueError(f"currentLiquidationValue (${cur_liq_val:,.2f}) must be > 0")
            if cash_avail_withdr < 0:
                raise ValueError(f"cashAvailableForWithdrawal (${cash_avail_withdr:,.2f}) cannot be negative")

            audit.record(
                4, chk_4_name, True,
                f"NLV: ${cur_liq_val:,.2f} | CashAvailableForWithdrawal: ${cash_avail_withdr:,.2f}"
            )
        except Exception as exc:
            audit.record(4, chk_4_name, False, "", str(exc))
    else:
        audit.record(4, chk_4_name, False, "", "Skipped due to account data extraction failure")

    # --------------------------------------------------------------------------
    # CHECK 5: Enforce Cash & Settlement Invariants (Zero GFV Risk)
    # --------------------------------------------------------------------------
    chk_5_name = "Zero GFV Risk Liquidity Invariant"
    try:
        max_pos_pct = float(risk_cfg.get("max_position_size_pct", risk_cfg.get("single_ticker_cap_pct", 0.33)))
        max_position_size_usd = round(cur_liq_val * max_pos_pct, 2)
        settled_liquidity = cash_avail_withdr

        # Invariant: If unsettledCash > 0, verify max_position_size_usd <= settled funds
        if unsettled_cash > 0:
            gfv_risk_free = max_position_size_usd <= settled_liquidity
            if not gfv_risk_free:
                raise ValueError(
                    f"GFV RISK DETECTED: Max position size (${max_position_size_usd:,.2f}) exceeds "
                    f"settled funds (${settled_liquidity:,.2f}) while unsettledCash=${unsettled_cash:,.2f}"
                )
            audit.record(
                5, chk_5_name, True,
                f"Unsettled: ${unsettled_cash:,.2f} | Settled: ${settled_liquidity:,.2f} >= MaxPos: ${max_position_size_usd:,.2f} (0 GFV)"
            )
        else:
            # Fully settled account
            audit.record(
                5, chk_5_name, True,
                f"Fully settled (Unsettled: $0.00) | Liquidity: ${settled_liquidity:,.2f} >= MaxPos: ${max_position_size_usd:,.2f}"
            )
    except Exception as exc:
        audit.record(5, chk_5_name, False, "", str(exc))

    # --------------------------------------------------------------------------
    # CHECK 6: Assert openPositionsCount == 0 prior to 09:30 EDT
    # --------------------------------------------------------------------------
    chk_6_name = "Pre-Market Flat Inventory (0 Open Pos)"
    try:
        open_positions = []
        for p in raw_positions:
            l_qty = float(p.get("longQuantity", 0.0) or 0.0)
            s_qty = float(p.get("shortQuantity", 0.0) or 0.0)
            if abs(l_qty - s_qty) > 0:
                open_positions.append(p)
        open_positions_count = len(open_positions)

        now_edt = datetime.now(NY_TZ)
        is_prior_to_open = (now_edt.hour < 9) or (now_edt.hour == 9 and now_edt.minute < 30)

        if is_prior_to_open and open_positions_count > 0:
            syms = [pos.get("instrument", {}).get("symbol", "UNKNOWN") for pos in open_positions]
            raise ValueError(f"Open positions found prior to 09:30 EDT: {syms} (count={open_positions_count})")
        elif open_positions_count > 0:
            # If tested outside pre-market, warn or verify flat requirement
            syms = [pos.get("instrument", {}).get("symbol", "UNKNOWN") for pos in open_positions]
            raise ValueError(f"Inventory not flat: openPositionsCount={open_positions_count} ({syms})")
        else:
            audit.record(
                6, chk_6_name, True,
                f"openPositionsCount == 0 (Inventory 100% flat at {now_edt.strftime('%H:%M:%S EDT')})"
            )
    except Exception as exc:
        audit.record(6, chk_6_name, False, "", str(exc))

    # --------------------------------------------------------------------------
    # CHECK 7: Verify Storage Integrity Check (trades.db, telemetry.db)
    # --------------------------------------------------------------------------
    chk_7_name = "SQLite Storage Integrity Check"
    try:
        db_candidates = [
            ("trades.db", TRADES_DB_PATH),
            ("telemetry.db", TELEMETRY_DB_PATH),
        ]
        status_pieces = []
        for db_name, db_path in db_candidates:
            if not db_path.exists():
                # Check VM fallback
                alt = Path("/home/nicho/schwab_state") / db_name
                if alt.exists():
                    db_path = alt
                else:
                    raise FileNotFoundError(f"Database {db_name} not found at {db_path}")

            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            res = con.execute("PRAGMA integrity_check").fetchone()
            con.close()
            check_str = str(res[0]).strip().lower() if res else ""
            if check_str != "ok":
                raise ValueError(f"{db_name} integrity check returned '{check_str}'")
            status_pieces.append(f"{db_name}: ok")

        audit.record(7, chk_7_name, True, " | ".join(status_pieces) + " (0 corruption)")
    except Exception as exc:
        audit.record(7, chk_7_name, False, "", str(exc))

    # --------------------------------------------------------------------------
    # CHECK 8: Verify SQLite WAL Journal Mode (trades.db, telemetry.db)
    # --------------------------------------------------------------------------
    chk_8_name = "SQLite Storage WAL Journal Mode"
    try:
        db_candidates = [
            ("trades.db", TRADES_DB_PATH),
            ("telemetry.db", TELEMETRY_DB_PATH),
        ]
        status_pieces = []
        for db_name, db_path in db_candidates:
            if not db_path.exists():
                alt = Path("/home/nicho/schwab_state") / db_name
                if alt.exists():
                    db_path = alt
                else:
                    raise FileNotFoundError(f"Database {db_name} not found at {db_path}")

            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            res = con.execute("PRAGMA journal_mode").fetchone()
            con.close()
            mode_str = str(res[0]).strip().lower() if res else ""
            if mode_str != "wal":
                raise ValueError(f"{db_name} journal_mode is '{mode_str}' (expected 'wal')")
            status_pieces.append(f"{db_name}: wal")

        audit.record(8, chk_8_name, True, " | ".join(status_pieces) + " (WAL mode active)")
    except Exception as exc:
        audit.record(8, chk_8_name, False, "", str(exc))

    # --------------------------------------------------------------------------
    # CHECK 9: Check Systemd Unit States: schwab-trader.service
    # --------------------------------------------------------------------------
    chk_9_name = "Trading Daemon State (schwab-trader)"
    try:
        # Check systemctl status
        res = subprocess.run(
            ["systemctl", "is-active", "schwab-trader.service"],
            capture_output=True, text=True, timeout=5,
        )
        active_status = res.stdout.strip()
        
        res_en = subprocess.run(
            ["systemctl", "is-enabled", "schwab-trader.service"],
            capture_output=True, text=True, timeout=5,
        )
        enabled_status = res_en.stdout.strip()

        is_active_or_enabled = (active_status == "active") or (enabled_status == "enabled")
        if not is_active_or_enabled:
            raise ValueError(f"schwab-trader.service is {active_status} (enabled: {enabled_status})")

        audit.record(
            9, chk_9_name, True,
            f"schwab-trader.service is {active_status} and {enabled_status}"
        )
    except Exception as exc:
        audit.record(9, chk_9_name, False, "", str(exc))

    # --------------------------------------------------------------------------
    # CHECK 10: schwab-web.service responds HTTP 200 on port 8080
    # --------------------------------------------------------------------------
    chk_10_name = "Web Gateway Health (schwab-web :8080)"
    try:
        import urllib.request
        t0 = time.perf_counter()
        # Query root or /health
        req = urllib.request.Request("http://127.0.0.1:8080/", headers={"User-Agent": "SchwabPreflight/1.0"})
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            code = resp.getcode()
            elapsed_ms = (time.perf_counter() - t0) * 1000
            if code != 200:
                raise ValueError(f"Expected HTTP 200 from schwab-web:8080, got {code}")
            audit.record(
                10, chk_10_name, True,
                f"schwab-web.service responded HTTP 200 OK on port 8080 ({elapsed_ms:.1f}ms)"
            )
    except Exception as exc:
        audit.record(10, chk_10_name, False, "", str(exc))

    # --------------------------------------------------------------------------
    # CHECK 11: Verify CPU Affinity (Core 0 for engine, Core 1 for web)
    # --------------------------------------------------------------------------
    chk_11_name = "Daemon CPU Affinity Pinning"
    try:
        # Check systemd properties or proc status
        t_affinity_res = subprocess.run(
            ["systemctl", "show", "-p", "CPUAffinity", "--value", "schwab-trader.service"],
            capture_output=True, text=True, timeout=5,
        )
        w_affinity_res = subprocess.run(
            ["systemctl", "show", "-p", "CPUAffinity", "--value", "schwab-web.service"],
            capture_output=True, text=True, timeout=5,
        )
        t_aff = t_affinity_res.stdout.strip()
        w_aff = w_affinity_res.stdout.strip()

        # Also inspect running PIDs if available
        t_pid_res = subprocess.run(
            ["systemctl", "show", "-p", "MainPID", "--value", "schwab-trader.service"],
            capture_output=True, text=True, timeout=5,
        )
        w_pid_res = subprocess.run(
            ["systemctl", "show", "-p", "MainPID", "--value", "schwab-web.service"],
            capture_output=True, text=True, timeout=5,
        )
        t_pid = t_pid_res.stdout.strip()
        w_pid = w_pid_res.stdout.strip()

        if t_pid and t_pid != "0":
            status_p = Path(f"/proc/{t_pid}/status")
            if status_p.exists():
                for line in status_p.read_text().splitlines():
                    if line.startswith("Cpus_allowed_list:"):
                        t_aff = line.split(":", 1)[1].strip()

        if w_pid and w_pid != "0":
            status_p = Path(f"/proc/{w_pid}/status")
            if status_p.exists():
                for line in status_p.read_text().splitlines():
                    if line.startswith("Cpus_allowed_list:"):
                        w_aff = line.split(":", 1)[1].strip()

        if t_aff != "0":
            raise ValueError(f"schwab-trader CPU affinity is {t_aff!r} (expected '0')")
        if w_aff != "1":
            raise ValueError(f"schwab-web CPU affinity is {w_aff!r} (expected '1')")

        audit.record(
            11, chk_11_name, True,
            f"Trader daemon pinned Core 0 | Web gateway pinned Core 1"
        )
    except Exception as exc:
        audit.record(11, chk_11_name, False, "", str(exc))

    # --------------------------------------------------------------------------
    # CHECK 12: Verify Risk Configuration & Recovery Latch
    # --------------------------------------------------------------------------
    chk_12_name = "Risk & Recovery Latch Calibration"
    try:
        max_pos_pct = float(risk_cfg.get("max_position_size_pct", risk_cfg.get("single_ticker_cap_pct", 0.0)))
        max_acc_risk = float(risk_cfg.get("max_account_risk_pct", risk_cfg.get("max_risk_per_trade_pct", 0.0)))

        if max_pos_pct > 0.33:
            raise ValueError(f"risk.max_position_size_pct ({max_pos_pct}) exceeds 0.33 limit")
        if max_acc_risk > 0.03:
            raise ValueError(f"risk.max_account_risk_pct ({max_acc_risk}) exceeds 0.03 limit")

        # Check recovery state
        rec_state = "UNKNOWN"
        for rf in [RECOVERY_STATUS_FILE, RECOVERY_STATE_FILE, Path("/home/nicho/schwab_state/recovery_status.json")]:
            if rf.exists():
                try:
                    s_data = json.loads(rf.read_text(encoding="utf-8"))
                    rec_state = s_data.get("state", "UNKNOWN")
                    if rec_state != "UNKNOWN":
                        break
                except Exception:
                    pass

        recovery_cfg = cfg.get("recovery", {})
        halt_entries = recovery_cfg.get("halt_new_entries", False)

        if rec_state != "DONE" and not halt_entries:
            raise ValueError(
                f"recovery.halt_new_entries must be true while recovery state is '{rec_state}'"
            )

        audit.record(
            12, chk_12_name, True,
            f"max_pos={max_pos_pct}<=0.33, risk={max_acc_risk}<=0.03, recovery={rec_state} (halt={halt_entries})"
        )
    except Exception as exc:
        audit.record(12, chk_12_name, False, "", str(exc))


# ==============================================================================
# CLI Entrypoint
# ==============================================================================

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="SchwabEngine Preflight Validator & Invariant Latch Gatekeeper")
    parser.add_argument("--no-latch", action="store_true", help="Do not engage latch lock if invariants fail (diagnostic only)")
    args = parser.parse_args(argv)

    cfg = load_config()
    audit = PreflightAudit()

    # Host environment context
    hostname = os.uname().nodename if hasattr(os, "uname") else "local"

    run_preflight_checks(audit, cfg)
    output_table = audit.render_table(execution_context=hostname)
    print("\n" + output_table + "\n", flush=True)

    if not audit.all_passed:
        failures = [r for r in audit.results if not r.passed]
        if not args.no_latch:
            engage_invariant_latch(failures)
        else:
            logger.warning("--no-latch specified: skipping automated lockout")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
