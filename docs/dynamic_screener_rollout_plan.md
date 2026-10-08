# Phased Intraday Implementation & Rollout Plan: Dynamic ETF Screener

**Document Status:** Production Operational Blueprint  
**Reference Specification:** [`docs/07-dynamic-screener-architecture.md`](file:///c:/Projects/schwab_engine/docs/07-dynamic-screener-architecture.md)  
**Target Environment:** Google Compute Engine VM `schwab-trader` (`us-central1-a`, Project `gen-lang-client-0334702303`)  
**Target Container:** `schwab_engine_container` (Live Trading Engine, Cash Account `...4015`)  
**Initial System Phase:** `MID_MORNING` (10:30–11:30 EDT)  
**Execution Date:** 2026-10-08  

---

## 1. Executive Strategy & Operational Guardrails

### 1.1 Objective & Context
The static 3-ticker watchlist (`SOXL`, `TQQQ`, `TNA`) suffers from documented structural limitations: intraday chop lockouts, lack of cross-sectional asset substitution, and missed afternoon volume breakouts. The dynamic screener architecture replaces this static binding with an automated selection engine across an expanded 24-ticker candidate universe while strictly adhering to:
1. **SEC Regulation T § 220.8 & FINRA T+1 Settlement**: Strict cash account compliance with zero Good Faith Violations (GFVs).
2. **Schwab API Quotas**: Bounded by a 20-token burst token bucket (100 RPM ceiling) and a hard 3,500 daily request ceiling.
3. **Zero In-Flight Disruption**: No process restarts or remote modifications while the morning execution window (`MID_MORNING`) is live.
4. **State Immutability**: All signal clamping and state mutations must use immutable dataclass patterns (`dataclasses.replace`) to prevent `FrozenInstanceError` regressions.

### 1.2 Chronological Operational Timeline

```
   10:35 EDT                    11:30 EDT               11:45 EDT               13:30 EDT               14:00 EDT               15:35 EDT    15:50 EDT   16:00 EDT
       │                            │                       │                       │                       │                       │            │           │
       ▼                            ▼                       ▼                       ▼                       ▼                       ▼            ▼           ▼
┌──────────────┐             ┌──────────────┐        ┌───────────────────────────────────────┐       ┌──────────────────────┐┌──────────────┐┌───────────┐┌───────────┐
│   PHASE 1    │             │   PHASE 2    │        │                PHASE 3                │       │       PHASE 4        ││   PRE_CLOSE  ││ MANDATORY ││  POST    │
│ Local Dev &  │ ──────────► │ Remote State │ ─────► │ Staged Deployment, Midday Screening,  │ ────► │ Power Hour Execution ││ Entries Lock ││  FLATTEN  ││ REFLECT   │
│ Unit Testing │             │ Safety Audit │        │ & Baseline Historical Cache Warmup    │       │ 0.5x Fractional Size ││ 0.0x Sizing  ││ Liquidation││ Audit &   │
│ (Isolated)   │             │ (No Actions) │        │ (Container Reload in Freeze Window)   │       │ Empirical Hurdles    ││ Unwind Prep  ││ 100% Cash ││ Persist   │
└──────────────┘             └──────────────┘        └───────────────────────────────────────┘       └──────────────────────┘└──────────────┘└───────────┘└───────────┘
 [MID_MORNING]               [MIDDAY_FREEZE]                      [MIDDAY_FREEZE]                          [POWER_HOUR]             [PRE_CLOSE]   [FLATTEN]   [OFFLINE]
```

---

## 2. Phase 1: Local Development & Isolated Testing (10:35 – 11:30 EDT / MID_MORNING)

**Operational Mandate:** All development is isolated strictly to the local machine on a dedicated git feature branch. Zero commands or file transfers may touch the remote GCP VM while live morning trading is active.

### 2.1 Branch Isolation
Initialize and switch to the designated implementation branch:

```powershell
# Execute in c:\Projects\schwab_engine
git checkout -b feat/dynamic-screener-universe
git status
```

### 2.2 Scaffolding & Module Implementation

#### A. Implement Dynamic Scanner (`core/dynamic_scanner.py`)
Implement the non-blocking scanner module incorporating:
- **Asynchronous Token Bucket Rate Limiter**: Capacity = 20 tokens, refill rate = 1.667 tokens/sec (100 RPM ceiling), enforcing a safe margin under Schwab's 120 RPM ceiling.
- **SQLite WAL Historical Cache**: Managed at `data/historical_candles.db` with PRAGMAs `journal_mode=WAL` and `synchronous=NORMAL`.
- **Batch Quote Ingestion**: Single request to `GET /marketdata/v1/quotes?symbols=...` across the 24-ticker candidate universe (consumes exactly 1 token).
- **Composite Selection Scoring Function**:
  $$S_{\text{comp}} = 0.40 G_o + 0.45 RVOL_{\$} - 0.15 P_{\text{spread}}$$
  * Overnight Gap: $G_o = \frac{|P_{\text{last}} - P_{\text{prev\_close}}|}{P_{\text{prev\_close}}}$
  * Pre-Market Dollar RVOL (Normalized against ADV):
    $$RVOL_{\$, i} = \frac{V_{\text{pre}, i} \cdot P_{\text{last}, i}}{\text{ADV}_{20, i} \cdot P_{\text{prev\_close}, i}}$$
  * Spread Penalty: $P_{\text{spread}} = \frac{\text{Ask} - \text{Bid}}{P_{\text{last}}}$
  * Strict cross-sectional min-max normalization to $[0, 1]$ across all candidate metrics prior to coefficient weighting.
- **Timezone-Aware Minute-of-Day (MOD) Binning**: Enforce `ZoneInfo("America/New_York")` on historical candle timestamps (`0 <= MOD < 1440`) to eliminate UTC offset bugs inside Linux/Docker environments.

#### B. Implement Universe Manager (`core/universe_manager.py`)
Implement the dynamic universe coordinator:
- Manage the active Top 3 assets in runtime memory.
- Provide rolling 390-bar minute deque ring buffers (`collections.deque(maxlen=390)`).
- Integrate with `core/session.py` to evaluate wall-clock temporal phase transitions.
- Implement strictly immutable signal sizing clamping:
  $$Q_{\text{Kelly}} = \left\lfloor \frac{50.00}{|P_{\text{entry}} - P_{\text{stop}}|} \right\rfloor$$
  $$Q_{\text{Cap}} = \left\lfloor \frac{0.33 \times \text{NLV}}{P_{\text{entry}}} \right\rfloor$$
  $$Q_{\text{Cash}} = \left\lfloor \frac{\text{SettledCash} - 10.00}{P_{\text{entry}}} \right\rfloor$$
  $$Q_{\text{Final}} = \max\left(0, \min(Q_{\text{Kelly}}, Q_{\text{Cap}}, Q_{\text{Cash}})\right)$$
  Re-instantiate via `dataclasses.replace(signal, quantity=clamped_qty)`.

#### C. Integrate HotSwap Subscriptions into Streamer (`data/streamer.py`)
Extend [`data/streamer.py`](file:///c:/Projects/schwab_engine/data/streamer.py) with dynamic hot-swap capability:
- Retain the active WebSocket connection reference (`self._ws = ws`).
- Implement `update_subscriptions(symbols: List[str], incremental: bool = False)`:
  * If `incremental=False`: Transmit `LEVELONE_EQUITIES` command `SUBS` to overwrite the subscription ledger with the new Top 3 assets.
  * If `incremental=True`: Transmit command `ADD` to append symbols dynamically.
  * Dispatch via `asyncio.run_coroutine_threadsafe(..., self._loop)` to allow non-blocking invocation from any engine thread.

#### D. Implement Corrected Backtest Replay Script (`scripts/replay_screener_backtest.py`)
Create the offline validation harness:
- Fix prior-day close extraction: Query true previous-session close rather than synthetic approximations (`open * 0.99`).
- Fix ORB slicing: Isolate the Opening Range strictly to `09:30:00`–`09:44:59` (15 closed bars), and begin breakout trading evaluation from `09:45:00` to `15:50:00`.
- Integrate Yang-Zhang volatility trailing stop simulation clamped between 10th and 90th percentile ATR boundaries.

### 2.3 Local Verification & Compilation
Execute local test runs to confirm zero syntax errors or regressions:

```powershell
# 1. Run type/syntax compilation check
python -m py_compile core/dynamic_scanner.py core/universe_manager.py data/streamer.py scripts/replay_screener_backtest.py

# 2. Execute local test suite
$env:PYTHONPATH="."
pytest tests/test_temporal_stops.py tests/test_order_manager.py tests/test_ledger.py tests/test_ledger_gfv.py
```

*Success Criteria:* All tests pass, zero unhandled exceptions, and no code changes touch the remote environment.

---

## 3. Phase 2: Remote Pre-Deployment Audit (11:30 EDT / Start of MIDDAY_FREEZE)

**Operational Trigger:** Wall clock reaches **11:30:00 EDT**.  
At 11:30 EDT, [`core/session.py:get_session_phase()`](file:///c:/Projects/schwab_engine/core/session.py#L55) transitions from `MID_MORNING` to `MIDDAY_FREEZE`. The engine enters a mandatory entry lockout (`is_entry_permitted` returns `(False, 0.0, ...)`), locking out all new trade initiations.

### 3.1 Non-Destructive Position & Telemetry Audit
Before touching any remote files or restarting the container, execute an audit over SSH to inspect the live engine's state:

```powershell
$env:PATH += ";C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin"

# 1. Verify container is healthy and inspect active session phase
gcloud.cmd compute ssh schwab-trader --zone=us-central1-a --project=gen-lang-client-0334702303 --command="sudo docker logs --tail 30 schwab_engine_container"

# 2. Inspect active inventory and cash ledger
gcloud.cmd compute ssh schwab-trader --zone=us-central1-a --project=gen-lang-client-0334702303 --command="curl -s http://127.0.0.1:8080/api/ledger?env=active"

# 3. Inspect open positions
gcloud.cmd compute ssh schwab-trader --zone=us-central1-a --project=gen-lang-client-0334702303 --command="curl -s http://127.0.0.1:8080/api/positions/all?env=active"
```

### 3.2 Pre-Deployment Gating Decision Tree

```
                       [11:30 EDT AUDIT CHECK]
                                  │
                   Does engine hold open positions?
                                  │
                 ┌────────────────┴────────────────┐
                 ▼                                 ▼
               [YES]                             [NO]
        Position is ACTIVE                 Inventory is 100% CASH
                 │                                 │
                 ▼                                 ▼
        ┌──────────────────┐             ┌──────────────────┐
        │  GATE: HOLDING   │             │   GATE: CLEAR    │
        │ DO NOT RESTART.  │             │ Proceed to       │
        │ Maintain stops.  │             │ Phase 3 Staged   │
        │ Wait for exit.   │             │ Deployment.      │
        └──────────────────┘             └──────────────────┘
```

1. **Scenario A (Active Position Exists)**:
   - **Gating Action:** **ABORT CONTAINER RESTART**.
   - **Rationale:** Restarting the container while a position is open temporarily drops the Tier-1 memory-managed Yang-Zhang trailing stop and risks desynchronizing with broker-side Tier-2 catastrophe orders.
   - **Protocol:** Monitor the position until it naturally reaches its target or trailing stop. If the position remains open past 13:30 EDT, defer deployment to post-market (16:05 EDT).
2. **Scenario B (Zero Active Positions — 100% Cash)**:
   - **Gating Action:** **GATE CLEAR FOR DEPLOYMENT**.
   - All funds reside securely in Bucket 1 (`settled_cash`). Proceed immediately to Phase 3.

---

## 4. Phase 3: Staged Deployment & Midday Dynamic Scanner Calibration (11:45 – 13:30 EDT)

**Operational Window:** 11:45 to 13:30 EDT (Deep inside `MIDDAY_FREEZE`).  
Because new trade entries are physically barred until 14:00 EDT, this 105-minute window provides an isolated, zero-risk operational zone to transfer code, restart the engine, execute an on-demand midday market scan, and preload historical minute-of-day volumes.

### 4.1 Remote File Transfer & Deployment
Upload the validated modules to the remote VM and inject them directly into the live Docker container filesystem:

```powershell
$env:PATH += ";C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin"
$VM = "schwab-trader"
$ZONE = "us-central1-a"
$PROJ = "gen-lang-client-0334702303"

# 1. SCP modified modules to host repository
gcloud.cmd compute scp --quiet core/dynamic_scanner.py ${VM}:schwab_engine/core/dynamic_scanner.py --zone=$ZONE --project=$PROJ
gcloud.cmd compute scp --quiet core/universe_manager.py ${VM}:schwab_engine/core/universe_manager.py --zone=$ZONE --project=$PROJ
gcloud.cmd compute scp --quiet data/streamer.py ${VM}:schwab_engine/data/streamer.py --zone=$ZONE --project=$PROJ
gcloud.cmd compute scp --quiet execution/strategies.py ${VM}:schwab_engine/execution/strategies.py --zone=$ZONE --project=$PROJ

# 2. Copy files into container volume mount
gcloud.cmd compute ssh $VM --zone=$ZONE --project=$PROJ --command="
sudo docker cp ~/schwab_engine/core/dynamic_scanner.py schwab_engine_container:/app/core/dynamic_scanner.py
sudo docker cp ~/schwab_engine/core/universe_manager.py schwab_engine_container:/app/core/universe_manager.py
sudo docker cp ~/schwab_engine/data/streamer.py schwab_engine_container:/app/data/streamer.py
sudo docker cp ~/schwab_engine/execution/strategies.py schwab_engine_container:/app/execution/strategies.py
"

# 3. Perform clean container restart
gcloud.cmd compute ssh $VM --zone=$ZONE --project=$PROJ --command="sudo docker restart schwab_engine_container"
```

### 4.2 Clean Boot Verification
Verify that the restarted container initializes cleanly without exceptions:

```powershell
gcloud.cmd compute ssh $VM --zone=$ZONE --project=$PROJ --command="
sleep 5
sudo docker logs --tail 40 schwab_engine_container
"
```

*Verification Checkpoints:*
- `SchwabAuthManager: access token valid / refreshed.`
- `OrderManager: firewall PASSED — account=...4015.`
- `TokenBucket initialised: capacity=60.0 tokens.`
- `Uvicorn running on http://0.0.0.0:8080.`
- Zero Python tracebacks or `FrozenInstanceError` logs.

### 4.3 On-Demand Midday Universe Screening & Calibration Pass
Rather than waiting for the next morning's pre-market run, trigger an on-demand screening pass at ~12:00 EDT across the expanded 24-ticker universe to establish the active Top 3 assets for the upcoming Power Hour session.

Create and execute a calibration script [`scripts/calibrate_midday_universe.py`](file:///c:/Projects/schwab_engine/scripts/calibrate_midday_universe.py):

```python
# scripts/calibrate_midday_universe.py (Executed inside container)
import asyncio
import os
import yaml
from pathlib import Path
from core.auth import SchwabAuthManager, SecurityVault
from core.dynamic_scanner import DynamicScanner
from data.rest_client import SchwabRestClient

EXPANDED_UNIVERSE = {
    "SOXL": {"adv_20d_shares": 59_000_000, "median_spread_cents": 1.5},
    "SOXS": {"adv_20d_shares": 42_000_000, "median_spread_cents": 1.5},
    "TQQQ": {"adv_20d_shares": 50_000_000, "median_spread_cents": 1.0},
    "SQQQ": {"adv_20d_shares": 65_000_000, "median_spread_cents": 1.0},
    "TNA":  {"adv_20d_shares":  4_700_000, "median_spread_cents": 2.0},
    "TZA":  {"adv_20d_shares":  2_800_000, "median_spread_cents": 2.0},
    "UPRO": {"adv_20d_shares":  1_500_000, "median_spread_cents": 1.5},
    "SPXU": {"adv_20d_shares":  1_800_000, "median_spread_cents": 1.5},
    "FNGU": {"adv_20d_shares":  2_200_000, "median_spread_cents": 2.0},
    "NVDL": {"adv_20d_shares": 14_000_000, "median_spread_cents": 1.5},
    "TECL": {"adv_20d_shares":  1_200_000, "median_spread_cents": 2.5},
    "USD":  {"adv_20d_shares":    600_000, "median_spread_cents": 2.5},
    "FAS":  {"adv_20d_shares":  1_000_000, "median_spread_cents": 3.0},
    "DPST": {"adv_20d_shares":    850_000, "median_spread_cents": 4.0},
    "BOIL": {"adv_20d_shares":  3_500_000, "median_spread_cents": 1.5},
    "KOLD": {"adv_20d_shares":  2_100_000, "median_spread_cents": 2.0},
    "UCO":  {"adv_20d_shares":  2_900_000, "median_spread_cents": 1.5},
    "SCO":  {"adv_20d_shares":  1_400_000, "median_spread_cents": 2.0},
    "NUGT": {"adv_20d_shares":  1_900_000, "median_spread_cents": 2.0},
    "LABU": {"adv_20d_shares":  1_800_000, "median_spread_cents": 2.5},
}

async def run_midday_calibration():
    with open("config/config.yaml") as f:
        cfg = yaml.safe_load(f)
    vault = SecurityVault(os.getenv("VAULT_PASSPHRASE"), 600000, Path("schwab_tokens_vault.json"))
    auth = SchwabAuthManager(os.getenv("SCHWAB_CLIENT_ID"), os.getenv("SCHWAB_CLIENT_SECRET"), vault, cfg)
    auth.load_tokens()
    token = auth.get_access_token()

    scanner = DynamicScanner(token, db_path="data/historical_candles.db")
    quotes = await scanner.fetch_batch_quotes(list(EXPANDED_UNIVERSE.keys()))
    scores = scanner.calculate_selection_scores(quotes, EXPANDED_UNIVERSE)

    print("\n--- MIDDAY UNIVERSE SELECTION SCORES ---")
    for score, sym in scores[:10]:
        print(f"  Rank: {sym} | Score: {score:.4f}")

    top_3 = [sym for _, sym in scores[:3]]
    print(f"\nTop 3 Active Assets for Afternoon Session: {top_3}")

    print("\nWarming up 10-day 1m history and baseline volumes...")
    baseline_vols = await scanner.fetch_target_history(top_3)
    for sym, vols in baseline_vols.items():
        print(f"  {sym}: Cached {len(vols)} minute-of-day baseline volume points.")

if __name__ == "__main__":
    asyncio.run(run_midday_calibration())
```

Execute on the VM:

```powershell
gcloud.cmd compute scp --quiet scripts/calibrate_midday_universe.py ${VM}:schwab_engine/calibrate_midday_universe.py --zone=$ZONE --project=$PROJ
gcloud.cmd compute ssh $VM --zone=$ZONE --project=$PROJ --command="
sudo docker cp ~/schwab_engine/calibrate_midday_universe.py schwab_engine_container:/app/calibrate_midday_universe.py
sudo docker exec schwab_engine_container python /app/calibrate_midday_universe.py
"
```

*Verification Checkpoints:*
- 20 candidate quotes ingested in 1 API call.
- Top 3 assets selected based on real-time gap, dollar RVOL, and spread penalty.
- 10-day 1m candles cached to SQLite database `data/historical_candles.db` (consumes exactly 3 REST calls).
- Total API calls consumed: **4 requests** (remaining daily quota $> 3,100$).

---

## 5. Phase 4: Power Hour Execution Authorization (14:00 – 15:35 EDT)

**Operational Trigger:** Wall clock reaches **14:00:00 EDT**.  
[`core/session.py:get_session_phase()`](file:///c:/Projects/schwab_engine/core/session.py#L55) transitions from `MIDDAY_FREEZE` to `POWER_HOUR`.

### 5.1 Temporal Gating Rules & Sizing Clamp
1. **Fractional Sizing**: `is_entry_permitted(TradingPhase.POWER_HOUR)` authorizes entries at **50% fractional sizing** (`power_hour_sizing_multiplier: 0.50`).
2. **Capital Invariants**:
   - Single-ticker exposure cap: $\text{Notional} \le 0.33 \times \text{NLV}$ (max ~\$1,238.73 on \$3,753.75 NLV).
   - Cash buffer floor: Orders must leave $\ge \$10.00$ in Bucket 1 (`settled_cash`).
   - Per-trade risk cap: Dollar risk bounded at $\le \$50.00$.

### 5.2 Mandatory Empirical Breakout Hurdles
To filter out false breakouts during afternoon trading, entries on dynamically selected assets must satisfy the empirical hurdle criteria established in [`research_context.md`](file:///c:/Projects/schwab_engine/research_context.md):

```
                   POWER HOUR ENTRY FILTER PIPELINE (14:00 - 15:35 EDT)
                                             │
                                             ▼
                 ┌────────────────────────────────────────────────────────┐
                 │ 1. Regime Confirmation: CI < 38.2 & OLS Slope > 0°    │
                 └───────────────────────────┬────────────────────────────┘
                                             │
                                             ▼
                 ┌────────────────────────────────────────────────────────┐
                 │ 2. Volume Hurdle: Intraday RVOL >= 1.40x vs Midday     │
                 └───────────────────────────┬────────────────────────────┘
                                             │
                                             ▼
                 ┌────────────────────────────────────────────────────────┐
                 │ 3. Displacement Hurdle: Bar Return >= Asset Mean %     │
                 │    (SOXL: >0.58% | TQQQ: >0.28% | TNA: >0.26%)         │
                 └───────────────────────────┬────────────────────────────┘
                                             │
                                             ▼
                 ┌────────────────────────────────────────────────────────┐
                 │ 4. Dispersion Hurdle: 15m TR >= Asset Median TR        │
                 │    (SOXL: >$1.20 | TQQQ: >$0.34 | TNA: >$0.27)         │
                 └───────────────────────────┬────────────────────────────┘
                                             │
                                             ▼
                                  [AUTHORIZE 0.5x ENTRY]
```

### 5.3 Live Telemetry Monitoring During Power Hour
Monitor the engine in real time from the command line:

```powershell
gcloud.cmd compute ssh $VM --zone=$ZONE --project=$PROJ --command="
sudo docker logs -f --tail 20 schwab_engine_container
"
```

*Expected Log Signatures:*
- `[INFO] session - State transition from MIDDAY_FREEZE to POWER_HOUR. Entry permitted: True.`
- `[INFO] execution.strategies - REGIME CHANGE | <SYMBOL> | MEAN_REVERSION → TREND_EXPANSION`
- If an entry triggers:
  - `[ROUTED] LONG <QTY> <SYMBOL> @ ~$XX.XX`
  - Zero `FrozenInstanceError` or `cannot assign to field 'quantity'` tracebacks.

---

## 6. Phase 5: Pre-Close Liquidation & Telemetry Audit (15:50 – 16:00 EDT)

**Operational Window:** 15:35 to 16:00 EDT (EOD Unwind & Settlement Quarantine).

### 6.1 Liquidation Sequence Execution

```
   15:35 EDT                         15:50 EDT                         15:55 EDT                         16:00 EDT
       │                                 │                                 │                                 │
       ▼                                 ▼                                 ▼                                 ▼
┌──────────────┐                  ┌──────────────┐                  ┌──────────────┐                  ┌──────────────┐
│  PRE_CLOSE   │                  │  MANDATORY   │                  │     FLAT     │                  │  POST-CLOSE  │
│ Phase Active │                  │   FLATTEN    │                  │   DEADLINE   │                  │  PERSISTENCE │
├──────────────┤                  ├──────────────┤                  ├──────────────┤                  ├──────────────┤
│ Entries lock │ ───────────────► │ Active market│ ───────────────► │ Hard audit:  │ ───────────────► │ SQLite WAL   │
│ (0.0x size). │                  │ sell orders  │                  │ cancel all   │                  │ state sync.  │
│ Stops active.│                  │ dispatched.  │                  │ working ord. │                  │ 100% cash.   │
└──────────────┘                  └──────────────┘                  └──────────────┘                  └──────────────┘
```

1. **15:35 EDT (`PRE_CLOSE`)**:
   - Engine phase transitions to `PRE_CLOSE`.
   - New entries are barred (`is_entry_permitted` returns False).
   - Trailing stops continue managing existing open positions.
2. **15:50 EDT (`MANDATORY_FLATTEN`)**:
   - [`core/engine.py:flatten_all()`](file:///c:/Projects/schwab_engine/core/engine.py#L642) triggers automatically:
     * Cancels all engine open working orders.
     * Executes market sell orders for all open intraday positions.
     * Converts all proceeds back into Bucket 1 (`settled_cash`).
   - **GFV Quarantine Check**: If any position was tagged as funded with unsettled cash, `can_sell_position()` enforces the overnight hold override, skipping liquidation to prevent a Good Faith Violation.
3. **15:55 EDT (`FLAT_DEADLINE`)**:
   - Automated sweep verifies active positions count == 0.
   - Emits high-priority alert confirming 100% cash state.

### 6.2 Post-Market Telemetry & Integrity Audit
Execute the end-of-day audit script to verify database state, order executions, and quota usage:

```powershell
gcloud.cmd compute ssh $VM --zone=$ZONE --project=$PROJ --command="
# 1. Query SQLite WAL telemetry events for today
sudo docker exec schwab_engine_container python -c '
import sqlite3
conn = sqlite3.connect(\"/app/data/schwab_telemetry.db\")
rows = conn.execute(\"SELECT timestamp, aggregate_id, event_type FROM event_log WHERE timestamp >= date(\"now\") ORDER BY id DESC LIMIT 20\").fetchall()
print(\"=== RECENT TELEMETRY EVENTS ===\")
for r in rows:
    print(r)
'

# 2. Check daily API quota consumption
sudo docker exec schwab_engine_container cat /app/schwab_state/daily_quota.json

# 3. Verify ledger is flat and cash is reconciled
curl -s http://127.0.0.1:8080/api/ledger?env=active
"
```

---

## 7. Rollback Safeguards & Disaster Recovery Plan

If an unexpected condition arises during deployment or live execution, execute the corresponding disaster recovery procedure immediately.

### 7.1 Rollback Trigger Matrix

| Severity | Condition / Trigger | Immediate System Action | Recovery CLI Command |
| :--- | :--- | :--- | :--- |
| **P0 (Critical)** | Continuous tick handling crashes / tracebacks | Halt engine entries immediately via Kill Switch | `curl -X POST http://127.0.0.1:8080/api/kill` |
| **P0 (Critical)** | WebSocket reconnection storm (>5 disconnects/min) | Revert to static 3-ticker baseline | `git checkout main && ./deploy.ps1 -Live` |
| **P1 (High)** | API Quota Warning ($\ge 3,200$ calls/day) | Background sync throttling engages automatically (60s) | Throttle verified via `daily_quota.json` |
| **P1 (High)** | GFV Blocked Sell Error | Position held overnight; do not force sell | Normal system invariant; let settle to T+1 |

### 7.2 Emergency Revert Procedure (Back to Static Baseline)
To revert the remote environment cleanly back to the last known-good static `main` branch deployment:

```powershell
# 1. Engage software kill switch
gcloud.cmd compute ssh $VM --zone=$ZONE --project=$PROJ --command="curl -X POST http://127.0.0.1:8080/api/kill"

# 2. Revert local working tree to origin/main
git checkout main
git reset --hard origin/main

# 3. Deploy clean HEAD archive to VM
.\deploy.ps1 -Live -AllowDirty

# 4. Verify healthy static state
gcloud.cmd compute ssh $VM --zone=$ZONE --project=$PROJ --command="sudo docker logs --tail 25 schwab_engine_container"
```

---

## 8. Definition of Success (Verification Gate Checklist)

Before concluding the rollout, all items below must be verified:

- [ ] **Phase 1 Complete:** `core/dynamic_scanner.py`, `core/universe_manager.py`, and `data/streamer.py` pass all local syntax checks and unit tests.
- [ ] **Phase 2 Complete:** Remote state audit at 11:30 EDT confirms 0 open positions before initiating container reload.
- [ ] **Phase 3 Complete:** Container restarts cleanly; on-demand midday scan ranks the Top 3 assets; 10-day 1m candle cache is populated with zero API quota warnings.
- [ ] **Phase 4 Complete:** Power Hour initiates at 14:00 EDT; empirical RVOL and return hurdles are actively enforced; zero `FrozenInstanceError` exceptions occur.
- [ ] **Phase 5 Complete:** Flatten sweep at 15:50 EDT confirms 0 open positions; 100% cash preserved; audit log recorded in `/app/data/schwab_telemetry.db`.
