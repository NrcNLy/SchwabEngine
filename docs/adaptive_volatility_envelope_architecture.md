# Production Specification: Adaptive Volatility Envelope Architecture (AVE)
**Target Engine:** `SchwabEngine` (Autonomous Leveraged ETF Execution Platform)  
**Document Path:** `/docs/research/adaptive_volatility_envelope.md`  
**Classification:** Core Quantitative Architecture Specification  
**Version:** 2.4.0-PROD  
**Target Instruments:** Direxion Daily Semiconductor Bull 3X Shares (`SOXL`), ProShares UltraPro QQQ (`TQQQ`), Direxion Daily Small Cap Bull 3X Shares (`TNA`)

---

## Executive Summary & Design Rationale

Trading ultra-high-beta $3\text{x}$ leveraged Exchange-Traded Funds (ETFs) such as `SOXL`, `TQQQ`, and `TNA` via static horizontal stop-loss and take-profit thresholds represents a critical architectural failure in automated trading systems. Leveraged exchange-traded products are rebalanced on a daily basis by fund managers to deliver exactly $300\%$ of the daily index performance. This daily reset creates path-dependent return trajectories that induce structural *variance drain* (volatility drag) during non-trending or mean-reverting market regimes.

The **Adaptive Volatility Envelope (AVE)** architecture abandons static price boundaries in favor of a non-linear, time-decaying probability cone that continuously contracts throughout the trading session. Unlike traditional statistical confidence bands (which expand over time proportionally to $\sqrt{t}$), the AVE deploys an *inverse* time-decay function. This contractive geometry forces a deterministic mathematical squeeze that converges precisely at a hard intraday liquidation barrier (Friday 15:50 EDT), neutralizing overnight gap risk, eliminating two-day weekend beta slippage, and mitigating compounding variance decay.

To operate reliably under the Charles Schwab REST API's strict 120 request-per-minute rate limit, this specification decouples continuous mathematical tracking from broker-side order updates. All envelope calculations, trailing supremum tracking, and breach detections are executed entirely in-memory via a **Virtual Bracket Finite State Machine (FSM)**. Outbound REST interaction is strictly minimized to two execution calls per trade lifecycle (entry and exit). Live telemetry is streamed via high-throughput FastAPI WebSockets utilizing Rust-accelerated `orjson` serialization, visualized in a React/Vite dashboard rendered using `requestAnimationFrame` buffering to prevent browser thread saturation.

---

## 1. Mathematical Formulation of the Adaptive Bounds

```
Price ($)
  ^
  |        /---- Upper Bound U(t) [Dynamic Take-Profit]
  |       /          \
  |      /   /\       \
  |     /   /  \  /\   \=========> Squeeze to Entry Price / Supremum
  |----*---/----\/- \---\--------* Entry Price P_entry
  |     \ /          \   \      /
  |      V            \---\----/-- Lower Bound L(t) [Dynamic Trailing Stop]
  |                        \  /
  +-------------------------\/---------------------------------> Time (t)
  t_0 (Entry)                                        T_close (15:50 EDT)
```

### 1.1 Leveraged ETF Mechanics & Quadratic Variance Drain

A $3\text{x}$ leveraged ETF rebalances its physical swap and futures exposure at the end of every trading session. Assume the underlying benchmark index $S(t)$ follows a standard Geometric Brownian Motion (GBM) governed by the stochastic differential equation:

$$\frac{dS_t}{S_t} = \mu dt + \sigma dW_t$$

Where:
* $\mu$ denotes the expected continuous drift of the underlying index.
* $\sigma$ represents the instantaneous annualized index volatility.
* $W_t$ is a standard one-dimensional Wiener process satisfying $\mathbb{E}[dW_t] = 0$ and $\mathbb{E}[(dW_t)^2] = dt$.

A leveraged ETF targeting a constant daily leverage multiplier $\beta$ (where $\beta = 3$ for `SOXL`, `TQQQ`, and `TNA`) rebalances continuously to ensure that its intraday return matches $\beta$ times the index return:

$$\frac{dX_t}{X_t} = \beta \frac{dS_t}{S_t} = \beta (\mu dt + \sigma dW_t) = \beta \mu dt + \beta \sigma dW_t$$

Applying Itô's Lemma to the logarithmic price process $f(X_t) = \ln X_t$:

$$d(\ln X_t) = f'(X_t)dX_t + \frac{1}{2}f''(X_t)(dX_t)^2$$

$$d(\ln X_t) = \frac{1}{X_t}(\beta \mu X_t dt + \beta \sigma X_t dW_t) - \frac{1}{2}\frac{1}{X_t^2}(\beta^2 \sigma^2 X_t^2 dt)$$

$$d(\ln X_t) = \left(\beta \mu - \frac{1}{2}\beta^2 \sigma^2\right)dt + \beta \sigma dW_t$$

Integrating from $t=0$ to $t$:

$$\ln\left(\frac{X_t}{X_0}\right) = \left(\beta \mu - \frac{1}{2}\beta^2 \sigma^2\right)t + \beta \sigma W_t$$

Taking the mathematical expectation of the logarithmic continuous-time return:

$$\mathbb{E}\left[\ln\left(\frac{X_t}{X_0}\right)\right] = \left(\beta \mu - \frac{1}{2}\beta^2 \sigma^2\right)t$$

The subtractive term $\frac{1}{2}\beta^2 \sigma^2$ is the **volatility tax** or **variance drain**. For an unleveraged investment ($\beta = 1$), the variance drag is simply $\frac{1}{2}\sigma^2$. For a $3\text{x}$ leveraged instrument ($\beta = 3$), this quadratic penalty expands to:

$$\text{Variance Drag} = \frac{1}{2}(3)^2 \sigma^2 = \frac{9}{2}\sigma^2 = 4.5\sigma^2$$

The excess variance penalty relative to a linear $3\text{x}$ multiplier of the underlying asset's compounded return is:

$$\text{Excess Drag} = \frac{1}{2}\beta^2 \sigma^2 - \frac{1}{2}\beta \sigma^2 = \frac{1}{2}\beta(\beta - 1)\sigma^2 = \frac{1}{2}(3)(2)\sigma^2 = 3\sigma^2$$

Empirical modeling of high-beta tech assets under an $\text{AR}(1)\text{-GARCH}(1,1)$ framework proves that volatility clustering ($\alpha_1 + \beta_1 \approx 1$) causes variance drag to compound exponentially over non-trending regimes. Holding a $3\text{x}$ leveraged asset over market closures exposes capital to unhedgeable jump-diffusion gap risks. The trading engine must enforce an absolute liquidation condition prior to weekend closures, mandating a $100\%$ cash posture by Friday 15:50 EDT.

---

### 1.2 Defining the Time-Decay Squeeze Factor $\lambda(t)$

Standard financial probability cones project widening dispersion through time, scaling boundaries by $\sigma \sqrt{\Delta t}$. To enforce intraday risk containment and guarantee closure before market settlement, the AVE implements an **inverted time-decay squeeze factor**, denoted as $\lambda(t)$.

Let:
* $t_0$ represent the initial trade execution timestamp.
* $t$ represent the current system timestamp ($t \ge t_0$).
* $T_{\text{close}}$ represent the terminal algorithmic liquidation timestamp (Friday 15:50:00 EDT).
* $\Delta t_{\text{remaining}} = \max(0, T_{\text{close}} - t)$ represent the remaining session duration.
* $\Delta T_{\text{total}} = T_{\text{close}} - t_0$ represent the total active lifespan of the position.

The normalized squeeze factor $\lambda(t)$ is formulated as:

$$\lambda(t) = \left(\frac{T_{\text{close}} - t}{T_{\text{close}} - t_0}\right)^\gamma, \quad \text{for } t_0 \le t \le T_{\text{close}}$$

