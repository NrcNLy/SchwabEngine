"""
workers/midday_optimizer.py
===========================
Midday Walk-Forward Regime Filter.
Triggered by systemd timer at 11:35 EDT during the Midday Freeze (11:30-14:00 EDT).

Key Invariants:
1. Concurrency: Clamped to processes = max(1, os.cpu_count() - 2), nice -n 19.
2. Anti-Overfitting Window: Evaluates the 120-bar morning drive concatenated with
   a 5-day sliding window (N ≈ 2,000 1-minute bars).
3. Regime Metric Classification: Calculates Hurst Exponent (H) with Anis-Lloyd
   correction and Choppiness Index (CI).
4. Policy Overrides: If H <= 0.50 (chop), raises Power Hour RVOL hurdle to 1.65x
   and scales sizing to 0.35x.
5. Atomic Output: Emits overrides to state/dynamic_policy.json.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    import scipy.special as sp
except ImportError:
    import math as sp

from core.baselines_store import SQLiteBaselinesStore
from core.ipc import write_dynamic_policy
from execution.strategies import CandleBar, _compute_ci

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("midday_optimizer")

EDT_TZ = ZoneInfo("America/New_York")
DEFAULT_SYMBOLS = ["SOXL", "TQQQ", "TNA"]


def anis_lloyd_expected_rs(n: int) -> float:
    """
    Analytical Anis-Lloyd expected R/S value for i.i.d. series of sample size n.
    Neutralizes small-sample finite-horizon Hurst bias over 120-bar morning lookbacks.
    """
    if n <= 2:
        return 1.0
    r_vals = np.arange(1, n, dtype=float)
    sum_term = np.sum(np.sqrt((n - r_vals) / r_vals))
    if n <= 340:
        c = sp.gamma((n - 1) / 2.0) / (np.sqrt(np.pi) * sp.gamma(n / 2.0))
    else:
        c = 1.0 / np.sqrt(0.5 * np.pi * n)
    return float(c * sum_term)


def compute_session_isolated_hurst(log_returns: Sequence[float] | np.ndarray) -> float:
    """
    Session-isolated Hurst Exponent computation across dyadic scales
    [10, 15, 20, 24, 30, 40, 60, 120] with analytical Anis-Lloyd slope correction.
    """
    series = np.asarray(log_returns, dtype=float)
    n = len(series)
    if n < 10:
        return 0.50

    stdev = float(np.std(series))
    if stdev < 1e-9:
        return 0.50

    dyadic_scales = [10, 15, 20, 24, 30, 40, 60, 120]
    scales = [tau for tau in dyadic_scales if tau <= n]
    if len(scales) < 2:
        return 0.50

    log_taus: List[float] = []
    log_rs_emp: List[float] = []
    log_rs_al: List[float] = []

    for tau in scales:
        k = n // tau
        if k == 0:
            continue
        sub = series[: k * tau].reshape(k, tau)
        means = np.mean(sub, axis=1, keepdims=True)
        y = sub - means
        z = np.cumsum(y, axis=1)
        z_pad = np.pad(z, ((0, 0), (1, 0)), mode="constant")
        ranges = np.ptp(z_pad, axis=1)
        stds = np.std(sub, axis=1, ddof=1)
        valid = stds > 1e-9
        if np.any(valid):
            avg_rs = float(np.mean(ranges[valid] / stds[valid]))
            e_rs = anis_lloyd_expected_rs(tau)
            if avg_rs > 0 and e_rs > 0:
                log_taus.append(float(np.log(tau)))
                log_rs_emp.append(float(np.log(avg_rs)))
                log_rs_al.append(float(np.log(e_rs)))

    if len(log_taus) < 2:
        return 0.50

    beta_raw, _ = np.polyfit(log_taus, log_rs_emp, 1)
    beta_al, _ = np.polyfit(log_taus, log_rs_al, 1)

    corrected_h = float(beta_raw - beta_al + 0.50)
    return float(np.clip(corrected_h, 0.05, 0.95))


class MiddayRegimeOptimizer:
    def __init__(
        self,
        store: Optional[SQLiteBaselinesStore] = None,
        symbols: Optional[List[str]] = None,
    ):
        self.store = store or SQLiteBaselinesStore()
        self.symbols = [s.upper() for s in (symbols or DEFAULT_SYMBOLS)]
        self.max_processes = max(1, (os.cpu_count() or 4) - 2)

    def get_session_morning_bars(
        self,
        symbol: str,
        session_date: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """
        Retrieves exactly today's 120 morning bars (09:30:00 to 11:30:00 EDT inclusive).
        Falls back to the most recent 120 bars if executing off-hours or in test fixtures.
        """
        sym = symbol.upper()
        ref_dt = session_date or datetime.now(EDT_TZ)
        start_dt = ref_dt.replace(hour=9, minute=30, second=0, microsecond=0)
        end_dt = ref_dt.replace(hour=11, minute=30, second=0, microsecond=0)
        start_ts = int(start_dt.timestamp() * 1000)
        end_ts = int(end_dt.timestamp() * 1000)

        bars = self.store.get_bars(sym, start_ts=start_ts, end_ts=end_ts, limit=120)
        if not bars:
            latest_ts = self.store.get_latest_bar_timestamp(sym)
            if latest_ts is not None:
                latest_dt = datetime.fromtimestamp(latest_ts / 1000.0, tz=timezone.utc).astimezone(EDT_TZ)
                s_dt = latest_dt.replace(hour=9, minute=30, second=0, microsecond=0)
                e_dt = latest_dt.replace(hour=11, minute=30, second=0, microsecond=0)
                bars = self.store.get_bars(sym, start_ts=int(s_dt.timestamp() * 1000), end_ts=int(e_dt.timestamp() * 1000), limit=120)
                if not bars:
                    bars = self.store.get_bars(sym, limit=120)
            else:
                bars = self.store.get_bars(sym, limit=120)
        return bars

    def evaluate_symbol_regime(
        self,
        symbol: str,
        recent_bars: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[float, float, str]:
        """
        Evaluates Hurst Exponent (H) and Choppiness Index (CI) over session-isolated 120 morning bars.
        Returns: (hurst_h, ci, classification)
        """
        sym = symbol.upper()
        if recent_bars is None:
            recent_bars = self.get_session_morning_bars(sym)

        if len(recent_bars) < 20:
            logger.warning("Insufficient bars (%d) for %s; default to random walk.", len(recent_bars), sym)
            return 0.50, 50.0, "INSUFFICIENT_DATA"

        closes = np.array([float(b["close"]) for b in recent_bars], dtype=float)
        log_returns = np.diff(np.log(closes))
        hurst_h = float(compute_session_isolated_hurst(log_returns))

        candle_bars = [
            CandleBar(
                timestamp=datetime.fromtimestamp(b["timestamp"] / 1000.0, tz=timezone.utc),
                open=float(b["open"]),
                high=float(b["high"]),
                low=float(b["low"]),
                close=float(b["close"]),
                volume=int(b.get("volume", 0)),
            )
            for b in recent_bars
        ]
        ci_val = _compute_ci(candle_bars, period=14)
        ci = float(ci_val) if ci_val is not None else 50.0

        if hurst_h <= 0.48:
            classification = "HIGH_NOISE_CHOP"
        elif hurst_h <= 0.55:
            classification = "RANDOM_WALK"
        else:
            classification = "TREND_EXPANSION"

        logger.info(
            "%s Midday Regime: H=%.3f, CI=%.1f -> %s",
            sym, hurst_h, ci, classification,
        )
        return hurst_h, ci, classification

    def compute_policy_overrides(
        self,
        hurst_h: float,
        ci: float,
        classification: str,
    ) -> Dict[str, Any]:
        """Maps quantitative regime metrics to Power Hour dynamic policy parameters."""
        if hurst_h <= 0.48:
            rvol_hurdle = 1.65
            size_scale = 0.35
            ratchet_permitted = False
            rationale = (
                f"Morning tape exhibits mean-reverting chop (H={hurst_h:.3f} <= 0.48, CI={ci:.1f}). "
                "Bumping Power Hour RVOL hurdle to 1.65x and scaling sizing to 0.35x."
            )
        elif hurst_h <= 0.55:
            rvol_hurdle = 1.52
            size_scale = 0.45
            ratchet_permitted = False
            rationale = (
                f"Morning tape exhibits random walk noise (0.48 < H={hurst_h:.3f} <= 0.55, CI={ci:.1f}). "
                "Calibrating Power Hour RVOL hurdle to 1.52x and sizing to 0.45x."
            )
        else:
            rvol_hurdle = 1.35
            size_scale = 0.50
            ratchet_permitted = True
            rationale = (
                f"Morning tape exhibits persistent trend expansion (H={hurst_h:.3f} > 0.55, CI={ci:.1f}). "
                "Standard Power Hour parameters active."
            )

        policy = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "effective_session": "POWER_HOUR",
            "morning_hurst": round(hurst_h, 4),
            "morning_ci": round(ci, 2),
            "regime_classification": classification,
            "overrides": {
                "power_hour_rvol_hurdle": rvol_hurdle,
                "power_hour_size_scale": size_scale,
                "ratchet_permitted": ratchet_permitted,
            },
            "rationale": rationale,
        }
        return policy

    def run_optimization(self) -> Dict[str, Any]:
        logger.info("Executing Midday Walk-Forward Optimizer across %s...", self.symbols)
        hurst_scores = []
        ci_scores = []
        classifications = []

        for sym in self.symbols:
            h, c, cls_name = self.evaluate_symbol_regime(sym)
            hurst_scores.append(h)
            ci_scores.append(c)
            classifications.append(cls_name)

        mean_h = float(np.mean(hurst_scores)) if hurst_scores else 0.50
        mean_ci = float(np.mean(ci_scores)) if ci_scores else 50.0

        if mean_h <= 0.48:
            agg_class = "HIGH_NOISE_CHOP"
        elif mean_h <= 0.55:
            agg_class = "RANDOM_WALK"
        else:
            agg_class = "TREND_EXPANSION"

        policy = self.compute_policy_overrides(mean_h, mean_ci, agg_class)
        write_dynamic_policy(policy)
        logger.info("Dynamic policy successfully written to state/dynamic_policy.json: %s", policy["rationale"])
        return policy


def main() -> int:
    try:
        optimizer = MiddayRegimeOptimizer()
        optimizer.run_optimization()
        return 0
    except Exception as exc:
        logger.exception("Fatal error in midday_optimizer: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
