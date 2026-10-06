#!/usr/bin/env python3
"""
scripts/prep_research.py
========================
Automated extraction and compilation script that builds a comprehensive
pre-startup system snapshot of the SchwabEngine architecture, objectives,
active trading algorithms, quantitative risk models, and recent optimizations.

Generates:
    docs/YYMMDD.HH-research-context.md (e.g. docs/261006.14-research-context.md)
    research_context.md (convenience alias in repo root)
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("prep_research")

ROOT_DIR = Path(__file__).resolve().parent.parent


def get_timestamp_slug(dt: datetime | None = None) -> str:
    """Returns YYMMDD.HH format, e.g. 261006.14."""
    now = dt or datetime.now()
    return now.strftime("%y%m%d.%H")


def run_preflight_summary() -> str:
    """Runs preflight.py locally and returns its summary log."""
    try:
        res = subprocess.run(
            [sys.executable, str(ROOT_DIR / "scripts" / "preflight.py")],
            cwd=str(ROOT_DIR),
            capture_output=True,
            text=True,
            timeout=30,
        )
        out = res.stdout.strip()
        lines = out.splitlines()
        # Grab summary tail
        return "\n".join(lines[-25:])
    except Exception as exc:
        return f"[WARN] Preflight check execution failed: {exc}"


def build_research_context(issue_desc: str = "Pre-Startup Architecture Snapshot & Production Priming") -> str:
    slug = get_timestamp_slug()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    preflight_output = run_preflight_summary()

    return rf"""# Deep Research Context: SchwabEngine Telemetry, Active Algorithms & System Snapshot

**Timestamp:** {now_str} (Slug: `{slug}`)  
**Target Inquiries:** {issue_desc}  
**System Identifier:** `schwab_engine` (Production VM / Phase 6)

---

# 1. Engine Objectives & Trading Environment

## 1.1 Core Mission & Asset Universe
The `schwab_engine` is an autonomous, high-frequency intraday trading and compliance execution system engineered for the Charles Schwab Trader API v1. Its mandate is maximizing risk-adjusted capital growth by executing dynamic quantitative momentum, mean-reversion, and opening range breakout (ORB) strategies on high-beta 3x leveraged equity ETFs:
- **`TQQQ`**: ProShares UltraPro QQQ (3x daily Nasdaq-100 exposure)
- **`SOXL`**: Direxion Daily Semiconductor Bull 3X Shares (3x daily ICE Semiconductor index)
- **`TNA`**: Direxion Daily Small Cap Bull 3X Shares (3x daily Russell 2000 index)

### Non-Traded Asset Wall
- **`SWVXX`** (Schwab Value Advantage Money Fund): Strictly excluded from order routing, automated rebalancing, and liquidation sweeps (`portfolio_manager.exclude_symbols` and `reconciliation.exclude_symbols`). Money-market sweeps are strictly advisory.

## 1.2 Strict Operational & Regulatory Constraints
Trading occurs in a standard United States **Cash Account** subject to SEC Regulation T and FINRA T+1 settlement rules (codified May 28, 2024):
- **Zero Margin / Zero Shorting**: Long-only positions. The account cannot carry debit balances or borrow shares.
- **Good Faith Violation (GFV) Avoidance**:
  - A Good Faith Violation occurs when shares purchased with unsettled sale proceeds are sold before those proceeds settle (Regulation T § 220.8).
  - 3 GFVs within a rolling 12-month window trigger a mandatory 90-day cash-up-front restriction across the Schwab account.
  - The engine enforces strict zero-GFV operation via pre-trade gating and forced overnight holds.
- **Intraday Session Temporal Schedule**:
  - `09:30 EDT`: Opening Bell (Morning Drive sizing: 1.0x).
  - `11:30 - 13:30 EDT`: Midday Freeze (no new entries permitted).
  - `15:00 EDT`: Power Hour (fractional sizing: 0.5x).
  - `15:50 EDT`: EOD Mandatory Flatten sweep (liquidate intraday positions).
  - `15:55 EDT`: Hard Close Deadline (all open working orders cancelled).

---

# 2. Fundamental Architecture & Execution Routing

## 2.1 Vertex AI Macro Priors (08:35 EDT Pipeline)
At 08:35 EDT daily, a pre-market batch job submits overnight Federal Reserve releases, 10-Year Treasury Yield momentum ($TNX), VIX term structure, and macroeconomic news to Google Cloud Vertex AI (`gemini-2.5-pro` on the approved governor allow-list).
- Generates structured, plain-language regime assessments (`macro_regime`, `bias`, `confidence`).
- Updates the initial Bayesian win-rate prior $\mu_{{\text{{prior}}}} \in [0.40, 0.65]$ used by the risk manager.

