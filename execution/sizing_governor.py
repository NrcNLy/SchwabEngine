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
from dataclasses import dataclass
from typing import Any, Dict, Optional

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


class BayesianSizingGovernor:
    """
    Bayesian Quarter-Kelly position sizer with ATR volatility normalization
    and strict T+1 cash settlement reserve gating.
    """

    def __init__(
        self,
        alpha_0: float = 10.0,
        beta_0: float = 10.0,
        win_loss_ratio: float = 1.25,
        single_ticker_cap_pct: float = 0.33,
        hard_dollar_ceiling: Optional[float] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Args:
            alpha_0: Beta prior pseudo-wins (default 10.0 for 50% prior).
            beta_0: Beta prior pseudo-losses (default 10.0 for 50% prior).
            win_loss_ratio: Payoff ratio b = avg_win_usd / avg_loss_usd (default 1.25).
            single_ticker_cap_pct: Maximum single-ticker fraction of total NLV (default 0.33).
            hard_dollar_ceiling: Absolute dollar cap override ($1,836.00 or $990.00).
            config: Optional global configuration dictionary.
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

        # Resolve config parameters if supplied
        if self.config:
            risk_cfg = self.config.get("risk", {})
            if "single_ticker_cap_pct" in risk_cfg:
                self.single_ticker_cap_pct = float(risk_cfg["single_ticker_cap_pct"])
            if self.hard_dollar_ceiling is None and "hard_dollar_ceiling" in risk_cfg:
                self.hard_dollar_ceiling = float(risk_cfg["hard_dollar_ceiling"])

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
    ) -> SizingVerdict:
        """
        Calculates optimal position size enforcing Bayesian Quarter-Kelly,
        ATR volatility risk budget, single-ticker cap, dollar ceiling,
        and strict settled cash reserve gating.

        Args:
            symbol: Ticker symbol (e.g. "SOXL").
            price: Execution entry price per share.
            atr: 14-period Average True Range in dollars.
            settled_cash: Cleared settled cash available (Bucket 1).
            total_nlv: Account Net Liquidation Value.

        Returns:
            SizingVerdict with shares, notional_usd, kelly_fraction, risk_usd, bottleneck_reason.
        """
        sym = symbol.upper()
        px = float(price)
        c_atr = float(atr)
        cash = max(0.0, float(settled_cash))
        nlv = max(0.0, float(total_nlv))
        f_qk = self.kelly_fraction

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
        # Bound 1: Single-ticker cap w_max = 0.33 of NLV ($1,836 max on $5,563 base).
        w_max = self.single_ticker_cap_pct * nlv
        shares_bound1 = math.floor(w_max / px)

        # Bound 2: Hard dollar ceiling clamp ($1,836.00 or $990.00 depending on active config).
        if self.hard_dollar_ceiling is not None:
            dollar_ceiling = self.hard_dollar_ceiling
        elif nlv <= 1500.0:
            dollar_ceiling = 990.00
        else:
            dollar_ceiling = 1836.00

        shares_bound2 = math.floor(dollar_ceiling / px)

        # Candidate shares before cash settlement constraint
        candidate_shares = max(0, min(target_shares_atr, shares_bound1, shares_bound2))
        candidate_notional = candidate_shares * px

        # Bound 3: Strict Settled Cash Ceiling:
        # Shares_final = min(Shares_raw, floor(Settled_Cash / Price))
        shares_cash = max(0, math.floor(cash / px))

        if cash < candidate_notional or shares_cash < candidate_shares:
            shares_final = shares_cash
            bottleneck_reason = "SETTLEMENT_RESERVE_CONSTRAINT"
            logger.warning(
                "[%s] Sizing restricted by settlement reserve: candidate %d shares ($%.2f) "
                "clamped to %d shares ($%.2f) on settled cash $%.2f (Zero GFV Guarantee).",
                sym, candidate_shares, candidate_notional, shares_final, shares_final * px, cash
            )
        else:
            shares_final = candidate_shares
            if candidate_shares == shares_bound1 and shares_bound1 < target_shares_atr:
                bottleneck_reason = "SINGLE_TICKER_CAP"
            elif candidate_shares == shares_bound2 and shares_bound2 < target_shares_atr:
                bottleneck_reason = "HARD_DOLLAR_CEILING"
            elif target_shares_atr <= min(shares_bound1, shares_bound2):
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
