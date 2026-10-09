"""
tests/test_ipc_control.py
=========================
Tests for atomic IPC signaling and local broadcast streaming:
1. Atomic write and acknowledgment of state/control_signal.json.
2. Atomic serialization of state/dynamic_policy.json.
3. TraderIPCServer broadcast to TraderIPCClient.
"""

import asyncio
from pathlib import Path
import pytest

from core.ipc import (
    TraderIPCClient,
    TraderIPCServer,
    acknowledge_control_signal,
    read_control_signal,
    read_dynamic_policy,
    write_control_signal,
    write_dynamic_policy,
)


def test_atomic_control_signal_write_and_read(tmp_path):
    sig_file = tmp_path / "control_signal.json"
    assert read_control_signal(sig_file) is None

    written = write_control_signal(action="HALT", reason="TEST_HALT", file_path=sig_file)
    assert written["action"] == "HALT"
    assert written["acknowledged_by_trader"] is False

    read_sig = read_control_signal(sig_file)
    assert read_sig is not None
    assert read_sig["action"] == "HALT"
    assert read_sig["reason"] == "TEST_HALT"

    ack_sig = acknowledge_control_signal(sig_file)
    assert ack_sig["acknowledged_by_trader"] is True
    assert ack_sig["acknowledged_at"] is not None

    # Re-reading reflects acknowledged state
    re_read = read_control_signal(sig_file)
    assert re_read["acknowledged_by_trader"] is True


def test_atomic_dynamic_policy_roundtrip(tmp_path):
    pol_file = tmp_path / "dynamic_policy.json"
    assert read_dynamic_policy(pol_file) is None

    policy_payload = {
        "effective_session": "POWER_HOUR",
        "morning_hurst": 0.48,
        "overrides": {
            "power_hour_rvol_hurdle": 1.65,
            "power_hour_size_scale": 0.35,
        },
    }
    write_dynamic_policy(policy_payload, file_path=pol_file)

    loaded = read_dynamic_policy(pol_file)
    assert loaded is not None
    assert loaded["effective_session"] == "POWER_HOUR"
    assert loaded["morning_hurst"] == 0.48
    assert loaded["overrides"]["power_hour_rvol_hurdle"] == 1.65
    assert loaded["overrides"]["power_hour_size_scale"] == 0.35
    assert "updated_at" in loaded


def test_trader_ipc_broadcast_stream(tmp_path):
    async def _run():
        sock_file = tmp_path / "test_trader.sock"
        server = TraderIPCServer(socket_path=sock_file, tcp_port=8991)
        await server.start()

        client = TraderIPCClient(socket_path=sock_file, tcp_port=8991)
        connected = await client.connect(timeout=2.0)
        assert connected is True

        received = []

        async def reader_coro():
            async for msg in client.listen():
                received.append(msg)
                if len(received) >= 2:
                    break

        reader_task = asyncio.create_task(reader_coro())
        await asyncio.sleep(0.05)

        server.broadcast({"type": "TICK", "symbol": "SOXL", "last": 155.20})
        server.broadcast({"type": "TICK", "symbol": "TNA", "last": 58.10})

        await asyncio.wait_for(reader_task, timeout=2.0)
        assert len(received) == 2
        assert received[0]["symbol"] == "SOXL"
        assert received[1]["symbol"] == "TNA"

        await client.disconnect()
        await server.stop()

    asyncio.run(_run())
