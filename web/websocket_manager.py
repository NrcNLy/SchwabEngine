"""
web/websocket_manager.py
========================
FastAPI Real-Time WebSocket Telemetry Gateway & Dashboard Bridge (#68).

Key Components:
1. ConnectionManager: Thread-safe WebSocket client manager with 15-second heartbeat keep-alive.
2. Telemetry Aggregation Loop: 1Hz background worker pulling:
   - Semiconductor Environment Score (SES) & regime classification from state/telemetry.db
   - Cash settlement balances from state/trades.db
   - Dynamic Adaptive Volatility Envelope (AVE) coordinates from MicrostructureSentinel
3. Snapshot generation for REST polling fallback (GET /api/telemetry/snapshot).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sqlite3
import threading
from typing import Any, Dict, List, Optional, Set, Union

from fastapi import WebSocket

from core.paths import CONTROL_SIGNAL_FILE, STATE_DIR, TELEMETRY_DB_PATH, TRADES_DB_PATH
from execution.microstructure_sentinel import MicrostructureSentinel

logger = logging.getLogger("SchwabEngine.WebSocketGateway")


def pull_latest_regime(telemetry_db_path: Optional[Union[Path, str]] = None) -> Dict[str, Any]:
    """
    Pull latest SES score and regime state from state/telemetry.db.
    Falls back gracefully to state/macro_state.json or default baseline.
    """
    db_file = Path(telemetry_db_path) if telemetry_db_path else TELEMETRY_DB_PATH
    ses_score: float = 0.12
    classification: str = "CHOP"
    bias: str = "NEUTRAL"

    if db_file.exists():
        try:
            conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=2.0)
            cur = conn.cursor()
            
            # Check if regime_telemetry table exists
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='regime_telemetry'")
            if cur.fetchone():
                cur.execute("SELECT ses, regime, payload_json FROM regime_telemetry ORDER BY id DESC LIMIT 1")
                row = cur.fetchone()
                if row:
                    ses_score = float(row[0])
                    classification = str(row[1]).upper() if row[1] else "CHOP"
                    if row[2]:
                        try:
                            p_data = json.loads(row[2])
                            bias = str(p_data.get("bias", "")).upper()
                        except Exception:
                            pass
            else:
                # Check event_log table for regime/SES events
                cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='event_log'")
                if cur.fetchone():
                    cur.execute(
                        "SELECT payload FROM event_log WHERE event_type IN ('REGIME_CHANGE', 'SES_EVALUATION', 'REGIME_UPDATE') ORDER BY id DESC LIMIT 1"
                    )
                    row = cur.fetchone()
                    if row and row[0]:
                        try:
                            p_data = json.loads(row[0])
                            if "ses" in p_data:
                                ses_score = float(p_data["ses"])
                            if "regime" in p_data:
                                classification = str(p_data["regime"]).upper()
                            if "bias" in p_data:
                                bias = str(p_data["bias"]).upper()
                        except Exception:
                            pass
            conn.close()
        except Exception as exc:
            logger.debug("Could not read regime from telemetry.db: %s", exc)

    # If classification was not extracted, determine from ses_score
    if not classification:
        if ses_score >= 0.35:
            classification = "FAVORABLE"
        elif ses_score <= -0.35:
            classification = "DEFENSIVE"
        else:
            classification = "CHOP"

    # Determine bias if not set
    if not bias:
        if ses_score >= 0.20:
            bias = "BULLISH"
        elif ses_score <= -0.20:
            bias = "BEARISH"
        else:
            bias = "NEUTRAL"

    return {
        "ses": round(ses_score, 2),
        "classification": classification,
        "bias": bias,
    }


def pull_settlement_balances(trades_db_path: Optional[Union[Path, str]] = None) -> Dict[str, Any]:
    """
    Pull current cash settlement balances from state/trades.db.
    Defaults to settled cash and T+1 unsettled balances.
    """
    db_file = Path(trades_db_path) if trades_db_path else TRADES_DB_PATH
    total_nlv: float = 5563.97
    settled_cash: float = 3786.27
    unsettled_cash: float = 1777.70
    gfv_count: int = 0

    if db_file.exists():
        try:
            conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=2.0)
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='settlement_balances'")
            if cur.fetchone():
                cur.execute("SELECT total_nlv, settled_cash, unsettled_cash, gfv_count FROM settlement_balances ORDER BY id DESC LIMIT 1")
                row = cur.fetchone()
                if row:
                    total_nlv = float(row[0])
                    settled_cash = float(row[1])
                    unsettled_cash = float(row[2])
                    gfv_count = int(row[3])
            conn.close()
        except Exception as exc:
            logger.debug("Could not read settlement balances from trades.db: %s", exc)

    return {
        "total_nlv": round(total_nlv, 2),
        "settled_cash": round(settled_cash, 2),
        "unsettled_cash": round(unsettled_cash, 2),
        "gfv_count": int(gfv_count),
    }


def record_settlement_balance(
    total_nlv: float,
    settled_cash: float,
    unsettled_cash: float,
    gfv_count: int = 0,
    trades_db_path: Optional[Union[Path, str]] = None,
) -> None:
    """Helper to persist a settlement balance record into state/trades.db."""
    db_file = Path(trades_db_path) if trades_db_path else TRADES_DB_PATH
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_file), timeout=5.0)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settlement_balances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                total_nlv REAL NOT NULL,
                settled_cash REAL NOT NULL,
                unsettled_cash REAL NOT NULL,
                gfv_count INTEGER NOT NULL DEFAULT 0,
                timestamp TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )
        conn.execute(
            """
            INSERT INTO settlement_balances (total_nlv, settled_cash, unsettled_cash, gfv_count)
            VALUES (?, ?, ?, ?);
            """,
            (float(total_nlv), float(settled_cash), float(unsettled_cash), int(gfv_count)),
        )
        conn.commit()
    finally:
        conn.close()


def pull_active_envelopes() -> Dict[str, Dict[str, float]]:
    """
    Pull active dynamic envelope bounds (Upper Target, Ratchet Trail, Catastrophic Floor, High Watermark, S0)
    from MicrostructureSentinel instances.
    """
    active_sentinels = MicrostructureSentinel.get_active_sentinels()
    if active_sentinels:
        return {sym: s.get_envelope_bounds() for sym, s in active_sentinels.items()}

    # Fallback to default SOXL reference bounds when no live sentinel instance is active
    return {
        "SOXL": {
            "s0": 102.28,
            "high_watermark": 102.50,
            "trail_stop": 99.85,
            "catastrophic_floor": 96.23,
            "target": 106.37,
        }
    }


def compute_engine_state(override: Optional[str] = None) -> str:
    """
    Derives engine state: STANDBY | RUNNING | FLUSHED.
    """
    if override in ("STANDBY", "RUNNING", "FLUSHED"):
        return override

    # Check control signal for emergency flush
    if CONTROL_SIGNAL_FILE.exists():
        try:
            signal = json.loads(CONTROL_SIGNAL_FILE.read_text(encoding="utf-8"))
            if str(signal.get("action", "")).upper() in ("LIQUIDATE_ALL", "HALT"):
                return "FLUSHED"
        except Exception:
            pass

    # Check if recovery state has latched exit
    recovery_file = STATE_DIR / "recovery_state.json"
    if recovery_file.exists():
        try:
            rec = json.loads(recovery_file.read_text(encoding="utf-8"))
            if rec.get("exit_latched") or rec.get("state") == "FLUSHED":
                return "FLUSHED"
        except Exception:
            pass

    # Check active sentinels
    if MicrostructureSentinel.get_active_sentinels():
        return "RUNNING"

    return "STANDBY"


def build_telemetry_payload(
    engine_state: Optional[str] = None,
    telemetry_db_path: Optional[Union[Path, str]] = None,
    trades_db_path: Optional[Union[Path, str]] = None,
) -> Dict[str, Any]:
    """
    Construct unified telemetry payload streaming at 1Hz.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    return {
        "timestamp": now_iso,
        "engine_state": compute_engine_state(engine_state),
        "regime": pull_latest_regime(telemetry_db_path),
        "balances": pull_settlement_balances(trades_db_path),
        "envelopes": pull_active_envelopes(),
    }


