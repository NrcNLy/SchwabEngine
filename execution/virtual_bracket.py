"""
execution/virtual_bracket.py
============================
Virtual Bracket Finite State Machine (FSM) & Actor Engine for AVE.
Executes non-linear, time-decaying probability cone monitoring in-memory.

State Flow:
  INIT -> ARMED -> ENTRY_PENDING <-> ENTRY_PARTIAL -> TRACKING <-> TIGHTENING -> TRIGGERED -> EXIT_PENDING -> SETTLED
  Fault states: ORPHANED (execution failure with open shares requiring recovery).

Key Design Invariants:
1. Single-writer actor loop per symbol to prevent race conditions from concurrent tick updates.
2. In-memory continuous tracking: generates ZERO intermediate broker REST calls between entry and exit.
3. Upper Breach: in TIGHTENING mode, upper breach does not exit; it tightens k_l and trails supremum.
4. Lower Breach: liquidates position via LiveEngine.exit_position.
5. Operating Mode:
   - "shadow": Logs transitions and evaluates envelope, but bypasses actual live order dispatch.
   - "live": Full broker order routing.
6. Squeeze Deadline (T_close):
   - FAVORABLE: Friday 15:50 EDT
   - CHOP / DEFENSIVE: Daily 15:50 EDT
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from enum import Enum, auto
import logging
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set, Tuple

import pytz

from core.envelope import AdaptiveVolatilityEnvelope, EnvelopeBounds
from core.ses_mapping import EnvelopeParams, load_ses_snapshot

logger = logging.getLogger("SchwabEngine.VirtualBracket")
_EDT = pytz.timezone("America/New_York")


class FSMState(Enum):
    INIT = 0
    ARMED = 1
    ENTRY_PENDING = 2
    ENTRY_PARTIAL = 3
    TRACKING = 4
    TIGHTENING = 5
    TRIGGERED = 6
    EXIT_PENDING = 7
    SETTLED = 8
    ORPHANED = 9


def _get_telemetry_hub():
    try:
        from api.ave_telemetry import AveTelemetryHub
        return AveTelemetryHub.get_instance()
    except Exception:
        return None


@dataclass(frozen=True)
class TickMessage:
    tick_time: datetime
    price: float
    sigma_t: float
    bid_price: Optional[float] = None


def compute_target_close(
    current_time: datetime,
    regime: str,
    eod_time: dtime = dtime(15, 50, 0),
) -> datetime:
    """
    Computes T_close based on regime:
    - FAVORABLE: Friday 15:50 EDT
    - CHOP or DEFENSIVE: Daily 15:50 EDT
    """
    # Normalize to New York timezone
    if current_time.tzinfo is None:
        ny_time = pytz.utc.localize(current_time).astimezone(_EDT)
    else:
        ny_time = current_time.astimezone(_EDT)

    current_date = ny_time.date()
    daily_close_ny = _EDT.localize(datetime.combine(current_date, eod_time))

    if regime.upper() == "FAVORABLE":
        # Friday is weekday 4 (Monday=0, Tuesday=1, ... Friday=4)
        days_ahead = (4 - current_date.weekday()) % 7
        target_friday = current_date + timedelta(days=days_ahead)
        close_ny = _EDT.localize(datetime.combine(target_friday, eod_time))
        # If today is Friday and already past 15:50, target is today's close (or next Friday)
        if days_ahead == 0 and ny_time >= close_ny:
            close_ny = daily_close_ny
        return close_ny.astimezone(timezone.utc)
    else:
        # Daily 15:50
        return daily_close_ny.astimezone(timezone.utc)


class VirtualBracketFSM:
    """
    Single-symbol Virtual Bracket State Machine.
    """

    def __init__(
        self,
        symbol: str,
        mode: str = "shadow",  # "off", "shadow", "live"
        upper_breach_mode: str = "tighten",  # "tighten" or "exit"
        exit_callback: Optional[Callable[[str, str], Coroutine[Any, Any, bool]]] = None,
        telemetry_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.symbol = symbol.upper()
        self.mode = mode.lower()
        self.upper_breach_mode = upper_breach_mode.lower()
        self.exit_callback = exit_callback
        self.telemetry_callback = telemetry_callback

        self.state: FSMState = FSMState.INIT
        self.envelope: Optional[AdaptiveVolatilityEnvelope] = None
        self.params: Optional[EnvelopeParams] = None

        self.entry_price: float = 0.0
        self.entry_time: Optional[datetime] = None
        self.quantity: int = 0
        self.latest_bounds: Optional[EnvelopeBounds] = None
        self.exit_reason: Optional[str] = None

        # Conflated queue for single-writer actor
        self._tick_queue: asyncio.Queue[TickMessage] = asyncio.Queue(maxsize=1)
        self._actor_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    def start_actor(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """Starts the background worker consuming ticks."""
        if self._actor_task is None or self._actor_task.done():
            loop = loop or asyncio.get_event_loop()
            self._actor_task = loop.create_task(self._actor_loop())

    def stop_actor(self) -> None:
        if self._actor_task and not self._actor_task.done():
            self._actor_task.cancel()

    def on_tick_conflated(
        self,
        tick_time: datetime,
        price: float,
        sigma_t: float,
        bid_price: Optional[float] = None,
    ) -> None:
        """
        Thread-safe entry point from market data feed.
        Drops older unconsumed tick to maintain zero lag (latest-value wins).
        """
        msg = TickMessage(
            tick_time=tick_time,
            price=price,
            sigma_t=sigma_t,
            bid_price=bid_price,
        )
        try:
            # If full, drop the old item
            if self._tick_queue.full():
                try:
                    self._tick_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            self._tick_queue.put_nowait(msg)
        except Exception as exc:
            logger.debug("[%s] Tick queue insert skipped: %s", self.symbol, exc)

    async def _actor_loop(self) -> None:
        """Processes incoming market ticks sequentially without concurrency race conditions."""
        while True:
            try:
                msg = await self._tick_queue.get()
                await self.process_tick(msg)
                self._tick_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("[%s] Unexpected exception in FSM actor loop: %s", self.symbol, exc)

    def _transition_to(self, new_state: FSMState, reason: Optional[str] = None, details: Optional[Dict[str, Any]] = None) -> None:
        prev = self.state
        self.state = new_state
        hub = _get_telemetry_hub()
        if hub is not None:
            try:
                hub.broadcast_fsm_event(
                    symbol=self.symbol,
                    state=new_state.name,
                    prev_state=prev.name if prev else None,
                    reason=reason,
                    details=details,
                )
            except Exception:
                pass

    def arm(
        self,
        planned_qty: int,
        params: Optional[EnvelopeParams] = None,
    ) -> bool:
        """Transitions INIT -> ARMED."""
        if self.state != FSMState.INIT:
            logger.warning("[%s] Cannot arm FSM in state %s", self.symbol, self.state)
            return False

        self.quantity = planned_qty
        self.params = params or load_ses_snapshot()
        self._transition_to(FSMState.ARMED, reason="SIGNAL_VERIFIED")
        logger.info(
            "[%s] FSM ARMED. Target Qty: %d, SES: %.4f (Regime: %s, k_u=%.2f, k_l=%.2f, gamma=%.2f)",
            self.symbol, self.quantity, self.params.ses_score, self.params.regime,
            self.params.k_u, self.params.k_l, self.params.gamma,
        )
        return True

    def notify_entry_fill(
        self,
        fill_price: float,
        fill_qty: int,
        fill_time: datetime,
        session_open: Optional[datetime] = None,
        session_close: Optional[datetime] = None,
    ) -> None:
        """
        Called when entry order fill is confirmed. Transitions to TRACKING.
        """
        self.entry_price = float(fill_price)
        self.quantity = fill_qty
        self.entry_time = fill_time

        # Calculate session bounds
        regime = self.params.regime if self.params else "CHOP"
        t_close = session_close or compute_target_close(fill_time, regime)
        
        # Session open defaults to 09:30 EDT of today
        if session_open is None:
            if fill_time.tzinfo is None:
                fill_ny = pytz.utc.localize(fill_time).astimezone(_EDT)
            else:
                fill_ny = fill_time.astimezone(_EDT)
            open_ny = _EDT.localize(datetime.combine(fill_ny.date(), dtime(9, 30, 0)))
            t_open = open_ny.astimezone(timezone.utc)
        else:
            t_open = session_open

        k_u = self.params.k_u if self.params else 1.5
        k_l = self.params.k_l if self.params else 1.5
        gamma = self.params.gamma if self.params else 0.6

        self.envelope = AdaptiveVolatilityEnvelope(
            entry_price=self.entry_price,
            entry_time=self.entry_time,
            session_open=t_open,
            session_close=t_close,
            k_u=k_u,
            k_l=k_l,
            gamma=gamma,
            upper_breach_mode=self.upper_breach_mode,
        )

        self._transition_to(
            FSMState.TRACKING,
            reason="ENTRY_FILLED",
            details={"fill_price": self.entry_price, "qty": self.quantity},
        )
        logger.info(
            "[%s] Entry filled (%d @ $%.2f). FSM transitioned to TRACKING (T_open=%s, T_close=%s).",
            self.symbol, self.quantity, self.entry_price, t_open.isoformat(), t_close.isoformat(),
        )

    async def process_tick(self, msg: TickMessage) -> Optional[EnvelopeBounds]:
        """
        Evaluates dynamic envelope bounds against tick.
        Handles state transitions for TRACKING / TIGHTENING / TRIGGERED.
        """
        if self.state not in (FSMState.TRACKING, FSMState.TIGHTENING):
            return None

        if self.envelope is None:
            return None

        bounds = self.envelope.evaluate_tick(
            current_time=msg.tick_time,
            current_price=msg.price,
            sigma_t=msg.sigma_t,
            bid_price=msg.bid_price,
        )
        self.latest_bounds = bounds

        # Emit high-frequency compact frame vector to AveTelemetryHub:
        # [seq, epoch_ms, price, upper, lower, lambda, supremum, state_code]
        hub = _get_telemetry_hub()
        if hub is not None:
            try:
                epoch_ms = int(bounds.timestamp.timestamp() * 1000)
                hub.ingest_tick_frame(
                    symbol=self.symbol,
                    epoch_ms=epoch_ms,
                    price=bounds.price,
                    upper=bounds.upper_bound,
                    lower=bounds.lower_bound,
                    lambda_factor=bounds.lambda_factor,
                    supremum=bounds.supremum,
                    state_code=self.state.value,
                )
            except Exception:
                pass

        # Emit telemetry event callback if configured
        if self.telemetry_callback:
            try:
                self.telemetry_callback({
                    "symbol": self.symbol,
                    "state": self.state.name,
                    "price": bounds.price,
                    "bid": bounds.bid_price,
                    "upper": bounds.upper_bound,
                    "lower": bounds.lower_bound,
                    "supremum": bounds.supremum,
                    "lambda": bounds.lambda_factor,
                    "k_l": bounds.k_l,
                    "breached_upper": bounds.breached_upper,
                    "breached_lower": bounds.breached_lower,
                })
            except Exception:
                pass

        # Check for state transitions
        if bounds.breached_upper:
            if self.upper_breach_mode == "tighten":
                if self.state == FSMState.TRACKING:
                    self._transition_to(
                        FSMState.TIGHTENING,
                        reason="UPPER_BREACH_TIGHTEN",
                        details={"price": bounds.price, "upper": bounds.upper_bound, "k_l": bounds.k_l},
                    )
                    logger.info(
                        "[%s] Upper bound reached ($%.2f >= $%.2f). Entering TIGHTENING mode (k_l halved to %.2f).",
                        self.symbol, bounds.price, bounds.upper_bound, bounds.k_l,
                    )
            else:
                # Immediate exit mode
                await self._trigger_exit("AVE_TARGET", bounds)
                return bounds

        if bounds.breached_lower:
            await self._trigger_exit("AVE_TRAILING_STOP", bounds)
            return bounds

        # Check terminal time convergence
        if bounds.lambda_factor <= 0.0:
            await self._trigger_exit("AVE_TIME_EXPIRATION", bounds)
            return bounds

        return bounds

    async def _trigger_exit(self, reason: str, bounds: EnvelopeBounds) -> None:
        """Engages atomic trigger and executes liquidation."""
        async with self._lock:
            if self.state not in (FSMState.TRACKING, FSMState.TIGHTENING):
                return
            self._transition_to(
                FSMState.TRIGGERED,
                reason=reason,
                details={
                    "price": bounds.price,
                    "bid": bounds.bid_price,
                    "upper": bounds.upper_bound,
                    "lower": bounds.lower_bound,
                    "lambda": bounds.lambda_factor,
                },
            )
            self.exit_reason = reason

        logger.warning(
            "[%s] Boundary BREACHED (%s). Price: $%.2f, Bid: $%.2f, Upper: $%.2f, Lower: $%.2f, Lambda: %.4f.",
            self.symbol, reason, bounds.price, bounds.bid_price,
            bounds.upper_bound, bounds.lower_bound, bounds.lambda_factor,
        )

        if self.mode == "shadow":
            logger.info("[%s] [SHADOW MODE] Virtual Bracket would execute market sell for %s.", self.symbol, reason)
            self._transition_to(FSMState.SETTLED, reason=f"SHADOW_LIQUIDATION_{reason}")
            return

        self._transition_to(FSMState.EXIT_PENDING, reason=reason)
        success = False
        if self.exit_callback:
            try:
                success = await self.exit_callback(self.symbol, reason)
            except Exception as exc:
                logger.critical("[%s] Exit callback raised error: %s", self.symbol, exc)
                success = False

        if success:
            self._transition_to(FSMState.SETTLED, reason=f"FILLED_{reason}")
            logger.info("[%s] FSM successfully SETTLED after %s.", self.symbol, reason)
        else:
            self._transition_to(FSMState.ORPHANED, reason=f"FAILED_{reason}")
            logger.critical(
                "[%s] EMERGENCY: Order execution failed or did not confirm fill! State escalated to ORPHANED.",
                self.symbol,
            )
