"""
data/rest_client.py
===================
Rate-limited Schwab REST API client.

All outbound REST calls are gated through a TokenBucket instance, ensuring
the engine never exceeds 60 RPM — 50% of Schwab's 120 RPM application limit.

Primary responsibility in Phase 2:
    Batch equity quote retrieval via:
        GET /marketdata/v1/quotes?symbols=SOXL,TQQQ,...

Configuration sources:
    api.market_data_base   → base URL for market data endpoints
    api.trader_base        → base URL for trader endpoints
    rate_limiter.*         → forwarded to TokenBucket factory
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests
from requests import Response, Session

from core.rate_limiter import TokenBucket, build_from_config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Retry configuration
# ---------------------------------------------------------------------------
_MAX_RETRIES         = 3
_INITIAL_BACKOFF_SEC = 1.0   # doubles on each retry
_HTTP_RETRY_CODES    = {429, 500, 502, 503, 504}


class SchwabRestClient:
    """
    Authenticated, rate-limited HTTP client for the Schwab REST API.

    Features
    --------
    - Every outbound call acquires one token from the shared TokenBucket
      before sending (blocks if bucket is empty).
    - Automatic exponential backoff on transient HTTP errors
      (429 / 5xx, up to _MAX_RETRIES attempts).
    - Pluggable auth: injects a fresh Bearer token on every request
      via an `auth_manager.get_access_token()` callable.

    Usage
    -----
        client = SchwabRestClient.from_config(cfg, auth_manager)
        quotes = client.get_quotes(["SOXL", "TQQQ", "TNA"])
    """

    def __init__(
        self,
        market_data_base: str,
        trader_base: str,
        token_bucket: TokenBucket,
        auth_manager: Any,          # SchwabAuthManager (avoid circular import)
    ) -> None:
        """
        Args:
            market_data_base: e.g. "https://api.schwabapi.com/marketdata/v1"
            trader_base:      e.g. "https://api.schwabapi.com/trader/v1"
            token_bucket:     Shared TokenBucket instance.
            auth_manager:     Object with a `get_access_token() -> str` method.
        """
        self._market_data_base = market_data_base.rstrip("/")
        self._trader_base      = trader_base.rstrip("/")
        self._bucket           = token_bucket
        self._auth_manager     = auth_manager

        self._session = Session()
        self._session.headers.update({"Accept": "application/json"})

        logger.info(
            "SchwabRestClient ready — market_data=%s, trader=%s",
            self._market_data_base, self._trader_base,
        )

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, cfg: dict, auth_manager: Any) -> "SchwabRestClient":
        """
        Construct a SchwabRestClient from the loaded config.yaml dict.

        Args:
            cfg:          Top-level config dict.
            auth_manager: Initialised SchwabAuthManager.
        """
        api_cfg = cfg.get("api", {})
        bucket  = build_from_config(cfg)
        return cls(
            market_data_base=api_cfg.get(
                "market_data_base", "https://api.schwabapi.com/marketdata/v1"
            ),
            trader_base=api_cfg.get(
                "trader_base", "https://api.schwabapi.com/trader/v1"
            ),
            token_bucket=bucket,
            auth_manager=auth_manager,
        )

    # ------------------------------------------------------------------
    # Public API — Market Data
    # ------------------------------------------------------------------

    def get_quotes(
        self,
        symbols: List[str],
        indicative: bool = False,
    ) -> Dict[str, Any]:
        """
        Fetch real-time Level 1 quotes for one or more equity symbols.

        Spec: GET /marketdata/v1/quotes?symbols=<comma-separated>
              Response: dict keyed by symbol, each containing bid/ask/last/volume etc.

        Args:
            symbols:    List of ticker strings, e.g. ["SOXL", "TQQQ", "TNA"].
            indicative: If True, include indicative quotes (extended hours). Default False.

        Returns:
            Raw Schwab API response dict keyed by symbol.

        Raises:
            requests.HTTPError: On unrecoverable HTTP errors.
            RuntimeError:       If the token bucket times out.
        """
        if not symbols:
            return {}

        params: Dict[str, Any] = {"symbols": ",".join(symbols)}
        if indicative:
            params["indicative"] = "true"

        url = f"{self._market_data_base}/quotes"
        response = self._get(url, params=params)
        return response.json()

    def get_price_history(
        self,
        symbol: str,
        period_type: str = "day",
        period: int = 1,
        frequency_type: str = "minute",
        frequency: int = 1,
        need_extended_hours_data: bool = False,
    ) -> Dict[str, Any]:
        """
        Fetch OHLCV bar history for a symbol.

        Spec: GET /marketdata/v1/pricehistory

        Args:
            symbol:                   Ticker string.
            period_type:              "day" | "month" | "year" | "ytd"
            period:                   Number of periods.
            frequency_type:           "minute" | "daily" | "weekly" | "monthly"
            frequency:                Bar interval (e.g. 1, 5, 15 for minutes).
            need_extended_hours_data: Include pre/post-market bars.

        Returns:
            Raw Schwab API response dict with "candles" list.
        """
        url = f"{self._market_data_base}/pricehistory"
        params = {
            "symbol":                 symbol,
            "periodType":             period_type,
            "period":                 period,
            "frequencyType":          frequency_type,
            "frequency":              frequency,
            "needExtendedHoursData":  str(need_extended_hours_data).lower(),
        }
        return self._get(url, params=params).json()

    # ------------------------------------------------------------------
    # Public API — Trader / Account
    # ------------------------------------------------------------------

    def get_user_preferences(self) -> Dict[str, Any]:
        """
        Fetch user preferences including WebSocket streamer credentials.

        Spec: GET /trader/v1/userPreference
        Required for the WebSocket ADMIN LOGIN handshake (Phase 2 streamer).

        Returns:
            Dict with streamerInfo, quotes preferences, etc.
        """
        url = f"{self._trader_base}/userPreference"
        return self._get(url).json()

    def get_accounts(self, fields: Optional[str] = "positions") -> List[Dict[str, Any]]:
        """
        Retrieve all linked accounts and their positions.

        Spec: GET /trader/v1/accounts?fields=positions

        Args:
            fields: Comma-separated fields to include ("positions", "orders", etc.)

        Returns:
            List of account dicts, each containing accountNumber, hashValue, etc.
        """
        url = f"{self._trader_base}/accounts"
        params = {"fields": fields} if fields else {}
        return self._get(url, params=params).json()

    def get_account_numbers(self) -> List[Dict[str, Any]]:
        """
        Retrieve the mapping of accountNumber → hashValue for all linked accounts.

        Spec: GET /trader/v1/accounts/accountNumbers

        Returns:
            List of dicts: [{"accountNumber": "...", "hashValue": "..."}, ...]
        """
        url = f"{self._trader_base}/accounts/accountNumbers"
        return self._get(url).json()

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    def _auth_headers(self) -> Dict[str, str]:
        """Inject a fresh Bearer token into request headers."""
        token = self._auth_manager.get_access_token()
        return {"Authorization": f"Bearer {token}"}

    def _acquire(self, timeout: float = 30.0, is_essential: bool = False, action: str = "NORMAL") -> bool:
        if getattr(self._auth_manager, "auth_circuit_open", False):
            from core.auth import AuthCircuitBreakerError
            raise AuthCircuitBreakerError(
                "SchwabRestClient: authentication circuit breaker is OPEN (AUTH_LOCKED). "
                "Automated outbound calls suppressed."
            )
        try:
            return self._bucket.acquire(timeout=timeout, is_essential=is_essential, action=action)
        except TypeError:
            return self._bucket.acquire(timeout=timeout)

    def _get(self, url: str, params: Optional[Dict[str, Any]] = None) -> Response:
        """
        Rate-limited GET with exponential-backoff retry.

        1. Acquires a token from the bucket (blocks if needed).
        2. Sends the request with current Bearer token.
        3. On 401, refreshes auth and retries once (token rotation mid-session).
        4. On 429/5xx, backs off exponentially up to _MAX_RETRIES times.

        Returns:
            Successful Response object.

        Raises:
            requests.HTTPError: After all retries are exhausted.
            RuntimeError:       If the rate-limiter bucket acquire times out.
        """
        backoff = _INITIAL_BACKOFF_SEC

        for attempt in range(1, _MAX_RETRIES + 2):  # +1 final raise
            # Rate-limit gate — blocks here if bucket is empty
            acquired = self._acquire(timeout=30, is_essential=False, action="GET")
            if not acquired:
                raise RuntimeError(
                    f"SchwabRestClient: rate-limiter timed out for GET {url}"
                )

            try:
                resp = self._session.get(
                    url,
                    params=params,
                    headers=self._auth_headers(),
                    timeout=10,
                )

                if resp.status_code == 401:
                    logger.warning(
                        "SchwabRestClient: 401 on GET %s — forcing token refresh.", url
                    )
                    self._auth_manager.get_access_token()  # Forces refresh
                    continue

                if resp.status_code in _HTTP_RETRY_CODES:
                    if attempt > _MAX_RETRIES:
                        resp.raise_for_status()
                    logger.warning(
                        "SchwabRestClient: HTTP %d on GET %s — backoff %.1fs (attempt %d/%d)",
                        resp.status_code, url, backoff, attempt, _MAX_RETRIES,
                    )
                    time.sleep(backoff)
                    backoff *= 2
                    continue

                resp.raise_for_status()
                logger.debug("SchwabRestClient: GET %s → %d", url, resp.status_code)
                return resp

            except requests.exceptions.Timeout:
                if attempt > _MAX_RETRIES:
                    raise
                logger.warning(
                    "SchwabRestClient: timeout on GET %s — backoff %.1fs (attempt %d/%d)",
                    url, backoff, attempt, _MAX_RETRIES,
                )
                time.sleep(backoff)
                backoff *= 2

        # Should never reach here, but satisfy type checker
        raise RuntimeError(f"SchwabRestClient: exhausted retries for GET {url}")

    def _post(self, url: str, json_body: dict, is_essential: bool = False, action: str = "POST") -> Response:
        """
        Rate-limited POST with exponential-backoff retry.

        Returns:
            Full Response object (caller must inspect status_code and headers).
            For order placement the caller reads the ``Location`` header.

        Raises:
            requests.HTTPError: After all retries are exhausted.
        """
        backoff = _INITIAL_BACKOFF_SEC

        for attempt in range(1, _MAX_RETRIES + 2):
            acquired = self._acquire(timeout=30, is_essential=is_essential, action=action)
            if not acquired:
                raise RuntimeError(
                    f"SchwabRestClient: rate-limiter timed out for POST {url}"
                )

            try:
                resp = self._session.post(
                    url,
                    json=json_body,
                    headers={**self._auth_headers(), "Content-Type": "application/json"},
                    timeout=10,
                )

                if resp.status_code == 401:
                    logger.warning(
                        "SchwabRestClient: 401 on POST %s — forcing token refresh.", url
                    )
                    self._auth_manager.get_access_token()
                    continue

                if resp.status_code in _HTTP_RETRY_CODES:
                    if attempt > _MAX_RETRIES:
                        resp.raise_for_status()
                    logger.warning(
                        "SchwabRestClient: HTTP %d on POST %s — backoff %.1fs (attempt %d/%d)",
                        resp.status_code, url, backoff, attempt, _MAX_RETRIES,
                    )
                    time.sleep(backoff)
                    backoff *= 2
                    continue

                resp.raise_for_status()
                logger.debug("SchwabRestClient: POST %s → %d", url, resp.status_code)
                return resp

            except requests.exceptions.Timeout:
                if attempt > _MAX_RETRIES:
                    raise
                logger.warning(
                    "SchwabRestClient: timeout on POST %s — backoff %.1fs (attempt %d/%d)",
                    url, backoff, attempt, _MAX_RETRIES,
                )
                time.sleep(backoff)
                backoff *= 2

        raise RuntimeError(f"SchwabRestClient: exhausted retries for POST {url}")

    def _delete(self, url: str, is_essential: bool = True, action: str = "CANCEL") -> Response:
        """
        Rate-limited DELETE with exponential-backoff retry.

        Returns:
            Response object (200 / 200 range on success).
        """
        backoff = _INITIAL_BACKOFF_SEC

        for attempt in range(1, _MAX_RETRIES + 2):
            acquired = self._acquire(timeout=30, is_essential=is_essential, action=action)
            if not acquired:
                raise RuntimeError(
                    f"SchwabRestClient: rate-limiter timed out for DELETE {url}"
                )

            try:
                resp = self._session.delete(
                    url,
                    headers=self._auth_headers(),
                    timeout=10,
                )

                if resp.status_code == 401:
                    logger.warning(
                        "SchwabRestClient: 401 on DELETE %s — forcing token refresh.", url
                    )
                    self._auth_manager.get_access_token()
                    continue

                if resp.status_code in _HTTP_RETRY_CODES:
                    if attempt > _MAX_RETRIES:
                        resp.raise_for_status()
                    logger.warning(
                        "SchwabRestClient: HTTP %d on DELETE %s — backoff %.1fs (attempt %d/%d)",
                        resp.status_code, url, backoff, attempt, _MAX_RETRIES,
                    )
                    time.sleep(backoff)
                    backoff *= 2
                    continue

                resp.raise_for_status()
                logger.debug("SchwabRestClient: DELETE %s → %d", url, resp.status_code)
                return resp

            except requests.exceptions.Timeout:
                if attempt > _MAX_RETRIES:
                    raise
                logger.warning(
                    "SchwabRestClient: timeout on DELETE %s — backoff %.1fs (attempt %d/%d)",
                    url, backoff, attempt, _MAX_RETRIES,
                )
                time.sleep(backoff)
                backoff *= 2

        raise RuntimeError(f"SchwabRestClient: exhausted retries for DELETE {url}")

    # ------------------------------------------------------------------
    # Public API — Order Lifecycle (Phase 6)
    # ------------------------------------------------------------------

    def place_order(self, account_hash: str, order_body: dict, is_essential: bool = False, action: str = "PLACE_ORDER") -> Response:
        """
        Submit a new order for the specified account.

        Spec: POST /trader/v1/accounts/{hashValue}/orders
              HTTP 201 Created → Location header contains the order URL.
              All prices must be formatted to 2 decimal places (``f"{price:.2f}"``).
              Quantity must be a whole integer — no fractional shares.

        Args:
            account_hash: The Schwab ``hashValue`` for the target account.
            order_body:   Fully-constructed order dict (see OrderManager._build_order).

        Returns:
            Full Response. Caller extracts order ID from Location header:
                order_id = response.headers["Location"].rstrip("/").split("/")[-1]

        Raises:
            requests.HTTPError: On HTTP 400 (bad payload), 403, or unrecoverable errors.
        """
        url = f"{self._trader_base}/accounts/{account_hash}/orders"
        resp = self._post(url, order_body, is_essential=is_essential, action=action)

        if resp.status_code != 201:
            logger.error(
                "SchwabRestClient: place_order returned unexpected HTTP %d "
                "(expected 201). Body: %s",
                resp.status_code, resp.text[:200],
            )
            resp.raise_for_status()

        return resp

    def get_order(self, account_hash: str, order_id: str) -> Dict[str, Any]:
        """
        Fetch the current state of a single order.

        Spec: GET /trader/v1/accounts/{hashValue}/orders/{orderId}

        Returns:
            Order dict with fields: orderId, status, filledQuantity,
            remainingQuantity, price, orderType, orderLegCollection, etc.
        """
        url = f"{self._trader_base}/accounts/{account_hash}/orders/{order_id}"
        return self._get(url).json()

    def get_orders(
        self,
        account_hash: str,
        status: Optional[str] = None,
        max_results: int = 200,
    ) -> List[Dict[str, Any]]:
        """
        Retrieve orders for an account, optionally filtered by status.

        Spec: GET /trader/v1/accounts/{hashValue}/orders

        Args:
            account_hash: Schwab account hashValue.
            status:       Optional filter, e.g. "WORKING", "FILLED", "CANCELLED".
            max_results:  Maximum number of orders to return (default 200).

        Returns:
            List of order dicts.
        """
        url    = f"{self._trader_base}/accounts/{account_hash}/orders"
        params: Dict[str, Any] = {"maxResults": max_results}
        if status:
            params["status"] = status
        return self._get(url, params=params).json()

    def cancel_order(self, account_hash: str, order_id: str) -> bool:
        """
        Cancel a resting or working order.

        Spec: DELETE /trader/v1/accounts/{hashValue}/orders/{orderId}
              HTTP 200 on success, 404 if already gone.

        Returns:
            True on successful cancellation, False if order was already gone (404).

        Raises:
            requests.HTTPError: On unexpected non-404 failures.
        """
        url = f"{self._trader_base}/accounts/{account_hash}/orders/{order_id}"
        try:
            self._delete(url)
            return True
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                logger.warning(
                    "SchwabRestClient: cancel_order %s → 404 (already gone).",
                    order_id,
                )
                return False
            raise
