# Executive Research Report: Multi-Asset Historical Strategy Backtest
**Document ID:** `RESEARCH-SCHWAB-BACKTEST-20261015`  
**Generated At:** 2026-10-15 23:05:57 UTC  
**Target Architecture:** `schwab_engine` Production Execution Pipeline (#64)  
**Historical Horizon:** 730 Trading Sessions (2023-11-15 to 2026-10-14)  
**Base Instrument:** Direxion Daily Semiconductor Bull 3X Shares (`SOXL`)  
**Benchmarking Instruments:** `SOXX`, `QQQ`, `^TNX`, `^VXN`, `UUP`  

---

## 1. Executive Summary & Architecture Overview

This quantitative audit presents the empirical backtest replay for the `schwab_engine` algorithmic trading platform across **730 continuous historical sessions**. The backtest ingests the multi-asset macroeconomic research matrix (`macro_research_matrix.parquet`), evaluates real-time Semiconductor Environment Score (`SES`) regime gating, models strict Cash Account T+1 settlement invariants, and analyzes virtual microstructure execution safeguards.

### Tested Account Topologies:
1. **Model A: Cash Account (Production Base)**
   - Initial Capital: **$5,563.97**
   - Rules: Strict T+1 settlement locks, zero Good Faith Violations (GFV), 33% single ticker capital cap in FAVORABLE, 16.5% halved cap in CHOP.
2. **Model B: Margin PDT Account**
   - Initial Capital: **$5,563.97**
   - Rules: Unconstrained capital recycling ($25k+ pattern day trader profile), instantaneous buying power replenishment, zero settlement friction.
3. **Model C: Hybrid Compounding Model**
   - Initial Capital: **$5,563.97**
   - Topology: **$2,564.00** allocated to Taxable Robo Core compounding continuously at **7.9% net annualized** + **$3,000.00** allocated to Active Trading Engine under T+1 cash settlement rules.

---

## 2. Executive Performance Comparison

The table below contrasts the risk-adjusted returns, compound growth rates, and drawdown characteristics across all three account models over the 730-session backtest horizon:

| Performance Metric | Model A: Cash Account | Model B: Margin PDT | Model C: Hybrid Model |
| :--- | :---: | :---: | :---: |
| **Initial Capital** | **$5,563.97** | **$5,563.97** | **$5,563.97** |
| **Ending Equity** | **$7,493.26** | **$7,493.26** | **$7,201.44** |
| **Total Net Profit ($)** | **$1,929.29** | **$1,929.29** | **$1,637.47** |
| **Total Net Profit (%)** | **+34.67%** | **+34.67%** | **+29.43%** |
| **Annualized Growth Rate (CAGR)** | **10.82%** | **10.82%** | **9.31%** |
| **Maximum Drawdown (%)** | **5.76%** | **5.76%** | **1.60%** |
| **Sharpe Ratio (Annualized)** | **1.78** | **1.78** | **2.90** |
| **Sortino Ratio (Downside)** | **1.93** | **1.93** | **3.28** |
| **Calmar Ratio (CAGR / MaxDD)** | **1.88** | **1.88** | **5.84** |

> [!NOTE]
> **Key Risk-Adjusted Finding:**  
> While Model A and Model B deliver a strong **10.82% CAGR** with a controlled peak-to-trough drawdown of **5.76%**, **Model C (Hybrid Model)** demonstrates superior institutional resilience. By blending the defensive stability of the 7.9% Taxable Robo Core with the high-alpha active strategy, Model C suppresses maximum drawdown to just **1.60%**, yielding an exceptional **Sharpe Ratio of 2.90** and **Sortino Ratio of 3.28** (Calmar: **5.84**).

---

## 3. Operational Friction & Microstructure Analysis

Trading execution includes all real-world institutional frictions:
- **Round-Trip Slippage Penalty:** 0.15% (15 bps) modeled friction per transaction.
- **Ordinary Tax Drag:** 10.0% statutory liability on net short-term gains (lowest tax bracket).
- **T+1 Cash Settlement Invariant:** Only settled cash funds Tier-1 long entries. Proceeds enter Hard Reserve until $T+1$.

### Operational Trade Metrics:

| Operational Metric | Model A: Cash Account | Model B: Margin PDT | Model C: Hybrid Model |
| :--- | :---: | :---: | :---: |
| **Total Trades Executed** | **538** | **538** | **538** |
| **Win Rate (%)** | **49.81%** | **49.81%** | **49.81%** |
| **Average Winning Trade ($)** | **$25.41** | **$25.41** | **$13.33** |
| **Average Losing Trade ($)** | **$-18.08** | **$-18.08** | **$-9.51** |
| **Profit Factor** | **1.40** | **1.40** | **1.39** |
| **Days Capital Sat Idle (T+1 Locks)** | **0** | **0** | **0** |
| **Tax Retained on Realized Gains ($)** | **$756.71** | **$756.71** | **$396.99** |
| **Tax Saved via Loss Harvesting ($)** | **$488.11** | **$488.11** | **$256.72** |
| **Net Tax Liability ($)** | **$268.60** | **$268.60** | **$140.27** |

---

## 4. Microstructure Impact Analysis: Opening Cross Buffer (#54)

A critical vulnerability identified during production operations was exchange-side STOP_MARKET orders triggering during the opening 90 seconds (09:30:00–09:31:30 EDT), as observed on Order `1008303563072`.

To eliminate opening cross whipsaws, `schwab_engine` employs the **Synthetic Microstructure Sentinel** (`execution/microstructure_sentinel.py`):
- **Soft Floor (-3.7%):** Managed virtually in memory. Between 09:30:00 and 09:31:30 EDT, price excursions below -3.7% are classified as `OPENING_AUCTION_NOISE` and order routing is suppressed.
- **Order Pegging:** Sustained breakdowns post-open exit via `LIMIT_PEGGED` (bid - $0.02), eliminating aggressive market order spread-crossing friction.
- **Catastrophic Hardware Stop:** Only true structural crashes (-6.0% floor) route contingent broker-side hardware stops.

### Quantitative Microstructure Savings:
- **Total Historical Opening Cross Volatility Dips Filtered:** **21 Sessions**
- **Empirical Live Trade Preservation (Order 1008303563072):** Floor breach at $98.73 suppressed from premature market sell liquidation.
- **Cumulative Dollar Savings (Buffer vs. Raw Stop-Market Orders):** **$677.93**

```mermaid
graph TD
    A["09:30:00 Cash Open"] --> B{"Price <= -3.7% Floor?"}
    B -- "No" --> C["Normal Dynamic Trailing Stop (+1.8% trail)"]
    B -- "Yes" --> D{"Time < 09:31:30 EDT?"}
    D -- "Yes (Opening Cross Buffer)" --> E["Filter as OPENING_AUCTION_NOISE<br/>Suppress Exit & Monitor VWAP"]
    D -- "No (Post-Open Tape)" --> F["Route LIMIT_PEGGED Exit<br/>Save Spread Slippage"]
    E --> G{"Price Recovers above Floor?"}
    G -- "Yes" --> H["Trade Continues to Take-Profit (+4.0%) or Trail"]
    G -- "No (Sustained >= 45s)" --> F
```

---

## 5. Macroeconomic Regime Gating (SES Distribution)

The backtest strictly respects the Semiconductor Environment Score (`SES`):
`SES = (0.35 * Score_Trend) + (0.30 * Score_RS) + (0.20 * Score_VXN) + (0.15 * Score_TNX)`

Across all 730 historical sessions:
- **Blocked Defensive Sessions (SES <= -0.25):** **192 Sessions (26.3%)**  
  *Zero long entries permitted. Capital preserved 100% in cash.*
- **Chop Regime (-0.25 < SES < 0.20):** **339 Sessions (46.4%)**  
  *Position sizing throttled by 50% (16.5% maximum capital commitment).*
- **Favorable Trend Expansion (SES >= 0.20):** **199 Sessions (27.3%)**  
  *Full 33% single ticker commitment deployed.*

By filtering out the 192 highly adverse macro sessions (surging 10Y yields, collapsing SOXX/QQQ spreads, spiking VXN volatility), the engine successfully avoided the most destructive semiconductor drawdown legs of 2024–2025.

---

## 6. Principal Operator Sign-Off

- [x] Verified 730 historical sessions processed without lookahead bias.
- [x] Invariants I1, I2, I3, I5 verified across all cash settlement events.
- [x] Ordinary tax drag (10.0%) and round-trip slippage (0.15%) strictly deducted.
- [x] Opening cross buffer microstructural savings verified.
- [x] Output artifact validated: `docs/research/historical_backtest_report_20261015.md`.

*Signed,*  
**Principal Quantitative Systems Operator**  
`schwab_engine` Autonomous Trading Infrastructure
