"""
core/ledger.py
==============
FINRA Rule 4210 Margin Framework Ledger.
Deprecates legacy Pattern Day Trader (PDT) constraints, Good Faith Violation (GFV)
prevention, and T+1 temporal locks. Enables instantaneous capital reallocation upon execution.

Equity & Leverage:
- $2,000 minimum equity check required for margin eligibility.
- Intraday Margin Buying Power: Up to 4x leverage (25% maintenance margin requirement).
- Regulation T Overnight Buying Power: Up to 2x leverage (50% margin requirement).
- Instantaneous settlement: Sale proceeds credit immediately to cleared funds.

Thread safety: every public method takes ``self._lock``; the object may be
shared between the asyncio event loop, the streamer thread and API threads.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Callable, Dict, List, Optional, Tuple

import pytz

from core.liquidity_policy import ZERO, add_business_days, next_business_day, q
from core.t1_ledger import (
    GFVRejectionError,
    RegulatoryCashLedger,
    T1CashLedger,
    T1LedgerSnapshot,
    UnsettledSaleLot,
    quantize_usd,
)

logger = logging.getLogger("ledger")

_EDT = pytz.timezone("America/New_York")


def _d(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def today_et() -> date:
    return datetime.now(_EDT).date()


@dataclass
class UnsettledLot:
    amount: Decimal
    trade_date: date
    settle_date: date
    source: str


class ReconciliationLockError(RuntimeError):
    """Raised when an attempt to credit cash buckets or record fills violates the Strict Reconciliation Lock."""


@dataclass
class ManagedPosition:
    symbol: str
    quantity: int
    entry_price: Decimal
    last_price: Decimal
    stop_price: Optional[Decimal] = None
    target_price: Optional[Decimal] = None
    regime: str = "A"
    opened_at: datetime = field(default_factory=lambda: datetime.now(_EDT))
    simulated: bool = True
    high_water_mark: Decimal = field(default=Decimal("0.00"))
    volatility_stop_distance: Optional[Decimal] = None
    ratchet_permitted: bool = True
    funded_with_settled: bool = True
    settle_date: Optional[date] = None
    recovery_locked: bool = False

    def __post_init__(self):
        if self.high_water_mark <= Decimal("0.00"):
            self.high_water_mark = max(self.entry_price, self.last_price)

    @property
    def notional(self) -> Decimal:
        return q(self.last_price * self.quantity)

    @property
    def unrealized_pnl(self) -> Decimal:
        return q((self.last_price - self.entry_price) * self.quantity)

    def is_gfv_safe_to_sell(self, today: Optional[date] = None) -> Tuple[bool, str]:
        """
        Under FINRA Rule 4210 Margin Framework, GFV rules are eliminated.
        Positions are eligible for instantaneous exit.
        """
        return True, f"Shares of {self.symbol} are eligible for instantaneous exit (GFV exempt under margin framework)."

    def update_trailing_stop(
        self,
        current_price: Decimal | float,
        stop_distance: Decimal | float,
        ratchet_permitted: bool = True,
    ) -> bool:
        """
        Updates the position's high-water-mark and ratchets stop_price upward.
        Strictly monotonic: candidate stop only replaces stop_price if candidate > stop_price
        AND ratchet_permitted is True (Hurst persistence gate).
        If ratchet_permitted is False (Hurst <= 0.50 chop), existing stop_price is maintained.
        Returns True if stop_price was adjusted upward.
        """
        px = _d(current_price)
        dist = _d(stop_distance)
        self.last_price = px
        if px > self.high_water_mark:
            self.high_water_mark = px

        self.volatility_stop_distance = q(dist)
        self.ratchet_permitted = ratchet_permitted
        if not ratchet_permitted:
            return False

        candidate = q(self.high_water_mark - dist)
        if self.stop_price is None or candidate > self.stop_price:
            self.stop_price = candidate
            return True
        return False


@dataclass
class TradeRecord:
    symbol: str
    side: str
    quantity: int
    price: Decimal
    cost_basis: Decimal
    timestamp: datetime
    simulated: bool
    realized_pnl: Decimal = ZERO
    disallowed_loss: Decimal = ZERO
    regime: str = ""
    trade_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class BrokerBalances:
    """Normalised Schwab account balances (see services.broker_sync.parse_account)."""
    liquidation_value: Decimal
    settled_cash: Decimal
    unsettled_cash: Decimal = ZERO
    swvxx_value: Decimal = ZERO
    positions_value: Decimal = ZERO


class SettlementLedger:
    def __init__(
        self,
        baseline_settled: Decimal | float = 0,
        *,
        single_ticker_cap_pct: Decimal | float = "0.33",
        risk_per_trade_pct: Decimal | float = "0.02",
        daily_drawdown_pct: Decimal | float = "0.05",
        cash_buffer: Decimal | float = "10.00",
        data_source: str = "SANDBOX_SIM",
        env: str = "active",
        trade_store: Optional[Any] = None,
        sizing_governor: Optional[Any] = None,
    ):
        self._lock = threading.RLock()
        self.data_source = data_source
        self.env = env
        self.cap_pct = _d(single_ticker_cap_pct)
        self.risk_pct = _d(risk_per_trade_pct)
        self.dd_pct = _d(daily_drawdown_pct)
        self.cash_buffer = _d(cash_buffer)
        self.trade_store = trade_store
        self.sizing_governor = sizing_governor

        self.min_margin_equity: Decimal = Decimal("2000.00")
        self.pdt_restricted: bool = False
        self.day_trade_count: int = 0

        self._settled: Decimal = q(_d(baseline_settled))
        self._lots: List[UnsettledLot] = []
        self._pending_ach: Decimal = ZERO
        self._reservations: Dict[str, Decimal] = {}
        self._reservation_funded_settled: Dict[str, bool] = {}

        self.positions: Dict[str, ManagedPosition] = {}
        self._trades: List[TradeRecord] = []
        if self.trade_store is not None:
            self._rehydrate_trades()

        # Broker-authoritative figures (live mode)
        self._broker_nlv: Optional[Decimal] = None
        self._swvxx: Decimal = ZERO
        self._swvxx_in_flight: Decimal = ZERO
        self._broker_positions_value: Decimal = ZERO
        self.synced: bool = False
        self.synced_at: Optional[datetime] = None
        self.drift: Decimal = ZERO
        self._ceiling_provider: Optional[Callable[[], Optional[Decimal]]] = None

    def _rehydrate_trades(self, target_date: Optional[date] = None) -> None:
        with self._lock:
            if self.trade_store is None:
                return
            records = self.trade_store.fetch_trades_today(env=self.env, target_date=target_date)
            loaded = []
            for r in records:
                try:
                    ts = datetime.fromisoformat(r["timestamp"])
                    if ts.tzinfo is None:
                        ts = _EDT.localize(ts)
                except Exception:
                    ts = datetime.now(_EDT)
                loaded.append(
                    TradeRecord(
                        symbol=r["symbol"],
                        side=r["side"],
                        quantity=int(r["quantity"]),
                        price=Decimal(str(r["price"])),
                        cost_basis=Decimal(str(r["cost_basis"])),
                        timestamp=ts,
                        simulated=bool(r["simulated"]),
                        realized_pnl=Decimal(str(r.get("realized_pnl", 0.0))),
                        disallowed_loss=Decimal(str(r.get("disallowed_loss", 0.0))),
                        regime=r.get("regime", ""),
                        trade_id=r.get("trade_id", str(uuid.uuid4())),
                    )
                )
            self._trades = loaded
            logger.info("Hydrated %d trade(s) from SQLite for env '%s'.", len(loaded), self.env)

    # ------------------------------------------------------------------
    # Read-only views
    # ------------------------------------------------------------------

    @property
    def settled_cash(self) -> Decimal:
        """Cleared funds available for trading (Bucket 1)."""
        with self._lock:
            return self._settled

    @property
    def unsettled_cash(self) -> Decimal:
        """Deprecated under FINRA 4210 (proceeds settle instantaneously into cleared cash)."""
        with self._lock:
            return ZERO

    @property
    def locked_cash(self) -> Decimal:
        """Committed funds reserved for pending in-flight orders."""
        with self._lock:
            return q(sum(self._reservations.values(), ZERO))

    @property
    def reserved_cash(self) -> Decimal:
        """Cash locked in active, unfilled working buy orders ($C_reserved$)."""
        with self._lock:
            return q(sum(self._reservations.values(), ZERO))

    @property
    def bucket1_settled(self) -> float:
        with self._lock:
            return float(self._settled)

    @property
    def bucket2_unsettled(self) -> float:
        with self._lock:
            return 0.0

    @property
    def bucket3_pending(self) -> float:
        with self._lock:
            return float(self._pending_ach)

    @property
    def settled(self) -> Decimal:
        with self._lock:
            return self._settled

    @property
    def unsettled_total(self) -> Decimal:
        with self._lock:
            return ZERO

    @property
    def swvxx_balance(self) -> Decimal:
        return self._swvxx

    @property
    def swvxx_in_flight(self) -> Decimal:
        return self._swvxx_in_flight

    @property
    def pending_ach(self) -> Decimal:
        return self._pending_ach

    @property
    def positions_value(self) -> Decimal:
        with self._lock:
            return q(sum((p.notional for p in self.positions.values()), ZERO))

    @property
    def nlv(self) -> Decimal:
        """Broker NLV when synced; otherwise settled + unsettled + marked positions."""
        with self._lock:
            if self._broker_nlv is not None:
                return self._broker_nlv
            return q(self._settled + self.unsettled_total + self.positions_value
                     + sum(self._reservations.values(), ZERO))

    @property
    def max_single_exposure(self) -> Decimal:
        return q(self.nlv * self.cap_pct)

    @property
    def max_risk_per_trade(self) -> Decimal:
        return q(self.nlv * self.risk_pct)

    @property
    def daily_drawdown_limit(self) -> Decimal:
        return q(-(self.nlv * self.dd_pct))

    def get_trade_log(self) -> List[TradeRecord]:
        with self._lock:
            return list(self._trades)

    def get_positions(self) -> List[ManagedPosition]:
        with self._lock:
            return list(self.positions.values())

    def realized_pnl_today(self, day: Optional[date] = None) -> Decimal:
        day = day or today_et()
        with self._lock:
            return q(sum((t.realized_pnl for t in self._trades
                          if t.side == "SELL" and t.timestamp.astimezone(_EDT).date() == day), ZERO))

    def unrealized_pnl(self) -> Decimal:
        with self._lock:
            return q(sum((p.unrealized_pnl for p in self.positions.values()), ZERO))

    # ------------------------------------------------------------------
    # Capital allocation (settled-only: invariant I1)
    # ------------------------------------------------------------------

    def set_order_ceiling_provider(self, provider: Optional[Callable[[], Optional[Decimal]]]) -> None:
        """
        Registers a callable returning the current policy-derived maximum order
        notional (or None for no extra ceiling). Evaluated on every pre-trade check
        so the soft-reserve policy is enforced for StrategyEngine entries as well.
        """
        self._ceiling_provider = provider

    def check_order_allowed(self, order_cost) -> Tuple[bool, str]:
        """Pre-trade gate used by StrategyEngine. Settled cash + 20% NLV cap + policy ceiling."""
        cost = _d(order_cost)
        with self._lock:
            if cost <= 0:
                return False, "non-positive order cost"
            # $2,000 Minimum Equity Check for margin eligibility
            if self.nlv < self.min_margin_equity and cost > self._settled - self.cash_buffer:
                return False, (
                    f"Account equity ${self.nlv:.2f} is below $2,000 minimum for margin eligibility; "
                    f"cost ${cost:.2f} exceeds cleared cash ${self._settled:.2f} less buffer"
                )
            if self.nlv >= self.min_margin_equity:
                max_bp = q(self.nlv * 4 - self.cash_buffer)
                if cost > max_bp:
                    return False, f"cost ${cost:.2f} exceeds intraday margin buying power ${max_bp:.2f}"
            else:
                if cost > self._settled - self.cash_buffer:
                    return False, f"cost ${cost:.2f} exceeds cash ${self._settled:.2f} less buffer"

            cap = self.max_single_exposure
            if cap > 0 and cost > cap:
                return False, f"cost ${cost:.2f} exceeds single-ticker cap ${cap:.2f} (33% NLV)"
            provider = self._ceiling_provider
        if provider is not None:
            try:
                ceiling = provider()
            except Exception as exc:
                logger.error("Order ceiling provider failed (%s); refusing order.", exc)
                return False, "order ceiling unavailable"
            if ceiling is not None and cost > ceiling:
                return False, f"cost ${cost:.2f} exceeds policy buying power ${ceiling:.2f}"
        return True, "ok"

    def allocate_capital(self, ticker: str, amount, allow_unsettled: bool = True) -> bool:
        """
        Reserves capital for an entry under FINRA Rule 4210 Margin Framework.
        Capital allocation is instantaneous without T+1 settlement locks.
        """
        amt = q(_d(amount))
        with self._lock:
            ok, reason = self.check_order_allowed(amt)
            if not ok:
                logger.error("Allocation refused for %s: %s", ticker, reason)
                return False
            self._settled -= amt
            self._reservations[ticker] = self._reservations.get(ticker, ZERO) + amt
            self._reservation_funded_settled[ticker] = True
            logger.info("Reserved $%s of capital for %s (instantaneous allocation).", amt, ticker)
            return True

    def cancel_reservation(self, ticker: str) -> None:
        with self._lock:
            amt = self._reservations.pop(ticker, ZERO)
            self._reservation_funded_settled.pop(ticker, True)
            self._settled += amt
            if amt:
                logger.info("Released reservation $%s for %s back to cash.", amt, ticker)

    def can_sell_position(self, symbol: str, quantity: int = 0, today: Optional[date] = None) -> Tuple[bool, str]:
        """
        Pre-trade check: under FINRA Rule 4210 Margin Framework, GFV rules are eliminated.
        Same-day round-trips and exits are 100% permitted.
        """
        return True, f"Margin account: {symbol} is eligible for instantaneous exit (GFV exempt)."

    def evaluate_gfv_compliance(self, ticker: str, today: Optional[date] = None) -> bool:
        """Returns True under FINRA Rule 4210 Margin Framework (GFV exempt)."""
        return True

    def can_submit_order(
        self,
        side: str,
        notional: Decimal | float | int | str,
        *,
        nlv: Optional[Decimal | float | int | str] = None,
        cash_floor_pct: Optional[Decimal | float | int | str] = None,
        symbol: str = "",
    ) -> Tuple[bool, str]:
        """
        Pre-Trade GFV Firewall (#123 Workstream 3).
        Validates whether an order can be submitted under SEC Rule 15c6-1 T+1 cash constraints:
            MaxAllowableBuyNotional(t) = max(0.00, C_settled(t) - C_reserved(t) - (NLV(t) * CashFloorPct))
        """
        side_norm = str(side or "").strip().upper()
        notional_dec = quantize_usd(notional)
        with self._lock:
            if side_norm in ("SELL", "SELL_TO_CLOSE"):
                return True, f"SELL order for {symbol or 'position'} permitted (${notional_dec:.2f})."
            if side_norm not in ("BUY", "BUY_TO_OPEN"):
                return False, f"GFV REJECTION: Unsupported order side '{side}'."
            if notional_dec <= ZERO:
                return False, f"GFV REJECTION: Non-positive BUY notional (${notional_dec:.2f})."

            eff_nlv = quantize_usd(nlv) if nlv is not None else quantize_usd(self.nlv)
            eff_floor_pct = _d(cash_floor_pct) if cash_floor_pct is not None else Decimal("0.00")
            floor_reserve = quantize_usd(eff_nlv * eff_floor_pct)
            c_reserved = self.reserved_cash
            max_allowable = max(ZERO, quantize_usd(self._settled - c_reserved - floor_reserve))
            if notional_dec > max_allowable:
                unsettled_lots = quantize_usd(sum((lot.amount for lot in self._lots), ZERO))
                reason = (
                    f"GFV REJECTION: BUY {symbol + ' ' if symbol else ''}notional ${notional_dec:.2f} "
                    f"exceeds MaxAllowableBuyNotional ${max_allowable:.2f} "
                    f"(C_settled=${self._settled:.2f}, C_unsettled=${unsettled_lots:.2f}, "
                    f"C_reserved=${c_reserved:.2f}, NLV=${eff_nlv:.2f}, CashFloorPct={eff_floor_pct:.4f})."
                )
                logger.warning(reason)
                return False, reason
            return (
                True,
                f"OK: BUY notional ${notional_dec:.2f} within MaxAllowableBuyNotional ${max_allowable:.2f}.",
            )

    def process_t1_overnight_clearing(
        self,
        current_dt: Optional[datetime | date] = None,
        *,
        force: bool = True,
    ) -> Decimal:
        """
        Overnight T+1 Clearing Transition (00:00:00 EDT).
        Sweeps matured unsettled sale lots into C_settled and resets C_unsettled.
        """
        with self._lock:
            if not self._lots:
                return ZERO
            clear_date = (
                current_dt.astimezone(_EDT).date()
                if isinstance(current_dt, datetime)
                else (current_dt or today_et())
            )
            cleared = ZERO
            remaining: List[UnsettledLot] = []
            for lot in self._lots:
                if force or lot.settle_date <= clear_date:
                    cleared = quantize_usd(cleared + lot.amount)
                else:
                    remaining.append(lot)
            self._lots = remaining
            self._settled = quantize_usd(self._settled + cleared)
            return cleared

    # ------------------------------------------------------------------
    # Fills
    # ------------------------------------------------------------------

    def confirm_buy(
        self,
        symbol: str,
        quantity: int,
        price,
        *,
        stop=None,
        target=None,
        regime: str = "A",
        simulated: bool = True,
        when: Optional[datetime] = None,
        funded_with_settled: Optional[bool] = None,
        settle_date: Optional[date] = None,
        execution_payload: Optional[Dict[str, Any]] = None,
    ) -> ManagedPosition:
        px = _d(price)
        cost = q(px * quantity)
        trade_dt = when or datetime.now(_EDT)
        trade_day = trade_dt.astimezone(_EDT).date()
        eff_settle_date = settle_date or next_business_day(trade_day)

        with self._lock:
            reserved = self._reservations.pop(symbol, ZERO)
            was_settled_res = self._reservation_funded_settled.pop(symbol, True)
            is_settled = funded_with_settled if funded_with_settled is not None else was_settled_res

            # Settle the difference between the reservation and the actual fill.
            diff = reserved - cost
            if diff != ZERO:
                self._settled += diff

            existing = self.positions.get(symbol)
            if existing:
                total_qty = existing.quantity + quantity
                avg = q((existing.entry_price * existing.quantity + px * quantity) / total_qty)
                existing.quantity, existing.entry_price, existing.last_price = total_qty, avg, px
                existing.funded_with_settled = True
                existing.settle_date = None
                pos = existing
            else:
                pos = ManagedPosition(
                    symbol=symbol, quantity=quantity, entry_price=px, last_price=px,
                    stop_price=_d(stop) if stop is not None else None,
                    target_price=_d(target) if target is not None else None,
                    regime=regime, simulated=simulated, opened_at=trade_dt,
                    funded_with_settled=True,
                    settle_date=None,
                )
                self.positions[symbol] = pos
            rec = TradeRecord(
                symbol=symbol, side="BUY", quantity=quantity, price=px, cost_basis=cost,
                timestamp=trade_dt, simulated=simulated, regime=regime,
            )
            self._trades.append(rec)
            if self.trade_store is not None:
                alpha = getattr(self.sizing_governor, "alpha", 10.0) if self.sizing_governor is not None else 10.0
                beta = getattr(self.sizing_governor, "beta", 10.0) if self.sizing_governor is not None else 10.0
                kelly = getattr(self.sizing_governor, "kelly_fraction", 0.0) if self.sizing_governor is not None else 0.0
                try:
                    self.trade_store.record_trade(
                        symbol=symbol, side="BUY", quantity=quantity, price=px, cost_basis=cost,
                        timestamp=trade_dt, simulated=simulated, realized_pnl=ZERO,
                        disallowed_loss=ZERO, regime=regime, trade_id=rec.trade_id, env=self.env,
                        posterior_alpha=alpha, posterior_beta=beta, kelly_fraction=kelly,
                    )
                except Exception as exc:
                    logger.error("Failed to persist BUY trade to SQLite: %s", exc)
            return pos

    def adopt_position(
        self,
        symbol: str,
        quantity: int,
        entry_price: Decimal | float,
        current_price: Optional[Decimal | float] = None,
        *,
        stop_price: Optional[Decimal | float] = None,
        target_price: Optional[Decimal | float] = None,
        regime: str = "A",
        simulated: bool = False,
        opened_at: Optional[datetime] = None,
        funded_with_settled: bool = True,
        settle_date: Optional[date] = None,
        recovery_locked: bool = False,
    ) -> ManagedPosition:
        """Adopts an existing holding into the managed positions ledger."""
        sym = symbol.upper()
        entry_px = _d(entry_price)
        last_px = _d(current_price) if current_price is not None else entry_px
        trade_dt = opened_at or datetime.now(_EDT)
        trade_day = trade_dt.astimezone(_EDT).date()
        eff_settle = settle_date or (next_business_day(trade_day) if not funded_with_settled else None)
        with self._lock:
            pos = ManagedPosition(
                symbol=sym,
                quantity=int(quantity),
                entry_price=entry_px,
                last_price=last_px,
                stop_price=_d(stop_price) if stop_price is not None else None,
                target_price=_d(target_price) if target_price is not None else None,
                regime=regime,
                simulated=simulated,
                opened_at=trade_dt,
                funded_with_settled=True,
                settle_date=None,
                recovery_locked=recovery_locked,
            )
            self.positions[sym] = pos
            logger.info("Adopted managed position: %s x%d @ $%s (recovery_locked=%s)",
                        sym, quantity, entry_px, recovery_locked)
            return pos

    def record_sell(
        self,
        symbol: str,
        quantity: int,
        price,
        *,
        simulated: bool = True,
        when: Optional[datetime] = None,
        execution_payload: Optional[Dict[str, Any]] = None,
    ) -> Decimal:
        """
        Closes (part of) a position. Proceeds become an UNSETTLED lot (Bucket 2 / Hard Reserve).

        Strict Reconciliation Lock:
        For live orders (simulated=False), the ledger will REFUSE to credit cash buckets
        or modify the position unless a definitive, completed execution payload from
        the Schwab API is provided (e.g. status='FILLED' or non-zero filledQuantity).
        """
        px = _d(price)
        with self._lock:
            # Strict Reconciliation Lock for live executions
            if not simulated:
                if execution_payload is None:
                    raise ReconciliationLockError(
                        f"Strict Reconciliation Lock: Refusing to credit sale proceeds for {symbol}. "
                        "Definitive completed execution payload from Schwab API is required for live orders."
                    )
                status = str(execution_payload.get("status", "")).upper()
                filled_qty = int(float(execution_payload.get("filledQuantity", 0) or 0))
                has_legs = False
                for act in execution_payload.get("orderActivityCollection", []) or []:
                    if act.get("executionLegs"):
                        has_legs = True
                        break
                if status not in ("FILLED", "EXECUTED") and filled_qty <= 0 and not has_legs:
                    raise ReconciliationLockError(
                        f"Strict Reconciliation Lock: Execution payload for {symbol} is not filled "
                        f"(status='{status}', filledQuantity={filled_qty}). Refusing to credit cash."
                    )

            pos = self.positions.get(symbol)
            qty = min(quantity, pos.quantity) if pos else quantity
            proceeds = q(px * qty)
            realized = q((px - pos.entry_price) * qty) if pos else ZERO
            trade_dt = when or datetime.now(_EDT)
            # FINRA Rule 4210: Instantaneous settlement — proceeds immediately credit cash!
            self._settled += proceeds
            if pos:
                pos.quantity -= qty
                pos.last_price = px
                if pos.quantity <= 0:
                    del self.positions[symbol]
            rec = TradeRecord(
                symbol=symbol, side="SELL", quantity=qty, price=px, cost_basis=proceeds,
                timestamp=trade_dt, simulated=simulated, realized_pnl=realized,
                regime=pos.regime if pos else "",
            )
            self._trades.append(rec)
            if self.trade_store is not None:
                if self.sizing_governor is not None and realized != ZERO:
                    self.sizing_governor.update_trade_outcome(is_win=(realized > ZERO))
                alpha = getattr(self.sizing_governor, "alpha", 10.0) if self.sizing_governor is not None else 10.0
                beta = getattr(self.sizing_governor, "beta", 10.0) if self.sizing_governor is not None else 10.0
                kelly = getattr(self.sizing_governor, "kelly_fraction", 0.0) if self.sizing_governor is not None else 0.0
                try:
                    self.trade_store.record_trade(
                        symbol=symbol, side="SELL", quantity=qty, price=px, cost_basis=proceeds,
                        timestamp=trade_dt, simulated=simulated, realized_pnl=realized,
                        disallowed_loss=ZERO, regime=pos.regime if pos else "", trade_id=rec.trade_id, env=self.env,
                        posterior_alpha=alpha, posterior_beta=beta, kelly_fraction=kelly,
                    )
                except Exception as exc:
                    logger.error("Failed to persist SELL trade to SQLite: %s", exc)
            logger.info("SELL %s x%d @ %s -> $%s credited immediately (FINRA Rule 4210 instantaneous settlement)",
                        symbol, qty, px, proceeds)
            return proceeds

    def mark_price(self, symbol: str, price) -> None:
        with self._lock:
            pos = self.positions.get(symbol)
            if pos:
                pos.last_price = _d(price)

    # ------------------------------------------------------------------
    # Settlement
    # ------------------------------------------------------------------

    def rollover(self, today: Optional[date] = None) -> Decimal:
        """Deprecated under FINRA Rule 4210 (proceeds settle instantaneously). Returns ZERO."""
        return ZERO

    def set_pending_ach(self, amount) -> None:
        with self._lock:
            self._pending_ach = q(max(_d(amount), ZERO))

    # ------------------------------------------------------------------
    # Broker reconciliation (live mode, invariant I5)
    # ------------------------------------------------------------------

    def sync_from_broker(self, balances: BrokerBalances, drift_tolerance: Decimal = Decimal("1.00")) -> Decimal:
        """
        Adopts broker balances. On the first sync the broker figure is authoritative.
        Afterwards, if the ledger's own settled figure exceeds the broker's by more
        than ``drift_tolerance`` the ledger uses the LOWER number (never over-spend).

        Returns the signed drift (ledger - broker) for the settled bucket.
        """
        with self._lock:
            broker_settled = q(balances.settled_cash)
            if not self.synced:
                self._settled = broker_settled
                self.drift = ZERO
            else:
                self.drift = q(self._settled - broker_settled)
                if self.drift > drift_tolerance:
                    logger.error("Ledger/broker drift $%s: using broker settled $%s.",
                                 self.drift, broker_settled)
                    self._settled = broker_settled
                elif abs(self.drift) <= drift_tolerance:
                    self._settled = min(self._settled, broker_settled)
                else:
                    self._settled = broker_settled
            self._broker_nlv = q(balances.liquidation_value)
            self._swvxx = q(balances.swvxx_value)
            self._broker_positions_value = q(balances.positions_value)
            self.synced = True
            self.synced_at = datetime.now(_EDT)
            return self.drift

    @property
    def gfv_risk_flag(self) -> bool:
        """GFV risk flag is permanently False under FINRA Rule 4210 Margin Framework."""
        return False

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def snapshot(self):
        """Margin-framework snapshot."""
        with self._lock:
            ledger = self

            class Snapshot:
                settled_cash = ledger._settled
                unsettled_cash = ZERO
                locked_cash = ledger.locked_cash
                bucket1_settled = ledger._settled
                bucket2_unsettled = ZERO
                bucket3_pending_ach = ledger._pending_ach
                nlv = ledger.nlv
                max_single_exposure = ledger.max_single_exposure
                max_order_value = max(q(min(ledger.nlv * 4 - ledger.cash_buffer, ledger.max_single_exposure)), ZERO) if ledger.nlv >= ledger.min_margin_equity else max(q(min(ledger._settled - ledger.cash_buffer, ledger.max_single_exposure)), ZERO)
                max_risk_per_trade = ledger.max_risk_per_trade
                daily_drawdown_limit = ledger.daily_drawdown_limit

            return Snapshot()
