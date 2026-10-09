import logging
from dataclasses import replace
from typing import List, Dict, Optional, Any
from core.dynamic_scanner import DynamicScanner
from execution.strategies import TradeSignal

logger = logging.getLogger(__name__)

class UniverseManager:
    def __init__(self, scanner: DynamicScanner, divergence_engine: Optional[Any] = None):
        self.scanner = scanner
        self.active_universe: List[str] = []
        self.ring_buffers: Dict[str, List[Dict[str, Any]]] = {}  # 390-bar buffers
        self.max_buffer_size = 390
        self.divergence_engine = divergence_engine

    def set_divergence_engine(self, engine: Any) -> None:
        """Register the SyntheticDivergenceEngine instance."""
        self.divergence_engine = engine

    def is_trap_active(self, symbol: str, current_time: Optional[Any] = None) -> bool:
        """Query if a liquidity trap is active for the symbol's synthetic pair."""
        if self.divergence_engine is None:
            return False
        return self.divergence_engine.is_trap_active(symbol, current_time=current_time)

    def is_entry_allowed(self, symbol: str, strategy: str = "15m_ORB", current_time: Optional[Any] = None) -> bool:
        """
        Check if an entry is allowed for the symbol, suppressing if an active trap is detected.
        Logs LIQUIDITY_TRAP_SUPPRESSION event to telemetry.db on suppression and keeps engine in cash.
        """
        if self.is_trap_active(symbol, current_time=current_time):
            logger.warning(
                "UniverseManager: entry for %s suppressed by synthetic divergence liquidity trap (%s).",
                symbol, strategy
            )
            self._log_trap_suppression(symbol, strategy)
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
