"""
execution/risk_manager.py
=========================
Bayesian Quarter-Kelly Risk Engine for High-Beta Leveraged ETFs.

Calculates mathematically optimal position sizing by shrinking empirical win rates
toward a prior expectation, then scaling absolute shares inversely to the asset's
current volatility (ATR).

Capital ceilings applied AFTER Kelly/ATR sizing and BEFORE Almgren-Chriss slicing:
    shares <= floor(max_notional / price)
where ``max_notional`` comes from ``core.liquidity_policy.compute_buying_power`` and is
already bounded by settled cash and 20% of NLV. Unsettled funds, pending ACH, SWVXX and
the external backstop never contribute to it.
"""

import math
import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("risk_manager")

# Prior used for Bayesian shrinkage and the Kelly robustness constant.
PRIOR_MEAN = 0.5
ROBUSTNESS_KAPPA = 30


class RiskEngine:
    def __init__(self, wins: int = 45, executions: int = 100, payoff_ratio: float = 1.6):
        self._active_positions = {}
        self.macro_risk_multiplier: float = 1.0
        # Empirical performance inputs. These are static defaults until the
        # trade log feeds them; the posterior (and therefore the high-probability
        # gate) is only as informative as these numbers.
        self.wins = wins
        self.executions = executions
        self.payoff_ratio = payoff_ratio

    def update_macro_risk_multiplier(self, multiplier: float) -> None:
        """Dynamically updates the macro liquidity risk multiplier (0.0 to 1.0)."""
        self.macro_risk_multiplier = max(0.0, min(1.0, float(multiplier)))
        logger.info(f"RiskEngine macro risk multiplier updated to: {self.macro_risk_multiplier:.4f}")

    def posterior_win_rate(self, wins: int = None, executions: int = None, prior_mean: float = PRIOR_MEAN) -> float:
        """Beta-conjugate posterior mean win rate."""
        wins = self.wins if wins is None else wins
        executions = self.executions if executions is None else executions
        return (wins + ROBUSTNESS_KAPPA * prior_mean) / (executions + ROBUSTNESS_KAPPA)

    def calculate_bayesian_kelly_sizing(self, wins: int, executions: int, payoff_ratio: float, account_base: float, prior_mean: float = 0.5) -> float:
        """
        Shrinks empirical win rates toward the prior expectation for small sample sizes
        using Beta distribution conjugate priors.
        """
        robustness_factor = ROBUSTNESS_KAPPA  # kappa parameter
        effective_sample = executions + robustness_factor
        
        # Bayesian Posterior Mean
        bayesian_posterior_win_rate = (wins + (robustness_factor * prior_mean)) / effective_sample
        
        # Prevent negative kelly or divide by zero
        if bayesian_posterior_win_rate <= 0 or payoff_ratio <= 0:
            return 0.0
            
        q = 1.0 - bayesian_posterior_win_rate
        
        # Base Full-Kelly Fraction
        full_kelly = bayesian_posterior_win_rate - (q / payoff_ratio)
        
        # Default to 0 if Kelly indicates no edge
        if full_kelly <= 0:
            return 0.0
            
        # Quarter-Kelly regularization for high-beta variance reduction
        quarter_kelly = full_kelly / 4.0
        
        # Apply the confidence penalty (shrinking early allocations)
        confidence_multiplier = effective_sample / (effective_sample + robustness_factor)
        final_kelly_fraction = quarter_kelly * confidence_multiplier
        
        risk_capital = account_base * final_kelly_fraction
        return risk_capital

    def _scale_inversely_to_atr(self, risk_capital: float, current_atr: float, stop_multiplier: float = 1.5) -> int:
        """
        Scales absolute share count inversely to the asset's current volatility.
        Ensures total dollar risk remains constant across regimes.
        """
        if current_atr <= 0:
            return 0
            
        # The stop distance is a multiple of the current ATR
        stop_distance = current_atr * stop_multiplier
        
        position_size = risk_capital / stop_distance
        return math.floor(position_size)

    def determine_position_size(
        self, 
        ticker: str, 
        atr: float, 
        account_base: float, 
        risk_multiplier: float = None,
        price: float = None,
        max_notional: float = None,
    ) -> int:
        """
        High-level macro wrapper to determine live execution size.

        Args:
            account_base:  Risk base for Kelly (pass NLV).
            risk_multiplier: Optional 0..1 scaler applied to total meta-order shares
                           before Almgren-Chriss slicing (1.0 unless explicitly set).
            price:         Last price, required to apply ``max_notional``.
            max_notional:  Ceiling from compute_buying_power (settled cash, 20% NLV).

        Returns 0 if total scaled shares < 1.
        """
        risk_capital = self.calculate_bayesian_kelly_sizing(
            wins=self.wins, 
            executions=self.executions, 
            payoff_ratio=self.payoff_ratio, 
            account_base=account_base
        )
        
        raw_shares = self._scale_inversely_to_atr(risk_capital, atr)
        mult = self.macro_risk_multiplier if risk_multiplier is None else max(0.0, min(1.0, float(risk_multiplier)))
        
        scaled_shares = math.floor(raw_shares * mult)

        if max_notional is not None:
            if price is None or price <= 0:
                logger.warning(f"[{ticker}] max_notional supplied without a valid price; sizing to 0.")
                return 0
            ceiling = math.floor(max(max_notional, 0.0) / price)
            if ceiling < scaled_shares:
                logger.info(f"[{ticker}] Notional ceiling ${max_notional:.2f} caps shares {scaled_shares} -> {ceiling}")
            scaled_shares = min(scaled_shares, ceiling)
        
        if scaled_shares < 1:
            logger.warning(
                f"[{ticker}] Sizing halted or below 1 share: Raw={raw_shares}, "
                f"Macro Multiplier={mult:.4f} -> Final Target Shares: 0"
            )
            return 0

        logger.info(
            f"[{ticker}] Bayesian Quarter-Kelly Risk Cap: ${risk_capital:.2f} | "
            f"Raw Shares: {raw_shares} | Multiplier: {mult:.4f} -> Final Target Shares: {scaled_shares}"
        )
        return scaled_shares

    def calculate_yang_zhang_stop_distance(
        self,
        high_water_mark: float,
        yz_vol: float,
        k_stop: float = 2.0,
        dt_days: Optional[float] = None,
        intraday_fraction: float = 1.0 / 390.0,
        min_stop_distance_pct: float = 0.005,
    ) -> float:
        """
        Calculates the volatility buffer distance for a trailing stop:
            Stop Distance = High Water Mark * (k_stop * sigma_yz * time_factor)
            where time_factor = math.sqrt(intraday_fraction / 252.0)

        If yz_vol is 0 or uninitialized, falls back to a 2% default floor to avoid zero distance.
        Maintains a mathematical floor (min_stop_distance_pct = 0.005 or 0.5%) so low-volatility
        compression cannot collapse the stop distance into the bid-ask spread.
        """
        if high_water_mark <= 0:
            return 0.0

        effective_vol = float(yz_vol)
        if effective_vol <= 0.0:
            # Safe floor fallback (2% distance)
            return high_water_mark * 0.02

        # Scale daily volatility to intraday resolution using the true intraday bar fraction:
        # time_factor = math.sqrt(intraday_fraction / 252.0), where intraday_fraction = 1.0 / 390.0 for 1-minute bars.
        fraction = float(dt_days) if dt_days is not None else float(intraday_fraction)
        time_factor = math.sqrt(max(fraction, 1e-8) / 252.0)
        distance = high_water_mark * (float(k_stop) * effective_vol * time_factor)
        floor_distance = high_water_mark * float(min_stop_distance_pct)
        return max(distance, floor_distance, 0.01)  # minimum 0.5% buffer, bounded at 1 tick ($0.01)

    def evaluate_trailing_stop(
        self,
        position: Any,
        current_price: float,
        yz_vol: float,
        k_stop: float = 2.0,
        dt_days: Optional[float] = None,
        intraday_fraction: float = 1.0 / 390.0,
        hurst_exponent: Optional[float] = None,
        min_stop_distance_pct: float = 0.005,
    ) -> tuple[float, bool]:
        """
        Calculates the stop distance and updates the position's monotonic stop ratchet
        gated by the Hurst Exponent regime filter.

        Regime Gating Rules:
            - If H <= 0.50 (mean-reverting chop): Freeze the trailing stop ratchet.
              Maintain existing stop level and prevent upward creep during microstructural consolidation.
            - If H > 0.55 (directional persistence): Permit the dynamic stop to ratchet upward with price excursions.
            - If 0.50 < H <= 0.55 (random walk): Retain previous ratchet state.

        Returns (new_stop_price: float, adjusted: bool).
        """
        hwm = float(getattr(position, "high_water_mark", getattr(position, "entry_price", current_price)))
        hwm = max(hwm, current_price)

        # Regime gating via Hurst Exponent
        ratchet_permitted = getattr(position, "ratchet_permitted", True)
        if hurst_exponent is not None:
            h = float(hurst_exponent)
            if h <= 0.50:
                ratchet_permitted = False
            elif h > 0.55:
                ratchet_permitted = True
            # 0.50 < h <= 0.55 retains previous ratchet_permitted state

        if hasattr(position, "ratchet_permitted"):
            position.ratchet_permitted = ratchet_permitted

        dist = self.calculate_yang_zhang_stop_distance(
            hwm,
            yz_vol,
            k_stop=k_stop,
            dt_days=dt_days,
            intraday_fraction=intraday_fraction,
            min_stop_distance_pct=min_stop_distance_pct,
        )
        adjusted = False
        if hasattr(position, "update_trailing_stop"):
            try:
                adjusted = position.update_trailing_stop(
                    current_price, dist, ratchet_permitted=ratchet_permitted
                )
            except TypeError:
                adjusted = position.update_trailing_stop(current_price, dist)

        stop_price = float(position.stop_price) if position.stop_price is not None else (hwm - dist)
        return stop_price, adjusted


