"""
news/aggregator.py
==================
Real-time market news aggregator using Finviz RSS feeds.

Fetches the 10 most recent market headlines every 5 minutes during
market hours (09:25–16:05 EDT), maintaining a rolling 30-minute window.

Headlines are fed into the MacroGatekeeper's LLM prompt to give Gemini
real-time context — transforming it from a time-window reasoner into an
actual news-aware veto system.

Sources:
    https://finviz.com/news_feed.ashx           — General market news
    https://finviz.com/news_feed.ashx?v=3&t=SYM — Symbol-specific headlines

No API key required. Finviz RSS is publicly accessible.
"""

from __future__ import annotations

import logging
import threading
import time
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Deque, Dict, List, Optional

import pytz
import requests

logger = logging.getLogger(__name__)

_EDT = pytz.timezone("America/New_York")
_FETCH_INTERVAL_SEC   = 300   # Every 5 minutes
_WINDOW_MINUTES       = 30    # Keep headlines from the last 30 min
_MAX_HEADLINES        = 20    # Max total per fetch
_REQUEST_TIMEOUT_SEC  = 8

_FINVIZ_GENERAL_URL   = "https://finviz.com/news_feed.ashx"
_FINVIZ_SYMBOL_URL    = "https://finviz.com/news_feed.ashx?v=3&t={symbol}"

_MARKET_OPEN_H  = 9
_MARKET_OPEN_M  = 25   # Start fetching a few minutes before open
_MARKET_CLOSE_H = 16
_MARKET_CLOSE_M = 5


@dataclass
class Headline:
    """A single news headline with source and timestamp."""
    title:     str
    source:    str
    url:       str
    fetched_at: datetime   # Wall-clock UTC time when we retrieved this


