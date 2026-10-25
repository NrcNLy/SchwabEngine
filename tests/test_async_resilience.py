"""
tests/test_async_resilience.py
==============================
Unit and property tests for Phase 1 (Workstream 4: Asynchronous Resilience,
Rate Limiting & Auth Lifecycle) of the SchwabEngine Resiliency Refactor (#123b).

Validates:
1. `AsyncTokenBucketRateLimiter` (`AsyncTokenBucket`) burst capacity (120.0),
   sustained rate (1.0 req/sec), daily hard ceiling (3,500), and non-blocking
   asyncio event-loop concurrency.
2. `@full_jitter_retry` mathematical backoff bounds
   t_wait = U(0, min(t_max, t_base * 2^(attempt - 1))), retry on transient
   network errors & HTTP {429, 500, 502, 503}, and strict bypass on HTTP {400, 401, 403}.
3. `OAuthTokenManager` 15-minute (900s) heartbeat, <1,800s (30m) TTL refresh trigger,
   mandatory 09:15:00 EDT Pre-Market Refresh Gate, and AUTH_LOCKED circuit breaker.
4. Event-loop de-blocking and structured exception logging across
   `core/rate_limiter.py`, `execution/almgren_chriss.py`, and `execution/order_manager.py`.
"""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime
from pathlib import Path
from typing import List
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytz

from core.auth import AuthCircuitBreakerError, OAuthTokenManager, SecurityVault
from core.rate_limiter import (
    AsyncTokenBucket,
    AsyncTokenBucketRateLimiter,
    DailyQuotaExceededError,
    compute_full_jitter_wait,
    full_jitter_retry,
)
from execution.almgren_chriss import AdaptiveExecutionRouter

EDT = pytz.timezone("America/New_York")


# ===========================================================================
# 1. AsyncTokenBucketRateLimiter: Rate Pacing, Ceiling & Non-Blocking Sleep
# ===========================================================================

def test_async_token_bucket_rate_pacing_and_daily_ceiling():
    """
    Verifies AsyncTokenBucketRateLimiter defaults (burst_capacity=120.0,
    sustained_rate=1.0, daily_ceiling=3500), token replenishment at 1.0 req/s,
    and DailyQuotaExceededError at 3,500 requests.
    """
    async def _run():
        virtual_now = [1000.0]
        recorded_sleeps: List[float] = []

        async def fake_sleep(dt: float) -> None:
            recorded_sleeps.append(dt)
            virtual_now[0] += dt

        limiter = AsyncTokenBucketRateLimiter(
            burst_capacity=120.0,
            sustained_rate=1.0,
            daily_ceiling=3500,
            clock=lambda: virtual_now[0],
            sleep_func=fake_sleep,
        )
        assert AsyncTokenBucket is AsyncTokenBucketRateLimiter
        assert limiter.burst_capacity == 120.0
        assert limiter.sustained_rate == 1.0
        assert limiter.daily_ceiling == 3500

        # 1. Drain all 120 burst tokens instantaneously (zero wait)
        for _ in range(120):
            waited = await limiter.acquire(1.0)
            assert waited == 0.0
        assert len(recorded_sleeps) == 0
        assert limiter.daily_count == 120

        # 2. Next 5 requests must pace at exactly 1.0 req/sec (1.0s sleep each)
        for _ in range(5):
            waited = await limiter.acquire(1.0)
            assert pytest.approx(waited, abs=1e-6) == 1.0
        assert len(recorded_sleeps) == 5
        assert all(pytest.approx(s, abs=1e-6) == 1.0 for s in recorded_sleeps)

        # 3. Advance clock by 10.0s -> 10 tokens replenish at 1.0 req/s
        virtual_now[0] += 10.0
        assert pytest.approx(limiter.available_tokens, abs=1e-4) == 10.0

        # 4. Daily hard ceiling enforcement at 3,500 requests
        limiter._daily_count = 3500
        with pytest.raises(DailyQuotaExceededError, match="3500"):
            await limiter.acquire(1.0, priority="NORMAL")

        # Critical exit / cancellation still permitted at ceiling
        waited_crit = await limiter.acquire(1.0, priority="CRITICAL_EXIT", is_essential=True)
        assert waited_crit == 0.0

    asyncio.run(_run())


