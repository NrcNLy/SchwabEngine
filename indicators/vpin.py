"""
indicators/vpin.py
==================
Directional VPIN on a volume clock with Bulk Volume Classification (BVC).

* Volume clock: every bucket holds exactly ``V = ADV / bucket_divisor`` shares. A print larger than the
  remaining room spills into the following buckets, so one big trade can close several.
* BVC: for bucket tau with price change dP_tau (close_tau - close_{tau-1}) and sigma = EWMA std of dP,
      V_B = V * Phi(dP / sigma),  V_S = V - V_B
  (sigma == 0 -> Phi(0): an even split).
* VPIN = sum_{tau=1..N} |V_B - V_S| / (N * V); the signed mean ``vpin_dir`` = sum (V_B - V_S) / (N * V).
  Buckets are stored as signed fractions of V, so carry-over across days is independent of the ADV used.
* Percentile = empirical CDF of VPIN over the last ``percentile_lookback`` completed buckets.

No ADV -> not ready (there is no default/fabricated ADV). A symbol is ``ready`` only after
``min_buckets`` classified buckets; ``toxic`` additionally requires ``percentile_min_history`` history
points, so cold starts are always neutral.
"""

from __future__ import annotations

import math
from bisect import bisect_right, insort
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional

_SQRT2 = math.sqrt(2.0)


def norm_cdf(x: float) -> float:
    """Standard normal CDF, Phi(x) = 0.5 * (1 + erf(x / sqrt(2)))."""
    return 0.5 * (1.0 + math.erf(x / _SQRT2))


@dataclass(frozen=True)
class VPINReading:
    symbol: str
    ready: bool
    buckets: int
    bucket_volume: float
    vpin: float
    vpin_dir: float
    percentile: float
    percentile_ready: bool
    toxic: bool


