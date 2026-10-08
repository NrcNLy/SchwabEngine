"""
scripts/test_hotpatch.py
Verifies that TradeSignal clamping works cleanly with dataclasses.replace
and does not raise FrozenInstanceError.
"""

import sys
import yaml
from datetime import datetime
from decimal import Decimal
import pytz

from execution.strategies import (
    StrategyEngine,
    TradeSignal,
    MarketRegime,
    RegimeMetrics,
    SymbolState,
)
from core.ledger import SettlementLedger
from core.universe_mask import UniverseExclusionMask

def test_signal_clamping():
    _EDT = pytz.timezone("America/New_York")
    with open("config/config.yaml") as f:
        cfg = yaml.safe_load(f)

    ledger = SettlementLedger(
        baseline_settled=Decimal("3753.75"),
        single_ticker_cap_pct=Decimal("0.33"),
        cash_buffer=Decimal("10.00"),
    )
    mask = UniverseExclusionMask(cfg)

    dispatched_signals = []
    def on_signal(sig: TradeSignal):
        dispatched_signals.append(sig)

    engine = StrategyEngine(cfg, ledger, mask)
    engine.set_signal_callback(on_signal)
    engine.register_symbol("TNA")

    state, lock = engine._states["TNA"]
    state.current_regime = MarketRegime.TREND_EXPANSION
    now = datetime(2026, 10, 7, 13, 5, 0, tzinfo=_EDT)

    state.orb_finalized = True
    state.orb_signal_fired = False
    state.orb_high = 57.00
    state.orb_low = 56.00
    state.last_price = 57.50

    state.last_metrics = RegimeMetrics(
        timestamp=now,
        symbol="TNA",
        ci=35.0,
        natr=0.14,
        rvol=2.0,
        vwap_slope_deg=10.0,
        atr14_dollars=0.50,
        vwap=57.00,
        vwap_upper_2_2=58.00,
        vwap_lower_2_2=56.00,
        rsi14=60.0,
        regime=MarketRegime.TREND_EXPANSION,
    )

    # Max single exposure = 3753.75 * 0.33 = 1238.73
    # Entry = 57.50, stop = 56.00, risk/shr = 1.50
    # max_risk = 50.00 -> quarter kelly shares = floor(50 / 1.50) = 33 shares
    # 33 * 57.50 = $1897.50 > $1238.73 cap
    # Max allowed by cap = floor(1238.73 / 57.50) = 21 shares!
    # Therefore clamped_quantity = 21 < 33!
    print("Executing _check_signals...")
    try:
        engine._check_signals(state, now)
        print("Success! _check_signals completed without error.")
    except Exception as exc:
        print(f"FAILED with exception: {type(exc).__name__}: {exc}")
        sys.exit(1)

    assert len(dispatched_signals) == 1, f"Expected 1 dispatched signal, got {len(dispatched_signals)}"
    sig = dispatched_signals[0]
    print(f"Dispatched signal: {sig.symbol} qty={sig.quantity} entry={sig.entry_price} stop={sig.stop_price}")
    assert sig.quantity == 21, f"Expected clamped quantity 21, got {sig.quantity}"
    print("Verification PASSED: Clamping from 33 to 21 shares succeeded using dataclasses.replace!")

if __name__ == "__main__":
    test_signal_clamping()
