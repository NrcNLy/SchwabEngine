"""
tests/test_midday_optimizer.py
==============================
Tests for workers/midday_optimizer.py:
1. Walk-forward Hurst exponent and Choppiness evaluation.
2. Anti-overfitting regime classification.
3. Policy overrides (RVOL hurdle 1.65x, size scale 0.35x when H <= 0.50).
4. Atomic emission to state/dynamic_policy.json.
"""

import numpy as np
import pytest

from core.baselines_store import SQLiteBaselinesStore
from core.ipc import read_dynamic_policy
from workers.midday_optimizer import MiddayRegimeOptimizer


def test_hurst_choppiness_evaluation(tmp_path):
    store = SQLiteBaselinesStore(db_path=tmp_path / "test_mb.db")
    optimizer = MiddayRegimeOptimizer(store=store)

    # Generate synthetic geometric Brownian motion
    np.random.seed(42)
    n = 200
    returns = np.random.normal(0, 0.01, n)
    prices = 100.0 * np.exp(np.cumsum(returns))

    bars = [
        {
            "timestamp": 1728414000000 + i * 60000,
            "open": float(prices[i]),
            "high": float(prices[i] * 1.002),
            "low": float(prices[i] * 0.998),
            "close": float(prices[i]),
            "volume": 1000,
        }
        for i in range(n)
    ]

    h, ci, cls_name = optimizer.evaluate_symbol_regime("SOXL", recent_bars=bars)
    assert 0.0 <= h <= 1.0
    assert 0.0 <= ci <= 100.0
    assert cls_name in ("HIGH_NOISE_CHOP", "RANDOM_WALK", "TREND_EXPANSION")


def test_policy_overrides_when_choppy():
    optimizer = MiddayRegimeOptimizer()
    policy = optimizer.compute_policy_overrides(hurst_h=0.48, ci=62.0, classification="HIGH_NOISE_CHOP")

    assert policy["effective_session"] == "POWER_HOUR"
    assert policy["regime_classification"] == "HIGH_NOISE_CHOP"
    assert policy["overrides"]["power_hour_rvol_hurdle"] == 1.65
    assert policy["overrides"]["power_hour_size_scale"] == 0.35
    assert policy["overrides"]["ratchet_permitted"] is False


def test_policy_overrides_when_trending():
    optimizer = MiddayRegimeOptimizer()
    policy = optimizer.compute_policy_overrides(hurst_h=0.62, ci=32.0, classification="TREND_EXPANSION")

    assert policy["effective_session"] == "POWER_HOUR"
    assert policy["regime_classification"] == "TREND_EXPANSION"
    assert policy["overrides"]["power_hour_rvol_hurdle"] == 1.25
    assert policy["overrides"]["power_hour_size_scale"] == 0.50
    assert policy["overrides"]["ratchet_permitted"] is True


def test_run_optimization_writes_dynamic_policy(tmp_path, monkeypatch):
    pol_file = tmp_path / "dynamic_policy.json"
    import core.ipc
    monkeypatch.setattr(core.ipc, "DYNAMIC_POLICY_FILE", pol_file)

    store = SQLiteBaselinesStore(db_path=tmp_path / "test_mb.db")
    optimizer = MiddayRegimeOptimizer(store=store, symbols=["SOXL"])

    policy = optimizer.run_optimization()
    assert policy is not None

    written = read_dynamic_policy(pol_file)
    assert written is not None
    assert written["effective_session"] == "POWER_HOUR"
