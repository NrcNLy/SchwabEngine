"""
core/ses_mapping.py
===================
Semiconductor Environment Score (SES) Mathematical Mapping and Snapshot Loader.

Key Mathematical Properties:
1. Normalization: Empirical SES is strictly bounded in [-1.0, 1.0].
2. Continuous Piecewise-Linear Interpolation:
   - S_sat = 0.80 (saturation threshold)
   - Zero point (SES = 0.0): CHOP regime anchors (k_u=1.5, k_l=1.5, gamma=0.6)
   - Saturated positive (SES >= +0.80): FAVORABLE regime anchors (k_u=3.0, k_l=2.0, gamma=1.8)
   - Saturated negative (SES <= -0.80): DEFENSIVE regime anchors (k_u=1.0, k_l=0.5, gamma=1.0)
   - Formula for p = clip(|SES| / S_sat, 0.0, 1.0):
     X(SES) = X_CHOP + p * (X_FAV - X_CHOP) if SES >= 0
     X(SES) = X_CHOP + p * (X_DEF - X_CHOP) if SES < 0
   - Prevents step discontinuities and boundary clipping.
   - Preserves invariant: k_l <= k_u everywhere in [-1.0, 1.0].
3. Staleness Guard:
   - Checks snapshot age against a 90-minute TTL.
   - Falls back gracefully to neutral CHOP parameters if stale or missing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_SNAPSHOT_PATH = Path("data/exports/latest_ses_snapshot.json")
DEFAULT_S_SAT = 0.80
DEFAULT_THETA = 0.35
DEFAULT_MAX_AGE_MINUTES = 90.0

# Anchors from approved specification (AVE Table §4.3)
# Parameter layout: (k_u, k_l, gamma)
CHOP_ANCHORS = (1.5, 1.5, 0.6)
FAVORABLE_ANCHORS = (3.0, 2.0, 1.8)
DEFENSIVE_ANCHORS = (1.0, 0.5, 1.0)


@dataclass(frozen=True, slots=True)
class EnvelopeParams:
    k_u: float
    k_l: float
    gamma: float
    regime: str
    ses_score: float
    is_fallback: bool = False


def ses_to_params(
    ses: float,
    s_sat: float = DEFAULT_S_SAT,
    theta: float = DEFAULT_THETA,
) -> EnvelopeParams:
    """
    Maps continuous SES in [-1.0, 1.0] to envelope parameters (k_u, k_l, gamma)
    without step discontinuities at regime boundaries.
    """
    s_sat = max(1e-4, float(s_sat))
    clamped_ses = max(-1.0, min(1.0, float(ses)))

    p = min(1.0, abs(clamped_ses) / s_sat)

    ku_chop, kl_chop, g_chop = CHOP_ANCHORS
    ku_fav, kl_fav, g_fav = FAVORABLE_ANCHORS
    ku_def, kl_def, g_def = DEFENSIVE_ANCHORS

    if clamped_ses >= 0.0:
        k_u = ku_chop + p * (ku_fav - ku_chop)
        k_l = kl_chop + p * (kl_fav - kl_chop)
        gamma = g_chop + p * (g_fav - g_chop)
    else:
        k_u = ku_chop + p * (ku_def - ku_chop)
        k_l = kl_chop + p * (kl_def - kl_chop)
        gamma = g_chop + p * (g_def - g_chop)

    # Classify qualitative regime string
    if clamped_ses >= theta:
        regime = "FAVORABLE"
    elif clamped_ses <= -theta:
        regime = "DEFENSIVE"
    else:
        regime = "CHOP"

    return EnvelopeParams(
        k_u=float(k_u),
        k_l=float(k_l),
        gamma=float(gamma),
        regime=regime,
        ses_score=clamped_ses,
        is_fallback=False,
    )


def load_ses_snapshot(
    snapshot_path: Optional[Path | str] = None,
    max_age_minutes: float = DEFAULT_MAX_AGE_MINUTES,
    s_sat: float = DEFAULT_S_SAT,
    theta: float = DEFAULT_THETA,
    now: Optional[datetime] = None,
) -> EnvelopeParams:
    """
    Loads latest SES snapshot from disk with a staleness check.
    If missing, malformed, or older than max_age_minutes, returns default CHOP parameters.
    """
    path = Path(snapshot_path or DEFAULT_SNAPSHOT_PATH)
    current_time = now or datetime.now(timezone.utc)

    if not path.exists():
        logger.warning("SES snapshot file %s not found. Falling back to neutral CHOP parameters.", path)
        fallback = ses_to_params(0.0, s_sat=s_sat, theta=theta)
        return EnvelopeParams(
            k_u=fallback.k_u,
            k_l=fallback.k_l,
            gamma=fallback.gamma,
            regime=fallback.regime,
            ses_score=fallback.ses_score,
            is_fallback=True,
        )

    try:
        raw_text = path.read_text(encoding="utf-8")
        data = json.loads(raw_text)
        ses_val = float(data.get("ses", 0.0))
        ts_str = data.get("timestamp")

        # Parse timestamp
        is_stale = False
        if ts_str:
            try:
                # Handle ISO format (e.g. 2026-10-14 15:30:00-04:00 or ISO-8601)
                parsed_ts = datetime.fromisoformat(ts_str.strip())
                if parsed_ts.tzinfo is None:
                    parsed_ts = parsed_ts.replace(tzinfo=timezone.utc)
                age_minutes = (current_time - parsed_ts).total_seconds() / 60.0
                if age_minutes > max_age_minutes:
                    logger.warning(
                        "SES snapshot %s is stale (age: %.1f mins > max: %.1f mins). Falling back to CHOP.",
                        path, age_minutes, max_age_minutes,
                    )
                    is_stale = True
            except Exception as e:
                logger.warning("Could not parse timestamp '%s' in SES snapshot: %s", ts_str, e)
                is_stale = True
        else:
            is_stale = True

        if is_stale:
            fallback = ses_to_params(0.0, s_sat=s_sat, theta=theta)
            return EnvelopeParams(
                k_u=fallback.k_u,
                k_l=fallback.k_l,
                gamma=fallback.gamma,
                regime=fallback.regime,
                ses_score=fallback.ses_score,
                is_fallback=True,
            )

        params = ses_to_params(ses_val, s_sat=s_sat, theta=theta)
        return params

    except Exception as exc:
        logger.error("Error reading SES snapshot from %s: %s. Using neutral CHOP fallback.", path, exc)
        fallback = ses_to_params(0.0, s_sat=s_sat, theta=theta)
        return EnvelopeParams(
            k_u=fallback.k_u,
            k_l=fallback.k_l,
            gamma=fallback.gamma,
            regime=fallback.regime,
            ses_score=fallback.ses_score,
            is_fallback=True,
        )