class NewsAggregator:
    """
    Background polling aggregator for Finviz RSS market headlines.

    Lifecycle
    ---------
    1. Instantiate.
    2. Call start() to launch the background fetch thread.
    3. Call get_recent_headlines(symbol) before each LLM prompt.
    4. Call stop() on shutdown.

    Thread Safety
    -------------
    _headlines_general and _headlines_by_symbol use deque + threading.Lock.
    All public methods are safe to call from any thread.
    """

    def __init__(self, cfg: dict) -> None:
        news_cfg = cfg.get("news", {})
        self._enabled: bool = news_cfg.get("enabled", True)
        self._fetch_interval: int = int(news_cfg.get("fetch_interval_sec", _FETCH_INTERVAL_SEC))
        self._window_minutes: int = int(news_cfg.get("window_minutes", _WINDOW_MINUTES))

        # Track which symbols to pre-fetch headlines for
        universe_cfg = cfg.get("universe", {})
        self._tracked_symbols: List[str] = [
            s.upper() for s in universe_cfg.get("day_trade_candidates", [])
        ]

        # Rolling headline stores
        self._headlines_general: Deque[Headline] = deque(maxlen=_MAX_HEADLINES)
        self._headlines_by_symbol: Dict[str, Deque[Headline]] = {
            sym: deque(maxlen=10) for sym in self._tracked_symbols
        }

        self._lock        = threading.Lock()
        self._stop_event  = threading.Event()
        self._thread: Optional[threading.Thread] = None

        logger.info(
            "NewsAggregator initialised — symbols=%s, interval=%ds, window=%dm",
            self._tracked_symbols, self._fetch_interval, self._window_minutes,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background fetch thread."""
        if not self._enabled:
            logger.info("NewsAggregator: disabled by config — not starting.")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._fetch_loop,
            name="NewsAggregatorThread",
            daemon=True,
        )
        self._thread.start()
        logger.info("NewsAggregator: background thread started.")

    def stop(self) -> None:
        """Stop the background fetch thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("NewsAggregator: stopped.")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_recent_headlines(self, symbol: str, max_count: int = 5) -> str:
        """
        Return a formatted string of recent headlines for use in an LLM prompt.

        Combines symbol-specific headlines with recent general market news.
        Headlines older than window_minutes are excluded.

        Args:
            symbol:    Ticker symbol (e.g. "SOXL").
            max_count: Max number of headlines to return (default 5).

        Returns:
            Multi-line string suitable for embedding in a Gemini prompt.
            Returns "No recent news available." if the store is empty.
        """
        now = datetime.utcnow()
        cutoff_secs = self._window_minutes * 60

        with self._lock:
            sym = symbol.upper()
            candidates: List[Headline] = []

            # Symbol-specific first
            sym_store = self._headlines_by_symbol.get(sym, deque())
            for h in sym_store:
                age = (now - h.fetched_at.replace(tzinfo=None)).total_seconds()
                if age <= cutoff_secs:
                    candidates.append(h)

            # Fill remaining slots with general news
            for h in self._headlines_general:
                age = (now - h.fetched_at.replace(tzinfo=None)).total_seconds()
                if age <= cutoff_secs and len(candidates) < max_count:
                    candidates.append(h)

        if not candidates:
            return "No recent news available."

        lines = [f"- [{h.source}] {h.title}" for h in candidates[:max_count]]
        return "\n".join(lines)

    def get_all_recent(self) -> List[Headline]:
        """Return all recent headlines (for API/dashboard consumption)."""
        now = datetime.utcnow()
        cutoff_secs = self._window_minutes * 60
        with self._lock:
            result = []
            for h in self._headlines_general:
                age = (now - h.fetched_at.replace(tzinfo=None)).total_seconds()
                if age <= cutoff_secs:
                    result.append(h)
            return result

    # ------------------------------------------------------------------
    # Background fetch loop
    # ------------------------------------------------------------------

    def _fetch_loop(self) -> None:
        """Background thread: fetch headlines every N seconds during market hours."""
        logger.info("NewsAggregator: fetch loop started.")
        # Fetch immediately on first start
        self._fetch_all()

        while not self._stop_event.wait(timeout=self._fetch_interval):
            if self._is_market_hours():
                self._fetch_all()
            else:
                logger.debug("NewsAggregator: outside market hours — skipping fetch.")

    def _is_market_hours(self) -> bool:
        """Return True if current EDT time is within the fetch window."""
        now = datetime.now(_EDT)
        now_sec = now.hour * 3600 + now.minute * 60 + now.second
        open_sec  = _MARKET_OPEN_H  * 3600 + _MARKET_OPEN_M  * 60
        close_sec = _MARKET_CLOSE_H * 3600 + _MARKET_CLOSE_M * 60
        return open_sec <= now_sec <= close_sec

    def _fetch_all(self) -> None:
        """Fetch general + per-symbol headlines."""
        self._fetch_feed(_FINVIZ_GENERAL_URL, symbol=None)
        for sym in self._tracked_symbols:
            url = _FINVIZ_SYMBOL_URL.format(symbol=sym)
            self._fetch_feed(url, symbol=sym)
            time.sleep(0.5)  # Polite delay between symbol fetches

    def _fetch_feed(self, url: str, symbol: Optional[str]) -> None:
        """
        Fetch and parse a single Finviz RSS feed URL.

        Args:
            url:    RSS endpoint.
            symbol: Symbol string if symbol-specific; None for general feed.
        """
        try:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (compatible; SchwabEngine/1.0; "
                    "+https://github.com/schwabengine)"
                )
            }
            resp = requests.get(url, headers=headers, timeout=_REQUEST_TIMEOUT_SEC)
            resp.raise_for_status()

            root = ET.fromstring(resp.text)
            channel = root.find("channel")
            if channel is None:
                return

            fetched_at = datetime.utcnow()
            new_headlines: List[Headline] = []

            for item in channel.findall("item")[:_MAX_HEADLINES]:
                title = (item.findtext("title") or "").strip()
                link  = (item.findtext("link")  or "").strip()
                src_el = item.find("{http://purl.org/rss/1.0/modules/content/}encoded")
                source = "Finviz"
                if src_el is not None and src_el.text:
                    source = src_el.text[:40]

                if title:
                    new_headlines.append(Headline(
                        title=title,
                        source=source,
                        url=link,
                        fetched_at=fetched_at,
                    ))

            with self._lock:
                if symbol is None:
                    for h in new_headlines:
                        self._headlines_general.appendleft(h)
                else:
                    sym = symbol.upper()
                    if sym not in self._headlines_by_symbol:
                        self._headlines_by_symbol[sym] = deque(maxlen=10)
                    for h in new_headlines:
                        self._headlines_by_symbol[sym].appendleft(h)

            logger.debug(
                "NewsAggregator: fetched %d headlines from %s",
                len(new_headlines), url[:60],
            )

        except requests.RequestException as exc:
            logger.warning("NewsAggregator: fetch failed for %s: %s", url[:60], exc)
        except ET.ParseError as exc:
            logger.warning("NewsAggregator: RSS parse error for %s: %s", url[:60], exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("NewsAggregator: unexpected error for %s: %s", url[:60], exc)
