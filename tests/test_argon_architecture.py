"""
tests/test_argon_architecture.py
================================
Comprehensive verification suite for Argon Analysis & Dual-Sleeve Robo-Core Architecture (#87 / #88 / #92).

Verifies:
1. Pure Scale-Invariance across account tiers ($5k to $100k+) without hardcoded dollar caps.
2. Optional settled cash gate bypass for instantaneous Dual-Sleeve reallocation.
3. 3x LETF Volatility-Drag Filter (phi_drag) and Correlation-Penalized Sub-Weights (omega_j).
4. Stock-vs-Flow Order Deltas (Delta Q_j) and paired Robo-Core sweep with lot rollover.
5. Continuous 3-State HMM Meta-Governor transitions and T_kk = 0.92 self-persistence hysteresis.
6. Unchained Leland No-Trade Zone dual-sleeve paired rebalancing (zero cash drag).
7. Strategy D: Core-Accelerated Drawdown Recovery boosted by 1x Robo-Core returns r_core(t).
8. FINRA Rule 4210(f)(8)(A) 3x LETF maintenance margin (75% intraday / 90% overnight).
9. Two-Stage EOD De-Levering (15:50 ET primary trim + 19:45 ET extended-hours backstop).
10. MultiRateStateSynchronizer Hot, Warm (mid-session hot boot), and Cold planes.
"""

from __future__ import annotations

import math
from typing import Dict
import pytest

from data.multi_rate_sync import (
    MultiRateStateSynchronizer,
    ColdMacroState,
    DualSleeveState,
    HotQuoteUpdate,
)
from execution.risk_manager import (
    MetaGovernorHMM,
    DualSleeveRouter,
    RiskEngine,
    LETF_3X_SYMBOLS,
    CORE_1X_SYMBOLS,
)
from execution.sizing_governor import (
    BayesianSizingGovernor,
    MultiAssetOrderDeltaVerdict,
    SizingVerdict,
)


# =============================================================================
# 1. SCALE-INVARIANT SIZING & SETTLED CASH GATE BYPASS
# =============================================================================

def test_scale_invariance_across_account_tiers():
    """
    Verifies that when hard_dollar_ceiling is None, sizing scales purely as a fractional
    function of Net Liquidation Value (single_ticker_cap_pct * nlv) across small ($5k),
    medium ($15k), and large ($100k+) accounts without hitting legacy $1,836 caps.
    """
    governor = BayesianSizingGovernor(
        alpha_0=10.0,
        beta_0=10.0,
        win_loss_ratio=1.25,
        single_ticker_cap_pct=0.33,
        hard_dollar_ceiling=None,
    )

    price = 50.0
    atr = 0.10  # Low ATR so volatility risk limit doesn't artificially bind below 33%

    # Small account: $5,000 NLV -> 33% cap = $1,650 -> 33 shares ($1,650)
    verdict_5k = governor.calculate_position_size(
        symbol="SOXL",
        price=price,
        atr=atr,
        settled_cash=10000.0,
        total_nlv=5000.0,
    )
    assert verdict_5k.shares == 33
    assert verdict_5k.notional_usd == 1650.0
    assert verdict_5k.bottleneck_reason == "SINGLE_TICKER_CAP"

    # Medium account: $15,000 NLV -> 33% cap = $4,950 -> 99 shares ($4,950)
    verdict_15k = governor.calculate_position_size(
        symbol="SOXL",
        price=price,
        atr=atr,
        settled_cash=20000.0,
        total_nlv=15000.0,
    )
    assert verdict_15k.shares == 99
    assert verdict_15k.notional_usd == 4950.0
    # Must exceed the legacy $1,836 cap!
    assert verdict_15k.notional_usd > 1836.00
    assert verdict_15k.bottleneck_reason == "SINGLE_TICKER_CAP"

    # Large account: $100,000 NLV -> 33% cap = $33,000 -> 660 shares ($33,000)
    verdict_100k = governor.calculate_position_size(
        symbol="SOXL",
        price=price,
        atr=atr,
        settled_cash=100000.0,
        total_nlv=100000.0,
    )
    assert verdict_100k.shares == 660
    assert verdict_100k.notional_usd == 33000.0
    assert verdict_100k.notional_usd > 1836.00
    assert verdict_100k.bottleneck_reason == "SINGLE_TICKER_CAP"


