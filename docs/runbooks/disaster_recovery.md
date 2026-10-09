# Schwab Engine - Disaster Recovery & Failover Runbook

This document provides step-by-step procedures to recover from critical systemic failures in the `schwab_engine` production environment.

## 1. Midday Flatten Failure (15:50 EDT)

**Symptom**: The unconditional 15:50 EDT `POWER_HOUR` liquidation routine fails to execute, leaving positions open into the overnight session.
**Impact**: Extreme overnight gap risk, potential margin calls, and violation of the intraday strategy mandate.

**Resolution Steps**:
1. **Trigger Emergency Liquidation via Dashboard**:
   - Immediately access the web dashboard and click the **"Liquidate All"** button. This will write an atomic `LIQUIDATE_ALL` action to `state/control_signal.json`.
2. **Verify Trader Acknowledgment**:
   - Monitor `logs/engine.log`. Ensure the `schwab-trader` daemon logs `Acknowledged LIQUIDATE_ALL control signal`.
3. **Manual CLI Fallback** (If Trader Daemon is Unresponsive):
   - If the dashboard fails or the trader daemon is frozen, SSH into the VM:
   ```bash
   sudo systemctl stop schwab-trader.service
   python main.py --liquidate-now
   ```
4. **Brokerage Fallback**:
   - If the API is completely down, log in to the Schwab web portal or thinkorswim immediately to manually flatten the portfolio before the 16:00 EDT bell.

## 2. Token Invalidation / OAuth Desync

**Symptom**: Repeated `401 Unauthorized` or `400 Bad Request` errors in the logs. The WebSocket stream drops and cannot reconnect with "Invalid token" messages.
**Impact**: Complete loss of trading capability and market data.

**Resolution Steps**:
1. **Halt Trading Operations**:
   - Pause the engine to prevent infinite retry loops that could trigger a rate-limit ban.
   ```bash
   sudo systemctl stop schwab-trader.service
   ```
2. **Re-authenticate**:
   - The refresh token has likely expired (7-day lifespan) or become desynced.
   - Run the manual authentication script to acquire a fresh OAuth token pair:
   ```bash
   python main.py --authenticate
   ```
   - Follow the prompt to paste the Schwab callback URL.
3. **Restart the Trader**:
   - Once the new tokens are saved securely, restart the daemon:
   ```bash
   sudo systemctl start schwab-trader.service
   ```
4. **Verify Telemetry**:
   - Check `logs/engine.log` to ensure the WebSocket streamer successfully connects using the newly minted access token.

## 3. Database Corruption Recovery

**Symptom**: `sqlite3.DatabaseError: database disk image is malformed` when querying `state/trades.db` or `state/market_baselines.db`.
**Impact**: Inability to hydrate intraday execution state or evaluate walk-forward optimizations.

**Resolution Steps**:

### Rebuilding `trades.db`
1. **Stop all Services**:
   ```bash
   sudo systemctl stop schwab-trader.service schwab-web.service
   ```
2. **Attempt WAL Recovery**:
   - SQLite WAL mode is highly resilient. Often, simply backing up the main DB file and allowing SQLite to replay the WAL fixes the issue.
   ```bash
   cd /opt/schwab_engine/state
   sqlite3 trades.db ".recover" | sqlite3 trades_recovered.db
   ```
3. **Restore**:
   ```bash
   mv trades.db trades.db.corrupt
   mv trades_recovered.db trades.db
   ```
4. **Start Web Service & Verify**:
   - Start the web service and ensure the dashboard loads historical trades correctly:
   ```bash
   sudo systemctl start schwab-web.service
   ```
5. **Start Trader**:
   ```bash
   sudo systemctl start schwab-trader.service
   ```

### Rebuilding `market_baselines.db`
If `market_baselines.db` is corrupt, it can be entirely regenerated from the Schwab REST APIs (assuming quota permits).
1. **Stop Services**:
   ```bash
   sudo systemctl stop schwab-trader.service schwab-backfill.service
   ```
2. **Delete Corrupt File**:
   ```bash
   rm state/market_baselines.db
   ```
3. **Trigger Manual Backfill**:
   - Note: This will consume REST API quotas (Max 3,500/day).
   ```bash
   python workers/off_hours_backfill.py --force
   ```
4. **Restart Services**:
   ```bash
   sudo systemctl start schwab-trader.service
   ```

## 4. Single-Streamer Violation (WebSocket Rate Limit)

**Symptom**: Connection abruptly terminated by Charles Schwab. Reconnection attempts fail with `409 Conflict` or `429 Too Many Requests`.
**Impact**: The engine cannot receive live market data.

**Resolution Steps**:
1. **Enforce Daemon Exclusivity**:
   - Ensure that no other processes (e.g., local test scripts or zombie processes) are holding a WebSocket connection.
   ```bash
   ps aux | grep main.py
   ```
   - Kill any orphaned Python processes.
2. **Wait for Timeout**:
   - Schwab may enforce a temporary cooldown if the single-streamer rule was repeatedly violated. Wait 5 minutes.
3. **Restart the Trader**:
   ```bash
   sudo systemctl restart schwab-trader.service
   ```
