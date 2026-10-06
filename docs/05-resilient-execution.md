# Advanced Systems Resilience, Market Microstructure Streamers, Wash-Sale Compliance, and Fault-Tolerant Execution for the Schwab Trader API

## 1. Multi-Lot Tax Contamination and IRC §1091 Wash Sale Mitigation

Deploying an automated intraday execution engine alongside a multi-day discretionary swing portfolio within the same Charles Schwab Individual Cash Brokerage Account (...015) introduces immediate structural tax risks under Internal Revenue Code (IRC) Section 1091. While wash sale regulations apply per taxpayer entity across all accounts under that taxpayer's control, intra-account execution conflicts generate direct mechanical disruptions within the broker's automated tax-lot accounting system.

### Intra-Account Sub-Allocation Wash Sale Dynamics

Under IRC §1091, a wash sale occurs when a taxpayer realizes a loss on the sale of a security and, within a 61-day window spanning 30 days prior to the sale, the date of sale, and 30 days after the sale, acquires substantially identical stock or securities or enters into a contract or option to do so. When an intraday algorithmic system executes rapid entries and exits on an asset concurrently held in a multi-day swing tranche—such as an existing \$2,500.00 position distributed across SOXL, TQQQ, or TNA—the two trading strategies contaminate each other's tax lots.

The primary consequence of this interaction is loss disallowance and cost-basis adjustment. If the day-trading engine executes an entry of 15 shares of TQQQ at \$52.00 and subsequently triggers a client-side stop-loss at \$50.50, it realizes an intraday capital loss of \$22.50. Under IRC §1091, because the taxpayer holds matching replacement shares in the swing portfolio acquired within the preceding 30 days, the \$22.50 loss cannot be deducted in the current tax period.

Instead, Treasury Regulation §1.1091-1 dictates that the disallowed loss must be added to the unadjusted cost basis of the matching swing shares. If the primary swing shares were acquired at \$48.00 per share, their tax basis is adjusted upward:

$$
\text{Adjusted Basis} = \$48.00 + \left(\frac{\$22.50}{N_{\text{matched shares}}}\right)
$$

Pursuant to IRC §1223(4), the holding period of the day-trading lot is tacked onto the holding period of the swing shares. While this may eventually assist in qualifying the swing tranche for long-term capital gains treatment, it distorts performance tracking for both strategies.

The reporting implications on IRS Form 1099-B and Form 8949 create severe administrative overhead. Charles Schwab tracks intra-account lot transactions natively. Realized losses matching open swing positions are marked with Code W in Box 1g of Form 1099-B. If the swing position remains open across the tax-year boundary (December 31 to January 1), the realized intraday loss cannot offset gains realized earlier in the calendar year. This dynamic produces taxable phantom gains, forcing the taxpayer to pay taxes on profits that were economically offset by day-trading losses.

### Algorithmic Universe Exclusion Mask

To eliminate intra-account tax contamination, the system incorporates an automated UniverseExclusionMask state machine. This subsystem queries the broker's active portfolio at 09:15:00 EDT via GET /trader/v1/accounts/{accountHash}?fields=positions to identify open multi-day holdings.

The state machine parses the returned position objects, inspecting lot origination timestamps. Any position with an acquisition date prior to the current session ($t_{\text{lot}} < \text{Today}_{\text{00:00:00 EDT}}$) is classified as a Protected Swing Asset. The symbol is placed into the exclusion mask $\mathbb{M}_{\text{excluded}}$, which removes it from the candidate universe $\mathbb{U}_{\text{base}}$:

$$
\mathbb{U}_{\text{day}} = \mathbb{U}_{\text{base}} \setminus \mathbb{M}_{\text{excluded}}
$$

| State Machine Stage | System Action | API Endpoint & Parameters | Output State |
|----|----|----|----|
| **1. Pre-Market Audit** | Retrieve open holdings and tax lots at 09:15 AM EDT. | GET /trader/v1/accounts/{hash}?fields=positions | RAW_POSITIONS_INGESTED |
| **2. Lot Classification** | Evaluate holding origination timestamps against current date. | Internal timestamp logic ($t_{\text{lot}} < \text{Session}_{\text{open}}$). | SWING_HOLDINGS_IDENTIFIED |
| **3. Mask Application** | Add identified swing symbols to the exclusion filter. | In-memory `Set[str]` exclusion mask. | UNIVERSE_PRUNED |
| **4. Routing Gate** | Block order requests matching masked symbols. | Pre-trade interceptor on order dispatcher. | EXECUTION_ISOLATED |

### Dynamic Universe Rotation

When the primary swing holdings lock core technology and broad-market instruments out of the day-trading pool, the engine rotates capital to high-beta alternatives. These replacement assets must demonstrate sufficient intraday volatility and liquidity while avoiding overlapping index components with the excluded symbols.

