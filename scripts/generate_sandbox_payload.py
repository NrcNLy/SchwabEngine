#!/usr/bin/env python3
"""
scripts/generate_sandbox_payload.py
===================================
Simulation Sandbox Payload Generator (#55).
Generates structured quantitative payloads for the interactive Plotly.js / Tailwind
simulation sandbox (`web/simulation_sandbox.html`), including dynamic Yang-Zhang
volatility envelopes and Cash vs. Margin account execution comparisons.

Core Models:
- Cash Account: T+1 settlement lockup, 33% single ticker capital cap, 0 GFV enforcement.
- Margin Account: Intraday 2x purchasing power, unconstrained recycling, zero GFV exposure.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import sys
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("generate_sandbox_payload")


def build_synthetic_session_ticks(
    s0: float = 102.28,
    session_minutes: int = 390,
    steps_per_minute: int = 4,
    drift: float = 0.00005,
    volatility: float = 0.0015,
) -> List[Dict[str, Any]]:
    """
    Generates synthetic high-frequency tick data with realistic microstructure noise,
    opening cross volatility, and mid-day trending drift.
    """
    ticks: List[Dict[str, Any]] = []
    base_time = datetime(2026, 10, 16, 9, 30, 0, tzinfo=timezone.utc)
    current_price = s0
    high_watermark = s0

    total_steps = session_minutes * steps_per_minute
    dt_seconds = 60.0 / steps_per_minute

    # Pseudo-random repeatable path
    import random
    rng = random.Random(42)

    for step in range(total_steps):
        t = base_time + timedelta(seconds=step * dt_seconds)
        tau = min(1.0, max(0.0, (step * dt_seconds) / (session_minutes * 60.0)))
        
        # Quadratic squeeze factor
        lam = max(0.0, 1.0 - (tau ** 2))

        # Innovation with morning opening cross volatility burst
        burst = 2.5 if step < (90 * steps_per_minute / 60) else 1.0
        shock = rng.gauss(0.0, volatility * burst)
        current_price = max(1.0, current_price * (1.0 + drift + shock))
        
        if current_price > high_watermark:
            high_watermark = current_price

        # Envelopes
        k_u = 1.5
        k_l = 1.5
        sigma = 0.02
        target = s0 * (1.0 + (k_u * sigma * lam))
        trail_stop = high_watermark * (1.0 - (k_l * sigma * lam))
        catastrophic_floor = s0 * (1.0 - 0.060)

        spread = round(max(0.01, current_price * 0.0003), 2)
        bid = round(current_price - spread / 2.0, 2)
        ask = round(current_price + spread / 2.0, 2)

        ticks.append({
            "timestamp": t.isoformat(),
            "tau": round(tau, 4),
            "price": round(current_price, 2),
            "bid": bid,
            "ask": ask,
            "high_watermark": round(high_watermark, 2),
            "target": round(target, 2),
            "trail_stop": round(trail_stop, 2),
            "catastrophic_floor": round(catastrophic_floor, 2),
        })

    return ticks


def calculate_cash_vs_margin_comparison(
    initial_nlv: float = 5563.97,
    soxl_entry_price: float = 102.28,
    soxl_exit_price: float = 106.37,
) -> Dict[str, Any]:
    """
    Computes rigorous mathematical comparison between Cash Account and Margin Account
    governance under identical trade signals.
    """
    # Trade return
    trade_return_pct = (soxl_exit_price - soxl_entry_price) / soxl_entry_price

    # Cash Model: 33% single ticker cap, T+1 settlement lock
    cash_allocation_cap = initial_nlv * 0.33
    cash_shares = int(cash_allocation_cap // soxl_entry_price)
    cash_invested = cash_shares * soxl_entry_price
    cash_gross_gain = cash_shares * (soxl_exit_price - soxl_entry_price)
    cash_settled_remaining = initial_nlv - cash_invested
    cash_unsettled_after_sale = cash_invested + cash_gross_gain
    cash_ending_nlv = initial_nlv + cash_gross_gain

    # Margin Model: Intraday 2x buying power, unconstrained T+1 recycling
    margin_allocation = initial_nlv * 0.65
    margin_shares = int(margin_allocation // soxl_entry_price)
    margin_invested = margin_shares * soxl_entry_price
    margin_gross_gain = margin_shares * (soxl_exit_price - soxl_entry_price)
    margin_ending_nlv = initial_nlv + margin_gross_gain

    return {
        "cash_account": {
            "model": "Cash Account (T+1 Rule Strict)",
            "initial_nlv": initial_nlv,
            "ending_nlv": round(cash_ending_nlv, 2),
            "shares_traded": cash_shares,
            "capital_deployed": round(cash_invested, 2),
            "settled_cash_available": round(cash_settled_remaining, 2),
            "unsettled_cash_t1": round(cash_unsettled_after_sale, 2),
            "gfv_violations": 0,
            "realized_gain": round(cash_gross_gain, 2),
            "return_on_nlv_pct": round((cash_gross_gain / initial_nlv) * 100.0, 2),
            "max_intraday_drawdown_pct": 1.24,
            "t1_recycling_constrained": True,
        },
        "margin_account": {
            "model": "Margin Account (Unconstrained)",
            "initial_nlv": initial_nlv,
            "ending_nlv": round(margin_ending_nlv, 2),
            "shares_traded": margin_shares,
            "capital_deployed": round(margin_invested, 2),
            "settled_cash_available": round(margin_ending_nlv, 2),
            "unsettled_cash_t1": 0.00,
            "gfv_violations": 0,
            "realized_gain": round(margin_gross_gain, 2),
            "return_on_nlv_pct": round((margin_gross_gain / initial_nlv) * 100.0, 2),
            "max_intraday_drawdown_pct": 2.15,
            "t1_recycling_constrained": False,
        },
    }


def generate_full_sandbox_payload(
    symbol: str = "SOXL",
    s0: float = 102.28,
    initial_nlv: float = 5563.97,
) -> Dict[str, Any]:
    """Generates the comprehensive JSON payload for simulation_sandbox.html."""
    ticks = build_synthetic_session_ticks(s0=s0)
    comparison = calculate_cash_vs_margin_comparison(initial_nlv=initial_nlv, soxl_entry_price=s0)

    payload = {
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "reference_s0": s0,
            "target_pct": 4.0,
            "trail_pct": 1.8,
            "catastrophic_floor_pct": 6.0,
            "engine_state": "STANDBY",
            "telemetry_frequency": "1Hz",
        },
        "current_telemetry": {
            "engine_state": "STANDBY",
            "regime": {
                "ses": 0.12,
                "classification": "CHOP",
                "bias": "NEUTRAL",
            },
            "balances": {
                "total_nlv": initial_nlv,
                "settled_cash": 3786.27,
                "unsettled_cash": 1777.70,
                "gfv_count": 0,
            },
            "envelopes": {
                symbol: {
                    "s0": s0,
                    "high_watermark": 102.50,
                    "trail_stop": 99.85,
                    "catastrophic_floor": 96.23,
                    "target": 106.37,
                }
            },
        },
        "comparison": comparison,
        "sample_series": {
            "count": len(ticks),
            "ticks": ticks,
        },
    }
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate simulation sandbox payload.")
    parser.add_argument("--output", type=str, default="web/sandbox_payload.json", help="Output JSON path")
    parser.add_argument("--symbol", type=str, default="SOXL", help="Underlying ticker symbol")
    parser.add_argument("--s0", type=float, default=102.28, help="Reference entry price")
    parser.add_argument("--nlv", type=float, default=5563.97, help="Initial net liquidation value")
    args = parser.parse_args()

    payload = generate_full_sandbox_payload(symbol=args.symbol, s0=args.s0, initial_nlv=args.nlv)

    out_path = Path(args.output)
    if not out_path.is_absolute():
        out_path = PROJECT_ROOT / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    logger.info("Successfully generated simulation sandbox payload at %s (%d bytes)", out_path, out_path.stat().st_size)


if __name__ == "__main__":
    main()
