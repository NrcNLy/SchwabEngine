"""
core/ledger.py
==============
Zero-GFV settlement ledger (cash account, T+1).

Buckets
-------
Bucket 1  settled cash      - the ONLY pool Tier 1 may spend (invariant I1).
Bucket 2  unsettled lots    - sale proceeds waiting for T+1. This is the HARD
                              RESERVE: it can never fund an entry (I2).
Bucket 3  pending ACH       - informational only, never buying power (I3).

The previous implementation allowed entries funded from Bucket 2. Because the
engine flattens to cash at 15:50, any such entry is a same-day round trip on
unsettled funds, i.e. a Good Faith Violation. That branch no longer exists.

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
        Evaluates whether selling this position today is compliant with T+1 settlement rules.
        - If funded with settled cash, same-day exit is 100% GFV-safe.
        - If funded with unsettled cash, selling prior to settle_date causes a Good Faith Violation (GFV).
        """
        if self.funded_with_settled:
            return True, f"Shares of {self.symbol} were acquired with settled funds (GFV safe)."
        current_date = today or today_et()
        if self.settle_date is not None and current_date >= self.settle_date:
            return True, f"Shares of {self.symbol} settled on {self.settle_date} (GFV safe)."
        return False, (
            f"GFV VETO: {self.symbol} shares acquired with unsettled funds. "
            f"Selling before {self.settle_date or 'T+1'} settlement causes a Good Faith Violation (overnight hold required)."
        )

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
    ):
        self._lock = threading.RLock()
        self.data_source = data_source
        self.env = env
        self.cap_pct = _d(single_ticker_cap_pct)
        self.risk_pct = _d(risk_per_trade_pct)
        self.dd_pct = _d(daily_drawdown_pct)
        self.cash_buffer = _d(cash_buffer)
        self.trade_store = trade_store

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
        """Sale proceeds waiting for T+1 settlement (Bucket 2 - Hard Reserve)."""
        with self._lock:
            return self.unsettled_total

    @property
    def locked_cash(self) -> Decimal:
        """Committed funds reserved for pending in-flight orders."""
        with self._lock:
            return q(sum(self._reservations.values(), ZERO))

    @property
    def bucket1_settled(self) -> float:
        with self._lock:
            return float(self._settled)

    @property
    def bucket2_unsettled(self) -> float:
        with self._lock:
            return float(self.unsettled_total)

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
            return q(sum((l.amount for l in self._lots), ZERO))

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
            if cost > self._settled - self.cash_buffer:
                return False, (f"cost ${cost:.2f} exceeds settled cash ${self._settled:.2f} "
                               f"less ${self.cash_buffer:.2f} buffer (unsettled funds are Hard Reserve)")
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

    def allocate_capital(self, ticker: str, amount, allow_unsettled: bool = False) -> bool:
        """
        Reserves cash for an entry. By default (allow_unsettled=False), strictly allocates
        from settled cash (Bucket 1). If allow_unsettled=True, can draw from unsettled funds
        if settled cash is insufficient, which tags the subsequent fill as unsettled.
        """
        amt = q(_d(amount))
        with self._lock:
            if not allow_unsettled:
                ok, reason = self.check_order_allowed(amt)
                if not ok:
                    logger.error("Allocation refused for %s: %s", ticker, reason)
                    return False
                self._settled -= amt
                self._reservations[ticker] = self._reservations.get(ticker, ZERO) + amt
                self._reservation_funded_settled[ticker] = True
                logger.info("Reserved $%s of SETTLED cash for %s.", amt, ticker)
                return True
            else:
                available_settled = max(self._settled - self.cash_buffer, ZERO)
                total_avail = available_settled + self.unsettled_total
                if amt > total_avail:
                    logger.error("Allocation refused for %s: cost $%s exceeds total cash $%s.", ticker, amt, total_avail)
                    return False
                if amt <= available_settled:
                    self._settled -= amt
                    self._reservations[ticker] = self._reservations.get(ticker, ZERO) + amt
                    self._reservation_funded_settled[ticker] = True
                    logger.info("Reserved $%s of SETTLED cash for %s.", amt, ticker)
                else:
                    from_settled = available_settled
                    from_unsettled = amt - from_settled
                    self._settled -= from_settled
                    remaining_to_deduct = from_unsettled
                    updated_lots = []
                    for lot in self._lots:
                        if remaining_to_deduct <= ZERO:
                            updated_lots.append(lot)
                        elif lot.amount <= remaining_to_deduct:
                            remaining_to_deduct -= lot.amount
                        else:
                            updated_lots.append(UnsettledLot(
                                amount=lot.amount - remaining_to_deduct,
                                trade_date=lot.trade_date,
                                settle_date=lot.settle_date,
                                source=lot.source,
                            ))
                            remaining_to_deduct = ZERO
                    self._lots = updated_lots
                    self._reservations[ticker] = self._reservations.get(ticker, ZERO) + amt
                    self._reservation_funded_settled[ticker] = False
                    logger.warning("Reserved $%s ($%s unsettled) for %s. POSITION WILL REQUIRE OVERNIGHT HOLD.",
                                   amt, from_unsettled, ticker)
                return True

    def cancel_reservation(self, ticker: str) -> None:
        with self._lock:
            amt = self._reservations.pop(ticker, ZERO)
            was_settled = self._reservation_funded_settled.pop(ticker, True)
            if was_settled:
                self._settled += amt
            else:
                if amt > ZERO:
                    self._lots.append(UnsettledLot(
                        amount=amt,
                        trade_date=today_et(),
                        settle_date=next_business_day(today_et()),
                        source=f"CANCEL_RESERVATION:{ticker}",
                    ))
            if amt:
                logger.info("Released reservation $%s for %s back to cash.", amt, ticker)

    def can_sell_position(self, symbol: str, quantity: int = 0, today: Optional[date] = None) -> Tuple[bool, str]:
        """
        Pre-trade check: verifies if shares of `symbol` are safe to sell today without
        causing a Good Faith Violation.
        """
        with self._lock:
            sym = symbol.upper()
            pos = self.positions.get(sym)
            if pos is None:
                return True, f"No active position in {sym}."
            return pos.is_gfv_safe_to_sell(today=today)

    def evaluate_gfv_compliance(self, ticker: str, today: Optional[date] = None) -> bool:
        """Returns True if the position in ticker can be sold without GFV."""
        return self.can_sell_position(ticker, today=today)[0]

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
                if was_settled_res:
                    self._settled += diff
                else:
                    if diff > ZERO:
                        self._lots.append(UnsettledLot(
                            amount=diff, trade_date=trade_day,
                            settle_date=eff_settle_date, source=f"RESERVATION_SURPLUS:{symbol}"
                        ))
                    else:
                        self._settled += diff

            existing = self.positions.get(symbol)
            if existing:
                total_qty = existing.quantity + quantity
                avg = q((existing.entry_price * existing.quantity + px * quantity) / total_qty)
                existing.quantity, existing.entry_price, existing.last_price = total_qty, avg, px
                if not is_settled:
                    existing.funded_with_settled = False
                    existing.settle_date = max(existing.settle_date or eff_settle_date, eff_settle_date)
                pos = existing
            else:
                pos = ManagedPosition(
                    symbol=symbol, quantity=quantity, entry_price=px, last_price=px,
                    stop_price=_d(stop) if stop is not None else None,
                    target_price=_d(target) if target is not None else None,
                    regime=regime, simulated=simulated, opened_at=trade_dt,
                    funded_with_settled=is_settled,
                    settle_date=eff_settle_date if not is_settled else None,
                )
                self.positions[symbol] = pos
            rec = TradeRecord(
                symbol=symbol, side="BUY", quantity=quantity, price=px, cost_basis=cost,
                timestamp=trade_dt, simulated=simulated, regime=regime,
            )
            self._trades.append(rec)
            if self.trade_store is not None:
                try:
                    self.trade_store.record_trade(
                        symbol=symbol, side="BUY", quantity=quantity, price=px, cost_basis=cost,
                        timestamp=trade_dt, simulated=simulated, realized_pnl=ZERO,
                        disallowed_loss=ZERO, regime=regime, trade_id=rec.trade_id, env=self.env,
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
                funded_with_settled=funded_with_settled,
                settle_date=eff_settle,
            )
            self.positions[sym] = pos
            logger.info("Adopted managed position: %s x%d @ $%s (settled_funding=%s)",
                        sym, quantity, entry_px, funded_with_settled)
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
            trade_day = trade_dt.astimezone(_EDT).date()
            self._lots.append(UnsettledLot(
                amount=proceeds, trade_date=trade_day,
                settle_date=next_business_day(trade_day), source=f"SELL:{symbol}",
            ))
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
                try:
                    self.trade_store.record_trade(
                        symbol=symbol, side="SELL", quantity=qty, price=px, cost_basis=proceeds,
                        timestamp=trade_dt, simulated=simulated, realized_pnl=realized,
                        disallowed_loss=ZERO, regime=pos.regime if pos else "", trade_id=rec.trade_id, env=self.env,
                    )
                except Exception as exc:
                    logger.error("Failed to persist SELL trade to SQLite: %s", exc)
            logger.info("SELL %s x%d @ %s -> $%s UNSETTLED until %s", symbol, qty, px, proceeds,
                        self._lots[-1].settle_date)
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
        """Moves lots whose settle date has arrived from Bucket 2 into Bucket 1."""
        today = today or today_et()
        moved = ZERO
        with self._lock:
            remaining: List[UnsettledLot] = []
            for lot in self._lots:
                if lot.settle_date <= today:
                    moved += lot.amount
                else:
                    remaining.append(lot)
            self._lots = remaining
            self._settled += moved
        if moved:
            logger.info("T+1 rollover: $%s moved Bucket 2 -> Bucket 1.", moved)
        return moved

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
                    # Broker holds MORE settled cash than the ledger (e.g. ACH landed).
                    self._settled = broker_settled
            if balances.unsettled_cash > 0 and not self._lots:
                # Broker reports unsettled funds we did not originate (e.g. a manual sale).
                self._lots.append(UnsettledLot(
                    amount=q(balances.unsettled_cash), trade_date=today_et(),
                    settle_date=next_business_day(today_et()), source="BROKER"))
            self._broker_nlv = q(balances.liquidation_value)
            self._swvxx = q(balances.swvxx_value)
            self._broker_positions_value = q(balances.positions_value)
            self.synced = True
            self.synced_at = datetime.now(_EDT)
            return self.drift

    @property
    def gfv_risk_flag(self) -> bool:
        with self._lock:
            return self.drift > Decimal("1.00")

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def snapshot(self):
        """Legacy-compatible snapshot object plus three-bucket capital states."""
        with self._lock:
            ledger = self

            class Snapshot:
                settled_cash = ledger._settled
                unsettled_cash = ledger.unsettled_total
                locked_cash = ledger.locked_cash
                bucket1_settled = ledger._settled
                bucket2_unsettled = ledger.unsettled_total
                bucket3_pending_ach = ledger._pending_ach
                nlv = ledger.nlv
                max_single_exposure = ledger.max_single_exposure
                max_order_value = max(q(min(ledger._settled - ledger.cash_buffer, ledger.max_single_exposure)), ZERO)
                max_risk_per_trade = ledger.max_risk_per_trade
                daily_drawdown_limit = ledger.daily_drawdown_limit

            return Snapshot()
