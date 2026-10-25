"""
core/rate_limiter.py
====================
Thread-safe, non-blocking asynchronous rate limiter, Burst Token Bucket,
and Schwab Daily Message Quota Guard (#66).

Enforces institutional rate bounds:
1. Multi-Tier Token Bucket Architecture:
   - Tier 1: Sustained Rate Limiter:
     * 120 requests per rolling 60-second window (2.0 req/sec maximum).
   - Tier 2: Micro-Burst Bucket:
     * Max burst capacity: 20 tokens; refill rate: 2 tokens/second.
   - Tier 3: Daily Quota Hard-Cap:
     * 3,500 requests per calendar day (UTC reset at 00:00:00).
     * Warning threshold at 3,000 requests.
     * Emergency non-essential REST freeze at 3,300 requests (only critical order cancellations/stops allowed).
2. Priority Preemption:
   - CRITICAL_EXIT: Bypasses queue up to burst cap, preempts pending NORMAL polling tasks.
   - NORMAL: Standard quotes, account polling, order placement.
   - BACKGROUND: Historical data backfill, off-hours workers.
3. HTTP 429 Protection:
   - Detects HTTP 429 responses; triggers exponential backoff (initial 2s, 4s, 8s, up to 30s)
   - Cooldown for all BACKGROUND tasks.
"""

from __future__ import annotations

import asyncio
import collections
import functools
import inspect
import json
import logging
import os
import random
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, Optional, Tuple

import httpx
import requests

logger = logging.getLogger(__name__)

DEFAULT_QUOTA_FILE = Path("state/remote_vm_state/daily_quota.json")
MAX_DAILY_CALLS = 3500
FREEZE_THRESHOLD = 3300
WARNING_THRESHOLD = 3000
SUSTAINED_RPM = 120
BURST_CAPACITY = 20.0
BURST_REFILL_RATE = 2.0  # tokens / second

# Phase 1 Resiliency Refactor (#123) Constants
ASYM_BURST_CAPACITY = 120.0       # 120.0 requests/minute (2.0 req/sec burst headroom)
ASYM_SUSTAINED_RATE = 1.0         # 1.0 request/sec continuous replenishment
NON_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({400, 401, 403})
RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503})


class DailyQuotaExceededError(RuntimeError):
    """Raised when the daily outbound request ceiling is reached for non-essential calls."""


def _run_nonblocking_sleep(seconds: float) -> None:
    """
    Executes an asynchronous sleep from synchronous compatibility paths without
    invoking blocking time.sleep().
    """
    if seconds <= 0.0:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is None:
        asyncio.run(asyncio.sleep(seconds))
    else:
        done_evt = threading.Event()
        loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(asyncio.sleep(seconds)).add_done_callback(
                lambda _: done_evt.set()
            )
        )
        done_evt.wait(timeout=seconds + 1.0)


def _extract_http_status_code(exc_or_resp: Any) -> Optional[int]:
    """Extract an HTTP status code from an exception or response object if present."""
    if exc_or_resp is None:
        return None
    status = getattr(exc_or_resp, "status_code", None)
    if isinstance(status, int):
        return status
    resp = getattr(exc_or_resp, "response", None)
    if resp is not None:
        resp_status = getattr(resp, "status_code", None)
        if isinstance(resp_status, int):
            return resp_status
    msg = str(exc_or_resp)
    for code in (400, 401, 403, 429, 500, 502, 503):
        if f"HTTP {code}" in msg or f"status {code}" in msg.lower() or f"status_code={code}" in msg:
            return code
    return None


def _is_transient_network_exception(exc: BaseException) -> bool:
    """Return True if `exc` is a transient network or transport exception."""
    transient_types = (
        httpx.TransportError,
        httpx.TimeoutException,
        requests.ConnectionError,
        requests.Timeout,
        ConnectionError,
        TimeoutError,
        asyncio.TimeoutError,
    )
    return isinstance(exc, transient_types)


def compute_full_jitter_wait(
    attempt: int,
    t_base: float = 0.5,
    t_max: float = 30.0,
    rng: Optional[Any] = None,
) -> float:
    """
    Compute Full-Jitter exponential backoff wait duration:
        t_wait = U(0, min(t_max, t_base * 2^(attempt - 1)))
    """
    exp_step = max(0, int(attempt) - 1)
    ceiling = min(float(t_max), float(t_base) * (2.0 ** exp_step))
    if ceiling <= 0.0:
        return 0.0
    if rng is not None:
        if hasattr(rng, "uniform"):
            return float(rng.uniform(0.0, ceiling))
        if callable(rng):
            return float(rng(0.0, ceiling))
    return float(random.uniform(0.0, ceiling))


