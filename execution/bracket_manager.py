"""
execution/bracket_manager.py
============================
Multi-Tranche Profit Scaling & Virtual Ratchet Bracket Manager (#62).

Architecture & Mathematical Governance:
---------------------------------------
1. 3-Tier Scaling Ladder:
   - Tranche 1 (Scale-Out Bank): 35% of total shares @ Entry + 2.5%.
     * Upon fill: Ratchets remaining stop to Breakeven (Entry + $0.02 regulatory fee buffer).
     * Marks position state as 'Risk-Free'.
   - Tranche 2 (Core Target): 35% of total shares @ Entry + 4.5%.
     * Upon fill: Locks trailing stop on Runner (Tranche 3) to at least Tranche 1 target price (+2.5%).
   - Tranche 3 (Runner): 30% of total shares trailing dynamic Yang-Zhang volatility envelope.
     * Guaranteed 100% terminal flush at 15:50:00 EDT.

2. Virtual vs. Broker Bracket Governance:
   - Take-profit triggers are maintained in memory as virtual triggers to eliminate order book clutter
     and avoid broker cancellation latency.
   - Catastrophic stop-loss is held as a contingent order on Schwab matching engines.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from enum import Enum
from typing import Any, Dict, List, Optional
try:
    from zoneinfo import ZoneInfo
    _EDT = ZoneInfo("America/New_York")
except ImportError:
    import pytz
    _EDT = pytz.timezone("America/New_York")

logger = logging.getLogger("SchwabEngine.BracketManager")


def _to_edt(dt: datetime) -> datetime:
    """Normalize any datetime (naive or timezone-aware) to America/New_York."""
    if dt.tzinfo is None:
        try:
            return dt.replace(tzinfo=_EDT)
        except Exception:
            return _EDT.localize(dt)
    return dt.astimezone(_EDT)


class ActionType(str, Enum):
    SELL = "SELL"
    UPDATE_STOP = "UPDATE_STOP"
    NOTIFY = "NOTIFY"


@dataclass
class BracketAction:
    """Action emitted by MultiTrancheBracket for execution routing."""
    action_type: str                     # "SELL" | "UPDATE_STOP"
    symbol: str
    shares: int
    price: float
    reason: str
    stop_price: Optional[float] = None
    tranche_id: Optional[int] = None
    timestamp: Optional[datetime] = None

    @property
    def is_sell(self) -> bool:
        return self.action_type == ActionType.SELL.value

    @property
    def is_stop_update(self) -> bool:
        return self.action_type == ActionType.UPDATE_STOP.value


class MultiTrancheBracket:
    """
    Multi-Tranche Profit Scaling & Virtual Ratchet Bracket Manager.

    Parameters per active position:
      - symbol: str
      - total_shares: int
      - entry_price: float
      - s0_reference: float
      - tranches: Dict[int, Dict] defining the 3-tier scaling ladder:
          * Tranche 1 (Scale-Out Bank): 35% of shares at Entry + 2.5%
          * Tranche 2 (Core Target): 35% of shares at Entry + 4.5%
          * Tranche 3 (Runner): 30% of shares trailing dynamic Yang-Zhang envelope until 15:50 EDT
    """

    def __init__(
        self,
        symbol: str,
        total_shares: int,
        entry_price: float,
        s0_reference: Optional[float] = None,
        initial_stop_pct: float = 0.018,
        breakeven_offset: float = 0.02,
        tranches: Optional[Dict[int, Dict]] = None,
        min_stop_distance_pct: float = 0.010,
        min_tp1_pct: float = 0.012,
        min_tp2_pct: float = 0.025,
        breakeven_delay_pct: float = 0.0080,
    ) -> None:
        if total_shares < 1:
            raise ValueError(f"total_shares must be >= 1, got {total_shares}")
        if entry_price <= 0:
            raise ValueError(f"entry_price must be > 0, got {entry_price}")

        self.symbol: str = symbol.upper()
        self.total_shares: int = int(total_shares)
        self.entry_price: float = float(entry_price)
        self.s0_reference: float = float(s0_reference) if s0_reference is not None else float(entry_price)
        self.min_stop_distance_pct: float = float(min_stop_distance_pct)
        self.min_tp1_pct: float = float(min_tp1_pct)
        self.min_tp2_pct: float = float(min_tp2_pct)
        self.breakeven_delay_pct: float = float(breakeven_delay_pct)
        self.initial_stop_pct: float = max(float(initial_stop_pct), self.min_stop_distance_pct)
        self.breakeven_offset: float = float(breakeven_offset)

        # Allocate shares across the 3 tranches: 35% / 35% / 30%
        t1_shares = math.floor(self.total_shares * 0.35)
        t2_shares = math.floor(self.total_shares * 0.35)
        t3_shares = self.total_shares - (t1_shares + t2_shares)

        # Handle small total_shares edge-cases
        if self.total_shares >= 3:
            if t1_shares == 0:
                t1_shares = 1
            if t2_shares == 0:
                t2_shares = 1
            t3_shares = self.total_shares - t1_shares - t2_shares
        elif self.total_shares == 2:
            t1_shares = 1
            t2_shares = 1
            t3_shares = 0
        elif self.total_shares == 1:
            t1_shares = 1
            t2_shares = 0
            t3_shares = 0

        # Build default or adopt custom tranches (enforcing minimum profit floors #140)
        t1_target_pct = max(0.025, self.min_tp1_pct)
        t2_target_pct = max(0.045, self.min_tp2_pct)
        t1_target_px = round(self.entry_price * (1.0 + t1_target_pct), 2)
        t2_target_px = round(self.entry_price * (1.0 + t2_target_pct), 2)

        if tranches is not None:
            self.tranches = tranches
            if 1 in self.tranches and "target_price" in self.tranches[1] and self.tranches[1]["target_price"] is not None:
                floor_tp1 = round(self.entry_price * (1.0 + self.min_tp1_pct), 2)
                self.tranches[1]["target_price"] = max(self.tranches[1]["target_price"], floor_tp1)
            if 2 in self.tranches and "target_price" in self.tranches[2] and self.tranches[2]["target_price"] is not None:
                floor_tp2 = round(self.entry_price * (1.0 + self.min_tp2_pct), 2)
                self.tranches[2]["target_price"] = max(self.tranches[2]["target_price"], floor_tp2)
        else:
            self.tranches = {
                1: {
                    "name": "Scale-Out Bank",
                    "pct_shares": 0.35,
                    "shares": t1_shares,
                    "target_pct": t1_target_pct,
                    "target_price": t1_target_px,
                    "filled": False,
                    "fill_price": None,
                    "fill_time": None,
                },
                2: {
                    "name": "Core Target",
                    "pct_shares": 0.35,
                    "shares": t2_shares,
                    "target_pct": t2_target_pct,
                    "target_price": t2_target_px,
                    "filled": False,
                    "fill_price": None,
                    "fill_time": None,
                },
                3: {
                    "name": "Runner",
                    "pct_shares": 0.30,
                    "shares": t3_shares,
                    "target_pct": None,
                    "target_price": None,
                    "filled": False,
                    "fill_price": None,
                    "fill_time": None,
                },
            }

        # Pricing anchors - enforce minimum stop distance floor
        initial_stop_raw = round(self.entry_price * (1.0 - self.initial_stop_pct), 2)
        max_allowed_stop = round(self.entry_price * (1.0 - self.min_stop_distance_pct), 2)
        self.initial_stop_price: float = min(initial_stop_raw, max_allowed_stop)
        self.breakeven_stop_price: float = round(self.entry_price + self.breakeven_offset, 2)
        self.current_stop_price: float = self.initial_stop_price

        # Progression & Accounting
        self.remaining_shares: int = self.total_shares
        self.tranche_1_filled: bool = False
        self.tranche_2_filled: bool = False
        self.breakeven_latched: bool = False
        self.is_risk_free: bool = False
        self.high_water_mark: float = self.entry_price
        self.realized_pnl: float = 0.0
        self.terminal_flushed: bool = False

    @property
    def stop_price(self) -> float:
        return self.current_stop_price

    @property
    def is_closed(self) -> bool:
        return self.remaining_shares <= 0

    def update_state(
        self,
        current_price: float,
        current_time: datetime,
        yz_vol: float = 0.0,
        hurst_exponent: Optional[float] = None,
    ) -> List[BracketAction]:
        """
        Dynamic Ratchet State Transitions:
          - State Initial: All shares protected by initial stop (Entry - 1.8%).
          - Trigger Tranche 1 Fill (+2.5%):
              * Move stop on remaining 65% of shares to Breakeven (Entry Price + $0.02).
              * Mark trade as 'Risk-Free'.
          - Trigger Tranche 2 Fill (+4.5%):
              * Lock trailing stop on Runner (Tranche 3) to at least Tranche 1 target price (+2.5%).
          - Terminal Flush (15:50:00 EDT):
              * Force exit on all unfulfilled tranches to guarantee 100% cash clearance.
        """
        if self.remaining_shares <= 0:
            return []

        px = float(current_price)
        if px > self.high_water_mark:
            self.high_water_mark = px

        # ------------------------------------------------------------------
        # 1. Terminal Flush (15:50:00 EDT)
        # ------------------------------------------------------------------
        time_edt = _to_edt(current_time).time()
        if time_edt >= dtime(15, 50, 0):
            flush_qty = self.remaining_shares
            self.remaining_shares = 0
            self.terminal_flushed = True

            for t_id in (1, 2, 3):
                t = self.tranches[t_id]
                if not t["filled"] and t["shares"] > 0:
                    t["filled"] = True
                    t["fill_price"] = px
                    t["fill_time"] = current_time

            self.realized_pnl += round(flush_qty * (px - self.entry_price), 2)
            logger.warning(
                "[%s] 15:50 EDT Terminal Flush triggered! Closing remaining %d shares @ $%.2f",
                self.symbol, flush_qty, px,
            )
            return [
                BracketAction(
                    action_type=ActionType.SELL.value,
                    symbol=self.symbol,
                    shares=flush_qty,
                    price=px,
                    reason="TERMINAL_FLUSH",
                    timestamp=current_time,
                )
            ]

        # ------------------------------------------------------------------
        # 2. Stop Loss Check (Active Stop breached)
        # ------------------------------------------------------------------
        if px <= self.current_stop_price:
            exit_qty = self.remaining_shares
            self.remaining_shares = 0

            if self.tranche_2_filled:
                reason = "RUNNER_TRAILING_STOP"
            elif self.breakeven_latched:
                reason = "BREAKEVEN_STOP"
            else:
                reason = "INITIAL_STOP"

            for t_id in (1, 2, 3):
                t = self.tranches[t_id]
                if not t["filled"] and t["shares"] > 0:
                    t["filled"] = True
                    t["fill_price"] = px
                    t["fill_time"] = current_time

            self.realized_pnl += round(exit_qty * (px - self.entry_price), 2)
            logger.info(
                "[%s] %s hit @ $%.2f (stop=$%.2f). Exiting remaining %d shares. Net PnL: $%.2f",
                self.symbol, reason, px, self.current_stop_price, exit_qty, self.realized_pnl,
            )
            return [
                BracketAction(
                    action_type=ActionType.SELL.value,
                    symbol=self.symbol,
                    shares=exit_qty,
                    price=px,
                    reason=reason,
                    stop_price=self.current_stop_price,
                    timestamp=current_time,
                )
            ]

        # ------------------------------------------------------------------
        # 3. Virtual Take-Profit Targets
        # ------------------------------------------------------------------
        actions: List[BracketAction] = []

        # --- Tranche 1: Scale-Out Bank (+2.5%) ---
        unrealized_gain_pct = (self.high_water_mark - self.entry_price) / self.entry_price
        breakeven_permitted = unrealized_gain_pct >= self.breakeven_delay_pct

        t1 = self.tranches[1]
        if not self.tranche_1_filled and t1["shares"] > 0:
            target_1 = t1["target_price"]
            if px >= target_1:
                t1["filled"] = True
                t1["fill_price"] = px
                t1["fill_time"] = current_time
                self.tranche_1_filled = True
                self.is_risk_free = True

                sold_qty = t1["shares"]
                self.remaining_shares -= sold_qty
                self.realized_pnl += round(sold_qty * (px - self.entry_price), 2)

                # Move stop to Breakeven (Entry Price + $0.02) only after unrealized gain exceeds +0.80% (#140)
                if breakeven_permitted:
                    self.breakeven_latched = True
                    self.current_stop_price = max(self.current_stop_price, self.breakeven_stop_price)
                    logger.info(
                        "[%s] Tranche 1 Filled @ $%.2f! Sold %d shares. Ratcheted stop to Breakeven $%.2f (gain +%.2f%% >= +%.2f%%). State: Risk-Free.",
                        self.symbol, px, sold_qty, self.current_stop_price, unrealized_gain_pct * 100, self.breakeven_delay_pct * 100,
                    )
                else:
                    logger.info(
                        "[%s] Tranche 1 Filled @ $%.2f! Sold %d shares. Breakeven ratchet delayed (gain +%.2f%% < +%.2f%%). State: Risk-Free.",
                        self.symbol, px, sold_qty, unrealized_gain_pct * 100, self.breakeven_delay_pct * 100,
                    )

                actions.append(
                    BracketAction(
                        action_type=ActionType.SELL.value,
                        symbol=self.symbol,
                        shares=sold_qty,
                        price=px,
                        reason="TRANCHE_1_TARGET",
                        tranche_id=1,
                        timestamp=current_time,
                    )
                )
                if self.breakeven_latched:
                    actions.append(
                        BracketAction(
                            action_type=ActionType.UPDATE_STOP.value,
                            symbol=self.symbol,
                            shares=self.remaining_shares,
                            price=px,
                            reason="RATCHET_BREAKEVEN",
                            stop_price=self.current_stop_price,
                            tranche_id=1,
                            timestamp=current_time,
                        )
                    )

        # Check delayed breakeven ratchet if Tranche 1 already filled but gain just crossed +0.80%
        if self.tranche_1_filled and not self.breakeven_latched and breakeven_permitted and self.remaining_shares > 0:
            self.breakeven_latched = True
            self.current_stop_price = max(self.current_stop_price, self.breakeven_stop_price)
            logger.info(
                "[%s] Delayed breakeven threshold crossed (+%.2f%% >= +%.2f%%)! Ratcheted stop to Breakeven $%.2f.",
                self.symbol, unrealized_gain_pct * 100, self.breakeven_delay_pct * 100, self.current_stop_price,
            )
            actions.append(
                BracketAction(
                    action_type=ActionType.UPDATE_STOP.value,
                    symbol=self.symbol,
                    shares=self.remaining_shares,
                    price=px,
                    reason="RATCHET_BREAKEVEN",
                    stop_price=self.current_stop_price,
                    tranche_id=1,
                    timestamp=current_time,
                )
            )

        # --- Tranche 2: Core Target (+4.5%) ---
        t2 = self.tranches[2]
        if self.tranche_1_filled and not self.tranche_2_filled and t2["shares"] > 0:
            target_2 = t2["target_price"]
            if px >= target_2:
                t2["filled"] = True
                t2["fill_price"] = px
                t2["fill_time"] = current_time
                self.tranche_2_filled = True

                sold_qty = t2["shares"]
                self.remaining_shares -= sold_qty
                self.realized_pnl += round(sold_qty * (px - self.entry_price), 2)

                # Lock trailing stop on Runner (Tranche 3) to at least Tranche 1 target price (+2.5%)
                t1_target = self.tranches[1]["target_price"]
                self.current_stop_price = max(self.current_stop_price, t1_target)

                logger.info(
                    "[%s] Tranche 2 Filled @ $%.2f! Sold %d shares. Locked Runner stop to at least Tranche 1 target $%.2f.",
                    self.symbol, px, sold_qty, self.current_stop_price,
                )
                actions.append(
                    BracketAction(
                        action_type=ActionType.SELL.value,
                        symbol=self.symbol,
                        shares=sold_qty,
                        price=px,
                        reason="TRANCHE_2_TARGET",
                        tranche_id=2,
                        timestamp=current_time,
                    )
                )
                actions.append(
                    BracketAction(
                        action_type=ActionType.UPDATE_STOP.value,
                        symbol=self.symbol,
                        shares=self.remaining_shares,
                        price=px,
                        reason="LOCK_RUNNER_STOP",
                        stop_price=self.current_stop_price,
                        tranche_id=2,
                        timestamp=current_time,
                    )
                )

        # --- Tranche 3: Runner Trailing Yang-Zhang Dynamic Volatility Envelope ---
        t3 = self.tranches[3]
        if self.remaining_shares > 0 and t3["shares"] > 0:
            if yz_vol > 0:
                # Dynamic volatility stop calculation
                vol = yz_vol * math.sqrt(252.0) if yz_vol < 0.20 else yz_vol
                time_factor = math.sqrt((15.0 / 390.0) / 252.0)
                dist = self.high_water_mark * (2.0 * vol * time_factor)
                dist = max(dist, self.high_water_mark * self.min_stop_distance_pct)
                candidate_stop = round(self.high_water_mark - dist, 2)

                # Delayed breakeven ratchet: Trailing stop cannot ratchet to or above breakeven until gain > +0.80%
                if candidate_stop >= self.entry_price and not breakeven_permitted:
                    candidate_stop = round(self.entry_price * (1.0 - self.min_stop_distance_pct), 2)

                # Regime Gating via Hurst Exponent if supplied
                ratchet_permitted = True
                if hurst_exponent is not None:
                    h = float(hurst_exponent)
                    if h <= 0.50:
                        ratchet_permitted = False
                    elif h > 0.55:
                        ratchet_permitted = True

                # Tranche 2 locked floor guarantee
                if self.tranche_2_filled:
                    t1_target = self.tranches[1]["target_price"]
                    candidate_stop = max(candidate_stop, t1_target)
                elif self.tranche_1_filled and self.breakeven_latched:
                    candidate_stop = max(candidate_stop, self.breakeven_stop_price)

                if candidate_stop > self.current_stop_price and ratchet_permitted:
                    self.current_stop_price = candidate_stop
                    actions.append(
                        BracketAction(
                            action_type=ActionType.UPDATE_STOP.value,
                            symbol=self.symbol,
                            shares=self.remaining_shares,
                            price=px,
                            reason="RUNNER_TRAILING_RATCHET",
                            stop_price=self.current_stop_price,
                            tranche_id=3,
                            timestamp=current_time,
                        )
                    )

        return actions

    def to_dict(self) -> Dict[str, Any]:
        """Serializes current bracket state for telemetry and auditing."""
        return {
            "symbol": self.symbol,
            "total_shares": self.total_shares,
            "entry_price": self.entry_price,
            "s0_reference": self.s0_reference,
            "remaining_shares": self.remaining_shares,
            "current_stop_price": self.current_stop_price,
            "initial_stop_price": self.initial_stop_price,
            "breakeven_stop_price": self.breakeven_stop_price,
            "tranche_1_filled": self.tranche_1_filled,
            "tranche_2_filled": self.tranche_2_filled,
            "breakeven_latched": self.breakeven_latched,
            "is_risk_free": self.is_risk_free,
            "high_water_mark": self.high_water_mark,
            "realized_pnl": self.realized_pnl,
            "terminal_flushed": self.terminal_flushed,
            "tranches": self.tranches,
        }


class MultiTrancheBracketManager:
    """Multi-position manager orchestrating active MultiTrancheBrackets across symbols."""

    def __init__(self) -> None:
        self.brackets: Dict[str, MultiTrancheBracket] = {}

    def add_bracket(self, bracket: MultiTrancheBracket) -> None:
        self.brackets[bracket.symbol.upper()] = bracket

    def create_bracket(
        self,
        symbol: str,
        total_shares: int,
        entry_price: float,
        s0_reference: Optional[float] = None,
        min_stop_distance_pct: float = 0.010,
        min_tp1_pct: float = 0.012,
        min_tp2_pct: float = 0.025,
        breakeven_delay_pct: float = 0.0080,
    ) -> MultiTrancheBracket:
        bracket = MultiTrancheBracket(
            symbol=symbol,
            total_shares=total_shares,
            entry_price=entry_price,
            s0_reference=s0_reference,
            min_stop_distance_pct=min_stop_distance_pct,
            min_tp1_pct=min_tp1_pct,
            min_tp2_pct=min_tp2_pct,
            breakeven_delay_pct=breakeven_delay_pct,
        )
        self.add_bracket(bracket)
        return bracket

    def get_bracket(self, symbol: str) -> Optional[MultiTrancheBracket]:
        return self.brackets.get(symbol.upper())

    def remove_bracket(self, symbol: str) -> Optional[MultiTrancheBracket]:
        return self.brackets.pop(symbol.upper(), None)

    def update_state(
        self,
        symbol: str,
        current_price: float,
        current_time: datetime,
        yz_vol: float = 0.0,
        hurst_exponent: Optional[float] = None,
    ) -> List[BracketAction]:
        bracket = self.get_bracket(symbol)
        if bracket is not None:
            return bracket.update_state(
                current_price, current_time, yz_vol=yz_vol, hurst_exponent=hurst_exponent
            )
        return []
