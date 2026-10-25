#!/usr/bin/env python3
"""
scripts/dev/replay_session.py
=============================
Shadow Replay Validation (#123 Section 6.2) against the October 22 Session Telemetry
(`docs/261022.16-session-replay-analysis.md`).

Validates all four Resiliency Refactor workstreams on the Oct 22 QQQ -2.75% drawdown:
- Acceptance Criterion 1 (AsymmetricHMMGovernor):
  The HMM posterior pi_Goldilocks drops below 0.50 during the afternoon selloff,
  scaling target satellite weight W_sat^* from 0.25 down to <= 0.10.
- Acceptance Criterion 2 (EscalatedMicropriceRouter):
  At 15:50 EDT, all satellite positions (`SOXL`, `TQQQ`, `TNA`) are fully liquidated
  to 0 shares before 15:55:00 EDT without exceeding 50 bps in spread slippage.
- T+1 Regulatory Cash Ledger (T1CashLedger):
  Satellite liquidation proceeds route to C_unsettled, blocking any same-day GFV
  redeployment before sweeping into C_settled at 00:00:00 EDT.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import pytz

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.t1_ledger import T1CashLedger
from execution.escalated_router import (
    DeleveragingReport,
    EscalatedMicropriceRouter,
    EscalationStage,
    QuoteSnapshot,
)
from strategies.regime_governor import AsymmetricHMMGovernor

_EDT = pytz.timezone("America/New_York")


@dataclass
class ReplaySummary:
    session_date: str
    account_suffix: str
    starting_nlv_usd: float
    initial_pi_goldilocks: float
    initial_w_sat_target: float
    afternoon_min_pi_goldilocks: float
    afternoon_min_w_sat_target: float
    dominant_regime_at_trough: str
    eod_remaining_satellite_shares: int
    max_deleveraging_slippage_bps: float
    gfv_same_day_redeploy_blocked: bool
    t1_overnight_swept_usd: float
    acceptance_criterion_1_passed: bool
    acceptance_criterion_2_passed: bool


async def run_oct22_shadow_replay(journal_db_path: Path | None = None) -> ReplaySummary:
    """
    Execute the October 22 shadow replay (`docs/261022.16-session-replay-analysis.md`)
    and verify Acceptance Criteria 1 & 2.
    """
    gov = AsymmetricHMMGovernor(
        initial_pi=[1.0, 0.0, 0.0],
        journal_db_path=journal_db_path,
    )
    ledger = T1CashLedger(settled_cash="412.90", nlv="4400.90")

    initial_pi_g = gov.pi_goldilocks
    initial_w_sat = gov.calculate_target_satellite_weight()

    # 1. Replay morning -> afternoon trajectory from 2026-10-22 session (QQQ -2.75%, SPY -1.18%)
    oct22_telemetry_stream = [
        # Morning calm (09:30 - 11:30 EDT): Goldilocks regime holds
        {"ts": "2026-10-22T09:35:09-04:00", "z_s": 0.85, "s_nlp": 0.42, "cs_bps": 319.49, "v_qqq": -0.0020, "d_cs": 3.3},
        {"ts": "2026-10-22T10:30:10-04:00", "z_s": 0.40, "s_nlp": 0.20, "cs_bps": 321.73, "v_qqq": -0.0055, "d_cs": 5.5},
        {"ts": "2026-10-22T11:30:11-04:00", "z_s": 0.15, "s_nlp": 0.10, "cs_bps": 321.66, "v_qqq": -0.0070, "d_cs": 5.4},
        # Midday deterioration (12:30 - 13:45 EDT)
        {"ts": "2026-10-22T12:50:13-04:00", "z_s": -0.25, "s_nlp": -0.05, "cs_bps": 323.53, "v_qqq": -0.0115, "d_cs": 8.0},
        {"ts": "2026-10-22T13:30:14-04:00", "z_s": -0.30, "s_nlp": -0.08, "cs_bps": 323.35, "v_qqq": -0.0135, "d_cs": 9.5},
        # Afternoon localized tech drawdown (14:00 - 14:35 EDT): QQQ -2.75% session drawdown, 15m velocity <= -1.55%
        {"ts": "2026-10-22T14:05:15-04:00", "z_s": -0.45, "s_nlp": -0.15, "cs_bps": 329.80, "v_qqq": -0.0155, "d_cs": 12.2},
        {"ts": "2026-10-22T14:15:15-04:00", "z_s": -0.55, "s_nlp": -0.20, "cs_bps": 331.20, "v_qqq": -0.0168, "d_cs": 13.6},
        {"ts": "2026-10-22T14:30:15-04:00", "z_s": -0.65, "s_nlp": -0.25, "cs_bps": 332.40, "v_qqq": -0.0172, "d_cs": 14.8},
        {"ts": "2026-10-22T14:33:45-04:00", "z_s": -0.70, "s_nlp": -0.28, "cs_bps": 333.10, "v_qqq": -0.0175, "d_cs": 15.5},
    ]

    min_pi_g = 1.0
    min_w_sat = 0.25
    trough_regime = "Goldilocks"

    for tick in oct22_telemetry_stream:
        gov.update(
            macro_surprise_z=tick["z_s"],
            macro_sentiment=tick["s_nlp"],
            macro_hy_spread_bps=tick["cs_bps"],
            tech_velocity_15m=tick["v_qqq"],
            delta_cs_1h_bps=tick["d_cs"],
        )
        w_sat = gov.calculate_target_satellite_weight()
        if gov.pi_goldilocks < min_pi_g:
            min_pi_g = gov.pi_goldilocks
        if w_sat <= min_w_sat:
            min_w_sat = w_sat
            trough_regime = gov.get_dominant_regime()

    # 2. Replay 15:50:00 - 15:55:00 EDT Escalated Microprice Deleveraging Window
    # Satellite portfolio: SOXL (139 shares in stress scenario + TQQQ 5 shares + TNA 6 shares)
    satellite_inventory: Dict[str, Dict[str, float]] = {
        "SOXL": {"qty": 139, "bid": 28.50, "ask": 28.56},
        "TQQQ": {"qty": 5, "bid": 70.15, "ask": 70.23},
        "TNA": {"qty": 6, "bid": 54.80, "ask": 54.88},
    }

    router = EscalatedMicropriceRouter(window_duration_seconds=300.0)
    reports: List[DeleveragingReport] = []

    for sym, info in satellite_inventory.items():
        qty_target = int(info["qty"])
        base_bid = float(info["bid"])
        base_ask = float(info["ask"])
        virtual_clock = {"t": 0.0}

        async def _fast_sleep(sec: float) -> None:
            virtual_clock["t"] += 55.0

        def _quote_fn(s: str, tau: float, b: float = base_bid, a: float = base_ask) -> QuoteSnapshot:
            return QuoteSnapshot(symbol=s, bid_price=b, ask_price=a, bid_size=250.0, ask_size=150.0)

        def _sim_fill(step, rem_qty: int, quote: QuoteSnapshot) -> tuple[int, float]:
            if step.stage == EscalationStage.STAGE_A_PASSIVE_MICROPRICE:
                fill = max(1, rem_qty // 3)
                return fill, step.truncated_price
            if step.stage == EscalationStage.STAGE_B_SPREAD_CONCESSION:
                fill = max(1, rem_qty // 2)
                return fill, step.truncated_price
            return rem_qty, step.truncated_price

        rep = await router.liquidate_position(
            symbol=sym,
            target_quantity=qty_target,
            quote_provider=_quote_fn,
            clock_fn=lambda: virtual_clock["t"],
            sleep_fn=_fast_sleep,
            simulated_fill_fn=_sim_fill,
        )
        reports.append(rep)
        # Credit liquidation proceeds into T+1 unsettled cash bucket
        ledger.record_sell_fill(
            sym,
            rep.filled_quantity,
            rep.vwap_fill_price,
            when=_EDT.localize(datetime(2026, 10, 22, 15, 53, 30)),
        )

    remaining_shares = sum(r.remaining_quantity for r in reports)
    max_slippage_bps = max((r.slippage_bps for r in reports), default=0.0)

    # 3. Verify T+1 GFV Firewall blocks same-day redeployment of unsettled liquidation proceeds
    allowed_same_day, reason_same_day = ledger.can_submit_order("BUY", "1500.00", symbol="SOXL")
    gfv_blocked = (not allowed_same_day) and ("GFV REJECTION" in reason_same_day)

    # 4. Verify 00:00:00 EDT overnight clearing sweeps unsettled proceeds to settled
    swept_usd = float(
        ledger.process_t1_overnight_clearing(
            current_dt=_EDT.localize(datetime(2026, 10, 23, 0, 0, 0))
        )
    )

    ac1_passed = (min_pi_g < 0.50) and (min_w_sat <= 0.10)
    ac2_passed = (remaining_shares == 0) and (max_slippage_bps <= 50.0)

    return ReplaySummary(
        session_date="2026-10-22",
        account_suffix="4015",
        starting_nlv_usd=4400.90,
        initial_pi_goldilocks=round(initial_pi_g, 4),
        initial_w_sat_target=round(initial_w_sat, 4),
        afternoon_min_pi_goldilocks=round(min_pi_g, 6),
        afternoon_min_w_sat_target=round(min_w_sat, 4),
        dominant_regime_at_trough=trough_regime,
        eod_remaining_satellite_shares=remaining_shares,
        max_deleveraging_slippage_bps=round(max_slippage_bps, 2),
        gfv_same_day_redeploy_blocked=gfv_blocked,
        t1_overnight_swept_usd=round(swept_usd, 2),
        acceptance_criterion_1_passed=ac1_passed,
        acceptance_criterion_2_passed=ac2_passed,
    )


def main() -> int:
    summary = asyncio.run(run_oct22_shadow_replay())
    print(json.dumps(asdict(summary), indent=2))
    if not (summary.acceptance_criterion_1_passed and summary.acceptance_criterion_2_passed):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
