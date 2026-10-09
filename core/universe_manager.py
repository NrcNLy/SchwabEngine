import logging
from dataclasses import replace
from typing import List, Dict, Optional, Any, Set
from core.dynamic_scanner import DynamicScanner
from execution.strategies import TradeSignal

logger = logging.getLogger(__name__)

class UniverseManager:
    def __init__(
        self,
        scanner: Optional[DynamicScanner] = None,
        divergence_engine: Optional[Any] = None,
        exclusion_mask: Optional[Any] = None,
    ):
        self.scanner = scanner
        self.active_universe: List[str] = []
        self.ring_buffers: Dict[str, List[Dict[str, Any]]] = {}  # 390-bar buffers
        self.max_buffer_size = 390
        self.divergence_engine = divergence_engine
        self.exclusion_mask = exclusion_mask
        self.momentum_scores: Dict[str, float] = {}

    def set_exclusion_mask(self, mask: Any) -> None:
        """Register the UniverseExclusionMask instance."""
        self.exclusion_mask = mask

    def get_active_universe(self) -> List[str]:
        """Returns the current active universe symbols."""
        return list(self.active_universe)

    def set_momentum_score(self, symbol: str, score: float) -> None:
        """Record the Composite Momentum Score (S_i) for a symbol."""
        self.momentum_scores[symbol.upper()] = float(score)

    def get_momentum_score(self, symbol: str) -> float:
        """Get Composite Momentum Score (S_i) for a symbol, defaulting to 0.0."""
        return self.momentum_scores.get(symbol.upper(), 0.0)

    def rank_candidate_signals(self, signals: List[TradeSignal]) -> List[TradeSignal]:
        """
        Sort candidate trade signals by Composite Momentum Score (S_i) descending
        prior to checking settled cash availability, preventing secondary ETFs
        from starving primary assets.
        """
        return sorted(
            signals,
            key=lambda sig: self.get_momentum_score(sig.symbol),
            reverse=True,
        )

    def dispatch_candidate_signals(
        self,
        signals: List[TradeSignal],
        ledger: Any,
        execute_callback: Optional[Any] = None,
    ) -> List[TradeSignal]:
        """
        Ranks candidate signals by Composite Momentum Score (S_i) before checking
        settled cash and position sizing limits, ensuring higher-momentum primary
        assets are allocated buying power first.
        """
        ranked = self.rank_candidate_signals(signals)
        approved: List[TradeSignal] = []

        for sig in ranked:
            if not self.is_entry_allowed(sig.symbol, sig.strategy):
                continue

            # Check settled cash availability
            if hasattr(ledger, "settled_cash") and hasattr(ledger, "cash_buffer"):
                avail_cash = float(ledger.settled_cash) - float(ledger.cash_buffer)
                if avail_cash <= 0 or (sig.entry_price * sig.quantity) > avail_cash:
                    max_by_cash = int(avail_cash // float(sig.entry_price))
                    if max_by_cash <= 0:
                        logger.warning(
                            "UniverseManager: %s signal starved by cash allocation ($%.2f avail)",
                            sig.symbol, avail_cash
                        )
                        continue
                    sig = self.clamp_signal_size(sig, max_by_cash)

            if hasattr(ledger, "check_order_allowed"):
                allowed, reason = ledger.check_order_allowed(float(sig.entry_price * sig.quantity))
                if not allowed:
                    logger.warning("UniverseManager: %s signal disallowed by ledger: %s", sig.symbol, reason)
                    continue

            approved.append(sig)
            if execute_callback:
                execute_callback(sig)

        return approved

    def set_divergence_engine(self, engine: Any) -> None:
        """Register the SyntheticDivergenceEngine instance."""
        self.divergence_engine = engine

    def is_trap_active(self, symbol: str, current_time: Optional[Any] = None) -> bool:
        """Query if a liquidity trap is active for the symbol's synthetic pair."""
        if self.divergence_engine is None:
            return False
        return self.divergence_engine.is_trap_active(symbol, current_time=current_time)

    def audit_wash_sale_holdings(
        self,
        positions: Any,
        session_open: Optional[Any] = None,
    ) -> Set[str]:
        """
        Audit holdings for active external swing lots to prevent IRC §1091 wash-sale contamination.
        Does NOT query trades.db for closed losses from past sessions or past 30 days.
        """
        if self.exclusion_mask is not None and hasattr(self.exclusion_mask, "audit_holdings_for_wash_sale"):
            return self.exclusion_mask.audit_holdings_for_wash_sale(positions, session_open=session_open)
        return set()

    def is_entry_allowed(self, symbol: str, strategy: str = "15m_ORB", current_time: Optional[Any] = None) -> bool:
        """
        Check if an entry is allowed for the symbol, suppressing if an active trap is detected
        or if the symbol is in the exclusion mask (e.g. active external swing lot).
        Logs LIQUIDITY_TRAP_SUPPRESSION event to telemetry.db on trap suppression and keeps engine in cash.
        """
        sym = symbol.upper()
        if self.exclusion_mask is not None and hasattr(self.exclusion_mask, "is_symbol_tradeable"):
            if not self.exclusion_mask.is_symbol_tradeable(sym):
                logger.warning("UniverseManager: %s entry disallowed by exclusion mask (wash-sale/restricted).", sym)
                return False

        if self.is_trap_active(sym, current_time=current_time):
            logger.warning(
                "UniverseManager: entry for %s suppressed by synthetic divergence liquidity trap (%s).",
                sym, strategy
            )
            self._log_trap_suppression(sym, strategy)
            return False
        return True

    def _log_trap_suppression(self, symbol: str, strategy: str) -> None:
        try:
            from core.telemetry import SQLiteWALEventStore
            store = SQLiteWALEventStore()
            store.record_event(
                aggregate_id=symbol.upper(),
                event_type="LIQUIDITY_TRAP_SUPPRESSION",
                payload={
                    "symbol": symbol.upper(),
                    "strategy": strategy,
                    "reason": "LIQUIDITY_TRAP_ACTIVE",
                }
            )
        except Exception as exc:
            logger.warning("UniverseManager: failed to log trap suppression event: %s", exc)

    def update_universe(self, new_universe: List[str]) -> None:
        """Update active universe and initialize empty buffers for new symbols."""
        logger.info(f"Updating active universe from {self.active_universe} to {new_universe}")
        self.active_universe = new_universe
        for sym in new_universe:
            if sym not in self.ring_buffers:
                self.ring_buffers[sym] = []

    def append_bar(self, symbol: str, bar: Dict[str, Any]) -> None:
        """Append a 1m bar to the ring buffer for the given symbol."""
        if symbol not in self.ring_buffers:
            return
            
        self.ring_buffers[symbol].append(bar)
        if len(self.ring_buffers[symbol]) > self.max_buffer_size:
            self.ring_buffers[symbol] = self.ring_buffers[symbol][-self.max_buffer_size:]

    def get_buffer(self, symbol: str) -> List[Dict[str, Any]]:
        return self.ring_buffers.get(symbol, [])

    def clamp_signal_size(self, signal: TradeSignal, max_quantity: int) -> TradeSignal:
        """
        Safely clamps signal quantity using immutable replace 
        to prevent FrozenInstanceError.
        """
        if signal.quantity > max_quantity:
            logger.info(f"Clamping signal quantity from {signal.quantity} to {max_quantity}")
            return replace(signal, quantity=max_quantity)
        return signal
