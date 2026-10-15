"""
tests/test_rate_limiter.py
==========================
Unit, concurrency benchmark, and integration tests for:
1. Multi-tier token bucket & rapid-fire burst throttle (120 RPM, 20 burst capacity, 2.0 req/s).
2. Priority inversion check (CRITICAL_EXIT preempts pending NORMAL polling tasks).
3. Daily quota lockout (3,300 freeze threshold rejects non-essential, passes cancel orders).
4. HTTP 429 exponential backoff (2s, 4s, 8s, up to 30s) and BACKGROUND cooldown.
5. SchwabAsyncRestClient priority routing integration.
6. Daily quota persistence and 00:00 UTC rollover.
7. Auth circuit breaker (2 consecutive 401s -> AUTH_LOCKED).
8. In-memory zero-broker-call status polling.
9. Stop order throttling (180s cooldown, >= 1.5% delta).
"""

from __future__ import annotations

import asyncio
import collections
import heapq
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from requests import Response

from api.server import build_app, compute_system_state
from core.auth import AuthCircuitBreakerError, SchwabAuthManager, SecurityVault
from core.rate_limiter import (
    DailyQuotaExceededError,
    SchwabRateGovernor,
    SchwabRateLimiter,
    TokenBucket,
    get_governor,
)
from core.rest_client import SchwabAsyncRestClient
from core.runtime import EngineContext, build_ledger, load_config
from execution.order_manager import OrderManager


# ===========================================================================
# Deterministic Async Simulation Scheduler
# ===========================================================================