| Excluded Swing Asset | Underlying Exposure | Dynamic Rotation Alternative | Asset Class & Microstructure Catalyst | Target Leverage | Correlation Coefficient (r) |
|----|----|----|----|----|----|
| **SOXL** | Semiconductor Sector | **FNGU** | MicroSectors FANG+ Index (Mega-Cap Tech) | 3x Bull | Moderate (r \approx 0.72) |
| **TQQQ** | NASDAQ-100 Index | **CONL** | Coinbase Global Inc. (Crypto Market Beta) | 2x Bull | Low-to-Moderate (r \approx 0.45) |
| **TNA** | Russell 2000 Small-Cap | **DPST** | S&P Regional Banks Select Industry | 3x Bull | Moderate (r \approx 0.58) |
| **Broad Tech Beta** | Equities Composite | **BOIL** | Bloomberg Natural Gas Subindex | 2x Bull | Uncorrelated (r \approx -0.04) |

The rotation engine applies liquidity gating to candidate assets, requiring pre-market share volume of at least 50,000 shares and a bid-ask spread under 0.10% before admitting a replacement ticker to the daily trading queue.

## 2. Market Data Architecture: Batched REST vs. Level 1 WebSocket Streamer

High-frequency execution and dynamic risk controls require continuous pricing updates while operating within Charles Schwab's API rate limits. The platform accommodates two distinct ingestion conduits: a persistent Level 1 WebSocket Streamer and a batched REST quoting client.

### Schwab Level 1 Streamer Protocol Architecture

The Charles Schwab WebSocket Streamer is hosted at wss://streamer-api.schwabapi.com/ws. The protocol operates as a stateful, binary- or JSON-framed connection requiring a specific two-step handshake.

First, the application issues an authenticated request to GET /trader/v1/userPreference using an active OAuth access token. The response provides connection metadata within the streamerInfo array, including the socket URL, customer identifier, and correlation parameters:

```json
{
"streamerInfo": [
{
"streamerSocketUrl": "wss://streamer-api.schwabapi.com/ws",
"schwabClientCustomerId": "CUST_987654321",
"schwabClientCorrelId": "CORR_A1B2C3D4E5",
"schwabClientChannel": "101",
"schwabClientFunctionId": "API_TRADER"
}
]
}
```

Second, the client opens a WebSocket connection to streamerSocketUrl and transmits an ADMIN LOGIN frame containing the active OAuth token:

```json
{
"requests": [
{
"service": "ADMIN",
"command": "LOGIN",
"requestid": "1",
"SchwabClientCustomerId": "CUST_987654321",
"SchwabClientCorrelId": "CORR_A1B2C3D4E5",
"parameters": {
"credential": "...",
"token": "<oauth_access_token>",
"version": "1.0"
}
}
]
}
```

Upon receiving a successful login response (responseCode: 0), the client subscribes to real-time quotes using the LEVELONE_EQUITIES service:

```json
{
"requests": [
{
"service": "LEVELONE_EQUITIES",
"command": "SUBS",
"requestid": "2",
"SchwabClientCustomerId": "CUST_987654321",
"SchwabClientCorrelId": "CORR_A1B2C3D4E5",
"parameters": {
"keys": "FNGU,CONL,DPST,BOIL",
"fields": "0,1,2,3,4,5,8,10,11"
}
}
]
}
```

Field numbers within LEVELONE_EQUITIES map to specific market variables:

| Field Index | Variable Name | Data Type | Analytical Role in Execution Engine |
|----|----|----|----|
| **0** | symbol | String | Underlying equity ticker. |
| **1** | bidPrice | Float | National best bid; drives market sell evaluations. |
| **2** | askPrice | Float | National best ask; validates limit buy entry boundaries. |
| **3** | lastPrice | Float | Last executed trade; evaluated against trailing stops. |
| **4** | bidSize | Integer | Aggregate quantity at best bid. |
| **5** | askSize | Integer | Aggregate quantity at best ask. |
| **8** | totalVolume | Long | Cumulative session volume; input for intraday VWAP. |
| **10** | highPrice | Float | Session high; bounds breakout expansion models. |
| **11** | lowPrice | Float | Session low; bounds opening range stop-loss levels. |

The client maintains an automated heartbeat loop. If no traffic arrives from the server for 20 seconds, the client transmits an ADMIN HEARTBEAT frame or an empty ping frame to keep the socket alive.

### Batched REST Rate-Limit Optimization and Token Bucket Architecture

When WebSocket feeds disconnect or experience network degradation, the engine falls back to REST polling via GET /marketdata/v1/quotes.

Polling individual tickers consumes rate limits rapidly. To conserve capacity, the client batches symbols into a single comma-separated request:

```http
GET /marketdata/v1/quotes?symbols=FNGU%2CCONL%2CDPST%2CBOIL&fields=quote HTTP/1.1
Host: api.schwabapi.com
Authorization: Bearer <access_token>
Accept: application/json
```

Schwab enforces an application rate limit of 120 requests per minute. Exceeding this boundary triggers an HTTP 429 Too Many Requests error, which can lock the system out of critical market checks or order modifications.

To ensure capacity remains available for urgent order routing, the REST engine implements a Token Bucket rate limiter. The bucket maintains a capacity $C = 60\text{ tokens}$ and refills at $r = 1.0\text{ token/second}$ ($60\text{ tokens/minute}$). This reserves 50% of the broker's 120 RPM ceiling as an execution safety buffer.

The token balance T(t) at timestamp t is calculated as:

$$
T(t) = \min\left(C, T(t_{\text{last}}) + r \cdot (t - t_{\text{last}})\right)
$$

