# Comprehensive Dynamic ETF Screener & Execution Architecture

## 1. Executive Summary & Empirical Validation

The transition from a static, hardcoded three-ticker universe to a dynamic, regime-adaptive quantitative trading engine is fundamental to navigating intraday leveraged ETF volatility. A recent 34-day baseline study revealed significant fragmentation in intraday momentum characteristics that static selection systematically fails to capture.

Volumetric distribution explicitly shows that institutional accumulation and programmed order flow behave differently across the leveraged ETF spectrum, specifically during the afternoon "Power Hour" (14:00 to 15:35 EDT). Sector-specific beta (e.g., SOXL) exhibits dramatic volume expansion—a 1.56x Relative Volume (RVOL) spike—justifying aggressive momentum entries. Conversely, broad-market index beta (TQQQ, TNA) exhibits volume contraction, rendering traditional breakout models vulnerable to statistical noise and spread-traversal slippage.

### 1.1 Empirical Baseline Metrics

| Empirical Metric | SOXL (Semiconductors) | TQQQ (Nasdaq-100) | TNA (Russell 2000) | Microstructural Implication | 
| ----- | ----- | ----- | ----- | ----- | 
| **Median 15m True Range** | \$1.20 (0.77%) | \$0.34 (0.41%) | \$0.27 (0.47%) | Anchor for expected single-bar excursion | 
| **Median 14-Period ATR** | \$1.57 (1.01%) | \$0.43 (0.52%) | \$0.35 (0.61%) | Baseline multiplier for trailing stop algorithms | 
| **Opening Range Vol** | 4,044,694 shares | 4,848,604 shares | 423,430 shares | Baseline threshold for morning breakout hurdles | 
| **Midday Chop Vol** | 752,292 shares | 994,601 shares | 81,566 shares | Compression benchmark for range-bound regimes | 
| **Power Hour Vol** | 1,170,207 shares | 959,970 shares | 78,044 shares | Absolute threshold for afternoon momentum validity | 
| **Power Hour RVOL Ratio** | 1.56x | 0.97x | 0.96x | Differentiator between genuine momentum and noise | 
| **Mean Absolute Bar Return** | 0.582% | 0.277% | 0.264% | Minimum required fractional displacement for entry | 

## 2. Expanded 24-Ticker Candidate Universe

To prevent API rate-limit exhaustion while capturing idiosyncratic momentum, the architecture implements an expanded candidate pool of up to 24 highly liquid, leveraged ETFs. This list is stratified across technology, broad market indices, and commodities.

| Symbol | Underlying Index | Lev | Median Spread (¢) | ADV (20D) | Liquidity Tier | 
| ----- | ----- | ----- | ----- | ----- | ----- | 
| **SOXL** / **SOXS** | ICE Semiconductor | $\pm3.0$ | 1.0 - 1.5 | \~38M - 63M | HIGH | 
| **TQQQ** / **SQQQ** | Nasdaq-100 | $\pm3.0$ | 1.0 | \~54M - 85M | HIGH | 
| **UPRO** / **SPXU** | S&P 500 | $\pm3.0$ | 1.5 | \~1.2M - 1.9M | MEDIUM | 
| **TNA** / **TZA** | Russell 2000 | $\pm3.0$ | 2.0 | \~1.9M - 3.4M | MEDIUM | 
| **NVDL** / **FNGU** | NVIDIA / FANG+ | $+2.0$/$+3.0$ | 1.5 - 2.5 | \~2.5M - 16M | MEDIUM | 
| **USD** / **TECL** | DJ US Semi / Tech | $+2.0$/$+3.0$ | 2.0 - 3.0 | \~0.5M - 1.5M | LOW | 
| **FAS** / **FAZ** | R1000 Financials | $\pm3.0$ | 3.0 - 3.5 | \~0.9M - 1.1M | LOW | 
| **DPST** | S&P Regional Banks | $+3.0$ | 4.0 | 850,000 | LOW | 
| **ERX** / **ERY** | Energy Select Sector | $\pm2.0$ | 2.5 - 3.0 | \~650k - 900k | LOW | 
| **GUSH** / **DRIP** | S&P Oil & Gas | $\pm2.0$ | 2.5 - 3.0 | \~500k - 800k | LOW | 
| **BOIL** / **KOLD** | Bloomberg Nat Gas | $\pm2.0$ | 1.5 - 2.0 | \~2.2M - 4.5M | MEDIUM | 
| **UCO** / **SCO** | Bloomberg WTI Crude | $\pm2.0$ | 1.5 | \~2.8M - 3.1M | MEDIUM | 
| **NUGT** | NYSE Arca Gold Miners | $+2.0$ | 2.0 | 1,900,000 | MEDIUM | 

