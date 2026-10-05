"""
core/rate_limiter.py
====================
Thread-safe Token Bucket rate limiter and Schwab Daily Message Quota Enforcer.

Enforces:
1. Schwab Daily Message Quota (<4,000 calls/day per Schwab Trader API limits):
   - Hard-cap ceiling of 3,500 calls/day (persisted in schwab_state/daily_quota.json).
   - Rollover reset at 00:00 UTC.
   - At >= 3,200 calls: logs WARNING and throttles non-essential background syncs to 60s intervals.
   - At >= 3,500 calls: hard-blocks all non-essential REST calls; only allows
     cancellations and emergency liquidations (MANDATORY_FLATTEN).
2. Per-minute Token Bucket:
   - Max 100 requests/minute (refill rate = 100/60 = 1.667 tokens/sec), burst capacity 20.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_QUOTA_FILE = Path("schwab_state/daily_quota.json")
MAX_DAILY_CALLS = 3500
WARNING_THRESHOLD = 3200
DEFAULT_RPM = 100
DEFAULT_BURST = 20


class DailyQuotaExceededError(RuntimeError):
    """Raised when the 3,500 daily outbound request limit is reached for non-essential calls."""


class TokenBucket:
    """
    Thread-safe token bucket implementation.

    Tokens refill continuously at `refill_rate` tokens/second up to `capacity`.
    Each API call must acquire one token via `acquire()` before proceeding.
    """

    def __init__(self, capacity: float, refill_rate: float) -> None:
        if capacity <= 0:
            raise ValueError(f"capacity must be > 0, got {capacity}")
        if refill_rate <= 0:
            raise ValueError(f"refill_rate must be > 0, got {refill_rate}")

        self._capacity: float = capacity
        self._refill_rate: float = refill_rate
        self._tokens: float = capacity          # Start full
        self._last_refill: float = time.monotonic()
        self._lock: threading.Lock = threading.Lock()

        logger.info(
            "TokenBucket initialised: capacity=%.1f tokens, "
            "refill_rate=%.2f tokens/sec (≈%.0f RPM ceiling)",
            capacity, refill_rate, refill_rate * 60,
        )

    def _refill(self) -> None:
        """Credit elapsed tokens since last refill (call inside lock)."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        credited = elapsed * self._refill_rate
        self._tokens = min(self._capacity, self._tokens + credited)
        self._last_refill = now

    def acquire(self, tokens: float = 1.0, timeout: Optional[float] = None) -> bool:
        """
        Block until `tokens` tokens are available and consume them.

        Returns:
            True  — tokens acquired, caller may proceed.
            False — timed out before tokens became available.
        """
        if tokens > self._capacity:
            raise ValueError(
                f"Requested {tokens} tokens exceeds bucket capacity {self._capacity}"
            )

        deadline = None if timeout is None else time.monotonic() + timeout

        while True:
            with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return True

                deficit = tokens - self._tokens
                wait_needed = deficit / self._refill_rate

            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        "TokenBucket: acquire timed out after %.2fs (requested %.1f tokens)",
                        timeout, tokens,
                    )
                    return False
                sleep_duration = min(wait_needed, remaining)
            else:
                sleep_duration = wait_needed

            time.sleep(min(sleep_duration, 0.05))

    def available(self) -> float:
        """Return current token count (approximate — for monitoring only)."""
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
    Singleton rate limiter and daily message quota enforcer for the Schwab API.

    Features:
    - Daily call tracker persisted to `schwab_state/daily_quota.json`, reset at 00:00 UTC.
    - Warning threshold at 3,200 calls/day: triggers sync throttling to 60s.
    - Hard ceiling at 3,500 calls/day: blocks all non-essential REST calls; only allows
      cancellations and emergency liquidations (MANDATORY_FLATTEN).
    - Per-minute token bucket: max 100 requests/minute, burst 20.
    """

    _instance: Optional[SchwabRateLimiter] = None
    _singleton_lock: threading.Lock = threading.Lock()

    def __init__(
        self,
        quota_file: Optional[Path | str] = None,
        max_daily_calls: int = MAX_DAILY_CALLS,
        warning_threshold: int = WARNING_THRESHOLD,
        capacity: float = DEFAULT_BURST,
        refill_rate: float = DEFAULT_RPM / 60.0,
    ) -> None:
        self._quota_file = Path(quota_file or DEFAULT_QUOTA_FILE)
        self._max_daily_calls = max_daily_calls
        self._warning_threshold = warning_threshold

        self._bucket = TokenBucket(capacity=capacity, refill_rate=refill_rate)
        self._lock = threading.RLock()

        # In-memory cached quota state
        self._cached_date_utc: Optional[str] = None
        self._cached_count: int = 0
        self._has_warned: bool = False

        self._init_state()

    @classmethod
    def get_instance(
        cls,
        quota_file: Optional[Path | str] = None,
        max_daily_calls: int = MAX_DAILY_CALLS,
        warning_threshold: int = WARNING_THRESHOLD,
        capacity: float = DEFAULT_BURST,
        refill_rate: float = DEFAULT_RPM / 60.0,
    ) -> SchwabRateLimiter:
        """Thread-safe access to the singleton rate limiter."""
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
        """Reset the singleton instance (primarily for unit tests)."""
        with cls._singleton_lock:
            cls._instance = None

    # ------------------------------------------------------------------
    # Persistent State Management
    # ------------------------------------------------------------------

    @staticmethod
    def _current_utc_date() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _init_state(self) -> None:
        with self._lock:
            self._load_or_reset_quota()

    def _load_or_reset_quota(self) -> Tuple[str, int]:
        """Loads quota from disk. If the stored date is older than today UTC, resets count to 0."""
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
                logger.info(
                    "SchwabRateLimiter: 00:00 UTC rollover detected (previous: %s, now: %s). Resetting daily quota.",
                    file_date, today,
                )
                self._cached_date_utc = today
                self._cached_count = 0
                self._save_quota()
        except Exception as exc:
            logger.warning(
                "SchwabRateLimiter: failed to parse quota file %s: %s. Re-initializing.",
                self._quota_file, exc,
            )
            self._cached_date_utc = today
            self._cached_count = 0
            self._save_quota()

        return self._cached_date_utc, self._cached_count

    def _save_quota(self) -> None:
        """Persists current date and call count atomically."""
        self._quota_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "date_utc": self._cached_date_utc,
            "count": self._cached_count,
            "last_updated_utc": datetime.now(timezone.utc).isoformat(),
        }
        data_str = json.dumps(payload, indent=2)

        # Atomic write
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
    # Quota Inquiries & Checks
    # ------------------------------------------------------------------

    def get_daily_count(self) -> int:
        """Returns the current outbound request count for today UTC."""
        with self._lock:
            _, count = self._load_or_reset_quota()
            return count

    def should_throttle_sync(self) -> bool:
        """True if the daily request count exceeds the 3,200 warning threshold."""
        return self.get_daily_count() >= self._warning_threshold

    def is_daily_limit_exceeded(self) -> bool:
        """True if the daily count has reached or exceeded 3,500."""
        return self.get_daily_count() >= self._max_daily_calls

    @staticmethod
    def is_essential_call(action: str = "NORMAL", is_essential: bool = False) -> bool:
        """
        Returns True if the action is essential: order cancellations or emergency liquidations.
        """
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
        }

    # ------------------------------------------------------------------
    # Token & Quota Acquisition
    # ------------------------------------------------------------------

    def acquire(
        self,
        tokens: float = 1.0,
        timeout: Optional[float] = None,
        is_essential: bool = False,
        action: str = "NORMAL",
    ) -> bool:
        """
        Acquire rate-limited token and increment daily quota.

        Rules:
        - If daily count >= 3,500 and request is not essential:
          raises DailyQuotaExceededError.
        - If daily count >= 3,200: logs WARNING.
        - Essential requests (cancellations and MANDATORY_FLATTEN) are permitted even if quota >= 3,500.
        - Acquires token from the per-minute token bucket (max 100 RPM, burst 20).
        """
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

        # Acquire token from the per-minute bucket
        acquired = self._bucket.acquire(tokens=tokens, timeout=timeout)
        if not acquired:
            return False

        # On successful token acquisition, increment daily count
        with self._lock:
            _, count = self._load_or_reset_quota()
            self._cached_count = count + int(tokens)
            self._save_quota()

        return True

    def available(self) -> float:
        """Return available tokens in the bucket."""
        return self._bucket.available()

    def __repr__(self) -> str:
        return (
            f"SchwabRateLimiter(daily_count={self.get_daily_count()}/{self._max_daily_calls}, "
            f"bucket={self._bucket})"
        )


def build_from_config(cfg: dict) -> SchwabRateLimiter:
    """
    Construct or return the singleton SchwabRateLimiter from config.
    """
    rl_cfg = cfg.get("rate_limiter", {})
    capacity = float(rl_cfg.get("capacity", DEFAULT_BURST))
    refill_rate = float(rl_cfg.get("refill_rate", DEFAULT_RPM / 60.0))
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
