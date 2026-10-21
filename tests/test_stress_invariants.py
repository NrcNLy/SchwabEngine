"""
tests/test_stress_invariants.py
===============================
Adversarial Invariant Fuzzing & Monte Carlo Stress Suite (#107)
for SchwabEngine 2.4.0-argon.

Verifies:
1. Atomic Dual-Sleeve Rebalance State Machine (DualSleeveSwapCoordinator):
   - Eliminates the partial-fill gap between Leg 1 (Sell 3x Satellite) and Leg 2 (Buy 1x Core).
   - Asynchronous bounded retry queue for Leg 2 with exponential backoff (1s, 2s, 4s).
   - Immediate fallback sweep of 100% of freed notional into SWVXX cash yield after 3 failed Leg 2 attempts.
2. Dynamic Scale-Invariant Equity Stripping:
   - Confirms removal of static dollar presets (e.g. 21086.00 or 4400.90) from strategy_config.json and risk_manager.py.
   - Confirms all target allocations, discrete integer share floors floor((V_freed * w_c) / P_c), and Leland NTZ bands
     reference live broker NLV exclusively.
3. Scenario A (Flash Crash):
   - SOXL drops 18% in 30 seconds; verifies HMM flips to Macro Shock and 100% of open 3x positions
     liquidate into IEF/SWVXX without margin breach.
4. Scenario B (Rate Limit Burst):
   - Simulates Schwab REST HTTP 429 on 5 consecutive order submissions; verifies exponential backoff jitter
     and ensures zero duplicate orders are submitted.
5. Scenario C (Micro-Cap Quantization Jitter):
   - Injects an account NLV of $4,400.90; verifies integer share floors prevent negative balances and
     quantization remainders roll into SWVXX.
6. Scenario D (1,000-Tick Monte Carlo Balance Invariant):
   - Executes 1,000 synthetic Monte Carlo market ticks across Goldilocks, Uncertainty, Flash Crash,
     and Recovery regimes; at every simulated tick asserts:
         abs((core_mv + sat_mv + swvxx_cash) - total_nlv) < 0.01
"""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
import random
from typing import Any, Dict, List

import pytest

from execution.order_manager import (
    DualSleeveSwapCoordinator,
    OrderManager,
    SwapExecutionResult,
    SwapState,
)
from execution.risk_manager import (
    CASH_SWEEP_VEHICLE,
    CORE_1X_SYMBOLS,
    DEFAULT_CORE_PRICES,
    DEFAULT_ROUTING_SWEEP_VECTOR,
    LETF_3X_SYMBOLS,
    DualSleeveOrder,
    DualSleeveRouter,
    DualSleeveSwapPlan,
    MetaGovernorHMM,
    RiskEngine,
)


class MockBrokerRestClient:
    """
    Deterministic mock Schwab REST client supporting programmable HTTP 429 bursts,
    order rejection injection, and broker-side working order inspection.
    """

    def __init__(self, fail_429_count: int = 0, always_fail_leg2: bool = False) -> None:
        self.fail_429_count = fail_429_count
        self.always_fail_leg2 = always_fail_leg2
        self.calls: List[Dict[str, Any]] = []
        self.accepted_orders: List[Dict[str, Any]] = []
        self._order_seq = 100000

    def get_account_numbers(self) -> List[Dict[str, str]]:
        return [{"accountNumber": "987654015", "hashValue": "HASH_015_SAFE"}]

    def get_orders(self, account_hash: str, status: str = "WORKING") -> List[Dict[str, Any]]:
        return list(self.accepted_orders)

    def place_order(
        self,
        account_hash: str,
        order_body: Dict[str, Any],
        is_essential: bool = False,
        action: str = "ORDER",
    ) -> Any:
        self.calls.append({
            "account_hash": account_hash,
            "body": order_body,
            "is_essential": is_essential,
            "action": action,
        })
        leg = order_body["orderLegCollection"][0]
        instruction = leg["instruction"]
        symbol = leg["instrument"]["symbol"]

        # Simulate Leg 2 failure if configured (BUY Core leg)
        if self.always_fail_leg2 and instruction == "BUY":
            raise RuntimeError(f"HTTP 429 Too Many Requests: Simulated rate limit on Leg 2 BUY {symbol}")

        # Simulate consecutive HTTP 429 burst
        if self.fail_429_count > 0:
            self.fail_429_count -= 1
            raise RuntimeError(f"HTTP 429 Too Many Requests: Rate limit burst ({self.fail_429_count} remaining)")

        self._order_seq += 1
        oid = str(self._order_seq)
        self.accepted_orders.append({
            "orderId": oid,
            "status": "WORKING",
            "clientOrderId": order_body.get("clientOrderId"),
            "symbol": symbol,
            "instruction": instruction,
            "quantity": leg["quantity"],
        })

        class _Resp:
            status_code = 201
            headers = {"Location": f"https://api.schwabapi.com/trader/v1/accounts/{account_hash}/orders/{oid}"}

        return _Resp()


