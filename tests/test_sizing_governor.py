"""
tests/test_sizing_governor.py
=============================
Unit & Property-based tests for Bayesian Quarter-Kelly Sizing & Settlement Reserve Governor.
"""

from __future__ import annotations

from unittest.mock import MagicMock
import pytest

from execution.sizing_governor import BayesianSizingGovernor, SizingVerdict
from execution.order_manager import OrderManager


def test_cold_start_verification():
    """
    Test 1: Cold start verification.
    Verifies weakly informative Beta conjugate prior initializes at alpha_0=10, beta_0=10 (50% win rate),
    and Quarter-Kelly fraction initializes at f_qk ~ 2.0% - 3.5%.
    """
    governor = BayesianSizingGovernor(alpha_0=10.0, beta_0=10.0, win_loss_ratio=1.25)

    assert governor.alpha == 10.0
    assert governor.beta == 10.0
    assert governor.posterior_win_rate == pytest.approx(0.50, rel=1e-4)

    # With p_hat = 0.50 and b = 1.25:
    # f_star = (0.5 * 1.25 - 0.5) / 1.25 = 0.125 / 1.25 = 0.10 (10.0%)
    # f_qk = 0.25 * 0.10 = 0.025 (2.50%)
    assert 0.020 <= governor.kelly_fraction <= 0.035
    assert governor.kelly_fraction == pytest.approx(0.025, rel=1e-4)

    # Test Bayesian updating with trade outcomes
    governor.update_trade_outcome(is_win=True, win_loss_ratio=1.35)
    assert governor.alpha == 11.0
    assert governor.beta == 10.0
    assert governor.posterior_win_rate == pytest.approx(11.0 / 21.0, rel=1e-4)
    assert governor.kelly_fraction > 0.025

    # Test outcome updating on loss
    governor.update_trade_outcome(is_win=False)
    assert governor.alpha == 11.0
    assert governor.beta == 11.0
    assert governor.posterior_win_rate == pytest.approx(0.50, rel=1e-4)


def test_settlement_reserve_constraint():
    """
    Test 2: Settlement reserve constraint.
    Passes settled_cash = $500 with $5,500 NLV; verifies shares are clamped strictly
    to $500 without GFV risk and bottleneck_reason reports SETTLEMENT_RESERVE_CONSTRAINT.
    """
    governor = BayesianSizingGovernor(alpha_0=10.0, beta_0=10.0, win_loss_ratio=1.25)
    nlv = 5500.0
    settled_cash = 500.0
    price = 25.0
    atr = 0.50

    # Risk_dollar = 5500 * min(0.015, 0.025) = $82.50
    # Stop distance = 0.50 * 1.5 = $0.75
    # Raw ATR shares = 82.50 / 0.75 = 110 shares ($2,750.00 notional)
    # Bound 1 (0.33 * 5500 = $1815): 72 shares ($1,800.00 notional)
    # Settled cash limit ($500.00): floor(500 / 25) = 20 shares ($500.00 notional)
    verdict: SizingVerdict = governor.calculate_position_size(
        symbol="SOXL",
        price=price,
        atr=atr,
        settled_cash=settled_cash,
        total_nlv=nlv,
    )

    assert verdict.shares == 20
    assert verdict.notional_usd == 500.0
    assert verdict.notional_usd <= settled_cash
    assert verdict.bottleneck_reason == "SETTLEMENT_RESERVE_CONSTRAINT"

    # Verify zero GFV guarantee: notional never exceeds cleared settled funds
    assert verdict.notional_usd <= 500.00