def test_enforce_settled_cash_gate_bypass():
    """
    Verifies that enforce_settled_cash_gate=False permits sizing beyond settled cash
    for instantaneous Dual-Sleeve routing (where Core sales fund entries immediately).
    """
    governor = BayesianSizingGovernor(
        alpha_0=10.0,
        beta_0=10.0,
        win_loss_ratio=1.25,
        single_ticker_cap_pct=0.33,
    )

    nlv = 50000.0
    settled_cash = 200.0  # Minimal settled cash
    price = 50.0
    atr = 1.0

    # 1. Enforcing settled cash gate clamps shares to floor(200 / 50) = 4 shares
    verdict_gated = governor.calculate_position_size(
        symbol="SOXL",
        price=price,
        atr=atr,
        settled_cash=settled_cash,
        total_nlv=nlv,
        enforce_settled_cash_gate=True,
    )
    assert verdict_gated.shares == 4
    assert verdict_gated.bottleneck_reason == "SETTLEMENT_RESERVE_CONSTRAINT"

    # 2. Bypassing settled cash gate allows full allocation
    verdict_ungated = governor.calculate_position_size(
        symbol="SOXL",
        price=price,
        atr=atr,
        settled_cash=settled_cash,
        total_nlv=nlv,
        enforce_settled_cash_gate=False,
    )
    assert verdict_ungated.shares > 4
    assert verdict_ungated.bottleneck_reason != "SETTLEMENT_RESERVE_CONSTRAINT"


# =============================================================================
# 2. 3X VOLATILITY DRAG & CORRELATION-PENALIZED SUB-WEIGHTS
# =============================================================================

def test_letf_volatility_drag_multiplier():
    """
    Verifies that phi_drag shrinks as Yang-Zhang volatility expands, protecting
    the account from 3x leveraged ETF geometric variance drain.
    """
    governor = BayesianSizingGovernor()

    # Low volatility: sigma_yz = 0.015 (1.5%) -> minimal drag, multiplier near 0.75 - 1.0
    drag_low = governor.compute_letf_drag_multiplier(sigma_yz=0.015, leverage=3.0, mu_hat=0.15)
    assert 0.70 <= drag_low <= 1.0

    # High volatility: sigma_yz = 0.15 (15%) -> high variance drain, multiplier drops
    drag_high = governor.compute_letf_drag_multiplier(sigma_yz=0.15, leverage=3.0, mu_hat=0.15)
    assert drag_high < drag_low
    assert drag_high < 0.60

    # Extreme volatility: sigma_yz = 0.35 -> severe drag clamped at 0.0
    drag_extreme = governor.compute_letf_drag_multiplier(sigma_yz=0.35, leverage=3.0, mu_hat=0.15)
    assert drag_extreme == 0.0


def test_correlation_penalized_sub_weights():
    """
    Verifies sub-weights penalize intra-portfolio correlation and excessive volatility.
    """
    governor = BayesianSizingGovernor()
    vols = {"SOXL": 0.03, "TQQQ": 0.02, "TNA": 0.04}
    correlations = {
        "SOXL": {"SOXL": 1.0, "TQQQ": 0.85, "TNA": 0.70},
        "TQQQ": {"SOXL": 0.85, "TQQQ": 1.0, "TNA": 0.65},
        "TNA": {"SOXL": 0.70, "TQQQ": 0.65, "TNA": 1.0},
    }

    sub_weights = governor.compute_sub_weights(volatilities=vols, correlations=correlations)
    assert pytest.approx(sum(sub_weights.values()), rel=1e-3) == 1.0
    # TQQQ has lowest volatility and moderate correlation -> highest sub-weight
    assert sub_weights["TQQQ"] > sub_weights["SOXL"]
    assert sub_weights["TQQQ"] > sub_weights["TNA"]