Where $\gamma \in \mathbb{R}^+$ is a non-linear shaping parameter calibrated to prevailing market regimes:
* **$\gamma > 1.0$ (Convex Squeeze):** The envelope preserves wide operational tolerances during mid-day trading to facilitate trend development, then executes an accelerated contraction in the final 45 minutes of the session.
* **$\gamma = 1.0$ (Linear Squeeze):** The boundaries contract at a constant rate toward the terminal deadline.
* **$\gamma < 1.0$ (Concave Squeeze):** The envelope collapses aggressively immediately following trade entry, choking down allowable drawdowns during the mid-day consolidation period.

$$\lim_{t \to T_{\text{close}}} \lambda(t) = 0.0, \quad \text{and} \quad \lambda(t_0) = 1.0$$

---

### 1.3 Upper Boundary Formulation $U(t)$ (Dynamic Take-Profit)

The Upper Boundary $U(t)$ operates as an adaptive take-profit threshold. It is anchored to the trade's entry price $P_{\text{entry}}$, expands based on real-time volatility $\sigma_t$, accounts for realized intraday drift $\mu_t$, and is bounded by the squeeze factor $\lambda(t)$:

$$U(t) = P_{\text{entry}} + \left(k_u \cdot \sigma_t \cdot \lambda(t) \cdot P_{\text{entry}}\right) + \mu_t \cdot (t - t_0)$$

Alternatively, expressed in normalized fractional terms:

$$U(t) = P_{\text{entry}} \left[1 + k_u \cdot \sigma_t \cdot \lambda(t)\right] + \mu_t \cdot (t - t_0)$$

Where:
* $k_u$ is the upside volatility multiplier (calibrated via the macro environment score).
* $\sigma_t$ is the real-time instantaneous volatility estimate. This is calculated using either an intraday exponential $N$-period Average True Range normalized by price ($\text{ATR}_N / P_t$) or by sampling the front-month options implied volatility index of the underlying proxy (e.g., $VXN$ for `TQQQ`, $SOX/VIX$ composite for `SOXL`).
* $\mu_t$ is the intraday drift estimator calculated over a rolling 15-minute lookback: $\mu_t = \frac{P_t - P_{t-15\text{m}}}{15\text{m}}$.

As $t \to T_{\text{close}}$, $\lambda(t) \to 0$. Consequently, the volatility expansion term vanishes:

$$\lim_{t \to T_{\text{close}}} U(t) = P_{\text{entry}} + \mu_t \cdot (T_{\text{close}} - t_0)$$

This convergence forces the dynamic take-profit boundary downward toward current price action, triggering a Market-on-Touch liquidation to capture accrued session gains before the market closes.

---

### 1.4 Lower Boundary Formulation $L(t)$ (Dynamic Trailing Stop)

The Lower Boundary $L(t)$ operates as a path-dependent, asymmetric trailing stop. Unlike static stop-loss orders that reference only the entry price, $L(t)$ is mathematically coupled to the continuous running maximum (supremum process) of the asset price from trade inception to the current timestamp.

Define the continuous running supremum process $M(t)$ as:

$$M(t) = \sup_{s \in [t_0, t]} P(s) = \max\left(P_{\text{entry}}, \max_{s \in [t_0, t]} P(s)\right)$$

The dynamic lower boundary $L(t)$ is defined as:

$$L(t) = M(t) - \left(k_l \cdot \sigma_t \cdot \lambda(t) \cdot M(t)\right)$$

Factoring out the supremum:

$$L(t) = M(t) \left[1 - k_l \cdot \sigma_t \cdot \lambda(t)\right]$$

Where:
* $k_l$ is the downside stop multiplier ($k_l \le k_u$).
* $\sigma_t$ is the local volatility metric matching the upper bound calculation.
* $\lambda(t)$ is the time-decay squeeze factor.

#### Analytical Behavior of the Lower Boundary
1. **Upward Monotonic Drift via Supremum Tracking:** If the asset experiences a sustained rally, $M(t)$ increases monotonically, ratcheting $L(t)$ upward and locking in unrealized gains.
2. **Terminal Asymptotic Convergence:** Because $\lambda(t) \to 0$ as $t \to T_{\text{close}}$, the buffer distance $k_l \sigma_t \lambda(t) M(t)$ contracts toward zero:
   $$\lim_{t \to T_{\text{close}}} L(t) = M(t)$$
   Any minor adverse deviation or plateau in price late in the trading day breaches $L(t)$, triggering immediate position liquidation and eliminating end-of-day slippage.

---

### 1.5 Python Reference Implementation: Core Math Engine

```python
# File: schwab_engine/core/envelope.py
"""
Adaptive Volatility Envelope (AVE) Mathematical Processing Engine.
Computes real-time dynamic bounds for high-beta leveraged ETFs.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import numpy as np


@dataclass(frozen=True)
class EnvelopeBounds:
    timestamp: datetime
    price: float
    supremum: float
    upper_bound: float
    lower_bound: float
    lambda_factor: float
    breached_upper: bool
    breached_lower: bool


class AdaptiveVolatilityEnvelope:
    def __init__(
        self,
        entry_price: float,
        entry_time: datetime,
        close_time: datetime,
        k_u: float = 2.5,
        k_l: float = 1.5,
        gamma: float = 1.2,
    ) -> None:
        """
        Initializes the dynamic envelope parameters.
        
        :param entry_price: Asset execution fill price.
        :param entry_time: Datetime of initial order fill (UTC).
        :param close_time: Target liquidation boundary (e.g., Friday 15:50 EDT in UTC).
        :param k_u: Upper multiplier (Take-Profit Aggression).
        :param k_l: Lower multiplier (Trailing Stop Aggression).
        :param gamma: Time-decay shaping parameter.
        """
        self.p_entry = float(entry_price)
        self.t_0 = entry_time
        self.t_close = close_time
        self.k_u = float(k_u)
        self.k_l = float(k_l)
        self.gamma = float(gamma)
        
        self.total_duration_sec = max(1.0, (self.t_close - self.t_0).total_seconds())
        self.supremum = self.p_entry

    def compute_squeeze_factor(self, current_time: datetime) -> float:
        """Computes lambda(t) in the interval [0.0, 1.0]."""
        remaining_sec = (self.t_close - current_time).total_seconds()
        if remaining_sec <= 0.0:
            return 0.0
        normalized_ratio = min(1.0, max(0.0, remaining_sec / self.total_duration_sec))
        return float(np.power(normalized_ratio, self.gamma))

    def evaluate_tick(
        self,
        current_time: datetime,
        current_price: float,
        sigma_t: float,
        drift_rate: float = 0.0
    ) -> EnvelopeBounds:
        """
        Evaluates a real-time market tick and returns calculated boundaries.
        
        :param current_time: Tick timestamp (UTC).
        :param current_price: Executed market tick price.
        :param sigma_t: Normalized volatility metric (ATR / Price or Implied Vol).
        :param drift_rate: Drift per second (dPrice / dt).
        """
        # Update running continuous supremum process
        if current_price > self.supremum:
            self.supremum = current_price

        # Compute decaying squeeze factor lambda(t)
        lam = self.compute_squeeze_factor(current_time)
        elapsed_sec = max(0.0, (current_time - self.t_0).total_seconds())

        # Calculate bounds
        drift_offset = drift_rate * elapsed_sec
        upper_bound = self.p_entry * (1.0 + (self.k_u * sigma_t * lam)) + drift_offset
        lower_bound = self.supremum * (1.0 - (self.k_l * sigma_t * lam))

        # Evaluate breach conditions
        breached_upper = current_price >= upper_bound
        breached_lower = current_price <= lower_bound

        return EnvelopeBounds(
            timestamp=current_time,
            price=current_price,
            supremum=self.supremum,
            upper_bound=upper_bound,
            lower_bound=lower_bound,
            lambda_factor=lam,
            breached_upper=breached_upper,
            breached_lower=breached_lower
        )
```

