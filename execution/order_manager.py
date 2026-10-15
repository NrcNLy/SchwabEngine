"""
execution/order_manager.py
==========================
Account-firewalled order submission and lifecycle management for the
Schwab Day-Trading Engine.

Account Firewall
----------------
ALL order submissions are gated by an account pre-flight check:
    1. Fetch all linked accounts via GET /trader/v1/accounts/accountNumbers.
    2. Locate the account whose ``accountNumber`` ends in the configured
       required_suffix (default "015").
    3. Verify that none of the Schwab-defined blocked keywords appear in the
       account metadata (ROBO, INTELLIGENT, IRA, PORTFOLIO_MANAGED).
    4. Cache the account's ``hashValue`` in ``_account_hash``.
    5. Every subsequent order POST uses this cached hash exclusively.

If no matching account is found, or if a blocked keyword is detected,
``initialize_firewall()`` raises ``AccountFirewallError`` and the engine
aborts before submitting any order.

Order Formatting
----------------
Per spec (research doc): "Sub-penny pricing is strictly formatted to
2 decimal places to avoid HTTP 400 rejections."
    - All prices:    f"{price:.2f}"   (e.g.  "25.50")
    - All quantities: int(quantity)   (e.g.  10)

All order bodies use ``orderStrategyType: SINGLE`` and ``duration: DAY``.

Order ID Extraction
-------------------
Schwab returns HTTP 201 Created with a ``Location`` header for placed orders:
    Location: https://api.schwabapi.com/trader/v1/accounts/{hash}/orders/{orderId}
The order ID is the trailing URL path segment:
    order_id = location_header.rstrip("/").split("/")[-1]

Configuration sources (config.yaml):
    account.required_suffix   → "015"
    account.blocked_keywords  → ["ROBO", "INTELLIGENT", "IRA", "PORTFOLIO_MANAGED"]
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import time
from typing import Any, Dict, List, Optional, Tuple
import uuid
import pytz

from execution.sizing_governor import BayesianSizingGovernor, SizingVerdict

logger = logging.getLogger(__name__)

_EDT = pytz.timezone("America/New_York")

# ---------------------------------------------------------------------------
# Schwab order constants (Schwab API v1 field names)
# ---------------------------------------------------------------------------
_ORDER_STRATEGY_TYPE = "SINGLE"
_DURATION_DAY        = "DAY"
_SESSION_NORMAL      = "NORMAL"
_ASSET_TYPE_EQUITY   = "EQUITY"
_ORDER_LEG_EQUITY    = "EQUITY"

# Order types
_OT_LIMIT  = "LIMIT"
_OT_MARKET = "MARKET"
_OT_STOP   = "STOP"

# Instructions
_INST_BUY  = "BUY"
_INST_SELL = "SELL"

# Tier 2 Stop Throttling & Coalescing
TIER2_MIN_DELTA_PCT = 0.015  # 1.5% minimum price movement threshold
TIER2_COOLDOWN_SEC  = 180.0  # 180-second minimum cooldown between cancel-replace requests



class AccountFirewallError(RuntimeError):
    """Raised when the account firewall pre-flight check fails."""


class GoodFaithViolationBlockedError(RuntimeError):
    """Raised when an intraday sell order is blocked to prevent a Good Faith Violation (GFV)."""


class OrderManager:
    """
    Authenticated order submission with account firewall enforcement.

    All methods are safe to call from multiple threads — the account hash
    is read-only after ``initialize_firewall()`` completes, so no locking
    is needed on the cached state.

    Typical usage
    -------------
        om = OrderManager(rest_client, ledger, cfg)
        om.initialize_firewall()            # One-time startup check

        order_id = om.place_limit_buy("SOXL", 10, 25.50)
        tier2_id = om.place_broker_stop("SOXL", 10, 24.00)
        om.cancel_order(tier2_id)
        om.execute_market_sell("SOXL", 10)
    """

    def __init__(
        self,
        rest_client,
        ledger,
        cfg: dict,
        sizing_governor: Optional[BayesianSizingGovernor] = None,
    ) -> None:
        """
        Args:
            rest_client:     Initialised SchwabRestClient.
            ledger:          ComplianceLedger (for order cost validation).
            cfg:             Top-level config dict.
            sizing_governor: BayesianSizingGovernor instance (or instantiated from cfg).
        """
        self._rest      = rest_client
        self._ledger    = ledger
        self.sizing_governor: BayesianSizingGovernor = sizing_governor or BayesianSizingGovernor(
            config=cfg
        )
        if self._ledger is not None and hasattr(self._ledger, "sizing_governor"):
            if getattr(self._ledger, "sizing_governor", None) is None:
                self._ledger.sizing_governor = self.sizing_governor

        acct_cfg = cfg.get("account", {})
        self._required_suffix:   str       = str(acct_cfg.get("required_suffix",  "015"))
        self._blocked_keywords:  List[str] = [
            kw.upper() for kw in acct_cfg.get("blocked_keywords", [
                "ROBO", "INTELLIGENT", "IRA", "PORTFOLIO_MANAGED"
            ])
        ]

        # Populated by initialize_firewall()
        self._account_hash:   Optional[str] = None
        self._account_number: Optional[str] = None
        self._firewall_ok:    bool = False
        self._tier2_stops:    Dict[str, Dict[str, Any]] = {}
        self.telemetry:       Any = None
        self._router:         Any = None

    # ------------------------------------------------------------------
    # Account Firewall
    # ------------------------------------------------------------------

    def initialize_firewall(self) -> None:
        """
        Execute the mandatory account firewall pre-flight check.

        Steps:
            1. GET /trader/v1/accounts/accountNumbers
            2. Find the account whose ``accountNumber`` ends in ``required_suffix``.
            3. Cross-check that no ``blocked_keywords`` appear in account fields.
            4. Cache ``hashValue`` for all subsequent order routing.

        Raises:
            AccountFirewallError: If no valid account found or blocked keyword detected.
        """
        logger.info(
            "OrderManager: executing account firewall — suffix='%s', blocked=%s",
            self._required_suffix, self._blocked_keywords,
        )

        try:
            account_entries = self._rest.get_account_numbers()
        except Exception as exc:
            raise AccountFirewallError(
                f"OrderManager: failed to fetch account numbers: {exc}"
            ) from exc

        target_hash:   Optional[str] = None
        target_number: Optional[str] = None

        for entry in account_entries:
            acct_num  = str(entry.get("accountNumber", ""))
            hash_val  = str(entry.get("hashValue", ""))

            if not acct_num.endswith(self._required_suffix):
                continue

            # Blocked keyword check against account number string and any metadata
            combined_str = acct_num.upper() + hash_val.upper()
            for kw in self._blocked_keywords:
                if kw in combined_str:
                    raise AccountFirewallError(
                        f"OrderManager: account {acct_num} contains blocked keyword "
                        f"'{kw}' — aborting."
                    )

            if not hash_val:
                raise AccountFirewallError(
                    f"OrderManager: account {acct_num} has no hashValue — aborting."
                )

            target_hash   = hash_val
            target_number = acct_num
            break

        if target_hash is None:
            raise AccountFirewallError(
                f"OrderManager: no account ending in '{self._required_suffix}' found "
                f"among {len(account_entries)} linked account(s). Aborting."
            )

        self._account_hash   = target_hash
        self._account_number = target_number
        self._firewall_ok    = True
        if self._router is not None:
            self._router.account_hash = target_hash

        logger.info(
            "OrderManager: firewall PASSED — account=...%s | hash=%s…",
            target_number[-4:] if target_number and len(target_number) >= 4 else target_number,
            target_hash[:12],
        )

    @property
    def router(self) -> Any:
        """AdaptiveExecutionRouter instance attached to OrderManager."""
        if self._router is None:
            from execution.almgren_chriss import AdaptiveExecutionRouter
            self._router = AdaptiveExecutionRouter(
                rest_client=self._rest,
                account_hash=self._account_hash,
                telemetry=self.telemetry,
            )
        else:
            if self._account_hash and not getattr(self._router, "account_hash", None):
                self._router.account_hash = self._account_hash
            if self.telemetry and not getattr(self._router, "telemetry", None):
                self._router.telemetry = self.telemetry
        return self._router

    @property
    def account_hash(self) -> Optional[str]:
        """The whitelisted account's Schwab hashValue (None until firewall passes)."""
        return self._account_hash

    @property
    def account_number(self) -> Optional[str]:
        """The whitelisted account's plain account number (None until firewall passes)."""
        return self._account_number

    # ------------------------------------------------------------------
    # Position Sizing & Trade Recording
    # ------------------------------------------------------------------

    def calculate_position_size(
        self,
        symbol: str,
        price: float,
        atr: float,
        settled_cash: Optional[float] = None,
        total_nlv: Optional[float] = None,
    ) -> SizingVerdict:
        """
        Calculates position size using Bayesian Quarter-Kelly sizing, ATR volatility
        normalization, and hard T+1 cash settlement reserve gating.
        """
        if settled_cash is None:
            settled_cash = float(self._ledger.settled_cash) if self._ledger is not None else 0.0
        if total_nlv is None:
            total_nlv = float(self._ledger.nlv) if self._ledger is not None else 0.0
        return self.sizing_governor.calculate_position_size(
            symbol=symbol,
            price=price,
            atr=atr,
            settled_cash=settled_cash,
            total_nlv=total_nlv,
        )

    def record_trade_execution(
        self,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        cost_basis: Optional[float] = None,
        realized_pnl: float = 0.0,
        disallowed_loss: float = 0.0,
        regime: str = "",
        timestamp: Optional[datetime] = None,
        trade_id: Optional[str] = None,
        simulated: bool = False,
        env: str = "active",
        is_win: Optional[bool] = None,
        win_loss_ratio: Optional[float] = None,
    ) -> str:
        """
        Records a trade execution in state/trades.db with updated Bayesian posterior
        alpha, beta, and Kelly fractions.
        """
        side_upper = side.upper()
        if is_win is not None:
            self.sizing_governor.update_trade_outcome(is_win=is_win, win_loss_ratio=win_loss_ratio)
        elif side_upper == "SELL" and realized_pnl != 0.0:
            win = realized_pnl > 0
            self.sizing_governor.update_trade_outcome(is_win=win, win_loss_ratio=win_loss_ratio)

        alpha = self.sizing_governor.alpha
        beta = self.sizing_governor.beta
        kelly = self.sizing_governor.kelly_fraction

        eff_cost = cost_basis if cost_basis is not None else (quantity * price)
        eff_ts = timestamp or datetime.now(_EDT)

        trade_store = getattr(self._ledger, "trade_store", None)
        if trade_store is None:
            try:
                from core.trade_store import SQLiteTradeStore
                trade_store = SQLiteTradeStore()
            except Exception as exc:
                logger.debug("OrderManager: SQLiteTradeStore fallback failed: %s", exc)

        tid = trade_id
        if trade_store is not None:
            tid = trade_store.record_trade(
                symbol=symbol,
                side=side_upper,
                quantity=quantity,
                price=price,
                cost_basis=eff_cost,
                timestamp=eff_ts,
                simulated=simulated,
                realized_pnl=realized_pnl,
                disallowed_loss=disallowed_loss,
                regime=regime,
                trade_id=trade_id,
                env=env,
                posterior_alpha=alpha,
                posterior_beta=beta,
                kelly_fraction=kelly,
            )
        return tid or str(uuid.uuid4())

    # ------------------------------------------------------------------
    # Order submission
    # ------------------------------------------------------------------

    def place_limit_buy(
        self,
        symbol:      str,
        quantity:    Optional[int] = None,
        limit_price: float = 0.0,
        atr:         Optional[float] = None,
    ) -> str:
        """
        Submit a SINGLE Day LIMIT BUY order.

        Spec: POST /trader/v1/accounts/{hash}/orders → HTTP 201
              Extract Order ID from the ``Location`` response header.
              Price formatted to exactly 2 decimal places.
              Quantity must be a whole integer.

        If quantity is omitted or None, calculates position size dynamically via
        BayesianSizingGovernor using price, ATR, and account settled cash.

        Args:
            symbol:      Ticker string (e.g. "SOXL").
            quantity:    Whole number of shares to buy (optional if atr provided).
            limit_price: Limit price per share.
            atr:         Optional 14-period ATR for dynamic Bayesian sizing.

        Returns:
            Order ID string extracted from the Location header.

        Raises:
            AccountFirewallError: If firewall not initialised.
            requests.HTTPError:   On HTTP 400 (bad payload) or other errors.
        """
        self._require_firewall()
        px_float = float(limit_price)

        if quantity is None:
            eff_atr = float(atr) if atr is not None else 1.0
            verdict = self.calculate_position_size(symbol, px_float, eff_atr)
            qty = verdict.shares
            if qty < 1:
                raise ValueError(
                    f"OrderManager: sizing governor returned 0 shares for {symbol} "
                    f"(reason: {verdict.bottleneck_reason})"
                )
            logger.info(
                "OrderManager: dynamic Bayesian sizing allocated %d shares ($%.2f) for %s [bottleneck=%s]",
                qty, verdict.notional_usd, symbol, verdict.bottleneck_reason,
            )
        else:
            qty = int(quantity)

        price = f"{px_float:.2f}"   # 2 decimal places — HTTP 400 prevention

        body = self._build_order(
            instruction=_INST_BUY,
            order_type=_OT_LIMIT,
            symbol=symbol,
            quantity=qty,
            price=price,
        )

        logger.info(
            "OrderManager: LIMIT BUY %s ×%d @ $%s | account=...%s",
            symbol, qty, price,
            self._account_number[-4:] if self._account_number else "?",
        )

        resp     = self._rest.place_order(self._account_hash, body, is_essential=False, action="LIMIT_BUY")
        order_id = self._extract_order_id(resp.headers.get("Location", ""))

        logger.info(
            "OrderManager: LIMIT BUY order placed — id=%s | %s ×%d @ $%s",
            order_id, symbol, qty, price,
        )
        if self.telemetry is not None:
            self.telemetry.record_event(
                aggregate_id=symbol.upper(),
                event_type="LIMIT_BUY",
                payload={
                    "symbol": symbol.upper(),
                    "quantity": qty,
                    "price": price,
                    "order_id": order_id,
                    "posterior_alpha": self.sizing_governor.alpha,
                    "posterior_beta": self.sizing_governor.beta,
                    "kelly_fraction": self.sizing_governor.kelly_fraction,
                    "category": "ORDER/FLOW",
                    "message": f"Limit buy placed for {symbol.upper()} x{qty} @ ${price} (f_qk={self.sizing_governor.kelly_fraction:.4f})",
                },
            )
        try:
            from core.event_ledger import publish_event
            from core.event_models import OrderRoutedEvent
            publish_event(
                OrderRoutedEvent(
                    client_order_id=order_id,
                    symbol=symbol.upper(),
                    action="BUY",
                    shares=qty,
                    limit_price=px_float,
                    notional=round(qty * px_float, 2),
                )
            )
        except Exception:
            pass
        return order_id

    def place_catastrophic_hardware_stop(
        self,
        symbol: str,
        quantity: int,
        s0_price: float,
        catastrophic_pct: float = 0.06,
    ) -> str:
        """
        Route only the catastrophic hardware floor ($S_0 \times 0.94$) to Schwab API
        as a contingent broker stop order.

        NOTE (#54): Submitting unconditional exchange-side STOP_MARKET orders at 09:30:00
        open is deprecated to eliminate opening cross whipsaw. Synthetic stops are managed
        in-memory via MicrostructureSentinel.
        """
        stop_price = float(s0_price) * (1.0 - float(catastrophic_pct))
        return self.place_broker_stop(symbol, quantity, stop_price)

    def place_broker_stop(
        self,
        symbol:     str,
        quantity:   int,
        stop_price: float,
    ) -> str:
        """
        Submit a Tier 2 catastrophe STOP SELL order.

        Spec (#54): Submitting unconditional exchange-side STOP_MARKET orders at 09:30:00
        open is DEPRECATED to eliminate opening cross whipsaw. Route only the catastrophic
        hardware floor (S0 * 0.94) to Schwab API as a contingent order.

        This is the broker-side safety net — it leaves shares unencumbered
        (no OCO bracket) so the client-side Tier 1 can execute market sells
        at any time without interference.

        Args:
            symbol:     Ticker string.
            quantity:   Whole shares to protect.
            stop_price: Trigger price (entry × 0.94 per #54 catastrophic floor).

        Returns:
            Order ID of the placed stop order.
        """
        self._require_firewall()
        qty   = int(quantity)
        price = f"{float(stop_price):.2f}"

        body = self._build_order(
            instruction=_INST_SELL,
            order_type=_OT_STOP,
            symbol=symbol,
            quantity=qty,
            stop_price=price,
        )

        logger.info(
            "OrderManager: TIER2 STOP SELL %s ×%d @ $%s (broker catastrophe stop)",
            symbol, qty, price,
        )

        resp     = self._rest.place_order(self._account_hash, body, is_essential=True, action="TIER2_STOP")
        order_id = self._extract_order_id(resp.headers.get("Location", ""))

        logger.info(
            "OrderManager: TIER2 STOP order placed — id=%s | %s ×%d @ $%s",
            order_id, symbol, qty, price,
        )
        self._tier2_stops[symbol.upper()] = {
            "order_id": order_id,
            "stop_price": float(stop_price),
            "quantity": qty,
            "last_update_time": time.monotonic(),
        }
        if self.telemetry is not None:
            self.telemetry.record_event(
                aggregate_id=symbol.upper(),
                event_type="TIER2_STOP_PLACED",
                payload={
                    "symbol": symbol.upper(),
                    "quantity": qty,
                    "stop_price": float(stop_price),
                    "order_id": order_id,
                    "category": "RISK",
                    "message": f"Tier-2 catastrophe stop placed for {symbol.upper()} x{qty} @ ${price} (id={order_id})",
                },
            )
        try:
            from core.event_ledger import publish_event
            from core.event_models import OrderRoutedEvent
            publish_event(
                OrderRoutedEvent(
                    client_order_id=order_id,
                    symbol=symbol.upper(),
                    action="SELL",
                    shares=qty,
                    limit_price=float(stop_price),
                    notional=round(qty * float(stop_price), 2),
                )
            )
        except Exception:
            pass
        return order_id

    def update_broker_stop(
        self,
        symbol: str,
        quantity: int,
        new_stop_price: float,
        current_order_id: Optional[str] = None,
        last_stop_price: Optional[float] = None,
    ) -> Tuple[Optional[str], bool]:
        """
        Throttle and execute cancel-replace for broker-side Tier-2 catastrophe stops:
        - Coalesces and throttles updates: dynamic Yang-Zhang trailing stops are tracked
          synthetically in memory (Tier 1), while Tier-2 broker stops only update when:
          1. Price movement delta >= 1.5% compared to the existing stop price.
          2. Cooldown >= 180 seconds has elapsed since the last cancel-replace.

        Args:
            symbol:           Ticker symbol.
            quantity:         Share quantity.
            new_stop_price:   New target catastrophe stop price.
            current_order_id: Existing order ID to cancel if replace is approved.
            last_stop_price:  Previous stop price (if known; falls back to cached).

        Returns:
            (order_id, replaced): Tuple of the effective order ID (new or existing)
            and a boolean indicating whether a cancel-replace was executed.
        """
        self._require_firewall()
        sym = symbol.upper()
        now = time.monotonic()

        existing = self._tier2_stops.get(sym, {})
        oid = current_order_id or existing.get("order_id")
        prev_price = last_stop_price if last_stop_price is not None else existing.get("stop_price")
        last_time = existing.get("last_update_time", 0.0)

        # 1. Check cooldown (180s)
        elapsed = now - last_time
        if elapsed < TIER2_COOLDOWN_SEC:
            remaining = TIER2_COOLDOWN_SEC - elapsed
            logger.info(
                "OrderManager: Tier-2 stop update for %s throttled by 180s cooldown "
                "(%.1fs remaining). Resting stop remains unchanged.",
                sym, remaining,
            )
            return oid, False

        # 2. Check minimum price movement threshold (>= 1.5% delta)
        if prev_price is not None and prev_price > 0:
            delta_pct = abs(float(new_stop_price) - float(prev_price)) / float(prev_price)
            if delta_pct < TIER2_MIN_DELTA_PCT:
                logger.info(
                    "OrderManager: Tier-2 stop update for %s skipped: delta %.2f%% < %.2f%% threshold. "
                    "Resting stop remains unchanged.",
                    sym, delta_pct * 100, TIER2_MIN_DELTA_PCT * 100,
                )
                return oid, False

        # 3. Both conditions satisfied: execute cancel-replace
        if oid:
            try:
                self.cancel_order(oid)
            except Exception as exc:
                logger.warning("OrderManager: could not cancel old Tier-2 stop %s during replace: %s", oid, exc)

        new_id = self.place_broker_stop(sym, quantity, new_stop_price)
        logger.info(
            "OrderManager: Tier-2 stop cancel-replace executed for %s — old=%s, new=%s @ $%.2f",
            sym, oid, new_id, new_stop_price,
        )
        if self.telemetry is not None:
            self.telemetry.record_event(
                aggregate_id=sym,
                event_type="TIER2_STOP_REPLACED",
                payload={
                    "symbol": sym,
                    "quantity": quantity,
                    "old_order_id": oid,
                    "new_order_id": new_id,
                    "new_stop_price": float(new_stop_price),
                    "category": "RISK",
                    "message": f"Tier-2 catastrophe stop cancel-replace executed for {sym} — old={oid}, new={new_id} @ ${float(new_stop_price):.2f}",
                },
            )
        try:
            from core.event_ledger import publish_event
            from core.event_models import SentinelRatchetEvent
            publish_event(
                SentinelRatchetEvent(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    symbol=sym,
                    high_watermark=float(new_stop_price),
                    dynamic_trail_price=float(new_stop_price),
                    floor_price=float(new_stop_price),
                )
            )
        except Exception:
            pass
        return new_id, True

    replace_broker_stop = update_broker_stop

    @staticmethod
    def is_essential(reason: str) -> bool:
        """Determines if an exit order is essential and bypasses daily quota limits."""
        return reason in (
            "MANDATORY_FLATTEN", "EMERGENCY", "API_EMERGENCY", "TIER1_STOP", "TIER2_STOP",
            "RECOVERY_FLOOR", "RECOVERY_TRAIL", "RECOVERY_TARGET", "RECOVERY_EOD",
            "EOD_FLAT", "IPC_EMERGENCY_LIQUIDATE",
            "EMERGENCY_HARD_SWEEP", "LIMIT_PEGGED", "SOFT_FLOOR_BREACH", "SUSTAINED_SOFT_BREACH"
        )

    def liquidate_optimal(
        self,
        symbol: str,
        total_shares: int,
        urgency: str = "NORMAL",
        reason: Optional[str] = None,
    ) -> Any:
        """
        Executes optimal liquidation via AdaptiveExecutionRouter.
        Enforces GFV pre-trade checks and account firewall before routing child orders.
        """
        sym = symbol.upper()
        qty = int(total_shares)
        action_reason = reason or urgency

        # 1. Hard Pre-Trade GFV Check
        if self._ledger is not None and hasattr(self._ledger, "can_sell_position"):
            can_sell, veto_msg = self._ledger.can_sell_position(sym, qty)
            if not can_sell:
                logger.critical(
                    "OrderManager: GFV EXECUTION VETO — Refusing to sell %s x%d [%s]. %s (Forced Overnight Hold).",
                    sym, qty, action_reason, veto_msg,
                )
                if self.telemetry is not None:
                    self.telemetry.record_event(
                        aggregate_id=sym,
                        event_type="GFV_SELL_BLOCKED",
                        payload={
                            "symbol": sym,
                            "quantity": qty,
                            "reason": action_reason,
                            "veto_reason": veto_msg,
                            "action": "FORCED_OVERNIGHT_HOLD",
                            "category": "GFV_PROTECTION",
                            "message": f"Sell order for {sym} blocked to prevent Good Faith Violation: {veto_msg}",
                        },
                    )
                raise GoodFaithViolationBlockedError(veto_msg)

        self._require_firewall()
        return self.router.liquidate_optimal(symbol=sym, total_shares=qty, urgency=urgency)

    def execute_terminal_flush(self, symbol: str, quantity: int) -> Any:
        """
        15:50:00 EDT Terminal Cash Flush.
        Triggers AdaptiveExecutionRouter with urgency='TERMINAL_FLUSH',
        allowing 60 seconds of adaptive limit-pegging before falling back to IOC if unfilled by 15:52:00 EDT.
        """
        return self.liquidate_optimal(symbol=symbol, total_shares=quantity, urgency="TERMINAL_FLUSH", reason="TERMINAL_FLUSH")

    def execute_market_sell(
        self,
        symbol:   str,
        quantity: int,
        reason:   str = "TIER1_STOP",
    ) -> str:
        """
        Execute an optimal liquidation sell order via AdaptiveExecutionRouter.
        Replaces direct unconstrained market order submissions to eliminate 3x ETF slippage.

        Used for:
            - Tier 1 trailing stop trigger (client-side).
            - EOD liquidation sweep (3:55 PM EDT).
            - Manual emergency exit.

        Hard Pre-Trade GFV Check:
        Verifies whether targeted shares were purchased with settled funds.
        If purchased with unsettled funds (and not yet settled), blocks the sell order
        and raises GoodFaithViolationBlockedError to force an overnight hold, overriding
        intraday EOD liquidation routines.
        """
        sym = symbol.upper()
        qty = int(quantity)

        # Map reason to urgency mode
        if reason in (
            "EMERGENCY_CATASTROPHIC", "CATASTROPHIC", "CIRCUIT_BREAKER",
            "EMERGENCY", "API_EMERGENCY", "IPC_EMERGENCY_LIQUIDATE", "EMERGENCY_HARD_SWEEP"
        ):
            urgency = "EMERGENCY_CATASTROPHIC"
        elif reason in ("EOD_FLAT", "MANDATORY_FLATTEN", "TERMINAL_FLUSH", "RECOVERY_EOD"):
            urgency = "TERMINAL_FLUSH"
        else:
            urgency = "NORMAL"

        logger.info(
            "OrderManager: OPTIMAL LIQUIDATION SELL %s ×%d [reason=%s, urgency=%s]",
            sym, qty, reason, urgency,
        )

        summary = self.liquidate_optimal(symbol=sym, total_shares=qty, urgency=urgency, reason=reason)
        order_id = summary.primary_order_id or (summary.order_ids[-1] if summary.order_ids else "OPTIMAL_FILL")

        logger.info(
            "OrderManager: OPTIMAL SELL order executed — id=%s | %s ×%d @ $%.2f (slippage: %.1fbps)",
            order_id, sym, summary.filled_shares, summary.average_fill_price, summary.slippage_bps,
        )

        if self.telemetry is not None:
            self.telemetry.record_event(
                aggregate_id=symbol.upper(),
                event_type="MARKET_SELL",
                payload={
                    "symbol": symbol.upper(),
                    "quantity": qty,
                    "reason": reason,
                    "urgency": urgency,
                    "order_id": order_id,
                    "avg_fill_price": summary.average_fill_price,
                    "slippage_bps": summary.slippage_bps,
                    "execution_duration": summary.execution_duration,
                    "category": "ORDER/FLOW",
                    "message": f"Market sell executed via AdaptiveRouter for {symbol.upper()} x{qty} [reason={reason}, avg_px=${summary.average_fill_price:.2f}, slippage={summary.slippage_bps:.1f}bps]",
                },
            )
        try:
            from core.event_ledger import publish_event
            from core.event_models import OrderRoutedEvent
            publish_event(
                OrderRoutedEvent(
                    client_order_id=order_id,
                    symbol=sym,
                    action="SELL",
                    shares=qty,
                    limit_price=summary.average_fill_price,
                    notional=summary.filled_shares * summary.average_fill_price,
                )
            )
        except Exception:
            pass
        return order_id

    def execute_marketable_limit_sell(
        self,
        symbol: str,
        quantity: int,
        limit_price: float,
        reason: str = "RECOVERY_TARGET",
    ) -> str:
        """
        Submits a marketable LIMIT SELL order (DAY, NORMAL).
        Used for non-urgent target exits to control slippage.
        """
        sym = symbol.upper()
        qty = int(quantity)

        # 1. Hard Pre-Trade GFV Check
        if self._ledger is not None and hasattr(self._ledger, "can_sell_position"):
            can_sell, veto_msg = self._ledger.can_sell_position(sym, qty)
            if not can_sell:
                logger.critical(
                    "OrderManager: GFV EXECUTION VETO — Refusing limit sell %s x%d [%s]. %s",
                    sym, qty, reason, veto_msg,
                )
                raise GoodFaithViolationBlockedError(veto_msg)

        self._require_firewall()

        px_str = f"{limit_price:.2f}"
        body = self._build_order(
            instruction=_INST_SELL,
            order_type=_OT_LIMIT,
            symbol=sym,
            quantity=qty,
            price=px_str,
        )

        is_essential = self.is_essential(reason)
        action = reason if is_essential else "LIMIT_SELL"
        resp = self._rest.place_order(self._account_hash, body, is_essential=is_essential, action=action)
        order_id = self._extract_order_id(resp.headers.get("Location", ""))

        logger.info(
            "OrderManager: LIMIT SELL order placed — id=%s | %s ×%d @ $%s [reason=%s]",
            order_id, sym, qty, px_str, reason,
        )
        if self.telemetry is not None:
            self.telemetry.record_event(
                aggregate_id=sym,
                event_type="LIMIT_SELL",
                payload={
                    "symbol": sym,
                    "quantity": qty,
                    "price": px_str,
                    "reason": reason,
                    "order_id": order_id,
                    "category": "ORDER/FLOW",
                    "message": f"Marketable limit sell placed for {sym} x{qty} @ ${px_str} [reason={reason}]",
                },
            )
        try:
            from core.event_ledger import publish_event
            from core.event_models import OrderRoutedEvent
            publish_event(
                OrderRoutedEvent(
                    client_order_id=order_id,
                    symbol=sym,
                    action="SELL",
                    shares=qty,
                    limit_price=px_val,
                    notional=round(qty * px_val, 2),
                )
            )
        except Exception:
            pass
        return order_id

    def find_working_sell_stop(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Finds any active WORKING SELL STOP order for symbol.
        Used for idempotent broker stop verification.
        """
        self._require_firewall()
        sym = symbol.upper()
        try:
            working = self._rest.get_orders(self._account_hash, status="WORKING")
        except Exception as exc:
            logger.warning("OrderManager: failed to fetch working orders to find stop for %s: %s", sym, exc)
            return None

        for o in working:
            if str(o.get("orderType", "")).upper() == "STOP":
                legs = o.get("orderLegCollection", [])
                for leg in legs:
                    leg_sym = leg.get("instrument", {}).get("symbol", "").upper()
                    leg_inst = str(leg.get("instruction", "")).upper()
                    if leg_sym == sym and leg_inst == "SELL":
                        return o
        return None

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    def cancel_order(self, order_id: str) -> bool:
        """
        Cancel a single working order by ID.

        Args:
            order_id: The Schwab order ID string.

        Returns:
            True if cancelled, False if already gone (404).
        """
        self._require_firewall()
        result = self._rest.cancel_order(self._account_hash, order_id)
        if result:
            logger.info("OrderManager: cancelled order %s.", order_id)
        return result

    def cancel_all_open_orders(self) -> int:
        """
        Cancel every WORKING order on the whitelisted account.

        Called at:
            - 3:55 PM EDT EOD liquidation sweep.
            - Emergency shutdown.

        Returns:
            Count of orders successfully cancelled.
        """
        self._require_firewall()
        try:
            working_orders = self._rest.get_orders(
                self._account_hash, status="WORKING"
            )
        except Exception as exc:
            logger.error(
                "OrderManager: cancel_all_open_orders — failed to fetch "
                "working orders: %s", exc,
            )
            return 0

        if not working_orders:
            logger.info("OrderManager: cancel_all_open_orders — no working orders found.")
            return 0

        cancelled = 0
        for order in working_orders:
            oid = str(order.get("orderId", ""))
            if not oid:
                continue
            try:
                if self.cancel_order(oid):
                    cancelled += 1
            except Exception as exc:
                logger.error(
                    "OrderManager: failed to cancel order %s: %s", oid, exc
                )

        logger.info(
            "OrderManager: cancel_all_open_orders — cancelled %d/%d orders.",
            cancelled, len(working_orders),
        )
        return cancelled

    def get_working_orders(self) -> List[Dict[str, Any]]:
        """Return all WORKING orders for the whitelisted account."""
        self._require_firewall()
        return self._rest.get_orders(self._account_hash, status="WORKING")

    def get_order_status(self, order_id: str) -> Dict[str, Any]:
        """Fetch the current state of a specific order."""
        self._require_firewall()
        return self._rest.get_order(self._account_hash, order_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_order(
        self,
        instruction: str,
        order_type:  str,
        symbol:      str,
        quantity:    int,
        price:       Optional[str] = None,      # Limit price (str, 2 dp)
        stop_price:  Optional[str] = None,      # Stop trigger (str, 2 dp)
    ) -> Dict[str, Any]:
        """
        Construct the Schwab order request body dict.

        Enforces:
        - ``orderStrategyType: SINGLE``     (no complex multi-leg orders)
        - ``duration: DAY``                 (all orders expire at close)
        - ``session: NORMAL``               (regular market hours only)
        - ``quantity``: integer only        (no fractional shares)
        - ``price`` / ``stopPrice``: str    (exactly 2 decimal places)

        A MARKET order must NOT include a ``price`` field (HTTP 400 if present).
        """
        order: Dict[str, Any] = {
            "orderType":         order_type,
            "session":           _SESSION_NORMAL,
            "duration":          _DURATION_DAY,
            "orderStrategyType": _ORDER_STRATEGY_TYPE,
            "orderLegCollection": [
                {
                    "orderLegType": _ORDER_LEG_EQUITY,
                    "legId":        1,
                    "instrument": {
                        "symbol":    symbol.upper(),
                        "assetType": _ASSET_TYPE_EQUITY,
                    },
                    "instruction": instruction,
                    "quantity":    quantity,     # whole integer
                }
            ],
        }

        if price is not None:
            order["price"] = price           # e.g. "25.50"

        if stop_price is not None:
            order["stopPrice"] = stop_price  # e.g. "24.00"

        return order

    def _extract_order_id(self, location_header: str) -> str:
        """
        Parse the Order ID from the Schwab ``Location`` response header.

        Location format:
            https://api.schwabapi.com/trader/v1/accounts/{hash}/orders/{orderId}

        Returns:
            The orderId string (trailing URL segment).

        Raises:
            ValueError: If the header is missing or malformed.
        """
        if not location_header:
            raise ValueError(
                "OrderManager: place_order response missing Location header — "
                "cannot determine order ID."
            )
        order_id = location_header.rstrip("/").split("/")[-1]
        if not order_id:
            raise ValueError(
                f"OrderManager: could not parse order ID from Location header: "
                f"'{location_header}'"
            )
        return order_id

    def _require_firewall(self) -> None:
        """Raise AccountFirewallError if initialize_firewall() has not been called."""
        if not self._firewall_ok or self._account_hash is None:
            raise AccountFirewallError(
                "OrderManager: initialize_firewall() must be called before "
                "submitting any orders."
            )

    def record_execution_fill(
        self,
        order_id: str,
        symbol: str,
        fill_price: float,
        slippage_usd: float = 0.0,
        fee_usd: float = 0.0,
    ) -> None:
        """
        Record an order fill execution into the zero-allocation event ledger (< 5us).
        """
        try:
            from core.event_ledger import publish_event
            from core.event_models import ExecutionFilledEvent
            publish_event(
                ExecutionFilledEvent(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    order_id=str(order_id),
                    symbol=str(symbol).upper(),
                    fill_price=float(fill_price),
                    slippage_usd=float(slippage_usd),
                    fee_usd=float(fee_usd),
                )
            )
        except Exception:
            pass

    def record_settlement_update(
        self,
        settled_cash: float,
        unsettled_cash: float,
        total_nlv: float,
        gfv_count: int = 0,
    ) -> None:
        """
        Record cash settlement and NLV state update into the event ledger (< 5us).
        """
        try:
            from core.event_ledger import publish_event
            from core.event_models import SettlementUpdatedEvent
            publish_event(
                SettlementUpdatedEvent(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    settled_cash=float(settled_cash),
                    unsettled_cash=float(unsettled_cash),
                    total_nlv=float(total_nlv),
                    gfv_count=int(gfv_count),
                )
            )
        except Exception:
            pass

