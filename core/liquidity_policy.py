"""
core/liquidity_policy.py
========================
Tactical liquidity model: Hard Reserve / Soft Reserve / SWVXX sweep advisory /
anticipated weekly inflow ("inflow velocity").

Design rules (GFV safety invariants)
------------------------------------
I1  Tier 1 entries are funded from SETTLED cash only.
I2  Every sale (including a SWVXX redemption) is unsettled until T+1 and is
    part of the Hard Reserve until its settle date.
I3  Pending / anticipated ACH and the external backstop NEVER enter buying
    power. Anticipated inflow only lowers the Soft Reserve *target*.
I4  SWVXX is advisory-only: this module computes a recommendation and never
    imports or calls any order client.
I5  Buying power is always <= settled cash.

Everything is ``Decimal``. All functions are pure, so they are unit-testable
and safe to call from the Tier 1 event loop (no I/O except the explicit
``load_policy`` / ``save_policy`` helpers, which are blocking).
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Dict, Literal, Optional

from pydantic import BaseModel, Field

from core.atomic_io import atomic_write_json, read_json
from core.paths import POLICY_FILE

logger = logging.getLogger("liquidity_policy")

CENT = Decimal("0.01")
ZERO = Decimal("0.00")

WEEKDAYS = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]

# NYSE full-day closures (best effort; extend yearly). Used only for settlement-date math.
NYSE_HOLIDAYS = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
}

POLICY_PATH = POLICY_FILE


def q(value: Decimal) -> Decimal:
    """Quantize to cents."""
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def is_business_day(d: date) -> bool:
    return d.weekday() < 5 and d not in NYSE_HOLIDAYS


def add_business_days(start: date, n: int) -> date:
    """Returns the date ``n`` business days after ``start`` (n >= 0)."""
    d = start
    remaining = n
    while remaining > 0:
        d += timedelta(days=1)
        if is_business_day(d):
            remaining -= 1
    return d


def next_business_day(d: date) -> date:
    return add_business_days(d, 1)


def business_days_between(start: date, end: date) -> int:
    """Number of business days in (start, end]."""
    if end <= start:
        return 0
    count = 0
    d = start
    while d < end:
        d += timedelta(days=1)
        if is_business_day(d):
            count += 1
    return count


# ---------------------------------------------------------------------------
# Policy models
# ---------------------------------------------------------------------------

class InflowSchedule(BaseModel):
    """Anticipated recurring checking-account -> Schwab transfer."""
    enabled: bool = True
    amount: Decimal = Field(default=Decimal("250.00"), ge=0)
    weekday: Literal["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"] = "FRIDAY"
    lag_business_days: int = Field(default=3, ge=0, le=7)
    confidence: Decimal = Field(default=Decimal("0.80"), ge=0, le=1)
    horizon_business_days: int = Field(default=5, ge=1, le=10)
    offset_cap_fraction: Decimal = Field(default=Decimal("0.50"), ge=0, le=1)


class SoftReservePolicy(BaseModel):
    pct_nlv: Decimal = Field(default=Decimal("0.05"), ge=0, le=1)
    floor_usd: Decimal = Field(default=Decimal("100.00"), ge=0)
    max_draw_fraction: Decimal = Field(default=Decimal("0.50"), ge=0, le=1)
    min_cash_buffer: Decimal = Field(default=Decimal("10.00"), ge=0)


class SweepPolicy(BaseModel):
    """SWVXX handling. Advisory-only for the 2026-10-05 session (no orders are ever routed)."""
    symbol: Literal["SWVXX"] = "SWVXX"
    mode: Literal["OFF", "ADVISORY", "AUTO_WITH_APPROVAL"] = "ADVISORY"
    min_idle_usd: Decimal = Field(default=Decimal("250.00"), ge=0)
    trading_float_pct_nlv: Decimal = Field(default=Decimal("0.40"), ge=0, le=1)
    redemption_lead_business_days: int = Field(default=1, ge=1, le=3)


class RiskGatePolicy(BaseModel):
    single_ticker_cap_pct: Decimal = Field(default=Decimal("0.33"), gt=0, le=1)
    high_prob_posterior_min: Decimal = Field(default=Decimal("0.40"), ge=0, le=1)
    high_prob_regime: Literal["A", "B", "C"] = "A"


class LiquidityPolicy(BaseModel):
    inflow: InflowSchedule = Field(default_factory=InflowSchedule)
    soft_reserve: SoftReservePolicy = Field(default_factory=SoftReservePolicy)
    sweep: SweepPolicy = Field(default_factory=SweepPolicy)
    risk_gate: RiskGatePolicy = Field(default_factory=RiskGatePolicy)


class SweepAdvisory(BaseModel):
    action: Literal["NONE", "SWEEP_IN", "REDEEM_FOR_NEXT_SESSION"] = "NONE"
    amount: Decimal = ZERO
    place_by: Optional[date] = None
    settles_on: Optional[date] = None
    mode: str = "ADVISORY"
    routed: bool = False  # always False: no automated SWVXX orders this session
    rationale: str = ""


class BuyingPowerBreakdown(BaseModel):
    as_of: date
    nlv: Decimal
    settled_cash: Decimal
    hard_reserve: Decimal
    hard_reserve_unsettled_cash: Decimal
    hard_reserve_swvxx_in_flight: Decimal
    swvxx_balance: Decimal
    pending_ach: Decimal
    external_backstop: Decimal
    soft_reserve_target: Decimal
    soft_reserve_target_effective: Decimal
    soft_reserve_current: Decimal
    soft_reserve_shortfall: Decimal
    inflow_offset: Decimal
    next_inflow_date: Optional[date] = None
    next_inflow_settles: Optional[date] = None
    replenish_eta: Optional[date] = None
    base_float: Decimal
    soft_draw_available: Decimal
    high_probability: bool
    tactical_float: Decimal
    single_ticker_cap: Decimal
    max_order_notional: Decimal
    sweep: SweepAdvisory


# ---------------------------------------------------------------------------
# Inflow helpers
# ---------------------------------------------------------------------------

def next_inflow_date(today: date, weekday: str) -> date:
    """Next occurrence of ``weekday`` on or after ``today`` (business day)."""
    target = WEEKDAYS.index(weekday)
    d = today
    for _ in range(14):
        if d.weekday() == target and is_business_day(d):
            return d
        d += timedelta(days=1)
    return d


def effective_inflow(schedule: InflowSchedule, today: date) -> Decimal:
    """
    Haircut inflow counted toward the soft-reserve target.

    Counted only if enabled and the next transfer is due within the horizon.
    """
    if not schedule.enabled or schedule.amount <= 0:
        return ZERO
    due = next_inflow_date(today, schedule.weekday)
    if business_days_between(today, due) > schedule.horizon_business_days:
        return ZERO
    return q(schedule.amount * schedule.confidence)


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------

def compute_buying_power(
    *,
    nlv: Decimal,
    settled_cash: Decimal,
    unsettled_cash: Decimal,
    policy: LiquidityPolicy,
    today: date,
    swvxx_settled: Decimal = ZERO,
    swvxx_in_flight: Decimal = ZERO,
    pending_ach: Decimal = ZERO,
    external_backstop: Decimal = ZERO,
    regime: Optional[str] = None,
    posterior: Optional[float] = None,
) -> BuyingPowerBreakdown:
    """
    Computes the tactical buying-power breakdown. Pure function.

    Buying power rule (never touches unsettled cash, pending/anticipated ACH,
    SWVXX or the external backstop):

        base_float     = max(0, S - T_eff)
        tactical_float = base_float + (soft_draw if high_probability else 0)
        max_order      = max(0, min(tactical_float - cash_buffer, cap * NLV))
    """
    nlv = max(Decimal(nlv), ZERO)
    settled = max(Decimal(settled_cash), ZERO)
    unsettled = max(Decimal(unsettled_cash), ZERO)
    swvxx_settled = max(Decimal(swvxx_settled), ZERO)
    swvxx_in_flight = max(Decimal(swvxx_in_flight), ZERO)

    sr = policy.soft_reserve
    target = q(max(sr.floor_usd, nlv * sr.pct_nlv))

    inflow_w = effective_inflow(policy.inflow, today)
    offset = q(min(inflow_w, target * policy.inflow.offset_cap_fraction))
    target_eff = q(max(target - offset, ZERO))

    soft_current = q(min(settled, target_eff))
    shortfall = q(max(target_eff - settled, ZERO))

    base_float = q(max(settled - target_eff, ZERO))
    soft_draw = q(soft_current * sr.max_draw_fraction)

    gate = policy.risk_gate
    high_prob = bool(
        regime is not None
        and posterior is not None
        and regime.upper() == gate.high_prob_regime
        and Decimal(str(posterior)) >= gate.high_prob_posterior_min
    )

    tactical = q(base_float + (soft_draw if high_prob else ZERO))
    tactical = min(tactical, settled)  # I5

    cap = q(nlv * gate.single_ticker_cap_pct)
    max_order = q(max(min(tactical - sr.min_cash_buffer, cap), ZERO))

    # Inflow timing / replenishment ETA
    inflow = policy.inflow
    n_date: Optional[date] = None
    n_settle: Optional[date] = None
    eta: Optional[date] = None
    if inflow.enabled and inflow.amount > 0:
        n_date = next_inflow_date(today, inflow.weekday)
        n_settle = add_business_days(n_date, inflow.lag_business_days)
        if shortfall > 0:
            eta = n_settle

    sweep = _advise_sweep(
        policy=policy, nlv=nlv, settled=settled, target_eff=target_eff,
        swvxx_settled=swvxx_settled, today=today,
    )

    hard_total = q(unsettled + swvxx_in_flight)
    return BuyingPowerBreakdown(
        as_of=today,
        nlv=q(nlv),
        settled_cash=q(settled),
        hard_reserve=hard_total,
        hard_reserve_unsettled_cash=q(unsettled),
        hard_reserve_swvxx_in_flight=q(swvxx_in_flight),
        swvxx_balance=q(swvxx_settled),
        pending_ach=q(max(Decimal(pending_ach), ZERO)),
        external_backstop=q(max(Decimal(external_backstop), ZERO)),
        soft_reserve_target=target,
        soft_reserve_target_effective=target_eff,
        soft_reserve_current=soft_current,
        soft_reserve_shortfall=shortfall,
        inflow_offset=offset,
        next_inflow_date=n_date,
        next_inflow_settles=n_settle,
        replenish_eta=eta,
        base_float=base_float,
        soft_draw_available=soft_draw,
        high_probability=high_prob,
        tactical_float=tactical,
        single_ticker_cap=cap,
        max_order_notional=max_order,
        sweep=sweep,
    )


def _advise_sweep(
    *,
    policy: LiquidityPolicy,
    nlv: Decimal,
    settled: Decimal,
    target_eff: Decimal,
    swvxx_settled: Decimal,
    today: date,
) -> SweepAdvisory:
    """
    Recommendation only. Nothing here routes an order.

    SWVXX proceeds are T+1, so a redemption must be placed the business day
    BEFORE the capital is needed (it settles on the session open).
    """
    sw = policy.sweep
    if sw.mode == "OFF":
        return SweepAdvisory(mode=sw.mode, rationale="Sweep advisory disabled.")

    keep = q(max(nlv * sw.trading_float_pct_nlv, ZERO)) + target_eff
    idle = q(settled - keep)

    if idle >= sw.min_idle_usd:
        amount = q(idle.quantize(Decimal("1"), rounding=ROUND_DOWN))
        return SweepAdvisory(
            action="SWEEP_IN", amount=amount, mode=sw.mode,
            place_by=today, settles_on=next_business_day(today),
            rationale=(
                f"${amount} of settled cash exceeds the trading float plus soft reserve. "
                f"Move to {sw.symbol} after the 15:50 flat; it is redeemable only after T+1."
            ),
        )

    need = q(keep - settled)
    if need > 0 and swvxx_settled > 0:
        amount = q(min(swvxx_settled, need))
        place_by = today if is_business_day(today) else next_business_day(today)
        settles = add_business_days(place_by, sw.redemption_lead_business_days)
        return SweepAdvisory(
            action="REDEEM_FOR_NEXT_SESSION", amount=amount, mode=sw.mode,
            place_by=place_by, settles_on=settles,
            rationale=(
                f"Settled cash is ${need} below the trading float. Redeeming ${amount} of "
                f"{sw.symbol} on {place_by.isoformat()} settles {settles.isoformat()}; "
                f"it can never fund same-day trades (GFV)."
            ),
        )

    return SweepAdvisory(mode=sw.mode, rationale="No sweep needed.")


# ---------------------------------------------------------------------------
# Persistence (blocking - call from a thread)
# ---------------------------------------------------------------------------

def load_policy(path: Path | str = POLICY_PATH) -> LiquidityPolicy:
    """Loads the persisted policy; returns defaults if missing or invalid."""
    raw = read_json(path, default=None)
    if not raw:
        return LiquidityPolicy()
    try:
        return LiquidityPolicy(**raw)
    except Exception as exc:
        logger.error("Invalid %s (%s); falling back to defaults.", path, exc)
        return LiquidityPolicy()


def save_policy(policy: LiquidityPolicy, path: Path | str = POLICY_PATH) -> None:
    atomic_write_json(path, policy.model_dump(mode="json"))


def policy_to_dict(policy: LiquidityPolicy) -> Dict:
    """JSON-friendly dict (Decimals as numbers) for API responses."""
    import json
    from core.atomic_io import StateEncoder
    return json.loads(json.dumps(policy.model_dump(), cls=StateEncoder))