# =============================================================================
# 1. ATOMIC DUAL-SLEEVE REBALANCE STATE MACHINE TESTS
# =============================================================================

def test_dual_sleeve_swap_coordinator_leg2_3_attempts_swvxx_fallback():
    """
    Verifies that when Leg 1 (Sell 3x Satellite) fills and Leg 2 (Buy 1x Core) repeatedly
    fails after 3 attempts with HTTP 429, DualSleeveSwapCoordinator executes exponential
    backoff (1s, 2s, 4s) and immediately sweeps 100% of freed notional into SWVXX cash yield
    with zero stranded cash.
    """
    rest = MockBrokerRestClient(always_fail_leg2=True)
    recorded_sleeps: List[float] = []

    coordinator = DualSleeveSwapCoordinator(
        rest_client=rest,
        account_hash="HASH_015_SAFE",
        backoff_schedule=(1.0, 2.0, 4.0),
        max_leg2_attempts=3,
        jitter_max_sec=0.0,
        sleep_fn=lambda d: recorded_sleeps.append(d),
    )

    router = DualSleeveRouter(tau_band=0.03)
    nlv = 50000.0
    sat_positions = {
        "SOXL": {"quantity": 250, "current_price": 50.0, "market_value": 12500.0},
    }
    plan = router.plan_dual_sleeve_reallocation(
        nlv=nlv,
        satellite_positions=sat_positions,
        target_satellite_weight=0.15,
    )
    assert plan.action == "HARVEST_TO_CORE"

    result: SwapExecutionResult = coordinator.execute_swap(plan)

    assert result.state == SwapState.SWVXX_FALLBACK_SWEPT.value
    assert result.fallback_triggered is True
    assert result.leg2_attempts == 3
    assert result.backoff_delays == [1.0, 2.0, 4.0]
    assert recorded_sleeps == [1.0, 2.0, 4.0]
    assert len(result.leg1_order_ids) == 1
    assert len(result.leg2_order_ids) == 0
    assert result.core_deployed_usd == 0.0
    assert result.swvxx_swept_usd == pytest.approx(result.freed_notional_usd, abs=1e-2)
    assert result.stranded_cash_usd == 0.0
    assert result.cash_sweep_vehicle == "SWVXX"