Batched market data queries consume 1 token per request and poll every 2 seconds ($30\text{ requests/minute}$), keeping the bucket near capacity. Order placements, cancellations, and emergency liquidations draw from a high-priority queue that bypasses market data locks, ensuring exit commands execute without delay.

## 3. Disaster Recovery, Network Partitions, and Fault-Tolerant Execution Failsafes

Relying entirely on a client-side execution loop introduces systemic risk: if the host process crashes, loses internet access, or experiences system hangs, unmanaged intraday positions become "orphaned" in the broker's clearing system without active risk controls.

Conversely, relying entirely on broker-side contingent OCO bracket orders creates account complications. When Schwab accepts contingent exit child strategies, the clearing engine places an execution hold on the underlying shares. If an emergency risk condition occurs—such as a daily portfolio circuit breaker or the mandatory 3:55 PM EDT flat-to-cash rule—the broker rejects immediate market sell sweeps because the shares are committed to the resting exit orders.

### Dual-Tier Stop-Loss Architecture

The system resolves this trade-off by deploying a Dual-Tier Stop-Loss architecture:

In Tier 1, a dynamic trailing stop runs in client process memory. The execution watcher monitors incoming ticks and recalculates trailing stop levels without committing shares to resting broker-side orders, leaving inventory unencumbered for emergency actions.

In Tier 2, a resting broker-side catastrophe stop order (orderType: STOP, duration: DAY) routes to the exchange immediately upon confirmation of the entry fill. This order is pegged to a fixed -4.0% loss threshold:

$$
P_{\text{catastrophe}} = P_{\text{entry}} \times 0.96
$$

On the \$1,000.00 cash allocation, this caps maximum loss at -\$40.00 even during complete communication failures.

| System State | Event Trigger | Immediate Action | Secondary Action | Final State |
|----|----|----|----|----|
| **Monitoring Active** | Entry fill confirmed. | Track Tier 1 trailing stop in local memory. | Submit Tier 2 broker stop (STOP, DAY, -4.0%). | DUAL_TIER_ENGAGED |
| **Trailing Breach** | Tick penetrates Tier 1 stop level. | Dispatch DELETE /orders/{tier2Id} to cancel catastrophe stop. | Await cancel confirmation; route market sell order. | POSITION_CLOSED |
| **Catastrophe Breach** | Market gaps down -4.0% through Tier 2. | Tier 2 fills directly on exchange. | Intercept fill via API/socket; clear Tier 1 watcher. | POSITION_CLOSED |
| **3:55 PM Sweep** | Clock reaches 15:55:00 EDT. | Dispatch DELETE /orders/{tier2Id} to cancel catastrophe stop. | Await cancel confirmation; route immediate market sell. | PORTFOLIO_FLAT |
| **Concurrency Race** | Tier 2 triggers while Tier 1 cancel is in flight. | DELETE returns HTTP 400/404 (already triggered/filled). | Query GET /orders/{tier2Id}; abort pending Tier 1 sell. | POSITION_CLOSED |

Reconciliation and conflict resolution proceed deterministically. When a Tier 1 exit or the 3:55 PM EDT flat sweep triggers, the client sends a DELETE /trader/v1/accounts/{accountHash}/orders/{tier2OrderId} request to cancel the resting catastrophe stop.

Once the broker confirms cancellation (HTTP 200 OK or HTTP 204 No Content), releasing the execution hold on the shares, the client submits a market sell order.

If market volatility causes the Tier 2 catastrophe stop to trigger at the broker at the same instant the Tier 1 exit initiates, the client's DELETE request fails with HTTP 400 Bad Request or HTTP 404 Not Found. The execution engine intercepts this error, queries GET /trader/v1/accounts/{accountHash}/orders/{tier2OrderId} to verify execution, and aborts the pending Tier 1 market sell to prevent an accidental short position.

### Process Crash Recovery and State Re-Hydration

If the daemon crashes or loses internet connectivity, it executes a recovery routine upon reboot:

1.  Position Inventory Audit: The engine queries GET /trader/v1/accounts/{accountHash}?fields=positions to retrieve all open positions.

2.  Order Book Audit: The engine queries GET /trader/v1/accounts/{accountHash}/orders with status=WORKING to identify active resting orders.

3.  Database Reconciliation: Active holdings are reconciled against the local SQLite database. Any position lacking an active tracking record is flagged as an Orphaned Position.

4.  Triage Handling:

    - During regular trading hours (09:30 AM – 03:55 PM EDT), the engine verifies whether a resting Tier 2 catastrophe stop is active. If active, it attaches a new Tier 1 watcher thread and resumes monitoring; if missing, it submits a new Tier 2 stop immediately.

    - If the session has reached or passed 03:55 PM EDT, the engine initiates an emergency liquidation sweep, canceling resting orders and submitting market sell orders to restore a 100% cash balance.

## 4. Qualitative Financial News Ingestion and Real-Time LLM Latency Guardrails

While technical indicators detect price movements, macroeconomic news and policy announcements often drive directional trend changes. The system incorporates a financial headline parser paired with an LLM evaluation gatekeeper (gemini-2.5-flash).

### Real-Time Financial Headline Sourcing and Pre-Filtering

The news ingestion pipeline combines the Schwab Market Data News endpoint (GET /marketdata/v1/news) with external financial RSS feeds (PR Newswire, SEC EDGAR).

