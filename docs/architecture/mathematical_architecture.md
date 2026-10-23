# SchwabEngine Mathematical Architecture
## Quantitative Specification: Dual-Sleeve Robo-Core Routing, Continuous 3-State HMM Meta-Governor, & FINRA Rule 4210 Margin Engine

---

## Executive Summary & Architectural Axioms

The SchwabEngine operates as an institutional-grade, continuous-time quantitative execution system tailored for high-beta leveraged exchange-traded funds ($3\times$ LETFs: `SOXL`, `TQQQ`, `TNA`) paired with a passive $1\times$ beta index basket (**Schwab Robo-Core**: `SPY`, `QQQ`, `IWM`, `HYG`, `IEF`).

The system is governed by five fundamental mathematical axioms:
1. **Zero Cash Drag Capital Conservation:** Idle cash drag is eliminated ($M(t) \to \epsilon_{\mathrm{buf}}$). Total Net Liquidation Value $N(t)$ is partition-divided between the $1\times$ Robo-Core $\mathbf{C}(t)$ and the $3\times$ Tactical Satellite $\mathbf{S}(t)$:
   $$W_{\mathrm{core}}(t) + W_{\mathrm{sat}}(t) \equiv 1.0$$
2. **Complete Deprecation of PDT & T+1 Settlement Restrictions:** All legacy Pattern Day Trader (PDT) equity tiers and T+1 settled-cash throttling locks are eliminated. All sizing and rebalancing operations are scale-invariant functions of $N(t)$ scaling seamlessly from $\$5\text{k}$ to $\$100\text{k+}$.
3. **Continuous 3-State HMM Meta-Governor:** Replaces memoryless discrete thresholding with a Bayesian filtered probability vector $\boldsymbol{\pi}(t) = [\pi_G(t), \pi_U(t), \pi_S(t)]^\top \in \Delta^2$ over $\{\text{Goldilocks}, \text{Uncertainty}, \text{Macro Shock}\}$ featuring a $T_{kk} = 0.92$ self-persistence transition hysteresis matrix.
4. **Core-Accelerated Drawdown Recovery (Strategy D):** Capital trimmed from the satellite sleeve during high-risk regimes compounds at the live $1\times$ beta return $r_{\mathrm{core}}(t)$, dynamically accelerating high-water-mark decay ($\lambda_{\mathrm{hwm}}^{\mathrm{eff}}(t)$) and healing tactical drawdowns significantly faster than static cash.
5. **FINRA Rule 4210(f)(8)(A) Leverage Governance:** Enforces differential maintenance margin requirements ($\mu_{\mathrm{sat}} = 0.75\text{--}1.00$ vs. $\mu_{\mathrm{core}} = 0.25$) alongside a Two-Stage End-of-Day De-Levering schedule (`15:50 ET` primary regular-session trim + `19:45 ET` extended-hours hard backstop).

---

## 1. The Macro Meta-Governor (Continuous 3-State HMM)

### 1.1 Multi-Rate Macroeconomic State Vector $\mathbf{M}(t)$
The continuous observation vector synthesizes three decoupled telemetry channels:

$$\mathbf{M}(t) = \begin{bmatrix} Z_s(t) \\ S_{\mathrm{nlp}}(t_0) \, e^{-\lambda_{\mathrm{nlp}}(t - t_0)} \\ CS_{\mathrm{hybrid}}(t) \end{bmatrix}$$

where:
* $Z_s(t) \in \mathbb{R}$: Daily Economic Surprise index normalized as a 20-day rolling Z-score.
* $S_{\mathrm{nlp}}(t_0) \in [-1.0, +1.0]$: Deterministic Gemini NLP sentiment polarity score sampled at $t_0$, subjected to continuous exponential time decay with half-life $\tau_{1/2} = 4\text{ hours}$ ($\lambda_{\mathrm{nlp}} = \frac{\ln 2}{14400}\text{ s}^{-1}$).
* $CS_{\mathrm{hybrid}}(t) \in \mathbb{R}^+$: Credit spread metric fusing the daily FRED ICE BofA High-Yield Option-Adjusted Spread anchor (`BAMLH0A0HYM2`) with the real-time intraday credit spread proxy:
  $$CS_{\mathrm{hybrid}}(t) = 0.70 \cdot OAS_{\mathrm{fred}} + 0.30 \cdot CS_{\mathrm{intraday}}(t)$$

