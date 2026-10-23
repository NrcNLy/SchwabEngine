import asyncio, json, sys, yaml
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.runtime import load_config
from core.auth import SecurityVault, SchwabAuthManager
from core.paths import STRATEGY_CONFIG_FILE
from data.multi_rate_sync import MultiRateStateSynchronizer

async def verify_live_broker_link():
    print("Testing Schwab API read-only account hydration...")
    cfg = load_config()
    with open(STRATEGY_CONFIG_FILE) as f:
        strat_cfg = json.load(f)

    auth = SchwabAuthManager(cfg)
    token = await auth.get_access_token()
    print(f"Access Token Verified (length: {len(token)} chars)")

    sync = MultiRateStateSynchronizer(config=cfg, strategy_config=strat_cfg, auth_manager=auth)
    account_state = await sync.poll_warm_plane()

    acct_num = account_state.get("account_number", account_state.get("account_hash", "MASKED"))
    nlv = float(account_state.get("net_liquidation_value", account_state.get("liquidation_value", 0.0)))
    cash = float(account_state.get("settled_cash", account_state.get("cash_balance", 0.0)))
    positions = list(account_state.get("positions", {}).keys())
    sat_wt = float(account_state.get("current_satellite_weight", 0.0)) * 100
    lower_b = strat_cfg["leland_ntz"]["drift_lower_cutoff"] * 100
    upper_b = strat_cfg["leland_ntz"]["drift_upper_cutoff"] * 100

    print("\n--- Live Broker Telemetry ---")
    print(f"Account Identifier: {acct_num}")
    print(f"Net Liquidation Value: ${nlv:,.2f}")
    print(f"Cash Balance (SWVXX/Settled): ${cash:,.2f}")
    print(f"Active Positions: {positions}")
    print(f"Current Sat Weight: {sat_wt:.2f}%")
    print(f"Leland NTZ Bounds: [{lower_b:.1f}%, {upper_b:.1f}%]")
    print("-----------------------------\n")

if __name__ == "__main__":
    # Live broker smoke script: run manually (python tests/test_broker_sync.py); never executed on pytest import.
    asyncio.run(verify_live_broker_link())
