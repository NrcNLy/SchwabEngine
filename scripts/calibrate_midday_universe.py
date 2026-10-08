import asyncio
import os
import sys
from pathlib import Path
import yaml
import logging
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv()

from core.auth import SchwabAuthManager, SecurityVault
from core.dynamic_scanner import DynamicScanner

EXPANDED_UNIVERSE = {
    "SOXL": {"adv_20d_shares": 59_000_000, "median_spread_cents": 1.5},
    "SOXS": {"adv_20d_shares": 42_000_000, "median_spread_cents": 1.5},
    "TQQQ": {"adv_20d_shares": 50_000_000, "median_spread_cents": 1.0},
    "SQQQ": {"adv_20d_shares": 65_000_000, "median_spread_cents": 1.0},
    "TNA":  {"adv_20d_shares":  4_700_000, "median_spread_cents": 2.0},
    "TZA":  {"adv_20d_shares":  2_800_000, "median_spread_cents": 2.0},
    "UPRO": {"adv_20d_shares":  1_500_000, "median_spread_cents": 1.5},
    "SPXU": {"adv_20d_shares":  1_800_000, "median_spread_cents": 1.5},
    "FNGU": {"adv_20d_shares":  2_200_000, "median_spread_cents": 2.0},
    "NVDL": {"adv_20d_shares": 14_000_000, "median_spread_cents": 1.5},
    "TECL": {"adv_20d_shares":  1_200_000, "median_spread_cents": 2.5},
    "USD":  {"adv_20d_shares":    600_000, "median_spread_cents": 2.5},
    "FAS":  {"adv_20d_shares":  1_000_000, "median_spread_cents": 3.0},
    "DPST": {"adv_20d_shares":    850_000, "median_spread_cents": 4.0},
    "BOIL": {"adv_20d_shares":  3_500_000, "median_spread_cents": 1.5},
    "KOLD": {"adv_20d_shares":  2_100_000, "median_spread_cents": 2.0},
    "UCO":  {"adv_20d_shares":  2_900_000, "median_spread_cents": 1.5},
    "SCO":  {"adv_20d_shares":  1_400_000, "median_spread_cents": 2.0},
    "NUGT": {"adv_20d_shares":  1_900_000, "median_spread_cents": 2.0},
    "LABU": {"adv_20d_shares":  1_800_000, "median_spread_cents": 2.5},
}

async def calibrate_universe():
    try:
        if Path("/app").exists() and (Path("/app") / "config" / "config.yaml").exists():
            PROJECT_ROOT = Path("/app")
        else:
            PROJECT_ROOT = Path(__file__).resolve().parent.parent

        config_path = PROJECT_ROOT / "config" / "config.yaml"
        if not config_path.exists():
            config_path = Path("config/config.yaml")

        with open(config_path, "r") as f:
            cfg = yaml.safe_load(f)

        client_id = os.getenv("SCHWAB_CLIENT_ID")
        client_secret = os.getenv("SCHWAB_CLIENT_SECRET")
        passphrase = os.getenv("VAULT_PASSPHRASE")

        auth_cfg = cfg.get("auth", {}) or {}
        vault_file = auth_cfg.get("vault_file", "schwab_tokens_vault.json")
        vault_path = PROJECT_ROOT / vault_file
        if not vault_path.exists():
            vault_path = Path(vault_file)

        vault = SecurityVault(
            passphrase=passphrase,
            iterations=int(auth_cfg.get("pbkdf2_iterations", 600000)),
            vault_path=vault_path,
        )
        auth = SchwabAuthManager(client_id, client_secret, vault, cfg)
        await asyncio.to_thread(auth.load_tokens)
        access_token = await asyncio.to_thread(auth.get_access_token)
        if not access_token:
            logger.error("No access token available.")
            return

        db_path = str(PROJECT_ROOT / "data" / "historical_candles.db")
        scanner = DynamicScanner(auth_token=access_token, db_path=db_path)

        symbols = list(EXPANDED_UNIVERSE.keys())
        logger.info(f"Fetching quotes for {len(symbols)} symbols...")
        quotes = await scanner.fetch_batch_quotes(symbols)

        # Patch fundamental data with expanded universe ADV
        for sym, data in quotes.items():
            if 'fundamental' not in data:
                data['fundamental'] = {}
            if sym in EXPANDED_UNIVERSE:
                data['fundamental']['avg10DaysVolume'] = EXPANDED_UNIVERSE[sym]['adv_20d_shares']

        scores = scanner.calculate_selection_scores(quotes, {})
        logger.info("Scores calculated:")
        for score, sym in scores:
            logger.info(f"{sym}: {score:.4f}")

        top_3 = [sym for _, sym in scores[:3]]
        logger.info(f"Top 3 Universe: {top_3}")

        # Update config/config.yaml immediately
        with open("config/config.yaml", "r") as f:
            full_cfg = yaml.safe_load(f)

        full_cfg["engine"]["symbols"] = top_3

        with open("config/config.yaml", "w") as f:
            yaml.safe_dump(full_cfg, f, default_flow_style=False, sort_keys=False)

        print(f"[SUCCESS] Persisted engine.symbols to config/config.yaml: {top_3}")

        logger.info("Fetching target history for Top 3...")
        volumes = await scanner.fetch_target_history(top_3)
        logger.info("History caching complete. Engine calibration ready.")

    except Exception as e:
        logger.error(f"Calibration failed: {e}", exc_info=True)

if __name__ == "__main__":
    asyncio.run(calibrate_universe())
