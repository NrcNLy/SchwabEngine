"""
macro/governor_schema.py
========================
Enhanced Pydantic V2 macro schema and deterministic validation harness
for the Schwab Algorithmic Day-Trading Engine.

Provides:
- MacroRegimeEnum (REGIME_A, REGIME_B, REGIME_C, REGIME_D)
- SectorDriftMultipliers (tech_beta, small_cap_credit, energy_beta)
- MacroRegimeResponse with strict invariant validators
- create_safe_degraded_fallback: fail-safe defaulting to REGIME_B with 50% risk cut
- parse_and_validate_macro_payload: robust parser handling code blocks & malformed input
"""

from __future__ import annotations

from enum import Enum
import json
import logging
import re
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger("macro_schema")


class MacroRegimeEnum(str, Enum):
    """
    Macroeconomic regimes:
    REGIME_A: Trend Expansion / Directional Breakout (Favors 15m ORB)
    REGIME_B: Mean-Reverting Bracket / Range-Bound (Favors VWAP MR)
    REGIME_C: High-Noise Chop / Indecision (Compressed position sizing)
    REGIME_D: Macro Shock / Event Blackout / Severe Dislocation (Hard lockout / Cash stay)
    """
    REGIME_A = "REGIME_A"
    REGIME_B = "REGIME_B"
    REGIME_C = "REGIME_C"
    REGIME_D = "REGIME_D"


class SectorDriftMultipliers(BaseModel):
    """
    Sector-specific risk scaling factors bounded in [0.0, 2.0].
    """
    tech_beta: float = Field(
        default=1.0,
        ge=0.0,
        le=2.0,
        description="Tech beta risk scalar for SOXL, TQQQ, FNGU [0.0, 2.0]",
    )
    small_cap_credit: float = Field(
        default=1.0,
        ge=0.0,
        le=2.0,
        description="Small-cap and regional banking credit risk scalar for TNA, DPST [0.0, 2.0]",
    )
    energy_beta: float = Field(
        default=1.0,
        ge=0.0,
        le=2.0,
        description="Energy beta risk scalar for UCO, SCO [0.0, 2.0]",
    )


class MacroRegimeResponse(BaseModel):
    """
    Validated macro assessment payload emitted by the Macro Sentinel.
    """
    macro_regime: MacroRegimeEnum = Field(
        description="Classified macro regime (REGIME_A, REGIME_B, REGIME_C, REGIME_D)"
    )
    confidence_score: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Model certainty/conviction score between 0.0 and 1.0",
    )
    macro_risk_multiplier: float = Field(
        default=1.0,
        ge=0.0,
        le=2.0,
        description="Overall capital risk scaling factor [0.0, 2.0]",
    )
    sector_multipliers: SectorDriftMultipliers = Field(
        default_factory=SectorDriftMultipliers,
        description="Sector drift multipliers across tech, small cap credit, and energy",
    )
    yang_zhang_multiplier_adjustment: float = Field(
        default=1.0,
        ge=0.8,
        le=3.0,
        description="Scalar adjustment to Yang-Zhang stop distance [0.8, 3.0]",
    )
    trap_volume_tightening: float = Field(
        default=0.0,
        ge=-0.5,
        le=1.0,
        description="Dual-leg volume absorption trap tightening increment [-0.5, 1.0]",
    )
    emergency_flatten: bool = Field(
        default=False,
        description="Emergency liquidation flag requiring immediate position close",
    )
    hard_lockout_active: bool = Field(
        default=False,
        description="Hard lockout active vetoing all new order entries",
    )
    rationale: str = Field(
        default="",
        description="Sanitized analytical rationale for the macro regime assignment",
    )
    market_bias: Optional[str] = Field(
        default="NEUTRAL",
        description="Directional bias (BULLISH, BEARISH, NEUTRAL)",
    )

    @model_validator(mode="after")
    def enforce_invariants(self) -> "MacroRegimeResponse":
        """
        Enforce platform risk invariants:
        1. emergency_flatten == True -> hard_lockout_active = True and macro_risk_multiplier = 0.0
        2. If REGIME_D and multiplier > 0.25, clamp to 0.0
        """
        if self.emergency_flatten:
            self.hard_lockout_active = True
            self.macro_risk_multiplier = 0.0

        if self.macro_regime == MacroRegimeEnum.REGIME_D and self.macro_risk_multiplier > 0.25:
            self.macro_risk_multiplier = 0.0

        return self


