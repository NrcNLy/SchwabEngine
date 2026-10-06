"""
main.py
=======
SchwabEngine single-process entrypoint: trading engine + REST/WebSocket API + built dashboard.

Modes
-----
    python main.py                 DRY-RUN (default). Simulated pipeline on mock ticks; nothing
                                   is ever sent to Schwab. Feeds the dashboard's "Sandbox" view.
    python main.py --live          LIVE. Real Schwab data + orders. Fails closed: if the token
                                   vault, credentials, account firewall or first broker balance
                                   sync is not healthy the process exits instead of degrading to
                                   mocks. (``engine.live_trading: true`` in config.yaml is
                                   equivalent to --live; the shipped default is false.)
    python main.py --ignore-session   Dry-run only: allow simulated entries outside 09:30-15:50 ET.
    python main.py --test          Build the API app against an empty dry-run context and exit.

Concurrency: the asyncio loop hosts the API, the schedulers and order tracking; every blocking
broker/file call is pushed to worker threads (``asyncio.to_thread``). Document ingestion and the
LLM governor run as background tasks and can never block Tier 1.
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.atomic_io import read_json
from core.engine import EngineSettings, LiveEngine, SimEngine
from core.liquidity_models import CollateralInvariantState, MacroLiquidityEvent, PromotionalDebt, extract_snapshot
from core.paths import MACRO_STATE_FILE
from core.runtime import EngineContext, build_ledger, ledger_buying_power, load_config
from execution.risk_manager import RiskEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
logger = logging.getLogger("main")


class StartupError(RuntimeError):
    """Raised when LIVE mode cannot start safely."""


class EventBus:
    """In-memory pub/sub used by the dry-run pipeline."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue = asyncio.Queue()
        self._subscribers: List[Any] = []

    def subscribe(self, callback: Any) -> None:
        self._subscribers.append(callback)

    async def publish(self, event: Any) -> None:
        await self._queue.put(event)

    async def _dispatch_loop(self) -> None:
        while True:
            event = await self._queue.get()
            for callback in self._subscribers:
                asyncio.create_task(self._safe(callback, event))
            self._queue.task_done()

    @staticmethod
    async def _safe(callback: Any, event: Any) -> None:
        try:
            await callback(event)
        except Exception:
            logger.exception("Event consumer failed")


async def macro_liquidity_poller(bus: EventBus, poll_interval: float = 2.0) -> None:
    """Watches state/macro_liquidity.json and publishes a passive MacroLiquidityEvent on change."""
    state_file = MACRO_STATE_FILE
    last_mtime = 0.0
    while True:
        try:
            if state_file.exists():
                mtime = state_file.stat().st_mtime
                if mtime > last_mtime:
                    last_mtime = mtime
                    data = await asyncio.to_thread(read_json, state_file, {})
                    if data and "collateral_state" in data:
                        from datetime import datetime
                        await bus.publish(MacroLiquidityEvent(
                            timestamp=datetime.utcnow().isoformat() + "Z",
                            state=CollateralInvariantState(**data["collateral_state"]),
                            snapshot=extract_snapshot(data),
                            promotional_debts=[PromotionalDebt(**d) for d in data.get("promotional_debts", [])],
                        ))
        except Exception as exc:
            logger.warning("Macro liquidity poller: %s", exc)
        await asyncio.sleep(poll_interval)


async def handle_liquidity_event(event: Any) -> None:
    """Passive record-keeping only: document ingestion never changes sizing or throttles risk."""
    if isinstance(event, MacroLiquidityEvent):
        logger.info("Macro liquidity updated -> net collateral buffer $%.2f, solvent=%s",
                    event.state.net_collateral_buffer, event.state.is_solvent)


# ---------------------------------------------------------------------------
# Mode construction
# ---------------------------------------------------------------------------

def resolve_live(args: argparse.Namespace, cfg: dict) -> bool:
    return bool(args.live or (cfg.get("engine", {}) or {}).get("live_trading", False))


def make_governor() -> Optional[Any]:
    """The Tier 2 LLM governor; None (with a warning) if its dependencies/credentials are missing."""
    try:
        from governor import GovernorDaemon
        return GovernorDaemon()
    except (Exception, SystemExit) as exc:
        logger.warning("Governor unavailable (%s); macro analysis disabled. Tier 1 is unaffected.", exc)
        return None


async def build_sim(ctx: EngineContext, cfg: dict, settings: EngineSettings, ignore_session: bool,
                    tasks: List[asyncio.Task]) -> SimEngine:
    from core.streamer import SchwabStreamer as MockStreamer
    from execution.order_client import SchwabOrderClient
    from execution.router import ExecutionRouter

    ledger = build_ledger(cfg, live=False)
    ctx.ledgers["sandbox"] = ledger

    from indicators.microstructure import MicrostructureHub
    ctx.microstructure = MicrostructureHub.from_config(cfg, None)

    bus = EventBus()
    router = ExecutionRouter(order_client=SchwabOrderClient(auth_manager=None, live_trading=False))
    engine = SimEngine(ctx, cfg, ledger, settings, router, ignore_session=ignore_session)
    bus.subscribe(engine.handle_market_event)
    bus.subscribe(handle_liquidity_event)

    tasks.append(asyncio.create_task(bus._dispatch_loop(), name="bus"))
    tasks.append(asyncio.create_task(macro_liquidity_poller(bus), name="liquidity-poller"))
    tasks.append(asyncio.create_task(MockStreamer(bus, auth_manager=None).listener_loop(), name="mock-stream"))
    logger.warning("DRY-RUN: simulated ticks and simulated fills only. Nothing is sent to Schwab.")
    return engine