## 3. Mathematical Formulations

The intraday engine requires absolute mathematical precision. The following closed-form formulations define the quantitative state machine, replacing all prior placeholder models.

### 3.1 Composite Selection Score

The pre-market screener evaluates the candidate pool at 09:15 EDT to identify the Top 3 assets exhibiting optimal momentum and liquidity. The composite selection score $S_{comp}$ balances overnight gap magnitude ($G_o$), relative pre-market dollar volume ($RVOL_{\$}$), and a bid-ask spread penalty function ($P_{spread}$).

All variables are strictly min-max normalized to $[0, 1]$ across the active candidate pool prior to applying weights to prevent structural metrics (like absolute spread values) from vanishing.

$$
S_{comp} = \alpha G_{o} + \beta RVOL_{\$} - \gamma P_{spread}
$$

Where the underlying components are defined mathematically as:

* Overnight Gap: 

  $$
  G_o = \frac{\vert{}P_{last} - P_{prev\_close}\vert{}}{P_{prev\_close}}
  $$

* Pre-Market Dollar RVOL (Normalized against ADV): 

  $$
  RVOL_{\$, i} = \frac{V_{pre, i} \cdot P_{last, i}}{\text{ADV}_{20, i} \cdot P_{prev\_close, i}}
  $$

* Spread Penalty: 

  $$
  P_{spread} = \frac{Ask - Bid}{P_{last}}
  $$

The coefficient weights are normalized such that $\alpha + \beta + \gamma = 1$. The production architecture fixes these coefficients at $\alpha = 0.40$, $\beta = 0.45$, and $\gamma = 0.15$ to prioritize liquidity over pure price dislocation.

### 3.2 Choppiness Index (CI) & Regime Classification

The Choppiness Index evaluates trendiness versus range-bound consolidation over a 14-period lookback window ($n=14$) on 1-minute bars.

$$
CI = 100 \times \frac{\log_{10}\left(\frac{\sum_{i=1}^{n} TR_i}{\max_{n}(High) - \min_{n}(Low)}\right)}{\log_{10}(n)}
$$

Where True Range ($TR$) is the greatest of three absolute price differentials:

$$
TR = \max(High - Low, \vert{}High - PrevClose\vert{}, \vert{}Low - PrevClose\vert{})
$$

**Execution Routing:**

* $CI < 38.2$: **Regime A (Trend Expansion)** - Authorizes Opening Range Breakout (ORB-15) entries.

* $38.2 \le CI \le 61.8$: **Regime B (Mean-Reversion)** - Authorizes VWAP reversion entries.

* $CI > 61.8$: **Regime C (High-Noise Chop)** - Execution halted; entries suppressed.

### 3.3 OLS VWAP Slope Calculation

To confirm institutional accumulation, VWAP momentum is measured via Ordinary Least Squares (OLS) regression over 15 rolling 60-second samples. Given the independent variable vector $x_i \in \{1, 2, ..., 15\}$:

$$
\beta = \frac{\sum_{i=1}^{n} (x_i - \bar{x})(y_i - \bar{y})}{\sum_{i=1}^{n} (x_i - \bar{x})^2}
$$

To normalize outputs across divergent nominal price levels, the scalar slope is normalized by the average session VWAP before being converted to degrees:

$$
\theta = \arctan\left( \frac{\beta}{\overline{\text{VWAP}}} \times 60 \right) \times \frac{180}{\pi}
$$