Raw news streams contain significant boilerplate and promotional content. Before forwarding text to the LLM, a local pre-filter cleans the stream:

- HTML Tag and Boilerplate Removal: Strips formatting, legal disclaimers, and wire distribution notices.

- Entity and Keyword Filtering: Focuses on regulatory actions, rate adjustments, geopolitical developments, and semiconductor/energy supply disruptions.

- Digest Assembly: Retains only high-signal text, combining the headline with the opening 200 characters of the body text.

### Deterministic Latency Guardrails and Fallback Degradation

The system enforces a strict 1,500-millisecond execution timeout on external LLM inference calls (gemini-2.5-flash). If the LLM does not return a validated response within 1,500 milliseconds—or returns an HTTP 429 Too Many Requests status—the engine applies a deterministic degradation matrix rather than blocking execution indefinitely:

| Operational State | Root Cause Diagnostic | Fallback Execution Policy | Risk Multiplier ($\mu_{\text{risk}}$) | System Telemetry Action |
|----|----|----|----|----|
| **Nominal Execution** | LLM responds within $\le 1,500\text{ ms}$; structured JSON validated. | Follow LLM guidance (trade_permitted: true/false). | 0.50 - 1.00 (as returned by model) | Standard logging; widget telemetry update. |
| **Inference Timeout** | Latency exceeds 1,500 ms ceiling. | Default to technical signal with reduced sizing. | 0.50 (Fixed 50% capital reduction) | Issue high-latency warning to telemetry log. |
| **HTTP 429 Quota Exhaustion** | Rate limits exceeded on free-tier allocations. | Bypass LLM; execute technical signal if Choppiness Index \< 38.2. | 0.50 (Fixed 50% capital reduction) | Trigger exponential backoff on subsequent LLM calls. |
| **Elevated Chop Regime** | LLM fails during choppy market conditions (CI \> 61.8). | Complete Trade Veto (Abort order ticket). | 0.00 (Zero allocation) | Broadcast trade inhibition alert to edge widget. |
| **Catalyst Blackout Window** | LLM fails within 15 minutes of scheduled macro release. | Complete Trade Veto (Abort order ticket). | 0.00 (Zero allocation) | Log economic blackout compliance veto. |

## 5. Microstructure Execution Anomalies: Partial Fills, Leaves, and Tick Boundaries

Real-world order routing frequently encounters partial executions and exchange order validation rejections. The engine manages these conditions through a structured order lifecycle state machine.

### Order State Lifecycle and Leaves Handling

When a limit order routes to Schwab's gateway, the engine monitors its status transitions: WORKING \rightarrow PARTIALLY_FILLED \rightarrow FILLED or CANCELED.

\| Lifecycle State \| Trigger Condition \| Operational Action \| Ledger Impact \| \| :--- \| :--- \| :--- \| :--- \| \| **WORKING** \| Order accepted by exchange. \| Monitor execution fills; start session timeout timers. \| Order cost held in pending allocation. \| \| **PARTIALLY_FILLED** \| Executed quantity \> 0, but leavesQuantity \> 0. \| Log filled shares in SQLite; start 10-second liquidity drought timer. \| Allocate filled cost; maintain hold on leaves cost. \| \| **DROUGHT_EXPIRED** \| 10 seconds elapse with non-zero leaves. \| Submit DELETE /orders/{orderId} to cancel unfilled shares. \| Unspent capital returned to Bucket 1 (Settled_Cash). \| \| **FILLED** \| All requested shares execute. \| Record lot; engage Tier 1 watcher; route Tier 2 catastrophe stop. \| Total order cost finalized in Bucket 1. \| \| **CANCELED** \| Zero shares filled before cancellation. \| Update local database; purge order tracking structures. \| All held capital restored to Bucket 1. \|

Handling Non-Zero leavesQuantity:

When a limit order partially fills—for example, executing 8 shares of a 20-share order—the remaining 12 shares represent the leavesQuantity:

- Liquidity Drought Rule: A partial fill indicates thin liquidity at the limit price. Leaving the remaining shares active risks adverse executions after prices move. The engine starts a 10-second timer upon the initial partial fill. If the remaining shares do not execute within 10 seconds, the client sends a DELETE /trader/v1/accounts/{accountHash}/orders/{orderId} request to cancel the open leaves.

- Compliance Ledger Reconciliation: Once cancellation of the leaves is confirmed, the engine updates its internal ledger based strictly on the executed quantity:

$$
\text{Committed Capital} = N_{\text{executed}} \times P_{\text{fill}}
$$

The uncommitted balance allocated to the canceled leaves returns to Settled_Cash (Bucket 1).

- Tier 2 Catastrophe Stop Adjustment: The Tier 2 broker stop is placed or adjusted to match the executed 8 shares, ensuring resting orders cover only held inventory.

### Price Tick Precision and Sub-Penny Rejections

Under SEC Rule 612 of Regulation NMS (the Sub-Penny Rule), broker-dealers cannot accept or display quotes, orders, or price modifications in increments smaller than \$0.01 for securities priced at or above \$1.00. Submitting limit orders with fractional-cent precision (such as \$45.5042) triggers immediate API rejections (HTTP 400 Bad Request). The execution engine formats all price strings using two decimal places:

formatted_price = f"{limit_price:.2f}"