### 1.2 Transition Matrix with Self-Persistence Hysteresis ($\mathbf{T}$)
To prevent regime chattering at decision boundaries, the HMM incorporates a $3\times 3$ Markov transition matrix $\mathbf{T}$ where diagonal elements maintain $T_{kk} = 0.92$ self-persistence:

$$\mathbf{T} = \begin{bmatrix}
0.920 & 0.068 & 0.012 \\
0.040 & 0.920 & 0.040 \\
0.012 & 0.068 & 0.920
\end{bmatrix}$$

where $T_{ij} = \mathbb{P}\big(X(t+\Delta t) = j \mid X(t) = i\big)$ for states $\{\text{Goldilocks } (0), \text{ Uncertainty } (1), \text{ Macro Shock } (2)\}$.

### 1.3 Bayesian Filtering & Posterior Updating
The filtered state probability vector $\boldsymbol{\pi}(t) = [\pi_G(t), \pi_U(t), \pi_S(t)]^\top$ updates continuously via Bayes' rule:

$$\boldsymbol{\pi}_{\mathrm{pred}}(t) = \mathbf{T}^\top \boldsymbol{\pi}(t - \Delta t)$$

$$\boldsymbol{\pi}(t) = \frac{\boldsymbol{\pi}_{\mathrm{pred}}(t) \odot \mathbf{L}\big(\mathbf{M}(t)\big)}{\left\|\boldsymbol{\pi}_{\mathrm{pred}}(t) \odot \mathbf{L}\big(\mathbf{M}(t)\big)\right\|_1}$$

where the emission likelihood vector $\mathbf{L}(\mathbf{M}(t)) = [L_0, L_1, L_2]^\top$ evaluates multivariate Gaussian probability densities:

$$\ln L_k\big(\mathbf{M}(t)\big) = -\frac{1}{2} \sum_{d=1}^3 \left(\frac{M_d(t) - \mu_{k, d}}{\sigma_{k, d}}\right)^2$$

with empirical regime parameterization:
* **Goldilocks ($k=0$):** $\boldsymbol{\mu}_0 = [+1.0, +0.70, 320\text{ bps}]$, $\boldsymbol{\sigma}_0 = [1.0, 0.40, 50\text{ bps}]$
* **Uncertainty ($k=1$):** $\boldsymbol{\mu}_1 = [0.0, 0.00, 420\text{ bps}]$, $\boldsymbol{\sigma}_1 = [0.8, 0.35, 60\text{ bps}]$
* **Macro Shock ($k=2$):** $\boldsymbol{\mu}_2 = [-1.5, -0.70, 580\text{ bps}]$, $\boldsymbol{\sigma}_2 = [1.2, 0.50, 80\text{ bps}]$

---

## 2. Portfolio State Space & FINRA Margin Constraints

### 2.1 Dual-Sleeve Portfolio Partition & Capital Conservation SDE
Total Net Liquidation Value $N(t)$ anchors all relative strategy weights:

$$N(t) = \underbrace{\sum_{i=1}^{25} C_i(t)}_{\|\mathbf{C}(t)\|_1 \text{ (1x Robo-Core)}} + \underbrace{\sum_{j \in \{\text{SOXL, TQQQ, TNA}\}} S_j(t)}_{\|\mathbf{S}(t)\|_1 \text{ (3x Tactical Satellite)}} + M_{\mathrm{buf}}(t)$$

Capital transfers between the sleeves follow the **Dual-Sleeve Capital Conservation SDE**:

$$d\big(\|\mathbf{C}(t)\|_1\big) = r_{\mathrm{core}}(t) \|\mathbf{C}(t)\|_1 \, dt - d\big(\|\mathbf{S}(t)\|_1\big) - \phi \cdot \big|d\big(\|\mathbf{S}(t)\|_1\big)\big|$$

where:
* $r_{\mathrm{core}}(t) = \sum_i \nu_i \frac{dP_{\mathrm{core}, i}(t)}{P_{\mathrm{core}, i}(t)}$ is the continuous $1\times$ beta return of the Robo-Core sleeve.
* $\phi$ represents frictional transaction costs and bid-ask slippage.
* Idle cash is strictly bounded: $M_{\mathrm{buf}}(t) \to \epsilon \ll N(t)$.