def test_stock_vs_flow_order_delta_and_core_sweep():
    """
    Verifies calculation of stock-vs-flow order deltas (Delta Q_j) and the paired
    opposing Robo-Core funding/sweep notional (Delta C_usd = -sum_j Delta Q_j * P_j).
    """
    governor = BayesianSizingGovernor()
    nlv = 100000.0
    held = {"SOXL": 0, "TQQQ": 0, "TNA": 0}
    prices = {"SOXL": 50.0, "TQQQ": 80.0, "TNA": 40.0}
    vols = {"SOXL": 0.025, "TQQQ": 0.018, "TNA": 0.030}

    verdict: MultiAssetOrderDeltaVerdict = governor.calculate_multi_asset_order_delta(
        nlv=nlv,
        held_shares=held,
        prices=prices,
        volatilities=vols,
        target_satellite_weight=0.15,
    )

    assert verdict.satellite_notional_delta > 0
    # Core notional sweep is exactly opposing: Delta C = -Delta S
    assert verdict.core_notional_sweep_usd == pytest.approx(-verdict.satellite_notional_delta, abs=0.05)
    for sym, dq in verdict.delta_shares.items():
        assert dq == verdict.target_shares[sym] - held[sym]


def test_small_account_lot_quantization_rollover():
    """
    Verifies that on small accounts ($5k), integer lot rollover allocates residual
    unallocated cash into the highest sub-weight asset instead of stranding capital.
    """
    governor = BayesianSizingGovernor()
    nlv = 5000.0
    held = {"SOXL": 0, "TQQQ": 0, "TNA": 0}
    prices = {"SOXL": 45.0, "TQQQ": 78.0, "TNA": 50.0}
    vols = {"SOXL": 0.025, "TQQQ": 0.018, "TNA": 0.030}

    verdict = governor.calculate_multi_asset_order_delta(
        nlv=nlv,
        held_shares=held,
        prices=prices,
        volatilities=vols,
        target_satellite_weight=0.15,
    )

    # Rollover ensures residual unallocated cash is minimized
    assert verdict.lot_quantization_rollover_usd < max(prices.values())


# =============================================================================
# 3. CONTINUOUS 3-STATE HMM META-GOVERNOR
# =============================================================================

def test_continuous_3_state_hmm_transitions_and_hysteresis():
    """
    Verifies Bayesian filtering over ColdMacroState updates with T_kk = 0.92 hysteresis.
    """
    hmm = MetaGovernorHMM(persistence=0.92)

    # 1. Benign Goldilocks telemetry: Z_s = +1.5, S_nlp = +0.85, CS = 310 bps
    p_gold = hmm.update((1.5, 0.85, 310.0))
    assert hmm.get_dominant_regime() == "Goldilocks"
    assert hmm.pi_goldilocks > 0.85

    # 2. Transient neutral blip: hysteresis prevents abrupt flip to Shock
    p_blip = hmm.update((0.0, 0.0, 420.0))
    # Self-persistence cushions the transition
    assert hmm.pi_goldilocks > 0.15

    # 3. Severe Macro Shock: Z_s = -2.5, S_nlp = -0.90, CS = 640 bps
    p_shock = hmm.update((-2.5, -0.90, 640.0))
    assert hmm.get_dominant_regime() == "Macro Shock"
    assert hmm.pi_shock > 0.80

    # Under shock, target satellite weight is severely throttled
    w_target = hmm.calculate_target_satellite_weight(w_base=0.15)
    assert w_target < 0.03


# =============================================================================
# 4. DUAL-SLEEVE ROUTER & UNCHAINED LELAND NO-TRADE ZONE
# =============================================================================