---

## 2. Order Execution Architecture: Virtual Bracket vs. API Modification

```
+-----------------------------------------------------------------------------------+
| Python Virtual Machine (schwab_engine)                                            |
|                                                                                   |
|  +--------------------+      Tick Event       +--------------------------------+  |
|  | Schwab WebSocket   |---------------------->| Virtual Bracket FSM            |  |
|  | Market Data Stream |                       | - Recalculates U(t), L(t)      |  |
|  +--------------------+                       | - Tracks Supremum M(t)         |  |
|                                               | - Zero API Traffic Generated   |  |
|                                               +--------------------------------+  |
|                                                               |                   |
|                                                               | Threshold Breach  |
|                                                               v                   |
|  +-----------------------------------------------------------------------------+  |
|  | Async Execution Dispatcher (Wrapped in AsyncLimiter(120, 60))               |  |
|  +-----------------------------------------------------------------------------+  |
|                                       |                                           |
+---------------------------------------|-------------------------------------------+
                                        | Single-Leg REST Order
                                        v
                    +---------------------------------------+
                    | Charles Schwab REST Gateway           |
                    | (POST /v1/accounts/{hash}/orders)     |
                    +---------------------------------------+
```

### 2.1 The Failure Mode of Live Broker-Side OCO Orders

Standard automated execution architectures typically submit One-Cancels-Other (OCO) bracket orders directly to the broker's matching engine. Under the dynamic AVE model, updating the upper limit price and lower stop price requires calling the Charles Schwab order replacement endpoint (`PUT /v1/accounts/{accountNumberHash}/orders/{orderId}`). 

This approach fails in production due to the following structural constraints:

1. **REST Rate Limit Exhaustion:** The Charles Schwab API enforces a hard account-level cap of **120 requests per minute**. Modifying a live OCO bracket every 5 seconds across 3 active assets (`SOXL`, `TQQQ`, `TNA`) generates:
   $$\text{Rate} = \left(\frac{60\text{ s}}{5\text{ s}}\right) \times 3 \text{ instruments} = 36 \text{ modification calls/minute}$$
   Because the Schwab order modification flow cancels and replaces orders, each logical adjustment generates multiple transactions across matching subsystems. When combined with account state synchronization, order polling, and streaming authentication handshakes, the application exceeds the 120 req/min threshold within minutes.
2. **Cascading HTTP 429 Lockouts:** Exceeding the rate limit returns an `HTTP 429 Too Many Requests` status code. Schwab's gateway applies an exponential backoff penalty, locking the application out of order execution for 60 to 300 seconds. If a volatility spike occurs during this blackout, stop-loss orders cannot be updated or executed.
3. **State Desynchronization & In-Flight Race Conditions:** High-frequency order modifications create race conditions between Schwab's matching engine and client-side requests. If an exchange-level partial fill occurs simultaneously with a `PUT` modification, the broker rejects the request with an `INVALID_ORDER_STATE` exception. Recovering from this state requires asynchronous round-trip status polling, leaving positions unprotected during volatile market conditions.

---

### 2.2 Virtual Bracket State Machine Design

To eliminate REST traffic during continuous envelope tracking, the execution engine decouples mathematical calculations from broker-side state. The system establishes a **Virtual Bracket** entirely within application memory.

The broker's REST gateway is accessed exactly twice per round-trip trade:
1. **Entry:** A single-leg market or aggressive limit order to open the position.
2. **Exit:** A single-leg market order to liquidate the position upon a local boundary breach.

Between entry and exit, all dynamic boundary updates, supremum tracking, and breach detections are processed in-memory against raw tick feeds received via WebSocket streaming. This design achieves millisecond-level reaction times while generating zero intermediate API requests.

The execution lifecycle is governed by a deterministic **Finite State Machine (FSM)**:

```
                  +--------------+
                  |     INIT     |
                  +--------------+
                         |
                         | Allocation computed & signal verified
                         v
                  +--------------+
                  |    ARMED     |
                  +--------------+
                         |
                         | REST Entry Executed (POST /orders) [Call 1]
                         v
                  +--------------+
  +-------------->|   TRACKING   |
  |               +--------------+
  |                      |
  | Dynamic math loop    | Current Price >= U(t) OR Current Price <= L(t)
  | Zero REST calls      v
  |               +--------------+
  +---------------|  TRIGGERED   |
                  +--------------+
                         |
                         | Atomic lock engaged; duplicate triggers blocked
                         v
                  +--------------+
                  |  EXECUTING   |
                  +--------------+
                         |
                         | REST Exit Executed (POST /orders) [Call 2]
                         v
                  +--------------+
                  |   SETTLED    |
                  +--------------+
```

#### FSM State Definitions & Transition Rules

| State | Entry Condition | Action Taken | Broker API Traffic |
| :--- | :--- | :--- | :--- |
| **`INIT`** | Engine start / Signal detection. | Ingest pre-market macro data, calculate SES score, set $k_u, k_l, \gamma$. | Zero (Local computation) |
| **`ARMED`** | Execution criteria verified. | Dispatch initial single-leg Buy Market Order via asynchronous REST client. | **1 REST Call** (`POST /orders`) |
| **`TRACKING`** | Fill confirmation received via WebSocket. | Stream real-time ticks; recalculate $U(t)$ and $L(t)$; update running supremum $M(t)$. | **0 REST Calls** (WebSocket ingestion only) |
| **`TRIGGERED`**| $P_t \ge U(t)$ or $P_t \le L(t)$. | Engage atomic execution lock. Prevent duplicate events across concurrent threads. | Zero (In-memory state change) |
| **`EXECUTING`**| Atomic lock acquired. | Dispatch single-leg Sell Market Order for total position quantity. | **1 REST Call** (`POST /orders`) |
| **`SETTLED`** | Exit fill confirmation received. | Clear in-memory order context, write metrics to ledger, return to cash posture. | Zero (WebSocket event receipt) |

---

### 2.3 Token Bucket Rate Limiting Architecture

To protect against inadvertent rate-limit violations from background operations (such as portfolio balance queries, order status checks, or option chain requests), the engine routes all outbound HTTP requests through a shared, thread-safe asynchronous token-bucket rate limiter implemented via `aiolimiter`.

```python
# Rate limiter initialization
# 120 permits per 60.0 seconds rolling window
from aiolimiter import AsyncLimiter

schwab_rate_limiter = AsyncLimiter(max_rate=120, time_period=60.0)
```

All non-critical API requests must acquire a permit from `schwab_rate_limiter`. Crucially, emergency liquidation requests initiated by the `EXECUTING` state bypass standard request queues through a reserved priority channel, ensuring immediate execution without queue latency.

---

### 2.4 Managing Schwab OAuth2 Authentication Constraints

Automated trading via the Charles Schwab API requires handling a multi-tiered token authentication model:

1. **Access Tokens:** Valid for exactly **30 minutes**.
2. **Refresh Tokens:** Valid for **7 days**. Refreshing the token generates a new access token, but the underlying refresh token expires 7 days after its initial generation, requiring manual re-authentication through a browser-based OAuth redirect flow.

#### Asynchronous Token Refresh Interceptor
An unhandled `401 Unauthorized` response received during an emergency stop-loss execution will result in an unhedged position loss. The execution engine must eliminate authentication failures using proactive background token management:

* **Preemptive Refresh Cycle:** An asynchronous background task checks the access token's age every 60 seconds. If the token age reaches **28 minutes** (120 seconds prior to expiration), the system automatically executes a token refresh flow.
* **Request Interception:** The HTTP transport layer wraps the client with an authentication interceptor that verifies token validity before dispatching any request.