### 2.2 FINRA Rule 4210(f)(8)(A) Differential House Maintenance Margin
Under FINRA Rule 4210(f)(8)(A), leveraged ETFs require maintenance margin scaled by leverage factor $L=3$:

$$\mu_{\mathrm{sat}, j}^{\mathrm{intraday}} = \min\big(1.00, \ L_j \cdot 0.25\big) = 0.75, \qquad \mu_{\mathrm{sat}, j}^{\mathrm{overnight}} = \min\big(1.00, \ L_j \cdot 0.30\big) = 0.90 \text{ (or } 1.00\text{)}$$

$$\mu_{\mathrm{core}, i}^{\mathrm{intraday}} = 0.25, \qquad \mu_{\mathrm{core}, i}^{\mathrm{overnight}} = 0.50 \text{ (Reg T)}$$

Aggregate Intraday Margin Excess $E_{\mathrm{excess}}^{\mathrm{intra}}(t)$ and Asset-Specific Buying Power $IBP_j(t)$ are:

$$E_{\mathrm{excess}}^{\mathrm{intra}}(t) = N(t) - \left(\mu_{\mathrm{core}}^{\mathrm{intra}} \|\mathbf{C}(t)\|_1 + \sum_{j} \mu_{\mathrm{sat}, j}^{\mathrm{intra}} S_j(t)\right)$$

$$IBP_j(t) = \max\left(0, \ \frac{E_{\mathrm{excess}}^{\mathrm{intra}}(t)}{\mu_{\mathrm{sat}, j}^{\mathrm{intra}}}\right)$$

### 2.3 Instantaneous Dual-Sleeve Margin-Bridge Swaps
Because selling $1\times$ Robo-Core assets $\Delta \mathbf{C}(t) < 0$ releases both cash proceeds $|\Delta \mathbf{C}|$ and margin requirement $\mu_{\mathrm{core}}^{\mathrm{intra}} |\Delta \mathbf{C}|$, the engine executes paired swaps with **zero settlement delay**:

$$\Delta S_{\mathrm{deployable}}(t) = |\Delta \mathbf{C}(t)| \cdot (1 - \phi)$$

No T+1 settlement locks, Good Faith Violation (GFV) delays, or Pattern Day Trader (PDT) limits inhibit reallocation.

### 2.4 Two-Stage EOD De-Levering Schedule ($\Delta_{\mathrm{EOD}}$)
To eliminate overnight margin excess prior to broker cutoff snapshots:

$$\Delta_{\mathrm{EOD}}(t) = \max\left(0, \ \sum_{i} \mu_{\mathrm{core}, i}^{\mathrm{overnight}} C_i(t) + \sum_{j} \mu_{\mathrm{sat}, j}^{\mathrm{overnight}} S_j(t) - N(t)\right)$$

* **Stage 1 ($t_{\mathrm{EOD}, 1} = \text{15:50:00 ET}$ Primary Auction Trim):** Liquidates excess leverage into peak regular-session auction liquidity. Positions are sorted to liquidate $3\times$ LETFs first ($\mu_{\mathrm{overnight}} = 0.90$), maximizing margin relief per dollar sold.
* **Stage 2 ($t_{\mathrm{EOD}, 2} = \text{19:45:00 ET}$ Extended-Hours Hard Backstop):** Failsafe liquidation before the 20:00 ET extended-hours close.

---

## 3. Unified Layered Tactical Strategies (A–D)

The composite tactical satellite target weight $W_{\mathrm{sat}}^*(t)$ is determined via continuous evaluation of four layered strategies:

$$W_{\mathrm{sat}}^*(t) = \min\Big(W_{\mathrm{max}, A}(t), \ \big(\pi_G(t) W_C(t) + \pi_U(t) \min(w_{\mathrm{base}}, W_C(t))\big)(1 - \pi_S(t)) \cdot D_{\mathrm{sat}}\big(DD_{\mathrm{sat}}^{\mathrm{eff}}(t)\big)\Big)$$

### 3.1 Strategy A: Inverse-Volatility Parity Upper Envelope
Upper-bounds exposure inversely with 32-hour Yang-Zhang realized volatility $\sigma_{32\mathrm{h}}(t)$:

$$W_{\mathrm{max}, A}(t) = \min\left(0.30, \ w_{\mathrm{base}} \cdot \frac{\sigma_{\mathrm{target}}}{\sigma_{32\mathrm{h}}(t)}\right)$$

