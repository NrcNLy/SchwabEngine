"""
data/book.py
============
Pure (no I/O) Level 2 order-book types and the Schwab BOOK frame parser used by the
microstructure engine.

Schwab ``NASDAQ_BOOK`` / ``NYSE_BOOK`` content items carry the full book per update:

    key : symbol
    1   : market snapshot time (ms)
    2   : bid side  -> list of {0: price, 1: aggregate size, 2: number of orders, 3: [per-exchange bids]}
    3   : ask side  -> same layout

``parse_book_frame`` is deliberately tolerant of the per-level key type (``"0"`` vs ``0``) and of
levels arriving unsorted. It never invents levels: if a side is empty the snapshot is rejected.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Tuple

MAX_LEVELS = 5

Level = Tuple[float, float]  # (price, size)


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    symbol: str
    ts_ms: int
    bids: Tuple[Level, ...]   # best -> worse, descending price
    asks: Tuple[Level, ...]   # best -> worse, ascending price
    venue: str = ""           # originating book service (NASDAQ_BOOK / NYSE_BOOK); books are per-venue

    @property
    def mid(self) -> float:
        return (self.bids[0][0] + self.asks[0][0]) / 2.0

    @property
    def spread(self) -> float:
        return self.asks[0][0] - self.bids[0][0]

    def depth(self, levels: int = MAX_LEVELS) -> float:
        return sum(s for _, s in self.bids[:levels]) + sum(s for _, s in self.asks[:levels])


def _get(d: Dict[Any, Any], key: int) -> Any:
    if key in d:
        return d[key]
    return d.get(str(key))


def _finite(x: Any) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _parse_side(raw: Any, descending: bool, max_levels: int) -> Tuple[Level, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    levels = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        px, sz = _finite(_get(item, 0)), _finite(_get(item, 1))
        if px is None or sz is None or px <= 0 or sz < 0:
            continue
        levels.append((px, sz))
    levels.sort(key=lambda lv: lv[0], reverse=descending)
    return tuple(levels[:max_levels])


def is_valid(snap: BookSnapshot) -> bool:
    """True when both sides are populated, strictly monotone, and the book is not locked or crossed."""
    if not snap.bids or not snap.asks:
        return False
    if snap.asks[0][0] <= snap.bids[0][0]:
        return False
    for side, descending in ((snap.bids, True), (snap.asks, False)):
        for (p0, _), (p1, _) in zip(side, side[1:]):
            if (p1 >= p0) if descending else (p1 <= p0):
                return False
    return True


def parse_book_frame(content: Dict[str, Any], now_ms: Optional[int] = None,
                     max_levels: int = MAX_LEVELS, venue: str = "") -> Optional[BookSnapshot]:
    """Parses one Schwab BOOK content item; returns ``None`` when it has no usable two-sided book."""
    symbol = content.get("key") or _get(content, 0)
    if not symbol:
        return None
    ts = _finite(_get(content, 1))
    if ts is None:
        ts = float(now_ms) if now_ms is not None else 0.0
    bids = _parse_side(_get(content, 2), True, max_levels)
    asks = _parse_side(_get(content, 3), False, max_levels)
    if not bids or not asks:
        return None
    return BookSnapshot(symbol=str(symbol).upper(), ts_ms=int(ts), bids=bids, asks=asks, venue=venue)
