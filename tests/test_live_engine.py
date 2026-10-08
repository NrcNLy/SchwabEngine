"""LiveEngine entry / exit / flatten behaviour against a fake broker (no network)."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import time as dtime
from decimal import Decimal
from typing import Any, Dict, List, Optional

import pytest

from core.engine import EngineSettings, LiveEngine
from core.ledger import BrokerBalances, SettlementLedger
from core.runtime import EngineContext

D = Decimal


@dataclass(frozen=True)
class Regime:
    value: str


@dataclass
class Signal:
    symbol: str
    entry_price: Decimal
    quantity: int
    stop_price: Decimal
    target_price: Decimal
    regime: Regime = Regime("A")


class FakeStrategy:
    def set_signal_callback(self, cb) -> None:
        self.cb = cb

    def on_tick(self, symbol: str, fields: Dict[str, Any]) -> None:
        return None


class FakeSync:
    poll_interval = 30.0

    def seconds_since_ok(self) -> Optional[float]:
        return 1.0


class FakeOrderManager:
    """Scripted broker. ``fills`` maps order id -> status payload returned by get_order_status."""

    def __init__(self) -> None:
        self.calls: List[tuple] = []
        self.next_id = 100
        self.entry_status: Dict[str, Any] = {}
        self.sell_status: Dict[str, Any] = {}
        self.fail_sell = False
        self.statuses: Dict[str, Dict[str, Any]] = {}

    def _oid(self) -> str:
        self.next_id += 1
        return str(self.next_id)

    def place_limit_buy(self, symbol: str, qty: int, price: float) -> str:
        oid = self._oid()
        self.calls.append(("buy", symbol, qty, price))
        self.statuses[oid] = dict(self.entry_status)
        return oid

    def place_broker_stop(self, symbol: str, qty: int, stop: float) -> str:
        oid = self._oid()
        self.calls.append(("stop", symbol, qty, round(stop, 2)))
        return oid

    def execute_market_sell(self, symbol: str, qty: int, reason: str) -> str:
        if self.fail_sell:
            raise RuntimeError("broker rejected")
        oid = self._oid()
        self.calls.append(("sell", symbol, qty, reason))
        self.statuses[oid] = dict(self.sell_status)
        return oid

    def cancel_order(self, order_id: str) -> bool:
        self.calls.append(("cancel", order_id))
        return True

    def cancel_all_open_orders(self) -> None:
        raise AssertionError("must never cancel the user's manual orders")

    def get_order_status(self, order_id: str) -> Dict[str, Any]:
        return self.statuses.get(order_id, {"status": "WORKING"})

    def kinds(self, kind: str) -> List[tuple]:
        return [c for c in self.calls if c[0] == kind]


def filled(qty: int, price: str) -> Dict[str, Any]:
    return {
        "status": "FILLED",
        "filledQuantity": qty,
        "orderActivityCollection": [{"executionLegs": [{"quantity": qty, "price": price}]}],
    }


def make_engine(loop: asyncio.AbstractEventLoop, nlv="1000", settled="1000"):
    ctx = EngineContext({}, live=True)
    ledger = SettlementLedger(D("0"), data_source="LIVE_SCHWAB")
    ledger.sync_from_broker(BrokerBalances(liquidation_value=D(nlv), settled_cash=D(settled)))
    ctx.ledgers["active"] = ledger
    ctx.broker_sync = FakeSync()
    om = FakeOrderManager()
    settings = EngineSettings(
        symbols=["SOXL"], eod=dtime(15, 50), flat_deadline=dtime(15, 55),
        tier2_stop_pct=D("0.04"), fill_timeout_s=0.2, fill_poll_s=0.01,
    )
    engine = LiveEngine(ctx, {}, ledger, settings, om, FakeStrategy(), loop)
    engine.ignore_session = True  # wall clock is irrelevant to these tests
    return engine, ctx, ledger, om


def sig(symbol="SOXL", entry="100", qty=50) -> Signal:
    return Signal(symbol=symbol, entry_price=D(entry), quantity=qty,
                  stop_price=D("95"), target_price=D("110"))


def run(coro):
    return asyncio.run(coro)


def test_entry_is_capped_by_buying_power_and_arms_a_broker_stop():
    async def scenario():
        engine, _ctx, ledger, om = make_engine(asyncio.get_running_loop())
        om.entry_status = filled(3, "100.50")
        assert await engine.handle_signal(sig(qty=50)) is True
        return ledger, om

    ledger, om = run(scenario())
    # strategy asked for 50 sh ($5,000); 33% of $1,000 NLV caps the order at 3 sh @ $100
    assert om.kinds("buy") == [("buy", "SOXL", 3, 100.0)]
    pos = ledger.positions["SOXL"]
    assert pos.quantity == 3 and pos.entry_price == D("100.50") and pos.simulated is False
    assert om.kinds("stop") == [("stop", "SOXL", 3, 96.48)]  # 100.50 * (1 - 0.04)
    assert ledger.settled == D("698.50")


def test_unfilled_entry_is_cancelled_and_reservation_released():
    async def scenario():
        engine, _ctx, ledger, om = make_engine(asyncio.get_running_loop())
        om.entry_status = {"status": "WORKING", "filledQuantity": 0}
        assert await engine.handle_signal(sig()) is False
        return ledger, om

    ledger, om = run(scenario())
    assert len(om.kinds("cancel")) >= 1
    assert "SOXL" not in ledger.positions
    assert ledger.settled == D("1000.00")


@pytest.mark.parametrize("setup,expected", [
    ("halted", "kill switch"),
    ("open", "position already open"),
])
def test_entry_guards(setup, expected):
    async def scenario():
        engine, ctx, ledger, om = make_engine(asyncio.get_running_loop())
        if setup == "halted":
            ctx.is_halted = True
        else:
            ledger.confirm_buy("SOXL", 1, "100", simulated=False)
        return engine.entry_block_reason("SOXL"), await engine.handle_signal(sig()), om

    reason, handled, om = run(scenario())
    assert expected in reason
    assert handled is False and om.kinds("buy") == []


def test_entry_blocked_until_ledger_syncs_with_broker():
    async def scenario():
        engine, ctx, _ledger, om = make_engine(asyncio.get_running_loop())
        fresh = SettlementLedger(D("1000"), data_source="LIVE_SCHWAB")  # holds cash but never synced
        engine.ledger = fresh
        ctx.ledgers["active"] = fresh
        return engine.entry_block_reason("SOXL"), await engine.handle_signal(sig()), om

    reason, handled, om = run(scenario())
    assert "synced" in reason and handled is False and om.kinds("buy") == []


def test_exit_cancels_stop_sells_at_market_and_books_unsettled_proceeds():
    async def scenario():
        engine, _ctx, ledger, om = make_engine(asyncio.get_running_loop())
        om.entry_status = filled(2, "100.00")
        await engine.handle_signal(sig())
        om.sell_status = filled(2, "104.00")
        ok = await engine.exit_position("SOXL", "TARGET")
        return ok, ledger, om

    ok, ledger, om = run(scenario())
    assert ok is True
    assert om.kinds("sell") == [("sell", "SOXL", 2, "TARGET")]
    assert len(om.kinds("cancel")) == 1  # the broker stop
    assert "SOXL" not in ledger.positions
    assert ledger.unsettled_total == D("208.00")  # proceeds are Hard Reserve until T+1
    assert ledger.realized_pnl_today() == D("8.00")


def test_failed_market_sell_rearms_the_broker_stop():
    async def scenario():
        engine, _ctx, ledger, om = make_engine(asyncio.get_running_loop())
        om.entry_status = filled(2, "100.00")
        await engine.handle_signal(sig())
        om.fail_sell = True
        ok = await engine.exit_position("SOXL", "TIER1_STOP")
        return ok, ledger, om

    ok, ledger, om = run(scenario())
    assert ok is False
    assert "SOXL" in ledger.positions
    assert len(om.kinds("stop")) == 2  # original + re-armed


def test_flatten_cancels_only_engine_orders_and_closes_positions():
    async def scenario():
        engine, _ctx, ledger, om = make_engine(asyncio.get_running_loop())
        om.entry_status = filled(2, "100.00")
        await engine.handle_signal(sig())
        om.sell_status = filled(2, "99.00")
        count = await engine.flatten_all("EOD_FLAT")
        return count, ledger, om

    count, ledger, om = run(scenario())  # FakeOrderManager.cancel_all_open_orders would raise
    assert count == 1
    assert ledger.positions == {}
    assert len(om.kinds("sell")) == 1


def test_duplicate_exit_requests_are_ignored():
    async def scenario():
        engine, _ctx, _ledger, om = make_engine(asyncio.get_running_loop())
        om.entry_status = filled(2, "100.00")
        await engine.handle_signal(sig())
        om.sell_status = filled(2, "101.00")
        results = await asyncio.gather(engine.exit_position("SOXL", "A"), engine.exit_position("SOXL", "B"))
        return results, om

    results, om = run(scenario())
    assert sorted(results) == [False, True]
    assert len(om.kinds("sell")) == 1