### 3.2 Strategy C: Per-Asset Underlying Benchmark Trend Gate
Evaluates each $3\times$ LETF $j \in \{\text{SOXL}, \text{TQQQ}, \text{TNA}\}$ against its unlevered underlying index $b(j) \in \{\text{SOXX}, \text{QQQ}, \text{IWM}\}$:

$$W_{C, j}(t) = \begin{cases} 
w_{\mathrm{base}} & \text{if } P_{b(j)}(t) > SMA_{50, b(j)}(t) \land \sigma_{\mathrm{YZ}, j}(t) < \gamma_j \\ 
0.75 \, w_{\mathrm{base}} & \text{if } P_{b(j)}(t) > SMA_{50, b(j)}(t) \land \sigma_{\mathrm{YZ}, j}(t) \ge \gamma_j \\ 
0.25 \, w_{\mathrm{base}} & \text{if } P_{b(j)}(t) \le SMA_{50, b(j)}(t) \land \sigma_{\mathrm{YZ}, j}(t) < \gamma_j \\ 
0.00 & \text{if } P_{b(j)}(t) \le SMA_{50, b(j)}(t) \land \sigma_{\mathrm{YZ}, j}(t) \ge \gamma_j 
\end{cases}$$

with aggregate trend target $W_C(t) = \sum_j \omega_j(t) W_{C, j}(t)$.

### 3.3 Strategy D: Core-Accelerated Drawdown Recovery
Let $W_{\mathrm{swept}}(t) = \max\big(0, w_{\mathrm{base}} - w(t)\big)$ denote tactical capital sheltered in the $1\times$ Robo-Core. The live beta return $r_{\mathrm{core}}(t)$ **accelerates high-water mark decay**:

$$\lambda_{\mathrm{hwm}}^{\mathrm{eff}}(t) = \lambda_0 + \kappa_{\mathrm{core}} \cdot \max\big(0, \, r_{\mathrm{core}}(t)\big) \cdot \left(\frac{W_{\mathrm{swept}}(t)}{w_{\mathrm{base}}}\right)$$

$$\frac{d E_{\mathrm{sat, peak}}(t)}{dt} = -\lambda_{\mathrm{hwm}}^{\mathrm{eff}}(t) \big(E_{\mathrm{sat, peak}}(t) - E_{\mathrm{sat}}(t)\big)$$

Simultaneously, cumulative Core beta gains $\Delta_{\mathrm{core\text{-}heal}}(t) = \int_{t_{\mathrm{shock}}}^t W_{\mathrm{swept}}(u) N(u) r_{\mathrm{core}}(u) \, du$ actively heal effective drawdown:

$$DD_{\mathrm{sat}}^{\mathrm{eff}}(t) = \max\left(0, \ \frac{E_{\mathrm{sat, peak}}(t) - \big(E_{\mathrm{sat}}(t) + \Delta_{\mathrm{core\text{-}heal}}(t)\big)}{E_{\mathrm{sat, peak}}(t)}\right)$$

The continuous drawdown damper $D_{\mathrm{sat}}(t) \in [0.0, 1.0]$ scales:
* $DD_{\mathrm{sat}}^{\mathrm{eff}} < 0.05 \implies D_{\mathrm{sat}} = 1.0$
* $0.05 \le DD_{\mathrm{sat}}^{\mathrm{eff}} \le 0.10 \implies D_{\mathrm{sat}} = 1.0 - \left(\frac{DD - 0.05}{0.05}\right) \cdot 0.5$
* $DD_{\mathrm{sat}}^{\mathrm{eff}} > 0.10 \implies D_{\mathrm{sat}} = \max\left(0.0, \ 0.5 - \left(\frac{DD - 0.10}{0.05}\right) \cdot 0.5\right)$

### 3.4 Strategy B: Unchained Leland No-Trade Zone Rebalancer
Strategy B continuously checks active satellite weight $w(t) = \frac{\|\mathbf{S}(t)\|_1}{N(t)}$ against $W_{\mathrm{sat}}^*(t) \pm \tau_{\mathrm{band}}$:

