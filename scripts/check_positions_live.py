from core.runtime import load_config
from core.auth import SchwabAuthManager
from data.rest_client import SchwabRestClient

def main():
    cfg = load_config()
    auth = SchwabAuthManager.from_config(cfg)
    rest = SchwabRestClient.from_config(cfg, auth)
    hash_val = cfg["account"]["account_hash"]
    resp = rest._get(f"{rest._trader_base}/accounts/{hash_val}?fields=positions", is_essential=True, action="CRITICAL_EXIT")
    sec_acct = resp.json().get("securitiesAccount", {})
    balances = sec_acct.get("currentBalances", {})
    print("Account NLV:", balances.get("liquidationValue"))
    print("Settled Cash:", balances.get("settledCash"))
    positions = sec_acct.get("positions", [])
    print("Schwab Positions Count:", len(positions))
    for p in positions:
        print("  Symbol:", p.get("instrument", {}).get("symbol"), "Qty:", p.get("longQuantity"), "MktVal:", p.get("marketValue"))

if __name__ == "__main__":
    main()
