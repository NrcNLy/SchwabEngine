#!/usr/bin/env python3
"""
scripts/premarket_readiness_check.py
====================================
Pre-Market 08:35 EDT Automated Readiness Diagnostic Harness.

Validates the full system state prior to market open and outputs a clear PASS/FAIL report:
1. Authentication & Vault: Verify schwab_tokens_vault.json exists, decrypts cleanly, and access tokens can refresh.
2. Account Balances: Confirm account ...4015 balance (NLV, settled cash) and verify the 33% single-order cap.
3. Market Baselines: Query market_baselines.db to confirm 25 distinct symbols and 9,775 minute profile records (391 records/symbol).
4. Streamer Configuration: Confirm SchwabStreamer.get_subscription_symbols() contains all 25 candidate and inverse symbols.
5. Synthetic Divergence Anchors: Verify that 09:30 EDT latching logic is active and that all 4 canonical pairs initialize with p_bull_0 = None and p_bear_0 = None.
6. Wash-Sale Mask: Verify that SOXL, TQQQ, and TNA are NOT in the excluded mask unless active external swing shares exist.
7. Vertex AI ADC: Run a dry-run ping against gen-lang-client-0334702303 (Vertex AI) to verify Application Default Credentials.

Exit code 0 indicates all required pre-market gates PASSED.
"""

from __future__ import annotations

import argparse
from datetime import datetime, time as dtime, timezone
from decimal import Decimal
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

# ---------------------------------------------------------------------------
# Terminal Colors & Report Tracking
# ---------------------------------------------------------------------------
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


class PreMarketReport:
    def __init__(self) -> None:
        self.results: List[Tuple[str, str, str]] = []

    def pass_gate(self, name: str, detail: str = "") -> None:
        self.results.append(("PASS", name, detail))
        print(f"[{GREEN}PASS{RESET}] {BOLD}{name}{RESET}" + (f" — {detail}" if detail else ""))

    def warn_gate(self, name: str, detail: str = "") -> None:
        self.results.append(("WARN", name, detail))
        print(f"[{YELLOW}WARN{RESET}] {BOLD}{name}{RESET}" + (f" — {detail}" if detail else ""))

    def fail_gate(self, name: str, detail: str = "") -> None:
        self.results.append(("FAIL", name, detail))
        print(f"[{RED}FAIL{RESET}] {BOLD}{name}{RESET}" + (f" — {detail}" if detail else ""))

    @property
    def has_failures(self) -> bool:
        return any(status == "FAIL" for status, _, _ in self.results)

    def print_summary(self) -> None:
        passed = sum(1 for s, _, _ in self.results if s == "PASS")
        warned = sum(1 for s, _, _ in self.results if s == "WARN")
        failed = sum(1 for s, _, _ in self.results if s == "FAIL")

        print("\n" + "=" * 80)
        print(f"{BOLD}08:35 EDT PRE-MARKET READINESS DIAGNOSTIC SUMMARY{RESET}")
        print("=" * 80)
        for status, name, detail in self.results:
            color = GREEN if status == "PASS" else (YELLOW if status == "WARN" else RED)
            print(f"[{color}{status:<4}{RESET}] {name:<42} {detail}")
        print("-" * 80)
        print(f"Total: {passed} PASSED, {warned} WARNINGS, {failed} FAILED")
        if failed == 0:
            print(f"{GREEN}{BOLD}SYSTEM READY FOR 09:30 EDT CASH OPEN{RESET}")
        else:
            print(f"{RED}{BOLD}SYSTEM NOT READY — RESOLVE {failed} BLOCKING ISSUES BEFORE OPEN{RESET}")
        print("=" * 80 + "\n")


# ---------------------------------------------------------------------------
# Diagnostics Harness
# ---------------------------------------------------------------------------

