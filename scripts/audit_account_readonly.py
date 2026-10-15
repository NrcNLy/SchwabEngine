#!/usr/bin/env python3
"""
scripts/audit_account_readonly.py
=================================
Strictly read-only auditor script for Schwab account ...4015.
Pulls live balances, positions, orders, and transaction history from Schwab API.
Queries SQLite ledger and state files.
NO state mutations, NO order placements or cancellations.
"""
import os
import sys
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.paths import STATE_DIR, RECOVERY_PLAN_FILE, RECOVERY_STATUS_FILE
from core.runtime import load_config
from core.auth import SecurityVault, SchwabAuthManager
from data.rest_client import SchwabRestClient

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


def main():
    print("=" * 80)
    print(" SCHWAB ACCOUNT & LEDGER READ-ONLY AUDIT")
    print(f" Timestamp (UTC): {datetime.now(timezone.utc).isoformat()}")
    print("=" * 80)

    # 1. Load config and auth
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
        Path(vault_name),
    ]
    vault_path = next((p for p in candidates if p.exists() and p.stat().st_size > 0), None)
    if not vault_path:
        print(f"[ERROR] Token vault not found.")
        sys.exit(1)

    vault = SecurityVault(
        passphrase=passphrase,
        iterations=int(auth_cfg.get("pbkdf2_iterations", 600000)),
        vault_path=vault_path,
    )
    auth = SchwabAuthManager(client_id, client_secret, vault, cfg)
    auth.load_tokens()

    rest = SchwabRestClient.from_config(cfg, auth)

    # 2. Get Account Numbers and match target suffix
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
        print(f"[ERROR] Could not find account matching suffix {target_suffix}")
        sys.exit(1)

    print(f"\n[1] ACCOUNT IDENTIFICATION:")
    print(f"  Account Number: ...{target_acct_num[-4:]}")
    print(f"  Account Hash:   {target_hash[:10]}...{target_hash[-6:]}")

    # 3. Pull Account Details (Balances & Positions)
    # GET /trader/v1/accounts/{accountHash}?fields=positions
    acct_url = f"{rest._trader_base}/accounts/{target_hash}"
    acct_resp = rest._get(acct_url, params={"fields": "positions"}).json()
    sec_acct = acct_resp.get("securitiesAccount", acct_resp)
    cur_bal = sec_acct.get("currentBalances", {})
    init_bal = sec_acct.get("initialBalances", {})
    proj_bal = sec_acct.get("projectedBalances", {})
    positions = sec_acct.get("positions", [])

    print(f"\n[2] LIVE SCHWAB BALANCES (currentBalances):")
    print(f"  Net Liquidation Value (NLV):  ${float(cur_bal.get('liquidationValue', 0.0)):,.2f}")
    print(f"  Cash Balance:                 ${float(cur_bal.get('cashBalance', 0.0)):,.2f}")
    print(f"  Settled Cash:                 ${float(cur_bal.get('settledCash', 0.0)):,.2f}")
    print(f"  Unsettled Cash:               ${float(cur_bal.get('unsettledCash', 0.0)):,.2f}")
    print(f"  Cash Available for Trading:   ${float(cur_bal.get('cashAvailableForTrading', 0.0)):,.2f}")
    print(f"  Cash Available for Withdrawal:${float(cur_bal.get('cashAvailableForWithdrawal', 0.0)):,.2f}")
    print(f"  Cash Debit / Credit Call:     ${float(cur_bal.get('cashDebitCallValue', 0.0)):,.2f}")
    print(f"  Money Market (SWVXX/etc):     ${float(cur_bal.get('moneyMarketFund', 0.0)):,.2f}")

    print(f"\n[3] POSITIONS AUDIT:")
    print(f"  Total Open Positions Count: {len(positions)}")
    if positions:
        for p in positions:
            sym = p.get("instrument", {}).get("symbol")
            qty = p.get("longQuantity", 0) - p.get("shortQuantity", 0)
            mkt_val = p.get("marketValue", 0.0)
            print(f"    - {sym}: {qty} shares (Market Value: ${mkt_val:,.2f})")
    else:
        print("  --> ALL POSITIONS FLAT (0 shares open). SOXL is confirmed closed.")

    print(f"\n[2.1] RAW CURRENT BALANCES:")
    for k, v in sorted(cur_bal.items()):
        print(f"  {k:<35}: {v}")

    if proj_bal:
        print(f"\n[2.2] RAW PROJECTED BALANCES:")
        for k, v in sorted(proj_bal.items()):
            print(f"  {k:<35}: {v}")

    # 4. Working & Today's Orders
    orders_url = f"{rest._trader_base}/accounts/{target_hash}/orders"
    today_from = "2026-10-15T00:00:00.000Z"
    today_to = "2026-10-15T23:59:59.000Z"

    working_orders = []
    try:
        working_orders = rest._get(orders_url, params={
            "fromEnteredTime": today_from,
            "toEnteredTime": today_to,
            "status": "WORKING",
            "maxResults": 50,
        }).json()
    except Exception as exc:
        print(f"  [WARN] Failed to query working orders with status=WORKING: {exc}")

    print(f"\n[4] WORKING ORDERS AUDIT:")
    print(f"  Active Working Orders Count: {len(working_orders)}")
    if working_orders:
        for o in working_orders:
            print(f"    - Order ID {o.get('orderId')}: {o.get('orderType')} {o.get('status')}")
    else:
        print("  --> NO WORKING ORDERS (Order book is confirmed clean).")

    all_recent_orders = []
    try:
        all_recent_orders = rest._get(orders_url, params={
            "fromEnteredTime": today_from,
            "toEnteredTime": today_to,
            "maxResults": 50,
        }).json()
    except Exception as exc:
        print(f"  [WARN] Failed to query recent orders: {exc}")

    print(f"\n[5] TODAY'S RECENT EXECUTED / RECENT ORDERS ({len(all_recent_orders)} total):")
    for o in all_recent_orders[:10]:
        oid = o.get("orderId")
        status = o.get("status")
        otype = o.get("orderType")
        qty = o.get("filledQuantity", o.get("quantity"))
        legs = o.get("orderLegCollection", [])
        sym = legs[0].get("instrument", {}).get("symbol") if legs else "N/A"
        instruction = legs[0].get("instruction") if legs else "N/A"
        entered_time = o.get("enteredTime", "")
        close_time = o.get("closeTime", "")
        price = o.get("price", "MKT")
        acts = o.get("orderActivityCollection", [])
        exec_price = "N/A"
        if acts:
            exec_price = acts[0].get("executionLegs", [{}])[0].get("price", "N/A")
        print(f"  Order {oid}: {instruction} {sym} x{qty} | Type: {otype} | Status: {status} | Executed: ${exec_price} | Entered: {entered_time} | Closed: {close_time}")

    # 5. Transactions for today
    print(f"\n[6] TODAY'S TRANSACTIONS & CLEARING AUDIT (2026-10-15):")
    tx_url = f"{rest._trader_base}/accounts/{target_hash}/transactions"
    tx_resp = []
    try:
        tx_resp = rest._get(tx_url, params={
            "startDate": today_from,
            "endDate": today_to,
        }).json()
    except Exception as exc:
        print(f"  [WARN] Transactions query with dates failed: {exc}")
        try:
            tx_resp = rest._get(tx_url).json()
        except Exception as exc2:
            print(f"  [FAIL] Unparameterized transactions query also failed: {exc2}")

    if isinstance(tx_resp, list):
        print(f"  Found {len(tx_resp)} transaction(s) for today:")
        for tx in tx_resp:
            tx_id = tx.get("activityId")
            tx_date = tx.get("time") or tx.get("settlementDate") or tx.get("tradeDate")
            tx_type = tx.get("type")
            net_amt = tx.get("netAmount", 0.0)
            desc = tx.get("description", "")
            tx_item = tx.get("transactionItem", {})
            fees = tx.get("fees", {})
            print(f"  -------------------------------------------------------------")
            print(f"  Transaction ID: {tx_id}")
            print(f"  Timestamp:      {tx_date}")
            print(f"  Type:           {tx_type}")
            print(f"  Net Amount:     ${net_amt:,.2f}")
            print(f"  Description:    {desc}")
            if tx_item:
                inst = tx_item.get("instrument", {})
                sym = inst.get("symbol", "")
                qty = tx_item.get("amount", "")
                price = tx_item.get("price", "")
                cost = tx_item.get("cost", "")
                print(f"  Item Details:   {sym} | Qty: {qty} | Price: ${price} | Cost: ${cost}")
            if fees:
                print(f"  Fees:           {fees}")
    else:
        print(f"  Transactions response: {tx_resp}")

    # 6. Check internal SQLite and state files
    print(f"\n[7] INTERNAL ENGINE STATE & SQLITE RECONCILIATION:")
    trades_db_path = STATE_DIR / "trades.db"
    if trades_db_path.exists():
        conn = sqlite3.connect(trades_db_path)
        cur = conn.cursor()
        try:
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [r[0] for r in cur.fetchall()]
            print(f"  trades.db tables: {tables}")
            if "trades" in tables:
                cur.execute("SELECT symbol, side, quantity, price, realized_pnl, timestamp FROM trades ORDER BY timestamp DESC LIMIT 5")
                rows = cur.fetchall()
                print("  Recent trades from trades.db:")
                for r in rows:
                    print(f"    - {r[5]} | {r[0]} {r[1]} x{r[2]} @ ${r[3]} | PnL: ${r[4]}")
        except Exception as e:
            print(f"  Error reading trades.db: {e}")
        finally:
            conn.close()

    # Raw recovery_plan and recovery_status
    if RECOVERY_PLAN_FILE.exists():
        plan_data = json.loads(RECOVERY_PLAN_FILE.read_text())
        print(f"\n  recovery_plan.json: halt_new_entries={plan_data.get('halt_new_entries')}, floor={plan_data.get('floor_price')}")
    if RECOVERY_STATUS_FILE.exists():
        stat_data = json.loads(RECOVERY_STATUS_FILE.read_text())
        print(f"  recovery_status.json: state={stat_data.get('state')}, exit_latched={stat_data.get('exit_latched')}, exit_reason={stat_data.get('exit_reason')}")

    print("\n" + "=" * 80)
    print(" AUDIT EXECUTION COMPLETE")
    print("=" * 80)


if __name__ == "__main__":
    main()
