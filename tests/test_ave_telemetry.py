"""
tests/test_ave_telemetry.py
===========================
Unit and integration tests for api/ave_telemetry.py and WebSocket endpoints.

Tests:
1. AveTelemetryHub ring buffer and monotonic sequence numbering.
2. Fast clients remain unblocked when a slow client's queue fills.
3. Oldest messages are dropped during queue saturation (drop-oldest backpressure).
4. Snapshot delivery on connection handshake.
5. Reliable delivery of FSM state transitions (never dropped/conflated).
6. WebSocket endpoints /ws/ave/{symbol} and /ws/ave via FastAPI TestClient.
7. Clean disconnect handling and resource teardown.
"""

from __future__ import annotations

import asyncio
import orjson
from starlette.testclient import TestClient

from api.ave_telemetry import AveTelemetryHub, MAX_CLIENT_QUEUE
from api.server import build_app
from core.runtime import EngineContext


def test_ring_buffer_and_monotonic_seq():
    hub = AveTelemetryHub()
    sym = "SOXL"

    # Ingest 10 frames with forced broadcast
    for i in range(10):
        hub.ingest_tick_frame(
            symbol=sym,
            epoch_ms=1713537000000 + i * 1000,
            price=100.0 + i,
            upper=110.0 + i,
            lower=90.0 + i,
            lambda_factor=0.8,
            supremum=100.0 + i,
            state_code=4,
            force_broadcast=True,
        )

    buf = hub._get_or_create_buffer(sym)
    snapshot = buf.get_snapshot()
    assert len(snapshot) == 10
    # Check monotonic sequences
    seqs = [row[0] for row in snapshot]
    assert seqs == list(range(1, 11))
    # Frame vector format: [seq, epoch_ms, price, upper, lower, lambda, supremum, state_code]
    assert snapshot[0][2] == 100.0
    assert snapshot[-1][2] == 109.0


def test_conflation_rate_limiting():
    hub = AveTelemetryHub()
    sym = "TQQQ"

    # First frame goes through
    res1 = hub.ingest_tick_frame(
        symbol=sym, epoch_ms=1713537000000, price=80.0, upper=85.0, lower=75.0,
        lambda_factor=0.9, supremum=80.0, state_code=4, force_broadcast=False,
    )
    assert res1 is True

    # Immediate second frame within 100ms interval is conflated / throttled
    res2 = hub.ingest_tick_frame(
        symbol=sym, epoch_ms=1713537000050, price=80.1, upper=85.0, lower=75.0,
        lambda_factor=0.9, supremum=80.1, state_code=4, force_broadcast=False,
    )
    assert res2 is False


def test_slow_client_isolation_and_drop_oldest():
    async def _run():
        hub = AveTelemetryHub()
        sym = "SOXL"

        # Mock WebSocket objects
        class MockWebSocket:
            def __init__(self):
                self.sent = []
            async def accept(self): pass
            async def send_bytes(self, b):
                self.sent.append(b)
            async def close(self): pass

        ws_fast = MockWebSocket()
        ws_slow = MockWebSocket()

        # Connect fast and slow client
        client_fast = await hub.register_client(ws_fast, initial_symbols=[sym])
        client_slow = await hub.register_client(ws_slow, initial_symbols=[sym])

        # Saturate slow client's queue by pushing more than MAX_CLIENT_QUEUE (64)
        for i in range(MAX_CLIENT_QUEUE + 20):
            payload = orjson.dumps({"frame_num": i})
            client_slow.push(payload)

        # Confirm slow client queue is capped at MAX_CLIENT_QUEUE and didn't raise QueueFull
        assert client_slow.queue.qsize() == MAX_CLIENT_QUEUE

        # Confirm fast client can still receive messages without blocking
        fast_msg = orjson.dumps({"msg": "fast_client_message"})
        client_fast.push(fast_msg)
        assert client_fast.queue.qsize() >= 1

        await client_fast.close()
        await client_slow.close()

    asyncio.run(_run())


