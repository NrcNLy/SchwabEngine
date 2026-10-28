# SchwabEngine: Core System & Cloud Infrastructure Architecture

## Overview
SchwabEngine is an autonomous, quantitative day-trading system engineered for the Charles Schwab Developer API. Designed for high-frequency intraday trading across high-beta and 3x leveraged equity ETFs (e.g., `SOXL`, `TQQQ`, `TNA`), the platform enforces deterministic risk governance, unidirectional state flow, real-time telemetry streaming, and automated ledger persistence.

---

## High-Level System Architecture

```
+---------------------------------------------------------------------------------------------------+
|                                      SCHWABENGINE ARCHITECTURE                                     |
|                                                                                                   |
|  +---------------------------------------------------------------------------------------------+  |
|  |                                Charles Schwab Developer API                                 |  |
|  |             OAuth 2.0 / REST (Quotes, Orders, Accounts) / WebSocket Streaming               |  |
|  +----------------------------------------------+----------------------------------------------+  |
|                                                 |                                                 |
|                                                 v                                                 |
|  +---------------------------------------------------------------------------------------------+  |
|  |                               CORE EXECUTION ENGINE (DAEMON)                                |  |
|  |                                                                                             |  |
|  |  +--------------------------+  +--------------------------+  +--------------------------+   |  |
|  |  |  Streaming WebSocket    |  |  Market Regime & HMM     |  |  Execution & Strategy   |   |  |
|  |  |  - 1s Tick Aggregator    |  |  - Volatility Envelopes  |  |  - Dual-Sleeve Router    |   |  |
|  |  |  - L1/L2 Book & Trades   |  |  - Dynamic Hurdle Rates  |  |  - Dynamic Bracket Orders|   |  |
|  |  +------------+-------------+  +------------+-------------+  +------------+-------------+   |  |
|  |               |                             |                             |                 |  |
|  |               +-----------------------------+-----------------------------+                 |  |
|  |                                             |                                               |  |
|  |                                             v                                               |  |
|  |  +---------------------------------------------------------------------------------------+  |  |
|  |  |                              RISK & LIQUIDITY GOVERNOR                                |  |  |
|  |  |  - Sizing Governor (Fractional Kelly)        - Cash Settled Sweep Guard (T+1 / GFV)   |  |  |
|  |  |  - Drawdown Circuit Breakers                 - Hard 15:50 EDT Auto-Flatten Sentinel   |  |  |
|  |  +------------------------------------------+--------------------------------------------+  |  |
|  +---------------------------------------------|-----------------------------------------------+  |
|                                                |                                                  |
|                        +-----------------------+-----------------------+                          |
|                        |                                               |                          |
|                        v                                               v                          |
|  +-------------------------------------------+   +-------------------------------------------+    |
|  |             API GATEWAY (FASTAPI)         |   |          PERSISTENCE & TELEMETRY          |    |
|  |  - Host: 0.0.0.0:8080 (Uvicorn)           |   |  - SQLite Ledgers: trades.db, telemetry.db|    |
|  |  - Server-Sent Events (SSE) Streamer      |   |  - Microsecond Telemetry: JSONL Logs      |    |
|  |  - Remote Control Signals & Health Checks |   |  - AES-GCM Encrypted Token Vault          |    |
|  +-------------------------------------------+   +-------------------------------------------+    |
|                        |                                               |                          |
|                        v                                               v                          |
|  +-------------------------------------------+   +-------------------------------------------+    |
|  |           VITE / REACT DASHBOARD          |   |          EXTERNAL RECONCILIATION          |    |
|  |  - Dark Mode Real-Time Operations UI      |   |  - Automated EOD Markdown Digest Generator|    |
|  |  - Multi-Sleeve Allocation & P&L Charts   |   |  - Google Drive Bidirectional Doc Sync    |    |
|  +-------------------------------------------+   +-------------------------------------------+    |
+---------------------------------------------------------------------------------------------------+
```

---

## 3. Portfolio Partitioning & Sizing (Pure Tactical Model - #140)