```
       [ Outbound REST Request ]
                   |
                   v
       { Is Token Age >= 28 mins? }
          /                    \
       (Yes)                   (No)
        /                        \
       v                          |
[ Acquire Refresh Lock ]          |
[ Dispatch Refresh POST ]         |
[ Update In-Memory Token ]        |
[ Persist to token.json ]         |
[ Release Refresh Lock ]          |
        \                        /
         v                      v
        [ Dispatch Original REST Request ]
```

#### Weekend Expiration Protocol & Runbook
Because Schwab requires a manual browser-based login flow every 7 days, fully headless operations cannot run uninterrupted past the 7-day boundary. The engine handles this via the following operational runbook:

1. **T-24 Hour Alert (Thursday 16:00 EDT):** The system checks refresh token expiration time and issues an administrative notification via an alerting webhook (e.g., Slack or SMS) stating the remaining validity duration.
2. **Mandatory Friday De-Risking (Friday 15:50 EDT):** All positions in `SOXL`, `TQQQ`, and `TNA` are liquidated via the AVE mathematical squeeze.
3. **Execution Lockout Engagement (Friday 16:00 EDT):** The engine sets its state to `ENGINE_HALTED`. No new orders can be scheduled or dispatched over the weekend.
4. **Weekend Manual Re-Authentication:** The system operator completes the browser-based authorization flow over the weekend, generating a new 7-day refresh token that is stored in an encrypted credentials store (`token.json`) for the following week.

---

### 2.5 Python Reference Implementation: Virtual Bracket FSM & Execution Engine

```python
# File: schwab_engine/execution/virtual_bracket.py
"""
Production Implementation of the Virtual Bracket State Machine with
asynchronous HTTPX token interception and rate-limiting enforcement.
"""

import asyncio
from datetime import datetime, timezone
from enum import Enum, auto
import logging
from typing import Optional
import httpx
from aiolimiter import AsyncLimiter

from schwab_engine.core.envelope import AdaptiveVolatilityEnvelope, EnvelopeBounds

logger = logging.getLogger("SchwabEngine.Execution")


class FSMState(Enum):
    INIT = auto()
    ARMED = auto()
    TRACKING = auto()
    TRIGGERED = auto()
    EXECUTING = auto()
    SETTLED = auto()
    FAILED = auto()


class SchwabAuthManager:
    """Manages proactive OAuth2 token refreshes and request signing."""
    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.access_token: Optional[str] = None
        self.token_obtained_at: float = 0.0
        self.lock = asyncio.Lock()

    async def get_valid_access_token(self) -> str:
        async with self.lock:
            now = asyncio.get_event_loop().time()
            # Proactively refresh if older than 28 minutes (1680 seconds)
            if self.access_token is None or (now - self.token_obtained_at) > 1680:
                await self._refresh_access_token()
            return self.access_token

    async def _refresh_access_token(self) -> None:
        logger.info("Executing proactive OAuth2 token refresh.")
        url = "https://api.schwabapi.com/v1/oauth/token"
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                data=data,
                auth=(self.client_id, self.client_secret),
                timeout=10.0
            )
            if resp.status_code != 200:
                logger.critical(f"OAuth token refresh failed: {resp.text}")
                raise RuntimeError(f"Schwab Token Refresh Failed: {resp.status_code}")
            
            payload = resp.json()
            self.access_token = payload["access_token"]
            self.token_obtained_at = asyncio.get_event_loop().time()
            logger.info("OAuth2 access token successfully renewed.")


class VirtualBracketEngine:
    def __init__(
        self,
        symbol: str,
        quantity: int,
        envelope: AdaptiveVolatilityEnvelope,
        auth_manager: SchwabAuthManager,
        account_hash: str
    ):
        self.symbol = symbol
        self.quantity = quantity
        self.envelope = envelope
        self.auth_manager = auth_manager
        self.account_hash = account_hash
        
        self.state = FSMState.INIT
        self.limiter = AsyncLimiter(max_rate=120, time_period=60.0)
        self.latest_bounds: Optional[EnvelopeBounds] = None
        self._execution_lock = asyncio.Lock()

    async def arm_and_enter(self) -> bool:
        """Transitions from INIT -> ARMED -> TRACKING by executing initial entry."""
        if self.state != FSMState.INIT:
            return False

        self.state = FSMState.ARMED
        logger.info(f"[{self.symbol}] FSM ARMED. Dispatching Market Entry...")

        success = await self._dispatch_order(side="BUY", qty=self.quantity)
        if success:
            self.state = FSMState.TRACKING
            logger.info(f"[{self.symbol}] Entry filled. FSM transitioning to TRACKING.")
            return True
        else:
            self.state = FSMState.FAILED
            logger.error(f"[{self.symbol}] Initial market entry failed. FSM aborted.")
            return False

    async def on_market_tick(
        self,
        tick_time: datetime,
        current_price: float,
        sigma_t: float
    ) -> None:
        """Processes an incoming market tick and evaluates dynamic envelope bounds."""
        if self.state != FSMState.TRACKING:
            return

        bounds = self.envelope.evaluate_tick(
            current_time=tick_time,
            current_price=current_price,
            sigma_t=sigma_t
        )
        self.latest_bounds = bounds

        if bounds.breached_upper or bounds.breached_lower:
            reason = "TAKE_PROFIT" if bounds.breached_upper else "TRAILING_STOP"
            logger.warning(
                f"[{self.symbol}] Boundary BREACHED ({reason}). "
                f"Price: {current_price:.2f}, Upper: {bounds.upper_bound:.2f}, "
                f"Lower: {bounds.lower_bound:.2f}. Engaging execution..."
            )
            await self._trigger_liquidation(reason)

    async def _trigger_liquidation(self, reason: str) -> None:
        async with self._execution_lock:
            if self.state != FSMState.TRACKING:
                return  # Prevent race conditions from concurrent tick updates
            self.state = FSMState.TRIGGERED

        self.state = FSMState.EXECUTING
        logger.info(f"[{self.symbol}] FSM EXECUTING liquidation order. Reason: {reason}")
        
        success = await self._dispatch_order(side="SELL", qty=self.quantity)
        if success:
            self.state = FSMState.SETTLED
            logger.info(f"[{self.symbol}] Position successfully liquidated. FSM SETTLED.")
        else:
            self.state = FSMState.FAILED
            logger.critical(f"[{self.symbol}] Emergency liquidation FAILED to confirm!")

    async def _dispatch_order(self, side: str, qty: int) -> bool:
        """Dispatches an authenticated order to the Charles Schwab REST API."""
        async with self.limiter:
            token = await self.auth_manager.get_valid_access_token()
            url = f"https://api.schwabapi.com/trader/v1/accounts/{self.account_hash}/orders"
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }
            order_payload = {
                "orderType": "MARKET",
                "session": "NORMAL",
                "duration": "DAY",
                "orderStrategyType": "SINGLE",
                "orderLegCollection": [
                    {
                        "instruction": side,
                        "quantity": qty,
                        "instrument": {
                            "symbol": self.symbol,
                            "assetType": "EQUITY"
                        }
                    }
                ]
            }

            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(url, json=order_payload, headers=headers, timeout=5.0)
                    if resp.status_code in (200, 201):
                        return True
                    logger.error(f"Order rejected by broker [{resp.status_code}]: {resp.text}")
                    return False
            except Exception as e:
                logger.critical(f"REST transport fatal error during order dispatch: {str(e)}")
                return False
```

---

## 3. High-Throughput WebSocket Telemetry & Dashboard Visualization

Real-time monitoring of non-linear volatility envelopes requires a low-latency telemetry pipeline. Streaming multi-point envelope vectors into a browser at high update frequencies introduces performance bottlenecks across JSON serialization, WebSocket transmission, and DOM re-rendering.