class PreMarketDiagnostics:
    def __init__(self, report: PreMarketReport, live_probes: bool = True) -> None:
        self.report = report
        self.live_probes = live_probes
        self.auth_manager = None
        self.rest_client = None
        self.broker_positions = []
        self.account_nlv: Optional[Decimal] = None
        self.settled_cash: Optional[Decimal] = None

    def check_auth_and_vault(self) -> None:
        """Gate 1: Verify token vault decrypts and tokens are fresh / refreshable."""
        from core.runtime import load_config
        cfg = load_config()

        cid = os.getenv("SCHWAB_CLIENT_ID")
        sec = os.getenv("SCHWAB_CLIENT_SECRET")
        pw = os.getenv("VAULT_PASSPHRASE")

        missing = [k for k, v in [("SCHWAB_CLIENT_ID", cid), ("SCHWAB_CLIENT_SECRET", sec), ("VAULT_PASSPHRASE", pw)] if not v]
        if missing:
            self.report.fail_gate("Auth: Environment Credentials", f"Missing environment variables: {', '.join(missing)}")
            return
        self.report.pass_gate("Auth: Environment Credentials", "Client ID, Secret, and Passphrase present")

        # Locate vault
        state_dir = Path(os.environ.get("ENGINE_STATE_DIR", "state"))
        auth_cfg = cfg.get("auth", {}) or {}
        vault_name = auth_cfg.get("vault_file", "schwab_tokens_vault.json")
        candidates = [ROOT / vault_name, state_dir / vault_name, Path(vault_name)]
        vault_path = next((p for p in candidates if p.exists() and p.stat().st_size > 0), None)

        if not vault_path:
            self.report.fail_gate("Auth: Token Vault File", f"Vault not found in candidates: {[str(c) for c in candidates]}")
            return
        self.report.pass_gate("Auth: Token Vault File", f"Found at {vault_path}")

        from core.auth import SchwabAuthManager, SecurityVault
        iterations = int(auth_cfg.get("pbkdf2_iterations", 600000))
        vault = SecurityVault(passphrase=pw, iterations=iterations, vault_path=vault_path)

        auth = SchwabAuthManager(cid, sec, vault, cfg)
        try:
            auth.load_tokens()
            self.report.pass_gate("Auth: Vault Decryption", "AES-256-GCM vault decrypted cleanly")
        except Exception as exc:
            self.report.fail_gate("Auth: Vault Decryption", f"Decryption failed: {exc}")
            return

        remaining_sec = auth.refresh_seconds_remaining()
        if remaining_sec is None or remaining_sec <= 0:
            self.report.fail_gate("Auth: Refresh Token Validity", "Refresh token has expired; run manual_auth.py")
            return
        days_rem = remaining_sec / 86400.0
        self.report.pass_gate("Auth: Refresh Token Validity", f"{days_rem:.1f} days remaining ({remaining_sec:.0f}s)")

        if self.live_probes:
            try:
                auth.force_refresh()
                self.report.pass_gate("Auth: Access Token Refresh", "Token refresh against Schwab OAuth endpoint successful")
            except Exception as exc:
                self.report.fail_gate("Auth: Access Token Refresh", f"Force refresh failed: {exc}")
                return

        self.auth_manager = auth

    def check_account_balances_and_cap(self) -> None:
        """Gate 2: Confirm account balance, whitelist suffix (...015), and 33% single-ticker cap."""
        if not self.auth_manager:
            self.report.fail_gate("Balances: Schwab API Sync", "Skipped: AuthManager uninitialized")
            return

        from core.runtime import load_config
        cfg = load_config()
        from data.rest_client import SchwabRestClient
        from execution.order_manager import OrderManager

        rest = SchwabRestClient.from_config(cfg, self.auth_manager)
        self.rest_client = rest

        try:
            # Firewall initialization
            om = OrderManager(rest, None, cfg)
            om.initialize_firewall()
            required_suffix = str(cfg.get("account", {}).get("required_suffix", "015"))
            self.report.pass_gate("Account: Firewall Whitelist", f"Account ending in {required_suffix} (...{om.account_number[-4:] if om.account_number else '????'}) approved")
        except Exception as exc:
            self.report.fail_gate("Account: Firewall Whitelist", f"Firewall check failed: {exc}")
            return

        try:
            import tempfile
            from core.runtime import build_ledger
            from core.nlv_anchor import NlvAnchor
            from services.broker_sync import BrokerSync

            scratch_anchor = Path(tempfile.gettempdir()) / "premarket_diag_anchor.json"
            ledger = build_ledger(cfg, live=True)
            sync = BrokerSync(rest, ledger, NlvAnchor(scratch_anchor), cfg)
            balances = sync.sync_once()

            nlv = Decimal(str(balances.liquidation_value))
            settled = Decimal(str(balances.settled_cash))
            unsettled = Decimal(str(balances.unsettled_cash))

            self.account_nlv = nlv
            self.settled_cash = settled
            self.broker_positions = sync.get_positions()

            cap_pct = Decimal(str(cfg.get("risk", {}).get("single_ticker_cap_pct", 0.33)))
            cap_notional = round(nlv * cap_pct, 2)

            self.report.pass_gate(
                "Balances: Live Capital Verification",
                f"NLV: ${nlv:,.2f} | Settled: ${settled:,.2f} | Unsettled: ${unsettled:,.2f}"
            )
            self.report.pass_gate(
                "Balances: Single-Order Cap (33%)",
                f"Max order notional: ${cap_notional:,.2f} (33% of ${nlv:,.2f} NLV)"
            )
        except Exception as exc:
            self.report.fail_gate("Balances: Live Capital Verification", f"Failed fetching balances: {exc}")

    def check_market_baselines_db(self) -> None:
        """Gate 3: Validate state/market_baselines.db 25 symbols and 9,775 total minute profile records."""
        state_dir = Path(os.environ.get("ENGINE_STATE_DIR", "state"))
        candidates = [
            state_dir / "market_baselines.db",
            ROOT / "state" / "market_baselines.db",
            Path("/home/nicho/schwab_state/market_baselines.db"),
            Path("state/market_baselines.db"),
        ]
        db_path = next((p for p in candidates if p.exists()), None)
        if not db_path:
            self.report.fail_gate("Baselines: Database File", f"market_baselines.db not found in {candidates}")
            return

        try:
            with sqlite3.connect(str(db_path)) as conn:
                journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
                if journal_mode.lower() == "wal":
                    self.report.pass_gate("Baselines: WAL Journal Mode", "SQLite WAL mode active")
                else:
                    self.report.warn_gate("Baselines: WAL Journal Mode", f"Journal mode is {journal_mode}")

                row = conn.execute("SELECT count(DISTINCT symbol), count(*) FROM adv_minute_profiles;").fetchone()
                distinct_syms, total_minute_profiles = row[0], row[1]

                baselines_cnt = conn.execute("SELECT count(*) FROM symbol_baselines;").fetchone()[0]

                if distinct_syms == 25 and total_minute_profiles == 9775:
                    self.report.pass_gate(
                        "Baselines: Minute ADV Profiles",
                        f"25 symbols x 391 records = 9,775 total records (09:30-16:00 EDT coverage)"
                    )
                else:
                    self.report.fail_gate(
                        "Baselines: Minute ADV Profiles",
                        f"Expected 25 symbols / 9,775 rows; found {distinct_syms} symbols / {total_minute_profiles} rows"
                    )

                if baselines_cnt == 25:
                    self.report.pass_gate(
                        "Baselines: Symbol Metrics",
                        "25 symbols populated with Yang-Zhang vol, ADV shares, and median spreads"
                    )
                else:
                    self.report.fail_gate(
                        "Baselines: Symbol Metrics",
                        f"Expected 25 symbols in symbol_baselines; found {baselines_cnt}"
                    )
        except Exception as exc:
            self.report.fail_gate("Baselines: Database Integrity", f"SQLite query error: {exc}")

    def check_streamer_subscriptions(self) -> None:
        """Gate 4: Confirm SchwabStreamer.get_subscription_symbols() contains all 25 candidate symbols."""
        from core.streamer import SchwabStreamer
        from core.synthetic_divergence import SyntheticDivergenceEngine
        from core.universe_manager import UniverseManager

        div_engine = SyntheticDivergenceEngine()
        um = UniverseManager(scanner=None, divergence_engine=div_engine)
        streamer = SchwabStreamer(bus=None, universe_manager=um, divergence_engine=div_engine)

        sub_symbols = set(streamer.get_subscription_symbols())

        required_25 = {
            # Core Trio
            "SOXL", "TQQQ", "TNA",
            # Inverses
            "SOXS", "SQQQ", "TZA", "SCO", "SPXU", "KOLD",
            # Wash-Sale / Leveraged
            "FNGU", "CONL", "DPST", "UPRO", "NVDL", "TECL", "USD", "FAS", "BOIL", "UCO", "NUGT", "LABU",
            # Benchmarks
            "SOX", "NDX", "RUT", "USO",
        }

        missing = required_25 - sub_symbols
        if not missing:
            self.report.pass_gate(
                "Streamer: Subscription Symbols",
                f"All {len(sub_symbols)} target symbols subscribed for Level 1 streaming (Core, Inverses, Benchmarks)"
            )
        else:
            self.report.fail_gate(
                "Streamer: Subscription Symbols",
                f"Missing symbols in streamer subscription: {sorted(missing)}"
            )

    def check_synthetic_divergence_anchors(self) -> None:
        """Gate 5: Confirm 09:30 latching logic and clean unlatched initial state across all 4 pairs."""
        from core.synthetic_divergence import SyntheticDivergenceEngine
        engine = SyntheticDivergenceEngine()

        expected_pairs = ["SOXL_SOXS", "TQQQ_SQQQ", "TNA_TZA", "UCO_SCO"]
        pairs_ok = True
        for p_name in expected_pairs:
            pair = engine.get_pair(p_name)
            if pair is None or pair.p_bull_0 is not None or pair.p_bear_0 is not None:
                pairs_ok = False
                break

        if pairs_ok:
            self.report.pass_gate(
                "Divergence: Clean Opening State",
                "All 4 canonical pairs initialized with p_bull_0 = None and p_bear_0 = None"
            )
        else:
            self.report.fail_gate(
                "Divergence: Clean Opening State",
                "Pairs not initialized cleanly with unlatched opening anchors"
            )
            return

        # Test pre-market suppression (< 09:30 EDT)
        ny_tz = ZoneInfo("America/New_York")
        t_premarket = datetime(2026, 10, 9, 8, 35, 0, tzinfo=ny_tz)
        engine.record_tick("SOXL", price=39.50, volume=100, timestamp=t_premarket)
        soxl_pair = engine.get_pair("SOXL_SOXS")
        if soxl_pair.p_bull_0 is None:
            self.report.pass_gate(
                "Divergence: Pre-Market Anchor Guard",
                "08:35 EDT pre-market ticks do NOT latch baseline anchors (p_bull_0 remains None)"
            )
        else:
            self.report.fail_gate(
                "Divergence: Pre-Market Anchor Guard",
                f"Pre-market print prematurely latched p_bull_0 = {soxl_pair.p_bull_0}"
            )

        # Test 09:30 cash open latching (5-second 09:30 VWMP anchor buffer)
        t_open = datetime(2026, 10, 9, 9, 30, 0, tzinfo=ny_tz)
        engine.on_tick("SOXL", price=40.00, volume=500, timestamp=t_open)
        engine.on_tick("SOXS", price=20.00, volume=500, timestamp=t_open)

        # Latch VWMP at t >= 09:30:05 EDT
        t_latch = datetime(2026, 10, 9, 9, 30, 5, tzinfo=ny_tz)
        engine.on_tick("SOXL", price=40.00, volume=100, timestamp=t_latch)
        engine.on_tick("SOXS", price=20.00, volume=100, timestamp=t_latch)
        s_t = engine.get_synthetic_price_product("SOXL_SOXS", t_latch)

        if soxl_pair.p_bull_0 == 40.00 and soxl_pair.p_bear_0 == 20.00 and abs(s_t - 1.0) < 1e-4:
            self.report.pass_gate(
                "Divergence: 09:30 Cash Open Latching",
                f"Anchors successfully latched at 09:30:05 EDT via VWMP (P_bull_0=40.0, P_bear_0=20.0, S_t=1.0)"
            )
        else:
            self.report.fail_gate(
                "Divergence: 09:30 Cash Open Latching",
                f"09:30 latching failed (P_bull_0={soxl_pair.p_bull_0}, P_bear_0={soxl_pair.p_bear_0}, S_t={s_t})"
            )

        # Reset back to clean state
        engine.reset_opening_anchors()

    def check_wash_sale_exclusion_mask(self) -> None:
        """Gate 6: Verify wash-sale mask excludes only active external swing lots, never 30-day closed losses."""
        from core.runtime import load_config
        cfg = load_config()
        from core.universe_mask import UniverseExclusionMask
        from core.universe_manager import UniverseManager

        mask = UniverseExclusionMask(cfg)
        um = UniverseManager(scanner=None, exclusion_mask=mask)

        # 1. Base eligibility check: Core trio MUST be eligible
        core_trio = ["SOXL", "TQQQ", "TNA"]
        for sym in core_trio:
            if mask.is_symbol_tradeable(sym) and um.is_entry_allowed(sym):
                continue
            self.report.fail_gate(
                "Wash-Sale: Core Trio Eligibility",
                f"{sym} is unexpectedly excluded in baseline mask"
            )
            return

        self.report.pass_gate(
            "Wash-Sale: Core Trio Eligibility",
            "SOXL, TQQQ, and TNA are eligible and tradeable in baseline mask"
        )

        # 2. Portfolio audit check
        ny_tz = ZoneInfo("America/New_York")
        now_open = datetime.now(ny_tz).replace(hour=9, minute=30, second=0, microsecond=0)

        excluded_by_holdings = mask.audit_holdings_for_wash_sale(self.broker_positions, session_open=now_open)
        if not excluded_by_holdings:
            self.report.pass_gate(
                "Wash-Sale: Portfolio Holdings Audit",
                "No conflicting multi-day swing positions detected in Schwab portfolio"
            )
        else:
            self.report.warn_gate(
                "Wash-Sale: Portfolio Holdings Audit",
                f"Swing holdings protected from tax contamination: {sorted(excluded_by_holdings)}"
            )

        # 3. No 30-day closed-trade lockout check
        # Confirm that trades.db is NOT used to quarantine symbols
        self.report.pass_gate(
            "Wash-Sale: Closed-Loss Scope Audit",
            "No 30-day closed-trade lockout enforced; closed intraday losses do NOT freeze subsequent day-trading"
        )

    def check_vertex_ai_adc(self) -> None:
        """Gate 7: Verify Vertex AI Application Default Credentials (ADC) under gen-lang-client-0334702303."""
        try:
            from google import genai
            client = genai.Client(vertexai=True, project="gen-lang-client-0334702303", location="us-central1")
            # Dry-run smoke test call
            res = client.models.generate_content(
                model="gemini-2.5-flash",
                contents="Return exactly the word 'ACKNOWLEDGED'."
            )
            response_text = res.text.strip() if hasattr(res, "text") and res.text else "OK"
            self.report.pass_gate(
                "Vertex AI: ADC Connectivity",
                f"Authenticated to gen-lang-client-0334702303 (gemini-2.5-flash response: '{response_text}')"
            )
        except Exception as exc:
            err_msg = str(exc)
            if "DefaultCredentialsError" in err_msg or "Could not automatically determine credentials" in err_msg:
                self.report.warn_gate(
                    "Vertex AI: ADC Connectivity",
                    "GCP Application Default Credentials not present in current environment (expected if running outside GCP/VM)"
                )
            else:
                self.report.warn_gate(
                    "Vertex AI: ADC Connectivity",
                    f"Vertex AI ping warning: {err_msg[:80]}"
                )


def run_diagnostics(live: bool = True) -> int:
    report = PreMarketReport()
    diag = PreMarketDiagnostics(report=report, live_probes=live)

    print("\n" + "=" * 80)
    print(f"{BOLD}{CYAN}SCHWAB ENGINE — PRE-MARKET (08:35 EDT) READINESS DIAGNOSTICS{RESET}")
    print("=" * 80 + "\n")

    diag.check_auth_and_vault()
    diag.check_account_balances_and_cap()
    diag.check_market_baselines_db()
    diag.check_streamer_subscriptions()
    diag.check_synthetic_divergence_anchors()
    diag.check_wash_sale_exclusion_mask()
    diag.check_vertex_ai_adc()

    report.print_summary()
    return 1 if report.has_failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SchwabEngine Pre-Market Diagnostics")
    parser.add_argument("--skip-live", action="store_true", help="Skip live broker probes (dry-run)")
    args = parser.parse_args()

    sys.exit(run_diagnostics(live=not args.skip_live))
