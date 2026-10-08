from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from core.engine import parse_fill, regime_code_from_ci, size_entry
from core.liquidity_policy import LiquidityPolicy, compute_buying_power
from core.universe_mask import UniverseExclusionMask
from datetime import date
from services.regime_summary import STALE_HOURS, build_regime_summary

D = Decimal


def _bp(settled="1000", nlv="1000"):
    return compute_buying_power(
        nlv=D(nlv), settled_cash=D(settled), unsettled_cash=D("0"),
        policy=LiquidityPolicy(), today=date(2026, 10, 5),
    )


class TestSizeEntry:
    def test_strategy_size_wins_when_smaller(self):
        assert size_entry(1, D("100"), _bp()) == 1

    def test_notional_ceiling_wins_when_smaller(self):
        assert size_entry(50, D("100"), _bp()) == 3  # max order 330

    def test_never_negative_or_fractional(self):
        assert size_entry(5, D("0"), _bp()) == 0
        assert size_entry(0, D("10"), _bp()) == 0
        assert size_entry(5, D("500"), _bp()) == 0  # one share exceeds the cap

    def test_no_buying_power_means_zero(self):
        assert size_entry(10, D("10"), _bp(settled="5")) == 0


class TestParseFill:
    def test_vwap_from_execution_legs(self):
        doc = {
            "filledQuantity": 3,
            "orderActivityCollection": [
                {"executionLegs": [{"quantity": 1, "price": 10.0}, {"quantity": 2, "price": 11.0}]},
            ],
        }
        filled, avg = parse_fill(doc)
        assert filled == 3 and avg == D("10.67")

    def test_missing_legs_gives_no_average(self):
        assert parse_fill({"filledQuantity": 2}) == (2, None)
        assert parse_fill({}) == (0, None)

    def test_garbage_legs_are_ignored(self):
        doc = {"filledQuantity": 1, "orderActivityCollection": [{"executionLegs": [{"quantity": "x", "price": 1}]}]}
        assert parse_fill(doc) == (1, None)


@pytest.mark.parametrize("ci,expected", [(10, "A"), (38.19, "A"), (38.2, "B"), (50, "B"), (61.8, "B"), (61.81, "C"), (90, "C")])
def test_regime_code_from_ci(ci, expected):
    assert regime_code_from_ci(ci) == expected


class TestRegimeSummary:
    NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)

    def test_plain_language_and_no_raw_payload(self):
        cfg = {
            "macro_bias": "Bullish", "target_regime": "A", "volatility_multiplier": 1.5,
            "_updated_at": (self.NOW - timedelta(hours=2)).isoformat(),
        }
        out = build_regime_summary(cfg, now=self.NOW, engine_regimes={"SOXL": "A", "TQQQ": "C"})
        assert out["bias"] == "bullish" and out["stale"] is False
        assert "Bullish bias" in out["headline"] and "High volatility" in out["headline"]
        assert [r["symbol"] for r in out["engine_regimes"]] == ["SOXL", "TQQQ"]
        assert "{" not in out["headline"]
        assert "does not change position sizing" in out["advisory_note"]

    def test_stale_and_empty_inputs_are_reported_honestly(self):
        old = {"macro_bias": "bearish", "_updated_at": (self.NOW - timedelta(hours=STALE_HOURS + 1)).isoformat()}
        assert build_regime_summary(old, now=self.NOW)["stale"] is True
        empty = build_regime_summary(None, now=self.NOW)
        assert empty["stale"] is True and "No macro view" in empty["headline"]


class TestUniverseMask:
    CFG = {
        "engine": {"symbols": ["SOXL", "TQQQ", "TNA"]},
        "portfolio_manager": {"exclude_symbols": ["SWVXX"]},
        "reconciliation": {"exclude_symbols": ["SWVXX", "SPAXX"]},
    }

    def test_swvxx_is_never_tradeable(self):
        mask = UniverseExclusionMask(self.CFG)
        assert mask.is_symbol_tradeable("SOXL") is True
        for sym in ("SWVXX", "SPAXX", "AAPL"):
            assert mask.is_symbol_tradeable(sym) is False
        assert "SWVXX" not in mask.universe
