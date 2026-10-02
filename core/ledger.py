"""
core/ledger.py
==============
Double-Entry Cash Ledger and Algorithmic Universe Exclusion Mask.

Enforces T+1 cash settlement compliance (preventing Good Faith Violations)
and IRC §1091 wash-sale isolation for the Schwab Day-Trading Engine.

Components
----------
ComplianceLedger
    Manages three cash buckets and enforces the sizing invariant:
        Gross Order Value <= Bucket1 (Settled_Cash) - $10.00 buffer

    Bucket 1 — Settled_Cash:    Fully cleared, usable intraday capital.
    Bucket 2 — Unsettled_Proceeds: Intraday sale proceeds locked until T+1.
    Bucket 3 — Pending_ACH_Credit: Incoming electronic deposits in transit.

    Scheduled events (fired by LedgerScheduler):
        09:00:00 AM EDT → rollover_t1()   — Bucket2 → Bucket1 (NSCC clearing)
        09:15:00 AM EDT → sync_with_broker() — Reconcile Bucket1 vs broker cashBalance

UniverseExclusionMask
    Implements the IRC §1091 wash-sale algorithmic isolation.
    At 09:15 AM EDT, any holding whose lot was acquired before today midnight
    EDT is classified as a swing asset and masked from day-trading.
    Masked symbols are replaced by their config-defined rotation partners.

LedgerScheduler
    Background thread that fires both scheduled ledger events at their
    exact EDT wall-clock times, deduplicating within each calendar day.

All monetary values use Python's decimal.Decimal for exact arithmetic.
All configuration is sourced from config.yaml.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Set, Tuple

import pytz

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_EDT           = pytz.timezone("America/New_York")
_CENT          = Decimal("0.01")     # Two-decimal-place quantization
_ZERO          = Decimal("0.00")
_RECONCILE_WARN_THRESHOLD = Decimal("0.50")  # Log WARNING if sync delta > $0.50
_RECONCILE_ERR_THRESHOLD  = Decimal("5.00")  # Log ERROR  if sync delta > $5.00


# ===========================================================================
# LedgerSnapshot — immutable state view
# ===========================================================================

@dataclass(frozen=True)
class LedgerSnapshot:
    """Immutable point-in-time snapshot of the three cash buckets."""
    bucket1_settled:     Decimal
    bucket2_unsettled:   Decimal
    bucket3_pending_ach: Decimal
    max_order_value:     Decimal          # bucket1 - cash_buffer
    as_of:               datetime


# ===========================================================================
# TradeRecord — internal audit log entry
# ===========================================================================

@dataclass
class TradeRecord:
    """Lightweight immutable record of a single fill stored for daily audit."""
    timestamp:  datetime
    symbol:     str
    side:       str          # "BUY" or "SELL"
    quantity:   int
    cost_basis: Decimal      # For BUY: total cost deducted from Bucket1
                             # For SELL: gross proceeds credited to Bucket2


# ===========================================================================
# ComplianceLedger
# ===========================================================================

class ComplianceLedger:
    """
    Thread-safe double-entry cash ledger enforcing T+1 settlement compliance.

    The ledger maintains three cash buckets and one hard invariant:

        max_order_value = Bucket1 - cash_buffer
        Invariant: all orders must satisfy cost <= max_order_value

    Bucket lifecycle
    ----------------
    Initial state (from sandbox_baseline):
        Bucket1 = $1,000.00   Bucket2 = $0.00   Bucket3 = $0.00

    On a buy fill (10:00 AM, 100 shares @ $25.00 = $2,500.00):
        Bucket1 -= $2,500.00

    On a sell fill (11:00 AM, 100 shares @ $26.00 = $2,600.00):
        Bucket2 += $2,600.00   (proceeds locked until T+1)

    On T+1 rollover (next day 09:00 AM EDT):
        Bucket1 += Bucket2; Bucket2 = $0.00

    On ACH credit confirmation:
        Bucket1 += Bucket3; Bucket3 = $0.00
    """

    def __init__(self, cfg: dict) -> None:
        """
        Args:
            cfg: Top-level config dict loaded from config.yaml.
        """
        risk_cfg = cfg.get("risk", {})
        sandbox  = Decimal(str(risk_cfg.get("sandbox_baseline", "1000.00")))
        self._cash_buffer = Decimal(str(risk_cfg.get("cash_buffer", "10.00")))

        self._bucket1: Decimal = sandbox   # Settled_Cash
        self._bucket2: Decimal = _ZERO     # Unsettled_Proceeds
        self._bucket3: Decimal = _ZERO     # Pending_ACH_Credit

        # Daily audit log — cleared at midnight EDT by LedgerScheduler
        self._trade_log: List[TradeRecord] = []

        # Symbols the engine opened positions in today (UTC date → set of symbols)
        # Key = date string "YYYY-MM-DD", Value = set of symbols
        self._todays_opened: Dict[str, Set[str]] = {}

        self._lock = threading.RLock()  # Reentrant: some public methods call others

        logger.info(
            "ComplianceLedger initialised — Bucket1=$%.2f, buffer=$%.2f",
            self._bucket1, self._cash_buffer,
        )

    # ------------------------------------------------------------------
    # Read-only accessors
    # ------------------------------------------------------------------

    def snapshot(self) -> LedgerSnapshot:
        """Return an immutable snapshot of current bucket state."""
        with self._lock:
            return LedgerSnapshot(
                bucket1_settled=self._bucket1,
                bucket2_unsettled=self._bucket2,
                bucket3_pending_ach=self._bucket3,
                max_order_value=self._max_order_value(),
                as_of=datetime.now(_EDT),
            )

    def max_order_value(self) -> Decimal:
        """Thread-safe read of maximum allowed single order cost."""
        with self._lock:
            return self._max_order_value()

    def _max_order_value(self) -> Decimal:
        """Internal (call inside lock)."""
        return max(_ZERO, self._bucket1 - self._cash_buffer)

    # ------------------------------------------------------------------
    # Invariant enforcement
    # ------------------------------------------------------------------

    def check_order_allowed(self, gross_order_value: Decimal) -> Tuple[bool, str]:
        """
        Check the T+1 sizing invariant before submitting an order.

        Invariant: Gross Order Value <= Bucket1 - $10.00 buffer

        Args:
            gross_order_value: Total cost of the prospective order (price × qty).

        Returns:
            (True, "")                    — order is permitted.
            (False, "<reason string>")    — order is blocked; reason explains why.
        """
        gov = _quantize(gross_order_value)
        with self._lock:
            available = self._max_order_value()
            if gov <= _ZERO:
                return False, f"Invalid order value: ${gov}"
            if gov > available:
                return False, (
                    f"Order value ${gov} exceeds max allowed ${available} "
                    f"(Bucket1=${self._bucket1} - buffer=${self._cash_buffer}). "
                    "GFV prevention — order blocked."
                )
            return True, ""

    # ------------------------------------------------------------------
    # Fill processing
    # ------------------------------------------------------------------

    def process_fill_buy(
        self,
        symbol: str,
        quantity: int,
        fill_price: Decimal,
    ) -> Decimal:
        """
        Record a buy fill: deduct total cost from Bucket1.

        This must only be called AFTER the order has actually filled
        (confirmed by the execution engine). The cost is pre-validated
        by check_order_allowed() earlier in the order lifecycle.

        Args:
            symbol:     Ticker symbol (e.g. "SOXL").
            quantity:   Number of shares filled.
            fill_price: Actual fill price per share (2 decimal places).

        Returns:
            The total cost deducted from Bucket1 (quantity × fill_price).

        Raises:
            ValueError: If the deduction would make Bucket1 negative (safety guard).
        """
        cost = _quantize(Decimal(str(fill_price)) * Decimal(quantity))
        with self._lock:
            if cost > self._bucket1:
                raise ValueError(
                    f"ComplianceLedger: BUY fill for {quantity} {symbol} @ ${fill_price} "
                    f"(cost=${cost}) exceeds Bucket1=${self._bucket1}. "
                    "This indicates a missed invariant check — investigate immediately."
                )
            self._bucket1 -= cost
            self._bucket1  = _quantize(self._bucket1)

            # Track today's opened symbols for wash-sale mask (EDT date)
            today_key = datetime.now(_EDT).strftime("%Y-%m-%d")
            self._todays_opened.setdefault(today_key, set()).add(symbol)

            record = TradeRecord(
                timestamp=datetime.now(_EDT),
                symbol=symbol,
                side="BUY",
                quantity=quantity,
                cost_basis=cost,
            )
            self._trade_log.append(record)

        logger.info(
            "LEDGER BUY  | %-6s %4d shares @ $%-8.2f | cost=$%-10.2f | "
            "B1=$%-10.2f B2=$%-10.2f",
            symbol, quantity, fill_price, cost,
            self._bucket1, self._bucket2,
        )
        return cost

    def process_fill_sell(
        self,
        symbol: str,
        quantity: int,
        fill_price: Decimal,
    ) -> Decimal:
        """
        Record a sell fill: credit gross proceeds to Bucket2 (T+1 locked).

        Sale proceeds are NOT immediately available for new orders.
        They remain in Bucket2 until the 09:00 AM EDT T+1 rollover transfers
        them to Bucket1.

        Args:
            symbol:     Ticker symbol.
            quantity:   Number of shares sold.
            fill_price: Actual fill price per share.

        Returns:
            Gross proceeds credited to Bucket2.
        """
        proceeds = _quantize(Decimal(str(fill_price)) * Decimal(quantity))
        with self._lock:
            self._bucket2 += proceeds
            self._bucket2  = _quantize(self._bucket2)

            record = TradeRecord(
                timestamp=datetime.now(_EDT),
                symbol=symbol,
                side="SELL",
                quantity=quantity,
                cost_basis=proceeds,
            )
            self._trade_log.append(record)

        logger.info(
            "LEDGER SELL | %-6s %4d shares @ $%-8.2f | proceeds=$%-10.2f | "
            "B1=$%-10.2f B2=$%-10.2f",
            symbol, quantity, fill_price, proceeds,
            self._bucket1, self._bucket2,
        )
        return proceeds

    def credit_ach(self, amount: Decimal) -> None:
        """
        Credit an incoming ACH deposit to Bucket3 (Pending_ACH_Credit).
        A separate confirmation step moves Bucket3 → Bucket1 when the
        deposit is confirmed settled.

        Args:
            amount: ACH credit amount in dollars.
        """
        amount = _quantize(amount)
        with self._lock:
            self._bucket3 += amount
            self._bucket3  = _quantize(self._bucket3)
        logger.info("LEDGER ACH CREDIT | $%.2f → Bucket3=$%.2f", amount, self._bucket3)

    def confirm_ach_settlement(self) -> Decimal:
        """
        Move a confirmed ACH deposit from Bucket3 → Bucket1.

        Returns:
            The amount transferred.
        """
        with self._lock:
            transferred    = self._bucket3
            self._bucket1 += transferred
            self._bucket1  = _quantize(self._bucket1)
            self._bucket3  = _ZERO
        logger.info(
            "LEDGER ACH SETTLED | $%.2f transferred B3→B1 | B1=$%.2f",
            transferred, self._bucket1,
        )
        return transferred

    # ------------------------------------------------------------------
    # Scheduled ledger events
    # ------------------------------------------------------------------

    def rollover_t1(self) -> Decimal:
        """
        09:00:00 AM EDT — T+1 NSCC Clearing Rollover.

        Transfers all of Bucket2 (Unsettled_Proceeds) into Bucket1
        (Settled_Cash), making yesterday's intraday sale proceeds available
        for new orders.

        Returns:
            Amount transferred from Bucket2 → Bucket1.
        """
        with self._lock:
            transferred    = self._bucket2
            self._bucket1 += transferred
            self._bucket1  = _quantize(self._bucket1)
            self._bucket2  = _ZERO

        now_edt = datetime.now(_EDT)
        logger.info(
            "LEDGER T+1 ROLLOVER | %s | $%.2f moved B2→B1 | B1=$%.2f B2=$%.2f",
            now_edt.strftime("%Y-%m-%d %H:%M:%S %Z"),
            transferred,
            self._bucket1,
            self._bucket2,
        )
        return transferred

    def sync_with_broker(self, broker_cash_balance: Decimal) -> Decimal:
        """
        09:15:00 AM EDT — Broker Telemetry Sync.

        Compares the internal Bucket1 (Settled_Cash) against the broker-
        reported `cashBalance` from the account endpoint. Any discrepancy
        is logged at the appropriate severity level.

        If the delta exceeds $5.00, the internal Bucket1 is updated to the
        broker value (conservative: trust the authoritative source) and a
        WARNING is emitted for operator review.

        Args:
            broker_cash_balance: The `cashBalance` value from the Schwab
                                 account API response.

        Returns:
            The absolute delta: |broker_cash_balance - internal_bucket1|.
        """
        broker_val = _quantize(broker_cash_balance)
        with self._lock:
            internal_val = self._bucket1
            delta        = abs(broker_val - internal_val)

            if delta == _ZERO:
                logger.info(
                    "LEDGER BROKER SYNC | ✓ Bucket1 matches broker cashBalance=$%.2f",
                    broker_val,
                )
            elif delta <= _RECONCILE_WARN_THRESHOLD:
                logger.info(
                    "LEDGER BROKER SYNC | Minor delta $%.4f "
                    "(internal=$%.2f, broker=$%.2f) — within rounding tolerance.",
                    delta, internal_val, broker_val,
                )
            elif delta <= _RECONCILE_ERR_THRESHOLD:
                logger.warning(
                    "LEDGER BROKER SYNC | ⚠ Delta $%.4f detected "
                    "(internal=$%.2f, broker=$%.2f). Reconciliation recommended.",
                    delta, internal_val, broker_val,
                )
            else:
                logger.error(
                    "LEDGER BROKER SYNC | ✗ LARGE DELTA $%.4f "
                    "(internal=$%.2f, broker=$%.2f). "
                    "Overriding Bucket1 with broker authoritative value.",
                    delta, internal_val, broker_val,
                )
                self._bucket1 = broker_val

        return delta

    def get_todays_opened_symbols(self) -> FrozenSet[str]:
        """
        Return the set of symbols the engine opened positions in today (EDT).
        Used by UniverseExclusionMask to distinguish new vs. swing positions.
        """
        today_key = datetime.now(_EDT).strftime("%Y-%m-%d")
        with self._lock:
            return frozenset(self._todays_opened.get(today_key, set()))

    def reset_daily_state(self) -> None:
        """
        Called at midnight EDT to clear the daily trade log and purge
        stale entries from _todays_opened (keep only today's entry).
        """
        today_key = datetime.now(_EDT).strftime("%Y-%m-%d")
        with self._lock:
            self._trade_log.clear()
            # Retain only today's opened symbols; discard prior days
            todays_set = self._todays_opened.get(today_key, set())
            self._todays_opened = {today_key: todays_set}
        logger.info("LEDGER DAILY RESET | trade log cleared for new session.")

    def get_trade_log(self) -> List[TradeRecord]:
        """Return a copy of today's trade records (thread-safe)."""
        with self._lock:
            return list(self._trade_log)


# ===========================================================================
# UniverseExclusionMask
# ===========================================================================

@dataclass
class MaskResult:
    """Result of a single universe exclusion mask evaluation."""
    evaluation_time:  datetime
    masked_symbols:   FrozenSet[str]       # Symbols blocked from day-trading
    rotation_applied: Dict[str, str]       # { original_symbol: rotation_symbol }
    active_universe:  List[str]            # Final tradeable symbol list (post-rotation)
    reason:           Dict[str, str]       # { masked_symbol: reason_string }


class UniverseExclusionMask:
    """
    IRC §1091 Algorithmic Universe Exclusion Mask.

    At 09:15 AM EDT, queries open broker positions. Any holding in the
    configured `active_swing_symbols` list whose acquisition date predates
    today midnight EDT is classified as a swing asset and masked from
    day-trading to prevent wash-sale contamination of multi-day P&L.

    Masked symbols are replaced by their designated rotation partners
    (from config.yaml → universe.wash_sale_rotations).

    Rotation map (from spec):
        SOXL → FNGU
        TQQQ → CONL
        TNA  → DPST

    Logic
    -----
    A position is treated as a PRE-TODAY swing asset if:
        (a) The symbol is in `active_swing_symbols`, AND
        (b) The broker reports a non-zero quantity open for that symbol, AND
        (c) The engine's ledger did NOT open that position today
            (i.e., symbol ∉ ledger.get_todays_opened_symbols()).

    This is correct because:
    - At 09:15 AM (15 min before market open at 09:30), the engine has not
      yet traded today. Any existing position therefore predates today.
    - Tracking `todays_opened_symbols` ensures the mask stays accurate
      if ever re-evaluated mid-session (e.g., after a system restart).
    """

    def __init__(self, cfg: dict) -> None:
        """
        Args:
            cfg: Top-level config dict loaded from config.yaml.
        """
        universe_cfg = cfg.get("universe", {})

        self._swing_symbols: List[str] = universe_cfg.get("active_swing_symbols", [])
        self._rotations:     Dict[str, str] = universe_cfg.get("wash_sale_rotations", {})
        self._candidates:    List[str] = universe_cfg.get("day_trade_candidates", [])

        # Most recently computed mask (updated at 09:15 AM each day)
        self._current_result: Optional[MaskResult] = None
        self._lock = threading.Lock()

        logger.info(
            "UniverseExclusionMask initialised — swing_symbols=%s, rotations=%s",
            self._swing_symbols, self._rotations,
        )

    # ------------------------------------------------------------------
    # Core evaluation
    # ------------------------------------------------------------------

    def evaluate(
        self,
        broker_positions: List[Dict[str, Any]],
        todays_opened_symbols: FrozenSet[str],
    ) -> MaskResult:
        """
        Evaluate the exclusion mask against current broker positions.

        This is the primary method called by LedgerScheduler at 09:15 AM EDT.

        Args:
            broker_positions:
                List of position dicts from the Schwab account positions endpoint.
                Each dict must contain at minimum:
                    "symbol"   (str)  — ticker
                    "quantity" (float | int) — long quantity held
                Optionally:
                    "acquired_date" (str "YYYY-MM-DD") — lot acquisition date.
                    If absent, the position is treated as pre-today by default.

            todays_opened_symbols:
                Symbols the engine opened today (from ledger.get_todays_opened_symbols()).
                These are EXCLUDED from masking even if the broker shows them open.

        Returns:
            MaskResult with the masked set, rotation map, and active universe.
        """
        now_edt    = datetime.now(_EDT)
        today_edt  = now_edt.date()
        today_midnight_edt = _EDT.localize(
            datetime(today_edt.year, today_edt.month, today_edt.day, 0, 0, 0)
        )

        # Build a quick-lookup dict: symbol → position dict
        open_positions: Dict[str, Dict[str, Any]] = {}
        for pos in broker_positions:
            sym = pos.get("symbol", "").upper()
            qty = float(pos.get("quantity", 0))
            if sym and qty > 0:
                open_positions[sym] = pos

        masked_symbols:   Set[str]       = set()
        rotation_applied: Dict[str, str] = {}
        reason:           Dict[str, str] = {}

        for sym in self._swing_symbols:
            if sym not in open_positions:
                # No open position in this swing symbol — nothing to mask
                continue

            if sym in todays_opened_symbols:
                # Engine opened this position today — NOT a pre-existing swing asset
                logger.debug(
                    "ExclusionMask: %s held but opened today by engine — not masked.", sym
                )
                continue

            # Check acquisition date if available from the broker response
            pos_data = open_positions[sym]
            acquired_date_str = pos_data.get("acquired_date")

            if acquired_date_str:
                try:
                    acquired_dt = _EDT.localize(
                        datetime.strptime(acquired_date_str, "%Y-%m-%d")
                    )
                    if acquired_dt >= today_midnight_edt:
                        # Lot acquired today — not a pre-existing swing asset
                        logger.debug(
                            "ExclusionMask: %s lot acquired today (%s) — not masked.",
                            sym, acquired_date_str,
                        )
                        continue
                except ValueError:
                    logger.warning(
                        "ExclusionMask: could not parse acquired_date '%s' for %s — "
                        "treating as pre-today (masking).",
                        acquired_date_str, sym,
                    )

            # Pre-today swing position confirmed → MASK
            qty = open_positions[sym].get("quantity", 0)
            masked_symbols.add(sym)
            reason[sym] = (
                f"Open swing position ({qty} shares) acquired before "
                f"{today_midnight_edt.strftime('%Y-%m-%d %H:%M %Z')} — "
                f"IRC §1091 wash-sale risk."
            )
            logger.warning(
                "ExclusionMask: %-6s MASKED — %s", sym, reason[sym]
            )

        # Build active universe with rotations applied
        active_universe: List[str] = []
        for candidate in self._candidates:
            if candidate in masked_symbols:
                rotation = self._rotations.get(candidate)
                if rotation:
                    rotation_applied[candidate] = rotation
                    # Add rotation only if it is itself not masked
                    if rotation not in masked_symbols:
                        active_universe.append(rotation)
                        logger.info(
                            "ExclusionMask: %s → ROTATE to %s (wash-sale avoidance)",
                            candidate, rotation,
                        )
                    else:
                        logger.warning(
                            "ExclusionMask: both %s and its rotation %s are masked — "
                            "dropping from universe.",
                            candidate, rotation,
                        )
                else:
                    logger.warning(
                        "ExclusionMask: %s masked but no rotation defined — "
                        "dropping from universe.",
                        candidate,
                    )
            else:
                active_universe.append(candidate)

        # Deduplicate while preserving order
        seen: Set[str] = set()
        deduped: List[str] = []
        for sym in active_universe:
            if sym not in seen:
                seen.add(sym)
                deduped.append(sym)
        active_universe = deduped

        result = MaskResult(
            evaluation_time=now_edt,
            masked_symbols=frozenset(masked_symbols),
            rotation_applied=rotation_applied,
            active_universe=active_universe,
            reason=reason,
        )

        with self._lock:
            self._current_result = result

        logger.info(
            "ExclusionMask RESULT | masked=%s | active_universe=%s",
            sorted(masked_symbols) or "none",
            active_universe,
        )
        return result

    def get_active_universe(self) -> List[str]:
        """
        Return the most recently computed active trading universe.

        Returns the full candidate list (no masking applied) if evaluate()
        has not been called yet today (safe default for pre-09:15 startup).
        """
        with self._lock:
            if self._current_result is None:
                return list(self._candidates)
            return list(self._current_result.active_universe)

    def get_masked_symbols(self) -> FrozenSet[str]:
        """Return the current set of IRC §1091 masked symbols."""
        with self._lock:
            if self._current_result is None:
                return frozenset()
            return self._current_result.masked_symbols

    def is_symbol_tradeable(self, symbol: str) -> bool:
        """
        Quick check: is a given symbol currently in the active universe?

        Args:
            symbol: Ticker string.

        Returns:
            True if the symbol is permitted for day-trading today.
        """
        return symbol.upper() in self.get_active_universe()


# ===========================================================================
# LedgerScheduler
# ===========================================================================

class LedgerScheduler:
    """
    Background thread that fires scheduled ledger events at exact EDT times.

    Scheduled events:
        09:00:00 AM EDT → ledger.rollover_t1()
        09:15:00 AM EDT → ledger.sync_with_broker() + mask.evaluate()
        00:00:00 AM EDT → ledger.reset_daily_state()  (midnight reset)

    Each event fires exactly ONCE per calendar day. The scheduler tracks
    which events have already fired today and skips re-firing until the
    next calendar day.

    The broker-sync callback (broker_sync_fn) must be provided by the
    caller and is responsible for querying the Schwab REST API for the
    current `cashBalance` and open `positions` list. This keeps the ledger
    module decoupled from the REST client.

    Configuration sources:
        schedule.t1_rollover_time    → "09:00:00"
        schedule.telemetry_sync_time → "09:15:00"
        schedule.timezone            → "America/New_York"
    """

    def __init__(
        self,
        ledger: ComplianceLedger,
        mask: UniverseExclusionMask,
        cfg: dict,
        broker_sync_fn: Optional[Callable[[], Tuple[Decimal, List[Dict[str, Any]]]]] = None,
    ) -> None:
        """
        Args:
            ledger:
                The live ComplianceLedger instance.
            mask:
                The live UniverseExclusionMask instance.
            cfg:
                Top-level config dict.
            broker_sync_fn:
                Callable that returns (cash_balance, positions_list).
                The scheduler calls this at 09:15 AM to feed sync_with_broker()
                and mask.evaluate(). If None, the sync step is skipped with a
                warning (useful for unit testing).
        """
        sched_cfg = cfg.get("schedule", {})
        tz_name   = sched_cfg.get("timezone", "America/New_York")

        self._ledger         = ledger
        self._mask           = mask
        self._broker_sync_fn = broker_sync_fn
        self._tz             = pytz.timezone(tz_name)

        self._rollover_time  = sched_cfg.get("t1_rollover_time", "09:00:00")
        self._sync_time      = sched_cfg.get("telemetry_sync_time", "09:15:00")

        self._stop_event = threading.Event()
        self._thread:     Optional[threading.Thread] = None

        # Track which events fired today (keyed by event name + date string)
        self._fired: Set[str] = set()

        logger.info(
            "LedgerScheduler initialised — rollover=%s EDT, sync=%s EDT (tz=%s)",
            self._rollover_time, self._sync_time, tz_name,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the scheduler background thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="LedgerSchedulerThread",
            daemon=True,
        )
        self._thread.start()
        logger.info("LedgerScheduler: background thread started.")

    def stop(self) -> None:
        """Stop the scheduler thread cleanly."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("LedgerScheduler: stopped.")

    # ------------------------------------------------------------------
    # Scheduler loop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """
        Main scheduler loop. Polls once per second and fires events when
        the current EDT time crosses a scheduled boundary.

        Deduplication key: "<event_name>_<YYYY-MM-DD>" — prevents double-firing
        if the system clock drifts or the scheduler restarts mid-day.
        """
        logger.info("LedgerScheduler: running.")

        while not self._stop_event.wait(timeout=1.0):
            now = datetime.now(self._tz)
            date_key = now.strftime("%Y-%m-%d")
            time_str = now.strftime("%H:%M:%S")

            # --- Midnight reset ---
            if time_str >= "00:00:00" and self._should_fire("midnight", date_key):
                self._fire_midnight_reset()

            # --- 09:00:00 — T+1 Rollover ---
            if time_str >= self._rollover_time and self._should_fire("rollover", date_key):
                self._fire_rollover()

            # --- 09:15:00 — Broker Sync + Wash-Sale Mask ---
            if time_str >= self._sync_time and self._should_fire("sync", date_key):
                self._fire_sync_and_mask()

    def _should_fire(self, event: str, date_key: str) -> bool:
        """
        Return True if this event has NOT yet fired today. Marks it as fired.
        Thread-safe via the GIL on set operations.
        """
        key = f"{event}_{date_key}"
        if key in self._fired:
            return False
        self._fired.add(key)
        # Prune old entries (keep only today's keys)
        self._fired = {k for k in self._fired if k.endswith(date_key)}
        return True

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _fire_midnight_reset(self) -> None:
        """Midnight EDT: clear daily state for the new session."""
        try:
            self._ledger.reset_daily_state()
        except Exception as exc:  # noqa: BLE001
            logger.exception("LedgerScheduler: midnight reset failed: %s", exc)

    def _fire_rollover(self) -> None:
        """09:00:00 AM EDT: T+1 NSCC clearing rollover (Bucket2 → Bucket1)."""
        try:
            self._ledger.rollover_t1()
        except Exception as exc:  # noqa: BLE001
            logger.exception("LedgerScheduler: T+1 rollover failed: %s", exc)

    def _fire_sync_and_mask(self) -> None:
        """
        09:15:00 AM EDT:
            1. Query broker for cashBalance + open positions.
            2. Sync Bucket1 against broker cashBalance.
            3. Evaluate the IRC §1091 universe exclusion mask.
        """
        if self._broker_sync_fn is None:
            logger.warning(
                "LedgerScheduler: broker_sync_fn not set — "
                "skipping 09:15 sync and wash-sale mask evaluation."
            )
            return

        try:
            cash_balance, positions = self._broker_sync_fn()

            # Step 1: reconcile internal ledger
            self._ledger.sync_with_broker(cash_balance)

            # Step 2: evaluate IRC §1091 exclusion mask
            todays_opened = self._ledger.get_todays_opened_symbols()
            self._mask.evaluate(
                broker_positions=positions,
                todays_opened_symbols=todays_opened,
            )

        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "LedgerScheduler: 09:15 broker sync / mask evaluation failed: %s", exc
            )


# ===========================================================================
# Utility helpers
# ===========================================================================

def _quantize(value: Decimal) -> Decimal:
    """Round a Decimal to 2 decimal places (ROUND_DOWN — conservative)."""
    try:
        return value.quantize(_CENT, rounding=ROUND_DOWN)
    except InvalidOperation:
        raise ValueError(f"Cannot quantize value: {value!r}")


def build_from_config(
    cfg: dict,
    broker_sync_fn: Optional[Callable[[], Tuple[Decimal, List[Dict[str, Any]]]]] = None,
) -> Tuple[ComplianceLedger, UniverseExclusionMask, LedgerScheduler]:
    """
    Factory: construct and wire all three Phase 3 components from config.yaml.

    Args:
        cfg:            Top-level config dict.
        broker_sync_fn: Callable → (cash_balance, positions). See LedgerScheduler.

    Returns:
        (ledger, mask, scheduler) — all initialised but not yet started.
        Call scheduler.start() after the REST client is ready.
    """
    ledger    = ComplianceLedger(cfg)
    mask      = UniverseExclusionMask(cfg)
    scheduler = LedgerScheduler(
        ledger=ledger,
        mask=mask,
        cfg=cfg,
        broker_sync_fn=broker_sync_fn,
    )
    return ledger, mask, scheduler