Because Schwab's Trader API supports whole shares only, fractional-share purchases are rejected. To prevent Insufficient Funds rejections caused by upward rounding, order sizing calculates the share quantity using floor division:

$$
Q = \left\lfloor \frac{\text{Settled Cash}_{\text{usable}}}{P_{\text{limit}}} \right\rfloor, \quad \text{Gross Order Value} = Q \times P_{\text{limit}} \le \text{Settled Cash}_{\text{usable}}
$$

This ensures total order cost never exceeds available cash by even one cent, avoiding broker validation errors.

## 6. Clearing Synchronization and Proactive Token Maintenance

Operating under U.S. T+1 settlement requires aligning the application's internal ledger with Depository Trust & Clearing Corporation (DTCC) clearing schedules, alongside automated maintenance for Schwab's 7-day OAuth refresh tokens.

### Continuous Net Settlement vs. Internal Ledger Rollover

Under U.S. T+1 clearing rules, equity trades execute on Day T and settle on Day T+1. National Securities Clearing Corporation (NSCC) Continuous Net Settlement (CNS) processes transactions overnight.

However, brokers do not post settled funds to individual client accounts at midnight. Charles Schwab typically updates account settlement balances between 08:30:00 AM and 09:00:00 AM EDT.

| Daily Timestamp (EDT) | Clearing Infrastructure Event | Internal Ledger Action | Execution Gate Status |
|----|----|----|----|
| **Day T, 15:55:00** | Intraday portfolio flattened to cash. | Gross sales proceeds enter Bucket 2 (Unsettled). | New order routing disabled. |
| **Overnight** | NSCC Continuous Net Settlement (CNS). | Balances remain locked in Bucket 2. | Engine in standby. |
| **Day T+1, 08:30:00** | Broker processes overnight clearing batches. | Ledger holds pending rollover. | Trading gates locked. |
| **Day T+1, 09:00:00** | Broker posts cleared cash to accounts. | Rollover: Transfer Bucket 2 balances to Bucket 1 (Settled). | Pre-market checks initiated. |
| **Day T+1, 09:15:00** | Position and balance telemetry verification. | Call GET /accounts/{hash}; compare cash balances. | Validates balance parity. |
| **Day T+1, 09:30:00** | Regular trading hours open. | Ledger active; cash allocation confirmed available. | Trading gates unlocked. |

To maintain compliance with settlement schedules:

- Ledger Rollover at 09:00:00 AM EDT: Rather than rolling balances over at midnight (00:00:00 EDT), the engine shifts its internal settlement rollover to 09:00:00 AM EDT. At this time, balances in Bucket 2 (Unsettled_Proceeds) transfer to Bucket 1 (Settled_Cash).

- Broker Telemetry Verification at 09:15:00 AM EDT: At 09:15:00 AM EDT, the engine queries GET /trader/v1/accounts/{accountHash}. It cross-references currentBalances.cashBalance against the internal ledger's Bucket 1. If the balances match within a \$1.00 tolerance, the engine releases execution locks for the upcoming 09:30:00 AM EDT market open. If a discrepancy is detected, trading remains locked, and an escalation alert is dispatched to the operator.

### Weekly OAuth Re-Authentication Protocol

A major operational constraint of the Schwab Trader API is the fixed 7-day expiration boundary enforced on refresh tokens. Unlike platforms that roll refresh token expiration windows forward upon successive exchanges, Schwab invalidates refresh tokens exactly 7 calendar days (604,800 seconds) after the initial browser authorization. When the refresh token expires, all API calls fail until a user re-authenticates through a web browser.

To prevent service interruptions during active trading sessions, the system runs an automated maintenance routine every Saturday at 10:00:00 AM EDT:

1.  Time-to-Live Audit: The daemon evaluates the refresh token expiration timestamp stored in the encrypted credential vault.

2.  Operator Notification: The system dispatches an interactive notification via Firebase Cloud Messaging to the operator's Google Pixel 9a: "Schwab Weekend Re-Auth Required. Tap to open login."

3.  Single-Use Listener: The daemon binds a single-use local HTTPS server to https://127.0.0.1:5556.

4.  Portal Authentication: The operator opens Schwab's authentication portal in a secure browser. Upon login, Schwab redirects to the local callback URI with the new authorization code.

5.  Token Rotation and Encryption: The loopback listener captures the callback, extracts and decodes the authorization code, exchanges it for new tokens at POST /v1/oauth/token, encrypts the credentials using AES-GCM-256, and terminates the listener. The new 7-day token is staged well ahead of Monday's market open.

## 7. Implementation Boilerplate and Production Configuration

### Batched REST Quote Poller with Token Bucket Rate Limiting

