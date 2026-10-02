"""
api/server.py
=============
FastAPI REST + WebSocket server exposing the engine's internal state
to the Android dashboard app and other consumers.

Binds to 0.0.0.0:8080 on the GCP VM. All endpoints read engine state
through thread-safe snapshots — no writes to core engine state except
through the /strategy endpoints which use the engine's own config APIs.

Endpoints:
    GET  /status                    Engine health + auth status
    GET  /positions                 Active (engine-managed) positions
    GET  /positions/all             All Schwab positions (managed + unmanaged)
    GET  /orders                    Today's order history
    GET  /regime/{symbol}           Current regime metrics per symbol
    GET  /ledger                    Cash bucket balances
    GET  /portfolio/recommendations Latest take-profit advisories
    GET  /compression/latest        Latest compression research report
    GET  /news/recent               Recent news headlines
    POST /strategy/{name}/toggle    Enable/disable a strategy
    POST /strategy/config           Update strategy parameters
    POST /portfolio/scan            Trigger an immediate portfolio scan
    POST /auth/exchange             Receive OAuth code from Android deep-link
    WS   /stream                    WebSocket live event feed

Usage:
    import uvicorn
    from api.server import build_app
    app = build_app(engine_context)
    uvicorn.run(app, host="0.0.0.0", port=8080)
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import pytz

logger = logging.getLogger(__name__)
_EDT = pytz.timezone("America/New_York")


def build_app(ctx: "EngineContext"):
    """
    Factory that constructs the FastAPI app with all engine references injected.

    Args:
        ctx: EngineContext dataclass with references to all live engine objects.

    Returns:
        A configured FastAPI application instance.
    """
    try:
        from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
        from fastapi.middleware.cors import CORSMiddleware
        from pydantic import BaseModel
    except ImportError:
        logger.error(
            "api/server.py: FastAPI/uvicorn not installed. "
            "Run: pip install fastapi uvicorn"
        )
        return None

    app = FastAPI(
        title="Schwab Engine API",
        description="Trading engine internal state API for the Android dashboard.",
        version="1.0.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # -----------------------------------------------------------------
    # WebSocket connection manager
    # -----------------------------------------------------------------

    class _ConnectionManager:
        def __init__(self):
            self.active: List[WebSocket] = []

        async def connect(self, ws: WebSocket):
            await ws.accept()
            self.active.append(ws)

        def disconnect(self, ws: WebSocket):
            self.active.remove(ws)

        async def broadcast(self, msg: dict):
            dead = []
            for ws in self.active:
                try:
                    await ws.send_json(msg)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.active.remove(ws)

    manager = _ConnectionManager()

    # -----------------------------------------------------------------
    # Pydantic request models
    # -----------------------------------------------------------------

    class StrategyConfigRequest(BaseModel):
        ci_threshold_regime_a: Optional[float] = None
        ci_threshold_regime_c: Optional[float] = None
        rvol_min_regime_a:     Optional[float] = None
        rsi_oversold:          Optional[float] = None
        llm_mode:              Optional[str]   = None  # "pro", "flash", "disabled"

    class AuthExchangeRequest(BaseModel):
        code: str

    # -----------------------------------------------------------------
    # Endpoints
    # -----------------------------------------------------------------

    @app.get("/status")
    async def get_status():
        auth_ok = ctx.auth_manager.has_refresh_token()
        active_pos = len(getattr(ctx.risk_manager, "_active_positions", {}))
        pnl = 0.0
        if ctx.ledger:
            snap = ctx.ledger.snapshot()
            # Rough today P&L estimate from trade log
            trades = ctx.ledger.get_trade_log()
            buys  = sum(float(t.cost_basis) for t in trades if t.side == "BUY")
            sells = sum(float(t.cost_basis) for t in trades if t.side == "SELL")
            pnl = sells - buys

        external = (
            len(ctx.reconciliation_monitor.get_unmanaged_positions()) > 0
            if ctx.reconciliation_monitor else False
        )

        return {
            "status":                    "ONLINE" if auth_ok else "WAITING_AUTH",
            "auth_status":               "AUTHORIZED" if auth_ok else "NEEDS_AUTH",
            "uptime_seconds":            ctx.uptime_seconds(),
            "active_positions":          active_pos,
            "today_pnl":                 round(pnl, 2),
            "external_positions_detected": external,
            "timestamp_edt":             datetime.now(_EDT).isoformat(),
        }

    @app.get("/positions")
    async def get_positions():
        positions = []
        active = getattr(ctx.risk_manager, "_active_positions", {})
        for sym, pos in active.items():
            positions.append({
                "symbol":        sym,
                "quantity":      pos.quantity,
                "entry_price":   pos.entry_price,
                "current_price": pos.last_price if hasattr(pos, "last_price") else pos.entry_price,
                "stop_price":    pos.current_stop,
                "target_price":  pos.target_price,
                "unrealized_pnl": round(
                    (pos.last_price - pos.entry_price) * pos.quantity
                    if hasattr(pos, "last_price") else 0.0, 2
                ),
                "regime":        "A",  # Will be wired from strategy engine
                "managed":       True,
            })
        return positions

    @app.get("/positions/all")
    async def get_all_positions():
        managed = await get_positions()
        unmanaged = []
        if ctx.reconciliation_monitor:
            for u in ctx.reconciliation_monitor.get_unmanaged_positions():
                unmanaged.append({
                    "symbol":        u.symbol,
                    "quantity":      u.quantity,
                    "avg_cost":      u.avg_cost,
                    "current_price": u.current_price,
                    "unrealized_pnl": round(
                        (u.current_price - u.avg_cost) * u.quantity, 2
                    ),
                    "managed":       False,
                    "stop_order_id": u.stop_order_id,
                })
        return {"managed": managed, "unmanaged": unmanaged}

    @app.get("/orders")
    async def get_orders():
        trades = []
        if ctx.ledger:
            for t in ctx.ledger.get_trade_log():
                trades.append({
                    "symbol":    t.symbol,
                    "side":      t.side,
                    "quantity":  t.quantity,
                    "price":     float(t.cost_basis) / t.quantity if t.quantity > 0 else 0,
                    "cost":      float(t.cost_basis),
                    "timestamp": t.timestamp.isoformat(),
                    "status":    "FILLED",
                })
        return trades

    @app.get("/ledger")
    async def get_ledger():
        if not ctx.ledger:
            return {}
        snap = ctx.ledger.snapshot()
        return {
            "bucket1_settled":     float(snap.bucket1_settled),
            "bucket2_unsettled":   float(snap.bucket2_unsettled),
            "bucket3_pending":     float(snap.bucket3_pending_ach),
            "max_order_value":     float(snap.max_order_value),
        }

    @app.get("/regime/{symbol}")
    async def get_regime(symbol: str):
        sym = symbol.upper()
        state = None
        if ctx.strategy_engine:
            state = ctx.strategy_engine._symbol_states.get(sym)
        if not state:
            raise HTTPException(status_code=404, detail=f"No data for {sym}")
        return {
            "symbol":        sym,
            "regime":        state.regime.name if hasattr(state, "regime") else "UNKNOWN",
            "ci":            round(state.ci, 2) if hasattr(state, "ci") else None,
            "rvol":          round(state.rvol, 2) if hasattr(state, "rvol") else None,
            "vwap_slope_deg": round(state.vwap_slope_deg, 2) if hasattr(state, "vwap_slope_deg") else None,
            "rsi14":         round(state.rsi14, 2) if hasattr(state, "rsi14") else None,
            "natr":          round(state.natr, 4) if hasattr(state, "natr") else None,
        }

    @app.get("/portfolio/recommendations")
    async def get_recommendations():
        if not ctx.portfolio_manager:
            return []
        recs = ctx.portfolio_manager.get_latest_recommendations()
        return [
            {
                "symbol":            r.symbol,
                "quantity":          r.quantity,
                "avg_cost":          r.avg_cost,
                "current_price":     r.current_price,
                "unrealized_pnl":    round(r.unrealized_pnl, 2),
                "take_profit_price": r.take_profit_price,
                "stop_loss_price":   r.stop_loss_price,
                "time_horizon_days": r.time_horizon_days,
                "confidence":        round(r.confidence, 3),
                "rationale":         r.rationale,
                "mode":              r.mode,
                "generated_at":      r.generated_at.isoformat(),
                "order_id":          r.order_id,
            }
            for r in recs
        ]

    @app.get("/compression/latest")
    async def get_compression_report():
        if not ctx.compression_agent:
            return {"error": "Compression agent not enabled."}
        report = ctx.compression_agent.get_latest_report()
        if not report:
            return {"date": None, "report_text": "No reports generated yet."}
        return report

    @app.get("/news/recent")
    async def get_news():
        if not ctx.news_aggregator:
            return []
        headlines = ctx.news_aggregator.get_all_recent()
        return [
            {"title": h.title, "source": h.source, "url": h.url,
             "fetched_at": h.fetched_at.isoformat()}
            for h in headlines[:20]
        ]

    @app.post("/strategy/{name}/toggle")
    async def toggle_strategy(name: str):
        if not ctx.strategy_engine:
            raise HTTPException(status_code=503, detail="Strategy engine not ready.")
        result = ctx.strategy_engine.toggle_strategy(name)
        await manager.broadcast({"event": "STRATEGY_TOGGLED", "name": name, "enabled": result})
        return {"strategy": name, "enabled": result}

    @app.post("/strategy/config")
    async def update_strategy_config(req: StrategyConfigRequest):
        if not ctx.strategy_engine:
            raise HTTPException(status_code=503, detail="Strategy engine not ready.")
        updates = req.dict(exclude_none=True)
        ctx.strategy_engine.update_config(updates)
        return {"updated": updates}

    @app.post("/portfolio/scan")
    async def trigger_portfolio_scan():
        if not ctx.portfolio_manager:
            raise HTTPException(status_code=503, detail="Portfolio manager not enabled.")
        recs = ctx.portfolio_manager.run_morning_scan()
        return {"recommendations_generated": len(recs)}

    @app.post("/auth/exchange")
    async def auth_exchange(req: AuthExchangeRequest):
        """Receive OAuth code from Android deep-link and complete token exchange."""
        try:
            ctx.auth_manager.exchange_authorization_code(
                auth_code=req.code,
                redirect_uri=ctx.redirect_uri,
            )
            await manager.broadcast({"event": "AUTH_COMPLETE"})
            return {"status": "authorized"}
        except Exception as exc:
            logger.error("API auth/exchange failed: %s", exc)
            raise HTTPException(status_code=400, detail=str(exc))

    @app.websocket("/stream")
    async def websocket_stream(ws: WebSocket):
        await manager.connect(ws)
        try:
            while True:
                await asyncio.sleep(5)
                # Broadcast a heartbeat with current status
                auth_ok = ctx.auth_manager.has_refresh_token()
                await ws.send_json({
                    "event":          "HEARTBEAT",
                    "status":         "ONLINE" if auth_ok else "WAITING_AUTH",
                    "active_positions": len(getattr(ctx.risk_manager, "_active_positions", {})),
                    "ts":             datetime.now(_EDT).isoformat(),
                })
        except WebSocketDisconnect:
            manager.disconnect(ws)

    # Expose broadcast so the engine can push events to connected APK clients
    app.state.broadcast = manager.broadcast

    return app


# -----------------------------------------------------------------
# EngineContext — wire-up container
# -----------------------------------------------------------------

class EngineContext:
    """
    Holds references to all live engine objects for dependency injection
    into the FastAPI app. Populated in main.py during build_engine().
    """

    def __init__(self):
        self.auth_manager          = None
        self.risk_manager          = None
        self.order_manager         = None
        self.strategy_engine       = None
        self.ledger                = None
        self.portfolio_manager     = None
        self.compression_agent     = None
        self.news_aggregator       = None
        self.reconciliation_monitor = None
        self.redirect_uri          = "https://127.0.0.1"
        self._start_time           = datetime.utcnow()

    def uptime_seconds(self) -> int:
        return int((datetime.utcnow() - self._start_time).total_seconds())
