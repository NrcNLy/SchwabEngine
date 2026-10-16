"""
web/app.py
==========
FastAPI Real-Time WebSocket Telemetry Gateway & Dashboard Bridge (#68).
Runs on Core 1 (schwab-web.service).

Exposes:
- Real-time WebSocket endpoint: /ws/telemetry
- HTTP polling fallback endpoint: GET /api/telemetry/snapshot
- Simulation sandbox frontend: GET /simulation_sandbox
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from web.websocket_manager import telemetry_manager

logger = logging.getLogger("SchwabEngine.WebGateway")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SANDBOX_HTML_PATH = Path(__file__).resolve().parent / "simulation_sandbox.html"


def create_gateway_app() -> FastAPI:
    """
    Creates or extends the FastAPI application for the Web Gateway on Core 1.
    Integrates all REST routes from api.server and adds real-time WebSocket telemetry.
    """
    base_app: Optional[FastAPI] = None
    try:
        from api.server import app as _server_app
        base_app = _server_app
    except Exception as exc:
        logger.warning("Could not import api.server.app directly: %s", exc)

    if base_app is not None:
        app = base_app
    else:
        app = FastAPI(
            title="SchwabEngine Real-Time Telemetry Gateway",
            description="WebSocket Telemetry & Simulation Sandbox Gateway on Core 1",
            version="2.0.0",
        )
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Register WebSocket Telemetry Endpoint (#68)
    @app.websocket("/ws/telemetry")
    async def websocket_telemetry(ws: WebSocket):
        """
        High-concurrency 1Hz real-time telemetry stream:
        - ISO-8601 Timestamp
        - Engine Execution State (STANDBY|RUNNING|FLUSHED)
        - Macro Semiconductor Environment Score (SES) and regime state
        - T+1 Cash settlement balances
        - Active Yang-Zhang dynamic volatility envelope coordinates
        """
        await telemetry_manager.connect(ws)
        try:
            while True:
                msg = await ws.receive_text()
                if msg == "ping":
                    try:
                        await ws.send_json({"type": "pong"})
                    except Exception:
                        break
        except WebSocketDisconnect:
            telemetry_manager.disconnect(ws)
        except Exception as exc:
            logger.debug("WebSocket connection terminated: %s", exc)
            telemetry_manager.disconnect(ws)

    # Register REST Polling Fallback (#68)
    @app.get("/api/telemetry/snapshot")
    @app.get("/telemetry/snapshot")
    async def get_telemetry_snapshot():
        """
        HTTP polling fallback returning unified telemetry payload snapshot.
        """
        return telemetry_manager.get_latest_payload()

    # Register Interactive Simulation Sandbox UI Route (#68)
    @app.get("/simulation_sandbox", response_class=HTMLResponse)
    @app.get("/sandbox", response_class=HTMLResponse)
    async def get_simulation_sandbox_ui():
        """Serves the interactive simulation sandbox HTML dashboard."""
        if SANDBOX_HTML_PATH.exists():
            return HTMLResponse(content=SANDBOX_HTML_PATH.read_text(encoding="utf-8"))
        raise HTTPException(status_code=404, detail="simulation_sandbox.html not found.")

    return app


app = create_gateway_app()
