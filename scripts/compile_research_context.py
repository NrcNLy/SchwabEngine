#!/usr/bin/env python3
"""
scripts/compile_research_context.py
===================================
Authoritative research context and architecture review compilation utility for SchwabEngine.
Programmatically compiles codebase architecture logic, runtime configuration state,
mathematical models (with strict raw LaTeX formatting), systemd journalctl telemetry,
and SQLite state databases into a unified Markdown reference document (`research_context.md`).

Upon completion, automatically synchronizes the output document to Google Drive via
`scripts/sync_engine_drive.py --once`.

Usage:
    python scripts/compile_research_context.py [--scope general|comprehensive|execution|risk|tools|all]
                                               [--since "today"]
                                               [--output research_context.md]
                                               [--no-sync]
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Add project root to sys.path so core.paths is authoritative
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.paths import (
    BASELINES_DB_PATH,
    CONFIG_DIR,
    CONTROL_SIGNAL_FILE,
    DAILY_QUOTA_FILE,
    DATA_DIR,
    DOCS_DIR,
    DYNAMIC_POLICY_FILE,
    FIXTURES_DIR,
    PROJECT_ROOT,
    RECOVERY_PLAN_FILE,
    RECOVERY_STATE_FILE,
    STATE_DIR,
    STRATEGY_CONFIG_FILE,
    TELEMETRY_DB_PATH,
    TELEMETRY_FIXTURE_FILE,
    TRADES_DB_PATH,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("compile_research_context")

# Subsystem file scopes reflecting consolidated layout: docs/, state/, data/fixtures/, scripts/tools/
COMPREHENSIVE_CATEGORIES: Dict[str, List[Tuple[str, str]]] = {
    "Execution Subsystem": [
        ("execution/order_manager.py", "Routing firewall, limit order placement & order state transitions"),
        ("execution/risk_manager.py", "Two-stage EOD deleveraging, HMM meta-governor & Leland NTZ router"),
        ("execution/sizing_governor.py", "Bayesian Quarter-Kelly sizing & cash settlement clamp"),
        ("execution/almgren_chriss.py", "Optimal execution slicing & NBBO limit pegging"),
        ("execution/bracket_manager.py", "Multi-tranche profit ladder & breakeven ratchet"),
        ("execution/adaptive_envelope.py", "Yang-Zhang volatility & asymptotic time-decay squeeze"),
        ("execution/microstructure_sentinel.py", "Opening cross VWAP buffer & soft/hard triggers"),
    ],
    "Core & Data Subsystem": [
        ("core/paths.py", "Consolidated filesystem layout single source of truth"),
        ("core/ledger.py", "Three-bucket cash accounting & T+1 GFV state machine"),
        ("core/auth.py", "OAuth token lifecycle & circuit breaker management"),
        ("core/rate_limiter.py", "Schwab REST token bucket, burst controller & daily quota"),
        ("core/event_ledger.py", "Zero-allocation SQLite WAL event ledger"),
        ("core/event_models.py", "Zero-allocation SQLite WAL event models & schemas"),
        ("core/synthetic_divergence.py", "Multi-factor SES regime governor & relative strength"),
        ("data/multi_rate_sync.py", "Multi-rate state synchronizer, microprice, OBI & credit spread proxy"),
        ("data/stream_resampler.py", "WebSocket Level 1 streaming tick resampler"),
    ],
    "Operational Scripts & Consolidated Tools": [
        ("scripts/tools/prep_argon_payload.py", "Argon release packaging & payload builder"),
        ("scripts/sync_engine_drive.py", "Authoritative Google Drive mirror & WAL snapshot daemon"),
        ("scripts/preflight.py", "12 pre-market invariant latch gates"),
        ("scripts/verify_friday_settlement.py", "Automated T+1 settlement auditor"),
        ("scripts/export_research_datasets.py", "Market data packaging pipeline"),
        ("web/websocket_manager.py", "FastAPI 1Hz real-time telemetry gateway"),
    ],
    "Configuration, Fixtures & State": [
        ("config/strategy_config.json", "Production strategy and regime configuration"),
        ("data/fixtures/schwab_telemetry_micro_sequence.json", "Microstructure Level 1 quote replay sequence fixture"),
        ("metadata.json", "Engine deployment metadata & release state"),
    ],
}

SCOPE_DEFINITIONS: Dict[str, List[str]] = {
    "comprehensive": [item[0] for sublist in COMPREHENSIVE_CATEGORIES.values() for item in sublist],
    "general": [
        "core/paths.py",
        "core/ledger.py",
        "core/auth.py",
        "execution/order_manager.py",
        "execution/risk_manager.py",
        "data/multi_rate_sync.py",
        "config/strategy_config.json",
        "metadata.json",
    ],
    "execution": [
        "execution/order_manager.py",
        "execution/risk_manager.py",
        "execution/sizing_governor.py",
        "execution/almgren_chriss.py",
        "core/ledger.py",
        "core/rate_limiter.py",
        "config/strategy_config.json",
    ],
    "risk": [
        "execution/risk_manager.py",
        "execution/sizing_governor.py",
        "core/synthetic_divergence.py",
        "data/multi_rate_sync.py",
        "config/strategy_config.json",
    ],
    "tools": [
        "scripts/tools/prep_argon_payload.py",
        "scripts/sync_engine_drive.py",
        "scripts/preflight.py",
        "scripts/verify_friday_settlement.py",
    ],
    "fixtures_and_state": [
        "data/fixtures/schwab_telemetry_micro_sequence.json",
        "config/strategy_config.json",
        "metadata.json",
    ],
    "telemetry": [
        "data/multi_rate_sync.py",
        "data/stream_resampler.py",
        "web/websocket_manager.py",
    ],
    "all": [item[0] for sublist in COMPREHENSIVE_CATEGORIES.values() for item in sublist],
}


def sanitize_markdown_output(text: str) -> str:
    """
    Sanitizes markdown text to remove rendering artifacts that could resolve
    as broken image markdown, image placeholders, or proprietary tags in Google Docs
    or external Markdown viewers.
    """
    if not text:
        return ""

    # Replace markdown image syntax ![alt](url) with safe text reference
    sanitized = re.sub(r"!\[(.*?)\]\((.*?)\)", r"[Image Reference: \1](\2)", text)

    # Strip HTML img tags
    sanitized = re.sub(r"<img\b[^>]*>", "[Image Element Removed]", sanitized, flags=re.IGNORECASE)

    # Strip literal [image] or [image: ...] placeholders
    sanitized = re.sub(r"\[image(?::\s*[^\]]+)?\]", "[Image Reference]", sanitized, flags=re.IGNORECASE)

    return sanitized


def run_command(cmd: List[str], timeout: int = 30) -> Tuple[int, str]:
    """Executes a subprocess command safely returning exit code and stdout/stderr."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = proc.stdout.strip()
        if proc.returncode != 0 and proc.stderr.strip():
            if output:
                output += "\n" + proc.stderr.strip()
            else:
                output = proc.stderr.strip()
        return proc.returncode, output
    except Exception as exc:
        return 1, f"[ERROR executing {' '.join(cmd)}: {exc}]"


