"""
core/rest_client.py
===================
Asynchronous, rate-limited Schwab REST API client.

All outgoing aiohttp / requests sessions are gated through `SchwabRateGovernor`:
- `place_order`, `cancel_order`, and emergency liquidation calls are routed with `priority="CRITICAL_EXIT"`.
- Quote retrieval, account polling, and order status are routed with `priority="NORMAL"`.
- Historical price data backfill is routed with `priority="BACKGROUND"`.
- Detects HTTP 429 and records telemetry into `SchwabRateGovernor.record_response()`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import aiohttp
from aiohttp import ClientResponse, ClientSession

from core.rate_limiter import SchwabRateGovernor, get_governor

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_INITIAL_BACKOFF_SEC = 1.0
_HTTP_RETRY_CODES = {429, 500, 502, 503, 504}


class SchwabAsyncRestClient:
    """
    Asynchronous, rate-limited client for Schwab REST API.
    Gated by `SchwabRateGovernor` with multi-tier priority routing.
    """

    def __init__(
        self,
        market_data_base: str = "https://api.schwabapi.com/marketdata/v1",
        trader_base: str = "https://api.schwabapi.com/trader/v1",
        rate_governor: Optional[SchwabRateGovernor] = None,
        auth_manager: Any = None,
        session: Optional[ClientSession] = None,
    ) -> None:
        self._market_data_base = market_data_base.rstrip("/")
        self._trader_base = trader_base.rstrip("/")
        self._rate_governor = rate_governor or get_governor()
        self._auth_manager = auth_manager
        self._session = session
        self._owns_session = session is None

    async def _get_session(self) -> ClientSession:
        if self._session is None or self._session.closed:
            self._session = ClientSession(headers={"Accept": "application/json"})
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    async def __aenter__(self) -> SchwabAsyncRestClient:
        await self._get_session()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    def _auth_headers(self) -> Dict[str, str]:
        if self._auth_manager and hasattr(self._auth_manager, "get_access_token"):
            token = self._auth_manager.get_access_token()
            return {"Authorization": f"Bearer {token}"}
        return {}

    async def _request(
        self,
        method: str,
        url: str,
        priority: str = "NORMAL",
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Send rate-gated HTTP request with priority scheduling and backoff.
        """
        # Circuit breaker check if auth_manager has one
        if self._auth_manager and getattr(self._auth_manager, "auth_circuit_open", False):
            from core.auth import AuthCircuitBreakerError
            raise AuthCircuitBreakerError(
                "SchwabAsyncRestClient: auth circuit breaker is OPEN. Outbound requests blocked."
            )

        session = await self._get_session()
        backoff = _INITIAL_BACKOFF_SEC

        for attempt in range(1, _MAX_RETRIES + 2):
            # Gate through multi-tier rate governor
            acquired = await self._rate_governor.acquire(priority=priority)
            if not acquired:
                raise RuntimeError(
                    f"SchwabAsyncRestClient: Rate governor rejected {priority} request to {url} (quota limit/freeze)."
                )

            req_headers = {**self._auth_headers(), **(headers or {})}
            if json_data is not None:
                req_headers["Content-Type"] = "application/json"

            try:
                async with session.request(
                    method,
                    url,
                    params=params,
                    json=json_data,
                    headers=req_headers,
                    timeout=aiohttp.ClientTimeout(total=10.0),
                ) as resp:
                    resp_headers = dict(resp.headers)
                    self._rate_governor.record_response(resp.status, resp_headers)

                    if resp.status == 401:
                        logger.warning("SchwabAsyncRestClient: 401 Unauthorized for %s %s. Refreshing auth.", method, url)
                        if self._auth_manager and hasattr(self._auth_manager, "get_access_token"):
                            self._auth_manager.get_access_token()
                        continue

                    if resp.status in _HTTP_RETRY_CODES:
                        if attempt > _MAX_RETRIES:
                            resp.raise_for_status()
                        logger.warning(
                            "SchwabAsyncRestClient: HTTP %d on %s %s. Backoff %.1fs (attempt %d/%d).",
                            resp.status, method, url, backoff, attempt, _MAX_RETRIES,
                        )
                        await asyncio.sleep(backoff)
                        backoff *= 2
                        continue

                    resp.raise_for_status()

                    # For POST order placement, Location header has order ID
                    if resp.status == 201:
                        return {
                            "status": resp.status,
                            "headers": resp_headers,
                            "location": resp_headers.get("Location") or resp_headers.get("location", ""),
                        }

                    if resp.status == 204 or resp.content_type != "application/json":
                        return {"status": resp.status, "headers": resp_headers}

                    return await resp.json()

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt > _MAX_RETRIES:
                    raise
                logger.warning(
                    "SchwabAsyncRestClient: transport error on %s %s (%s). Retrying in %.1fs...",
                    method, url, exc, backoff,
                )
                await asyncio.sleep(backoff)
                backoff *= 2

        raise RuntimeError(f"SchwabAsyncRestClient: exhausted retries for {method} {url}")

    # ------------------------------------------------------------------
    # Market Data (priority="NORMAL", pricehistory priority="BACKGROUND")
    # ------------------------------------------------------------------

    async def get_quotes(self, symbols: List[str], indicative: bool = False) -> Dict[str, Any]:
        """Fetch quotes routed with priority='NORMAL'."""
        if not symbols:
            return {}
        params: Dict[str, Any] = {"symbols": ",".join(symbols)}
        if indicative:
            params["indicative"] = "true"
        url = f"{self._market_data_base}/quotes"
        return await self._request("GET", url, priority="NORMAL", params=params)

    async def get_price_history(
        self,
        symbol: str,
        period_type: str = "day",
        period: int = 1,
        frequency_type: str = "minute",
        frequency: int = 1,
        need_extended_hours_data: bool = False,
    ) -> Dict[str, Any]:
        """Fetch historical price bars routed with priority='BACKGROUND'."""
        url = f"{self._market_data_base}/pricehistory"
        params = {
            "symbol": symbol,
            "periodType": period_type,
            "period": period,
            "frequencyType": frequency_type,
            "frequency": frequency,
            "needExtendedHoursData": str(need_extended_hours_data).lower(),
        }
        return await self._request("GET", url, priority="BACKGROUND", params=params)

    # ------------------------------------------------------------------
    # Trader & Account Polling (priority="NORMAL")
    # ------------------------------------------------------------------

    async def get_user_preferences(self) -> Dict[str, Any]:
        """Fetch user preferences routed with priority='NORMAL'."""
        url = f"{self._trader_base}/userPreference"
        return await self._request("GET", url, priority="NORMAL")

    async def get_accounts(self, fields: Optional[str] = "positions") -> List[Dict[str, Any]]:
        """Fetch accounts and positions routed with priority='NORMAL'."""
        url = f"{self._trader_base}/accounts"
        params = {"fields": fields} if fields else None
        res = await self._request("GET", url, priority="NORMAL", params=params)
        return res if isinstance(res, list) else [res]

    async def get_account_numbers(self) -> List[Dict[str, Any]]:
        """Fetch account hash numbers routed with priority='NORMAL'."""
        url = f"{self._trader_base}/accounts/accountNumbers"
        res = await self._request("GET", url, priority="NORMAL")
        return res if isinstance(res, list) else [res]

    async def get_orders(
        self,
        account_hash: str,
        status: Optional[str] = None,
        max_results: int = 200,
    ) -> List[Dict[str, Any]]:
        """Fetch working/filled orders routed with priority='NORMAL'."""
        url = f"{self._trader_base}/accounts/{account_hash}/orders"
        params: Dict[str, Any] = {"maxResults": max_results}
        if status:
            params["status"] = status
        res = await self._request("GET", url, priority="NORMAL", params=params)
        return res if isinstance(res, list) else [res]

    async def get_order(self, account_hash: str, order_id: str) -> Dict[str, Any]:
        """Fetch single order status routed with priority='NORMAL'."""
        url = f"{self._trader_base}/accounts/{account_hash}/orders/{order_id}"
        return await self._request("GET", url, priority="NORMAL")

    # ------------------------------------------------------------------
    # Critical Order Lifecycle (priority="CRITICAL_EXIT")
    # ------------------------------------------------------------------

    async def place_order(self, account_hash: str, order_body: dict) -> Dict[str, Any]:
        """Submit new order routed with priority='CRITICAL_EXIT'."""
        url = f"{self._trader_base}/accounts/{account_hash}/orders"
        return await self._request("POST", url, priority="CRITICAL_EXIT", json_data=order_body)

    async def cancel_order(self, account_hash: str, order_id: str) -> bool:
        """Cancel working order routed with priority='CRITICAL_EXIT'."""
        url = f"{self._trader_base}/accounts/{account_hash}/orders/{order_id}"
        try:
            await self._request("DELETE", url, priority="CRITICAL_EXIT")
            return True
        except Exception as exc:
            if "404" in str(exc):
                return False
            raise

    async def emergency_liquidation(
        self,
        account_hash: str,
        symbol: str,
        quantity: int,
        action: str = "SELL",
    ) -> Dict[str, Any]:
        """Emergency liquidation market order routed with priority='CRITICAL_EXIT'."""
        order_body = {
            "orderType": "MARKET",
            "session": "NORMAL",
            "duration": "DAY",
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [
                {
                    "instruction": action,
                    "quantity": int(quantity),
                    "instrument": {"symbol": symbol, "assetType": "EQUITY"},
                }
            ],
        }
        return await self.place_order(account_hash, order_body)


# Alias / Sync compatibility export
from data.rest_client import SchwabRestClient  # noqa: E402
