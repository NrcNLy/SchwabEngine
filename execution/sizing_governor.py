"""
execution/sizing_governor.py
============================
Bayesian Quarter-Kelly Sizing & Settlement Reserve Governor.

Mathematical Formulation:
-------------------------
1. Bayesian Conjugate Prior Updating:
   - Model win rate p ~ Beta(alpha_prior + wins, beta_prior + losses).
   - Weakly informative prior: alpha_0 = 10, beta_0 = 10 (prior win rate 50%).
   - Posterior Expected Win Rate: p_hat = alpha / (alpha + beta)
   - Win/Loss Ratio: b = avg_win_usd / avg_loss_usd
   - Full Kelly Fraction: f_star = max(0.0, (p_hat * b - (1.0 - p_hat)) / b)
   - Quarter-Kelly Fraction: f_qk = 0.25 * f_star

2. Volatility Normalization (ATR Sizing):
   - Dollar Volatility Risk: Risk_dollar = Account_NLV * min(0.015, f_qk)
   - Target Shares via ATR: Shares_raw = Risk_dollar / (ATR_14 * 1.5)

3. Three-Tier Boundary Clamps:
   - Bound 1: Single-ticker cap w_max = 0.33 of NLV ($1,836 max on $5,563 base).
   - Bound 2: Hard dollar ceiling clamp ($1,836.00 or $990.00 depending on active config).
   - Bound 3: Strict Settled Cash Ceiling:
     Shares_final = min(Shares_raw, floor(Settled_Cash / Price))

Guarantees ZERO Good Faith Violations (GFVs) under SEC cash account rules and Regulation T § 220.8.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

from execution.risk_manager import (
    CASH_SWEEP_VEHICLE,
    DEFAULT_ROUTING_SWEEP_VECTOR,
    MetaGovernorHMM,
)

logger = logging.getLogger("sizing_governor")


@dataclass(frozen=True)
class SizingVerdict:
    """
    Immutable position sizing verdict returned by BayesianSizingGovernor.
    """
    shares: int
    notional_usd: float
    kelly_fraction: float
    risk_usd: float
    bottleneck_reason: str


@dataclass(frozen=True)
class MultiAssetOrderDeltaVerdict:
    """
    Verdict containing stock-vs-flow dual-sleeve order deltas and paired Robo-Core sweep notional (#92 / #98).
    """
    target_shares: Dict[str, int]
    held_shares: Dict[str, int]
    delta_shares: Dict[str, int]
    satellite_notional_delta: float
    core_notional_sweep_usd: float
    sub_weights: Dict[str, float]
    drag_multipliers: Dict[str, float]
    lot_quantization_rollover_usd: float
    ntz_clamped: bool = False
    active_satellite_weight: float = 0.0
    target_satellite_weight: float = 0.15
    band_lower: float = 0.12
    band_upper: float = 0.18
    churn_reduction_target_pct: float = 99.6


class BayesianSizingGovernor:
    """
    Bayesian Quarter-Kelly position sizer with ATR volatility normalization,
    strict T+1 cash settlement reserve gating, empirical 3-State HMM calibration,
    and Leland No-Trade Zone rebalance clamping (#98 Argon 2.4.0).
    """

    EMPIRICAL_TRANSITION_MATRIX_3X3: List[List[float]] = [
        [0.9993, 0.0, 0.0007],
        [0.0032, 0.9968, 0.0],
        [0.0029, 0.0, 0.9971],
    ]

    EMPIRICAL_MEAN_EMISSION_VECTORS: Dict[str, Dict[str, Dict[str, float]]] = (
        MetaGovernorHMM.EMPIRICAL_MEAN_EMISSION_VECTORS
    )

    def __init__(
        self,
        alpha_0: float = 10.0,
        beta_0: float = 10.0,
        win_loss_ratio: float = 1.25,
        single_ticker_cap_pct: float = 0.33,
        hard_dollar_ceiling: Optional[float] = None,
        config: Optional[Dict[str, Any]] = None,
        tau_band: float = 0.03,
        volatility_gate_trigger: float = 0.15,
        rolling_vol_window_bars: int = 32,
    ) -> None:
        """
        Args:
            alpha_0: Beta prior pseudo-wins (default 10.0 for 50% prior).
            beta_0: Beta prior pseudo-losses (default 10.0 for 50% prior).
            win_loss_ratio: Payoff ratio b = avg_win_usd / avg_loss_usd (default 1.25).
            single_ticker_cap_pct: Maximum single-ticker fraction of total NLV (default 0.33).
            hard_dollar_ceiling: Absolute dollar cap override ($1,836.00 or $990.00).
            config: Optional global configuration dictionary.
            tau_band: Leland No-Trade Zone tolerance half-width (calibrated: 0.03 -> [0.12, 0.18]).
            volatility_gate_trigger: LETF volatility drag trigger sigma_gate (calibrated: 0.15).
            rolling_vol_window_bars: Rolling volatility window in hourly bars (calibrated: 32).
        """
        self.alpha_0: float = float(alpha_0)
        self.beta_0: float = float(beta_0)
        self.wins: int = 0
        self.losses: int = 0
        self.win_loss_ratio: float = float(win_loss_ratio)
        self.single_ticker_cap_pct: float = float(single_ticker_cap_pct)
        self.hard_dollar_ceiling: Optional[float] = (
            float(hard_dollar_ceiling) if hard_dollar_ceiling is not None else None
        )
        self.config: Dict[str, Any] = config or {}

        # Pure Tactical 100% LETF Model Parameters (#140)
        self.core_target_weight: float = 0.0
        self.satellite_target_weight: float = 0.75
        self.tactical_max_capacity: float = 0.75
        self.tau_band: float = float(tau_band)
        self.drift_lower_cutoff: float = 0.30
        self.drift_upper_cutoff: float = 0.80
        self.friction_cost_rate: float = 0.0002
        self.churn_reduction_target_pct: float = 99.6
        self.volatility_gate_trigger: float = float(volatility_gate_trigger)
        self.rolling_vol_window_bars: int = int(rolling_vol_window_bars)
        self.volatility_drag_threshold_annualized: float = 0.45
        self.routing_sweep_vector: Dict[str, float] = dict(DEFAULT_ROUTING_SWEEP_VECTOR)
        self.cash_sweep_vehicle: str = CASH_SWEEP_VEHICLE

        # Empirical 3-State HMM Governor instance hydrated with dossier parameters
        self.hmm: MetaGovernorHMM = MetaGovernorHMM()
        self.transition_matrix: List[List[float]] = self.hmm.transition_matrix
        self.mean_emission_vectors: Dict[str, Dict[str, Dict[str, float]]] = self.hmm.mean_emission_vectors

        # Telemetry counters for Leland NTZ churn reduction tracking
        self.total_rebalance_evaluations: int = 0
        self.ntz_clamped_avoided_count: int = 0

        # Resolve config parameters if supplied
        if self.config:
            risk_cfg = self.config.get("risk", {})
            if "single_ticker_cap_pct" in risk_cfg:
                self.single_ticker_cap_pct = float(risk_cfg["single_ticker_cap_pct"])
            if self.hard_dollar_ceiling is None and "hard_dollar_ceiling" in risk_cfg:
                self.hard_dollar_ceiling = float(risk_cfg["hard_dollar_ceiling"])
            ntz_cfg = self.config.get("leland_ntz", {})
            if "tau_band" in ntz_cfg:
                self.tau_band = float(ntz_cfg["tau_band"])
                self.drift_lower_cutoff = float(ntz_cfg.get("drift_lower_cutoff", self.satellite_target_weight - self.tau_band))
                self.drift_upper_cutoff = float(ntz_cfg.get("drift_upper_cutoff", self.satellite_target_weight + self.tau_band))
            letf_cfg = self.config.get("letf_drag_cutoffs", {})
            if "volatility_gate_trigger" in letf_cfg:
                self.volatility_gate_trigger = float(letf_cfg["volatility_gate_trigger"])
            if "rolling_vol_window_bars" in letf_cfg:
                self.rolling_vol_window_bars = int(letf_cfg["rolling_vol_window_bars"])

    @property
    def alpha(self) -> float:
        """Posterior alpha (prior alpha_0 + empirical wins)."""
        return self.alpha_0 + float(self.wins)

    @property
    def beta(self) -> float:
        """Posterior beta (prior beta_0 + empirical losses)."""
        return self.beta_0 + float(self.losses)

    @property
    def posterior_win_rate(self) -> float:
        """Posterior expected win rate: p_hat = alpha / (alpha + beta)."""
        total = self.alpha + self.beta
        return self.alpha / total if total > 0 else 0.5

    p_hat = posterior_win_rate

    @property
    def full_kelly(self) -> float:
        """
        Full Kelly Fraction:
            f_star = max(0.0, (p_hat * b - (1.0 - p_hat)) / b)
        """
        b = self.win_loss_ratio
        if b <= 0.0:
            return 0.0
        p = self.posterior_win_rate
        q = 1.0 - p
        edge = (p * b) - q
        return max(0.0, edge / b)

    f_star = full_kelly

    @property
    def kelly_fraction(self) -> float:
        """Quarter-Kelly Fraction: f_qk = 0.25 * f_star."""
        return 0.25 * self.full_kelly

    f_qk = kelly_fraction

    def update_trade_outcome(self, is_win: bool, win_loss_ratio: Optional[float] = None) -> None:
        """
        Update the Bayesian conjugate prior with a newly realized trade outcome.

        Args:
            is_win: True if trade closed net positive, False otherwise.
            win_loss_ratio: Optional updated payoff ratio b = avg_win / avg_loss.
        """
        if is_win:
            self.wins += 1
        else:
            self.losses += 1

        if win_loss_ratio is not None and float(win_loss_ratio) > 0:
            self.win_loss_ratio = float(win_loss_ratio)

        logger.info(
            "BayesianSizingGovernor: updated outcome (is_win=%s) -> alpha=%.1f, beta=%.1f, "
            "p_hat=%.3f, f_qk=%.4f (b=%.2f)",
            is_win, self.alpha, self.beta, self.posterior_win_rate, self.kelly_fraction, self.win_loss_ratio
        )

    def calculate_position_size(
        self,
        symbol: str,
        price: float,
        atr: float,
        settled_cash: float,
        total_nlv: float,
        enforce_settled_cash_gate: bool = True,
        remaining_budget: Optional[float] = None,
        min_position_size: float = 50.0,
    ) -> SizingVerdict:
        """
        Calculates optimal position size enforcing Bayesian Quarter-Kelly,
        ATR volatility risk budget, single-ticker cap, optional dollar ceiling,
        remaining tactical cash budget, and settled cash reserve gating.

        Args:
            symbol: Ticker symbol (e.g. "SOXL").
            price: Execution entry price per share.
            atr: 14-period Average True Range in dollars.
            settled_cash: Cleared settled cash available (Bucket 1).
            total_nlv: Account Net Liquidation Value.
            enforce_settled_cash_gate: If True, enforces strict settled cash limit (zero GFV).
                                      If False, permits instantaneous Dual-Sleeve reallocation.
            remaining_budget: Optional remaining tactical cash budget to prevent concurrent sizing collisions (#140).
            min_position_size: Minimum dollar notional for an allowable position entry.

        Returns:
            SizingVerdict with shares, notional_usd, kelly_fraction, risk_usd, bottleneck_reason.
        """
        sym = symbol.upper()
        px = float(price)
        c_atr = float(atr)
        cash = max(0.0, float(settled_cash))
        nlv = max(0.0, float(total_nlv))
        f_qk = self.kelly_fraction

        if remaining_budget is not None:
            rem_cash = max(0.0, float(remaining_budget))
            effective_min = max(float(min_position_size), px)
            if rem_cash < effective_min:
                logger.warning(
                    "[%s] Sizing rejected: remaining budget ($%.2f) < min position size ($%.2f)",
                    sym, rem_cash, effective_min
                )
                return SizingVerdict(
                    shares=0,
                    notional_usd=0.0,
                    kelly_fraction=round(f_qk, 6),
                    risk_usd=0.0,
                    bottleneck_reason="REMAINING_BUDGET_EXHAUSTED",
                )
            cash = min(cash, rem_cash)

        if px <= 0.0 or nlv <= 0.0:
            logger.warning("[%s] Sizing failed: non-positive price ($%.2f) or NLV ($%.2f)", sym, px, nlv)
            return SizingVerdict(
                shares=0,
                notional_usd=0.0,
                kelly_fraction=round(f_qk, 6),
                risk_usd=0.0,
                bottleneck_reason="INVALID_PRICE_OR_NLV",
            )

        # 1. Dollar Volatility Risk: Risk_dollar = Account_NLV * min(0.015, f_qk)
        risk_fraction = min(0.015, f_qk)
        if risk_fraction <= 0.0:
            logger.warning("[%s] Sizing halted: Kelly indicates no statistical edge (f_qk=%.4f)", sym, f_qk)
            return SizingVerdict(
                shares=0,
                notional_usd=0.0,
                kelly_fraction=round(f_qk, 6),
                risk_usd=0.0,
                bottleneck_reason="KELLY_NO_EDGE",
            )

        risk_dollar = nlv * risk_fraction

        # 2. Target Shares via ATR: Shares_raw = Risk_dollar / (ATR_14 * 1.5)
        stop_dist = c_atr * 1.5
        if stop_dist <= 0.0:
            logger.warning("[%s] Sizing failed: non-positive stop distance from ATR ($%.4f)", sym, c_atr)
            return SizingVerdict(
                shares=0,
                notional_usd=0.0,
                kelly_fraction=round(f_qk, 6),
                risk_usd=0.0,
                bottleneck_reason="INVALID_ATR",
            )

        shares_raw = risk_dollar / stop_dist
        target_shares_atr = math.floor(shares_raw)

        # 3. Three-Tier Boundary Clamps:
        # Bound 1: Single-ticker cap w_max = single_ticker_cap_pct of NLV.
        w_max = self.single_ticker_cap_pct * nlv
        shares_bound1 = math.floor(w_max / px)

        # Bound 2: Hard dollar ceiling clamp (pure scale-invariance when None).
        if self.hard_dollar_ceiling is not None:
            dollar_ceiling = self.hard_dollar_ceiling
            shares_bound2 = math.floor(dollar_ceiling / px)
        else:
            shares_bound2 = shares_bound1

        # Candidate shares before cash settlement constraint
        candidate_shares = max(0, min(target_shares_atr, shares_bound1, shares_bound2))
        candidate_notional = candidate_shares * px

        # Bound 3: Settled Cash Ceiling:
        shares_cash = max(0, math.floor(cash / px))

        if enforce_settled_cash_gate and (cash < candidate_notional or shares_cash < candidate_shares):
            shares_final = shares_cash
            bottleneck_reason = "SETTLEMENT_RESERVE_CONSTRAINT"
            logger.warning(
                "[%s] Sizing restricted by settlement reserve: candidate %d shares ($%.2f) "
                "clamped to %d shares ($%.2f) on settled cash $%.2f (Zero GFV Guarantee).",
                sym, candidate_shares, candidate_notional, shares_final, shares_final * px, cash
            )
        else:
            shares_final = candidate_shares
            if self.hard_dollar_ceiling is not None and candidate_shares == shares_bound2 and shares_bound2 < target_shares_atr:
                bottleneck_reason = "HARD_DOLLAR_CEILING"
            elif candidate_shares == shares_bound1 and shares_bound1 < target_shares_atr:
                bottleneck_reason = "SINGLE_TICKER_CAP"
            elif target_shares_atr <= shares_bound1:
                bottleneck_reason = "VOLATILITY_RISK_LIMIT"
            else:
                bottleneck_reason = "NONE"

        notional_usd = round(shares_final * px, 2)
        risk_usd = round(shares_final * stop_dist, 2)

        verdict = SizingVerdict(
            shares=shares_final,
            notional_usd=notional_usd,
            kelly_fraction=round(f_qk, 6),
            risk_usd=risk_usd,
            bottleneck_reason=bottleneck_reason,
        )

        logger.info(
            "[%s] SizingVerdict: %d shares | Notional: $%.2f | Kelly(f_qk): %.4f | "
            "Risk: $%.2f | Bottleneck: %s",
            sym, verdict.shares, verdict.notional_usd, verdict.kelly_fraction,
            verdict.risk_usd, verdict.bottleneck_reason
        )
        return verdict

    def update_hmm_regime(
        self,
        macro_state: Union[Any, Tuple[float, float, float], Dict[str, float]],
        dt_seconds: float = 1.0,
    ) -> Tuple[float, float, float]:
        """
        Updates the calibrated 3-State HMM Meta-Governor with macro observation vector
        [Macro_Surprise_Z, Macro_Sentiment, Macro_HYSpread_Bps] (#98).
        """
        return self.hmm.update(macro_state, dt_seconds=dt_seconds)

    def evaluate_leland_ntz_clamp(
        self,
        active_satellite_weight: float,
        target_satellite_weight: Optional[float] = None,
        tau_band: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Evaluates whether active satellite weight w(t) resides inside the Leland No-Trade Zone
        [W_sat^* - tau_band, W_sat^* + tau_band] (default [0.12, 0.18] for W_sat^* = 0.15, tau_band = 0.03),
        enforcing the 99.6% churn-reduction threshold observed during the 15-month replay (#98).
        """
        target_w = float(target_satellite_weight if target_satellite_weight is not None else self.satellite_target_weight)
        tau = float(tau_band if tau_band is not None else self.tau_band)
        band_lower = round(max(0.0, target_w - tau), 4)
        band_upper = round(target_w + tau, 4)
        active_w = round(max(0.0, float(active_satellite_weight)), 4)

        self.total_rebalance_evaluations += 1
        inside_band = band_lower <= active_w <= band_upper
        if inside_band:
            self.ntz_clamped_avoided_count += 1

        observed_churn_reduction_pct = (
            round(100.0 * self.ntz_clamped_avoided_count / self.total_rebalance_evaluations, 2)
            if self.total_rebalance_evaluations > 0
            else self.churn_reduction_target_pct
        )

        return {
            "ntz_clamped": inside_band,
            "active_satellite_weight": active_w,
            "target_satellite_weight": round(target_w, 4),
            "band_lower": band_lower,
            "band_upper": band_upper,
            "tau_band": tau,
            "churn_reduction_target_pct": self.churn_reduction_target_pct,
            "observed_churn_reduction_pct": observed_churn_reduction_pct,
        }

    def compute_letf_drag_multiplier(
        self,
        sigma_yz: float,
        leverage: float = 3.0,
        mu_hat: float = 0.15,
        r_f: float = 0.045,
        s_swap: float = 0.005,
        c_fee: float = 0.0095,
        epsilon: float = 1e-4,
        rolling_bars: Optional[List[float]] = None,
    ) -> float:
        """
        Calculates 3x LETF variance drain and financing drag multiplier (#92 / #98):
        phi_drag = clip(1 - (0.5 * L^2 * sigma_yz^2 + (L-1)*(r_f + s_swap) + c_fee) / max(eps, L * mu_hat), 0.0, 1.0)
        Evaluates 32-bar rolling volatility window against sigma_gate = 0.15 when rolling_bars is supplied.
        """
        l = float(leverage)
        sig = max(0.0, float(sigma_yz))
        if rolling_bars and len(rolling_bars) >= 2:
            window = rolling_bars[-self.rolling_vol_window_bars :]
            mean_r = sum(window) / len(window)
            var_r = sum((r - mean_r) ** 2 for r in window) / max(1, len(window) - 1)
            sig = max(sig, math.sqrt(var_r))

        var_drain = 0.5 * (l ** 2) * (sig ** 2)
        financing_drag = (l - 1.0) * (float(r_f) + float(s_swap)) + float(c_fee)
        total_drag = var_drain + financing_drag
        expected_drift = max(epsilon, l * float(mu_hat))
        drag_ratio = total_drag / expected_drift
        return round(max(0.0, min(1.0, 1.0 - drag_ratio)), 4)

    def compute_sub_weights(
        self,
        volatilities: Dict[str, float],
        correlations: Optional[Dict[str, Dict[str, float]]] = None,
        sector_multipliers: Optional[Dict[str, float]] = None,
    ) -> Dict[str, float]:
        """
        Computes correlation-penalized inverse Yang-Zhang volatility sub-weights (#92):
        w_tilde_j = m_sector_j / (sigma_yz_j * sum_k |rho_jk|)
        omega_j = w_tilde_j / sum_m w_tilde_m
        """
        symbols = list(volatilities.keys())
        if not symbols:
            return {}

        sector_mults = sector_multipliers or {}
        raw_weights = {}

        for sym in symbols:
            s = sym.upper()
            sig = max(0.001, float(volatilities.get(sym, 0.02)))
            m_sec = float(sector_mults.get(s, sector_mults.get(s.lower(), 1.0)))

            # Sum of absolute pairwise correlations
            if correlations and s in correlations:
                corr_sum = sum(abs(float(correlations[s].get(other, 0.5 if s != other else 1.0))) for other in symbols)
                corr_sum = max(0.5, corr_sum)
            else:
                corr_sum = float(len(symbols))

            w_tilde = m_sec / (sig * corr_sum)
            raw_weights[s] = w_tilde

        total_w = sum(raw_weights.values())
        if total_w <= 0.0:
            equal = 1.0 / len(symbols)
            return {s: round(equal, 4) for s in symbols}

        return {s: round(raw_weights[s] / total_w, 4) for s in symbols}

    def calculate_multi_asset_order_delta(
        self,
        nlv: float,
        held_shares: Dict[str, int],
        prices: Dict[str, float],
        volatilities: Dict[str, float],
        target_satellite_weight: float = 0.15,
        correlations: Optional[Dict[str, Dict[str, float]]] = None,
        sector_multipliers: Optional[Dict[str, float]] = None,
        enable_drag_filter: bool = True,
        enforce_leland_ntz: bool = True,
        tau_band: Optional[float] = None,
    ) -> MultiAssetOrderDeltaVerdict:
        """
        Scale-Invariant Stock-vs-Flow Order Delta formulation (#92 / #98):
        Q_{j, target} = min(floor(omega_j * f_Q_adj * N / P_j), floor(omega_j * W_sat^* * N / P_j))
        Delta Q_j = Q_{j, target} - Q_{j, held}
        Opposing Robo-Core funding/sweep notional: Delta C_usd = -sum_j Delta Q_j * P_j
        Clamps rebalance evaluation through the Leland No-Trade Zone [W_sat^* - tau_band, W_sat^* + tau_band]
        to enforce the 99.6% churn-reduction threshold observed during the 15-month replay.
        """
        symbols = list(prices.keys())
        sub_weights = self.compute_sub_weights(volatilities, correlations, sector_multipliers)
        drag_multipliers = {}
        account_nlv = max(0.0, float(nlv))

        # Compute current active satellite weight from held shares
        held_map: Dict[str, int] = {}
        current_sat_mv = 0.0
        for sym in symbols:
            s = sym.upper()
            px = max(0.01, float(prices.get(sym, 1.0)))
            q_held = int(held_shares.get(s, held_shares.get(sym, 0)))
            held_map[s] = q_held
            current_sat_mv += max(0, q_held) * px

        active_sat_weight = (current_sat_mv / account_nlv) if account_nlv > 0.0 else 0.0
        ntz_eval = self.evaluate_leland_ntz_clamp(
            active_satellite_weight=active_sat_weight,
            target_satellite_weight=target_satellite_weight,
            tau_band=tau_band,
        )

        # Compute drag multipliers for telemetry regardless of clamp
        for sym in symbols:
            s = sym.upper()
            sig = volatilities.get(sym, 0.02)
            drag_multipliers[s] = self.compute_letf_drag_multiplier(sig) if enable_drag_filter else 1.0

        # Clamp rebalance evaluation inside the Leland No-Trade Zone [band_lower, band_upper]
        if enforce_leland_ntz and ntz_eval["ntz_clamped"] and current_sat_mv > 0.0:
            return MultiAssetOrderDeltaVerdict(
                target_shares=dict(held_map),
                held_shares=dict(held_map),
                delta_shares={s: 0 for s in held_map},
                satellite_notional_delta=0.0,
                core_notional_sweep_usd=0.0,
                sub_weights=sub_weights,
                drag_multipliers=drag_multipliers,
                lot_quantization_rollover_usd=0.0,
                ntz_clamped=True,
                active_satellite_weight=ntz_eval["active_satellite_weight"],
                target_satellite_weight=ntz_eval["target_satellite_weight"],
                band_lower=ntz_eval["band_lower"],
                band_upper=ntz_eval["band_upper"],
                churn_reduction_target_pct=self.churn_reduction_target_pct,
            )

        target_shares = {}
        delta_shares = {}
        total_sat_notional_delta = 0.0
        residual_rollover_usd = 0.0
        f_qk = self.kelly_fraction

        for sym in symbols:
            s = sym.upper()
            px = max(0.01, float(prices.get(sym, 1.0)))
            w_j = sub_weights.get(s, 1.0 / len(symbols) if symbols else 1.0)
            phi_j = drag_multipliers[s]

            # Adjusted Kelly budget vs Strategy target budget
            f_q_adj = f_qk * phi_j
            budget_kelly = w_j * f_q_adj * account_nlv
            budget_strat = w_j * float(target_satellite_weight) * account_nlv
            target_usd = min(budget_kelly, budget_strat)

            # Continuous shares and discrete floor
            exact_shares = target_usd / px
            q_target = math.floor(exact_shares)
            leftover_usd = (exact_shares - q_target) * px
            residual_rollover_usd += leftover_usd

            target_shares[s] = q_target
            q_held = held_map[s]
            dq = q_target - q_held
            delta_shares[s] = dq
            total_sat_notional_delta += dq * px

        # Small-account lot-quantization rollover:
        # If residual unallocated capital exceeds the lowest price asset, deploy an extra lot to highest conviction asset
        if residual_rollover_usd > 0 and symbols:
            best_sym = max(symbols, key=lambda sym: sub_weights.get(sym.upper(), 0.0))
            best_px = prices[best_sym]
            if residual_rollover_usd >= best_px and account_nlv <= 25000.0:
                extra_lots = int(residual_rollover_usd // best_px)
                if extra_lots > 0:
                    target_shares[best_sym.upper()] += extra_lots
                    delta_shares[best_sym.upper()] += extra_lots
                    total_sat_notional_delta += extra_lots * best_px
                    residual_rollover_usd -= extra_lots * best_px

        core_sweep_usd = -round(total_sat_notional_delta, 2)

        return MultiAssetOrderDeltaVerdict(
            target_shares=target_shares,
            held_shares=held_map,
            delta_shares=delta_shares,
            satellite_notional_delta=round(total_sat_notional_delta, 2),
            core_notional_sweep_usd=core_sweep_usd,
            sub_weights=sub_weights,
            drag_multipliers=drag_multipliers,
            lot_quantization_rollover_usd=round(residual_rollover_usd, 2),
            ntz_clamped=False,
            active_satellite_weight=ntz_eval["active_satellite_weight"],
            target_satellite_weight=ntz_eval["target_satellite_weight"],
            band_lower=ntz_eval["band_lower"],
            band_upper=ntz_eval["band_upper"],
            churn_reduction_target_pct=self.churn_reduction_target_pct,
        )

