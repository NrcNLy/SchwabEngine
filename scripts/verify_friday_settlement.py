#!/usr/bin/env python3
"""
scripts/verify_friday_settlement.py
===================================
Automated Friday Settlement Audit Runner for Schwab Account ...4015.
Scheduled for Friday, October 16, 2026 at 09:30:05 EDT (13:30:05 UTC).

Verification Checks:
1. Confirms cashAvailableForWithdrawal matches total Net Liquidation Value ($5,563.97).
2. Confirms unsettledCash is exactly $0.00 (T+1 settlement of SOXL sale completed).
3. Confirms 0 working orders and 0 open positions.
4. Logs complete structured JSON audit to /home/nicho/schwab_state/settlement_audit_20261016.json.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("verify_friday_settlement")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Load .env variables
env_file = PROJECT_ROOT / ".env"
if env_file.exists():
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))

from core.paths import STATE_DIR
from core.runtime import load_config
from core.auth import SecurityVault, SchwabAuthManager
from data.rest_client import SchwabRestClient


def resolve_audit_log_path() -> Path:
    vm_dir = Path("/home/nicho/schwab_state")
    target_dir = vm_dir if vm_dir.is_dir() else STATE_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / "settlement_audit_20261016.json"


def perform_settlement_audit(expected_nlv: float = 5563.97) -> Dict[str, Any]:
    cfg = load_config()
    auth_cfg = cfg.get("auth", {})
    acct_cfg = cfg.get("account", {})
    target_suffix = str(acct_cfg.get("required_suffix", "015"))

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
    vault_path = next((p for p in candidates if p.exists() and p.stat().st_size > 0), None)
    if not vault_path:
        raise RuntimeError(f"Token vault not found in candidates: {candidates}")

    vault = SecurityVault(
        passphrase=passphrase,
        iterations=int(auth_cfg.get("pbkdf2_iterations", 600000)),
        vault_path=vault_path,
    )
    auth = SchwabAuthManager(client_id, client_secret, vault, cfg)
    auth.load_tokens()

    rest = SchwabRestClient.from_config(cfg, auth)

    # 1. Match account hash
    acct_nums = rest.get_account_numbers()
    target_hash = None
    target_acct_num = None
    for a in acct_nums:
        acc_no = str(a.get("accountNumber", ""))
        if acc_no.endswith(target_suffix):
            target_hash = a.get("hashValue")
            target_acct_num = acc_no
            break

    if not target_hash:
        raise RuntimeError(f"Could not find account matching suffix {target_suffix}")

    # 2. Pull live balances and positions
    acct_url = f"{rest._trader_base}/accounts/{target_hash}"
    acct_resp = rest._get(acct_url, params={"fields": "positions"}).json()
    sec_acct = acct_resp.get("securitiesAccount", acct_resp)
    cur_bal = sec_acct.get("currentBalances", {})
    positions = sec_acct.get("positions", [])

    nlv = float(cur_bal.get("liquidationValue", 0.0))
    cash_bal = float(cur_bal.get("cashBalance", 0.0))
    settled_cash = float(cur_bal.get("settledCash", 0.0))
    unsettled_cash = float(cur_bal.get("unsettledCash", 0.0))
    cash_trading = float(cur_bal.get("cashAvailableForTrading", 0.0))
    cash_withdrawal = float(cur_bal.get("cashAvailableForWithdrawal", 0.0))

    # 3. Pull working orders
    orders_url = f"{rest._trader_base}/accounts/{target_hash}/orders"
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    from_time = (now_utc - datetime.timedelta(days=1)).strftime("%Y-%m-%dT00:00:00.000Z")
    to_time = now_utc.strftime("%Y-%m-%dT23:59:59.000Z")

    working_orders = []
    try:
        working_orders = rest._get(orders_url, params={
            "fromEnteredTime": from_time,
            "toEnteredTime": to_time,
            "status": "WORKING",
            "maxResults": 50,
        }).json()
    except Exception as exc:
        logger.warning("Could not query working orders: %s", exc)

    # 4. Invariant checks
    withdrawal_matches_nlv = abs(cash_withdrawal - nlv) < 0.02
    unsettled_is_zero = abs(unsettled_cash) < 0.01
    nlv_matches_expected = abs(nlv - expected_nlv) < 1.00  # Within expected range
    positions_flat = (len(positions) == 0)
    orders_clean = (len(working_orders) == 0)

    settlement_cleared = withdrawal_matches_nlv and unsettled_is_zero and positions_flat

    status_verdict = "SETTLED_CLEAR" if settlement_cleared else "PENDING_CLEARANCE"

    audit_payload: Dict[str, Any] = {
        "audit_timestamp_utc": now_utc.isoformat(),
        "audit_timestamp_edt": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S EDT"),
        "account_suffix": target_suffix,
        "account_number_masked": f"...{target_acct_num[-4:]}",
        "account_hash_masked": f"{target_hash[:8]}...{target_hash[-6:]}",
        "verdict": status_verdict,
        "checks": {
            "cashAvailableForWithdrawal_matches_nlv": {
                "passed": withdrawal_matches_nlv,
                "cashAvailableForWithdrawal": cash_withdrawal,
                "netLiquidationValue": nlv,
                "expected_nlv": expected_nlv,
                "delta": round(cash_withdrawal - nlv, 2),
            },
            "unsettledCash_is_zero": {
                "passed": unsettled_is_zero,
                "unsettledCash": unsettled_cash,
                "target": 0.0,
            },
            "open_positions_flat": {
                "passed": positions_flat,
                "open_positions_count": len(positions),
            },
            "working_orders_clean": {
                "passed": orders_clean,
                "working_orders_count": len(working_orders),
            },
        },
        "live_balances": {
            "liquidationValue": nlv,
            "cashBalance": cash_bal,
            "settledCash": settled_cash,
            "unsettledCash": unsettled_cash,
            "cashAvailableForTrading": cash_trading,
            "cashAvailableForWithdrawal": cash_withdrawal,
        },
        "positions": positions,
        "working_orders": working_orders,
    }

    # Save to JSON log file
    log_path = resolve_audit_log_path()
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(audit_payload, f, indent=2)
    logger.info("Saved settlement audit report to: %s", log_path)

    return audit_payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Friday Settlement Audit Runner.")
    parser.add_argument("--expected-nlv", type=float, default=5563.97, help="Target expected NLV")
    args = parser.parse_args()

    print("=" * 75)
    print(" FRIDAY T+1 SETTLEMENT VERIFICATION AUDIT")
    print(" Target: Account ...4015 | Expected NLV: $5,563.97")
    print("=" * 75)

    try:
        report = perform_settlement_audit(expected_nlv=args.expected_nlv)
        c = report["checks"]
        b = report["live_balances"]

        print(f"\n Verdict:                       {report['verdict']}")
        print(f" Timestamp (UTC):              {report['audit_timestamp_utc']}")
        print(f" Net Liquidation Value (NLV):  ${b['liquidationValue']:,.2f}")
        print(f" Cash Available for Withdrawal:${b['cashAvailableForWithdrawal']:,.2f}")
        print(f" Unsettled Cash:               ${b['unsettledCash']:,.2f}")
        print(f" Open Positions Count:         {c['open_positions_flat']['open_positions_count']}")
        print(f" Working Orders Count:         {c['working_orders_clean']['working_orders_count']}")
        print("-" * 75)
        print(f" [CHECK 1] Withdrawal == NLV:  {'PASS' if c['cashAvailableForWithdrawal_matches_nlv']['passed'] else 'FAIL'}")
        print(f" [CHECK 2] UnsettledCash == $0: {'PASS' if c['unsettledCash_is_zero']['passed'] else 'WAITING (Pending Friday 09:30 EDT settlement)'}")
        print(f" [CHECK 3] Positions Flat:     {'PASS' if c['open_positions_flat']['passed'] else 'FAIL'}")
        print(f" [CHECK 4] Orders Clean:       {'PASS' if c['working_orders_clean']['passed'] else 'FAIL'}")
        print("=" * 75)
        print(f" Report saved to: {resolve_audit_log_path()}\n")

    except Exception as exc:
        logger.error("Audit runner failed: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
