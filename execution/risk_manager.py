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
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from core.t1_ledger import GFVRejectionError, T1CashLedger

logger = logging.getLogger("risk_manager")

# Prior used for Bayesian shrinkage and the Kelly robustness constant.
PRIOR_MEAN = 0.5
ROBUSTNESS_KAPPA = 15

LETF_3X_SYMBOLS: Set[str] = {"SOXL", "TQQQ", "TNA", "FNGU", "UPRO", "TECL"}
CORE_1X_SYMBOLS: Set[str] = {"SPY", "QQQ", "IWM", "HYG", "IEF"}
CASH_SWEEP_VEHICLE: str = "SWVXX"

DEFAULT_ROUTING_SWEEP_VECTOR: Dict[str, float] = {
    "SPY": 0.35,
    "QQQ": 0.25,
    "IWM": 0.10,
    "HYG": 0.15,
    "IEF": 0.15,
    "cash": 0.0,
}

DEFAULT_CORE_PRICES: Dict[str, float] = {
    "SPY": 585.0,
    "QQQ": 490.0,
    "IWM": 220.0,
    "HYG": 79.0,
    "IEF": 95.0,
}

DEFAULT_REGIME_SATELLITE_TARGETS: Dict[str, Dict[str, Any]] = {
    "Goldilocks": {
        "target": 0.25,
        "ntz_band": [0.21, 0.29],
    },
    "Uncertainty": {
        "target": 0.10,
        "ntz_band": [0.08, 0.12],
    },
    "Shock": {
        "target": 0.00,
        "ntz_band": [0.00, 0.00],
    },
}


class MetaGovernorHMM:
    """
    3-State Online Continuous HMM Meta-Governor (#91 / #98 Argon 2.4.0 Calibration).
    Maintains a filtered probability vector pi(t) = [pi_Goldilocks, pi_Uncertainty, pi_Shock]^T in Delta^2.
    Hydrated with 15-month empirical transition matrix T and Gaussian emission vectors (mu_k, sigma_k)
    for M(t) = [Macro_Surprise_Z, Macro_Sentiment, Macro_HYSpread_Bps]^T.
    """
    REGIMES = ("Goldilocks", "Uncertainty", "Macro Shock")
    REGIME_ORDER = ("goldilocks", "uncertainty", "shock")

    EMPIRICAL_TRANSITION_MATRIX_3X3: List[List[float]] = [
        [0.9993, 0.0, 0.0007],
        [0.0032, 0.9968, 0.0],
        [0.0029, 0.0, 0.9971],
    ]

    EMPIRICAL_PERSISTENCE_DIAGONAL: Dict[str, float] = {
        "T_goldilocks_persistence": 0.9993,
        "T_uncertainty_persistence": 0.9968,
        "T_shock_persistence": 0.9971,
    }

    EMPIRICAL_MEAN_EMISSION_VECTORS: Dict[str, Dict[str, Dict[str, float]]] = {
        "goldilocks": {
            "mu": {
                "Macro_Surprise_Z": 0.9946,
                "Macro_Sentiment": 0.4803,
                "Macro_HYSpread_Bps": 317.6,
            },
            "sigma": {
                "Macro_Surprise_Z": 0.2498,
                "Macro_Sentiment": 0.1064,
                "Macro_HYSpread_Bps": 18.8,
            },
        },
        "uncertainty": {
            "mu": {
                "Macro_Surprise_Z": -0.2989,
                "Macro_Sentiment": -0.06,
                "Macro_HYSpread_Bps": 406.8,
            },
            "sigma": {
                "Macro_Surprise_Z": 0.092,
                "Macro_Sentiment": 0.0417,
                "Macro_HYSpread_Bps": 6.7,
            },
        },
        "shock": {
            "mu": {
                "Macro_Surprise_Z": -1.243,
                "Macro_Sentiment": -0.472,
                "Macro_HYSpread_Bps": 481.9,
            },
            "sigma": {
                "Macro_Surprise_Z": 0.2657,
                "Macro_Sentiment": 0.1452,
                "Macro_HYSpread_Bps": 35.3,
            },
        },
    }

    def __init__(
        self,
        initial_pi: Optional[Tuple[float, float, float]] = None,
        persistence: float = 0.9993,
        transition_matrix: Optional[List[List[float]]] = None,
    ) -> None:
        self.persistence = persistence
        self.persistence_diagonal: Tuple[float, float, float] = (0.9993, 0.9968, 0.9971)
        self.recommended_hmm_persistence_diagonal: Dict[str, float] = dict(self.EMPIRICAL_PERSISTENCE_DIAGONAL)

        # Hydrate 3x3 transition matrix with empirical diagonal persistence values (#98)
        if transition_matrix is not None:
            self.transition_matrix = [list(row) for row in transition_matrix]
        else:
            self.transition_matrix = [list(row) for row in self.EMPIRICAL_TRANSITION_MATRIX_3X3]
        self.transition_matrix_3x3 = self.transition_matrix

        self.pi: List[float] = list(initial_pi) if initial_pi else [0.70, 0.25, 0.05]
        self._normalize_pi()

        # Calibrated Gaussian emission parameters for [Macro_Surprise_Z, Macro_Sentiment, Macro_HYSpread_Bps]
        self.mean_emission_vectors = {
            k: {"mu": dict(v["mu"]), "sigma": dict(v["sigma"])}
            for k, v in self.EMPIRICAL_MEAN_EMISSION_VECTORS.items()
        }
        self.mu = [
            [0.9946, 0.4803, 317.6],   # Goldilocks
            [-0.2989, -0.06, 406.8],   # Uncertainty
            [-1.243, -0.472, 481.9],   # Macro Shock
        ]
        self.sigma = [
            [0.2498, 0.1064, 18.8],    # Goldilocks
            [0.092, 0.0417, 6.7],      # Uncertainty
            [0.2657, 0.1452, 35.3],    # Macro Shock
        ]

    def _normalize_pi(self) -> None:
        total = sum(self.pi)
        if total > 0:
            self.pi = [p / total for p in self.pi]
        else:
            self.pi = [1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0]

    def update(
        self,
        macro_state: Union[Any, Tuple[float, float, float], Dict[str, float]],
        dt_seconds: float = 1.0,
    ) -> Tuple[float, float, float]:
        """
        Executes continuous Bayesian filtering step:
        pi(t) = ((T^dt)^T * pi(t-dt) * L(M(t))) / ||...||_1
        """
        if isinstance(macro_state, dict):
            flash_ret = float(
                macro_state.get(
                    "soxl_return_30s",
                    macro_state.get("flash_crash_return", macro_state.get("price_change_pct", 0.0)),
                )
            )
            flash_drop = float(macro_state.get("flash_crash_drop_pct", 0.0))
            sigma_yz = float(macro_state.get("sigma_yz", 0.0))
            is_flash_crash = (flash_ret <= -0.15) or (flash_drop >= 0.15) or (sigma_yz >= 0.15)
            z_s = float(
                macro_state.get(
                    "Macro_Surprise_Z",
                    macro_state.get("economic_surprise_z_score", -2.5 if is_flash_crash else 0.0),
                )
            )
            s_nlp = float(
                macro_state.get(
                    "Macro_Sentiment",
                    macro_state.get("sentiment_decayed", -0.90 if is_flash_crash else 0.0),
                )
            )
            cs = float(
                macro_state.get(
                    "Macro_HYSpread_Bps",
                    macro_state.get("hybrid_credit_spread_bps", 640.0 if is_flash_crash else 317.6),
                )
            )
        elif hasattr(macro_state, "economic_surprise_z_score"):
            z_s = float(macro_state.economic_surprise_z_score)
            s_nlp = float(getattr(macro_state, "sentiment_decayed", getattr(macro_state, "sentiment_polarity_raw", 0.0)))
            cs = float(getattr(macro_state, "hybrid_credit_spread_bps", getattr(macro_state, "fred_oas_bps", 317.6)))
        elif isinstance(macro_state, (tuple, list)):
            z_s, s_nlp, cs = float(macro_state[0]), float(macro_state[1]), float(macro_state[2])
        else:
            return self.get_regime_posterior()

        # 1. State prediction via transition matrix: pi_pred = T^T * pi (with 1e-4 ergodic floor)
        pi_pred = [0.0, 0.0, 0.0]
        for j in range(3):
            for i in range(3):
                pi_pred[j] += self.pi[i] * self.transition_matrix[i][j]
            pi_pred[j] = max(pi_pred[j], 1e-4)

        # 2. Dimension-normalized Gaussian log-likelihood for each state k in {0, 1, 2}
        m = [z_s, s_nlp, cs]
        num_dims = 3.0
        log_liks = []
        for k in range(3):
            ll = 0.0
            for dim in range(3):
                diff = (m[dim] - self.mu[k][dim]) / self.sigma[k][dim]
                ll += -0.5 * (diff * diff) / num_dims
            log_liks.append(ll)

        max_ll = max(log_liks)
        liks = [math.exp(ll - max_ll) for ll in log_liks]

        # 3. Bayes measurement update: pi_new = (pi_pred * L) / sum(pi_pred * L)
        unnorm = [pi_pred[k] * liks[k] for k in range(3)]
        total = sum(unnorm)
        if total > 0:
            self.pi = [u / total for u in unnorm]
        else:
            self.pi = pi_pred
        self._normalize_pi()
        return self.get_regime_posterior()

    def get_regime_posterior(self) -> Tuple[float, float, float]:
        return (round(self.pi[0], 4), round(self.pi[1], 4), round(self.pi[2], 4))

    @property
    def pi_goldilocks(self) -> float:
        return self.pi[0]

    @property
    def pi_uncertainty(self) -> float:
        return self.pi[1]

    @property
    def pi_shock(self) -> float:
        return self.pi[2]

    def get_dominant_regime(self) -> str:
        idx = self.pi.index(max(self.pi))
        return self.REGIMES[idx]

    def calculate_target_satellite_weight(
        self,
        w_base: Optional[float] = None,
        w_c: Optional[float] = None,
        drawdown_damper: float = 1.0,
        w_max_a: float = 0.35,
        w_goldilocks: float = 0.25,
        w_uncertainty: float = 0.10,
        w_shock: float = 0.00,
        regime_targets: Optional[Dict[str, Any]] = None,
    ) -> float:
        """
        Regime-Dynamic Satellite Target Weight (#111):
        W_sat*(t) = (pi_Goldilocks * 0.25) + (pi_Uncertainty * 0.10) + (pi_Shock * 0.00)
        Under Macro Shock (pi_shock >= 0.60 or dominant regime == 'Macro Shock'), overrides W_sat*(t) = 0.0.
        """
        if self.pi_shock >= 0.60 or self.get_dominant_regime() == "Macro Shock":
            return 0.0

        if regime_targets:
            wg = regime_targets.get("Goldilocks", w_goldilocks)
            w_goldilocks = float(wg.get("target", wg) if isinstance(wg, dict) else wg)
            wu = regime_targets.get("Uncertainty", w_uncertainty)
            w_uncertainty = float(wu.get("target", wu) if isinstance(wu, dict) else wu)
            ws = regime_targets.get("Shock", w_shock)
            w_shock = float(ws.get("target", ws) if isinstance(ws, dict) else ws)

        raw_target = (
            self.pi_goldilocks * w_goldilocks
            + self.pi_uncertainty * w_uncertainty
            + self.pi_shock * w_shock
        ) * drawdown_damper
        return round(min(w_max_a, max(0.0, raw_target)), 4)

    def calculate_dynamic_leland_bounds(
        self,
        w_sat_target: Optional[float] = None,
    ) -> Tuple[float, float, float]:
        """
        Dynamically compute Leland NTZ lower and upper bounds around W_sat*(t) (#111):
        tau_band = 0.04 if pi_Goldilocks > 0.70 else 0.02
        lower_bound = max(0.0, W_sat* - tau_band)
        upper_bound = min(0.35, W_sat* + tau_band)
        Returns (tau_band, lower_bound, upper_bound).
        """
        if w_sat_target is None:
            w_sat_target = self.calculate_target_satellite_weight()
        if w_sat_target <= 0.0 or self.pi_shock >= 0.60 or self.get_dominant_regime() == "Macro Shock":
            return 0.0, 0.0, 0.0
        tau_band = 0.04 if self.pi_goldilocks > 0.70 else 0.02
        lower_bound = round(max(0.0, w_sat_target - tau_band), 4)
        upper_bound = round(min(0.35, w_sat_target + tau_band), 4)
        return tau_band, lower_bound, upper_bound


