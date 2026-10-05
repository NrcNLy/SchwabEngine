"""
core/session.py
===============
Temporal Session State Machine and Intraday Execution Phase Gating.

Intraday Phases (EDT wall clock, business days):
- OFFLINE:               Before 08:35 or after 17:00 EDT (or weekends/holidays)
- PRE_MARKET:            08:35 - 09:30 EDT
- MORNING_DRIVE:         09:30 - 10:30 EDT (Full 100% Quarter-Kelly sizing for ORB entries)
- MID_MORNING:           10:30 - 11:30 EDT (Standard parameters, trend continuation)
- MIDDAY_FREEZE:         11:30 - 14:00 EDT (Lockout on new entries; manages trailing stops only)
- POWER_HOUR:            14:00 - 15:35 EDT (Secondary trend entries at 50% fractional sizing)
- PRE_CLOSE:             15:35 - 15:50 EDT (Lockout on new entries; preparing for EOD unwind)
- MANDATORY_FLATTEN:     15:50 - 15:55 EDT (Active liquidation sequence)
- POST_CLOSE_REFLECTION: 15:55 - 17:00 EDT
"""

from __future__ import annotations

import logging
from datetime import datetime, time as dtime
from enum import Enum
from typing import Any, Dict, Optional, Tuple

import pytz

logger = logging.getLogger("session")
_EDT = pytz.timezone("America/New_York")


class TradingPhase(str, Enum):
    OFFLINE = "OFFLINE"
    PRE_MARKET = "PRE_MARKET"
    MORNING_DRIVE = "MORNING_DRIVE"
    MID_MORNING = "MID_MORNING"
    MIDDAY_FREEZE = "MIDDAY_FREEZE"
    POWER_HOUR = "POWER_HOUR"
    PRE_CLOSE = "PRE_CLOSE"
    MANDATORY_FLATTEN = "MANDATORY_FLATTEN"
    POST_CLOSE_REFLECTION = "POST_CLOSE_REFLECTION"


def parse_time_str(val: Any, default: dtime) -> dtime:
    if isinstance(val, dtime):
        return val
    if not val:
        return default
    parts = str(val).split(":")
    h = int(parts[0])
    m = int(parts[1]) if len(parts) > 1 else 0
    s = int(parts[2]) if len(parts) > 2 else 0
    return dtime(h, m, s)


def get_session_phase(now: Optional[datetime] = None, cfg: Optional[Dict[str, Any]] = None) -> TradingPhase:
    """
    Evaluates the active TradingPhase from the EDT wall clock.
    """
    from core.liquidity_policy import is_business_day

    now = (now or datetime.now(_EDT)).astimezone(_EDT)
    if not is_business_day(now.date()):
        return TradingPhase.OFFLINE

    t = now.time()
    gates = (cfg or {}).get("risk", {}).get("temporal_gates", {}) or {}
    sched = (cfg or {}).get("schedule", {}) or {}

    # Define boundaries with robust defaults
    t_pre_market = dtime(8, 35)
    t_open = parse_time_str(gates.get("morning_drive_start"), dtime(9, 30))
    t_morning_drive_end = parse_time_str(gates.get("morning_drive_end"), dtime(10, 30))
    t_mid_morning_end = parse_time_str(gates.get("mid_morning_end"), dtime(11, 30))
    t_midday_freeze_start = parse_time_str(gates.get("midday_freeze_start"), dtime(11, 30))
    t_midday_freeze_end = parse_time_str(gates.get("midday_freeze_end"), dtime(14, 0))
    t_power_hour_start = parse_time_str(gates.get("power_hour_start"), dtime(14, 0))
    t_power_hour_end = parse_time_str(gates.get("power_hour_end"), dtime(15, 35))
    t_pre_close_start = parse_time_str(gates.get("pre_close_start"), dtime(15, 35))
    t_flatten_start = parse_time_str(
        gates.get("mandatory_flatten_start") or sched.get("eod_liquidation_time"), dtime(15, 50)
    )
    t_flatten_end = parse_time_str(
        gates.get("mandatory_flatten_deadline") or sched.get("flat_deadline_time"), dtime(15, 55)
    )
    t_post_close_end = dtime(17, 0)

    if t < t_pre_market:
        return TradingPhase.OFFLINE
    if t_pre_market <= t < t_open:
        return TradingPhase.PRE_MARKET
    if t_open <= t < t_morning_drive_end:
        return TradingPhase.MORNING_DRIVE
    if t_morning_drive_end <= t < t_mid_morning_end:
        return TradingPhase.MID_MORNING
    if t_midday_freeze_start <= t < t_midday_freeze_end:
        return TradingPhase.MIDDAY_FREEZE
    if t_power_hour_start <= t < t_power_hour_end:
        return TradingPhase.POWER_HOUR
    if t_pre_close_start <= t < t_flatten_start:
        return TradingPhase.PRE_CLOSE
    if t_flatten_start <= t < t_flatten_end:
        return TradingPhase.MANDATORY_FLATTEN
    if t_flatten_end <= t < t_post_close_end:
        return TradingPhase.POST_CLOSE_REFLECTION
    return TradingPhase.OFFLINE


def is_entry_permitted(phase: TradingPhase, cfg: Optional[Dict[str, Any]] = None) -> Tuple[bool, float, str]:
    """
    Returns (permitted: bool, size_multiplier: float, reason: str).
    """
    gates = (cfg or {}).get("risk", {}).get("temporal_gates", {}) or {}
    power_hour_mult = float(gates.get("power_hour_sizing_multiplier", 0.50))

    if phase == TradingPhase.MORNING_DRIVE:
        return True, 1.0, "Morning drive: full sizing authorized"
    elif phase == TradingPhase.MID_MORNING:
        return True, 1.0, "Mid-morning: standard parameters authorized"
    elif phase == TradingPhase.POWER_HOUR:
        return True, power_hour_mult, f"Power hour: {int(power_hour_mult * 100)}% fractional sizing authorized"
    elif phase == TradingPhase.MIDDAY_FREEZE:
        return False, 0.0, "Midday freeze (11:30-14:00 EDT): entries locked against chop"
    elif phase == TradingPhase.PRE_CLOSE:
        return False, 0.0, "Pre-close unwind (15:35-15:50 EDT): new entries locked"
    elif phase == TradingPhase.MANDATORY_FLATTEN:
        return False, 0.0, "Mandatory flatten active: liquidation only"
    elif phase == TradingPhase.PRE_MARKET:
        return False, 0.0, "Pre-market session: entries not yet open"
    elif phase == TradingPhase.POST_CLOSE_REFLECTION:
        return False, 0.0, "Post-close reflection: market closed"
    else:
        return False, 0.0, "Outside trading hours: offline"
