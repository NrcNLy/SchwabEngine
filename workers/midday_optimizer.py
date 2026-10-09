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

from core.baselines_store import SQLiteBaselinesStore
from core.ipc import write_dynamic_policy
from execution.strategies import CandleBar, _compute_ci
from indicators.volatility import compute_hurst_rs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("midday_optimizer")

EDT_TZ = ZoneInfo("America/New_York")
DEFAULT_SYMBOLS = ["SOXL", "TQQQ", "TNA"]


class MiddayRegimeOptimizer:
    def __init__(
        self,
        store: Optional[SQLiteBaselinesStore] = None,
        symbols: Optional[List[str]] = None,
    ):
        self.store = store or SQLiteBaselinesStore()
        self.symbols = [s.upper() for s in (symbols or DEFAULT_SYMBOLS)]
        self.max_processes = max(1, (os.cpu_count() or 4) - 2)

    def evaluate_symbol_regime(
        self,
        symbol: str,
        recent_bars: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[float, float, str]:
        """
        Evaluates Hurst Exponent (H) and Choppiness Index (CI) over 5-day + morning window.
        Returns: (hurst_h, ci, classification)
        """
        sym = symbol.upper()
        if recent_bars is None:
            # Query up to 2,000 1-minute bars (~5 trading days + morning drive)
            recent_bars = self.store.get_bars(sym, limit=2000)

        if len(recent_bars) < 20:
            logger.warning("Insufficient bars (%d) for %s; default to random walk.", len(recent_bars), sym)
            return 0.50, 50.0, "INSUFFICIENT_DATA"

        closes = np.array([float(b["close"]) for b in recent_bars], dtype=float)
        log_returns = np.diff(np.log(closes))
        hurst_h = float(compute_hurst_rs(log_returns))

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

        if hurst_h <= 0.50:
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
        if hurst_h <= 0.50:
            rvol_hurdle = 1.65
            size_scale = 0.35
            ratchet_permitted = False
            rationale = (
                f"Morning tape exhibits mean-reverting chop (H={hurst_h:.3f} <= 0.50, CI={ci:.1f}). "
                "Bumping Power Hour RVOL hurdle to 1.65x and scaling sizing to 0.35x."
            )
        elif hurst_h <= 0.55:
            rvol_hurdle = 1.40
            size_scale = 0.45
            ratchet_permitted = False
            rationale = (
                f"Morning tape exhibits random walk noise (H={hurst_h:.3f}, CI={ci:.1f}). "
                "Moderating Power Hour RVOL hurdle to 1.40x and sizing to 0.45x."
            )
        else:
            rvol_hurdle = 1.25
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

        if mean_h <= 0.50:
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
