"""
services/broker_sync.py
=======================
Read-only Schwab account synchroniser used in LIVE mode.

* Polls ``GET /trader/v1/accounts?fields=positions`` (blocking ``requests`` call,
  so it always runs through ``asyncio.to_thread``).
* Normalises balances into ``BrokerBalances`` and feeds the ledger
  (``sync_from_broker`` applies the lower-of-ledger/broker rule, invariant I5).
* Exposes unmanaged (external) positions to the API. It NEVER places or cancels
  orders - protective stops on external holdings are intentionally not automated
  this session, and symbols in ``reconciliation.exclude_symbols`` (SWVXX) are
  never listed as unmanaged.

Fails closed: if settled cash or liquidation value cannot be located in the
payload, ``BrokerParseError`` is raised and the caller must not trade on a
guessed balance.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pytz

from core.ledger import BrokerBalances, SettlementLedger
from core.liquidity_policy import ZERO, q
from core.nlv_anchor import NlvAnchor

logger = logging.getLogger("broker_sync")
_EDT = pytz.timezone("America/New_York")

SETTLED_FIELDS = ("cashAvailableForTrading", "availableFundsNonMarginableTrade", "cashBalance")
LIQUIDATION_FIELDS = ("liquidationValue",)


class BrokerParseError(RuntimeError):
    """Raised when the account payload lacks a field we refuse to guess."""


@dataclass
class BrokerPosition:
    symbol: str
    quantity: int
    avg_cost: float
    current_price: float
    market_value: float
    day_pnl: float = 0.0


@dataclass
class UnmanagedPosition:
    symbol: str
    quantity: int
    avg_cost: float
    current_price: float
    detected_at: datetime
    stop_order_id: Optional[str] = None


def _first_number(block: Dict[str, Any], keys: Iterable[str]) -> Optional[Decimal]:
    for key in keys:
        if key in block and block[key] is not None:
            try:
                return Decimal(str(block[key]))
            except Exception:
                continue
    return None


def parse_account(
    account: Dict[str, Any],
    sweep_symbol: str = "SWVXX",
) -> Tuple[BrokerBalances, List[BrokerPosition]]:
    """Parses one element of the Schwab ``/accounts`` response."""
    sec = account.get("securitiesAccount", account)
    cur = sec.get("currentBalances") or {}
    agg = account.get("aggregatedBalance") or {}

    nlv = _first_number(cur, LIQUIDATION_FIELDS)
    if nlv is None:
        nlv = _first_number(agg, ("currentLiquidationValue", "liquidationValue"))
    settled = _first_number(cur, SETTLED_FIELDS)
    if nlv is None or settled is None:
        raise BrokerParseError(
            "Account payload missing liquidation value or settled cash "
            f"(currentBalances keys: {sorted(cur.keys())})"
        )
    unsettled = _first_number(cur, ("unsettledCash",)) or ZERO

    positions: List[BrokerPosition] = []
    swvxx_value = ZERO
    for raw in sec.get("positions", []) or []:
        instrument = raw.get("instrument", {})
        symbol = str(instrument.get("symbol", "")).upper()
        if not symbol:
            continue
        long_qty = Decimal(str(raw.get("longQuantity", 0) or 0))
        market_value = Decimal(str(raw.get("marketValue", 0) or 0))
        if symbol == sweep_symbol.upper():
            swvxx_value += market_value
            continue
        if long_qty <= 0:
            continue
        qty = int(long_qty)
        last = float(market_value / long_qty) if long_qty else 0.0
        positions.append(BrokerPosition(
            symbol=symbol, quantity=qty,
            avg_cost=float(raw.get("averagePrice", 0) or 0),
            current_price=round(last, 4),
            market_value=float(market_value),
            day_pnl=float(raw.get("currentDayProfitLoss", 0) or 0),
        ))

    positions_value = sum((Decimal(str(p.market_value)) for p in positions), ZERO)
    balances = BrokerBalances(
        liquidation_value=q(nlv), settled_cash=q(settled), unsettled_cash=q(unsettled),
        swvxx_value=q(swvxx_value), positions_value=q(positions_value),
    )
    return balances, positions


class BrokerSync:
    """Polls the broker and keeps a ledger + position snapshot current."""

    def __init__(
        self,
        rest_client,
        ledger: SettlementLedger,
        anchor: NlvAnchor,
        cfg: Dict[str, Any],
    ):
        self._rest = rest_client
        self._ledger = ledger
        self._anchor = anchor
        acct = cfg.get("account", {})
        recon = cfg.get("reconciliation", {})
        self._suffix = str(acct.get("required_suffix", "015"))
        self._exclude = {s.upper() for s in recon.get("exclude_symbols", [])} | {"SWVXX"}
        self._poll_interval = float(recon.get("poll_interval_sec", 30.0))

        self._lock = threading.RLock()
        self._positions: List[BrokerPosition] = []
        self._managed_symbols: set = set()
        self._known_unmanaged: Dict[str, UnmanagedPosition] = {}
        self.last_ok_at: Optional[float] = None
        self.last_error: Optional[str] = None
        self.last_latency_ms: Optional[float] = None
        self.consecutive_failures = 0

    # -- blocking ------------------------------------------------------

    def sync_once(self) -> BrokerBalances:
        """One poll cycle. Raises on parse failure; records failures for health."""
        auth = getattr(self._rest, "_auth_manager", None)
        if auth and getattr(auth, "auth_circuit_open", False):
            logger.warning("BrokerSync: skipping sync because auth circuit breaker is OPEN (AUTH_LOCKED).")
            with self._lock:
                self.last_error = "Auth circuit breaker OPEN (AUTH_LOCKED)"
            return self._ledger.balances

        started = time.monotonic()
        try:
            accounts = self._rest.get_accounts(fields="positions")
            match = next(
                (a for a in accounts
                 if str(a.get("securitiesAccount", {}).get("accountNumber", "")).endswith(self._suffix)),
                None,
            )
            if match is None:
                raise BrokerParseError(f"No account ending in '{self._suffix}' in /accounts response")
            balances, positions = parse_account(match)
        except Exception as exc:
            with self._lock:
                self.consecutive_failures += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
            raise

        self._ledger.sync_from_broker(balances)
        try:
            self._anchor.update(balances.liquidation_value)
        except Exception as exc:
            logger.warning("NLV anchor update failed: %s", exc)

        with self._lock:
            self._positions = positions
            self.last_ok_at = time.time()
            self.last_error = None
            self.consecutive_failures = 0
            self.last_latency_ms = round((time.monotonic() - started) * 1000.0, 1)
            self._refresh_unmanaged_locked()
        return balances

    # -- views ---------------------------------------------------------

    def set_managed_symbols(self, symbols: Iterable[str]) -> None:
        with self._lock:
            self._managed_symbols = {s.upper() for s in symbols}
            self._refresh_unmanaged_locked()

    def _refresh_unmanaged_locked(self) -> None:
        now = datetime.now(_EDT)
        current: Dict[str, UnmanagedPosition] = {}
        for p in self._positions:
            if p.symbol in self._managed_symbols or p.symbol in self._exclude:
                continue
            prior = self._known_unmanaged.get(p.symbol)
            current[p.symbol] = UnmanagedPosition(
                symbol=p.symbol, quantity=p.quantity, avg_cost=p.avg_cost,
                current_price=p.current_price,
                detected_at=prior.detected_at if prior else now,
            )
        self._known_unmanaged = current

    def get_positions(self) -> List[BrokerPosition]:
        with self._lock:
            return list(self._positions)

    def get_unmanaged_positions(self) -> List[UnmanagedPosition]:
        with self._lock:
            return list(self._known_unmanaged.values())

    def seconds_since_ok(self) -> Optional[float]:
        with self._lock:
            return None if self.last_ok_at is None else time.time() - self.last_ok_at

    @property
    def poll_interval(self) -> float:
        return self._poll_interval

    def get_sync_interval(self, lifecycle_phase: str = "CORE_SESSION", limiter: Optional[Any] = None) -> float:
        """
        Calculates sync interval:
        - 45s during market hours (CORE_SESSION)
        - 300s during off-market hours
        - Throttled to 60s if daily call quota warning threshold (3,200) is exceeded
        """
        interval = 45.0 if lifecycle_phase == "CORE_SESSION" else 300.0
        if limiter is not None and getattr(limiter, "should_throttle_sync", lambda: False)():
            interval = max(interval, 60.0)
        return interval