def test_dual_sleeve_router_harvest_and_sweep():
    """
    Verifies that when active satellite weight breaches the upper Leland NTZ band,
    paired atomic orders liquidate excess satellite and sweep 100% of freed notional
    into the 1x Robo-Core sleeve (SPY) with zero cash drag.
    """
    router = DualSleeveRouter(tau_band=0.03, core_symbol="SPY")
    nlv = 100000.0

    # Active satellite holding: $22,000 (w = 0.22)
    sat_positions = {
        "SOXL": {"quantity": 200, "current_price": 50.0, "market_value": 10000.0},
        "TQQQ": {"quantity": 150, "current_price": 80.0, "market_value": 12000.0},
    }
    # Target = 0.15, Upper band = 0.18 -> w(0.22) > 0.18 -> HARVEST_TO_CORE
    plan = router.plan_dual_sleeve_reallocation(
        nlv=nlv,
        satellite_positions=sat_positions,
        target_satellite_weight=0.15,
        core_price=585.0,
    )

    assert plan.action == "HARVEST_TO_CORE"
    assert plan.active_satellite_weight == 0.22
    assert plan.band_upper == 0.18

    # Orders must contain: SELL Satellite leg(s) + BUY Core leg (SPY)
    sells = [o for o in plan.orders if o.side == "SELL"]
    buys = [o for o in plan.orders if o.side == "BUY" and o.symbol == "SPY"]
    assert len(sells) >= 1
    assert len(buys) == 1

    # Exact dollar conservation: 100% of freed satellite notional is swept into Core
    freed_sat = abs(plan.satellite_notional_delta)
    core_bought = plan.core_notional_delta
    assert pytest.approx(freed_sat, abs=600.0) == core_bought  # Integer lot difference < 1 SPY share ($585)
    assert plan.instantaneous_settlement_bypass is True


def test_dual_sleeve_router_trim_core_and_dip_buy():
    """
    Verifies that when satellite weight drops below the lower Leland band,
    the router trims Robo-Core (SPY) to fund satellite re-entry with 0h settlement wait.
    """
    router = DualSleeveRouter(tau_band=0.03, core_symbol="SPY")
    nlv = 100000.0

    # Active satellite holding: $8,000 (w = 0.08)
    sat_positions = {
        "SOXL": {"quantity": 80, "current_price": 50.0, "market_value": 4000.0},
        "TQQQ": {"quantity": 50, "current_price": 80.0, "market_value": 4000.0},
    }
    # Target = 0.15, Lower band = 0.12 -> w(0.08) < 0.12 -> TRIM_CORE_TO_SATELLITE
    plan = router.plan_dual_sleeve_reallocation(
        nlv=nlv,
        satellite_positions=sat_positions,
        target_satellite_weight=0.15,
        core_price=585.0,
        trend_gate_active=True,
    )

    assert plan.action == "TRIM_CORE_TO_SATELLITE"
    assert plan.active_satellite_weight == 0.08
    assert plan.band_lower == 0.12

    # Orders must contain: SELL Core leg (SPY) + BUY Satellite legs
    core_sells = [o for o in plan.orders if o.side == "SELL" and o.symbol == "SPY"]
    sat_buys = [o for o in plan.orders if o.side == "BUY"]
    assert len(core_sells) == 1
    assert len(sat_buys) >= 1


def test_dual_sleeve_router_inside_leland_ntz():
    """
    Verifies that when satellite weight is within tolerance bounds, no trade is generated.
    """
    router = DualSleeveRouter(tau_band=0.03, core_symbol="SPY")
    nlv = 100000.0

    # Active satellite holding: $15,000 (w = 0.15) exactly at target inside [0.12, 0.18]
    sat_positions = {
        "SOXL": {"quantity": 150, "current_price": 50.0, "market_value": 7500.0},
        "TQQQ": {"quantity": 93, "current_price": 80.0, "market_value": 7440.0},
    }
    plan = router.plan_dual_sleeve_reallocation(
        nlv=nlv,
        satellite_positions=sat_positions,
        target_satellite_weight=0.15,
    )
    assert plan.action == "NO_TRADE_ZONE"
    assert len(plan.orders) == 0


