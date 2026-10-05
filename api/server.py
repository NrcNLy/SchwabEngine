"""
api/server.py
=============
FastAPI REST + WebSocket server exposing the engine's internal state to the
React dashboard (and any other consumer).

Every route is registered once on an ``APIRouter`` and mounted at BOTH ``""``
and ``"/api"`` so ``/status`` and ``/api/status`` (and ``/api/v1/...``) resolve.

Design rules
------------
* Nothing here fabricates data. If an environment has no ledger (for example
  ``env=active`` while the engine runs in DRY_RUN) the response says so through
  ``data_source: "UNAVAILABLE"`` instead of inventing balances.
* ``env=active`` -> live Schwab balances/positions; ``env=sandbox`` -> the
  engine's dry-run/simulated pipeline.
* Blocking work (file I/O, Schwab REST, token refresh) runs via
  ``asyncio.to_thread`` so the event loop that also hosts Tier 1 never stalls.

Endpoints (each also available under /api):
    GET  /status?env=               Engine health, NLV, net change, P&L, system_state
    GET  /positions?env=            Engine-managed positions
    GET  /positions/all?env=        Managed + unmanaged positions
    GET  /orders?env=               Trade log
    GET  /ledger?env=               Buckets, risk limits, buying power
    GET  /regime/{symbol}           Per-symbol regime metrics
    GET  /v1/regime/summary         Plain-language macro/regime summary
    GET  /v1/liquidity/policy       Liquidity policy
    PUT  /v1/liquidity/policy       Update liquidity policy (inflow, soft reserve, sweep, gate)
    GET  /v1/liquidity/buying-power Hard/soft reserve breakdown
    GET  /v1/liquidity/state        Collateral invariant + promotional debts + targets
    POST /v1/liquidity/promotional-debt
    POST /v1/liquidity/targets      DELETE /v1/liquidity/targets/{id}
    POST /v1/documents/upload       GET /v1/documents/status/{saved_as}
    POST /strategy/{name}/toggle    POST /strategy/config
    POST /portfolio/scan            GET /portfolio/recommendations
    POST /auth/exchange             POST /auth/refresh
    POST /emergency/halt            POST /emergency/liquidate
    GET  /health
    WS   /stream                    Heartbeat + engine events
"""

import asyncio
import json
import logging
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytz
from pydantic import BaseModel

try:
    from fastapi import (
        APIRouter, BackgroundTasks, FastAPI, File, HTTPException, UploadFile,
        WebSocket, WebSocketDisconnect,
    )
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles
except ImportError:  # pragma: no cover - dependency guard
    FastAPI = None

from core.atomic_io import StateEncoder, read_json, update_json
from core.paths import DOCUMENTS_DIR, MACRO_STATE_FILE
from core.liquidity_policy import LiquidityPolicy, policy_to_dict, save_policy
from core.runtime import (
    EngineContext,  # re-exported: ``from api.server import EngineContext``
    ledger_buying_power,
    lifecycle_phase,
    now_et,
)
from core.session import get_session_phase, is_entry_permitted
from services.regime_summary import build_regime_summary

__all__ = ["build_app", "EngineContext", "compute_system_state"]

logger = logging.getLogger(__name__)
_EDT = pytz.timezone("America/New_York")

STRATEGY_CONFIG_FILE = Path("strategy_config.json")
DOCUMENTS_VAULT_DIR = DOCUMENTS_DIR
DIST_DIR = Path("dist")

ALLOWED_UPLOAD_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg"}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

CORS_ORIGINS = [
    "http://localhost:3000", "http://127.0.0.1:3000",
    "http://localhost:5173", "http://127.0.0.1:5173",
    "http://localhost:8080", "http://127.0.0.1:8080",
]


# -----------------------------------------------------------------
# Pydantic request models (module level so FastAPI can resolve them)
# -----------------------------------------------------------------

class StrategyConfigRequest(BaseModel):
    ci_threshold_regime_a: Optional[float] = None
    ci_threshold_regime_c: Optional[float] = None
    rvol_min_regime_a:     Optional[float] = None
    rsi_oversold:          Optional[float] = None
    llm_mode:              Optional[str]   = None  # "pro", "flash", "disabled"


class AuthExchangeRequest(BaseModel):
    code: str


class HaltRequest(BaseModel):
    halted: bool


class PromotionalDebtRequest(BaseModel):
    id: str
    institution: str
    total_balance: float
    promotional_apr: float = 0.0
    expiration_date: str
    minimum_monthly_payment: float = 0.0
    notes: Optional[str] = ""


