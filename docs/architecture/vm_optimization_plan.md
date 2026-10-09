# Production Linux VM Architecture & Optimization Plan
## `schwab_engine` High-Performance Dual-Process Infrastructure

**Document Version:** 2.0.0  
**Target Environment:** Ubuntu 22.04 LTS / Debian 12 (Linux VM, Dedicated vCPUs)  
**Status:** Approved Architectural Implementation Plan  
**Related Specifications:** [01-day-trading-architecture.md](file:///c:/Projects/schwab_engine/docs/01-day-trading-architecture.md), [05-resilient-execution.md](file:///c:/Projects/schwab_engine/docs/05-resilient-execution.md)

---

## 1. Executive Summary & Architectural Overview

The `schwab_engine` system is transitioning from a monolithic single-process server to a decoupled, high-resilience, dual-process Linux VM architecture. In high-frequency, low-latency intraday trading on 3x leveraged equity ETFs (`SOXL`, `TQQQ`, `TNA`), co-locating HTTP dashboard rendering, file I/O, and WebSocket streaming within the same Python process introduces execution jitter, event loop lag, and potential cascade failures.

### Key Architectural Pillars

```
+---------------------------------------------------------------------------------------------------+
|                                        LINUX VM HOST                                              |
|                                                                                                   |
|  [Core 0 - Dedicated]              [Core 1 - Dedicated]            [Core 2+ - Shared Background]  |
|  nice -n -10                       nice -n 0                       nice -n 19                     |
|  +---------------------------+     +------------------------+      +---------------------------+  |
|  |   schwab-trader.service   |     |   schwab-web.service   |      |  Workers (Timers)         |  |
|  |                           |     |                        |      |                           |  |
|  | - Schwab Streaming WS     |     | - FastAPI / Uvicorn    |      | - schwab-backfill.timer   |  |
|  | - Bracket/Stop Evaluator  |     | - PWA UI (Port 8080)   |      |   (17:15 EDT, 30 RPM)     |  |
|  | - Risk & Kelly Sizing     |     | - Local SSE/WS Relay   |      | - schwab-midday.timer     |  |
|  | - 15:50 EDT Auto-Flatten  |     | - Read-Only API Views  |      |   (11:35 EDT, Hurst/CI)   |  |
|  | - Order Routing Engine    |     |                        |      |                           |  |
|  +-------------+-------------+     +-----------+------------+      +-------------+-------------+  |
|                |                               |                                 |                |
|                | Exclusive Writes              | Read-Only Queries               | Delta Ingestion|
|                v                               v                                 v                |
|       +-----------------+             +-----------------+               +-----------------+       |
|       | state/trades.db |             |   state/IPC &   |               |      state/     |       |
|       |  (Fills, P&L,   |             | control_signal  |               | market_baselines|       |
|       |   Wash-Sales)   |             |  .json (Halt)   |               |       .db       |       |
|       +-----------------+             +-----------------+               +-----------------+       |
|                |                               |                                 |                |
|                +-------------------------------+---------------------------------+                |
|                                                |                                                  |
|                                                v                                                  |
|                                      +--------------------+                                       |
|                                      | state/telemetry.db |                                       |
|                                      | (Append-Only WAL)  |                                       |
|                                      +--------------------+                                       |
+---------------------------------------------------------------------------------------------------+
```

1. **Zero-Jitter Dual-Process Decoupling**:
   - `schwab-trader.service`: Runs the headless execution engine pinned to **CPU Core 0** via `taskset -c 0` with elevated scheduler priority `nice -n -10`. Dedicated exclusively to market data ingestion, trailing stop evaluation, order execution, and the mandatory 15:50 EDT flattening routine.
   - `schwab-web.service`: Runs the FastAPI dashboard server pinned to **CPU Core 1** (`taskset -c 1`) with standard priority. Serves the PWA and WebSocket telemetry without ever blocking the trading loop.

2. **Strict SQLite Partitioning & WAL Concurrency**:
   - Eliminates single-writer database lock contention (`database is locked`) by segregating data into three specialized SQLite databases: `state/trades.db` (exclusive writes from trader daemon), `state/market_baselines.db` (exclusive writes from off-hours backfill), and `state/telemetry.db` (async event log).
   - Enforces `PRAGMA journal_mode=WAL;` and `PRAGMA busy_timeout=5000;` on all connections.

3. **Charles Schwab Single-Streamer Invariant**:
   - Charles Schwab enforces a strict single-active-streamer constraint per API account; secondary connections trigger fatal `400/409` session disconnections.
   - Only `schwab-trader.service` opens the authenticated Schwab streaming WebSocket. The web service streams real-time updates to UI clients via an internal local Unix domain socket / SSE broadcast relay.

4. **Off-Hours Bar Archival & Synthetic Baseline Engine**:
   - `workers/off_hours_backfill.py` runs at 17:15 EDT via systemd timer. Throttled to 30 RPM (1 call every 2.0s) and aborted if daily consumption exceeds 2,500 calls (safeguarding against the 3,500 hard cap).
   - Persists 1-minute delta bars and pre-calculates 20-day rolling minute-of-day Average Daily Volume (ADV) curves for 24 candidate ETFs and benchmarks (`SOX`, `NDX`, `RUT`, `USO`). Enables zero-API-call, zero-latency RVOL normalization at 09:30 EDT.

5. **Midday Walk-Forward Regime Filter**:
   - `workers/midday_optimizer.py` executes during the 11:30–14:00 EDT Midday Freeze at 11:35 EDT. Pinned to Core 2+ at `nice -n 19`.
   - Evaluates morning price action against a 5-day sliding window ($N \approx 2,000$ bars). Calculates Hurst Exponent ($H$) and Choppiness Index ($CI$). If choppy ($H \le 0.50$), updates `state/dynamic_policy.json` at 13:55 EDT, raising the Power Hour RVOL hurdle to $1.65\times$ and scaling sizing to $0.35\times$.

6. **Preservation of Core Defect Fixes**:
   - Maintains verified liquidity invariant math: $\text{Total Liquid Backstop} = \text{settled\_cash} + \text{open\_positions\_market\_value} + \text{external\_liquid\_backstop}$.
   - Preserves SQLite trade rehydration on boot (`GET /api/orders/today`).
   - Retains the SWVXX sweep firewall ($<\$25,000$ account equity or $<\$3,500$ settled cash).

---

## 2. File Map & Module Responsibilities

### 2.1 Files to Create

| File Path | Component | Responsibility |
|:---|:---|:---|
| `core/baselines_store.py` | Database Layer | SQLite repository for `state/market_baselines.db` (1m bars, ADV minute profiles, Yang-Zhang baselines). |
| `core/ipc.py` | IPC Layer | Atomic file signaling (`control_signal.json`, `dynamic_policy.json`) and local Unix socket broadcast relay (`/tmp/schwab_trader.sock`). |
| `workers/off_hours_backfill.py` | Background Worker | 17:15 EDT quota-aware bar archival and 20-day ADV profile generator. |
| `workers/midday_optimizer.py` | Background Worker | 11:35 EDT walk-forward regime classifier (Hurst $H$, CI) generating Power Hour policy overrides. |
| `scripts/systemd/schwab-trader.service` | System Supervision | Systemd unit for Core 0 high-priority trading daemon. |
| `scripts/systemd/schwab-web.service` | System Supervision | Systemd unit for Core 1 FastAPI dashboard service. |
| `scripts/systemd/schwab-backfill.service` | System Supervision | One-shot service for 17:15 EDT bar backfill worker. |
| `scripts/systemd/schwab-backfill.timer` | System Supervision | Systemd timer triggering backfill service at 17:15 EDT weekdays. |
| `scripts/systemd/schwab-midday.service` | System Supervision | One-shot service for 11:35 EDT midday regime optimizer. |
| `scripts/systemd/schwab-midday.timer` | System Supervision | Systemd timer triggering midday optimizer at 11:35 EDT weekdays. |
| `tests/test_baselines_store.py` | Verification Suite | Unit tests for `market_baselines.db` WAL mode, bar deduplication, and ADV profile queries. |
| `tests/test_ipc_control.py` | Verification Suite | Unit tests for atomic signal writing, timeout handling, and socket stream relay. |
| `tests/test_off_hours_backfill.py` | Verification Suite | Unit tests for quota ceiling guard (abort at $\ge 2,500$), 30 RPM rate limiting, and delta calculation. |
| `tests/test_midday_optimizer.py` | Verification Suite | Unit tests for Hurst calculation on 5-day sliding window and atomic `dynamic_policy.json` emission. |

### 2.2 Files to Modify

| File Path | Planned Modifications |
|:---|:---|
| `core/paths.py` | Add `BASELINES_DB_PATH`, `TELEMETRY_DB_PATH`, `CONTROL_SIGNAL_FILE`, `DYNAMIC_POLICY_FILE`, `TRADER_IPC_SOCKET`. |
| `main.py` | Add `--headless-trader` mode flag. Initialize IPC control poller, local tick broadcast relay, and dynamic policy hot-reloader. |
| `api/server.py` | Decouple from in-memory engine context. Read trade history directly from `trades.db`, baselines from `market_baselines.db`, emit emergency halts via `control_signal.json`, and subscribe to local IPC socket for UI WebSocket streaming. |
| `core/telemetry.py` | Re-point `DEFAULT_TELEMETRY_DB` to `STATE_DIR / "telemetry.db"`. |
| `execution/strategies.py` | In `preload_historical()`, query `market_baselines.db` instead of issuing outbound Schwab REST calls during boot. Load dynamic policy parameters (`rvol_hurdle`, `size_scale`) from `dynamic_policy.json`. |
| `core/engine.py` | Incorporate dynamic policy reload hook prior to Power Hour (13:55 EDT). |
| `remote_deploy.sh` | Update deployment pipeline to register and restart both systemd services and timers on the VM. |

### 2.3 Files to Deprecate / Supersede

| File Path | Disposition | Rationale |
|:---|:---|:---|
| `scripts/systemd/schwab_engine.service` | Deprecate | Superseded by separated `schwab-trader.service` and `schwab-web.service`. |
| `scripts/calibrate_midday_universe.py` | Deprecate | Superseded by production-grade `workers/midday_optimizer.py`. |

---

## 3. Complete Systemd Unit and Timer Definitions

All units are deployed to `/etc/systemd/system/` on the production Linux VM.

### 3.1 `schwab-trader.service` (Dedicated Execution Daemon)

```ini
[Unit]
Description=Schwab Trading Engine Daemon (Core 0 Execution)
After=network.target time-sync.target
Wants=time-sync.target

[Service]
Type=simple
User=schwab
Group=schwab
WorkingDirectory=/home/schwab/schwab_engine
EnvironmentFile=/home/schwab/schwab_engine/.env
Environment=PYTHONUNBUFFERED=1
Environment=ENGINE_STATE_DIR=/home/schwab/schwab_state

# Zero-Jitter OS Scheduling: CPU Core 0 Pinning + Elevated Real-Time Priority
CPUAffinity=0
Nice=-10
LimitMEMLOCK=infinity
LimitNOFILE=65536

# Execution Command
ExecStart=/home/schwab/schwab_engine/.venv/bin/python main.py --headless-trader --live

# Robust Process Lifecycle Management
Restart=always
RestartSec=3s
KillSignal=SIGTERM
TimeoutStopSec=25
FinalKillSignal=SIGKILL

# Journald Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=schwab-trader

[Install]
WantedBy=multi-user.target
```

### 3.2 `schwab-web.service` (Dashboard & API Server)

```ini
[Unit]
Description=Schwab Trading Web & API Gateway (Core 1+)
After=network.target schwab-trader.service
Wants=schwab-trader.service

[Service]
Type=simple
User=schwab
Group=schwab
WorkingDirectory=/home/schwab/schwab_engine
EnvironmentFile=/home/schwab/schwab_engine/.env
Environment=PYTHONUNBUFFERED=1
Environment=ENGINE_STATE_DIR=/home/schwab/schwab_state

# Bind to CPU Core 1 to isolate HTTP workloads from Core 0
CPUAffinity=1
Nice=0
LimitNOFILE=65536

# FastAPI / Uvicorn Server on Port 8080
ExecStart=/home/schwab/schwab_engine/.venv/bin/python -m uvicorn api.server:app --host 0.0.0.0 --port 8080 --workers 1 --log-level info

# Process Lifecycle Management
Restart=always
RestartSec=5s
KillSignal=SIGTERM
TimeoutStopSec=15

StandardOutput=journal
StandardError=journal
SyslogIdentifier=schwab-web

[Install]
WantedBy=multi-user.target
```

### 3.3 `schwab-backfill.service` & `schwab-backfill.timer` (17:15 EDT Archival)

#### `scripts/systemd/schwab-backfill.service`
```ini
[Unit]
Description=Schwab Off-Hours Bar Archival & Baseline Sync Worker
After=network.target

[Service]
Type=oneshot
User=schwab
Group=schwab
WorkingDirectory=/home/schwab/schwab_engine
EnvironmentFile=/home/schwab/schwab_engine/.env
Environment=PYTHONUNBUFFERED=1
Environment=ENGINE_STATE_DIR=/home/schwab/schwab_state

# Background Priority on Core 2+
CPUAffinity=2-3
Nice=19
IOSchedulingClass=idle

ExecStart=/home/schwab/schwab_engine/.venv/bin/python workers/off_hours_backfill.py

StandardOutput=journal
StandardError=journal
SyslogIdentifier=schwab-backfill
```

#### `scripts/systemd/schwab-backfill.timer`
```ini
[Unit]
Description=Timer for Schwab Off-Hours Bar Archival (17:15 EDT Weekdays)

[Timer]
# 17:15 EDT is 21:15 UTC (Daylight Saving) or 22:15 UTC (Standard Time)
# systemd supports timezone specification:
OnCalendar=Mon..Fri *-*-* 17:15:00 America/New_York
Persistent=true

[Install]
WantedBy=timers.target
```

### 3.4 `schwab-midday.service` & `schwab-midday.timer` (11:35 EDT Midday Optimizer)

#### `scripts/systemd/schwab-midday.service`
```ini
[Unit]
Description=Schwab Midday Regime Walk-Forward Optimizer
After=network.target

[Service]
Type=oneshot
User=schwab
Group=schwab
WorkingDirectory=/home/schwab/schwab_engine
EnvironmentFile=/home/schwab/schwab_engine/.env
Environment=PYTHONUNBUFFERED=1
Environment=ENGINE_STATE_DIR=/home/schwab/schwab_state

# Background Priority on Core 2+
CPUAffinity=2-3
Nice=19
IOSchedulingClass=idle

ExecStart=/home/schwab/schwab_engine/.venv/bin/python workers/midday_optimizer.py

StandardOutput=journal
StandardError=journal
SyslogIdentifier=schwab-midday
```

#### `scripts/systemd/schwab-midday.timer`
```ini
[Unit]
Description=Timer for Schwab Midday Regime Optimizer (11:35 EDT Weekdays)

[Timer]
OnCalendar=Mon..Fri *-*-* 11:35:00 America/New_York
Persistent=true

[Install]
WantedBy=timers.target
```

---

## 4. Multi-Database SQLite Partitioning & IPC Architecture

### 4.1 SQLite Database Partitioning Map

| Database Path | Exclusive Writer | Concurrent Readers | Concurrency PRAGMAs | Contents |
|:---|:---|:---|:---|:---|
| `state/trades.db` | `schwab-trader` | `schwab-web`, manual CLI | WAL, `busy_timeout=5000`, `synchronous=NORMAL` | Executed trades, fills, realized P&L, wash-sale disallowances. |
| `state/market_baselines.db` | `schwab-backfill` | `schwab-trader`, `schwab-midday`, `schwab-web` | WAL, `busy_timeout=5000`, `synchronous=NORMAL` | Historical 1m bars, 20-day ADV minute curves, Yang-Zhang baselines. |
| `state/telemetry.db` | `schwab-trader` (async worker) | `schwab-web` | WAL, `busy_timeout=5000`, `synchronous=NORMAL` | Append-only domain events, book snapshots, tick telemetry. |

### 4.2 SQLite DDL Schemas

#### 1. `state/trades.db` Schema
```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT UNIQUE NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,               -- BUY / SELL
    quantity INTEGER NOT NULL,
    price REAL NOT NULL,
    cost_basis REAL NOT NULL,
    realized_pnl REAL DEFAULT 0.0,
    disallowed_loss REAL DEFAULT 0.0,
    timestamp TEXT NOT NULL,          -- ISO 8601 UTC
    regime TEXT DEFAULT '',           -- A (Trend), B (Mean-Rev), C (Chop)
    simulated INTEGER NOT NULL DEFAULT 0,
    env TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_trades_created_at ON trades (created_at);
CREATE INDEX IF NOT EXISTS idx_trades_env_created ON trades (env, created_at);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades (symbol);
```

#### 2. `state/market_baselines.db` Schema
```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;

-- 1-minute historical OHLCV bars
CREATE TABLE IF NOT EXISTS bars_1m (
    symbol TEXT NOT NULL,
    timestamp INTEGER NOT NULL,       -- Epoch milliseconds (UTC)
    datetime_edt TEXT NOT NULL,       -- ISO 8601 EDT string
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume INTEGER NOT NULL,
    PRIMARY KEY (symbol, timestamp)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_bars_sym_time ON bars_1m (symbol, timestamp DESC);

-- Rolling 20-day Average Daily Volume (ADV) minute-of-day curves
CREATE TABLE IF NOT EXISTS adv_minute_profiles (
    symbol TEXT NOT NULL,
    minute_of_day INTEGER NOT NULL,   -- 0 to 1439 (Eastern Time minute)
    adv_volume REAL NOT NULL,         -- 20-day mean volume for this minute
    sample_days INTEGER NOT NULL,     -- Number of distinct days in window
    updated_at TEXT NOT NULL,
    PRIMARY KEY (symbol, minute_of_day)
) WITHOUT ROWID;

-- Rolling 20-day Yang-Zhang Volatility & Baseline Metrics
CREATE TABLE IF NOT EXISTS symbol_baselines (
    symbol TEXT PRIMARY KEY,
    yz_volatility_20d REAL NOT NULL,
    median_spread_cents REAL NOT NULL,
    adv_20d_shares REAL NOT NULL,
    updated_at TEXT NOT NULL
);
```

#### 3. `state/telemetry.db` Schema
```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS event_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE NOT NULL,
    timestamp TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_event_lookup ON event_log (aggregate_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_event_type ON event_log (event_type, timestamp DESC);
```

### 4.3 Atomic IPC Control & Policy Schemas

Inter-process communication between `schwab-web` and `schwab-trader` avoids synchronous HTTP coupling by utilizing atomic file writes with `os.replace`.

#### 1. Emergency Kill-Switch & Flatten: `state/control_signal.json`
```json
{
  "sequence_id": 1042,
  "timestamp": "2026-10-08T15:02:11.450Z",
  "action": "HALT",
  "reason": "OPERATOR_KILL_SWITCH",
  "acknowledged_by_trader": true,
  "acknowledged_at": "2026-10-08T15:02:11.480Z"
}
```
*Supported Actions:*
- `HALT`: Trading daemon blocks all new entry orders immediately. Active trailing stops remain live.
- `RESUME`: Restores order entry permissions.
- `LIQUIDATE_ALL`: Blocks entries and executes immediate market orders to flatten all active positions.

#### 2. Midday Regime Calibration Override: `state/dynamic_policy.json`
```json
{
  "updated_at": "2026-10-08T13:55:00.000Z",
  "effective_session": "POWER_HOUR",
  "morning_hurst": 0.47,
  "morning_ci": 58.4,
  "regime_classification": "HIGH_NOISE_CHOP",
  "overrides": {
    "power_hour_rvol_hurdle": 1.65,
    "power_hour_size_scale": 0.35,
    "ratchet_permitted": false
  },
  "rationale": "Morning tape exhibits Hurst 0.47 <= 0.50 (anti-persistent chop). Throttling Power Hour risk."
}
```

---

## 5. Centralized WebSocket Ingestion & Local IPC Stream Relay

Charles Schwab limits each developer app to **one active streaming WebSocket connection** per account. Multiple simultaneous connections cause status 400/409 disconnects.

```
+-------------------------------------------------------------------------+
|                         CENTRALIZED STREAMING RELAY                     |
|                                                                         |
|  [Charles Schwab Streaming API]                                         |
|  wss://streamer-api.schwabapi.com/ws                                    |
|                   |                                                     |
|                   | Single Authenticated TLS Session                    |
|                   v                                                     |
|  +---------------------------------+                                    |
|  |       schwab-trader.service     |                                    |
|  | - SchwabStreamer (Core 0)       |                                    |
|  | - Dispatches to Trading Engine  |                                    |
|  | - Broadcasts to UNIX Socket     |                                    |
|  +----------------+----------------+                                    |
|                   |                                                     |
|                   | UNIX Domain Socket (/tmp/schwab_trader.sock)        |
|                   | Low-Latency Non-Blocking Broadcast (<0.1ms)         |
|                   v                                                     |
|  +---------------------------------+                                    |
|  |        schwab-web.service       |                                    |
|  | - Unix Socket Client (Core 1)   |                                    |
|  | - WebSocket Manager             |                                    |
|  +----------------+----------------+                                    |
|                   |                                                     |
|                   | FastAPI /stream Endpoint                            |
|                   v                                                     |
|       [Browser / React PWA Dashboard]                                   |
+-------------------------------------------------------------------------+
```

### Protocol Implementation
1. `schwab-trader.service` opens a Unix domain socket server at `/tmp/schwab_trader.sock`.
2. As ticks arrive from Schwab's `LEVELONE_EQUITIES` stream, the trader daemon serializes high-frequency updates into JSON frames:
   ```json
   {"type": "TICK", "symbol": "SOXL", "last": 156.40, "bid": 156.38, "ask": 156.41, "vol": 4210050, "ts": 1728414000120}
   ```
3. `schwab-web.service` connects to `/tmp/schwab_trader.sock` asynchronously using `asyncio.open_unix_connection()`.
4. When web clients connect to `ws://localhost:8080/stream`, the web server relays ticks and internal heartbeats directly to connected browsers. Zero external Schwab sockets are created by the web layer.

---

## 6. Incremental Off-Hours Bar Archival (`workers/off_hours_backfill.py`)

Triggered at **17:15 EDT** by `schwab-backfill.timer`.

### 6.1 Architectural Rules & Safety Quotas
1. **API Daily Quota Ceiling Guard**:
   - Queries `core/rate_limiter.py` or `state/daily_quota.json`.
   - If `daily_calls_used >= 2500`, **abort backfill immediately** with a critical log and notify ntfy alert topic. This preserves 1,000 calls buffer against the 3,500 hard daily cap for emergency broker operations.
2. **Strict Request Rate Throttling**:
   - Implements a 30 RPM rate limiter: sleep $\ge 2.0\text{s}$ between outbound REST calls.
3. **Incremental Delta Querying**:
   - For each symbol, inspect `MAX(timestamp)` in `state/market_baselines.db`.
   - If data exists, request only the delta from `MAX(timestamp)` to market close (16:00 EDT).
   - If empty, backfill the past 20 trading days.
4. **Symbol Universe Coverage (24 Symbols)**:
   - **20 Candidate ETFs**: `SOXL`, `SOXS`, `TQQQ`, `SQQQ`, `TNA`, `TZA`, `UPRO`, `SPXU`, `FNGU`, `NVDL`, `TECL`, `USD`, `FAS`, `DPST`, `BOIL`, `KOLD`, `UCO`, `SCO`, `NUGT`, `LABU`.
   - **4 Macro Benchmarks**: `SOX` (PHLX Semiconductor), `NDX` (Nasdaq-100), `RUT` (Russell 2000), `USO` (United States Oil).
5. **ADV Minute Curve Pre-Calculation**:
   - For each minute of the trading day ($570 \le \text{mod} \le 960$, corresponding to 09:30–16:00 EDT), compute:
     $$\text{ADV}(\text{symbol}, m) = \frac{1}{20} \sum_{d=1}^{20} \text{Volume}_{d, m}$$
   - Write updated profiles to `adv_minute_profiles` table.
   - At 09:30 EDT next day, `StrategyEngine` loads these curves instantly from SQLite in $<5\text{ms}$ with zero REST calls.

---

## 7. Midday Walk-Forward Regime Filter (`workers/midday_optimizer.py`)

Triggered at **11:35 EDT** by `schwab-midday.timer` during the 11:30–14:00 EDT Midday Freeze.

### 7.1 Resource Isolation
- Pinned to **CPU Cores 2-3** (`taskset -c 2-3`) with lowest CPU scheduling priority (`nice -n 19`).
- Clamped worker process pool: `processes = max(1, os.cpu_count() - 2)` to eliminate thread contention on Core 0/1.

### 7.2 Walk-Forward Methodology (Anti-Overfitting)
- Evaluating the 120-bar morning session in isolation creates severe small-sample estimation bias.
- **Window Formulation**: Concatenates the 120 1-minute bars from today's morning drive (09:30–11:30 EDT) with the prior 5 trading days' 1-minute bars ($N \approx 2,000$ bars total).
- Evaluates:
  1. **Hurst Exponent ($H$)**: Uses Rescaled Range (R/S) analysis with Anis-Lloyd theoretical small-sample bias correction ([`compute_hurst_rs`](file:///c:/Projects/schwab_engine/indicators/volatility.py#L192-L230)).
  2. **Choppiness Index ($CI_{14}$)**: Average Choppiness Index across the morning tape.

### 7.3 Quantitative Decision Matrix & Action
| Hurst Exponent ($H$) | Regime Classification | Power Hour RVOL Hurdle | Sizing Multiplier | Trailing Stop Ratchet |
|:---|:---|:---|:---|:---|
| $H \le 0.50$ | High-Noise Chop / Anti-Persistent | $1.65\times$ (Bumped from $1.25\times$) | $0.35\times$ (Scaled from $0.50\times$) | Locked (Disallowed) |
| $0.50 < H \le 0.55$ | Random Walk / Efficient Tape | $1.40\times$ | $0.45\times$ | Locked (Disallowed) |
| $H > 0.55$ | Trend-Reinforcing / Persistent | $1.25\times$ (Standard) | $0.50\times$ (Standard) | Permitted |

- At **13:55 EDT** (5 minutes prior to Power Hour open at 14:00 EDT), the worker writes the calibrated parameters atomically to `state/dynamic_policy.json`.
- `schwab-trader.service` reloads `dynamic_policy.json` on mtime change, enforcing the calibrated hurdles without process restart.

---

## 8. Concurrency & CPU Core Pinning Architecture

### 8.1 VM vCPU Allocation Matrix

```
+========================================================================================+
| CORE 0 (Dedicated)        | CORE 1 (Dedicated)        | CORE 2+ (Shared Background)    |
| Priority: nice -n -10     | Priority: nice -n 0       | Priority: nice -n 19           |
+===========================+===========================+================================+
| Process: schwab-trader    | Process: schwab-web       | Process: Batch Workers         |
|                           |                           |                                |
| Workloads:                | Workloads:                | Workloads:                     |
| - Schwab Streamer WS      | - Uvicorn ASGI loop       | - off_hours_backfill.py        |
| - Level 1 Tick Handling   | - FastAPI REST endpoints  |   (17:15 EDT, 30 RPM)          |
| - Microstructure Orderflow| - Static Asset PWA        | - midday_optimizer.py          |
| - Trailing Stop Ratchets  | - Browser WebSockets      |   (11:35 EDT, Hurst / CI)      |
| - Order Routing Engine    | - Local Socket Reader     | - Telemetry DB VACUUM          |
| - 15:50 EDT Auto-Flatten  | - Macro JSON Poller       | - Off-line Log Compaction      |
+========================================================================================+
```

### 8.2 Execution Invariants & Non-Interference
- **Core 0 Purity**: No background document ingestion, heavy LLM generation, or database vacuuming may execute on Core 0.
- **Lockless Read-Throughs**: The web service queries SQLite databases using read-only connections (`sqlite3.connect('file:...trades.db?mode=ro', uri=True)`) under WAL mode, ensuring queries never block trader writes.

---

## 9. Verification & Automated Test Suites

The test suite enforces regression parity for the four core bug fixes while validating the new multi-process architecture.

### 9.1 Test Matrix

| Test Module | Coverage & Verification Invariants |
|:---|:---|
| `tests/test_ledger_macro_invariant.py` | Macro liquidity invariant parity: $\text{Total Backstop} = \text{true\_equity} + \text{external\_backstop}$. Verifies gross closed turnover (Bucket 2) is strictly non-additive. |
| `tests/test_trade_store_hydration.py` | Intraday trade persistence across engine restarts: WAL mode validation, query by EDT date, verification of `/orders/today`. |
| `tests/test_sweep_guard.py` | SWVXX sweep firewall: Verifies `action="NONE"` when NLV $<\$25,000$ or settled cash $<\$3,500$. |
| `tests/test_baselines_store.py` | Validates `SQLiteBaselinesStore` DDL, bar deduplication via `(symbol, timestamp)` primary key, and ADV minute profile calculations. |
| `tests/test_ipc_control.py` | Tests atomic signal serialization in `control_signal.json`, emergency kill-switch state transition, and Unix socket event streaming. |
| `tests/test_off_hours_backfill.py` | Tests 30 RPM rate limiter, daily API quota abortion guard at $\ge 2,500$ calls, and delta bar calculation. |
| `tests/test_midday_optimizer.py` | Tests Hurst exponent and Choppiness calculation across 2,000 synthetic bars, verifying Power Hour hurdle adjustments at $H \le 0.50$. |

---

## 10. Step-by-Step Terminal Deployment & Validation Runbook

### Phase 1: Environment & Directory Preparation
```bash
# 1. Connect to Linux VM
ssh schwab@<VM_IP_ADDRESS>

# 2. Navigate to project root and ensure directories exist
cd /home/schwab/schwab_engine
mkdir -p /home/schwab/schwab_state
mkdir -p /home/schwab/schwab_engine/workers
mkdir -p /home/schwab/schwab_engine/logs

# 3. Pull latest repository changes
git pull origin main

# 4. Activate Python virtual environment and install requirements
source .venv/bin/activate
pip install -r requirements.txt
```

### Phase 2: Run Full Automated Verification Suite
```bash
# Verify 100% pass rate across all unit and integration tests
pytest tests/ -v
```

### Phase 3: Install & Reload Systemd Services and Timers
```bash
# 1. Copy unit definitions to systemd directory
sudo cp scripts/systemd/schwab-trader.service /etc/systemd/system/
sudo cp scripts/systemd/schwab-web.service /etc/systemd/system/
sudo cp scripts/systemd/schwab-backfill.service /etc/systemd/system/
sudo cp scripts/systemd/schwab-backfill.timer /etc/systemd/system/
sudo cp scripts/systemd/schwab-midday.service /etc/systemd/system/
sudo cp scripts/systemd/schwab-midday.timer /etc/systemd/system/

# 2. Reload systemd daemon
sudo systemctl daemon-reload

# 3. Enable timers and services
sudo systemctl enable schwab-trader.service
sudo systemctl enable schwab-web.service
sudo systemctl enable schwab-backfill.timer
sudo systemctl enable schwab-midday.timer

# 4. Start services
sudo systemctl start schwab-trader.service
sudo systemctl start schwab-web.service
sudo systemctl start schwab-backfill.timer
sudo systemctl start schwab-midday.timer
```

### Phase 4: Production Runtime Validation Commands
```bash
# 1. Verify CPU Pinning and Priority Status
ps -eo pid,ni,psr,comm | grep -E "python|uvicorn"
# Expected:
# schwab-trader PID has NI=-10, PSR=0
# schwab-web PID has NI=0, PSR=1

# 2. Check Systemd Service Status
sudo systemctl status schwab-trader.service --no-pager
sudo systemctl status schwab-web.service --no-pager

# 3. Verify Systemd Timers
systemctl list-timers | grep schwab
# Expected:
# schwab-backfill.timer scheduled for 17:15 EDT
# schwab-midday.timer scheduled for 11:35 EDT

# 4. Verify SQLite Database WAL Modes
sqlite3 /home/schwab/schwab_state/trades.db "PRAGMA journal_mode;"
sqlite3 /home/schwab/schwab_state/market_baselines.db "PRAGMA journal_mode;"
sqlite3 /home/schwab/schwab_state/telemetry.db "PRAGMA journal_mode;"
# Expected: wal for all three databases

# 5. Test Web Dashboard and Endpoints via Localhost
curl -s http://localhost:8080/health | jq .
curl -s http://localhost:8080/api/v1/liquidity/state | jq .state.total_liquid_backstop
curl -s http://localhost:8080/api/orders/today | jq .

# 6. Test Emergency Halt Control IPC
curl -X POST http://localhost:8080/emergency/halt \
  -H "Content-Type: application/json" \
  -d '{"halted": true}' | jq .
cat /home/schwab/schwab_state/control_signal.json
```

---

## 11. Conclusion & Next Steps

Upon review and approval of this plan, implementation proceeds in sequential stages:
1. **Stage 1**: Database schemas and repositories (`core/baselines_store.py`, `core/paths.py`).
2. **Stage 2**: Decoupled dual-process runtime and IPC engine (`core/ipc.py`, `main.py`, `api/server.py`).
3. **Stage 3**: Background workers (`workers/off_hours_backfill.py`, `workers/midday_optimizer.py`).
4. **Stage 4**: Test suite implementation and validation runs.
5. **Stage 5**: Systemd unit deployment on the production VM.
