"""
execution/order_client.py
=========================
Live REST trading execution layer for the Charles Schwab API.
Translates algorithmic execution trajectories into live HTTP POST requests.
"""

import os
import json
import logging
import asyncio

try:
    import httpx
except ImportError:
    logging.warning("httpx not found. Install via: pip install httpx")

logger = logging.getLogger("order_client")

class SchwabOrderClient:
    def __init__(self, auth_manager=None, live_trading: bool = False):
        self.auth_manager = auth_manager
        self.live_trading = live_trading
        self.base_url = "https://api.schwabapi.com/trader/v1"
        
    async def _get_headers(self) -> dict:
        """Dynamically retrieves the valid OAuth 2.0 Bearer token from the AuthManager."""
        token = "MOCK_BEARER_TOKEN"
        if self.auth_manager and self.auth_manager.has_refresh_token():
            token = self.auth_manager.get_access_token()
            
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        
    async def _get_account_hash(self) -> str:
        """Retrieves the encrypted account hash value required for routing."""
        # Typically fetched via GET /trader/v1/accounts
        return "ENCRYPTED_ACCOUNT_HASH_8812A"

    async def _dispatch_order(self, payload: dict, order_type_name: str):
        """
        Core dispatch logic. 
        If LIVE_TRADING is False, strictly logs the payload and bypasses the POST request.
        """
        account_hash = await self._get_account_hash()
        endpoint = f"{self.base_url}/accounts/{account_hash}/orders"
        
        if not self.live_trading:
            logger.warning(f"[DRY-RUN: {order_type_name}] Order safely intercepted. Routing aborted.")
            logger.info(f"JSON Payload Dump:\n{json.dumps(payload, indent=2)}")
            return {"status": "DRY_RUN_INTERCEPTED", "payload": payload}
            
        logger.warning(f"[LIVE TRADING] Dispatching {order_type_name} to {endpoint}...")
        headers = await self._get_headers()
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(endpoint, headers=headers, json=payload)
                response.raise_for_status()
                logger.info(f"Order successfully routed! Response Code: {response.status_code}")
                return response.json()
            except Exception as e:
                logger.error(f"Failed to route order: {e}")
                return {"status": "ERROR", "message": str(e)}

    async def submit_pegged_midpoint_order(self, ticker: str, qty: int, side: str, offset: float = 0.01):
        """
        Constructs the JSON payload for a relative/pegged order to capture temporal queue priority.
        """
        # Formulating the exact Schwab API schema for a pegged/relative limit order
        payload = {
            "orderType": "LIMIT",
            "session": "NORMAL",
            "duration": "DAY",
            "orderStrategyType": "SINGLE",
            "complexOrderStrategyType": "NONE",
            "orderLegCollection": [
                {
                    "instruction": side.upper(),
                    "quantity": qty,
                    "instrument": {
                        "symbol": ticker.upper(),
                        "assetType": "EQUITY"
                    }
                }
            ],
            # To specify a peg in the Schwab schema, specific priceLink/offset nodes are utilized
            "priceLinkBasis": "MARKET_AVERAGE",  # Represents MIDPOINT
            "priceLinkType": "RELATIVE",
            "offset": {
                "amount": offset,
                "offsetType": "VALUE"
            }
        }
        
        await self._dispatch_order(payload, f"PEGGED_MIDPOINT ({side} {qty} {ticker})")

    async def submit_stop_limit_order(self, ticker: str, qty: int, side: str, stop_price: float, limit_price: float):
        """
        Constructs the payload for hard-stop risk mitigation entries/exits.
        """
        payload = {
            "orderType": "STOP_LIMIT",
            "session": "NORMAL",
            "duration": "DAY",
            "orderStrategyType": "SINGLE",
            "stopPrice": round(stop_price, 2),
            "price": round(limit_price, 2),
            "orderLegCollection": [
                {
                    "instruction": side.upper(),
                    "quantity": qty,
                    "instrument": {
                        "symbol": ticker.upper(),
                        "assetType": "EQUITY"
                    }
                }
            ]
        }
        
        await self._dispatch_order(payload, f"STOP_LIMIT ({side} {qty} {ticker})")
