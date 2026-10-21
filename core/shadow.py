"""
core/shadow.py
==============
Shadow-mode runtime (``python main.py --mode shadow``).

Shadow mode attaches to the LIVE authenticated Schwab Level 1 streamer, marks the dual-sleeve book to
market on real ticks, and evaluates the regime-dynamic rebalance (HMM satellite target + Leland no-trade
zone + integer-share quantization) every cycle. Orders are never sent to the broker: each decision is
recorded as a mock order in the session telemetry JSONL and applied to an in-memory shadow book.

Components
----------
ShadowTelemetrySink      Thread-safe append-only JSONL writer (logs/session_telemetry_<YYMMDD>.jsonl).
ShadowRebalancer         Dual-sleeve rebalance evaluator that emits mock orders into the sink.
load_strategy_targets    Reads regime targets / routing weights from strategy_config.json.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from data.multi_rate_sync import (
    DEFAULT_CORE_SYMBOLS,
    DEFAULT_SATELLITE_SYMBOLS,
    MultiRateStateSynchronizer,
)
from execution.risk_manager import MetaGovernorHMM

logger = logging.getLogger("shadow")

ET = ZoneInfo("America/New_York")
ENGINE_VERSION = "2.4.0-argon"

SHADOW_UNIVERSE: Tuple[str, ...] = ("SPY", "QQQ", "IWM", "HYG", "IEF", "SOXL", "TQQQ", "TNA")

DEFAULT_ROUTING_VECTOR: Dict[str, float] = {"SPY": 0.35, "QQQ": 0.25, "IWM": 0.10, "HYG": 0.15, "IEF": 0.15}
DEFAULT_REGIME_TARGETS: Dict[str, Dict[str, Any]] = {
    "Goldilocks": {"target": 0.25, "ntz_band": [0.21, 0.29]},
    "Uncertainty": {"target": 0.10, "ntz_band": [0.08, 0.12]},
    "Shock": {"target": 0.0, "ntz_band": [0.0, 0.0]},
}


def session_telemetry_path(project_root: Path, now: Optional[datetime] = None) -> Path:
    """logs/session_telemetry_<YYMMDD>.jsonl for the current ET trading date."""
    stamp = (now or datetime.now(ET)).strftime("%y%m%d")
    return project_root / "logs" / f"session_telemetry_{stamp}.jsonl"


def load_strategy_targets(project_root: Path) -> Dict[str, Any]:
    """Loads regime satellite targets, core routing vector and Leland tau from strategy_config.json."""
    out: Dict[str, Any] = {
        "regime_targets": DEFAULT_REGIME_TARGETS,
        "routing_vector": DEFAULT_ROUTING_VECTOR,
        "tau_band": 0.03,
    }
    path = project_root / "strategy_config.json"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("strategy_config.json unreadable (%s); using built-in regime targets.", exc)
        return out
    if isinstance(data.get("regime_satellite_targets"), dict):
        out["regime_targets"] = data["regime_satellite_targets"]
    vec = data.get("routing_sweep_vector")
    if isinstance(vec, dict):
        core = {k.upper(): float(v) for k, v in vec.items() if k.upper() in DEFAULT_CORE_SYMBOLS and float(v) > 0}
        total = sum(core.values())
        if total > 0:
            out["routing_vector"] = {k: v / total for k, v in core.items()}
    ntz = data.get("leland_ntz", {})
    if isinstance(ntz, dict) and "tau_band" in ntz:
        out["tau_band"] = float(ntz["tau_band"])
    return out


class ShadowTelemetrySink:
    """Thread-safe append-only JSONL telemetry sink. Streamer-thread and asyncio-safe."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.records_written = 0

    def write(self, event: str, **payload: Any) -> None:
        record: Dict[str, Any] = {
            "event": event,
            "timestamp_iso": datetime.now(ET).isoformat(timespec="milliseconds"),
            "engine_version": ENGINE_VERSION,
            "mode": "shadow",
        }
        record.update(payload)
        line = json.dumps(record, default=str)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            self.records_written += 1