def get_git_metadata() -> Dict[str, str]:
    """Extracts git branch, commit hash, and working tree cleanliness."""
    _, branch = run_command(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    _, commit = run_command(["git", "rev-parse", "HEAD"])
    _, status = run_command(["git", "status", "--porcelain"])
    return {
        "branch": branch or "feat/dynamic-screener-universe",
        "commit": commit[:7] if commit else "unknown",
        "dirty": "dirty" if status else "clean",
    }


def query_journalctl(unit: str, since: str, lines: int) -> str:
    """Queries systemd journalctl for the specified service unit."""
    if not shutil.which("journalctl"):
        return f"[WARN: journalctl binary not found on {platform.system()}]"

    cmd = [
        "journalctl",
        "-u",
        unit,
        "--since",
        since,
        "-n",
        str(lines),
        "--no-pager",
    ]
    code, out = run_command(cmd, timeout=30)
    if code != 0 or not out:
        return f"[No journalctl logs recorded for {unit} since '{since}']"
    return out


def query_systemctl_status(unit: str) -> str:
    """Queries systemctl status for a service unit."""
    if not shutil.which("systemctl"):
        return "systemctl unavailable"
    code, out = run_command(["systemctl", "is-active", unit])
    return out if code == 0 else f"inactive ({out})"


def inspect_sqlite_db(db_path: Path) -> Dict[str, Any]:
    """Inspects SQLite database for WAL pragma, size, tables and row counts."""
    is_rel = False
    try:
        is_rel = db_path.is_relative_to(PROJECT_ROOT)
    except Exception:
        pass
    info: Dict[str, Any] = {
        "path": str(db_path.relative_to(PROJECT_ROOT)) if is_rel else str(db_path),
        "exists": db_path.exists(),
        "size_kb": round(db_path.stat().st_size / 1024, 2) if db_path.exists() else 0,
        "journal_mode": "unknown",
        "tables": {},
    }
    if not db_path.exists():
        return info

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode;")
        row = cursor.fetchone()
        if row:
            info["journal_mode"] = str(row[0]).lower()

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
        tables = [t[0] for t in cursor.fetchall()]
        for table in tables:
            try:
                cursor.execute(f"SELECT COUNT(*) FROM \"{table}\";")
                cnt_row = cursor.fetchone()
                info["tables"][table] = cnt_row[0] if cnt_row else 0
            except Exception:
                info["tables"][table] = "err"
        conn.close()
    except Exception as exc:
        info["error"] = str(exc)

    return info


def sanitize_env_status() -> Dict[str, str]:
    """Inspects .env variables presence without revealing sensitive secrets."""
    env_file = PROJECT_ROOT / ".env"
    status: Dict[str, str] = {}
    critical_keys = [
        "SCHWAB_API_KEY",
        "SCHWAB_APP_SECRET",
        "SCHWAB_REDIRECT_URI",
        "SCHWAB_ACCOUNT_ID",
        "GEMINI_API_KEY",
        "STATE_DIR",
        "ENGINE_STATE_DIR",
    ]
    if not env_file.exists():
        return {"status": "NOT FOUND (.env missing)"}

    present_keys = set()
    try:
        for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k = line.split("=", 1)[0].strip()
                present_keys.add(k)
    except Exception as exc:
        return {"status": f"READ_ERROR ({exc})"}

    for key in critical_keys:
        status[key] = "CONFIGURED (SET)" if key in present_keys else "MISSING / UNSET"
    return status


def extract_code_file(rel_path: str, subtitle: Optional[str] = None) -> str:
    """Reads and formats a source code or config file into a fenced Markdown block."""
    full_path = PROJECT_ROOT / rel_path
    if not full_path.exists():
        alt_path = STATE_DIR / Path(rel_path).name
        if alt_path.exists():
            full_path = alt_path

    if not full_path.exists():
        header = f"#### `{rel_path}`\n" if not subtitle else f"#### `{rel_path}` - {subtitle}\n"
        return f"{header}\n> [!WARNING] File not found at `{full_path}`\n\n"

    try:
        content = full_path.read_text(encoding="utf-8", errors="replace")
        content = sanitize_markdown_output(content)
        lines = content.splitlines()
        line_count = len(lines)
        size_bytes = full_path.stat().st_size

        ext = full_path.suffix.lstrip(".").lower()
        fence = "json" if ext == "json" else "python" if ext in ("py", "pyw") else "text"

        header = f"#### `{rel_path}` ({line_count} lines, {size_bytes} bytes)\n"
        if subtitle:
            header = f"#### `{rel_path}` - {subtitle} ({line_count} lines, {size_bytes} bytes)\n"
        return f"{header}\n```{fence}\n{content}\n```\n\n"
    except Exception as exc:
        header = f"#### `{rel_path}`\n" if not subtitle else f"#### `{rel_path}` - {subtitle}\n"
        return f"{header}\n> [!ERROR] Could not read file: {exc}\n\n"


def sync_to_google_drive() -> bool:
    """Executes scripts/sync_engine_drive.py --once to mirror context files to Google Drive."""
    sync_script = PROJECT_ROOT / "scripts" / "sync_engine_drive.py"
    if not sync_script.exists():
        logger.warning("Drive sync script not found at %s", sync_script)
        return False

    cmd = [sys.executable, str(sync_script), "--once"]
    logger.info("Executing automated Google Drive sync: %s", " ".join(cmd))
    code, out = run_command(cmd, timeout=60)
    if code == 0:
        logger.info("Google Drive synchronization completed successfully.")
        return True
    else:
        logger.error("Google Drive sync exited with code %d:\n%s", code, out)
        return False


def build_section_1_markdown() -> str:
    """Builds Section 1: System Invariants & Account Profile."""
    return r"""## 1. System Invariants & Account Profile

### 1.1 Account Profile & Dual-Sleeve Capital Model
SchwabEngine operates on a single Charles Schwab brokerage account (`***4015`) with a calibrated baseline Net Liquidation Value (NLV) of **$4,400.00 USD** (actual session initialization baseline: **$4,400.90 USD**). The engine governs capital using an unchained **Dual-Sleeve Allocation Model** designed to maximize risk-adjusted capital efficiency while mitigating idle cash drag:

1. **Passive 1x Robo-Core Sleeve ($C(t)$)**:
   - Target Portfolio Weight: $W_{core}^* = 0.85$ (85% of NLV under neutral conditions).
   - Multi-asset diversified ETF basket serving as the primary capital preservation and equity-beta engine:
     - `SPY`: 0.35 (Large-Cap Broad US Equity Beta)
     - `QQQ`: 0.25 (Large-Cap Technology Beta)
     - `HYG`: 0.15 (High-Yield Corporate Credit Beta)
     - `IEF`: 0.15 (Intermediate 7-10Y US Treasury Duration Hedge)
     - `IWM`: 0.10 (Russell 2000 Small-Cap Beta)
   - Zero Cash Drag Policy: Target cash allocation within the core routing vector is 0.00.

2. **Tactical 3x Leveraged Satellite Sleeve ($S(t)$)**:
   - Target Portfolio Weight: $W_{sat}^*(t) \in [0.00, 0.35]$ dynamically determined by the Online Hidden Markov Model (HMM) Meta-Governor.
   - High-beta leveraged momentum instruments:
     - `SOXL`: 0.40 (3x Daily Bull Semiconductor ETF)
     - `TQQQ`: 0.35 (3x Daily Bull NASDAQ-100 ETF)
     - `TNA`:  0.25 (3x Daily Bull Russell 2000 Small-Cap ETF)

3. **Cash Sweep & Margin Buffer ($M_{buf}(t)$)**:
   - Residual fractional and clearance buffer vehicle: `SWVXX` (Schwab Value Advantage Money Fund).
   - Invariant: $W_{core}(t) + W_{sat}(t) + W_{buf}(t) \equiv 1.00$.

```yaml
system_profile:
  account_number_suffix: "015"
  account_type: "CASH_ACCOUNT" # Subject to SEC Rule 15c6-1 T+1 Cash Settlement
  baseline_nlv_usd: 4400.00
  hot_boot_starting_nlv: 4400.90
  max_single_ticker_pct: 0.20 # 20% hard single-name exposure limit
  daily_api_quota: 3500       # Charles Schwab REST API call ceiling
  api_burst_limit_rpm: 120    # Token bucket burst capacity (2.0 req/sec)
  cash_reserve_floor_pct: 0.05
```

### 1.2 End-to-End Runtime Architecture
```mermaid
flowchart TD
    subgraph Ingestion ["1. Multi-Rate Ingestion & Microstructure Hot Plane"]
        WS["Schwab WebSocket Tick Stream\n(1s Level 1 BBO Quotes)"] --> Sync["MultiRateStateSynchronizer\n(Zero-Order Hold & Resampling)"]
        REST["Schwab REST API Hydration\n(Balances, Positions, Working Orders)"] --> Sync
        Macro["FRED OAS & Treasury Yields\n(10Y $TNX, VIX, OAS Proxy)"] --> Sync
    end

    subgraph Governance ["2. Regime Classification & Dynamic Sizing"]
        Sync --> StateVec["Macro Feature Vector M(t)\n[Z_surprise, S_sentiment, CS_hybrid]"]
        StateVec --> HMM["3-State Online HMM Meta-Governor\npi(t) = [Goldilocks, Uncertainty, Shock]"]
        HMM --> SizingGov["Dynamic Satellite Sizing\nW_sat*(t) = 0.25*pi_G + 0.10*pi_U + 0.00*pi_S"]
    end

    subgraph NTZ ["3. Leland No-Trade Zone (NTZ) Evaluation"]
        SizingGov --> NTZCheck{"Active w_sat in NTZ?\n[W_sat* - tau <= w <= W_sat* + tau]"}
        NTZCheck -- "Inside NTZ Band" --> NTZHold["Invariant Hold (dQ = 0)\nZero Order Generation & Churn Suppression"]
        NTZCheck -- "Upper Harvest Breach\n(w_sat > W_upper)" --> TrimSat["Trim Excess Satellite\nSweep Freed USD -> Robo-Core Vector"]
        NTZCheck -- "Lower Accumulate Breach\n(w_sat < W_lower)" --> FundSat["Trim Robo-Core Basket\nRebalance -> 3x LETFs"]
    end

    subgraph Firewall ["4. Pre-Trade Risk Firewall & Execution Gating"]
        TrimSat --> RiskGates{"Pre-Trade Risk Gates\n- Account Firewall (015 Whitelist)\n- Reg T / Cash Ledger (GFV Check)\n- 20% Single-Ticker Cap\n- 3500 Daily API Limit"}
        FundSat --> RiskGates
        RiskGates -- "Pass" --> Router["Almgren-Chriss Slicer &\nAdaptive Execution Router"]
        RiskGates -- "Veto" --> VetoReject["Order Rejected / Safety Latch"]
        Router --> SchwabAPI["Schwab Trader API v1"]
    end

    subgraph TemporalGate ["5. Temporal Lifecycle & EOD Flatten"]
        SchwabAPI --> ClockCheck{"Intraday Time Gate (ET)"}
        ClockCheck -- "09:30 - 11:30 ET" --> ActiveOpen["Active Morning Drive (1.0x Kelly)"]
        ClockCheck -- "11:30 - 13:30 ET" --> MiddayFreeze["Midday Volatility Freeze (Hold Only)"]
        ClockCheck -- "13:30 - 15:50 ET" --> AfternoonDrive["Afternoon Rebalance Window"]
        ClockCheck -- "15:50 ET" --> Stage1Flatten["Stage 1 EOD Auction Trim (Regular Close)"]
        ClockCheck -- "15:55 ET" --> HardCancel["Hard Close: Cancel Working Orders"]
        ClockCheck -- "19:45 ET" --> Stage2Flatten["Stage 2 Extended-Hours Hard Backstop"]
    end
```
"""


def build_section_2_markdown() -> str:
    """Builds Section 2: Active Codebase Logic & Mathematical Formulations."""
    return r'''## 2. Active Codebase Logic & Mathematical Formulations

### 2.1 Order Submission & Rate-Limited Dispatch (`execution/order_manager.py`)
All order placement is strictly firewalled to verify account hash matches the whitelisted `***4015` account and does not contain prohibited keywords (`ROBO`, `INTELLIGENT`, `IRA`, `PORTFOLIO_MANAGED`). Prices are formatted to exactly 2 decimal places to prevent Schwab API HTTP 400 Bad Request rejections.

```python
# execution/order_manager.py: Exact placement & dispatch logic
def place_limit_buy(
    self,
    symbol: str,
    limit_price: float,
    quantity: Optional[int] = None,
    atr: Optional[float] = None,
    extended_hours: bool = False,
    is_essential: bool = False,
) -> str:
    sym = symbol.upper()
    px_float = float(limit_price)
    eff_atr = float(atr) if (atr is not None and atr > 0.0) else (px_float * 0.015)

    if quantity is None or quantity <= 0:
        verdict = self.calculate_position_size(symbol, px_float, eff_atr)
        if not verdict.allowed or verdict.shares <= 0:
            logger.warning("OrderManager: SizingGovernor refused BUY for %s: %s", sym, verdict.reason)
            return ""
        qty = verdict.shares
    else:
        qty = int(quantity)

    self._require_firewall()
    px_str = f"{px_float:.2f}"
    session = _SESSION_NORMAL if not extended_hours else "SEAMLESS"
    body = self._build_order(
        instruction=_INST_BUY,
        order_type=_OT_LIMIT,
        symbol=sym,
        quantity=qty,
        price=px_str,
        session=session,
    )
    resp = self._rest.place_order(self._account_hash, body, is_essential=is_essential, action="LIMIT_BUY")
    return self._extract_order_id(resp.headers.get("Location", ""))
```

Rate-limited asynchronous execution uses an exponential backoff retry loop with quota tracking:
```python
# execution/order_manager.py:submit_order_with_rate_limit_backoff
def submit_order_with_rate_limit_backoff(
    self,
    order: DualSleeveOrder,
    attempt_index: int = 0,
) -> str:
    delay = self._compute_backoff_delay(attempt_index)
    if delay > 0.0:
        self._sleep_fn(delay) # WARNING: synchronous time.sleep in runtime path

    rest, account_hash = self._resolve_rest_and_hash()
    px_str = f"{float(order.price):.2f}"
    instruction = _INST_BUY if order.side.upper() == "BUY" else _INST_SELL
    body = self._build_order(
        instruction=instruction,
        order_type=_OT_LIMIT,
        symbol=order.symbol.upper(),
        quantity=int(order.shares),
        price=px_str,
    )
    resp = rest.place_order(account_hash, body, is_essential=True, action=f"SWAP_{order.side.upper()}")
    return self._extract_order_id(resp.headers.get("Location", ""))
```

### 2.2 Volume-Weighted Microprice & Order Book Imbalance (`data/multi_rate_sync.py`)
Microstructure quote updates derive mid-price, order book imbalance (OBI), and volume-weighted microprice from high-frequency Level 1 BBO feeds:

$$\text{Mid Price} = \frac{P_{bid} + P_{ask}}{2}$$

$$\text{Volume-Weighted Microprice} = \frac{P_{bid} \cdot Q_{ask} + P_{ask} \cdot Q_{bid}}{Q_{bid} + Q_{ask}}$$

$$\text{Order Book Imbalance (OBI)} = \frac{Q_{bid} - Q_{ask}}{Q_{bid} + Q_{ask}} \in [-1.0, 1.0]$$

```python
# data/multi_rate_sync.py:186-215
# Mid-price
if bid > 0.0 and ask > 0.0:
    mid_price = (bid + ask) / 2.0
elif last > 0.0:
    mid_price = last
else:
    mid_price = max(bid, ask, 0.0)

# Volume-Weighted Microprice & Order Book Imbalance (OBI)
total_size = bid_size + ask_size
if total_size > 0 and bid > 0.0 and ask > 0.0:
    microprice = (bid * ask_size + ask * bid_size) / float(total_size)
    obi = (bid_size - ask_size) / float(total_size)
else:
    microprice = mid_price if mid_price > 0.0 else last
    obi = 0.0

# Quote Age Watermarking & Staleness Check (DEFAULT_STALE_TOLERANCE_SEC = 5.0s)
quote_age_sec = (now - (quote_epoch_ms / 1000.0)) if quote_epoch_ms > 0 else 0.0
is_stale = quote_age_sec > self.stale_tolerance_sec if quote_epoch_ms > 0 else False
```

### 2.3 Bayesian Quarter-Kelly Sizing & Cash Settlement Clamping (`execution/sizing_governor.py`)
Optimal trade sizing integrates Bayesian empirical win-rate posterior estimation with Quarter-Kelly fractions, volatility risk budgets, and T+1 settled cash clearance:

$$\theta_{post} = \frac{W + \kappa \cdot \mu_{prior}}{N + \kappa}$$

$$f^* = \frac{1}{4} \left( \theta_{post} - \frac{1 - \theta_{post}}{R} \right) \left( \frac{N + \kappa}{N + 2\kappa} \right)$$

$$\text{Dollar Risk} = \text{NLV} \cdot \min(0.015, f^*)$$

$$\text{Target Shares} = \min\left( \left\lfloor \frac{\text{Dollar Risk}}{c_{ATR} \cdot \text{ATR}} \right\rfloor, \left\lfloor \frac{0.20 \cdot \text{NLV}}{P} \right\rfloor, \left\lfloor \frac{\text{Settled Cash}}{P} \right\rfloor \right)$$

```python
# execution/sizing_governor.py: Core Kelly properties
@property
def posterior_win_rate(self) -> float:
    total = self.alpha + self.beta
    return self.alpha / total if total > 0 else 0.5

@property
def full_kelly(self) -> float:
    b = self.win_loss_ratio
    if b <= 0.0:
        return 0.0
    p = self.posterior_win_rate
    q = 1.0 - p
    edge = (p * b) - q
    return max(0.0, edge / b)

@property
def kelly_fraction(self) -> float:
    return 0.25 * self.full_kelly
```

### 2.4 Stage 1 & Stage 2 EOD Deleveraging Logic (`execution/risk_manager.py`)
Two-Stage EOD De-Levering enforces overnight exposure reduction under FINRA Rule 4210(f)(8)(A) and cash risk constraints:
- **Stage 1 (15:50:00 ET Regular Close Trim)**: Peak closing auction liquidity; trims excess exposure to prevent overnight borrowing.
- **Stage 2 (19:45:00 ET Extended-Hours Hard Backstop)**: Hard backstop before 20:00 ET close.
- Positions are sorted prioritizing 3x LETFs first (`SOXL`, `TQQQ`, `TNA`, 90% overnight margin requirement), then by market value descending:

```python
# execution/risk_manager.py:1545-1633
def plan_two_stage_eod_deleveraging(
    self,
    current_time_et: Optional[Union[datetime, str]] = None,
) -> Dict[str, Any]:
    stage_name = "STAGE_1_1550_ET_REGULAR_CLOSE_TRIM"
    if current_time_et is not None:
        hour = current_time_et.hour if isinstance(current_time_et, datetime) else int(str(current_time_et).split(":")[0])
        if hour >= 17:
            stage_name = "STAGE_2_1945_ET_EXTENDED_HOURS_BACKSTOP"

    delta, total_val, allowable_limit = self.calculate_overnight_excess(enforce_letf_house_margin=True)
    if delta <= 0:
        return {"stage": stage_name, "delta": 0.0, "orders": [], "action_required": False}

    orders = []
    remaining_reduction_needed = delta

    # Sort positions prioritizing 3x LETFs first, then by market value descending
    sorted_positions = sorted(
        self.positions.items(),
        key=lambda item: (item[0].upper() in LETF_3X_SYMBOLS, item[1].get("quantity", 0) * item[1].get("current_price", 0.0)),
        reverse=True,
    )

    for sym, pos in sorted_positions:
        qty = pos.get("quantity", 0)
        px = pos.get("current_price", 0.0)
        if qty <= 0 or px <= 0:
            continue

        pos_val = qty * px
        req_over = self.get_asset_margin_requirements(sym, True)[1] # 0.90 for 3x LETFs
        effective_reduction_needed = remaining_reduction_needed / req_over if req_over > 0 else remaining_reduction_needed

        if pos_val <= effective_reduction_needed:
            shares_to_sell = qty
            notional_reduction = pos_val
        else:
            shares_to_sell = min(math.ceil(effective_reduction_needed / px), qty)
            notional_reduction = shares_to_sell * px

        margin_released = notional_reduction * req_over
        orders.append({
            "symbol": sym,
            "shares_to_sell": shares_to_sell,
            "estimated_price": px,
            "notional_reduction": round(notional_reduction, 2),
            "margin_released": round(margin_released, 2),
            "stage": stage_name,
            "reason": f"TWO_STAGE_EOD_DELEVERAGING_{stage_name}",
        })
        remaining_reduction_needed -= margin_released
        if remaining_reduction_needed <= 0:
            break

    return {"stage": stage_name, "delta": round(delta, 2), "orders": orders, "action_required": True}
```

### 2.5 Order Cancellation & Throttling Rules (`execution/order_manager.py`)
To prevent broker API quota exhaustion and WebSocket ping-ponging, Tier 2 catastrophe stops enforce minimum delta and cooldown throttling:
```python
# execution/order_manager.py:82-84, 561-630
TIER2_MIN_DELTA_PCT = 0.015  # 1.5% minimum price movement threshold
TIER2_COOLDOWN_SEC  = 180.0  # 180-second minimum cooldown between cancel-replace requests

def update_broker_stop(self, symbol: str, new_stop_price: float, current_order_id: Optional[str] = None):
    # Evaluates:
    # 1. abs(new_stop - last_stop) / last_stop >= 0.015
    # 2. time.monotonic() - last_cancel_replace_time >= 180.0s
    # If satisfied, cancels old stop and places new STOP order; otherwise suppressed.
```

Batch cancellation of open orders iterates over working orders:
```python
# execution/order_manager.py:923-967
def cancel_all_open_orders(self) -> int:
    working_orders = self.get_working_orders()
    cancelled = 0
    for order in working_orders:
        oid = str(order.get("orderId", ""))
        if self.cancel_order(oid):
            cancelled += 1
    return cancelled
```

### 2.6 Online 3-State HMM Meta-Governor (`execution/risk_manager.py`)
Maintains a continuous posterior probability vector $\pi(t) = [\pi_{Goldilocks}, \pi_{Uncertainty}, \pi_{Shock}]^T \in \Delta^2$ driven by a 3-dimensional macroeconomic observation vector $M(t) = [Z_s, S_{nlp}, CS_{bps}]^T$:
1. $Z_s$: Economic Surprise Z-Score (normalized forecast delta).
2. $S_{nlp}$: Exponentially decayed NLP Sentiment ($\lambda = \frac{\ln 2}{14400}$).
3. $CS_{bps}$: Live Hybrid High-Yield Credit Spread (OAS anchor + live HYG/IEF ratio).

#### Transition Matrix Formulation
$$\mathbf{T} = \begin{bmatrix}
T_{00} & T_{01} & T_{02} \\
T_{10} & T_{11} & T_{12} \\
T_{20} & T_{21} & T_{22}
\end{bmatrix} = \begin{bmatrix}
0.9993 & 0.0000 & 0.0007 \\
0.0032 & 0.9968 & 0.0000 \\
0.0029 & 0.0000 & 0.9971
\end{bmatrix}$$

#### Continuous Bayesian Filtering Step
$$\pi_{pred}(t) = \mathbf{T}^T \pi(t - \Delta t)$$

$$L_k(M(t)) = \exp\left( -\frac{1}{2 \cdot 3} \sum_{d=1}^3 \left( \frac{M_d(t) - \mu_{k, d}}{\sigma_{k, d}} \right)^2 \right)$$

$$\pi_k(t) = \frac{\pi_{pred, k} \cdot L_k(M(t))}{\sum_{j=0}^2 \pi_{pred, j} \cdot L_j(M(t))}$$

Calibrated Gaussian emission parameters:
- **Goldilocks ($k=0$)**: $\mu = [0.9946, 0.4803, 317.6]$, $\sigma = [0.2498, 0.1064, 18.8]$
- **Uncertainty ($k=1$)**: $\mu = [-0.2989, -0.0600, 406.8]$, $\sigma = [0.0920, 0.0417, 6.7]$
- **Macro Shock ($k=2$)**: $\mu = [-1.2430, -0.4720, 481.9]$, $\sigma = [0.2657, 0.1452, 35.3]$

#### Dynamic Target Satellite Weight
$$W_{sat}^*(t) = \begin{cases}
0.00, & \text{if } \pi_{Shock} \ge 0.60 \text{ or Dominant Regime} = \text{"Macro Shock"} \\
\min\left(0.35, \pi_{Goldilocks} \cdot 0.25 + \pi_{Uncertainty} \cdot 0.10 + \pi_{Shock} \cdot 0.00\right), & \text{otherwise}
\end{cases}$$

### 2.7 Dynamic Leland No-Trade Zone (NTZ) Router (`execution/risk_manager.py`)
To prevent transaction churn and bid-ask spread erosion in leveraged ETFs, capital allocations are filtered through a dynamic No-Trade Zone band $[\tau_{lower}, \tau_{upper}]$ centered on $W_{sat}^*(t)$:

$$\tau_{band} = \begin{cases} 0.04, & \text{if } \pi_{Goldilocks} > 0.70 \\ 0.02, & \text{otherwise (configurable to 0.03)} \end{cases}$$

$$\tau_{lower} = \max(0.0, W_{sat}^*(t) - \tau_{band}), \quad \tau_{upper} = \min(0.35, W_{sat}^*(t) + \tau_{band})$$

#### Decision Matrix
```text
IF active_w_sat > tau_upper OR (pi_Shock >= 0.60 AND active_w_sat > 0) THEN
    State = LELAND_NTZ_HARVEST ->
        1. Calculate excess satellite value: V_freed = (active_w_sat - tau_upper) * NLV (or 100% of sat_val if Shock)
        2. Liquidate 3x LETFs (SOXL/TQQQ/TNA) down to boundary
        3. Sweep freed capital into 1x Robo-Core basket:
           Delta Q_c = floor((V_freed * w_sweep_c) / P_c(t))
        4. Quantize residual cash into SWVXX (M_buf)

ELSE IF active_w_sat < tau_lower AND Trend_Gate == PASS THEN
    State = LELAND_NTZ_ACCUMULATE ->
        1. Calculate required capital: V_fund = (tau_lower - active_w_sat) * NLV
        2. Trim 1x Robo-Core holdings (SPY/QQQ/IWM/HYG/IEF)
        3. Rebalance into 3x LETFs according to sub-weights [0.40 SOXL, 0.35 TQQQ, 0.25 TNA]
        4. Quantize residual cash into SWVXX

ELSE (tau_lower <= active_w_sat <= tau_upper) THEN
    State = HOLD_INSIDE_NTZ ->
        Suppress order generation (Delta Q = 0). Maintain existing portfolio allocation.
```

### 2.8 Live HYG/IEF Credit Spread Proxy Calculations (`data/multi_rate_sync.py`)
Intraday credit risk expansion is tracked at 1-second resolution using the price ratio between high-yield corporate credit (`HYG`) and 7-10Y Treasuries (`IEF`). HYG has an effective spread duration of approximately 3.5 years, meaning a 1 bp increase in OAS drives a 0.035% ($3.5 \times 10^{-4}$) decline in the $\ln(P_{HYG} / P_{IEF})$ ratio.

$$r_{live}(t) = \frac{P_{HYG}(t)}{P_{IEF}(t)}, \quad r_{ref} = \text{Anchor Ratio at Session Open}$$

$$\Delta CS_{bps}(t) = -\frac{\ln\left( \frac{r_{live}(t)}{r_{ref}} \right)}{3.5 \times 10^{-4}}$$

$$CS_{intraday}(t) = OAS_{FRED} + \Delta CS_{bps}(t)$$

$$CS_{hybrid}(t) = 0.70 \cdot OAS_{FRED} + 0.30 \cdot CS_{intraday}(t)$$

### 2.9 Cash, Settlement & NLV Accounting Logic (`core/ledger.py`)
Total account equity (NLV) combines cleared funds, unsettled lots, marked position market values, and active order reservations:

```python
# core/ledger.py:295-302
@property
def nlv(self) -> Decimal:
    """Broker NLV when synced; otherwise settled + unsettled + marked positions."""
    with self._lock:
        if self._broker_nlv is not None:
            return self._broker_nlv
        return q(self._settled + self.unsettled_total + self.positions_value
                 + sum(self._reservations.values(), ZERO))
```

Position Mark-to-Market:
$$\text{positions\_value} = \sum_{i=1}^N Q_i \cdot P_{last, i}$$
'''


def build_section_3_static_markdown() -> str:
    """Builds the static baseline telemetry for Section 3."""
    return r"""## 3. Empirical Telemetry & Down-Day Incident Baselines

### 3.1 Header Metadata & Financial Summary
- **Session Date**: `2026-10-22` (Replay window: 2026-10-21 11:03:00 to 2026-10-22 14:33:45 EDT)
- **Engine Build**: Commit `93c03102a21aeee07d7e587382542abc7fb548cf` (Argon 2.4.0)
- **Target Account**: `***4015`
- **Starting Baseline NLV**: **$4,400.90 USD**
- **Ending NLV**: **$4,290.19 USD**
- **Net Portfolio Return**: **-$110.71 USD (-2.52%)**
- **Benchmark Performance**:
  - `SPY`: **-1.18%** (Open: $771.00, Close: $761.95) $\rightarrow$ Portfolio Delta: **-1.34% (-134.1 bps)**
  - `QQQ`: **-2.75%** (Open: $723.52, Close: $703.60) $\rightarrow$ Portfolio Delta: **+0.24% (+23.9 bps)**

### 3.2 Macro Regime & Credit Spread Expansion Metrics
| Metric | Value | Reference / Context |
| :--- | :--- | :--- |
| **Goldilocks Regime Occupancy** | **3,301 intervals (100.00%)** | $\pi_{Goldilocks} \approx 1.0$, Target $W_{sat}^* = 0.25$ |
| **Uncertainty Regime Occupancy** | **0 intervals (0.00%)** | Failed to trigger despite Tech selloff |
| **Macro Shock Regime Occupancy** | **0 intervals (0.00%)** | Failed to trigger |
| **Hybrid Credit Spread (Min)** | 314.29 bps | Morning baseline |
| **Hybrid Credit Spread (Max)** | 324.81 bps | Afternoon peak (+10.52 bps widening) |
| **Hybrid Credit Spread (Median)**| 316.95 bps | Session median |
| **Live HYG/IEF Price Ratio Range**| 0.843577 to 0.853994 | Range: 0.010417 |

### 3.3 Leland No-Trade Zone (NTZ) Evaluation Metrics
| Metric | Count / Value | Ratio / Operational Context |
| :--- | :--- | :--- |
| **Total Evaluated Intervals** | 3,301 | 30-second continuous evaluation sweeps |
| **Hold Inside NTZ (`HOLD_INSIDE_NTZ`)** | 3,300 | **99.97%** of evaluated intervals |
| **Initial Deployment Triggers** | 1 | 0.03% (at session startup 11:03:09 ET) |
| **Upper Harvest Breaches ($w_{sat} > \tau_{upper}$)** | 0 | 0.00% (No de-risking executed) |
| **Lower Accumulate Breaches ($w_{sat} < \tau_{lower}$)**| 0 | 0.00% (No dip-buying triggered) |
| **Total Orders Emitted** | 8 orders (28 shares) | Executed during initial portfolio hydration |
| **Total Turnover Notional** | $3,685.95 | 83.75% of starting baseline NLV |

#### Initial Portfolio Deployment (2026-10-21 11:03:09 ET)
| Order ID | Symbol | Sleeve | Side | Qty | Price ($) | Microprice ($) | $\Delta$ Micro | Notional ($) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `SHADOW-261021-0001` | **SPY** | CORE | `BUY` | 1 | $771.10 | $771.0880 | +$0.0120 | $771.10 |
| `SHADOW-261021-0002` | **QQQ** | CORE | `BUY` | 1 | $723.60 | $723.5829 | +$0.0171 | $723.60 |
| `SHADOW-261021-0003` | **IWM** | CORE | `BUY` | 1 | $276.64 | $276.6376 | +$0.0024 | $276.64 |
| `SHADOW-261021-0004` | **HYG** | CORE | `BUY` | 6 | $76.90 | $76.8984 | +$0.0016 | $461.40 |
| `SHADOW-261021-0005` | **IEF** | CORE | `BUY` | 5 | $90.24 | $90.2335 | +$0.0065 | $451.20 |
| `SHADOW-261021-0006` | **SOXL**| SATELLITE | `BUY` | 3 | $101.17 | $101.1433 | +$0.0267 | $303.51 |
| `SHADOW-261021-0007` | **TQQQ**| SATELLITE | `BUY` | 5 | $72.14 | $72.1397 | +$0.0003 | $360.70 |
| `SHADOW-261021-0008` | **TNA** | SATELLITE | `BUY` | 6 | $56.30 | $56.2996 | +$0.0004 | $337.80 |

### 3.4 Downsampled 5-Minute NLV & Regime Trajectory
| Timestamp (ET) | NLV ($) | Active $W_{sat}$ | Target $W_{sat}^*$ | Leland NTZ Band | Dominant Regime | Credit Spread (bps) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **2026-10-21 11:03:09** | $4,400.90 | 0.0000 | 0.2500 | [0.2100, 0.2900] | Goldilocks | 316.13 |
| **2026-10-21 11:30:10** | $4,408.92 | 0.2284 | 0.2500 | [0.2100, 0.2900] | Goldilocks | 316.65 |
| **2026-10-21 13:20:12** | $4,426.89 | 0.2304 | 0.2500 | [0.2100, 0.2900] | Goldilocks | 315.37 |
| **2026-10-21 15:00:15** | $4,401.01 | 0.2279 | 0.2500 | [0.2100, 0.2900] | Goldilocks | 314.63 |
| **2026-10-22 04:00:02** | $4,330.97 | 0.2194 | 0.2500 | [0.2100, 0.2900] | Goldilocks | 321.44 |
| **2026-10-22 08:25:07** | $4,338.92 | 0.2199 | 0.2500 | [0.2100, 0.2900] | Goldilocks | 321.32 |
| **2026-10-22 09:30:09** | $4,329.35 | 0.2185 | 0.2500 | [0.2100, 0.2900] | Goldilocks | 322.02 |
| **2026-10-22 10:00:09** | $4,351.11 | 0.2212 | 0.2500 | [0.2100, 0.2900] | Goldilocks | 320.45 |
| **2026-10-22 10:35:10** | $4,310.31 | 0.2164 | 0.2500 | [0.2100, 0.2900] | Goldilocks | 322.03 |
| **2026-10-22 11:15:11** | $4,295.20 | 0.2139 | 0.2500 | [0.2100, 0.2900] | Goldilocks | 322.26 |
| **2026-10-22 12:50:13** | $4,289.44 | 0.2130 | 0.2500 | [0.2100, 0.2900] | Goldilocks | 323.53 |
| **2026-10-22 14:05:15** | $4,287.04 | 0.2127 | 0.2500 | [0.2100, 0.2900] | Goldilocks | 324.67 |
| **2026-10-22 14:33:45** | $4,290.19 | 0.2132 | 0.2500 | [0.2100, 0.2900] | Goldilocks | 324.81 |

### 3.5 Incident Logs & Operational Failure Patterns
Analysis of incident logs from October 21-23 reveals critical operational fault modes across process deduplication, authentication state machines, and order dispatch:

1. **Rogue Cloud VM Daemon & Duplicate `AUTH_LOCKED` Alert Storm (#117)**:
   - *Timestamp*: `2026-10-22T09:45:08-04:00`
   - *Failure*: Legacy systemd unit `schwab-trader.service` (PID 259167) running on Google Cloud VM `schwab-trader` (`us-central1-a`) attempted 09:45 dynamic bracket recalibration for SOXL with stale, expired credentials.
   - *Symptom*: Schwab API returned HTTP 401 Unauthorized, tripping the auth circuit breaker to `AUTH_LOCKED`. This updated `/home/nicho/schwab_state/recovery_state.json` into `SHADOW_FLOOR_ONLY` and broadcast duplicate critical alert storms (`Recovery [SOXL]: S0 CORRUPTED`) to Android endpoints via ntfy.
   - *Root Cause*: Process duplication between the GCP VM background service and the primary Windows development host.
   - *Resolution*: Remediated via systemd service disabling (`systemctl disable schwab-trader schwab-web schwab-recovery-watchdog`) and rogue process termination.

2. **Authentication Circuit Breaker Latching (`AUTH_LOCKED`)**:
   - In `core/auth.py`, if 2 consecutive token refresh attempts return HTTP `401 Unauthorized` or `400 Bad Request`, the auth circuit breaker permanently trips to `AUTH_LOCKED` (`self.auth_circuit_open = True`).
   - *Deficiency*: When tripped, `_refresh_loop()` skips further execution. Any subsequent call to `get_access_token()` immediately raises `AuthCircuitBreakerError`, freezing all automated trading and order management until manual reset via `/api/auth/reset` or explicit user reauthorization.

3. **Order Dispatch Timing & Rate-Limit Clamping**:
   - Daily Schwab REST quota is capped at 3,500 requests (`daily_api_quota: 3500`), with an intraday burst governor enforcing 120 req/min (2.0 req/sec token bucket).
   - Essential exit orders (`MANDATORY_FLATTEN`, `TIER1_STOP`, `EMERGENCY`) bypass daily quotas via `is_essential=True`, but non-essential rebalancing sweeps risk quota starvation during high-volatility sessions.
"""


def build_section_4_markdown() -> str:
    """Builds Section 4: Identified Architectural & Modeling Gaps."""
    return r"""## 4. Identified Architectural & Modeling Gaps

### Gap A: Absence of Fill-Ratio and Time-Decay Escalation in EOD Flatten Routine
- **Current Behavior**:
  - The 15:50 EOD deleveraging routine (`plan_two_stage_eod_deleveraging` in `execution/risk_manager.py`) calculates notional reduction and generates passive limit orders or standard router calls.
  - In `execution/order_manager.py:714-720`, `execute_terminal_flush()` routes orders with urgency `TERMINAL_FLUSH`, which allows 60 seconds of adaptive limit-pegging before falling back to IOC sweeps.
- **Defect & Failure Mode**:
  - There is zero awareness of the **unfilled execution ratio** as the clock approaches 16:00 ET regular market close.
  - If closing auction liquidity is thin or an order remains partially filled at 15:56 ET, the system does not dynamically widen its limit offsets, does not escalate from limit to marketable aggressive pegging, and does not execute an asymptotic time-decay squeeze.
  - Consequently, leveraged 3x positions (`SOXL`, `TQQQ`, `TNA`) risk being trapped overnight into an unclosed state, violating FINRA Rule 4210(f)(8)(A) margin limits and exposing the cash account to catastrophic gap-down risk.
- **Refactor Target**:
  - Implement an asymptotic time-decay escalation curve over $[15:50:00, 15:58:00]$:
    $$\text{Peg Offset}(t) = \text{Base Offset} \cdot \left( 1 + \left(\frac{t - t_{start}}{t_{end} - t_{start}}\right)^\kappa \right)$$
  - Track fill-ratio $R_{fill} = \frac{Q_{filled}}{Q_{target}}$. If $R_{fill} < 1.0$ at $15:55:00$ ET, automatically escalate urgency to aggressive NBBO cross, and at $15:58:00$ ET force immediate IOC sweep.

### Gap B: Symmetrical, Static & Degenerate HMM State Transition Probabilities
- **Current Behavior**:
  - In `execution/risk_manager.py:76-80`, the empirical transition matrix is statically hardcoded:
    ```python
    EMPIRICAL_TRANSITION_MATRIX_3X3 = [
        [0.9993, 0.0, 0.0007],
        [0.0032, 0.9968, 0.0],
        [0.0029, 0.0, 0.9971],
    ]
    ```
- **Defect & Failure Mode**:
  - **Zero Transition Probability to Uncertainty**: The transition probability from Goldilocks to Uncertainty ($T_{01}$) is strictly `0.0000`. The engine is mathematically prohibited from transitioning directly from Goldilocks into Uncertainty via markovian prediction! It can only jump directly to Macro Shock ($T_{02} = 0.0007$).
  - **Severe Diagonal Inertia**: The diagonal persistence values ($0.9993$, $0.9968$, $0.9971$) create massive hysteresis. Unless an observation vector $M(t)$ exhibits an extreme $5\sigma$ outlier, $\pi(t)$ remains anchored in its initial state.
  - **Empirical Confirmation**: During the October 22 selloff (where QQQ dropped -2.75% and credit spreads widened +10.5 bps), the HMM remained 100.00% occupied in Goldilocks across 3,301 intervals. The system failed to de-risk or transition to Uncertainty, leaving 3x LETFs fully exposed to a -2.52% drawdown.
- **Refactor Target**:
  - Re-estimate the transition matrix $\mathbf{T}$ to introduce asymmetric transition dynamics:
    - Fast crash transitions: $T_{01} \approx 0.020$, $T_{02} \approx 0.005$.
    - Slow, damped recovery hysteresis: $T_{20} \approx 0.001$, $T_{10} \approx 0.005$.
  - Implement dynamic transition matrix scaling indexed to live implied volatility ($VIX$) and credit spread velocity ($\frac{d}{dt} CS_{hybrid}$).

### Gap C: Missing Programmatic GFV Checks on Intraday Redeployment of Cash
- **Current Behavior**:
  - `core/ledger.py` explicitly purges Good Faith Violation (GFV) checks, claiming the account operates under a FINRA Rule 4210 margin framework:
    ```python
    # core/ledger.py:88-92, 404-413, 576-577
    def can_sell_position(self, symbol: str, quantity: int = 0, today: Optional[date] = None):
        return True, f"Margin account: {symbol} is eligible for instantaneous exit (GFV exempt)."

    @property
    def unsettled_cash(self) -> Decimal:
        return ZERO

    def record_sell(self, symbol, quantity, price, ...):
        # FINRA Rule 4210: Instantaneous settlement — proceeds immediately credit cash!
        self._settled += proceeds
    ```
- **Defect & Failure Mode**:
  - Account `***4015` with $4,400 NLV is legally a **cash account** subject to **SEC Rule 15c6-1 T+1 Cash Settlement Rules** (the $25,000 Pattern Day Trader minimum prevents unconstrained margin day trading).
  - Under SEC Rule 15c6-1, sales of equities require $T+1$ business days to clear. If proceeds from a morning sale ($T_0$) are immediately redeployed to purchase another security ($T_0$) and that newly purchased security is subsequently sold on the same day (e.g. via stop-loss or EOD flatten), the trade constitutes a **Good Faith Violation (GFV)**.
  - Accruing 3 GFVs within a rolling 12-month window results in Schwab placing a mandatory **90-Day Cash Restriction** on the account (requiring fully cleared cash up-front for all purchases).
  - Because `ledger.py` immediately credits sale proceeds into `_settled`, the engine allows unrestricted intraday redeployment and same-day re-selling, creating an immediate regulatory compliance risk.
- **Refactor Target**:
  - Re-establish strict Three-Bucket Cash Accounting:
    1. `settled_cash`: Cleared funds available for unrestricted purchase and immediate round-trips.
    2. `unsettled_cash`: Unsettled lots with explicit settlement timestamps ($T+1$).
    3. `pending_ach`: External transfer reservations.
  - Tag each position with `funded_with_settled: bool`. If an asset was purchased with unsettled funds, block intraday market/limit exits unless overridden by catastrophic stops, forcing an overnight hold until settlement clears.

### Gap D: Synchronous Blocking Calls, Silent Exception Handlers & Missing Token Heartbeats
- **Current Behavior & Defects**:
  1. **Synchronous `time.sleep` in Core Execution Paths**:
     - `execution/almgren_chriss.py:420`: `time.sleep(min(self.slice_interval_seconds, 0.5))` directly inside the slicing loop.
     - `execution/almgren_chriss.py:643`: `time.sleep(min(poll_interval, max(0.01, timeout_seconds - elapsed)))` during order status polling.
     - `execution/order_manager.py:1233`: `self._sleep_fn = sleep_fn or time.sleep` executed in `DualSleeveSwapCoordinator` during backoff retry loops.
     - `core/rate_limiter.py:509`: `time.sleep(min(sleep_duration, 0.05))` inside `acquire()`.
     - *Impact*: In an asynchronous asyncio event loop or multi-threaded tick ingestion architecture, synchronous `time.sleep` blocks the OS thread, delaying WebSocket tick processing, stale quote watermarking, and bracket updates.
  2. **Silent Exception Swallowing (`except Exception: pass`)**:
     - `execution/order_manager.py:454, 557, 656, 800, 876`: Hardware stop cancellations, bracket replaces, and telemetry events catch generic `Exception` and silently `pass`.
     - `execution/microstructure_sentinel.py:234, 364`: Quote parsing errors silently swallowed.
     - *Impact*: Prevents root-cause diagnosis of API failures, payload schema changes, or database write locks.
  3. **Absence of Proactive Token Refresh Heartbeat**:
     - `core/auth.py` runs a 60-second polling daemon (`_refresh_loop`), but it lacks an active health heartbeat sent to the broker API.
     - If the system remains idle pre-market (e.g. 08:00 - 09:25 ET), tokens expiring right at 09:30:00 ET cause immediate `401 Unauthorized` errors on opening cross orders, tripping the circuit breaker into `AUTH_LOCKED` at the most critical liquidity window of the session.
- **Refactor Target**:
  - Replace all synchronous `time.sleep` in async-facing modules with non-blocking `asyncio.sleep` or event-driven timers.
  - Replace generic `except Exception: pass` blocks with typed exception handling, structured error logging, and telemetry event recording.
  - Implement a dedicated pre-market Token Health Heartbeat at 09:15:00 ET (`scripts/premarket_check.py`) that forces a token refresh and validates connectivity against `/trader/v1/accounts/accountNumbers` prior to market open.

---
*SchwabEngine Quantitative Research Reference v2.4 (Argon) — Compiled for Issue #122 Architecture & Quantitative Audit*
"""


def compile_research_context(
    scope: str = "general",
    since: str = "today",
    lines: int = 50,
    units: Optional[List[str]] = None,
    subject: str = "DEEP QUANTITATIVE RESEARCH CONTEXT (#122)",
) -> str:
    """Compiles the four-section grounded research context document."""
    if units is None:
        units = [
            "schwab-trader.service",
            "schwab-web.service",
            "schwab-recovery-watchdog.service",
        ]

    now_iso = datetime.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    if len(now_iso) >= 5 and (now_iso[-5] in ("+", "-")) and (":" not in now_iso[-5:]):
        now_iso = now_iso[:-2] + ":" + now_iso[-2:]

    git_meta = get_git_metadata()

    # Collect database telemetry using core.paths
    db_candidates = [
        TRADES_DB_PATH,
        TELEMETRY_DB_PATH,
        BASELINES_DB_PATH,
        STATE_DIR / "trading_journal.db",
        DATA_DIR / "schwab_telemetry.db",
        PROJECT_ROOT / "state" / "trades.db",
        PROJECT_ROOT / "state" / "telemetry.db",
        PROJECT_ROOT / "state" / "market_baselines.db",
    ]
    seen_db = set()
    unique_dbs = []
    for p in db_candidates:
        try:
            resolved = str(p.resolve())
            if p.exists() and resolved not in seen_db:
                seen_db.add(resolved)
                unique_dbs.append(p)
        except Exception:
            if p.exists():
                unique_dbs.append(p)

    db_reports = [inspect_sqlite_db(p) for p in unique_dbs]

    # Collect journalctl logs
    service_logs: Dict[str, Dict[str, str]] = {}
    for unit in units:
        status = query_systemctl_status(unit)
        logs = query_journalctl(unit, since=since, lines=lines)
        service_logs[unit] = {"status": status, "logs": logs}

    # Format scoped code extraction blocks
    if scope == "comprehensive":
        sections_output = []
        sec_idx = 1
        for cat_name, file_tuples in COMPREHENSIVE_CATEGORIES.items():
            sections_output.append(f"#### 2.10.{sec_idx} {cat_name}\n\n")
            for rel_path, desc in file_tuples:
                sections_output.append(extract_code_file(rel_path, subtitle=desc))
            sec_idx += 1
        scoped_code_content = "".join(sections_output)
    else:
        scoped_files = SCOPE_DEFINITIONS.get(scope, SCOPE_DEFINITIONS["general"])
        scoped_code_content = "".join([extract_code_file(f) for f in scoped_files])

    # Construct the document adhering strictly to the 4 canonical sections
    header = f"""# SYSTEM ARCHITECTURE & STRATEGY REVIEW: {subject}
- timestamp_iso: "{now_iso}"
- engine_version: "2.4.0-argon"
- git_branch: "{git_meta['branch']}"
- commit_sha: "{git_meta['commit']}"
- account_scope: "single_account_comprehensive" (Account: ***4015)
- baseline_nlv_usd: 4400.00

---
"""

    section_1 = build_section_1_markdown()
    section_2_base = build_section_2_markdown()
    section_2_scoped = f"\n### 2.10 Scoped Codebase Logic & Extracted Modules (Scope: `{scope}`)\n\n" + scoped_code_content
    section_3_static = build_section_3_static_markdown()

    # Dynamic telemetry portions for Section 3
    section_3_dynamic = "\n### 3.6 Live Systemd Daemon Health & Journalctl Telemetry\n"
    for unit, data in service_logs.items():
        section_3_dynamic += f"#### Unit: `{unit}`\n"
        section_3_dynamic += f"* **Daemon Status:** `{data['status']}`\n\n"
        section_3_dynamic += f"```text\n{data['logs']}\n```\n\n"

    section_3_dynamic += """### 3.7 State Store & SQLite Database Telemetry

| Database Path | Exists | Size (KB) | Journal Mode | Tables (Row Counts) |
| :--- | :---: | :---: | :---: | :--- |
"""
    for rep in db_reports:
        tbl_summary = ", ".join([f"{k}: {v}" for k, v in rep.get("tables", {}).items()]) or "None"
        section_3_dynamic += f"| `{rep['path']}` | {rep['exists']} | {rep['size_kb']} | `{rep['journal_mode']}` | {tbl_summary} |\n"

    section_4 = build_section_4_markdown()

    full_doc = "\n".join([
        header.strip(),
        section_1.strip(),
        "---\n",
        section_2_base.strip(),
        section_2_scoped.strip(),
        "---\n",
        section_3_static.strip(),
        section_3_dynamic.strip(),
        "---\n",
        section_4.strip(),
    ])

    return sanitize_markdown_output(full_doc)


def main() -> None:
    parser = argparse.ArgumentParser(description="Compile Schwab Engine Research Context Snapshot.")
    parser.add_argument(
        "--scope",
        default="general",
        choices=list(SCOPE_DEFINITIONS.keys()),
        help="Subsystem extraction scope (default: general)",
    )
    parser.add_argument(
        "--since",
        default="today",
        help="Journalctl time horizon query (default: 'today')",
    )
    parser.add_argument(
        "--output",
        default="research_context.md",
        help="Target output markdown path (default: research_context.md)",
    )
    parser.add_argument(
        "--subject",
        default="DEEP QUANTITATIVE RESEARCH CONTEXT (#122)",
        help="Subject title for document header",
    )
    parser.add_argument(
        "--lines",
        type=int,
        default=50,
        help="Max log lines to extract per systemd unit (default: 50)",
    )
    parser.add_argument(
        "--units",
        default="schwab-trader.service,schwab-web.service,schwab-recovery-watchdog.service",
        help="Comma-separated systemd service units to query",
    )
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help="Skip automated Google Drive mirror synchronization",
    )
    args = parser.parse_args()

    units_list = [u.strip() for u in args.units.split(",") if u.strip()]

    logger.info("Starting research context compilation...")
    logger.info("  Scope: %s", args.scope)
    logger.info("  Since: %s", args.since)
    logger.info("  Output target: %s", args.output)

    markdown_content = compile_research_context(
        scope=args.scope,
        since=args.since,
        lines=args.lines,
        units=units_list,
        subject=args.subject,
    )

    out_path = Path(args.output)
    if not out_path.is_absolute():
        out_path = PROJECT_ROOT / out_path

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown_content, encoding="utf-8")

    # If compiling to docs/ or custom file, also mirror to project root research_context.md
    root_target = PROJECT_ROOT / "research_context.md"
    if out_path.resolve() != root_target.resolve():
        root_target.write_text(markdown_content, encoding="utf-8")
        logger.info("Mirrored to repository root: %s", root_target)

    total_lines = len(markdown_content.splitlines())
    size_bytes = out_path.stat().st_size

    logger.info("Compilation complete!")
    logger.info("  File: %s", out_path)
    logger.info("  Lines: %d", total_lines)
    logger.info("  Size: %d bytes (%.2f KB)", size_bytes, size_bytes / 1024)

    # Print header preview
    print("\n--- [HEADER PREVIEW] ---")
    for line in markdown_content.splitlines()[:20]:
        print(line)
    print("------------------------\n")

    # Automated Google Drive Sync
    if not args.no_sync:
        sync_ok = sync_to_google_drive()
        if sync_ok:
            logger.info("Authoritative Google Drive mirror updated at G:\\My Drive\\Projects\\SchwabEngine\\research_context.md")
        else:
            logger.warning("Google Drive mirror encountered a warning or was skipped.")


if __name__ == "__main__":
    main()