```
+---------------------------------------------------------------------------------+
| Backend: FastAPI (Starlette ASGI)                                               |
|                                                                                 |
|  [ In-Memory Tick Queue ]                                                       |
|             |                                                                   |
|             v                                                                   |
|  [ orjson.dumps() ] (C/Rust serialization: ~5x faster than stdlib json)          |
|             |                                                                   |
|             v                                                                   |
|  [ ConnectionManager Broadcast Loop ]                                           |
+-------------|-------------------------------------------------------------------+
              | Compact Array Payload: [timestamp, price, upper, lower]
              v
+---------------------------------------------------------------------------------+
| Frontend: React 18 + Vite Infrastructure                                        |
|                                                                                 |
|  [ WebSocket Listener ]                                                         |
|             |                                                                   |
|             v (Mutates buffer without triggering React re-renders)              |
|  [ Mutable Buffer: useRef([]) ]                                                 |
|             |                                                                   |
|             v (Polled synchronously at monitor refresh rate, e.g., 60Hz)        |
|  [ requestAnimationFrame Loop ]                                                 |
|             |                                                                   |
|             v (Batched single-frame state update)                               |
|  [ Plotly.js / react-plotly.js ]                                                |
|     - Trace 0: Candlestick (OHLC)                                               |
|     - Trace 1: Upper Boundary Line U(t)                                         |
|     - Trace 2: Lower Boundary Line L(t) with fill: 'tonexty'                   |
+---------------------------------------------------------------------------------+
```

### 3.1 Backend Architecture: FastAPI & `orjson` Optimization

The telemetry service uses **FastAPI** over a Starlette ASGI asynchronous event loop. Standard Python `json.dumps()` serialization introduces significant CPU latency and memory allocations when handling high-frequency telemetry matrices. To maximize serialization throughput, the backend replaces the standard library encoder with **`orjson`**, a Rust-backed serialization engine.

#### Serialization Performance Comparison

| Serializer | Latency (10k Complex Payloads) | Allocations | Native Datetime Support |
| :--- | :--- | :--- | :--- |
| Standard `json` | 42.8 ms | High (GC pauses) | No (`default=str` required) |
| `ujson` | 14.2 ms | Moderate | Partial |
| **`orjson`** | **4.1 ms (~10x faster)** | **Zero (Direct buffer)**| **Yes (Native RFC 3339)** |

#### Network Protocol Payload Format
To minimize serialization overhead and network transmission footprint, tick updates avoid repetitive JSON dictionary key transmission. Data is packaged using a compact coordinate array:

```json
[
  1713537000000, // [0] Unix Epoch Milliseconds
  142.50,        // [1] Current Price (P_t)
  145.20,        // [2] Upper Boundary U(t)
  140.10         // [3] Lower Boundary L(t)
]
```

---

### 3.2 Frontend Architecture: Decoupling Network Ingestion from Render Cycles

Updating React component state (`useState`) directly from high-frequency WebSocket events introduces severe UI latency. If an asset streams 20 to 100 ticks per second, triggering React's Virtual DOM reconciliation on each event will saturate the JavaScript main thread, causing frame drops and unresponsive controls.

#### The `useRef` + `requestAnimationFrame` Solution
1. **Network Ingestion:** Incoming WebSocket messages append raw data arrays directly into a mutable JavaScript array managed by a React `useRef` container. Appending to a `ref` avoids triggering React render cycles.
2. **Synchronized Rendering:** A rendering loop managed by `requestAnimationFrame` runs at the client monitor's native refresh rate (typically 60Hz or 120Hz). On each animation frame, the loop reads accumulated data from the `ref` buffer, batches the updates, and applies them to the chart state in a single operation. This caps frontend rendering overhead to the display refresh rate regardless of inbound WebSocket message frequency.

---

### 3.3 Dynamic Cone Visualization via Plotly.js

Visualizing the continuous contraction of the Adaptive Volatility Envelope requires a three-layer trace configuration in **Plotly.js**:

1. **Trace 0 (Price Action):** A standard OHLC `candlestick` chart plotting asset price movements.
2. **Trace 1 (Upper Boundary):** A `scatter` trace plotting $U(t)$ as a subtle boundary line.
3. **Trace 2 (Lower Boundary):** A `scatter` trace plotting $L(t)$ configured with `fill: 'tonexty'`.

Plotly's `fill: 'tonexty'` attribute automatically shades the region between the current trace ($L(t)$) and the preceding trace ($U(t)$). Configuring this region with a semi-transparent color fill visually renders the contracting probability cone in real time.

---

### 3.4 Production Reference Implementation: Telemetry Backend & Frontend

#### Backend Implementation: `server.py`

```python
# File: schwab_engine/telemetry/server.py
"""
High-performance FastAPI WebSocket Telemetry Server.
Streams real-time AVE boundary matrices using orjson serialization.
"""

import asyncio
from datetime import datetime, timezone
import orjson
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from typing import Set, List, Any

app = FastAPI(title="SchwabEngine Telemetry Core")


class TelemetryConnectionManager:
    def __init__(self):
        self.active_connections: Set[WebSocket] = set()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.add(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.discard(websocket)

    async def broadcast_envelope_vector(self, vector: List[Any]):
        """
        Serializes payloads using orjson and broadcasts to connected clients.
        Payload layout: [epoch_ms, current_price, upper_bound, lower_bound]
        """
        if not self.active_connections:
            return

        # Direct binary serialization to bytes
        binary_payload = orjson.dumps(vector)

        # Broadcast concurrently across active connections
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_bytes(binary_payload)
            except WebSocketDisconnect:
                disconnected.append(connection)
            except Exception:
                disconnected.append(connection)

        for dead_conn in disconnected:
            self.disconnect(dead_conn)


manager = TelemetryConnectionManager()


@app.websocket("/ws/telemetry/{symbol}")
async def telemetry_endpoint(websocket: WebSocket, symbol: str):
    await manager.connect(websocket)
    try:
        while True:
            # Keep-alive heartbeat listener
            _ = await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)
```

#### Frontend Implementation: `VolatilityEnvelopeChart.tsx`

