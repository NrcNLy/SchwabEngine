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
- [ ] **Step 6:** Run pre-flight diagnostic suite in the remote environment:
  ```bash
  python scripts/preflight.py
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