def test_high_volatility_clamp():
    """
    Test 3: High-volatility clamp.
    Verifies ATR expansion compresses share count to preserve 1.5% max account risk.
    """
    governor = BayesianSizingGovernor(alpha_0=10.0, beta_0=10.0, win_loss_ratio=1.25)
    nlv = 5500.0
    settled_cash = 5000.0
    price = 25.0

    max_account_risk = nlv * 0.015  # $82.50 max allowable dollar risk

    # Baseline moderate volatility: ATR = 1.00
    verdict_low_vol = governor.calculate_position_size(
        symbol="SOXL",
        price=price,
        atr=1.00,
        settled_cash=settled_cash,
        total_nlv=nlv,
    )

    # Expanded severe volatility: ATR = 3.00 (3x expansion)
    verdict_high_vol = governor.calculate_position_size(
        symbol="SOXL",
        price=price,
        atr=3.00,
        settled_cash=settled_cash,
        total_nlv=nlv,
    )

    # Share count must compress significantly under ATR expansion
    assert verdict_high_vol.shares < verdict_low_vol.shares
    assert verdict_low_vol.shares == 55   # 82.50 / (1.0 * 1.5) = 55
    assert verdict_high_vol.shares == 18  # 82.50 / (3.0 * 1.5) = 18.33 -> 18

    # Both must strictly respect and preserve the 1.5% max account risk bound ($82.50)
    assert verdict_low_vol.risk_usd <= max_account_risk
    assert verdict_high_vol.risk_usd <= max_account_risk
    assert verdict_low_vol.risk_usd == pytest.approx(82.50, abs=0.01)
    assert verdict_high_vol.risk_usd == pytest.approx(81.00, abs=0.01)


def test_single_ticker_ceiling():
    """
    Test 4: Single-ticker ceiling.
    Verifies sizing never exceeds 33.0% of total NLV even under ultra-low ATR and abundant cash.
    """
    governor = BayesianSizingGovernor(alpha_0=10.0, beta_0=10.0, win_loss_ratio=1.25)
    nlv = 5563.0
    settled_cash = 10000.0
    price = 10.0
    atr = 0.02  # Ultra low volatility would demand ~2,781 shares ($27,810)

    verdict = governor.calculate_position_size(
        symbol="SOXL",
        price=price,
        atr=atr,
        settled_cash=settled_cash,
        total_nlv=nlv,
    )

    # 33% single-ticker cap on $5,563 base is $1,835.79 -> 183 shares @ $10.00
    max_notional_cap = 0.33 * nlv
    assert verdict.notional_usd <= max_notional_cap
    assert verdict.shares == 183
    assert verdict.notional_usd == 1830.00
    assert verdict.bottleneck_reason == "SINGLE_TICKER_CAP"


def test_hard_dollar_ceiling_clamp():
    """
    Tests Bound 2 hard dollar ceiling clamp ($1,836.00 or $990.00).
    """
    # Test $990.00 sandbox ceiling
    gov_sandbox = BayesianSizingGovernor(alpha_0=10.0, beta_0=10.0, win_loss_ratio=1.25)
    verdict_sandbox = gov_sandbox.calculate_position_size(
        symbol="TQQQ",
        price=10.0,
        atr=0.01,
        settled_cash=2000.0,
        total_nlv=1000.0,  # <= 1500 triggers $990.00 clamp
    )
    # Bound 1: 0.33 * 1000 = $330.00, Bound 2 = $990.00
    assert verdict_sandbox.notional_usd <= 330.00

    # Test explicit $1,836.00 hard dollar ceiling with cap_pct = 1.0
    gov_explicit = BayesianSizingGovernor(
        single_ticker_cap_pct=1.0,
        hard_dollar_ceiling=1836.00,
    )
    verdict_hard = gov_explicit.calculate_position_size(
        symbol="SOXL",
        price=10.0,
        atr=0.01,
        settled_cash=10000.0,
        total_nlv=5563.0,
    )
    assert verdict_hard.shares == 183  # floor(1836 / 10) = 183
    assert verdict_hard.notional_usd == 1830.00
    assert verdict_hard.bottleneck_reason == "HARD_DOLLAR_CEILING"