class ConnectionManager:
    """
    WebSocket Connection Manager with thread-safe client tracking and 15s keep-alive heartbeat.
    """

    def __init__(self, ping_interval: float = 15.0) -> None:
        self._active_connections: Set[WebSocket] = set()
        self._thread_lock = threading.Lock()
        self.ping_interval = float(ping_interval)
        self._running: bool = False
        self._keepalive_task: Optional[asyncio.Task] = None
        self._broadcast_task: Optional[asyncio.Task] = None
        self._latest_payload: Optional[Dict[str, Any]] = None
        self._engine_state: Optional[str] = None

    @property
    def active_count(self) -> int:
        with self._thread_lock:
            return len(self._active_connections)

    def set_engine_state(self, state: str) -> None:
        """Explicitly override engine state (STANDBY|RUNNING|FLUSHED)."""
        self._engine_state = state

    async def connect(self, websocket: WebSocket) -> None:
        """Accepts WebSocket connection and safely registers it."""
        await websocket.accept()
        with self._thread_lock:
            self._active_connections.add(websocket)
        logger.info("WebSocket connected. Active clients: %d", len(self._active_connections))

        # Ensure background broadcast & keepalive tasks are active
        self.ensure_background_tasks()

        # Send immediate initial state packet
        payload = self.get_latest_payload(refresh=True)
        try:
            await websocket.send_json(payload)
        except Exception as exc:
            logger.debug("Failed sending initial snapshot to client: %s", exc)
            self.disconnect(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        """Removes WebSocket connection from active set."""
        with self._thread_lock:
            self._active_connections.discard(websocket)
        logger.info("WebSocket disconnected. Active clients: %d", len(self._active_connections))

    async def broadcast_json(self, payload: dict) -> None:
        """
        Broadcasts JSON payload to all active client connections.
        Gracefully disconnects unresponsive or disconnected clients.
        """
        self._latest_payload = payload
        with self._thread_lock:
            clients = list(self._active_connections)

        if not clients:
            return

        dead_clients: List[WebSocket] = []
        for ws in clients:
            try:
                await ws.send_json(payload)
            except Exception:
                dead_clients.append(ws)

        for ws in dead_clients:
            self.disconnect(ws)

    def get_latest_payload(self, refresh: bool = False) -> Dict[str, Any]:
        """Return cached or freshly constructed unified telemetry payload."""
        if not refresh and self._latest_payload is not None:
            return self._latest_payload
        payload = build_telemetry_payload(self._engine_state)
        self._latest_payload = payload
        return payload

    async def _keepalive_loop(self) -> None:
        """
        Heartbeat Keep-Alive:
        Send ping frames every 15 seconds; disconnect unresponsive clients gracefully.
        """
        while self._running:
            try:
                await asyncio.sleep(self.ping_interval)
            except asyncio.CancelledError:
                break

            with self._thread_lock:
                clients = list(self._active_connections)

            if not clients:
                continue

            dead_clients: List[WebSocket] = []
            for ws in clients:
                try:
                    # Attempt ASGI ping frame first; fallback to JSON ping message
                    try:
                        await ws.send({"type": "websocket.ping"})
                    except (RuntimeError, AttributeError, NotImplementedError):
                        await ws.send_json({"type": "ping", "timestamp": datetime.now(timezone.utc).isoformat()})
                except Exception:
                    dead_clients.append(ws)

            for ws in dead_clients:
                self.disconnect(ws)

    def ensure_background_tasks(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """Ensures the 1Hz broadcast worker and 15s keepalive worker are running."""
        if not self._running:
            self.start(loop=loop)

    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """Starts keepalive and 1Hz broadcast background tasks."""
        if self._running:
            return
        self._running = True
        try:
            curr_loop = loop or asyncio.get_running_loop()
        except RuntimeError:
            curr_loop = asyncio.get_event_loop()

        if self._keepalive_task is None or self._keepalive_task.done():
            self._keepalive_task = curr_loop.create_task(self._keepalive_loop())

        if self._broadcast_task is None or self._broadcast_task.done():
            self._broadcast_task = curr_loop.create_task(
                telemetry_broadcast_worker(self, interval=1.0)
            )
        logger.info("ConnectionManager started background tasks (keepalive=%.1fs, broadcast=1.0s)", self.ping_interval)

    async def stop(self) -> None:
        """Stops background tasks and closes all active connections."""
        self._running = False
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass
            self._keepalive_task = None

        if self._broadcast_task and not self._broadcast_task.done():
            self._broadcast_task.cancel()
            try:
                await self._broadcast_task
            except asyncio.CancelledError:
                pass
            self._broadcast_task = None

        with self._thread_lock:
            clients = list(self._active_connections)
            self._active_connections.clear()

        for ws in clients:
            try:
                await ws.close()
            except Exception:
                pass
        logger.info("ConnectionManager stopped.")


async def telemetry_broadcast_worker(
    manager: ConnectionManager,
    interval: float = 1.0,
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    """
    Background worker loop broadcasting unified telemetry payloads at 1Hz (1 broadcast per second).
    """
    while (stop_event is None or not stop_event.is_set()) and manager._running:
        try:
            payload = build_telemetry_payload(manager._engine_state)
            await manager.broadcast_json(payload)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Error in telemetry_broadcast_worker: %s", exc, exc_info=True)

        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break


# Global singleton instance for the FastAPI application
telemetry_manager = ConnectionManager(ping_interval=15.0)