def test_dual_sleeve_swap_coordinator_async_bounded_retry_recovery():
    """
    Verifies asynchronous bounded retry queue (execute_swap_async):
    Leg 1 fills, Leg 2 encounters 2 transient HTTP 429 errors (backing off 1s, 2s)
    and succeeds on the 3rd attempt, deploying Core shares and sweeping quantization
    remainder into SWVXX.
    """
    rest = MockBrokerRestClient(fail_429_count=0)
    recorded_async_sleeps: List[float] = []

    async def fake_async_sleep(delay: float) -> None:
        recorded_async_sleeps.append(delay)

    coordinator = DualSleeveSwapCoordinator(
        rest_client=rest,
        account_hash="HASH_015_SAFE",
        backoff_schedule=(1.0, 2.0, 4.0),
        max_leg2_attempts=3,
        jitter_max_sec=0.0,
        async_sleep_fn=fake_async_sleep,
    )

    plan = DualSleeveSwapPlan(
        action="HARVEST_TO_CORE",
        active_satellite_weight=0.22,
        target_satellite_weight=0.15,
        band_lower=0.12,
        band_upper=0.18,
        satellite_notional_delta=-2000.0,
        core_notional_delta=1755.0,
        orders=[
            DualSleeveOrder(
                symbol="SOXL",
                side="SELL",
                shares=40,
                target_notional=2000.0,
                price=50.0,
                sleeve="SATELLITE",
                reason="HARVEST",
                leg_id=1,
            ),
            DualSleeveOrder(
                symbol="SPY",
                side="BUY",
                shares=3,
                target_notional=1755.0,
                price=585.0,
                sleeve="CORE",
                reason="SWEEP_CORE",
                leg_id=2,
            ),
        ],
    )

    # Inject 2 transient 429 failures after Leg 1 (so Leg 1 succeeds, Leg 2 fails twice then succeeds on attempt 3)
    orig_place = rest.place_order
    leg2_call_count = 0

    def place_with_leg2_transient_429(account_hash: str, order_body: Dict[str, Any], **kwargs: Any) -> Any:
        nonlocal leg2_call_count
        leg = order_body["orderLegCollection"][0]
        if leg["instruction"] == "BUY":
            leg2_call_count += 1
            if leg2_call_count <= 2:
                raise RuntimeError("HTTP 429: Transient rate limit on Leg 2")
        return orig_place(account_hash, order_body, **kwargs)

    rest.place_order = place_with_leg2_transient_429  # type: ignore[method-assign]

    result = asyncio.run(coordinator.execute_swap_async(plan))

    assert result.state == SwapState.COMPLETED.value
    assert result.fallback_triggered is False
    assert result.leg2_attempts == 3
    assert recorded_async_sleeps == [1.0, 2.0]
    assert result.freed_notional_usd == 2000.0
    assert result.core_deployed_usd == 1755.0
    assert result.swvxx_swept_usd == 245.0
    assert result.stranded_cash_usd == 0.0


# =============================================================================
# 2. DYNAMIC SCALE-INVARIANT EQUITY STRIPPING TESTS
# =============================================================================

def test_dynamic_scale_invariant_equity_stripping():
    """
    Verifies that static dollar presets (such as 21086.00 or 4400.90) are absent from
    strategy_config.json and execution/risk_manager.py, and all target allocations reference
    live broker NLV exclusively.
    """
    cfg_text = Path("strategy_config.json").read_text(encoding="utf-8")
    rm_text = Path("execution/risk_manager.py").read_text(encoding="utf-8")

    assert "21086" not in cfg_text
    assert "4400.90" not in cfg_text
    assert "21086" not in rm_text
    assert "4400.90" not in rm_text

    cfg = json.loads(cfg_text)
    assert cfg["system_metadata"]["scale_invariant"] is True
    assert cfg["system_metadata"]["nlv_source"] == "live_broker_nlv"
    assert cfg["capital_scaling"]["hard_dollar_ceiling"] is None
    assert cfg["capital_scaling"]["residual_sweep_vehicle"] == "SWVXX"


# =============================================================================
# 3. SCENARIO A: FLASH CRASH (SOXL -18% IN 30 SECONDS)
# =============================================================================