# ---------------------------------------------------------------------------
# Microstructure entry gate (veto / confirm only)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MicroDecision:
    """
    Outcome of the microstructure gate for one prospective BUY.

    The gate is VETO-ONLY: ``size_multiplier`` is always 1.0 and no code path can raise a size, touch the
    ledger, loosen a stop or delay a flatten. It also never unblocks a macro Regime C halt.
    """
    action: str                      # "ALLOW" | "SUPPRESS"
    code: str                        # OK, OK_DEGRADED, DISABLED, MLOFI_NEG, MLOFI_FLOOR, VPIN_TOXIC, ...
    reason: str
    regime_hint: Optional[str] = None   # "C" defensive liquidity withdrawal | "A" accumulation validated
    degraded: bool = False
    size_multiplier: float = 1.0

    @property
    def suppress(self) -> bool:
        return self.action == "SUPPRESS"


_ORB = "15m_ORB"


def evaluate_microstructure_gate(snap: Any, symbol: str, strategy: str, phase: Any,
                                 cfg: Optional[dict]) -> MicroDecision:
    """
    Pure decision function. ``snap`` exposes ``.mlofi`` (MLOFIReading), ``.vpin`` (VPINReading) and
    ``.lead`` (LeadLagReading). ``cfg`` is the ``microstructure`` config block. First matching rule wins.
    """
    if not cfg or not cfg.get("enabled", False) or snap is None:
        return MicroDecision("ALLOW", "DISABLED", "microstructure gate disabled")

    ml_cfg = cfg.get("mlofi", {}) or {}
    gate_cfg = cfg.get("gate", {}) or {}
    neg_deadband = float(ml_cfg.get("neg_deadband", 0.05))
    hard_floor = float(ml_cfg.get("hard_floor", -0.50))
    confirm_thr = float(ml_cfg.get("confirm_threshold", 0.10))
    neutral_band = float(ml_cfg.get("neutral_band", 0.05))
    accum_min = float(ml_cfg.get("accum_min", 0.10))
    lead_confirm = float(gate_cfg.get("lead_confirm", 0.10))
    policy = str(gate_cfg.get("lead_unavailable_policy", "mlofi_only")).lower()

    sym = symbol.upper()
    is_orb = strategy == _ORB
    phase_name = str(getattr(phase, "value", phase))
    ml, vp, lead = snap.mlofi, snap.vpin, snap.lead
    n5 = ml.norm.get("5s", 0.0)
    n1 = ml.norm.get("1s", 0.0)

    # 1. Hard directional vetoes from the cross-asset leaders.
    if sym == "TQQQ" and lead.available and lead.veto_long:
        return MicroDecision("SUPPRESS", "YIELD_SPIKE", "10Y yield spike: TQQQ long entries suppressed")
    if sym == "TNA" and is_orb and lead.available and lead.invalidate_breakout:
        return MicroDecision("SUPPRESS", "KRE_WEAK", "KRE weakness invalidates the small-cap breakout")

    # 2. Adverse-selection toxicity (needs a ready VPIN with percentile history; otherwise neutral).
    hint: Optional[str] = None
    if vp.ready and vp.toxic:
        accumulation = ml.ready and ml.unanimous_positive and n5 >= accum_min
        if accumulation:
            hint = "A"
        else:
            kind = "neutral" if (not ml.ready or abs(n5) < neutral_band) else "divergent"
            return MicroDecision(
                "SUPPRESS", "VPIN_TOXIC",
                f"VPIN {vp.vpin:.2f} (p{vp.percentile * 100:.0f}) toxic with {kind} order flow",
                regime_hint="C")

    # 3. Own-book order-flow sign (differentiated by strategy).
    if is_orb:
        if ml.ready and n5 < -neg_deadband:
            return MicroDecision("SUPPRESS", "MLOFI_NEG", f"MLOFI(5s) {n5:+.3f} below -{neg_deadband:.2f}", hint)
    else:
        if ml.ready and n5 < hard_floor:
            return MicroDecision("SUPPRESS", "MLOFI_FLOOR", f"MLOFI(5s) {n5:+.3f} below hard floor {hard_floor:+.2f}", hint)

    # 4. ORB authorization during MORNING_DRIVE requires positive flow AND lead confirmation.
    if is_orb and phase_name == "MORNING_DRIVE":
        if not (ml.ready and n1 >= confirm_thr):
            return MicroDecision("SUPPRESS", "ORB_UNCONFIRMED",
                                 f"MORNING_DRIVE ORB needs MLOFI(1s) >= {confirm_thr:.2f} (got {n1:+.3f}, ready={ml.ready})", hint)
        if lead.available:
            if lead.bias < lead_confirm:
                return MicroDecision("SUPPRESS", "ORB_UNCONFIRMED",
                                     f"lead-lag bias {lead.bias:+.2f} below {lead_confirm:.2f}", hint)
        else:
            if policy == "block_orb":
                return MicroDecision("SUPPRESS", "LEAD_UNAVAILABLE", "lead feeds unavailable (policy block_orb)", hint)
            return MicroDecision("ALLOW", "OK_DEGRADED",
                                 "lead feeds unavailable: confirmed on own-book MLOFI only", hint, degraded=True)

    return MicroDecision("ALLOW", "OK", "microstructure checks passed", hint)