### 1. Decommissioning of Legacy Core Sleeve
- **Removal of Static Core Allocation:** The legacy 65% Core sleeve (`SPY`, `QQQ`, `IWM`, `HYG`, `IEF`) is formally decommissioned. The engine maintains zero static index holdings.
- **Dedicated Tactical Capital Allocation:** 100% of trading capacity is reallocated to high-conviction, intraday 3x leveraged equity ETF satellites (`SOXL`, `TQQQ`, `TNA`).

### 2. Pure Tactical Intraday Capital Lifecycle
- **100% Settled Cash Baseline at 09:30 EDT Open:**
  - Account `***4015` opens every trading day with a 100% settled cash baseline ($4,400.90 baseline) and 0.00% equity exposure.
  - Sizing algorithms strictly bind to cleared settled cash ($S(t)$) under live broker Net Liquidation Value (NLV), guaranteeing zero Good Faith Violations (GFV).
- **0.00% Overnight Equity Exposure & SEC Rule 15c6-1 T+1 Compliance:**
  - A mandatory 15:50:00 EDT terminal liquidation gate aggressively flattens 100% of open 3x leveraged satellites to settled cash via marketable limit and market sweeps.
  - Zero equity inventory (0.00%) is carried overnight, fully eliminating LETF overnight volatility drag and compound drift while ensuring clean T+1 settlement under SEC Rule 15c6-1.
- **Macro Regime Priors Governing Intraday Deployment Capacity:**
  - Cold Plane Bayesian macro regime probabilities dictate aggregate intraday tactical deployment capacity:
    * **Goldilocks Regime:** Up to 75% gross tactical deployment capacity ($W_{\text{sat}}^* \le 0.75$).
    * **Uncertainty Regime:** Throttled to 35% gross deployment capacity ($W_{\text{sat}}^* \le 0.35$).
    * **Macro Shock Regime:** 0.00% deployment capacity ($W_{\text{sat}}^* = 0.00$); trading engine is immediately locked out and all tactical exposures remain in 100% cash.

---

## Production Cloud Topology & Migration Status (Task #119)

### 1. Topology Split (Current vs. Target)

The operational topology of SchwabEngine is transitioning from a fragile single-workstation configuration to an autonomous, distributed cloud infrastructure.

```
CURRENT STATE (Local Monolith):
+----------------------------------------------------------------+      +-------------------------------+
|                      Local PC Workstation                      |      |       Google Cloud VM         |
|                                                                |      |       (schwab-trader)         |
|  - Engine Daemon (PID 15164, Shadow / Live loop)               |      |                               |
|  - WebSocket Streaming Client (1s tick aggregation)           |      |  - Power: RUNNING             |
|  - SQLite Ledger & Active JSONL Telemetry (Single Truth)       |      |  - Systemd Services: DISABLED |
|  - Vite Web UI (PID 8652, Port 3000)                           |      |  - Engine Process: STOPPED    |
|  - Subject to: Sleep modes, Windows Updates, ISP Drops         |      |  - Tokens: Stale (Oct 19)     |
+----------------------------------------------------------------+      +-------------------------------+

TARGET STATE (Cloud Autonomous Daemon + Ad-Hoc Thin Client):
+----------------------------------------------------------------+      +-------------------------------+
|                      Google Cloud VM                           |      |      Local PC / Thin Client   |
|                 (schwab-trader, us-central1-a)                 |      |                               |
|                                                                |      |  - Vite Dashboard UI Viewer   |
|  - 24/7 Headless Engine Daemon (`schwab-trader.service`)       | <--- |  - Ad-hoc Monitoring & Audits |
|    * Mode: `--mode shadow --config config/config.yaml`         |      |  - No local broker orders     |
|    * Core 0 Pinning (`CPUAffinity=0`, `Nice=-10`)              |      |  - No local state mutation    |
|  - High-Throughput WebSocket Streamer & 1s Tick Aggregator     |      |                               |
|  - Persistent SQLite Ledgers (`trades.db`, `telemetry.db`)     |      +-------------------------------+
|  - FastAPI Gateway (`schwab-web.service`, Core 1 Pinning)      |                      |
|  - Automated EOD Markdown Digest & Google Drive Cloud Sync     | <--------------------+
+----------------------------------------------------------------+
```

