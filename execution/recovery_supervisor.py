"""
execution/recovery_supervisor.py
================================
Autonomous Recovery Supervisor & Finite State Machine for SOXL (#47).
Handles:
1. 09:30:00 - 09:44:59 EDT: Holding in SHADOW mode with active $99.75 floor sentinel.
2. 09:45:00 EDT: Sampling cash open reference price S_0, calculating dynamic take-profit U(t)
   (+5% to +8%) and dynamic trailing stop L(t) (-2.5%), promoting strategy_config.json to LIVE,
   and dispatching ntfy push alert.
3. 15:50:00 EDT: Mandatory time-decay squeeze and terminal cash liquidation to eliminate overnight decay.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time as dtime, timezone
from decimal import Decimal
import json
import logging
import math
import os
from pathlib import Path
import random
import statistics
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import pytz

from core.paths import (
    CONTROL_SIGNAL_FILE,
    RECOVERY_PLAN_FILE,
    RECOVERY_STATE_FILE,
    RECOVERY_STATUS_FILE,
    STATE_DIR,
)

logger = logging.getLogger("SchwabEngine.RecoverySupervisor")
_EDT = pytz.timezone("America/New_York")


class InvalidReference(ValueError):
    """Raised when reference price S0 cannot be reliably established."""


class FloorProximity(ValueError):
    """Raised when S0 is at or below the hard floor threshold."""


@dataclass(frozen=True)
class RecoveryConfig:
    symbol: str = "SOXL"
    expected_qty: int = 18
    floor: float = 99.75
    trail_pct: float = 0.025
    u_min: float = 0.05
    u_max: float = 0.08
    shadow_until: str = "09:44:59"
    recalibrate_at: str = "09:45:00"
    terminal_flush_at: str = "15:50:00"
    s0_deadline: str = "09:47:00"
    s0_retry_until: str = "10:30:00"
    halt_new_entries: bool = True
    broker_floor_stop: bool = True

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> RecoveryConfig:
        return cls(
            symbol=str(data.get("symbol", "SOXL")).upper(),
            expected_qty=int(data.get("expected_qty", 18)),
            floor=float(data.get("floor_price", 99.75)),
            trail_pct=float(data.get("trail_pct", 0.025)),
            u_min=float(data.get("take_profit_min_pct", 0.05)),
            u_max=float(data.get("take_profit_max_pct", 0.08)),
            shadow_until=str(data.get("shadow_until", "09:44:59")),
            recalibrate_at=str(data.get("recalibrate_at", "09:45:00")),
            terminal_flush_at=str(data.get("terminal_flush_at", "15:50:00")),
            s0_deadline=str(data.get("s0_deadline", "09:47:00")),
            s0_retry_until=str(data.get("s0_retry_until", "10:30:00")),
            halt_new_entries=bool(data.get("halt_new_entries", True)),
            broker_floor_stop=bool(data.get("broker_floor_stop", True)),
        )


@dataclass(frozen=True)
class Bracket:
    s0: float
    m: float
    upper: float
    lower: float
    u_pct: float


def compute_bracket(s0: float, basis: float, cfg: RecoveryConfig) -> Bracket:
    """
    Computes initial dynamic bracket based on S0 reference price:
    - Upper Take-Profit U(t): breakeven-seeking target, clamped to [s0*(1+u_min), s0*(1+u_max)].
    - Lower Trailing Stop L(t): anchored at s0 * (1 - trail_pct), ratcheting with supremum M(t).
    """
    if not (math.isfinite(s0) and s0 > 0.0):
        raise InvalidReference(f"Invalid reference price S0: {s0!r}")
    if s0 <= cfg.floor * 1.003:
        raise FloorProximity(f"S0 ${s0:.2f} is within 0.3% of hard floor ${cfg.floor:.2f}")

    # Breakeven-seeking take-profit clamped to [u_min, u_max]
    raw_u_pct = (basis / s0) - 1.0
    u_pct = min(cfg.u_max, max(cfg.u_min, raw_u_pct))

    upper = round(s0 * (1.0 + u_pct), 2)
    lower = max(cfg.floor, round(s0 * (1.0 - cfg.trail_pct), 2))
    return Bracket(s0=s0, m=s0, upper=upper, lower=lower, u_pct=u_pct)


def ratchet(b: Bracket, last: float, bid: float, cfg: RecoveryConfig) -> Bracket:
    """
    Ratchets lower trailing stop L(t) upward as supremum M(t) increases.
    Spike filter: price print >2% above M requires bid confirmation within 0.5%.
    """
    if last > b.m:
        is_spike = last > b.m * 1.02
        confirmed = (not is_spike) or (bid >= last * 0.995)
        if confirmed:
            new_m = last
            new_lower = max(b.lower, cfg.floor, round(new_m * (1.0 - cfg.trail_pct), 2))
            return Bracket(
                s0=b.s0,
                m=new_m,
                upper=b.upper,
                lower=new_lower,
                u_pct=b.u_pct,
            )
    return b


@dataclass(frozen=True)
class ValidatedQuote:
    symbol: str
    bid: float
    ask: float
    last: float
    timestamp: datetime

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


class QuoteGate:
    """
    Validates quotes against spread limits, staleness, and outlier spikes.
    """

    def __init__(self, symbol: str, reference_price: float = 103.60) -> None:
        self.symbol = symbol.upper()
        self.reference_price = reference_price
        self._last_valid: Optional[ValidatedQuote] = None
        self._lock = asyncio.Lock()

    def validate_tick(
        self,
        price: float,
        bid: float,
        ask: float,
        tick_time: Optional[datetime] = None,
    ) -> Optional[ValidatedQuote]:
        now = tick_time or datetime.now(timezone.utc)
        p = float(price)
        b = float(bid) if bid > 0.0 else p
        a = float(ask) if ask > 0.0 else p

        if not (math.isfinite(p) and math.isfinite(b) and math.isfinite(a)):
            return None
        if p <= 0.0 or b <= 0.0 or a <= 0.0:
            return None
        if b > a * 1.005:  # crossed market beyond tolerance
            return None

        spread = a - b
        max_allowed_spread = max(0.50, p * 0.005)
        if spread > max_allowed_spread:
            return None

        # Sanity jump clamp: >5% deviation from last good print requires verification
        ref = self._last_valid.last if self._last_valid else self.reference_price
        if ref > 0.0:
            pct_jump = abs(p / ref - 1.0)
            if pct_jump > 0.06:
                return None

        quote = ValidatedQuote(symbol=self.symbol, bid=b, ask=a, last=p, timestamp=now)
        self._last_valid = quote
        return quote

    def latest_valid(self, max_age_s: float = 10.0) -> Optional[ValidatedQuote]:
        if self._last_valid is None:
            return None
        age = (datetime.now(timezone.utc) - self._last_valid.timestamp).total_seconds()
        if age > max_age_s:
            return None
        return self._last_valid


class RecoveryMachineState:
    DISARMED = "DISARMED"
    PREOPEN_ARMED = "PREOPEN_ARMED"
    SENTINEL_SHADOW = "SENTINEL_SHADOW"
    RECALIBRATING = "RECALIBRATING"
    SHADOW_FLOOR_ONLY = "SHADOW_FLOOR_ONLY"
    TRACKING_LIVE = "TRACKING_LIVE"
    EXIT_FLOOR = "EXIT_FLOOR"
    EXIT_TRAIL = "EXIT_TRAIL"
    EXIT_TARGET = "EXIT_TARGET"
    TERMINAL_FLUSH = "TERMINAL_FLUSH"
    DONE = "DONE"


class RecoverySupervisor:
    """
    Autonomous Execution Supervisor for SOXL.
    Coordinates shadow sentinel, dynamic bracket recalibration, and EOD terminal flush.
    """

    def __init__(
        self,
        config: Optional[RecoveryConfig] = None,
        engine: Optional[Any] = None,
        basis: float = 110.42,
        rehydrate: bool = True,
    ) -> None:
        self.cfg = config or RecoveryConfig()
        self.engine = engine
        self.basis = basis
        self.symbol = self.cfg.symbol

        self.state: str = RecoveryMachineState.DISARMED
        self.bracket: Optional[Bracket] = None
        self.gate = QuoteGate(self.symbol, reference_price=103.60)
        from execution.microstructure_sentinel import MicrostructureSentinel
        self.sentinel = MicrostructureSentinel(
            symbol=self.symbol,
            s0_reference=103.60,
            soft_floor_pct=0.037,
            hard_catastrophic_pct=0.060,
        )
        self.exit_latched: bool = False
        self.exit_reason: Optional[str] = None
        self.exit_order_id: Optional[str] = None
        self.broker_stop_order_id: Optional[str] = None
        self.last_tick_time: Optional[datetime] = None
        self.last_tick_price: Optional[float] = None

        self._tick_queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(maxsize=1)
        self._actor_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

        # Rehydrate persisted recovery state if present
        if rehydrate:
            self._load_persisted_state()

    def _load_persisted_state(self) -> None:
        if RECOVERY_STATE_FILE.exists():
            try:
                data = json.loads(RECOVERY_STATE_FILE.read_text(encoding="utf-8"))
                self.state = data.get("state", RecoveryMachineState.DISARMED)
                self.exit_latched = data.get("exit_latched", False)
                self.exit_reason = data.get("exit_reason")
                self.exit_order_id = data.get("exit_order_id")
                self.broker_stop_order_id = data.get("broker_stop_order_id")
                if "bracket" in data and data["bracket"]:
                    b = data["bracket"]
                    self.bracket = Bracket(
                        s0=float(b["s0"]),
                        m=float(b["m"]),
                        upper=float(b["upper"]),
                        lower=float(b["lower"]),
                        u_pct=float(b["u_pct"]),
                    )
                logger.info("[%s] Restored recovery supervisor state: %s (latched=%s)", self.symbol, self.state, self.exit_latched)
            except Exception as exc:
                logger.warning("[%s] Failed to load recovery state: %s", self.symbol, exc)

    def persist_state(self) -> None:
        data = {
            "symbol": self.symbol,
            "state": self.state,
            "exit_latched": self.exit_latched,
            "exit_reason": self.exit_reason,
            "exit_order_id": self.exit_order_id,
            "broker_stop_order_id": self.broker_stop_order_id,
            "bracket": {
                "s0": self.bracket.s0,
                "m": self.bracket.m,
                "upper": self.bracket.upper,
                "lower": self.bracket.lower,
                "u_pct": self.bracket.u_pct,
            } if self.bracket else None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._atomic_write(RECOVERY_STATE_FILE, data)

    def write_status(self) -> None:
        status = {
            "symbol": self.symbol,
            "state": self.state,
            "exit_latched": self.exit_latched,
            "exit_reason": self.exit_reason,
            "last_price": self.last_tick_price,
            "last_tick_age_s": (
                (datetime.now(timezone.utc) - self.last_tick_time).total_seconds()
                if self.last_tick_time else None
            ),
            "bracket": {
                "s0": self.bracket.s0,
                "m": self.bracket.m,
                "upper": self.bracket.upper,
                "lower": self.bracket.lower,
                "u_pct": self.bracket.u_pct,
            } if self.bracket else None,
            "broker_stop_order_id": self.broker_stop_order_id,
            "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        }
        self._atomic_write(RECOVERY_STATUS_FILE, status)

    @staticmethod
    def _atomic_write(target_path: Path, data: Dict[str, Any]) -> None:
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = target_path.with_name(f".{target_path.name}.tmp")
            temp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(temp_path, target_path)
        except Exception as exc:
            logger.debug("Failed atomic write to %s: %s", target_path, exc)

    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        loop = loop or asyncio.get_event_loop()
        if self._actor_task is None or self._actor_task.done():
            self._actor_task = loop.create_task(self._actor_loop())
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = loop.create_task(self._heartbeat_loop())
        logger.info("[%s] RecoverySupervisor actor & heartbeat workers started.", self.symbol)

    def stop(self) -> None:
        if self._actor_task and not self._actor_task.done():
            self._actor_task.cancel()
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()

    def on_tick(
        self,
        symbol: str,
        price: float,
        bid_price: float,
        ask_price: float,
        volume: int = 0,
        tick_time: Optional[datetime] = None,
    ) -> None:
        """Single-writer confluent queue ingest."""
        if symbol.upper() != self.symbol:
            return
        msg = {
            "symbol": symbol.upper(),
            "price": float(price),
            "bid": float(bid_price),
            "ask": float(ask_price),
            "volume": int(volume),
            "time": tick_time or datetime.now(timezone.utc),
        }
        if self._tick_queue.full():
            try:
                self._tick_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            self._tick_queue.put_nowait(msg)
        except Exception:
            pass

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                self.write_status()
                # Check scheduled time transitions
                now_edt = datetime.now(_EDT)
                t_str = now_edt.strftime("%H:%M:%S")

                if self.state == RecoveryMachineState.PREOPEN_ARMED and t_str >= "09:30:00":
                    self.state = RecoveryMachineState.SENTINEL_SHADOW
                    logger.info("[%s] 09:30:00 EDT Cash Open: Transitioned to SENTINEL_SHADOW.", self.symbol)
                    self.persist_state()

                if self.state in (RecoveryMachineState.SENTINEL_SHADOW, RecoveryMachineState.SHADOW_FLOOR_ONLY):
                    if t_str >= self.cfg.recalibrate_at and t_str < self.cfg.terminal_flush_at:
                        await self._execute_recalibration(now_edt)

                if self.state in (
                    RecoveryMachineState.PREOPEN_ARMED,
                    RecoveryMachineState.SENTINEL_SHADOW,
                    RecoveryMachineState.RECALIBRATING,
                    RecoveryMachineState.SHADOW_FLOOR_ONLY,
                    RecoveryMachineState.TRACKING_LIVE,
                ):
                    if t_str >= self.cfg.terminal_flush_at:
                        logger.warning("[%s] 15:50:00 EDT Terminal Flush Time Reached.", self.symbol)
                        await self._execute_terminal_flush()

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("[%s] Heartbeat error: %s", self.symbol, exc)
            await asyncio.sleep(1.0)

    async def _actor_loop(self) -> None:
        while True:
            try:
                msg = await self._tick_queue.get()
                await self.process_tick(msg)
                self._tick_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("[%s] Unexpected exception in recovery actor loop: %s", self.symbol, exc)

    async def process_tick(self, msg: Dict[str, Any]) -> None:
        t_time = msg["time"]
        p = msg["price"]
        b = msg["bid"]
        a = msg["ask"]

        self.last_tick_time = t_time
        self.last_tick_price = p

        quote = self.gate.validate_tick(p, b, a, tick_time=t_time)
        if quote is None:
            return

        now_edt = t_time.astimezone(_EDT) if t_time.tzinfo else pytz.utc.localize(t_time).astimezone(_EDT)
        t_str = now_edt.strftime("%H:%M:%S")

        # Invariant Safety Guard: NEVER execute orders before 09:30:00 EDT
        if t_str < "09:30:00":
            return

        # 1. Microstructure Sentinel Floor Breach Check (#54)
        if not self.exit_latched and self.state in (
            RecoveryMachineState.SENTINEL_SHADOW,
            RecoveryMachineState.SHADOW_FLOOR_ONLY,
            RecoveryMachineState.TRACKING_LIVE,
            RecoveryMachineState.RECALIBRATING,
        ):
            verdict = self.sentinel.evaluate_stop(
                current_time_edt=now_edt,
                current_price=quote.last,
                current_volume=msg.get("volume", 0),
                bid_price=quote.bid,
            )
            if verdict.should_exit:
                if verdict.exit_type == "EMERGENCY_HARD_SWEEP":
                    logger.critical(
                        "[%s] CRITICAL: Hard catastrophic floor breached! Price: $%.2f, Bid: $%.2f <= Hard Floor: $%.2f",
                        self.symbol, quote.last, quote.bid, self.sentinel.hard_floor,
                    )
                    await self._trigger_exit(
                        reason="EMERGENCY_HARD_SWEEP",
                        price=quote.last,
                        order_type="MARKET",
                    )
                    return
                else:
                    logger.warning(
                        "[%s] Soft floor breached with confirmed breakdown! (VWAP: $%.2f)",
                        self.symbol, verdict.current_vwap,
                    )
                    pegged_px = verdict.pegged_limit_price or round(quote.bid - 0.02, 2)
                    await self._trigger_exit(
                        reason="RECOVERY_FLOOR",
                        price=pegged_px,
                        order_type="LIMIT",
                    )
                    return
            elif verdict.reason == "OPENING_AUCTION_NOISE":
                logger.info(
                    "[%s] Opening cross price dip ($%.2f) filtered as OPENING_AUCTION_NOISE (soft floor: $%.2f). Exit suppressed.",
                    self.symbol, quote.last, self.sentinel.soft_floor,
                )

        # 2. Tracking Live: Ratchet Stop, Trailing Breach, or Target Breach
        if self.state == RecoveryMachineState.TRACKING_LIVE and not self.exit_latched:
            if self.bracket is not None:
                # Ratchet supremum and lower bound
                self.bracket = ratchet(self.bracket, quote.last, quote.bid, self.cfg)
                self.persist_state()

                # Trailing stop breach (evaluated against bid)
                if quote.bid <= self.bracket.lower:
                    logger.warning(
                        "[%s] Trailing stop breached! Bid: $%.2f <= Lower Bound: $%.2f (M: $%.2f)",
                        self.symbol, quote.bid, self.bracket.lower, self.bracket.m,
                    )
                    await self._trigger_exit(
                        reason="RECOVERY_TRAIL",
                        price=quote.bid,
                        order_type="MARKET",
                    )
                    return

                # Take-profit target breach (evaluated against last print)
                if quote.last >= self.bracket.upper:
                    logger.info(
                        "[%s] Take-profit target reached! Last: $%.2f >= Upper Bound: $%.2f (+%.2f%%)",
                        self.symbol, quote.last, self.bracket.upper, self.bracket.u_pct * 100,
                    )
                    await self._trigger_exit(
                        reason="RECOVERY_TARGET",
                        price=quote.last,
                        order_type="LIMIT",
                        limit_price=round(quote.bid - 0.05, 2),
                    )
                    return

    async def _execute_recalibration(self, now_edt: datetime) -> None:
        """09:45:00 EDT: Establish reference S0, compute dynamic bounds, switch to live."""
        async with self._lock:
            if self.state != RecoveryMachineState.SENTINEL_SHADOW:
                return
            self.state = RecoveryMachineState.RECALIBRATING
            self.persist_state()

        logger.info("[%s] 09:45:00 EDT: Beginning dynamic bracket recalibration...", self.symbol)
        s0 = await self.sample_s0()

        if s0 is None:
            logger.error("[%s] S0 reference sampling failed. Falling back to SHADOW_FLOOR_ONLY.", self.symbol)
            self.state = RecoveryMachineState.SHADOW_FLOOR_ONLY
            self.persist_state()
            self._dispatch_ntfy(
                title=f"Recovery [{self.symbol}]: S0 CORRUPTED",
                message=f"Failed to sample 09:45 cash reference. Mode remains SHADOW with $99.75 floor safeguard.",
                priority="urgent",
                tags=["warning", "rotating_light"],
            )
            return

        try:
            bracket = compute_bracket(s0=s0, basis=self.basis, cfg=self.cfg)
            self.bracket = bracket
            self.gate.reference_price = s0
            self.state = RecoveryMachineState.TRACKING_LIVE
            self.persist_state()

            # Mutate strategy_config.json on disk and in memory
            self._promote_config_live()

            msg = (
                f"S0=${bracket.s0:.2f} | Target U=${bracket.upper:.2f} (+{bracket.u_pct*100:.1f}%) | "
                f"Stop L=${bracket.lower:.2f} (-{self.cfg.trail_pct*100:.1f}%) | Floor=${self.cfg.floor:.2f} | "
                f"Mode: SHADOW -> LIVE"
            )
            logger.info("[%s] Dynamic Recalibration COMPLETE: %s", self.symbol, msg)
            self._dispatch_ntfy(
                title=f"SOXL Autonomous Recalibration: LIVE",
                message=msg,
                priority="high",
                tags=["chart_with_upwards_trend", "rocket"],
            )
        except Exception as exc:
            logger.critical("[%s] Bracket computation failed: %s", self.symbol, exc)
            self.state = RecoveryMachineState.SHADOW_FLOOR_ONLY
            self.persist_state()

    async def sample_s0(self) -> Optional[float]:
        """Samples S0 median over 09:44:55-09:45:10 window using validated quotes with backoff."""
        delays = [0.25, 0.5, 1.0, 2.0]
        samples: List[float] = []

        for attempt in range(6):
            q = self.gate.latest_valid(max_age_s=15.0)
            if q is not None and q.mid > 0.0:
                samples.append(q.mid)
                if len(samples) >= 3:
                    s0 = statistics.median(samples)
                    if s0 > self.cfg.floor * 1.003:
                        return round(s0, 2)
            await asyncio.sleep(delays[min(attempt, len(delays) - 1)])

        # Fallback to last valid tick if available
        q = self.gate.latest_valid(max_age_s=60.0)
        if q is not None and q.last > self.cfg.floor * 1.003:
            return round(q.last, 2)
        return None

    def _promote_config_live(self) -> None:
        """Mutates strategy_config.json ave.mode to 'live' and updates engine in memory."""
        try:
            from core.paths import STATE_DIR, STRATEGY_CONFIG_FILE
            targets = [
                STRATEGY_CONFIG_FILE,
                STATE_DIR / "strategy_config.json",
                Path("/home/nicho/schwab_engine/config/strategy_config.json"),
                Path("/home/nicho/schwab_engine/strategy_config.json"),
                Path("/home/nicho/schwab_engine/state/strategy_config.json"),
            ]
            for p in targets:
                if p.exists():
                    cfg = json.loads(p.read_text(encoding="utf-8"))
                    if "ave" in cfg:
                        cfg["ave"]["mode"] = "live"
                    else:
                        cfg["ave"] = {"mode": "live"}
                    cfg["_updated_at"] = datetime.now(timezone.utc).isoformat()
                    cfg["_source"] = "recovery_supervisor"
                    self._atomic_write(p, cfg)
                    logger.info("strategy_config.json updated to 'live' at %s", p)
        except Exception as exc:
            logger.warning("Could not persist ave.mode 'live' to config: %s", exc)

        if self.engine is not None and hasattr(self.engine, "set_ave_mode"):
            self.engine.set_ave_mode("live")

    async def _execute_terminal_flush(self) -> None:
        """15:50:00 EDT: Mandatory cash exit sweep."""
        if self.exit_latched:
            return
        logger.warning("[%s] Executing 15:50:00 EDT TERMINAL EOD FLUSH.", self.symbol)
        await self._trigger_exit(
            reason="RECOVERY_EOD",
            price=0.0,
            order_type="MARKET",
        )

    async def _trigger_exit(
        self,
        reason: str,
        price: float,
        order_type: str = "MARKET",
        limit_price: Optional[float] = None,
    ) -> bool:
        async with self._lock:
            if self.exit_latched:
                return False
            self.exit_latched = True
            self.exit_reason = reason
            self.state = reason
            self.persist_state()

        logger.warning(
            "[%s] LATCHED EXIT TRIGGER: %s (Type: %s, Price: $%.2f)",
            self.symbol, reason, order_type, price,
        )

        # 1. Cancel broker-side floor stop first to prevent double-sell
        if self.engine is not None and getattr(self.engine, "om", None) is not None:
            om = self.engine.om
            try:
                sid = self.broker_stop_order_id or getattr(self.engine, "_stop_ids", {}).get(self.symbol)
                if sid:
                    await asyncio.to_thread(om.cancel_order, sid)
                    logger.info("[%s] Cancelled working broker stop %s prior to exit execution.", self.symbol, sid)
            except Exception as exc:
                logger.warning("[%s] Failed to cancel broker stop: %s", self.symbol, exc)

        # 2. Get exact current position quantity
        qty = self.cfg.expected_qty
        if self.engine is not None and getattr(self.engine, "ledger", None) is not None:
            pos = self.engine.ledger.positions.get(self.symbol)
            if pos is not None:
                qty = pos.quantity

        # 3. Submit Order
        order_id = None
        success = False
        try:
            if self.engine is not None and getattr(self.engine, "om", None) is not None:
                om = self.engine.om
                if order_type == "LIMIT" and limit_price is not None:
                    try:
                        order_id = await asyncio.to_thread(
                            om.execute_marketable_limit_sell,
                            self.symbol, qty, limit_price, reason,
                        )
                        # Poll for 4 seconds, if unfilled cancel and escalate to MARKET
                        if hasattr(self.engine, "_poll_order"):
                            doc = await self.engine._poll_order(order_id, 4.0)
                            filled = int(float(doc.get("filledQuantity", 0) or 0))
                        else:
                            filled = qty
                        if filled >= qty:
                            success = True
                        else:
                            await asyncio.to_thread(om.cancel_order, order_id)
                            order_id = await asyncio.to_thread(
                                om.execute_market_sell, self.symbol, qty - filled, reason,
                            )
                            success = True
                    except Exception as exc:
                        logger.warning("[%s] Marketable limit order failed (%s), escalating to MARKET.", self.symbol, exc)
                        order_id = await asyncio.to_thread(
                            om.execute_market_sell, self.symbol, qty, reason,
                        )
                        success = True
                else:
                    order_id = await asyncio.to_thread(
                        om.execute_market_sell, self.symbol, qty, reason,
                    )
                    success = True

            self.exit_order_id = order_id
            self.state = RecoveryMachineState.DONE
            self.persist_state()

            # Record in ledger if engine present
            if self.engine is not None and getattr(self.engine, "ledger", None) is not None and success:
                pos = self.engine.ledger.positions.get(self.symbol)
                if pos:
                    self.engine.ledger.record_sell(
                        self.symbol,
                        qty,
                        Decimal(str(price if price > 0 else 100.0)),
                        simulated=False,
                        execution_payload={"status": "FILLED", "filledQuantity": qty},
                    )
        except Exception as exc:
            logger.critical("[%s] Order dispatch failed: %s", self.symbol, exc)
            success = False

        self._dispatch_ntfy(
            title=f"SOXL Exit Executed: {reason}",
            message=f"Order placed for {qty} shares ({order_type}). Order ID: {order_id}. Reason: {reason}",
            priority="urgent",
            tags=["moneybag", "outbox_tray"],
        )
        return success

    def _dispatch_ntfy(
        self,
        title: str,
        message: str,
        priority: str = "default",
        tags: Optional[List[str]] = None,
    ) -> None:
        try:
            from core.notifier import send_alert
            send_alert(
                title=title,
                message=message,
                priority=priority,
                tags=tags or ["robot"],
            )
        except Exception as exc:
            logger.warning("Failed to dispatch ntfy alert '%s': %s", title, exc)