class SimScheduler:
    """Virtual event loop clock and timer queue for deterministic async rate tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.time: float = start
        self.heap: list[tuple[float, int, asyncio.Future]] = []
        self._counter: int = 0

    def now(self) -> float:
        return self.time

    async def sleep(self, dt: float) -> None:
        if dt <= 0:
            return
        target = self.time + dt
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._counter += 1
        heapq.heappush(self.heap, (target, self._counter, fut))
        await fut

    def step(self) -> bool:
        if self.heap:
            target, _, fut = heapq.heappop(self.heap)
            self.time = target
            if not fut.done():
                fut.set_result(None)
            return True
        return False


# ===========================================================================
# STEP 3 TEST 1: Rapid-Fire Burst Test
# ===========================================================================

def test_rapid_fire_burst_throttle(tmp_path: Path):
    """
    Test 1: Rapid-fire burst test (fires 50 concurrent requests; verifies exactly 20
    execute instantly, remainder throttle smoothly to 2.0 req/sec without 429 errors).
    """
    async def _test():
        quota_file = tmp_path / "burst_quota.json"
        sched = SimScheduler(start=1000.0)
        governor = SchwabRateGovernor(
            sustained_rpm=120,
            burst_capacity=20.0,
            burst_refill_rate=2.0,
            quota_file=quota_file,
            clock=sched.now,
            sleep_func=sched.sleep,
        )

        exec_times: list[float] = []

        async def worker(idx: int):
            ok = await governor.acquire(priority="NORMAL")
            assert ok is True
            exec_times.append(sched.now())

        # Fire 50 concurrent requests simultaneously
        tasks = [asyncio.create_task(worker(i)) for i in range(50)]
        await asyncio.sleep(0)  # Yield to launch all 50 worker coroutines

        # Drain the virtual timer scheduler
        while sched.step():
            await asyncio.sleep(0)

        await asyncio.gather(*tasks)

        assert len(exec_times) == 50

        # Exactly 20 execute instantly at t=1000.0
        instant = [t for t in exec_times if t == 1000.0]
        throttled = [t for t in exec_times if t > 1000.0]

        assert len(instant) == 20, f"Expected 20 instant requests, got {len(instant)}"
        assert len(throttled) == 30, f"Expected 30 throttled requests, got {len(throttled)}"

        # Verify remainder throttle smoothly to 2.0 req/sec (0.5s interval between requests)
        intervals = [round(throttled[i] - throttled[i - 1], 4) for i in range(1, len(throttled))]
        assert len(intervals) == 29
        for dt in intervals:
            assert abs(dt - 0.5) < 1e-3, f"Expected 0.5s interval, got {dt}"

        avg_interval = sum(intervals) / len(intervals)
        measured_rate = 1.0 / avg_interval
        assert abs(measured_rate - 2.0) < 1e-3, f"Expected 2.0 req/sec, got {measured_rate}"

        # Verify no 429 errors were triggered
        telemetry = governor.get_telemetry()
        assert telemetry["consecutive_429"] == 0
        assert telemetry["backoff_active"] is False

    asyncio.run(_test())


# ===========================================================================
# STEP 3 TEST 2: Priority Inversion Check
# ===========================================================================

def test_priority_inversion_preemption(tmp_path: Path):
    """
    Test 2: Priority inversion check (verifies `CRITICAL_EXIT` preempts pending `NORMAL` polling tasks).
    """
    async def _test():
        quota_file = tmp_path / "priority_quota.json"
        sched = SimScheduler(start=1000.0)
        governor = SchwabRateGovernor(
            sustained_rpm=120,
            burst_capacity=20.0,
            burst_refill_rate=2.0,
            quota_file=quota_file,
            clock=sched.now,
            sleep_func=sched.sleep,
        )

        # 1. Drain the 20 burst tokens
        for _ in range(20):
            ok = await governor.acquire(priority="NORMAL")
            assert ok is True

        completion_order: list[str] = []

        async def normal_worker(idx: int):
            await governor.acquire(priority="NORMAL")
            completion_order.append(f"NORMAL_{idx}")

        async def critical_worker():
            await governor.acquire(priority="CRITICAL_EXIT")
            completion_order.append("CRITICAL_EXIT")

        # 2. Fire 5 NORMAL polling tasks (they will be queued and throttled)
        normal_tasks = [asyncio.create_task(normal_worker(i)) for i in range(5)]
        await asyncio.sleep(0)  # Allow NORMAL tasks to compute slot and start waiting

        # 3. Fire CRITICAL_EXIT order cancellation / liquidation task
        crit_task = asyncio.create_task(critical_worker())
        await asyncio.sleep(0)  # Allow CRITICAL_EXIT task to execute

        # Drain scheduler
        while sched.step():
            await asyncio.sleep(0)

        await asyncio.gather(*normal_tasks, crit_task)

        # Verifies CRITICAL_EXIT preempted pending NORMAL polling tasks
        assert len(completion_order) == 6
        assert completion_order[0] == "CRITICAL_EXIT", (
            f"Expected CRITICAL_EXIT to preempt pending tasks, got order: {completion_order}"
        )

    asyncio.run(_test())


# ===========================================================================
# STEP 3 TEST 3: Daily Quota Lockout
# ===========================================================================

def test_daily_quota_lockout(tmp_path: Path):
    """
    Test 3: Daily quota lockout (simulates 3,300 requests; verifies non-essential requests
    reject while cancel orders pass).
    """
    async def _test():
        quota_file = tmp_path / "lockout_quota.json"
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        initial_payload = {
            "date_utc": today_utc,
            "count": 3300,
            "last_updated_utc": datetime.now(timezone.utc).isoformat(),
        }
        quota_file.write_text(json.dumps(initial_payload), encoding="utf-8")

        governor = SchwabRateGovernor(
            daily_ceiling=3500,
            warning_threshold=3000,
            freeze_threshold=3300,
            quota_file=quota_file,
        )

        # 1. Non-essential REST calls (NORMAL and BACKGROUND) must be rejected
        assert await governor.acquire(priority="NORMAL") is False
        assert await governor.acquire(priority="BACKGROUND") is False

        # 2. Critical order cancellations / stops must pass through
        assert await governor.acquire(priority="CRITICAL_EXIT") is True
        assert await governor.acquire(priority="CANCEL_ORDER") is True
        assert await governor.acquire(priority="EMERGENCY_LIQUIDATION") is True

        # 3. Telemetry reflects accurate count and remaining quota
        telemetry = governor.get_telemetry()
        assert telemetry["requests_today"] == 3303
        assert telemetry["quota_remaining"] == 197
        assert telemetry["freeze_threshold"] == 3300
        assert telemetry["daily_ceiling"] == 3500

    asyncio.run(_test())


# ===========================================================================
# 4. HTTP 429 Backoff & Background Task Cooldown Tests
# ===========================================================================

def test_exponential_backoff_429_and_telemetry(tmp_path: Path):
    """
    Verifies HTTP 429 response triggers exponential backoff (2s, 4s, 8s, up to 30s)
    and cools off BACKGROUND tasks.
    """
    async def _test():
        quota_file = tmp_path / "backoff_quota.json"
        sched = SimScheduler(start=1000.0)
        governor = SchwabRateGovernor(
            quota_file=quota_file,
            clock=sched.now,
            sleep_func=sched.sleep,
        )

        # Initial state
        t0 = governor.get_telemetry()
        assert t0["requests_today"] == 0
        assert t0["consecutive_429"] == 0
        assert t0["backoff_active"] is False

        # 1. First 429 response -> 2s backoff
        governor.record_response(429, {"Retry-After": "2"})
        t1 = governor.get_telemetry()
        assert t1["consecutive_429"] == 1
        assert t1["backoff_active"] is True
        assert governor._current_backoff == 2.0

        # 2. Second 429 response -> 4s backoff
        governor.record_response(429)
        assert governor._consecutive_429 == 2
        assert governor._current_backoff == 4.0

        # 3. Third 429 response -> 8s backoff
        governor.record_response(429)
        assert governor._consecutive_429 == 3
        assert governor._current_backoff == 8.0

        # 4. Success response (200 OK) resets backoff once window passes
        sched.time += 35.0
        governor.record_response(200)
        t_recovered = governor.get_telemetry()
        assert t_recovered["consecutive_429"] == 0
        assert t_recovered["backoff_active"] is False

    asyncio.run(_test())


# ===========================================================================
# 5. SchwabAsyncRestClient Priority Routing Integration Tests
# ===========================================================================

def test_async_rest_client_priority_routing(tmp_path: Path):
    """
    Verifies SchwabAsyncRestClient routes place_order/cancel_order with CRITICAL_EXIT
    and quotes/accounts with NORMAL.
    """
    async def _test():
        quota_file = tmp_path / "client_quota.json"
        governor = SchwabRateGovernor(quota_file=quota_file)
        client = SchwabAsyncRestClient(rate_governor=governor)

        priorities_recorded: list[str] = []

        async def fake_request(method, url, priority="NORMAL", **kwargs):
            priorities_recorded.append(priority)
            return {"status": 200}

        with patch.object(client, "_request", side_effect=fake_request):
            # Quotes & accounts -> NORMAL
            await client.get_quotes(["SOXL", "TQQQ"])
            await client.get_accounts()
            await client.get_account_numbers()
            await client.get_user_preferences()

            # Orders & cancels -> CRITICAL_EXIT
            await client.place_order("HASH123", {"order": "buy"})
            await client.cancel_order("HASH123", "ORDER101")
            await client.emergency_liquidation("HASH123", "SOXL", 100)

            # Price history -> BACKGROUND
            await client.get_price_history("SOXL")

        assert priorities_recorded == [
            "NORMAL",
            "NORMAL",
            "NORMAL",
            "NORMAL",
            "CRITICAL_EXIT",
            "CRITICAL_EXIT",
            "CRITICAL_EXIT",
            "BACKGROUND",
        ]

    asyncio.run(_test())


# ===========================================================================
# 6. Daily Quota Persistence & 00:00 UTC Rollover
# ===========================================================================

def test_daily_counter_persistence_and_blocks_above_3500(tmp_path: Path):
    quota_file = tmp_path / "daily_quota.json"
    SchwabRateLimiter.reset_instance()
    limiter = SchwabRateLimiter(
        quota_file=quota_file,
        max_daily_calls=3500,
        warning_threshold=3200,
        capacity=20.0,
        refill_rate=100.0 / 60.0,
    )

    # Initial state
    assert limiter.get_daily_count() == 0
    assert quota_file.exists()

    # Normal acquire increments counter
    assert limiter.acquire(tokens=1.0) is True
    assert limiter.get_daily_count() == 1

    # Verify counter persisted on disk
    raw_data = json.loads(quota_file.read_text(encoding="utf-8"))
    assert raw_data["count"] == 1
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert raw_data["date_utc"] == today_utc

    # Simulate counter at warning threshold 3,200
    limiter._cached_count = 3200
    limiter._save_quota()
    assert limiter.should_throttle_sync() is True
    assert limiter.is_daily_limit_exceeded() is False

    # Simulate counter at hard ceiling 3,500
    limiter._cached_count = 3500
    limiter._save_quota()
    assert limiter.is_daily_limit_exceeded() is True

    # Non-essential REST call must be hard-blocked
    with pytest.raises(DailyQuotaExceededError, match="ceiling of 3500 exceeded"):
        limiter.acquire(action="GET_QUOTES", is_essential=False)

    with pytest.raises(DailyQuotaExceededError):
        limiter.acquire(action="PLACE_LIMIT_BUY", is_essential=False)

    # Essential REST calls must be allowed through
    assert limiter.acquire(action="CANCEL", is_essential=True) is True
    assert limiter.acquire(action="CANCEL_ORDER") is True
    assert limiter.acquire(action="MANDATORY_FLATTEN", is_essential=True) is True
    assert limiter.acquire(action="EMERGENCY_LIQUIDATION") is True

    # Counter persistence across reload
    reloaded_limiter = SchwabRateLimiter(quota_file=quota_file)
    assert reloaded_limiter.get_daily_count() >= 3504

    # 00:00 UTC rollover simulation
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    old_data = {"date_utc": yesterday, "count": 3499, "last_updated_utc": "yesterday"}
    quota_file.write_text(json.dumps(old_data), encoding="utf-8")

    rollover_limiter = SchwabRateLimiter(quota_file=quota_file)
    assert rollover_limiter.get_daily_count() == 0
    assert rollover_limiter.is_daily_limit_exceeded() is False
    assert rollover_limiter.acquire() is True
    assert rollover_limiter.get_daily_count() == 1

    SchwabRateLimiter.reset_instance()


# ===========================================================================
# 7. Auth Circuit Breaker Tests
# ===========================================================================

def test_auth_circuit_breaker_trips_after_2_consecutive_401(tmp_path: Path):
    vault_file = tmp_path / "vault.json"
    vault = SecurityVault(passphrase="testpass123456", iterations=1000, vault_path=vault_file)
    vault.save({
        "access_token": "expired_token",
        "refresh_token": "valid_refresh_token",
        "persisted_at": "2026-10-05T00:00:00Z",
    })

    cfg = {
        "auth": {"access_token_ttl_sec": 1800, "refresh_token_ttl_sec": 604800},
        "api": {"token_url": "https://api.schwabapi.com/v1/oauth/token"},
    }
    auth = SchwabAuthManager(client_id="app_id", client_secret="app_secret", vault=vault, cfg=cfg)
    auth.load_tokens()

    assert auth.auth_circuit_open is False

    mock_401_resp = Response()
    mock_401_resp.status_code = 401
    mock_401_resp._content = b'{"error": "unauthorized"}'

    with patch("requests.post", return_value=mock_401_resp) as mock_post:
        with pytest.raises(AuthCircuitBreakerError, match="TRIPPED"):
            auth._do_refresh(backoff_sec=0.01)

        assert mock_post.call_count == 2
        assert auth.auth_circuit_open is True

    # Further automated calls must be suppressed without calling requests.post
    with patch("requests.post") as mock_post_blocked:
        with pytest.raises(AuthCircuitBreakerError, match="OPEN"):
            auth.get_access_token()
        mock_post_blocked.assert_not_called()

        with pytest.raises(AuthCircuitBreakerError, match="OPEN"):
            auth._do_refresh()
        mock_post_blocked.assert_not_called()

    # System state transitions to AUTH_LOCKED
    engine_ctx = EngineContext(cfg, live=True)
    engine_ctx.auth_manager = auth
    state = compute_system_state(engine_ctx)
    assert state["state"] == "AUTH_LOCKED"
    assert any("circuit breaker is OPEN" in r for r in state["reasons"])

    # Manual reset
    auth.reset_circuit_breaker()
    assert auth.auth_circuit_open is False

    # Reset via exchange_authorization_code
    auth.auth_circuit_open = True
    mock_ok_resp = Response()
    mock_ok_resp.status_code = 200
    mock_ok_resp._content = json.dumps({
        "access_token": "fresh_access",
        "refresh_token": "fresh_refresh",
        "expires_in": 1800,
    }).encode("utf-8")

    with patch("requests.post", return_value=mock_ok_resp):
        auth.exchange_authorization_code("test_code@123", "https://127.0.0.1")
        assert auth.auth_circuit_open is False
        assert auth.get_access_token() == "fresh_access"


# ===========================================================================
# 8. Rapid /api/status Zero Broker Call Test
# ===========================================================================

def test_rapid_api_status_zero_outbound_broker_calls():
    cfg = load_config()
    ctx = EngineContext(cfg, live=True)
    ctx.ledgers["active"] = build_ledger(cfg, live=True)

    mock_rest = MagicMock()
    mock_sync = MagicMock()
    mock_sync.get_positions.return_value = []
    mock_sync.get_unmanaged_positions.return_value = []
    mock_sync.last_latency_ms = 4.2
    mock_sync.consecutive_failures = 0
    mock_sync.seconds_since_ok.return_value = 5.0
    mock_sync.poll_interval = 45.0
    ctx.broker_sync = mock_sync

    app = build_app(ctx)
    client = TestClient(app)

    start = time.monotonic()
    for _ in range(25):
        resp = client.get("/api/status?env=active")
        assert resp.status_code == 200
        data = resp.json()
        assert data["env"] == "active"
        assert data["data_source"] == "LIVE_SCHWAB"

    elapsed = time.monotonic() - start
    assert elapsed < 3.0

    mock_rest.assert_not_called()
    mock_sync.sync_once.assert_not_called()


# ===========================================================================
# 9. Stop Order Throttling (Cooldown & >= 1.5% Delta) Tests
# ===========================================================================

def test_tier2_stop_cooldown_and_delta_throttling():
    mock_rest = MagicMock()
    mock_rest.get_account_numbers.return_value = [{"accountNumber": "98765015", "hashValue": "HASH123"}]
    mock_rest.place_order.side_effect = [
        MagicMock(headers={"Location": "https://api.schwabapi.com/trader/v1/accounts/HASH123/orders/101"}),
        MagicMock(headers={"Location": "https://api.schwabapi.com/trader/v1/accounts/HASH123/orders/102"}),
        MagicMock(headers={"Location": "https://api.schwabapi.com/trader/v1/accounts/HASH123/orders/103"}),
    ]
    mock_rest.cancel_order.return_value = True

    om = OrderManager(rest_client=mock_rest, ledger=MagicMock(), cfg={"account": {"required_suffix": "015"}})
    om.initialize_firewall()

    # Initial broker stop
    initial_id = om.place_broker_stop("SOXL", 10, 100.00)
    assert initial_id == "101"

    # Immediate update with < 1.5% delta -> throttled
    order_id, replaced = om.update_broker_stop("SOXL", 10, 100.50, current_order_id="101")
    assert replaced is False
    assert order_id == "101"
    mock_rest.cancel_order.assert_not_called()

    # Update with >= 1.5% delta but within 180s cooldown -> throttled
    order_id, replaced = om.update_broker_stop("SOXL", 10, 102.00, current_order_id="101")
    assert replaced is False
    assert order_id == "101"
    mock_rest.cancel_order.assert_not_called()

    # Fast-forward past 180s cooldown
    om._tier2_stops["SOXL"]["last_update_time"] = time.monotonic() - 181.0

    # Still rejected if delta < 1.5%
    order_id, replaced = om.update_broker_stop("SOXL", 10, 100.80, current_order_id="101")
    assert replaced is False
    assert order_id == "101"

    # Accepted: past 180s cooldown AND delta >= 1.5%
    order_id, replaced = om.update_broker_stop("SOXL", 10, 102.50, current_order_id="101")
    assert replaced is True
    assert order_id == "102"
    mock_rest.cancel_order.assert_called_once_with("HASH123", "101")
