"""
strategies/regime_governor.py
=============================
Workstream 1 (#123): Asymmetric HMM Regime Classification & Tail-Risk Governors.

Implements `AsymmetricHMMGovernor`:
1. Ergodic, asymmetric 3x3 transition matrix T_asym enforcing rapid de-risking into
   high-volatility regimes (v_crash >> v_recovery) and conservative confirmation
   before re-accumulating risk:
       T_00 = 0.9850, T_01 = 0.0125, T_02 = 0.0025
       T_10 = 0.0030, T_11 = 0.9880, T_12 = 0.0090
       T_20 = 0.0005, T_21 = 0.0035, T_22 = 0.9960
2. 4-Dimensional Continuous Observation Vector:
       M(t) = [Z_s(t), S_nlp(t), CS_hybrid(t), v_15m^tech(t)]^T
   incorporating rolling 15-minute technology equity velocity v_15m^tech(t) = Delta P_QQQ / P_QQQ
   alongside hybrid high-yield credit spread proxies (CS_hybrid).
3. Deterministic Tail-Risk Override Circuit Breaker:
       C_shock(t) = (Delta CS_1h >= 12 bps or Z_CS >= 3.0)
                    and (v_15m^SPY <= -1.25% or v_15m^QQQ <= -1.75%)
4. Hysteresis Filtering with Joint-Dislocation Bypass:
   - Enforces a continuous 30-tick (Delta t = 1.0s) confirmation horizon for isolated
     credit spread anomalies to filter transient noise during scheduled macroeconomic
     data releases (CPI/FOMC).
   - Bypasses isolated credit hysteresis when accompanied by an equity velocity
     dislocation (v_15m <= -1.5% and Delta CS >= 12 bps, or C_shock(t)).
5. Dynamic Satellite Target Weighting:
       W_sat^*(t) = 0.00 if pi_Shock >= 0.60 or Dominant Regime == "Macro Shock"
                    else min(0.35, pi_G * 0.25 + pi_U * 0.10)
6. Atomic SQLite Telemetry Audit Trail in `state/trading_journal.db`.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from core.paths import TRADING_JOURNAL_DB_PATH

logger = logging.getLogger("regime_governor")


def _normalize_velocity(val: Optional[float]) -> float:
    """
    Normalize 15-minute equity velocity into decimal return units.
    Accepts either decimal form (e.g. -0.015 for -1.5%) or percentage-point form
    (e.g. -1.5 for -1.5%). Values with |val| > 0.20 are interpreted as percentages.
    """
    if val is None:
        return 0.0
    v = float(val)
    if abs(v) > 0.20:
        return v / 100.0
    return v


@dataclass(frozen=True)
class ObservationVector4D:
    """
    4-Dimensional Continuous Observation Vector M(t):
        M(t) = [Z_s(t), S_nlp(t), CS_hybrid(t), v_15m^tech(t)]^T
    """

    macro_surprise_z: float = 0.0
    nlp_sentiment: float = 0.0
    credit_spread_hybrid_bps: float = 317.6
    tech_velocity_15m: float = 0.0
    delta_cs_1h_bps: float = 0.0
    credit_spread_z: float = 0.0
    spy_velocity_15m: Optional[float] = None

    def to_vector(self) -> Tuple[float, float, float, float]:
        return (
            float(self.macro_surprise_z),
            float(self.nlp_sentiment),
            float(self.credit_spread_hybrid_bps),
            _normalize_velocity(self.tech_velocity_15m),
        )


@dataclass(frozen=True)
class SizingGovernorTranslation:
    """Output of translating the filtered HMM state into dual-sleeve sizing parameters."""

    pi_goldilocks: float
    pi_uncertainty: float
    pi_shock: float
    dominant_regime: str
    target_satellite_weight: float
    tail_risk_override_active: bool
    hysteresis_ticks: int


class AsymmetricHMMGovernor:
    """
    4D Asymmetric Hidden Markov Model Regime Classifier & Tail-Risk Governor (#123 Workstream 1).
    Separates state prediction, 4D Gaussian likelihood evaluation, and sizing governor translation.
    """

    REGIMES: Tuple[str, str, str] = ("Goldilocks", "Uncertainty", "Macro Shock")
    REGIME_ORDER: Tuple[str, str, str] = ("goldilocks", "uncertainty", "shock")

    # Ergodic, asymmetric transition matrix T_asym (v_crash >> v_recovery)
    ASYMMETRIC_TRANSITION_MATRIX_3X3: List[List[float]] = [
        [0.9850, 0.0125, 0.0025],
        [0.0030, 0.9880, 0.0090],
        [0.0005, 0.0035, 0.9960],
    ]

    # 4D Gaussian emission parameters: M(t) = [Z_s, S_nlp, CS_hybrid, v_15m_tech]^T
    DEFAULT_EMISSION_PARAMETERS_4D: Dict[str, Dict[str, Dict[str, float]]] = {
        "goldilocks": {
            "mu": {
                "Macro_Surprise_Z": 0.9946,
                "Macro_Sentiment": 0.4803,
                "Macro_HYSpread_Bps": 317.6,
                "Tech_Velocity_15m": 0.0015,
            },
            "sigma": {
                "Macro_Surprise_Z": 0.2498,
                "Macro_Sentiment": 0.1064,
                "Macro_HYSpread_Bps": 18.8,
                "Tech_Velocity_15m": 0.0040,
            },
        },
        "uncertainty": {
            "mu": {
                "Macro_Surprise_Z": -0.2989,
                "Macro_Sentiment": -0.0600,
                "Macro_HYSpread_Bps": 360.0,
                "Tech_Velocity_15m": -0.0125,
            },
            "sigma": {
                "Macro_Surprise_Z": 0.2500,
                "Macro_Sentiment": 0.1200,
                "Macro_HYSpread_Bps": 22.0,
                "Tech_Velocity_15m": 0.0045,
            },
        },
        "shock": {
            "mu": {
                "Macro_Surprise_Z": -1.2430,
                "Macro_Sentiment": -0.4720,
                "Macro_HYSpread_Bps": 440.0,
                "Tech_Velocity_15m": -0.0225,
            },
            "sigma": {
                "Macro_Surprise_Z": 0.3000,
                "Macro_Sentiment": 0.1500,
                "Macro_HYSpread_Bps": 30.0,
                "Tech_Velocity_15m": 0.0055,
            },
        },
    }

    CREDIT_HYSTERESIS_TICKS: int = 30
    BASELINE_CREDIT_SPREAD_BPS: float = 317.6

    def __init__(
        self,
        initial_pi: Optional[Sequence[float]] = None,
        transition_matrix: Optional[List[List[float]]] = None,
        emission_vectors: Optional[Dict[str, Dict[str, Dict[str, float]]]] = None,
        journal_db_path: Optional[Union[str, Path]] = None,
        hysteresis_horizon_ticks: int = CREDIT_HYSTERESIS_TICKS,
    ) -> None:
        self._lock = threading.RLock()
        if initial_pi is not None and len(initial_pi) == 3:
            total = sum(float(x) for x in initial_pi)
            self.pi: List[float] = (
                [float(x) / total for x in initial_pi] if total > 0 else [1.0, 0.0, 0.0]
            )
        else:
            self.pi = [1.0, 0.0, 0.0]

        self.transition_matrix: List[List[float]] = (
            [list(row) for row in transition_matrix]
            if transition_matrix is not None
            else [list(row) for row in self.ASYMMETRIC_TRANSITION_MATRIX_3X3]
        )
        self.emission_vectors: Dict[str, Dict[str, Dict[str, float]]] = (
            emission_vectors
            if emission_vectors is not None
            else json.loads(json.dumps(self.DEFAULT_EMISSION_PARAMETERS_4D))
        )

        self.hysteresis_horizon_ticks: int = max(1, int(hysteresis_horizon_ticks))
        self._credit_anomaly_ticks: int = 0
        self._last_confirmed_cs_bps: float = self.BASELINE_CREDIT_SPREAD_BPS
        self.tail_risk_override_active: bool = False
        self._last_dominant_regime: str = self.get_dominant_regime()
        self.journal_db_path: Path = Path(journal_db_path) if journal_db_path is not None else TRADING_JOURNAL_DB_PATH
        self._init_journal_db()

    # ------------------------------------------------------------------
    # SQLite Audit Trail (state/trading_journal.db)
    # ------------------------------------------------------------------

    def _init_journal_db(self) -> None:
        try:
            self.journal_db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(str(self.journal_db_path), timeout=5.0) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS regime_transitions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp_utc TEXT NOT NULL,
                        previous_regime TEXT NOT NULL,
                        new_regime TEXT NOT NULL,
                        pi_goldilocks REAL NOT NULL,
                        pi_uncertainty REAL NOT NULL,
                        pi_shock REAL NOT NULL,
                        target_satellite_weight REAL NOT NULL,
                        tail_risk_override INTEGER NOT NULL,
                        hysteresis_ticks INTEGER NOT NULL,
                        observation_json TEXT NOT NULL
                    )
                    """
                )
                conn.commit()
        except Exception as exc:
            logger.warning("AsymmetricHMMGovernor: failed to initialize journal DB at %s: %s", self.journal_db_path, exc)

    def _record_transition_telemetry(
        self,
        previous_regime: str,
        new_regime: str,
        obs_payload: Dict[str, Any],
    ) -> None:
        """Atomically persist regime transition into state/trading_journal.db."""
        try:
            self.journal_db_path.parent.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).isoformat()
            w_sat = self.calculate_target_satellite_weight()
            with sqlite3.connect(str(self.journal_db_path), timeout=5.0) as conn:
                conn.execute(
                    """
                    INSERT INTO regime_transitions (
                        timestamp_utc,
                        previous_regime,
                        new_regime,
                        pi_goldilocks,
                        pi_uncertainty,
                        pi_shock,
                        target_satellite_weight,
                        tail_risk_override,
                        hysteresis_ticks,
                        observation_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ts,
                        previous_regime,
                        new_regime,
                        round(self.pi_goldilocks, 6),
                        round(self.pi_uncertainty, 6),
                        round(self.pi_shock, 6),
                        round(w_sat, 4),
                        1 if self.tail_risk_override_active else 0,
                        int(self._credit_anomaly_ticks),
                        json.dumps(obs_payload),
                    ),
                )
                conn.commit()
        except Exception as exc:
            logger.warning("AsymmetricHMMGovernor: failed to log transition to trading_journal.db: %s", exc)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def pi_goldilocks(self) -> float:
        return float(self.pi[0])

    @property
    def pi_uncertainty(self) -> float:
        return float(self.pi[1])

    @property
    def pi_shock(self) -> float:
        return float(self.pi[2])

    def get_dominant_regime(self) -> str:
        idx = max(range(3), key=lambda i: self.pi[i])
        return self.REGIMES[idx]

    # ------------------------------------------------------------------
    # Step 1: State Prediction (Chapman-Kolmogorov Prior)
    # ------------------------------------------------------------------

    def predict_state(self, pi_prev: Optional[Sequence[float]] = None) -> List[float]:
        """
        Compute predicted state distribution prior to observation update:
            pi_prior_j(t) = sum_i pi_i(t-1) * T_{i,j}
        """
        with self._lock:
            vec = list(pi_prev) if pi_prev is not None else list(self.pi)
            prior = [0.0, 0.0, 0.0]
            for j in range(3):
                prior[j] = sum(vec[i] * self.transition_matrix[i][j] for i in range(3))
            total = sum(prior)
            if total > 0:
                prior = [p / total for p in prior]
            return prior

    # ------------------------------------------------------------------
    # Step 2: 4D Gaussian Emission Likelihood Evaluation
    # ------------------------------------------------------------------

    @staticmethod
    def _gaussian_pdf(x: float, mu: float, sigma: float) -> float:
        eff_sigma = max(1e-6, abs(float(sigma)))
        z = (float(x) - float(mu)) / eff_sigma
        exponent = max(-60.0, min(60.0, -0.5 * z * z))
        return (1.0 / (eff_sigma * math.sqrt(2.0 * math.pi))) * math.exp(exponent)

    def evaluate_gaussian_likelihood(
        self,
        observation: Union[ObservationVector4D, Sequence[float], Dict[str, float]],
    ) -> List[float]:
        """
        Evaluate 4D Gaussian emission likelihoods L_k(M(t)) for k in {Goldilocks, Uncertainty, Shock}
        over M(t) = [Z_s(t), S_nlp(t), CS_hybrid(t), v_15m^tech(t)]^T.
        """
        if isinstance(observation, ObservationVector4D):
            z_s, s_nlp, cs_bps, v_tech = observation.to_vector()
        elif isinstance(observation, dict):
            z_s = float(observation.get("Macro_Surprise_Z", observation.get("z_s", 0.0)))
            s_nlp = float(observation.get("Macro_Sentiment", observation.get("s_nlp", 0.0)))
            cs_bps = float(observation.get("Macro_HYSpread_Bps", observation.get("cs_hybrid", 317.6)))
            v_tech = _normalize_velocity(
                observation.get("Tech_Velocity_15m", observation.get("v_15m_tech", 0.0))
            )
        else:
            seq = list(observation)
            z_s = float(seq[0]) if len(seq) > 0 else 0.0
            s_nlp = float(seq[1]) if len(seq) > 1 else 0.0
            cs_bps = float(seq[2]) if len(seq) > 2 else 317.6
            v_tech = _normalize_velocity(seq[3] if len(seq) > 3 else 0.0)

        obs_map = {
            "Macro_Surprise_Z": z_s,
            "Macro_Sentiment": s_nlp,
            "Macro_HYSpread_Bps": cs_bps,
            "Tech_Velocity_15m": v_tech,
        }

        likelihoods: List[float] = []
        for r_key in self.REGIME_ORDER:
            params = self.emission_vectors.get(r_key, {})
            mu_map = params.get("mu", {})
            sig_map = params.get("sigma", {})
            prod = 1.0
            for feat_name, x_val in obs_map.items():
                if feat_name in mu_map and feat_name in sig_map:
                    prod *= self._gaussian_pdf(x_val, mu_map[feat_name], sig_map[feat_name])
            likelihoods.append(max(prod, 1e-300))
        return likelihoods

    # ------------------------------------------------------------------
    # Step 3: Tail-Risk Circuit Breaker & Credit Hysteresis Filter
    # ------------------------------------------------------------------

    @staticmethod
    def evaluate_tail_risk_circuit_breaker(
        *,
        delta_cs_1h_bps: float = 0.0,
        credit_spread_z: float = 0.0,
        spy_velocity_15m: Optional[float] = None,
        qqq_velocity_15m: Optional[float] = None,
    ) -> bool:
        """
        Deterministic Tail-Risk Override Circuit Breaker:
            C_shock(t) = (Delta CS_1h >= 12 bps or Z_CS >= 3.0)
                         and (v_15m^SPY <= -1.25% or v_15m^QQQ <= -1.75%)
        """
        credit_trigger = (float(delta_cs_1h_bps) >= 12.0) or (float(credit_spread_z) >= 3.0)
        if not credit_trigger:
            return False

        v_spy = _normalize_velocity(spy_velocity_15m) if spy_velocity_15m is not None else 0.0
        v_qqq = _normalize_velocity(qqq_velocity_15m) if qqq_velocity_15m is not None else 0.0
        equity_trigger = (
            (spy_velocity_15m is not None and v_spy <= -0.0125)
            or (qqq_velocity_15m is not None and v_qqq <= -0.0175)
        )
        return bool(credit_trigger and equity_trigger)

    def _apply_credit_hysteresis(
        self,
        raw_cs_bps: float,
        delta_cs_1h_bps: float,
        v_tech_norm: float,
    ) -> Tuple[float, bool]:
        """
        Enforces a continuous 30-tick (Delta t = 1.0s) confirmation horizon for isolated
        credit spread anomalies to filter transient noise during CPI/FOMC releases.
        Joint equity + credit dislocations (v_15m <= -1.5% and Delta CS >= 12 bps)
        bypass isolated credit hysteresis for immediate de-risking.

        Returns `(effective_cs_bps, joint_dislocation_active)`.
        """
        implied_delta_cs = max(float(delta_cs_1h_bps), float(raw_cs_bps) - self.BASELINE_CREDIT_SPREAD_BPS)
        is_credit_anomaly = implied_delta_cs >= 12.0 or float(raw_cs_bps) >= 345.0
        is_joint_dislocation = is_credit_anomaly and (v_tech_norm <= -0.015)

        if is_joint_dislocation:
            self._credit_anomaly_ticks += 1
            eff_cs = max(float(raw_cs_bps), self.BASELINE_CREDIT_SPREAD_BPS + max(12.0, implied_delta_cs * 3.5))
            self._last_confirmed_cs_bps = eff_cs
            return eff_cs, True

        if is_credit_anomaly:
            self._credit_anomaly_ticks += 1
            if self._credit_anomaly_ticks >= self.hysteresis_horizon_ticks:
                self._last_confirmed_cs_bps = float(raw_cs_bps)
                return float(raw_cs_bps), False
            # Isolated credit spike under 30 ticks with benign equity velocity -> hold confirmed baseline
            return self._last_confirmed_cs_bps, False

        # No credit anomaly -> reset tick counter and update confirmed baseline
        self._credit_anomaly_ticks = 0
        self._last_confirmed_cs_bps = float(raw_cs_bps)
        return float(raw_cs_bps), False

    # ------------------------------------------------------------------
    # Step 4: Sizing Governor Translation & Online Update
    # ------------------------------------------------------------------

    def calculate_target_satellite_weight(
        self,
        regime_targets: Optional[Dict[str, Any]] = None,
    ) -> float:
        """
        Dynamic satellite target weighting (#123 Workstream 1):
            W_sat^*(t) = 0.00 if pi_Shock >= 0.60 or Dominant Regime == "Macro Shock"
                         else min(0.35, pi_G * 0.25 + pi_U * 0.10)
        """
        with self._lock:
            if self.tail_risk_override_active or self.pi_shock >= 0.60 or self.get_dominant_regime() == "Macro Shock":
                return 0.0

            g_target = 0.25
            u_target = 0.10
            if regime_targets:
                g_cfg = regime_targets.get("Goldilocks", {})
                u_cfg = regime_targets.get("Uncertainty", {})
                if isinstance(g_cfg, dict) and "target" in g_cfg:
                    g_target = float(g_cfg["target"])
                if isinstance(u_cfg, dict) and "target" in u_cfg:
                    u_target = float(u_cfg["target"])

            w_sat = min(0.35, (self.pi_goldilocks * g_target) + (self.pi_uncertainty * u_target))
            return round(max(0.0, w_sat), 4)

    def translate_sizing_governor(
        self,
        regime_targets: Optional[Dict[str, Any]] = None,
    ) -> SizingGovernorTranslation:
        """Translate current posterior state vector into a structured SizingGovernorTranslation."""
        with self._lock:
            return SizingGovernorTranslation(
                pi_goldilocks=round(self.pi_goldilocks, 6),
                pi_uncertainty=round(self.pi_uncertainty, 6),
                pi_shock=round(self.pi_shock, 6),
                dominant_regime=self.get_dominant_regime(),
                target_satellite_weight=self.calculate_target_satellite_weight(regime_targets=regime_targets),
                tail_risk_override_active=self.tail_risk_override_active,
                hysteresis_ticks=self._credit_anomaly_ticks,
            )

    def update(
        self,
        macro_surprise_z: float = 0.0,
        macro_sentiment: float = 0.0,
        macro_hy_spread_bps: float = 317.6,
        tech_velocity_15m: float = 0.0,
        *,
        delta_cs_1h_bps: float = 0.0,
        credit_spread_z: float = 0.0,
        spy_velocity_15m: Optional[float] = None,
    ) -> List[float]:
        """
        Execute one online Bayesian filter cycle on M(t) = [Z_s, S_nlp, CS_hybrid, v_15m^tech]^T:
        1. Evaluate deterministic tail-risk circuit breaker C_shock(t).
        2. Apply 30-tick credit hysteresis filter (with joint-dislocation bypass).
        3. Compute prior via asymmetric transition matrix T_asym.
        4. Weight prior by 4D Gaussian likelihoods and normalize posterior pi(t).
        5. Persist atomic telemetry to `state/trading_journal.db` on regime transitions.
        """
        with self._lock:
            prev_regime = self.get_dominant_regime()
            v_qqq_norm = _normalize_velocity(tech_velocity_15m)
            v_spy_norm = _normalize_velocity(spy_velocity_15m) if spy_velocity_15m is not None else None

            implied_delta_cs = max(
                float(delta_cs_1h_bps),
                float(macro_hy_spread_bps) - self.BASELINE_CREDIT_SPREAD_BPS,
            )

            # 1. Deterministic Tail-Risk Override C_shock(t)
            c_shock = self.evaluate_tail_risk_circuit_breaker(
                delta_cs_1h_bps=implied_delta_cs,
                credit_spread_z=credit_spread_z,
                spy_velocity_15m=v_spy_norm,
                qqq_velocity_15m=v_qqq_norm,
            )
            if c_shock:
                self.tail_risk_override_active = True
                self.pi = [0.0, 0.05, 0.95]
                new_regime = self.get_dominant_regime()
                if new_regime != prev_regime:
                    self._record_transition_telemetry(
                        previous_regime=prev_regime,
                        new_regime=new_regime,
                        obs_payload={
                            "macro_surprise_z": float(macro_surprise_z),
                            "macro_sentiment": float(macro_sentiment),
                            "macro_hy_spread_bps": float(macro_hy_spread_bps),
                            "tech_velocity_15m": v_qqq_norm,
                            "delta_cs_1h_bps": implied_delta_cs,
                            "credit_spread_z": float(credit_spread_z),
                            "spy_velocity_15m": v_spy_norm,
                            "trigger": "TAIL_RISK_CIRCUIT_BREAKER",
                        },
                    )
                    self._last_dominant_regime = new_regime
                return list(self.pi)

            self.tail_risk_override_active = False

            # 2. Credit Hysteresis Filtering (30-tick isolated vs joint-dislocation bypass)
            eff_cs_bps, is_joint_dislocation = self._apply_credit_hysteresis(
                raw_cs_bps=float(macro_hy_spread_bps),
                delta_cs_1h_bps=implied_delta_cs,
                v_tech_norm=v_qqq_norm,
            )

            # When joint dislocation is active, align Z_s and S_nlp with the macro stress signal
            eff_z_s = min(float(macro_surprise_z), -0.45) if is_joint_dislocation else float(macro_surprise_z)
            eff_s_nlp = min(float(macro_sentiment), -0.10) if is_joint_dislocation else float(macro_sentiment)

            # 3. State Prediction Step
            prior = self.predict_state(self.pi)

            # 4. 4D Gaussian Emission Likelihood Evaluation
            obs = ObservationVector4D(
                macro_surprise_z=eff_z_s,
                nlp_sentiment=eff_s_nlp,
                credit_spread_hybrid_bps=eff_cs_bps,
                tech_velocity_15m=v_qqq_norm,
                delta_cs_1h_bps=implied_delta_cs,
                credit_spread_z=float(credit_spread_z),
                spy_velocity_15m=v_spy_norm,
            )
            likelihoods = self.evaluate_gaussian_likelihood(obs)

            unnormalized = [prior[k] * likelihoods[k] for k in range(3)]
            total = sum(unnormalized)
            if total > 0.0:
                self.pi = [u / total for u in unnormalized]
            else:
                self.pi = prior

            new_regime = self.get_dominant_regime()
            if new_regime != prev_regime:
                self._record_transition_telemetry(
                    previous_regime=prev_regime,
                    new_regime=new_regime,
                    obs_payload={
                        "macro_surprise_z": float(macro_surprise_z),
                        "macro_sentiment": float(macro_sentiment),
                        "macro_hy_spread_bps": float(macro_hy_spread_bps),
                        "effective_cs_bps": eff_cs_bps,
                        "tech_velocity_15m": v_qqq_norm,
                        "delta_cs_1h_bps": implied_delta_cs,
                        "joint_dislocation": is_joint_dislocation,
                    },
                )
                self._last_dominant_regime = new_regime

            return list(self.pi)

    def fetch_recent_transitions(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Read recent regime transitions from state/trading_journal.db for verification/auditing."""
        if not self.journal_db_path.exists():
            return []
        with sqlite3.connect(str(self.journal_db_path), timeout=5.0) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM regime_transitions ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            return [dict(r) for r in rows]