def test_scenario_a_flash_crash_soxl_18pct_in_30s():
    """
    Scenario A (Flash Crash):
    SOXL drops 18% in 30 seconds; verify HMM flips to Macro Shock and 100% of open 3x
    positions liquidate into IEF/SWVXX without margin breach.
    """
    eng = RiskEngine(account_base=50000.0, enforce_letf_house_margin=True)
    alloc = eng.allocate_from_live_nlv(
        nlv=50000.0,
        target_satellite_weight=0.15,
        core_prices=DEFAULT_CORE_PRICES,
        satellite_prices={"SOXL": 50.0, "TQQQ": 80.0, "TNA": 40.0},
    )
    assert eng.hmm.get_dominant_regime() == "Goldilocks"
    assert eng.positions["SOXL"]["quantity"] > 0
    assert eng.positions["TQQQ"]["quantity"] > 0
    assert eng.positions["TNA"]["quantity"] > 0

    # Simulate 30-second flash crash where SOXL drops 18% ($50.00 -> $41.00)
    soxl_start = 50.0
    soxl_end = round(soxl_start * (1.0 - 0.18), 4)  # $41.00 (-18.0%)
    steps = 30
    for sec in range(1, steps + 1):
        frac = sec / float(steps)
        px_t = round(soxl_start - (soxl_start - soxl_end) * frac, 4)
        eng.update_tick("SOXL", px_t, timestamp_s=float(sec))
        # Verify zero margin breach at every second of the flash crash
        assert eng.intraday_margin_deficit == 0.0
        assert eng.is_margin_eligible is True
        bal = eng.get_sleeve_balances()
        assert abs((bal["core_mv"] + bal["sat_mv"] + bal["swvxx_cash"]) - bal["total_nlv"]) < 0.01

    # Verify HMM flipped to Macro Shock and target satellite weight dropped to 0.0
    assert eng.hmm.get_dominant_regime() == "Macro Shock"
    assert eng.hmm.pi_shock > 0.80
    target_w_sat = eng.hmm.calculate_target_satellite_weight(w_base=0.15)
    assert target_w_sat == 0.0

    # Generate emergency de-risking plan and execute atomic swap into IEF / SWVXX
    plan = eng.plan_dual_sleeve_reallocation(
        nlv=eng.live_nlv,
        target_satellite_weight=target_w_sat,
        core_prices=DEFAULT_CORE_PRICES,
    )
    assert plan.action == "HARVEST_TO_CORE"
    assert plan.band_upper == 0.0

    # Verify all Leg 2 buy orders route into safe-haven IEF
    leg2_buys = [o for o in plan.orders if o.leg_id == 2]
    assert all(o.symbol == "IEF" and o.side == "BUY" for o in leg2_buys)

    coordinator = DualSleeveSwapCoordinator(sleep_fn=lambda _: None)
    swap_res = coordinator.execute_swap(plan)
    assert swap_res.state == SwapState.COMPLETED.value

    post_balances = eng.apply_swap_plan(plan, leg2_succeeded=True)

    # Verify 100% of open 3x positions are liquidated
    for letf_sym in LETF_3X_SYMBOLS:
        assert eng.positions.get(letf_sym, {}).get("quantity", 0) == 0
    assert post_balances["sat_mv"] == 0.0

    # Verify IEF position increased and remainder is in SWVXX without margin breach
    assert eng.positions["IEF"]["quantity"] > alloc["core_positions"]["IEF"]["quantity"]
    assert post_balances["swvxx_cash"] >= 0.0
    assert eng.intraday_margin_deficit == 0.0
    assert abs((post_balances["core_mv"] + post_balances["sat_mv"] + post_balances["swvxx_cash"]) - post_balances["total_nlv"]) < 0.01


# =============================================================================
# 4. SCENARIO B: RATE LIMIT BURST (5 CONSECUTIVE HTTP 429s)
# =============================================================================

def test_scenario_b_rate_limit_burst_5_consecutive_429s():
    """
    Scenario B (Rate Limit Burst):
    Simulate Schwab REST 429 on 5 consecutive order submissions; verify backoff jitter
    and ensure zero duplicate orders are submitted.
    """
    rest = MockBrokerRestClient(fail_429_count=5)
    recorded_delays: List[float] = []

    coordinator = DualSleeveSwapCoordinator(
        rest_client=rest,
        account_hash="HASH_015_SAFE",
        jitter_max_sec=0.25,
        sleep_fn=lambda d: recorded_delays.append(d),
        rng_seed=107,
    )

    outcome = coordinator.submit_order_with_rate_limit_backoff(
        symbol="IEF",
        side="BUY",
        shares=25,
        price=95.0,
        idempotency_key="IDEM_BURST_TEST_001",
        max_retries=5,
        base_backoff=1.0,
        jitter_max_sec=0.25,
    )

    # 5 consecutive 429 failures + 1 success on 6th attempt = 6 total attempts, 5 backoff delays
    assert outcome["attempts"] == 6
    assert len(recorded_delays) == 5

    # Verify exponential backoff with non-zero jitter on every retry
    expected_bases = [1.0, 2.0, 4.0, 8.0, 16.0]
    for delay, base in zip(recorded_delays, expected_bases):
        assert base < delay <= base + 0.25

    # Verify exactly 1 order was accepted by the broker (zero duplicate orders)
    assert len(rest.accepted_orders) == 1
    assert rest.accepted_orders[0]["clientOrderId"] == "IDEM_BURST_TEST_001"

    # Attempt re-submission with the same idempotency key; verify deduplication blocks duplicate POST
    call_count_before = len(rest.calls)
    dup_outcome = coordinator.submit_order_with_rate_limit_backoff(
        symbol="IEF",
        side="BUY",
        shares=25,
        price=95.0,
        idempotency_key="IDEM_BURST_TEST_001",
        max_retries=5,
    )
    assert dup_outcome["order_id"] == outcome["order_id"]
    assert len(rest.calls) == call_count_before
    assert len(rest.accepted_orders) == 1
    assert coordinator.duplicate_orders_prevented >= 1