class VPINEngine:
    def __init__(
        self,
        symbol: str,
        adv: Optional[float] = None,
        bucket_divisor: int = 50,
        n_buckets: int = 50,
        min_buckets: int = 10,
        percentile_lookback: int = 250,
        percentile_min_history: int = 20,
        sigma_halflife: int = 20,
        sigma_min_buckets: int = 5,
        toxic_percentile: float = 0.90,
    ) -> None:
        self.symbol = symbol.upper()
        self.bucket_divisor = max(1, int(bucket_divisor))
        self.n_buckets = max(1, int(n_buckets))
        self.min_buckets = max(1, int(min_buckets))
        self.percentile_lookback = max(2, int(percentile_lookback))
        self.percentile_min_history = max(2, int(percentile_min_history))
        self.sigma_min_buckets = max(2, int(sigma_min_buckets))
        self.toxic_percentile = float(toxic_percentile)
        self._alpha = 1.0 - 0.5 ** (1.0 / max(1, int(sigma_halflife)))

        self.bucket_volume: Optional[float] = None
        self._cur_vol = 0.0
        self._prev_close: Optional[float] = None

        self._buckets: Deque[float] = deque()      # signed (V_B - V_S) / V per bucket
        self._sum_abs = 0.0
        self._sum_signed = 0.0

        self._hist: Deque[float] = deque()         # completed-bucket VPIN values (insertion order)
        self._sorted: List[float] = []             # same values, sorted, for the empirical CDF

        self._sig_mean = 0.0
        self._sig_var = 0.0
        self._sig_n = 0

        self._last_vpin = 0.0
        if adv is not None:
            self.set_adv(adv)

    # ------------------------------------------------------------------ config
    def set_adv(self, adv: float) -> None:
        if adv is None or not math.isfinite(adv) or adv <= 0:
            return
        self.bucket_volume = float(adv) / self.bucket_divisor
        if self._cur_vol >= self.bucket_volume:
            self._cur_vol = 0.0

    # ------------------------------------------------------------------ ingest
    def on_trade(self, price: float, volume: float, ts_ms: int = 0) -> None:
        v = self.bucket_volume
        if v is None or volume <= 0 or price <= 0 or not math.isfinite(price) or not math.isfinite(volume):
            return
        remaining = float(volume)
        while remaining > 1e-12:
            room = v - self._cur_vol
            take = room if remaining >= room else remaining
            self._cur_vol += take
            remaining -= take
            if self._cur_vol >= v - 1e-9:
                self._close_bucket(price, v)
                self._cur_vol = 0.0

    def _close_bucket(self, close_px: float, v: float) -> None:
        prev = self._prev_close
        self._prev_close = close_px
        if prev is None:
            return  # first close only seeds the price anchor; nothing to classify
        dp = close_px - prev

        # EWMA mean/variance of bucket price changes (West's incremental update)
        self._sig_n += 1
        if self._sig_n == 1:
            self._sig_mean, self._sig_var = dp, 0.0
        else:
            diff = dp - self._sig_mean
            incr = self._alpha * diff
            self._sig_mean += incr
            self._sig_var = (1.0 - self._alpha) * (self._sig_var + diff * incr)
        if self._sig_n < self.sigma_min_buckets:
            return  # sigma not trustworthy yet: skip rather than guess

        sigma = math.sqrt(max(self._sig_var, 0.0))
        v_buy = v * (norm_cdf(dp / sigma) if sigma > 0 else 0.5)
        signed = (v_buy - (v - v_buy)) / v

        self._buckets.append(signed)
        self._sum_abs += abs(signed)
        self._sum_signed += signed
        if len(self._buckets) > self.n_buckets:
            old = self._buckets.popleft()
            self._sum_abs -= abs(old)
            self._sum_signed -= old

        n = len(self._buckets)
        if n >= self.min_buckets:
            self._last_vpin = self._sum_abs / n
            self._push_history(self._last_vpin)

    def _push_history(self, value: float) -> None:
        self._hist.append(value)
        insort(self._sorted, value)
        if len(self._hist) > self.percentile_lookback:
            old = self._hist.popleft()
            idx = bisect_right(self._sorted, old) - 1
            if idx >= 0:
                self._sorted.pop(idx)

    # ------------------------------------------------------------------ read
    def reading(self) -> VPINReading:
        n = len(self._buckets)
        ready = self.bucket_volume is not None and n >= self.min_buckets
        vpin = (self._sum_abs / n) if n else 0.0
        vpin_dir = (self._sum_signed / n) if n else 0.0
        pct_ready = len(self._sorted) >= self.percentile_min_history
        percentile = (bisect_right(self._sorted, vpin) / len(self._sorted)) if (pct_ready and ready) else 0.5
        toxic = bool(ready and pct_ready and percentile >= self.toxic_percentile)
        return VPINReading(
            symbol=self.symbol, ready=ready, buckets=n,
            bucket_volume=float(self.bucket_volume or 0.0),
            vpin=vpin, vpin_dir=vpin_dir, percentile=percentile,
            percentile_ready=pct_ready, toxic=toxic,
        )

    # ------------------------------------------------------------------ carry-over
    def snapshot_state(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "buckets": list(self._buckets),
            "vpin_hist": list(self._hist),
            "sigma": {"mean": self._sig_mean, "var": self._sig_var, "n": self._sig_n},
        }

    def restore_state(self, state: Dict[str, Any]) -> bool:
        """Restores prior-session buckets/history. The price anchor is deliberately NOT restored: the
        overnight gap must not be classified as a bucket move."""
        try:
            buckets = [float(x) for x in state.get("buckets", []) if math.isfinite(float(x))]
            hist = [float(x) for x in state.get("vpin_hist", []) if math.isfinite(float(x))]
            sig = state.get("sigma", {}) or {}
            mean, var, n = float(sig.get("mean", 0.0)), float(sig.get("var", 0.0)), int(sig.get("n", 0))
        except (TypeError, ValueError):
            return False
        self._buckets = deque(max(-1.0, min(1.0, b)) for b in buckets[-self.n_buckets:])
        self._sum_abs = sum(abs(b) for b in self._buckets)
        self._sum_signed = sum(self._buckets)
        self._hist = deque(hist[-self.percentile_lookback:])
        self._sorted = sorted(self._hist)
        self._sig_mean, self._sig_var, self._sig_n = mean, max(var, 0.0), max(n, 0)
        self._prev_close = None
        self._cur_vol = 0.0
        return True
