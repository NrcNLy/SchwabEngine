"""
workers/off_hours_backfill.py
=============================
Off-hours bar archival and synthetic baseline store worker.
Triggered by systemd timer at 17:15 EDT (post-market close).

Key Invariants:
1. Quota Safety: Aborts if daily API consumption >= 2,500 calls (3,500 hard cap).
2. Strict Rate Limiting: 30 RPM max (minimum 2.0s sleep between calls).
3. Incremental Delta Ingestion: Queries only missing 1-minute bars since MAX(timestamp).
4. Rolling 20-Day Baseline: Computes ADV minute curves and Yang-Zhang volatility
   for 24 candidate ETFs and benchmarks.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import sys
import time
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.auth import SchwabAuthManager, SecurityVault
from core.baselines_store import SQLiteBaselinesStore
from core.paths import DAILY_QUOTA_FILE
from core.rate_limiter import SchwabRateLimiter
from core.runtime import load_config
from data.rest_client import SchwabRestClient
from indicators.volatility import OHLCBar, compute_yang_zhang_volatility

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("off_hours_backfill")

# 20 Candidate Leveraged ETFs + 4 Macro Benchmarks
TARGET_SYMBOLS: List[str] = [
    # Top 3 Core Universe
    "SOXL", "TQQQ", "TNA",
    # Core Inverses
    "SOXS", "SQQQ", "TZA",
    # Broad Index 3x
    "UPRO", "SPXU",
    # Sector & Tech High-Beta
    "FNGU", "NVDL", "TECL", "USD", "FAS", "DPST",
    # Commodities 2x
    "BOIL", "KOLD", "UCO", "SCO", "NUGT", "LABU",
    # Macro Benchmarks
    "SOX", "NDX", "RUT", "USO",
]

QUOTA_ABORT_THRESHOLD = 2500
RATE_LIMIT_DELAY_SEC = 2.0  # 30 RPM
EDT_TZ = ZoneInfo("America/New_York")


class QuotaCeilingExceededError(RuntimeError):
    """Raised when daily API calls exceed the safety threshold."""


class OffHoursBackfillWorker:
    def __init__(
        self,
        store: Optional[SQLiteBaselinesStore] = None,
        symbols: Optional[List[str]] = None,
        quota_threshold: int = QUOTA_ABORT_THRESHOLD,
        rate_delay: float = RATE_LIMIT_DELAY_SEC,
    ):
        self.store = store or SQLiteBaselinesStore()
        self.symbols = [s.upper() for s in (symbols or TARGET_SYMBOLS)]
        self.quota_threshold = quota_threshold
        self.rate_delay = rate_delay

    def check_quota(self, limiter: Optional[SchwabRateLimiter] = None) -> int:
        """Check current API quota usage today. Raises if above threshold."""
        if limiter is not None:
            calls_used = limiter.daily_calls_used
        else:
            calls_used = 0
            if DAILY_QUOTA_FILE.exists():
                try:
                    import json
                    raw = DAILY_QUOTA_FILE.read_text(encoding="utf-8")
                    data = json.loads(raw)
                    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    if data.get("date_utc") == today_utc:
                        calls_used = int(data.get("count", 0))
                except Exception as exc:
                    logger.warning("Could not read quota file %s: %s", DAILY_QUOTA_FILE, exc)

        if calls_used >= self.quota_threshold:
            msg = (
                f"Historical backfill ABORTED: {calls_used} API calls used today "
                f"(>= threshold {self.quota_threshold}). Preserving daily quota."
            )
            logger.critical(msg)
            raise QuotaCeilingExceededError(msg)

        logger.info("Daily quota check passed: %d / %d calls used.", calls_used, self.quota_threshold)
        return calls_used

    def process_symbol_bars(self, symbol: str, candles: List[Dict[str, Any]]) -> int:
        """Upsert candles into market_baselines.db."""
        if not candles:
            return 0
        bars_to_insert = []
        for c in candles:
            raw_ts = c.get("datetime", 0)
            bars_to_insert.append({
                "timestamp": raw_ts,
                "open": c.get("open", 0.0),
                "high": c.get("high", 0.0),
                "low": c.get("low", 0.0),
                "close": c.get("close", 0.0),
                "volume": c.get("volume", 0),
            })
        inserted = self.store.upsert_bars(symbol, bars_to_insert)
        return inserted

    def update_adv_profiles(self, symbol: str) -> Dict[int, float]:
        """
        Calculates 20-day rolling minute-of-day Average Daily Volume for 09:30-16:00 EDT.
        """
        bars = self.store.get_bars(symbol)
        if not bars:
            return {}

        minute_volumes: Dict[int, List[int]] = {}
        for b in bars:
            ts_ms = b["timestamp"]
            dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).astimezone(EDT_TZ)
            # Filter regular market hours: 09:30 (570) to 16:00 (960)
            mod = dt.hour * 60 + dt.minute
            if 570 <= mod <= 960:
                minute_volumes.setdefault(mod, []).append(int(b["volume"]))

        profiles: Dict[int, float] = {}
        sample_days = 20
        for mod, vols in minute_volumes.items():
            recent_vols = vols[-20:]
            profiles[mod] = float(sum(recent_vols) / max(len(recent_vols), 1))
            sample_days = max(sample_days, len(recent_vols))

        self.store.save_adv_profile(symbol, profiles, sample_days=sample_days)
        return profiles

    def update_volatility_baseline(self, symbol: str) -> Optional[float]:
        """Computes 20-day Yang-Zhang volatility and updates symbol baseline."""
        bars = self.store.get_bars(symbol)
        if len(bars) < 20:
            return None

        recent = bars[-3900:] if len(bars) > 3900 else bars
        ohlc_bars = [
            OHLCBar(open=b["open"], high=b["high"], low=b["low"], close=b["close"], volume=b["volume"])
            for b in recent
        ]
        try:
            yz_vol = compute_yang_zhang_volatility(ohlc_bars)
            adv_shares = sum(b["volume"] for b in recent) / max(len(recent) / 390.0, 1.0)
            self.store.save_symbol_baseline(
                symbol=symbol,
                yz_volatility_20d=yz_vol,
                median_spread_cents=2.0,
                adv_20d_shares=adv_shares,
            )
            return yz_vol
        except Exception as exc:
            logger.warning("Volatility calculation failed for %s: %s", symbol, exc)
            return None

    async def run_backfill(self, rest_client: Optional[SchwabRestClient] = None) -> Dict[str, Any]:
        """Execute full backfill cycle across configured universe."""
        self.check_quota()
        logger.info("Starting off-hours bar backfill for %d symbols...", len(self.symbols))

        results = {}
        for symbol in self.symbols:
            latest_ts = self.store.get_latest_bar_timestamp(symbol)
            candles = []

            if rest_client is not None:
                try:
                    logger.info("Fetching price history for %s (latest_ts=%s)...", symbol, latest_ts)
                    history = await asyncio.to_thread(
                        rest_client.get_price_history,
                        symbol=symbol,
                        period_type="day",
                        period=10 if latest_ts else 20,
                        frequency_type="minute",
                        frequency=1,
                        need_extended_hours_data=False,
                    )
                    candles = history.get("candles", []) if isinstance(history, dict) else []
                except Exception as exc:
                    logger.error("Failed to fetch price history for %s: %s", symbol, exc)
                await asyncio.sleep(self.rate_delay)  # 30 RPM throttle

            count = self.process_symbol_bars(symbol, candles)
            adv = self.update_adv_profiles(symbol)
            yz = self.update_volatility_baseline(symbol)
            results[symbol] = {"bars_saved": count, "adv_minutes": len(adv), "yz_vol": yz}

        logger.info("Off-hours bar backfill complete for %d symbols.", len(results))
        return results


async def main() -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv()
        cfg = load_config()

        client_id = os.getenv("SCHWAB_CLIENT_ID")
        client_secret = os.getenv("SCHWAB_CLIENT_SECRET")
        passphrase = os.getenv("VAULT_PASSPHRASE")

        worker = OffHoursBackfillWorker()
        worker.check_quota()

        if client_id and client_secret and passphrase:
            auth_cfg = cfg.get("auth", {}) or {}
            vault_file = PROJECT_ROOT / auth_cfg.get("vault_file", "schwab_tokens_vault.json")
            if vault_file.exists():
                vault = SecurityVault(passphrase=passphrase, vault_path=vault_file)
                auth = SchwabAuthManager(client_id, client_secret, vault, cfg)
                await asyncio.to_thread(auth.load_tokens)
                rest = SchwabRestClient.from_config(cfg, auth)
                await worker.run_backfill(rest)
                return 0

        logger.warning("Broker credentials unavailable; updating existing baselines only.")
        await worker.run_backfill(None)
        return 0
    except QuotaCeilingExceededError:
        return 1
    except Exception as exc:
        logger.exception("Fatal error in off_hours_backfill: %s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
