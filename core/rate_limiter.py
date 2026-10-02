"""
core/rate_limiter.py
====================
Thread-safe Token Bucket rate limiter.

Caps Schwab REST API consumption at 60 RPM — exactly 50% of the Schwab
application limit of 120 RPM — providing headroom for burst tolerance.

Configuration source: config.yaml → rate_limiter.capacity / refill_rate
"""

import threading
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class TokenBucket:
    """
    Thread-safe token bucket implementation.

    Tokens refill continuously at `refill_rate` tokens/second up to `capacity`.
    Each API call must acquire one token via `acquire()` before proceeding.

    Spec:
        capacity    = 60.0  (from config.yaml → rate_limiter.capacity)
        refill_rate = 1.0   (from config.yaml → rate_limiter.refill_rate)
        → Effective rate ceiling: 60 requests/minute (50% of Schwab's 120 RPM)
    """

    def __init__(self, capacity: float, refill_rate: float) -> None:
        """
        Args:
            capacity:    Maximum token reservoir (burst ceiling). Tokens/request = 1.
            refill_rate: Tokens added per second (continuous refill).
        """
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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _refill(self) -> None:
        """Credit elapsed tokens since last refill (call inside lock)."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        credited = elapsed * self._refill_rate
        self._tokens = min(self._capacity, self._tokens + credited)
        self._last_refill = now

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(self, tokens: float = 1.0, timeout: Optional[float] = None) -> bool:
        """
        Block until `tokens` tokens are available and consume them.

        Args:
            tokens:  Number of tokens to consume (default 1.0 per API call).
            timeout: Maximum seconds to wait. None = wait indefinitely.
                     Returns False immediately if timeout elapses.

        Returns:
            True  — tokens acquired, caller may proceed with the API request.
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
                    logger.debug(
                        "TokenBucket: acquired %.1f token(s), %.2f remaining",
                        tokens, self._tokens,
                    )
                    return True

                # Calculate how long until enough tokens are available
                deficit = tokens - self._tokens
                wait_needed = deficit / self._refill_rate

            # Check timeout before sleeping
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

            # Sleep outside lock so other threads can proceed
            time.sleep(min(sleep_duration, 0.05))  # Max 50ms sleep granularity

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


# ---------------------------------------------------------------------------
# Factory — build from config.yaml
# ---------------------------------------------------------------------------

def build_from_config(cfg: dict) -> TokenBucket:
    """
    Construct a TokenBucket using values from the loaded config.yaml dict.

    Expected config structure:
        rate_limiter:
          capacity: 60.0
          refill_rate: 1.0

    Args:
        cfg: The top-level config dict (output of yaml.safe_load).

    Returns:
        Configured TokenBucket instance.
    """
    rl_cfg = cfg.get("rate_limiter", {})
    capacity = float(rl_cfg.get("capacity", 60.0))
    refill_rate = float(rl_cfg.get("refill_rate", 1.0))
    return TokenBucket(capacity=capacity, refill_rate=refill_rate)