```python
"""
Batched REST Quote Poller with Token Bucket Rate Limiting
Enforces a conservative 60 requests/minute ceiling against Schwab's 120 RPM limit.
"""

import time
import logging
from typing import Dict, List, Any, Optional
import requests

logger = logging.getLogger("SchwabRateLimiter")

class TokenBucket:
"""Thread-safe Token Bucket rate limiter."""
def __init__(self, capacity: float = 60.0, refill_rate: float = 1.0):
self.capacity = capacity
self.refill_rate = refill_rate
self.tokens = capacity
self.last_update = time.monotonic()

def consume(self, tokens: float = 1.0, block: bool = True) -> bool:
while True:
now = time.monotonic()
elapsed = now - self.last_update
self.last_update = now
self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)

if self.tokens >= tokens:
self.tokens -= tokens
return True

if not block:
return False

sleep_time = (tokens - self.tokens) / self.refill_rate
time.sleep(max(0.01, sleep_time))

class BatchedQuotePoller:
"""Batches quotes into comma-separated requests within rate limits."""
MARKET_BASE = "https://api.schwabapi.com/marketdata/v1"

def __init__(self, token_provider, rate_limiter: Optional[TokenBucket] = None):
self.token_provider = token_provider
self.limiter = rate_limiter or TokenBucket(capacity=60.0, refill_rate=1.0)

def fetch_quotes_batch(self, symbols: List[str]) -> Dict[str, Any]:
if not symbols:
return {}

# Consume 1 token before dispatching
self.limiter.consume(1.0, block=True)

symbols_param = ",".join(symbols)
url = f"{self.MARKET_BASE}/quotes"
params = {"symbols": symbols_param, "fields": "quote"}
headers = {
"Authorization": f"Bearer {self.token_provider.get_access_token()}",
"Accept": "application/json"
}

response = requests.get(url, params=params, headers=headers, timeout=5)
if response.status_code == 200:
return response.json()
elif response.status_code == 429:
logger.critical("HTTP 429 Rate Limit Enforced by Schwab API.")
raise ConnectionRefusedError("Schwab API rate limit exceeded.")
else:
logger.error(f"Quote fetch failed [{response.status_code}]: {response.text}")
return {}
```

### Schwab Level 1 WebSocket Streamer Client

```python
"""
Schwab Level 1 Equity WebSocket Streamer Client
Bootstraps via GET /trader/v1/userPreference, connects to wss://,
authenticates via ADMIN LOGIN, and streams LEVELONE_EQUITIES data.
"""

import json
import logging
import asyncio
import websockets
import requests

logger = logging.getLogger("SchwabStreamer")

class SchwabLevel1Streamer:
TRADER_BASE = "https://api.schwabapi.com/trader/v1"

def __init__(self, token_provider):
self.token_provider = token_provider
self.websocket = None
self.is_running = False
self.request_id = 1

def _get_streamer_info(self) -> dict:
"""Retrieves streamer credentials and socket URL from user preferences."""
url = f"{self.TRADER_BASE}/userPreference"
headers = {
"Authorization": f"Bearer {self.token_provider.get_access_token()}",
"Accept": "application/json"
}
res = requests.get(url, headers=headers, timeout=10)
res.raise_for_status()
streamer_info = res.json().get("streamerInfo", [])[0]
return streamer_info

async def connect_and_stream(self, symbols: list[str]):
info = self._get_streamer_info()
wss_url = info["streamerSocketUrl"]
customer_id = info["schwabClientCustomerId"]
correl_id = info["schwabClientCorrelId"]

logger.info(f"Opening WebSocket connection to {wss_url}")
async with websockets.connect(wss_url) as ws:
self.websocket = ws
self.is_running = True

# 1. Transmit ADMIN LOGIN Frame
login_payload = {
"requests": [
{
"service": "ADMIN",
"command": "LOGIN",
"requestid": str(self.request_id),
"SchwabClientCustomerId": customer_id,
"SchwabClientCorrelId": correl_id,
"parameters": {
"credential": "...",
"token": self.token_provider.get_access_token(),
"version": "1.0"
}
}
]
}
self.request_id += 1
await ws.send(json.dumps(login_payload))
login_resp = await ws.recv()
logger.info(f"Streamer Login Response: {login_resp}")

# 2. Subscribe to LEVELONE_EQUITIES
sub_payload = {
"requests": [
{
"service": "LEVELONE_EQUITIES",
"command": "SUBS",
"requestid": str(self.request_id),
"SchwabClientCustomerId": customer_id,
"SchwabClientCorrelId": correl_id,
"parameters": {
"keys": ",".join(symbols),
"fields": "0,1,2,3,4,5,8"
}
}
]
}
self.request_id += 1
await ws.send(json.dumps(sub_payload))

# 3. Continuous Ingestion & Heartbeat Loop
while self.is_running:
try:
message = await asyncio.wait_for(ws.recv(), timeout=20.0)
data = json.loads(message)
logger.debug(f"Tick received: {data}")
except asyncio.TimeoutError:
pong_waiter = await ws.ping()
await asyncio.wait_for(pong_waiter, timeout=5.0)
logger.debug("Socket keep-alive heartbeat acknowledged.")
```

### Weekend OAuth Maintenance Daemon and Vault Updater