| Dimension | Current Local Monolith | Target GCP Cloud Deployment |
| :--- | :--- | :--- |
| **Execution Authority** | Local PC (Single source of truth) | GCP VM (`schwab-trader`, Dedicated Headless) |
| **Uptime Guarantee** | ~90–95% (Interrupted by OS, power, sleep) | 99.99% Tier-3 Datacenter SLA |
| **Telemetry Ingestion** | Local `logs/session_telemetry_*.jsonl` | Remote VM `/home/nicho/schwab_state/` & Cloud Drive |
| **UI Presentation** | Local Vite server on `localhost:3000` | Secure tunnel or API gateway relay to local UI |
| **State Mutation** | Local disk writes (`trading_journal.db`) | VM writes with automated off-box cloud replication |

---

### 2. Environmental Reliability Rationale

Operating high-leverage quantitative trading systems (e.g. 3x levered ETFs like `SOXL`) on local residential desktop hardware introduces severe existential and tail-risk failure modes:

1. **Power Interruption & Unscheduled Hardware Reboots:**
   - Residential power lines are vulnerable to brownouts, storm outages, and tripping breakers. An ungraceful shutdown during active market hours leaves open unfilled bracket legs, unhedged market exposure, and corrupts WAL-mode SQLite database locks.
2. **Operating System Sleep States & Windows Update Cycles:**
   - Consumer desktop operating systems (Windows 11) prioritize power-saving states and enforce automatic overnight/midday restarts for cumulative security patches. A suspended CPU halts the `asyncio` event loop, drops socket heartbeats, and fails to fire the mandatory 15:50 EDT auto-flatten risk rule.
3. **Residential ISP Latency Spikes & IP Dynamic Re-Allocation:**
   - Residential internet service providers (ISPs) suffer from bufferbloat, unannounced line maintenance, and periodic dynamic IP re-leasing. The Schwab OAuth 2.0 streaming broker connection requires uninterrupted TCP Keep-Alive frames. Packet drop bursts cause socket disconnections during high-volatility market opens.
4. **Thermal Throttling & Background Workstation Interference:**
   - Running compilation tasks, IDEs, browser rendering, or background user tasks on the same workstation introduces CPU scheduling contention, context switching latency, and garbage collection pauses that directly compromise the sub-second reactivity required by the Volatility Envelope sizing governor.

Migrating to the remote Google Cloud VM (`schwab-trader`) isolates the trading daemon within Google's us-central1-a software-defined datacenter, offering redundant fiber backbones, multi-source power feeds, and strict hardware-level virtualization guarantees.

---

### 3. VM Migration Action Plan

To transition production execution cleanly from the local workstation to `schwab-trader` without double-execution, stale token rejections, or telemetry collisions, execute the following step-by-step checklist:

#### Phase A: Workspace Cleanliness & Codebase Alignment
- [ ] **Step 1:** Commit and push all verified local improvements to remote:
  ```bash
  git push origin feat/dynamic-screener-universe
  ```
- [ ] **Step 2:** Open an SSH session into the GCP instance:
  ```bash
  gcloud compute ssh schwab-trader --zone=us-central1-a
  ```
- [ ] **Step 3:** Cleanly stash or reset untracked/dirty local artifacts on the VM:
  ```bash
  cd /home/nicho/schwab_engine
  git stash push -m "pre-migration-cleanup-$(date +%Y%m%d)"
  ```
- [ ] **Step 4:** Pull the latest branch state and verify commit parity:
  ```bash
  git fetch origin feat/dynamic-screener-universe
  git checkout feat/dynamic-screener-universe
  git pull origin feat/dynamic-screener-universe
  git log -n 1 --oneline
  # Verify HEAD matches local commit hash (e.g., 25b29ea)
  ```

#### Phase B: Environment & Dependency Verification
- [ ] **Step 5:** Activate the VM virtual environment and synchronize dependencies:
  ```bash
  source /home/nicho/schwab_engine/.venv/bin/activate
  pip install -r requirements.txt
  ```
