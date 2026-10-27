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

import asyncio
import json
import logging
import math
import subprocess
import sys
import threading
import time
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from data.multi_rate_sync import (
    DEFAULT_CORE_SYMBOLS,
    DEFAULT_SATELLITE_SYMBOLS,
    MultiRateStateSynchronizer,
)
from core.paths import MACRO_PRIOR_PATH
from execution.risk_manager import MetaGovernorHMM
from strategies.regime_governor import AsymmetricHMMGovernor

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
    path = project_root / "config" / "strategy_config.json"
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
    """
    Thread-safe append-only JSONL telemetry sink with dynamic daily rotation (#130).
    Streamer-thread and asyncio-safe. Automatically rolls the active JSONL file handle
    when the date changes in US/Eastern time, rather than binding permanently to the
    daemon boot timestamp.
    """

    def __init__(self, path_or_root: Path, dynamic_rotation: bool = True) -> None:
        p = Path(path_or_root)
        if p.suffix == ".jsonl":
            self._explicit_path: Optional[Path] = p
            self._project_root: Path = p.parent.parent if p.parent.name == "logs" else p.parent
        else:
            self._explicit_path = None
            self._project_root = p
        self.dynamic_rotation = dynamic_rotation
        self._lock = threading.Lock()
        self.records_written = 0

    @property
    def path(self) -> Path:
        """Dynamically resolves the log file for the current ET trading date."""
        if not self.dynamic_rotation and self._explicit_path is not None:
            return self._explicit_path
        if self._explicit_path is not None and not self._explicit_path.name.startswith("session_telemetry_"):
            return self._explicit_path
        return session_telemetry_path(self._project_root)

    def write(self, event: str, **payload: Any) -> None:
        target_path = self.path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        record: Dict[str, Any] = {
            "event": event,
            "timestamp_iso": datetime.now(ET).isoformat(timespec="milliseconds"),
            "engine_version": ENGINE_VERSION,
            "mode": "shadow",
        }
        record.update(payload)
        line = json.dumps(record, default=str)
        with self._lock:
            with open(target_path, "a", encoding="utf-8") as fh:
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
        governor: Optional[AsymmetricHMMGovernor] = None,
        macro_prior_path: Optional[Path] = None,
    ) -> None:
        self.sync = sync
        self.sink = sink
        self.hmm = hmm
        self.governor: AsymmetricHMMGovernor = governor if governor is not None else AsymmetricHMMGovernor()
        self.macro_prior_path: Path = Path(macro_prior_path) if macro_prior_path is not None else MACRO_PRIOR_PATH
        self._last_cold_signal_ts: Optional[str] = None
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

    def evaluate(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Runs one shadow rebalance cycle. Returns the summary record that was written."""
        if self._cash is None:
            self._adopt_broker_book()

        now_et = now or datetime.now(ET)
        if now_et.tzinfo is None:
            now_et = now_et.replace(tzinfo=ET)
        else:
            now_et = now_et.astimezone(ET)

        # 15:50 EDT Mandatory Satellite Liquidation Gate (#132)
        is_eod = now_et.time() >= dtime(15, 50, 0)
        held_satellites = {
            sym: self._book.get(sym, (0, 0.0))[0]
            for sym in DEFAULT_SATELLITE_SYMBOLS
            if self._book.get(sym, (0, 0.0))[0] > 0
        }

        # Step 1: Ingest Cold Plane Macro Prior if available and fresh (#130)
        cold_signal = None
        try:
            cold_signal = self.governor.ingest_cold_plane_signal(
                signal_path=self.macro_prior_path,
                max_age_seconds=3600.0,
            )
            if cold_signal is not None and cold_signal.timestamp_utc != self._last_cold_signal_ts:
                self._last_cold_signal_ts = cold_signal.timestamp_utc
                self.hmm.pi = list(self.governor.pi)
                logger.info(
                    "ShadowRebalancer: fresh cold plane signal ingested (model=%s dominant=%s w_sat=%.2f cb_override=%s)",
                    cold_signal.model_version,
                    cold_signal.dominant_prior,
                    cold_signal.satellite_target_weight,
                    cold_signal.circuit_breaker_override,
                )
        except Exception as exc:
            logger.debug("ShadowRebalancer: cold plane ingestion error: %s", exc)

        macro_vec = self.sync.get_macro_feature_vector()
        pi = self.hmm.update(macro_vec)

        # Step 2: Determine Dominant Regime, Target Weight, and Leland NTZ Band
        if is_eod:
            dominant = "Shock" if self.governor.tail_risk_override_active or (cold_signal and cold_signal.circuit_breaker_override) else self._regime_key(self.hmm.get_dominant_regime())
            target_sat = 0.0
            lo, hi = 0.0, 0.0
        elif self.governor.tail_risk_override_active or (cold_signal and cold_signal.circuit_breaker_override):
            dominant = "Shock"
            target_sat = 0.0
            lo, hi = 0.0, 0.0
        else:
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
        if cold_signal is not None:
            base["cold_plane_prior"] = cold_signal.dominant_prior
            base["cold_plane_model"] = cold_signal.model_version
            base["cold_plane_cb_override"] = cold_signal.circuit_breaker_override

        # Mandatory EOD Liquidation Event (#132)
        if is_eod and held_satellites:
            orders: List[Dict[str, Any]] = []
            for sym in sorted(held_satellites.keys()):
                held = held_satellites[sym]
                q = quotes.get(sym) or self.sync.get_latest_quote(sym)
                px = (
                    q.bid if (q is not None and q.bid > 0.0)
                    else (q.microprice if (q is not None and q.microprice > 0.0) else self._book[sym][1])
                )
                self._order_seq += 1
                orders.append({
                    "order_id": f"SHADOW-{now_et.strftime('%y%m%d')}-{self._order_seq:04d}",
                    "symbol": sym,
                    "side": "SELL",
                    "shares": held,
                    "price": round(px, 4),
                    "notional": round(held * px, 2),
                    "sleeve": "SATELLITE",
                    "target_shares": 0,
                    "broker_submitted": False,
                })
            self._apply_orders(orders)
            self._push_book_to_sync(nlv)
            for order in orders:
                self.sink.write("SHADOW_REBALANCE_ORDER", **order)
            state = self.sync.get_live_portfolio_state()
            logger.info(
                "ShadowRebalancer: EOD mandatory satellite liquidation executed (%d orders, settled cash=%.2f)",
                len(orders),
                state.cash_buffer,
            )
            residual = float(state.cash_buffer)
            rec = dict(
                base,
                decision="EOD_MANDATORY_LIQUIDATION",
                orders=len(orders),
                post_core_weight=round(float(state.core_weight), 4),
                post_satellite_weight=round(float(state.satellite_weight), 4),
                post_cash=round(residual, 2),
                post_cash_pct_nlv=round(residual / nlv, 4) if nlv > 0 else 0.0,
                satellite_target_gap=round(float(state.satellite_weight) - target_sat, 4),
            )
            self.sink.write("SHADOW_REBALANCE_SUMMARY", **rec)
            return rec

        missing = [s for s, q in quotes.items() if q is None or q.ask <= 0.0 or q.bid <= 0.0]
        stale = [s for s, q in quotes.items() if q is not None and q.is_stale]

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
            orders = self._plan_orders(nlv, target_sat, quotes, date_str=now_et.strftime('%y%m%d'))
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

    def _plan_orders(
        self,
        nlv: float,
        target_sat: float,
        quotes: Dict[str, Any],
        date_str: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
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
        d_str = date_str or datetime.now(ET).strftime("%y%m%d")
        for sym in SHADOW_UNIVERSE:
            held = self._book.get(sym, (0, 0.0))[0]
            diff = desired.get(sym, 0) - held
            if diff == 0:
                continue
            side = "BUY" if diff > 0 else "SELL"
            px = quotes[sym].ask if diff > 0 else quotes[sym].bid
            self._order_seq += 1
            orders.append({
                "order_id": f"SHADOW-{d_str}-{self._order_seq:04d}",
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


DRIVE_SYNC_SCRIPT = Path("scripts") / "sync_engine_drive.py"


def _run_drive_sync_worker(project_root: Optional[Path] = None, timeout: float = 180.0) -> bool:
    """Runs one authoritative Local -> Drive pass (scripts/sync_engine_drive.py --once).

    Exits cleanly (returns True) on hosts without a Drive mount such as the Linux VM.
    """
    if project_root is None:
        project_root = Path(__file__).resolve().parent.parent
    sync_script = project_root / DRIVE_SYNC_SCRIPT
    cmd_sync = [sys.executable, str(sync_script), "--once", "--skip-if-unavailable"]
    logger.info("Executing automated Drive sync pass: %s", " ".join(cmd_sync))
    try:
        res_sync = subprocess.run(
            cmd_sync,
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=timeout,
        )
        if res_sync.returncode != 0:
            logger.error("sync_engine_drive failed (rc=%d): %s", res_sync.returncode, res_sync.stderr or res_sync.stdout)
            return False
        logger.info("sync_engine_drive completed: %s", res_sync.stdout.strip().splitlines()[-2:] if res_sync.stdout else "")
        return True
    except Exception as exc:
        logger.exception("Failed to execute sync_engine_drive: %s", exc)
        return False


async def run_drive_sync(project_root: Optional[Path] = None) -> bool:
    """Non-blocking async wrapper for a single Drive sync pass (periodic + shutdown hooks)."""
    return await asyncio.to_thread(_run_drive_sync_worker, project_root)


def _run_digest_and_sync_worker(
    project_root: Optional[Path] = None,
    log_path: Optional[Path] = None,
    output_path: Optional[Path] = None,
) -> bool:
    """Synchronous worker that invokes parse_session_telemetry.py and then sync_engine_drive.py."""
    if project_root is None:
        project_root = Path(__file__).resolve().parent.parent

    if log_path is None:
        log_path = session_telemetry_path(project_root)
        if not log_path.exists():
            candidates = sorted((project_root / "logs").glob("session_telemetry_*.jsonl"))
            if candidates:
                log_path = candidates[-1]

    if not log_path or not log_path.exists():
        logger.warning("EOD Digest: active session telemetry log does not exist (%s); skipping digest, syncing Drive only.", log_path)
        _run_drive_sync_worker(project_root)
        return False

    parse_script = project_root / "scripts" / "parse_session_telemetry.py"

    cmd_parse = [sys.executable, str(parse_script), "--log", str(log_path)]
    if output_path:
        cmd_parse.extend(["--output", str(output_path)])

    logger.info("Executing automated EOD telemetry digest: %s", " ".join(cmd_parse))
    try:
        res_parse = subprocess.run(
            cmd_parse,
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=120,
        )
        if res_parse.returncode != 0:
            logger.error("parse_session_telemetry failed (rc=%d): %s", res_parse.returncode, res_parse.stderr)
            return False
        logger.info("parse_session_telemetry completed: %s", res_parse.stdout.strip())
    except Exception as exc:
        logger.exception("Failed to execute parse_session_telemetry: %s", exc)
        return False

    return _run_drive_sync_worker(project_root)


async def run_eod_digest_and_sync(
    project_root: Optional[Path] = None,
    log_path: Optional[Path] = None,
    output_path: Optional[Path] = None,
) -> bool:
    """Non-blocking async task wrapper for automated EOD digest and Drive sync."""
    return await asyncio.to_thread(_run_digest_and_sync_worker, project_root, log_path, output_path)
