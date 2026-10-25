---
name: compile-research-context
description: >-
  Compiles codebase architecture logic, runtime configuration state, and systemd journalctl
  telemetry into a comprehensive research context or system architecture & strategy review document
  (docs/YYMMDD.HH-architecture-strategy-review.md or docs/YYMMDD.HH-research-context.md) for LLM
  reasoning, post-mortem incident analysis, and quantitative audits.
---

# Research Context & Architecture Review Compilation Skill

This skill defines the mandatory operational standards, mathematical conventions, architectural invariants, and automated workflows for compiling and auditing the active state of the `schwab_engine` trading infrastructure. It generates grounded Markdown research context and architecture review documents (`research_context.md` and `docs/YYMMDD.HH-research-context.md`) and immediately synchronizes them with Google Drive.

---

## 1. Trigger Conditions & When to Use
Execute this workflow whenever:
- A system architecture review, research context, or quantitative strategy audit is requested (e.g., `#103`, `#121`, `#122`, pre-startup priming, post-session debrief).
- Priming an LLM agent context before executing architectural refactors, risk model adjustments, or execution router debugging.
- Conducting post-session or pre-market diagnostic audits on the GCP VM (`schwab-trader`) or local workstation.
- Documenting operational state, multi-rate synchronization, Leland No-Trade Zone (NTZ) bounds, and HMM Meta-Governor transitions.

---

## 2. Mandatory System Invariants & Account Constraints

All compiled research contexts and architectural reviews **MUST** strictly adhere to the following account and regulatory specifications:

### 2.1 Account Scope & Baseline Capital
- **Account Identification**: Single Charles Schwab Cash Account `***4015` (`single_account_comprehensive`).
- **Baseline NLV**: **$4,400.00 USD** (hot-boot reference baseline: **$4,400.90 USD**).
- **Prohibited Account Types**: The engine is strictly firewalled from interacting with any Margin, IRA, Robo, or Managed accounts.

### 2.2 Regulatory Boundaries & Settlement Rules
- **Regulatory Framework**: SEC Rule 15c6-1 T+1 Cash Settlement Rules (codified May 28, 2024).
- **Zero Margin & Zero PDT Exemptions**: Long-only cash account. Zero margin borrowing, zero short selling, and zero Pattern Day Trader (PDT) margin exemptions ($25,000 equity minimum applies to margin accounts, providing zero exemption here).
- **Good Faith Violation (GFV) Avoidance**:
  - A GFV occurs under Regulation T § 220.8 when shares purchased with unsettled sale proceeds are sold before those proceeds settle ($T+1$).
  - 3 GFVs within a rolling 12-month window trigger a mandatory 90-day cash-up-front restriction across the Schwab account.
  - The engine enforces strict zero-GFV operation via three-bucket cash accounting (`settled_cash`, `unsettled_cash`, `pending_ach`) and forced overnight holds on unsettled lots.

### 2.3 Unchained Dual-Sleeve Capital Model
- **Passive 1x Robo-Core Sleeve ($W_{core}^* = 0.85$)**:
  - Primary capital preservation and equity-beta basket:
    - `SPY`: 0.35 (Large-Cap Broad US Equity Beta)
    - `QQQ`: 0.25 (Large-Cap Technology Beta)
    - `HYG`: 0.15 (High-Yield Corporate Credit Beta)
    - `IEF`: 0.15 (Intermediate 7-10Y US Treasury Duration Hedge)
    - `IWM`: 0.10 (Russell 2000 Small-Cap Beta)
  - Zero Cash Drag Policy: Target cash allocation within the core routing vector is 0.00.
- **Tactical 3x Leveraged Satellite Sleeve ($W_{sat}^*(t) \in [0.00, 0.35]$)**:
  - Dynamically sized by the Online Hidden Markov Model (HMM) Meta-Governor:
    - `SOXL`: 0.40 (3x Daily Bull Semiconductor ETF)
    - `TQQQ`: 0.35 (3x Daily Bull NASDAQ-100 ETF)
    - `TNA`:  0.25 (3x Daily Bull Russell 2000 Small-Cap ETF)
- **Cash Sweep & Margin Buffer ($M_{buf}(t)$)**:
  - Residual fractional and clearance vehicle: `SWVXX` (Schwab Value Advantage Money Fund). Advisory sweep only; non-traded execution wall.
  - Core Portfolio Invariant: $W_{core}(t) + W_{sat}(t) + W_{buf}(t) \equiv 1.00$.

