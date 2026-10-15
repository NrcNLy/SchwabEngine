"""
execution/adaptive_envelope.py
==============================
Adaptive Volatility Envelope (AVE) Execution Engine (#58).
Provides Yang-Zhang variance calculation, dynamic take-profit decay,
and quadratic trailing squeeze for leveraged ETF intraday execution.

Mathematical Governance:
-------------------------
1. Normalized Session Clock:
   tau = (t - T_open) / (T_close - T_open), clamped to [0.0, 1.0]

2. Quadratic Squeeze Factor:
   lambda(tau) = 1.0 - tau^2
   (Quadratic decay preserves wide envelope during morning session, accelerating
    contraction during afternoon/pre-close convergence).

3. Yang-Zhang Drift-Independent Volatility Estimator:
   sigma_yz^2 = sigma_o^2 + k * sigma_c^2 + (1 - k) * sigma_rs^2
   where k = 0.34 / (1.34 + (n + 1) / (n - 1))
   Falls back smoothly to close-to-close log return stddev or ATR for n < 5 bars.

4. Dynamic Take-Profit Decay:
   U(t) = P_entry * (1.0 + k_u * sigma_yz * lambda(tau)) + drift_offset
   Target smoothly decays throughout the trading session to lock in morning alpha.

5. Dynamic Trailing Squeeze (Supremum Ratchet):
   L(t) = M(t) * (1.0 - k_l * sigma_yz * lambda(tau))
   where M(t) = sup_{s in [t0, t]} P(s)
   As tau -> 1.0 (session close), lambda -> 0.0 and L(t) -> M(t).

6. Asymmetric Breach Detection:
   - Lower bound breached when BID price <= L(t)
   - Upper bound breached when LAST price >= U(t)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timezone
from enum import Enum
import math
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple
from collections import deque

from indicators.volatility import OHLCBar, compute_yang_zhang_volatility


class BreachType(str, Enum):
    NONE = "NONE"
    UPPER_TAKE_PROFIT = "UPPER_TAKE_PROFIT"
    LOWER_TRAILING_STOP = "LOWER_TRAILING_STOP"
    TERMINAL_FLUSH = "TERMINAL_FLUSH"


@dataclass(frozen=True, slots=True)
class EnvelopeState:
    """Immutable snapshot of the dynamic envelope coordinates."""
    timestamp: datetime
    price: float
    bid_price: float
    supremum: float
    upper_bound: float
    lower_bound: float
    squeeze_factor: float
    sigma_yz: float
    breach: BreachType
    is_tightened: bool
    tau: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "price": round(self.price, 4),
            "bid_price": round(self.bid_price, 4),
            "supremum": round(self.supremum, 4),
            "upper_bound": round(self.upper_bound, 4),
            "lower_bound": round(self.lower_bound, 4),
            "squeeze_factor": round(self.squeeze_factor, 4),
            "sigma_yz": round(self.sigma_yz, 6),
            "breach": self.breach.value,
            "is_tightened": self.is_tightened,
            "tau": round(self.tau, 4),
        }


class AdaptiveEnvelopeExecutor:
    """
    Execution-layer manager for the Adaptive Volatility Envelope.
    Integrates Yang-Zhang continuous variance tracking, quadratic trailing squeeze,
    and take-profit decay.
    """

    def __init__(
        self,
        symbol: str,
        entry_price: float,
        entry_time: datetime,
        session_open: datetime,
        session_close: datetime,
        k_u: float = 1.5,
        k_l: float = 1.5,
        gamma: float = 2.0,  # Default 2.0: Quadratic squeeze
        min_k_l: float = 0.25,
        upper_breach_mode: str = "tighten",  # "tighten" | "exit"
        spike_filter_sigma_mult: float = 4.0,
        baseline_sigma: float = 0.02,
        history_window: int = 30,
    ) -> None:
        if float(entry_price) <= 0.0:
            raise ValueError(f"entry_price must be positive, got {entry_price}")
        if session_close <= session_open:
            raise ValueError(f"session_close must be greater than session_open ({session_close} <= {session_open})")

        self.symbol: str = str(symbol).upper()
        self.entry_price: float = float(entry_price)
        self.entry_time: datetime = entry_time
        self.session_open: datetime = session_open
        self.session_close: datetime = session_close

        self.k_u: float = float(k_u)
        self.k_l: float = float(k_l)
        self.initial_k_l: float = float(k_l)
        self.gamma: float = float(gamma)
        self.min_k_l: float = float(min_k_l)
        self.upper_breach_mode: str = upper_breach_mode.lower()
        self.spike_filter_sigma_mult: float = float(spike_filter_sigma_mult)
        self.baseline_sigma: float = float(baseline_sigma)

        self.total_session_seconds: float = max(
            1.0, (self.session_close - self.session_open).total_seconds()
        )
        self.supremum: float = self.entry_price
        self.is_tightened: bool = False
        self.last_tick_time: Optional[datetime] = None

        # Rolling bar buffer for Yang-Zhang volatility
        self._bar_buffer: Deque[OHLCBar] = deque(maxlen=max(5, history_window))
        self._current_sigma_yz: float = self.baseline_sigma

    @property
    def current_sigma_yz(self) -> float:
        """Current Yang-Zhang volatility estimate."""
        return self._current_sigma_yz

    def add_bar(self, bar: OHLCBar) -> float:
        """
        Ingests a completed OHLCBar and updates continuous Yang-Zhang volatility.
        Returns the updated sigma_yz.
        """
        self._bar_buffer.append(bar)
        if len(self._bar_buffer) >= 2:
            computed_vol = compute_yang_zhang_volatility(list(self._bar_buffer))
            if computed_vol > 1e-5:
                self._current_sigma_yz = computed_vol
        return self._current_sigma_yz

    def compute_tau(self, current_time: datetime) -> float:
        """
        Calculates normalized session clock:
        tau = (current_time - session_open) / total_session_seconds in [0.0, 1.0].
        """
        if current_time <= self.session_open:
            return 0.0
        if current_time >= self.session_close:
            return 1.0
        elapsed = (current_time - self.session_open).total_seconds()
        return min(1.0, max(0.0, elapsed / self.total_session_seconds))

    def compute_squeeze_factor(self, current_time: datetime) -> float:
        """
        Quadratic squeeze factor: lambda(tau) = 1.0 - tau^gamma in [0.0, 1.0].
        When gamma=2.0 (quadratic):
        - At tau = 0.0 (open): lambda = 1.0 (envelope at maximum width)
        - At tau = 0.5 (midday): lambda = 0.75 (retains 75% width)
        - At tau = 1.0 (close): lambda = 0.0 (converges completely to supremum)
        """
        tau = self.compute_tau(current_time)
        return max(0.0, min(1.0, 1.0 - math.pow(tau, self.gamma)))

    def apply_tightening(self) -> None:
        """Halves k_l upon upper boundary touch, ratcheting trailing stop tighter."""
        if not self.is_tightened:
            self.k_l = max(self.min_k_l, self.k_l * 0.5)
            self.is_tightened = True

    def evaluate_tick(
        self,
        current_time: datetime,
        current_price: float,
        bid_price: Optional[float] = None,
        sigma_override: Optional[float] = None,
        drift_rate: float = 0.0,
    ) -> EnvelopeState:
        """
        Evaluates dynamic envelope against an incoming tick.
        Updates supremum, computes bounds, and determines breach verdict.
        """
        if self.last_tick_time is not None and current_time < self.last_tick_time:
            current_time = self.last_tick_time
        else:
            self.last_tick_time = current_time

        price = float(current_price)
        bid = float(bid_price) if bid_price is not None and bid_price > 0.0 else price
        sigma = float(sigma_override) if sigma_override is not None else self._current_sigma_yz
        sigma = max(1e-6, sigma)

        # Monotonic supremum ratcheting with outlier spike filter
        max_valid_jump = self.supremum * (1.0 + self.spike_filter_sigma_mult * sigma)
        if price > max_valid_jump:
            bid_confirms = (bid_price is not None) and (bid >= max_valid_jump * 0.98)
            if bid_confirms:
                self.supremum = price
        else:
            if price > self.supremum:
                self.supremum = price

        tau = self.compute_tau(current_time)
        lam = self.compute_squeeze_factor(current_time)

        # Elapsed drift offset
        elapsed_entry = max(0.0, (current_time - self.entry_time).total_seconds())
        drift_offset = drift_rate * elapsed_entry

        # Upper bound: Take-Profit Decay
        raw_upper = self.entry_price * (1.0 + (self.k_u * sigma * lam)) + drift_offset
        upper_bound = max(self.entry_price * 1.0001, raw_upper)

        # Lower bound: Quadratic Trailing Squeeze (supremum pegged)
        lower_bound = self.supremum * (1.0 - (self.k_l * sigma * lam))

        # Check breach conditions
        breached_upper = (price >= upper_bound)
        breached_lower = (bid <= lower_bound)

        breach = BreachType.NONE
        if breached_upper:
            if self.upper_breach_mode == "tighten":
                self.apply_tightening()
                # Recalculate lower bound with tightened k_l
                lower_bound = self.supremum * (1.0 - (self.k_l * sigma * lam))
                breached_lower = (bid <= lower_bound)
                if breached_lower:
                    breach = BreachType.LOWER_TRAILING_STOP
            else:
                breach = BreachType.UPPER_TAKE_PROFIT
        elif breached_lower:
            breach = BreachType.LOWER_TRAILING_STOP

        # Terminal convergence check at session close
        if tau >= 1.0 and breach == BreachType.NONE:
            breach = BreachType.TERMINAL_FLUSH

        return EnvelopeState(
            timestamp=current_time,
            price=price,
            bid_price=bid,
            supremum=self.supremum,
            upper_bound=upper_bound,
            lower_bound=lower_bound,
            squeeze_factor=lam,
            sigma_yz=sigma,
            breach=breach,
            is_tightened=self.is_tightened,
            tau=tau,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serializes executor state."""
        return {
            "symbol": self.symbol,
            "entry_price": self.entry_price,
            "entry_time": self.entry_time.isoformat(),
            "session_open": self.session_open.isoformat(),
            "session_close": self.session_close.isoformat(),
            "supremum": self.supremum,
            "k_u": self.k_u,
            "k_l": self.k_l,
            "initial_k_l": self.initial_k_l,
            "gamma": self.gamma,
            "is_tightened": self.is_tightened,
            "sigma_yz": self._current_sigma_yz,
            "bar_count": len(self._bar_buffer),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> AdaptiveEnvelopeExecutor:
        """Deserializes executor from stored state."""
        entry_time = datetime.fromisoformat(data["entry_time"])
        session_open = datetime.fromisoformat(data["session_open"])
        session_close = datetime.fromisoformat(data["session_close"])

        inst = cls(
            symbol=data["symbol"],
            entry_price=data["entry_price"],
            entry_time=entry_time,
            session_open=session_open,
            session_close=session_close,
            k_u=data.get("k_u", 1.5),
            k_l=data.get("k_l", 1.5),
            gamma=data.get("gamma", 2.0),
            baseline_sigma=data.get("sigma_yz", 0.02),
        )
        inst.supremum = data.get("supremum", inst.entry_price)
        inst.is_tightened = data.get("is_tightened", False)
        return inst


# Compatibility alias
AdaptiveEnvelope = AdaptiveEnvelopeExecutor