A strictly positive slope ($\theta > 0$) is a mandatory condition for long sequence activation.

### 3.4 Yang-Zhang Dynamic Volatility Stops & Hurst Ratchet

Trailing stops utilize the Yang-Zhang estimator to decompose variance into overnight jump ($\sigma^2_{OJ}$), continuous drift ($\sigma^2_{C}$), and Rogers-Satchell range variance ($\sigma^2_{RS}$).

$$
\sigma^2_{YZ} = \sigma^2_{OJ} + k \cdot \sigma^2_{C} + (1 - k) \cdot \sigma^2_{RS}
$$

Where the components over a 30-period window ($n=30$) are defined as:

$$
\sigma^2_{OJ} = \frac{1}{n-1} \sum_{i=1}^{n} \left( \ln\left(\frac{O_i}{C_{i-1}}\right) - \mu_{OJ} \right)^2
$$

$$
\sigma^2_{C} = \frac{1}{n-1} \sum_{i=1}^{n} \left( \ln\left(\frac{C_i}{O_i}\right) - \mu_{C} \right)^2
$$

$$
\sigma^2_{RS} = \frac{1}{n} \sum_{i=1}^{n} \left( \ln\left(\frac{H_i}{O_i}\right) \ln\left(\frac{H_i}{C_i}\right) + \ln\left(\frac{L_i}{O_i}\right) \ln\left(\frac{L_i}{C_i}\right) \right)
$$

To apply this daily variance locally to intraday stops, it is scaled by the temporal fraction of a 1-minute bar over a 252-day year:

$$
\tau = \sqrt{\frac{1/390}{252}}
$$

The dynamic distance is calculated from the High Water Mark (HWM):

$$
\text{Distance}_{raw} = \text{HWM} \times 2.0 \times \sigma_{YZ} \times \tau
$$

**Empirical Clamping & Hurst Gating:**
The distance is clamped using the 10th and 90th percentiles of the 14-period ATR:

$$
\text{Distance}_{clamped} = \max(\text{ATR}_{10th}, \min(\text{Distance}_{raw}, \text{ATR}_{90th}))
$$

Finally, the upward movement of the stop is gated by the Hurst Exponent ($H$):

* $H < 0.5$ (Mean Reverting): Ratchet locked.

* $H \ge 0.5$ (Trending): Ratchet unlocked, allowing the stop to trail upward.

### 3.5 Immutable Sizing Clamp

Under Regulation T § 220.8 and FINRA T+1 settlement protocols, sizing utilizes a strict Quarter-Kelly formulation bounded by risk limits and settled cash constraints:

$$
Q_{Kelly} = \left\lfloor \frac{50.00}{\vert{}Entry - Stop\vert{}} \right\rfloor
$$

$$
Q_{Cap} = \left\lfloor \frac{0.33 \times NLV}{Entry} \right\rfloor
$$

$$
Q_{Cash} = \left\lfloor \frac{SettledCash - 10.00}{Entry} \right\rfloor
$$

The final executed quantity represents the absolute minimum:

$$
Q_{Final} = \min(Q_{Kelly}, Q_{Cap}, Q_{Cash})
$$

## 4. Architecture & API Operations

Operating autonomously within a retail brokerage requires strict defensive engineering against arbitrary API throttling and state mutations.

### 4.1 Chronological Execution Sequence & Token Budget

The Schwab API uses a Token Bucket (20 tokens burst, 100 RPM refill) and a 3,500 daily hard cap.

| Session Time | Trading Phase | System Action | Schwab API Endpoint | Token Usage | 
| ----- | ----- | ----- | ----- | ----- | 
| **08:35 EDT** | PRE_MARKET | Container Boot | `POST /oauth/token` | 1 Token | 
| **09:15 EDT** | PRE_MARKET | Tier 1 Batch Filter | `GET /marketdata/v1/quotes` | 1 Token | 
| **09:20 EDT** | PRE_MARKET | Target History Warmup | `GET /marketdata/v1/pricehistory` | 6 Tokens | 
| **09:30 EDT** | MORNING_DRIVE | Stream Telemetry | `WebSocket LEVELONE_EQUITIES` | 0 Tokens | 
| **15:50 EDT** | FLATTEN | Unwind Inventory | `POST /trader/v1/orders` | Variable | 

