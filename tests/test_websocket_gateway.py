"""
tests/test_websocket_gateway.py
================================
Integration & Endpoint Tests for FastAPI Real-Time WebSocket Telemetry Gateway (#68).

Tests:
1. WebSocket connection handshake returns HTTP 101 Switching Protocols.
2. Broadcast reception (verifies connected mock client receives structured 1Hz telemetry packets).
3. Disconnect resilience (verifies client drop does not leak memory or throw unhandled exceptions in the broadcast loop).
4. REST polling fallback (GET /api/telemetry/snapshot returns unified payload).
5. Interactive simulation sandbox HTML endpoint availability.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import pytest
from starlette.testclient import TestClient

from execution.microstructure_sentinel import MicrostructureSentinel
from web.app import app
from web.websocket_manager import (
    ConnectionManager,
    build_telemetry_payload,
    pull_active_envelopes,
    pull_latest_regime,
    pull_settlement_balances,
    record_settlement_balance,
    telemetry_manager,
)


def test_websocket_handshake_101():
    """
    Test 1: WebSocket connection handshake returns HTTP 101 Switching Protocols.
    """
    client = TestClient(app)
    initial_count = telemetry_manager.active_count

    with client.websocket_connect("/ws/telemetry") as ws:
        # Successful connection context implies HTTP 101 Switching Protocols handshake succeeded
        assert ws is not None
        assert telemetry_manager.active_count == initial_count + 1

    # On exit from context, connection is disconnected
    assert telemetry_manager.active_count == initial_count


def test_broadcast_reception_structured_packets():
    """
    Test 2: Broadcast reception: verifies connected mock client receives structured 1Hz telemetry packets.
    """
    # Register active MicrostructureSentinel instance for SOXL
    MicrostructureSentinel.clear_active_sentinels()
    sentinel = MicrostructureSentinel(
        symbol="SOXL",
        s0_reference=102.28,
        soft_floor_pct=0.037,
        hard_catastrophic_pct=0.060,
    )
    sentinel.high_watermark = 102.50
    sentinel.trail_stop = 99.85
    sentinel.catastrophic_floor = 96.23
    sentinel.target = 106.37

    client = TestClient(app)

    with client.websocket_connect("/ws/telemetry") as ws:
        # Receive initial or 1Hz broadcast packet
        data = ws.receive_json()

        # 1. Assert Root Keys
        assert "timestamp" in data
        assert "engine_state" in data
        assert data["engine_state"] in ("STANDBY", "RUNNING", "FLUSHED")
        assert "regime" in data
        assert "balances" in data
        assert "envelopes" in data

        # 2. Assert Regime Structure
        regime = data["regime"]
        assert "ses" in regime
        assert "classification" in regime
        assert "bias" in regime
        assert isinstance(regime["ses"], (int, float))
        assert regime["classification"] in ("CHOP", "FAVORABLE", "DEFENSIVE")
        assert regime["bias"] in ("NEUTRAL", "BULLISH", "BEARISH")

        # 3. Assert Balances Structure
        balances = data["balances"]
        assert "total_nlv" in balances
        assert "settled_cash" in balances
        assert "unsettled_cash" in balances
        assert "gfv_count" in balances
        assert balances["total_nlv"] == pytest.approx(5563.97, abs=1.0)
        assert balances["settled_cash"] == pytest.approx(3786.27, abs=1.0)
        assert balances["unsettled_cash"] == pytest.approx(1777.70, abs=1.0)
        assert balances["gfv_count"] == 0

        # 4. Assert Envelopes Structure & Dynamic Coordinates
        envelopes = data["envelopes"]
        assert "SOXL" in envelopes
        soxl_env = envelopes["SOXL"]
        assert soxl_env["s0"] == pytest.approx(102.28, abs=0.01)
        assert soxl_env["high_watermark"] == pytest.approx(102.50, abs=0.01)
        assert soxl_env["trail_stop"] == pytest.approx(99.85, abs=0.01)
        assert soxl_env["catastrophic_floor"] == pytest.approx(96.23, abs=0.01)
        assert soxl_env["target"] == pytest.approx(106.37, abs=0.01)

        # 5. Broadcast dynamic packet manually and verify reception
        custom_payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "engine_state": "RUNNING",
            "regime": {"ses": 0.45, "classification": "FAVORABLE", "bias": "BULLISH"},
            "balances": {"total_nlv": 5600.0, "settled_cash": 3800.0, "unsettled_cash": 1800.0, "gfv_count": 0},
            "envelopes": {
                "SOXL": {"s0": 102.28, "high_watermark": 105.0, "trail_stop": 101.12, "catastrophic_floor": 96.23, "target": 106.37}
            },
        }

        asyncio.run(telemetry_manager.broadcast_json(custom_payload))
        recv = ws.receive_json()
        assert recv["engine_state"] == "RUNNING"
        assert recv["regime"]["classification"] == "FAVORABLE"
        assert recv["envelopes"]["SOXL"]["high_watermark"] == 105.0


def test_disconnect_resilience_and_memory_leak():
    """
    Test 3: Disconnect resilience: verifies client drop does not leak memory or
    throw unhandled exceptions in the broadcast loop.
    """
    manager = ConnectionManager(ping_interval=0.5)

    class MockWebSocket:
        def __init__(self, should_fail_send=False):
            self.accepted = False
            self.closed = False
            self.sent = []
            self.should_fail_send = should_fail_send

        async def accept(self):
            self.accepted = True

        async def send_json(self, data):
            if self.should_fail_send or self.closed:
                raise ConnectionResetError("Client disconnected unexpectedly")
            self.sent.append(data)

        async def send(self, data):
            if self.should_fail_send or self.closed:
                raise ConnectionResetError("Client disconnected unexpectedly")
            self.sent.append(data)

        async def close(self):
            self.closed = True

    async def _scenario():
        ws1 = MockWebSocket()
        ws2 = MockWebSocket(should_fail_send=False)
        ws3 = MockWebSocket()

        # Connect all 3
        await manager.connect(ws1)
        await manager.connect(ws2)
        await manager.connect(ws3)
        assert manager.active_count == 3

        # Abruptly drop ws2
        ws2.closed = True
        ws2.should_fail_send = True

        # Broadcast packet
        test_payload = {"timestamp": "2026-10-15T18:00:00Z", "engine_state": "RUNNING"}
        # Must not throw unhandled exception
        await manager.broadcast_json(test_payload)

        # Disconnected ws2 should be pruned automatically (no memory leak)
        assert ws2 not in manager._active_connections
        assert manager.active_count == 2
        assert len(ws1.sent) >= 1
        assert len(ws3.sent) >= 1

        # Disconnect remaining clients cleanly
        manager.disconnect(ws1)
        manager.disconnect(ws3)
        assert manager.active_count == 0

        # Broadcast with 0 clients must succeed cleanly
        await manager.broadcast_json(test_payload)
        assert manager.active_count == 0

        await manager.stop()

    asyncio.run(_scenario())


def test_rest_telemetry_snapshot_fallback():
    """
    Test 4: Expose REST endpoint GET /api/telemetry/snapshot as an HTTP polling fallback.
    """
    client = TestClient(app)
    resp = client.get("/api/telemetry/snapshot")
    assert resp.status_code == 200
    data = resp.json()

    assert "timestamp" in data
    assert "engine_state" in data
    assert "regime" in data
    assert "balances" in data
    assert "envelopes" in data
    assert "SOXL" in data["envelopes"]


def test_simulation_sandbox_html_endpoint():
    """
    Test 5: Interactive simulation sandbox HTML served at /simulation_sandbox.
    """
    client = TestClient(app)
    resp = client.get("/simulation_sandbox")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "SchwabEngine Core 1 Telemetry Gateway" in resp.text
    assert "/ws/telemetry" in resp.text