def full_jitter_retry(
    func: Optional[Callable[..., Any]] = None,
    *,
    max_attempts: int = 4,
    max_retries: Optional[int] = None,
    t_base: float = 0.5,
    base_delay: Optional[float] = None,
    t_max: float = 30.0,
    max_delay: Optional[float] = None,
    sleep_func: Callable[[float], Coroutine[Any, Any, None]] = asyncio.sleep,
    rng: Optional[Any] = None,
) -> Callable[..., Any]:
    """
    Full-Jitter Retry Decorator (#123 Workstream 4):
        t_wait = U(0, min(t_max, t_base * 2^(attempt - 1)))

    - Catches transient network exceptions (httpx.TransportError, httpx.TimeoutException,
      ConnectionError, TimeoutError) and HTTP status codes {429, 500, 502, 503}.
    - Explicitly bypasses retries on HTTP {400, 401, 403} to avoid masking security
      errors or triggering broker IP bans.
    """
    eff_attempts = (int(max_retries) + 1) if max_retries is not None else max(1, int(max_attempts))
    eff_base = float(base_delay) if base_delay is not None else float(t_base)
    eff_max = float(max_delay) if max_delay is not None else float(t_max)

    def _decorator(target_fn: Callable[..., Any]) -> Callable[..., Any]:
        if inspect.iscoroutinefunction(target_fn):
            @functools.wraps(target_fn)
            async def _async_wrapper(*args: Any, **kwargs: Any) -> Any:
                for attempt in range(1, eff_attempts + 1):
                    try:
                        result = await target_fn(*args, **kwargs)
                        status_code = _extract_http_status_code(result)
                        if status_code in NON_RETRYABLE_STATUS_CODES:
                            return result
                        if status_code in RETRYABLE_STATUS_CODES and attempt < eff_attempts:
                            wait_s = compute_full_jitter_wait(attempt, eff_base, eff_max, rng=rng)
                            logger.warning(
                                "full_jitter_retry: %s returned HTTP %d (attempt %d/%d); backing off %.3fs",
                                target_fn.__name__, status_code, attempt, eff_attempts, wait_s,
                            )
                            await sleep_func(wait_s)
                            continue
                        return result
                    except Exception as exc:
                        status_code = _extract_http_status_code(exc)
                        if status_code in NON_RETRYABLE_STATUS_CODES:
                            logger.error(
                                "full_jitter_retry: %s encountered non-retryable HTTP %d; bypassing retry.",
                                target_fn.__name__, status_code,
                            )
                            raise
                        is_retryable = (
                            status_code in RETRYABLE_STATUS_CODES
                            or _is_transient_network_exception(exc)
                        )
                        if not is_retryable or attempt >= eff_attempts:
                            raise
                        wait_s = compute_full_jitter_wait(attempt, eff_base, eff_max, rng=rng)
                        logger.warning(
                            "full_jitter_retry: %s transient failure (%s) on attempt %d/%d; backing off %.3fs",
                            target_fn.__name__, exc, attempt, eff_attempts, wait_s,
                        )
                        await sleep_func(wait_s)
                raise RuntimeError(f"full_jitter_retry exhausted {eff_attempts} attempts for {target_fn.__name__}")

            return _async_wrapper

        @functools.wraps(target_fn)
        def _sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            for attempt in range(1, eff_attempts + 1):
                try:
                    result = target_fn(*args, **kwargs)
                    status_code = _extract_http_status_code(result)
                    if status_code in NON_RETRYABLE_STATUS_CODES:
                        return result
                    if status_code in RETRYABLE_STATUS_CODES and attempt < eff_attempts:
                        wait_s = compute_full_jitter_wait(attempt, eff_base, eff_max, rng=rng)
                        logger.warning(
                            "full_jitter_retry: %s returned HTTP %d (attempt %d/%d); backing off %.3fs",
                            target_fn.__name__, status_code, attempt, eff_attempts, wait_s,
                        )
                        _run_nonblocking_sleep(wait_s)
                        continue
                    return result
                except Exception as exc:
                    status_code = _extract_http_status_code(exc)
                    if status_code in NON_RETRYABLE_STATUS_CODES:
                        logger.error(
                            "full_jitter_retry: %s encountered non-retryable HTTP %d; bypassing retry.",
                            target_fn.__name__, status_code,
                        )
                        raise
                    is_retryable = (
                        status_code in RETRYABLE_STATUS_CODES
                        or _is_transient_network_exception(exc)
                    )
                    if not is_retryable or attempt >= eff_attempts:
                        raise
                    wait_s = compute_full_jitter_wait(attempt, eff_base, eff_max, rng=rng)
                    logger.warning(
                        "full_jitter_retry: %s transient failure (%s) on attempt %d/%d; backing off %.3fs",
                        target_fn.__name__, exc, attempt, eff_attempts, wait_s,
                    )
                    _run_nonblocking_sleep(wait_s)
            raise RuntimeError(f"full_jitter_retry exhausted {eff_attempts} attempts for {target_fn.__name__}")

        return _sync_wrapper

    if func is not None:
        return _decorator(func)
    return _decorator


# ===========================================================================
# AsyncTokenBucketRateLimiter (#123 Workstream 4)
# ===========================================================================