def test_async_token_bucket_non_blocking_event_loop_concurrency():
    """
    Verification Invariant #4 (Async Non-Blocking):
    Validates that high-frequency rate-limiting delays yield to the asyncio
    event loop without stalling concurrent tasks.
    """
    async def _run():
        limiter = AsyncTokenBucketRateLimiter(
            burst_capacity=2.0,
            sustained_rate=20.0,  # 50ms per token refill
            initial_tokens=0.0,
        )

        heartbeat_ticks = 0
        throttled_completed = False

        async def concurrent_fast_task():
            nonlocal heartbeat_ticks
            while not throttled_completed:
                heartbeat_ticks += 1
                await asyncio.sleep(0.005)

        async def throttled_requester():
            nonlocal throttled_completed
            # 3 sequential acquisitions requiring ~150ms total async wait
            for _ in range(3):
                await limiter.acquire(1.0)
            throttled_completed = True

        await asyncio.gather(concurrent_fast_task(), throttled_requester())

        # Concurrent task must have ticked many times while limiter was awaiting sleep
        assert throttled_completed is True
        assert heartbeat_ticks >= 5, f"Expected concurrent task to run freely, got {heartbeat_ticks} ticks"

    asyncio.run(_run())


# ===========================================================================
# 2. Full-Jitter Retry Decorator: Math Bounds, Transient Retries & 4xx Bypass
# ===========================================================================

def test_full_jitter_retry_transient_errors_and_status_codes():
    """
    Verifies @full_jitter_retry catches transient network exceptions and
    HTTP {429, 500, 502, 503} with t_wait = U(0, min(t_max, t_base * 2^(attempt - 1))).
    """
    async def _run():
        sleeps: List[float] = []

        async def fake_sleep(dt: float) -> None:
            sleeps.append(dt)

        # Deterministic RNG returning midpoint of [0, ceiling]
        def midpoint_rng(low: float, high: float) -> float:
            return (low + high) * 0.5

        outcomes = [
            httpx.ConnectError("Connection reset by peer"),
            RuntimeError("HTTP 429 Too Many Requests"),
            RuntimeError("HTTP 503 Service Unavailable"),
            {"status": "OK", "status_code": 200},
        ]
        call_count = 0

        @full_jitter_retry(
            max_attempts=4,
            t_base=1.0,
            t_max=10.0,
            sleep_func=fake_sleep,
            rng=midpoint_rng,
        )
        async def flaky_endpoint():
            nonlocal call_count
            item = outcomes[call_count]
            call_count += 1
            if isinstance(item, Exception):
                raise item
            return item

        result = await flaky_endpoint()
        assert result["status"] == "OK"
        assert call_count == 4
        # Ceilings for attempts 1, 2, 3 with t_base=1.0: 1.0, 2.0, 4.0 -> midpoints: 0.5, 1.0, 2.0
        assert sleeps == [0.5, 1.0, 2.0]

        # Property check on compute_full_jitter_wait across 100 samples
        for attempt in range(1, 7):
            expected_cap = min(10.0, 1.0 * (2.0 ** (attempt - 1)))
            for _ in range(25):
                w = compute_full_jitter_wait(attempt=attempt, t_base=1.0, t_max=10.0)
                assert 0.0 <= w <= expected_cap

    asyncio.run(_run())