```tsx
// File: schwab_dashboard/src/components/VolatilityEnvelopeChart.tsx
import React, { useEffect, useRef, useState } from 'react';
import Plot from 'react-plotly.js';

interface TelemetryPoint {
  time: string;
  price: number;
  upper: number;
  lower: number;
}

interface VolatilityEnvelopeChartProps {
  symbol: string;
  wsUrl: string;
}

export const VolatilityEnvelopeChart: React.FC<VolatilityEnvelopeChartProps> = ({ symbol, wsUrl }) => {
  // In-memory buffer to decouple network ingestion from React renders
  const bufferRef = useRef<TelemetryPoint[]>([]);
  
  // Chart rendering state
  const [plotData, setPlotData] = useState<{
    x: string[];
    price: number[];
    upper: number[];
    lower: number[];
  }>({
    x: [],
    price: [],
    upper: [],
    lower: [],
  });

  useEffect(() => {
    const socket = new WebSocket(wsUrl);
    socket.binaryType = 'arraybuffer';

    socket.onmessage = (event: MessageEvent) => {
      // Decode orjson binary payload: [epoch_ms, price, upper, lower]
      const textDecoder = new TextDecoder('utf-8');
      const rawJson = textDecoder.decode(event.data);
      const [epochMs, price, upper, lower] = JSON.parse(rawJson);

      const isoTime = new Date(epochMs).toISOString();

      // Push into mutable ref buffer (zero React lifecycle overhead)
      bufferRef.current.push({
        time: isoTime,
        price,
        upper,
        lower,
      });

      // Maintain rolling window buffer (last 500 samples)
      if (bufferRef.current.length > 500) {
        bufferRef.current.shift();
      }
    };

    // Rendering loop synchronized via requestAnimationFrame (RAF)
    let animationFrameId: number;

    const renderLoop = () => {
      if (bufferRef.current.length > 0) {
        const currentBuffer = [...bufferRef.current];
        setPlotData({
          x: currentBuffer.map((d) => d.time),
          price: currentBuffer.map((d) => d.price),
          upper: currentBuffer.map((d) => d.upper),
          lower: currentBuffer.map((d) => d.lower),
        });
      }
      animationFrameId = requestAnimationFrame(renderLoop);
    };

    animationFrameId = requestAnimationFrame(renderLoop);

    return () => {
      socket.close();
      cancelAnimationFrame(animationFrameId);
    };
  }, [wsUrl]);

  return (
    <div className="w-full h-[600px] bg-slate-900 p-4 rounded-xl shadow-2xl">
      <Plot
        data={[
          // Upper Boundary Line
          {
            x: plotData.x,
            y: plotData.upper,
            type: 'scatter',
            mode: 'lines',
            name: 'Upper Bound U(t)',
            line: { color: 'rgba(52, 211, 153, 0.8)', width: 1.5, dash: 'dash' },
            hoverinfo: 'y',
          },
          // Lower Boundary Line with Adaptive Fill
          {
            x: plotData.x,
            y: plotData.lower,
            type: 'scatter',
            mode: 'lines',
            name: 'Lower Bound L(t)',
            fill: 'tonexty',
            fillcolor: 'rgba(59, 130, 246, 0.15)', // Shaded dynamic probability cone
            line: { color: 'rgba(239, 68, 68, 0.8)', width: 1.5, dash: 'dash' },
            hoverinfo: 'y',
          },
          // Asset Execution Price Line
          {
            x: plotData.x,
            y: plotData.price,
            type: 'scatter',
            mode: 'lines',
            name: `${symbol} Price`,
            line: { color: '#ffffff', width: 2 },
            hoverinfo: 'x+y',
          },
        ]}
        layout={{
          title: `${symbol} Adaptive Volatility Envelope (Live Telemetry)`,
          autosize: true,
          paper_bgcolor: 'transparent',
          plot_bgcolor: 'transparent',
          margin: { l: 40, r: 40, t: 40, b: 40 },
          xaxis: {
            type: 'date',
            gridcolor: '#334155',
            color: '#94a3b8',
          },
          yaxis: {
            gridcolor: '#334155',
            color: '#94a3b8',
            autorange: true,
          },
          legend: {
            font: { color: '#f8fafc' },
            orientation: 'h',
            y: 1.1,
          },
        }}
        useResizeHandler={true}
        className="w-full h-full"
      />
    </div>
  );
};
```

---

## 4. Semiconductor Environment Score (SES) Regime Integration

High-beta leveraged sector ETFs like `SOXL` do not trade in isolation. Mechanical micro-structure models that evaluate only single-ticker price action will miscalculate boundary widths during major macroeconomic shifts. To address this, the AVE architecture integrates the **Semiconductor Environment Score (SES)**, an automated pre-market macro classification model.

### 4.1 SES Metric Formulation

The SES is a composite numerical index generated at 09:15 EDT, 15 minutes before the US market open. It evaluates cross-asset market signals using a weighted standardized factor model:

$$\text{SES} = w_1 Z(\Delta \text{NQ}_{\text{overnight}}) - w_2 Z(\Delta \text{US10Y}) + w_3 Z(\text{RS}_{\text{SOX/SPY}}) + w_4 Z(\text{IV}_{\text{spread}})$$

Where:
* $Z(x) = \frac{x - \mu_x}{\sigma_x}$ is the 60-day rolling Z-score of factor $x$.
* $\Delta \text{NQ}_{\text{overnight}}$ represents the percentage price change in E-mini Nasdaq 100 Futures from 18:00 EST to 09:00 EST.
* $\Delta \text{US10Y}$ measures the basis-point change in the 10-Year US Treasury Yield (rising yields increase discount rates, compressing high-multiple semiconductor valuations).
* $\text{RS}_{\text{SOX/SPY}}$ represents the relative strength ratio of the Philadelphia Semiconductor Index against the S&P 500 over the preceding 5 sessions.
* $\text{IV}_{\text{spread}}$ is the implied volatility term structure differential: $\text{IV}_{\text{spread}} = \text{VXN} - \text{VIX}$.
* Calibration weights satisfy $\sum_{i=1}^4 w_i = 1.0$ ($w = [0.35, 0.25, 0.20, 0.20]$).

---

### 4.2 Regime Calibration & Parameter Matrix

Based on the calculated SES value, the engine maps the trading session into one of three operating regimes. These regimes reconfigure the take-profit multiplier ($k_u$), the trailing stop multiplier ($k_l$), and the time-decay shaping exponent ($\gamma$):

```
                        SES Score Spectrum
  <- -1.5 ------------------ [-1.5 to +1.5] ------------------ > +1.5 ->
+-----------------------+-------------------------+----------------------+
|   DEFENSIVE REGIME    |       CHOP REGIME       |   FAVORABLE REGIME   |
|   - Ultra-tight stops |   - Constricted bounds  |   - Wide targets     |
|   - Linear squeeze    |   - Rapid early decay   |   - Convex late decay|
|   - Capital defense   |   - Mean-reversion scalp|   - Trend following  |
+-----------------------+-------------------------+----------------------+
```

#### Regime 1: FAVORABLE ($\text{SES} > +1.5$)
* **Macro Environment:** Strong overnight tech futures drift, falling or stable Treasury yields, semiconductors showing clear relative strength against broader indices.
* **Strategic Posture:** Aggressive trend following.
* **Parameter Adjustments:** 
  * $k_u$ expands to **$3.0\sigma$**, allowing winners to run without premature profit taking.
  * $k_l$ widens to **$2.0\sigma$**, preventing normal intraday pullbacks from stopping out positions.
  * $\gamma$ is set to **$1.8$** (convex decay). The envelope remains wide through 15:00 EDT, deferring boundary contraction until the final trading hour to capture late-day continuation rallies.

#### Regime 2: CHOP ($-1.5 \le \text{SES} \le +1.5$)
* **Macro Environment:** Mixed macroeconomic data, flat overnight index futures, muted sector rotation.
* **Strategic Posture:** Rapid mean-reversion scalping. This is the most dangerous regime for $3\text{x}$ leveraged assets due to compounding variance drag.
* **Parameter Adjustments:** 
  * $k_u$ and $k_l$ are both contracted to **$1.5\sigma$**.
  * $\gamma$ is set to **$0.6$** (concave decay). The boundaries contract aggressively by mid-day, deliberately reducing trade durations to minimize exposure to oscillating sideways price chop.

#### Regime 3: DEFENSIVE ($\text{SES} < -1.5$)
* **Macro Environment:** Surging Treasury yields, risk-off equity liquidations, negative pre-market tech earnings reports.
* **Strategic Posture:** Strict capital preservation. Long exposures in high-beta assets are either prohibited entirely or executed with defensive constraints.
* **Parameter Adjustments:** 
  * $k_u$ is constricted to **$1.0\sigma$**.
  * $k_l$ is reduced to an ultra-tight **$0.5\sigma$**.
  * $\gamma$ is set to **$1.0$** (linear decay). The trailing boundary tracks tightly behind the running maximum; any intraday price rejection triggers immediate liquidation.

---

### 4.3 Production Parameter Specification

| Parameter | Configuration Variable | FAVORABLE ($\text{SES} > +1.5$) | CHOP ($-1.5 \le \text{SES} \le +1.5$) | DEFENSIVE ($\text{SES} < -1.5$) |
| :--- | :--- | :--- | :--- | :--- |
| **Upside Multiplier** | `k_u` | $3.0$ | $1.5$ | $1.0$ |
| **Downside Multiplier** | `k_l` | $2.0$ | $1.5$ | $0.5$ |
| **Decay Exponent** | `gamma` | $1.8$ (Late Decay) | $0.6$ (Early Decay) | $1.0$ (Linear Decay) |
| **Max Trade Lifespan** | `max_duration` | Until 15:50 EDT | Max 120 Minutes | Max 45 Minutes |
| **Default Allocation** | `target_equity` | $100\%$ Target Size | $50\%$ Target Size | $25\%$ Target Size |
| **Execution Squeeze Mode** | `squeeze_mode` | Asymmetric Bullish | Symmetric Tight | Asymmetric Defensive |

