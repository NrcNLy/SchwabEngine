"""
main.py
=======
Schwab Automated Day-Trading Engine â€” Event-Driven Architecture (EDA).

Refactored to completely eliminate synchronous blocking.
Leverages Python's asyncio to decouple Event Producers (data streams) from
Event Consumers (Risk Engine, Execution Router) via a high-throughput memory bus.
"""

import asyncio
import logging
import sys
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.models import MarketEvent
from core.liquidity_models import (
    MacroLiquidityEvent, 
    CollateralInvariantState, 
    CreditReportSnapshot, 
    PromotionalDebt
)
from core.ledger import SettlementLedger
from execution.risk_manager import RiskEngine
from execution.router import ExecutionRouter
from governor import GovernorDaemon

from core.streamer import SchwabStreamer
from execution.order_client import SchwabOrderClient
from datetime import datetime
import json
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
logger = logging.getLogger("main_eda")

class EventBus:
    def __init__(self):
        self._queue = asyncio.Queue()
        self._subscribers = []

    def subscribe(self, callback):
        self._subscribers.append(callback)

    async def publish(self, event: Any):
        await self._queue.put(event)

    async def _dispatch_loop(self):
        while True:
            event = await self._queue.get()
            for callback in self._subscribers:
                # Dispatch concurrently to all consumers without blocking
                asyncio.create_task(callback(event))
            self._queue.task_done()

class TradingSystem:
    def __init__(self, bus: EventBus, order_client: SchwabOrderClient = None):
        self.bus = bus
        self.ledger = SettlementLedger()
        self.risk_engine = RiskEngine()
        self.router = ExecutionRouter(order_client=order_client)
        self.governor = GovernorDaemon()

        # Wire up the consumer to the event bus
        self.bus.subscribe(self.route_event)

    async def route_event(self, event: Any):
        """Dispatches typed events to specialized handlers."""
        if isinstance(event, MarketEvent):
            await self.handle_market_event(event)
        elif isinstance(event, MacroLiquidityEvent):
            await self.handle_liquidity_event(event)

    async def handle_liquidity_event(self, event: MacroLiquidityEvent):
        """Logs passive macro liquidity updates without affecting order sizing."""
        logger.info(
            f"MacroLiquidityEvent Processed -> Net Collateral Buffer: ${event.state.net_collateral_buffer:.2f}, "
            f"Solvent: {event.state.is_solvent}"
        )

    async def handle_market_event(self, event: MarketEvent):
        """Asynchronous Consumer: Processes the Pydantic strictly typed event."""
        logger.info(f"Consumer Received Event -> {event.ticker} @ ${event.price:.2f} (ATR: {event.atr})")
        
        # 1. Check if we should trigger a macro ingestion (mocking condition)
        if event.choppiness_index > 60:
            logger.warning(f"High choppiness detected ({event.choppiness_index:.1f}). Dispatching async AI governor...")
            # This does not block the event loop!
            asyncio.create_task(self.governor.pre_market_macro())

        # 2. Risk Management Sizing
        account_base = self.ledger.bucket1_settled + self.ledger.bucket2_unsettled
        target_shares = self.risk_engine.determine_position_size(event.ticker, event.atr, account_base)
        
        if target_shares <= 0:
            return

        # 3. Capital Allocation & GFV Temporal Locking
        notional_value = target_shares * event.price
        if self.ledger.allocate_capital(event.ticker, notional_value):
            
            # 4. Almgren-Chriss Execution
            await self.router.execute_almgren_chriss_trajectory(event.ticker, target_shares, side="BUY")

async def macro_liquidity_poller(bus: EventBus, poll_interval: float = 2.0):
    """Watches data/macro_liquidity.json for changes and publishes MacroLiquidityEvent."""
    state_file = Path("data/macro_liquidity.json")
    last_mtime = 0.0
    while True:
        try:
            if state_file.exists():
                mtime = state_file.stat().st_mtime
                if mtime > last_mtime:
                    last_mtime = mtime
                    data = json.loads(state_file.read_text())
                    if "collateral_state" in data:
                        state = CollateralInvariantState(**data["collateral_state"])
                        snapshot = CreditReportSnapshot(**data["credit_report"]) if data.get("credit_report") else None
                        promos = [PromotionalDebt(**d) for d in data.get("promotional_debts", [])]
                        evt = MacroLiquidityEvent(
                            timestamp=datetime.utcnow().isoformat() + "Z",
                            state=state,
                            snapshot=snapshot,
                            promotional_debts=promos
                        )
                        logger.info("Macro liquidity file update detected. Publishing MacroLiquidityEvent to bus.")
                        await bus.publish(evt)
        except Exception as e:
            logger.warning(f"Error in macro liquidity poller: {e}")
        await asyncio.sleep(poll_interval)

async def main_loop():
    logger.info("Booting SchwabEngine Event-Driven Architecture (EDA)...")
    
    bus = EventBus()
    
    # Initialize the core REST Execution client (Dry-Run by default for safety)
    order_client = SchwabOrderClient(auth_manager=None, live_trading=False)
    
    # Initialize the core system
    system = TradingSystem(bus, order_client=order_client)
    
    # Start the event dispatcher in the background
    dispatcher_task = asyncio.create_task(bus._dispatch_loop())
    
    # Start the macro liquidity poller in the background
    liquidity_task = asyncio.create_task(macro_liquidity_poller(bus))
    
    # Instantiate the Charles Schwab WebSocket Streamer
    # auth_manager can be injected here for actual OAuth fetching
    streamer = SchwabStreamer(bus, auth_manager=None)
    
    # Start the live market data stream producer
    producer_task = asyncio.create_task(streamer.listener_loop())
    
    # Wait for the producer to finish (or run indefinitely)
    await producer_task
    
    # Wait for the queue to drain
    await bus._queue.join()
    
    logger.info("Engine Event Loop Terminated Cleanly.")
    
    # Cleanup
    dispatcher_task.cancel()
    liquidity_task.cancel()

if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        logger.info("Manual Interrupt.")