- [ ] **Step 6:** Run regression test suite and 5-point Monday preflight invariant verifier:
  ```bash
  # Regression test gate
  pytest tests/ -k "not integration" -q

  # Monday pre-market invariant verifier (09:00:00 - 09:25:00 EDT)
  python scripts/tools/check_preflight_invariants.py --dry-run --nlv 4400.0
  ```

#### Phase C: Cryptographic Token Vault Synchronization
- [ ] **Step 7:** Synchronize the verified active `schwab_tokens_vault.json` from the local desktop to the VM:
  ```powershell
  # Executed from local desktop
  gcloud compute scp C:\Projects\schwab_engine\schwab_tokens_vault.json schwab-trader:/home/nicho/schwab_state/schwab_tokens_vault.json --zone=us-central1-a
  gcloud compute scp C:\Projects\schwab_engine\schwab_tokens_vault.json schwab-trader:/home/nicho/schwab_engine/schwab_tokens_vault.json --zone=us-central1-a
  ```
- [ ] **Step 8:** Lock file permissions on the VM to owner-only read/write (`600`):
  ```bash
  chmod 600 /home/nicho/schwab_state/schwab_tokens_vault.json /home/nicho/schwab_engine/schwab_tokens_vault.json
  ```
- [ ] **Step 9:** Verify decryption and token hydration on the VM:
  ```bash
  python main.py --authenticate
  ```

#### Phase D: Systemd Service Reconfiguration & Drop-in Purge
- [ ] **Step 10:** Remove legacy override drop-ins (such as obsolete `--live --headless-trader` flags in `live.conf`):
  ```bash
  sudo rm -f /etc/systemd/system/schwab-trader.service.d/live.conf
  ```
- [ ] **Step 11:** Reconfigure `/etc/systemd/system/schwab-trader.service` to enforce modern CLI arguments:
  ```ini
  [Unit]
  Description=Schwab Trading Engine Daemon (Core 0 Execution)
  After=network.target time-sync.target
  Wants=time-sync.target

  [Service]
  Type=simple
  User=nicho
  Group=nicho
  WorkingDirectory=/home/nicho/schwab_engine
  EnvironmentFile=/home/nicho/schwab_engine/.env
  Environment=PYTHONUNBUFFERED=1
  Environment=ENGINE_STATE_DIR=/home/nicho/schwab_state
  Environment=TRADER_IPC_SOCKET=/tmp/schwab_trader.sock

  # Performance Tuning: Pin to Core 0 with Elevated Nice Priority
  CPUAffinity=0
  Nice=-10
  LimitMEMLOCK=infinity
  LimitNOFILE=65536

  # Headless Engine in Shadow Mode
  ExecStart=/home/nicho/schwab_engine/.venv/bin/python main.py --mode shadow --config config/config.yaml --headless-trader

  Restart=always
  RestartSec=5s
  KillSignal=SIGTERM
  TimeoutStopSec=25
  FinalKillSignal=SIGKILL

  StandardOutput=journal
  StandardError=journal
  SyslogIdentifier=schwab-trader

  [Install]
  WantedBy=multi-user.target
  ```
- [ ] **Step 12:** Reload systemd configuration:
  ```bash
  sudo systemctl daemon-reload
  ```

#### Phase E: Execution Handoff & Anti-Collision Verification
- [ ] **Step 13:** Confirm local background engine daemons are fully terminated:
  ```powershell
  # Verify port 8080 and 3000 are clear on local host
  Get-NetTCPConnection -LocalPort 8080, 3000 -ErrorAction SilentlyContinue
  ```
- [ ] **Step 14:** Enable and launch services on the VM:
  ```bash
  sudo systemctl enable --now schwab-trader.service schwab-web.service
  ```
- [ ] **Step 15:** Tail remote logs in real-time to confirm clean initialization:
  ```bash
  journalctl -u schwab-trader -f --no-pager
  ```

---

### 4. Hardware Sizing Verdict & Cost Analysis

Google Cloud Recommender suggests downsizing instance `schwab-trader` from `e2-standard-2` to `e2-medium`, projecting an estimated cost reduction of **$24.12/month** ($48.92/mo $\rightarrow$ $24.80/mo).