# =============================================================================
# 5. CORE-ACCELERATED STRATEGY D DRAWDOWN RECOVERY
# =============================================================================

def test_core_accelerated_strategy_d_drawdown_recovery():
    """
    Proves that positive returns r_core(t) > 0 earned on swept satellite capital
    sheltered in 1x Robo-Core accelerate high-water-mark decay (lambda_hwm_eff > lambda_0)
    and heal the drawdown damper D_sat faster than holding inert cash.
    """
    eng = RiskEngine(account_base=10000.0)

    # Establish initial peak satellite equity at $10,000
    damper_init = eng.update_core_accelerated_drawdown(current_satellite_equity=10000.0, r_core=0.0)
    assert damper_init == 1.0

    # Satellite drops to $9,200 (8% drawdown) under static cash (r_core = 0.0)
    eng_cash = RiskEngine(account_base=10000.0)
    eng_cash.update_core_accelerated_drawdown(current_satellite_equity=10000.0, r_core=0.0)
    damper_cash = eng_cash.update_core_accelerated_drawdown(
        current_satellite_equity=9200.0,
        r_core=0.0,
        w_sat=0.05,
        w_base=0.15,
    )

    # Satellite drops to $9,200 but swept capital in Robo-Core earns +4.5% beta gain
    eng_core = RiskEngine(account_base=10000.0)
    eng_core.update_core_accelerated_drawdown(current_satellite_equity=10000.0, r_core=0.0)
    damper_core = eng_core.update_core_accelerated_drawdown(
        current_satellite_equity=9200.0,
        r_core=0.045,
        w_sat=0.05,
        w_base=0.15,
    )

    # Core-accelerated recovery heals damper value faster than static cash
    assert damper_core > damper_cash
    assert eng_core.cumulative_core_heal > 0.0


# =============================================================================
# 6. FINRA RULE 4210 3X LETF HOUSE MAINTENANCE MARGIN & EOD DE-LEVERING
# =============================================================================

def test_finra_4210_3x_letf_house_maintenance_margin():
    """
    Verifies that enforce_letf_house_margin=True applies 75% intraday / 90% overnight
    maintenance margin to 3x LETFs vs 25% / 50% to 1x Core equities.
    """
    eng = RiskEngine(account_base=10000.0, enforce_letf_house_margin=True)

    # SOXL is 3x LETF -> (0.75, 0.90)
    soxl_intra, soxl_over = eng.get_asset_margin_requirements("SOXL")
    assert soxl_intra == 0.75
    assert soxl_over == 0.90

    # SPY is 1x Core -> (0.25, 0.50)
    spy_intra, spy_over = eng.get_asset_margin_requirements("SPY")
    assert spy_intra == 0.25
    assert spy_over == 0.50

    # Pre-trade margin check:
    # 200 shares SOXL @ $50 = $10,000 notional * 75% = $7,500 <= $10,000 equity -> ALLOWED
    ok_soxl_pass, _ = eng.validate_pre_trade_margin("SOXL", 200, 50.0)
    assert ok_soxl_pass is True

    # 300 shares SOXL @ $50 = $15,000 notional * 75% = $11,250 > $10,000 equity -> BLOCKED
    ok_soxl_block, reason = eng.validate_pre_trade_margin("SOXL", 300, 50.0)
    assert ok_soxl_block is False
    assert "75%" in reason and "FINRA Rule 4210" in reason


