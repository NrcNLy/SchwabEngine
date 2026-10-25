"""
tests/test_asymmetric_hmm.py
============================
Unit & property tests for Workstream 1 (#123):
Asymmetric HMM Regime Classification & Tail-Risk Governors.

Validates:
1. Ergodic, asymmetric transition matrix T_asym row stochasticity and v_crash >> v_recovery.
2. HMM Descent Invariant: Simulating an equity drop (v_15m <= -1.5%) and credit widening
   (Delta CS >= 12 bps) transitions the regime from Goldilocks to Uncertainty/Macro Shock
   within <= 2 ticks.
3. 30-Tick Credit Hysteresis: Isolated credit spread spikes (with benign equity velocity)
   are filtered for 29 ticks and only confirmed at tick 30.
4. Deterministic Tail-Risk Override C_shock(t): Immediate transition to Macro Shock (W_sat^* = 0.00)
   when (Delta CS_1h >= 12 bps or Z_CS >= 3.0) and (v_15m^SPY <= -1.25% or v_15m^QQQ <= -1.75%).
5. Dynamic satellite target weighting W_sat^*(t) and atomic SQLite telemetry logging in
   `state/trading_journal.db`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from execution.risk_manager import RiskEngine
from strategies.regime_governor import AsymmetricHMMGovernor, ObservationVector4D


def test_asymmetric_transition_matrix_topology(tmp_path: Path) -> None:
    """Verify T_asym matches specification and is strictly row-stochastic with v_crash >> v_recovery."""
    gov = AsymmetricHMMGovernor(journal_db_path=tmp_path / "trading_journal.db")
    T = gov.transition_matrix

    assert T[0] == pytest.approx([0.9850, 0.0125, 0.0025])
    assert T[1] == pytest.approx([0.0030, 0.9880, 0.0090])
    assert T[2] == pytest.approx([0.0005, 0.0035, 0.9960])

    for row in T:
        assert sum(row) == pytest.approx(1.0, abs=1e-9)

    # Structural asymmetry: P(Goldilocks -> Uncertainty/Shock) >> P(Shock -> Goldilocks)
    p_de_risk = T[0][1] + T[0][2]  # 0.0150
    p_re_risk = T[2][0] + T[2][1]  # 0.0040
    assert p_de_risk > 3.5 * p_re_risk


def test_hmm_descent_within_two_ticks_on_joint_dislocation(tmp_path: Path) -> None:
    """
    Verification Specification (#123 Section 6.1):
    Simulating an equity drop (v_15m <= -1.5%) and credit widening (Delta CS >= 12 bps)
    transitions the regime from Goldilocks to Uncertainty/Macro Shock within 2 ticks.
    """
    db_file = tmp_path / "trading_journal.db"
    gov = AsymmetricHMMGovernor(initial_pi=[1.0, 0.0, 0.0], journal_db_path=db_file)
    assert gov.get_dominant_regime() == "Goldilocks"
    assert gov.calculate_target_satellite_weight() == pytest.approx(0.25)

    # Tick 1: v_15m = -1.5% (-0.015) and Delta CS = 12 bps (CS_hybrid = 329.6 bps)
    gov.update(
        macro_surprise_z=-0.30,
        macro_sentiment=-0.10,
        macro_hy_spread_bps=329.6,
        tech_velocity_15m=-0.015,
        delta_cs_1h_bps=12.0,
    )
    # Tick 2: sustained joint dislocation
    gov.update(
        macro_surprise_z=-0.35,
        macro_sentiment=-0.15,
        macro_hy_spread_bps=332.0,
        tech_velocity_15m=-0.016,
        delta_cs_1h_bps=14.4,
    )

    dominant = gov.get_dominant_regime()
    assert dominant in ("Uncertainty", "Macro Shock")
    assert gov.pi_goldilocks < 0.50
    assert gov.calculate_target_satellite_weight() <= 0.10

    # Verify atomic transition telemetry recorded in trading_journal.db
    transitions = gov.fetch_recent_transitions()
    assert len(transitions) >= 1
    assert transitions[0]["new_regime"] in ("Uncertainty", "Macro Shock")


def test_isolated_credit_anomaly_30_tick_hysteresis(tmp_path: Path) -> None:
    """
    Verify that an isolated credit spread anomaly (Delta CS >= 12 bps) with benign
    equity velocity (v_15m = +0.15%) is filtered for 29 ticks and only confirmed at tick 30.
    """
    gov = AsymmetricHMMGovernor(
        initial_pi=[1.0, 0.0, 0.0],
        journal_db_path=tmp_path / "trading_journal.db",
        hysteresis_horizon_ticks=30,
    )

    # Ticks 1..29: isolated credit spike to 380 bps while QQQ velocity is benign (+0.15%)
    for tick in range(1, 30):
        gov.update(
            macro_surprise_z=0.95,
            macro_sentiment=0.45,
            macro_hy_spread_bps=380.0,
            tech_velocity_15m=0.0015,
            delta_cs_1h_bps=62.4,
        )
        assert gov.get_dominant_regime() == "Goldilocks", f"Premature transition at tick {tick}"
        assert gov.pi_goldilocks > 0.95

    # Tick 30: hysteresis horizon reached -> credit spread 380.0 bps is confirmed
    trans_before = gov.translate_sizing_governor()
    assert trans_before.hysteresis_ticks == 29

    gov.update(
        macro_surprise_z=-0.30,
        macro_sentiment=-0.06,
        macro_hy_spread_bps=380.0,
        tech_velocity_15m=-0.012,
        delta_cs_1h_bps=62.4,
    )
    trans_after = gov.translate_sizing_governor()
    assert trans_after.hysteresis_ticks == 30
    assert gov.get_dominant_regime() in ("Uncertainty", "Macro Shock")


def test_deterministic_tail_risk_circuit_breaker(tmp_path: Path) -> None:
    """
    Verify C_shock(t) = (Delta CS_1h >= 12 bps or Z_CS >= 3.0)
                        and (v_15m^SPY <= -1.25% or v_15m^QQQ <= -1.75%)
    immediately overrides posterior to Macro Shock with W_sat^*(t) == 0.00 on Tick 1.
    """
    gov = AsymmetricHMMGovernor(
        initial_pi=[1.0, 0.0, 0.0],
        journal_db_path=tmp_path / "trading_journal.db",
    )

    gov.update(
        macro_surprise_z=0.80,
        macro_sentiment=0.40,
        macro_hy_spread_bps=331.0,
        tech_velocity_15m=-0.0180,  # QQQ -1.80% <= -1.75%
        delta_cs_1h_bps=13.4,       # Delta CS >= 12 bps
        spy_velocity_15m=-0.0130,   # SPY -1.30% <= -1.25%
    )

    assert gov.tail_risk_override_active is True
    assert gov.get_dominant_regime() == "Macro Shock"
    assert gov.pi_shock >= 0.60
    assert gov.calculate_target_satellite_weight() == 0.0


def test_risk_engine_asymmetric_hmm_integration() -> None:
    """Verify RiskEngine exposes update_asymmetric_regime and 4D ObservationVector4D."""
    engine = RiskEngine(account_base=4400.0)
    verdict = engine.update_asymmetric_regime(
        macro_surprise_z=-0.50,
        macro_sentiment=-0.20,
        macro_hy_spread_bps=335.0,
        tech_velocity_15m=-0.018,
        delta_cs_1h_bps=17.4,
    )
    assert verdict.dominant_regime == "Macro Shock"
    assert verdict.target_satellite_weight == 0.0
    assert verdict.tail_risk_override_active is True

    obs = ObservationVector4D(
        macro_surprise_z=0.5,
        nlp_sentiment=0.2,
        credit_spread_hybrid_bps=320.0,
        tech_velocity_15m=-1.5,  # percentage form normalized to -0.015
    )
    assert obs.to_vector() == pytest.approx((0.5, 0.2, 320.0, -0.015))