def test_edge_case_negative_kelly_halts_trading():
    """
    Verifies that severe consecutive losses push expected win rate below breakeven,
    causing Kelly fraction to become 0 and safely halting share sizing.
    """
    governor = BayesianSizingGovernor(alpha_0=10.0, beta_0=10.0, win_loss_ratio=1.0)
    # Incur consecutive losses
    for _ in range(15):
        governor.update_trade_outcome(is_win=False)

    # Alpha=10, Beta=25 -> p_hat = 10 / 35 ~ 28.57%
    # With b=1.0, edge = 0.2857 - 0.7143 < 0 -> f_star = 0.0
    assert governor.posterior_win_rate < 0.30
    assert governor.full_kelly == 0.0
    assert governor.kelly_fraction == 0.0

    verdict = governor.calculate_position_size(
        symbol="SOXL",
        price=25.0,
        atr=1.0,
        settled_cash=5000.0,
        total_nlv=5500.0,
    )
    assert verdict.shares == 0
    assert verdict.notional_usd == 0.0
    assert verdict.bottleneck_reason == "KELLY_NO_EDGE"


def test_order_manager_wire_and_dynamic_sizing(tmp_path):
    """
    Verifies OrderManager integration:
    1. calculate_position_size delegates to BayesianSizingGovernor.
    2. place_limit_buy dynamically sizes when quantity is None.
    3. record_trade_execution updates posterior alpha, beta, and Kelly in trades.db.
    """
    from core.trade_store import SQLiteTradeStore

    db_path = tmp_path / "trades.db"
    store = SQLiteTradeStore(db_path=db_path)

    ledger_mock = MagicMock()
    ledger_mock.settled_cash = 2000.0
    ledger_mock.nlv = 5500.0
    ledger_mock.trade_store = store

    rest_mock = MagicMock()
    rest_mock.get_account_numbers.return_value = [
        {"accountNumber": "123456015", "hashValue": "TEST_HASH_015"}
    ]
    fake_resp = MagicMock()
    fake_resp.headers = {"Location": "https://api.schwabapi.com/trader/v1/accounts/xyz/orders/112233"}
    rest_mock.place_order.return_value = fake_resp

    cfg = {"account": {"required_suffix": "015", "blocked_keywords": []}}
    om = OrderManager(rest_mock, ledger_mock, cfg)
    om.initialize_firewall()

    # 1. Verify calculate_position_size
    verdict = om.calculate_position_size("SOXL", price=25.0, atr=1.0)
    assert isinstance(verdict, SizingVerdict)
    assert verdict.shares > 0
    assert verdict.bottleneck_reason in ("VOLATILITY_RISK_LIMIT", "SINGLE_TICKER_CAP", "NONE")

    # 2. Verify place_limit_buy with dynamic sizing (quantity=None, atr=1.0)
    order_id = om.place_limit_buy("SOXL", quantity=None, limit_price=25.0, atr=1.0)
    assert order_id == "112233"
    rest_mock.place_order.assert_called_once()
    placed_body = rest_mock.place_order.call_args[0][1]
    assert placed_body["orderLegCollection"][0]["quantity"] == verdict.shares

    # 3. Verify record_trade_execution updates trades.db with posterior alpha, beta, and kelly
    tid = om.record_trade_execution(
        symbol="SOXL",
        side="BUY",
        quantity=verdict.shares,
        price=25.0,
        regime="A",
    )
    assert tid is not None

    # Verify SQLite record has posterior parameters
    today_trades = store.fetch_trades_today()
    assert len(today_trades) == 1
    t = today_trades[0]
    assert t["symbol"] == "SOXL"
    assert t["posterior_alpha"] == pytest.approx(10.0)
    assert t["posterior_beta"] == pytest.approx(10.0)
    assert t["kelly_fraction"] == pytest.approx(0.025, abs=0.005)


