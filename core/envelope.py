"""
core/envelope.py
================
Adaptive Volatility Envelope (AVE) Mathematical Processing Engine.
Calculates continuous time-decaying probability cones for high-beta leveraged ETFs.

Key Mathematical Invariants:
1. Normalized Session Clock: tau = (t - T_open) / (T_close - T_open) in [0.0, 1.0]
2. Squeeze Factor: lambda(tau) = 1.0 - tau^gamma in [0.0, 1.0]
   - gamma > 1.0 (Convex): envelope stays wide mid-session, accelerates contraction near close (FAVORABLE)
   - gamma = 1.0 (Linear): steady linear decay
   - gamma < 1.0 (Concave): aggressive early contraction (CHOP)
3. Dynamic Boundaries:
   - Upper Bound: U(t) = P_entry * (1.0 + k_u * sigma_t * lambda) + drift_offset
   - Lower Bound: L(t) = M(t) * (1.0 - k_l * sigma_t * lambda)
     where M(t) = sup_{s in [t0, t]} P(s)
4. Bid/Last Asymmetry:
   - Lower bound breached when bid_price <= L(t) (or current_price if bid unavailable)
   - Upper bound breached when current_price >= U(t)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime, timezone
from decimal import Decimal
import math
from typing import Optional, Tuple


@dataclass(frozen=True, slots=True)
class EnvelopeBounds:
    timestamp: datetime
    price: float
    bid_price: float
    supremum: float
    upper_bound: float
    lower_bound: float
    lambda_factor: float
    breached_upper: bool
    breached_lower: bool
    k_u: float
    k_l: float
    gamma: float


class AdaptiveVolatilityEnvelope:
    """
    Manages continuous running supremum and time-decaying envelope boundaries.
    """

    def __init__(
        self,
        entry_price: float,
        entry_time: datetime,
        session_open: datetime,
        session_close: datetime,
        k_u: float = 1.5,
        k_l: float = 1.5,
        gamma: float = 0.6,
        min_k_l: float = 0.25,
        upper_breach_mode: str = "tighten",  # "tighten" or "exit"
        spike_filter_sigma_mult: float = 4.0,
    ) -> None:
        if entry_price <= 0.0:
            raise ValueError(f"entry_price must be > 0, got {entry_price}")
        if session_close <= session_open:
            raise ValueError(f"session_close must be > session_open ({session_close} <= {session_open})")

        self.p_entry: float = float(entry_price)
        self.t_0: datetime = entry_time
        self.session_open: datetime = session_open
        self.session_close: datetime = session_close

        self.k_u: float = float(k_u)
        self.k_l: float = float(k_l)
        self.gamma: float = float(gamma)
        self.min_k_l: float = float(min_k_l)
        self.upper_breach_mode: str = upper_breach_mode.lower()
        self.spike_filter_sigma_mult: float = float(spike_filter_sigma_mult)

        self.total_session_seconds: float = max(
            1.0, (self.session_close - self.session_open).total_seconds()
        )
        self.supremum: float = self.p_entry
        self.is_tightened: bool = False
        self.last_tick_time: Optional[datetime] = None

    def compute_squeeze_factor(self, current_time: datetime) -> float:
        """
        Computes lambda(tau) = 1.0 - tau^gamma where:
        tau = (current_time - session_open) / total_session_seconds, clamped to [0.0, 1.0].
        
        If current_time < session_open: tau = 0.0 => lambda = 1.0
        If current_time >= session_close: tau = 1.0 => lambda = 0.0
        """
        if current_time <= self.session_open:
            return 1.0
        if current_time >= self.session_close:
            return 0.0

        elapsed_sec = (current_time - self.session_open).total_seconds()
        tau = min(1.0, max(0.0, elapsed_sec / self.total_session_seconds))
        return max(0.0, min(1.0, 1.0 - math.pow(tau, self.gamma)))

    def apply_tightening(self) -> None:
        """
        Engages TIGHTENING mode upon upper boundary touch:
        halves k_l (with a floor of min_k_l), locking in running profits.
        """
        if not self.is_tightened:
            self.k_l = max(self.min_k_l, self.k_l * 0.5)
            self.is_tightened = True

    def evaluate_tick(
        self,
        current_time: datetime,
        current_price: float,
        sigma_t: float,
        bid_price: Optional[float] = None,
        drift_rate: float = 0.0,
    ) -> EnvelopeBounds:
        """
        Evaluates dynamic envelope against an incoming tick.
        
        Args:
            current_time: UTC or tz-aware tick timestamp.
            current_price: Executed last trade price.
            sigma_t: Instantaneous volatility estimate (e.g. Yang-Zhang vol normalized).
            bid_price: Optional top of book bid price (used for stop evaluation).
            drift_rate: Realized drift per second.
        """
        # Monotonic time guard: ignore or warn on out-of-order ticks
        if self.last_tick_time is not None and current_time < self.last_tick_time:
            # Drop or clamp backwards time ticks
            current_time = self.last_tick_time
        else:
            self.last_tick_time = current_time

        price = float(current_price)
        bid = float(bid_price) if bid_price is not None and bid_price > 0.0 else price
        sigma = max(1e-6, float(sigma_t))

        # Outlier / bad print filter:
        # If price exceeds supremum * (1.0 + spike_mult * sigma):
        # - If bid_price was explicitly provided and confirms the move (bid >= max_valid_jump * 0.98), allow it.
        # - Otherwise, it's treated as a bad print outlier and does not ratchet supremum.
        max_valid_jump = self.supremum * (1.0 + self.spike_filter_sigma_mult * sigma)
        if price > max_valid_jump:
            bid_confirms = (bid_price is not None) and (bid >= max_valid_jump * 0.98)
            if bid_confirms:
                self.supremum = price
        else:
            if price > self.supremum:
                self.supremum = price

        lam = self.compute_squeeze_factor(current_time)

        # Elapsed time from trade inception for drift offset calculation
        elapsed_since_entry = max(0.0, (current_time - self.t_0).total_seconds())
        drift_offset = drift_rate * elapsed_since_entry

        # Upper bound calculation: clamped >= p_entry * 1.0001
        raw_upper = self.p_entry * (1.0 + (self.k_u * sigma * lam)) + drift_offset
        upper_bound = max(self.p_entry * 1.0001, raw_upper)

        # Lower bound calculation: trails running supremum M(t)
        # As lambda -> 0 near close, lower_bound -> supremum
        lower_bound = self.supremum * (1.0 - (self.k_l * sigma * lam))

        # Breach evaluation:
        # Lower breach: evaluated against BID price (market sell hits bid)
        # Upper breach: evaluated against LAST trade price
        breached_lower = (bid <= lower_bound)
        breached_upper = (price >= upper_bound)

        if breached_upper and self.upper_breach_mode == "tighten":
            self.apply_tightening()
            # In tightening mode, breached_upper does not trigger an immediate liquidation
            # We recalculate lower_bound with tightened k_l
            lower_bound = self.supremum * (1.0 - (self.k_l * sigma * lam))
            breached_lower = (bid <= lower_bound)

        return EnvelopeBounds(
            timestamp=current_time,
            price=price,
            bid_price=bid,
            supremum=self.supremum,
            upper_bound=upper_bound,
            lower_bound=lower_bound,
            lambda_factor=lam,
            breached_upper=breached_upper,
            breached_lower=breached_lower,
            k_u=self.k_u,
            k_l=self.k_l,
            gamma=self.gamma,
        )
