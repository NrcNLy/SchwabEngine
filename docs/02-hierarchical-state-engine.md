The system maintains continuous market awareness and dynamic adaptability through a **Hierarchical State Engine** that decouples macro-market telemetry, asset-level price discovery, and internal capital compliance.

Rather than treating the market as a static sequence of ticker prices, the architecture treats incoming data as a continuous multivariate stream that determines whether the engine is permitted to trade, which regime is active, and how resting orders must be restructured.

### 1. Identifying and Tracking Relevant Variables

To avoid exceeding the Schwab Trader API platform rate limit (120 requests per minute sustained), the system tracks a focused matrix of six quantitative variables calculated across rolling in-memory ring buffers:

- **Intraday VWAP Slope ($\Delta \text{VWAP}$)**:

  - *What it measures*: Directional institutional flow across the broad market (monitored via QQQ and SPY).

  - *Tracking logic*: Sampled every 60 seconds over a rolling 15-minute linear regression window. A positive slope ($\theta > 15^\circ$) confirms institutional accumulation; a negative slope indicates institutional distribution.

- **Normalized Average True Range (NATR)**:

  - *What it measures*: Local asset volatility independent of nominal share price.

  - *Tracking logic*: Calculated over a 14-period window on 1-minute bars: $$
\text{NATR}_{14} = \left( \frac{\text{ATR}_{14}}{\text{Close}} \right) \times 100
$$

  - *Usage*: When NATR expands rapidly, it signals an volatility breakout; when NATR contracts below its 20-day mean, it signals low-liquidity chop.

- **Relative Volume (RVOL)**:

  - *What it measures*: Volume conviction relative to time of day.

  - *Tracking logic*: Compares cumulative volume of the active candle at time t against the 20-day historical average volume for that exact minute interval. An $\text{RVOL} > 1.5$ confirms breakout validity; an $\text{RVOL} < 0.8$ invalidates breakout signals as false alarms.

- **Cross-Asset Momentum Z-Score ($Z_{\text{mom}}$)**:

  - *What it measures*: Relative strength across your target high-beta universe (SOXL, TQQQ, FNGU, CONL, DPST, BOIL).

  - *Tracking logic*: Evaluates 30-minute rate-of-change (ROC) normalized by historical standard deviation: $$
Z_{\text{mom}} = \frac{\text{ROC}_i - \mu_{\text{ROC}}}{\sigma_{\text{ROC}}}
$$

  - *Usage*: Determines which asset receives capital allocation during rotation cycles.

- **Cumulative Realized Drawdown ($\text{PnL}_{\text{daily}}$)**:

  - *What it measures*: Proximity to your hard -\$30.00 (-3.0%) daily circuit breaker.

  - *Tracking logic*: Updated instantaneously upon every sell order execution.

- **Time-to-Close Decay ($\tau$)**:

  - *What it measures*: Time remaining until the mandatory 3:55 PM EDT flat-to-cash deadline.

  - *Tracking logic*: Shrinks trade duration thresholds as the clock approaches 3:30 PM EDT, preventing late-day entries that lack time to develop.

### 2. Market Regime Classification Engine

The system feeds these variables into an automated **Regime State Machine** evaluated every 3 minutes. The active regime determines whether strategy execution is authorized or inhibited:

- **Regime A: Trend Expansion (Breakout Active)**

  - *Condition*: $\text{QQQ VWAP Slope} > 0$, $\text{RVOL} \ge 1.5$, $\text{NATR}$ expanding.

  - *Engine Response*: Activates the 15-Minute Opening Range Breakout (ORB) on the highest-ranking tech/semiconductor ETF (SOXL, TQQQ, or FNGU). Permits momentum entries with standard 2:1 profit targets.

- **Regime B: Range-Bound Compression (Mean-Reversion / Sideways Chop)**

  - *Condition*: $\text{QQQ VWAP Slope}$ oscillating near zero, $\text{RVOL} < 1.0$, price contained within the opening 15-minute high/low band.

  - *Engine Response*: Deactivates trend-following models. Switches either to VWAP 2.0-sigma mean-reversion limits or stands down in 100% cash to avoid whipsaw decay.

- **Regime C: Sector Decoupling / Macro Divergence**

  - *Condition*: Broad tech indices stall while independent assets break out (e.g., natural gas inventory release or crypto momentum surges).

  - *Engine Response*: Restructures focus away from tech assets and routes scanning logic toward uncorrelated high-beta instruments (BOIL or CONL).

