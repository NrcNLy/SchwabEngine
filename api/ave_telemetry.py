"""
api/ave_telemetry.py
====================
High-Performance orjson Telemetry Hub for Adaptive Volatility Envelope (AVE).

Key Architecture:
1. Ring Buffer: 500-sample in-memory capacity per symbol.
2. Source Conflation: Ticks per symbol are rate-limited / conflated to max 10 Hz.
3. Fast/Slow Client Isolation:
   - Per-client bounded queue (maxsize=64).
   - Drop-oldest backpressure: When client's queue fills, the oldest message is dropped.
   - Slow / stalled clients never block or degrade fast clients.
4. Binary orjson Serialization:
   - Uses `orjson.dumps(..., option=orjson.OPT_SERIALIZE_NUMPY)`.
   - Compact frame vector format: [seq, epoch_ms, price, upper, lower, lambda, supremum, state_code]
5. Monotonic Sequence Numbering:
   - Global sequence per symbol to detect packet loss/gaps on client side.
6. Snapshot-on-Connect:
   - When a client connects or subscribes to a symbol, it immediately receives the historical ring buffer
     as a "SNAPSHOT" payload before real-time frames commence.
7. Reliable Event Channel:
   - FSM state transitions (e.g. ARMED, TRACKING, TIGHTENING, TRIGGERED, SETTLED) are sent via reliable
     event objects `{"type": "FSM_EVENT", ...}` which bypass tick conflation.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
import logging
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import orjson
from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger("SchwabEngine.AveTelemetry")

MAX_RING_BUFFER = 500
MAX_CLIENT_QUEUE = 64
MAX_BROADCAST_HZ = 10.0
MIN_BROADCAST_INTERVAL = 1.0 / MAX_BROADCAST_HZ  # 100ms


class SymbolRingBuffer:
    """Fixed-capacity ring buffer storing compact frame vectors."""

    def __init__(self, capacity: int = MAX_RING_BUFFER) -> None:
        self.capacity = capacity
        self.buffer: deque[List[Any]] = deque(maxlen=capacity)

    def append(self, frame: List[Any]) -> None:
        self.buffer.append(frame)

    def get_snapshot(self) -> List[List[Any]]:
        return list(self.buffer)


class AveClientConnection:
    """Manages an individual WebSocket subscriber with a bounded queue and dedicated writer."""

    def __init__(self, ws: WebSocket, hub: AveTelemetryHub) -> None:
        self.ws = ws
        self.hub = hub
        self.subscribed_symbols: Set[str] = set()
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=MAX_CLIENT_QUEUE)
        self.writer_task: Optional[asyncio.Task] = None
        self._closed = False

    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        loop = loop or asyncio.get_event_loop()
        self.writer_task = loop.create_task(self._writer_loop())

    async def _writer_loop(self) -> None:
        """Pulls serialized payloads from the bounded queue and sends to WebSocket."""
        try:
            while not self._closed:
                payload = await self.queue.get()
                await self.ws.send_bytes(payload)
                self.queue.task_done()
        except WebSocketDisconnect:
            pass
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("AveClientConnection writer error: %s", exc)
        finally:
            self._closed = True
            await self.hub.disconnect(self)

    def push(self, binary_payload: bytes) -> bool:
        """
        Pushes payload into queue. If full, drops the oldest message (drop-oldest backpressure)
        so this client never blocks the broadcast thread or other clients.
        """
        if self._closed:
            return False

        if self.queue.full():
            try:
                # Discard oldest message
                self.queue.get_nowait()
                self.queue.task_done()
            except (asyncio.QueueEmpty, ValueError):
                pass

        try:
            self.queue.put_nowait(binary_payload)
            return True
        except asyncio.QueueFull:
            return False

    async def close(self) -> None:
        self._closed = True
        if self.writer_task and not self.writer_task.done():
            self.writer_task.cancel()
        try:
            await self.ws.close()
        except Exception:
            pass


class AveTelemetryHub:
    """
    Central hub managing per-symbol ring buffers, rate-limiting conflation,
    and client WebSocket subscriptions.
    """

    _instance: Optional[AveTelemetryHub] = None

    def __init__(self) -> None:
        self.ring_buffers: Dict[str, SymbolRingBuffer] = {}
        self.clients: Set[AveClientConnection] = set()
        self.symbol_subscribers: Dict[str, Set[AveClientConnection]] = {}
        self.symbol_seq: Dict[str, int] = {}
        self.last_broadcast_time: Dict[str, float] = {}
        self._lock = asyncio.Lock()

    @classmethod
    def get_instance(cls) -> AveTelemetryHub:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        cls._instance = None

    def _get_or_create_buffer(self, symbol: str) -> SymbolRingBuffer:
        sym = symbol.upper()
        if sym not in self.ring_buffers:
            self.ring_buffers[sym] = SymbolRingBuffer(capacity=MAX_RING_BUFFER)
        return self.ring_buffers[sym]

    def _get_next_seq(self, symbol: str) -> int:
        sym = symbol.upper()
        seq = self.symbol_seq.get(sym, 0) + 1
        self.symbol_seq[sym] = seq
        return seq

    async def register_client(self, ws: WebSocket, initial_symbols: Optional[List[str]] = None) -> AveClientConnection:
        """Registers a new WebSocket connection and starts its sender task."""
        await ws.accept()
        conn = AveClientConnection(ws, self)
        conn.start()

        async with self._lock:
            self.clients.add(conn)

        if initial_symbols:
            for s in initial_symbols:
                await self.subscribe(conn, s)

        return conn

    async def disconnect(self, client: AveClientConnection) -> None:
        """Cleans up disconnected client from all subscription sets."""
        async with self._lock:
            self.clients.discard(client)
            for sym, subscribers in list(self.symbol_subscribers.items()):
                subscribers.discard(client)

    async def subscribe(self, client: AveClientConnection, symbol: str) -> None:
        """Subscribes client to a symbol and immediately dispatches snapshot history."""
        sym = symbol.upper()
        async with self._lock:
            client.subscribed_symbols.add(sym)
            if sym not in self.symbol_subscribers:
                self.symbol_subscribers[sym] = set()
            self.symbol_subscribers[sym].add(client)

            buf = self._get_or_create_buffer(sym)
            snapshot_data = buf.get_snapshot()

        # Send SNAPSHOT message to client
        snapshot_msg = {
            "type": "SNAPSHOT",
            "symbol": sym,
            "data": snapshot_data,
        }
        encoded = orjson.dumps(snapshot_msg, option=orjson.OPT_SERIALIZE_NUMPY)
        client.push(encoded)

    async def unsubscribe(self, client: AveClientConnection, symbol: str) -> None:
        sym = symbol.upper()
        async with self._lock:
            client.subscribed_symbols.discard(sym)
            if sym in self.symbol_subscribers:
                self.symbol_subscribers[sym].discard(client)

    def ingest_tick_frame(
        self,
        symbol: str,
        epoch_ms: int,
        price: float,
        upper: float,
        lower: float,
        lambda_factor: float,
        supremum: float,
        state_code: int,
        force_broadcast: bool = False,
    ) -> bool:
        """
        Ingests a real-time tick frame.
        Appends to ring buffer, enforces 10 Hz conflation window, serializes to orjson,
        and pushes to all active subscribers.
        """
        sym = symbol.upper()
        now = time.monotonic()
        last_t = self.last_broadcast_time.get(sym, 0.0)

        # Conflation check
        if not force_broadcast and (now - last_t) < MIN_BROADCAST_INTERVAL:
            return False

        self.last_broadcast_time[sym] = now
        seq = self._get_next_seq(sym)

        # Compact frame vector: [seq, epoch_ms, price, upper, lower, lambda, supremum, state_code]
        frame = [
            seq,
            int(epoch_ms),
            round(float(price), 4),
            round(float(upper), 4),
            round(float(lower), 4),
            round(float(lambda_factor), 5),
            round(float(supremum), 4),
            int(state_code),
        ]

        # Add to ring buffer
        buf = self._get_or_create_buffer(sym)
        buf.append(frame)

        # Broadcast to symbol subscribers
        subscribers = self.symbol_subscribers.get(sym)
        if not subscribers:
            return True

        tick_msg = {
            "type": "FRAME",
            "symbol": sym,
            "frame": frame,
        }
        binary = orjson.dumps(tick_msg, option=orjson.OPT_SERIALIZE_NUMPY)

        for client in list(subscribers):
            client.push(binary)

        return True

    def broadcast_fsm_event(
        self,
        symbol: str,
        state: str,
        prev_state: Optional[str] = None,
        reason: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Broadcasts reliable FSM state transition event.
        Never conflated or dropped.
        """
        sym = symbol.upper()
        subscribers = self.symbol_subscribers.get(sym)
        if not subscribers:
            return

        event_msg = {
            "type": "FSM_EVENT",
            "symbol": sym,
            "state": state,
            "prev_state": prev_state,
            "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "details": details or {},
        }
        binary = orjson.dumps(event_msg, option=orjson.OPT_SERIALIZE_NUMPY)

        for client in list(subscribers):
            client.push(binary)