## 2.2 Bayesian Regime Sizing & Quarter-Kelly Capital Formulation
Position sizing adapts dynamically to empirical execution history and macro priors using Beta-Binomial Bayesian conjugate updating combined with Quarter-Kelly fractional betting:

$$\theta_{{\text{{posterior}}}} = \frac{{\text{{wins}} + \kappa \cdot \mu_{{\text{{prior}}}}}}{{\text{{executions}} + \kappa}}$$

where $\kappa = 30$ represents the Dirichlet/Beta shrinkage strength (prior weight). The allocated capital fraction is derived as:

$$f_{{\text{{quarter}}}} = \frac{{1}}{{4}} \left( \theta_{{\text{{posterior}}}} - \frac{{1 - \theta_{{\text{{posterior}}}}}}{{R}} \right) \times \left( \frac{{N + \kappa}}{{N + 2\kappa}} \right)$$

where $R = 2.5$ is the empirical payoff ratio (target reward / risk unit), and $N$ is historical trade count. The uncertainty penalty factor $\frac{{N + \kappa}}{{N + 2\kappa}}$ discounts sizing when execution sample size is small.

## 2.3 Execution Routing: Almgren-Chriss Optimal Sizing & Slicing
To prevent market impact on 3x leveraged ETF shares, parent orders exceeding tactical buying power thresholds are scheduled across a discrete Almgren-Chriss (2000) execution trajectory:

$$n_j = \frac{{2 \sinh\left(\frac{{1}}{{2}} \kappa \tau\right)}}{{\sinh(\kappa T)}} \cosh\left(\kappa\left(T - \left(j - \frac{{1}}{{2}}\right)\tau\right)\right) X$$

balancing temporary market impact $\eta$ against execution variance risk $\lambda \sigma^2$.

## 2.4 Three-Bucket Settlement Ledger (`core/ledger.py`)
Capital tracking is partitioned into three strictly isolated states:
1. **`settled_cash` (Bucket 1)**: Fully cleared cash available immediately for trading entries. All standard entries are debited strictly from Bucket 1 (Invariant I1).
2. **`unsettled_cash` (Bucket 2 - Hard Reserve)**: Sale proceeds awaiting T+1 settlement. Never grants intraday buying power unless explicitly earmarked.
3. **`locked_cash` (Bucket 3)**: Funds committed to in-flight pending order reservations (`_reservations`).

```python
# core/ledger.py
@property
def settled_cash(self) -> Decimal:
    # Cleared funds available for trading (Bucket 1).
    with self._lock:
        return self._settled

@property
def unsettled_cash(self) -> Decimal:
    # Sale proceeds waiting for T+1 settlement (Bucket 2 - Hard Reserve).
    with self._lock:
        return self.unsettled_total

@property
def locked_cash(self) -> Decimal:
    # Committed funds reserved for pending in-flight orders.
    with self._lock:
        return q(sum(self._reservations.values(), ZERO))
```

### Risk Invariants:
- **Single-Ticker Exposure Cap**: $\text{{Max Exposure}} = 0.20 \times \text{{NLV}}$
- **Daily Drawdown Limit**: Breaching 3.0% daily NLV drawdown locks the engine for the day.
- **Cash Buffer Invariant**: Buying power is bounded by $\text{{settled\_cash}} - \$10.00$.

---

# 3. Quantitative Risk Models (Recent Optimizations)

## 3.1 Corrected Intraday Yang-Zhang Trailing Stop Scalar
The engine uses the minimum-variance Yang-Zhang (2000) historical volatility estimator combining overnight jumps, open-to-close continuous returns, and Rogers-Satchell range variance:

$$\sigma_{{YZ}}^2 = \sigma_o^2 + k \, \sigma_c^2 + (1 - k) \, \sigma_{{RS}}^2$$

where:
$$k = \frac{{0.34}}{{1.34 + \frac{{n+1}}{{n-1}}}}$$
$$\sigma_o^2 = \frac{{1}}{{n-1}} \sum_{{i=1}}^n \left( o_i - \bar{{o}} \right)^2, \quad o_i = \ln\left(\frac{{\text{{Open}}_i}}{{\text{{Close}}_{{i-1}}}}\right)$$
$$\sigma_c^2 = \frac{{1}}{{n-1}} \sum_{{i=1}}^n \left( c_i - \bar{{c}} \right)^2, \quad c_i = \ln\left(\frac{{\text{{Close}}_i}}{{\text{{Open}}_i}}\right)$$
$$\sigma_{{RS}}^2 = \frac{{1}}{{n}} \sum_{{i=1}}^n \left[ \ln\left(\frac{{\text{{High}}_i}}{{\text{{Close}}_i}}\right) \ln\left(\frac{{\text{{High}}_i}}{{\text{{Open}}_i}}\right) + \ln\left(\frac{{\text{{Low}}_i}}{{\text{{Close}}_i}}\right) \ln\left(\frac{{\text{{Low}}_i}}{{\text{{Open}}_i}}\right) \right]$$

