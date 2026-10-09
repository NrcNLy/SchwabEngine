"""
tests/test_governor_schema.py
=============================
Unit tests for the enhanced Pydantic V2 macro schema, platform invariants,
fallback generator, and payload parser.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from macro.governor_schema import (
    MacroRegimeEnum,
    SectorDriftMultipliers,
    MacroRegimeResponse,
    create_safe_degraded_fallback,
    parse_and_validate_macro_payload,
)


def test_macro_regime_enum_values():
    assert MacroRegimeEnum.REGIME_A == "REGIME_A"
    assert MacroRegimeEnum.REGIME_B == "REGIME_B"
    assert MacroRegimeEnum.REGIME_C == "REGIME_C"
    assert MacroRegimeEnum.REGIME_D == "REGIME_D"


def test_sector_drift_multipliers_bounds():
    # Valid bounds [0.0, 2.0]
    m = SectorDriftMultipliers(tech_beta=1.5, small_cap_credit=0.8, energy_beta=0.0)
    assert m.tech_beta == 1.5
    assert m.small_cap_credit == 0.8
    assert m.energy_beta == 0.0

    # Upper bound error
    with pytest.raises(ValidationError):
        SectorDriftMultipliers(tech_beta=2.1)

    # Lower bound error
    with pytest.raises(ValidationError):
        SectorDriftMultipliers(tech_beta=-0.1)


def test_invariant_emergency_flatten():
    """emergency_flatten == True -> hard_lockout_active = True and macro_risk_multiplier = 0.0"""
    resp = MacroRegimeResponse(
        macro_regime=MacroRegimeEnum.REGIME_A,
        confidence_score=0.9,
        macro_risk_multiplier=1.0,
        emergency_flatten=True,
        hard_lockout_active=False,
    )
    assert resp.emergency_flatten is True
    assert resp.hard_lockout_active is True
    assert resp.macro_risk_multiplier == 0.0


def test_invariant_regime_d_clamping():
    """If REGIME_D and multiplier > 0.25, clamp to 0.0"""
    resp_clamped = MacroRegimeResponse(
        macro_regime=MacroRegimeEnum.REGIME_D,
        confidence_score=0.95,
        macro_risk_multiplier=0.50,
    )
    assert resp_clamped.macro_risk_multiplier == 0.0

    # If REGIME_D and multiplier <= 0.25, preserve it
    resp_preserved = MacroRegimeResponse(
        macro_regime=MacroRegimeEnum.REGIME_D,
        confidence_score=0.95,
        macro_risk_multiplier=0.20,
    )
    assert resp_preserved.macro_risk_multiplier == 0.20


def test_yang_zhang_and_trap_bounds():
    # Valid adjustments
    resp = MacroRegimeResponse(
        macro_regime=MacroRegimeEnum.REGIME_A,
        yang_zhang_multiplier_adjustment=0.8,
        trap_volume_tightening=-0.5,
    )
    assert resp.yang_zhang_multiplier_adjustment == 0.8
    assert resp.trap_volume_tightening == -0.5

    # Out of bounds YZ
    with pytest.raises(ValidationError):
        MacroRegimeResponse(
            macro_regime=MacroRegimeEnum.REGIME_A,
            yang_zhang_multiplier_adjustment=0.79,
        )

    with pytest.raises(ValidationError):
        MacroRegimeResponse(
            macro_regime=MacroRegimeEnum.REGIME_A,
            yang_zhang_multiplier_adjustment=3.1,
        )

    # Out of bounds Trap tightening
    with pytest.raises(ValidationError):
        MacroRegimeResponse(
            macro_regime=MacroRegimeEnum.REGIME_A,
            trap_volume_tightening=-0.6,
        )

    with pytest.raises(ValidationError):
        MacroRegimeResponse(
            macro_regime=MacroRegimeEnum.REGIME_A,
            trap_volume_tightening=1.1,
        )


def test_create_safe_degraded_fallback():
    fb = create_safe_degraded_fallback("LLM service unavailable")
    assert fb.macro_regime == MacroRegimeEnum.REGIME_B
    assert fb.macro_risk_multiplier == 0.50
    assert fb.confidence_score == 0.50
    assert fb.sector_multipliers.tech_beta == 0.50
    assert fb.sector_multipliers.small_cap_credit == 0.50
    assert fb.sector_multipliers.energy_beta == 0.50
    assert fb.emergency_flatten is False
    assert fb.hard_lockout_active is False
    assert "LLM service unavailable" in fb.rationale


def test_parse_and_validate_macro_payload_markdown_blocks():
    raw_markdown = """
    ```json
    {
        "macro_regime": "REGIME_A",
        "confidence_score": 0.88,
        "macro_risk_multiplier": 1.25,
        "sector_multipliers": {
            "tech_beta": 1.4,
            "small_cap_credit": 1.1,
            "energy_beta": 0.9
        },
        "yang_zhang_multiplier_adjustment": 1.15,
        "trap_volume_tightening": 0.10,
        "emergency_flatten": false,
        "hard_lockout_active": false,
        "rationale": "Orderly pre-market expansion with low Treasury yield volatility."
    }
    ```
    """
    resp = parse_and_validate_macro_payload(raw_markdown)
    assert resp.macro_regime == MacroRegimeEnum.REGIME_A
    assert resp.confidence_score == 0.88
    assert resp.macro_risk_multiplier == 1.25
    assert resp.sector_multipliers.tech_beta == 1.4
    assert resp.yang_zhang_multiplier_adjustment == 1.15
    assert resp.trap_volume_tightening == 0.10
    assert resp.emergency_flatten is False


def test_parse_and_validate_macro_payload_malformed_fallback():
    # Bad JSON text returns safe fallback
    bad_json = "```json {not valid json...} ```"
    fb = parse_and_validate_macro_payload(bad_json)
    assert fb.macro_regime == MacroRegimeEnum.REGIME_B
    assert fb.macro_risk_multiplier == 0.50
    assert "JSON decode failure" in fb.rationale

    # Validation failure (e.g. invalid bounds)
    invalid_bounds_json = '{"macro_regime": "REGIME_A", "yang_zhang_multiplier_adjustment": 99.0}'
    fb2 = parse_and_validate_macro_payload(invalid_bounds_json)
    assert fb2.macro_regime == MacroRegimeEnum.REGIME_B
    assert fb2.macro_risk_multiplier == 0.50
    assert "Validation failure" in fb2.rationale
