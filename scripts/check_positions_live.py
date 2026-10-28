import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.paths import STATE_DIR
from core.runtime import load_config
from core.auth import SecurityVault, SchwabAuthManager
from data.rest_client import SchwabRestClient

env_file = PROJECT_ROOT / ".env"
if env_file.exists():
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))

def main():
    cfg = load_config()
    auth_cfg = cfg.get("auth", {})
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
        print("[ERROR] Token vault not found.")
        sys.exit(1)

    vault = SecurityVault(
        passphrase=passphrase,
        iterations=int(auth_cfg.get("pbkdf2_iterations", 600000)),
        vault_path=vault_path,
    )
    auth = SchwabAuthManager(client_id, client_secret, vault, cfg)
    auth.load_tokens()

    rest = SchwabRestClient.from_config(cfg, auth)
    acct_nums = rest._get(f"{rest._trader_base}/accounts/accountNumbers", is_essential=True, action="CRITICAL_EXIT").json()
    target_hash = None
    for entry in acct_nums:
        if str(entry.get("accountNumber", "")).endswith("015"):
            target_hash = entry.get("hashValue")
            break
    if not target_hash:
        target_hash = acct_nums[0].get("hashValue")

    resp = rest._get(f"{rest._trader_base}/accounts/{target_hash}?fields=positions", is_essential=True, action="CRITICAL_EXIT")
    sec_acct = resp.json().get("securitiesAccount", {})
    balances = sec_acct.get("currentBalances", {})
    print("Account NLV:", balances.get("liquidationValue"))
    print("Settled Cash:", balances.get("settledCash"))
    positions = sec_acct.get("positions", [])
    print("Schwab Actual Positions Count:", len(positions))
    for p in positions:
        print("  Symbol:", p.get("instrument", {}).get("symbol"), "Qty:", p.get("longQuantity"), "MktVal:", p.get("marketValue"))

if __name__ == "__main__":
    main()
