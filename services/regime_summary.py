"""
services/regime_summary.py
==========================
Turns the Tier 2 governor output (``strategy_config.json``) and optional live
per-symbol regime metrics into a short, user-facing summary. Pure functions,
no I/O, no LLM call - so it can never block or bill.

IMPORTANT: ``strategy_config.json`` is advisory today. Nothing in Tier 1 reads
it, and by design it never scales position size (zero auto-throttling). The
summary says so rather than implying otherwise.

Label semantics
---------------
* Governor ``target_regime`` follows ``core.models.GovernorConfig``:
  "A" = breakout / trend, "C" = mean-reversion.
* Live engine regimes follow ``execution.strategies.MarketRegime``:
  "A" trend expansion, "B" range / mean-reversion, "C" high-noise chop (HALT).
  These are reported separately and never conflated with the governor field.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

STALE_HOURS = 18.0

GOVERNOR_REGIME_LABELS = {
    "A": ("Breakout / trend", "Opening-range breakouts are favored."),
    "C": ("Mean-reversion", "Fades back toward VWAP are favored over breakouts."),
}

ENGINE_REGIME_LABELS = {
    "A": "Trend expansion",
    "B": "Range-bound",
    "C": "High-noise chop (entries halted)",
}


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _bias_key(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    if text.startswith("bull"):
        return "bullish"
    if text.startswith("bear"):
        return "bearish"
    return "neutral"


def volatility_label(multiplier: Optional[float]) -> str:
    if multiplier is None:
        return "Volatility expectation unavailable"
    if multiplier >= 1.4:
        return f"High volatility expected (~{multiplier:.1f}x normal)"
    if multiplier >= 1.1:
        return f"Somewhat elevated volatility (~{multiplier:.1f}x normal)"
    if multiplier > 0.9:
        return f"Normal volatility (~{multiplier:.1f}x)"
    return f"Subdued volatility (~{multiplier:.1f}x normal)"


def build_regime_summary(
    strategy_config: Optional[Dict[str, Any]],
    now: Optional[datetime] = None,
    engine_regimes: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Returns a JSON-serialisable dict:
        headline, bias, bias_label, regime, regime_label, volatility_label,
        volatility_multiplier, as_of, age_hours, stale, engine_regimes, advisory_note
    """
    now = now or datetime.now(timezone.utc)
    cfg = strategy_config or {}

    bias = _bias_key(cfg.get("macro_bias"))
    bias_label = {"bullish": "Bullish bias", "bearish": "Bearish bias", "neutral": "Neutral bias"}[bias]

    regime = str(cfg.get("target_regime", "")).upper() or None
    regime_label, regime_desc = GOVERNOR_REGIME_LABELS.get(regime, ("Not set", ""))

    try:
        mult: Optional[float] = float(cfg["volatility_multiplier"])
    except (KeyError, TypeError, ValueError):
        mult = None
    vol_label = volatility_label(mult)

    updated = _parse_ts(cfg.get("_updated_at"))
    age_hours = (now - updated).total_seconds() / 3600.0 if updated else None
    stale = age_hours is None or age_hours > STALE_HOURS

    engine_list: List[Dict[str, str]] = []
    for sym, code in sorted((engine_regimes or {}).items()):
        engine_list.append({"symbol": sym, "regime": code, "label": ENGINE_REGIME_LABELS.get(code, code)})

    if not cfg:
        headline = "No macro view yet - the pre-market analysis has not run."
    else:
        parts = [bias_label, f"{regime_label} setups favored" if regime else "No regime preference", vol_label]
        headline = " | ".join(parts)

    return {
        "headline": headline,
        "bias": bias,
        "bias_label": bias_label,
        "regime": regime,
        "regime_label": regime_label,
        "regime_description": regime_desc,
        "volatility_label": vol_label,
        "volatility_multiplier": mult,
        "as_of": updated.isoformat() if updated else None,
        "age_hours": round(age_hours, 1) if age_hours is not None else None,
        "stale": stale,
        "engine_regimes": engine_list,
        "advisory_note": "Advisory only: this view does not change position sizing or entry rules.",
    }