### 2.4 Execution & Risk Invariants
```yaml
system_profile:
  account_number_suffix: "015"
  account_type: "CASH_ACCOUNT"  # SEC Rule 15c6-1 T+1 Cash Settlement
  baseline_nlv_usd: 4400.00
  hot_boot_starting_nlv: 4400.90
  max_single_ticker_pct: 0.20  # 20% hard single-ticker exposure cap
  daily_api_quota: 3500        # Charles Schwab REST API daily request ceiling
  api_burst_limit_rpm: 120     # Token bucket burst capacity (2.0 req/sec)
  cash_reserve_floor_pct: 0.05 # 5% minimum cash reserve
```

---

## 3. Strict Output & LaTeX Rules

### 3.1 Standard Raw LaTeX Mandate
All mathematical formulations and quantitative models **MUST** be rendered using standard raw LaTeX enclosed in `$inline$` or `$$display$$`.
- **Allowed LaTeX**: Standard environments and macros including `\frac`, `\sum`, `\exp`, `\ln`, `\sqrt`, `\begin{bmatrix}`, `\begin{cases}`, `\in`, `\Delta`, `\pi`, `\sigma`, `\mu`, `\tau`, `\kappa`, `\lfloor`, `\rfloor`.
- **Examples**:
  - Intraday volatility root scaling:
    $$\sigma_{intraday} = \sigma_{YZ} \cdot \sqrt{\frac{1/390}{252}}$$
  - Bayesian win-rate posterior update:
    $$\theta_{post} = \frac{W + \kappa \cdot \mu_{prior}}{N + \kappa}$$
  - Fraction Quarter-Kelly sizing:
    $$f^* = \frac{1}{4} \left( \theta_{post} - \frac{1 - \theta_{post}}{R} \right) \left( \frac{N + \kappa}{N + 2\kappa} \right)$$
  - Leland No-Trade Zone boundaries:
    $$\tau_{lower} = \max(0.0, W_{sat}^*(t) - \tau_{band}), \quad \tau_{upper} = \min(0.35, W_{sat}^*(t) + \tau_{band})$$
  - Discrete Core Rebalance sweep delta:
    $$\Delta Q_c = \left\lfloor \frac{V_{freed} \cdot w_c}{P_c(t)} \right\rfloor$$
  - HYG/IEF Intraday Credit Spread proxy:
    $$\Delta CS_{bps}(t) = -\frac{\ln\left( \frac{r_{live}(t)}{r_{ref}} \right)}{3.5 \times 10^{-4}}$$

### 3.2 Prohibition of Image Placeholders & Proprietary Markup
- **No Image Placeholders**: Generating `[image]`, `[image: ...]`, or placeholder tags is strictly forbidden.
- **No Broken Markdown Images**: Do not output `![alt](url)` referencing non-existent files or local paths that fail to render in external viewers or Google Docs.
- **No Proprietary HTML Tags**: Avoid raw `<img ...>`, custom UI pills, or unsupported markdown tags that break downstream rendering in Google Drive, Google Docs, or Markdown export pipelines.

### 3.3 Production-Grade Python Code Blocks
- All presented Python code blocks **MUST** be complete, production-grade snippets extracted directly from the codebase.
- **Strictly Prohibited**: `pass` stubs, pseudo-code, ellipsis truncation (`...`), and missing function bodies.
- Code blocks must include authentic type hints, docstrings, variable names, and error handling as implemented in production.

---

## 4. Structured Research Document Standard (4 Canonical Sections)

Every compiled context file must begin with the standard front header and follow the four canonical sections:

### 4.1 Mandatory Front Header
```markdown
# SYSTEM ARCHITECTURE & STRATEGY REVIEW: [SUBJECT]
- timestamp_iso: "[YYYY-MM-DDTHH:MM:SS-04:00]"
- engine_version: "2.4.0-argon"
- git_branch: "[CURRENT_BRANCH]"
- commit_sha: "[CURRENT_COMMIT_SHA]"
- account_scope: "single_account_comprehensive" (Account: ***4015)
- baseline_nlv_usd: 4400.00
```

### 4.2 Section 1: System Invariants & Account Profile
1. **Account Profile & Dual-Sleeve Model**:
   - Detailed profile breakdown, allocation percentages, cash buffer, and YAML registry.
2. **Clean End-to-End Mermaid Topology**:
   - Compile-ready ````mermaid flowchart TD ```` tracing ingestion, governance, NTZ, firewall/risk gating, router execution, and temporal lifecycle.