- **Regime D: High-Volatility Shock / Distribution (Defense Mode)**

  - *Condition*: Broad indices drop \> 1.5\\ intraday with expanding volume, or your sandbox hits 2 consecutive stopped-out trades.

  - *Engine Response*: Immediate execution halt. Cancels all working limit buy orders, inhibits new signal generation, and protects the remaining cash balance.

### 3. Dynamic Restructuring and Adaptive Responses

When market conditions shift intraday, the engine adapts through three specific mechanical behaviors:

- **Volatility-Scaled Position Sizing**:

  - Rather than fixing a static share count, the engine dynamically recalculates order quantity using current ATR: $$
\text{Quantity} = \min\left( \left\lfloor \frac{\$10.00}{1.5 \times \text{ATR}_{14}} \right\rfloor, \left\lfloor \frac{\$990.00}{P_{\text{entry}}} \right\rfloor \right)
$$

  - If market volatility doubles, share sizing is automatically cut in half to ensure the dollar risk never exceeds your hard \$10.00 limit.

- **Trailing Stop Ratchets**:

  - When an active position achieves a +1.0R gain (unrealized profit equal to initial risk), the engine automatically moves the internal stop-loss to the entry price (break-even).

  - If the asset reaches +1.5R, it transitions to an ATR-stepped trailing stop ($1.2 \times \text{ATR}$ below the current high watermark), locking in gains as momentum decelerates.

- **Time-Based Order Expiration (TTL Engine)**:

  - Any resting limit buy order placed on the order book that does not fill within 20 minutes is canceled automatically. This prevents stale orders from sitting on the book right as an afternoon distribution leg begins.

### 4. Telemetry Pipeline & Android Edge Interaction

To keep you informed and maintain oversight without requiring you to watch terminal logs, the system connects to your Google Pixel 9a through a two-way telemetry pipeline:

- **Telemetry Dispatcher**:

  - The execution backend broadcasts lightweight JSON event payloads via Firebase Cloud Messaging (FCM) high-priority data messages or authenticated WebSockets.

  - Events include: REGIME_CHANGE (e.g., from Chop to Trend), ORDER_STAGED, ORDER_FILLED, STOP_RATCHETED, and CIRCUIT_BREAKER_TRIPPED.

- **Native Android Home Screen Widget (Jetpack Glance)**:

  - Built using Android Jetpack Glance (androidx.glance), rendering directly on your home screen without loading an entire app UI.

  - **Top Header**: Live status pill (**ACTIVE / TRENDING**, **CHOP / STANDBY**, or **HALTED / FROZEN**), current cash balance (1,000 baseline), and daily realized P&L ( / %).

  - **Mid-Card (Telemetry & Active Lots)**: Displays current open holding (e.g., 23x TQQQ @ \$42.50), current market bid, and distance to stop-loss / profit target.

  - **Interactive Action Callbacks**:

    - *Toggle Engine Button*: A one-tap button using GlanceModifier.clickable that toggles the backend state between **Autonomous** and **Paused**.

    - *Approval Card (Staged Mode)*: When the engine identifies a setup, it displays a card with the proposed trade (Ticker, Shares, Stop, Target) alongside single-tap **Approve** and **Dismiss** buttons.

    - *Emergency Flatten Button*: A dedicated red control that sends an authenticated cryptographic payload (POST /api/emergency-flatten), canceling all open orders and liquidating open day-trading inventory immediately.

- **Biometric Safeguard**:

  - Any destructive action sent from the widget (such as manual order approval or emergency flattening) triggers Android's native BiometricPrompt on your Pixel 9a before transmitting the authenticated directive to the backend.

### Actionable Next Steps & Verification Checklist

1.  **Verify Sandbox Cash Readiness**

    - Confirm that your Individual account (...015) retains **\$1,000.01 in cleared cash** to serve as the active bankroll for strategy initialization.

2.  **Maintain Primary Swing Lockup**

    - Keep your 260928 holdings in **SOXL** (8.8200 sh), **TQQQ** (9.7465 sh), and **TNA** (8.4388 sh) completely untouched through 4:00 PM EDT on Thursday (**261001**) to maintain full Federal Regulation T compliance.

3.  **Stage Freedom Date Execution (261002)**

    - Set a calendar alert for **261002 at 09:30 AM EDT**, when all three primary swing positions become fully unrestricted and eligible for liquidation or rotation.