```
+-----------------------------------+-----------------------------------+
|     e2-standard-2 (Current)       |       e2-medium (Proposed)        |
+-----------------------------------+-----------------------------------+
| 2 vCPUs (Dedicated Allocation)    | 2 vCPUs (Shared / Burstable)      |
| 8 GB Memory                       | 4 GB Memory                       |
| Sustained 100% CPU time-slice     | Baseline 0.5 vCPU/core (50% max)  |
| Clean native CPU core pinning     | Throttled upon credit depletion   |
| ~$48.92 / month                   | ~$24.80 / month (Saves $24.12/mo) |
+-----------------------------------+-----------------------------------+
```

#### Quantitative Workload Profiling
1. **Memory Profile:**
   - FastAPI + Uvicorn API gateway: ~90–120 MB
   - Schwab multi-symbol WebSocket streaming client (L1/L2 order book + 1s bars): ~110–140 MB
   - Continuous Hidden Markov Model (HMM) filter & Parkinson volatility estimators: ~70–120 MB
   - Total steady-state memory demand: **~270–380 MB** (Peaking at ~650 MB during EOD journal batch parsing).
   - *Memory Verdict:* Both 4 GB (`e2-medium`) and 8 GB (`e2-standard-2`) satisfy memory constraints with substantial headroom.
2. **CPU & Scheduling Latency Analysis:**
   - SchwabEngine relies on strict sub-second loop frequency:
     $$\Delta t_{\text{tick}} \le 1.0\,\text{second}$$
   - During continuous high-frequency intraday market hours, the engine processes concurrent WebSocket packet arrival, real-time bar aggregation, Hurst exponent / ATR recalculations, and background SQLite WAL writes.
   - `e2-medium` uses **burstable shared cores**. Under continuous load, the instance rapidly exhausts its CPU burst credits. GCP's hypervisor will throttle CPU scheduling, causing `asyncio` event loop lag, delayed stop-loss order evaluation, and missed WebSocket ping/pong frames leading to abrupt TCP disconnects.
   - Furthermore, systemd native CPU pinning (`CPUAffinity=0` for the trader daemon and `CPUAffinity=1` for the web gateway) requires real dedicated hyperthread allocations. On `e2-medium`, both virtual cores share physical compute with noisy multi-tenant neighbors.

#### Engineering Recommendation
**VERDICT: REJECT DOWNSIZING. RETAIN `e2-standard-2`.**

> [!IMPORTANT]
> In leveraged ETF trading (`SOXL` 3x), an execution delay of just 2 to 3 seconds during a volatility spike can incur hundreds of dollars in slippage, completely erasing the $24.12 monthly infrastructure savings. Hardware determinism and non-burstable CPU guarantees are non-negotiable prerequisites for autonomous quantitative execution.
>
> **Cost Optimization Alternative:** If budget reduction is required, purchase a 1-year or 3-year Google Cloud Committed Use Discount (CUD) for `e2-standard-2`, which saves 37% to 55% ($18 to $27/month) while preserving 100% dedicated compute performance.

---

## Cold Plane Vertex AI Ingestion & Signal Persistence (Task #124)

### 1. Decoupled Asynchronous Macro Synthesis Topology
To insulate the microsecond hot execution loop from network latency, cloud authentication overhead, and variable API response times, macroeconomic analysis is completely segregated into an asynchronous **Cold Plane Worker** (`core/cold_plane_worker.py`).

Trader IPC signals (`HALT`, `RESUME`, `LIQUIDATE_ALL`) operate via `state/ipc_control.json`, strictly separated from macroeconomic prior payloads persisted to `state/macro_prior.json`.