def test_sizing_concurrent_collision_prevention():
    """
    Test Pure Tactical Concurrent Sizing Collision Prevention (#140):
    - When evaluating a new entry signal, calculate remaining_cash_budget = total_tactical_budget - current_open_satellite_notional.
    - Prevent concurrent orders from independently sizing against total account NLV.
    - If remaining_cash_budget < min_position_size, reject additional entries.
    """
    governor = BayesianSizingGovernor(alpha_0=10.0, beta_0=10.0, win_loss_ratio=1.25)
    nlv = 4400.90
    settled_cash = 4400.90

    # Total tactical budget under Goldilocks (75% gross capacity) = $3,300.675 -> $3,300.68
    total_tactical_budget = 4400.90 * 0.75  # $3,300.675
    
    # 1. First order evaluated (e.g. SOXL): zero open satellite notional
    open_satellite_notional = 0.0
    remaining_budget_1 = total_tactical_budget - open_satellite_notional  # $3,300.68
    verdict_1 = governor.calculate_position_size(
        symbol="SOXL",
        price=25.0,
        atr=0.50,
        settled_cash=settled_cash,
        total_nlv=nlv,
        remaining_budget=remaining_budget_1,
        min_position_size=50.0,
    )
    assert verdict_1.shares > 0
    # Bound 1: 0.33 * 4400.90 = $1452.29 -> 58 shares @ $25 = $1450.00
    assert verdict_1.shares == 58
    assert verdict_1.notional_usd == 1450.00

    # 2. Second concurrent order evaluated (e.g. TQQQ) while SOXL committed/open:
    open_satellite_notional = verdict_1.notional_usd  # $1,450.00
    remaining_budget_2 = total_tactical_budget - open_satellite_notional  # $1,850.68
    verdict_2 = governor.calculate_position_size(
        symbol="TQQQ",
        price=70.0,
        atr=1.20,
        settled_cash=settled_cash,
        total_nlv=nlv,
        remaining_budget=remaining_budget_2,
        min_position_size=50.0,
    )
    assert verdict_2.shares > 0
    # Sizing capped by remaining_budget_2 ($1,850.68) rather than full account NLV
    assert verdict_2.notional_usd <= remaining_budget_2

    # 3. Third concurrent order when budget is nearly exhausted (< min_position_size $50.0)
    open_satellite_notional = total_tactical_budget - 30.0  # only $30 remaining
    remaining_budget_3 = total_tactical_budget - open_satellite_notional
    verdict_3 = governor.calculate_position_size(
        symbol="TNA",
        price=40.0,
        atr=1.0,
        settled_cash=settled_cash,
        total_nlv=nlv,
        remaining_budget=remaining_budget_3,
        min_position_size=50.0,
    )
    assert verdict_3.shares == 0
    assert verdict_3.notional_usd == 0.0
    assert verdict_3.bottleneck_reason == "REMAINING_BUDGET_EXHAUSTED"


def test_portfolio_pure_tactical_budget():
    """
    Test Pure Tactical Portfolio Regime Capacities (#140):
    - Goldilocks: 75% gross tactical capacity ($3,300.68 on $4,400.90).
    - Uncertainty: 35% gross tactical capacity ($1,540.32 on $4,400.90).
    - Macro Shock: 0% gross tactical capacity ($0.00 on $4,400.90).
    """
    from decimal import Decimal
    from core.engine import _EngineBase
    from core.ledger import SettlementLedger

    ledger = SettlementLedger(baseline_settled=Decimal("4400.90"))
    cfg = {
        "tactical_satellite_max_capacity": 0.75,
        "regime_satellite_targets": {
            "Goldilocks": {"target": 0.75, "ntz_band": [0.65, 0.80]},
            "Uncertainty": {"target": 0.35, "ntz_band": [0.30, 0.40]},
            "Shock": {"target": 0.0, "ntz_band": [0.0, 0.0]},
        },
    }

    class DummyCtx:
        pass

    engine = _EngineBase(
        ctx=DummyCtx(),
        cfg=cfg,
        ledger=ledger,
        settings=None,
    )

    # Goldilocks
    rem, total, open_notional = engine.calculate_remaining_tactical_budget("Goldilocks")
    assert total == Decimal("3300.68")
    assert rem == Decimal("3300.68")
    assert open_notional == Decimal("0.00")

    # Uncertainty
    rem_u, total_u, _ = engine.calculate_remaining_tactical_budget("Uncertainty")
    assert total_u == Decimal("1540.32")
    assert rem_u == Decimal("1540.32")

    # Macro Shock
    rem_s, total_s, _ = engine.calculate_remaining_tactical_budget("Shock")
    assert total_s == Decimal("0.00")
    assert rem_s == Decimal("0.00")
