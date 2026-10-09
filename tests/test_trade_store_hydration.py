from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import pytz
import pytest
from fastapi.testclient import TestClient

from api.server import build_app
from core.ledger import SettlementLedger
from core.runtime import EngineContext, load_config
from core.trade_store import SQLiteTradeStore

_EDT = pytz.timezone("America/New_York")


def test_sqlite_trade_store_wal_and_hydration_across_restart(tmp_path):
    db_file = tmp_path / "trades_test.db"
    store = SQLiteTradeStore(db_path=db_file)

    now = datetime.now(_EDT)

    # Ingest morning executions:
    # 1. SOXL round trip: buy 10 @ 50 ($500), sell 10 @ 50.50 ($505) -> P&L +$5.00
    store.record_trade("SOXL", "BUY", 10, Decimal("50.00"), Decimal("500.00"), now, False, realized_pnl=Decimal("0.00"))
    store.record_trade("SOXL", "SELL", 10, Decimal("50.50"), Decimal("505.00"), now, False, realized_pnl=Decimal("5.00"))

    # 2. TNA round trip 1: buy 20 @ 30 ($600), sell 20 @ 30.25 ($605) -> P&L +$5.00
    store.record_trade("TNA", "BUY", 20, Decimal("30.00"), Decimal("600.00"), now, False, realized_pnl=Decimal("0.00"))
    store.record_trade("TNA", "SELL", 20, Decimal("30.25"), Decimal("605.00"), now, False, realized_pnl=Decimal("5.00"))

    # 3. TNA round trip 2: buy 20 @ 30 ($600), sell 20 @ 30.285 ($605.71) -> P&L +$5.71
    store.record_trade("TNA", "BUY", 20, Decimal("30.00"), Decimal("600.00"), now, False, realized_pnl=Decimal("0.00"))
    store.record_trade("TNA", "SELL", 20, Decimal("30.2855"), Decimal("605.71"), now, False, realized_pnl=Decimal("5.71"))

    # Simulate server reboot: create a new SettlementLedger with the same persistent store
    ledger = SettlementLedger(baseline_settled=Decimal("3772.10"), trade_store=store, env="active")

    # Assert morning trades are hydrated
    assert len(ledger.get_trade_log()) == 6
    assert ledger.realized_pnl_today() == Decimal("15.71")

    # Ingest afternoon trade: SCO round trip (buy @ 1280, sell @ 1282.64 -> P&L +$2.64)
    ledger.allocate_capital("SCO", Decimal("1280.00"))
    ledger.confirm_buy("SCO", 10, Decimal("128.00"), simulated=False)
    ledger.record_sell("SCO", 10, Decimal("128.264"), simulated=False, execution_payload={
        "status": "FILLED",
        "filledQuantity": 10,
        "orderActivityCollection": [{"executionLegs": [{"legId": 1, "quantity": 10, "price": 128.264}]}],
    })

    # Assert total Today P&L reflects the full +$18.35 rather than just +$2.64
    assert ledger.realized_pnl_today() == Decimal("18.35")
    assert len(ledger.get_trade_log()) == 8


def test_api_orders_today_endpoint(tmp_path):
    db_file = tmp_path / "trades_api.db"
    store = SQLiteTradeStore(db_path=db_file)

    now = datetime.now(_EDT)
    store.record_trade("SOXL", "BUY", 10, Decimal("50.00"), Decimal("500.00"), now, False, env="active")
    store.record_trade("SOXL", "SELL", 10, Decimal("50.50"), Decimal("505.00"), now, False, realized_pnl=Decimal("5.00"), env="active")

    cfg = load_config()
    ctx = EngineContext(cfg, live=True)
    ctx.ledgers["active"] = SettlementLedger(
        baseline_settled=Decimal("3772.10"), trade_store=store, env="active"
    )

    client = TestClient(build_app(ctx))

    res = client.get("/api/orders/today?env=active")
    assert res.status_code == 200
    orders = res.json()
    assert len(orders) == 2
    assert orders[0]["symbol"] == "SOXL"
    assert orders[0]["side"] == "BUY"
    assert orders[1]["symbol"] == "SOXL"
    assert orders[1]["side"] == "SELL"
    assert orders[1]["realized_pnl"] == 5.0
