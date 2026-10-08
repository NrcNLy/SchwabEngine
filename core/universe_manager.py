import logging
from dataclasses import replace
from typing import List, Dict, Optional, Any
from core.dynamic_scanner import DynamicScanner
from execution.strategies import TradeSignal

logger = logging.getLogger(__name__)

class UniverseManager:
    def __init__(self, scanner: DynamicScanner):
        self.scanner = scanner
        self.active_universe: List[str] = []
        self.ring_buffers: Dict[str, List[Dict[str, Any]]] = {}  # 390-bar buffers
        self.max_buffer_size = 390

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
