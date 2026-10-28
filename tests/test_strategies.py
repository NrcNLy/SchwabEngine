"""
tests/test_strategies.py
========================
Unit tests for strategy execution and minimum bracket floors on 3x Leveraged ETFs (#140).
"""

from __future__ import annotations

from decimal import Decimal
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import MagicMock
import pytest

from execution.strategies import (
    SymbolState,
    RegimeMetrics,
    MarketRegime,
    update_trailing_stop,
    StrategyEngine,
)

_EDT = ZoneInfo("America/New_York")


def test_strategies_trailing_stop_floors_and_delayed_breakeven():
    """
    Verifies update_trailing_stop enforces:
    - 1.0% minimum stop distance floor
    - Delayed breakeven ratchet (+0.80% gain threshold)
    """
    class DummyPosition:
        def __init__(self, entry: float):
            self.entry_price = Decimal(str(entry))
            self.high_water_mark = Decimal(str(entry))
            self.stop_price = Decimal(str(round(entry * 0.98, 2)))
            self.ratchet_permitted = True

    pos = DummyPosition(100.0)

    # 1. Price at $100.40 (+0.40% gain < +0.80% threshold). Stop distance tiny (e.g. 0.20)
    # Floor must enforce stop distance >= 1.0% of HWM ($1.004 -> 99.40)
    stop, adj = update_trailing_stop(pos, current_price=100.40, stop_distance=0.20)
    # Stop distance is floored at 1.0% (100.40 * 0.01 = 1.004) -> stop is at 99.40
    assert stop <= 99.40
    assert (100.40 - stop) >= 1.00

    # 2. Candidate that would attempt to move stop to breakeven when gain is only +0.40%:
    # If stop_distance was 0, candidate would be 100.40 >= entry (100.00).
    # Delayed breakeven ratchet MUST prevent moving above or at breakeven when gain < +0.80%!
    stop_early, _ = update_trailing_stop(pos, current_price=100.40, stop_distance=0.0)
    assert stop_early < 100.00  # Cannot ratchet to or above breakeven

    # 3. Price advances to $102.00 (+2.00% gain > +0.80% threshold)
    stop2, adj2 = update_trailing_stop(pos, current_price=102.00, stop_distance=1.00)
    assert stop2 >= 100.00  # Breakeven / positive permitted because gain (+2.0%) > +0.80%
    assert adj2 is True


def test_strategies_orb_minimum_bracket_floors():
    """
    Verifies 15m_ORB enforces:
    - Min stop distance floor: at least 1.0% ($0.60 - $1.00 on LETFs)
    - Min Take-Profit 1 floor: at least 1.2%
    """
    cfg = {
        "regime": {"evaluation_interval_sec": 180},
        "risk": {"max_loss_per_trade": 50.00},
        "strategies": {
            "orb_15m": {
                "entry_rvol_min": 1.50,
                "orb_morning_rvol_hurdle": 2.00,
                "risk_reward_target": 2.5,
            },
            "vwap_mean_reversion": {"vwap_band_sigma": 2.2, "rsi_oversold_threshold": 28},
        },
    }
    ledger = MagicMock()
    mask = MagicMock()
    mask.is_excluded.return_value = False

    engine = StrategyEngine(cfg=cfg, ledger=ledger, mask=mask)
    engine.register_symbol("SOXL")

    now = datetime(2026, 10, 28, 9, 50, 0, tzinfo=_EDT)

    # Simulate sub-floor ORB range (entry = $100.05, orb_high = $100.00, orb_low = $99.90 -> only 0.15 or 0.15% risk)
    state, lock = engine._states["SOXL"]
    with lock:
        state.orb_high = 100.00
        state.orb_low = 99.90  # 0.15% risk without floor
        state.orb_finalized = True
        state.orb_signal_fired = False
        state.last_price = 100.05
        state.current_regime = MarketRegime.TREND_EXPANSION
        state.last_metrics = RegimeMetrics(
            timestamp=now,
            symbol="SOXL",
            ci=30.0,
            natr=2.0,
            rvol=2.5,
            vwap_slope_deg=5.0,
            atr14_dollars=0.50,
            vwap=100.00,
            vwap_upper_2_2=101.0,
            vwap_lower_2_2=99.0,
            rsi14=55.0,
            regime=MarketRegime.TREND_EXPANSION,
        )

    sig = engine._check_orb_signal(state, now)
    assert sig is not None
    # Stop distance must be floored to at least 1.0% of entry
    stop_dist = float(sig.entry_price - sig.stop_price)
    assert stop_dist >= round(float(sig.entry_price) * 0.010, 2)
    assert float(sig.stop_price) <= round(float(sig.entry_price) * 0.990, 2)

    # Target price must be floored to at least 1.2% above entry
    assert float(sig.target_price) >= round(float(sig.entry_price) * 1.012, 2)
