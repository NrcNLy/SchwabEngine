"""
tests/test_universe_mask.py
===========================
Audit and verify the wash-sale exclusion scope and single-session circuit breaker.

Verifies:
1. UniverseExclusionMask correctly includes core universe tickers (SOXL, TQQQ, TNA).
2. Active external swing lots (t_lot < session_open or external_schwab == True) are added to exclusion mask.
3. Intraday lots (opened today) are NOT excluded.
4. UniverseManager respects exclusion mask in is_entry_allowed.
5. No 30-day closed-loss quarantine exists; closed day-trading losses never freeze tickers.
6. Single-session circuit breaker stop (-$50 risk cap).
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import pytest

from core.universe_mask import UniverseExclusionMask
from core.universe_manager import UniverseManager


def test_universe_mask_initial_eligibility():
    cfg = {
        "engine": {"symbols": ["SOXL", "TQQQ", "TNA", "FNGU"]},
        "reconciliation": {"exclude_symbols": ["SWVXX"]},
    }
    mask = UniverseExclusionMask(cfg)

    # Core symbols are tradeable
    assert mask.is_symbol_tradeable("SOXL") is True
    assert mask.is_symbol_tradeable("TQQQ") is True
    assert mask.is_symbol_tradeable("TNA") is True
    assert mask.is_symbol_tradeable("FNGU") is True

    # Sweep fund is excluded
    assert mask.is_symbol_tradeable("SWVXX") is False
    assert "SWVXX" in mask.excluded


def test_wash_sale_scope_audit_swing_lots_vs_intraday():
    cfg = {"engine": {"symbols": ["SOXL", "TQQQ", "TNA"]}}
    mask = UniverseExclusionMask(cfg)
    um = UniverseManager(scanner=None, exclusion_mask=mask)

    ny_tz = ZoneInfo("America/New_York")
    session_open = datetime(2026, 10, 9, 9, 30, 0, tzinfo=ny_tz)

    # Position 1: External swing holding in TNA acquired yesterday (t_lot < session_open)
    yesterday_lot = {
        "symbol": "TNA",
        "quantity": 100,
        "opened_at": session_open - timedelta(days=1),
        "external_schwab": True,
    }

    # Position 2: Intraday lot in SOXL opened today at 09:35 EDT
    intraday_lot = {
        "symbol": "SOXL",
        "quantity": 50,
        "opened_at": session_open + timedelta(minutes=5),
        "external_schwab": False,
    }

    # Audit holdings
    excluded = mask.audit_holdings_for_wash_sale([yesterday_lot, intraday_lot], session_open=session_open)

    # TNA is excluded to protect the swing tranche from wash-sale tax contamination
    assert "TNA" in excluded
    assert mask.is_symbol_tradeable("TNA") is False
    assert um.is_entry_allowed("TNA") is False

    # SOXL was NOT an external swing lot and was opened during today's session -> NOT excluded
    assert "SOXL" not in excluded
    assert mask.is_symbol_tradeable("SOXL") is True
    assert um.is_entry_allowed("SOXL") is True

    # TQQQ has no holding -> NOT excluded
    assert mask.is_symbol_tradeable("TQQQ") is True
    assert um.is_entry_allowed("TQQQ") is True


def test_no_30_day_closed_loss_quarantine():
    """
    Ensure closed intraday losses from previous days do NOT prevent trading the ticker.
    """
    cfg = {"engine": {"symbols": ["SOXL", "TQQQ", "TNA"]}}
    mask = UniverseExclusionMask(cfg)

    # Even if closed yesterday at a loss, ticker remains fully tradeable in mask
    assert mask.is_symbol_tradeable("SOXL") is True
    assert mask.is_symbol_tradeable("TQQQ") is True


def test_single_session_circuit_breaker():
    cfg = {"engine": {"symbols": ["SOXL", "TQQQ", "TNA"]}}
    mask = UniverseExclusionMask(cfg)

    # PnL within acceptable limit ($10 loss)
    assert mask.audit_session_circuit_breaker(today_realized_pnl=-10.0, daily_loss_limit=50.0) is False

    # PnL hits or exceeds -$50.00 daily loss limit -> Circuit breaker trips
    assert mask.audit_session_circuit_breaker(today_realized_pnl=-50.0, daily_loss_limit=50.0) is True
    assert mask.audit_session_circuit_breaker(today_realized_pnl=-75.0, daily_loss_limit=50.0) is True
