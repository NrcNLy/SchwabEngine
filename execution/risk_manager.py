"""
execution/risk_manager.py
=========================
Dual-Tier Stop-Loss Architecture and EOD Liquidation Sweep.

Design Overview
---------------
Every approved trade signal flows through this module after order placement.
The RiskManager maintains all in-memory position state and enforces two
independent (but complementary) stop mechanisms:

Tier 1 — Client-Side Dynamic Trailing Stop
    Runs entirely in memory. Updates on every Level 1 market tick.
    Three transition states:
        INITIAL:    Stop fixed at signal stop price (e.g. ORB low).
        BREAKEVEN:  Stop moved to entry_price when unrealised profit >= +1.0R.
        ATR_TRAIL:  Stop becomes (high_watermark - 1.2 × ATR14) when profit >= +1.5R.
                    Only ever ratchets upward (stop never moves down).
    Trigger: current_price <= current_stop → cancel Tier 2 → market sell.

Tier 2 — Broker-Side Catastrophe Stop
    A resting STOP DAY order placed immediately when the entry fill is confirmed.
    Fixed at entry_price × 0.96 (-4.0% from entry).
    Purpose: protect against process crashes or network failures where
    Tier 1 cannot execute. Leaves shares unencumbered (no OCO brackets).
    Cancelled when Tier 1 triggers (to prevent phantom short positions).

Liquidity Drought (Partial Fill handling)
    When an order is detected as PARTIALLY_FILLED:
        1. Record drought_started timestamp.
        2. After 10 seconds, if leavesQuantity > 0:
            a. DELETE the order (cancel unfilled leaves).
            b. Finalize position at executed_quantity.
            c. Place Tier 2 stop sized to executed_quantity only.

EOD Liquidation Sweep — 15:55:00 EDT
    Background polling thread triggers exactly once per calendar day at 15:55:00 EDT:
        1. cancel_all_open_orders() — cancels Tier 2 stop orders AND any working
           day orders (stale limit buys, etc.).
        2. execute_market_sell() for every symbol in active_positions.
    After the sweep, the engine is 100% cash for overnight.

Configuration sources (config.yaml):
    schedule.eod_liquidation_time     → "15:55:00"
    schedule.timezone                 → "America/New_York"
    risk.tier2_stop_pct               → 0.04  (−4.0% from entry)
    risk.tier1_breakeven_r            → 1.0   (move stop to entry at +1.0R)
    risk.tier1_atr_trail_r            → 1.5   (start ATR trail at +1.5R)
    risk.tier1_atr_trail_multiplier   → 1.2   (stop = HWM − 1.2×ATR)
    risk.drought_timer_sec            → 10    (seconds before drought fires)
    risk.order_poll_interval_sec      → 2     (polling cadence)
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Set

import pytz

logger = logging.getLogger(__name__)

_EDT = pytz.timezone("America/New_York")


# ===========================================================================
# Data Classes
# ===========================================================================

@dataclass
class ActivePosition:
    """
    In-memory representation of a confirmed (filled) day-trading position.
    Tracks Tier 1 stop state and references the Tier 2 broker stop order.
    """
    symbol:          str
    entry_price:     float       # Confirmed fill price
    quantity:        int         # Executed share count (may be < intended on drought)
    initial_stop:    float       # Original stop price from the trade signal
    target_price:    float       # Profit target from the trade signal
    atr14_dollars:   float       # ATR14 in dollars at signal time (for ATR trail)
    fill_timestamp:  datetime    # When the fill was confirmed

    # Tier 2 broker stop
    tier2_order_id:  Optional[str] = None     # Broker stop order ID

    # Tier 1 dynamic state
    high_watermark:   float = 0.0    # Highest price since entry (updated on every tick)
    current_stop:     float = 0.0    # Live Tier 1 stop (starts at initial_stop)
    breakeven_moved:  bool  = False  # Has stop been moved to breakeven (+1.0R)?
    atr_trail_active: bool  = False  # Is the ATR trailing stop active (+1.5R)?
    tier1_triggered:  bool  = False  # Guard: prevent double-trigger

    def __post_init__(self) -> None:
        if self.high_watermark == 0.0:
            self.high_watermark = self.entry_price
        if self.current_stop == 0.0:
            self.current_stop = self.initial_stop


@dataclass
class PendingOrder:
    """
    Tracks an order from submission until fill confirmation.
    Used by the background polling thread for fill detection and drought logic.
    """
    order_id:       str
    symbol:         str
    intended_qty:   int
    signal_entry:   float
    signal_stop:    float
    signal_target:  float
    signal_atr:     float
    submitted_at:   datetime

    # Drought state
    drought_started: Optional[datetime] = None
    last_exec_qty:   int = 0    # Tracks most recent partial fill count
    finalized:       bool = False


# ===========================================================================
# RiskManager
# ===========================================================================

class RiskManager:
    """
    Dual-tier stop-loss engine with EOD liquidation sweep.

    Thread Model
    ------------
    - ``on_tick()``    — called from the WebSocket streamer thread (asyncio loop
                         in daemon thread). Updates Tier 1 stops in real time.
    - Background thread — polls order status every 2 seconds for fill detection,
                          drought handling, and EOD sweep timing.
    Both paths modify ``_active_positions`` and ``_pending_orders``, which are
    protected by ``_lock`` (threading.RLock — reentrant because on_tick paths
    may call into order submission).
    """

    def __init__(
        self,
        order_manager,
        rest_client,
        ledger,
        cfg: dict,
        dispatcher=None,
    ) -> None:
        """
        Args:
            order_manager: Initialised OrderManager (firewall already passed).
            rest_client:   SchwabRestClient (for order status polling).
            ledger:        ComplianceLedger (for fill accounting).
            cfg:           Top-level config dict.
            dispatcher:    Optional TelemetryDispatcher for FCM alerts.
        """
        self._order_manager = order_manager
        self._rest          = rest_client
        self._ledger        = ledger
        self._dispatcher    = dispatcher

        risk_cfg  = cfg.get("risk", {})
        sched_cfg = cfg.get("schedule", {})
        tz_name   = sched_cfg.get("timezone", "America/New_York")
        self._tz  = pytz.timezone(tz_name)

        # Risk parameters
        self._tier2_stop_pct:         float = float(risk_cfg.get("tier2_stop_pct",           0.04))
        self._breakeven_r:            float = float(risk_cfg.get("tier1_breakeven_r",         1.0))
        self._atr_trail_r:            float = float(risk_cfg.get("tier1_atr_trail_r",         1.5))
        self._atr_trail_mult:         float = float(risk_cfg.get("tier1_atr_trail_multiplier",1.2))
        self._drought_timer_sec:      float = float(risk_cfg.get("drought_timer_sec",         10.0))
        self._poll_interval_sec:      float = float(risk_cfg.get("order_poll_interval_sec",   2.0))

        # EOD sweep time
        eod_str  = sched_cfg.get("eod_liquidation_time", "15:55:00")
        eod_parts = [int(x) for x in eod_str.split(":")]
        self._eod_hms: tuple = (eod_parts[0], eod_parts[1], eod_parts[2])

        # State
        self._active_positions: Dict[str, ActivePosition] = {}   # symbol → position
        self._pending_orders:   Dict[str, PendingOrder]   = {}   # order_id → pending
        self._lock = threading.RLock()

        # Background thread
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # EOD sweep deduplication
        self._eod_fired_date: Optional[date] = None

        logger.info(
            "RiskManager initialised — T2_stop=%.0f%%, breakeven@+%.1fR, "
            "ATR_trail@+%.1fR (%.1f×ATR), drought=%.0fs, poll=%.0fs, EOD=%02d:%02d:%02d",
            self._tier2_stop_pct * 100,
            self._breakeven_r, self._atr_trail_r, self._atr_trail_mult,
            self._drought_timer_sec, self._poll_interval_sec,
            *self._eod_hms,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background order-polling thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="RiskManagerPollThread",
            daemon=True,
        )
        self._thread.start()
        logger.info("RiskManager: background polling thread started.")

    def stop(self) -> None:
        """Stop the background polling thread cleanly."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("RiskManager: stopped.")

    # ------------------------------------------------------------------
    # Order registration (called by main after place_limit_buy)
    # ------------------------------------------------------------------

    def register_pending_order(
        self,
        order_id: str,
        signal_data: Dict[str, Any],
    ) -> None:
        """
        Register a newly placed order for fill monitoring.

        Args:
            order_id:    The order ID returned by OrderManager.place_limit_buy().
            signal_data: Dict with keys: symbol, entry_price, stop_price,
                         target_price, quantity, atr14_dollars.
        """
        pending = PendingOrder(
            order_id=order_id,
            symbol=str(signal_data.get("symbol", "")).upper(),
            intended_qty=int(signal_data.get("quantity", 0)),
            signal_entry=float(signal_data.get("entry_price", 0)),
            signal_stop=float(signal_data.get("stop_price", 0)),
            signal_target=float(signal_data.get("target_price", 0)),
            signal_atr=float(signal_data.get("atr14_dollars", 0)),
            submitted_at=datetime.now(self._tz),
        )
        with self._lock:
            self._pending_orders[order_id] = pending

        logger.info(
            "RiskManager: registered pending order %s | %s ×%d @ $%.2f",
            order_id, pending.symbol, pending.intended_qty, pending.signal_entry,
        )

    # ------------------------------------------------------------------
    # Tick handler — Tier 1 trailing stop
    # ------------------------------------------------------------------

    def on_tick(self, symbol: str, fields: dict) -> None:
        """
        Process a Level 1 market tick to update Tier 1 dynamic trailing stops.

        Called from the WebSocket streamer's on_tick callback on every tick.
        Non-blocking and designed for high throughput (nanosecond per tick).

        Tier 1 state machine:
            INITIAL   → BREAKEVEN  when profit ≥ +1.0R  (stop → entry)
            BREAKEVEN → ATR_TRAIL  when profit ≥ +1.5R  (stop → HWM − 1.2×ATR)
            ATR_TRAIL              stop ratchets up with HWM each tick

        Args:
            symbol: Ticker string.
            fields: Tick data dict with "last_price" and/or "bid_price".
        """
        sym = symbol.upper()
        with self._lock:
            pos = self._active_positions.get(sym)
            if pos is None or pos.tier1_triggered:
                return

        # Use bid price for stop evaluation (conservative — fills at bid on sell)
        bid   = float(fields.get("bid_price",  0.0))
        last  = float(fields.get("last_price", 0.0))
        price = bid if bid > 0 else last
        if price <= 0:
            return

        with self._lock:
            pos = self._active_positions.get(sym)
            if pos is None or pos.tier1_triggered:
                return

            one_r = pos.entry_price - pos.initial_stop   # 1R in dollars
            if one_r <= 0:
                return   # Degenerate case — no valid stop distance

            profit = price - pos.entry_price

            # Update high watermark (ratchets up only)
            if price > pos.high_watermark:
                pos.high_watermark = price

            # --- Tier 1 state transitions ---

            # +1.5R: ATR trailing stop (overrides breakeven)
            if profit >= self._atr_trail_r * one_r:
                if not pos.atr_trail_active:
                    pos.atr_trail_active = True
                    pos.breakeven_moved  = True   # Implicitly covered
                    logger.info(
                        "TIER1 ATR TRAIL activated | %s | profit=$%.2f (+%.1fR) | "
                        "HWM=$%.2f ATR=$%.4f",
                        sym, profit, profit / one_r,
                        pos.high_watermark, pos.atr14_dollars,
                    )

                if pos.atr14_dollars > 0:
                    atr_stop = pos.high_watermark - self._atr_trail_mult * pos.atr14_dollars
                    if atr_stop > pos.current_stop:
                        logger.debug(
                            "TIER1 TRAIL STEP | %s | stop $%.2f → $%.2f "
                            "(HWM=%.2f - %.1f×ATR=%.4f)",
                            sym, pos.current_stop, atr_stop,
                            pos.high_watermark, self._atr_trail_mult, pos.atr14_dollars,
                        )
                        pos.current_stop = atr_stop

            # +1.0R: move to breakeven
            elif profit >= self._breakeven_r * one_r and not pos.breakeven_moved:
                pos.current_stop    = pos.entry_price
                pos.breakeven_moved = True
                logger.info(
                    "TIER1 BREAKEVEN | %s | profit=$%.2f (+%.1fR) → "
                    "stop moved to entry=$%.2f",
                    sym, profit, profit / one_r, pos.entry_price,
                )

            # --- Tier 1 trigger check ---
            if price <= pos.current_stop:
                self._trigger_tier1_exit(pos, price)

    def _trigger_tier1_exit(self, pos: ActivePosition, trigger_price: float) -> None:
        """
        Execute the Tier 1 stop exit sequence (called inside _lock).

        1. Mark position as triggered (guard against double-fire).
        2. Cancel the Tier 2 broker stop (prevent phantom short).
        3. Submit market sell.
        4. Update the compliance ledger.
        5. Remove from active_positions.
        """
        if pos.tier1_triggered:
            return   # Already in flight
        pos.tier1_triggered = True

        logger.warning(
            "TIER1 STOP TRIGGERED | %s | price=$%.2f ≤ stop=$%.2f | "
            "qty=%d | cancelling T2 stop=%s",
            pos.symbol, trigger_price, pos.current_stop,
            pos.quantity, pos.tier2_order_id,
        )

        # Cancel Tier 2 (must happen before sell to prevent over-filled short)
        if pos.tier2_order_id:
            try:
                self._order_manager.cancel_order(pos.tier2_order_id)
                logger.info(
                    "TIER1 EXIT: Tier 2 stop %s cancelled.", pos.tier2_order_id
                )
            except Exception as exc:
                logger.error(
                    "TIER1 EXIT: failed to cancel Tier 2 %s: %s — "
                    "proceeding with market sell anyway.",
                    pos.tier2_order_id, exc,
                )

        # Submit market sell
        try:
            sell_id = self._order_manager.execute_market_sell(
                pos.symbol, pos.quantity, reason="TIER1_STOP"
            )
            logger.info(
                "TIER1 EXIT: market sell submitted — order_id=%s", sell_id
            )
        except Exception as exc:
            logger.error(
                "TIER1 EXIT: CRITICAL — market sell failed for %s ×%d: %s",
                pos.symbol, pos.quantity, exc,
            )

        # Update ledger (use trigger price as conservative proceeds estimate)
        try:
            self._ledger.process_fill_sell(
                pos.symbol, pos.quantity, trigger_price  # type: ignore[arg-type]
            )
        except Exception as exc:
            logger.warning("TIER1 EXIT: ledger update failed: %s", exc)

        # Remove position
        self._active_positions.pop(pos.symbol, None)

        if self._dispatcher:
            self._dispatcher.send_risk_breach({
                "symbol": pos.symbol,
                "tier": "TIER_1",
                "trigger_price": trigger_price,
                "qty": pos.quantity
            })

    # ------------------------------------------------------------------
    # Background polling loop
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        """
        Background thread: polls order status and manages scheduled events.

        Cadence: every ``order_poll_interval_sec`` seconds (default 2s).
        Each cycle:
            1. Check all pending orders for fill / partial-fill transitions.
            2. Check all Tier 2 stop orders for broker-side fills.
            3. Check EOD sweep timing.
        """
        logger.info("RiskManager: polling loop started.")

        while not self._stop_event.wait(timeout=self._poll_interval_sec):
            now = datetime.now(self._tz)

            # 1. Pending order fill detection
            with self._lock:
                pending_snapshot = list(self._pending_orders.items())

            for order_id, pending in pending_snapshot:
                if pending.finalized:
                    continue
                try:
                    self._check_pending_order(order_id, pending, now)
                except Exception as exc:
                    logger.warning(
                        "RiskManager: error polling pending order %s: %s",
                        order_id, exc,
                    )

            # 2. Tier 2 stop fill detection
            with self._lock:
                positions_snapshot = list(self._active_positions.values())

            for pos in positions_snapshot:
                if pos.tier2_order_id and not pos.tier1_triggered:
                    try:
                        self._check_tier2_stop(pos)
                    except Exception as exc:
                        logger.warning(
                            "RiskManager: error polling Tier 2 stop %s: %s",
                            pos.tier2_order_id, exc,
                        )

            # 3. EOD liquidation sweep
            self._maybe_eod_sweep(now)

    # ------------------------------------------------------------------
    # Pending order state machine
    # ------------------------------------------------------------------

    def _check_pending_order(
        self,
        order_id: str,
        pending:  PendingOrder,
        now:      datetime,
    ) -> None:
        """
        Fetch order status and apply the fill / drought state machine.
        """
        account_hash = self._order_manager.account_hash
        if not account_hash:
            return

        order = self._rest.get_order(account_hash, order_id)
        status    = order.get("status", "UNKNOWN")
        exec_qty  = int(order.get("filledQuantity",   0))
        leaves_qty= int(order.get("remainingQuantity", 0))
        fill_price = float(order.get("price", pending.signal_entry))

        if status == "FILLED":
            logger.info(
                "RiskManager: order %s FILLED | %s ×%d @ $%.2f",
                order_id, pending.symbol, exec_qty, fill_price,
            )
            self._on_fill_confirmed(pending, exec_qty, fill_price)
            with self._lock:
                pending.finalized = True
                self._pending_orders.pop(order_id, None)

        elif status == "PARTIALLY_FILLED":
            pending.last_exec_qty = exec_qty
            self._handle_drought(order_id, pending, exec_qty, leaves_qty, fill_price, now)

        elif status in ("CANCELLED", "REJECTED", "EXPIRED"):
            logger.info(
                "RiskManager: order %s → %s | removing from pending.",
                order_id, status,
            )
            with self._lock:
                pending.finalized = True
                self._pending_orders.pop(order_id, None)

    def _handle_drought(
        self,
        order_id:   str,
        pending:    PendingOrder,
        exec_qty:   int,
        leaves_qty: int,
        fill_price: float,
        now:        datetime,
    ) -> None:
        """
        Manage the 10-second partial-fill liquidity drought timer.

        Spec: "If leaves remain after 10 seconds, submit DELETE for the order,
        finalize at executed quantity, adjust Tier 2 stop to match."
        """
        if pending.drought_started is None:
            pending.drought_started = now
            logger.warning(
                "RiskManager: DROUGHT TIMER started | order=%s %s ×%d/%d filled",
                order_id, pending.symbol, exec_qty, pending.intended_qty,
            )
            return

        elapsed = (now - pending.drought_started).total_seconds()
        if elapsed < self._drought_timer_sec:
            return   # Still within the grace period

        if leaves_qty <= 0:
            return   # Order fully filled since drought started (race)

        # Drought expired — cancel leaves and finalize
        logger.warning(
            "RiskManager: DROUGHT EXPIRED (%.0fs) | order=%s | "
            "cancelling %d unfilled shares, finalising at %d shares",
            elapsed, order_id, leaves_qty, exec_qty,
        )

        try:
            self._order_manager.cancel_order(order_id)
        except Exception as exc:
            logger.error(
                "RiskManager: drought cancel failed for order %s: %s",
                order_id, exc,
            )

        # Finalize at the executed quantity only
        if exec_qty > 0:
            self._on_fill_confirmed(pending, exec_qty, fill_price)

        with self._lock:
            pending.finalized = True
            self._pending_orders.pop(order_id, None)

        if self._dispatcher:
            self._dispatcher.send_order_lifecycle({
                "status": "DROUGHT_CANCEL",
                "symbol": pending.symbol,
                "cancelled_qty": leaves_qty,
                "final_qty": exec_qty
            })

    # ------------------------------------------------------------------
    # Fill confirmation → position registration + Tier 2 placement
    # ------------------------------------------------------------------

    def _on_fill_confirmed(
        self,
        pending:    PendingOrder,
        exec_qty:   int,
        fill_price: float,
    ) -> None:
        """
        Transition an order from PENDING to an ActivePosition.

        Steps:
            1. Create ActivePosition record with Tier 1 stop at signal_stop.
            2. Place Tier 2 catastrophe stop at fill_price × (1 - tier2_stop_pct).
            3. Update compliance ledger (process_fill_buy).
        """
        if exec_qty <= 0:
            logger.warning(
                "RiskManager: _on_fill_confirmed called with exec_qty=0 "
                "for order %s — skipping.", pending.order_id,
            )
            return

        tier2_stop_price = round(fill_price * (1.0 - self._tier2_stop_pct), 2)

        pos = ActivePosition(
            symbol=pending.symbol,
            entry_price=fill_price,
            quantity=exec_qty,
            initial_stop=pending.signal_stop,
            target_price=pending.signal_target,
            atr14_dollars=pending.signal_atr,
            fill_timestamp=datetime.now(self._tz),
            high_watermark=fill_price,
            current_stop=pending.signal_stop,
        )

        # Place Tier 2 broker stop
        try:
            tier2_id = self._order_manager.place_broker_stop(
                symbol=pending.symbol,
                quantity=exec_qty,
                stop_price=tier2_stop_price,
            )
            pos.tier2_order_id = tier2_id
            logger.info(
                "RiskManager: Tier 2 stop placed — order_id=%s | "
                "%s ×%d stop=$%.2f (-%.0f%%)",
                tier2_id, pending.symbol, exec_qty,
                tier2_stop_price, self._tier2_stop_pct * 100,
            )
        except Exception as exc:
            logger.error(
                "RiskManager: CRITICAL — Tier 2 stop placement FAILED for "
                "%s ×%d: %s — position is unprotected at broker level!",
                pending.symbol, exec_qty, exc,
            )

        # Register active position
        with self._lock:
            self._active_positions[pending.symbol] = pos

        # Update ledger
        try:
            self._ledger.process_fill_buy(
                symbol=pending.symbol,
                quantity=exec_qty,
                fill_price=fill_price,  # type: ignore[arg-type]
            )
        except Exception as exc:
            logger.error(
                "RiskManager: ledger BUY update failed for %s ×%d @ $%.2f: %s",
                pending.symbol, exec_qty, fill_price, exc,
            )

        logger.info(
            "RiskManager: POSITION OPENED | %s ×%d @ $%.2f | "
            "T1_stop=$%.2f T2_stop=$%.2f target=$%.2f",
            pending.symbol, exec_qty, fill_price,
            pos.current_stop, tier2_stop_price, pos.target_price,
        )

        if self._dispatcher:
            self._dispatcher.send_order_lifecycle({
                "status": "FILLED",
                "symbol": pending.symbol,
                "qty": exec_qty,
                "price": fill_price,
                "t1_stop": pos.current_stop,
                "t2_stop": tier2_stop_price
            })

    # ------------------------------------------------------------------
    # Tier 2 broker stop monitoring
    # ------------------------------------------------------------------

    def _check_tier2_stop(self, pos: ActivePosition) -> None:
        """
        Poll the Tier 2 stop order to detect broker-side fills.
        If the Tier 2 fills (process crash scenario), update the ledger
        and remove the position from active tracking.
        """
        account_hash = self._order_manager.account_hash
        if not account_hash or not pos.tier2_order_id:
            return

        order  = self._rest.get_order(account_hash, pos.tier2_order_id)
        status = order.get("status", "UNKNOWN")

        if status == "FILLED":
            fill_qty   = int(  order.get("filledQuantity", pos.quantity))
            fill_price = float(order.get("price",          pos.initial_stop))
            logger.warning(
                "RiskManager: TIER2 STOP FILLED (catastrophe scenario) | "
                "%s ×%d @ $%.2f | T1 was at $%.2f",
                pos.symbol, fill_qty, fill_price, pos.current_stop,
            )
            try:
                self._ledger.process_fill_sell(pos.symbol, fill_qty, fill_price)  # type: ignore[arg-type]
            except Exception as exc:
                logger.error("RiskManager: ledger SELL update failed: %s", exc)

            with self._lock:
                pos.tier1_triggered = True   # Prevent Tier 1 double-trigger
                self._active_positions.pop(pos.symbol, None)

            if self._dispatcher:
                self._dispatcher.send_risk_breach({
                    "symbol": pos.symbol,
                    "tier": "TIER_2_CATASTROPHE",
                    "fill_price": fill_price,
                    "qty": fill_qty
                })

        elif status in ("CANCELLED", "REJECTED", "EXPIRED"):
            logger.warning(
                "RiskManager: Tier 2 stop %s → %s — position %s unprotected "
                "at broker level.", pos.tier2_order_id, status, pos.symbol,
            )
            with self._lock:
                pos.tier2_order_id = None

    # ------------------------------------------------------------------
    # EOD Liquidation Sweep — 15:55:00 EDT
    # ------------------------------------------------------------------

    def _maybe_eod_sweep(self, now: datetime) -> None:
        """
        Check if the EOD liquidation sweep should fire.

        Fires once per calendar day when current EDT time ≥ 15:55:00.
        """
        today = now.date()
        if self._eod_fired_date == today:
            return   # Already fired today

        now_sec  = now.hour * 3600 + now.minute * 60 + now.second
        eod_sec  = self._eod_hms[0] * 3600 + self._eod_hms[1] * 60 + self._eod_hms[2]

        if now_sec < eod_sec:
            return   # Not yet 15:55

        self._eod_fired_date = today
        logger.warning(
            "RiskManager: EOD LIQUIDATION SWEEP triggered at %s EDT.",
            now.strftime("%H:%M:%S"),
        )
        self.trigger_eod_sweep()

    def trigger_eod_sweep(self) -> None:
        """
        Execute the 3:55 PM EOD liquidation mandate.

        Spec (dev plan + research): "At 15:55:00 EDT, cancel all resting orders
        (including Tier 2 stops) and execute market sells for all open inventory
        to return the portfolio to 100% cash overnight."

        This method is safe to call externally for emergency flat-to-cash.
        """
        logger.warning(
            "RiskManager: ===== EOD FLAT-TO-CASH SWEEP ===== "
            "%d active positions | cancelling all orders first…",
            len(self._active_positions),
        )

        # Step 1: Cancel ALL open orders (Tier 2 stops + any resting limit buys)
        try:
            cancelled = self._order_manager.cancel_all_open_orders()
            logger.info("RiskManager: EOD — %d orders cancelled.", cancelled)
        except Exception as exc:
            logger.error("RiskManager: EOD cancel_all_open_orders failed: %s", exc)

        # Step 2: Market sell every active position
        with self._lock:
            positions_to_close = list(self._active_positions.values())

        for pos in positions_to_close:
            if pos.quantity <= 0:
                continue
            try:
                sell_id = self._order_manager.execute_market_sell(
                    pos.symbol, pos.quantity, reason="EOD_SWEEP"
                )
                logger.warning(
                    "RiskManager: EOD SELL | %s ×%d → order_id=%s",
                    pos.symbol, pos.quantity, sell_id,
                )
                # Ledger update (conservative: use current stop as fill estimate)
                try:
                    self._ledger.process_fill_sell(
                        pos.symbol, pos.quantity, pos.current_stop  # type: ignore[arg-type]
                    )
                except Exception as exc:
                    logger.error(
                        "RiskManager: EOD ledger update failed for %s: %s",
                        pos.symbol, exc,
                    )
            except Exception as exc:
                logger.error(
                    "RiskManager: EOD CRITICAL — market sell FAILED for "
                    "%s ×%d: %s", pos.symbol, pos.quantity, exc,
                )

        # Clear all active positions
        with self._lock:
            self._active_positions.clear()

        logger.warning(
            "RiskManager: ===== EOD SWEEP COMPLETE — engine is FLAT-TO-CASH ====="
        )

        if self._dispatcher:
            self._dispatcher.send_portfolio_sweep({
                "reason": "15:55 EDT EOD Sweep",
                "status": "FLAT_TO_CASH"
            })

    # ------------------------------------------------------------------
    # Public accessors
    # ------------------------------------------------------------------

    def get_active_positions(self) -> Dict[str, ActivePosition]:
        """Return a copy of the current active positions dict (thread-safe)."""
        with self._lock:
            return dict(self._active_positions)

    def get_pending_orders(self) -> Dict[str, PendingOrder]:
        """Return a copy of the current pending orders dict (thread-safe)."""
        with self._lock:
            return dict(self._pending_orders)


# ===========================================================================
# Factory
# ===========================================================================

def build_from_config(
    cfg:           dict,
    order_manager,
    rest_client,
    ledger,
    dispatcher=None,
) -> "RiskManager":
    """
    Construct a RiskManager from the loaded config.yaml dict.

    Args:
        cfg:           Top-level config dict.
        order_manager: Initialised OrderManager (firewall already passed).
        rest_client:   SchwabRestClient.
        ledger:        ComplianceLedger.
        dispatcher:    Optional TelemetryDispatcher.

    Returns:
        Configured RiskManager (not yet started — call .start() when ready).
    """
    return RiskManager(
        order_manager=order_manager,
        rest_client=rest_client,
        ledger=ledger,
        cfg=cfg,
        dispatcher=dispatcher,
    )