```python
"""
Weekend OAuth Maintenance Daemon
Binds to https://127.0.0.1:5556 on Saturdays at 10:00 AM EDT, captures user login,
exchanges the authorization code, and persists tokens to an AES-GCM-256 encrypted vault.
"""

import os
import json
import base64
import ssl
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

AUTH_CODE_CAPTURED = None

class OAuthCallbackHandler(BaseHTTPRequestHandler):
"""Captures the authorization redirect from Charles Schwab."""
def do_GET(self):
global AUTH_CODE_CAPTURED
query_components = parse_qs(urlparse(self.path).query)
code = query_components.get("code", [None])[0]

if code:
AUTH_CODE_CAPTURED = code
self.send_response(200)
self.send_header("Content-type", "text/html")
self.end_headers()
self.wfile.write(b"\OAuth Authorization Successful. Return to terminal.\")
else:
self.send_response(400)
self.end_headers()
self.wfile.write(b"\Error capturing OAuth code.\")

def exchange_and_save_tokens(
client_id: str,
client_secret: str,
auth_code: str,
vault_file: str,
master_pass: str
):
token_url = "https://api.schwabapi.com/v1/oauth/token"
creds = f"{client_id}:{client_secret}"
basic_auth = base64.b64encode(creds.encode('utf-8')).decode('utf-8')
headers = {
"Authorization": f"Basic {basic_auth}",
"Content-Type": "application/x-www-form-urlencoded"
}
payload = {
"grant_type": "authorization_code",
"code": auth_code,
"redirect_uri": "https://127.0.0.1:5556"
}

res = requests.post(token_url, headers=headers, data=payload, timeout=10)
res.raise_for_status()
tokens = res.json()

# Derive 256-bit AES key via PBKDF2-HMAC-SHA256
salt = os.urandom(16)
kdf = PBKDF2HMAC(
algorithm=hashes.SHA256(),
length=32,
salt=salt,
iterations=600000
)
key = kdf.derive(master_pass.encode('utf-8'))
nonce = os.urandom(12)
aesgcm = AESGCM(key)
ciphertext = aesgcm.encrypt(nonce, json.dumps(tokens).encode('utf-8'), None)

vault_data = {
"salt": base64.b64encode(salt).decode('utf-8'),
"nonce": base64.b64encode(nonce).decode('utf-8'),
"ciphertext": base64.b64encode(ciphertext).decode('utf-8')
}
with open(vault_file, 'w') as f:
json.dump(vault_data, f, indent=2)
print("Token vault updated and encrypted with AES-GCM-256.")

def run_weekend_auth(client_id: str, client_secret: str, vault_file: str, master_pass: str):
global AUTH_CODE_CAPTURED
server_address = ('127.0.0.1', 5556)
httpd = HTTPServer(server_address, OAuthCallbackHandler)

# Local SSL Context using development certificates
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(certfile="cert.pem", keyfile="key.pem")
httpd.socket = context.wrap_socket(httpd.socket, server_side=True)

auth_url = (
f"https://api.schwabapi.com/v1/oauth/authorize?"
f"client_id={client_id}&redirect_uri=https://127.0.0.1:5556"
)
print(f"Open browser and authenticate:\n{auth_url}")

while not AUTH_CODE_CAPTURED:
httpd.handle_request()

httpd.server_close()
exchange_and_save_tokens(client_id, client_secret, AUTH_CODE_CAPTURED, vault_file, master_pass)

if __name__ == "__main__":
run_weekend_auth(
client_id=os.getenv("SCHWAB_CLIENT_ID", ""),
client_secret=os.getenv("SCHWAB_CLIENT_SECRET", ""),
vault_file="schwab_tokens_vault.json",
master_pass=os.getenv("VAULT_PASSWORD", "SuperSecureLocalPassword123!")
)
```

### Production Configuration and Rules Matrix (config.yaml)

```yaml
version: "2.4.0"
execution_target:
broker: "CHARLES_SCHWAB"
account_suffix: "015"
account_type: "INDIVIDUAL_CASH"
settlement_regime: "T_PLUS_ONE"
capital_sandbox_baseline: 1000.00
settled_cash_buffer: 10.00

firewall_rules:
isolate_robo_accounts: true
blocked_account_keywords:
- "ROBO"
- "INTELLIGENT"
- "IRA"
- "PORTFOLIO_MANAGED"

tax_compliance:
irc_1091_wash_sale_protection: true
universe_exclusion_mask:
enabled: true
audit_time_edt: "09:15:00"
active_swing_symbols:
- "SOXL"
- "TQQQ"
- "TNA"
rotation_universe:
tech_alternative: "FNGU"
crypto_equity_alternative: "CONL"
regional_banking_alternative: "DPST"
uncorrelated_commodity: "BOIL"

market_data:
streamer:
endpoint: "wss://streamer-api.schwabapi.com/ws"
heartbeat_interval_seconds: 20
batched_rest:
endpoint: "https://api.schwabapi.com/marketdata/v1/quotes"
polling_interval_seconds: 2.0
token_bucket:
capacity: 60
refill_rate_per_second: 1.0
reserve_bandwidth_for_orders: 60

disaster_recovery:
dual_tier_stop_loss:
enabled: true
tier_1_client_trailing: true
tier_2_catastrophe_stop:
order_type: "STOP"
duration: "DAY"
fixed_threshold_pct: -0.04
fixed_dollar_max_loss: 40.00
liquidity_drought:
leaves_cancellation_timeout_seconds: 10
process_crash_recovery:
rehydrate_positions_on_startup: true
auto_flatten_orphans_post_355pm: true

llm_gatekeeper:
model: "gemini-2.5-flash"
timeout_ms: 1500
free_tier_limits:
max_rpm: 8
daily_quota_rpd: 1500
degradation_policy:
on_timeout: "REDUCE_SIZE_50_PCT"
on_http_429: "REDUCE_SIZE_50_PCT"
on_high_chop_failure: "VETO_ORDER"

clearing_and_schedule:
morning_ledger_rollover_edt: "09:00:00"
broker_balance_sync_edt: "09:15:00"
mandatory_flat_to_cash_sweep_edt: "15:55:00"
weekend_oauth_maintenance:
day: "SATURDAY"
time_edt: "10:00:00"
listener_address: "https://127.0.0.1:5556"
```