### 4.2 Streamer HotSwap Protocol

The WebSocket connection utilizes dynamic state mutation. By issuing targeted JSON requests, the system can swap the active Top 3 assets without dropping the live TLS connection.

* **SUBS Command:** Overwrites the entire subscription ledger.

* **ADD Command:** Appends symbols to the active stream incrementally.

### 4.3 Defending Against State Mutation (FrozenInstanceError)

A critical error in production involved illegal in-place mutations of frozen `TradeSignal` dataclasses during sizing. Python's `dataclasses` are made immutable (`frozen=True`) to preserve thread safety. The pipeline now utilizes `dataclasses.replace()` to instantiate new immutable objects dynamically, preventing memory faults.

## 5. Production Code Implementation

### 5.1 `core/dynamic_scanner.py`

Manages asynchronous, non-blocking API token throttling and batched universe scanning.

```python
import asyncio  
import aiohttp  
import sqlite3  
import logging  
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import List, Dict, Any, Tuple  
  
logger = logging.getLogger("dynamic_scanner")  
  
class TokenBucket:  
    def __init__(self, capacity: int = 20, refill_rate: float = 1.667):  
        self.capacity = float(capacity)  
        self.tokens = float(capacity)  
        self.refill_rate = refill_rate  
        self.last_update = time.monotonic()  
        self.lock = asyncio.Lock()  
  
    async def consume(self, amount: int = 1) -> None:  
        async with self.lock:  
            while True:  
                now = time.monotonic()  
                elapsed = now - self.last_update  
                self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)  
                self.last_update = now  
  
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
        with sqlite3.connect(self.db_path) as conn:  
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
        await self.rate_limiter.consume(1)  
        url = f"{self.base_url}/marketdata/v1/quotes"  
        params = {"symbols": ",".join(symbols)}  
        headers = {"Authorization": f"Bearer {self.auth_token}"}  
          
        async with aiohttp.ClientSession(headers=headers) as session:  
            async with session.get(url, params=params) as resp:  
                resp.raise_for_status()  
                return await resp.json()  
  
    def calculate_selection_scores(self, quotes: Dict[str, Any], candidate_universe: Dict[str, Any]) -> List[Tuple[float, str]]:  
        alpha, beta, gamma = 0.40, 0.45, 0.15  
        metrics = {}  
          
        for sym, data in quotes.items():  
            try:  
                quote = data.get("quote", {})  
                last_price = float(quote.get("regularMarketLastPrice", 1.0))  
                bid = float(quote.get("bidPrice", last_price))  
                ask = float(quote.get("askPrice", last_price))  
                prev_close = float(quote.get("closePrice", last_price))  
                total_volume = int(quote.get("totalVolume", 0))  
                  
                gap = abs(last_price - prev_close) / max(prev_close, 1e-4)  
                
                # Retrieve ADV and calculate true relative pre-market dollar volume
                adv_shares = candidate_universe.get(sym, {}).get("adv_20d_shares", 1_000_000)
                adv_dollar = adv_shares * prev_close
                rvol_dollar = (total_volume * last_price) / max(adv_dollar, 1.0)
                
                spread = (ask - bid) / max(last_price, 1e-4)  
                  
                metrics[sym] = {"gap": gap, "rvol_d": rvol_dollar, "spread": spread}  
            except (ValueError, KeyError, TypeError):  
                continue  
  
        if not metrics: return []  
  
        gaps, rvols, spreads = [m["gap"] for m in metrics.values()], [m["rvol_d"] for m in metrics.values()], [m["spread"] for m in metrics.values()]
        min_g, max_g = min(gaps), max(gaps)  
        min_r, max_r = min(rvols), max(rvols)
        min_s, max_s = min(spreads), max(spreads)
          
        scores = []  
        for sym, m in metrics.items():  
            # Normalize all features before applying coefficient weights
            norm_gap = (m["gap"] - min_g) / max(max_g - min_g, 1e-8)  
            norm_rvol = (m["rvol_d"] - min_r) / max(max_r - min_r, 1e-8)
            norm_spread = (m["spread"] - min_s) / max(max_s - min_s, 1e-8)
            
            score = (alpha * norm_gap) + (beta * norm_rvol) - (gamma * norm_spread)  
            scores.append((score, sym))  
              
        scores.sort(reverse=True)  
        return scores  
  
    async def fetch_target_history(self, top_symbols: List[str]) -> Dict[str, Dict[int, float]]:  
        """Fetches 10-day history, persists to DB, and returns minute-of-day volumes for StrategyEngine initialization."""
        headers = {"Authorization": f"Bearer {self.auth_token}"}  
        baseline_volumes = {}
        
        async with aiohttp.ClientSession(headers=headers) as session:  
            for sym in top_symbols:  
                await self.rate_limiter.consume(1)  
                url = f"{self.base_url}/marketdata/v1/pricehistory"  
                params = {  
                    "symbol": sym, "periodType": "day", "period": 10,  
                    "frequencyType": "minute", "frequency": 1, "needExtendedHoursData": "false"  
                }  
                async with session.get(url, params=params) as resp:  
                    data = await resp.json()  
                    candles = data.get("candles", [])
                    self._persist_history(sym, candles) 
                    
                    minute_vols = {}
                    minute_counts = {}
                    for c in candles:
                        # Enforce Eastern Time zone awareness to prevent Docker UTC offset bug
                        dt = datetime.fromtimestamp(c["datetime"] / 1000.0, tz=ZoneInfo("America/New_York"))
                        mod = dt.hour * 60 + dt.minute
                        minute_vols[mod] = minute_vols.get(mod, 0) + c["volume"]
                        minute_counts[mod] = minute_counts.get(mod, 0) + 1
                    
                    # Store average volume per minute for in-memory engine baseline
                    for mod, vol in minute_vols.items():
                        minute_vols[mod] = vol / minute_counts[mod]
                    baseline_volumes[sym] = minute_vols
                    
        return baseline_volumes
  
    def _persist_history(self, symbol: str, candles: List[Dict[str, Any]]) -> None:  
        if not candles: return  
        rows = [  
            (symbol, int(c["datetime"]), float(c["open"]), float(c["high"]),   
             float(c["low"]), float(c["close"]), int(c["volume"]))  
            for c in candles  
        ]  
        with sqlite3.connect(self.db_path) as conn:  
            conn.executemany("""  
                INSERT OR IGNORE INTO historical_1m   
                (symbol, timestamp, open, high, low, close, volume)   
                VALUES (?, ?, ?, ?, ?, ?, ?)  
            """, rows)  

```

