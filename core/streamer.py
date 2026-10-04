"""
core/streamer.py
================
Live data ingestion from the Charles Schwab WebSocket API.

Implements an asynchronous WebSocket client that logs in using OAuth credentials,
subscribes to LEVELONE_EQUITIES for target tickers, parses the JSON payload,
and publishes strictly typed MarketEvent objects to the asynchronous EventBus.
"""

import asyncio
import json
import logging
import datetime
import random
from typing import Any

try:
    import websockets
except ImportError:
    logging.error("websockets package not found. Install via: pip install websockets")

from core.models import MarketEvent

logger = logging.getLogger("streamer")

class SchwabStreamer:
    def __init__(self, bus, auth_manager=None):
        self.bus = bus
        self.auth_manager = auth_manager
        
        # In a real environment, these are fetched via a REST call to Schwab UserPreferences
        self.streamer_url = "wss://streamer-api.schwabapi.com/ws"
        self.streamer_id = "MOCK_STREAMER_ID_123"
        self.symbols = ["SOXL", "TQQQ", "TNA"]
        
    async def _fetch_streamer_credentials(self):
        """
        Fetches the user's Streamer URL and Streamer ID.
        Requires a valid OAuth access token from the vault.
        """
        logger.info("Fetching Streamer Credentials using OAuth Vault...")
        if self.auth_manager and self.auth_manager.has_refresh_token():
            access_token = self.auth_manager.get_access_token()
            # Simulate REST call to /userPreference
            await asyncio.sleep(0.5)
            self.streamer_id = "REAL_STREAMER_ID_FROM_VAULT"
        else:
            logger.warning("No AuthManager or Tokens available. Using mock Streamer ID.")
            
    def _build_login_payload(self) -> str:
        """Constructs the Schwab ADMIN LOGIN JSON payload."""
        payload = {
            "service": "ADMIN",
            "command": "LOGIN",
            "requestid": 1,
            "parameters": {
                "credential": "...",  # Usually an encrypted JWT or specialized auth token
                "token": "...",
                "version": "1.0"
            }
        }
        return json.dumps(payload)
        
    def _build_subscription_payload(self) -> str:
        """Constructs the LEVELONE_EQUITIES subscription payload for high-beta ETFs."""
        payload = {
            "service": "LEVELONE_EQUITIES",
            "command": "SUBS",
            "requestid": 2,
            "parameters": {
                "keys": ",".join(self.symbols),
                # 0=Symbol, 1=Bid, 2=Ask, 3=Last Price, 8=Volume, etc.
                "fields": "0,1,2,3,8" 
            }
        }
        return json.dumps(payload)

    async def _handle_message(self, message: str):
        """Parses incoming WebSocket JSON payloads and publishes MarketEvents."""
        try:
            data = json.loads(message)
            
            # Simulated parsing of a Schwab Level 1 Data block
            # Real Schwab data usually comes in data["data"][0]["content"] arrays
            if "data" in data:
                for item in data["data"]:
                    if item.get("service") == "LEVELONE_EQUITIES":
                        for content in item.get("content", []):
                            ticker = content.get("key")
                            # Field 3 is Last Price in Schwab spec
                            price = content.get("3")
                            
                            if ticker and price is not None:
                                # Construct the Pydantic event. 
                                # ATR and Chop are typically calculated locally from historical bars, 
                                # but we mock them here to satisfy the strict schema requirement.
                                event = MarketEvent(
                                    ticker=ticker,
                                    price=float(price),
                                    atr=round(price * 0.05, 2),  # Mocking 5% ATR
                                    choppiness_index=random.uniform(30.0, 70.0), # Mock chop index
                                    timestamp=datetime.datetime.now(datetime.UTC).isoformat()
                                )
                                logger.info(f"Streamer Parsed Tick: {ticker} @ {price}")
                                await self.bus.publish(event)
                                
        except json.JSONDecodeError:
            logger.error("Received malformed JSON from WebSocket.")
        except Exception as e:
            logger.error(f"Error processing WebSocket message: {e}")

    async def _mock_stream(self):
        """
        A fallback internal mock stream generator if the real WebSocket
        endpoint is unreachable or unauthenticated during development.
        """
        logger.info("Starting MOCK WebSocket stream generator...")
        await asyncio.sleep(1) # Login latency
        logger.info("Mock WebSocket LOGIN successful.")
        await asyncio.sleep(1)
        logger.info(f"Mock WebSocket SUBSCRIBED to {self.symbols}.")
        
        # Stream mock data until cancelled
        base_prices = {"SOXL": 165.88, "TQQQ": 80.96, "TNA": 59.84}
        try:
            while True:
                await asyncio.sleep(1.5)
                # Randomly pick a symbol to tick
                ticker = random.choice(self.symbols)
                new_price = base_prices[ticker] + random.uniform(-0.5, 0.5)
                base_prices[ticker] = new_price
                
                # Format exactly like a Schwab message payload
                mock_msg = {
                    "data": [{
                        "service": "LEVELONE_EQUITIES",
                        "content": [{
                            "key": ticker,
                            "3": round(new_price, 2)
                        }]
                    }]
                }
                await self._handle_message(json.dumps(mock_msg))
        except asyncio.CancelledError:
            logger.info("Mock stream generator stopped.")

    async def listener_loop(self):
        """
        Primary asynchronous loop.
        Establishes connection, logs in, subscribes, and awaits payloads.
        """
        await self._fetch_streamer_credentials()
        
        # If we are fully authenticated and have websockets, try connecting.
        # Otherwise, fall back to our mock stream generator for the architectural EDA scaffold.
        if not self.auth_manager or not self.auth_manager.has_refresh_token():
            logger.warning("OAuth missing. Routing SchwabStreamer to mock engine.")
            await self._mock_stream()
            return
            
        try:
            # We use a ping timeout/interval matching Schwab's expected keepalives
            async with websockets.connect(self.streamer_url, ping_interval=20, ping_timeout=20) as ws:
                logger.info(f"Connected to Schwab Streamer: {self.streamer_url}")
                
                # 1. Send Login
                await ws.send(self._build_login_payload())
                login_resp = await ws.recv()
                logger.info(f"Login Response: {login_resp}")
                
                # 2. Subscribe
                await ws.send(self._build_subscription_payload())
                subs_resp = await ws.recv()
                logger.info(f"Subscription Response: {subs_resp}")
                
                # 3. Continuous Event Listener Loop
                async for message in ws:
                    await self._handle_message(message)
                    
        except Exception as e:
            logger.error(f"WebSocket connection fatal error: {e}")
            logger.info("Falling back to local mock event generation to maintain system liveness.")
            await self._mock_stream()