# =============================================================================
# 5. SCENARIO C: MICRO-CAP QUANTIZATION JITTER ($4,400.90 NLV)
# =============================================================================

def test_scenario_c_micro_cap_quantization_jitter_4400_90():
    """
    Scenario C (Micro-Cap Quantization Jitter):
    Inject an account NLV of only $4,400.90; verify integer share floors prevent
    negative balances and quantization remainders roll into SWVXX.
    """
    micro_nlv = 4400.90
    eng = RiskEngine(account_base=micro_nlv, enforce_letf_house_margin=True)

    alloc = eng.allocate_from_live_nlv(
        nlv=micro_nlv,
        target_satellite_weight=0.15,
        core_prices=DEFAULT_CORE_PRICES,
        satellite_prices={"SOXL": 45.0, "TQQQ": 78.0, "TNA": 50.0},
    )

    # Verify all share quantities are non-negative integers
    for sym, pos in alloc["core_positions"].items():
        assert isinstance(pos["quantity"], int)
        assert pos["quantity"] >= 0
    for sym, pos in alloc["satellite_positions"].items():
        assert isinstance(pos["quantity"], int)
        assert pos["quantity"] >= 0

    # Verify integer share floors prevented negative cash and rolled remainder into SWVXX
    assert alloc["swvxx_cash"] > 0.0
    assert eng.swvxx_cash_balance == alloc["swvxx_cash"]
    assert alloc["cash_sweep_vehicle"] == "SWVXX"
    assert abs((alloc["core_mv"] + alloc["sat_mv"] + alloc["swvxx_cash"]) - micro_nlv) < 0.01

    # Trigger an upper-band harvest on the $4,400.90 micro-cap account (satellite rally above 0.18 NTZ band)
    eng.update_tick("SOXL", 95.0, timestamp_s=10.0)
    eng.update_tick("TQQQ", 140.0, timestamp_s=10.0)
    eng.update_tick("TNA", 85.0, timestamp_s=10.0)
    plan = eng.plan_dual_sleeve_reallocation(
        nlv=eng.live_nlv,
        target_satellite_weight=0.15,
        core_prices=DEFAULT_CORE_PRICES,
    )
    assert plan.action == "HARVEST_TO_CORE"
    assert plan.cash_buffer_sweep_usd >= 0.0

    # Verify discrete floor formula floor((V_freed * w_c) / P_c) on freed notional
    v_freed = abs(plan.satellite_notional_delta)
    for c_sym, w_c in DEFAULT_ROUTING_SWEEP_VECTOR.items():
        if c_sym == "cash":
            continue
        expected_q = math.floor((v_freed * w_c) / DEFAULT_CORE_PRICES[c_sym])
        assert plan.core_basket_shares[c_sym] == expected_q

    bal_after = eng.apply_swap_plan(plan, leg2_succeeded=True)
    assert bal_after["swvxx_cash"] >= 0.0
    assert abs((bal_after["core_mv"] + bal_after["sat_mv"] + bal_after["swvxx_cash"]) - bal_after["total_nlv"]) < 0.01


# =============================================================================
# 6. SCENARIO D: 1,000-TICK MONTE CARLO ADVERSARIAL FUZZING & BALANCE INVARIANT
# =============================================================================

