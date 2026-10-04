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
