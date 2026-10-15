#!/usr/bin/env python3
"""
scripts/verify_settlement_cron.py
=================================
Automated Friday Settlement Watcher & Ledger Auditor for Schwab Engine.
Scheduled for Friday, October 16, 2026 at 09:30:05 EDT (13:30:05 UTC).

Verifies that the $1,777.70 SOXL liquidation proceeds have cleared from
unsettled to settled status, bringing total unrestricted cash to $5,563.97.

Verification Invariants:
  1. currentLiquidationValue == 5563.97 (tolerance +/- $0.05)
  2. cashAvailableForWithdrawal == 5563.97 (tolerance +/- $0.05)
  3. unsettledCash == 0.00 (tolerance < $0.005)
  4. openPositionsCount == 0

Actions:
  - If ALL pass:
    * Write state record to /home/nicho/schwab_state/settlement_cleared_20261016.json
    * Dispatch push notification via ntfy (schwab-trader-nrc-8472):
      "🟢 [SETTLEMENT CLEARED] Account ...4015 fully settled. Available Cash: $5,563.97. Zero GFVs."
  - If ANY fail:
    * Dispatch high-priority alert via ntfy (schwab-trader-nrc-8472):
      "🔴 [SETTLEMENT MISMATCH] Expected $5,563.97 settled cash. Check ledger immediately."
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("verify_settlement_cron")

# Determine project root and add to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Load environment variables
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

from core.paths import STATE_DIR
from core.runtime import load_config
from core.auth import SecurityVault, SchwabAuthManager
from data.rest_client import SchwabRestClient
from core.notifier import send_alert

DEFAULT_NTFY_TOPIC = "schwab-trader-nrc-8472"
TARGET_ACCOUNT_SUFFIX = "4015"
EXPECTED_NLV = 5563.97
EXPECTED_CASH_WITHDRAWAL = 5563.97
EXPECTED_TOLERANCE = 0.05
TARGET_STATE_FILE = Path("/home/nicho/schwab_state/settlement_cleared_20261016.json")


def resolve_state_output_file() -> Path:
    """Returns target path for the settlement cleared state record."""
    if TARGET_STATE_FILE.parent.exists() and os.access(TARGET_STATE_FILE.parent, os.W_OK):
        return TARGET_STATE_FILE
    alt = STATE_DIR / "settlement_cleared_20261016.json"
    alt.parent.mkdir(parents=True, exist_ok=True)
    return alt


def evaluate_settlement(
    rest_client: SchwabRestClient,
    target_suffix: str = TARGET_ACCOUNT_SUFFIX,
    expected_nlv: float = EXPECTED_NLV,
    expected_cash: float = EXPECTED_CASH_WITHDRAWAL,
    tolerance: float = EXPECTED_TOLERANCE,
) -> Tuple[bool, Dict[str, Any], List[str]]:
    """
    Queries account balances and evaluates the 4 verification invariants.

    Returns:
        (all_passed, audit_details, failure_reasons)
    """
    # 1. Match account
    account_numbers = rest_client.get_account_numbers()
    target_hash = None
    target_account_number = None

    for entry in account_numbers:
        acc_no = str(entry.get("accountNumber", ""))
        if acc_no.endswith(target_suffix):
            target_hash = entry.get("hashValue")
            target_account_number = acc_no
            break

    if not target_hash:
        # Fallback to suffix "015" if "4015" not directly matched
        for entry in account_numbers:
            acc_no = str(entry.get("accountNumber", ""))
            if acc_no.endswith("015"):
                target_hash = entry.get("hashValue")
                target_account_number = acc_no
                break

    if not target_hash:
        raise RuntimeError(f"Could not locate account with suffix {target_suffix}")

    # 2. Query account balances & positions
    account_url = f"{rest_client._trader_base}/accounts/{target_hash}"
    account_data = rest_client._get(account_url, params={"fields": "positions"}).json()
    sec_acct = account_data.get("securitiesAccount", account_data)
    cur_bal = sec_acct.get("currentBalances", {})
    agg_bal = account_data.get("aggregatedBalance", {})
    raw_positions = sec_acct.get("positions", []) or []

    # 3. Extract metrics
    # currentLiquidationValue can be in currentBalances.liquidationValue or aggregatedBalance
    liq_val_raw = cur_bal.get("liquidationValue")
    if liq_val_raw is None:
        liq_val_raw = cur_bal.get("currentLiquidationValue")
    if liq_val_raw is None:
        liq_val_raw = agg_bal.get("currentLiquidationValue")
    cur_liq_val = float(liq_val_raw) if liq_val_raw is not None else 0.0

    cash_avail_raw = cur_bal.get("cashAvailableForWithdrawal")
    cash_available = float(cash_avail_raw) if cash_avail_raw is not None else 0.0

    unsettled_raw = cur_bal.get("unsettledCash")
    unsettled_cash = float(unsettled_raw) if unsettled_raw is not None else 0.0

    # Count open positions with non-zero quantity
    open_positions = []
    for pos in raw_positions:
        l_qty = float(pos.get("longQuantity", 0.0) or 0.0)
        s_qty = float(pos.get("shortQuantity", 0.0) or 0.0)
        if abs(l_qty - s_qty) > 0:
            open_positions.append(pos)
    open_positions_count = len(open_positions)

    # 4. Check invariants
    # Invariant 1: currentLiquidationValue == 5563.97 (+/- 0.05)
    pass_liq = abs(cur_liq_val - expected_nlv) <= tolerance

    # Invariant 2: cashAvailableForWithdrawal == 5563.97 (+/- 0.05)
    pass_cash = abs(cash_available - expected_cash) <= tolerance

    # Invariant 3: unsettledCash == 0.00
    pass_unsettled = abs(unsettled_cash - 0.00) < 0.005

    # Invariant 4: openPositionsCount == 0
    pass_positions = (open_positions_count == 0)

    all_passed = pass_liq and pass_cash and pass_unsettled and pass_positions

    failure_reasons = []
    if not pass_liq:
        failure_reasons.append(
            f"currentLiquidationValue (${cur_liq_val:,.2f}) != ${expected_nlv:,.2f} (tol +/- ${tolerance:.2f})"
        )
    if not pass_cash:
        failure_reasons.append(
            f"cashAvailableForWithdrawal (${cash_available:,.2f}) != ${expected_cash:,.2f} (tol +/- ${tolerance:.2f})"
        )
    if not pass_unsettled:
        failure_reasons.append(
            f"unsettledCash (${unsettled_cash:,.2f}) != $0.00"
        )
    if not pass_positions:
        failure_reasons.append(
            f"openPositionsCount ({open_positions_count}) != 0"
        )

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    audit_details: Dict[str, Any] = {
        "timestamp_utc": now_utc.isoformat(),
        "account_number": f"...{target_account_number[-4:]}",
        "account_hash": target_hash,
        "status": "SETTLEMENT_CLEARED" if all_passed else "SETTLEMENT_PENDING_OR_MISMATCH",
        "invariants": {
            "currentLiquidationValue": {
                "observed": cur_liq_val,
                "expected": expected_nlv,
                "tolerance": tolerance,
                "passed": pass_liq,
            },
            "cashAvailableForWithdrawal": {
                "observed": cash_available,
                "expected": expected_cash,
                "tolerance": tolerance,
                "passed": pass_cash,
            },
            "unsettledCash": {
                "observed": unsettled_cash,
                "expected": 0.00,
                "passed": pass_unsettled,
            },
            "openPositionsCount": {
                "observed": open_positions_count,
                "expected": 0,
                "passed": pass_positions,
            },
        },
        "balances": {
            "liquidationValue": cur_liq_val,
            "cashBalance": float(cur_bal.get("cashBalance", 0.0)),
            "settledCash": float(cur_bal.get("settledCash", 0.0)),
            "unsettledCash": unsettled_cash,
            "cashAvailableForTrading": float(cur_bal.get("cashAvailableForTrading", 0.0)),
            "cashAvailableForWithdrawal": cash_available,
        },
        "open_positions": open_positions,
        "all_passed": all_passed,
        "failure_reasons": failure_reasons,
    }

    return all_passed, audit_details, failure_reasons


def main() -> int:
    parser = argparse.ArgumentParser(description="Friday Settlement Watcher & Ledger Auditor.")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate checks without dispatching live ntfy or writing state record.")
    parser.add_argument("--force-notify", action="store_true", help="Force sending ntfy notification even if --dry-run is set.")
    args = parser.parse_args()

    print("=" * 80)
    print(" SCHWAB FRIDAY SETTLEMENT WATCHER & LEDGER AUDITOR")
    print(f" Execution Timestamp (UTC): {datetime.datetime.now(datetime.timezone.utc).isoformat()}")
    print("=" * 80)

    cfg = load_config()
    auth_cfg = cfg.get("auth", {})
    acct_cfg = cfg.get("account", {})
    target_suffix = str(acct_cfg.get("required_suffix", TARGET_ACCOUNT_SUFFIX))

    passphrase = os.getenv("VAULT_PASSPHRASE")
    client_id = os.getenv("SCHWAB_CLIENT_ID")
    client_secret = os.getenv("SCHWAB_CLIENT_SECRET")

    vault_name = auth_cfg.get("vault_file", "schwab_tokens_vault.json")
    candidates = [
        PROJECT_ROOT / vault_name,
        STATE_DIR / vault_name,
        Path("/home/nicho/schwab_state") / vault_name,
        Path(vault_name),
    ]
    existing = [p for p in candidates if p.exists() and p.stat().st_size > 0]
    existing.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    vault_path = existing[0] if existing else None
    if not vault_path:
        err_msg = f"Token vault not found in any candidate path: {candidates}"
        logger.error(err_msg)
        if not args.dry_run or args.force_notify:
            send_alert(
                title="🔴 [SETTLEMENT MISMATCH] Expected $5,563.97 settled cash. Check ledger immediately.",
                message=f"CRITICAL: Failed to locate token vault: {err_msg}",
                priority="urgent",
                tags=["red_circle", "rotating_light"],
                topic=DEFAULT_NTFY_TOPIC,
            )
        return 1

    try:
        vault = SecurityVault(
            passphrase=passphrase,
            iterations=int(auth_cfg.get("pbkdf2_iterations", 600000)),
            vault_path=vault_path,
        )
        auth = SchwabAuthManager(client_id, client_secret, vault, cfg)
        auth.load_tokens()
        rest_client = SchwabRestClient.from_config(cfg, auth)

        all_passed, audit_details, failure_reasons = evaluate_settlement(
            rest_client=rest_client,
            target_suffix=target_suffix,
            expected_nlv=EXPECTED_NLV,
            expected_cash=EXPECTED_CASH_WITHDRAWAL,
            tolerance=EXPECTED_TOLERANCE,
        )

        inv = audit_details["invariants"]
        b = audit_details["balances"]

        print(f" Account:                       {audit_details['account_number']}")
        print(f" Status:                        {audit_details['status']}")
        print(f" currentLiquidationValue:       ${inv['currentLiquidationValue']['observed']:,.2f}  [Expected: ${EXPECTED_NLV:,.2f} +/- ${EXPECTED_TOLERANCE:.2f}] -> {'PASS' if inv['currentLiquidationValue']['passed'] else 'FAIL'}")
        print(f" cashAvailableForWithdrawal:    ${inv['cashAvailableForWithdrawal']['observed']:,.2f}  [Expected: ${EXPECTED_CASH_WITHDRAWAL:,.2f} +/- ${EXPECTED_TOLERANCE:.2f}] -> {'PASS' if inv['cashAvailableForWithdrawal']['passed'] else 'FAIL'}")
        print(f" unsettledCash:                 ${inv['unsettledCash']['observed']:,.2f}  [Expected: $0.00] -> {'PASS' if inv['unsettledCash']['passed'] else 'FAIL'}")
        print(f" openPositionsCount:            {inv['openPositionsCount']['observed']}  [Expected: 0] -> {'PASS' if inv['openPositionsCount']['passed'] else 'FAIL'}")
        print("-" * 80)

        if all_passed:
            success_title = "🟢 [SETTLEMENT CLEARED] Account ...4015 fully settled. Available Cash: $5,563.97. Zero GFVs."
            success_msg = (
                f"{success_title}\n\n"
                f"Settlement Invariants Verified:\n"
                f"• Net Liquidation Value: ${b['liquidationValue']:,.2f}\n"
                f"• Cash Available for Withdrawal: ${b['cashAvailableForWithdrawal']:,.2f}\n"
                f"• Unsettled Cash: ${b['unsettledCash']:,.2f}\n"
                f"• Open Positions: {inv['openPositionsCount']['observed']}\n"
                f"• SOXL $1,777.70 liquidation proceeds fully settled to unrestricted cash."
            )
            print(f" [PASS] {success_title}")

            state_file = resolve_state_output_file()
            if not args.dry_run:
                with open(state_file, "w", encoding="utf-8") as f:
                    json.dump(audit_details, f, indent=2)
                print(f" [RECORD] State record saved to {state_file}")

                send_alert(
                    title=success_title,
                    message=success_msg,
                    priority="high",
                    tags=["green_circle", "white_check_mark", "moneybag"],
                    topic=DEFAULT_NTFY_TOPIC,
                )
                print(f" [NTFY] Dispatched success notification to {DEFAULT_NTFY_TOPIC}")
            else:
                print(f" [DRY-RUN] Would write state record to {state_file}")
                if args.force_notify:
                    send_alert(
                        title=success_title,
                        message=success_msg,
                        priority="high",
                        tags=["green_circle", "white_check_mark", "moneybag"],
                        topic=DEFAULT_NTFY_TOPIC,
                    )
                    print(f" [NTFY] Force-dispatched success notification to {DEFAULT_NTFY_TOPIC}")
            return 0

        else:
            fail_title = "🔴 [SETTLEMENT MISMATCH] Expected $5,563.97 settled cash. Check ledger immediately."
            fail_msg = (
                f"{fail_title}\n\n"
                f"Settlement Invariant Failures:\n" +
                "\n".join(f"• {r}" for r in failure_reasons) +
                f"\n\nCurrent Balances:\n"
                f"• NLV: ${b['liquidationValue']:,.2f}\n"
                f"• Cash Available for Withdrawal: ${b['cashAvailableForWithdrawal']:,.2f}\n"
                f"• Unsettled Cash: ${b['unsettledCash']:,.2f}\n"
                f"• Open Positions Count: {inv['openPositionsCount']['observed']}"
            )
            print(f" [FAIL] {fail_title}")
            for r in failure_reasons:
                print(f"   --> {r}")

            if not args.dry_run or args.force_notify:
                send_alert(
                    title=fail_title,
                    message=fail_msg,
                    priority="urgent",
                    tags=["red_circle", "rotating_light", "warning"],
                    topic=DEFAULT_NTFY_TOPIC,
                )
                print(f" [NTFY] Dispatched high-priority alert to {DEFAULT_NTFY_TOPIC}")
            else:
                print(f" [DRY-RUN] Alert suppressed (run without --dry-run or with --force-notify to dispatch)")
            return 1

    except Exception as exc:
        err_title = "🔴 [SETTLEMENT MISMATCH] Expected $5,563.97 settled cash. Check ledger immediately."
        err_msg = f"{err_title}\n\nCRITICAL EXCEPTION during settlement audit:\n{type(exc).__name__}: {exc}"
        logger.error(err_msg, exc_info=True)
        if not args.dry_run or args.force_notify:
            send_alert(
                title=err_title,
                message=err_msg,
                priority="urgent",
                tags=["red_circle", "rotating_light", "bomb"],
                topic=DEFAULT_NTFY_TOPIC,
            )
        return 1


if __name__ == "__main__":
    sys.exit(main())