@pytest.mark.parametrize("fatal_status", [400, 401, 403])
def test_full_jitter_retry_bypasses_400_401_403_immediately(fatal_status: int):
    """
    Verifies @full_jitter_retry explicitly bypasses retries on HTTP 400, 401, and 403
    with zero sleeps and a single invocation.
    """
    async def _run():
        sleeps: List[float] = []
        calls = 0

        async def fake_sleep(dt: float) -> None:
            sleeps.append(dt)

        @full_jitter_retry(max_attempts=5, t_base=0.5, t_max=10.0, sleep_func=fake_sleep)
        async def auth_or_bad_request():
            nonlocal calls
            calls += 1
            req = httpx.Request("POST", "https://api.schwabapi.com/trader/v1/orders")
            resp = httpx.Response(fatal_status, request=req)
            raise httpx.HTTPStatusError(f"HTTP {fatal_status} error", request=req, response=resp)

        with pytest.raises(httpx.HTTPStatusError):
            await auth_or_bad_request()

        assert calls == 1, f"HTTP {fatal_status} must not be retried (got {calls} calls)"
        assert len(sleeps) == 0

    asyncio.run(_run())


# ===========================================================================
# 3. OAuthTokenManager: 15m Heartbeat, <1800s TTL Trigger & 09:15 EDT Gate
# ===========================================================================

def test_oauth_token_manager_ttl_threshold_and_0915_pre_market_gate(tmp_path: Path):
    """
    Verifies OAuthTokenManager:
    1. Polls every 15 minutes (900s).
    2. Refreshes proactively whenever remaining TTL < 1,800s (30 minutes).
    3. Enforces the mandatory 09:15:00 EDT Pre-Market Refresh Gate before 09:30:00 EDT
       even if remaining TTL >= 1,800s.
    4. Transitions to AUTH_LOCKED on HTTP 401.
    """
    async def _run():
        vault = SecurityVault(
            passphrase="test_passphrase_argon",
            iterations=1000,
            vault_path=tmp_path / "vault.json",
        )
        mono_now = [5000.0]
        wall_now = [EDT.localize(datetime(2026, 10, 22, 8, 45, 0))]

        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_resp_ok = MagicMock()
        mock_resp_ok.status_code = 200
        mock_resp_ok.json.return_value = {
            "access_token": "fresh_token_1",
            "refresh_token": "fresh_refresh_1",
            "expires_in": 3600,
        }
        mock_http.post.return_value = mock_resp_ok

        mgr = OAuthTokenManager(
            client_id="cid",
            client_secret="csec",
            vault=vault,
            initial_ttl_sec=2400.0,  # 40 minutes remaining (>= 1800s)
            http_client=mock_http,
            clock=lambda: mono_now[0],
            wall_clock=lambda: wall_now[0],
        )
        assert mgr.heartbeat_interval_sec == 900.0
        assert mgr.min_ttl_threshold_sec == 1800.0

        # Case 1: At 08:45 EDT with 2,400s TTL -> healthy, no refresh triggered
        refreshed = await mgr.check_and_refresh_if_needed()
        assert refreshed is False
        assert mgr.refresh_count == 0

        # Case 2: Advance monotonic clock by 700s -> remaining TTL = 1,700s (< 1,800s)
        mono_now[0] += 700.0
        assert mgr.get_remaining_ttl() == 1700.0
        refreshed_ttl = await mgr.check_and_refresh_if_needed()
        assert refreshed_ttl is True
        assert mgr.refresh_count == 1
        assert mgr.last_refresh_reason == "TTL_BELOW_1800S"
        assert mgr.get_remaining_ttl() == 3600.0

        # Case 3: Advance wall clock to 09:15:00 EDT while TTL is still 3,600s (> 1,800s)
        # Mandatory Pre-Market Refresh Gate MUST trigger a fresh session token!
        wall_now[0] = EDT.localize(datetime(2026, 10, 22, 9, 15, 0))
        refreshed_gate = await mgr.enforce_pre_market_gate()
        assert refreshed_gate is True
        assert mgr.refresh_count == 2
        assert mgr.last_refresh_reason == "PRE_MARKET_GATE_0915_EDT"

        # Calling enforce_pre_market_gate again on the same morning is idempotent
        refreshed_gate_again = await mgr.enforce_pre_market_gate()
        assert refreshed_gate_again is False
        assert mgr.refresh_count == 2

        # Case 4: Simulate HTTP 401 Unauthorized -> transitions to AUTH_LOCKED
        mock_resp_401 = MagicMock()
        mock_resp_401.status_code = 401
        mock_http.post.return_value = mock_resp_401

        with pytest.raises(AuthCircuitBreakerError, match="AUTH_LOCKED"):
            await mgr.refresh_access_token(reason="MANUAL_TEST")
        assert mgr.auth_circuit_open is True
        assert mgr.state == "AUTH_LOCKED"

    asyncio.run(_run())