$$\Delta \mathbf{S}(t) = \begin{cases} 
-\Big(w(t) - \big(W_{\mathrm{sat}}^*(t) + \tau_{\mathrm{band}}\big)\Big) N(t) & \text{if } w(t) > W_{\mathrm{sat}}^*(t) + \tau_{\mathrm{band}} \quad \text{(Harvest to Core } \mathbf{C}(t)\text{)} \\ 
+\Big(\big(W_{\mathrm{sat}}^*(t) - \tau_{\mathrm{band}}\big) - w(t)\Big) N(t) & \text{if } w(t) < W_{\mathrm{sat}}^*(t) - \tau_{\mathrm{band}} \land W_C(t) > 0 \quad \text{(Trim Core to Buy Satellite)} \\ 
0 & \text{otherwise (Inside Leland NTZ)} 
\end{cases}$$

$$\Delta \mathbf{C}(t) = -\Delta \mathbf{S}(t)$$

---

## 4. Hybrid Bayesian-Merton Sizing & Stock-vs-Flow Order Deltas

### 4.1 $3\times$ LETF Volatility Drag & Financing Multiplier ($\phi_{\mathrm{drag}, j}$)
To account for leveraged variance drain and swap financing costs:

$$\phi_{\mathrm{drag}, j}(t) = \text{clip}\left(1 - \frac{\frac{1}{2}L^2 \sigma_{\mathrm{YZ}, j}^2(t) + (L-1)(r_f + s_{\mathrm{swap}}) + c_{\mathrm{fee}}}{\max(\epsilon, \, L \hat{\mu}_j(t))}, \ 0.0, \ 1.0\right)$$

$$f_{Q, \mathrm{adj}, j}(t) = \frac{1}{4}\left(\frac{b \hat{p} - \hat{q}}{b}\right) \cdot \phi_{\mathrm{drag}, j}(t)$$

### 4.2 Correlation-Penalized Inverse Yang-Zhang Sub-Weights ($\omega_j(t)$)
Sub-weights penalize intra-portfolio correlation and excessive volatility:

$$\tilde{\omega}_j(t) = \frac{m_{\mathrm{sector}, j}(t)}{\sigma_{\mathrm{YZ}, j}(t) \cdot \sum_{k} |\rho_{jk}(t)|}, \qquad \omega_j(t) = \frac{\tilde{\omega}_j(t)}{\sum_m \tilde{\omega}_m(t)}$$

where $m_{\mathrm{sector}, j}(t)$ represents NLP sector drift multipliers.

### 4.3 Scale-Invariant Stock-vs-Flow Order Delta ($\Delta Q_j(t)$)
Target share inventory $Q_{j, \mathrm{target}}(t)$ and discrete order delta $\Delta Q_j(t)$:

$$Q_{j, \mathrm{target}}(t) = \min\left(\left\lfloor \frac{\omega_j(t) f_{Q, \mathrm{adj}, j}(t) N(t)}{P_{\mathrm{micro}, j}(t)} \right\rfloor, \ \left\lfloor \frac{\omega_j(t) W_{\mathrm{sat}}^*(t) N(t)}{P_{\mathrm{micro}, j}(t)} \right\rfloor\right)$$

$$\Delta Q_j(t) = Q_{j, \mathrm{target}}(t) - Q_{j, \mathrm{held}}(t)$$

paired with the opposing Robo-Core funding/sweep notional:

$$\Delta C_{\mathrm{usd}}(t) = -\sum_{j} \Delta Q_j(t) P_{\mathrm{micro}, j}(t)$$

$$\Delta Q_{\mathrm{core}, i}(t) = \text{round}\left(\frac{\nu_i \Delta C_{\mathrm{usd}}(t)}{P_{\mathrm{core}, i}(t)}\right)$$

### 4.4 Small-Account Lot-Quantization Rollover
For accounts scaling between $\$5\text{k}$ and $\$20\text{k}$, unallocated residual cash resulting from integer floor truncation $\lfloor \cdot \rfloor$ accumulates:

$$R_{\mathrm{rollover}}(t) = \sum_j \left(\frac{\omega_j(t) W_{\mathrm{sat}}^*(t) N(t)}{P_{\mathrm{micro}, j}(t)} - Q_{j, \mathrm{target}}(t)\right) P_{\mathrm{micro}, j}(t)$$

When $R_{\mathrm{rollover}}(t) \ge \min_j P_{\mathrm{micro}, j}(t)$, the residual is dynamically allocated as an extra discrete lot to the highest sub-weight asset $\arg\max_j \omega_j(t)$, eliminating stranded purchasing power.