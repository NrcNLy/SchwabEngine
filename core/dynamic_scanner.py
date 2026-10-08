import asyncio
import sqlite3
import time
from typing import Any, Dict, List, Tuple
import httpx
from datetime import datetime
from zoneinfo import ZoneInfo
import logging

logger = logging.getLogger(__name__)

class TokenBucket:
    def __init__(self, capacity: int = 20, refill_rate: float = 1.667):
        self.capacity = capacity
        self.tokens = float(capacity)
        self.refill_rate = refill_rate
        self.last_refill = time.monotonic()
        self.lock = asyncio.Lock()

    async def consume(self, amount: int = 1) -> None:
        async with self.lock:
            while True:
                now = time.monotonic()
                elapsed = now - self.last_refill
                self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
                self.last_refill = now

                if self.tokens >= amount:
                    self.tokens -= amount
                    return

                wait_time = (amount - self.tokens) / self.refill_rate
                await asyncio.sleep(wait_time)

class DynamicScanner:
    def __init__(self, auth_token: str, db_path: str = "data/historical_candles.db"):
        self.auth_token = auth_token
        self.db_path = db_path
        self.base_url = "https://api.schwabapi.com"
        self.rate_limiter = TokenBucket()
        self._init_db()

    def _init_db(self) -> None:
        from pathlib import Path
        db_p = Path(self.db_path)
        db_p.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(db_p)) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS historical_1m (
                    symbol TEXT,
                    timestamp INTEGER,
                    open REAL, high REAL, low REAL, close REAL, volume INTEGER,
                    PRIMARY KEY (symbol, timestamp)
                ) WITHOUT ROWID;
            """)

    async def fetch_batch_quotes(self, symbols: List[str]) -> Dict[str, Any]:
        """Fetch quotes in chunks of 20 (max for Schwab quotes API)."""
        headers = {"Authorization": f"Bearer {self.auth_token}"}
        all_quotes = {}
        
        async with httpx.AsyncClient(headers=headers, timeout=20.0) as session:
            for i in range(0, len(symbols), 20):
                chunk = symbols[i:i+20]
                await self.rate_limiter.consume(1)
                url = f"{self.base_url}/marketdata/v1/quotes"
                params = {"symbols": ",".join(chunk)}
                
                resp = await session.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
                all_quotes.update(data)
                    
        return all_quotes

    def calculate_selection_scores(self, quotes: Dict[str, Any], prior_closes: Dict[str, float]) -> List[Tuple[float, str]]:
        """
        Calculate scores using: score = (alpha * norm_gap) + (beta * norm_rvol_d) - (gamma * norm_spread)
        alpha=0.40, beta=0.45, gamma=0.15
        """
        alpha = 0.40
        beta = 0.45
        gamma = 0.15

        metrics = {}
        for sym, quote_data in quotes.items():
            try:
                # Assuming quote_data contains quote dict and fundamental dict
                if 'quote' not in quote_data: continue
                
                last_price = float(quote_data['quote'].get('lastPrice', 0))
                if last_price == 0: continue
                
                ask_price = float(quote_data['quote'].get('askPrice', 0))
                bid_price = float(quote_data['quote'].get('bidPrice', 0))
                spread_pct = (ask_price - bid_price) / last_price if last_price > 0 else 0
                
                # Gap calculation
                prev_close = prior_closes.get(sym)
                if not prev_close:
                    prev_close = float(quote_data['quote'].get('closePrice', last_price))
                    
                gap = abs(last_price - prev_close) / prev_close if prev_close > 0 else 0
                
                # RVOL calculation (Proxying using total volume and ADV)
                vol = float(quote_data['quote'].get('totalVolume', 0))
                
                adv_shares = float(quote_data.get('fundamental', {}).get('avg10DaysVolume', 1_000_000))
                if adv_shares == 0: adv_shares = 1_000_000
                adv_dollar = adv_shares * prev_close
                
                rvol_d = (vol * last_price) / max(adv_dollar, 1.0)
                
                metrics[sym] = {
                    "gap": gap,
                    "rvol_d": rvol_d,
                    "spread": spread_pct
                }
            except Exception as e:
                logger.error(f"Error calculating metrics for {sym}: {e}")
                continue

        if not metrics:
            return []

        gaps = [m["gap"] for m in metrics.values()]
        rvols = [m["rvol_d"] for m in metrics.values()]
        spreads = [m["spread"] for m in metrics.values()]

        min_g, max_g = min(gaps), max(gaps)
        min_r, max_r = min(rvols), max(rvols)
        min_s, max_s = min(spreads), max(spreads)

        scores = []
        for sym, m in metrics.items():
            norm_gap = (m["gap"] - min_g) / max(max_g - min_g, 1e-8)
            norm_rvol = (m["rvol_d"] - min_r) / max(max_r - min_r, 1e-8)
            norm_spread = (m["spread"] - min_s) / max(max_s - min_s, 1e-8)

            score = (alpha * norm_gap) + (beta * norm_rvol) - (gamma * norm_spread)
            scores.append((score, sym))

        scores.sort(reverse=True)
        return scores

    def _persist_history(self, sym: str, candles: List[Dict[str, Any]]) -> None:
        with sqlite3.connect(self.db_path) as conn:
            records = [
                (sym, c["datetime"], c["open"], c["high"], c["low"], c["close"], c.get("volume", 0))
                for c in candles
            ]
            conn.executemany("""
                INSERT OR IGNORE INTO historical_1m (symbol, timestamp, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, records)
            conn.commit()

    async def fetch_target_history(self, top_symbols: List[str]) -> Dict[str, Dict[int, float]]:
        """Fetches 10-day history, persists to DB, and returns minute-of-day volumes for StrategyEngine initialization."""
        headers = {"Authorization": f"Bearer {self.auth_token}"}
        baseline_volumes = {}
        
        async with httpx.AsyncClient(headers=headers, timeout=30.0) as session:
            for sym in top_symbols:
                await self.rate_limiter.consume(1)
                url = f"{self.base_url}/marketdata/v1/pricehistory"
                params = {
                    "symbol": sym, "periodType": "day", "period": 10,
                    "frequencyType": "minute", "frequency": 1, "needExtendedHoursData": "false"
                }
                resp = await session.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
                candles = data.get("candles", [])
                self._persist_history(sym, candles)
                    
                minute_vols = {}
                minute_counts = {}
                for c in candles:
                    # Enforce Eastern Time zone awareness to prevent Docker UTC offset bug
                    dt = datetime.fromtimestamp(c["datetime"] / 1000.0, tz=ZoneInfo("America/New_York"))
                    mod = dt.hour * 60 + dt.minute
                    minute_vols[mod] = minute_vols.get(mod, 0) + c.get("volume", 0)
                    minute_counts[mod] = minute_counts.get(mod, 0) + 1
                    
                baseline_volumes[sym] = {mod: minute_vols[mod] / count for mod, count in minute_counts.items()}
                    
        return baseline_volumes
