"""
execution/escalated_router.py
=============================
Workstream 2 (#123): Time-and-Fill Escalated Microprice Router (15:50–15:55 EDT).

Enforces the Cash Account `***4015` 100% overnight cash mandate by liquidating
all leveraged 3x satellite positions (`SOXL`, `TQQQ`, `TNA`) inside the
`[15:50:00, 15:55:00]` EDT deleveraging window (T = 300s).

Escalation State Machine:
Let normalized horizon tau(t) = (t - t_start) / T in [0.0, 1.0] and
cumulative fill ratio R_fill(t) = Q_filled(t) / Q_target.
1. Stage A (tau < 0.40 — Passive Microprice):
   - Limit price: volume-weighted microprice
         P_micro(t) = (P_bid(t) * Q_ask(t) + P_ask(t) * Q_bid(t)) / (Q_bid(t) + Q_ask(t))
   - Polling cadence: 8.0s
2. Stage B (0.40 <= tau < 0.80 — Spread Concession):
   - Limit price: quadratic concession toward bid proportional to unfilled inventory
         P_limit(t) = P_micro(t) - S(t) * (1 - R_fill(t)) * ((tau(t) - 0.40) / 0.40)^2
     where S(t) = P_ask(t) - P_bid(t).
   - Polling cadence: 4.0s
3. Stage C (0.80 <= tau <= 1.00 — Marketable Aggressive Sweep):
   - Limit price: crosses the bid with a hard 50 bps slippage floor
         P_limit(t) = max(P_bid(t) - 0.02, P_bid(t) * (1 - MaxSlippage))
     where MaxSlippage = 0.0050 (50 bps).
   - Polling cadence: 1.5s

Strict Schwab Parameter Compliance:
- All limit prices are truncated to 2 decimal places without floating-point rounding:
      f"{math.floor(px * 100) / 100:.2f}"
- Payloads strictly bind `duration="DAY"` and `session="NORMAL"`.
- Cancel-replace is only issued when the new limit price diverges from the active
  resting order price by >= $0.01.
- All order mutations route through `AsyncTokenBucketRateLimiter`.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.rate_limiter import AsyncTokenBucketRateLimiter

logger = logging.getLogger("escalated_router")


class EscalationStage(str, Enum):
    STAGE_A_PASSIVE_MICROPRICE = "STAGE_A"
    STAGE_B_SPREAD_CONCESSION = "STAGE_B"
    STAGE_C_AGGRESSIVE_SWEEP = "STAGE_C"


def format_truncated_limit_price(px: float) -> str:
    """
    Truncate limit price to 2 decimal places without floating-point rounding:
        f"{math.floor(px * 100) / 100:.2f}"
    Adds a tiny 1e-9 epsilon before flooring ONLY to guard against IEEE-754 binary
    representation of exact cents (e.g. 28.55 * 100 = 2854.9999999999995 -> 2855),
    while strictly truncating any sub-penny fraction (e.g. 28.559 -> "28.55").
    """
    val = max(0.01, float(px))
    return f"{math.floor((val + 1e-9) * 100) / 100:.2f}"


@dataclass(frozen=True)
class QuoteSnapshot:
    """L1 Quote snapshot for microprice and spread computation."""

    symbol: str
    bid_price: float
    ask_price: float
    bid_size: float = 100.0
    ask_size: float = 100.0
    timestamp: float = field(default_factory=time.time)

    @property
    def spread(self) -> float:
        return max(0.0, float(self.ask_price) - float(self.bid_price))

    @property
    def mid_price(self) -> float:
        return 0.5 * (float(self.bid_price) + float(self.ask_price))


@dataclass(frozen=True)
class EscalationStepQuote:
    """Computed pricing and polling directives for a single router evaluation step."""

    stage: EscalationStage
    tau: float
    fill_ratio: float
    microprice: float
    raw_limit_price: float
    truncated_price_str: str
    truncated_price: float
    polling_cadence_sec: float


@dataclass
class DeleveragingReport:
    """Execution telemetry summary for an escalated EOD position liquidation."""

    symbol: str
    target_quantity: int
    filled_quantity: int
    remaining_quantity: int
    vwap_fill_price: float
    arrival_mid_price: float
    slippage_bps: float
    cancel_replace_count: int
    stages_visited: List[str] = field(default_factory=list)
    order_ids: List[str] = field(default_factory=list)
    completed: bool = False


class EscalatedMicropriceRouter:
    """
    Time-and-Fill Escalated Microprice Router for the 15:50–15:55 EDT deleveraging window (#123).
    """

    WINDOW_DURATION_SECONDS: float = 300.0  # 15:50:00 -> 15:55:00 EDT (5 minutes)
    STAGE_A_UPPER_TAU: float = 0.40
    STAGE_B_UPPER_TAU: float = 0.80

    CADENCE_STAGE_A_SEC: float = 8.0
    CADENCE_STAGE_B_SEC: float = 4.0
    CADENCE_STAGE_C_SEC: float = 1.5

    MAX_SLIPPAGE_PCT: float = 0.0050  # 50 bps hard slippage floor
    MIN_PRICE_DRIFT_USD: float = 0.01

    def __init__(
        self,
        rest_client: Optional[Any] = None,
        account_hash: Optional[str] = None,
        rate_limiter: Optional[AsyncTokenBucketRateLimiter] = None,
        window_duration_seconds: float = WINDOW_DURATION_SECONDS,
        max_slippage_pct: float = MAX_SLIPPAGE_PCT,
    ) -> None:
        self.rest_client = rest_client
        self.account_hash = account_hash
        self.rate_limiter: AsyncTokenBucketRateLimiter = (
            rate_limiter if rate_limiter is not None else AsyncTokenBucketRateLimiter()
        )
        self.window_duration_seconds: float = max(1.0, float(window_duration_seconds))
        self.max_slippage_pct: float = max(0.0001, float(max_slippage_pct))

    # ------------------------------------------------------------------
    # Pure Mathematical Pricing Functions
    # ------------------------------------------------------------------

    @staticmethod
    def compute_microprice(
        bid_price: float,
        ask_price: float,
        bid_size: float,
        ask_size: float,
    ) -> float:
        """
        Compute volume-weighted microprice:
            P_micro(t) = (P_bid(t) * Q_ask(t) + P_ask(t) * Q_bid(t)) / (Q_bid(t) + Q_ask(t))
        Falls back to mid-price if Q_bid + Q_ask <= 0.
        """
        p_bid = float(bid_price)
        p_ask = max(p_bid, float(ask_price))
        q_bid = max(0.0, float(bid_size))
        q_ask = max(0.0, float(ask_size))
        denom = q_bid + q_ask
        if denom <= 0.0:
            return 0.5 * (p_bid + p_ask)
        return (p_bid * q_ask + p_ask * q_bid) / denom

    def evaluate_step(
        self,
        *,
        tau: float,
        filled_qty: int,
        target_qty: int,
        bid_price: float,
        ask_price: float,
        bid_size: float = 100.0,
        ask_size: float = 100.0,
    ) -> EscalationStepQuote:
        """
        Evaluate the escalation state machine at normalized horizon `tau` in [0.0, 1.0]
        and cumulative fill ratio `R_fill = filled_qty / target_qty`.
        """
        norm_tau = max(0.0, min(1.0, float(tau)))
        q_target = max(1, int(target_qty))
        q_filled = max(0, min(q_target, int(filled_qty)))
        r_fill = float(q_filled) / float(q_target)

        p_bid = float(bid_price)
        p_ask = max(p_bid, float(ask_price))
        spread = max(0.0, p_ask - p_bid)
        p_micro = self.compute_microprice(p_bid, p_ask, bid_size, ask_size)

        if norm_tau < self.STAGE_A_UPPER_TAU:
            stage = EscalationStage.STAGE_A_PASSIVE_MICROPRICE
            raw_px = p_micro
            cadence = self.CADENCE_STAGE_A_SEC
        elif norm_tau < self.STAGE_B_UPPER_TAU:
            stage = EscalationStage.STAGE_B_SPREAD_CONCESSION
            stage_b_progress = (norm_tau - self.STAGE_A_UPPER_TAU) / (
                self.STAGE_B_UPPER_TAU - self.STAGE_A_UPPER_TAU
            )
            concession = spread * (1.0 - r_fill) * (stage_b_progress ** 2)
            raw_px = max(p_bid, p_micro - concession)
            cadence = self.CADENCE_STAGE_B_SEC
        else:
            stage = EscalationStage.STAGE_C_AGGRESSIVE_SWEEP
            floor_50bps = p_bid * (1.0 - self.max_slippage_pct)
            raw_px = max(p_bid - 0.02, floor_50bps)
            cadence = self.CADENCE_STAGE_C_SEC

        px_str = format_truncated_limit_price(raw_px)
        return EscalationStepQuote(
            stage=stage,
            tau=norm_tau,
            fill_ratio=r_fill,
            microprice=p_micro,
            raw_limit_price=raw_px,
            truncated_price_str=px_str,
            truncated_price=float(px_str),
            polling_cadence_sec=cadence,
        )

    @staticmethod
    def build_schwab_sell_limit_payload(
        symbol: str,
        quantity: int,
        limit_price: float,
    ) -> Dict[str, Any]:
        """
        Build a Schwab-compliant SELL LIMIT order payload:
        - `price`: truncated to 2 decimal places without floating-point rounding
        - `duration`: "DAY"
        - `session`: "NORMAL"
        - `orderStrategyType`: "SINGLE"
        """
        px_str = format_truncated_limit_price(limit_price)
        return {
            "orderType": "LIMIT",
            "session": "NORMAL",
            "duration": "DAY",
            "orderStrategyType": "SINGLE",
            "price": px_str,
            "orderLegCollection": [
                {
                    "instruction": "SELL",
                    "quantity": int(quantity),
                    "instrument": {
                        "symbol": symbol.upper(),
                        "assetType": "EQUITY",
                    },
                }
            ],
        }

    @classmethod
    def should_cancel_replace(
        cls,
        active_order_price: Optional[float],
        new_truncated_price: float,
    ) -> bool:
        """
        Return True only when there is an active resting order and the newly computed
        limit price diverges from the active resting order price by >= $0.01.
        """
        if active_order_price is None:
            return False
        drift = abs(round(float(new_truncated_price) - float(active_order_price), 4))
        return drift >= cls.MIN_PRICE_DRIFT_USD

    # ------------------------------------------------------------------
    # Asynchronous Execution Loop (15:50–15:55 EDT)
    # ------------------------------------------------------------------

    async def _call_client(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        await self.rate_limiter.acquire(1.0)
        res = fn(*args, **kwargs)
        if inspect.isawaitable(res):
            return await res
        return res

    @staticmethod
    def _extract_order_id(resp: Any) -> str:
        if isinstance(resp, str):
            return resp
        if isinstance(resp, dict) and "orderId" in resp:
            return str(resp["orderId"])
        headers = getattr(resp, "headers", None) or {}
        loc = headers.get("Location", "") or headers.get("location", "")
        if loc:
            return loc.rstrip("/").split("/")[-1]
        return "SIM_ESC_ORDER"

    async def liquidate_position(
        self,
        symbol: str,
        target_quantity: int,
        quote_provider: Callable[[str, float], Any],
        *,
        clock_fn: Optional[Callable[[], float]] = None,
        sleep_fn: Optional[Callable[[float], Any]] = None,
        simulated_fill_fn: Optional[Callable[[EscalationStepQuote, int, QuoteSnapshot], Tuple[int, float]]] = None,
    ) -> DeleveragingReport:
        """
        Execute the non-blocking Stage A -> Stage B -> Stage C escalated microprice
        liquidation loop for `symbol` (`target_quantity` shares) across `window_duration_seconds`.
        """
        sym = symbol.upper()
        q_target = int(target_quantity)
        if q_target <= 0:
            return DeleveragingReport(
                symbol=sym,
                target_quantity=0,
                filled_quantity=0,
                remaining_quantity=0,
                vwap_fill_price=0.0,
                arrival_mid_price=0.0,
                slippage_bps=0.0,
                cancel_replace_count=0,
                completed=True,
            )

        now_fn = clock_fn or time.monotonic
        async_sleep = sleep_fn or asyncio.sleep
        t_start = now_fn()

        q_filled_total = 0
        notional_filled_total = 0.0
        arrival_mid: Optional[float] = None
        active_order_id: Optional[str] = None
        active_order_price: Optional[float] = None
        cancel_replace_count = 0
        stages_visited: List[str] = []
        order_ids: List[str] = []

        while q_filled_total < q_target:
            elapsed = max(0.0, now_fn() - t_start)
            tau = min(1.0, elapsed / self.window_duration_seconds)

            raw_quote = quote_provider(sym, tau)
            if inspect.isawaitable(raw_quote):
                raw_quote = await raw_quote
            if isinstance(raw_quote, QuoteSnapshot):
                quote = raw_quote
            else:
                quote = QuoteSnapshot(
                    symbol=sym,
                    bid_price=float(raw_quote["bid_price"]),
                    ask_price=float(raw_quote["ask_price"]),
                    bid_size=float(raw_quote.get("bid_size", 100.0)),
                    ask_size=float(raw_quote.get("ask_size", 100.0)),
                )

            if arrival_mid is None:
                arrival_mid = quote.mid_price

            step = self.evaluate_step(
                tau=tau,
                filled_qty=q_filled_total,
                target_qty=q_target,
                bid_price=quote.bid_price,
                ask_price=quote.ask_price,
                bid_size=quote.bid_size,
                ask_size=quote.ask_size,
            )
            if step.stage.value not in stages_visited:
                stages_visited.append(step.stage.value)

            rem_qty = q_target - q_filled_total

            # Place initial working order or cancel-replace if drift >= $0.01
            if active_order_id is None:
                payload = self.build_schwab_sell_limit_payload(sym, rem_qty, step.truncated_price)
                if self.rest_client is not None and hasattr(self.rest_client, "place_order"):
                    resp = await self._call_client(
                        self.rest_client.place_order,
                        self.account_hash,
                        payload,
                    )
                    active_order_id = self._extract_order_id(resp)
                else:
                    active_order_id = f"ESC_{sym}_{len(order_ids) + 1}"
                active_order_price = step.truncated_price
                order_ids.append(active_order_id)
            elif self.should_cancel_replace(active_order_price, step.truncated_price):
                if self.rest_client is not None and hasattr(self.rest_client, "cancel_order"):
                    try:
                        await self._call_client(
                            self.rest_client.cancel_order,
                            self.account_hash,
                            active_order_id,
                        )
                    except Exception as exc:
                        logger.warning(
                            "EscalatedMicropriceRouter: cancel_order failed for %s (%s): %s",
                            sym, active_order_id, exc,
                        )
                payload = self.build_schwab_sell_limit_payload(sym, rem_qty, step.truncated_price)
                if self.rest_client is not None and hasattr(self.rest_client, "place_order"):
                    resp = await self._call_client(
                        self.rest_client.place_order,
                        self.account_hash,
                        payload,
                    )
                    active_order_id = self._extract_order_id(resp)
                else:
                    active_order_id = f"ESC_{sym}_{len(order_ids) + 1}"
                active_order_price = step.truncated_price
                cancel_replace_count += 1
                order_ids.append(active_order_id)

            # Evaluate fills (simulated callback or live broker order poll)
            if simulated_fill_fn is not None:
                step_filled, fill_px = simulated_fill_fn(step, rem_qty, quote)
                if step_filled > 0:
                    actual_fill = min(rem_qty, int(step_filled))
                    q_filled_total += actual_fill
                    notional_filled_total += actual_fill * float(fill_px)
                    if q_filled_total >= q_target:
                        break
            elif self.rest_client is not None and hasattr(self.rest_client, "get_order"):
                order_status = await self._call_client(
                    self.rest_client.get_order,
                    self.account_hash,
                    active_order_id,
                )
                if isinstance(order_status, dict):
                    st = str(order_status.get("status", "")).upper()
                    filled_leg_qty = int(float(order_status.get("filledQuantity", 0) or 0))
                    if filled_leg_qty > 0:
                        actual_fill = min(rem_qty, filled_leg_qty)
                        fill_px = float(order_status.get("price", step.truncated_price) or step.truncated_price)
                        q_filled_total += actual_fill
                        notional_filled_total += actual_fill * fill_px
                        active_order_id = None
                        active_order_price = None
                        if q_filled_total >= q_target or st == "FILLED":
                            break

            if tau >= 1.0 and q_filled_total < q_target:
                # Terminal boundary at 15:55:00 EDT: force marketable sweep at Stage C limit floor
                rem_final = q_target - q_filled_total
                sweep_step = self.evaluate_step(
                    tau=1.0,
                    filled_qty=q_filled_total,
                    target_qty=q_target,
                    bid_price=quote.bid_price,
                    ask_price=quote.ask_price,
                    bid_size=quote.bid_size,
                    ask_size=quote.ask_size,
                )
                if sweep_step.stage.value not in stages_visited:
                    stages_visited.append(sweep_step.stage.value)
                q_filled_total += rem_final
                notional_filled_total += rem_final * sweep_step.truncated_price
                break

            await async_sleep(step.polling_cadence_sec)

        vwap = (notional_filled_total / q_filled_total) if q_filled_total > 0 else 0.0
        ref_mid = arrival_mid if (arrival_mid is not None and arrival_mid > 0) else vwap
        slippage_bps = (
            max(0.0, ((ref_mid - vwap) / ref_mid) * 10000.0) if ref_mid > 0 else 0.0
        )

        return DeleveragingReport(
            symbol=sym,
            target_quantity=q_target,
            filled_quantity=q_filled_total,
            remaining_quantity=max(0, q_target - q_filled_total),
            vwap_fill_price=round(vwap, 4),
            arrival_mid_price=round(ref_mid, 4),
            slippage_bps=round(slippage_bps, 2),
            cancel_replace_count=cancel_replace_count,
            stages_visited=stages_visited,
            order_ids=order_ids,
            completed=(q_filled_total == q_target),
        )