```
┌────────────────────────────────────────────────────────────────────────┐
│                        COLD PLANE (Asynchronous)                       │
│                                                                        │
│   Vertex AI / Gemini Models:                                           │
│     - 09:15 EDT Pre-Market Macro Prior Gate: gemini-4-argon            │
│     - Intraday Periodic Cadence (30m):       gemini-3.8-flash          │
│     - 15:55 EDT EOD Macro Synthesis:         gemini-4-argon            │
│                                                                        │
│                      │ Writes atomic temp file                         │
│                      ▼                                                 │
│       state/macro_prior.json.tmp (fsync)                              │
│                      │                                                 │
│                      ▼ os.replace                                      │
│           state/macro_prior.json ◄───────────────────────────┐        │
└──────────────────────────────────────────────────────────────┼─────────┘
                                                               │ Read (Fault-Tolerant)
┌──────────────────────────────────────────────────────────────┼─────────┐
│                        HOT LOOP (Real-Time)                  │         │
│                                                              │         │
│   strategies/regime_governor.py (AsymmetricHMMGovernor)      │         │
│     - Periodic check: ingest_cold_plane_signal() ────────────┘         │
│     - Max age staleness check: age <= 3,600s                           │
│     - If circuit_breaker_override == True:                             │
│         * Enforces Macro Shock [0.0, 0.0, 1.0]                         │
│         * Zeroes Satellite Target Weight: W_sat*(t) = 0.00             │
│         * Activates tail_risk_override_active                          │
│     - If circuit_breaker_override == False:                            │
│         * Bayesian Prior Blending: (1 - w)*pi + w*prior_probs          │
│     - Telemetry audited atomically to state/trading_journal.db         │
└────────────────────────────────────────────────────────────────────────┘
```

### 2. Schema Contract (`ControlSignalPayload`)
The signal file `state/macro_prior.json` adheres to a strict validated contract (`ControlSignalPayload`):

| Field | Type | Invariant / Validation Rule |
| :--- | :--- | :--- |
| `timestamp_utc` | `string` | Non-empty ISO-8601 UTC timestamp string (e.g., `"2026-10-26T13:15:00Z"`). |
| `model_version` | `string` | Must be one of `"gemini-4-argon"`, `"gemini-3.8-flash"`. |
| `dominant_prior` | `string` | Must be one of `"Goldilocks"`, `"Uncertainty"`, `"Macro Shock"`. |
| `prior_probabilities` | `dict` | Exact keys `{"goldilocks", "uncertainty", "macro_shock"}`; values $\in [0.0, 1.0]$, $\sum = 1.0 \pm 10^{-3}$. |
| `credit_spread_z_score` | `float` | Hybrid credit spread anomaly z-score. |
| `nlp_sentiment_score` | `float` | Macroeconomic NLP sentiment score, clamped to $[-1.0, 1.0]$. |
| `satellite_target_weight` | `float` | Target satellite allocation weight, clamped to $[0.0, 0.35]$. |
| `circuit_breaker_override` | `bool` | Deterministic override triggering emergency de-risking when True. |

### 3. Model Tier Routing & Atomic Ingestion Semantics
- **Model Tier Router (`select_cold_plane_model`):**
  - **`gemini-4-argon`:** Selected during the critical 09:10:00–09:25:00 EDT pre-market prior gate and $\ge$ 15:55:00 EDT EOD synthesis for deep analytical reasoning.
  - **`gemini-3.8-flash`:** Selected for all intraday periodic evaluations (30-minute cadence between 09:30:00 and 15:50:00 EDT) for ultra-low latency and computational efficiency.
- **Atomic File-Drop Writer (`write_control_signal_atomic`):** Serializes to sibling `.tmp`, invokes `f.flush()` and `os.fsync()`, and atomically moves via `os.replace()`. Guarantees zero partially written or torn reads.
- **Fault-Tolerant Hot-Loop Ingestion (`read_control_signal` & `AsymmetricHMMGovernor.ingest_cold_plane_signal`):**
  - Safely catches `(OSError, json.JSONDecodeError, ValueError, KeyError)` without interrupting trading loops.
  - Rejects stale signals older than `max_age_seconds` ($3,600\text{s}$).
  - Blends fresh priors into the active HMM state vector using blend weight $w = 0.35$, or forces state to `[0.0, 0.0, 1.0]` (`Macro Shock`) and $W_{\text{sat}}^*(t) = 0.00$ on `circuit_breaker_override == True`.

---

## Monday Pre-Market Invariant Verification (Task #124)