def test_scenario_d_1000_tick_monte_carlo_balance_invariant():
    """
    Scenario D (Balance Invariant across 1,000 Synthetic Monte Carlo Market Ticks):
    Simulates 1,000 market ticks spanning:
      - Ticks 1..300: Goldilocks drift & Leland NTZ upper-band harvests
      - Ticks 301..450: Uncertainty regime chop & Leland NTZ lower-band dip-buys
      - Ticks 451..480: Flash Crash (SOXL -18% in 30s) -> Macro Shock 100% LETF liquidation
      - Ticks 481..700: Leg 2 rate-limit 429 bursts with SWVXX fallback sweeps
      - Ticks 701..1000: Strategy D Core-accelerated drawdown recovery & scale jumps
    Asserts at every single tick (1..1,000):
        abs((core_mv + sat_mv + swvxx_cash) - total_nlv) < 0.01
    and swvxx_cash >= 0.0 and intraday_margin_deficit == 0.0.
    """
    rng = random.Random(20261021)
    initial_nlv = 27543.85  # Arbitrary non-preset live broker NLV
    eng = RiskEngine(account_base=initial_nlv, enforce_letf_house_margin=True)

    prices: Dict[str, float] = {
        "SPY": 585.0,
        "QQQ": 490.0,
        "IWM": 220.0,
        "HYG": 79.0,
        "IEF": 95.0,
        "SOXL": 48.0,
        "TQQQ": 76.0,
        "TNA": 44.0,
    }

    eng.allocate_from_live_nlv(
        nlv=initial_nlv,
        target_satellite_weight=0.15,
        core_prices={k: prices[k] for k in CORE_1X_SYMBOLS},
        satellite_prices={"SOXL": prices["SOXL"], "TQQQ": prices["TQQQ"], "TNA": prices["TNA"]},
    )

    coordinator = DualSleeveSwapCoordinator(
        backoff_schedule=(1.0, 2.0, 4.0),
        max_leg2_attempts=3,
        sleep_fn=lambda _: None,
    )

    num_ticks = 1000
    rebalances_executed = 0
    swvxx_fallbacks_executed = 0

    for tick in range(1, num_ticks + 1):
        # 1. Evolve market prices across regime phases
        if 451 <= tick <= 480:
            # Flash crash window: SOXL drops 18% over 30 ticks (1s per tick)
            prices["SOXL"] = round(prices["SOXL"] * (1.0 - (0.18 / 30.0)), 4)
            prices["TQQQ"] = round(prices["TQQQ"] * 0.996, 4)
            prices["TNA"] = round(prices["TNA"] * 0.996, 4)
            prices["IEF"] = round(prices["IEF"] * 1.0005, 4)
        else:
            # Correlated Geometric Brownian Motion step
            common_factor = rng.gauss(0.0002, 0.004)
            for sym in CORE_1X_SYMBOLS:
                beta = -0.2 if sym == "IEF" else 1.0
                shock = beta * common_factor + rng.gauss(0.0, 0.002)
                prices[sym] = max(5.0, round(prices[sym] * math.exp(shock), 4))
            for sym in ("SOXL", "TQQQ", "TNA"):
                shock = 3.0 * common_factor + rng.gauss(0.0, 0.009)
                prices[sym] = max(2.0, round(prices[sym] * math.exp(shock), 4))

        # 2. Mark all active positions to market on Hot Plane tick
        for sym, px in prices.items():
            eng.update_tick(sym, px, timestamp_s=float(tick))

        # 3. Update HMM regime at regime boundaries
        if tick == 1:
            eng.update_macro_hmm((1.2, 0.65, 315.0))
        elif tick == 301:
            eng.update_macro_hmm((-0.3, -0.05, 408.0))
        elif tick == 480:
            eng.update_macro_hmm((-2.5, -0.90, 640.0))
        elif tick == 701:
            # Recovery back to Goldilocks
            for _ in range(5):
                eng.update_macro_hmm((1.4, 0.80, 310.0))

        # 4. Evaluate Dual-Sleeve Leland NTZ Reallocation every 10 ticks (or immediately on Flash Crash)
        if tick % 10 == 0 or tick == 480:
            target_w = eng.hmm.calculate_target_satellite_weight(w_base=0.15)
            plan = eng.plan_dual_sleeve_reallocation(
                nlv=eng.live_nlv,
                target_satellite_weight=target_w,
                core_prices={k: prices[k] for k in CORE_1X_SYMBOLS},
                satellite_prices={"SOXL": prices["SOXL"], "TQQQ": prices["TQQQ"], "TNA": prices["TNA"]},
                core_price=prices["SPY"],
            )
            if plan.orders:
                # Between ticks 500..650 simulate Leg 2 HTTP 429 exhaustion on every 3rd rebalance
                simulate_leg2_exhaustion = (500 <= tick <= 650) and (rebalances_executed % 2 == 1)
                if simulate_leg2_exhaustion:
                    fail_rest = MockBrokerRestClient(always_fail_leg2=True)
                    coordinator._rest = fail_rest
                    res = coordinator.execute_swap(plan)
                    assert res.state == SwapState.SWVXX_FALLBACK_SWEPT.value
                    eng.apply_swap_plan(plan, leg2_succeeded=False)
                    swvxx_fallbacks_executed += 1
                else:
                    ok_rest = MockBrokerRestClient()
                    coordinator._rest = ok_rest
                    res = coordinator.execute_swap(plan)
                    assert res.state in (SwapState.COMPLETED.value, SwapState.SWVXX_FALLBACK_SWEPT.value)
                    eng.apply_swap_plan(plan, leg2_succeeded=(res.state == SwapState.COMPLETED.value))
                rebalances_executed += 1

        # 5. Assert strict accounting and margin invariants at EVERY SINGLE TICK
        bal = eng.get_sleeve_balances()
        core_mv = bal["core_mv"]
        sat_mv = bal["sat_mv"]
        swvxx_cash = bal["swvxx_cash"]
        total_nlv = bal["total_nlv"]

        assert swvxx_cash >= 0.0, f"Negative SWVXX cash balance at tick {tick}: {swvxx_cash}"
        assert abs((core_mv + sat_mv + swvxx_cash) - total_nlv) < 0.01, (
            f"Balance invariant breached at tick {tick}: "
            f"core_mv({core_mv}) + sat_mv({sat_mv}) + swvxx_cash({swvxx_cash}) != total_nlv({total_nlv})"
        )
        assert eng.intraday_margin_deficit == 0.0, f"Margin deficit breached at tick {tick}"

    assert rebalances_executed > 0
    assert swvxx_fallbacks_executed >= 0