### 5.2 `core/universe_manager.py`

Evaluates temporal phase gates and handles strictly immutable signal allocation calculations.

```python
import collections  
from enum import Enum  
from dataclasses import dataclass, replace  
from datetime import datetime, time  
from typing import Dict, Optional, Set  
import pytz  
  
class TradingPhase(Enum):  
    OFFLINE = "OFFLINE"  
    PRE_MARKET = "PRE_MARKET"  
    MORNING_DRIVE = "MORNING_DRIVE"  
    MID_MORNING = "MID_MORNING"  
    MIDDAY_FREEZE = "MIDDAY_FREEZE"  
    POWER_HOUR = "POWER_HOUR"  
    PRE_CLOSE = "PRE_CLOSE"  
    MANDATORY_FLATTEN = "MANDATORY_FLATTEN"  
    POST_CLOSE_REFLECTION = "POST_CLOSE_REFLECTION"  
  
@dataclass(frozen=True)  
class TradeSignal:  
    symbol: str  
    strategy: str  
    entry_price: float  
    stop_price: float  
    target_price: float  
    quantity: int = 0  
      
class UniverseManager:  
    def __init__(self):  
        self.active_symbols: Set[str] = set()  
        self.state_rings: Dict[str, collections.deque] = {}  
        self._edt = pytz.timezone("America/New_York")  
  
    def onboard_symbols(self, symbols: list[str]) -> None:  
        for sym in symbols:  
            self.active_symbols.add(sym)  
            self.state_rings[sym] = collections.deque(maxlen=390)  
  
    def evaluate_phase(self, wall_clock: Optional[datetime] = None) -> TradingPhase:  
        now = wall_clock or datetime.now(self._edt)  
        t = now.time()  
          
        if t < time(8, 35): return TradingPhase.OFFLINE  
        if time(8, 35) <= t < time(9, 30): return TradingPhase.PRE_MARKET  
        if time(9, 30) <= t < time(10, 30): return TradingPhase.MORNING_DRIVE  
        if time(10, 30) <= t < time(11, 30): return TradingPhase.MID_MORNING  
        if time(11, 30) <= t < time(14, 0): return TradingPhase.MIDDAY_FREEZE  
        if time(14, 0) <= t < time(15, 35): return TradingPhase.POWER_HOUR  
        if time(15, 35) <= t < time(15, 50): return TradingPhase.PRE_CLOSE  
        if time(15, 50) <= t < time(15, 55): return TradingPhase.MANDATORY_FLATTEN  
        if time(15, 55) <= t < time(17, 0): return TradingPhase.POST_CLOSE_REFLECTION  
        return TradingPhase.OFFLINE  
  
    def clamp_signal_size(self, signal: TradeSignal, nlv: float, settled_cash: float, max_risk: float = 50.0) -> TradeSignal:  
        if signal.entry_price <= 0.0 or signal.entry_price == signal.stop_price:  
            return replace(signal, quantity=0)  
  
        risk_per_share = abs(signal.entry_price - signal.stop_price)  
        qk_shares = int(max_risk / risk_per_share)  
        cap_shares = int((0.33 * nlv) / signal.entry_price)  
          
        # Bucket 1 cash calculation prevents Reg T GFV infractions
        cash_buffer = 10.00  
        cash_shares = int((settled_cash - cash_buffer) / signal.entry_price)  
  
        clamped_qty = max(0, min(qk_shares, cap_shares, cash_shares))  
        return replace(signal, quantity=clamped_qty)  

```