class AsyncTokenBucketRateLimiter:
    """
    Continuous asynchronous token-bucket governor enforcing Schwab's quota limits:
    - Burst Capacity: 120.0 requests/minute (2.0 req/sec burst headroom).
    - Sustained Rate: 1.0 request/sec continuous replenishment.
    - Daily Hard Ceiling: 3,500 requests per session.

    All rate-limiting delays yield cleanly to the Python asyncio event loop via
    `await asyncio.sleep()` outside the scheduling lock so concurrent tasks never stall.
    """

    def __init__(
        self,
        burst_capacity: float = ASYM_BURST_CAPACITY,
        sustained_rate: float = ASYM_SUSTAINED_RATE,
        daily_ceiling: int = MAX_DAILY_CALLS,
        max_burst_rps: float = 2.0,
        initial_tokens: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep_func: Callable[[float], Coroutine[Any, Any, None]] = asyncio.sleep,
        quota_file: Optional[Path | str] = None,
        capacity: Optional[float] = None,
        refill_rate: Optional[float] = None,
        max_daily_calls: Optional[int] = None,
    ) -> None:
        eff_capacity = float(capacity if capacity is not None else burst_capacity)
        eff_rate = float(refill_rate if refill_rate is not None else sustained_rate)
        eff_ceiling = int(max_daily_calls if max_daily_calls is not None else daily_ceiling)

        if eff_capacity <= 0.0:
            raise ValueError(f"burst_capacity must be > 0, got {eff_capacity}")
        if eff_rate <= 0.0:
            raise ValueError(f"sustained_rate must be > 0, got {eff_rate}")
        if eff_ceiling <= 0:
            raise ValueError(f"daily_ceiling must be > 0, got {eff_ceiling}")

        self.burst_capacity: float = eff_capacity
        self.sustained_rate: float = eff_rate
        self.daily_ceiling: int = eff_ceiling
        self.max_burst_rps: float = float(max_burst_rps)
        self._clock: Callable[[], float] = clock
        self._sleep: Callable[[float], Coroutine[Any, Any, None]] = sleep_func
        self._quota_file: Optional[Path] = Path(quota_file) if quota_file is not None else None

        now = self._clock()
        self._tokens: float = float(initial_tokens if initial_tokens is not None else eff_capacity)
        self._last_refill: float = now
        self._daily_count: int = 0
        self._lock: asyncio.Lock = asyncio.Lock()

    @property
    def tokens(self) -> float:
        now = self._clock()
        elapsed = max(0.0, now - self._last_refill)
        return min(self.burst_capacity, self._tokens + elapsed * self.sustained_rate)

    @property
    def available_tokens(self) -> float:
        return self.tokens

    @property
    def daily_count(self) -> int:
        return self._daily_count

    @property
    def quota_remaining(self) -> int:
        return max(0, self.daily_ceiling - self._daily_count)

    def _refill_locked(self, now: float) -> None:
        if now > self._last_refill:
            elapsed = now - self._last_refill
            self._tokens = min(
                self.burst_capacity,
                self._tokens + elapsed * self.sustained_rate,
            )
            self._last_refill = now

    async def acquire(
        self,
        tokens: float = 1.0,
        priority: str = "NORMAL",
        is_essential: bool = False,
    ) -> float:
        """
        Acquire `tokens` from the bucket non-blockingly.
        Yields to the asyncio event loop via `await self._sleep(wait_time)` when
        burst tokens are depleted, returning the waited duration in seconds.
        Raises `DailyQuotaExceededError` if `daily_count >= daily_ceiling` for non-essential calls.
        """
        if tokens <= 0.0:
            return 0.0
        if tokens > self.burst_capacity:
            raise ValueError(f"Requested tokens ({tokens}) exceeds burst_capacity ({self.burst_capacity})")

        priority_norm = priority.upper().strip()
        critical = is_essential or priority_norm in {
            "CRITICAL_EXIT",
            "CRITICAL",
            "CANCEL",
            "CANCEL_ORDER",
            "EMERGENCY",
            "EMERGENCY_LIQUIDATION",
            "MANDATORY_FLATTEN",
        }

        async with self._lock:
            if self._daily_count >= self.daily_ceiling and not critical:
                logger.error(
                    "AsyncTokenBucketRateLimiter: daily hard ceiling reached (%d/%d). Rejecting %s call.",
                    self._daily_count, self.daily_ceiling, priority_norm,
                )
                raise DailyQuotaExceededError(
                    f"Daily hard ceiling of {self.daily_ceiling} requests reached ({self._daily_count}/{self.daily_ceiling})."
                )

            now = self._clock()
            self._refill_locked(now)

            if self._tokens >= tokens:
                self._tokens -= tokens
                wait_time = 0.0
            else:
                deficit = tokens - self._tokens
                wait_time = deficit / self.sustained_rate
                self._tokens = 0.0
                # Advance virtual refill watermark so concurrent callers queue cleanly
                self._last_refill = now + wait_time

            self._daily_count += int(max(1, round(tokens)))

        if wait_time > 0.0:
            await self._sleep(wait_time)

        return wait_time

    def reset_daily_counter(self) -> None:
        """Reset session daily request count."""
        self._daily_count = 0

    def get_telemetry(self) -> Dict[str, Any]:
        """Return current token bucket telemetry."""
        return {
            "burst_capacity": self.burst_capacity,
            "sustained_rate": self.sustained_rate,
            "available_tokens": round(self.tokens, 4),
            "requests_today": self._daily_count,
            "daily_ceiling": self.daily_ceiling,
            "quota_remaining": self.quota_remaining,
        }

    async def __aenter__(self) -> AsyncTokenBucketRateLimiter:
        await self.acquire(1.0)
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