### 4.3 Section 2: Active Codebase Logic & Mathematical Formulations
1. Mathematical formulations rendered in standard raw LaTeX (`$inline$` and `$$display$$`).
2. Production code blocks directly extracted from the codebase covering:
   - Order submission & rate-limited dispatch (`execution/order_manager.py`)
   - Volume-weighted microprice, mid-price, and Order Book Imbalance (OBI) (`data/multi_rate_sync.py`)
   - Bayesian Quarter-Kelly sizing & cash settlement clamping (`execution/sizing_governor.py`)
   - Two-stage EOD deleveraging logic (`execution/risk_manager.py`)
   - Broker stop update throttling & order cancellation rules (`execution/order_manager.py`)
   - Online 3-State HMM Meta-Governor transition matrix $\mathbf{T}$ & emission likelihoods (`execution/risk_manager.py`)
   - Dynamic Leland No-Trade Zone (NTZ) decision matrix (`execution/risk_manager.py`)
   - Live HYG/IEF credit spread proxy calculations (`data/multi_rate_sync.py`)
   - Three-bucket cash accounting & Mark-to-Market NLV (`core/ledger.py`)
3. Scoped code extraction covering the consolidated directory layout (`docs/`, `state/`, `data/fixtures/`, `scripts/tools/`).

### 4.4 Section 3: Empirical Telemetry & Down-Day Incident Baselines
1. Header metadata & financial summary (261022 selloff baseline: starting NLV $4,400.90, ending NLV $4,290.19, net return -$110.71 (-2.52%), benchmark deltas).
2. Macro regime occupancy & credit spread widening telemetry table.
3. Leland NTZ interval evaluation counts and initial portfolio deployment table.
4. Downsampled 5-minute NLV & regime trajectory table.
5. Incident post-mortems (rogue VM daemon deduplication `#117`, authentication circuit breaker `AUTH_LOCKED`, REST API quota clamping).
6. Live daemon health & systemd journalctl telemetry (if running).
7. Live SQLite state store telemetry (table row counts, sizes, and journal modes for `trades.db`, `telemetry.db`, `market_baselines.db`).

### 4.5 Section 4: Identified Architectural & Modeling Gaps
1. **Gap A**: Absence of fill-ratio and time-decay escalation in EOD flatten routine.
2. **Gap B**: Symmetrical, static & degenerate HMM state transition probabilities ($T_{01} = 0.0000$, diagonal inertia).
3. **Gap C**: Missing programmatic GFV checks on intraday redeployment of cash.
4. **Gap D**: Synchronous blocking calls (`time.sleep`), silent exception handlers (`except Exception: pass`), and missing token heartbeats.

---

## 5. Mandatory Google Drive Synchronization Protocol

Every compilation run **MUST** automatically conclude by executing:
```bash
python scripts/sync_engine_drive.py --once
```
- **Authoritative Mirror**: The local repository checkout is the sole source of truth.
- **Drive Target**: `G:\My Drive\Projects\SchwabEngine\`
- **Target Mirror**: Immediately updates `G:\My Drive\Projects\SchwabEngine\research_context.md` and mirrors any timestamped docs under `docs/`.
- **SQLite Safety**: Ensures all SQLite state databases are safely snapshotted via SQLite online backup APIs (`*_backup.db`) without locking active WAL files.

---

## 6. Execution Runbook

### 6.1 Automated Compilation & Synchronization
Execute the compilation utility from repository root:
```bash
# Standard compilation (generates research_context.md and immediately syncs to Drive)
python scripts/compile_research_context.py

# Comprehensive extraction with all subsystems
python scripts/compile_research_context.py --scope comprehensive

# Timestamped archival snapshot
python scripts/compile_research_context.py --output docs/YYMMDD.HH-research-context.md
```

### 6.2 Manual Verification Checklist
Before completing an architecture review or research context update:
- [ ] Destination path is `research_context.md` or `docs/YYMMDD.HH-research-context.md`.
- [ ] Header includes `account_scope: "single_account_comprehensive" (Account: ***4015)` and `baseline_nlv_usd: 4400.00`.
- [ ] All mathematical expressions use standard raw LaTeX enclosed in `$inline$` or `$$display$$`.
- [ ] No `[image]` placeholders, broken image tags, or proprietary markup are present.
- [ ] Python code blocks are complete, production-grade implementations without `pass` stubs or `...` truncation.
- [ ] All 4 canonical sections are populated in full depth.
- [ ] Automated call to `python scripts/sync_engine_drive.py --once` completed with exit code 0.
- [ ] Verified that `G:\My Drive\Projects\SchwabEngine\research_context.md` is updated.
