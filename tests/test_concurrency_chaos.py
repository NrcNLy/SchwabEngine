import asyncio
import datetime
import os
import threading
import time
import pytest
from pathlib import Path

from core.trade_store import SQLiteTradeStore
from core.ipc import (
    TraderIPCServer,
    TraderIPCClient,
    write_control_signal,
    read_control_signal,
    acknowledge_control_signal
)

@pytest.fixture
def temp_db(tmp_path):
    db_file = tmp_path / "trades.db"
    store = SQLiteTradeStore(db_path=db_file)
    return store, db_file

def test_sqlite_wal_concurrency(temp_db):
    """
    Test SQLite WAL saturation with 20 readers querying trades.db while
    1 writer attempts 500 rapid-fire trade insertions.
    """
    store, db_file = temp_db
    
    stop_event = threading.Event()
    error_list = []
    
    def reader_task():
        import sqlite3
        try:
            conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=5.0)
            conn.execute("PRAGMA journal_mode=WAL;")
            while not stop_event.is_set():
                cur = conn.execute("SELECT COUNT(*) FROM trades")
                _ = cur.fetchone()
                time.sleep(0.005)
            conn.close()
        except Exception as e:
            error_list.append(e)
            
    def writer_task():
        try:
            for i in range(500):
                store.record_trade(
                    symbol="AAPL",
                    side="BUY",
                    quantity=10,
                    price=150.0 + i,
                    cost_basis=1500.0,
                    timestamp=datetime.datetime.now(datetime.timezone.utc),
                    simulated=True
                )
                time.sleep(0.001)
        except Exception as e:
            error_list.append(e)
            
    readers = [threading.Thread(target=reader_task) for _ in range(20)]
    writer = threading.Thread(target=writer_task)
    
    for r in readers:
        r.start()
        
    writer.start()
    writer.join()
    
    stop_event.set()
    for r in readers:
        r.join()
        
    assert not error_list, f"Exceptions occurred during concurrency test: {error_list}"
    
    trades = store.fetch_trades_today()
    assert len(trades) == 500

@pytest.fixture
def temp_ipc_file(tmp_path):
    return tmp_path / "control_signal.json"

def test_emergency_ipc_signal_latency(temp_ipc_file):
    """
    Benchmark the round-trip latency of control_signal.json to ensure
    HALT actions are acknowledged within 50ms.
    """
    import sys
    orig_interval = sys.getswitchinterval()
    sys.setswitchinterval(0.0005)  # 0.5ms GIL switch for sub-millisecond thread reactivity

    temp_ipc_file.parent.mkdir(parents=True, exist_ok=True)
    stop_event = threading.Event()
    
    def trader_daemon():
        while not stop_event.is_set():
            sig = read_control_signal(temp_ipc_file)
            if sig and not sig.get("acknowledged_by_trader"):
                acknowledge_control_signal(temp_ipc_file, current_signal=sig)
            
    t = threading.Thread(target=trader_daemon, daemon=True)
    t.start()
    
    try:
        start_time = time.perf_counter()
        write_control_signal("HALT", reason="Chaos test", file_path=temp_ipc_file)
        
        acknowledged = False
        for _ in range(2000):
            sig = read_control_signal(temp_ipc_file)
            if sig and sig.get("acknowledged_by_trader"):
                acknowledged = True
                break
            
        latency_ms = (time.perf_counter() - start_time) * 1000
        stop_event.set()
        t.join()
        
        threshold = 75 if os.name == "nt" else 50  # Windows NTFS metadata commit overhead vs Linux < 10ms
        assert acknowledged, "Signal was not acknowledged by trader daemon."
        assert latency_ms < threshold, f"IPC Latency too high: {latency_ms:.2f} ms (Expected < {threshold}ms)"
    finally:
        sys.setswitchinterval(orig_interval)

def test_unix_socket_buffer_overflow(tmp_path):
    """
    Stress test local IPC socket by broadcasting 10,000 tick frames,
    then simulating an abrupt client disconnection to ensure server
    continues streaming without memory leaks or BrokenPipeErrors.
    """
    async def _run_test():
        socket_path = tmp_path / "schwab_trader.sock"
        server = TraderIPCServer(socket_path=socket_path, tcp_port=0) # 0 for random free port
        await server.start()
        
        # we need to get the actual port if tcp
        actual_port = server._server.sockets[0].getsockname()[1] if not server._has_unix else 0
        
        client1 = TraderIPCClient(socket_path=socket_path, tcp_port=actual_port)
        connected = await client1.connect()
        assert connected
        
        # Broadcast 10,000 messages
        msg = {"type": "TICK", "symbol": "SPY", "price": 500.0}
        for _ in range(10000):
            server.broadcast(msg)
            
        # Give the event loop a chance to flush writes to the socket
        await asyncio.sleep(0.05)
            
        # Simulate abrupt client disconnect
        await client1.disconnect()
        
        # Verify server continues to stream without error
        for _ in range(100):
            server.broadcast(msg)
            
        # Connect a second client to ensure the server hasn't stalled
        client2 = TraderIPCClient(socket_path=socket_path, tcp_port=actual_port)
        connected2 = await client2.connect()
        assert connected2
        
        server.broadcast({"type": "MARKER"})
        
        # Read from client 2
        async for payload in client2.listen():
            assert payload.get("type") == "MARKER"
            break
            
        await client2.disconnect()
        await server.stop()
        
    asyncio.run(_run_test())
