"""
core/t1_ledger.py
=================
Regulatory Cash Ledger & SEC Rule 15c6-1 (T+1 Settlement) State Machine
for Charles Schwab Cash Account `***4015` (#123 Workstream 3).

Architectural Invariants:
1. Sub-$25,000 cash accounts cannot trade on margin. Under SEC Rule 15c6-1,
   sale proceeds clear at T+1 (00:00:00 EDT on the next settlement session).
2. Three-Bucket Capital Segregation:
       C_total(t) = C_settled(t) + C_unsettled(t) + C_reserved(t)
   - C_settled:   Cleared funds available for unrestricted deployment and immediate intraday exit.
   - C_unsettled: Maturing proceeds from intraday sales locked until T+1 clearing.
   - C_reserved:  Funds committed to active working limit buy orders.
3. Pre-Trade GFV Firewall (`can_submit_order`):
       Max Allowable Buy Notional = max(0.00, C_settled - C_reserved - (NLV * CashFloorPct))
   Rejects any BUY order where notional exceeds liquid C_settled with explicit
   `GFV REJECTION`, completely forbidding consumption of C_unsettled for intraday buys.
4. Autonomous Overnight Rollover (`process_t1_overnight_clearing`):
   Scheduled for 00:00:00 EDT to sweep matured C_unsettled into C_settled.
5. Thread safety via `threading.RLock()` and strict `Decimal` accounting quantized
   with `ROUND_HALF_UP` to two decimal places (`0.01`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as dt_time
from decimal import Decimal, ROUND_HALF_UP
import logging
import threading
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

EDT = ZoneInfo("America/New_York")
CENT = Decimal("0.01")
ZERO = Decimal("0.00")


class GFVRejectionError(RuntimeError):
    """Raised when a BUY order violates the SEC Rule 15c6-1 Pre-Trade GFV Firewall."""


def quantize_usd(value: Decimal | float | int | str) -> Decimal:
    """Quantize any numeric input to 2 decimal places using ROUND_HALF_UP."""
    if isinstance(value, Decimal):
        raw = value
    else:
        raw = Decimal(str(value))
    return raw.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class UnsettledSaleLot:
    """Represents proceeds from an intraday sale maturing at T+1 (00:00:00 EDT)."""
    lot_id: str
    symbol: str
    proceeds: Decimal
    trade_date: date
    settle_date: date
    recorded_at: datetime


@dataclass(frozen=True)
class T1LedgerSnapshot:
    """Immutable point-in-time snapshot of the three-bucket T+1 cash state machine."""
    settled_cash: Decimal
    unsettled_cash: Decimal
    reserved_cash: Decimal
    total_cash: Decimal
    nlv: Decimal
    cash_floor_pct: Decimal
    max_allowable_buy_notional: Decimal


class T1CashLedger:
    """
    SEC Rule 15c6-1 (T+1 Cash Settlement) State Machine for Cash Account `***4015`.

    Maintains strict three-bucket segregation:
        C_total(t) = C_settled(t) + C_unsettled(t) + C_reserved(t)
    with `threading.RLock()` concurrency protection and `Decimal` (`ROUND_HALF_UP`)
    two-decimal-place quantization.
    """

    OVERNIGHT_CLEARING_TIME: dt_time = dt_time(0, 0, 0)

    def __init__(
        self,
        settled_cash: Decimal | float | int | str = Decimal("4400.00"),
        unsettled_cash: Decimal | float | int | str = Decimal("0.00"),
        reserved_cash: Decimal | float | int | str = Decimal("0.00"),
        nlv: Optional[Decimal | float | int | str] = None,
        cash_floor_pct: Decimal | float | int | str = Decimal("0.00"),
        account_id: str = "***4015",
    ) -> None:
        self._lock: threading.RLock = threading.RLock()
        self.account_id: str = account_id

        c_settled = quantize_usd(settled_cash)
        c_unsettled = quantize_usd(unsettled_cash)
        c_reserved = quantize_usd(reserved_cash)

        if c_settled < ZERO:
            raise ValueError(f"settled_cash cannot be negative: {c_settled}")
        if c_unsettled < ZERO:
            raise ValueError(f"unsettled_cash cannot be negative: {c_unsettled}")
        if c_reserved < ZERO:
            raise ValueError(f"reserved_cash cannot be negative: {c_reserved}")

        self._settled_cash: Decimal = c_settled
        self._unsettled_cash: Decimal = c_unsettled
        self._reserved_cash: Decimal = c_reserved
        self._cash_floor_pct: Decimal = Decimal(str(cash_floor_pct))
        self._explicit_nlv: Optional[Decimal] = quantize_usd(nlv) if nlv is not None else None

        # Active working BUY order reservations: order_id -> reserved Decimal notional
        self._order_reservations: Dict[str, Decimal] = {}
        # Pending unsettled sale lots awaiting T+1 00:00:00 EDT rollover
        self._unsettled_lots: List[UnsettledSaleLot] = []
        self._lot_seq: int = 0
        self._last_clearing_date: Optional[date] = None

    # ------------------------------------------------------------------
    # Three-Bucket Capital Properties
    # ------------------------------------------------------------------

    @property
    def settled_cash(self) -> Decimal:
        """C_settled: Cleared funds available for unrestricted deployment."""
        with self._lock:
            return self._settled_cash

    @property
    def unsettled_cash(self) -> Decimal:
        """C_unsettled: Maturing proceeds from intraday sales locked until T+1 clearing."""
        with self._lock:
            return self._unsettled_cash

    @property
    def reserved_cash(self) -> Decimal:
        """C_reserved: Funds committed to active working limit buy orders."""
        with self._lock:
            return self._reserved_cash

    @property
    def total_cash(self) -> Decimal:
        """C_total(t) = C_settled(t) + C_unsettled(t) + C_reserved(t)."""
        with self._lock:
            return quantize_usd(self._settled_cash + self._unsettled_cash + self._reserved_cash)

    @property
    def liquid_settled_cash(self) -> Decimal:
        """Unencumbered settled cash: max(0.00, C_settled - C_reserved)."""
        with self._lock:
            return max(ZERO, quantize_usd(self._settled_cash - self._reserved_cash))

    @property
    def nlv(self) -> Decimal:
        """Current Net Liquidating Value (NLV)."""
        with self._lock:
            if self._explicit_nlv is not None:
                return self._explicit_nlv
            return self.total_cash

    @nlv.setter
    def nlv(self, value: Decimal | float | int | str) -> None:
        with self._lock:
            self._explicit_nlv = quantize_usd(value)

    @property
    def cash_floor_pct(self) -> Decimal:
        with self._lock:
            return self._cash_floor_pct

    @cash_floor_pct.setter
    def cash_floor_pct(self, value: Decimal | float | int | str) -> None:
        with self._lock:
            self._cash_floor_pct = Decimal(str(value))

    # ------------------------------------------------------------------
    # Pre-Trade GFV Firewall
    # ------------------------------------------------------------------

    def compute_max_allowable_buy_notional(
        self,
        nlv: Optional[Decimal | float | int | str] = None,
        cash_floor_pct: Optional[Decimal | float | int | str] = None,
    ) -> Decimal:
        """
        Compute maximum allowable buy notional under the Pre-Trade GFV Firewall:
            Max Allowable Buy Notional = max(0.00, C_settled - C_reserved - (NLV * CashFloorPct))
        """
        with self._lock:
            eff_nlv = quantize_usd(nlv) if nlv is not None else self.nlv
            eff_floor_pct = Decimal(str(cash_floor_pct)) if cash_floor_pct is not None else self._cash_floor_pct
            cash_floor_usd = quantize_usd(eff_nlv * eff_floor_pct)
            available = quantize_usd(self._settled_cash - self._reserved_cash - cash_floor_usd)
            return max(ZERO, available)

    def can_submit_order(
        self,
        side: str = "BUY",
        notional: Decimal | float | int | str = Decimal("0.00"),
        *,
        symbol: str = "",
        quantity: Optional[int] = None,
        price: Optional[Decimal | float | int | str] = None,
        nlv: Optional[Decimal | float | int | str] = None,
        cash_floor_pct: Optional[Decimal | float | int | str] = None,
    ) -> Tuple[bool, str]:
        """
        Pre-Trade GFV Firewall (#123 Workstream 3):
        - Enforces:
              Max Allowable Buy Notional = max(0.00, C_settled - C_reserved - (NLV * CashFloorPct))
        - Rejects any BUY order where notional exceeds liquid settled cash,
          returning `(False, "GFV REJECTION: ...")`.
        - SELL orders of settled positions are permitted to convert holdings to cash.
        """
        side_norm = str(side).upper().strip()
        with self._lock:
            if quantity is not None and price is not None and quantize_usd(notional) == ZERO:
                order_notional = quantize_usd(Decimal(str(quantity)) * Decimal(str(price)))
            else:
                order_notional = quantize_usd(notional)

            if side_norm == "SELL":
                return True, f"SELL order permitted for {symbol or 'position'} (proceeds route to C_unsettled for T+1)."

            if order_notional <= ZERO:
                return False, f"GFV REJECTION: Non-positive BUY notional ${order_notional:.2f}."

            max_buy = self.compute_max_allowable_buy_notional(nlv=nlv, cash_floor_pct=cash_floor_pct)
            if order_notional > max_buy:
                reason = (
                    f"GFV REJECTION: BUY {symbol + ' ' if symbol else ''}notional ${order_notional:.2f} "
                    f"exceeds Max Allowable Buy Notional ${max_buy:.2f} "
                    f"(C_settled=${self._settled_cash:.2f}, C_reserved=${self._reserved_cash:.2f}, "
                    f"C_unsettled=${self._unsettled_cash:.2f} locked until T+1 00:00:00 EDT)."
                )
                logger.warning("T1CashLedger: %s", reason)
                return False, reason

            return True, (
                f"APPROVED (OK): BUY notional ${order_notional:.2f} within liquid settled cash "
                f"${max_buy:.2f} (C_settled=${self._settled_cash:.2f})."
            )

    # ------------------------------------------------------------------
    # Working Order Reservation & Fill State Transitions
    # ------------------------------------------------------------------

    def reserve_for_buy_order(
        self,
        order_id: str,
        notional: Decimal | float | int | str,
        *,
        symbol: str = "",
        nlv: Optional[Decimal | float | int | str] = None,
        cash_floor_pct: Optional[Decimal | float | int | str] = None,
    ) -> Decimal:
        """
        Commit funds to an active working limit BUY order.
        Segregates capital from C_settled into C_reserved while preserving C_total(t).
        Raises `GFVRejectionError` if the order violates the Pre-Trade GFV Firewall.
        """
        amt = quantize_usd(notional)
        with self._lock:
            ok, reason = self.can_submit_order(
                "BUY",
                amt,
                symbol=symbol,
                nlv=nlv,
                cash_floor_pct=cash_floor_pct,
            )
            if not ok:
                raise GFVRejectionError(reason)

            # Segregate from C_settled into C_reserved so C_total = C_settled + C_unsettled + C_reserved
            self._settled_cash = quantize_usd(self._settled_cash - amt)
            self._reserved_cash = quantize_usd(self._reserved_cash + amt)
            self._order_reservations[order_id] = quantize_usd(
                self._order_reservations.get(order_id, ZERO) + amt
            )
            logger.info(
                "T1CashLedger: reserved $%s for working BUY order %s (%s) | "
                "C_settled=$%s, C_reserved=$%s, C_unsettled=$%s",
                amt, order_id, symbol, self._settled_cash, self._reserved_cash, self._unsettled_cash,
            )
            return amt

    def release_buy_reservation(self, order_id: str) -> Decimal:
        """
        Release reserved funds from a cancelled or expired working limit BUY order
        from C_reserved back into C_settled.
        """
        with self._lock:
            reserved_amt = self._order_reservations.pop(order_id, ZERO)
            if reserved_amt > ZERO:
                self._reserved_cash = max(ZERO, quantize_usd(self._reserved_cash - reserved_amt))
                self._settled_cash = quantize_usd(self._settled_cash + reserved_amt)
                logger.info(
                    "T1CashLedger: released reservation $%s for order %s back to C_settled ($%s).",
                    reserved_amt, order_id, self._settled_cash,
                )
            return reserved_amt

    def record_buy_fill(
        self,
        notional_or_symbol: Decimal | float | int | str,
        quantity: Optional[int] = None,
        price: Optional[Decimal | float | int | str] = None,
        *,
        order_id: Optional[str] = None,
        symbol: str = "",
        nlv: Optional[Decimal | float | int | str] = None,
        cash_floor_pct: Optional[Decimal | float | int | str] = None,
    ) -> Decimal:
        """
        Record a completed BUY execution fill.
        Accepts either `(notional, *, order_id=..., symbol=...)` or
        `(symbol, quantity, price, *, order_id=...)`.
        - If the order had a working reservation in C_reserved, consumes C_reserved
          and refunds any price improvement back to C_settled.
        - Otherwise validates via `can_submit_order` and deducts directly from C_settled.
        """
        if quantity is not None and price is not None:
            sym = str(notional_or_symbol)
            fill_cost = quantize_usd(Decimal(str(quantity)) * Decimal(str(price)))
        else:
            sym = symbol
            fill_cost = quantize_usd(notional_or_symbol)
        with self._lock:
            if order_id is not None and order_id in self._order_reservations:
                reserved_amt = self._order_reservations.pop(order_id)
                self._reserved_cash = max(ZERO, quantize_usd(self._reserved_cash - reserved_amt))
                diff = quantize_usd(reserved_amt - fill_cost)
                if diff >= ZERO:
                    # Refund unused reservation (price improvement) to C_settled
                    self._settled_cash = quantize_usd(self._settled_cash + diff)
                else:
                    extra = abs(diff)
                    if extra > self._settled_cash:
                        raise GFVRejectionError(
                            f"GFV REJECTION: BUY fill overrun ${extra:.2f} exceeds C_settled ${self._settled_cash:.2f}."
                        )
                    self._settled_cash = quantize_usd(self._settled_cash - extra)
                return fill_cost

            ok, reason = self.can_submit_order(
                "BUY",
                fill_cost,
                symbol=sym,
                nlv=nlv,
                cash_floor_pct=cash_floor_pct,
            )
            if not ok:
                raise GFVRejectionError(reason)

            self._settled_cash = quantize_usd(self._settled_cash - fill_cost)
            logger.info(
                "T1CashLedger: BUY fill %s for $%s deducted from C_settled (remaining C_settled=$%s).",
                sym, fill_cost, self._settled_cash,
            )
            return fill_cost

    def record_sell_fill(
        self,
        proceeds_or_symbol: Decimal | float | int | str,
        quantity: Optional[int] = None,
        price: Optional[Decimal | float | int | str] = None,
        *,
        symbol: str = "",
        when: Optional[datetime] = None,
        settle_date: Optional[date] = None,
    ) -> Decimal:
        """
        Record an intraday SELL execution fill.
        Accepts either `(proceeds, *, symbol=..., when=...)` or
        `(symbol, quantity, price, *, when=...)`.
        Under SEC Rule 15c6-1 (T+1), sale proceeds are strictly credited to
        `C_unsettled` and locked until `process_t1_overnight_clearing` at 00:00:00 EDT.
        """
        if quantity is not None and price is not None:
            sym = str(proceeds_or_symbol)
            gross_proceeds = quantize_usd(Decimal(str(quantity)) * Decimal(str(price)))
        else:
            sym = symbol
            gross_proceeds = quantize_usd(proceeds_or_symbol)
        if gross_proceeds < ZERO:
            raise ValueError(f"Sale proceeds cannot be negative: {gross_proceeds}")

        with self._lock:
            now_dt = when or datetime.now(EDT)
            if now_dt.tzinfo is None:
                now_dt = now_dt.replace(tzinfo=EDT)
            else:
                now_dt = now_dt.astimezone(EDT)
            trade_day = now_dt.date()
            from core.ledger import next_business_day
            eff_settle_date = settle_date or next_business_day(trade_day)

            self._lot_seq += 1
            lot = UnsettledSaleLot(
                lot_id=f"LOT_{self._lot_seq:06d}",
                symbol=sym.upper() if sym else "EQUITY",
                proceeds=gross_proceeds,
                trade_date=trade_day,
                settle_date=eff_settle_date,
                recorded_at=now_dt,
            )
            self._unsettled_lots.append(lot)
            self._unsettled_cash = quantize_usd(self._unsettled_cash + gross_proceeds)

            logger.info(
                "T1CashLedger: SELL fill %s credited $%s to C_unsettled (settle_date=%s) | "
                "C_settled=$%s, C_unsettled=$%s, C_reserved=$%s",
                sym, gross_proceeds, eff_settle_date,
                self._settled_cash, self._unsettled_cash, self._reserved_cash,
            )
            return gross_proceeds

    # ------------------------------------------------------------------
    # Autonomous Overnight Rollover (00:00:00 EDT)
    # ------------------------------------------------------------------

    def process_t1_overnight_clearing(
        self,
        clearing_dt: Optional[datetime | date] = None,
        sweep_all_unsettled: bool = True,
        *,
        current_dt: Optional[datetime | date] = None,
    ) -> Decimal:
        """
        Autonomous Overnight Rollover handler scheduled for 00:00:00 EDT (#123 Workstream 3).
        Sweeps matured `C_unsettled` proceeds into `C_settled`.
        Returns the total `Decimal` amount swept from `C_unsettled` to `C_settled`.
        """
        eff_dt = current_dt if current_dt is not None else clearing_dt
        with self._lock:
            if eff_dt is None:
                eff_date = datetime.now(EDT).date()
            elif isinstance(eff_dt, datetime):
                eff_date = eff_dt.astimezone(EDT).date() if eff_dt.tzinfo else eff_dt.date()
            else:
                eff_date = eff_dt

            if sweep_all_unsettled or not self._unsettled_lots:
                swept = self._unsettled_cash
                self._settled_cash = quantize_usd(self._settled_cash + swept)
                self._unsettled_cash = ZERO
                self._unsettled_lots.clear()
            else:
                matured_lots = [lot for lot in self._unsettled_lots if lot.settle_date <= eff_date]
                remaining_lots = [lot for lot in self._unsettled_lots if lot.settle_date > eff_date]
                swept = quantize_usd(sum((lot.proceeds for lot in matured_lots), ZERO))
                self._settled_cash = quantize_usd(self._settled_cash + swept)
                self._unsettled_cash = max(ZERO, quantize_usd(self._unsettled_cash - swept))
                self._unsettled_lots = remaining_lots

            self._last_clearing_date = eff_date
            logger.info(
                "T1CashLedger: 00:00:00 EDT overnight clearing swept $%s from C_unsettled -> C_settled | "
                "C_settled=$%s, C_unsettled=$%s, C_reserved=$%s",
                swept, self._settled_cash, self._unsettled_cash, self._reserved_cash,
            )
            return swept

    def snapshot(self) -> T1LedgerSnapshot:
        """Return an atomic thread-safe snapshot of all three capital buckets."""
        with self._lock:
            return T1LedgerSnapshot(
                settled_cash=self._settled_cash,
                unsettled_cash=self._unsettled_cash,
                reserved_cash=self._reserved_cash,
                total_cash=self.total_cash,
                nlv=self.nlv,
                cash_floor_pct=self._cash_floor_pct,
                max_allowable_buy_notional=self.compute_max_allowable_buy_notional(),
            )

    def to_dict(self) -> Dict[str, Any]:
        snap = self.snapshot()
        return {
            "account_id": self.account_id,
            "settled_cash": str(snap.settled_cash),
            "unsettled_cash": str(snap.unsettled_cash),
            "reserved_cash": str(snap.reserved_cash),
            "total_cash": str(snap.total_cash),
            "nlv": str(snap.nlv),
            "cash_floor_pct": str(snap.cash_floor_pct),
            "max_allowable_buy_notional": str(snap.max_allowable_buy_notional),
        }


# Alias
RegulatoryCashLedger = T1CashLedger
