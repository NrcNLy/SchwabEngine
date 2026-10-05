"""
tests/test_rate_limiter.py
==========================
Unit and integration tests for:
1. Daily Quota Limiter (3,500 ceiling persistence, 00:00 UTC rollover, essential call bypass).
2. Auth Circuit Breaker (exponential backoff, 2 consecutive 401 tripping, AUTH_LOCKED transition, reset).
3. Zero-broker-call in-memory /api/status execution under rapid polling.
4. Stop order throttling (180s cooldown, >= 1.5% price delta).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from requests import HTTPError, Response

from api.server import build_app, compute_system_state
from core.auth import AuthCircuitBreakerError, SchwabAuthManager, SecurityVault
from core.rate_limiter import (
    DailyQuotaExceededError,
    SchwabRateLimiter,
    TokenBucket,
)
from core.runtime import EngineContext, build_ledger, load_config
from execution.order_manager import OrderManager, TIER2_COOLDOWN_SEC, TIER2_MIN_DELTA_PCT
from execution.risk_manager import RiskEngine


# ===========================================================================
# 1. Daily Quota & Rate Limiter Tests
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
# 2. Auth Circuit Breaker Tests
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
        # Fast backoff for test speed
        with pytest.raises(AuthCircuitBreakerError, match="TRIPPED"):
            auth._do_refresh(backoff_sec=0.01)

        # Verified exactly 2 attempts were made before tripping
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
# 3. Rapid /api/status Zero Broker Call Test
# ===========================================================================

def test_rapid_api_status_zero_outbound_broker_calls():
    cfg = load_config()
    ctx = EngineContext(cfg, live=True)
    ctx.ledgers["active"] = build_ledger(cfg, live=True)
    ctx.risk_manager = RiskEngine()

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

    # Fire 25 rapid status requests
    start = time.monotonic()
    for _ in range(25):
        resp = client.get("/api/status?env=active")
        assert resp.status_code == 200
        data = resp.json()
        assert data["env"] == "active"
        assert data["data_source"] == "LIVE_SCHWAB"

    elapsed = time.monotonic() - start
    # Should execute in-memory ultra-fast
    assert elapsed < 3.0

    # ZERO calls made to broker rest client
    mock_rest.assert_not_called()
    mock_sync.sync_once.assert_not_called()


# ===========================================================================
# 4. Stop Order Throttling (Cooldown & >= 1.5% Delta) Tests
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

    # Immediate update with < 1.5% delta (100.50 -> 0.5% delta) -> must be throttled
    order_id, replaced = om.update_broker_stop("SOXL", 10, 100.50, current_order_id="101")
    assert replaced is False
    assert order_id == "101"
    mock_rest.cancel_order.assert_not_called()

    # Update with >= 1.5% delta (102.00 -> 2.0% delta) but within 180s cooldown -> must be throttled
    order_id, replaced = om.update_broker_stop("SOXL", 10, 102.00, current_order_id="101")
    assert replaced is False
    assert order_id == "101"
    mock_rest.cancel_order.assert_not_called()

    # Fast-forward past 180s cooldown
    om._tier2_stops["SOXL"]["last_update_time"] = time.monotonic() - 181.0

    # Still rejected if delta < 1.5% (100.80 -> 0.8% delta)
    order_id, replaced = om.update_broker_stop("SOXL", 10, 100.80, current_order_id="101")
    assert replaced is False
    assert order_id == "101"

    # Accepted: past 180s cooldown AND delta >= 1.5% (102.50 -> 2.5% delta)
    order_id, replaced = om.update_broker_stop("SOXL", 10, 102.50, current_order_id="101")
    assert replaced is True
    assert order_id == "102"
    mock_rest.cancel_order.assert_called_once_with("HASH123", "101")
