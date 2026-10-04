# SchwabEngine Complete System Walkthrough

This document provides a comprehensive walkthrough of the **SchwabEngine** ecosystem, reflecting the transition to an institutional-grade, asynchronous Event-Driven Architecture (EDA). It encompasses the deterministic Tier 1 execution engine, the Tier 2 Vertex AI Governor, and the real-time Vite/React telemetry dashboard.

---

## 1. Core Architecture Overview

The system operates on a bifurcated intelligence model:
*   **Tier 1 (Deterministic Execution):** A strictly deterministic, high-speed Python backend running an `asyncio` event loop. It is solely responsible for routing orders, protecting capital, enforcing SEC T+1 settlement rules, and managing websocket streams.
*   **Tier 2 (AI Governor):** An off-market, asynchronous daemon powered by Google Cloud Vertex AI (Gemini 2.5 Flash/Pro). It provides macroeconomic context and strategic bias without ever blocking the Tier 1 execution thread.

---

## 2. Tier 1: Event-Driven Execution Engine

The core execution layer has been rewritten from a procedural script into a highly concurrent **EventBus Architecture**.

### The `EventBus` (`main.py`)
All components are decoupled. Producers push data onto the bus, and Consumers process it asynchronously.
- **SchwabStreamer (`core/streamer.py`):** Acts as the primary Producer. Connects to the Charles Schwab WebSocket API, parses `LEVELONE_EQUITIES` payloads for the managed universe (SOXL, TQQQ, TNA), and publishes strongly-typed `MarketEvent` Pydantic models.
- **TradingSystem (Consumer):** Listens to the bus and orchestrates risk, capital allocation, and order routing.

### Settlement Ledger & T+1 Temporal Locks (`core/ledger.py`)
To maximize capital velocity without triggering SEC Good Faith Violations (GFVs), the ledger isolates capital into three strict buckets:
- **Bucket 1 (Settled):** Safe for immediate day-trading.
- **Bucket 2 (Unsettled):** Proceeds from intraday sales. The engine algorithmically applies **Temporal Locks** to these funds—blocking them from algorithmic liquidation loops until they clear NSCC settlement at 09:00 EDT the following day.
- **Bucket 3 (Pending):** In-transit ACH deposits (excluded from risk metrics).

### Bayesian Quarter-Kelly Sizing (`execution/risk_manager.py`)
Order sizing dynamically scales based on conviction and volatility. The system applies Bayesian shrinkage to historical win-rates and uses inverse Normalized Average True Range (NATR) to dictate final share quantities, ensuring a strict 1.0% single-trade risk ceiling.

### Almgren-Chriss Trajectory Slicing (`execution/router.py` & `execution/order_client.py`)
Large meta-orders are no longer fired blindly at the ask. 
- The `ExecutionRouter` chops target quantities using a hyperbolic sine function to minimize market impact.
- The new `SchwabOrderClient` converts these slices into live REST JSON payloads, dynamically mapping them to `LIMIT` orders with `RELATIVE` links (`priceLinkBasis: "MARKET_AVERAGE"`) to capture the spread (Pegged-to-Midpoint).
- *Safety Feature:* The client operates with a strict `live_trading=False` dry-run toggle by default, intercepting outbound HTTP requests and logging the JSON payloads safely to the terminal.

---

## 3. Tier 2: Asynchronous AI Governor (`governor.py`)

The Tier 2 Governor acts as the quantitative researcher for the engine.

### Vertex AI Integration
The engine uses the official `google-genai` Python SDK, securely pointing to the `gen-lang-client-0334702303` quota project.
- **Pre-Market Macro Routine (08:35 EDT):** Uses `gemini-2.5-flash` to parse pre-market index futures (SPY, QQQ, IWM). It outputs a strict Pydantic JSON schema dictating whether the day should operate under Regime A (Opening Range Breakout) or Regime C (VWAP Mean Reversion).
- **Post-Market Reflection (16:15 EDT):** Uses `gemini-2.5-pro` to analyze daily fill logs, slippage, and PnL, archiving strategic feedback.

### Atomic State Synchronization
To prevent race conditions with the highly concurrent Tier 1 engine, the Governor writes its JSON payload to a temporary file (`strategy_config.json.tmp`) and swaps it using an OS-level atomic replace (`os.replace`). 

---

## 4. Real-Time Telemetry Dashboard (Vite / React)

The frontend `src/` has been optimized to render the complexities of the backend in a clean, institutional interface.

### Dynamic Ledger & Risk Bounds
The `LedgerCard.tsx` strictly tracks the **$3,747.50 Total NLV**. It visually renders the 3-bucket architecture, featuring an active padlock icon over Bucket 2 to indicate T+1 Temporal Locks, and explicitly maps out the $749.50 (20%) single-ticker capacity.

### Execution Visualizer & LLM Insights
The Overview tab features custom-built panels to audit the engine's real-time state:
- **Lifecycle Tracker (`LifecycleTracker.tsx`):** A pulsing timeline mapping the system's exact phase: *Preparation -> Action -> Recovery -> Reflection*.
- **LLM Intelligence Console (`LlmInsightConsole.tsx`):** Exposes the Tier 2 Governor. Users can view the exact system prompt dispatched to Vertex AI, the structured JSON response, and inference latency.
- **Active Execution Trajectory:** Tracks Almgren-Chriss child-order progress (e.g., "2 of 4 Executed") alongside the live Bayesian Quarter-Kelly confidence metric.

### Wash-Sale Pivot Triggers
The `EtfPositionTracker.tsx` monitors the managed high-beta ETF universe. It handles advanced status flags natively, replacing standard guards with a pulsing amber `ALGMREN-CHRISS TRIM ACTIVE` badge when a position is actively seeking exit liquidity.

---

## 5. Security & Authentication

### The Schwab OAuth Vault (`core/auth.py` & `manual_auth.py`)
- Authentication operates via a local loopback callback (`https://127.0.0.1:5556`).
- Upon extracting the authorization code from the Charles Schwab login portal, `manual_auth.py` exchanges the code for a 7-day refresh token.
- Tokens are heavily encrypted into `schwab_tokens_vault.json` using AES-256-GCM. The encryption key is derived using PBKDF2HMAC (600,000 iterations) from the local `.env` passphrase.

## 6. How to Run

1. **Backend Engine:**
   Ensure you are in `c:\Projects\schwab_engine` and run the main entrypoint:
   ```bash
   python main.py
   ```
2. **Frontend Dashboard:**
   Start the Vite dev server in a parallel terminal:
   ```bash
   npm run dev
   ```
   The application will compile and mount at `http://localhost:3000` (or `3001` if occupied).