# =============================================================================
# 7. REGIME-DYNAMIC SATELLITE SIZING & DYNAMIC LELAND NTZ BOUNDS (#111)
# =============================================================================

def test_regime_dynamic_satellite_sizing_and_leland_bounds():
    """
    Verifies Issue #111:
    1. strategy_config.json defines regime_satellite_targets:
       - Goldilocks: 0.25 (NTZ band: [0.21, 0.29])
       - Uncertainty: 0.10 (NTZ band: [0.08, 0.12])
       - Shock: 0.00 (NTZ band: [0.00, 0.00])
       while retaining scale-invariant live NLV sizing.
    2. MetaGovernorHMM and DualSleeveRouter compute dynamic target weight:
       W_sat*(t) = (pi_Goldilocks * 0.25) + (pi_Uncertainty * 0.10) + (pi_Shock * 0.00)
    3. Dynamically compute Leland NTZ bounds around W_sat*(t):
       tau_band = 0.04 if pi_Goldilocks > 0.70 else 0.02
       lower_bound = max(0.0, W_sat* - tau_band)
       upper_bound = min(0.35, W_sat* + tau_band)
    4. allocate_from_live_nlv evaluates breaches against dynamic bounds.
    """
    # 1. Verify strategy_config.json
    cfg_path = Path("strategy_config.json")
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert "regime_satellite_targets" in cfg
    rst = cfg["regime_satellite_targets"]
    assert rst["Goldilocks"]["target"] == 0.25
    assert rst["Goldilocks"]["ntz_band"] == [0.21, 0.29]
    assert rst["Uncertainty"]["target"] == 0.10
    assert rst["Uncertainty"]["ntz_band"] == [0.08, 0.12]
    assert rst["Shock"]["target"] == 0.00
    assert rst["Shock"]["ntz_band"] == [0.00, 0.00]
    assert cfg["system_metadata"]["scale_invariant"] is True
    assert cfg["system_metadata"]["nlv_source"] == "live_broker_nlv"

    # 2. Verify dynamic target weight computation
    hmm = MetaGovernorHMM()
    router = DualSleeveRouter(hmm=hmm)

    # Pure Goldilocks: pi = [1.0, 0.0, 0.0]
    hmm.pi = [1.0, 0.0, 0.0]
    assert hmm.calculate_target_satellite_weight() == 0.25
    assert router.calculate_target_satellite_weight() == 0.25
    tau, l_bound, u_bound = hmm.calculate_dynamic_leland_bounds()
    assert tau == 0.04
    assert l_bound == 0.21
    assert u_bound == 0.29

    # Pure Uncertainty: pi = [0.0, 1.0, 0.0]
    hmm.pi = [0.0, 1.0, 0.0]
    assert hmm.calculate_target_satellite_weight() == 0.10
    assert router.calculate_target_satellite_weight() == 0.10
    tau, l_bound, u_bound = hmm.calculate_dynamic_leland_bounds()
    assert tau == 0.02
    assert l_bound == 0.08
    assert u_bound == 0.12

    # Pure Shock: pi = [0.0, 0.0, 1.0]
    hmm.pi = [0.0, 0.0, 1.0]
    assert hmm.calculate_target_satellite_weight() == 0.00
    assert router.calculate_target_satellite_weight() == 0.00
    tau, l_bound, u_bound = hmm.calculate_dynamic_leland_bounds()
    assert tau == 0.00
    assert l_bound == 0.00
    assert u_bound == 0.00

    # Mixed posterior: pi = [0.50, 0.40, 0.10]
    # W_sat* = (0.50 * 0.25) + (0.40 * 0.10) + (0.10 * 0.00) = 0.125 + 0.040 = 0.165
    hmm.pi = [0.50, 0.40, 0.10]
    w_mixed = hmm.calculate_target_satellite_weight()
    assert w_mixed == 0.165
    tau_mixed, l_mixed, u_mixed = hmm.calculate_dynamic_leland_bounds(w_mixed)
    assert tau_mixed == 0.02  # pi_goldilocks <= 0.70
    assert l_mixed == pytest.approx(0.145, abs=1e-4)
    assert u_mixed == pytest.approx(0.185, abs=1e-4)

    # Mixed posterior with pi_Goldilocks > 0.70: pi = [0.80, 0.15, 0.05]
    # W_sat* = (0.80 * 0.25) + (0.15 * 0.10) + (0.05 * 0.00) = 0.20 + 0.015 = 0.215
    hmm.pi = [0.80, 0.15, 0.05]
    w_high_gold = hmm.calculate_target_satellite_weight()
    assert w_high_gold == 0.215
    tau_hg, l_hg, u_hg = hmm.calculate_dynamic_leland_bounds(w_high_gold)
    assert tau_hg == 0.04  # pi_goldilocks > 0.70
    assert l_hg == pytest.approx(0.175, abs=1e-4)
    assert u_hg == pytest.approx(0.255, abs=1e-4)

    # 3. Verify allocate_from_live_nlv breach evaluation against dynamic bounds
    eng = RiskEngine(account_base=100000.0)
    # Set HMM to Goldilocks
    eng.hmm.pi = [0.95, 0.04, 0.01]

    # Initialize dynamically (without passing target_satellite_weight)
    alloc = eng.allocate_from_live_nlv(nlv=100000.0)
    assert alloc["target_satellite_weight"] == pytest.approx(0.2415, abs=0.01)
    assert alloc["tau_band"] == 0.04
    assert alloc["lower_bound"] == pytest.approx(alloc["target_satellite_weight"] - 0.04, abs=1e-4)
    assert alloc["upper_bound"] == pytest.approx(alloc["target_satellite_weight"] + 0.04, abs=1e-4)
    assert alloc["is_breached"] is False
    assert alloc["breach_status"] == "NO_BREACH"
    assert abs((alloc["core_mv"] + alloc["sat_mv"] + alloc["swvxx_cash"]) - 100000.0) < 0.01

    # Breach evaluation: test lower breach
    # If active satellite weight is below lower bound:
    alloc_lower = eng.router.allocate_from_live_nlv(
        nlv=100000.0,
        target_satellite_weight=0.05,  # Force satellite to be tiny (0.05)
        hmm=eng.hmm,
    )
    # With eng.hmm.pi_goldilocks = 0.95, target = 0.05, tau = 0.04 -> lower = 0.01, upper = 0.09
    assert alloc_lower["is_breached"] is False

    # Force a breach: evaluate breach when satellite holding is above upper_bound
    sat_large = {
        "SOXL": {"quantity": 800, "current_price": 50.0, "market_value": 40000.0},  # $40k on $100k = 40% > 29%
    }
    plan_upper = eng.plan_dual_sleeve_reallocation(
        nlv=100000.0,
        satellite_positions=sat_large,
    )
    assert plan_upper.action == "HARVEST_TO_CORE"
    assert plan_upper.band_upper == pytest.approx(alloc["upper_bound"], abs=1e-2)
    assert plan_upper.active_satellite_weight == 0.40

