"""
indicators/volatility.py
========================
Yang-Zhang Drift-Independent Volatility Estimator.

The Yang-Zhang (2000) volatility estimator is a minimum-variance, unbiased,
drift-independent estimator that combines overnight jump variance, open-to-close
variance, and Rogers-Satchell range variance.

Mathematical Formulation:
--------------------------
    sigma_yz^2 = sigma_o^2 + k * sigma_c^2 + (1 - k) * sigma_rs^2

where:
    k = 0.34 / (1.34 + (n + 1) / (n - 1))

    sigma_o^2  = (1 / (n - 1)) * sum((o_i - mean(o))^2)
                 with o_i = ln(Open_i / Close_{i-1})

    sigma_c^2  = (1 / (n - 1)) * sum((c_i - mean(c))^2)
                 with c_i = ln(Close_i / Open_i)

    sigma_rs^2 = (1 / n) * sum(
                     ln(High_i / Close_i) * ln(High_i / Open_i) +
                     ln(Low_i / Close_i)  * ln(Low_i / Open_i)
                 )

Graceful Fallback:
------------------
If fewer than 5 closed bars are available, the estimator smoothly falls back to
normalized close-to-close standard deviation or ATR so that the trading engine
never halts or crashes during the first minutes after market open.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional, Sequence, Tuple

import numpy as np


@dataclass(frozen=True)
class OHLCBar:
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float] = None


def compute_yang_zhang_volatility(bars: Sequence[OHLCBar]) -> float:
    """
    Computes Yang-Zhang historical volatility from a sequence of OHLCBar objects.
    Returns standard deviation (sigma_yz) as a positive float.
    """
    n = len(bars)
    if n < 2:
        return 0.0

    # Fallback for small sample sizes (n < 5)
    if n < 5:
        # Close-to-close log returns variance
        returns = []
        for i in range(1, n):
            c_prev = bars[i - 1].close
            c_curr = bars[i].close
            if c_prev > 0 and c_curr > 0:
                returns.append(math.log(c_curr / c_prev))
        if len(returns) >= 2:
            return statistics.stdev(returns)
        # Approximate from high-low range of the last bar if only 1 return exists
        last = bars[-1]
        if last.close > 0:
            return (last.high - last.low) / last.close
        return 0.0

    # 1. k weighting constant
    k = 0.34 / (1.34 + (n + 1) / (n - 1))

    # 2. Overnight jump returns o_i and Open-to-Close returns c_i
    o_returns: List[float] = []
    c_returns: List[float] = []
    rs_terms: List[float] = []

    for i in range(n):
        bar = bars[i]
        o = max(bar.open, 1e-6)
        h = max(bar.high, 1e-6)
        l = max(bar.low, 1e-6)
        c = max(bar.close, 1e-6)

        # Ensure high is >= open, close and low is <= open, close
        h = max(h, o, c)
        l = min(l, o, c)

        # Open-to-close return
        c_i = math.log(c / o)
        c_returns.append(c_i)

        # Overnight return (from bar 1 onwards)
        if i > 0:
            c_prev = max(bars[i - 1].close, 1e-6)
            o_i = math.log(o / c_prev)
            o_returns.append(o_i)

        # Rogers-Satchell term
        rs_i = (math.log(h / c) * math.log(h / o)) + (math.log(l / c) * math.log(l / o))
        rs_terms.append(max(0.0, rs_i))

    # Calculate overnight variance (sigma_o^2)
    if len(o_returns) >= 2:
        sigma_o_sq = statistics.variance(o_returns)
    elif len(o_returns) == 1:
        sigma_o_sq = o_returns[0] ** 2
    else:
        sigma_o_sq = 0.0

    # Calculate open-to-close variance (sigma_c^2)
    sigma_c_sq = statistics.variance(c_returns) if len(c_returns) >= 2 else 0.0

    # Calculate Rogers-Satchell variance (sigma_rs^2)
    sigma_rs_sq = sum(rs_terms) / n

    # Combine into Yang-Zhang total variance
    sigma_yz_sq = sigma_o_sq + (k * sigma_c_sq) + ((1.0 - k) * sigma_rs_sq)
    return math.sqrt(max(0.0, sigma_yz_sq))


class YangZhangEstimator:
    """
    Streaming accumulator maintaining a rolling window of OHLC bars for a single symbol.
    """

    def __init__(self, window_size: int = 30):
        self.window_size = max(5, int(window_size))
        self._bars: Deque[OHLCBar] = deque(maxlen=self.window_size)

    def add_bar(self, open_p: float, high_p: float, low_p: float, close_p: float, volume: Optional[float] = None) -> float:
        """
        Adds a completed OHLC bar and returns the latest Yang-Zhang volatility estimate.
        """
        bar = OHLCBar(open=float(open_p), high=float(high_p), low=float(low_p), close=float(close_p), volume=volume)
        self._bars.append(bar)
        return self.current_volatility()

    def current_volatility(self) -> float:
        """
        Returns the current volatility estimate over the active bar buffer.
        """
        if not self._bars:
            return 0.0
        return compute_yang_zhang_volatility(list(self._bars))

    def current_hurst(self) -> Optional[float]:
        """
        Calculates the rolling Hurst exponent from closed-bar log returns.
        Returns a float between 0.0 and 1.0, or None if fewer than 10 closed bars exist.
        """
        bars = list(self._bars)
        if len(bars) < 10:
            return None
        closes = np.array([b.close for b in bars], dtype=float)
        if np.any(closes <= 0):
            return None
        log_returns = np.diff(np.log(closes))
        return compute_hurst_rs(log_returns)

    @property
    def bar_count(self) -> int:
        return len(self._bars)

    def clear(self) -> None:
        self._bars.clear()


def anis_lloyd_expected_rs(n: int) -> float:
    """
    Theoretical expected R/S value for i.i.d. Gaussian series of length n (Anis & Lloyd, 1976).
    Corrects small-sample bias so that Brownian noise centers at H = 0.50.
    """
    if n <= 2:
        return 1.0
    term1 = math.gamma((n - 1) / 2.0) / (math.sqrt(math.pi) * math.gamma(n / 2.0))
    term2 = sum(math.sqrt((n - i) / i) for i in range(1, n))
    return term1 * term2


def compute_hurst_rs(log_returns: Sequence[float] | np.ndarray) -> float:
    """
    Computes the Hurst Exponent (H) using Rescaled Range (R/S) analysis with Anis-Lloyd correction.

    Interpretation:
        H <= 0.50: Mean-reverting / anti-persistent (chop / consolidation)
        0.50 < H <= 0.55: Geometric Brownian Motion (random walk / uninformative)
        H > 0.55: Trend-reinforcing / persistent (directional momentum)

    Returns:
        float: Estimated Hurst exponent in [0.0, 1.0]. Returns 0.50 on insufficient data (<10 points)
               or zero variance.
    """
    series = np.asarray(log_returns, dtype=float)
    n = len(series)
    if n < 10:
        return 0.50

    stdev = float(np.std(series))
    if stdev < 1e-9:
        return 0.50

    candidate_lags = [8, 12, 16, 24, 32, 48, 64]
    lags = [tau for tau in candidate_lags if tau <= n]
    if len(lags) < 2:
        lags = [max(4, n // 3), max(6, (2 * n) // 3), n]
        lags = sorted(list(set([l for l in lags if 4 <= l <= n])))
        if len(lags) < 2:
            return 0.50

    log_lags: List[float] = []
    log_ratio: List[float] = []

    for tau in lags:
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
                log_lags.append(float(np.log(tau)))
                log_ratio.append(float(np.log(avg_rs) - np.log(e_rs)))

    if len(log_lags) < 2:
        return 0.50

    slope, _ = np.polyfit(log_lags, log_ratio, 1)
    return float(np.clip(0.5 + slope, 0.0, 1.0))
