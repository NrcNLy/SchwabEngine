"""
indicators/lead_lag.py
======================
Cross-asset lead-lag tracker. Each tradeable target has a leader complex:

    SOXL  <- NVDA, TSM   (own-book MLOFI of the leaders; anticipatory when SOXL's quote has not moved)
    TQQQ  <- 10Y yield ($TNX, else /ZN inverted) tick delta + NQ futures delta
    TNA   <- KRE regional-bank internals (MLOFI + short-horizon return)

Every reading carries ``available`` (False => bias 0, the gate degrades per policy), ``stale_sources``
and the raw ``components`` so the HUD / logs can show exactly why a bias was produced. Sources silent for
longer than ``stale_after_ms`` (``rate_stale_after_ms`` for rates) are dropped, never carried forward.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

from data.book import BookSnapshot
from indicators.of_imbalance import MLOFIEngine

_SAMPLE_MS = 1000          # history sampling cadence for price/rate series
_STAT_MIN_SAMPLES = 50     # z-score warm-up for leader MLOFI
_RATE_MIN_SAMPLES = 30     # sigma warm-up for rate deltas


@dataclass(frozen=True)
class LeadLagReading:
    target: str
    available: bool
    bias: float
    veto_long: bool
    invalidate_breakout: bool
    anticipatory: bool
    components: Dict[str, float] = field(default_factory=dict)
    stale_sources: Tuple[str, ...] = ()


def _empty(target: str, stale: Tuple[str, ...] = ()) -> LeadLagReading:
    return LeadLagReading(target, False, 0.0, False, False, False, {}, stale)


class _Ewma:
    """EWMA mean / variance (West) with a sample count."""

    __slots__ = ("alpha", "mean", "var", "n")

    def __init__(self, alpha: float) -> None:
        self.alpha, self.mean, self.var, self.n = alpha, 0.0, 0.0, 0

    def update(self, x: float) -> None:
        self.n += 1
        if self.n == 1:
            self.mean, self.var = x, 0.0
            return
        diff = x - self.mean
        incr = self.alpha * diff
        self.mean += incr
        self.var = (1.0 - self.alpha) * (self.var + diff * incr)

    def z(self, x: float) -> float:
        sd = math.sqrt(self.var) if self.var > 0 else 0.0
        return (x - self.mean) / sd if sd > 1e-12 else 0.0


class _Series:
    """Time-sampled (ts, value) history with look-back lookup."""

    __slots__ = ("q", "span")

    def __init__(self, span_ms: int) -> None:
        self.q: Deque[Tuple[int, float]] = deque()
        self.span = int(span_ms)

    def add(self, ts: int, value: float) -> None:
        if self.q and ts - self.q[-1][0] < _SAMPLE_MS:
            self.q[-1] = (self.q[-1][0], value)   # refresh the latest sample inside the interval
            return
        self.q.append((ts, value))
        cutoff = ts - self.span
        while len(self.q) > 2 and self.q[1][0] <= cutoff:
            self.q.popleft()

    def at(self, ts: int) -> Optional[float]:
        """Latest sample with timestamp <= ts (None when the history does not reach back that far)."""
        best = None
        for t, v in self.q:
            if t <= ts:
                best = v
            else:
                break
        return best

    def last(self) -> Optional[Tuple[int, float]]:
        return self.q[-1] if self.q else None


class _RateState:
    __slots__ = ("sign", "series", "sq", "last_ts")

    def __init__(self, sign: float, span_ms: int) -> None:
        self.sign = sign
        self.series = _Series(span_ms)
        self.sq = _Ewma(0.01)       # tracks dY^2 around zero via mean-0 use of (var + mean^2)
        self.last_ts = 0


class LeadLagTracker:
    def __init__(self, cfg: Dict[str, Any], mlofi_cfg: Optional[Dict[str, Any]] = None) -> None:
        ll = cfg or {}
        mf = dict(mlofi_cfg or {})
        self.stale_after_ms = int(ll.get("stale_after_ms", 3000))
        self.rate_stale_after_ms = int(ll.get("rate_stale_after_ms", 30000))
        self.horizon_ms = int(ll.get("horizon_ms", 5000))

        soxl = ll.get("soxl", {}) or {}
        self.soxl_leaders: Dict[str, float] = {str(k).upper(): float(v) for k, v in
                                               (soxl.get("leaders") or {"NVDA": 0.6, "TSM": 0.4}).items()}
        self.soxl_spike_z = float(soxl.get("spike_z", 2.0))
        self.soxl_follower = str(soxl.get("follower", "SOXL")).upper()
        self.soxl_stale_ticks = float(soxl.get("follower_stale_ticks", 1))

        tq = ll.get("tqqq", {}) or {}
        self.yield_symbol = str(tq.get("yield_symbol", "$TNX")).upper()
        self.fallback_symbol = str(tq.get("fallback_futures", "/ZN")).upper()
        self.nq_symbol = str(tq.get("nq_symbol", "/NQ")).upper()
        self.rate_window_ms = int(float(tq.get("window_s", 60)) * 1000)
        self.rate_spike_z = float(tq.get("spike_z", 2.0))
        self.rate_accel_z = float(tq.get("accel_z", 1.0))

        tn = ll.get("tna", {}) or {}
        self.kre_symbol = str(tn.get("leader", "KRE")).upper()
        self.kre_neg = float(tn.get("kre_neg", 0.10))
        self.kre_ret_floor = float(tn.get("kre_ret_floor", 0.004))
        self.kre_ret_window_ms = int(float(tn.get("ret_window_s", 900)) * 1000)

        self.tick_size = float(mf.get("tick_size", 0.01))
        windows = tuple(mf.get("windows_ms", (100, 1000, 5000)))
        engine_kwargs = dict(
            levels=int(mf.get("levels", 5)), windows_ms=windows, tick_size=self.tick_size,
            weight_floor_ticks=float(mf.get("weight_floor_ticks", 0.5)),
            spoof_clip_mult=float(mf.get("spoof_clip_mult", 6.0)),
            min_events=int(mf.get("min_events", 20)),
            unanimous_min_levels=int(mf.get("unanimous_min_levels", 3)),
        )
        leader_syms = set(self.soxl_leaders) | {self.kre_symbol}
        self.leaders: Dict[str, MLOFIEngine] = {s: MLOFIEngine(s, **engine_kwargs) for s in sorted(leader_syms)}
        self._leader_stats: Dict[str, _Ewma] = {s: _Ewma(0.01) for s in self.leaders}
        self._leader_last: Dict[str, int] = {s: 0 for s in self.leaders}
        self._leader_px: Dict[str, _Series] = {s: _Series(self.kre_ret_window_ms + 60000) for s in self.leaders}

        span = self.rate_window_ms * 2 + 60000
        self._rates: Dict[str, _RateState] = {
            self.yield_symbol: _RateState(+1.0, span),
            self.fallback_symbol: _RateState(-1.0, span),   # /ZN is a price: yield up == price down
            self.nq_symbol: _RateState(+1.0, span),
        }
        self._follower_mid = _Series(10_000)

    # ------------------------------------------------------------------ routing helpers
    def is_leader(self, sym: str) -> bool:
        return sym.upper() in self.leaders

    def is_rate_source(self, sym: str) -> bool:
        return sym.upper() in self._rates

    @property
    def reference_symbols(self) -> List[str]:
        return sorted(set(self.leaders) | set(self._rates))

    def leader_has_book(self, sym: str, now_ms: int, within_ms: int = 2000) -> bool:
        eng = self.leaders.get(sym.upper())
        return bool(eng and eng.last_book_ms and now_ms - eng.last_book_ms <= within_ms)

    # ------------------------------------------------------------------ ingest
    def on_leader_book(self, sym: str, snap: BookSnapshot, now_ms: int) -> None:
        eng = self.leaders.get(sym.upper())
        if eng is None:
            return
        eng.on_book(snap)
        self._after_leader(sym.upper(), now_ms, snap.mid)

    def on_leader_l1(self, sym: str, bid: float, bid_sz: float, ask: float, ask_sz: float,
                     last: Optional[float], now_ms: int) -> None:
        s = sym.upper()
        eng = self.leaders.get(s)
        if eng is None:
            return
        eng.on_l1(bid, bid_sz, ask, ask_sz, now_ms)
        mid = (bid + ask) / 2.0 if bid > 0 and ask > bid else (last or 0.0)
        self._after_leader(s, now_ms, mid)

    def _after_leader(self, sym: str, now_ms: int, px: float) -> None:
        self._leader_last[sym] = now_ms
        eng = self.leaders[sym]
        if 1000 in eng._windows and eng.event_count(5000 if 5000 in eng._windows else 1000) > 0:
            self._leader_stats[sym].update(eng.norm_fast(1000, now_ms))
        if px and px > 0:
            self._leader_px[sym].add(now_ms, px)

    def on_rate_tick(self, sym: str, value: float, now_ms: int) -> None:
        st = self._rates.get(sym.upper())
        if st is None or value is None or not math.isfinite(value) or value == 0:
            return
        st.last_ts = now_ms
        st.series.add(now_ms, float(value))
        base = st.series.at(now_ms - self.rate_window_ms)
        if base is not None:
            dy = st.sign * (float(value) - base)
            # RMS of dY (mean-zero scale): E[dY^2] tracked through the EWMA of dY
            st.sq.update(dy)

    def on_follower_quote(self, sym: str, bid: float, ask: float, now_ms: int) -> None:
        if sym.upper() != self.soxl_follower or not (bid > 0 and ask > bid):
            return
        self._follower_mid.add(now_ms, (bid + ask) / 2.0)

    # ------------------------------------------------------------------ readings
    def bias(self, target: str, now_ms: int) -> LeadLagReading:
        t = target.upper()
        if t == "SOXL":
            return self._soxl(now_ms)
        if t == "TQQQ":
            return self._tqqq(now_ms)
        if t == "TNA":
            return self._tna(now_ms)
        return _empty(t)

    def _decay(self, newest_ts: int, now_ms: int) -> float:
        if self.horizon_ms <= 0:
            return 1.0
        return max(0.0, 1.0 - (now_ms - newest_ts) / self.horizon_ms)

    def _soxl(self, now: int) -> LeadLagReading:
        stale: List[str] = []
        num = den = 0.0
        newest = 0
        comps: Dict[str, float] = {}
        for sym, w in self.soxl_leaders.items():
            eng = self.leaders.get(sym)
            last = self._leader_last.get(sym, 0)
            stats = self._leader_stats.get(sym)
            if eng is None or stats is None or not last or now - last > self.stale_after_ms \
                    or stats.n < _STAT_MIN_SAMPLES:
                stale.append(sym)
                continue
            n1 = eng.norm_fast(1000, now)
            z = stats.z(n1)
            comps[f"{sym}_norm_1s"] = n1
            comps[f"{sym}_z"] = z
            num += w * z
            den += w
            newest = max(newest, last)
        if den <= 0:
            return _empty("SOXL", tuple(sorted(stale)))

        lead = num / den
        comps["lead_z"] = lead
        flat = True
        move = 0.0
        last_mid = self._follower_mid.last()
        base = self._follower_mid.at(now - 1000)
        if last_mid is not None and base is not None:
            move = last_mid[1] - base
            flat = abs(move) <= self.soxl_stale_ticks * self.tick_size
        comps["follower_move_1s"] = move
        anticipatory = bool(lead >= self.soxl_spike_z and flat)
        bias = math.tanh(lead / max(self.soxl_spike_z, 1e-9))
        if not flat and (move > 0) == (lead > 0):
            bias *= 0.25      # the follower already repriced: little anticipation left
        bias *= self._decay(newest, now)
        return LeadLagReading("SOXL", True, max(-1.0, min(1.0, bias)), False, False, anticipatory,
                              comps, tuple(sorted(stale)))

    def _rate_z(self, sym: str, now: int) -> Optional[Tuple[float, float, float]]:
        """(z, accel_z, dY) for a rate source or None when stale / not warmed up."""
        st = self._rates.get(sym)
        if st is None or not st.last_ts or now - st.last_ts > self.rate_stale_after_ms \
                or st.sq.n < _RATE_MIN_SAMPLES:
            return None
        last = st.series.last()
        cur = st.series.at(now)
        base = st.series.at(now - self.rate_window_ms)
        prev_base = st.series.at(now - 2 * self.rate_window_ms)
        if last is None or cur is None or base is None:
            return None
        sigma = math.sqrt(max(st.sq.var + st.sq.mean ** 2, 0.0))
        if sigma <= 1e-12:
            return None
        dy = st.sign * (cur - base)
        prev_dy = st.sign * (base - prev_base) if prev_base is not None else dy
        return dy / sigma, (dy - prev_dy) / sigma, dy

    def _tqqq(self, now: int) -> LeadLagReading:
        stale: List[str] = []
        src = None
        for sym in (self.yield_symbol, self.fallback_symbol):
            res = self._rate_z(sym, now)
            if res is not None:
                src = (sym, res)
                break
            stale.append(sym)
        if src is None:
            return _empty("TQQQ", tuple(stale))

        sym, (z, accel_z, dy) = src
        comps = {"yield_source_is_primary": 1.0 if sym == self.yield_symbol else 0.0,
                 "yield_dY": dy, "yield_z": z, "yield_accel_z": accel_z}
        bias = 0.75 * math.tanh(-z / max(self.rate_spike_z, 1e-9)) \
            + 0.25 * math.tanh(-accel_z / max(self.rate_accel_z, 1e-9))
        nq = self._rate_z(self.nq_symbol, now)
        if nq is None:
            stale.append(self.nq_symbol)
        else:
            comps["nq_z"] = nq[0]
            if (nq[0] > 0) == (bias > 0) and abs(nq[0]) > 0:
                bias *= 1.25      # NQ agreeing only ever amplifies; it never flips the sign
        veto = bool(z >= self.rate_spike_z)
        return LeadLagReading("TQQQ", True, max(-1.0, min(1.0, bias)), veto, False, False,
                              comps, tuple(sorted(set(stale))))

    def _tna(self, now: int) -> LeadLagReading:
        sym = self.kre_symbol
        eng = self.leaders.get(sym)
        last = self._leader_last.get(sym, 0)
        if eng is None or not last or now - last > self.stale_after_ms:
            return _empty("TNA", (sym,))
        n5 = eng.norm_fast(5000 if 5000 in eng._windows else 1000, now)
        series = self._leader_px[sym]
        cur = series.last()
        base = series.at(now - self.kre_ret_window_ms)
        if base is None and series.q:
            oldest = series.q[0]
            base = oldest[1] if cur is not None and cur[0] - oldest[0] >= 60_000 else None
        ret = (cur[1] / base - 1.0) if (cur is not None and base and base > 0) else 0.0
        weak = bool(n5 < -self.kre_neg and ret < -self.kre_ret_floor)
        bias = 0.6 * math.tanh(n5 / max(2 * self.kre_neg, 1e-9)) \
            + 0.4 * math.tanh(ret / max(2 * self.kre_ret_floor, 1e-9))
        return LeadLagReading("TNA", True, max(-1.0, min(1.0, bias)), False, weak, False,
                              {"kre_norm_5s": n5, "kre_ret": ret})
