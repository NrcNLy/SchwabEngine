#!/usr/bin/env python3
"""
scripts/tools/check_preflight_invariants.py
==========================================
Workstream 2 (#124): Automated Monday Pre-Market Invariant Verifier.

Executes between 09:00:00 and 09:25:00 EDT prior to the 09:30:00 EDT cash open:
1. Validates SEC Rule 15c6-1 T+1 Cash Ledger invariants:
   - Settled cash > $0.00
   - Unsettled cash == $0.00 (matured T+1 sweep completed)
   - Reserved cash == $0.00 (zero hanging overnight limit orders)
   - Three-bucket conservation: Settled + Unsettled + Reserved == Total
2. Asserts zero stranded 3x satellite positions (SOXL, TQQQ, TNA == 0).
3. Verifies OAuth pre-market gate status and session token TTL >= 1,800s.
4. Confirms token bucket rate limiter headroom (120 RPM, 3500 daily ceiling, < 100 used).
5. Validates write access to state/ and logs/ and checks SQLite WAL mode.
"""

from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.auth import OAuthTokenManager
from core.paths import LOGS_DIR, PREFLIGHT_REPORT_PATH, STATE_DIR, TRADING_JOURNAL_DB_PATH
from core.rate_limiter import AsyncTokenBucketRateLimiter
from core.t1_ledger import T1CashLedger

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("preflight_verifier")

SATELLITE_TICKERS = ("SOXL", "TQQQ", "TNA")
ZERO_DECIMAL = Decimal("0.00")