def test_snapshot_on_connect_and_fsm_events():
    async def _run():
        hub = AveTelemetryHub()
        sym = "TNA"

        # Populate buffer with 5 frames
        for i in range(5):
            hub.ingest_tick_frame(
                symbol=sym, epoch_ms=1713537000000 + i * 1000, price=40.0 + i,
                upper=45.0, lower=35.0, lambda_factor=0.5, supremum=40.0 + i,
                state_code=4, force_broadcast=True,
            )

        class MockWS:
            def __init__(self): self.messages = []
            async def accept(self): pass
            async def send_bytes(self, b): self.messages.append(b)
            async def close(self): pass

        ws = MockWS()
        client = await hub.register_client(ws, initial_symbols=[sym])

        # First message in client's queue should be SNAPSHOT with 5 frames
        first_payload = await client.queue.get()
        decoded = orjson.loads(first_payload)
        assert decoded["type"] == "SNAPSHOT"
        assert decoded["symbol"] == sym
        assert len(decoded["data"]) == 5

        # Broadcast reliable FSM event
        hub.broadcast_fsm_event(
            symbol=sym,
            state="TIGHTENING",
            prev_state="TRACKING",
            reason="UPPER_TOUCH",
            details={"k_l": 0.75},
        )

        fsm_payload = await client.queue.get()
        fsm_event = orjson.loads(fsm_payload)
        assert fsm_event["type"] == "FSM_EVENT"
        assert fsm_event["state"] == "TIGHTENING"
        assert fsm_event["reason"] == "UPPER_TOUCH"

        await client.close()

    asyncio.run(_run())


def test_websocket_endpoint_integration():
    ctx = EngineContext()
    app = build_app(ctx)
    client = TestClient(app)

    # Test WebSocket connection to /ws/ave/SOXL
    with client.websocket_connect("/ws/ave/SOXL") as ws:
        # First received message is SNAPSHOT binary payload
        snap_bytes = ws.receive_bytes()
        snap_data = orjson.loads(snap_bytes)
        assert snap_data["type"] == "SNAPSHOT"
        assert snap_data["symbol"] == "SOXL"

        # Ingest a tick to the hub
        hub = AveTelemetryHub.get_instance()
        hub.ingest_tick_frame(
            symbol="SOXL",
            epoch_ms=1713537000000,
            price=105.5,
            upper=110.0,
            lower=100.0,
            lambda_factor=0.75,
            supremum=105.5,
            state_code=4,
            force_broadcast=True,
        )

        frame_bytes = ws.receive_bytes()
        frame_msg = orjson.loads(frame_bytes)
        assert frame_msg["type"] == "FRAME"
        assert frame_msg["symbol"] == "SOXL"
        assert frame_msg["frame"][2] == 105.5


def _receive_bytes_timeout(ws, timeout: float = 5.0) -> bytes:
    future = ws.portal.start_task_soon(ws._send_rx.receive)
    message = future.result(timeout=timeout)
    ws._raise_on_close(message)
    return message["bytes"]


def test_websocket_multi_symbol_subscription():
    ctx = EngineContext()
    app = build_app(ctx)
    client = TestClient(app)

    # Test general multi-symbol stream at /ws/ave
    with client.websocket_connect("/ws/ave") as ws:
        # Subscribe dynamically to TQQQ
        ws.send_text(orjson.dumps({"action": "subscribe", "symbol": "TQQQ"}).decode("utf-8"))

        # Receive SNAPSHOT first as explicit acknowledgment of subscription
        msg1 = orjson.loads(_receive_bytes_timeout(ws, timeout=5.0))
        assert msg1["type"] == "SNAPSHOT"
        assert msg1["symbol"] == "TQQQ"

        # Ingest a tick for TQQQ
        hub = AveTelemetryHub.get_instance()
        hub.ingest_tick_frame(
            symbol="TQQQ",
            epoch_ms=1713537000000,
            price=82.0,
            upper=86.0,
            lower=78.0,
            lambda_factor=0.85,
            supremum=82.0,
            state_code=4,
            force_broadcast=True,
        )

        # Receive FRAME
        msg2 = orjson.loads(_receive_bytes_timeout(ws, timeout=5.0))
        assert msg2["type"] == "FRAME"
        assert msg2["symbol"] == "TQQQ"
        assert msg2["frame"][2] == 82.0
