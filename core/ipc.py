"""
core/ipc.py
===========
Inter-Process Communication (IPC) layer between schwab-trader and schwab-web:
1. Atomic file control signaling (state/control_signal.json).
2. Dynamic policy overrides (state/dynamic_policy.json).
3. Local streaming relay (Unix domain socket / local IPC stream).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import socket
import tempfile
from typing import Any, AsyncIterator, Dict, List, Optional, Set, Union

from core.paths import CONTROL_SIGNAL_FILE, DYNAMIC_POLICY_FILE, TRADER_IPC_SOCKET

logger = logging.getLogger(__name__)

DEFAULT_TCP_FALLBACK_PORT = 8765


# ---------------------------------------------------------------------------
# Atomic File IPC
# ---------------------------------------------------------------------------

def _atomic_write_json(file_path: Path | str, data: Dict[str, Any]) -> None:
    target = Path(file_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(data, indent=2, default=str).encode("utf-8")
    temp_path = target.with_name(f".{target.name}.tmp")

    temp_path.write_bytes(serialized)

    for attempt in range(5):
        try:
            os.replace(temp_path, target)
            return
        except PermissionError:
            if attempt == 4:
                raise
            import time
            time.sleep(0)


def write_control_signal(
    action: str,
    reason: str = "",
    sequence_id: Optional[int] = None,
    file_path: Optional[Path | str] = None,
) -> Dict[str, Any]:
    """
    Atomically writes state/control_signal.json to request HALT, RESUME, or LIQUIDATE_ALL.
    """
    target_path = Path(file_path) if file_path is not None else CONTROL_SIGNAL_FILE
    now_utc = datetime.now(timezone.utc).isoformat()
    if sequence_id is None:
        sequence_id = int(datetime.now(timezone.utc).timestamp() * 1000)

    payload = {
        "sequence_id": sequence_id,
        "timestamp": now_utc,
        "action": action.upper(),
        "reason": reason,
        "acknowledged_by_trader": False,
        "acknowledged_at": None,
    }
    _atomic_write_json(target_path, payload)
    return payload


def read_control_signal(file_path: Optional[Path | str] = None) -> Optional[Dict[str, Any]]:
    target = Path(file_path) if file_path is not None else CONTROL_SIGNAL_FILE
    if not target.exists():
        return None
    for attempt in range(5):
        try:
            content = target.read_text(encoding="utf-8")
            return json.loads(content)
        except PermissionError:
            if attempt == 4:
                return None
            import time
            time.sleep(0)
        except Exception as exc:
            logger.warning("Error reading control signal from %s: %s", target, exc)
            return None
    return None


def acknowledge_control_signal(
    file_path: Optional[Path | str] = None,
    current_signal: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Mark the current control signal as acknowledged by the trading daemon."""
    target_path = Path(file_path) if file_path is not None else CONTROL_SIGNAL_FILE
    sig = current_signal if current_signal is not None else read_control_signal(target_path)
    if sig is None or sig.get("acknowledged_by_trader"):
        return sig

    sig["acknowledged_by_trader"] = True
    sig["acknowledged_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(target_path, sig)
    return sig


def write_dynamic_policy(
    policy_data: Dict[str, Any],
    file_path: Optional[Path | str] = None,
) -> None:
    """Atomically write dynamic policy overrides from midday optimization."""
    target_path = Path(file_path) if file_path is not None else DYNAMIC_POLICY_FILE
    data = dict(policy_data)
    if "updated_at" not in data:
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(target_path, data)


def read_dynamic_policy(file_path: Optional[Path | str] = None) -> Optional[Dict[str, Any]]:
    target = Path(file_path) if file_path is not None else DYNAMIC_POLICY_FILE
    if not target.exists():
        return None
    try:
        content = target.read_text(encoding="utf-8")
        return json.loads(content)
    except Exception as exc:
        logger.warning("Error reading dynamic policy from %s: %s", target, exc)
        return None


# ---------------------------------------------------------------------------
# Local Broadcast IPC (Unix Domain Socket / Localhost Fallback)
# ---------------------------------------------------------------------------

class TraderIPCServer:
    """
    Broadcast server run inside schwab-trader.service.
    Publishes ticks and status frames to connected local clients (e.g. schwab-web.service).
    Uses Unix domain sockets on POSIX/Linux, and localhost TCP fallback when AF_UNIX is unavailable.
    """

    def __init__(
        self,
        socket_path: Path | str = TRADER_IPC_SOCKET,
        tcp_port: int = DEFAULT_TCP_FALLBACK_PORT,
    ):
        self.socket_path = Path(socket_path)
        self.tcp_port = tcp_port
        self._server: Optional[asyncio.AbstractServer] = None
        self._clients: Set[asyncio.StreamWriter] = set()
        self._has_unix = hasattr(socket, "AF_UNIX")

    async def start(self) -> None:
        if self._has_unix:
            if self.socket_path.exists():
                try:
                    self.socket_path.unlink()
                except OSError:
                    pass
            self.socket_path.parent.mkdir(parents=True, exist_ok=True)
            self._server = await asyncio.start_unix_server(
                self._handle_client,
                path=str(self.socket_path),
            )
            logger.info("TraderIPCServer listening on Unix socket %s", self.socket_path)
        else:
            self._server = await asyncio.start_server(
                self._handle_client,
                host="127.0.0.1",
                port=self.tcp_port,
            )
            logger.info("TraderIPCServer listening on localhost TCP port %d", self.tcp_port)

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._clients.add(writer)
        try:
            while not reader.at_eof():
                # Keep alive until disconnect
                data = await reader.read(1024)
                if not data:
                    break
        except Exception:
            pass
        finally:
            self._clients.discard(writer)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    def broadcast(self, message: Dict[str, Any]) -> None:
        """Non-blocking broadcast of a JSON payload to all connected clients."""
        if not self._clients:
            return
        line = (json.dumps(message, default=str) + "\n").encode("utf-8")
        dead = []
        for writer in self._clients:
            try:
                writer.write(line)
            except Exception:
                dead.append(writer)
        for w in dead:
            self._clients.discard(w)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for writer in list(self._clients):
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        self._clients.clear()
        if self._has_unix and self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError:
                pass
        logger.info("TraderIPCServer stopped.")


class TraderIPCClient:
    """
    Client consumed by schwab-web.service to stream ticks without opening
    any external connections to Schwab.
    """

    def __init__(
        self,
        socket_path: Path | str = TRADER_IPC_SOCKET,
        tcp_port: int = DEFAULT_TCP_FALLBACK_PORT,
    ):
        self.socket_path = Path(socket_path)
        self.tcp_port = tcp_port
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._has_unix = hasattr(socket, "AF_UNIX")

    async def connect(self, timeout: float = 3.0) -> bool:
        try:
            if self._has_unix:
                coro = asyncio.open_unix_connection(path=str(self.socket_path))
            else:
                coro = asyncio.open_connection(host="127.0.0.1", port=self.tcp_port)
            self._reader, self._writer = await asyncio.wait_for(coro, timeout=timeout)
            return True
        except Exception as exc:
            logger.debug("TraderIPCClient failed to connect: %s", exc)
            self._reader, self._writer = None, None
            return False

    async def listen(self) -> AsyncIterator[Dict[str, Any]]:
        """Yield parsed JSON messages from trader."""
        if self._reader is None:
            return
        try:
            while not self._reader.at_eof():
                line = await self._reader.readline()
                if not line:
                    break
                try:
                    payload = json.loads(line.decode("utf-8").strip())
                    yield payload
                except json.JSONDecodeError:
                    continue
        except Exception as exc:
            logger.debug("TraderIPCClient stream error: %s", exc)
        finally:
            await self.disconnect()

    async def disconnect(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
            self._reader = None