class LiquidityTargetRequest(BaseModel):
    target_id: str
    label: str
    target_amount: float
    target_date: str
    is_active: bool = True


# -----------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------

def _plain(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    return obj


def _jsonable(obj: Any) -> Any:
    """Round-trips through StateEncoder (Decimal -> number, date -> ISO string)."""
    return json.loads(json.dumps(_plain(obj), cls=StateEncoder))


def _f(value: Any) -> float:
    return float(value) if value is not None else 0.0


def compute_system_state(ctx: EngineContext) -> Dict[str, Any]:
    """
    Single source of truth for the header status pill.

    States: OK | DEGRADED | DOWN | HALTED | SIMULATED. ``reasons`` explains why
    in plain language (shown as the pill tooltip).
    """
    if ctx.is_halted:
        return {"state": "HALTED", "reasons": ["Kill switch engaged: no new orders are being routed."]}
    if not ctx.live:
        return {"state": "SIMULATED", "reasons": ["Dry-run mode: nothing is sent to Schwab."]}

    down: List[str] = []
    degraded: List[str] = []
    auth = ctx.auth_manager
    if auth is None or not auth.has_refresh_token():
        down.append("Schwab authorization is missing.")
    else:
        remaining = auth.refresh_seconds_remaining()
        if remaining is not None and remaining <= 0:
            down.append("Schwab refresh token has expired; re-authorize.")
        elif remaining is not None and remaining < 86400:
            degraded.append(f"Schwab refresh token expires in {remaining / 3600:.1f}h.")

    sync = ctx.broker_sync
    ledger = ctx.ledgers.get("active")
    if sync is None:
        down.append("Broker sync is not running.")
    else:
        if sync.consecutive_failures >= 3:
            down.append(f"Broker sync failing ({sync.last_error}).")
        elif sync.consecutive_failures > 0:
            degraded.append("Last broker sync failed.")
        age = sync.seconds_since_ok()
        if age is not None and age > max(3 * sync.poll_interval, 90):
            degraded.append(f"Broker data is {age:.0f}s old.")
    if ledger is not None:
        if not ledger.synced:
            degraded.append("Waiting for the first broker balance sync.")
        if ledger.gfv_risk_flag:
            degraded.append("Ledger and broker settled cash disagree.")

    if lifecycle_phase(now_et(), ctx.cfg) == "CORE_SESSION":
        silent = ctx.seconds_since_tick()
        if silent is None or silent > 30:
            degraded.append("No market data in the last 30 seconds.")

    if down:
        return {"state": "DOWN", "reasons": down + degraded}
    if degraded:
        return {"state": "DEGRADED", "reasons": degraded}
    return {"state": "OK", "reasons": []}


def _legacy_status(state: str, auth_ok: bool) -> str:
    if state == "HALTED":
        return "HALTED"
    if state == "DOWN":
        return "OFFLINE" if auth_ok else "WAITING_AUTH"
    return "ONLINE"


def _resolve_env(ctx: EngineContext, env: Optional[str]) -> str:
    if env is None or env == "":
        return "active" if ctx.live else "sandbox"
    value = env.lower()
    if value not in ("active", "sandbox"):
        raise HTTPException(status_code=422, detail="env must be 'active' or 'sandbox'")
    return value


def _data_source(ctx: EngineContext, env: str) -> str:
    ledger = ctx.ledger_for(env)
    return ledger.data_source if ledger is not None else "UNAVAILABLE"


def _pnl(ctx: EngineContext, env: str) -> Tuple[float, float]:
    """(realized_today, unrealized) for an environment."""
    ledger = ctx.ledger_for(env)
    if ledger is None:
        return 0.0, 0.0
    realized = _f(ledger.realized_pnl_today())
    if env == "active" and ctx.broker_sync is not None:
        unrealized = sum((p.current_price - p.avg_cost) * p.quantity for p in ctx.broker_sync.get_positions())
    else:
        unrealized = _f(ledger.unrealized_pnl())
    return round(realized, 2), round(unrealized, 2)


def _managed_rows(ctx: EngineContext, env: str) -> List[Dict[str, Any]]:
    ledger = ctx.ledger_for(env)
    if ledger is None:
        return []
    nlv = ledger.nlv
    cap = _f(ledger.max_single_exposure)
    rows = []
    for pos in ledger.get_positions():
        notional = pos.notional
        rows.append({
            "symbol": pos.symbol,
            "quantity": pos.quantity,
            "entry_price": _f(pos.entry_price),
            "current_price": _f(pos.last_price),
            "notional_value": _f(notional),
            "exposure_pct": round(_f(notional / nlv * 100), 2) if nlv > 0 else 0.0,
            "max_exposure_cap": cap,
            "hard_stop_price": _f(pos.stop_price) if pos.stop_price is not None else None,
            "stop_price": _f(pos.stop_price) if pos.stop_price is not None else None,
            "target_price": _f(pos.target_price) if pos.target_price is not None else None,
            "unrealized_pnl": _f(pos.unrealized_pnl),
            "regime": pos.regime,
            "managed": True,
            "simulated": pos.simulated,
        })
    return rows


def _unmanaged_rows(ctx: EngineContext, env: str) -> List[Dict[str, Any]]:
    if env != "active" or ctx.broker_sync is None:
        return []
    ledger = ctx.ledger_for("active")
    nlv = ledger.nlv if ledger is not None else Decimal(0)
    cap = _f(ledger.max_single_exposure) if ledger is not None else 0.0
    rows = []
    for u in ctx.broker_sync.get_unmanaged_positions():
        notional = u.current_price * u.quantity
        rows.append({
            "symbol": u.symbol,
            "quantity": u.quantity,
            "entry_price": u.avg_cost,
            "avg_cost": u.avg_cost,
            "current_price": u.current_price,
            "notional_value": round(notional, 2),
            "exposure_pct": round(notional / _f(nlv) * 100, 2) if nlv > 0 else 0.0,
            "max_exposure_cap": cap,
            "unrealized_pnl": round((u.current_price - u.avg_cost) * u.quantity, 2),
            "managed": False,
            "stop_order_id": u.stop_order_id,
            "status_flag": "UNMANAGED",
        })
    return rows


def _primary_cash(ctx: EngineContext) -> Tuple[Decimal, Decimal]:
    ledger = ctx.ledger
    if ledger is None:
        return Decimal("0"), Decimal("0")
    return ledger.settled, ledger.unsettled_total


def _collateral_view(ctx: EngineContext, doc: Dict[str, Any]):
    """Rebuilds the CollateralEngine from stored debts/backstop and evaluates it with LIVE cash."""
    from core.collateral_engine import CollateralEngine
    from core.liquidity_models import PromotionalDebt

    engine = CollateralEngine()
    for raw in doc.get("promotional_debts", []) or []:
        engine.add_or_update_promotional_debt(PromotionalDebt(**raw))
    engine.external_liquid_backstop = Decimal(str(doc.get("external_liquid_backstop", 0) or 0))
    settled, unsettled = _primary_cash(ctx)
    return engine, engine.evaluate_invariant(settled, unsettled)


# -----------------------------------------------------------------
# Application factory
# -----------------------------------------------------------------

def build_app(ctx: EngineContext):
    """
    Constructs the FastAPI app with all engine references injected.

    Args:
        ctx: EngineContext holding the live engine objects.

    Returns:
        A configured FastAPI application, or None if FastAPI is not installed.
    """
    if FastAPI is None:
        logger.error("api/server.py: FastAPI/uvicorn not installed. Run: pip install fastapi uvicorn")
        return None

    app = FastAPI(
        title="Schwab Engine API",
        description="Trading engine internal state API for the SchwabEngine dashboard.",
        version="2.0.0",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    router = APIRouter()
    doc_jobs: Dict[str, Dict[str, Any]] = {}

    # ---- WebSocket connection manager --------------------------------

    class _ConnectionManager:
        def __init__(self) -> None:
            self.active: List[WebSocket] = []

        async def connect(self, ws: WebSocket) -> None:
            await ws.accept()
            self.active.append(ws)

        def disconnect(self, ws: WebSocket) -> None:
            if ws in self.active:
                self.active.remove(ws)

        async def broadcast(self, msg: dict) -> None:
            for ws in list(self.active):
                try:
                    await ws.send_json(msg)
                except Exception:
                    self.disconnect(ws)

    manager = _ConnectionManager()

    # ---- status -----------------------------------------------------

    @router.get("/status")
    async def get_status(env: Optional[str] = None):
        env = _resolve_env(ctx, env)
        ledger = ctx.ledger_for(env)
        sys_state = compute_system_state(ctx)
        auth = ctx.auth_manager
        auth_ok = bool(auth and auth.has_refresh_token())
        remaining = auth.refresh_seconds_remaining() if auth else None

        if not ctx.live:
            auth_status = "NOT_REQUIRED"
        elif not auth_ok:
            auth_status = "NEEDS_AUTH"
        elif remaining is not None and remaining <= 0:
            auth_status = "EXPIRED"
        else:
            auth_status = "AUTHORIZED"

        realized, unrealized = _pnl(ctx, env)
        nlv = ledger.nlv if ledger is not None else None
        net_usd = net_pct = None
        if nlv is not None:
            change, pct = ctx.anchors[env].net_change(nlv)
            net_usd = _f(change) if change is not None else None
            net_pct = _f(pct) if pct is not None else None

        managed = _managed_rows(ctx, env)
        unmanaged = _unmanaged_rows(ctx, env)
        drawdown_limit = _f(ledger.daily_drawdown_limit) if ledger is not None else 0.0
        today_pnl = round(realized + unrealized, 2)

        vm_stats = None
        try:
            import psutil
            uptime_s = ctx.uptime_seconds()
            vm_stats = {
                "cpu_pct": psutil.cpu_percent(interval=None),
                "mem_pct": psutil.virtual_memory().percent,
                "api_ping_ms": ctx.broker_sync.last_latency_ms if ctx.broker_sync is not None else None,
                "uptime_string": f"{uptime_s // 86400}d {(uptime_s % 86400) // 3600}h {(uptime_s % 3600) // 60}m",
            }
        except ImportError:
            pass

        return {
            "status": _legacy_status(sys_state["state"], auth_ok),
            "system_state": sys_state["state"],
            "system_reasons": sys_state["reasons"],
            "auth_status": auth_status,
            "auth_expires_in_s": int(remaining) if remaining is not None else None,
            "env": env,
            "data_source": _data_source(ctx, env),
            "engine_mode": "LIVE_TRADING" if ctx.live else "SANDBOX_SIMULATION",
            "is_connected": True,
            "uptime_seconds": ctx.uptime_seconds(),
            "active_positions": len(managed) + len(unmanaged),
            "nlv": _f(nlv) if nlv is not None else None,
            "net_change_usd": net_usd,
            "net_change_pct": net_pct,
            "today_pnl": today_pnl,
            "today_realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "circuit_breaker_limit": drawdown_limit,
            "circuit_breaker_triggered": bool(ledger is not None and today_pnl <= drawdown_limit),
            "external_positions_detected": len(unmanaged) > 0,
            "timestamp_edt": datetime.now(_EDT).isoformat(),
            "lifecycle_phase": lifecycle_phase(now_et(), ctx.cfg),
            "session_phase": get_session_phase(now_et(), ctx.cfg).value,
            "entry_permitted": is_entry_permitted(get_session_phase(now_et(), ctx.cfg), ctx.cfg)[0],
            "phase_sizing_multiplier": is_entry_permitted(get_session_phase(now_et(), ctx.cfg), ctx.cfg)[1],
            "vm_stats": vm_stats,
            "microstructure": ctx.microstructure.telemetry() if ctx.microstructure is not None else {"enabled": False},
        }

    # ---- microstructure indicators ------------------------------------

    @router.get("/indicators")
    async def get_indicators(symbol: Optional[str] = None):
        if ctx.microstructure is None:
            return {"enabled": False, "symbols": {}, "recent_decisions": [], "latency_max_us": 0.0}
        return ctx.microstructure.indicators(symbol)

    # ---- positions / orders / ledger ----------------------------------

    @router.get("/positions")
    async def get_positions(env: Optional[str] = None):
        return _managed_rows(ctx, _resolve_env(ctx, env))

    @router.get("/positions/all")
    async def get_all_positions(env: Optional[str] = None):
        env = _resolve_env(ctx, env)
        return {
            "env": env,
            "data_source": _data_source(ctx, env),
            "managed": _managed_rows(ctx, env),
            "unmanaged": _unmanaged_rows(ctx, env),
        }

    @router.get("/orders")
    async def get_orders(env: Optional[str] = None):
        env = _resolve_env(ctx, env)
        ledger = ctx.ledger_for(env)
        if ledger is None:
            return []
        return [
            {
                "symbol": t.symbol,
                "side": t.side,
                "quantity": t.quantity,
                "price": _f(t.price),
                "cost": _f(t.cost_basis),
                "timestamp": t.timestamp.isoformat(),
                "status": "FILLED",
                "regime": t.regime,
                "simulated": t.simulated,
            }
            for t in ledger.get_trade_log()
        ]

    @router.get("/ledger")
    async def get_ledger(env: Optional[str] = None):
        env = _resolve_env(ctx, env)
        ledger = ctx.ledger_for(env)
        if ledger is None:
            return {
                "env": env, "data_source": "UNAVAILABLE", "available": False, "synced": False,
                "total_nlv": 0.0, "nlv": 0.0, "total_equity": 0.0,
                "bucket1_settled": 0.0, "bucket2_unsettled": 0.0, "bucket3_pending": 0.0,
                "max_single_exposure": 0.0, "max_order_value": 0.0, "max_risk_per_trade": 0.0,
                "daily_drawdown_limit": 0.0, "quarter_kelly_size": 0.0,
                "safe_daytrade_buying_power": 0.0, "gfv_risk_flag": False,
                "swvxx_balance": 0.0, "buying_power": None,
            }
        posterior = None
        kelly = 0.0
        if ctx.risk_manager is not None:
            posterior = ctx.risk_manager.posterior_win_rate()
            kelly = ctx.risk_manager.calculate_bayesian_kelly_sizing(
                wins=ctx.risk_manager.wins, executions=ctx.risk_manager.executions,
                payoff_ratio=ctx.risk_manager.payoff_ratio, account_base=_f(ledger.nlv),
            )
        bp = ledger_buying_power(ctx, env, regime=None, posterior=posterior)
        return {
            "env": env,
            "data_source": ledger.data_source,
            "available": True,
            "synced": ledger.synced,
            "synced_at": ledger.synced_at.isoformat() if ledger.synced_at else None,
            "total_nlv": _f(ledger.nlv),
            "nlv": _f(ledger.nlv),
            "total_equity": _f(ledger.nlv),
            "bucket1_settled": _f(ledger.settled),
            "bucket2_unsettled": _f(ledger.unsettled_total),
            "bucket3_pending": _f(ledger.pending_ach),
            "swvxx_balance": _f(ledger.swvxx_balance),
            "max_single_exposure": _f(ledger.max_single_exposure),
            "max_order_value": _f(bp.max_order_notional),
            "max_risk_per_trade": _f(ledger.max_risk_per_trade),
            "daily_drawdown_limit": _f(ledger.daily_drawdown_limit),
            "quarter_kelly_size": round(_f(kelly), 2),
            "safe_daytrade_buying_power": _f(bp.tactical_float),
            "gfv_risk_flag": ledger.gfv_risk_flag,
            "buying_power": _jsonable(bp),
        }

    # ---- regime ------------------------------------------------------

    @router.get("/regime/{symbol}")
    async def get_regime(symbol: str):
        sym = symbol.upper()
        engine = ctx.strategy_engine
        metrics = engine.get_metrics(sym) if engine is not None else None
        if metrics is None:
            raise HTTPException(status_code=404, detail=f"No data for {sym}")
        return {
            "symbol": sym,
            "regime": metrics.regime.name,
            "regime_code": metrics.regime.value,
            "ci": round(metrics.ci, 2),
            "rvol": round(metrics.rvol, 2),
            "vwap_slope_deg": round(metrics.vwap_slope_deg, 2),
            "rsi14": round(metrics.rsi14, 2),
            "natr": round(metrics.natr, 4),
        }

    @router.get("/v1/regime/summary")
    async def get_regime_summary():
        cfg = await asyncio.to_thread(read_json, STRATEGY_CONFIG_FILE, {})
        return build_regime_summary(cfg or {}, engine_regimes=ctx.engine_regimes())

    # ---- liquidity policy / buying power ------------------------------

    @router.get("/v1/liquidity/policy")
    async def get_liquidity_policy():
        return policy_to_dict(ctx.get_policy())

    @router.put("/v1/liquidity/policy")
    async def put_liquidity_policy(policy: LiquidityPolicy):
        if policy.sweep.mode == "AUTO_WITH_APPROVAL":
            raise HTTPException(
                status_code=422,
                detail="SWVXX sweeps are advisory-only in this build; use mode OFF or ADVISORY.",
            )
        await asyncio.to_thread(save_policy, policy)
        ctx.set_policy(policy)
        await manager.broadcast({"event": "LIQUIDITY_POLICY_UPDATED"})
        return policy_to_dict(policy)

    @router.get("/v1/liquidity/buying-power")
    async def get_buying_power(env: Optional[str] = None):
        env = _resolve_env(ctx, env)
        bp = ledger_buying_power(ctx, env)
        if bp is None:
            return {"env": env, "available": False, "data_source": "UNAVAILABLE", "buying_power": None}
        return {"env": env, "available": True, "data_source": _data_source(ctx, env),
                "buying_power": _jsonable(bp)}

    # ---- strategy / portfolio / news ----------------------------------

    @router.get("/portfolio/recommendations")
    async def get_recommendations():
        if not ctx.portfolio_manager:
            return []
        recs = ctx.portfolio_manager.get_latest_recommendations()
        return [
            {
                "symbol": r.symbol, "quantity": r.quantity, "avg_cost": r.avg_cost,
                "current_price": r.current_price, "unrealized_pnl": round(r.unrealized_pnl, 2),
                "take_profit_price": r.take_profit_price, "stop_loss_price": r.stop_loss_price,
                "time_horizon_days": r.time_horizon_days, "confidence": round(r.confidence, 3),
                "rationale": r.rationale, "mode": r.mode,
                "generated_at": r.generated_at.isoformat(), "order_id": r.order_id,
            }
            for r in recs
        ]

    @router.get("/compression/latest")
    async def get_compression_report():
        if not ctx.compression_agent:
            return {"error": "Compression agent not enabled."}
        report = ctx.compression_agent.get_latest_report()
        return report or {"date": None, "report_text": "No reports generated yet."}

    @router.get("/news/recent")
    async def get_news():
        if not ctx.news_aggregator:
            return []
        return [
            {"title": h.title, "source": h.source, "url": h.url, "fetched_at": h.fetched_at.isoformat()}
            for h in ctx.news_aggregator.get_all_recent()[:20]
        ]

    @router.post("/strategy/{name}/toggle")
    async def toggle_strategy(name: str):
        engine = ctx.strategy_engine
        if engine is None or not hasattr(engine, "toggle_strategy"):
            raise HTTPException(status_code=503, detail="Strategy engine not ready.")
        result = engine.toggle_strategy(name)
        await manager.broadcast({"event": "STRATEGY_TOGGLED", "name": name, "enabled": result})
        return {"strategy": name, "enabled": result}

    @router.post("/strategy/config")
    async def update_strategy_config(req: StrategyConfigRequest):
        engine = ctx.strategy_engine
        if engine is None or not hasattr(engine, "update_config"):
            raise HTTPException(status_code=503, detail="Strategy engine not ready.")
        updates = req.model_dump(exclude_none=True)
        engine.update_config(updates)
        return {"updated": updates}

    @router.post("/portfolio/scan")
    async def trigger_portfolio_scan():
        if not ctx.portfolio_manager:
            raise HTTPException(status_code=503, detail="Portfolio manager not enabled.")
        recs = await asyncio.to_thread(ctx.portfolio_manager.run_morning_scan)
        return {"recommendations_generated": len(recs)}

    # ---- auth ----------------------------------------------------------

    @router.post("/auth/exchange")
    async def auth_exchange(req: AuthExchangeRequest):
        """Completes the OAuth code exchange (blocking HTTP, run in a worker thread)."""
        if ctx.auth_manager is None:
            raise HTTPException(status_code=503, detail="Auth manager not initialized (dry-run mode).")
        try:
            await asyncio.to_thread(
                ctx.auth_manager.exchange_authorization_code,
                auth_code=req.code,
                redirect_uri=ctx.redirect_uri,
            )
        except Exception as exc:
            logger.error("API auth/exchange failed: %s", exc)
            raise HTTPException(status_code=400, detail=str(exc))
        await manager.broadcast({"event": "AUTH_COMPLETE"})
        return {"status": "authorized"}

    @router.post("/auth/refresh")
    async def auth_refresh():
        if ctx.auth_manager is None:
            return {"success": False, "message": "Auth manager not initialized (dry-run mode)."}
        if not Path("schwab_tokens_vault.json").exists():
            return {"success": False, "message": "Vault read error: schwab_tokens_vault.json not found"}
        try:
            await asyncio.to_thread(ctx.auth_manager.force_refresh)
        except Exception as exc:
            return {"success": False, "message": f"Token refresh failed: {exc}"}
        expiry = getattr(ctx.auth_manager, "_access_token_expiry", None)
        expires_in = int(expiry - time.monotonic()) if expiry else 0
        return {
            "success": True,
            "message": "Schwab OAuth token renewed via the encrypted vault.",
            "expires_in_seconds": max(0, expires_in),
        }

    # ---- health / emergency ---------------------------------------------

    @router.get("/health")
    async def get_health():
        sys_state = compute_system_state(ctx)
        return {
            "status": "healthy" if sys_state["state"] in ("OK", "SIMULATED") else sys_state["state"].lower(),
            "system_state": sys_state["state"],
            "reasons": sys_state["reasons"],
            "mode": ctx.mode,
            "halted": ctx.is_halted,
            "ledgers": {k: v is not None for k, v in ctx.ledgers.items()},
        }

    @router.post("/emergency/halt")
    async def emergency_halt(req: HaltRequest):
        ctx.is_halted = req.halted
        await manager.broadcast({"event": "HALT_CHANGED", "halted": req.halted})
        return {
            "success": True,
            "halted": req.halted,
            "message": ("Master Kill Switch ENGAGED. No new orders will be routed."
                        if req.halted else "Trading engine RESUMED."),
        }

    @router.post("/emergency/liquidate")
    async def emergency_liquidate():
        """Halts new entries, then asks the engine to flatten. Reports what actually happened."""
        ctx.is_halted = True
        if ctx.flatten_cb is None:
            return {"success": False, "liquidated_count": 0,
                    "message": "Entries halted, but no flatten routine is registered with the engine."}
        try:
            count = await ctx.flatten_cb("API_EMERGENCY")
        except Exception as exc:
            logger.exception("Emergency liquidation failed")
            return {"success": False, "liquidated_count": 0,
                    "message": f"Entries halted; flatten FAILED: {exc}. Check Schwab directly."}
        await manager.broadcast({"event": "EMERGENCY_LIQUIDATION", "count": count})
        scope = "simulated " if not ctx.live else ""
        return {
            "success": True,
            "liquidated_count": count,
            "message": f"Entries halted; open orders cancelled and {count} {scope}position(s) sent for liquidation.",
        }

    # ---- documents ----------------------------------------------------

    async def _process_document_background(file_path: Path) -> None:
        job = doc_jobs[file_path.name]
        job["status"] = "PROCESSING"
        try:
            from services.document_parser import DocumentParser
            parser = DocumentParser(cash_provider=lambda: _primary_cash(ctx))
            snapshot, is_dup = await parser.extract_document(file_path)
            job.update(status="DUPLICATE" if is_dup else "DONE",
                       document_class=snapshot.document_class.value
                       if hasattr(snapshot.document_class, "value") else str(snapshot.document_class))
            logger.info("Extraction of %s finished (%s).", file_path.name, job["status"])
        except Exception as exc:
            job.update(status="FAILED", error=str(exc))
            logger.error("Extraction of %s failed: %s", file_path.name, exc)

    @router.post("/v1/documents/upload")
    async def upload_document(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
        safe_filename = "".join(c for c in (file.filename or "") if c.isalnum() or c in "._- ")
        suffix = Path(safe_filename).suffix.lower()
        if suffix not in ALLOWED_UPLOAD_SUFFIXES:
            raise HTTPException(status_code=415, detail="Upload a PDF, PNG or JPEG file.")
        contents = await file.read()
        if not contents:
            raise HTTPException(status_code=400, detail="Empty file.")
        if len(contents) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="File exceeds the 20 MB limit.")

        dest_filename = f"AUTO_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{safe_filename}"
        dest_path = DOCUMENTS_VAULT_DIR / dest_filename

        def _store() -> None:
            DOCUMENTS_VAULT_DIR.mkdir(parents=True, exist_ok=True)
            dest_path.write_bytes(contents)

        await asyncio.to_thread(_store)
        doc_jobs[dest_filename] = {"status": "QUEUED"}
        logger.info("Stored uploaded document %s (%d bytes); dispatching background extraction.",
                    dest_filename, len(contents))
        background_tasks.add_task(_process_document_background, dest_path)
        return {
            "success": True,
            "filename": safe_filename,
            "doc_type": "AUTO",
            "saved_as": dest_filename,
            "status": "QUEUED",
        }

    @router.get("/v1/documents/status/{saved_as}")
    async def get_document_status(saved_as: str):
        job = doc_jobs.get(saved_as)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown document.")
        return {"saved_as": saved_as, **job}

    # ---- macro liquidity ------------------------------------------------

    @router.get("/v1/liquidity/state")
    async def get_liquidity_state():
        doc = await asyncio.to_thread(read_json, MACRO_STATE_FILE, {})
        doc = doc if isinstance(doc, dict) else {}
        engine, state = _collateral_view(ctx, doc)
        return {
            "success": True,
            "state": _jsonable(state),
            "promotional_debts": _jsonable(list(engine.promotional_debts.values())),
            "liquidity_targets": doc.get("liquidity_targets", []),
            "snapshot": doc.get("snapshot"),
            "external_liquid_backstop": float(engine.external_liquid_backstop),
            "file_source": doc.get("file_source"),
            "updated_at": doc.get("_updated_at"),
            "sha256": doc.get("sha256"),
            "_is_simulated": not ctx.live,
        }

    @router.get("/v1/liquidity/document-snapshot")
    async def get_document_snapshot():
        doc = await asyncio.to_thread(read_json, MACRO_STATE_FILE, {})
        if isinstance(doc, dict) and doc.get("snapshot"):
            return {"success": True, "snapshot": doc["snapshot"]}
        return {"success": False, "message": "No document snapshot currently loaded."}

    @router.post("/v1/liquidity/promotional-debt")
    async def add_or_update_promotional_debt(req: PromotionalDebtRequest):
        from core.liquidity_models import PromotionalDebt
        try:
            new_debt = PromotionalDebt(
                id=req.id,
                institution=req.institution,
                total_balance=Decimal(str(req.total_balance)),
                promotional_apr=Decimal(str(req.promotional_apr)),
                expiration_date=datetime.strptime(req.expiration_date, "%Y-%m-%d").date(),
                minimum_monthly_payment=Decimal(str(req.minimum_monthly_payment)),
                is_manual=True,
                notes=req.notes or "",
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"Invalid promotional debt: {exc}")

        result: Dict[str, Any] = {}

        def mutate(curr: Dict[str, Any]) -> None:
            engine, _ = _collateral_view(ctx, curr)
            engine.add_or_update_promotional_debt(new_debt)
            settled, unsettled = _primary_cash(ctx)
            new_state = engine.evaluate_invariant(settled, unsettled)
            curr.update({
                "_updated_at": datetime.utcnow().isoformat() + "Z",
                "collateral_state": new_state.model_dump(),
                "promotional_debts": [d.model_dump() for d in engine.promotional_debts.values()],
                "external_liquid_backstop": float(engine.external_liquid_backstop),
            })
            result["state"] = new_state

        await asyncio.to_thread(update_json, MACRO_STATE_FILE, mutate, {})
        return {"success": True, "promotional_debt": _jsonable(new_debt), "new_state": _jsonable(result["state"])}

    @router.post("/v1/liquidity/targets")
    async def create_liquidity_target(req: LiquidityTargetRequest):
        from core.liquidity_models import LiquidityTarget
        try:
            new_target = LiquidityTarget(
                target_id=req.target_id,
                label=req.label,
                target_amount=Decimal(str(req.target_amount)),
                target_date=datetime.strptime(req.target_date, "%Y-%m-%d").date(),
                is_active=req.is_active,
                created_at=date.today(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"Invalid liquidity target: {exc}")

        def mutate(curr: Dict[str, Any]) -> None:
            targets = [LiquidityTarget(**lt) for lt in curr.get("liquidity_targets", [])]
            targets = [t for t in targets if t.target_id != new_target.target_id] + [new_target]
            curr["liquidity_targets"] = [t.model_dump() for t in targets]
            curr["_updated_at"] = datetime.utcnow().isoformat() + "Z"

        await asyncio.to_thread(update_json, MACRO_STATE_FILE, mutate, {})
        return {"success": True, "target": _jsonable(new_target)}

    @router.delete("/v1/liquidity/targets/{target_id}")
    async def delete_liquidity_target(target_id: str):
        def mutate(curr: Dict[str, Any]) -> None:
            curr["liquidity_targets"] = [lt for lt in curr.get("liquidity_targets", [])
                                         if lt.get("target_id") != target_id]
            curr["_updated_at"] = datetime.utcnow().isoformat() + "Z"

        await asyncio.to_thread(update_json, MACRO_STATE_FILE, mutate, {})
        return {"success": True}

    # ---- websocket ---------------------------------------------------------

    @router.websocket("/stream")
    async def websocket_stream(ws: WebSocket):
        await manager.connect(ws)
        try:
            while True:
                env = _resolve_env(ctx, None)
                sys_state = compute_system_state(ctx)
                auth_ok = bool(ctx.auth_manager and ctx.auth_manager.has_refresh_token())
                await ws.send_json({
                    "event": "HEARTBEAT",
                    "status": _legacy_status(sys_state["state"], auth_ok),
                    "system_state": sys_state["state"],
                    "active_positions": len(_managed_rows(ctx, env)),
                    "ts": datetime.now(_EDT).isoformat(),
                })
                await asyncio.sleep(5)
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.debug("WebSocket closed: %s", exc)
        finally:
            manager.disconnect(ws)

    app.include_router(router)
    app.include_router(router, prefix="/api")

    # Expose broadcast so the engine can push events to connected clients
    app.state.broadcast = manager.broadcast

    # ---- built dashboard (vite build -> dist/) --------------------------------
    index_file = DIST_DIR / "index.html"
    if index_file.exists():
        assets_dir = DIST_DIR / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa(full_path: str):
            if full_path.startswith("api/"):
                raise HTTPException(status_code=404, detail="Not found")
            candidate = (DIST_DIR / full_path).resolve()
            if full_path and candidate.is_file() and DIST_DIR.resolve() in candidate.parents:
                return FileResponse(candidate)
            return FileResponse(index_file)

    return app