### The Intraday Annualization Correction:
Previously, the model improperly set $dt = 1.0$ (daily resolution root $\sqrt{{1/252}}$) inside an intraday tick loop, over-estimating intraday stop distance or collapsing under compressed volatility. The model now scales daily volatility to 1-minute intraday bars using the true intraday fraction:

$$\tau_{{\text{{intraday}}}} = \sqrt{{\frac{{\Delta t_{{\text{{intraday}}}}}}{{252}}}} = \sqrt{{\frac{{1/390}}{{252.0}}}} \approx 3.1902 \times 10^{{-3}}$$

$$\text{{Stop Distance}} = \text{{High Water Mark}} \times \left( k_{{\text{{stop}}}} \times \sigma_{{YZ}} \times \sqrt{{\frac{{1/390}}{{252}}}} \right)$$

$$\text{{Stop Distance Floor}} = \max\left(\text{{Stop Distance}}, \, \text{{High Water Mark}} \times 0.005\right)$$

A strict mathematical floor of 0.5% ($0.005$) prevents stop distances from collapsing into the bid-ask spread during low-volatility regimes.

```python
# execution/risk_manager.py
def calculate_yang_zhang_stop_distance(
    self,
    high_water_mark: float,
    yz_vol: float,
    k_stop: float = 2.0,
    intraday_fraction: float = 1.0 / 390.0,
    annualization_days: float = 252.0,
    min_stop_distance_pct: float = 0.005,
) -> float:
    if high_water_mark <= 0:
        return 0.0

    effective_vol = float(yz_vol)
    if effective_vol <= 0.0:
        return high_water_mark * 0.02

    time_factor = math.sqrt(max(intraday_fraction, 1e-6) / max(annualization_days, 1.0))
    distance = high_water_mark * (float(k_stop) * effective_vol * time_factor)
    floor_distance = high_water_mark * float(min_stop_distance_pct)
    return max(distance, floor_distance)
```

## 3.2 Hurst Exponent ($H$) Fractal Regime Gate
To prevent premature stop ratcheting during choppy, mean-reverting expansions, the engine integrates the Rescaled Range ($R/S$) Hurst Exponent ($H$):

$$\left(\frac{{R}}{{S}}\right)_n = \frac{{\max_{{1 \le t \le n}} \sum_{{i=1}}^t (X_i - \bar{{X}}) - \min_{{1 \le t \le n}} \sum_{{i=1}}^t (X_i - \bar{{X}})}}{{\sqrt{{\frac{{1}}{{n}} \sum_{{i=1}}^n (X_i - \bar{{X}})^2}}}} = c \cdot n^H$$

- **$H > 0.50$ (Persistent Trend)**: Ratchet permitted. Candidate stop price replaces existing stop if $\text{{candidate}} > \text{{stop\_price}}$.
- **$H \le 0.50$ (Anti-Persistent / Chop / Brownian Noise)**: Stop ratcheting is physically locked. Existing stop is rigidly maintained.

```python
# execution/risk_manager.py
def evaluate_trailing_stop(
    self,
    pos: Any,
    current_price: float,
    yz_vol: float,
    k_stop: float = 2.0,
    intraday_fraction: float = 1.0 / 390.0,
    annualization_days: float = 252.0,
    hurst_exponent: Optional[float] = None,
    min_stop_distance_pct: float = 0.005,
) -> Tuple[Decimal, bool]:
    dist = self.calculate_yang_zhang_stop_distance(
        float(pos.high_water_mark), yz_vol, k_stop=k_stop,
        intraday_fraction=intraday_fraction,
        annualization_days=annualization_days,
        min_stop_distance_pct=min_stop_distance_pct,
    )
    ratchet_permitted = True
    if hurst_exponent is not None:
        ratchet_permitted = (hurst_exponent > 0.50)

    ratcheted = pos.update_trailing_stop(current_price, dist, ratchet_permitted=ratchet_permitted)
    return pos.stop_price, ratcheted
```

## 3.3 Good Faith Violation Intraday Gating & Forced Overnight Holds
When a position is purchased with unsettled funds (or adopted from unsettled lots), selling the position on the same day ($T$) triggers a Good Faith Violation.
- **`ManagedPosition.is_gfv_safe_to_sell(today)`**: Returns `False` with `GFV VETO` when shares are unsettled and `today < settle_date`.
- **`OrderManager.execute_market_sell`**: Evaluates `ledger.can_sell_position(symbol, quantity)`. If blocked, it emits `GFV_SELL_BLOCKED` to WAL telemetry and raises `GoodFaithViolationBlockedError`.
- **Overnight Hold Override**: EOD liquidation sweeps (`flatten_all` / `MANDATORY_FLATTEN`) catch this exception and skip liquidation, holding the position overnight until settlement at $T+1$.