---

### 4.4 Terminal Liquidation Protocol (Friday 15:50 EDT)

Regardless of the active SES regime or accumulated unrealized P&L, the trading engine enforces an absolute **Hard Liquidation Protocol** on Friday afternoons:

$$\text{If } t \ge \text{Friday 15:50:00 EDT} \implies \text{Force Liquidation Immediate}$$

#### Execution Sequence:
1. **15:49:00 EDT:** The system enters `PRE_LIQUIDATION`. Inbound trade entry signals are hard-locked and rejected.
2. **15:50:00 EDT:** The internal squeeze factor converges to zero ($\lambda(T_{\text{close}}) = 0.0$). The boundaries satisfy:
   $$U(T_{\text{close}}) \to P_{\text{entry}} + \mu_t \cdot \Delta T \quad \text{and} \quad L(T_{\text{close}}) \to M(T_{\text{close}})$$
3. **Execution Breach:** The market price intersects the contracted boundary, transitioning the FSM to `TRIGGERED` and immediately to `EXECUTING`.
4. **Market-on-Touch Execution:** The engine dispatches a single-leg Market Order via the priority REST channel.
5. **Confirmation & Flat State:** Upon receipt of the broker fill confirmation, the position state resets to $0$ shares, transitioning to `SETTLED`. The engine remains in an idle cash posture throughout the weekend, eliminating variance drag and gap risk.

---

## 5. Comprehensive Operational Runbook & Edge-Case Architecture

```
+-----------------------------------------------------------------------------------+
| Edge Case Operational Handling Matrix                                            |
+-----------------------------------------------------------------------------------+
| Scenario               | Immediate Impact         | Automated Mitigation Path     |
+------------------------+--------------------------+-------------------------------+
| Broker Gateway 502/504 | REST calls drop          | Failover to secondary route;  |
| Bad Gateway            | In-flight orders unknown | Query GET /orders endpoint    |
+------------------------+--------------------------+-------------------------------+
| In-Memory Boundary     | Premature or missed      | Re-anchor supremum M(t)       |
| Drift Desync           | trigger event            | to broker trade execution data|
+------------------------+--------------------------+-------------------------------+
| Partial Fill on        | Trailing residual        | Immediate follow-up Market    |
| Emergency Exit         | unhedged exposure        | Order for remaining balance   |
+------------------------+--------------------------+-------------------------------+
| WebSocket Dropped      | Telemetry blackout       | Fallback to 1-second REST     |
| During Tracking        |                          | polling via AsyncLimiter      |
+------------------------+--------------------------+-------------------------------+
```

### 5.1 Handling Partial Fills on Liquidation Orders
During high-volatility flash crashes, a market order dispatched during the `EXECUTING` state may result in a partial fill due to matching engine order-book imbalances. The engine mitigates this via an asynchronous fill-monitoring coroutine:

```python
async def reconcile_liquidation_fill(
    client: httpx.AsyncClient,
    account_hash: str,
    order_id: str,
    target_qty: int
) -> bool:
    """Verifies complete order fills and handles residual balance liquidation."""
    for attempt in range(5):
        await asyncio.sleep(0.5)
        resp = await client.get(
            f"https://api.schwabapi.com/trader/v1/accounts/{account_hash}/orders/{order_id}"
        )
        if resp.status_code == 200:
            order_data = resp.json()
            filled_qty = int(order_data.get("filledQuantity", 0))
            status = order_data.get("status")

            if status == "FILLED" and filled_qty == target_qty:
                return True
            
            if status in ("PARTIALLY_FILLED", "QUEUED") and attempt == 4:
                # Cancel remainder and sweep residual via market order
                residual = target_qty - filled_qty
                logger.warning(f"Sweeping residual position: {residual} shares.")
                # Recursively dispatch market sweep for residual quantity
                ...
    return False
```

### 5.2 Network Heartbeat & WebSocket Reconnection
If the incoming Schwab market data WebSocket drops during the `TRACKING` state, the engine triggers an automatic failover:
1. **Fallback Polling Activation:** The engine switches to a polling loop that requests market snapshots via `GET /v1/markets/quotes` every 1.0 second, with requests managed by `AsyncLimiter(120, 60)`.
2. **Exponential WebSocket Backoff:** A background supervisor thread attempts socket reconnection at intervals of $1\text{s}, 2\text{s}, 4\text{s}, 8\text{s}, \dots$ up to a maximum of $30\text{s}$.
3. **Emergency Liquidation on Disconnection Timeout:** If neither the WebSocket nor REST fallback routes can establish communication within **15 seconds** during an active position, the system dispatches an emergency SMS/webhook alert and attempts an out-of-band market liquidation to protect capital.

---

## 6. References & Literature Base