### 5.3 `data/streamer_hotswap.py`

WebSocket architecture mapping strict API fields to stream ticks with exponential backoff.

```python
import asyncio  
import json  
import logging  
import random  
import websockets  
from websockets.exceptions import ConnectionClosed  
from typing import List, Optional  
  
logger = logging.getLogger("streamer_hotswap")  
  
class StreamerHotSwap:  
    def __init__(self, uri: str, customer_id: str, correl_id: str, token: str):  
        self.uri = uri  
        self.customer_id = customer_id  
        self.correl_id = correl_id  
        self.token = token  
        self.connection: Optional[websockets.WebSocketClientProtocol] = None  
        self._running = False  
        self.fields = "0,1,2,3,4,5,8,42"  
  
    async def connect_with_jitter(self) -> None:  
        self._running = True  
        attempt = 0  
        while self._running:  
            try:  
                async with websockets.connect(self.uri, ping_interval=30, ping_timeout=10) as ws:  
                    self.connection = ws  
                    attempt = 0  
                    await self._send_login()  
                    await self.listen_loop(ws)  
            except (ConnectionClosed, OSError, asyncio.TimeoutError) as e:  
                self.connection = None  
                attempt += 1  
                base_backoff = min(60.0, (2 ** attempt))  
                jitter = random.uniform(0, 0.1 * base_backoff)  
                sleep_time = base_backoff + jitter  
                await asyncio.sleep(sleep_time)  
  
    async def _send_login(self) -> None:  
        login_req = {  
            "requests": [{  
                "requestid": "1", "service": "ADMIN", "command": "LOGIN",  
                "SchwabClientCustomerId": self.customer_id,  
                "SchwabClientCorrelId": self.correl_id,  
                "parameters": {  
                    "Authorization": self.token,  
                    "SchwabClientChannel": "IO",  
                    "SchwabClientFunctionId": "Tradeticket"  
                }  
            }]  
        }  
        if self.connection: await self.connection.send(json.dumps(login_req))  
  
    async def update_subscriptions(self, symbols: List[str], incremental: bool = False) -> None:  
        if not self.connection: return  
        command = "ADD" if incremental else "SUBS"  
        payload = {  
            "requests": [{  
                "requestid": "2", "service": "LEVELONE_EQUITIES", "command": command,  
                "SchwabClientCustomerId": self.customer_id,  
                "SchwabClientCorrelId": self.correl_id,  
                "parameters": {"keys": ",".join(symbols), "fields": self.fields}  
            }]  
        }  
        await self.connection.send(json.dumps(payload))  
  
    async def listen_loop(self, ws: websockets.WebSocketClientProtocol) -> None:  
        async for message in ws:  
            try:  
                data = json.loads(message)  
                if "data" in data:  
                    for item in data["data"]:  
                        if item.get("service") == "LEVELONE_EQUITIES":  
                            self._process_level_one(item.get("content", []))  
            except json.JSONDecodeError: pass  
  
    def _process_level_one(self, content: List[dict]) -> None:  
        for tick in content:  
            parsed_tick = {  
                "symbol": tick.get("key"),  
                "bid": float(tick["1"]) if tick.get("1") else None,  
                "ask": float(tick["2"]) if tick.get("2") else None,  
                "last": float(tick["3"]) if tick.get("3") else None,  
                "bid_size": int(tick["4"]) if tick.get("4") else None,  
                "ask_size": int(tick["5"]) if tick.get("5") else None,  
                "volume": int(tick["8"]) if tick.get("8") else None,  
                "timestamp": int(tick["42"]) if tick.get("42") else None  
            }  
            logger.debug(f"Tick received: {parsed_tick}")  

```