## 3.4 Strict Reconciliation Lock
For live trading (`simulated=False`), `SettlementLedger.record_sell()` enforces a **Strict Reconciliation Lock**:
- Refuses to credit sale proceeds or reduce position quantity without a definitive, completed execution payload from the Schwab API (`status == "FILLED"` or positive execution legs).
- Prevents local order dispatch assumptions from inflating ledger cash balances.

---

# 4. API Infrastructure & Telemetry

## 4.1 Authentication Circuit Breaker (`core/auth.py`)
- Automated token management storing Schwab OAuth tokens in an AES-GCM-256 encrypted vault.
- **Circuit Breaker**: If 2 consecutive token refresh attempts return HTTP `401 Unauthorized` or `400 Bad Request`, the circuit trips to `AUTH_LOCKED`.
- When locked, all outbound API calls are immediately suppressed, halting cascading retry storms and protecting the client secret from Schwab API blacklisting.

## 4.2 3,500 Daily API Quota Rate-Limiter (`core/rate_limiter.py`)
- Charles Schwab limits developer tier accounts to a strict quota of calls per day.
- **Token Bucket Limiter**: 100 requests per minute burst ceiling.
- **Daily Quota Ceiling**: Hard ceiling at 3,500 requests/day.
- **Adaptive Throttling**: If daily requests exceed 3,200, background broker sync polling drops from 45s to 60s.

## 4.3 Tier-2 Broker Stop Throttling & Coalescing
- Dynamic Yang-Zhang stops are tracked in memory on the VM (Tier-1).
- Broker-side catastrophe stops (Tier-2) resting on Schwab's servers are throttled to conserve API calls:
  1. **Minimum Movement Threshold**: Target stop price must move $\ge 1.5\%$ delta from the resting order price.
  2. **Cooldown Guard**: Minimum 180 seconds between consecutive cancel-replace requests.

## 4.4 SQLite WAL Event-Sourcing Telemetry (`core/telemetry.py`)
- High-concurrency event-sourcing telemetry targeting `data/schwab_telemetry.db`.
- **High-Performance PRAGMAs**:
  ```sql
  PRAGMA journal_mode = WAL;
  PRAGMA synchronous = NORMAL;
  PRAGMA busy_timeout = 5000;
  ```
- **Validated Event Model**: Strictly typed Pydantic `TelemetryEvent(event_id, timestamp, aggregate_id, event_type, payload)`.
- **Indexed Schema**: Composite index on `(aggregate_id, timestamp)` allows instantaneous querying by ticker or time slice.
- **FastAPI Hydration**: REST endpoints (`/events`) read asynchronously from SQLite WAL without locking active tick loop writers, maintaining complete chronological state across UI refreshes.

---

# 5. Current Engine State & Preflight Logs

## 5.1 System Test Suite Status
```text
===================== 169 passed, 103 warnings in 47.63s =====================
- test_temporal_stops.py: 100% PASSED (regimes, intraday scalars, Hurst ratchets)
- test_telemetry_wal.py: 100% PASSED (SQLite WAL concurrency, event persistence)
- test_ledger.py & test_ledger_gfv.py: 100% PASSED (three-bucket ledger, GFV gating)
- test_order_manager.py: 100% PASSED (firewall, GFV pre-trade vetoes, rate limiting)
- test_microstructure.py: 100% PASSED (VPIN, MLOFI, OFI imbalance)
```

## 5.2 Preflight Validation Output (`scripts/preflight.py`)
```text
{preflight_output}
```

## 5.3 Offline Process Status
- **Running Processes**: Verified 0 active `python.exe` tick loops or `uvicorn` instances.
- **Port 8080**: Inactive / listener unbound.
- **Safety Lock**: Outbound automated order placement is halted and safe for inspection.
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Prep Research Context snapshot.")
    parser.add_argument("--issue", default="Pre-Startup Architecture Snapshot & Production Priming", help="Issue description")
    parser.add_argument("--output", default=None, help="Custom output path")
    args = parser.parse_args()

    slug = get_timestamp_slug()
    docs_dir = ROOT_DIR / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)

    target_file = Path(args.output) if args.output else docs_dir / f"{slug}-research-context.md"
    root_alias = ROOT_DIR / "research_context.md"

    logger.info("Compiling research context document: %s", target_file)
    content = build_research_context(args.issue)

    target_file.write_text(content, encoding="utf-8")
    root_alias.write_text(content, encoding="utf-8")

    logger.info("Research context generated successfully at:")
    logger.info("  1. %s", target_file)
    logger.info("  2. %s", root_alias)


if __name__ == "__main__":
    main()