class LiveHandles:
    """Objects needing explicit shutdown."""
    streamer: Any = None
    auth: Any = None


async def build_live(ctx: EngineContext, cfg: dict, settings: EngineSettings, loop: asyncio.AbstractEventLoop,
                     tasks: List[asyncio.Task], stop: asyncio.Event, handles: LiveHandles) -> LiveEngine:
    """Fails closed: any problem raises StartupError and the process exits."""
    from dotenv import load_dotenv
    from core.auth import SchwabAuthManager, SecurityVault
    from core.universe_mask import UniverseExclusionMask
    from data.rest_client import SchwabRestClient
    from data.streamer import SchwabStreamer as LiveStreamer
    from execution.order_manager import OrderManager
    from execution.strategies import StrategyEngine
    from services.broker_sync import BrokerSync

    load_dotenv()
    client_id = os.getenv("SCHWAB_CLIENT_ID")
    client_secret = os.getenv("SCHWAB_CLIENT_SECRET")
    passphrase = os.getenv("VAULT_PASSPHRASE")
    missing = [k for k, v in (("SCHWAB_CLIENT_ID", client_id), ("SCHWAB_CLIENT_SECRET", client_secret),
                              ("VAULT_PASSPHRASE", passphrase)) if not v]
    if missing:
        raise StartupError(f"Missing environment variables: {', '.join(missing)}")

    auth_cfg = cfg.get("auth", {}) or {}
    vault_path = PROJECT_ROOT / auth_cfg.get("vault_file", "schwab_tokens_vault.json")
    if not vault_path.exists() or vault_path.stat().st_size == 0:
        raise StartupError(f"Token vault not found at {vault_path}. Run manual_auth.py on this machine first.")

    vault = SecurityVault(passphrase=passphrase, iterations=int(auth_cfg.get("pbkdf2_iterations", 600000)),
                          vault_path=vault_path)
    auth = SchwabAuthManager(client_id, client_secret, vault, cfg)
    handles.auth = auth
    try:
        await asyncio.to_thread(auth.load_tokens)
    except Exception as exc:
        raise StartupError(f"Could not decrypt/read the token vault: {exc}") from exc
    remaining = auth.refresh_seconds_remaining()
    if remaining is not None and remaining <= 0:
        raise StartupError("Refresh token has expired. Run manual_auth.py to re-authorize.")
    try:
        await asyncio.to_thread(auth.force_refresh)
    except Exception as exc:
        raise StartupError(f"Token refresh against Schwab failed: {exc}") from exc
    auth.start()
    ctx.auth_manager = auth

    rest = SchwabRestClient.from_config(cfg, auth)
    ctx.rest_client = rest
    ledger = build_ledger(cfg, live=True)
    ctx.ledgers["active"] = ledger

    om = OrderManager(rest, ledger, cfg)
    try:
        await asyncio.to_thread(om.initialize_firewall)
    except Exception as exc:
        raise StartupError(f"Account firewall failed: {exc}") from exc
    ctx.order_manager = om

    sync = BrokerSync(rest, ledger, ctx.anchors["active"], cfg)
    try:
        await asyncio.to_thread(sync.sync_once)
    except Exception as exc:
        raise StartupError(f"First broker balance sync failed: {exc}") from exc
    ctx.broker_sync = sync

    # Adopt existing broker holdings as actively managed inventory
    for bp_item in sync.get_positions():
        stop_val = round(float(bp_item.avg_cost) * (1.0 - float(cfg.get("risk", {}).get("tier2_stop_pct", 0.04))), 2)
        ledger.adopt_position(
            symbol=bp_item.symbol,
            quantity=bp_item.quantity,
            entry_price=bp_item.avg_cost,
            current_price=bp_item.current_price,
            stop_price=stop_val,
            regime="A",
            simulated=False,
        )
    sync.set_managed_symbols(ledger.positions.keys())

    # Conservative pre-check ceiling for StrategyEngine (no soft-reserve draw without a regime/posterior).
    def ceiling():
        bp = ledger_buying_power(ctx, "active")
        return bp.max_order_notional if bp is not None else None

    ledger.set_order_ceiling_provider(ceiling)

    from indicators.microstructure import MicrostructureHub
    micro_hub = MicrostructureHub.from_config(cfg, rest)
    ctx.microstructure = micro_hub

    mask = UniverseExclusionMask(cfg)
    strategy = StrategyEngine(cfg, ledger, mask)
    ctx.strategy_engine = strategy
    engine = LiveEngine(ctx, cfg, ledger, settings, om, strategy, loop)

    for sym in sorted(mask.universe):
        strategy.register_symbol(sym)
        try:
            await asyncio.to_thread(strategy.preload_historical, sym, rest)
        except Exception as exc:
            logger.warning("History preload failed for %s (%s); regime stays unclassified until bars accrue.", sym, exc)

    streamer = LiveStreamer.from_config(cfg, rest)
    if micro_hub is not None:
        all_equity_symbols = sorted(mask.universe | set(micro_hub.equity_reference_symbols()))
        streamer.set_symbols(all_equity_symbols)
        streamer.set_book_subscriptions(micro_hub.book_symbols(), micro_hub.book_services)
        futures_syms = micro_hub.futures_symbols()
        if futures_syms:
            streamer.set_futures_symbols(futures_syms)
        streamer.on_book = engine.on_book
    else:
        streamer.set_symbols(sorted(mask.universe))
    streamer.on_tick = engine.on_tick
    streamer.start()
    handles.streamer = streamer

    tasks.append(asyncio.create_task(engine.run_broker_sync(stop), name="broker-sync"))
    logger.warning("LIVE MODE: real orders will be routed to Schwab account ...%s.",
                   str(cfg.get("account", {}).get("required_suffix", "")))
    return engine


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def run(args: argparse.Namespace) -> int:
    cfg = load_config()
    live = resolve_live(args, cfg)
    if live and args.ignore_session:
        logger.critical("--ignore-session is refused in LIVE mode.")
        return 2

    ctx = EngineContext(cfg, live=live)
    ctx.risk_manager = RiskEngine()
    await asyncio.to_thread(ctx.load_persisted_policy)

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    tasks: List[asyncio.Task] = []
    handles = LiveHandles()
    settings = EngineSettings.from_cfg(cfg)

    try:
        if live:
            engine = await build_live(ctx, cfg, settings, loop, tasks, stop, handles)
        else:
            engine = await build_sim(ctx, cfg, settings, args.ignore_session, tasks)
    except StartupError as exc:
        logger.critical("LIVE startup refused: %s", exc)
        for t in tasks:
            t.cancel()
        if handles.auth is not None:
            handles.auth.stop()
        return 3

    ctx.flatten_cb = engine.flatten_all

    # Verification ping to ntfy alert topic on startup
    try:
        from core.notifier import send_alert
        send_alert(
            title="SchwabEngine Startup",
            message="SchwabEngine Alert Pipe Active on schwab-trader VM",
            priority="default",
            tags=["rocket", "satellite"],
        )
    except Exception:
        pass

    governor = make_governor() if (live or args.governor) else None
    tasks.append(asyncio.create_task(engine.run_schedulers(stop, governor), name="schedulers"))

    server_cfg = cfg.get("server", {}) or {}
    host = args.host or server_cfg.get("host", "0.0.0.0")
    port = int(args.port or server_cfg.get("port", 8080))
    try:
        if server_cfg.get("enabled", True) and not args.no_api:
            import uvicorn
            from api.server import build_app
            app = build_app(ctx)
            if app is None:
                logger.critical("FastAPI unavailable; install requirements.txt.")
                return 4
            server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info"))
            logger.info("Dashboard + API on http://%s:%s", host, port)
            await server.serve()
        else:
            await stop.wait()
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if handles.streamer is not None:
            await asyncio.to_thread(handles.streamer.stop)
        if ctx.microstructure is not None:
            await asyncio.to_thread(ctx.microstructure.persist)
        if handles.auth is not None:
            handles.auth.stop()
        open_positions = sorted(engine.ledger.positions.keys())
        if open_positions:
            logger.warning("Shutting down with open engine positions %s (broker-side catastrophe stops, if "
                           "placed, remain working).", open_positions)
        logger.info("Engine stopped.")
    return 0


def run_self_test() -> int:
    from api.server import build_app
    cfg = load_config()
    ctx = EngineContext(cfg, live=False)
    ctx.ledgers["sandbox"] = build_ledger(cfg, live=False)
    ctx.risk_manager = RiskEngine()
    app = build_app(ctx)
    if app is None:
        return 1
    logger.info("Test mode: FastAPI app built successfully. Contract verified.")
    return 0


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SchwabEngine")
    p.add_argument("--live", action="store_true", help="Route real orders to Schwab (default: dry-run).")
    p.add_argument("--ignore-session", action="store_true", help="Dry-run only: allow entries outside market hours.")
    p.add_argument("--governor", action="store_true", help="Dry-run: also run the Vertex AI governor schedule.")
    p.add_argument("--no-api", action="store_true", help="Do not start the HTTP API.")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--test", action="store_true", help="Build the API app and exit.")
    return p.parse_args(argv)


if __name__ == "__main__":
    os.chdir(PROJECT_ROOT)
    _args = parse_args()
    if _args.test:
        sys.exit(run_self_test())
    try:
        sys.exit(asyncio.run(run(_args)))
    except KeyboardInterrupt:
        logger.info("Manual interrupt.")