def test_two_stage_eod_deleveraging_schedule():
    """
    Verifies Two-Stage EOD De-Levering:
    - Stage 1 (15:50 ET primary regular-session auction trim)
    - Stage 2 (19:45 ET extended-hours backstop)
    Prioritizes liquidating 3x LETFs first to release 90% overnight margin.
    """
    eng = RiskEngine(account_base=10000.0, enforce_letf_house_margin=True)

    # Open positions:
    # SOXL (3x LETF): 200 shares @ $50 = $10,000 -> 90% overnight req = $9,000
    # SPY (1x Core): 10 shares @ $500 = $5,000 -> 50% overnight req = $2,500
    # Total overnight req = $11,500 > $10,000 equity -> $1,500 excess delta
    eng.update_position("SOXL", 200, 50.0)
    eng.update_position("SPY", 10, 500.0)

    # Stage 1 check at 15:50 ET
    plan_stage1 = eng.plan_two_stage_eod_deleveraging(current_time_et="15:50")
    assert plan_stage1["action_required"] is True
    assert plan_stage1["stage"] == "STAGE_1_1550_ET_REGULAR_CLOSE_TRIM"
    assert plan_stage1["delta"] == 1500.0

    # Orders must prioritize liquidating 3x LETF (SOXL) first
    assert plan_stage1["orders"][0]["symbol"] == "SOXL"
    assert plan_stage1["orders"][0]["overnight_margin_requirement"] == 0.90

    # Stage 2 check at 19:45 ET
    plan_stage2 = eng.plan_two_stage_eod_deleveraging(current_time_et="19:45")
    assert plan_stage2["action_required"] is True
    assert plan_stage2["stage"] == "STAGE_2_1945_ET_EXTENDED_HOURS_BACKSTOP"


# =============================================================================
# 7. MULTI-RATE STATE SYNCHRONIZER & MID-SESSION HOT BOOT
# =============================================================================

def test_multi_rate_sync_hot_warm_cold_planes_and_hot_boot():
    """
    Verifies MultiRateStateSynchronizer:
    1. Hot Plane: Microprice, Order Book Imbalance (OBI), and quote watermarking.
    2. Warm Plane: Mid-session hot-boot reconciliation without requiring 9:30 AM baseline.
    3. Cold Plane: FRED OAS, Economic Surprise, and exponential sentiment decay.
    """
    sync = MultiRateStateSynchronizer()

    # Hot Boot account payload mid-session
    account_payload = {
        "securitiesAccount": {
            "currentBalances": {
                "liquidationValue": 100000.0,
                "cashAvailableForTrading": 15000.0,
            },
            "positions": [
                {
                    "instrument": {"symbol": "SOXL"},
                    "longQuantity": 100.0,
                    "marketValue": 5000.0,
                    "averagePrice": 48.0,
                },
                {
                    "instrument": {"symbol": "SPY"},
                    "longQuantity": 140.0,
                    "marketValue": 80000.0,
                    "averagePrice": 570.0,
                },
            ],
        }
    }

    # Warm Plane: Mid-session hot boot
    state_boot: DualSleeveState = sync.on_account_sync(account_payload, is_hot_boot=True)
    assert sync.is_synchronized() is True
    assert state_boot.is_hot_boot_complete is True
    assert state_boot.net_liquidation_value == 100000.0
    assert state_boot.positions["SOXL"].sleeve == "SATELLITE"
    assert state_boot.positions["SPY"].sleeve == "CORE"

    # Hot Plane: Level 1 Quote updates mark-to-market
    quote_payload = {
        "service": "LEVELONE_EQUITIES",
        "content": [{
            "key": "SOXL",
            "bidPrice": 52.0,
            "askPrice": 52.2,
            "lastPrice": 52.1,
            "bidSize": 100,
            "askSize": 100,
            "totalVolume": 5000000,
            "quoteTime": 0,
        }],
    }
    quote_update: HotQuoteUpdate = sync.on_level1_quote(quote_payload)
    assert quote_update.mid_price == 52.1
    assert quote_update.microprice == 52.1
    assert quote_update.order_book_imbalance == 0.0

    # Live portfolio reflects marked position value without REST polling
    state_marked = sync.get_live_portfolio_state()
    assert state_marked.positions["SOXL"].current_price == 52.1
    assert state_marked.positions["SOXL"].market_value == 5210.0

    # Cold Plane: FRED Macro & Gemini NLP with exponential decay
    sync.on_fred_macro({
        "observations": [{"spread_basis_points": 315.0, "z_score_20d": -0.35}],
        "economic_surprise_z_score": 0.45,
    })
    sync.on_gemini_nlp({
        "dovish_hawkish_scoring": {"net_dovish_polarity": 0.80},
    })

    z_s, s_nlp, cs = sync.get_macro_feature_vector()
    assert z_s == 0.45
    assert 0.0 < s_nlp <= 0.80
    assert cs > 300.0


