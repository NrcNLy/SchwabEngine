"""
indicators/microstructure.py
============================
MicrostructureHub - the single owner of all order-flow state.

    streamer thread --on_tick / on_book--> hub --> MLOFIEngine (per tradeable symbol)
                                                  VPINEngine  (per tradeable symbol, volume clock)
                                                  LeadLagTracker (NVDA/TSM, $TNX|/ZN + /NQ, KRE)

Design rules
------------
* Veto/confirm only. The hub never sizes, never touches the ledger, stops or flatten logic.
* One clock. Every book snapshot is re-stamped with the receipt time so Level 1 (local clock) and book
  (exchange clock) data share the same windows and exchange clock skew cannot corrupt eviction.
* Book frames are per venue (NASDAQ_BOOK / NYSE_BOOK). The hub sticks to one venue per symbol and only
  switches when the active venue has been silent for ``venue_switch_ms``; it falls back to top-of-book
  (L1) OFI when no valid depth has arrived recently. Levels are never fabricated.
* Level 1 stream frames are deltas (changed fields only), so the hub merges them into per-symbol state.
* ``mode: shadow`` logs/serves decisions but ``blocks()`` is False; ``mode: enforce`` blocks.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from data.book import BookSnapshot
from indicators.lead_lag import LeadLagReading, LeadLagTracker
from indicators.of_imbalance import MLOFIEngine, MLOFIReading
from indicators.vpin import VPINEngine, VPINReading

logger = logging.getLogger("microstructure")

_L1_KEYS = ("bid_price", "ask_price", "last_price", "bid_size", "ask_size", "total_volume", "last_size")
_LOG_THROTTLE_S = 10.0
_STATE_FILE = "microstructure_state.json"


@dataclass(frozen=True)
class MicroSnapshot:
    symbol: str
    now_ms: int
    mlofi: MLOFIReading
    vpin: VPINReading
    lead: LeadLagReading
    depth_source: str          # "BOOK" | "L1" | "NONE"


def _num(x: Any) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


class MicrostructureHub:
    def __init__(self, cfg: Dict[str, Any], clock_ms: Optional[Callable[[], int]] = None) -> None:
        self.full_cfg = cfg
        micro = cfg.get("microstructure", {}) or {}
        self.cfg = micro
        self.enabled = bool(micro.get("enabled", False))
        self.mode = str(micro.get("mode", "shadow")).lower()
        if self.mode not in ("shadow", "enforce"):
            raise ValueError(f"microstructure.mode must be 'shadow' or 'enforce', got {self.mode!r}")
        self._clock = clock_ms or (lambda: int(time.time() * 1000))
        self._lock = threading.Lock()

        eng_cfg = cfg.get("engine", {}) or {}
        self.symbols: List[str] = [str(s).upper() for s in (eng_cfg.get("symbols") or [])]

        ml = micro.get("mlofi", {}) or {}
        vp = micro.get("vpin", {}) or {}
        book = micro.get("book", {}) or {}
        self._l1_fallback = bool(book.get("l1_fallback", True))
        self._l1_fallback_after_ms = int(book.get("l1_fallback_after_ms", 2000))
        self._venue_switch_ms = int(book.get("venue_switch_ms", 5000))
        self._levels = int(book.get("levels", ml.get("levels", 5)))
        self.book_services: List[str] = [str(s) for s in (book.get("services") or ["NASDAQ_BOOK", "NYSE_BOOK"])]

        self._ml_kwargs = dict(
            levels=self._levels,
            windows_ms=tuple(ml.get("windows_ms", (100, 1000, 5000))),
            tick_size=float(ml.get("tick_size", 0.01)),
            weight_floor_ticks=float(ml.get("weight_floor_ticks", 0.5)),
            spoof_clip_mult=float(ml.get("spoof_clip_mult", 6.0)),
            min_events=int(ml.get("min_events", 20)),
            unanimous_min_levels=int(ml.get("unanimous_min_levels", 3)),
        )
        self._vp_kwargs = dict(
            bucket_divisor=int(vp.get("bucket_divisor", 50)),
            n_buckets=int(vp.get("n_buckets", 50)),
            min_buckets=int(vp.get("min_buckets", 10)),
            percentile_lookback=int(vp.get("percentile_lookback", 250)),
            percentile_min_history=int(vp.get("percentile_min_history", 20)),
            sigma_halflife=int(vp.get("sigma_halflife", 20)),
            toxic_percentile=float(vp.get("toxic_percentile", 0.90)),
        )
        self.adv_lookback_days = int(vp.get("adv_lookback_days", 20))

        self._mlofi: Dict[str, MLOFIEngine] = {s: MLOFIEngine(s, **self._ml_kwargs) for s in self.symbols}
        self._vpin: Dict[str, VPINEngine] = {s: VPINEngine(s, **self._vp_kwargs) for s in self.symbols}
        self.lead = LeadLagTracker(micro.get("lead_lag", {}) or {}, micro.get("mlofi", {}) or {})

        configured_refs = {str(s).upper() for s in (micro.get("reference_symbols") or [])}
        self._reference = (configured_refs | set(self.lead.reference_symbols)) - set(self.symbols)

        self._l1: Dict[str, Dict[str, float]] = {}
        self._prev_tv: Dict[str, float] = {}
        self._venue: Dict[str, Tuple[str, int]] = {}
        self._book_seen: Dict[str, int] = {}

        self.decisions: Deque[Dict[str, Any]] = deque(maxlen=50)
        self._last_gate: Dict[str, Tuple[str, str]] = {}
        self._log_seen: Dict[Tuple[str, str, str], float] = {}
        self._degraded_logged = False
        self.latency_ns_max = 0

    # ------------------------------------------------------------------ construction
    @classmethod
    def from_config(cls, cfg: Dict[str, Any], rest_client: Any = None,
                    clock_ms: Optional[Callable[[], int]] = None) -> Optional["MicrostructureHub"]:
        micro = cfg.get("microstructure", {}) or {}
        if not micro.get("enabled", False):
            return None
        hub = cls(cfg, clock_ms)
        hub.restore()
        if rest_client is not None:
            hub.load_adv(rest_client)
        logger.info(
            "MicrostructureHub ready: mode=%s tradeable=%s reference=%s services=%s",
            hub.mode, hub.symbols, sorted(hub._reference), hub.book_services,
        )
        return hub

    # ------------------------------------------------------------------ routing info
    @property
    def enforce(self) -> bool:
        return self.mode == "enforce"

    def is_reference(self, sym: str) -> bool:
        return sym.upper() in self._reference

    def is_tracked(self, sym: str) -> bool:
        s = sym.upper()
        return s in self._mlofi or s in self._reference

    def equity_reference_symbols(self) -> List[str]:
        return sorted(s for s in self._reference if not s.startswith("/"))

    def futures_symbols(self) -> List[str]:
        return sorted(s for s in self._reference if s.startswith("/"))

    def book_symbols(self) -> List[str]:
        """Symbols that should receive Level 2 subscriptions (tradeable + equity leaders; never indices)."""
        leaders = [s for s in self.lead.leaders if not s.startswith("$")]
        return sorted(set(self.symbols) | set(leaders))

    # ------------------------------------------------------------------ ADV
    def set_adv(self, sym: str, adv: float) -> None:
        v = self._vpin.get(sym.upper())
        if v is not None:
            v.set_adv(adv)

    def load_adv(self, rest_client: Any) -> None:
        import pytz

        et = pytz.timezone("America/New_York")
        now = datetime.now(et)
        for sym in self.symbols:
            try:
                hist = rest_client.get_price_history(
                    symbol=sym, period_type="month", period=2, frequency_type="daily", frequency=1,
                    need_extended_hours_data=False)
                candles = hist.get("candles", []) or []
                rows = []
                for c in candles:
                    d = datetime.fromtimestamp(c["datetime"] / 1000.0, tz=et)
                    rows.append((d.date(), float(c.get("volume", 0))))
                # drop today's partial session so ADV is not biased low
                if rows and rows[-1][0] == now.date() and now.hour < 16:
                    rows = rows[:-1]
                vols = [v for _, v in rows[-self.adv_lookback_days:] if v > 0]
                if len(vols) >= 5:
                    adv = sum(vols) / len(vols)
                    self.set_adv(sym, adv)
                    logger.info("[%s] ADV=%.0f over %d sessions -> bucket volume %.0f",
                                sym, adv, len(vols), adv / self._vp_kwargs["bucket_divisor"])
                else:
                    logger.warning("[%s] only %d daily candles: VPIN stays not-ready (no fabricated ADV).",
                                   sym, len(vols))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s] ADV preload failed (%s): VPIN stays not-ready.", sym, exc)

    # ------------------------------------------------------------------ ingest (streamer thread)
    def on_tick(self, sym: str, fields: Dict[str, Any], ts_ms: Optional[int] = None) -> None:
        t0 = time.perf_counter_ns()
        s = sym.upper()
        now = int(ts_ms) if ts_ms is not None else self._clock()
        with self._lock:
            st = self._l1.setdefault(s, {})
            changed = set()
            for key in _L1_KEYS:
                if key in fields:
                    v = _num(fields[key])
                    if v is not None:
                        if st.get(key) != v:
                            changed.add(key)
                        st[key] = v

            bid, ask = st.get("bid_price", 0.0), st.get("ask_price", 0.0)
            bsz, asz = st.get("bid_size", 0.0), st.get("ask_size", 0.0)
            last = st.get("last_price", 0.0)
            quote_changed = bool(changed & {"bid_price", "ask_price", "bid_size", "ask_size"})
            quote_ok = bid > 0 and ask > bid

            eng = self._mlofi.get(s)
            if eng is not None:
                if quote_changed and quote_ok:
                    if self._l1_fallback and not self._has_recent_book(eng, now):
                        eng.on_l1(bid, bsz, ask, asz, now)
                    self.lead.on_follower_quote(s, bid, ask, now)
                if "total_volume" in fields:
                    self._on_total_volume(s, st, now)
            elif self.lead.is_leader(s):
                if (quote_changed or "last_price" in changed) and (quote_ok or last > 0):
                    if not self.lead.leader_has_book(s, now, self._l1_fallback_after_ms):
                        self.lead.on_leader_l1(s, bid, bsz, ask, asz, last or None, now)
            if self.lead.is_rate_source(s) and last > 0 and "last_price" in fields:
                self.lead.on_rate_tick(s, last, now)
        dt = time.perf_counter_ns() - t0
        if dt > self.latency_ns_max:
            self.latency_ns_max = dt

    def _on_total_volume(self, s: str, st: Dict[str, float], now: int) -> None:
        tv = st.get("total_volume")
        if tv is None:
            return
        prev = self._prev_tv.get(s)
        self._prev_tv[s] = tv
        if prev is None or tv <= prev:
            return                       # first sight, no new volume, or session reset
        price = st.get("last_price", 0.0)
        if price > 0:
            self._vpin[s].on_trade(price, tv - prev, now)

    def _has_recent_book(self, eng: MLOFIEngine, now: int) -> bool:
        return bool(eng.last_book_ms and now - eng.last_book_ms <= self._l1_fallback_after_ms)

    def on_book(self, snap: BookSnapshot) -> None:
        t0 = time.perf_counter_ns()
        s = snap.symbol.upper()
        now = self._clock()
        snap = dataclasses.replace(snap, ts_ms=now)
        with self._lock:
            venue = snap.venue or "?"
            cur = self._venue.get(s)
            if cur is not None and cur[0] != venue and now - cur[1] <= self._venue_switch_ms:
                return                   # another venue's book is active; never mix per-venue deltas
            switched = cur is not None and cur[0] != venue
            self._venue[s] = (venue, now)
            self._book_seen[s] = self._book_seen.get(s, 0) + 1
            eng = self._mlofi.get(s)
            if eng is not None:
                if switched:
                    eng.reset_book()
                eng.on_book(snap)
            elif self.lead.is_leader(s):
                self.lead.on_leader_book(s, snap, now)
        dt = time.perf_counter_ns() - t0
        if dt > self.latency_ns_max:
            self.latency_ns_max = dt

    # ------------------------------------------------------------------ read
    def snapshot(self, sym: str, now_ms: Optional[int] = None) -> Optional[MicroSnapshot]:
        s = sym.upper()
        if s not in self._mlofi:
            return None
        now = int(now_ms) if now_ms is not None else self._clock()
        with self._lock:
            eng = self._mlofi[s]
            if self._has_recent_book(eng, now):
                source = "BOOK"
            elif eng.event_count(max(eng.windows_ms)) > 0:
                source = "L1"
            else:
                source = "NONE"
            return MicroSnapshot(s, now, eng.reading(now), self._vpin[s].reading(), self.lead.bias(s, now), source)

    # ------------------------------------------------------------------ gate
    def check(self, sym: str, strategy: str, phase: Any, stage: str = "engine",
              now_ms: Optional[int] = None):
        """Evaluates the veto-only gate; records/logs the outcome. Returns a ``MicroDecision``."""
        from execution.risk_manager import MicroDecision, evaluate_microstructure_gate

        snap = self.snapshot(sym, now_ms)
        if snap is None:
            return MicroDecision("ALLOW", "DISABLED", f"{sym} is not tracked by the microstructure hub")
        decision = evaluate_microstructure_gate(snap, sym, strategy, phase, self.cfg)
        self._record(sym.upper(), strategy, decision, stage)
        return decision

    def blocks(self, decision: Any) -> bool:
        return bool(self.enforce and decision.suppress)

    def pre_signal_filter(self, sym: str, strategy: str) -> bool:
        """StrategyEngine hook: True lets the strategy raise its signal. Evaluated BEFORE the strategy
        consumes its one-per-day ORB or starts a cooldown, so a veto burns nothing."""
        try:
            from core.runtime import now_et
            from core.session import get_session_phase

            phase = get_session_phase(now_et(), self.full_cfg)
            decision = self.check(sym, strategy, phase, stage="pre")
            return not self.blocks(decision)
        except Exception:  # noqa: BLE001 - a gate fault must never stop trading logic
            logger.exception("microstructure pre-signal filter failed; failing open")
            return True

    def _record(self, sym: str, strategy: str, d: Any, stage: str) -> None:
        now = time.monotonic()
        key = (sym, strategy, d.code)
        last = self._log_seen.get(key, 0.0)
        verb = ("WOULD_SUPPRESS" if d.suppress else "WOULD_ALLOW") if not self.enforce else (
            "SUPPRESS" if d.suppress else "ALLOW")
        changed = self._last_gate.get(sym) != (strategy, d.code)
        self._last_gate[sym] = (strategy, d.code)
        if stage == "pre" and not changed and now - last < _LOG_THROTTLE_S:
            return
        self._log_seen[key] = now
        self.decisions.append({
            "ts": datetime.utcnow().isoformat() + "Z", "symbol": sym, "strategy": strategy, "stage": stage,
            "mode": self.mode, "action": d.action, "code": d.code, "reason": d.reason,
            "regime_hint": d.regime_hint, "degraded": d.degraded,
        })
        if d.suppress or d.degraded or stage != "pre":
            logger.info("MICRO %s %s %s [%s/%s] %s", verb, sym, strategy, stage, d.code, d.reason)
        if d.degraded and not self._degraded_logged:
            self._degraded_logged = True
            logger.warning("MICRO degraded state: lead feeds unavailable; gating on own-book MLOFI only.")
        elif not d.degraded and d.code == "OK":
            self._degraded_logged = False

    # ------------------------------------------------------------------ telemetry
    def _symbol_view(self, s: str, snap: MicroSnapshot) -> Dict[str, Any]:
        ml, vp, ld = snap.mlofi, snap.vpin, snap.lead
        gate = self._last_gate.get(s)
        return {
            "mlofi": round(ml.norm.get("5s", 0.0), 4) if ml.ready else None,
            "mlofi_1s": round(ml.norm.get("1s", 0.0), 4) if ml.ready else None,
            "mlofi_ready": ml.ready,
            "vpin": round(vp.vpin, 4) if vp.ready else None,
            "vpin_percentile": round(vp.percentile, 4) if (vp.ready and vp.percentile_ready) else None,
            "vpin_ready": vp.ready,
            "vpin_toxic": vp.toxic,
            "lead_lag_bias": round(ld.bias, 4) if ld.available else None,
            "lead_available": ld.available,
            "depth_levels": ml.depth_levels_available,
            "depth_source": snap.depth_source,
            "gate": gate[0] if gate else None,
            "gate_code": gate[1] if gate else None,
        }

    def telemetry(self) -> Dict[str, Any]:
        symbols: Dict[str, Any] = {}
        ready_all = True
        for s in self.symbols:
            snap = self.snapshot(s)
            if snap is None:
                continue
            view = self._symbol_view(s, snap)
            symbols[s] = view
            ready_all = ready_all and view["mlofi_ready"]
        return {"enabled": True, "mode": self.mode, "ready": bool(symbols) and ready_all, "symbols": symbols}

    def indicators(self, sym: Optional[str] = None) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        targets = [sym.upper()] if sym else self.symbols
        for s in targets:
            snap = self.snapshot(s)
            if snap is None:
                continue
            ml, vp, ld = snap.mlofi, snap.vpin, snap.lead
            out[s] = {
                "depth_source": snap.depth_source,
                "mlofi": {"ready": ml.ready, "raw": ml.raw, "norm": ml.norm,
                          "level_contrib": list(ml.level_contrib),
                          "unanimous_positive": ml.unanimous_positive,
                          "unanimous_negative": ml.unanimous_negative,
                          "depth_levels": ml.depth_levels_available,
                          "crossed_events": ml.crossed_events, "invalid_events": ml.invalid_events,
                          "events": ml.events_in_longest_window},
                "vpin": {"ready": vp.ready, "buckets": vp.buckets, "bucket_volume": vp.bucket_volume,
                         "vpin": vp.vpin, "vpin_dir": vp.vpin_dir, "percentile": vp.percentile,
                         "percentile_ready": vp.percentile_ready, "toxic": vp.toxic},
                "lead_lag": {"available": ld.available, "bias": ld.bias, "veto_long": ld.veto_long,
                             "invalidate_breakout": ld.invalidate_breakout,
                             "anticipatory": ld.anticipatory, "components": ld.components,
                             "stale_sources": list(ld.stale_sources)},
            }
        return {"enabled": True, "mode": self.mode, "symbols": out,
                "recent_decisions": list(self.decisions)[-20:],
                "latency_max_us": round(self.latency_ns_max / 1000.0, 1)}

    # ------------------------------------------------------------------ persistence (VPIN carry-over)
    @staticmethod
    def _state_path() -> Path:
        from core.paths import STATE_DIR

        return STATE_DIR / _STATE_FILE

    def persist(self) -> bool:
        from core.atomic_io import atomic_write_json

        try:
            with self._lock:
                payload = {
                    "saved_at": datetime.utcnow().isoformat() + "Z",
                    "symbols": {s: v.snapshot_state() for s, v in self._vpin.items()},
                }
            atomic_write_json(self._state_path(), payload)
            logger.info("Microstructure VPIN state persisted for %s.", sorted(self._vpin))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Microstructure persist failed: %s", exc)
            return False

    def restore(self, max_age_days: int = 5) -> int:
        from core.atomic_io import read_json

        data = read_json(self._state_path(), default=None)
        if not isinstance(data, dict):
            return 0
        try:
            saved = datetime.fromisoformat(str(data.get("saved_at", "")).rstrip("Z"))
        except ValueError:
            return 0
        if datetime.utcnow() - saved > timedelta(days=max_age_days):
            logger.info("Microstructure carry-over is older than %d days; starting cold.", max_age_days)
            return 0
        restored = 0
        for s, state in (data.get("symbols") or {}).items():
            v = self._vpin.get(str(s).upper())
            if v is not None and isinstance(state, dict) and v.restore_state(state):
                restored += 1
        if restored:
            logger.info("Microstructure carry-over restored for %d symbol(s).", restored)
        return restored