# ===========================================================================
# 4. Static & Runtime Verification of Event-Loop De-Blocking & Exception Sinks
# ===========================================================================

def test_no_blocking_time_sleep_or_silent_except_pass_in_phase1_modules():
    """
    Verifies that synchronous `time.sleep(...)` calls and silent `except Exception: pass`
    blocks have been completely excised from `core/rate_limiter.py`,
    `execution/almgren_chriss.py`, and `execution/order_manager.py`.
    """
    target_files = [
        Path("core/rate_limiter.py"),
        Path("execution/almgren_chriss.py"),
        Path("execution/order_manager.py"),
    ]

    for path in target_files:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))

        for node in ast.walk(tree):
            # 1. Assert no `time.sleep(...)` calls exist
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if (
                    isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "time"
                    and node.func.attr == "sleep"
                ):
                    pytest.fail(f"Found forbidden synchronous time.sleep() in {path}:{node.lineno}")

            # 2. Assert no `except Exception: pass` blocks exist
            if isinstance(node, ast.ExceptHandler):
                if (
                    node.type is not None
                    and isinstance(node.type, ast.Name)
                    and node.type.id == "Exception"
                    and len(node.body) == 1
                    and isinstance(node.body[0], ast.Pass)
                ):
                    pytest.fail(f"Found silent `except Exception: pass` in {path}:{node.lineno}")


def test_almgren_chriss_async_liquidate_optimal_non_blocking():
    """
    Verifies AdaptiveExecutionRouter.async_liquidate_optimal executes using
    non-blocking async sleep without stalling concurrent tasks.
    """
    async def _run():
        mock_rest = MagicMock()
        mock_rest.get_quotes.return_value = {
            "SOXL": {"quote": {"bidPrice": 26.00, "askPrice": 26.04, "totalVolume": 50000}}
        }
        mock_rest.place_order.return_value = MagicMock(
            headers={"Location": "https://api.schwabapi.com/trader/v1/accounts/HASH/orders/ASYNC-1"}
        )
        poll_calls = 0

        def fake_get_order(account_hash: str, order_id: str):
            nonlocal poll_calls
            poll_calls += 1
            if poll_calls == 1:
                return {"orderId": order_id, "status": "WORKING", "filledQuantity": 0, "price": 26.00}
            return {"orderId": order_id, "status": "FILLED", "filledQuantity": 20, "price": 26.00}

        mock_rest.get_order.side_effect = fake_get_order

        recorded_sleeps: List[float] = []

        async def track_sleep(dt: float) -> None:
            recorded_sleeps.append(dt)
            await asyncio.sleep(0)

        router = AdaptiveExecutionRouter(
            rest_client=mock_rest,
            account_hash="HASH_015_SAFE",
            slice_interval_seconds=1,
            sleep_func=track_sleep,
        )
        summary = await router.async_liquidate_optimal("SOXL", 40, urgency="NORMAL")
        assert summary.status == "FILLED"
        assert summary.filled_shares == 40
        assert len(recorded_sleeps) >= 1

    asyncio.run(_run())
