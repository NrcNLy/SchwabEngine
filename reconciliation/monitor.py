"""
reconciliation/monitor.py
==========================
External Order & Position Reconciliation Monitor.

Polls Schwab every 30 seconds and reconciles the broker's live state
against the engine's internal position/order tracking. Handles three
scenarios that arise when orders are placed or changed directly in the
Schwab app or website (outside the engine):

Scenario 1 — PHANTOM POSITION
    Engine tracks an ActivePosition that the broker no longer shows.
    Cause: user manually sold a position in the Schwab app.
    Action: Remove from active_positions, update ledger, fire FCM alert.
    Risk without this: engine's Tier 1 fires a duplicate sell → short position.

Scenario 2 — UNMANAGED POSITION
    Broker shows a position the engine never opened.
    Cause: user bought something manually in the Schwab app.
    Action: Log as UNMANAGED, optionally place a protective Tier 2 stop,
            fire FCM alert. Portfolio manager will generate take-profit advisory.
    Risk without this: position is completely unprotected by the engine.

Scenario 3 — ORPHAN ORDER
    Engine's _pending_orders references an order that's no longer at the broker.
    Cause: user cancelled a pending order directly in the Schwab app.
    Action: Mark finalized, clean up pending dict.
    Risk without this: engine waits forever for a fill that will never come.

Configuration (config.yaml):
    reconciliation.enabled               → true
    reconciliation.poll_interval_sec     → 30
    reconciliation.protect_unmanaged     → true  (place Tier 2 stop on foreign positions)
    reconciliation.unmanaged_stop_pct    → 0.05  (5% below avg cost as emergency stop)
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

import pytz

logger = logging.getLogger(__name__)
_EDT = pytz.timezone("America/New_York")


@dataclass
class UnmanagedPosition:
    """A position detected at the broker that the engine did not open."""
    symbol:        str
    quantity:      int
    avg_cost:      float
    current_price: float
    detected_at:   datetime
    stop_order_id: Optional[str] = None   # Protective stop placed by engine (if any)


class ExternalOrderMonitor:
    """
    Background reconciliation thread: 30-second poll, handles all three
    external-change scenarios.

    Thread Safety
    -------------
    All mutable state is protected by _lock (RLock). Calls into
    risk_manager and order_manager are made outside the lock to avoid
    deadlock, consistent with their own internal locking.
    """

    def __init__(
        self,
        rest_client,
        risk_manager,
        order_manager,
        ledger,
        dispatcher,
        cfg: dict,
    ) -> None:
        self._rest          = rest_client
        self._risk_manager  = risk_manager
        self._order_manager = order_manager
        self._ledger        = ledger
        self._dispatcher    = dispatcher

        recon_cfg = cfg.get("reconciliation", {})
        self._enabled:          bool  = recon_cfg.get("enabled", True)
        self._poll_interval:    float = float(recon_cfg.get("poll_interval_sec", 30.0))
        self._protect_unmanaged: bool = recon_cfg.get("protect_unmanaged", True)
        self._unmanaged_stop_pct: float = float(recon_cfg.get("unmanaged_stop_pct", 0.05))

        # Track known unmanaged positions so we don't re-alert on every poll
        self._known_unmanaged: Dict[str, UnmanagedPosition] = {}
        self._lock = threading.RLock()

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        logger.info(
            "ExternalOrderMonitor initialised — poll=%ds, protect_unmanaged=%s, "
            "unmanaged_stop=%.0f%%",
            self._poll_interval, self._protect_unmanaged,
            self._unmanaged_stop_pct * 100,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if not self._enabled:
            logger.info("ExternalOrderMonitor: disabled by config.")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="ReconciliationMonitorThread",
            daemon=True,
        )
        self._thread.start()
        logger.info("ExternalOrderMonitor: background thread started.")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("ExternalOrderMonitor: stopped.")

    def get_unmanaged_positions(self) -> List[UnmanagedPosition]:
        """Return current list of detected unmanaged positions (for API/dashboard)."""
        with self._lock:
            return list(self._known_unmanaged.values())

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        """Main background loop."""
        logger.info("ExternalOrderMonitor: polling loop started.")
        while not self._stop_event.wait(timeout=self._poll_interval):
            try:
                self._reconcile()
            except Exception as exc:  # noqa: BLE001
                logger.warning("ExternalOrderMonitor: reconcile cycle error: %s", exc)

    def _reconcile(self) -> None:
        """Single reconciliation cycle."""
        account_hash = self._order_manager.account_hash
        if not account_hash:
            return   # Firewall not yet passed

        # Fetch live broker state
        try:
            accounts = self._rest.get_accounts(fields="positions")
        except Exception as exc:
            logger.warning("ExternalOrderMonitor: failed to fetch accounts: %s", exc)
            return

        broker_positions: Dict[str, Dict[str, Any]] = {}
        for acct in accounts:
            positions = acct.get("securitiesAccount", {}).get("positions", [])
            for pos in positions:
                sym = pos.get("instrument", {}).get("symbol", "").upper()
                qty = float(pos.get("longQuantity", 0))
                if sym and qty > 0:
                    broker_positions[sym] = {
                        "quantity":      int(qty),
                        "avg_cost":      float(pos.get("averagePrice", 0)),
                        "current_value": float(pos.get("marketValue", 0)),
                    }

        # Get engine's tracked positions
        engine_positions: Set[str] = set(
            self._risk_manager._active_positions.keys()
        )

        broker_symbols: Set[str] = set(broker_positions.keys())

        # --- Scenario 1: PHANTOM POSITIONS ---
        # Engine tracks symbols that no longer exist at the broker
        phantoms = engine_symbols_at_broker_gone = engine_positions - broker_symbols
        for sym in phantoms:
            logger.warning(
                "ExternalOrderMonitor: PHANTOM POSITION detected — %s "
                "is in engine state but not at broker. User likely closed manually.",
                sym,
            )
            pos = self._risk_manager._active_positions.get(sym)
            if pos:
                # Remove from engine tracking
                with self._risk_manager._lock:
                    pos.tier1_triggered = True   # Prevent any further exit attempts
                    self._risk_manager._active_positions.pop(sym, None)

                # Estimate proceeds for ledger (use last known stop as conservative fill)
                try:
                    self._ledger.process_fill_sell(sym, pos.quantity, pos.current_stop)
                except Exception as exc:
                    logger.warning(
                        "ExternalOrderMonitor: ledger sell failed for phantom %s: %s",
                        sym, exc
                    )

                if self._dispatcher:
                    self._dispatcher._send("PHANTOM_POSITION_CLEARED", {
                        "symbol":   sym,
                        "quantity": pos.quantity,
                        "reason":   "Position closed externally (not by engine)",
                    })
                logger.info(
                    "ExternalOrderMonitor: PHANTOM %s cleared from engine state.", sym
                )

        # --- Scenario 2: UNMANAGED POSITIONS ---
        # Broker has positions the engine never opened
        universe = set(self._risk_manager._order_manager._rest.__class__.__name__ and [])  # noqa
        # Get engine's known universe symbols from the active mask
        with self._lock:
            known_unmanaged_syms = set(self._known_unmanaged.keys())

        unmanaged_syms = broker_symbols - engine_positions - known_unmanaged_syms

        for sym in unmanaged_syms:
            pos_data = broker_positions[sym]
            qty = pos_data["quantity"]
            avg_cost = pos_data["avg_cost"]

            logger.warning(
                "ExternalOrderMonitor: UNMANAGED POSITION detected — %s ×%d "
                "avg_cost=$%.2f (not opened by engine).",
                sym, qty, avg_cost,
            )

            unmanaged = UnmanagedPosition(
                symbol=sym,
                quantity=qty,
                avg_cost=avg_cost,
                current_price=avg_cost,  # Will be updated by price feed
                detected_at=datetime.now(_EDT),
            )

            # Optionally place a protective stop
            if self._protect_unmanaged and avg_cost > 0:
                stop_price = round(avg_cost * (1.0 - self._unmanaged_stop_pct), 2)
                try:
                    stop_id = self._order_manager.place_broker_stop(
                        symbol=sym, quantity=qty, stop_price=stop_price
                    )
                    unmanaged.stop_order_id = stop_id
                    logger.info(
                        "ExternalOrderMonitor: protective stop placed for %s "
                        "×%d @ $%.2f (order_id=%s)",
                        sym, qty, stop_price, stop_id,
                    )
                except Exception as exc:
                    logger.error(
                        "ExternalOrderMonitor: FAILED to place protective stop "
                        "for %s: %s", sym, exc
                    )

            with self._lock:
                self._known_unmanaged[sym] = unmanaged

            if self._dispatcher:
                self._dispatcher._send("EXTERNAL_POSITION_DETECTED", {
                    "symbol":        sym,
                    "quantity":      qty,
                    "avg_cost":      avg_cost,
                    "stop_placed":   unmanaged.stop_order_id is not None,
                    "stop_order_id": unmanaged.stop_order_id or "",
                })

        # --- Scenario 3: ORPHAN PENDING ORDERS ---
        try:
            broker_working_ids = {
                str(o.get("orderId", ""))
                for o in self._rest.get_orders(account_hash, status="WORKING")
            }
        except Exception as exc:
            logger.warning(
                "ExternalOrderMonitor: failed to fetch working orders: %s", exc
            )
            return

        with self._risk_manager._lock:
            pending_ids = list(self._risk_manager._pending_orders.keys())

        for order_id in pending_ids:
            if order_id not in broker_working_ids:
                pending = self._risk_manager._pending_orders.get(order_id)
                if pending and not pending.finalized:
                    logger.info(
                        "ExternalOrderMonitor: ORPHAN order %s for %s is no longer "
                        "WORKING at broker — marking finalized (user likely cancelled).",
                        order_id, pending.symbol,
                    )
                    with self._risk_manager._lock:
                        pending.finalized = True
                        self._risk_manager._pending_orders.pop(order_id, None)

        # Clean up unmanaged positions that are no longer at the broker
        with self._lock:
            gone = [s for s in self._known_unmanaged if s not in broker_symbols]
            for s in gone:
                logger.info(
                    "ExternalOrderMonitor: unmanaged position %s is gone "
                    "from broker — removing from tracking.", s
                )
                self._known_unmanaged.pop(s, None)