# =============================================================================
# 8. ARGON 2.4.0 CALIBRATION & MULTI-ASSET CROSS-SLEEVE SWEEP (#98)
# =============================================================================

def test_argon_240_strategy_config_sync():
    """
    Verifies that strategy_config.json is synced with the calibrated_config_payload
    from data/schwab_engine_argon_dossier.json (#98).
    """
    import json
    from pathlib import Path

    cfg = json.loads(Path("config/strategy_config.json").read_text(encoding="utf-8"))
    assert cfg["system_metadata"]["config_version"] == "2.4.0-argon"
    assert cfg["target_weights"]["core_target"] == 0.85
    assert cfg["target_weights"]["satellite_target"] == 0.15
    assert cfg["leland_ntz"]["tau_band"] == 0.03
    assert cfg["leland_ntz"]["drift_lower_cutoff"] == 0.12
    assert cfg["leland_ntz"]["drift_upper_cutoff"] == 0.18
    assert cfg["circuit_breaker"]["kappa_core"] == 0.5
    assert cfg["circuit_breaker"]["lambda_0"] == 0.03
    assert cfg["letf_drag_cutoffs"]["volatility_gate_trigger"] == 0.15
    assert cfg["letf_drag_cutoffs"]["rolling_vol_window_bars"] == 32
    assert cfg["margin_thresholds"]["cash_sweep_vehicle"] == "SWVXX"
    assert cfg["routing_sweep_vector"] == {
        "SPY": 0.35,
        "QQQ": 0.25,
        "IWM": 0.10,
        "HYG": 0.15,
        "IEF": 0.15,
        "cash": 0,
    }


def test_multi_asset_core_basket_sweep_and_swvxx_invariant():
    """
    Verifies Cross-Sleeve Dynamic Sweep (#98):
    - Routes liquidated satellite notional V_freed across SPY (0.35), QQQ (0.25),
      IWM (0.10), HYG (0.15), and IEF (0.15).
    - Enforces discrete integer share flooring: Delta Q_c = floor((V_freed * w_{sweep, c}) / P_c(t)).
    - Quantizes residual fractional cash into SWVXX / M_buf, preserving W_core + W_sat + W_buf == 1.0.
    """
    router = DualSleeveRouter(tau_band=0.03)
    nlv = 100000.0
    sat_positions = {
        "SOXL": {"quantity": 200, "current_price": 50.0, "market_value": 10000.0},
        "TQQQ": {"quantity": 150, "current_price": 80.0, "market_value": 12000.0},
    }
    core_prices = {"SPY": 585.0, "QQQ": 490.0, "IWM": 220.0, "HYG": 79.0, "IEF": 95.0}

    plan = router.plan_dual_sleeve_reallocation(
        nlv=nlv,
        satellite_positions=sat_positions,
        target_satellite_weight=0.15,
        core_prices=core_prices,
    )

    assert plan.action == "HARVEST_TO_CORE"
    v_freed = abs(plan.satellite_notional_delta)
    core_buys = {o.symbol: o for o in plan.orders if o.side == "BUY" and o.sleeve == "CORE"}

    expected_weights = {"SPY": 0.35, "QQQ": 0.25, "IWM": 0.10, "HYG": 0.15, "IEF": 0.15}
    for sym, w_c in expected_weights.items():
        expected_shares = math.floor((v_freed * w_c) / core_prices[sym])
        assert plan.core_basket_shares[sym] == expected_shares
        assert core_buys[sym].shares == expected_shares

    # Exact conservation: V_freed == Core equity deployed + SWVXX residual cash buffer
    assert pytest.approx(plan.core_notional_delta + plan.cash_buffer_sweep_usd, abs=1e-2) == v_freed
    assert plan.cash_sweep_vehicle == "SWVXX"
    assert pytest.approx(plan.w_core + plan.w_sat + plan.w_buf, abs=1e-6) == 1.0