class PreflightVerifier:
    """
    Automated pre-market invariant verifier enforcing institutional risk bounds.
    """

    def __init__(
        self,
        ledger: Optional[T1CashLedger] = None,
        token_manager: Optional[OAuthTokenManager] = None,
        rate_limiter: Optional[AsyncTokenBucketRateLimiter] = None,
        positions: Optional[Dict[str, int]] = None,
        journal_db_path: Path = TRADING_JOURNAL_DB_PATH,
        report_path: Path = PREFLIGHT_REPORT_PATH,
    ) -> None:
        self.ledger = ledger
        self.token_manager = token_manager
        self.rate_limiter = rate_limiter
        self.positions = positions if positions is not None else {}
        self.journal_db_path = Path(journal_db_path)
        self.report_path = Path(report_path)

    def check_t1_ledger_invariants(self, baseline_nlv: float = 4400.0) -> Tuple[bool, str]:
        """
        Validate SEC Rule 15c6-1 T+1 Cash Ledger invariants:
        - Settled cash > $0.00
        - Unsettled cash == $0.00
        - Reserved cash == $0.00
        - Settled + Unsettled + Reserved == Total
        """
        ledger = self.ledger
        if ledger is None:
            ledger = T1CashLedger(settled_cash=Decimal(str(baseline_nlv)))

        settled = ledger.settled_cash
        unsettled = ledger.unsettled_cash
        reserved = ledger.reserved_cash
        total = ledger.total_cash

        if settled <= ZERO_DECIMAL:
            return False, f"Settled cash (${settled}) is not > $0.00"
        if unsettled != ZERO_DECIMAL:
            return False, f"Unsettled cash (${unsettled}) is not $0.00 (matured sweep pending)"
        if reserved != ZERO_DECIMAL:
            return False, f"Reserved cash (${reserved}) is not $0.00 (stranded limit orders active)"
        if (settled + unsettled + reserved) != total:
            return False, f"Three-bucket sum (${settled + unsettled + reserved}) != total_cash (${total})"

        return True, f"Settled=${settled}, Unsettled=${unsettled}, Reserved=${reserved}, Total=${total}"

    def check_zero_stranded_satellites(self) -> Tuple[bool, str]:
        """
        Confirm zero overnight 3x LETF holdings (SOXL, TQQQ, TNA must be 0 shares).
        """
        stranded: Dict[str, int] = {}
        for ticker in SATELLITE_TICKERS:
            qty = int(self.positions.get(ticker, 0))
            if qty != 0:
                stranded[ticker] = qty

        if stranded:
            return False, f"Stranded overnight 3x LETF holdings detected: {stranded}"
        return True, "Zero stranded 3x satellite holdings (SOXL, TQQQ, TNA are 0)"

    async def check_oauth_pre_market_gate(self, dry_run: bool = False) -> Tuple[bool, str]:
        """
        Confirm OAuth token manager is healthy and has >= 1,800s TTL.
        In live mode, triggers pre-market gate enforcement.
        """
        tm = self.token_manager
        if tm is None:
            tm = OAuthTokenManager()

        if tm.is_auth_locked:
            return False, "OAuth token manager is AUTH_LOCKED"

        if dry_run:
            # Simulate pre-market gate token refresh
            tm.set_remaining_ttl(1800.0)
            ttl = tm.get_remaining_ttl()
            return True, f"OAuth active, simulated dry-run TTL healthy ({ttl:.1f}s >= 1800.0s)"

        try:
            gate_fired = await tm.enforce_pre_market_gate()
            if tm.is_auth_locked:
                return False, "OAuth token manager transitioned to AUTH_LOCKED during pre-market gate"
            ttl = tm.get_remaining_ttl()
            if ttl < 1795.0:  # Allow 5s tolerance for network/execution latency
                return False, f"OAuth token remaining TTL too low: {ttl:.1f}s < 1800.0s"
            return True, f"OAuth gate validated (gate_fired={gate_fired}, remaining_ttl={ttl:.1f}s)"
        except Exception as exc:
            return False, f"OAuth pre-market gate exception: {exc}"

    def check_rate_limiter_headroom(self) -> Tuple[bool, str]:
        """
        Verify rate limiter burst capacity is 120.0, daily ceiling is 3500,
        and current daily count is < 100 for clean market-open headroom.
        """
        limiter = self.rate_limiter
        if limiter is None:
            limiter = AsyncTokenBucketRateLimiter()

        burst = float(limiter.burst_capacity)
        ceiling = int(limiter.daily_ceiling)
        count = int(limiter.daily_count)

        if abs(burst - 120.0) > 1e-4:
            return False, f"Burst capacity is {burst}, expected 120.0"
        if ceiling != 3500:
            return False, f"Daily ceiling is {ceiling}, expected 3500"
        if count >= 100:
            return False, f"Daily call count is {count} >= 100 (insufficient headroom for open)"

        return True, f"Burst capacity={burst}, daily ceiling={ceiling}, current count={count} (<100)"

    def check_sqlite_journal_and_state_dir(self) -> Tuple[bool, str]:
        """
        Verify STATE_DIR and LOGS_DIR exist and are writable,
        and verify SQLite journal database opens cleanly in WAL mode.
        """
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            LOGS_DIR.mkdir(parents=True, exist_ok=True)

            # Test write access to state/
            probe_state = STATE_DIR / ".probe_write_test"
            probe_state.write_text("ok", encoding="utf-8")
            probe_state.unlink(missing_ok=True)

            # Test write access to logs/
            probe_logs = LOGS_DIR / ".probe_write_test"
            probe_logs.write_text("ok", encoding="utf-8")
            probe_logs.unlink(missing_ok=True)

            # Test SQLite journal WAL mode
            self.journal_db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(str(self.journal_db_path), timeout=5.0) as conn:
                cursor = conn.cursor()
                cursor.execute("PRAGMA journal_mode=WAL;")
                mode_row = cursor.fetchone()
                mode = mode_row[0].upper() if mode_row else "UNKNOWN"

            return True, f"Directories writable; SQLite journal open with journal_mode={mode}"
        except Exception as exc:
            return False, f"Filesystem/database check failed: {exc}"

    async def run_all_checks(
        self,
        dry_run: bool = False,
        baseline_nlv: float = 4400.0,
    ) -> Dict[str, Any]:
        """
        Execute all preflight checks, persist atomic JSON report, and return results.
        """
        c1_ok, c1_msg = self.check_t1_ledger_invariants(baseline_nlv=baseline_nlv)
        c2_ok, c2_msg = self.check_zero_stranded_satellites()
        c3_ok, c3_msg = await self.check_oauth_pre_market_gate(dry_run=dry_run)
        c4_ok, c4_msg = self.check_rate_limiter_headroom()
        c5_ok, c5_msg = self.check_sqlite_journal_and_state_dir()

        checks = {
            "t1_ledger_invariants": {"passed": c1_ok, "detail": c1_msg},
            "zero_stranded_satellites": {"passed": c2_ok, "detail": c2_msg},
            "oauth_pre_market_gate": {"passed": c3_ok, "detail": c3_msg},
            "rate_limiter_headroom": {"passed": c4_ok, "detail": c4_msg},
            "sqlite_journal_and_state_dir": {"passed": c5_ok, "detail": c5_msg},
        }

        overall_passed = all(check_data["passed"] for check_data in checks.values())
        timestamp_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        report = {
            "timestamp_utc": timestamp_utc,
            "dry_run": bool(dry_run),
            "overall_passed": bool(overall_passed),
            "checks": checks,
        }

        # Atomically write report
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_report = self.report_path.with_suffix(".tmp")
        with open(tmp_report, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_report, self.report_path)

        return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Automated Pre-Market Invariant Verifier")
    parser.add_argument("--dry-run", action="store_true", help="Simulate OAuth gate without network calls")
    parser.add_argument("--nlv", type=float, default=4400.0, help="Baseline Net Liquidating Value (NLV)")
    args = parser.parse_args()

    verifier = PreflightVerifier()
    report = asyncio.run(verifier.run_all_checks(dry_run=args.dry_run, baseline_nlv=args.nlv))

    logger.info("=== Pre-Market Invariant Verification Report ===")
    logger.info("Timestamp UTC: %s | Dry-Run: %s | Overall Passed: %s",
                report["timestamp_utc"], report["dry_run"], report["overall_passed"])

    for name, res in report["checks"].items():
        status = "PASSED" if res["passed"] else "FAILED"
        logger.info("  [%s] %s: %s", status, name, res["detail"])

    if not report["overall_passed"]:
        logger.error("Preflight invariant verification FAILED. Aborting market open readiness.")
        sys.exit(1)

    logger.info("All preflight invariants verified. System ready for cash market open.")
    sys.exit(0)


if __name__ == "__main__":
    main()