Prior to cash equity market open at 09:30:00 EDT, `scripts/tools/check_preflight_invariants.py` (`PreflightVerifier`) executes between 09:00:00 and 09:25:00 EDT to enforce 5 non-negotiable operational invariants:

```
09:00:00 EDT - Monday Pre-Market Invariant Verification Window Opens
 │
 ├── [1] T+1 Cash Ledger Invariants (SEC Rule 15c6-1)
 │     ├── Settled cash > $0.00
 │     ├── Unsettled cash == $0.00 (Overnight sweep matured and cleared all prior-day proceeds)
 │     ├── Reserved cash == $0.00 (Zero hanging overnight limit buy orders)
 │     └── Three-bucket conservation: Settled + Unsettled + Reserved == Total Cash
 │
 ├── [2] Zero Stranded 3x Satellites Invariant
 │     └── Holdings for SOXL, TQQQ, TNA strictly == 0 shares
 │
 ├── [3] OAuth Pre-Market Refresh Gate (09:15:00 EDT Gate)
 │     ├── Token manager not AUTH_LOCKED
 │     ├── Pre-market refresh gate executed
 │     └── Session Token Remaining TTL >= 1,800.0s (30 minutes)
 │
 ├── [4] API Rate Limiter Headroom
 │     ├── Burst capacity == 120.0 RPM (2.0 req/sec headroom)
 │     ├── Daily ceiling == 3,500 requests
 │     └── Start-of-day consumed calls < 100
 │
 └── [5] Filesystem & Database Persistence
       ├── state/ and logs/ exist and are writable
       └── state/trading_journal.db opens cleanly in SQLite WAL mode
 │
09:25:00 EDT - Verdict & Atomic Preflight Report Generation
 └── Persisted to logs/preflight_check_latest.json
 └── Exit code 0: Authorize Cash Market Open
 └── Exit code 1: Emergency Operator Halt
```

### Preflight Execution Commands
- **Dry-run simulation (CI / Staging / Development):**
  ```bash
  python scripts/tools/check_preflight_invariants.py --dry-run --nlv 4400.0
  ```
- **Production live execution (09:15:00 EDT on `schwab-trader` VM):**
  ```bash
  python scripts/tools/check_preflight_invariants.py --nlv 4400.0
  ```

---

## 14. Operational Remediation & Live Broker Cutover (#129–#136)

### 1. Diagnostic & Cold Plane Wiring (#129–#131)
- **Isolated IPC Namespaces:**
  - Resolved namespace collisions by isolating command/kill-switch controls (`state/ipc_control.json` / `schwab_state/ipc_control.json`) from macroeconomic quantitative priors (`/home/nicho/schwab_state/macro_prior.json`).
  - Signal exchange follows strict serialization guarantees: `TraderIPCServer` manages interactive runtime commands without tearing or overwriting Cold Plane synthesis signals.
- **Automated Cold Plane Scheduling:**
  - Configured and activated `schwab-coldplane.timer` on systemd:
    - `09:15:00 EDT`: Mandatory pre-market prior synthesis gate (`gemini-4-argon`) seeding initial regime priors ahead of cash open.
    - `09:30:00 – 15:50:00 EDT`: Intraday 30-minute evaluation cadence (`gemini-3.8-flash`) updating credit spread $Z$-scores and NLP sentiment vectors.
- **Dynamic Session Telemetry Log Rotation:**
  - Standardized local session telemetry file paths to `logs/session_telemetry_YYMMDD.jsonl` using dynamic `America/New_York` session date calculation, preventing stale overnight file handles or collisions across trading days.

### 2. EOD Satellite Liquidation Parity (#132)
- **Deterministic 15:50:00 EDT Flatten Gate:**
  - Hardcoded `EOD_MANDATORY_LIQUIDATION` parity within `core/shadow.py` and `core/engine.py`.
  - When Eastern Time reaches $\ge \text{15:50:00 EDT}$, all 3x leveraged satellite holdings (`SOXL`, `TQQQ`, `TNA`) are 100% liquidated to settled cash via limit and market sweeps.
  - Zero 3x satellite inventory is carried overnight, enforcing strict compliance with SEC Rule 15c6-1 cash preservation and LETF volatility drag constraints.
  - Legacy Core sleeve is fully decommissioned; zero equity positions (0.00%) are held overnight, preserving 100% settled cash under SEC Rule 15c6-1 T+1 settlement.

