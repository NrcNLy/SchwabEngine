---
name: compile-research-context
description: >-
  Compiles codebase architecture logic, runtime configuration state, and systemd journalctl
  telemetry into a comprehensive research context document (research_context.md) on the GCP VM
  (schwab-trader) for LLM reasoning, post-mortem incident analysis, and quantitative audits.
---

# Research Context Compilation Skill

This skill provides an automated, programmatic workflow to snapshot the active state of the `schwab_engine` trading infrastructure. It extracts scoped source code modules, runtime strategy configuration, live systemd journalctl telemetry, and SQLite state databases into a single grounded Markdown reference document (`research_context.md`).

## 1. When to Use This Skill
- Preparing research context or priming an LLM agent context before executing architectural refactors or trade debugging.
- Conducting post-session or pre-market diagnostic audits on the GCP VM (`schwab-trader`).
- Investigating live trading anomalies, auth circuit breaker trips, or broker synchronization issues.
- Documenting operational state and mathematical models (Yang-Zhang volatility stops, Bayesian Quarter-Kelly sizing, synthetic divergence).

## 2. Execution Runbook

### Primary Execution (General Session Snapshot)
From the repository root on the GCP VM (`schwab-trader`):
```bash
cd ~/schwab_engine && .venv/bin/python scripts/compile_research_context.py --scope general --since "today" --output research_context.md
```

### Scoped Invocations
Filter extracted logic and telemetry to specific subsystems:
```bash
# Execution & Order Management focus
.venv/bin/python scripts/compile_research_context.py --scope execution --since "today" --output research_context.md

# Risk, Volatility & Regime focus
.venv/bin/python scripts/compile_research_context.py --scope risk --since "today" --output research_context.md

# SOXL Autonomous Recovery focus
.venv/bin/python scripts/compile_research_context.py --scope recovery --since "today" --output research_context.md

# Event-sourcing & SQLite WAL telemetry focus
.venv/bin/python scripts/compile_research_context.py --scope telemetry --since "today" --output research_context.md

# Comprehensive full-system dump
.venv/bin/python scripts/compile_research_context.py --scope all --since "today" --output research_context.md
```

### CLI Parameters
- `--scope`: Target subsystem scope (`general`, `execution`, `risk`, `recovery`, `telemetry`, `all`). Default: `general`.
- `--since`: Journalctl time window filter (e.g. `"today"`, `"yesterday"`, `"1 hour ago"`, `"2026-10-15 00:00:00"`). Default: `"today"`.
- `--output`: Destination path for the generated Markdown document. Default: `research_context.md`.
- `--lines`: Maximum log lines extracted per systemd unit. Default: `50`.
- `--units`: Comma-separated list of systemd service units to query. Default: `schwab-trader.service,schwab-web.service,schwab-recovery-watchdog.service`.

## 3. Extracted Context Components

1. **Executive Summary & Environment**:
   - Hostname, operating system, timestamp, git branch, and active commit hash.
   - Core 0 / Core 1 CPU pinning and thread state.

2. **Configuration & Regime State**:
   - `strategy_config.json`: Macro regime (`REGIME_A`, `BULLISH`), risk multipliers, Yang-Zhang adjustments, AVE parameters.
   - `metadata.json`: Engine deployment metadata.
   - Sanitized `.env` indicator (presence of credentials without exposing plaintext secrets).

3. **Scoped Codebase Modules**:
   - Core algorithms: `core/ledger.py` (three-bucket cash accounting, GFV invariants).
   - Execution routing: `execution/order_manager.py` (firewall, pre-trade gates), `execution/almgren_chriss.py`.
   - Risk management: `execution/risk_manager.py` (Yang-Zhang trailing stop scalar, Hurst exponent ratchet).
   - Synthetic divergence: `core/synthetic_divergence.py` (cash-open anchor latching, RVOL delta spread).
   - Auth & Rate-Limiter: `core/auth.py` (circuit breaker), `core/rate_limiter.py` (3,500 daily quota).

4. **Live Systemd Journalctl Telemetry**:
   - `schwab-trader.service` (Core 0 trading daemon logs).
   - `schwab-web.service` (Core 1 FastAPI dashboard and REST gateway logs).
   - `schwab-recovery-watchdog.service` (Autonomous recovery supervisor logs).

5. **State Store & SQLite WAL Telemetry**:
   - WAL mode validation and file sizes for `state/trades.db`, `state/telemetry.db`, and `state/market_baselines.db`.
   - Record counts for trade events and order fills.

## 4. Verification & Output Validation
After running the script, verify the artifact:
```bash
# Verify file presence and line count
wc -l research_context.md

# Inspect headers and structure
head -n 40 research_context.md
```
Ensure all sections are populated and no unhandled execution exceptions occurred.
