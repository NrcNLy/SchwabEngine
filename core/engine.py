"""
core/engine.py
==============
Order-lifecycle engines for the two run modes.

* ``LiveEngine``  - routes real orders through ``OrderManager`` (Schwab REST) and keeps the
  ``SettlementLedger`` honest. Every blocking broker call is dispatched with
  ``asyncio.to_thread`` so the event loop (which also serves the API) never stalls.
* ``SimEngine``   - the dry-run pipeline: consumes ``MarketEvent`` objects from the EDA bus,
  sizes with the Bayesian Quarter-Kelly engine and the dynamic buying-power ceiling, "executes"
  through the dry-run order client (nothing leaves the process) and books simulated fills.

Invariants enforced by both
---------------------------
* Entries are funded from SETTLED cash only (ledger.allocate_capital) - zero GFV.
* Order notional <= min(tactical float - buffer, 20% of NLV) from ``compute_buying_power``.
* No new entries once the daily drawdown limit is breached, the kill switch is on, outside the
  core session, or at/after the 15:50 flatten.
* The engine only ever cancels ORDERS IT PLACED. User-placed orders (for example protective stops
  on positions the engine does not manage) are never touched.
* SWVXX is never traded; money-market sweeps are advisory only.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime
from decimal import Decimal
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from core.ledger import SettlementLedger
from core.liquidity_policy import BuyingPowerBreakdown, ZERO, is_business_day, q
from core.runtime import EngineContext, ledger_buying_power, lifecycle_phase, now_et, parse_hhmm
from core.session import TradingPhase, get_session_phase, is_entry_permitted
from execution.order_manager import GoodFaithViolationBlockedError

logger = logging.getLogger("engine")

TERMINAL_STATUSES = {"FILLED", "CANCELED", "REJECTED", "EXPIRED", "REPLACED"}


# ---------------------------------------------------------------------------
# Settings & pure helpers
# ---------------------------------------------------------------------------

@dataclass
class EngineSettings:
    symbols: List[str]
    eod: dtime
    flat_deadline: dtime
    tier2_stop_pct: Decimal
    fill_timeout_s: float = 45.0
    fill_poll_s: float = 2.0
    max_concurrent_positions: int = 3

    @classmethod
    def from_cfg(cls, cfg: Dict[str, Any]) -> "EngineSettings":
        sched = cfg.get("schedule", {}) or {}
        risk = cfg.get("risk", {}) or {}
        return cls(
            symbols=[str(s).upper() for s in (cfg.get("engine", {}) or {}).get("symbols", [])],
            eod=parse_hhmm(sched.get("eod_liquidation_time"), "15:50:00"),
            flat_deadline=parse_hhmm(sched.get("flat_deadline_time"), "15:55:00"),
            tier2_stop_pct=Decimal(str(risk.get("tier2_stop_pct", 0.04))),
        )


def size_entry(signal_qty: int, entry_price: Decimal, bp: BuyingPowerBreakdown) -> int:
    """Shares = min(strategy size, floor(max_order_notional / price)); never negative."""
    if entry_price <= 0 or signal_qty < 1:
        return 0
    ceiling = int(math.floor(bp.max_order_notional / entry_price))
    return max(min(int(signal_qty), ceiling), 0)


def parse_fill(order: Dict[str, Any]) -> Tuple[int, Optional[Decimal]]:
    """
    (filled_quantity, volume-weighted average price) from a Schwab order JSON.
    The average comes from ``orderActivityCollection[].executionLegs[]``; None if absent.
    """
    filled = int(float(order.get("filledQuantity", 0) or 0))
    notional = ZERO
    shares = ZERO
    for activity in order.get("orderActivityCollection", []) or []:
        for leg in activity.get("executionLegs", []) or []:
            try:
                qty = Decimal(str(leg.get("quantity", 0) or 0))
                px = Decimal(str(leg.get("price", 0) or 0))
            except Exception:
                continue
            if qty > 0 and px > 0:
                notional += qty * px
                shares += qty
    avg = q(notional / shares) if shares > 0 else None
    return filled, avg


def regime_code_from_ci(ci: float, trend_max: float = 38.2, chop_min: float = 61.8) -> str:
    """Choppiness Index -> engine regime letter (A trend / B mean-reversion / C chop = halt)."""
    if ci < trend_max:
        return "A"
    if ci <= chop_min:
        return "B"
    return "C"


# ---------------------------------------------------------------------------
# Shared base
# ---------------------------------------------------------------------------

class _EngineBase:
    env: str = "sandbox"

    def __init__(
        self,
        ctx: EngineContext,
        cfg: Dict[str, Any],
        ledger: SettlementLedger,
        settings: EngineSettings,
        ignore_session: bool = False,
    ):
        self.ctx = ctx
        self.cfg = cfg
        self.ledger = ledger
        self.settings = settings
        self.ignore_session = ignore_session
        self.micro = getattr(ctx, "microstructure", None)
        self.telemetry = getattr(ctx, "telemetry", None)
        if self.telemetry is None:
            from core.telemetry import SQLiteWALEventStore
            self.telemetry = SQLiteWALEventStore()
        self._in_flight: Set[str] = set()
        self._exiting: Set[str] = set()
        self._last_flatten_attempt = 0.0

    def on_book(self, snap: Any) -> None:
        """Called from the streamer thread with Level 2 order book snapshot."""
        if self.micro is not None:
            self.micro.on_book(snap)

    # -- guards ----------------------------------------------------------

    def drawdown_tripped(self) -> bool:
        pnl = self.ledger.realized_pnl_today() + self.ledger.unrealized_pnl()
        return pnl <= self.ledger.daily_drawdown_limit

    def entry_block_reason(self, symbol: str) -> Optional[str]:
        sym = symbol.upper()
        if self.ctx.is_halted:
            return "kill switch engaged"
        if not self.ignore_session:
            t_phase = get_session_phase(now_et(), self.cfg)
            permitted, _, reason = is_entry_permitted(t_phase, self.cfg)
            if not permitted:
                return f"session phase {t_phase.value}: {reason}"
        if sym in self._in_flight or sym in self._exiting:
            return "order already in flight"
        if sym in self.ledger.positions:
            return "position already open"
        if len(self.ledger.positions) + len(self._in_flight) >= self.settings.max_concurrent_positions:
            return "max concurrent positions reached"
        if self.drawdown_tripped():
            return "daily drawdown circuit breaker tripped"
        return None

    # -- scheduling ------------------------------------------------------

    async def flatten_all(self, reason: str) -> int:  # pragma: no cover - overridden
        raise NotImplementedError

    async def run_schedulers(self, stop: asyncio.Event, governor: Any = None) -> None:
        """
        Wall-clock jobs (ET, business days only): 08:35 macro analysis, 09:00 T+1 rollover,
        15:50 flatten (retried until flat), 15:55 flat-deadline audit, minute NLV anchor.
        A job whose window was missed (e.g. engine started at noon) does not fire late.
        """
        done: Dict[str, date] = {}
        last_anchor = 0.0
        last_audit = 0.0
        last_phase = None
        macro_at = parse_hhmm(None, "08:35:00")
        rollover_at = parse_hhmm((self.cfg.get("schedule", {}) or {}).get("t1_rollover_time"), "09:00:00")
        close = dtime(16, 15)

        while not stop.is_set():
            try:
                now = now_et()
                today, t = now.date(), now.time()

                current_phase = get_session_phase(now, self.cfg)
                if last_phase is not None and current_phase != last_phase:
                    permitted = is_entry_permitted(current_phase, self.cfg)[0]
                    from core.notifier import send_alert
                    import threading
                    threading.Thread(
                        target=send_alert,
                        args=(
                            f"[PHASE] Engine: {current_phase.value}",
                            f"State transition from {last_phase.value} to {current_phase.value}. Entry permitted: {permitted}.",
                            "default",
                            "clock,repeat"
                        ),
                        daemon=True
                    ).start()
                last_phase = current_phase

                if time.monotonic() - last_anchor >= 60:
                    last_anchor = time.monotonic()
                    if self.env == "sandbox":
                        await asyncio.to_thread(self.ctx.anchors["sandbox"].update, self.ledger.nlv)

                if is_business_day(today):
                    if t >= rollover_at and done.get("rollover") != today:
                        done["rollover"] = today
                        moved = await asyncio.to_thread(self.ledger.rollover, today)
                        logger.info("Scheduled T+1 rollover moved $%s into settled cash.", moved)

                    if (governor is not None and macro_at <= t < dtime(9, 30)
                            and done.get("macro") != today):
                        done["macro"] = today
                        asyncio.create_task(self._run_governor(governor))

                    if self.settings.eod <= t < close and self.ledger.positions:
                        if time.monotonic() - self._last_flatten_attempt >= 20:
                            self._last_flatten_attempt = time.monotonic()
                            count = await self.flatten_all("EOD_FLAT")
                            logger.info("EOD flatten pass closed %d position(s).", count)

                    if t >= self.settings.flat_deadline and done.get("flatten_sweep") != today:
                        done["flatten_sweep"] = today
                        remaining = len(self.ledger.positions)
                        syms = sorted(self.ledger.positions.keys())
                        try:
                            from core.notifier import send_alert
                            if remaining == 0:
                                send_alert(
                                    title="15:55 Flatten Sweep Complete",
                                    message="All engine positions successfully liquidated to 100% cash.",
                                    priority="high",
                                    tags=["broom", "checkered_flag"],
                                )
                            else:
                                send_alert(
                                    title="15:55 Flatten Sweep Alert - Open Positions!",
                                    message=f"Sweep completed but {remaining} position(s) remain open: {syms}. Check Schwab immediately.",
                                    priority="urgent",
                                    tags=["rotating_light", "warning"],
                                )
                        except Exception:
                            pass

                    if (self.settings.flat_deadline <= t < close and self.ledger.positions
                            and time.monotonic() - last_audit >= 60):
                        last_audit = time.monotonic()
                        logger.critical(
                            "FLAT DEADLINE PASSED with open engine positions: %s. Check Schwab immediately.",
                            sorted(self.ledger.positions.keys()),
                        )

                    if t >= dtime(16, 0) and done.get("micro_persist") != today:
                        done["micro_persist"] = today
                        if self.ctx.microstructure is not None:
                            await asyncio.to_thread(self.ctx.microstructure.persist)
            except Exception:
                logger.exception("Scheduler pass failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

    @staticmethod
    async def _run_governor(governor: Any) -> None:
        try:
            res = await governor.pre_market_macro()
            try:
                from core.notifier import send_alert
                msg = "Pre-market macro regime evaluated and strategy configuration updated."
                if isinstance(res, dict):
                    regime = res.get("regime") or res.get("target_regime")
                    if regime:
                        msg = f"08:35 macro regime determined: {regime}."
                send_alert(
                    title="08:35 Macro Regime Determined",
                    message=msg,
                    priority="low",
                    tags=["chart_with_upwards_trend", "crystal_ball"],
                )
            except Exception:
                pass
        except Exception:
            logger.exception("Governor pre-market macro failed (Tier 1 unaffected).")


# ---------------------------------------------------------------------------
# Live engine
# ---------------------------------------------------------------------------

class LiveEngine(_EngineBase):
    env = "active"

    def __init__(
        self,
        ctx: EngineContext,
        cfg: Dict[str, Any],
        ledger: SettlementLedger,
        settings: EngineSettings,
        order_manager: Any,
        strategy: Any,
        loop: asyncio.AbstractEventLoop,
    ):
        super().__init__(ctx, cfg, ledger, settings, ignore_session=False)
        self.om = order_manager
        if hasattr(self.om, "telemetry") and getattr(self.om, "telemetry", None) is None:
            self.om.telemetry = self.telemetry
        self.strategy = strategy
        self.loop = loop
        self._stop_ids: Dict[str, str] = {}
        self._open_order_ids: Dict[str, str] = {}   # order id -> symbol (engine-placed, still working)
        from indicators.volatility import YangZhangEstimator
        yz_cfg = (cfg.get("risk", {}) or {}).get("yang_zhang_stops", {}) or {}
        self._yz_window = int(yz_cfg.get("rolling_window", 30))
        self._yz_multipliers = yz_cfg.get("ticker_multipliers", {"TQQQ": 1.8, "SOXL": 2.0, "TNA": 2.2})
        self._yz_default_k = float(yz_cfg.get("default_k_stop", 2.0))
        self._yz_dt_days = float(yz_cfg.get("dt_annualization_days", 1.0))
        self._yz_intraday_fraction = float(yz_cfg.get("intraday_fraction", 1.0 / 390.0))
        self._yz_min_stop_pct = float(yz_cfg.get("min_stop_distance_pct", 0.005))
        self._yz_estimators: Dict[str, YangZhangEstimator] = {}
        strategy.set_signal_callback(self._on_signal)
        if self.micro is not None:
            strategy.set_pre_signal_filter(self.micro.pre_signal_filter)

    # -- streamer thread entry points --------------------------------------

    def on_tick(self, symbol: str, fields: Dict[str, Any]) -> None:
        """Called from the streamer thread. Must not block."""
        sym = symbol.upper()
        # Reference symbols (NVDA, TSM, KRE, $TNX, /ZN, /NQ) update microstructure hub only
        if self.micro is not None and self.micro.is_reference(sym):
            self.micro.on_tick(sym, fields)
            return

        # Microstructure ingest for tradeable symbols
        if self.micro is not None:
            self.micro.on_tick(sym, fields)

        price = fields.get("last_price")
        if price is None:
            return
        self.ctx.mark_tick("SCHWAB")
        try:
            px = Decimal(str(price))
            self.ledger.mark_price(sym, px)
            self.strategy.on_tick(sym, fields)

            # Update Yang-Zhang volatility bar tracker
            open_p = fields.get("open_price", price)
            high_p = fields.get("high_price", price)
            low_p = fields.get("low_price", price)
            close_p = price
            est = self._yz_estimators.get(sym)
            if est is None:
                from indicators.volatility import YangZhangEstimator
                est = YangZhangEstimator(window_size=self._yz_window)
                self._yz_estimators[sym] = est
            yz_vol = est.add_bar(open_p, high_p, low_p, close_p)
            hurst_h = est.current_hurst()

            pos = self.ledger.positions.get(sym)
            if pos is not None:
                # Dynamic volatility stop ratchet evaluation gated by Hurst Exponent
                if self.ctx.risk_manager is not None:
                    k_stop = float(self._yz_multipliers.get(sym, self._yz_default_k))
                    new_stop, ratcheted = self.ctx.risk_manager.evaluate_trailing_stop(
                        pos,
                        float(px),
                        yz_vol,
                        k_stop=k_stop,
                        intraday_fraction=self._yz_intraday_fraction,
                        hurst_exponent=hurst_h,
                        min_stop_distance_pct=self._yz_min_stop_pct,
                    )
                    if ratcheted:
                        logger.debug(
                            "[%s] Trailing stop ratcheted to $%s (HWM: $%s, YZ vol: %.4f, Hurst: %s)",
                            sym, pos.stop_price, pos.high_water_mark, yz_vol,
                            f"{hurst_h:.3f}" if hurst_h is not None else "N/A",
                        )
                        if self.telemetry is not None:
                            self.telemetry.record_event(
                                aggregate_id=sym,
                                event_type="DYNAMIC_STOP_RATCHET",
                                payload={
                                    "symbol": sym,
                                    "stop_price": str(pos.stop_price),
                                    "high_water_mark": str(pos.high_water_mark),
                                    "yz_vol": float(yz_vol),
                                    "hurst": hurst_h,
                                    "distance": str(pos.volatility_stop_distance),
                                    "category": "RISK",
                                    "message": f"Dynamic stop rose to ${pos.stop_price} on {sym} (trailing Yang-Zhang protect)",
                                },
                            )

                reason = None
                if pos.stop_price is not None and px <= pos.stop_price:
                    reason = "TIER1_STOP"
                elif pos.target_price is not None and px >= pos.target_price:
                    reason = "TARGET"
                if reason:
                    if self.telemetry is not None:
                        self.telemetry.record_event(
                            aggregate_id=sym,
                            event_type="TIER1_STOP_TRIGGER" if reason == "TIER1_STOP" else "TARGET_TRIGGER",
                            payload={
                                "symbol": sym,
                                "price": str(px),
                                "stop_price": str(pos.stop_price) if pos.stop_price else None,
                                "target_price": str(pos.target_price) if pos.target_price else None,
                                "reason": reason,
                                "category": "ORDER/FLOW",
                                "message": f"{reason} triggered on {sym} @ ${px}",
                            },
                        )
                    self.loop.call_soon_threadsafe(self._request_exit, sym, reason)
        except Exception:
            logger.exception("Tick handling failed for %s", sym)

    def _on_signal(self, signal: Any) -> None:
        self.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.handle_signal(signal)))

    def _request_exit(self, symbol: str, reason: str) -> None:
        if symbol in self._exiting:
            return
        asyncio.ensure_future(self.exit_position(symbol, reason))

    # -- entries -----------------------------------------------------------

    def entry_block_reason(self, symbol: str) -> Optional[str]:
        reason = super().entry_block_reason(symbol)
        if reason:
            return reason
        if not self.ledger.synced:
            return "ledger has not synced with the broker"
        sync = self.ctx.broker_sync
        if sync is not None:
            age = sync.seconds_since_ok()
            if age is None or age > max(3 * sync.poll_interval, 90):
                return "broker balances are stale"
        return None

    async def handle_signal(self, sig: Any) -> bool:
        sym = str(sig.symbol).upper()
        reason = self.entry_block_reason(sym)
        if reason:
            logger.info("[%s] Signal ignored: %s", sym, reason)
            return False

        # Microstructure gate confirmation
        if self.micro is not None:
            t_phase = get_session_phase(now_et(), self.cfg) if not self.ignore_session else TradingPhase.CORE_SESSION
            strat_name = str(getattr(sig, "strategy", "UNKNOWN"))
            dec = self.micro.check(sym, strat_name, t_phase, stage="engine")
            if self.micro.blocks(dec):
                logger.info("[%s] Signal blocked by microstructure gate: %s (%s)", sym, dec.code, dec.reason)
                return False

        self._in_flight.add(sym)
        try:
            posterior = self.ctx.risk_manager.posterior_win_rate() if self.ctx.risk_manager else None
            bp = ledger_buying_power(self.ctx, "active", regime=sig.regime.value, posterior=posterior)
            if bp is None:
                return False
            entry = Decimal(str(sig.entry_price))
            qty = size_entry(int(sig.quantity), entry, bp)
            # Apply temporal gate sizing multiplier (e.g. 50% during POWER_HOUR)
            if not self.ignore_session:
                t_phase = get_session_phase(now_et(), self.cfg)
                _, phase_mult, _ = is_entry_permitted(t_phase, self.cfg)
                if phase_mult < 1.0:
                    scaled_qty = math.floor(qty * phase_mult)
                    logger.info("[%s] Temporal gate (%s) scaled sizing: %d -> %d shares", sym, t_phase.value, qty, scaled_qty)
                    qty = scaled_qty
            if qty < 1:
                logger.info("[%s] Sized to 0 shares (max order $%s, entry $%s).", sym, bp.max_order_notional, entry)
                return False
            cost = q(entry * qty)
            if not self.ledger.allocate_capital(sym, cost):
                return False

            try:
                order_id = await asyncio.to_thread(self.om.place_limit_buy, sym, qty, float(entry))
            except Exception as exc:
                self.ledger.cancel_reservation(sym)
                logger.error("[%s] Entry order rejected: %s", sym, exc)
                return False
            self._open_order_ids[order_id] = sym
            return await self._complete_entry(sym, order_id, qty, entry, sig)
        finally:
            self._in_flight.discard(sym)

    async def _poll_order(self, order_id: str, timeout_s: float) -> Dict[str, Any]:
        """Polls until the order is terminal or the timeout lapses; returns the last status payload."""
        deadline = time.monotonic() + timeout_s
        last: Dict[str, Any] = {}
        while time.monotonic() < deadline:
            await asyncio.sleep(self.settings.fill_poll_s)
            try:
                last = await asyncio.to_thread(self.om.get_order_status, order_id)
            except Exception as exc:
                logger.warning("Order %s status poll failed: %s", order_id, exc)
                continue
            if str(last.get("status", "")).upper() in TERMINAL_STATUSES:
                break
        return last

    async def _complete_entry(self, sym: str, order_id: str, qty: int, entry: Decimal, sig: Any) -> bool:
        status_doc = await self._poll_order(order_id, self.settings.fill_timeout_s)
        status = str(status_doc.get("status", "")).upper()

        if status not in TERMINAL_STATUSES:
            try:
                await asyncio.to_thread(self.om.cancel_order, order_id)
            except Exception as exc:
                logger.warning("[%s] Cancel of unfilled entry %s failed: %s", sym, order_id, exc)
            # Capture fills that landed before the cancel took effect.
            for _ in range(3):
                try:
                    status_doc = await asyncio.to_thread(self.om.get_order_status, order_id)
                except Exception:
                    break
                if str(status_doc.get("status", "")).upper() in TERMINAL_STATUSES:
                    break
                await asyncio.sleep(self.settings.fill_poll_s)

        self._open_order_ids.pop(order_id, None)
        filled, avg = parse_fill(status_doc)
        if filled < 1:
            self.ledger.cancel_reservation(sym)
            logger.info("[%s] Entry %s ended unfilled (%s); reservation released.", sym, order_id, status or "UNKNOWN")
            return False

        price = avg if avg is not None else entry
        self.ledger.confirm_buy(
            sym, filled, price, stop=sig.stop_price, target=sig.target_price,
            regime=sig.regime.value, simulated=False,
        )
        logger.info("[%s] Entry filled: %d @ %s", sym, filled, price)
        try:
            from core.notifier import send_alert
            send_alert(
                title=f"Order Filled: BUY {filled} {sym}",
                message=f"Filled {filled} shares of {sym} at ${price} (Regime: {sig.regime.value}).",
                priority="default",
                tags=["shopping_cart", "chart_with_upwards_trend"],
            )
        except Exception:
            pass

        stop_px = q(price * (Decimal("1") - self.settings.tier2_stop_pct))
        try:
            sid = await asyncio.to_thread(self.om.place_broker_stop, sym, filled, float(stop_px))
            self._stop_ids[sym] = sid
            self._open_order_ids[sid] = sym
        except Exception as exc:
            logger.critical("[%s] BROKER CATASTROPHE STOP FAILED (%s). Position is protected only by Tier 1.", sym, exc)
        return True

    # -- exits -------------------------------------------------------------

    async def exit_position(self, symbol: str, reason: str, cancel_stop: bool = True) -> bool:
        sym = symbol.upper()
        if sym in self._exiting:
            return False
        pos = self.ledger.positions.get(sym)
        if pos is None:
            return False

        # Pre-exit GFV veto check: hold overnight if shares were purchased with unsettled funds
        can_sell, veto_reason = self.ledger.can_sell_position(sym, pos.quantity)
        if not can_sell:
            logger.warning("[%s] Exit blocked by GFV protection (%s). Position held overnight.", sym, veto_reason)
            return False

        self._exiting.add(sym)
        try:
            qty = pos.quantity
            sid = self._stop_ids.pop(sym, None) if cancel_stop else None
            if sid:
                self._open_order_ids.pop(sid, None)
                try:
                    await asyncio.to_thread(self.om.cancel_order, sid)
                except Exception as exc:
                    logger.warning("[%s] Could not cancel broker stop %s: %s", sym, sid, exc)

            try:
                oid = await asyncio.to_thread(self.om.execute_market_sell, sym, qty, reason)
            except GoodFaithViolationBlockedError as gfv_err:
                logger.warning("[%s] Market sell blocked by GFV protection (%s). Position held overnight.", sym, gfv_err)
                return False
            except Exception as exc:
                logger.critical("[%s] MARKET SELL FAILED (%s) [%s]. Re-arming broker stop.", sym, exc, reason)
                await self._rearm_stop(sym, pos.quantity, pos.entry_price)
                return False

            doc = await self._poll_order(oid, 30.0)
            filled, avg = parse_fill(doc)
            if filled < 1:
                logger.critical("[%s] Market sell %s shows no fill (%s). Verify at Schwab.", sym,
                                oid, doc.get("status"))
                await self._rearm_stop(sym, qty, pos.entry_price)
                return False
            price = avg if avg is not None else pos.last_price
            self.ledger.record_sell(sym, filled, price, simulated=False, execution_payload=doc)
            logger.info("[%s] Exit %s: sold %d @ %s", sym, reason, filled, price)
            try:
                from core.notifier import send_alert
                send_alert(
                    title=f"Order Filled: SELL {filled} {sym}",
                    message=f"Exit {reason}: sold {filled} {sym} at ${price}.",
                    priority="default",
                    tags=["moneybag", "outbox_tray"],
                )
            except Exception:
                pass
            return filled >= qty
        finally:
            self._exiting.discard(sym)

    async def _rearm_stop(self, sym: str, qty: int, entry: Decimal) -> None:
        stop_px = q(entry * (Decimal("1") - self.settings.tier2_stop_pct))
        try:
            sid = await asyncio.to_thread(self.om.place_broker_stop, sym, qty, float(stop_px))
            self._stop_ids[sym] = sid
            self._open_order_ids[sid] = sym
        except Exception as exc:
            logger.critical("[%s] Could not re-arm broker stop: %s", sym, exc)

    async def flatten_all(self, reason: str) -> int:
        """Cancels engine-placed orders only, then market-sells every engine position."""
        for oid in list(self._open_order_ids):
            try:
                await asyncio.to_thread(self.om.cancel_order, oid)
            except Exception as exc:
                logger.warning("Could not cancel engine order %s: %s", oid, exc)
            self._open_order_ids.pop(oid, None)
        self._stop_ids.clear()

        # Add a deterministic synchronization delay to allow the broker to confirm cancellations 
        # and the streamer to process any in-flight partial fills.
        await asyncio.sleep(2)

        closed = 0
        for pos in self.ledger.get_positions():
            if await self.exit_position(pos.symbol, reason, cancel_stop=False):
                closed += 1
        return closed

    # -- broker polling ------------------------------------------------------

    async def sync_broker_state(self, stop: asyncio.Event) -> None:
        """
        Broker background sync loop:
        - 45s during market hours (CORE_SESSION)
        - 300s during off-market hours
        - Throttled to 60s if daily call count exceeds 3,200
        - Suppressed if auth circuit breaker is open (AUTH_LOCKED)
        """
        sync = self.ctx.broker_sync
        from core.rate_limiter import SchwabRateLimiter
        limiter = SchwabRateLimiter.get_instance()

        while not stop.is_set():
            auth = self.ctx.auth_manager
            if auth and getattr(auth, "auth_circuit_open", False):
                logger.warning("LiveEngine: broker sync paused because auth circuit breaker is OPEN (AUTH_LOCKED).")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=60.0)
                except asyncio.TimeoutError:
                    pass
                continue

            try:
                await asyncio.to_thread(sync.sync_once)
            except Exception as exc:
                self.ctx.last_error = f"broker sync: {exc}"
                logger.error("Broker sync failed (%d consecutive): %s", sync.consecutive_failures, exc)

            sync.set_managed_symbols(self.ledger.positions.keys())

            phase = lifecycle_phase(now_et(), self.cfg)
            interval = sync.get_sync_interval(phase, limiter)
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    run_broker_sync = sync_broker_state


# ---------------------------------------------------------------------------
# Dry-run engine
# ---------------------------------------------------------------------------

class SimEngine(_EngineBase):
    env = "sandbox"

    def __init__(
        self,
        ctx: EngineContext,
        cfg: Dict[str, Any],
        ledger: SettlementLedger,
        settings: EngineSettings,
        router: Any,
        ignore_session: bool = False,
    ):
        super().__init__(ctx, cfg, ledger, settings, ignore_session)
        self.router = router
        reg = cfg.get("regime", {}) or {}
        self._trend_max = float(reg.get("ci_trend_threshold", 38.2))
        self._chop_min = float(reg.get("ci_chop_threshold", 61.8))
        orb = (cfg.get("strategies", {}) or {}).get("orb_15m", {}) or {}
        self._rr = Decimal(str(orb.get("risk_reward_target", 2.5)))

    async def handle_market_event(self, event: Any) -> None:
        sym = str(event.ticker).upper()
        price = Decimal(str(event.price))
        self.ctx.mark_tick("MOCK")
        self.ledger.mark_price(sym, price)
        code = regime_code_from_ci(float(event.choppiness_index), self._trend_max, self._chop_min)
        old_code = self.ctx.sim_regimes.get(sym)
        self.ctx.sim_regimes[sym] = code
        if old_code is not None and old_code != code and self.telemetry is not None:
            self.telemetry.record_event(
                aggregate_id=sym,
                event_type="REGIME_TRANSITION",
                payload={
                    "symbol": sym,
                    "old_regime": old_code,
                    "new_regime": code,
                    "choppiness_index": float(event.choppiness_index),
                    "category": "REGIME",
                    "message": f"Regime changed on {sym}: {old_code} -> {code} (CI={float(event.choppiness_index):.1f})",
                },
            )

        pos = self.ledger.positions.get(sym)
        if pos is not None:
            # Dynamic volatility stop ratchet evaluation
            if self.ctx.risk_manager is not None:
                yz_vol = float(getattr(event, "atr", 0.0)) / max(float(price), 1.0)
                hurst_h = getattr(event, "hurst", None)
                new_stop, ratcheted = self.ctx.risk_manager.evaluate_trailing_stop(
                    pos, float(price), yz_vol, k_stop=2.0, hurst_exponent=hurst_h
                )
                if ratcheted and self.telemetry is not None:
                    self.telemetry.record_event(
                        aggregate_id=sym,
                        event_type="DYNAMIC_STOP_RATCHET",
                        payload={
                            "symbol": sym,
                            "stop_price": str(pos.stop_price),
                            "high_water_mark": str(pos.high_water_mark),
                            "yz_vol": float(yz_vol),
                            "hurst": hurst_h,
                            "category": "RISK",
                            "message": f"Dynamic stop rose to ${pos.stop_price} on {sym}",
                        },
                    )
            if pos.stop_price is not None and price <= pos.stop_price:
                if self.telemetry is not None:
                    self.telemetry.record_event(
                        aggregate_id=sym,
                        event_type="TIER1_STOP_TRIGGER",
                        payload={
                            "symbol": sym,
                            "price": str(price),
                            "stop_price": str(pos.stop_price),
                            "category": "ORDER/FLOW",
                            "message": f"Tier-1 stop hit on {sym} @ ${price}",
                        },
                    )
                await self._sim_exit(sym, price, "TIER1_STOP")
            elif pos.target_price is not None and price >= pos.target_price:
                if self.telemetry is not None:
                    self.telemetry.record_event(
                        aggregate_id=sym,
                        event_type="TARGET_TRIGGER",
                        payload={
                            "symbol": sym,
                            "price": str(price),
                            "target_price": str(pos.target_price),
                            "category": "ORDER/FLOW",
                            "message": f"Target hit on {sym} @ ${price}",
                        },
                    )
                await self._sim_exit(sym, price, "TARGET")
            return

        if code != "A":
            return
        reason = self.entry_block_reason(sym)
        if reason:
            logger.debug("[%s] Sim entry blocked: %s", sym, reason)
            return

        self._in_flight.add(sym)
        try:
            risk = self.ctx.risk_manager
            posterior = risk.posterior_win_rate()
            bp = ledger_buying_power(self.ctx, "sandbox", regime=code, posterior=posterior)
            if bp is None:
                return
            shares = risk.determine_position_size(
                sym, float(event.atr), float(self.ledger.nlv),
                price=float(price), max_notional=float(bp.max_order_notional),
            )
            if not self.ignore_session:
                t_phase = get_session_phase(now_et(), self.cfg)
                _, phase_mult, _ = is_entry_permitted(t_phase, self.cfg)
                if phase_mult < 1.0:
                    scaled_shares = math.floor(shares * phase_mult)
                    logger.info("[%s] Sim temporal gate (%s) scaled sizing: %d -> %d", sym, t_phase.value, shares, scaled_shares)
                    shares = scaled_shares
            if shares < 1:
                return
            cost = q(price * shares)
            if not self.ledger.allocate_capital(sym, cost):
                return
            try:
                await self.router.execute_almgren_chriss_trajectory(sym, shares, side="BUY")
            except Exception as exc:
                self.ledger.cancel_reservation(sym)
                logger.error("[%s] Simulated routing failed: %s", sym, exc)
                return
            atr = Decimal(str(event.atr))
            stop = q(price - Decimal("1.5") * atr)
            target = q(price + self._rr * Decimal("1.5") * atr)
            self.ledger.confirm_buy(sym, shares, price, stop=stop, target=target, regime=code, simulated=True)
            logger.info("[%s] SIM entry: %d @ %s (stop %s, target %s)", sym, shares, price, stop, target)
            try:
                from core.notifier import send_alert
                send_alert(
                    title=f"Order Filled: BUY {shares} {sym} (SIM)",
                    message=f"Simulated fill: {shares} shares of {sym} at ${price} (Regime: {code}).",
                    priority="default",
                    tags=["shopping_cart", "chart_with_upwards_trend"],
                )
            except Exception:
                pass
        finally:
            self._in_flight.discard(sym)

    async def _sim_exit(self, sym: str, price: Decimal, reason: str) -> bool:
        if sym in self._exiting:
            return False
        pos = self.ledger.positions.get(sym)
        if pos is None:
            return False
        can_sell, veto_reason = self.ledger.can_sell_position(sym, pos.quantity)
        if not can_sell:
            logger.warning("[%s] SIM exit blocked by GFV protection: %s (holding overnight)", sym, veto_reason)
            return False
        self._exiting.add(sym)
        try:
            self.ledger.record_sell(sym, pos.quantity, price, simulated=True)
            logger.info("[%s] SIM exit %s @ %s", sym, reason, price)
            try:
                from core.notifier import send_alert
                send_alert(
                    title=f"Order Filled: SELL {pos.quantity} {sym} (SIM)",
                    message=f"Simulated exit {reason}: sold {pos.quantity} {sym} at ${price}.",
                    priority="default",
                    tags=["moneybag", "outbox_tray"],
                )
            except Exception:
                pass
            return True
        finally:
            self._exiting.discard(sym)

    async def flatten_all(self, reason: str) -> int:
        closed = 0
        for pos in self.ledger.get_positions():
            if await self._sim_exit(pos.symbol, pos.last_price, reason):
                closed += 1
        return closed