### 3. Live Entrypoint Binding & Invariant 6 (#133–#136)
- **Strategy Config Direct Binding:**
  - Bound `--headless-trader` entrypoint in `main.py` directly to `config/strategy_config.json` (`ave.mode`).
  - When `ave.mode == "live"`: initializes `build_live()`, activates live broker execution with `OrderManager` and `SchwabOrderClient(live_trading=True)`, and emits `[CRITICAL] RUNTIME EXECUTION MODE: LIVE BROKER ROUTING ARMED`.
  - When `ave.mode == "shadow"`: instantiates `build_shadow()` and logs `[INFO] RUNTIME EXECUTION MODE: SHADOW SIMULATION`.
- **Explicit `--live` Flag Enforcement:**
  - Added dedicated `--live` CLI argument ensuring live broker order routing is forced and rejecting shadow fallback if config specifies shadow mode (fails closed with exit code 2).
  - Updated systemd unit definition (`systemd/schwab-trader.service` and `scripts/systemd/schwab-trader.service`) to `ExecStart=/home/nicho/schwab_engine/.venv/bin/python main.py --headless-trader --live`.
- **Invariant 6 (`verify_execution_mode_alignment`):**
  - Incorporated Invariant 6 into `scripts/tools/check_preflight_invariants.py`.
  - Inspects `config/strategy_config.json` (`ave.mode`) alongside active runtime `/proc/<pid>/cmdline` and systemd unit definitions.
  - Asserts that when `ave.mode == "live"`, live broker routing is strictly active without shadow bypass, failing preflight gates with exit code 1 on any configuration or process discrepancy.

### 4. Deterministic Live Session Execution Lifecycle (Account `***4015` — $4,400.90 Baseline)

| Timestamp (EDT) | Execution Lifecycle Stage | Operational Actions & Verification |
| :--- | :--- | :--- |
| **09:15:00 EDT** | **Cold Plane Macro Prior Gate** | `schwab-coldplane.timer` triggers `core.cold_plane_worker` (`gemini-4-argon`), evaluating FRED yields, credit spreads, and pre-market financial sentiment. Persists normalized posterior regime probabilities and hybrid credit spread $Z$-score to `macro_prior.json`. |
| **09:25:00 EDT** | **Pre-Market Invariant & Token Gate** | `scripts/tools/check_preflight_invariants.py` executes all 6 invariants against $4,400.90 NLV. Enforces OAuth access token refresh (TTL $\ge 1,800\text{s}$), verifies zero stranded overnight satellites, confirms rate limiter headroom ($120\text{ RPM}$ burst / $3,500$ daily), and validates execution mode alignment. |
| **09:30:00 EDT** | **Pure Tactical Intraday Open** | Market opens with 100% settled cash baseline ($4,400.90 baseline). Headless trading engine evaluates pure tactical 3x satellite entries (`SOXL`, `TQQQ`, `TNA`) up to macro regime capacity (Goldilocks: up to 75%; Uncertainty: 35%; Shock: 0%) with 0% Core allocation ($0\text{ GFV}$). |
| **09:30:30 – 15:49:30 EDT** | **Dynamic Hot-Loop Rebalancing** | 30-second rebalancing loop evaluates portfolio weights against Leland No-Trade Zone bands governed by macro regime capacity. Blends Cold Plane priors every 30 minutes ($w = 0.35$) while continuously monitoring high-frequency intraday credit proxy (`HYG`/`IEF` ratio). |
| **15:50:00 EDT** | **Mandatory EOD Tactical Liquidation** | Terminal flush gate fires. 100% of open 3x satellite positions (`SOXL`, `TQQQ`, `TNA`) are aggressively flattened to settled cash via market/marketable limit sweeps. Guarantees 0.00% overnight equity exposure and 100% settled cash under SEC Rule 15c6-1 T+1 settlement. |