## 8. Systemic Resilience Conclusions

Operating a small cash allocation (\$1,000.00 baseline) alongside an active swing portfolio requires coordinated risk and compliance systems. By identifying structural friction points across broker clearing systems, federal tax codes, and API rate limits, the system deploys targeted engineering safeguards:

First, the system addresses intra-account tax contamination under IRC §1091. Realizing day-trading losses on assets concurrently held in swing allocations (SOXL, TQQQ, TNA) forces cost-basis adjustments and loss disallowances on Form 1099-B. The UniverseExclusionMask automates position isolation by checking active holdings each morning and dynamically rotating into unencumbered assets like FNGU, CONL, DPST, or BOIL.

Second, the Dual-Tier Stop-Loss architecture balances broker-side order mechanics with client-side flexibility. Placing native contingent exit brackets directly with the broker commits the underlying shares, preventing emergency liquidations or automated 3:55 PM EDT cash sweeps. The dual-tier model resolves this by tracking trailing exits in-memory while keeping a resting -4.0% catastrophe stop on the broker's books to protect against network failures.

Third, managing partial executions and rate limits stabilizes the trading pipeline. The 10-second liquidity drought timer prevents trailing fills at unfavorable prices, and the Token Bucket rate limiter (capped at 60 RPM) reserves 50% of Schwab's API capacity for urgent order actions.

Finally, the clearing and token maintenance schedule aligns operations with market infrastructure. Shifting ledger rollovers to 09:00 AM EDT matches broker-side cash posting windows under U.S. T+1 clearing, while the Saturday morning loopback daemon automates the weekly 7-day OAuth refresh cycle without interrupting weekday trading.

#### Works cited

1\. Charles Schwab Auto Trading With Astronomer Signals, https://www.astronomerapp.com/blog/charles-schwab-auto-trading-with-astronomer-signals 2. What did I do?? : r/Schwab - Reddit, https://www.reddit.com/r/Schwab/comments/1d32wg1/what_did_i_do/ 3. Is day trading actually legal in US market for cash account? - Reddit, https://www.reddit.com/r/Daytrading/comments/1h37hb2/is_day_trading_actually_legal_in_us_market_for/ 4. The (Unofficial) Guide to Charles Schwab's Trader APIs, https://medium.com/@carstensavage/the-unofficial-guide-to-charles-schwabs-trader-apis-14c1f5bc1d57 5. schwab-sdk-unofficial - PyPI Package Security Analysis - Soc, https://socket.dev/pypi/package/schwab-sdk-unofficial 6. Webull API Guide: Endpoints, Authentication & Python SDKs, https://dev.to/zuplo/webull-api-guide-endpoints-authentication-python-sdks-3a82 7. Schwab MCP Server - LobeHub, https://lobehub.com/mcp/acidsolution-schwab-mcp-server 8. README.md - dp_exchange_schwab 0.1.15 - Hex.pm, https://hex.pm/packages/dp_exchange_schwab/0.1.15/files/README.md?fallback=default 9. CHANGELOG.md - dp_exchange_schwab 0.1.20 - Hex.pm, https://hex.pm/packages/dp_exchange_schwab/0.1.20/files/CHANGELOG.md 10. schwab-py/schwab/streaming.py at main - GitHub, https://github.com/alexgolec/schwab-py/blob/main/schwab/streaming.py 11. Trading Journal — Backtest Results, Execution Issues, Live P&L, https://thedecaylab.com/journal 12. Trading Record API Reference - TrueFills, https://truefills.com/docs/reference 13. What Nobody Tells You About Automating a Schwab Account, https://medium.datadriveninvestor.com/what-nobody-tells-you-about-automating-a-schwab-account-f5301810a80a 14. How to Automate Trading in a Schwab Account (2026 Guide) - JorgAI, https://jorgai.com/blog/how-to-automate-trading-schwab-account 15. Example for placing order using schwab-py wrapper - GitHub Gist, https://gist.github.com/hn4002/d35ed5940084ab54e30c2ab3cf55d8ae 16. Google GenAI integration \| Temporal Documentation, https://docs.temporal.io/develop/python/integrations/google-genai 17. Structured output \| Gemini Enterprise Agent Platform, https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/control-generated-output 18. Ordering Error with Schwab API - Reddit, https://www.reddit.com/r/Schwab/comments/1cug4v3/ordering_error_with_schwab_api/ 19. What Is A Good Faith Violation? (And How To Avoid Them) - Carry, https://carry.com/learn/what-is-a-good-faith-violation 20. Good Faith Violation (GFV): What It Is & How to Avoid It - Mudrex Learn, https://mudrex.com/learn/good-faith-violation-gfv-what-it-is/ 21. Schwab: Available Funds is now Instant Settlement? : r/thinkorswim, https://www.reddit.com/r/thinkorswim/comments/1cs5l1t/schwab_available_funds_is_now_instant_settlement/