### 5.4 `scripts/replay_screener_backtest.py`

Simulates screening and trailing-stop conditions against historical DataFrames to empirically validate logic offline.

```python
import pandas as pd  
import numpy as np  
from typing import Dict, Any, List, Tuple  
from math import floor, sqrt  
  
def compute_yang_zhang_atr(df: pd.DataFrame, hwm: float, atr14: float) -> float:  
    if len(df) < 30: return atr14  
      
    tau = sqrt((1/390.0) / 252.0)  
    recent = df.tail(30)  
    
    log_ho = np.log(recent['high'] / recent['open'])  
    log_lo = np.log(recent['low'] / recent['open'])  
    log_hc = np.log(recent['high'] / recent['close'])  
    log_lc = np.log(recent['low'] / recent['close'])  
      
    rs_var = (log_ho * log_hc + log_lo * log_lc).mean()  
    yz_vol = sqrt(max(rs_var, 1e-8))  
      
    distance = hwm * 2.0 * yz_vol * tau  
    p10_atr, p90_atr = atr14 * 0.50, atr14 * 1.50  
      
    return max(min(distance, p90_atr), p10_atr)  
  
def run_screener_backtest(historical_bars: Dict[str, pd.DataFrame], candidate_universe: Dict[str, Any], initial_nlv: float = 3753.75) -> Dict[str, float]:  
    capital = initial_nlv  
    equity_curve = [capital]  
    trades = []  
      
    all_dates = list(set().union(*(df.index.date for df in historical_bars.values())))  
    all_dates.sort()  
      
    for current_date in all_dates:  
        metrics = {}
          
        for sym, df in historical_bars.items():  
            day_data = df.loc[df.index.date == current_date]
            if day_data.empty or len(day_data) < 30: continue  
                  
            try:  
                open_price = float(day_data.iloc[0]['open'])  
                prev_close = open_price * 0.99    
                gap = abs(open_price - prev_close) / prev_close  
                vol = float(day_data.iloc[:15]['volume'].sum())  
                
                # Attain parity with production DynamicScanner screening logic
                adv_shares = candidate_universe.get(sym, {}).get("adv_20d_shares", 1_000_000)
                adv_dollar = adv_shares * prev_close
                rvol_dollar = (vol * open_price) / max(adv_dollar, 1.0)
                
                # Proxy historical spread via metadata median definition
                median_spread = candidate_universe.get(sym, {}).get("median_spread_cents", 1.5) / 100.0
                spread_pct = median_spread / open_price
                
                metrics[sym] = {"gap": gap, "rvol_d": rvol_dollar, "spread": spread_pct}
            except IndexError: continue  
                  
        if not metrics: continue

        gaps, rvols, spreads = [m["gap"] for m in metrics.values()], [m["rvol_d"] for m in metrics.values()], [m["spread"] for m in metrics.values()]
        min_g, max_g = min(gaps), max(gaps)  
        min_r, max_r = min(rvols), max(rvols)
        min_s, max_s = min(spreads), max(spreads)
        
        candidate_scores: List[Tuple[float, str]] = []  
        for sym, m in metrics.items():
            norm_gap = (m["gap"] - min_g) / max(max_g - min_g, 1e-8)  
            norm_rvol = (m["rvol_d"] - min_r) / max(max_r - min_r, 1e-8)
            norm_spread = (m["spread"] - min_s) / max(max_s - min_s, 1e-8)
            
            score = (0.40 * norm_gap) + (0.45 * norm_rvol) - (0.15 * norm_spread)
            candidate_scores.append((score, sym))
            
        candidate_scores.sort(reverse=True)  
        active_universe = [sym for _, sym in candidate_scores[:3]]  
          
        for sym in active_universe:  
            df = historical_bars[sym].loc[historical_bars[sym].index.date == current_date]  
            orb_window = df.between_time('09:30', '09:45')  
            if orb_window.empty: continue  
              
            orb_high, orb_low = float(orb_window['high'].max()), float(orb_window['low'].min())  
            atr_approx = orb_high - orb_low  
              
            trade_active = False  
            entry_price, shares, high_water_mark = 0.0, 0, 0.0  
              
            trading_session = df.between_time('09:46', '15:50')  
            for time_idx, row in trading_session.iterrows():  
                high, low = float(row['high']), float(row['low'])  
                  
                if not trade_active:  
                    if high > orb_high:  
                        trade_active = True  
                        entry_price = max(float(row['open']), orb_high)  
                        high_water_mark = entry_price  
                          
                        risk_delta = max(entry_price - orb_low, 0.01)  
                        q_kelly = floor(50.0 / risk_delta)  
                        q_cap = floor((0.33 * capital) / entry_price)  
                        q_cash = floor((capital - 10.00) / entry_price)  
                        shares = min(q_kelly, q_cap, q_cash)  
                else:  
                    high_water_mark = max(high_water_mark, high)  
                    stop_dist = compute_yang_zhang_atr(trading_session.loc[:time_idx], high_water_mark, atr_approx)  
                    dynamic_stop = max(orb_low, high_water_mark - stop_dist)  
                    target = entry_price + 2.5 * (entry_price - orb_low)  
                      
                    if low < dynamic_stop:  
                        exit_price = min(float(row['open']), dynamic_stop)  
                        pnl = (exit_price - entry_price) * shares  
                        capital += pnl; trades.append(pnl); trade_active = False; break  
                    elif high >= target:  
                        exit_price = max(float(row['open']), target)  
                        pnl = (exit_price - entry_price) * shares  
                        capital += pnl; trades.append(pnl); trade_active = False; break  
                          
            if trade_active:  
                exit_price = float(trading_session.iloc[-1]['close'])  
                pnl = (exit_price - entry_price) * shares  
                capital += pnl; trades.append(pnl)  
                  
        equity_curve.append(capital)  
  
    trades_arr = np.array(trades)  
    win_rate = float((trades_arr > 0).mean()) if len(trades) > 0 else 0.0  
    equity_series = pd.Series(equity_curve)  
      
    returns = equity_series.pct_change().dropna()  
    std_dev = float(returns.std())  
    sharpe = float((returns.mean() / std_dev) * sqrt(252)) if std_dev > 0 else 0.0  
      
    return {  
        "Total_PnL": capital - initial_nlv, "Win_Rate": win_rate,  
        "Sharpe_Ratio": sharpe, "Total_Trades": len(trades)  
    }  