class ShadowRebalancer:
    """
    Evaluates the dual-sleeve target every cycle and emits mock orders.

    Decision rule:
      * Flat book with deployable cash  -> initial deployment toward the regime target.
      * Satellite weight outside the dominant regime's NTZ band -> rebalance back to target.
      * Otherwise inside the no-trade zone -> hold.
    Share counts use floor(NLV * sleeve_weight * w_c / ask); sells use the bid.
    """

    def __init__(
        self,
        sync: MultiRateStateSynchronizer,
        sink: ShadowTelemetrySink,
        hmm: MetaGovernorHMM,
        targets: Dict[str, Any],
    ) -> None:
        self.sync = sync
        self.sink = sink
        self.hmm = hmm
        self.regime_targets: Dict[str, Any] = targets["regime_targets"]
        self.routing_vector: Dict[str, float] = targets["routing_vector"]
        self.tau_band: float = float(targets.get("tau_band", 0.03))
        sat = sorted(DEFAULT_SATELLITE_SYMBOLS)
        self.satellite_vector: Dict[str, float] = {s: 1.0 / len(sat) for s in sat}
        self._book: Dict[str, Tuple[int, float]] = {}
        self._cash: Optional[float] = None
        self._order_seq = 0

    # -- helpers ----------------------------------------------------------------

    @staticmethod
    def _regime_key(dominant: str) -> str:
        return "Shock" if dominant.startswith("Macro Shock") or dominant == "Shock" else dominant

    def _adopt_broker_book(self) -> None:
        """First cycle: seed the shadow book from the synchronizer (hot-boot broker positions)."""
        state = self.sync.get_live_portfolio_state()
        self._cash = float(state.cash_buffer)
        for sym, pos in state.positions.items():
            self._book[sym] = (int(pos.quantity), float(pos.average_cost))

    def _push_book_to_sync(self, nlv_hint: float) -> None:
        """Pushes the shadow book into the synchronizer as a synthetic account payload."""
        positions: List[Dict[str, Any]] = []
        mkt_total = 0.0
        for sym, (qty, avg) in self._book.items():
            if qty <= 0:
                continue
            quote = self.sync.get_latest_quote(sym)
            px = quote.microprice if quote is not None and quote.microprice > 0 else avg
            mkt_total += qty * px
            positions.append({
                "instrument": {"symbol": sym},
                "longQuantity": float(qty),
                "shortQuantity": 0.0,
                "averagePrice": avg,
                "marketValue": qty * px,
            })
        cash = float(self._cash or 0.0)
        self.sync.on_account_sync({
            "securitiesAccount": {
                "currentBalances": {
                    "liquidationValue": cash + mkt_total,
                    "cashAvailableForTrading": cash,
                },
                "positions": positions,
            }
        })

    # -- main cycle --------------------------------------------------------------

    def evaluate(self) -> Dict[str, Any]:
        """Runs one shadow rebalance cycle. Returns the summary record that was written."""
        if self._cash is None:
            self._adopt_broker_book()

        macro_vec = self.sync.get_macro_feature_vector()
        pi = self.hmm.update(macro_vec)
        dominant = self._regime_key(self.hmm.get_dominant_regime())
        target_sat = self.hmm.calculate_target_satellite_weight(regime_targets=self.regime_targets)
        band_cfg = (self.regime_targets.get(dominant) or {}).get("ntz_band")
        if band_cfg and len(band_cfg) == 2:
            lo, hi = float(band_cfg[0]), float(band_cfg[1])
        else:
            lo, hi = max(0.0, target_sat - self.tau_band), target_sat + self.tau_band

        state = self.sync.get_live_portfolio_state()
        nlv = float(state.net_liquidation_value)
        w_sat = float(state.satellite_weight)
        credit = self.sync.get_credit_proxy_state()

        quotes = {s: self.sync.get_latest_quote(s) for s in SHADOW_UNIVERSE}
        missing = [s for s, q in quotes.items() if q is None or q.ask <= 0.0 or q.bid <= 0.0]
        stale = [s for s, q in quotes.items() if q is not None and q.is_stale]

        base = {
            "nlv": round(nlv, 2),
            "pi": list(pi),
            "dominant_regime": dominant,
            "target_satellite_weight": round(target_sat, 4),
            "ntz_band": [lo, hi],
            "satellite_weight": round(w_sat, 4),
            "core_weight": round(float(state.core_weight), 4),
            "cash": round(float(state.cash_buffer), 2),
            "credit_proxy": credit,
        }

        if missing or stale:
            rec = dict(base, decision="SKIP_NO_LIVE_QUOTES", missing=missing, stale=stale)
            self.sink.write("SHADOW_REBALANCE_SUMMARY", **rec)
            return rec

        flat = not any(qty > 0 for qty, _ in self._book.values())
        outside_band = (w_sat < lo or w_sat > hi) and not flat
        if flat and nlv > 0 and target_sat > 0.0:
            decision = "INITIAL_DEPLOYMENT"
        elif flat:
            decision = "HOLD_CASH_NO_TARGET"
        elif outside_band:
            decision = "REBALANCE_OUTSIDE_NTZ"
        else:
            decision = "HOLD_INSIDE_NTZ"

        orders: List[Dict[str, Any]] = []
        if decision in ("INITIAL_DEPLOYMENT", "REBALANCE_OUTSIDE_NTZ"):
            orders = self._plan_orders(nlv, target_sat, quotes)
            self._apply_orders(orders)
            self._push_book_to_sync(nlv)
            for order in orders:
                self.sink.write("SHADOW_REBALANCE_ORDER", **order)
            state = self.sync.get_live_portfolio_state()

        residual = float(state.cash_buffer)
        rec = dict(
            base,
            decision=decision,
            orders=len(orders),
            post_core_weight=round(float(state.core_weight), 4),
            post_satellite_weight=round(float(state.satellite_weight), 4),
            post_cash=round(residual, 2),
            post_cash_pct_nlv=round(residual / nlv, 4) if nlv > 0 else 0.0,
            satellite_target_gap=round(float(state.satellite_weight) - target_sat, 4),
        )
        self.sink.write("SHADOW_REBALANCE_SUMMARY", **rec)
        return rec

    def _plan_orders(self, nlv: float, target_sat: float, quotes: Dict[str, Any]) -> List[Dict[str, Any]]:
        sleeves: List[Tuple[str, float, Dict[str, float]]] = [
            ("SATELLITE", target_sat, self.satellite_vector),
            ("CORE", 1.0 - target_sat, self.routing_vector),
        ]
        desired: Dict[str, int] = {}
        for _, sleeve_w, vec in sleeves:
            for sym, w_c in vec.items():
                ask = quotes[sym].ask
                desired[sym] = int(math.floor((nlv * sleeve_w * w_c) / ask)) if sleeve_w > 0 else 0

        orders: List[Dict[str, Any]] = []
        for sym in SHADOW_UNIVERSE:
            held = self._book.get(sym, (0, 0.0))[0]
            diff = desired.get(sym, 0) - held
            if diff == 0:
                continue
            side = "BUY" if diff > 0 else "SELL"
            px = quotes[sym].ask if diff > 0 else quotes[sym].bid
            self._order_seq += 1
            orders.append({
                "order_id": f"SHADOW-{datetime.now(ET).strftime('%y%m%d')}-{self._order_seq:04d}",
                "symbol": sym,
                "side": side,
                "shares": abs(diff),
                "price": round(px, 4),
                "notional": round(abs(diff) * px, 2),
                "sleeve": "SATELLITE" if sym in self.satellite_vector else "CORE",
                "target_shares": desired.get(sym, 0),
                "broker_submitted": False,
            })
        # Sells first so proceeds fund buys; trim buys if cash would go negative
        orders.sort(key=lambda o: 0 if o["side"] == "SELL" else 1)
        cash = float(self._cash or 0.0)
        feasible: List[Dict[str, Any]] = []
        for o in orders:
            if o["side"] == "SELL":
                cash += o["notional"]
                feasible.append(o)
            elif o["notional"] <= cash + 1e-9:
                cash -= o["notional"]
                feasible.append(o)
            else:
                o = dict(o, side="BUY_REJECTED_INSUFFICIENT_CASH")
                self.sink.write("SHADOW_REBALANCE_ORDER", **o)
        return feasible

    def _apply_orders(self, orders: List[Dict[str, Any]]) -> None:
        for o in orders:
            sym, qty, px = o["symbol"], int(o["shares"]), float(o["price"])
            held, avg = self._book.get(sym, (0, 0.0))
            if o["side"] == "BUY":
                new_qty = held + qty
                new_avg = ((held * avg) + qty * px) / new_qty if new_qty else 0.0
                self._book[sym] = (new_qty, new_avg)
                self._cash = float(self._cash or 0.0) - qty * px
            else:
                self._book[sym] = (held - qty, avg)
                self._cash = float(self._cash or 0.0) + qty * px
        self._book = {s: v for s, v in self._book.items() if v[0] > 0}