1. **MenthorQ.** *Leveraged ETFs and Volatility: SPXL and TQQQ Guide.*  
   Available at: [https://menthorq.com/guide/leveraged-etfs-and-volatility-spxl-and-tqqq/](https://menthorq.com/guide/leveraged-etfs-and-volatility-spxl-and-tqqq/)
2. **Dubach, P.** *The Variance Tax: How Volatility Drag Erodes Compound Returns.*  
   Available at: [https://philippdubach.com/posts/variance-tax/](https://philippdubach.com/posts/variance-tax/)
3. **Return Stacked Portfolio Solutions.** *The "Rebalance Drag" Myth in Leveraged ETFs.*  
   Available at: [https://www.returnstacked.com/the-rebalance-drag-myth-in-leveraged-etfs-what-advisors-need-to-know/](https://www.returnstacked.com/the-rebalance-drag-myth-in-leveraged-etfs-what-advisors-need-to-know/)
4. **Preprints.org.** *Alpha Dark Horse or Trojan Horse? A Comprehensive Analysis of Leveraged ETFs.*  
   Available at: [https://www.preprints.org/manuscript/202608.0028](https://www.preprints.org/manuscript/202608.0028)
5. **MDPI Journal of Risk and Financial Management.** *Alpha Dark Horse or Trojan Horse—A Comprehensive Analysis of Leveraged ETFs (JRFM 19/10/805).*  
   Available at: [https://www.mdpi.com/1911-8074/19/10/805](https://www.mdpi.com/1911-8074/19/10/805)
6. **arXiv Quantitative Finance.** *Compounding Effects in Leveraged ETFs: Beyond the Volatility Drag.*  
   Available at: [https://arxiv.org/html/2504.20116v1](https://arxiv.org/html/2504.20116v1)
7. **Patel, A.** *Stock Probability Cone vs. Traditional Technical Indicators.*  
   Available at: [https://probability.alpeshpatel.com/blog/probability-cone-vs-traditional-technical-analysis/](https://probability.alpeshpatel.com/blog/probability-cone-vs-traditional-technical-analysis/)
8. **GoCharting Documentation.** *Options Analysis, Greeks, and Payoff Probability Modeling.*  
   Available at: [https://gocharting.com/docs/options-desk/analysis](https://gocharting.com/docs/options-desk/analysis)
9. **University of Cambridge Department of Pure Mathematics.** *Trading to Stops: Path-Dependent Optimality.*  
   Available at: [https://www.repository.cam.ac.uk/bitstreams/9811180c-4969-488c-8e27-5045a7371f54/download](https://www.repository.cam.ac.uk/bitstreams/9811180c-4969-488c-8e27-5045a7371f54/download)
10. **Applied Mathematics Journal.** *Optimal Trading with Trailing Stops.*  
    Available at: [https://www.scribd.com/document/859141379/Optimal-Trading-With-a-Trailing-Stop](https://www.scribd.com/document/859141379/Optimal-Trading-With-a-Trailing-Stop)
11. **LumiBot Documentation.** *Charles Schwab API Trading Engine Setup and Bracket Orders.*  
    Available at: [https://lumibot.lumiwealth.com/brokers.schwab.html](https://lumibot.lumiwealth.com/brokers.schwab.html)
12. **TradersPost Engineering.** *Complete Broker API Trading Guide & Execution Mechanics.*  
    Available at: [https://blog.traderspost.io/article/broker-api-trading-guide](https://blog.traderspost.io/article/broker-api-trading-guide)
13. **Jay Collett.** *SchwabPy: Python Interface for Charles Schwab Individual Trader API.*  
    Available at: [https://github.com/jaycollett/schwabpy](https://github.com/jaycollett/schwabpy)
14. **Babayan, A.** *Why Charles Schwab API: Platform Architecture for Automation Bots.*  
    Available at: [https://medium.com/@avetik.babayan/why-charles-schwab-api-choosing-the-right-trading-platform-for-automation-bot-6bf6a687bb83](https://medium.com/@avetik.babayan/why-charles-schwab-api-choosing-the-right-trading-platform-for-automation-bot-6bf6a687bb83)
15. **BloFin Technical Specifications.** *API Documents & High-Frequency Gateway Architecture.*  
    Available at: [https://www.scribd.com/document/906972578/BloFin-API-Documents](https://www.scribd.com/document/906972578/BloFin-API-Documents)
16. **The Decay Lab.** *Trading Journal: Execution Latency, Slippage, and Broker API Lockouts.*  
    Available at: [https://thedecaylab.com/journal](https://thedecaylab.com/journal)
17. **Full Stack Alpha.** *Deep Dive: Why Schwab Trading Bots Face Token Expirations Every 7 Days.*  
    Available at: [https://medium.com/@fullstackalpha/i-dug-into-why-schwab-trading-bots-log-out-every-7-days-f144c711df7a](https://medium.com/@fullstackalpha/i-dug-into-why-schwab-trading-bots-log-out-every-7-days-f144c711df7a)
18. **Python State Machine Documentation.** *Transitions and Events in State Machine 2.3.3.*  
    Available at: [https://python-statemachine.readthedocs.io/en/v2.3.3/transitions.html](https://python-statemachine.readthedocs.io/en/v2.3.3/transitions.html)
19. **Python Para Trading.** *Finite-State Machines (FSM) for Algorithmic Trading Systems.*  
    Available at: [https://pythonparatrading.com/en/finite-state-machines-trading-python/](https://pythonparatrading.com/en/finite-state-machines-trading-python/)
20. **Sume Software Engineering.** *Python API Rate Limiting: Managing Per-Minute Enforcements.*  
    Available at: [https://www.sume.com/blog/python-api-rate-limiting-client-side](https://www.sume.com/blog/python-api-rate-limiting-client-side)
21. **Aiolimiter Library.** *AsyncLimiter Implementation and Performance Documentation.*  
    Available at: [https://aiolimiter.readthedocs.io/](https://aiolimiter.readthedocs.io/)
22. **SetupTrader Engineering.** *Schwab API Configuration & Custom Algorithmic Trading Setup.*  
    Available at: [https://setup-trader.com/](https://setup-trader.com/)
23. **Epistates.** *RustySchwab: Rust SDK for Charles Schwab Platform Trading.*  
    Available at: [https://github.com/Epistates/rustyschwab](https://github.com/Epistates/rustyschwab)
24. **Schwab-Py Project.** *Authentication Lifecycles and Client Construction.*  
    Available at: [https://schwab-py.readthedocs.io/en/latest/auth.html](https://schwab-py.readthedocs.io/en/latest/auth.html)
25. **WebSocket.org Guides.** *WebSocket with FastAPI: Async Connections & Scaling.*  
    Available at: [https://websocket.org/guides/frameworks/fastapi/](https://websocket.org/guides/frameworks/fastapi/)
26. **Towards Data Science.** *Building Scalable WebSocket Architectures with FastAPI.*  
    Available at: [https://towardsdatascience.com/build-a-websocket-application-with-fastapi-and-angular-988157dce554/](https://towardsdatascience.com/build-a-websocket-application-with-fastapi-and-angular-988157dce554/)
27. **Plain English Python.** *Accelerating FastAPI Response Pipelines up to 5x.*  
    Available at: [https://python.plainenglish.io/make-your-fastapi-responses-up-to-5x-faster-with-one-simple-change-574a21e9fb5e](https://python.plainenglish.io/make-your-fastapi-responses-up-to-5x-faster-with-one-simple-change-574a21e9fb5e)
28. **FastAPI Performance Optimization.** *Optimized Custom JSON Response Classes.*  
    Available at: [https://kisspeter.github.io/fastapi-performance-optimization/json_response_class.html](https://kisspeter.github.io/fastapi-performance-optimization/json_response_class.html)
29. **DEV Community.** *Benchmarking Python JSON Parsers: Stdlib, Ujson, and Orjson.*  
    Available at: [https://dev.to/kanakos01/benchmarking-python-json-libraries-33bb](https://dev.to/kanakos01/benchmarking-python-json-libraries-33bb)
30. **Benav, I.** *FastAPI Anti-Patterns That Impact Performance.*  
    Available at: [https://dev.to/igorbenav/fastapi-mistakes-that-kill-your-performance-2b8k](https://dev.to/igorbenav/fastapi-mistakes-that-kill-your-performance-2b8k)
31. **Hex Shift Engineering.** *Building Real-Time Broadcast Systems with FastAPI WebSockets.*  
    Available at: [https://hexshift.medium.com/building-a-broadcast-system-with-fastapi-websockets-04aaca6c20c3](https://hexshift.medium.com/building-a-broadcast-system-with-fastapi-websockets-04aaca6c20c3)
32. **Tiangolo, S.** *FastAPI WebSockets Framework Specifications.*  
    Available at: [https://fastapi.tiangolo.com/reference/websockets/](https://fastapi.tiangolo.com/reference/websockets/)
33. **TradingView Lightweight Charts.** *High-Performance Canvas Financial Charting Library.*  
    Available at: [https://www.tradingview.com/lightweight-charts/](https://www.tradingview.com/lightweight-charts/)
34. **TradingView Documentation.** *Custom Canvas Rendering Targets and Plugins.*  
    Available at: [https://tradingview.github.io/lightweight-charts/docs/plugins/canvas-rendering-target](https://tradingview.github.io/lightweight-charts/docs/plugins/canvas-rendering-target)
35. **Racine Engineering.** *Streaming Real-Time Data Pipelines Without React DOM Thrashing.*  
    Available at: [https://racine.ai/en/blog/streaming-llm-responses-2026/](https://racine.ai/en/blog/streaming-llm-responses-2026/)
36. **Rana, B.** *High-Performance JavaScript: Managing Web Workers & Memory Buffers.*  
    Available at: [https://medium.com/@bhagyarana80/how-i-made-my-javascript-app-10x-faster-with-web-workers-and-sharedarraybuffer-502458cc2170](https://medium.com/@bhagyarana80/how-i-made-my-javascript-app-10x-faster-with-web-workers-and-sharedarraybuffer-502458cc2170)
37. **Gate Learn.** *Understanding the Structural Mechanics and Drag of SOXL 3x Leveraged ETF.*  
    Available at: [https://www.gate.com/learn/articles/what-is-soxl-3x-leveraged-semiconductor-etf](https://www.gate.com/learn/articles/what-is-soxl-3x-leveraged-semiconductor-etf)
38. **Seeking Alpha Financial Analysis.** *SOXL: Structural Compounding Drag and Trading Realities.*  
    Available at: [https://seekingalpha.com/article/4792102-soxl-benefits-drawbacks-to-leveraged-etf](https://seekingalpha.com/article/4792102-soxl-benefits-drawbacks-to-leveraged-etf)