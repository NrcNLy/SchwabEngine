"""
indicators/of_imbalance.py
==========================
Multi-Level Order Flow Imbalance (MLOFI).

Per-level event (Cont-Kukanov-Stoikov), for depth level m against the previous snapshot:

    e_m = 1{Pb_m >= Pb_m'} * qb_m  -  1{Pb_m <= Pb_m'} * qb_m'
        - 1{Pa_m <= Pa_m'} * qa_m  +  1{Pa_m >= Pa_m'} * qa_m'

When prices are unchanged this reduces exactly to  dBidSize - dAskSize  (the simplified form).

    MLOFI_t = sum_m w_m * e_m,   w_m proportional to 1 / max(distance_m, floor), sum_m w_m = 1

where distance_m is the mean absolute distance of level m's bid and ask from the mid-price.

Robustness
----------
* Crossed / locked / empty / non-monotone / non-finite books are dropped (previous snapshot kept).
* Zero distance from mid is floored (``weight_floor_ticks * tick_size``) - no division by zero.
* Spoofing: each level's contribution is winsorized at ``spoof_clip_mult`` x that level's EWMA of
  |contribution| (the EWMA is fed the *clipped* value, so a spoof cannot inflate its own ceiling);
  deeper levels are down-weighted by ``w_m``; quotes that appear and are pulled inside one window net
  to ~0 in the windowed sums.

Normalization
-------------
``norm[w] = tanh(sum_window(MLOFI) / mean_window(top-5 depth))`` - dimensionless, bounded in (-1, 1),
roughly linear near zero so small thresholds stay meaningful.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional, Tuple

from data.book import MAX_LEVELS, BookSnapshot, is_valid

_EPS = 1e-9
_EWMA_ALPHA = 0.05
_EWMA_WARMUP = 20          # events at a level before its clip ceiling is trusted
_LEVEL_FLOOR_SIZE = 1.0    # smallest clip ceiling (shares)


@dataclass(frozen=True)
class MLOFIReading:
    symbol: str
    ready: bool
    depth_levels_available: int
    raw: Dict[str, float]
    norm: Dict[str, float]
    level_contrib: Tuple[float, ...]
    unanimous_positive: bool
    unanimous_negative: bool
    crossed_events: int
    invalid_events: int
    events_in_longest_window: int


class _Window:
    """Sliding window with O(1) amortised running sums."""

    __slots__ = ("span", "q", "sum_e", "sum_d", "sum_lv", "track_levels")

    def __init__(self, span_ms: int, levels: int, track_levels: bool) -> None:
        self.span = int(span_ms)
        self.q: Deque[Tuple[int, float, float, Tuple[float, ...]]] = deque()
        self.sum_e = 0.0
        self.sum_d = 0.0
        self.track_levels = track_levels
        self.sum_lv = [0.0] * levels

    def add(self, ts: int, e: float, depth: float, wc: Tuple[float, ...]) -> None:
        self.q.append((ts, e, depth, wc))
        self.sum_e += e
        self.sum_d += depth
        if self.track_levels:
            lv = self.sum_lv
            for i, v in enumerate(wc):
                lv[i] += v
        self.evict(ts)

    def evict(self, now: int) -> None:
        cutoff = now - self.span
        q = self.q
        while q and q[0][0] <= cutoff:
            _, e, d, wc = q.popleft()
            self.sum_e -= e
            self.sum_d -= d
            if self.track_levels:
                lv = self.sum_lv
                for i, v in enumerate(wc):
                    lv[i] -= v
        if not q:  # kill accumulated float drift whenever the window empties
            self.sum_e = 0.0
            self.sum_d = 0.0
            if self.track_levels:
                self.sum_lv = [0.0] * len(self.sum_lv)

    def norm(self) -> float:
        n = len(self.q)
        if n == 0:
            return 0.0
        mean_depth = self.sum_d / n
        return math.tanh(self.sum_e / (mean_depth + _EPS))


class MLOFIEngine:
    def __init__(
        self,
        symbol: str,
        levels: int = MAX_LEVELS,
        windows_ms: Tuple[int, ...] = (100, 1000, 5000),
        tick_size: float = 0.01,
        weight_floor_ticks: float = 0.5,
        spoof_clip_mult: float = 6.0,
        min_events: int = 20,
        unanimous_min_levels: int = 3,
    ) -> None:
        self.symbol = symbol.upper()
        self.levels = max(1, min(int(levels), MAX_LEVELS))
        self.windows_ms = tuple(sorted(int(w) for w in windows_ms))
        self.tick_size = float(tick_size)
        self.floor = float(weight_floor_ticks) * self.tick_size
        self.clip_mult = float(spoof_clip_mult)
        self.min_events = int(min_events)
        self.unanimous_min_levels = int(unanimous_min_levels)

        longest = self.windows_ms[-1]
        self._windows: Dict[int, _Window] = {
            w: _Window(w, self.levels, track_levels=(w == longest)) for w in self.windows_ms
        }
        self._prev: Optional[BookSnapshot] = None
        self._last_ts = 0
        self._ewma = [0.0] * self.levels
        self._ewma_n = [0] * self.levels
        self._levels_available = 0
        self.crossed_events = 0
        self.invalid_events = 0
        self.last_book_ms = 0

    # ------------------------------------------------------------------ ingest
    def on_book(self, snap: BookSnapshot) -> None:
        if self._ingest(snap):
            self.last_book_ms = self._last_ts

    def on_l1(self, bid: float, bid_sz: float, ask: float, ask_sz: float, ts_ms: int) -> None:
        """Top-of-book fallback (one level) for symbols with no book feed."""
        snap = BookSnapshot(self.symbol, int(ts_ms), ((bid, bid_sz),), ((ask, ask_sz),))
        self._ingest(snap)

    def reset_book(self) -> None:
        """Forget the previous snapshot (e.g. when the source venue changes) so no cross-venue delta forms."""
        self._prev = None

    def _ingest(self, snap: BookSnapshot) -> bool:
        if not is_valid(snap):
            if snap.bids and snap.asks and snap.asks[0][0] <= snap.bids[0][0]:
                self.crossed_events += 1
            else:
                self.invalid_events += 1
            return False
        ts = max(int(snap.ts_ms), self._last_ts)
        self._last_ts = ts
        self._levels_available = min(len(snap.bids), len(snap.asks), self.levels)
        prev = self._prev
        self._prev = snap
        if prev is not None:
            self._process(prev, snap, ts)
        return True


    # ------------------------------------------------------------------ core
    def _process(self, prev: BookSnapshot, cur: BookSnapshot, ts: int) -> None:
        mid = cur.mid
        n_avail = min(len(cur.bids), len(cur.asks), len(prev.bids), len(prev.asks), self.levels)
        if n_avail == 0:
            return

        inv = []
        for m in range(n_avail):
            dist = (abs(cur.bids[m][0] - mid) + abs(cur.asks[m][0] - mid)) / 2.0
            inv.append(1.0 / max(dist, self.floor))
        total_inv = sum(inv)

        wc = [0.0] * self.levels
        event = 0.0
        for m in range(n_avail):
            pb, qb = cur.bids[m]
            pbp, qbp = prev.bids[m]
            pa, qa = cur.asks[m]
            pap, qap = prev.asks[m]
            e_b = (qb if pb >= pbp else 0.0) - (qbp if pb <= pbp else 0.0)
            e_a = (qa if pa <= pap else 0.0) - (qap if pa >= pap else 0.0)
            c = e_b - e_a

            # spoof winsorization against this level's robust scale
            if self._ewma_n[m] >= _EWMA_WARMUP:
                ceiling = self.clip_mult * max(self._ewma[m], _LEVEL_FLOOR_SIZE)
                if c > ceiling:
                    c = ceiling
                elif c < -ceiling:
                    c = -ceiling
            self._ewma[m] = abs(c) if self._ewma_n[m] == 0 else (
                (1.0 - _EWMA_ALPHA) * self._ewma[m] + _EWMA_ALPHA * abs(c))
            self._ewma_n[m] += 1

            weighted = (inv[m] / total_inv) * c
            wc[m] = weighted
            event += weighted

        depth = cur.depth(self.levels)
        wct = tuple(wc)
        for win in self._windows.values():
            win.add(ts, event, depth, wct)

    # ------------------------------------------------------------------ read
    def norm_fast(self, window_ms: int, now_ms: int) -> float:
        """Allocation-free normalized MLOFI for one configured window (hot-path read)."""
        win = self._windows[window_ms]
        win.evict(int(now_ms))
        return win.norm()

    def event_count(self, window_ms: int) -> int:
        return len(self._windows[window_ms].q)
    def reading(self, now_ms: Optional[int] = None) -> MLOFIReading:
        now = int(now_ms) if now_ms is not None else self._last_ts
        for win in self._windows.values():
            win.evict(now)
        longest = self._windows[self.windows_ms[-1]]
        n = len(longest.q)

        def label(w: int) -> str:
            return f"{w // 1000}s" if w % 1000 == 0 else f"{w}ms"

        raw = {label(w): self._windows[w].sum_e for w in self.windows_ms}
        norm = {label(w): self._windows[w].norm() for w in self.windows_ms}
        lv = tuple(longest.sum_lv[: self.levels])

        pos = sum(1 for v in lv if v > _EPS)
        neg = sum(1 for v in lv if v < -_EPS)
        enough = self._levels_available >= self.unanimous_min_levels
        unanimous_pos = enough and neg == 0 and pos >= self.unanimous_min_levels
        unanimous_neg = enough and pos == 0 and neg >= self.unanimous_min_levels

        return MLOFIReading(
            symbol=self.symbol,
            ready=n >= self.min_events,
            depth_levels_available=self._levels_available,
            raw=raw,
            norm=norm,
            level_contrib=lv,
            unanimous_positive=unanimous_pos,
            unanimous_negative=unanimous_neg,
            crossed_events=self.crossed_events,
            invalid_events=self.invalid_events,
            events_in_longest_window=n,
        )