@dataclass
class DualSleeveOrder:
    symbol: str
    side: str  # "BUY" or "SELL"
    shares: int
    target_notional: float
    price: float
    sleeve: str  # "CORE" or "SATELLITE"
    reason: str
    leg_id: int = 1


@dataclass
class DualSleeveSwapPlan:
    action: str  # "HARVEST_TO_CORE", "TRIM_CORE_TO_SATELLITE", "NO_TRADE_ZONE"
    active_satellite_weight: float
    target_satellite_weight: float
    band_lower: float
    band_upper: float
    satellite_notional_delta: float
    core_notional_delta: float
    orders: List[DualSleeveOrder] = field(default_factory=list)
    instantaneous_settlement_bypass: bool = True
    cash_buffer_sweep_usd: float = 0.0
    cash_sweep_vehicle: str = CASH_SWEEP_VEHICLE
    w_core: float = 0.85
    w_sat: float = 0.15
    w_buf: float = 0.0
    core_basket_shares: Dict[str, int] = field(default_factory=dict)
    routing_sweep_vector: Dict[str, float] = field(default_factory=dict)


class DualSleeveRouter:
    """
    Dual-Sleeve Capital Router implementing unchained Leland No-Trade Zone rebalancing
    between 3x Tactical Satellite S(t), multi-asset 1x Robo-Core C(t), and SWVXX cash
    sweep buffer M_buf(t), preserving W_core + W_sat + W_buf == 1.0 (#91 / #98 / #111).
    """

    def __init__(
        self,
        tau_band: Optional[float] = None,
        core_symbol: str = "SPY",
        routing_sweep_vector: Optional[Dict[str, float]] = None,
        cash_sweep_vehicle: str = CASH_SWEEP_VEHICLE,
        hmm: Optional[MetaGovernorHMM] = None,
        regime_targets: Optional[Dict[str, Any]] = None,
    ):
        self.tau_band = tau_band
        self.core_symbol = core_symbol
        self.routing_sweep_vector: Dict[str, float] = dict(
            routing_sweep_vector if routing_sweep_vector is not None else DEFAULT_ROUTING_SWEEP_VECTOR
        )
        self.cash_sweep_vehicle: str = cash_sweep_vehicle
        self.hmm: Optional[MetaGovernorHMM] = hmm
        self.regime_targets = regime_targets or DEFAULT_REGIME_SATELLITE_TARGETS

    def calculate_target_satellite_weight(
        self,
        hmm: Optional[MetaGovernorHMM] = None,
        pi: Optional[Union[Tuple[float, float, float], List[float]]] = None,
    ) -> float:
        """
        Compute dynamic target weight (#111):
        W_sat*(t) = (pi_Goldilocks * 0.25) + (pi_Uncertainty * 0.10) + (pi_Shock * 0.00)
        """
        active_hmm = hmm or self.hmm
        if pi is not None:
            pi_g, pi_u, pi_s = float(pi[0]), float(pi[1]), float(pi[2])
            if pi_s >= 0.60:
                return 0.0
            raw = (pi_g * 0.25) + (pi_u * 0.10) + (pi_s * 0.00)
            return round(min(0.35, max(0.0, raw)), 4)
        if active_hmm is not None:
            return active_hmm.calculate_target_satellite_weight(regime_targets=self.regime_targets)
        return 0.25

    def compute_dynamic_leland_bounds(
        self,
        target_satellite_weight: Optional[float] = None,
        hmm: Optional[MetaGovernorHMM] = None,
        tau_band: Optional[float] = None,
    ) -> Tuple[float, float, float]:
        """
        Dynamically compute Leland NTZ lower and upper bounds around W_sat*(t) (#111):
        tau_band = 0.04 if pi_Goldilocks > 0.70 else 0.02
        lower_bound = max(0.0, W_sat* - tau_band)
        upper_bound = min(0.35, W_sat* + tau_band)
        Returns (tau_band, lower_bound, upper_bound).
        """
        active_hmm = hmm or self.hmm
        w_sat = (
            float(target_satellite_weight)
            if target_satellite_weight is not None
            else self.calculate_target_satellite_weight(active_hmm)
        )
        is_macro_shock = (w_sat <= 0.0) or (
            active_hmm is not None
            and (active_hmm.pi_shock >= 0.60 or active_hmm.get_dominant_regime() == "Macro Shock")
        )
        if is_macro_shock:
            return 0.0, 0.0, 0.0

        if tau_band is not None:
            tau = float(tau_band)
        elif self.tau_band is not None:
            tau = float(self.tau_band)
        elif active_hmm is not None:
            tau = 0.04 if active_hmm.pi_goldilocks > 0.70 else 0.02
        else:
            tau = 0.02

        lower_bound = round(max(0.0, w_sat - tau), 4)
        upper_bound = round(min(0.35, w_sat + tau), 4)
        return tau, lower_bound, upper_bound

    compute_leland_bounds = compute_dynamic_leland_bounds

    def allocate_from_live_nlv(
        self,
        nlv: float,
        target_satellite_weight: Optional[float] = None,
        core_prices: Optional[Dict[str, float]] = None,
        satellite_prices: Optional[Dict[str, float]] = None,
        satellite_sub_weights: Optional[Dict[str, float]] = None,
        routing_sweep_vector: Optional[Dict[str, float]] = None,
        hmm: Optional[MetaGovernorHMM] = None,
        tau_band: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Scale-invariant portfolio initialization and quantization referencing live broker NLV exclusively.
        Evaluates breaches against dynamic Leland NTZ bounds (#111).
        Enforces discrete integer floor quantizations:
            Q_{sat, j} = floor((V_{sat, budget} * w_j) / P_j)
            Q_{core, c} = floor((V_{freed} * w_c) / P_c)
        and sweeps all fractional quantization remainders into SWVXX cash yield, guaranteeing:
            swvxx_cash >= 0.0 and abs((core_mv + sat_mv + swvxx_cash) - total_nlv) < 0.01.
        """
        live_nlv = max(0.0, float(nlv))
        active_hmm = hmm or self.hmm
        if target_satellite_weight is not None:
            w_sat_target = max(0.0, min(1.0, float(target_satellite_weight)))
        elif active_hmm is not None:
            w_sat_target = active_hmm.calculate_target_satellite_weight(regime_targets=self.regime_targets)
        else:
            w_sat_target = 0.25

        tau, lower_bound, upper_bound = self.compute_dynamic_leland_bounds(
            target_satellite_weight=w_sat_target,
            hmm=active_hmm,
            tau_band=tau_band,
        )

        c_px_map = dict(DEFAULT_CORE_PRICES)
        if core_prices:
            for sym, px in core_prices.items():
                if float(px) > 0.0:
                    c_px_map[sym.upper()] = float(px)

        sat_px_map = {"SOXL": 45.0, "TQQQ": 78.0, "TNA": 50.0}
        if satellite_prices:
            for sym, px in satellite_prices.items():
                if float(px) > 0.0:
                    sat_px_map[sym.upper()] = float(px)

        sub_w = dict(satellite_sub_weights or {"SOXL": 0.40, "TQQQ": 0.35, "TNA": 0.25})
        sweep_vec = dict(routing_sweep_vector if routing_sweep_vector is not None else self.routing_sweep_vector)

        sat_budget_usd = live_nlv * w_sat_target
        sat_positions: Dict[str, Dict[str, Any]] = {}
        sat_mv = 0.0

        for sym, w_j in sub_w.items():
            s = sym.upper()
            px_j = max(0.01, float(sat_px_map.get(s, 50.0)))
            q_j = math.floor((sat_budget_usd * float(w_j)) / px_j) if w_sat_target > 0.0 else 0
            mv_j = round(q_j * px_j, 4)
            sat_mv += mv_j
            sat_positions[s] = {
                "quantity": q_j,
                "current_price": px_j,
                "entry_price": px_j,
                "market_value": mv_j,
                "sleeve": "SATELLITE",
            }

        # Freed/remaining capital allocated across 1x Core basket using floor((V_freed * w_c) / P_c)
        v_freed_for_core = max(0.0, live_nlv - sat_mv)
        core_positions: Dict[str, Dict[str, Any]] = {}
        core_mv = 0.0

        for c_sym_raw, w_c_raw in sweep_vec.items():
            c_key = c_sym_raw.upper()
            if c_key in ("CASH", "CASH_BUFFER", self.cash_sweep_vehicle.upper()):
                continue
            w_c = float(w_c_raw)
            if w_c <= 0.0:
                continue
            px_c = max(0.01, float(c_px_map.get(c_key, 100.0)))
            q_c = math.floor((v_freed_for_core * w_c) / px_c)
            mv_c = round(q_c * px_c, 4)
            core_mv += mv_c
            core_positions[c_key] = {
                "quantity": q_c,
                "current_price": px_c,
                "entry_price": px_c,
                "market_value": mv_c,
                "sleeve": "CORE",
            }

        swvxx_cash = round(max(0.0, live_nlv - (core_mv + sat_mv)), 4)
        w_sat_actual = round(sat_mv / live_nlv, 6) if live_nlv > 0 else 0.0

        # Evaluate breaches against dynamic Leland NTZ bounds (#111)
        is_macro_shock = (w_sat_target <= 0.0) or (
            active_hmm is not None
            and (active_hmm.pi_shock >= 0.60 or active_hmm.get_dominant_regime() == "Macro Shock")
        )
        if is_macro_shock:
            is_breached = w_sat_actual > 0.0
            breach_direction = "UPPER" if is_breached else "NONE"
            breach_status = "MACRO_SHOCK_BREACH" if is_breached else "NO_BREACH"
        elif w_sat_actual > upper_bound:
            is_breached = True
            breach_direction = "UPPER"
            breach_status = "UPPER_BREACH"
        elif w_sat_actual < lower_bound:
            is_breached = True
            breach_direction = "LOWER"
            breach_status = "LOWER_BREACH"
        else:
            is_breached = False
            breach_direction = "NONE"
            breach_status = "NO_BREACH"

        return {
            "total_nlv": round(live_nlv, 4),
            "core_mv": round(core_mv, 4),
            "sat_mv": round(sat_mv, 4),
            "swvxx_cash": swvxx_cash,
            "cash_sweep_vehicle": self.cash_sweep_vehicle,
            "core_positions": core_positions,
            "satellite_positions": sat_positions,
            "w_core": round(core_mv / live_nlv, 6) if live_nlv > 0 else 0.0,
            "w_sat": w_sat_actual,
            "w_buf": round(swvxx_cash / live_nlv, 6) if live_nlv > 0 else 0.0,
            "target_satellite_weight": round(w_sat_target, 4),
            "tau_band": round(tau, 4),
            "lower_bound": round(lower_bound, 4),
            "upper_bound": round(upper_bound, 4),
            "band_lower": round(lower_bound, 4),
            "band_upper": round(upper_bound, 4),
            "is_breached": is_breached,
            "breach_direction": breach_direction,
            "breach_status": breach_status,
        }

    def plan_dual_sleeve_reallocation(
        self,
        nlv: float,
        satellite_positions: Dict[str, Any],
        target_satellite_weight: Optional[float] = None,
        core_positions: Optional[Dict[str, Any]] = None,
        core_symbol: Optional[str] = None,
        core_price: float = 585.0,
        core_prices: Optional[Dict[str, float]] = None,
        routing_sweep_vector: Optional[Dict[str, float]] = None,
        satellite_sub_weights: Optional[Dict[str, float]] = None,
        satellite_prices: Optional[Dict[str, float]] = None,
        tau_band: Optional[float] = None,
        trend_gate_active: bool = True,
        hmm: Optional[MetaGovernorHMM] = None,
    ) -> DualSleeveSwapPlan:
        """
        Evaluates active satellite weight w(t) against dynamic target W_sat^*(t) +- tau_band (#111).
        Generates paired atomic swap orders referencing live broker NLV exclusively:
        - When w(t) > W_sat^* + tau_band (or W_sat^* == 0.0 in Macro Shock):
          Sell Satellite to boundary -> Route freed notional V_freed across multi-asset Core
          basket (routing_sweep_vector) with discrete integer share flooring:
              Delta Q_c = floor((V_freed * w_{sweep, c}) / P_c(t))
          and quantize residual fractional cash balances into SWVXX / M_buf, preserving
              W_core + W_sat + W_buf == 1.0.
        - When w(t) < W_sat^* - tau_band (and trend gate active):
          Sell 1x Robo-Core -> Buy Satellite with proceeds and route fractional residue to SWVXX.
        - Inside band: No trade (zero turnover churn).
        """
        csym = (core_symbol or self.core_symbol).upper()
        active_hmm = hmm or self.hmm
        if target_satellite_weight is not None:
            w_sat_target = float(target_satellite_weight)
        elif active_hmm is not None:
            w_sat_target = active_hmm.calculate_target_satellite_weight(regime_targets=self.regime_targets)
        else:
            w_sat_target = 0.25

        tau, band_lower, band_upper = self.compute_dynamic_leland_bounds(
            target_satellite_weight=w_sat_target,
            hmm=active_hmm,
            tau_band=tau_band,
        )
        is_macro_shock_flatten = (w_sat_target <= 0.0) or (
            active_hmm is not None
            and (active_hmm.pi_shock >= 0.60 or active_hmm.get_dominant_regime() == "Macro Shock")
        )
        c_px = max(1.0, float(core_price))
        if routing_sweep_vector is not None:
            sweep_vec = dict(routing_sweep_vector)
        elif is_macro_shock_flatten:
            sweep_vec = {"IEF": 1.0, "cash": 0.0}
        else:
            sweep_vec = dict(self.routing_sweep_vector)

        # Build Core basket price map P_c(t)
        c_px_map: Dict[str, float] = dict(DEFAULT_CORE_PRICES)
        c_px_map[csym] = c_px
        if core_positions:
            for sym, pos in core_positions.items():
                s = sym.upper()
                if isinstance(pos, dict):
                    px_val = float(pos.get("current_price", pos.get("last_price", 0.0)))
                else:
                    px_val = float(getattr(pos, "current_price", getattr(pos, "last_price", 0.0)))
                if px_val > 0.0:
                    c_px_map[s] = px_val
        if core_prices:
            for sym, px_val in core_prices.items():
                if float(px_val) > 0.0:
                    c_px_map[sym.upper()] = float(px_val)

        # Calculate current satellite market value
        sat_val = 0.0
        sat_held: Dict[str, Tuple[int, float, float]] = {}  # sym -> (qty, px, mv)

        for sym, pos in satellite_positions.items():
            s = sym.upper()
            if isinstance(pos, dict):
                q = int(pos.get("quantity", 0))
                px = float(pos.get("current_price", pos.get("last_price", 0.0)))
                mv = float(pos.get("market_value", q * px))
            else:
                q = int(getattr(pos, "quantity", 0))
                px = float(getattr(pos, "current_price", getattr(pos, "last_price", 0.0)))
                mv = float(getattr(pos, "market_value", q * px))
            if q > 0 and px > 0.0:
                sat_held[s] = (q, px, mv)
                sat_val += mv

        active_weight = (sat_val / nlv) if nlv > 0.0 else 0.0

        # ---------------------------------------------------------------------
        # Case A: Harvesting / De-risking (Satellite > Upper Band or Macro Shock)
        # ---------------------------------------------------------------------
        if active_weight > band_upper:
            excess_weight = active_weight - band_upper
            excess_usd = excess_weight * nlv
            orders: List[DualSleeveOrder] = []
            freed_usd = 0.0

            # Leg 1: Sell 3x Satellite assets proportionally down to boundary (100% if Macro Shock)
            for s, (q, px, mv) in sat_held.items():
                if is_macro_shock_flatten:
                    shares_to_sell = q
                else:
                    frac = (mv / sat_val) if sat_val > 0.0 else (1.0 / len(sat_held))
                    asset_trim_usd = excess_usd * frac
                    shares_to_sell = min(q, max(1, int(round(asset_trim_usd / px))))
                notional = shares_to_sell * px
                freed_usd += notional
                orders.append(
                    DualSleeveOrder(
                        symbol=s,
                        side="SELL",
                        shares=shares_to_sell,
                        target_notional=round(notional, 2),
                        price=round(px, 4),
                        sleeve="SATELLITE",
                        reason=(
                            f"MACRO_SHOCK_LIQUIDATE: w({active_weight:.3f}) -> 0.000"
                            if is_macro_shock_flatten
                            else f"LELAND_NTZ_HARVEST: w({active_weight:.3f}) > upper({band_upper:.3f})"
                        ),
                        leg_id=1,
                    )
                )

            # Leg 2: Route freed notional V_freed across multi-asset Core basket (routing_sweep_vector)
            # Delta Q_c = floor((V_freed * w_{sweep, c}) / P_c(t))
            total_core_deployed = 0.0
            core_basket_shares: Dict[str, int] = {}

            for c_sym_raw, w_sweep_c in sweep_vec.items():
                c_key = c_sym_raw.upper()
                if c_key in ("CASH", "CASH_BUFFER", self.cash_sweep_vehicle.upper()):
                    continue
                w_c = float(w_sweep_c)
                if w_c <= 0.0:
                    continue
                px_c = max(0.01, float(c_px_map.get(c_key, c_px)))
                delta_q_c = math.floor((freed_usd * w_c) / px_c)
                core_basket_shares[c_key] = delta_q_c
                if delta_q_c > 0:
                    core_notional_c = delta_q_c * px_c
                    total_core_deployed += core_notional_c
                    orders.append(
                        DualSleeveOrder(
                            symbol=c_key,
                            side="BUY",
                            shares=delta_q_c,
                            target_notional=round(core_notional_c, 2),
                            price=round(px_c, 4),
                            sleeve="CORE",
                            reason=f"LELAND_NTZ_SWEEP_INTO_CORE: w_sweep({w_c:.2f}) multi-asset basket reinvestment",
                            leg_id=2,
                        )
                    )

            # Quantize residual fractional cash balance into cash sweep buffer (SWVXX / M_buf)
            residual_cash_buf = max(0.0, freed_usd - total_core_deployed)
            post_sat_val = max(0.0, sat_val - freed_usd)
            w_sat_post = (post_sat_val / nlv) if nlv > 0.0 else 0.0
            w_buf_post = (residual_cash_buf / nlv) if nlv > 0.0 else 0.0
            w_core_post = max(0.0, 1.0 - w_sat_post - w_buf_post)

            return DualSleeveSwapPlan(
                action="HARVEST_TO_CORE",
                active_satellite_weight=round(active_weight, 4),
                target_satellite_weight=round(w_sat_target, 4),
                band_lower=round(band_lower, 4),
                band_upper=round(band_upper, 4),
                satellite_notional_delta=-round(freed_usd, 2),
                core_notional_delta=round(total_core_deployed, 2),
                orders=orders,
                instantaneous_settlement_bypass=True,
                cash_buffer_sweep_usd=round(residual_cash_buf, 2),
                cash_sweep_vehicle=self.cash_sweep_vehicle,
                w_core=round(w_core_post, 6),
                w_sat=round(w_sat_post, 6),
                w_buf=round(1.0 - round(w_core_post, 6) - round(w_sat_post, 6), 6),
                core_basket_shares=core_basket_shares,
                routing_sweep_vector=sweep_vec,
            )

        # ---------------------------------------------------------------------
        # Case B: Risk-On / NTZ Dip-Buying (Satellite < Lower Band)
        # ---------------------------------------------------------------------
        elif active_weight < band_lower and trend_gate_active:
            deficit_weight = band_lower - active_weight
            expansion_usd = deficit_weight * nlv
            orders = []

            # Leg 1: Trim 1x Robo-Core to fund Satellite expansion
            core_shares_to_sell = max(1, int(math.ceil(expansion_usd / c_px)))
            if core_positions and csym in core_positions:
                pos_obj = core_positions[csym]
                held_core_q = int(pos_obj.get("quantity", 0) if isinstance(pos_obj, dict) else getattr(pos_obj, "quantity", 0))
                if held_core_q > 0:
                    core_shares_to_sell = min(core_shares_to_sell, held_core_q)
            core_trim_usd = core_shares_to_sell * c_px
            orders.append(
                DualSleeveOrder(
                    symbol=csym,
                    side="SELL",
                    shares=core_shares_to_sell,
                    target_notional=round(core_trim_usd, 2),
                    price=round(c_px, 4),
                    sleeve="CORE",
                    reason=f"LELAND_NTZ_TRIM_CORE: Fund satellite dip-buy w({active_weight:.3f}) < lower({band_lower:.3f})",
                    leg_id=1,
                )
            )

            # Leg 2: Deploy proceeds into 3x Satellite assets based on sub-weights
            sub_w = satellite_sub_weights or {"SOXL": 0.40, "TQQQ": 0.35, "TNA": 0.25}
            sat_px_map = satellite_prices or {"SOXL": 45.0, "TQQQ": 78.0, "TNA": 50.0}
            deployable_budget_usd = min(expansion_usd, core_trim_usd)
            total_deployed = 0.0

            for sym, weight in sub_w.items():
                s = sym.upper()
                px = sat_px_map.get(s, 50.0)
                asset_usd = deployable_budget_usd * weight
                shares_to_buy = math.floor(asset_usd / px)
                if shares_to_buy > 0:
                    notional = shares_to_buy * px
                    total_deployed += notional
                    orders.append(
                        DualSleeveOrder(
                            symbol=s,
                            side="BUY",
                            shares=shares_to_buy,
                            target_notional=round(notional, 2),
                            price=round(px, 4),
                            sleeve="SATELLITE",
                            reason=f"LELAND_NTZ_BUY_SATELLITE: Sub-weight {weight:.2f}",
                            leg_id=2,
                        )
                    )

            residual_cash_buf = max(0.0, core_trim_usd - total_deployed)
            post_sat_val = sat_val + total_deployed
            w_sat_post = (post_sat_val / nlv) if nlv > 0.0 else 0.0
            w_buf_post = (residual_cash_buf / nlv) if nlv > 0.0 else 0.0
            w_core_post = max(0.0, 1.0 - w_sat_post - w_buf_post)

            return DualSleeveSwapPlan(
                action="TRIM_CORE_TO_SATELLITE",
                active_satellite_weight=round(active_weight, 4),
                target_satellite_weight=round(w_sat_target, 4),
                band_lower=round(band_lower, 4),
                band_upper=round(band_upper, 4),
                satellite_notional_delta=round(total_deployed, 2),
                core_notional_delta=-round(core_trim_usd, 2),
                orders=orders,
                instantaneous_settlement_bypass=True,
                cash_buffer_sweep_usd=round(residual_cash_buf, 2),
                cash_sweep_vehicle=self.cash_sweep_vehicle,
                w_core=round(w_core_post, 6),
                w_sat=round(w_sat_post, 6),
                w_buf=round(1.0 - round(w_core_post, 6) - round(w_sat_post, 6), 6),
                core_basket_shares={csym: -core_shares_to_sell},
                routing_sweep_vector=sweep_vec,
            )

        # ---------------------------------------------------------------------
        # Case C: Inside Leland NTZ Band
        # ---------------------------------------------------------------------
        action_name = "NO_TRADE_ZONE" if trend_gate_active else "TREND_GATE_INHIBITED"
        w_sat_curr = round(active_weight, 6)
        w_core_curr = round(max(0.0, 1.0 - w_sat_curr), 6)
        return DualSleeveSwapPlan(
            action=action_name,
            active_satellite_weight=round(active_weight, 4),
            target_satellite_weight=round(w_sat_target, 4),
            band_lower=round(band_lower, 4),
            band_upper=round(band_upper, 4),
            satellite_notional_delta=0.0,
            core_notional_delta=0.0,
            orders=[],
            instantaneous_settlement_bypass=True,
            cash_buffer_sweep_usd=0.0,
            cash_sweep_vehicle=self.cash_sweep_vehicle,
            w_core=w_core_curr,
            w_sat=w_sat_curr,
            w_buf=round(1.0 - w_core_curr - w_sat_curr, 6),
            core_basket_shares={},
            routing_sweep_vector=sweep_vec,
        )


class RiskEngine:
    MIN_MARGIN_EQUITY: float = 2000.0
    INTRADAY_MARGIN_REQ: float = 0.25
    OVERNIGHT_MARGIN_REQ: float = 0.50
    MAX_INTRADAY_LEVERAGE: float = 4.0
    MAX_OVERNIGHT_LEVERAGE: float = 2.0

    def __init__(
        self,
        wins: int = 45,
        executions: int = 100,
        payoff_ratio: float = 1.6,
        account_base: float = 0.0,
        enforce_letf_house_margin: bool = False,
    ):
        self._active_positions = {}
        self.brackets: Dict[str, Any] = {}
        self.macro_risk_multiplier: float = 1.0
        # Empirical performance inputs. These are static defaults until the
        # trade log feeds them; the posterior (and therefore the high-probability
        # gate) is only as informative as these numbers.
        self.wins = wins
        self.executions = executions
        self.payoff_ratio = payoff_ratio

        # FINRA Rule 4210 Margin Framework State
        self.account_equity: float = float(account_base)
        self.cash_balance: float = float(account_base)
        self.swvxx_cash_balance: float = float(account_base)
        self.positions: Dict[str, Dict[str, Any]] = {}
        self._tick_history: Dict[str, List[Tuple[float, float]]] = {}
        self.intraday_buying_power: float = 0.0
        self.overnight_buying_power: float = 0.0
        self.total_position_value: float = 0.0
        self.intraday_margin_deficit: float = 0.0
        self.is_margin_eligible: bool = False
        self.enforce_letf_house_margin: bool = enforce_letf_house_margin

        # Dual-Sleeve Meta-Governor & Router State (#91 / #98 / #111)
        self.hmm = MetaGovernorHMM()
        self.router = DualSleeveRouter(hmm=self.hmm)
        self.satellite_peak_equity: float = float(account_base)
        self.cumulative_core_heal: float = 0.0
        self.lambda_0: float = 0.03
        self.kappa_core: float = 0.5
        self.last_lambda_hwm_eff: float = 0.03

        # Volatility Trigger & Instantaneous Reallocation State
        self.regime: str = "NORMAL"
        self.volatility_multiplier: float = 1.0
        self.instantaneous_reallocation_permitted: bool = True

        # Deprecation of legacy PDT & day-trade counters
        self.pdt_restricted: bool = False
        self.day_trade_count: int = 0

        # Workstream 3 (#123): SEC Rule 15c6-1 T+1 Cash Ledger & Pre-Trade GFV Firewall
        self.t1_ledger: T1CashLedger = T1CashLedger(
            settled_cash=account_base,
            nlv=account_base,
        )

        self.recalculate_margin_buying_power()

    def can_submit_order(
        self,
        side: str,
        notional: float,
        *,
        symbol: str = "",
        nlv: Optional[float] = None,
        cash_floor_pct: Optional[float] = None,
    ) -> Tuple[bool, str]:
        """Pre-Trade GFV Firewall delegated to the T+1 Cash Ledger (#123)."""
        return self.t1_ledger.can_submit_order(
            side=side,
            notional=notional,
            nlv=nlv,
            cash_floor_pct=cash_floor_pct,
            symbol=symbol,
        )

    def process_t1_overnight_clearing(self, current_dt: Optional[datetime] = None) -> float:
        """Executes 00:00:00 EDT T+1 overnight settlement clearing on the T1CashLedger."""
        return float(self.t1_ledger.process_t1_overnight_clearing(current_dt=current_dt))

    def create_breakout_bracket(
        self,
        symbol: str,
        total_shares: int,
        entry_price: float,
        s0_reference: Optional[float] = None,
        trade_store: Optional[Any] = None,
    ) -> Any:
        """
        Instantiates MultiTrancheBracket upon confirmed breakout entry (#62).
        Records initial state to active positions and trade_store if available.
        """
        from execution.bracket_manager import MultiTrancheBracket
        bracket = MultiTrancheBracket(
            symbol=symbol,
            total_shares=total_shares,
            entry_price=entry_price,
            s0_reference=s0_reference,
        )
        sym = symbol.upper()
        self.brackets[sym] = bracket
        self._active_positions[sym] = bracket

        if trade_store is not None and hasattr(trade_store, "record_tranche_progression"):
            try:
                trade_store.record_tranche_progression(
                    symbol=sym,
                    tranche_1_filled=False,
                    tranche_2_filled=False,
                    breakeven_latched=False,
                )
            finally:
                pass

        logger.info(
            "[%s] MultiTrancheBracket instantiated upon breakout entry: %d shares @ $%.2f",
            sym, total_shares, entry_price,
        )
        return bracket

    def get_bracket(self, symbol: str) -> Optional[Any]:
        return self.brackets.get(symbol.upper())

    def remove_bracket(self, symbol: str) -> Optional[Any]:
        sym = symbol.upper()
        self._active_positions.pop(sym, None)
        return self.brackets.pop(sym, None)

    def update_bracket_state(
        self,
        symbol: str,
        current_price: float,
        current_time: datetime,
        yz_vol: float = 0.0,
        hurst_exponent: Optional[float] = None,
        trade_store: Optional[Any] = None,
    ) -> List[Any]:
        """
        Updates bracket state on market price/time update.
        Records tranche progression to trade_store on state transitions.
        """
        bracket = self.get_bracket(symbol)
        if bracket is None:
            return []

        actions = bracket.update_state(
            current_price, current_time, yz_vol=yz_vol, hurst_exponent=hurst_exponent
        )
        if actions and trade_store is not None and hasattr(trade_store, "record_tranche_progression"):
            try:
                trade_store.record_tranche_progression(
                    symbol=symbol.upper(),
                    tranche_1_filled=bracket.tranche_1_filled,
                    tranche_2_filled=bracket.tranche_2_filled,
                    breakeven_latched=bracket.breakeven_latched,
                )
            except Exception as exc:
                logger.warning("[%s] Failed to update tranche progression in trade_store: %s", symbol, exc)

        return actions

    def update_macro_risk_multiplier(self, multiplier: float) -> None:
        """Dynamically updates the macro liquidity risk multiplier (0.0 to 1.0)."""
        self.macro_risk_multiplier = max(0.0, min(1.0, float(multiplier)))
        logger.info(f"RiskEngine macro risk multiplier updated to: {self.macro_risk_multiplier:.4f}")

    def get_asset_margin_requirements(
        self,
        symbol: str,
        enforce_letf_house_margin: Optional[bool] = None,
    ) -> Tuple[float, float]:
        """
        Returns (intraday_margin_req, overnight_margin_req) for a given symbol under FINRA Rule 4210.
        When enforce_letf_house_margin is enabled:
        - 3x Leveraged ETFs (SOXL, TQQQ, TNA, etc.): 75% intraday / 90% overnight.
        - 1x Core Equities (SPY, QQQ, etc.): 25% intraday / 50% Reg T overnight.
        When enforce_letf_house_margin is False (legacy default):
        - Returns uniform (0.25, 0.50).
        """
        enforce = self.enforce_letf_house_margin if enforce_letf_house_margin is None else enforce_letf_house_margin
        if enforce and symbol.upper() in LETF_3X_SYMBOLS:
            return (0.75, 0.90)
        return (self.INTRADAY_MARGIN_REQ, self.OVERNIGHT_MARGIN_REQ)

    def recalculate_margin_buying_power(self) -> Dict[str, Any]:
        """
        Dynamically calculates Dual Buying Power in real-time under FINRA Rule 4210:
        1. Intraday Margin Buying Power: Up to 4x leverage (25% maintenance margin requirement)
           calculated dynamically in real-time based on active tick valuations.
        2. Regulation T Overnight Buying Power: Up to 2x leverage (50% margin requirement).
        3. Enforces $2,000 minimum equity check for margin eligibility.
        """
        self.total_position_value = sum(
            pos.get("quantity", 0) * pos.get("current_price", 0.0)
            for pos in self.positions.values()
        )

        equity = self.account_equity
        self.is_margin_eligible = equity >= self.MIN_MARGIN_EQUITY

        if not self.is_margin_eligible:
            # Below $2,000: Account is not margin eligible (cash-only, no borrowing)
            avail_cash = max(0.0, equity - self.total_position_value)
            self.intraday_buying_power = avail_cash
            self.overnight_buying_power = avail_cash
            self.intraday_margin_deficit = 0.0 if avail_cash >= 0 else abs(avail_cash)
        elif self.enforce_letf_house_margin:
            # Asset-specific FINRA 4210 House Maintenance Margin
            intraday_req = sum(
                pos.get("quantity", 0) * pos.get("current_price", 0.0) * self.get_asset_margin_requirements(sym, True)[0]
                for sym, pos in self.positions.items()
            )
            intraday_excess = equity - intraday_req
            if intraday_excess < 0:
                self.intraday_margin_deficit = abs(intraday_excess)
                self.intraday_buying_power = 0.0
            else:
                self.intraday_margin_deficit = 0.0
                self.intraday_buying_power = max(0.0, intraday_excess * self.MAX_INTRADAY_LEVERAGE)

            overnight_req = sum(
                pos.get("quantity", 0) * pos.get("current_price", 0.0) * self.get_asset_margin_requirements(sym, True)[1]
                for sym, pos in self.positions.items()
            )
            overnight_excess = equity - overnight_req
            self.overnight_buying_power = max(0.0, overnight_excess * self.MAX_OVERNIGHT_LEVERAGE)
        else:
            # Intraday Margin: 25% maintenance requirement (up to 4x leverage)
            intraday_req = self.total_position_value * self.INTRADAY_MARGIN_REQ
            intraday_excess = equity - intraday_req
            if intraday_excess < 0:
                self.intraday_margin_deficit = abs(intraday_excess)
                self.intraday_buying_power = 0.0
            else:
                self.intraday_margin_deficit = 0.0
                self.intraday_buying_power = max(0.0, intraday_excess * self.MAX_INTRADAY_LEVERAGE)

            # Regulation T Overnight: 50% requirement (up to 2x leverage)
            overnight_req = self.total_position_value * self.OVERNIGHT_MARGIN_REQ
            overnight_excess = equity - overnight_req
            self.overnight_buying_power = max(0.0, overnight_excess * self.MAX_OVERNIGHT_LEVERAGE)

        return {
            "account_equity": self.account_equity,
            "total_position_value": self.total_position_value,
            "intraday_buying_power": self.intraday_buying_power,
            "overnight_buying_power": self.overnight_buying_power,
            "intraday_margin_deficit": self.intraday_margin_deficit,
            "is_margin_eligible": self.is_margin_eligible,
            "instantaneous_reallocation": self.instantaneous_reallocation_permitted,
        }

    @property
    def live_nlv(self) -> float:
        """Authoritative live broker Net Liquidation Value."""
        return self.account_equity

    def get_sleeve_balances(self) -> Dict[str, float]:
        """
        Computes current live valuations of Core sleeve, Satellite sleeve, SWVXX cash buffer,
        and total NLV to verify the conservation invariant:
            abs((core_mv + sat_mv + swvxx_cash) - total_nlv) < 0.01
        """
        core_mv = 0.0
        sat_mv = 0.0
        for sym, pos in self.positions.items():
            s = sym.upper()
            mv = float(pos.get("quantity", 0)) * float(pos.get("current_price", 0.0))
            if s in LETF_3X_SYMBOLS or pos.get("sleeve") == "SATELLITE":
                sat_mv += mv
            else:
                core_mv += mv
        swvxx_cash = max(0.0, float(self.swvxx_cash_balance))
        total_nlv = float(self.account_equity)
        return {
            "core_mv": round(core_mv, 4),
            "sat_mv": round(sat_mv, 4),
            "swvxx_cash": round(swvxx_cash, 4),
            "total_nlv": round(total_nlv, 4),
            "invariant_drift": abs((core_mv + sat_mv + swvxx_cash) - total_nlv),
        }

    def allocate_from_live_nlv(
        self,
        nlv: Optional[float] = None,
        target_satellite_weight: Optional[float] = None,
        core_prices: Optional[Dict[str, float]] = None,
        satellite_prices: Optional[Dict[str, float]] = None,
        satellite_sub_weights: Optional[Dict[str, float]] = None,
        routing_sweep_vector: Optional[Dict[str, float]] = None,
        tau_band: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Initializes or re-syncs internal dual-sleeve positions and SWVXX cash buffer
        exclusively from live broker NLV using discrete integer share floors.
        Evaluates breaches against dynamic Leland NTZ bounds (#111).
        """
        eff_nlv = float(nlv) if nlv is not None else float(self.account_equity)
        if target_satellite_weight is None:
            target_satellite_weight = self.hmm.calculate_target_satellite_weight()
        alloc = self.router.allocate_from_live_nlv(
            nlv=eff_nlv,
            target_satellite_weight=target_satellite_weight,
            core_prices=core_prices,
            satellite_prices=satellite_prices,
            satellite_sub_weights=satellite_sub_weights,
            routing_sweep_vector=routing_sweep_vector,
            hmm=self.hmm,
            tau_band=tau_band,
        )
        combined_positions: Dict[str, Dict[str, Any]] = {}
        for sym, pos in alloc["core_positions"].items():
            if pos["quantity"] > 0:
                combined_positions[sym] = dict(pos)
        for sym, pos in alloc["satellite_positions"].items():
            if pos["quantity"] > 0:
                combined_positions[sym] = dict(pos)

        self.account_equity = float(alloc["total_nlv"])
        self.cash_balance = float(alloc["swvxx_cash"])
        self.swvxx_cash_balance = float(alloc["swvxx_cash"])
        self.positions = combined_positions
        self.recalculate_margin_buying_power()
        return alloc

    def update_tick(
        self,
        symbol: str,
        price: float,
        timestamp_s: Optional[float] = None,
    ) -> None:
        """
        Real-time pipeline update: Adjusts position valuation from active tick data
        and updates account equity and intraday buying power in real-time.
        Monitors 30-second rolling returns on 3x LETFs to detect flash crashes (>= 15% drop in <= 30s)
        and immediately transition the HMM Meta-Governor to 'Macro Shock'.
        """
        sym = symbol.upper()
        new_px = float(price)
        now_s = float(timestamp_s) if timestamp_s is not None else 0.0

        # Track 30-second rolling tick window for flash-crash detection on 3x LETFs
        hist = self._tick_history.setdefault(sym, [])
        if sym in self.positions and not hist:
            ep = float(self.positions[sym].get("entry_price", self.positions[sym].get("current_price", new_px)))
            hist.append((max(0.0, now_s - 1.0), ep))
        hist.append((now_s, new_px))
        if now_s > 0.0:
            cutoff = now_s - 30.0
            self._tick_history[sym] = [(t, p) for (t, p) in hist if t >= cutoff]
        elif len(hist) > 60:
            self._tick_history[sym] = hist[-60:]

        if sym in LETF_3X_SYMBOLS and self._tick_history[sym]:
            window_max_px = max(p for _, p in self._tick_history[sym])
            if window_max_px > 0.0:
                drop_pct = (window_max_px - new_px) / window_max_px
                if drop_pct >= 0.15:
                    self.hmm.update((-2.5, -0.90, 640.0))
                    self.regime = "MACRO_SHOCK"

        if sym in self.positions:
            old_px = float(self.positions[sym].get("current_price", price))
            qty = int(self.positions[sym].get("quantity", 0))
            self.positions[sym]["current_price"] = new_px
            self.positions[sym]["market_value"] = qty * new_px
            pnl_diff = (new_px - old_px) * qty
            self.account_equity += pnl_diff
            self.recalculate_margin_buying_power()

    def update_position(
        self,
        symbol: str,
        quantity: int,
        current_price: float,
        entry_price: Optional[float] = None,
    ) -> None:
        """
        Updates an active position in the internal position book and recalculates buying power.
        """
        sym = symbol.upper()
        qty = int(quantity)
        px = float(current_price)
        ep = float(entry_price) if entry_price is not None else px

        if qty <= 0:
            self.positions.pop(sym, None)
        else:
            self.positions[sym] = {
                "quantity": qty,
                "entry_price": ep,
                "current_price": px,
                "market_value": qty * px,
            }
        self.recalculate_margin_buying_power()

    def sync_account_state(
        self,
        equity: float,
        cash: Optional[float] = None,
        positions: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Authoritative sync of account equity, cash balance, and open position valuations.
        """
        self.account_equity = float(equity)
        if cash is not None:
            self.cash_balance = float(cash)
            self.swvxx_cash_balance = float(cash)
        if positions is not None:
            parsed = {}
            for sym, p in positions.items():
                s = str(sym).upper()
                if isinstance(p, dict):
                    qty = int(p.get("quantity", 0))
                    entry_px = float(p.get("entry_price", 0.0))
                    curr_px = float(p.get("last_price", p.get("current_price", entry_px)))
                else:
                    qty = int(getattr(p, "quantity", 0))
                    entry_px = float(getattr(p, "entry_price", 0.0))
                    curr_px = float(getattr(p, "last_price", getattr(p, "current_price", entry_px)))
                if qty > 0:
                    parsed[s] = {
                        "quantity": qty,
                        "entry_price": entry_px,
                        "current_price": curr_px,
                        "market_value": qty * curr_px,
                    }
            self.positions = parsed
        self.recalculate_margin_buying_power()
        if cash is None:
            self.swvxx_cash_balance = max(0.0, self.account_equity - self.total_position_value)

    def validate_pre_trade_margin(
        self,
        symbol: str,
        quantity: int,
        price: float,
        side: str = "BUY",
    ) -> Tuple[bool, str]:
        """
        Pre-trade execution check under FINRA Rule 4210.
        Actively blocks any order that would create or exacerbate an intraday margin deficit.
        """
        if str(side).upper() != "BUY":
            return True, "ok"

        order_notional = float(quantity) * float(price)
        if order_notional <= 0:
            return False, "Order notional must be positive"

        # 1. $2,000 Minimum Equity Check for Margin Eligibility
        if self.account_equity < self.MIN_MARGIN_EQUITY:
            available_cash = max(0.0, self.account_equity - self.total_position_value)
            if order_notional > available_cash:
                return False, (
                    f"Margin ineligible: Account equity ${self.account_equity:,.2f} is below the "
                    f"${self.MIN_MARGIN_EQUITY:,.2f} minimum requirement. Order notional ${order_notional:,.2f} "
                    f"exceeds available cash ${available_cash:,.2f}."
                )

        # 2. Existing Margin Deficit Check: actively block any order that would exacerbate deficit
        if self.intraday_margin_deficit > 0:
            return False, (
                f"Order blocked: Account is currently in an intraday margin deficit of "
                f"${self.intraday_margin_deficit:,.2f}. Any new purchase would exacerbate the deficit."
            )

        # 3. Intraday Buying Power Check: actively block order exceeding intraday buying power
        if order_notional > self.intraday_buying_power:
            return False, (
                f"Order blocked: Order notional ${order_notional:,.2f} exceeds real-time "
                f"Intraday Margin Buying Power ${self.intraday_buying_power:,.2f}."
            )

        # 4. Prospective Deficit Verification: prospective requirement must not exceed equity
        if self.enforce_letf_house_margin:
            sym_intra, _ = self.get_asset_margin_requirements(symbol, True)
            current_margin_req = sum(
                pos.get("quantity", 0) * pos.get("current_price", 0.0) * self.get_asset_margin_requirements(s, True)[0]
                for s, pos in self.positions.items()
            )
            prospective_margin_req = current_margin_req + (order_notional * sym_intra)
            if prospective_margin_req > self.account_equity:
                deficit = prospective_margin_req - self.account_equity
                return False, (
                    f"Order blocked: Prospective maintenance margin requirement ${prospective_margin_req:,.2f} "
                    f"(including {sym_intra*100:.0f}% FINRA Rule 4210 requirement for {symbol}) "
                    f"exceeds equity ${self.account_equity:,.2f} (would create intraday margin deficit of ${deficit:,.2f})."
                )
        else:
            prospective_total_val = self.total_position_value + order_notional
            prospective_margin_req = prospective_total_val * self.INTRADAY_MARGIN_REQ
            if prospective_margin_req > self.account_equity:
                deficit = prospective_margin_req - self.account_equity
                return False, (
                    f"Order blocked: Prospective maintenance margin requirement ${prospective_margin_req:,.2f} "
                    f"exceeds equity ${self.account_equity:,.2f} (would create intraday margin deficit of ${deficit:,.2f})."
                )

        return True, "ok"

    def check_order_allowed(
        self,
        symbol: str,
        quantity: int,
        price: float,
        side: str = "BUY",
    ) -> Tuple[bool, str]:
        """Convenience alias for validate_pre_trade_margin."""
        return self.validate_pre_trade_margin(symbol, quantity, price, side=side)

    def update_governor_regime(self, regime: str, volatility_multiplier: float = 1.0) -> None:
        """
        Updates strategy regime and volatility trigger from governor.py.
        Permits instantaneous capital reallocation back into equities the moment
        the volatility trigger signals a 'Normal' regime, without applying a 24-hour settlement waiting period.
        """
        reg_upper = str(regime).upper()
        is_normal = reg_upper in ("NORMAL", "A") or (0.8 <= float(volatility_multiplier) <= 1.2)

        self.regime = "NORMAL" if is_normal else reg_upper
        self.volatility_multiplier = float(volatility_multiplier)
        self.macro_risk_multiplier = 1.0 if is_normal else min(1.0, max(0.0, 1.0 / self.volatility_multiplier))

        if is_normal:
            self.instantaneous_reallocation_permitted = True
            logger.info(
                "RiskEngine: Governor volatility trigger signals NORMAL regime (vol_mult=%.2f). "
                "Instantaneous capital reallocation back into equities enabled (0h settlement lag).",
                self.volatility_multiplier,
            )
        else:
            logger.info(
                "RiskEngine: Governor signals non-normal regime %s (vol_mult=%.2f). Macro multiplier: %.2f.",
                self.regime, self.volatility_multiplier, self.macro_risk_multiplier,
            )
        self.recalculate_margin_buying_power()

    def calculate_overnight_excess(
        self,
        enforce_letf_house_margin: Optional[bool] = None,
    ) -> Tuple[float, float, float]:
        """
        Calculates the delta between total open position value and allowable Reg T overnight limit.
        Returns: (delta, total_position_value, allowable_overnight_limit)
        """
        self.recalculate_margin_buying_power()
        enforce = self.enforce_letf_house_margin if enforce_letf_house_margin is None else enforce_letf_house_margin
        if enforce:
            overnight_req = sum(
                pos.get("quantity", 0) * pos.get("current_price", 0.0) * self.get_asset_margin_requirements(s, True)[1]
                for s, pos in self.positions.items()
            )
            delta = max(0.0, overnight_req - self.account_equity)
            allowable_overnight_limit = self.account_equity
            return delta, self.total_position_value, allowable_overnight_limit
        else:
            allowable_overnight_limit = self.account_equity * self.MAX_OVERNIGHT_LEVERAGE
            delta = max(0.0, self.total_position_value - allowable_overnight_limit)
            return delta, self.total_position_value, allowable_overnight_limit

    def plan_eod_overnight_liquidation(self) -> List[Dict[str, Any]]:
        """
        Determines the exact positions and quantities to liquidate to eliminate
        any Reg T overnight margin excess (delta) prior to the 8:00 p.m. ET cutoff.
        """
        delta, total_val, allowable_limit = self.calculate_overnight_excess()
        if delta <= 0:
            return []

        liquidation_orders = []
        remaining_reduction_needed = delta

        # Sort positions descending by market value to reduce largest exposures first
        sorted_positions = sorted(
            self.positions.items(),
            key=lambda item: item[1].get("quantity", 0) * item[1].get("current_price", 0.0),
            reverse=True,
        )

        for sym, pos in sorted_positions:
            qty = pos.get("quantity", 0)
            px = pos.get("current_price", 0.0)
            if qty <= 0 or px <= 0:
                continue

            pos_val = qty * px
            if pos_val <= remaining_reduction_needed:
                shares_to_sell = qty
                notional_reduction = pos_val
            else:
                shares_to_sell = math.ceil(remaining_reduction_needed / px)
                shares_to_sell = min(shares_to_sell, qty)
                notional_reduction = shares_to_sell * px

            liquidation_orders.append({
                "symbol": sym,
                "shares_to_sell": shares_to_sell,
                "estimated_price": px,
                "notional_reduction": notional_reduction,
                "reason": "REG_T_OVERNIGHT_EXCESS_LIQUIDATION",
            })

            remaining_reduction_needed -= notional_reduction
            if remaining_reduction_needed <= 0:
                break

        return liquidation_orders

    def execute_eod_overnight_liquidation(
        self,
        order_manager: Optional[Any] = None,
        notifier: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Executes automated liquidation of excess leveraged positions prior to the 8:00 p.m. ET cutoff
        to prevent margin calls.
        """
        delta, total_val, allowable_limit = self.calculate_overnight_excess()
        orders = self.plan_eod_overnight_liquidation()

        if delta <= 0 or not orders:
            logger.info(
                "EOD Reg T Check: Open positions ($%.2f) within overnight limit ($%.2f). No liquidation needed.",
                total_val, allowable_limit,
            )
            return {
                "delta": 0.0,
                "total_position_value": total_val,
                "allowable_overnight_limit": allowable_limit,
                "orders_executed": [],
                "alert_triggered": False,
            }

        alert_msg = (
            f"EOD Reg T Overnight Excess: Total position value ${total_val:,.2f} exceeds "
            f"allowable 2x limit ${allowable_limit:,.2f} by ${delta:,.2f}. "
            f"Executing automated liquidation of {len(orders)} position(s) prior to 8:00 p.m. ET cutoff."
        )
        logger.warning(alert_msg)

        if notifier is not None and hasattr(notifier, "send_alert"):
            try:
                notifier.send_alert(
                    title="⚠️ Reg T Overnight Margin Excess Alert",
                    message=alert_msg,
                    priority="urgent",
                    tags=["warning", "money_with_wings"],
                )
            except Exception as exc:
                logger.warning("Failed to dispatch EOD overnight alert: %s", exc)
        else:
            try:
                from core.notifier import send_alert
                send_alert(
                    title="⚠️ Reg T Overnight Margin Excess Alert",
                    message=alert_msg,
                    priority="urgent",
                    tags=["warning", "money_with_wings"],
                )
            except Exception:
                pass

        executed = []
        for o in orders:
            sym = o["symbol"]
            qty = o["shares_to_sell"]
            if order_manager is not None and hasattr(order_manager, "execute_market_sell"):
                try:
                    order_id = order_manager.execute_market_sell(
                        symbol=sym,
                        quantity=qty,
                        reason="REG_T_OVERNIGHT_EXCESS_LIQUIDATION",
                    )
                    o["order_id"] = order_id
                    executed.append(o)
                    logger.info("Executed Reg T overnight liquidation sell: %s x%d (order %s)", sym, qty, order_id)
                except Exception as exc:
                    logger.error("Failed to execute Reg T liquidation for %s: %s", sym, exc)
            else:
                o["order_id"] = f"SIM_EOD_LIQ_{sym}"
                executed.append(o)
                if sym in self.positions:
                    self.positions[sym]["quantity"] = max(0, self.positions[sym]["quantity"] - qty)
                    if self.positions[sym]["quantity"] == 0:
                        del self.positions[sym]

        self.recalculate_margin_buying_power()
        return {
            "delta": delta,
            "total_position_value": total_val,
            "allowable_overnight_limit": allowable_limit,
            "orders_executed": executed,
            "alert_triggered": True,
        }

    def plan_two_stage_eod_deleveraging(
        self,
        current_time_et: Optional[Union[datetime, str]] = None,
    ) -> Dict[str, Any]:
        """
        Two-Stage EOD De-Levering schedule under FINRA Rule 4210(f)(8)(A) (#91):
        - Stage 1 (15:50:00 ET Primary Regular-Session Closing Auction Trim):
          Eliminates overnight margin excess prior to 16:00 ET Reg T snapshot during peak liquidity.
        - Stage 2 (19:45:00 ET Extended-Hours Hard Backstop):
          Emergency sweep prior to 20:00 ET close.
        Prioritizes trimming 3x LETFs (90% overnight requirement) first to free maximum margin.
        """
        # Determine current stage
        stage_name = "STAGE_1_1550_ET_REGULAR_CLOSE_TRIM"
        if current_time_et is not None:
            hour = 15
            if isinstance(current_time_et, datetime):
                hour = current_time_et.hour
            elif isinstance(current_time_et, str):
                try:
                    hour = int(current_time_et.split(":")[0])
                except Exception:
                    hour = 15
            if hour >= 17:
                stage_name = "STAGE_2_1945_ET_EXTENDED_HOURS_BACKSTOP"

        delta, total_val, allowable_limit = self.calculate_overnight_excess(enforce_letf_house_margin=True)
        if delta <= 0:
            return {
                "stage": stage_name,
                "delta": 0.0,
                "total_position_value": total_val,
                "allowable_limit": allowable_limit,
                "orders": [],
                "action_required": False,
            }

        orders = []
        remaining_reduction_needed = delta

        # Sort positions prioritizing 3x LETFs first, then by market value descending
        sorted_positions = sorted(
            self.positions.items(),
            key=lambda item: (item[0].upper() in LETF_3X_SYMBOLS, item[1].get("quantity", 0) * item[1].get("current_price", 0.0)),
            reverse=True,
        )

        for sym, pos in sorted_positions:
            qty = pos.get("quantity", 0)
            px = pos.get("current_price", 0.0)
            if qty <= 0 or px <= 0:
                continue

            pos_val = qty * px
            req_over = self.get_asset_margin_requirements(sym, True)[1]
            effective_reduction_needed = remaining_reduction_needed / req_over if req_over > 0 else remaining_reduction_needed

            if pos_val <= effective_reduction_needed:
                shares_to_sell = qty
                notional_reduction = pos_val
            else:
                shares_to_sell = math.ceil(effective_reduction_needed / px)
                shares_to_sell = min(shares_to_sell, qty)
                notional_reduction = shares_to_sell * px

            margin_released = notional_reduction * req_over
            orders.append({
                "symbol": sym,
                "shares_to_sell": shares_to_sell,
                "estimated_price": px,
                "notional_reduction": round(notional_reduction, 2),
                "margin_released": round(margin_released, 2),
                "overnight_margin_requirement": req_over,
                "stage": stage_name,
                "reason": f"TWO_STAGE_EOD_DELEVERAGING_{stage_name}",
            })

            remaining_reduction_needed -= margin_released
            if remaining_reduction_needed <= 0:
                break

        return {
            "stage": stage_name,
            "delta": round(delta, 2),
            "total_position_value": round(total_val, 2),
            "allowable_limit": round(allowable_limit, 2),
            "orders": orders,
            "action_required": True,
        }

    def compute_effective_hwm_decay(
        self,
        r_core: Union[float, Dict[str, float], List[float], Tuple[float, ...]],
        w_sat: float = 0.15,
        w_base: float = 0.15,
        lambda_0: float = 0.03,
        kappa_core: float = 0.5,
        w_sweep: Optional[Dict[str, float]] = None,
    ) -> Tuple[float, float]:
        """
        Computes the Strategy D core-accelerated recovery decay rate (#98):
            lambda_hwm^eff(t) = lambda_0 + kappa_core * max(0, w_sweep^T * r_core(t)) * (W_swept(t) / w_base)
        Returns (lambda_hwm_eff, weighted_r_core).
        """
        sweep_vec = w_sweep if w_sweep is not None else self.router.routing_sweep_vector
        equity_weights = {
            k.upper(): float(v)
            for k, v in sweep_vec.items()
            if k.upper() not in ("CASH", "CASH_BUFFER", CASH_SWEEP_VEHICLE)
        }
        weight_sum = sum(equity_weights.values())

        if isinstance(r_core, dict):
            weighted_r_core = sum(
                equity_weights.get(sym.upper(), 0.0) * float(ret)
                for sym, ret in r_core.items()
            )
        elif isinstance(r_core, (list, tuple)):
            ordered_weights = list(equity_weights.values())
            weighted_r_core = sum(
                w * float(r) for w, r in zip(ordered_weights, r_core)
            )
        else:
            weighted_r_core = float(r_core) * (weight_sum if weight_sum > 0.0 else 1.0)

        w_swept = max(0.0, float(w_base) - float(w_sat))
        swept_ratio = (w_swept / float(w_base)) if float(w_base) > 0.0 else 1.0
        eff_decay = float(lambda_0) + float(kappa_core) * max(0.0, weighted_r_core) * swept_ratio
        return round(eff_decay, 6), weighted_r_core

    def update_core_accelerated_drawdown(
        self,
        current_satellite_equity: float,
        r_core: Union[float, Dict[str, float], List[float], Tuple[float, ...]],
        w_sat: float = 0.15,
        w_base: float = 0.15,
        lambda_0: float = 0.03,
        kappa_core: float = 0.5,
        dt_seconds: float = 1.0,
        w_sweep: Optional[Dict[str, float]] = None,
    ) -> float:
        """
        Strategy D: Core-Accelerated Sleeve Drawdown Damper (#91 / #98).
        Swept satellite capital sheltered in the multi-asset 1x Robo-Core basket earns
        w_sweep^T * r_core(t), accelerating the decay of the satellite high-water mark E_sat,peak(t):
            lambda_hwm^eff(t) = lambda_0 + kappa_core * max(0, w_sweep^T * r_core(t)) * (W_swept(t) / w_base)
        Returns the continuous drawdown damper D_sat in [0.0, 1.0].
        """
        curr_eq = max(0.0, float(current_satellite_equity))
        if curr_eq > self.satellite_peak_equity:
            self.satellite_peak_equity = curr_eq
            self.cumulative_core_heal = 0.0
            self.last_lambda_hwm_eff = float(lambda_0)
            return 1.0

        eff_decay_rate, weighted_r_core = self.compute_effective_hwm_decay(
            r_core=r_core,
            w_sat=w_sat,
            w_base=w_base,
            lambda_0=lambda_0,
            kappa_core=kappa_core,
            w_sweep=w_sweep,
        )
        self.last_lambda_hwm_eff = eff_decay_rate
        w_swept = max(0.0, float(w_base) - float(w_sat))

        # High-water-mark decay: dE_peak / dt = -lambda_eff * (E_peak - E_curr)
        decay_amount = eff_decay_rate * (self.satellite_peak_equity - curr_eq) * dt_seconds
        self.satellite_peak_equity = max(curr_eq, self.satellite_peak_equity - decay_amount)

        # Cumulative beta healing credit from swept capital
        if weighted_r_core > 0.0 and w_swept > 0.0:
            core_heal = w_swept * curr_eq * weighted_r_core * dt_seconds
            self.cumulative_core_heal += core_heal

        effective_equity = curr_eq + self.cumulative_core_heal
        if self.satellite_peak_equity > 0:
            drawdown = max(0.0, (self.satellite_peak_equity - effective_equity) / self.satellite_peak_equity)
        else:
            drawdown = 0.0

        # Tiered / continuous damper D_sat
        if drawdown < 0.05:
            d_sat = 1.0
        elif drawdown < 0.10:
            # Linear scaling from 1.0 down to 0.5 between 5% and 10% drawdown
            d_sat = 1.0 - ((drawdown - 0.05) / 0.05) * 0.5
        else:
            # Further throttling down to 0.0 for drawdowns >= 15%
            d_sat = max(0.0, 0.5 - ((drawdown - 0.10) / 0.05) * 0.5)

        return round(d_sat, 4)

    def plan_dual_sleeve_reallocation(
        self,
        nlv: Optional[float] = None,
        satellite_positions: Optional[Dict[str, Any]] = None,
        target_satellite_weight: Optional[float] = None,
        core_positions: Optional[Dict[str, Any]] = None,
        core_symbol: Optional[str] = None,
        core_price: float = 585.0,
        core_prices: Optional[Dict[str, float]] = None,
        routing_sweep_vector: Optional[Dict[str, float]] = None,
        satellite_sub_weights: Optional[Dict[str, float]] = None,
        satellite_prices: Optional[Dict[str, float]] = None,
        tau_band: Optional[float] = None,
        trend_gate_active: bool = True,
    ) -> DualSleeveSwapPlan:
        """Delegates to DualSleeveRouter to generate paired atomic swap orders referencing live broker NLV (#111)."""
        eff_nlv = float(nlv) if nlv is not None else float(self.account_equity)
        if target_satellite_weight is None:
            target_satellite_weight = self.hmm.calculate_target_satellite_weight()
        if satellite_positions is None:
            satellite_positions = {
                s: p for s, p in self.positions.items()
                if s.upper() in LETF_3X_SYMBOLS or p.get("sleeve") == "SATELLITE"
            }
        if core_positions is None and self.positions:
            core_positions = {
                s: p for s, p in self.positions.items()
                if s.upper() not in LETF_3X_SYMBOLS and p.get("sleeve") != "SATELLITE"
            }
        return self.router.plan_dual_sleeve_reallocation(
            nlv=eff_nlv,
            satellite_positions=satellite_positions,
            target_satellite_weight=target_satellite_weight,
            core_positions=core_positions,
            core_symbol=core_symbol,
            core_price=core_price,
            core_prices=core_prices,
            routing_sweep_vector=routing_sweep_vector,
            satellite_sub_weights=satellite_sub_weights,
            satellite_prices=satellite_prices,
            tau_band=tau_band,
            trend_gate_active=trend_gate_active,
            hmm=self.hmm,
        )

    def apply_swap_plan(
        self,
        plan: DualSleeveSwapPlan,
        leg2_succeeded: bool = True,
    ) -> Dict[str, float]:
        """
        Applies an executed DualSleeveSwapPlan to the internal position book and SWVXX cash balance,
        preserving exact dollar conservation:
            abs((core_mv + sat_mv + swvxx_cash) - total_nlv) < 0.01
        """
        if not plan.orders:
            return self.get_sleeve_balances()

        for order in plan.orders:
            sym = order.symbol.upper()
            if order.leg_id == 2 and not leg2_succeeded:
                continue
            pos = self.positions.get(sym, {
                "quantity": 0,
                "entry_price": order.price,
                "current_price": order.price,
                "market_value": 0.0,
                "sleeve": order.sleeve,
            })
            if order.side.upper() == "SELL":
                sell_q = min(int(pos.get("quantity", 0)), int(order.shares))
                new_q = max(0, int(pos.get("quantity", 0)) - sell_q)
                proceeds = sell_q * float(order.price)
                self.swvxx_cash_balance += proceeds
                self.cash_balance = self.swvxx_cash_balance
                if new_q <= 0:
                    self.positions.pop(sym, None)
                else:
                    pos["quantity"] = new_q
                    pos["current_price"] = float(order.price)
                    pos["market_value"] = new_q * float(order.price)
                    self.positions[sym] = pos
            elif order.side.upper() == "BUY":
                buy_q = int(order.shares)
                cost = buy_q * float(order.price)
                new_q = int(pos.get("quantity", 0)) + buy_q
                self.swvxx_cash_balance = max(0.0, self.swvxx_cash_balance - cost)
                self.cash_balance = self.swvxx_cash_balance
                pos["quantity"] = new_q
                pos["current_price"] = float(order.price)
                pos["market_value"] = new_q * float(order.price)
                pos["sleeve"] = order.sleeve
                self.positions[sym] = pos

        self.recalculate_margin_buying_power()
        return self.get_sleeve_balances()

    def update_macro_hmm(
        self,
        macro_state: Union[Any, Tuple[float, float, float]],
        dt_seconds: float = 1.0,
    ) -> Tuple[float, float, float]:
        """Updates continuous 3-state HMM Meta-Governor."""
        return self.hmm.update(macro_state, dt_seconds=dt_seconds)

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
        intraday_fraction: float = 15.0 / 390.0,
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

        # Scale volatility to intraday resolution using 15-minute horizon scalar:
        # scalar = math.sqrt((15.0 / 390.0) / 252.0) (~0.01235).
        # Daily volatility (< 0.20) is annualized so effective_stop_dist yields ~1.2% to 2.2% for TQQQ, TNA, SOXL.
        vol = effective_vol * math.sqrt(252.0) if effective_vol < 0.20 else effective_vol
        fraction = float(dt_days) if dt_days is not None else float(intraday_fraction)
        time_factor = math.sqrt(max(fraction, 1e-8) / 252.0)
        distance = high_water_mark * (float(k_stop) * vol * time_factor)
        floor_distance = high_water_mark * float(min_stop_distance_pct)
        return max(distance, floor_distance, 0.01)  # minimum 0.5% buffer, bounded at 1 tick ($0.01)

    def evaluate_trailing_stop(
        self,
        position: Any,
        current_price: float,
        yz_vol: float,
        k_stop: float = 2.0,
        dt_days: Optional[float] = None,
        intraday_fraction: float = 15.0 / 390.0,
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
