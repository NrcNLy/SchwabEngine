"""
data/streamer.py
================
Level 1 WebSocket streamer for Schwab real-time market data.

Implements the two-phase Schwab streaming protocol exactly as specified:

  Phase A — ADMIN LOGIN
      Fetch streamer credentials from GET /trader/v1/userPreference,
      then send an ADMIN/LOGIN request frame.

  Phase B — LEVELONE_EQUITIES subscription
      Subscribe to fields 0,1,2,3,4,5,8,10,11 for the active symbol universe.

The streamer runs its own asyncio event loop in a dedicated background thread,
publishing parsed tick data via a thread-safe callback.

Configuration sources (config.yaml):
    streamer.url                       → "wss://streamer-api.schwabapi.com/ws"
    streamer.user_preference_endpoint  → "/trader/v1/userPreference"
    streamer.levelone_equities_fields  → "0,1,2,3,4,5,8,10,11"
    api.trader_base                    → used to build full preference URL
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import websockets
from websockets.exceptions import ConnectionClosedError, WebSocketException

from data.book import BookSnapshot, parse_book_frame
from data.rest_client import SchwabRestClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Field index → human-readable name mapping (LEVELONE_EQUITIES)
# ---------------------------------------------------------------------------
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

# Reconnection backoff parameters
_RECONNECT_INITIAL_SEC = 2.0
_RECONNECT_MAX_SEC     = 60.0
_RECONNECT_FACTOR      = 2.0

# Request ID counter (monotonically increasing, scoped to session)
_REQUEST_ID_COUNTER = 0


def _next_request_id() -> int:
    global _REQUEST_ID_COUNTER
    _REQUEST_ID_COUNTER += 1
    return _REQUEST_ID_COUNTER


# ===========================================================================
# SchwabStreamer
# ===========================================================================

class SchwabStreamer:
    """
    Asynchronous Level 1 WebSocket streamer for Schwab market data.

    The streamer manages the full connection lifecycle:
        connect → login → subscribe → stream → reconnect on failure

    Tick data is delivered to the caller via a thread-safe `on_tick` callback:
        on_tick(symbol: str, fields: dict) -> None

    Usage
    -----
        def handle_tick(symbol: str, fields: dict) -> None:
            print(symbol, fields["last_price"])

        streamer = SchwabStreamer.from_config(cfg, rest_client)
        streamer.set_symbols(["SOXL", "TQQQ", "TNA"])
        streamer.on_tick = handle_tick
        streamer.start()
        ...
        streamer.stop()
    """

    def __init__(
        self,
        ws_url: str,
        fields: str,
        rest_client: SchwabRestClient,
    ) -> None:
        """
        Args:
            ws_url:       WebSocket endpoint, e.g. "wss://streamer-api.schwabapi.com/ws"
            fields:       Comma-separated LEVELONE_EQUITIES field indices.
            rest_client:  Initialised SchwabRestClient (for fetching credentials).
        """
        self._ws_url      = ws_url
        self._fields      = fields
        self._rest_client = rest_client

        self._symbols:  List[str]                         = []
        self.on_tick:   Optional[Callable[[str, dict], None]] = None
        self.on_book:   Optional[Callable[[BookSnapshot], None]] = None

        # Microstructure feeds (optional): Level 2 books and futures reference quotes
        self._book_symbols:   List[str] = []
        self._book_services:  List[str] = []
        self._futures_symbols: List[str] = []
        self._client_ids: Dict[str, str] = {"SchwabClientCustomerId": "", "SchwabClientCorrelId": ""}
        self._pending_labels: Dict[str, str] = {}

        self._loop:   Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread]          = None
        self._stop_event = threading.Event()
        self._running    = False

        logger.info(
            "SchwabStreamer initialised — ws_url=%s, fields=%s",
            self._ws_url, self._fields,
        )

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        cfg: dict,
        rest_client: SchwabRestClient,
    ) -> "SchwabStreamer":
        """
        Build a SchwabStreamer from the loaded config.yaml dict.

        Args:
            cfg:         Top-level config dict.
            rest_client: Initialised SchwabRestClient.
        """
        streamer_cfg = cfg.get("streamer", {})
        return cls(
            ws_url=streamer_cfg.get(
                "url", "wss://streamer-api.schwabapi.com/ws"
            ),
            fields=streamer_cfg.get(
                "levelone_equities_fields", "0,1,2,3,4,5,8,10,11"
            ),
            rest_client=rest_client,
        )

    # ------------------------------------------------------------------
    # Configuration setters
    # ------------------------------------------------------------------

    def set_symbols(self, symbols: List[str]) -> None:
        """
        Set (or replace) the equity universe to subscribe to.
        Must be called before start(), or triggers a resubscription if
        the stream is already live.
        """
        self._symbols = list(symbols)
        logger.info("SchwabStreamer: symbol universe set → %s", self._symbols)

    def set_book_subscriptions(self, symbols: List[str], services: Optional[List[str]] = None) -> None:
        """Configure symbols and services for Level 2 book subscriptions."""
        self._book_symbols = list(symbols)
        self._book_services = list(services or ["NASDAQ_BOOK", "NYSE_BOOK"])
        logger.info("SchwabStreamer: book subscriptions set → %s across %s", self._book_symbols, self._book_services)

    def set_futures_symbols(self, symbols: List[str]) -> None:
        """Configure symbols for Level 1 futures subscriptions (e.g. /ZN, /NQ)."""
        self._futures_symbols = list(symbols)
        logger.info("SchwabStreamer: futures symbols set → %s", self._futures_symbols)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Launch the streaming loop in a dedicated background thread."""
        if self._running:
            logger.warning("SchwabStreamer: already running — ignoring start().")
            return

        self._stop_event.clear()
        self._running = True
        self._thread  = threading.Thread(
            target=self._run_event_loop,
            name="SchwabStreamerThread",
            daemon=True,
        )
        self._thread.start()
        logger.info("SchwabStreamer: background thread started.")

    def stop(self) -> None:
        """Signal the streamer to disconnect and stop."""
        logger.info("SchwabStreamer: stop requested.")
        self._stop_event.set()
        self._running = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("SchwabStreamer: stopped.")

    # ------------------------------------------------------------------
    # Event loop runner (runs in background thread)
    # ------------------------------------------------------------------

    def _run_event_loop(self) -> None:
        """Entry point for the background thread; owns the asyncio event loop."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connection_loop())
        except Exception as exc:  # noqa: BLE001
            logger.exception("SchwabStreamer: event loop exited with error: %s", exc)
        finally:
            self._loop.close()
            self._running = False

    # ------------------------------------------------------------------
    # Core async streaming logic
    # ------------------------------------------------------------------

    async def _connection_loop(self) -> None:
        """
        Outer reconnection loop with exponential backoff.
        Runs until stop() is called or a fatal, unrecoverable error occurs.
        """
        backoff = _RECONNECT_INITIAL_SEC

        while not self._stop_event.is_set():
            try:
                logger.info("SchwabStreamer: fetching streamer credentials…")
                streamer_info = self._fetch_streamer_credentials()

                ws_url = str(streamer_info.get("streamerSocketUrl") or self._ws_url)
                logger.info("SchwabStreamer: connecting to %s", ws_url)
                async with websockets.connect(
                    ws_url,
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    # === Phase A: ADMIN LOGIN ===
                    await self._send_admin_login(ws, streamer_info)
                    login_response = await self._recv_and_validate(ws, "LOGIN")
                    if not login_response:
                        logger.error("SchwabStreamer: LOGIN rejected. Reconnecting…")
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * _RECONNECT_FACTOR, _RECONNECT_MAX_SEC)
                        continue

                    logger.info("SchwabStreamer: LOGIN successful.")
                    backoff = _RECONNECT_INITIAL_SEC  # Reset on successful connection

                    # === Phase B: Subscriptions ===
                    self._client_ids = {
                        "SchwabClientCustomerId": streamer_info.get("schwabClientCustomerId", ""),
                        "SchwabClientCorrelId": streamer_info.get("schwabClientCorrelId", ""),
                    }
                    await self._send_levelone_subscription(ws)
                    sub_response = await self._recv_and_validate(ws, "SUBS")
                    if not sub_response:
                        logger.error("SchwabStreamer: SUBS rejected. Reconnecting…")
                        continue

                    logger.info(
                        "SchwabStreamer: LEVELONE_EQUITIES subscription active for %s",
                        self._symbols,
                    )

                    # Level 2 Books and Futures Subscriptions (Microstructure)
                    await self._send_book_subscriptions(ws)
                    await self._send_futures_subscriptions(ws)

                    # === Streaming loop ===
                    await self._stream(ws)

            except ConnectionClosedError as exc:
                logger.warning(
                    "SchwabStreamer: WebSocket closed unexpectedly (%s). "
                    "Reconnecting in %.1fs…", exc, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * _RECONNECT_FACTOR, _RECONNECT_MAX_SEC)

            except WebSocketException as exc:
                logger.error(
                    "SchwabStreamer: WebSocket error: %s. Reconnecting in %.1fs…",
                    exc, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * _RECONNECT_FACTOR, _RECONNECT_MAX_SEC)

            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "SchwabStreamer: unexpected error: %s. Reconnecting in %.1fs…",
                    exc, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * _RECONNECT_FACTOR, _RECONNECT_MAX_SEC)

    async def _stream(self, ws: websockets.WebSocketClientProtocol) -> None:
        """
        Receive and dispatch tick messages until the connection drops or
        stop() is called.
        """
        while not self._stop_event.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=30)
                self._parse_and_dispatch(raw)
            except asyncio.TimeoutError:
                # No message in 30s — send a heartbeat ping
                await ws.ping()
                logger.debug("SchwabStreamer: heartbeat ping sent.")
            except ConnectionClosedError:
                raise   # Propagate to connection_loop for reconnect

    # ------------------------------------------------------------------
    # Schwab streaming protocol helpers
    # ------------------------------------------------------------------

    def _fetch_streamer_credentials(self) -> Dict[str, Any]:
        """
        Call GET /trader/v1/userPreference to retrieve streamer credentials.

        Spec: The response contains a `streamerInfo` array with:
            schwabClientCustomerId, schwabClientCorrelId,
            streamerSocketUrl, schwabClientChannel, schwabClientFunctionId
        """
        prefs = self._rest_client.get_user_preferences()

        # Schwab returns a list under "streamerInfo"
        streamer_info_list = prefs.get("streamerInfo", [])
        if not streamer_info_list:
            raise RuntimeError(
                "SchwabStreamer: userPreference response missing 'streamerInfo'."
            )
        return streamer_info_list[0]

    async def _send_admin_login(
        self,
        ws: websockets.WebSocketClientProtocol,
        streamer_info: Dict[str, Any],
    ) -> None:
        """
        Send the ADMIN/LOGIN request frame.

        Spec: The LOGIN request uses credentials from userPreference:
            schwabClientCustomerId → "userid"
            schwabClientCorrelId   → "token"
            schwabClientChannel    → "channel"
            schwabClientFunctionId → "functionId"
        """
        login_request = {
            "requests": [
                {
                    "service":    "ADMIN",
                    "requestid":  str(_next_request_id()),
                    "command":    "LOGIN",
                    "SchwabClientCustomerId": streamer_info["schwabClientCustomerId"],
                    "SchwabClientCorrelId":   streamer_info["schwabClientCorrelId"],
                    "parameters": {
                        "Authorization": self._rest_client._auth_manager.get_access_token(),
                        "SchwabClientChannel":    streamer_info["schwabClientChannel"],
                        "SchwabClientFunctionId": streamer_info["schwabClientFunctionId"],
                    },
                }
            ]
        }
        await ws.send(json.dumps(login_request))
        logger.debug("SchwabStreamer: ADMIN/LOGIN frame sent.")

    async def _send_levelone_subscription(
        self,
        ws: websockets.WebSocketClientProtocol,
    ) -> None:
        """
        Send the LEVELONE_EQUITIES/SUBS request frame.

        Spec: Subscribe to fields 0,1,2,3,4,5,8,10,11 for the symbol universe.
        """
        if not self._symbols:
            logger.warning(
                "SchwabStreamer: no symbols set — LEVELONE_EQUITIES subscription skipped."
            )
            return

        subs_request = {
            "requests": [
                {
                    "service":    "LEVELONE_EQUITIES",
                    "requestid":  str(_next_request_id()),
                    "command":    "SUBS",
                    "SchwabClientCustomerId": self._client_ids.get("SchwabClientCustomerId", ""),
                    "SchwabClientCorrelId":   self._client_ids.get("SchwabClientCorrelId", ""),
                    "parameters": {
                        "keys":   ",".join(self._symbols),
                        "fields": self._fields,
                    },
                }
            ]
        }
        await ws.send(json.dumps(subs_request))
        logger.debug(
            "SchwabStreamer: LEVELONE_EQUITIES/SUBS frame sent for %s (fields=%s).",
            self._symbols, self._fields,
        )

    async def _send_book_subscriptions(
        self,
        ws: websockets.WebSocketClientProtocol,
    ) -> None:
        """Send book subscription requests for configured book services."""
        if not self._book_symbols or not self._book_services:
            return
        requests = []
        for service in self._book_services:
            rid = str(_next_request_id())
            requests.append({
                "service": service,
                "requestid": rid,
                "command": "SUBS",
                "SchwabClientCustomerId": self._client_ids.get("SchwabClientCustomerId", ""),
                "SchwabClientCorrelId": self._client_ids.get("SchwabClientCorrelId", ""),
                "parameters": {
                    "keys": ",".join(self._book_symbols),
                    "fields": "0,1,2,3",
                },
            })
        if requests:
            await ws.send(json.dumps({"requests": requests}))
            logger.debug("SchwabStreamer: book subscription frames sent for %s across %s",
                         self._book_symbols, self._book_services)

    async def _send_futures_subscriptions(
        self,
        ws: websockets.WebSocketClientProtocol,
    ) -> None:
        """Send LEVELONE_FUTURES subscription frame if futures symbols configured."""
        if not self._futures_symbols:
            return
        rid = str(_next_request_id())
        req = {
            "requests": [
                {
                    "service": "LEVELONE_FUTURES",
                    "requestid": rid,
                    "command": "SUBS",
                    "SchwabClientCustomerId": self._client_ids.get("SchwabClientCustomerId", ""),
                    "SchwabClientCorrelId": self._client_ids.get("SchwabClientCorrelId", ""),
                    "parameters": {
                        "keys": ",".join(self._futures_symbols),
                        "fields": "0,1,2,3,4,5,8,9",
                    },
                }
            ]
        }
        await ws.send(json.dumps(req))
        logger.debug("SchwabStreamer: LEVELONE_FUTURES subscription sent for %s", self._futures_symbols)

    async def _recv_and_validate(
        self,
        ws: websockets.WebSocketClientProtocol,
        expected_command: str,
        timeout: float = 10.0,
    ) -> bool:
        """
        Wait for a response frame and check that it indicates success (code 0).

        Args:
            ws:               Active WebSocket connection.
            expected_command: "LOGIN" or "SUBS" — used for log messages only.
            timeout:          Seconds to wait for the response.

        Returns:
            True  — server acknowledged with code 0.
            False — non-zero code or timeout.
        """
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.error(
                "SchwabStreamer: timed out waiting for %s response.", expected_command
            )
            return False

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.error("SchwabStreamer: malformed %s response: %s", expected_command, exc)
            return False

        # Schwab acknowledgements arrive in a "response" array
        responses = msg.get("response", [])
        for resp in responses:
            content = resp.get("content", {})
            code    = content.get("code", -1)
            if code == 0:
                return True
            else:
                logger.error(
                    "SchwabStreamer: %s rejected — code=%d, msg=%s",
                    expected_command, code, content.get("msg", ""),
                )
                return False

        # Also accept data frames that arrive immediately after LOGIN
        if "data" in msg or "notify" in msg:
            return True

        logger.warning(
            "SchwabStreamer: unexpected %s response structure: %s",
            expected_command, msg,
        )
        return False

    # ------------------------------------------------------------------
    # Tick parsing and dispatch
    # ------------------------------------------------------------------

    def _parse_and_dispatch(self, raw: str) -> None:
        """
        Parse a raw WebSocket message and dispatch tick data.

        Schwab sends two message types in the stream:
          "data"   — array of service updates with content arrays (actual ticks)
          "notify" — heartbeat frames (ignored here)

        Each content item within a data frame is a dict of {field_index: value},
        keyed numerically. This method translates those indices using FIELD_MAP
        and calls self.on_tick(symbol, fields).
        """
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("SchwabStreamer: could not parse message: %s", exc)
            return

        # Heartbeat
        if "notify" in msg:
            logger.debug("SchwabStreamer: heartbeat received.")
            return

        data_frames = msg.get("data", [])
        for frame in data_frames:
            service = frame.get("service", "")
            if service in ("NASDAQ_BOOK", "NYSE_BOOK"):
                if self.on_book:
                    for content in frame.get("content", []):
                        snap = parse_book_frame(content, venue=service)
                        if snap is not None:
                            try:
                                self.on_book(snap)
                            except Exception as exc:  # noqa: BLE001
                                logger.exception("SchwabStreamer: on_book callback raised for %s: %s", snap.symbol, exc)
                continue

            if service not in ("LEVELONE_EQUITIES", "LEVELONE_FUTURES"):
                continue

            for content in frame.get("content", []):
                symbol = content.get("key", content.get("0", ""))
                if not symbol:
                    continue

                fields: Dict[str, Any] = {}
                for idx_str, value in content.items():
                    try:
                        idx = int(idx_str)
                    except (ValueError, TypeError):
                        # "key" is a string field — already captured above
                        continue
                    field_name = FIELD_MAP.get(idx)
                    if field_name:
                        fields[field_name] = value

                if fields and self.on_tick:
                    try:
                        self.on_tick(symbol, fields)
                    except Exception as exc:  # noqa: BLE001
                        logger.exception(
                            "SchwabStreamer: on_tick callback raised for %s: %s",
                            symbol, exc,
                        )
