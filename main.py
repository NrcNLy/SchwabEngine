"""
main.py
=======
Schwab Automated Day-Trading Engine — Application Entry Point.

Phase 2: SecurityVault, SchwabAuthManager, WeekendOAuthDaemon,
         SchwabRestClient, SchwabStreamer.
Phase 3: ComplianceLedger, UniverseExclusionMask, LedgerScheduler.
Phase 4: StrategyEngine (RegimeClassifier, 15m ORB, VWAP MR, Quarter-Kelly sizing).
Phase 5: MacroGatekeeper (temporal lockouts, Gemini 2.5 Flash, semantic cache).
Phase 6: OrderManager (Account Firewall), RiskManager (Dual-Tier Stops, Drought, 3:55 PM Sweep).

Later phases will extend this module with FCM telemetry (Phase 7).

Usage
-----
    python main.py
"""

from __future__ import annotations

import logging
import os
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

# ---------------------------------------------------------------------------
# Logging setup — configure before importing engine modules
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("schwab_engine.main")

# ---------------------------------------------------------------------------
# Resolve project root and config path
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH  = PROJECT_ROOT / "config" / "config.yaml"

sys.path.insert(0, str(PROJECT_ROOT))


# ===========================================================================
# Config loader
# ===========================================================================

def load_config() -> dict:
    """Load and return the master configuration from config.yaml."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"config.yaml not found at {CONFIG_PATH}")
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    logger.info("Configuration loaded from %s", CONFIG_PATH)
    return cfg


# ===========================================================================
# Phase 2 — Authentication & Networking
# ===========================================================================

def build_auth_and_network(cfg: dict):
    """
    Instantiate and wire Phase 2 components.

    Credentials sourced from environment variables:
        SCHWAB_CLIENT_ID      — Schwab Developer App Client ID
        SCHWAB_CLIENT_SECRET  — Schwab Developer App Client Secret
        VAULT_PASSPHRASE      — Master passphrase for SecurityVault encryption
        OAUTH_SSL_CERT        — (Optional) Path to PEM TLS cert for loopback server
        OAUTH_SSL_KEY         — (Optional) Path to PEM TLS key for loopback server

    Returns:
        (auth_manager, rest_client, streamer, weekend_daemon)
    """
    from core.auth import SecurityVault, SchwabAuthManager, WeekendOAuthDaemon
    from data.rest_client import SchwabRestClient
    from data.streamer import SchwabStreamer

    client_id     = os.environ.get("SCHWAB_CLIENT_ID", "")
    client_secret = os.environ.get("SCHWAB_CLIENT_SECRET", "")
    passphrase    = os.environ.get("VAULT_PASSPHRASE", "")

    if not all([client_id, client_secret, passphrase]):
        logger.warning(
            "SCHWAB_CLIENT_ID / SCHWAB_CLIENT_SECRET / VAULT_PASSPHRASE not set. "
            "Running in stub mode — token operations will fail."
        )

    # --- Security Vault ---
    auth_cfg   = cfg.get("auth", {})
    vault_file = PROJECT_ROOT / auth_cfg.get("vault_file", "schwab_tokens_vault.json")
    iterations = int(auth_cfg.get("pbkdf2_iterations", 600_000))

    vault = SecurityVault(
        passphrase=passphrase,
        iterations=iterations,
        vault_path=vault_file,
    )

    # --- Auth Manager ---
    auth_manager = SchwabAuthManager(
        client_id=client_id,
        client_secret=client_secret,
        vault=vault,
        cfg=cfg,
    )

    try:
        auth_manager.load_tokens()
    except FileNotFoundError:
        logger.warning(
            "Token vault not found — initial OAuth flow required. "
            "WeekendOAuthDaemon will guide you through authorization."
        )

    auth_manager.start()

    # --- Weekend OAuth Daemon ---
    ssl_cert = os.environ.get("OAUTH_SSL_CERT")
    ssl_key  = os.environ.get("OAUTH_SSL_KEY")

    weekend_daemon = WeekendOAuthDaemon(
        auth_manager=auth_manager,
        client_id=client_id,
        cfg=cfg,
        ssl_cert_path=ssl_cert,
        ssl_key_path=ssl_key,
    )
    weekend_daemon.start()

    # --- REST Client ---
    rest_client = SchwabRestClient.from_config(cfg, auth_manager)

    # --- WebSocket Streamer ---
    # Symbols are initially set from config; updated by UniverseExclusionMask at 09:15
    streamer = SchwabStreamer.from_config(cfg, rest_client)

    return auth_manager, rest_client, streamer, weekend_daemon


# ===========================================================================
# Phase 3 — Compliance Ledger & Wash-Sale Masking
# ===========================================================================

def build_compliance_layer(cfg: dict, rest_client) -> tuple:
    """
    Instantiate and wire Phase 3 components.

    The broker_sync_fn is constructed here to close over `rest_client` and
    the configured account suffix, providing the LedgerScheduler with a
    clean callable that returns (cash_balance, positions_list).

    Returns:
        (ledger, mask, scheduler)
    """
    from core.ledger import build_from_config as build_ledger
    from data.rest_client import SchwabRestClient

    required_suffix: str = cfg.get("account", {}).get("required_suffix", "015")

    def broker_sync_fn() -> Tuple[Decimal, List[Dict[str, Any]]]:
        """
        Query the Schwab REST API for the whitelisted account's current
        cashBalance and open positions list.

        Called by LedgerScheduler at 09:15 AM EDT.

        Returns:
            (cash_balance: Decimal, positions: list[dict])
            Each position dict contains at minimum "symbol" and "quantity".
        """
        # Fetch account hash for the whitelisted account
        account_numbers = rest_client.get_account_numbers()
        target_hash: str = ""
        for entry in account_numbers:
            acct_num = entry.get("accountNumber", "")
            if acct_num.endswith(required_suffix):
                target_hash = entry.get("hashValue", "")
                break

        if not target_hash:
            logger.error(
                "broker_sync_fn: no account ending in '%s' found — "
                "cannot sync ledger.", required_suffix,
            )
            return Decimal("0.00"), []

        # Fetch full account data including positions
        accounts = rest_client.get_accounts(fields="positions")
        cash_balance = Decimal("0.00")
        positions: List[Dict[str, Any]] = []

        for acct in accounts:
            inner = acct.get("securitiesAccount", acct)
            acct_num = inner.get("accountNumber", "")
            if not acct_num.endswith(required_suffix):
                continue

            # Extract cashBalance from the current balances block
            current_balances = inner.get("currentBalances", {})
            raw_cash = current_balances.get("cashBalance", 0.0)
            cash_balance = Decimal(str(raw_cash))

            # Extract positions with symbol and quantity
            for pos in inner.get("positions", []):
                instrument  = pos.get("instrument", {})
                symbol      = instrument.get("symbol", "")
                long_qty    = float(pos.get("longQuantity", 0))
                acquired    = pos.get("acquiredDate", "")   # May be absent

                if symbol and long_qty > 0:
                    positions.append({
                        "symbol":        symbol,
                        "quantity":      long_qty,
                        "acquired_date": acquired,  # "" if not provided by API
                    })
            break

        logger.info(
            "broker_sync_fn: cashBalance=$%.2f, %d open positions fetched.",
            cash_balance, len(positions),
        )
        return cash_balance, positions

    ledger, mask, scheduler = build_ledger(cfg, broker_sync_fn=broker_sync_fn)
    return ledger, mask, scheduler


# ===========================================================================
# Phase 6 — Execution Engine
# ===========================================================================

def build_execution_layer(cfg: dict, rest_client, ledger):
    """
    Instantiate the OrderManager and RiskManager.
    
    Returns:
        (order_manager, risk_manager)
    """
    from execution.order_manager import OrderManager
    from execution.risk_manager import build_from_config as build_risk_manager
    
    # 1. Initialize OrderManager and run firewall check
    order_manager = OrderManager(rest_client, ledger, cfg)
    order_manager.initialize_firewall()
    
    # 2. Initialize RiskManager
    risk_manager = build_risk_manager(cfg, order_manager, rest_client, ledger)
    
    return order_manager, risk_manager

# ===========================================================================
# Phase 4 — Strategy Engine
# ===========================================================================

def build_strategy_layer(cfg: dict, rest_client, ledger, mask, gatekeeper=None, order_manager=None, risk_manager=None, dispatcher=None):
    """
    Instantiate the StrategyEngine, register and preload all symbols,
    and return a wired on_tick callback ready to be attached to the streamer.

    Args:
        gatekeeper: Optional MacroGatekeeper. If provided (Phase 5+), all
                    signals are evaluated by the gatekeeper before proceeding.
        order_manager: Optional OrderManager for executing signals (Phase 6).
        risk_manager: Optional RiskManager for tracking stops and drought (Phase 6).
        dispatcher: Optional TelemetryDispatcher for push alerts (Phase 7).

    Returns:
        (strategy_engine, on_tick_fn)
    """
    from execution.strategies import build_from_config as build_strategies, TradeSignal

    engine = build_strategies(cfg=cfg, ledger=ledger, mask=mask)

    # Register every symbol in the active universe
    for symbol in mask.get_active_universe():
        engine.register_symbol(symbol)
        # Preload 20-day 1m history for accurate RVOL — non-fatal on failure
        engine.preload_historical(symbol, rest_client)

    def on_signal(signal: TradeSignal) -> None:
        """
        Handle a generated trade signal.

        Phase 5 behaviour: route through MacroGatekeeper before proceeding.
            - Temporal lockout → immediate veto.
            - Semantic cache hit → return cached decision instantly.
            - Gemini 2.5 Flash evaluation (1500ms timeout, 8 RPM limit).
            - Degradation: trade_permitted=True, risk_multiplier=0.50 on failure.

        Phase 6 behaviour:
            - Calculate final quantity using Gemini risk multiplier.
            - Route to OrderManager.place_limit_buy.
            - Register pending order with RiskManager.
            
        Phase 7 behaviour:
            - Broadcast SIGNAL_GENERATED via TelemetryDispatcher.
        """
        logger.info(
            "SIGNAL %-10s | %s %s ×%d @ $%.2f | "
            "stop=$%.2f target=$%.2f | regime=%s",
            signal.strategy,
            signal.symbol,
            signal.direction,
            signal.quantity,
            float(signal.entry_price),
            float(signal.stop_price),
            float(signal.target_price),
            signal.regime.name,
        )

        if gatekeeper is None:
            # Phase 4 fallback: no gatekeeper wired yet
            logger.info(
                "SIGNAL APPROVED (no gatekeeper) — Phase 6 will route to order_manager."
            )
            return

        # --- Phase 5: MacroGatekeeper evaluation ---
        signal_ctx = {
            "symbol":       signal.symbol,
            "strategy":     signal.strategy,
            "regime":       signal.regime.name,
            "entry_price":  float(signal.entry_price),
            "stop_price":   float(signal.stop_price),
            "target_price": float(signal.target_price),
            "quantity":     signal.quantity,
        }

        decision = gatekeeper.evaluate(signal_context=signal_ctx)

        if not decision.trade_permitted:
            logger.warning(
                "SIGNAL VETOED by MacroGatekeeper | %s | reason=%s | "
                "warning=%s",
                signal.symbol,
                decision.inhibition_reason,
                decision.high_impact_warning,
            )
            return

        logger.info(
            "SIGNAL APPROVED by MacroGatekeeper | %s | "
            "regime=%s bias=%s confidence=%.2f risk_mult=%.2f",
            signal.symbol,
            decision.macro_regime,
            decision.market_bias,
            decision.confidence_score,
            decision.risk_multiplier,
        )
        
        # --- Phase 6: Order Execution ---
        if order_manager and risk_manager:
            final_qty = int(signal.quantity * decision.risk_multiplier)
            if final_qty <= 0:
                logger.warning(
                    "SIGNAL ABORTED | %s | risk_multiplier (%.2f) reduced quantity to 0",
                    signal.symbol, decision.risk_multiplier
                )
                return
                
            try:
                # Phase 7: Send Telemetry Alert
                if dispatcher:
                    dispatcher.send_signal_generated({
                        "symbol": signal.symbol,
                        "strategy": signal.strategy,
                        "qty": final_qty,
                        "entry_price": float(signal.entry_price),
                        "macro_regime": decision.macro_regime
                    })
                    
                # 1. Place the limit buy order
                order_id = order_manager.place_limit_buy(
                    symbol=signal.symbol,
                    quantity=final_qty,
                    limit_price=float(signal.entry_price)
                )
                
                # 2. Register for fill monitoring and Tier 2 placement
                risk_manager.register_pending_order(
                    order_id=order_id,
                    signal_data={
                        "symbol": signal.symbol,
                        "quantity": final_qty,
                        "entry_price": float(signal.entry_price),
                        "stop_price": float(signal.stop_price),
                        "target_price": float(signal.target_price),
                        "atr14_dollars": float(signal.metrics.atr14_dollars),
                    }
                )
            except Exception as e:
                logger.error("Execution failed for %s: %s", signal.symbol, e)

    engine.set_signal_callback(on_signal)

    def on_tick(symbol: str, fields: dict) -> None:
        """
        WebSocket tick callback — routes ticks through the strategy engine
        and risk manager.
        """
        engine.on_tick(symbol, fields)
        if risk_manager:
            risk_manager.on_tick(symbol, fields)

    return engine, on_tick

# ===========================================================================
# Phase 5 — Macro Gatekeeper
# ===========================================================================

def build_macro_layer(cfg: dict):
    """
    Instantiate the MacroSemanticCache and MacroGatekeeper.

    Must be called BEFORE build_strategy_layer so the gatekeeper reference
    can be passed into the signal callback closure.

    Returns:
        (gatekeeper, cache)
    """
    from macro.gatekeeper import build_from_config as build_gatekeeper

    gatekeeper, cache = build_gatekeeper(cfg)
    return gatekeeper, cache


# ===========================================================================
# Phase 7 — Telemetry
# ===========================================================================

def build_telemetry_layer(cfg: dict):
    from telemetry.dispatcher import TelemetryDispatcher
    return TelemetryDispatcher.from_config(cfg)


# ===========================================================================
# Top-level engine builder
# ===========================================================================

def build_engine(cfg: dict):
    """
    Build and wire all engine components through Phase 7.

    Returns:
        (auth_manager, rest_client, streamer, weekend_daemon,
         ledger, mask, scheduler, strategy_engine, gatekeeper, cache,
         order_manager, risk_manager, dispatcher)
    """
    # Phase 2 — Auth & networking
    auth_manager, rest_client, streamer, weekend_daemon = build_auth_and_network(cfg)

    # Pause engine initialization if initial OAuth token is missing
    if not auth_manager.has_refresh_token():
        logger.warning(
            "Initial authorization required. WeekendOAuthDaemon listening on port 5556. Waiting for user authorization..."
        )
        while not auth_manager.has_refresh_token():
            time.sleep(2)
        logger.info("OAuth token received! Proceeding with engine initialization.")

    # Phase 3 — Compliance ledger & wash-sale mask
    ledger, mask, scheduler = build_compliance_layer(cfg, rest_client)

    # Phase 5 — Macro gatekeeper
    gatekeeper, cache = build_macro_layer(cfg)
    
    # Phase 7 — Telemetry
    dispatcher = build_telemetry_layer(cfg)
    
    # Phase 6 — Execution engine
    order_manager, risk_manager = build_execution_layer(cfg, rest_client, ledger)
    # Inject dispatcher into risk manager (already updated in risk_manager.py signature)
    risk_manager._dispatcher = dispatcher

    # Phase 4 — Strategy engine with gatekeeper-gated signal callback
    # Passes order_manager, risk_manager, and dispatcher for execution routing/alerts.
    strategy_engine, on_tick = build_strategy_layer(
        cfg, rest_client, ledger, mask, gatekeeper=gatekeeper,
        order_manager=order_manager, risk_manager=risk_manager,
        dispatcher=dispatcher
    )

    # Wire the active post-mask universe into the streamer
    active_universe = mask.get_active_universe()
    streamer.set_symbols(active_universe)
    streamer.on_tick = on_tick

    return (
        auth_manager,
        rest_client,
        streamer,
        weekend_daemon,
        ledger,
        mask,
        scheduler,
        strategy_engine,
        gatekeeper,
        cache,
        order_manager,
        risk_manager,
        dispatcher,
    )


# ===========================================================================
# Entry point
# ===========================================================================

def main() -> None:
    cfg = load_config()

    (
        auth_manager,
        rest_client,
        streamer,
        weekend_daemon,
        ledger,
        mask,
        scheduler,
        strategy_engine,
        gatekeeper,
        cache,
        order_manager,
        risk_manager,
        dispatcher,
    ) = build_engine(cfg)

    # Phase 3: compliance scheduler
    logger.info("Starting LedgerScheduler…")
    scheduler.start()
    
    # Phase 6: risk manager polling loop
    logger.info("Starting RiskManager polling…")
    risk_manager.start()

    # Phase 2: WebSocket market data stream
    logger.info("Starting WebSocket streamer…")
    streamer.start()

    snap = ledger.snapshot()
    logger.info(
        "Schwab Engine Phase 7 running — "
        "B1=$%.2f | universe=%s | cache_entries=%d | press Ctrl+C to exit.",
        snap.bucket1_settled,
        mask.get_active_universe(),
        cache.entry_count,
    )

    # Phase 7: Startup diagnostic telemetry
    dispatcher.send_pre_market_diagnostic({
        "status": "ONLINE",
        "b1_settled": snap.bucket1_settled,
        "universe_size": len(mask.get_active_universe())
    })

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutdown signal received.")
    finally:
        streamer.stop()
        risk_manager.stop()
        scheduler.stop()
        gatekeeper.shutdown()
        auth_manager.stop()
        weekend_daemon.stop()
        logger.info("Engine stopped cleanly.")

if __name__ == "__main__":
    main()