def create_safe_degraded_fallback(reason: str) -> MacroRegimeResponse:
    """
    Construct a deterministic, safe degraded fallback payload.
    Defaults to REGIME_B with 50% risk de-escalation across all multipliers.
    """
    return MacroRegimeResponse(
        macro_regime=MacroRegimeEnum.REGIME_B,
        confidence_score=0.50,
        macro_risk_multiplier=0.50,
        sector_multipliers=SectorDriftMultipliers(
            tech_beta=0.50,
            small_cap_credit=0.50,
            energy_beta=0.50,
        ),
        yang_zhang_multiplier_adjustment=1.20,
        trap_volume_tightening=0.20,
        emergency_flatten=False,
        hard_lockout_active=False,
        rationale=f"SAFE DEGRADED FALLBACK: {reason}",
        market_bias="NEUTRAL",
    )


def parse_and_validate_macro_payload(raw_text: Any) -> MacroRegimeResponse:
    """
    Parses and validates raw LLM output or dictionary into MacroRegimeResponse.
    Cleans Markdown fences, handles code blocks, normalizes shorthand regime codes,
    and returns create_safe_degraded_fallback() cleanly upon any error.
    """
    if raw_text is None:
        return create_safe_degraded_fallback("Received empty or None payload")

    if isinstance(raw_text, MacroRegimeResponse):
        return raw_text

    data: Any = None
    if isinstance(raw_text, dict):
        data = raw_text
    elif isinstance(raw_text, str):
        cleaned = raw_text.strip()
        # Strip markdown fences if present (e.g. ```json ... ``` or ``` ...)
        if "```" in cleaned:
            match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned)
            if match:
                cleaned = match.group(1).strip()
            else:
                cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
                cleaned = re.sub(r"\s*```$", "", cleaned).strip()

        try:
            data = json.loads(cleaned)
        except Exception as exc:
            logger.warning("Failed to decode JSON from macro payload: %s (payload: %.120s)", exc, cleaned)
            return create_safe_degraded_fallback(f"JSON decode failure: {exc}")
    elif hasattr(raw_text, "text"):
        return parse_and_validate_macro_payload(raw_text.text)
    else:
        logger.warning("Unsupported macro payload object type: %s", type(raw_text))
        return create_safe_degraded_fallback(f"Unsupported payload type: {type(raw_text)}")

    if not isinstance(data, dict):
        return create_safe_degraded_fallback("Payload did not parse to a JSON object")

    # Normalize regime shorthand if model outputs e.g. "A" or "REGIME_A"
    regime_raw = str(data.get("macro_regime", "")).strip().upper()
    regime_map = {
        "A": MacroRegimeEnum.REGIME_A.value,
        "REGIME_A": MacroRegimeEnum.REGIME_A.value,
        "EXPANSION": MacroRegimeEnum.REGIME_A.value,
        "B": MacroRegimeEnum.REGIME_B.value,
        "REGIME_B": MacroRegimeEnum.REGIME_B.value,
        "BRACKET": MacroRegimeEnum.REGIME_B.value,
        "C": MacroRegimeEnum.REGIME_C.value,
        "REGIME_C": MacroRegimeEnum.REGIME_C.value,
        "CHOP": MacroRegimeEnum.REGIME_C.value,
        "D": MacroRegimeEnum.REGIME_D.value,
        "REGIME_D": MacroRegimeEnum.REGIME_D.value,
        "EVENT_BLACKOUT": MacroRegimeEnum.REGIME_D.value,
        "BLACKOUT": MacroRegimeEnum.REGIME_D.value,
    }
    if regime_raw in regime_map:
        data["macro_regime"] = regime_map[regime_raw]

    try:
        return MacroRegimeResponse.model_validate(data)
    except Exception as exc:
        logger.warning("Pydantic validation failure for macro payload: %s", exc)
        return create_safe_degraded_fallback(f"Validation failure: {exc}")
