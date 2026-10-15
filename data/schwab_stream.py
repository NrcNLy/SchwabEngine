"""
data/schwab_stream.py
=====================
WebSocket Stream Supervisor for Charles Schwab Level 1 streaming feed.

Features:
- Automatic reconnection with exponential backoff (1s, 2s, 4s, 8s, 16s, up to 30s).
- Active streaming heartbeat pings every 20 seconds.
- Subscribes to LEVELONE_EQUITIES for core high-beta universe (SOXL, TQQQ, TNA, SOXX, QQQ).
- Directly pipes incoming raw tick streams to the high-performance zero-allocation BarResampler.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

try:
    import websockets
    from websockets.exceptions import ConnectionClosedError, WebSocketException
except ImportError:
    websockets = None  # type: ignore
    ConnectionClosedError = Exception  # type: ignore
    WebSocketException = Exception  # type: ignore

from data.stream_resampler import BarResampler, DEFAULT_TARGET_TICKERS

logger = logging.getLogger(__name__)

# Reconnection parameters
INITIAL_BACKOFF_SEC: float = 1.0
MAX_BACKOFF_SEC: float = 30.0
BACKOFF_FACTOR: float = 2.0
HEARTBEAT_INTERVAL_SEC: float = 20.0

# LEVELONE_EQUITIES Field Indices
FIELD_MAP: Dict[int, str] = {
    0:  "symbol",
    1:  "bid_price",
    2:  "ask_price",
    3:  "last_price",
    4:  "bid_size",
    5:  "ask_size",
    8:  "total_volume",
    9:  "last_size",
    10: "high_price",
    11: "low_price",
}

_REQUEST_ID_COUNTER = 0


def _next_request_id() -> int:
    global _REQUEST_ID_COUNTER
    _REQUEST_ID_COUNTER += 1
    return _REQUEST_ID_COUNTER


class SchwabStreamSupervisor:
    """
    Supervises Schwab WebSocket streaming connection with resilient reconnects,
    heartbeats, and direct pipelining to BarResampler.
    """

    def __init__(
        self,
        resampler: Optional[BarResampler] = None,
        symbols: Optional[Sequence[str]] = None,
        ws_url: str = "wss://streamer-api.schwabapi.com/ws",
        rest_client: Optional[Any] = None,
        auth_manager: Optional[Any] = None,
        initial_backoff: float = INITIAL_BACKOFF_SEC,
        max_backoff: float = MAX_BACKOFF_SEC,
        backoff_factor: float = BACKOFF_FACTOR,
        heartbeat_interval: float = HEARTBEAT_INTERVAL_SEC,
    ) -> None:
        self.symbols: List[str] = list(symbols) if symbols is not None else list(DEFAULT_TARGET_TICKERS)
        self.resampler: BarResampler = resampler if resampler is not None else BarResampler(symbols=self.symbols)
        self.ws_url = str(ws_url)
        self.rest_client = rest_client
        self.auth_manager = auth_manager

        # Exponential backoff config
        self.initial_backoff = float(initial_backoff)
        self.max_backoff = float(max_backoff)
        self.backoff_factor = float(backoff_factor)
        self.current_backoff = self.initial_backoff
        self.heartbeat_interval = float(heartbeat_interval)

        # Telemetry & callbacks
        self.reconnect_count: int = 0
        self.ticks_processed: int = 0
        self.is_connected: bool = False
        self.on_tick: Optional[Callable[[str, dict], None]] = None

        # Threading / event loop management
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._running = False
        self._active_ws: Any = None

    @classmethod
    def from_config(
        cls,
        cfg: dict,
        rest_client: Optional[Any] = None,
        resampler: Optional[BarResampler] = None,
    ) -> "SchwabStreamSupervisor":
        """Factory method to instantiate supervisor from config.yaml dictionary."""
        streamer_cfg = cfg.get("streamer", {}) or {}
        ws_url = streamer_cfg.get("url", "wss://streamer-api.schwabapi.com/ws")
        symbols = streamer_cfg.get("target_symbols", DEFAULT_TARGET_TICKERS)
        return cls(
            resampler=resampler,
            symbols=symbols,
            ws_url=ws_url,
            rest_client=rest_client,
        )

    # ------------------------------------------------------------------
    # Backoff Calculations
    # ------------------------------------------------------------------

    def compute_next_backoff(self) -> float:
        """Compute the next exponential backoff sleep interval and advance state."""
        backoff = self.current_backoff
        self.current_backoff = min(self.current_backoff * self.backoff_factor, self.max_backoff)
        self.reconnect_count += 1
        return backoff

    def reset_backoff(self) -> None:
        """Reset exponential backoff to initial interval upon successful connection."""
        self.current_backoff = self.initial_backoff

    @staticmethod
    def get_backoff_schedule(attempts: int = 6, initial: float = 1.0, factor: float = 2.0, max_val: float = 30.0) -> List[float]:
        """Compute the deterministic backoff schedule sequence for testing."""
        schedule = []
        cur = initial
        for _ in range(attempts):
            schedule.append(cur)
            cur = min(cur * factor, max_val)
        return schedule

    # ------------------------------------------------------------------
    # Frame Builders
    # ------------------------------------------------------------------

    def build_admin_login_payload(self, streamer_info: Dict[str, Any]) -> Dict[str, Any]:
        """Construct Schwab ADMIN/LOGIN JSON request frame."""
        return {
            "service": "ADMIN",
            "requestid": _next_request_id(),
            "command": "LOGIN",
            "SchwabClientCustomerId": streamer_info.get("schwabClientCustomerId", ""),
            "SchwabClientCorrelId": streamer_info.get("schwabClientCorrelId", ""),
            "parameters": {
                "credential": streamer_info.get("schwabClientFunctionId", ""),
                "token": streamer_info.get("schwabClientChannel", ""),
                "version": "1.0",
            },
        }

    def build_levelone_sub_payload(self, symbols: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        """Construct LEVELONE_EQUITIES subscription payload."""
        sym_list = list(symbols) if symbols is not None else self.symbols
        return {
            "service": "LEVELONE_EQUITIES",
            "requestid": _next_request_id(),
            "command": "SUBS",
            "parameters": {
                "keys": ",".join(s.upper() for s in sym_list),
                "fields": "0,1,2,3,4,5,8,9,10,11",
            },
        }

    # ------------------------------------------------------------------
    # Message Dispatcher & Resampler Pipeline
    # ------------------------------------------------------------------

    def handle_message(self, raw_message: Union[str, Dict[str, Any]]) -> int:
        """
        Parse raw incoming WebSocket frame and pipe tick data directly to BarResampler.
        Returns the count of ticks processed from this frame.
        """
        if isinstance(raw_message, str):
            try:
                msg = json.loads(raw_message)
            except json.JSONDecodeError as exc:
                logger.warning("SchwabStreamSupervisor: failed to parse JSON frame: %s", exc)
                return 0
        else:
            msg = raw_message

        # Handle heartbeat notifications
        if "notify" in msg:
            logger.debug("SchwabStreamSupervisor: heartbeat notification received.")
            return 0

        ticks_count = 0
        data_frames = msg.get("data", [])
        for frame in data_frames:
            service = frame.get("service", "")
            if service != "LEVELONE_EQUITIES":
                continue

            frame_ts = frame.get("timestamp", int(time.time() * 1000))

            for content in frame.get("content", []):
                symbol = content.get("key", content.get("0", ""))
                if not symbol:
                    continue

                # Parse mapped fields
                fields: Dict[str, Any] = {}
                for idx_str, value in content.items():
                    try:
                        idx = int(idx_str)
                    except (ValueError, TypeError):
                        continue
                    field_name = FIELD_MAP.get(idx)
                    if field_name:
                        fields[field_name] = value

                # Determine last price and volume
                last_price = None
                if "last_price" in fields:
                    last_price = float(fields["last_price"])
                elif "3" in content:
                    last_price = float(content["3"])
                elif "bid_price" in fields:
                    last_price = float(fields["bid_price"])

                if last_price is None or last_price <= 0:
                    continue

                # Determine trade volume: prefer last_size, fallback to total_volume or 0
                vol = 0.0
                if "last_size" in fields:
                    vol = float(fields["last_size"])
                elif "9" in content:
                    vol = float(content["9"])
                elif "total_volume" in fields:
                    vol = float(fields["total_volume"])
                elif "8" in content:
                    vol = float(content["8"])

                # Pipe directly to zero-allocation BarResampler
                self.resampler.on_tick(
                    symbol=symbol,
                    price=last_price,
                    volume=vol,
                    timestamp=frame_ts,
                )
                ticks_count += 1
                self.ticks_processed += 1

                # Forward to external on_tick callback if configured
                if self.on_tick is not None:
                    try:
                        self.on_tick(symbol, fields)
                    except Exception as exc:
                        logger.exception("SchwabStreamSupervisor: on_tick callback error: %s", exc)

        return ticks_count

    # ------------------------------------------------------------------
    # Lifecycle Management
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Launch background streaming supervisor thread."""
        if self._running:
            logger.warning("SchwabStreamSupervisor: already running.")
            return

        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            name="SchwabStreamSupervisorThread",
            daemon=True,
        )
        self._thread.start()
        logger.info("SchwabStreamSupervisor: background thread started.")

    def stop(self) -> None:
        """Signal supervisor to stop and disconnect."""
        logger.info("SchwabStreamSupervisor: stopping...")
        self._stop_event.set()
        self._running = False
        self.is_connected = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        logger.info("SchwabStreamSupervisor: stopped.")

    def _run_loop(self) -> None:
        """Entry point for background thread owning dedicated asyncio event loop."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self.run_supervisor())
        except Exception as exc:
            logger.exception("SchwabStreamSupervisor: event loop crashed: %s", exc)
        finally:
            self._loop.close()
            self._running = False
            self.is_connected = False

    async def run_supervisor(self) -> None:
        """
        Main connection supervisor loop with exponential backoff and heartbeat pings.
        """
        while not self._stop_event.is_set():
            try:
                # 1. Fetch credentials if rest_client available
                streamer_info = self._fetch_streamer_info()
                url = str(streamer_info.get("streamerSocketUrl") or self.ws_url)

                if websockets is None:
                    logger.error("SchwabStreamSupervisor: websockets library not available.")
                    break

                logger.info("SchwabStreamSupervisor: connecting to %s...", url)
                async with websockets.connect(
                    url,
                    ping_interval=None,  # Handled explicitly by supervisor heartbeat task
                    ping_timeout=10.0,
                ) as ws:
                    self._active_ws = ws
                    self.is_connected = True
                    self.reset_backoff()

                    # Phase A: Login
                    login_frame = self.build_admin_login_payload(streamer_info)
                    await ws.send(json.dumps(login_frame))

                    # Phase B: Subscription
                    sub_frame = self.build_levelone_sub_payload(self.symbols)
                    await ws.send(json.dumps(sub_frame))
                    logger.info("SchwabStreamSupervisor: subscribed to %s", self.symbols)

                    # Spawn 20-second heartbeat ping worker
                    heartbeat_task = asyncio.create_task(self._heartbeat_worker(ws))

                    # Ingestion loop
                    try:
                        while not self._stop_event.is_set():
                            msg = await ws.recv()
                            self.handle_message(msg)
                    finally:
                        heartbeat_task.cancel()
                        try:
                            await heartbeat_task
                        except asyncio.CancelledError:
                            pass

            except ConnectionClosedError as exc:
                self.is_connected = False
                backoff = self.compute_next_backoff()
                logger.warning(
                    "SchwabStreamSupervisor: WebSocket closed (%s). Reconnecting in %.1fs (attempt %d)...",
                    exc, backoff, self.reconnect_count,
                )
                await asyncio.sleep(backoff)

            except WebSocketException as exc:
                self.is_connected = False
                backoff = self.compute_next_backoff()
                logger.error(
                    "SchwabStreamSupervisor: WebSocket error: %s. Reconnecting in %.1fs...",
                    exc, backoff,
                )
                await asyncio.sleep(backoff)

            except Exception as exc:
                self.is_connected = False
                backoff = self.compute_next_backoff()
                logger.exception(
                    "SchwabStreamSupervisor: unexpected connection error: %s. Reconnecting in %.1fs...",
                    exc, backoff,
                )
                await asyncio.sleep(backoff)

    async def _heartbeat_worker(self, ws: Any) -> None:
        """Transmit streaming heartbeat ping frames every 20 seconds."""
        try:
            while not self._stop_event.is_set():
                await asyncio.sleep(self.heartbeat_interval)
                if ws and hasattr(ws, "ping"):
                    await ws.ping()
                    logger.debug("SchwabStreamSupervisor: heartbeat ping sent (20s interval).")
        except asyncio.CancelledError:
            pass

    def _fetch_streamer_info(self) -> Dict[str, Any]:
        """Fetch credentials from userPreference if available."""
        if self.rest_client is not None and hasattr(self.rest_client, "get_user_preferences"):
            try:
                prefs = self.rest_client.get_user_preferences()
                info_list = prefs.get("streamerInfo", [])
                if info_list:
                    return info_list[0]
            except Exception as exc:
                logger.warning("SchwabStreamSupervisor: could not fetch streamerInfo: %s", exc)
        return {
            "streamerSocketUrl": self.ws_url,
            "schwabClientCustomerId": "DEFAULT_CUSTOMER",
            "schwabClientCorrelId": "DEFAULT_CORREL",
            "schwabClientChannel": "DEFAULT_CHANNEL",
            "schwabClientFunctionId": "DEFAULT_FUNC",
        }