# Alias matching specification nomenclature
AsyncTokenBucket = AsyncTokenBucketRateLimiter


# ===========================================================================
# SchwabRateGovernor (Async, Multi-Tier, Priority Preemption, Quota Guard)
# ===========================================================================

class SchwabRateGovernor:
    """
    Thread-safe, non-blocking asynchronous rate governor for Schwab REST API.

    Features:
    - Tier 1: Sustained rate limiter (120 req / 60s rolling window, 2.0 req/s max).
    - Tier 2: Micro-burst bucket (capacity 20 tokens, refill 2.0 tokens/s).
    - Tier 3: Daily quota guard (3,500 daily ceiling, warning at 3,000, freeze at 3,300, 00:00 UTC rollover).
    - Priority queue preemption: CRITICAL_EXIT preempts pending NORMAL / BACKGROUND tasks.
    - HTTP 429 backoff: exponential backoff (2s -> 4s -> 8s -> 16s -> 30s) and background cooldown.
    """

    _instance: Optional[SchwabRateGovernor] = None
    _singleton_lock: threading.Lock = threading.Lock()

    def __init__(
        self,
        sustained_rpm: int = SUSTAINED_RPM,
        burst_capacity: float = BURST_CAPACITY,
        burst_refill_rate: float = BURST_REFILL_RATE,
        daily_ceiling: int = MAX_DAILY_CALLS,
        warning_threshold: int = WARNING_THRESHOLD,
        freeze_threshold: int = FREEZE_THRESHOLD,
        quota_file: Optional[Path | str] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep_func: Callable[[float], Coroutine[Any, Any, None]] = asyncio.sleep,
    ) -> None:
        self._sustained_rpm = sustained_rpm
        self._burst_capacity = float(burst_capacity)
        self._burst_refill_rate = float(burst_refill_rate)
        self._daily_ceiling = daily_ceiling
        self._warning_threshold = warning_threshold
        self._freeze_threshold = freeze_threshold
        self._quota_file = Path(quota_file or DEFAULT_QUOTA_FILE)
        self._clock = clock
        self._sleep = sleep_func

        self._lock = asyncio.Lock()
        self._file_lock = threading.RLock()

        # Token bucket state
        now = self._clock()
        self._burst_tokens: float = float(burst_capacity)
        self._last_burst_refill: float = now
        self._timestamps: collections.deque[float] = collections.deque()
        self._next_slot: float = now
        self._critical_burst_used: int = 0

        # Quota state
        self._cached_date_utc: Optional[str] = None
        self._daily_count: int = 0
        self._has_warned: bool = False
        self._init_quota_state()

        # 429 Backoff & Cooldown state
        self._consecutive_429: int = 0
        self._backoff_until: float = 0.0
        self._current_backoff: float = 0.0
        self._background_cooldown_until: float = 0.0

        logger.info(
            "SchwabRateGovernor initialised: sustained_rpm=%d (%.1f req/s), "
            "burst_capacity=%.1f, refill_rate=%.1f/s, daily_ceiling=%d, freeze_threshold=%d",
            self._sustained_rpm, self._sustained_rpm / 60.0,
            self._burst_capacity, self._burst_refill_rate,
            self._daily_ceiling, self._freeze_threshold,
        )

    @classmethod
    def get_instance(
        cls,
        sustained_rpm: int = SUSTAINED_RPM,
        burst_capacity: float = BURST_CAPACITY,
        burst_refill_rate: float = BURST_REFILL_RATE,
        daily_ceiling: int = MAX_DAILY_CALLS,
        warning_threshold: int = WARNING_THRESHOLD,
        freeze_threshold: int = FREEZE_THRESHOLD,
        quota_file: Optional[Path | str] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep_func: Callable[[float], Coroutine[Any, Any, None]] = asyncio.sleep,
    ) -> SchwabRateGovernor:
        """Thread-safe access to the singleton SchwabRateGovernor."""
        with cls._singleton_lock:
            if cls._instance is None:
                cls._instance = cls(
                    sustained_rpm=sustained_rpm,
                    burst_capacity=burst_capacity,
                    burst_refill_rate=burst_refill_rate,
                    daily_ceiling=daily_ceiling,
                    warning_threshold=warning_threshold,
                    freeze_threshold=freeze_threshold,
                    quota_file=quota_file,
                    clock=clock,
                    sleep_func=sleep_func,
                )
            return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton instance (primarily for testing)."""
        with cls._singleton_lock:
            cls._instance = None

    # ------------------------------------------------------------------
    # Persistent State Management (Daily Quota)
    # ------------------------------------------------------------------

    @staticmethod
    def _current_utc_date() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _init_quota_state(self) -> None:
        with self._file_lock:
            self._load_or_reset_quota()

    def _load_or_reset_quota(self) -> Tuple[str, int]:
        today = self._current_utc_date()
        if not self._quota_file.exists():
            self._cached_date_utc = today
            self._daily_count = 0
            self._save_quota()
            return today, 0

        try:
            raw = self._quota_file.read_text(encoding="utf-8")
            data = json.loads(raw)
            file_date = data.get("date_utc")
            count = int(data.get("count", 0))

            if file_date == today:
                self._cached_date_utc = today
                self._daily_count = count
            else:
                logger.info(
                    "SchwabRateGovernor: 00:00 UTC rollover detected (previous: %s, now: %s). Resetting daily quota.",
                    file_date, today,
                )
                self._cached_date_utc = today
                self._daily_count = 0
                self._has_warned = False
                self._save_quota()
        except Exception as exc:
            logger.warning(
                "SchwabRateGovernor: failed to parse quota file %s: %s. Re-initializing.",
                self._quota_file, exc,
            )
            self._cached_date_utc = today
            self._daily_count = 0
            self._save_quota()

        return self._cached_date_utc, self._daily_count

    def _save_quota(self) -> None:
        self._quota_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "date_utc": self._cached_date_utc,
            "count": self._daily_count,
            "last_updated_utc": datetime.now(timezone.utc).isoformat(),
        }
        data_str = json.dumps(payload, indent=2)

        temp_dir = self._quota_file.parent
        fd, temp_path = tempfile.mkstemp(dir=temp_dir, prefix="quota_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data_str)
            os.replace(temp_path, self._quota_file)
        except Exception:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise

    # ------------------------------------------------------------------
    # Internal Refill & Purge Helpers
    # ------------------------------------------------------------------

    def _refill_burst(self, now: float) -> None:
        elapsed = now - self._last_burst_refill
        if elapsed > 0:
            self._burst_tokens = min(
                self._burst_capacity,
                self._burst_tokens + elapsed * self._burst_refill_rate,
            )
            self._last_burst_refill = now
            # Decay critical burst used proportional to refilled tokens
            decay = elapsed * self._burst_refill_rate
            self._critical_burst_used = max(0, int(self._critical_burst_used - decay))

    def _purge_old_timestamps(self, now: float) -> None:
        cutoff = now - 60.0
        while self._timestamps and self._timestamps[0] <= cutoff:
            self._timestamps.popleft()

    # ------------------------------------------------------------------
    # Rate Governor Core Acquisition
    # ------------------------------------------------------------------

    async def acquire(self, priority: str = "NORMAL") -> bool:
        """
        Non-blocking asynchronous token acquisition with multi-tier bounds.

        Priorities:
        - `CRITICAL_EXIT`: Order cancellations, stops, emergency liquidations.
          Bypasses queue up to burst cap (20 tokens), preempts pending NORMAL polling.
        - `NORMAL`: Standard quotes, order submission, account sync.
        - `BACKGROUND`: Off-hours workers, historical bar backfill. Cooled off after 429.

        Returns:
            True if token acquired and caller may proceed.
            False if request is rejected due to daily quota freeze or hard limit.
        """
        priority_norm = priority.upper().strip()
        is_critical = priority_norm in {
            "CRITICAL_EXIT",
            "CRITICAL",
            "CANCEL",
            "CANCEL_ORDER",
            "EMERGENCY",
            "EMERGENCY_LIQUIDATION",
            "MANDATORY_FLATTEN",
            "STOP",
        }

        async with self._lock:
            # 1. Daily Quota Verification & Rollover Check
            with self._file_lock:
                self._load_or_reset_quota()

            now = self._clock()
            self._purge_old_timestamps(now)
            self._refill_burst(now)

            # Warning threshold check
            if self._daily_count >= self._warning_threshold and not self._has_warned:
                logger.warning(
                    "SchwabRateGovernor WARNING: daily request count %d reached warning threshold %d "
                    "(ceiling: %d, freeze: %d).",
                    self._daily_count, self._warning_threshold, self._daily_ceiling, self._freeze_threshold,
                )
                self._has_warned = True

            # Daily Quota Freeze (>= 3,300 requests) & Hard-Cap (>= 3,500 requests)
            if self._daily_count >= self._freeze_threshold and not is_critical:
                logger.error(
                    "SchwabRateGovernor: EMERGENCY NON-ESSENTIAL REST FREEZE ACTIVE (%d/%d requests). "
                    "Rejecting %s request.",
                    self._daily_count, self._daily_ceiling, priority_norm,
                )
                return False

            if self._daily_count >= self._daily_ceiling and not is_critical:
                logger.error(
                    "SchwabRateGovernor: DAILY CEILING HARD-CAP REACHED (%d/%d). Rejecting %s.",
                    self._daily_count, self._daily_ceiling, priority_norm,
                )
                return False

            # 2. Scheduling & Rate Limit Calculation
            target_time = now

            if is_critical:
                # CRITICAL_EXIT: Bypasses queue up to burst capacity
                if self._critical_burst_used < self._burst_capacity:
                    self._critical_burst_used += 1
                    target_time = now
                else:
                    # Exceeded burst cap under extreme emergency, schedule immediate next available slot
                    slot = max(now, self._next_slot) + (1.0 / self._burst_refill_rate)
                    self._next_slot = slot
                    target_time = slot
            else:
                # NORMAL or BACKGROUND
                if self._burst_tokens >= 1.0 and len(self._timestamps) < self._sustained_rpm:
                    # Token available in burst bucket and sustained rate not exceeded
                    self._burst_tokens -= 1.0
                    target_time = now
                else:
                    # Micro-burst or sustained window limit reached: schedule smooth refill slot
                    slot = max(now, self._next_slot) + (1.0 / self._burst_refill_rate)
                    if len(self._timestamps) >= self._sustained_rpm:
                        # Tier 1 Sustained Rate Limiter: oldest request in rolling 60s must roll out
                        earliest = self._timestamps[len(self._timestamps) - self._sustained_rpm]
                        slot = max(slot, earliest + 60.0)
                    self._next_slot = slot
                    target_time = slot

            # 3. 429 Backoff & Background Cooldown Application
            if priority_norm == "BACKGROUND" and self._background_cooldown_until > target_time:
                target_time = self._background_cooldown_until

            if self._backoff_until > target_time:
                target_time = self._backoff_until

            wait_needed = max(0.0, target_time - now)

        # 4. Asynchronous Sleep (outside lock to avoid blocking other tasks)
        if wait_needed > 0:
            await self._sleep(wait_needed)

        # 5. Commit Request Timestamp & Increment Daily Quota
        async with self._lock:
            now_exec = self._clock()
            self._timestamps.append(now_exec)
            with self._file_lock:
                self._daily_count += 1
                self._save_quota()
            return True

    def record_response(self, status_code: int, headers: Optional[Dict[str, Any]] = None) -> None:
        """
        Record outbound HTTP response status to detect 429 Too Many Requests
        and trigger exponential backoff.
        """
        now = self._clock()
        if status_code == 429:
            self._consecutive_429 += 1
            # Exponential backoff: 2s, 4s, 8s, 16s, up to 30s
            backoff = min(30.0, 2.0 * (2 ** (self._consecutive_429 - 1)))
            if headers:
                retry_after = headers.get("Retry-After") or headers.get("retry-after")
                if retry_after is not None:
                    try:
                        backoff = max(backoff, float(retry_after))
                    except (ValueError, TypeError):
                        pass

            self._current_backoff = backoff
            self._backoff_until = max(self._backoff_until, now + backoff)
            # Cool off BACKGROUND tasks with extended cooldown
            self._background_cooldown_until = max(
                self._background_cooldown_until,
                now + max(backoff * 2.0, 30.0),
            )
            logger.warning(
                "SchwabRateGovernor: HTTP 429 detected (#%d). Exponential backoff: %.1fs (until %.1f). "
                "BACKGROUND tasks cooled off until %.1f.",
                self._consecutive_429, backoff, self._backoff_until, self._background_cooldown_until,
            )
        elif 200 <= status_code < 300:
            if now >= self._backoff_until:
                self._consecutive_429 = 0
                self._current_backoff = 0.0

    def get_telemetry(self) -> Dict[str, Any]:
        """
        Return institutional telemetry dictionary:
        - requests_today
        - current_rpm
        - burst_tokens_available
        - quota_remaining
        """
        now = self._clock()
        self._purge_old_timestamps(now)
        self._refill_burst(now)
        with self._file_lock:
            self._load_or_reset_quota()
            today_count = self._daily_count

        return {
            "requests_today": today_count,
            "current_rpm": len(self._timestamps),
            "burst_tokens_available": round(self._burst_tokens, 2),
            "quota_remaining": max(0, self._daily_ceiling - today_count),
            "warning_threshold": self._warning_threshold,
            "freeze_threshold": self._freeze_threshold,
            "daily_ceiling": self._daily_ceiling,
            "consecutive_429": self._consecutive_429,
            "backoff_active": now < self._backoff_until,
            "background_cooling_off": now < self._background_cooldown_until,
        }


def get_governor() -> SchwabRateGovernor:
    """Return the singleton SchwabRateGovernor instance."""
    return SchwabRateGovernor.get_instance()


# ===========================================================================
# Backward Compatibility Wrappers (TokenBucket, SchwabRateLimiter)
# ===========================================================================

class TokenBucket:
    """
    Thread-safe token bucket implementation for synchronous components.
    Tokens refill continuously at `refill_rate` tokens/second up to `capacity`.
    """

    def __init__(
        self,
        capacity: float = BURST_CAPACITY,
        refill_rate: float = BURST_REFILL_RATE,
        reserve: float = 8.0,
        max_debt: float = 5.0,
    ) -> None:
        if capacity <= 0:
            raise ValueError(f"capacity must be > 0, got {capacity}")
        if refill_rate <= 0:
            raise ValueError(f"refill_rate must be > 0, got {refill_rate}")

        self._capacity: float = capacity
        self._refill_rate: float = refill_rate
        self._reserve: float = reserve
        self._max_debt: float = max_debt
        self._tokens: float = capacity
        self._last_refill: float = time.monotonic()
        self._lock: threading.Lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        credited = elapsed * self._refill_rate
        self._tokens = min(self._capacity, self._tokens + credited)
        self._last_refill = now

    def acquire(
        self,
        tokens: float = 1.0,
        timeout: Optional[float] = None,
        is_essential: bool = False,
    ) -> bool:
        if tokens > self._capacity:
            raise ValueError(
                f"Requested {tokens} tokens exceeds bucket capacity {self._capacity}"
            )

        deadline = None if timeout is None else time.monotonic() + timeout
        min_allowed = -self._max_debt if is_essential else self._reserve

        while True:
            with self._lock:
                self._refill()
                if (self._tokens - tokens) >= min_allowed:
                    self._tokens -= tokens
                    return True

                deficit = (min_allowed + tokens) - self._tokens
                wait_needed = deficit / self._refill_rate

            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        "TokenBucket: acquire timed out after %.2fs (requested %.1f tokens, essential=%s)",
                        timeout, tokens, is_essential,
                    )
                    return False
                sleep_duration = min(wait_needed, remaining)
            else:
                sleep_duration = wait_needed

            _run_nonblocking_sleep(min(sleep_duration, 0.05))

    async def async_acquire(
        self,
        tokens: float = 1.0,
        timeout: Optional[float] = None,
        is_essential: bool = False,
    ) -> bool:
        if tokens > self._capacity:
            raise ValueError(
                f"Requested {tokens} tokens exceeds bucket capacity {self._capacity}"
            )

        deadline = None if timeout is None else time.monotonic() + timeout
        min_allowed = -self._max_debt if is_essential else self._reserve

        while True:
            with self._lock:
                self._refill()
                if (self._tokens - tokens) >= min_allowed:
                    self._tokens -= tokens
                    return True

                deficit = (min_allowed + tokens) - self._tokens
                wait_needed = deficit / self._refill_rate

            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        "TokenBucket: async_acquire timed out after %.2fs (requested %.1f tokens, essential=%s)",
                        timeout, tokens, is_essential,
                    )
                    return False
                sleep_duration = min(wait_needed, remaining)
            else:
                sleep_duration = wait_needed

            await asyncio.sleep(min(sleep_duration, 0.05))

    def available(self) -> float:
        with self._lock:
            self._refill()
            return self._tokens

    def __repr__(self) -> str:
        return (
            f"TokenBucket(capacity={self._capacity}, "
            f"refill_rate={self._refill_rate}/s, "
            f"available≈{self.available():.2f})"
        )


class SchwabRateLimiter:
    """
    Synchronous rate limiter and daily message quota enforcer for backward compatibility.
    """

    _instance: Optional[SchwabRateLimiter] = None
    _singleton_lock: threading.Lock = threading.Lock()

    def __init__(
        self,
        quota_file: Optional[Path | str] = None,
        max_daily_calls: int = MAX_DAILY_CALLS,
        warning_threshold: int = 3200,
        capacity: float = BURST_CAPACITY,
        refill_rate: float = 100.0 / 60.0,
    ) -> None:
        self._quota_file = Path(quota_file or DEFAULT_QUOTA_FILE)
        self._max_daily_calls = max_daily_calls
        self._warning_threshold = warning_threshold

        self._bucket = TokenBucket(capacity=capacity, refill_rate=refill_rate)
        self._lock = threading.RLock()

        self._cached_date_utc: Optional[str] = None
        self._cached_count: int = 0
        self._has_warned: bool = False

        self._init_state()

    @classmethod
    def get_instance(
        cls,
        quota_file: Optional[Path | str] = None,
        max_daily_calls: int = MAX_DAILY_CALLS,
        warning_threshold: int = 3200,
        capacity: float = BURST_CAPACITY,
        refill_rate: float = 100.0 / 60.0,
    ) -> SchwabRateLimiter:
        with cls._singleton_lock:
            if cls._instance is None:
                cls._instance = cls(
                    quota_file=quota_file,
                    max_daily_calls=max_daily_calls,
                    warning_threshold=warning_threshold,
                    capacity=capacity,
                    refill_rate=refill_rate,
                )
            return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        with cls._singleton_lock:
            cls._instance = None

    @staticmethod
    def _current_utc_date() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _init_state(self) -> None:
        with self._lock:
            self._load_or_reset_quota()

    def _load_or_reset_quota(self) -> Tuple[str, int]:
        today = self._current_utc_date()
        if not self._quota_file.exists():
            self._cached_date_utc = today
            self._cached_count = 0
            self._save_quota()
            return today, 0

        try:
            raw = self._quota_file.read_text(encoding="utf-8")
            data = json.loads(raw)
            file_date = data.get("date_utc")
            count = int(data.get("count", 0))

            if file_date == today:
                self._cached_date_utc = today
                self._cached_count = count
            else:
                self._cached_date_utc = today
                self._cached_count = 0
                self._save_quota()
        except Exception:
            self._cached_date_utc = today
            self._cached_count = 0
            self._save_quota()

        return self._cached_date_utc, self._cached_count

    def _save_quota(self) -> None:
        self._quota_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "date_utc": self._cached_date_utc,
            "count": self._cached_count,
            "last_updated_utc": datetime.now(timezone.utc).isoformat(),
        }
        data_str = json.dumps(payload, indent=2)

        temp_dir = self._quota_file.parent
        fd, temp_path = tempfile.mkstemp(dir=temp_dir, prefix="quota_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data_str)
            os.replace(temp_path, self._quota_file)
        except Exception:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise

    def get_daily_count(self) -> int:
        with self._lock:
            _, count = self._load_or_reset_quota()
            return count

    def should_throttle_sync(self) -> bool:
        return self.get_daily_count() >= self._warning_threshold

    def is_daily_limit_exceeded(self) -> bool:
        return self.get_daily_count() >= self._max_daily_calls

    @staticmethod
    def is_essential_call(action: str = "NORMAL", is_essential: bool = False) -> bool:
        if is_essential:
            return True
        action_upper = action.upper()
        return action_upper in {
            "CANCEL",
            "CANCEL_ORDER",
            "CANCEL_ALL",
            "EMERGENCY",
            "EMERGENCY_LIQUIDATION",
            "MANDATORY_FLATTEN",
            "FLATTEN",
            "DELETE",
            "CRITICAL_EXIT",
        }

    def acquire(
        self,
        tokens: float = 1.0,
        timeout: Optional[float] = None,
        is_essential: bool = False,
        action: str = "NORMAL",
    ) -> bool:
        with self._lock:
            _, current_count = self._load_or_reset_quota()
            essential = self.is_essential_call(action, is_essential)

            if current_count >= self._max_daily_calls:
                if not essential:
                    logger.error(
                        "SchwabRateLimiter: DAILY QUOTA CEILING REACHED (%d/%d). "
                        "Blocking non-essential REST call '%s'.",
                        current_count, self._max_daily_calls, action,
                    )
                    raise DailyQuotaExceededError(
                        f"Daily outbound request ceiling of {self._max_daily_calls} exceeded (current: {current_count}). "
                        f"Non-essential call '{action}' blocked."
                    )
                logger.warning(
                    "SchwabRateLimiter: Quota ceiling (%d) reached, but allowing essential call '%s'.",
                    self._max_daily_calls, action,
                )
            elif current_count >= self._warning_threshold:
                if not self._has_warned or current_count % 50 == 0:
                    logger.warning(
                        "SchwabRateLimiter WARNING: daily outbound request count %d exceeded warning threshold %d "
                        "(hard ceiling: %d). Non-essential syncs throttled to 60s.",
                        current_count, self._warning_threshold, self._max_daily_calls,
                    )
                    self._has_warned = True

        acquired = self._bucket.acquire(tokens=tokens, timeout=timeout, is_essential=essential)
        if not acquired:
            return False

        with self._lock:
            _, count = self._load_or_reset_quota()
            self._cached_count = count + int(tokens)
            self._save_quota()

        return True

    async def async_acquire(
        self,
        tokens: float = 1.0,
        timeout: Optional[float] = None,
        is_essential: bool = False,
        action: str = "NORMAL",
    ) -> bool:
        return await asyncio.to_thread(
            self.acquire,
            tokens=tokens,
            timeout=timeout,
            is_essential=is_essential,
            action=action,
        )

    def available(self) -> float:
        return self._bucket.available()

    def __repr__(self) -> str:
        return (
            f"SchwabRateLimiter(daily_count={self.get_daily_count()}/{self._max_daily_calls}, "
            f"bucket={self._bucket})"
        )


def build_from_config(cfg: dict) -> SchwabRateLimiter:
    """Construct or return the singleton SchwabRateLimiter from config."""
    rl_cfg = cfg.get("rate_limiter", {})
    capacity = float(rl_cfg.get("capacity", BURST_CAPACITY))
    refill_rate = float(rl_cfg.get("refill_rate", SUSTAINED_RPM / 60.0))
    quota_file = rl_cfg.get("quota_file", DEFAULT_QUOTA_FILE)
    max_daily = int(rl_cfg.get("max_daily_calls", MAX_DAILY_CALLS))
    warn_thresh = int(rl_cfg.get("warning_threshold", WARNING_THRESHOLD))

    return SchwabRateLimiter.get_instance(
        quota_file=quota_file,
        max_daily_calls=max_daily,
        warning_threshold=warn_thresh,
        capacity=capacity,
        refill_rate=refill_rate,
    )
