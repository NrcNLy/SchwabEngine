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

    @property
    def notional(self) -> Decimal:
        return q(self.last_price * self.quantity)

    @property
    def unrealized_pnl(self) -> Decimal:
        return q((self.last_price - self.entry_price) * self.quantity)


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
    regime: str = ""


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
        single_ticker_cap_pct: Decimal | float = "0.20",
        risk_per_trade_pct: Decimal | float = "0.01",
        daily_drawdown_pct: Decimal | float = "0.03",
        cash_buffer: Decimal | float = "10.00",
        data_source: str = "SANDBOX_SIM",
    ):
        self._lock = threading.RLock()
        self.data_source = data_source
        self.cap_pct = _d(single_ticker_cap_pct)
        self.risk_pct = _d(risk_per_trade_pct)
        self.dd_pct = _d(daily_drawdown_pct)
        self.cash_buffer = _d(cash_buffer)

        self._settled: Decimal = q(_d(baseline_settled))
        self._lots: List[UnsettledLot] = []
        self._pending_ach: Decimal = ZERO
        self._reservations: Dict[str, Decimal] = {}

        self.positions: Dict[str, ManagedPosition] = {}
        self._trades: List[TradeRecord] = []

        # Broker-authoritative figures (live mode)
        self._broker_nlv: Optional[Decimal] = None
        self._swvxx: Decimal = ZERO
        self._swvxx_in_flight: Decimal = ZERO
        self._broker_positions_value: Decimal = ZERO
        self.synced: bool = False
        self.synced_at: Optional[datetime] = None
        self.drift: Decimal = ZERO
        self._ceiling_provider: Optional[Callable[[], Optional[Decimal]]] = None

    # ------------------------------------------------------------------
    # Read-only views
    # ------------------------------------------------------------------

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
                return False, f"cost ${cost:.2f} exceeds single-ticker cap ${cap:.2f} (20% NLV)"
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

    def allocate_capital(self, ticker: str, amount) -> bool:
        """Reserves settled cash for an entry. Never touches Bucket 2."""
        amt = q(_d(amount))
        with self._lock:
            ok, reason = self.check_order_allowed(amt)
            if not ok:
                logger.error("Allocation refused for %s: %s", ticker, reason)
                return False
            self._settled -= amt
            self._reservations[ticker] = self._reservations.get(ticker, ZERO) + amt
            logger.info("Reserved $%s of SETTLED cash for %s.", amt, ticker)
            return True

    def cancel_reservation(self, ticker: str) -> None:
        with self._lock:
            amt = self._reservations.pop(ticker, ZERO)
            self._settled += amt
            if amt:
                logger.info("Released reservation $%s for %s back to settled cash.", amt, ticker)

    def evaluate_gfv_compliance(self, ticker: str) -> bool:
        """Every entry is settled-funded, so exits are always GFV-safe."""
        return True

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
    ) -> ManagedPosition:
        px = _d(price)
        cost = q(px * quantity)
        with self._lock:
            reserved = self._reservations.pop(symbol, ZERO)
            # Settle the difference between the reservation and the actual fill.
            self._settled += reserved - cost
            existing = self.positions.get(symbol)
            if existing:
                total_qty = existing.quantity + quantity
                avg = q((existing.entry_price * existing.quantity + px * quantity) / total_qty)
                existing.quantity, existing.entry_price, existing.last_price = total_qty, avg, px
                pos = existing
            else:
                pos = ManagedPosition(
                    symbol=symbol, quantity=quantity, entry_price=px, last_price=px,
                    stop_price=_d(stop) if stop is not None else None,
                    target_price=_d(target) if target is not None else None,
                    regime=regime, simulated=simulated, opened_at=when or datetime.now(_EDT),
                )
                self.positions[symbol] = pos
            self._trades.append(TradeRecord(
                symbol=symbol, side="BUY", quantity=quantity, price=px, cost_basis=cost,
                timestamp=when or datetime.now(_EDT), simulated=simulated, regime=regime,
            ))
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
    ) -> ManagedPosition:
        """Adopts an existing holding into the managed positions ledger."""
        sym = symbol.upper()
        entry_px = _d(entry_price)
        last_px = _d(current_price) if current_price is not None else entry_px
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
                opened_at=opened_at or datetime.now(_EDT),
            )
            self.positions[sym] = pos
            logger.info("Adopted managed position: %s x%d @ $%s (current: $%s)", sym, quantity, entry_px, last_px)
            return pos

    def record_sell(
        self,
        symbol: str,
        quantity: int,
        price,
        *,
        simulated: bool = True,
        when: Optional[datetime] = None,
    ) -> Decimal:
        """Closes (part of) a position. Proceeds become an UNSETTLED lot (Hard Reserve)."""
        px = _d(price)
        with self._lock:
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
            self._trades.append(TradeRecord(
                symbol=symbol, side="SELL", quantity=qty, price=px, cost_basis=proceeds,
                timestamp=trade_dt, simulated=simulated, realized_pnl=realized,
                regime=pos.regime if pos else "",
            ))
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
        """Legacy-compatible snapshot object plus the derived risk figures."""
        with self._lock:
            ledger = self

            class Snapshot:
                bucket1_settled = ledger._settled
                bucket2_unsettled = ledger.unsettled_total
                bucket3_pending_ach = ledger._pending_ach
                nlv = ledger.nlv
                max_single_exposure = ledger.max_single_exposure
                max_order_value = max(q(min(ledger._settled - ledger.cash_buffer, ledger.max_single_exposure)), ZERO)
                max_risk_per_trade = ledger.max_risk_per_trade
                daily_drawdown_limit = ledger.daily_drawdown_limit

            return Snapshot()