def test_strategy_d_vector_dot_product_hwm_decay():
    """
    Verifies Strategy D core-accelerated recovery decay formula (#98):
        lambda_hwm^eff(t) = lambda_0 + kappa_core * max(0, w_sweep^T * r_core(t)) * (W_swept(t) / w_base)
    with calibrated kappa_core = 0.5 and lambda_0 = 0.03.
    """
    eng = RiskEngine(account_base=10000.0)
    eng.update_core_accelerated_drawdown(current_satellite_equity=10000.0, r_core=0.0)

    r_core_vec = {"SPY": 0.04, "QQQ": 0.06, "IWM": 0.02, "HYG": 0.01, "IEF": 0.01}
    # Dot product: 0.35*0.04 + 0.25*0.06 + 0.10*0.02 + 0.15*0.01 + 0.15*0.01 = 0.034
    # W_swept / w_base = (0.15 - 0.06) / 0.15 = 0.60
    # lambda_hwm_eff = 0.03 + 0.5 * 0.034 * 0.60 = 0.0402
    damper = eng.update_core_accelerated_drawdown(
        current_satellite_equity=9200.0,
        r_core=r_core_vec,
        w_sat=0.06,
        w_base=0.15,
    )
    assert pytest.approx(eng.last_lambda_hwm_eff, abs=1e-6) == 0.0402
    assert 0.0 < damper < 1.0


def test_empirical_hmm_calibration_and_leland_ntz_governor_clamp():
    """
    Verifies Empirical HMM Governor Calibration & Leland NTZ clamping in BayesianSizingGovernor (#98):
    - Transition matrix T matches dossier 3x3 persistence diagonal (0.9993, 0.9968, 0.9971).
    - Emission vectors (mu_k, sigma_k) match dossier definitions.
    - Rebalance evaluation inside [0.12, 0.18] is clamped to zero orders (99.6% churn reduction).
    """
    governor = BayesianSizingGovernor()

    assert governor.transition_matrix == [
        [0.9993, 0.0, 0.0007],
        [0.0032, 0.9968, 0.0],
        [0.0029, 0.0, 0.9971],
    ]
    assert governor.mean_emission_vectors["goldilocks"]["mu"]["Macro_Surprise_Z"] == 0.9946
    assert governor.mean_emission_vectors["uncertainty"]["mu"]["Macro_HYSpread_Bps"] == 406.8
    assert governor.mean_emission_vectors["shock"]["sigma"]["Macro_HYSpread_Bps"] == 35.3

    # Held satellite shares total $15,000 on $100,000 NLV -> active_satellite_weight = 0.15 in [0.12, 0.18]
    nlv = 100000.0
    held = {"SOXL": 100, "TQQQ": 75, "TNA": 100}  # 100*50 + 75*80 + 100*40 = 5000 + 6000 + 4000 = $15,000
    prices = {"SOXL": 50.0, "TQQQ": 80.0, "TNA": 40.0}
    vols = {"SOXL": 0.025, "TQQQ": 0.018, "TNA": 0.030}

    verdict = governor.calculate_multi_asset_order_delta(
        nlv=nlv,
        held_shares=held,
        prices=prices,
        volatilities=vols,
        target_satellite_weight=0.15,
    )
    assert verdict.ntz_clamped is True
    assert verdict.satellite_notional_delta == 0.0
    assert verdict.core_notional_sweep_usd == 0.0
    assert all(dq == 0 for dq in verdict.delta_shares.values())

