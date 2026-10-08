import asyncio
import os
import yaml
from pathlib import Path
from core.auth import SchwabAuthManager, SecurityVault
from core.dynamic_scanner import DynamicScanner
from data.rest_client import SchwabRestClient
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
        # Load config
        with open('config.yaml', 'r') as f:
            cfg = yaml.safe_load(f)
            
        vault = SecurityVault()
        auth = SchwabAuthManager(cfg, vault)
        # Ensure we are authenticated
        await asyncio.to_thread(auth._refresh_tokens_if_needed)
        access_token = vault.get_access_token()
        if not access_token:
            logger.error("No access token available.")
            return

        scanner = DynamicScanner(auth_token=access_token)
        
        symbols = list(EXPANDED_UNIVERSE.keys())
        
        logger.info(f"Fetching quotes for {len(symbols)} symbols...")
        quotes = await scanner.fetch_batch_quotes(symbols)
        
        # We need prior close prices to calculate scores, but if we don't have them
        # DynamicScanner falls back to closePrice from the quote
        
        # Patch the fundamental data with our expanded universe constants so scanner can use them
        for sym, data in quotes.items():
            if 'fundamental' not in data:
                data['fundamental'] = {}
            data['fundamental']['avg10DaysVolume'] = EXPANDED_UNIVERSE[sym]['adv_20d_shares']
            
            # Since median spread is an input to score calculation, and dynamic_scanner.py
            # calculates spread from quote ask-bid, we don't need to inject it.
            
        scores = scanner.calculate_selection_scores(quotes, {})
        logger.info("Scores calculated:")
        for score, sym in scores:
            logger.info(f"{sym}: {score:.4f}")
            
        top_3 = [sym for score, sym in scores[:3]]
        logger.info(f"Top 3 Universe: {top_3}")
        
        logger.info("Fetching target history for Top 3...")
        volumes = await scanner.fetch_target_history(top_3)
        logger.info("History caching complete. Engine calibration ready.")
        
    except Exception as e:
        logger.error(f"Calibration failed: {e}")

if __name__ == "__main__":
    asyncio.run(calibrate_universe())
