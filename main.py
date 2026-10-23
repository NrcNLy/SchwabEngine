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
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent
DRIVE_SYNC_INTERVAL_S = 600.0  # shadow_cycle triggers a Local -> Drive mirror pass this often
sys.path.insert(0, str(PROJECT_ROOT))

# Active configuration path (set from --config in run()/main); every load_config() call in this module
# goes through load_active_config() so the CLI-selected file is the single source of truth.
_ACTIVE_CONFIG_PATH: str = "config/config.yaml"

# Minimum settled funds required before --mode live is allowed to route orders
MIN_LIVE_SETTLED_FUNDS = Decimal("2000.00")

from core.atomic_io import read_json
from core.engine import EngineSettings, LiveEngine, SimEngine
from core.liquidity_models import CollateralInvariantState, MacroLiquidityEvent, PromotionalDebt, extract_snapshot
from core.paths import MACRO_STATE_FILE
from core.runtime import EngineContext, build_ledger, ledger_buying_power, load_config
from execution.risk_manager import RiskEngine
from execution.bracket_manager import MultiTrancheBracket, MultiTrancheBracketManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
logger = logging.getLogger("main")


def load_active_config() -> dict:
    """Loads the YAML selected via --config (default config/config.yaml)."""
    return load_config(_ACTIVE_CONFIG_PATH)


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
    """LIVE only when --mode live (or the legacy --live alias) was explicitly requested."""
    return str(getattr(args, "mode", "shadow")).lower() == "live"


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
    if ctx.risk_manager is not None:
        ctx.risk_manager.sync_account_state(
            equity=float(ledger.nlv),
            cash=float(ledger.settled_cash),
            positions=ledger.positions,
        )

    logger.warning("DRY-RUN: simulated ticks and simulated fills only. Nothing is sent to Schwab.")
    return engine


class LiveHandles:
    """Objects needing explicit shutdown."""
    streamer: Any = None
    auth: Any = None


async def _bootstrap_auth(ctx: EngineContext, cfg: dict, handles: "LiveHandles") -> tuple:
    """Vault decrypt -> forced OAuth refresh -> authenticated REST client. Fails closed with StartupError."""
    from dotenv import load_dotenv
    from core.auth import SchwabAuthManager, SecurityVault
    from core.paths import STATE_DIR
    from data.rest_client import SchwabRestClient

    load_dotenv()
    client_id = os.getenv("SCHWAB_CLIENT_ID")
    client_secret = os.getenv("SCHWAB_CLIENT_SECRET")
    passphrase = os.getenv("VAULT_PASSPHRASE")
    missing = [k for k, v in (("SCHWAB_CLIENT_ID", client_id), ("SCHWAB_CLIENT_SECRET", client_secret),
                              ("VAULT_PASSPHRASE", passphrase)) if not v]
    if missing:
        raise StartupError(f"Missing environment variables: {', '.join(missing)}")

    auth_cfg = cfg.get("auth", {}) or {}
    vault_file_name = auth_cfg.get("vault_file", "schwab_tokens_vault.json")
    candidates = [
        PROJECT_ROOT / vault_file_name,
        STATE_DIR / vault_file_name,
        Path(vault_file_name),
    ]
    vault_path = next((p for p in candidates if p.exists() and p.stat().st_size > 0), None)
    if vault_path is None:
        raise StartupError(f"Token vault not found at {PROJECT_ROOT / vault_file_name} or {STATE_DIR / vault_file_name}. Run scripts/auth/manual_auth.py on this machine first.")

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
        raise StartupError("Refresh token has expired. Run scripts/auth/manual_auth.py to re-authorize.")
    try:
        await asyncio.to_thread(auth.force_refresh)
    except Exception as exc:
        raise StartupError(f"Token refresh against Schwab failed: {exc}") from exc
    auth.start()
    ctx.auth_manager = auth

    rest = SchwabRestClient.from_config(cfg, auth)
    ctx.rest_client = rest
    return auth, rest


async def build_shadow(ctx: EngineContext, cfg: dict, settings: EngineSettings, loop: asyncio.AbstractEventLoop,
                       tasks: List[asyncio.Task], stop: asyncio.Event, handles: "LiveHandles",
                       ignore_session: bool) -> SimEngine:
    """
    Shadow mode: authenticated LIVE Level 1 market data, simulated execution. No order is ever submitted
    to the broker; mock orders and rebalance sweeps are written to logs/session_telemetry_<YYMMDD>.jsonl.
    """
    import time as _time
    from core.ipc import write_control_signal
    from core.shadow import (
        SHADOW_UNIVERSE, ShadowRebalancer, ShadowTelemetrySink, load_strategy_targets, session_telemetry_path,
    )
    from data.macro_client import MacroClient
    from data.multi_rate_sync import MultiRateStateSynchronizer
    from data.streamer import SchwabStreamer as LiveStreamer
    from execution.order_client import SchwabOrderClient
    from execution.risk_manager import MetaGovernorHMM
    from execution.router import ExecutionRouter
    from indicators.microstructure import MicrostructureHub

    auth, rest = await _bootstrap_auth(ctx, cfg, handles)

    ledger = build_ledger(cfg, live=False)
    ctx.ledgers["sandbox"] = ledger
    ctx.microstructure = MicrostructureHub.from_config(cfg, None)
    router = ExecutionRouter(order_client=SchwabOrderClient(auth_manager=None, live_trading=False))
    engine = SimEngine(ctx, cfg, ledger, settings, router, ignore_session=ignore_session)

    # Disengage the Master Kill Switch so mock orders and rebalance sweeps are evaluated and logged
    ctx.is_halted = False
    (cfg.setdefault("trading", {}))["kill_switch"] = False
    await asyncio.to_thread(write_control_signal, "RESUME", "Shadow mode startup: kill switch disengaged (no broker routing)")

    # Warm plane: read-only account hot boot
    suffix = str((cfg.get("account", {}) or {}).get("required_suffix", ""))
    accounts = await asyncio.to_thread(rest.get_accounts, "positions")
    target = None
    for acct in accounts:
        sec = acct.get("securitiesAccount", {})
        if suffix and str(sec.get("accountNumber", "")).endswith(suffix):
            target = acct
            break
    if target is None:
        raise StartupError(f"Shadow startup: no linked account ending in '{suffix}'.")

    sync = MultiRateStateSynchronizer()
    sync.on_account_sync(target, is_hot_boot=True)

    # Hot plane seed from REST snapshot (so marks and the HYG/IEF proxy exist before the first WS tick)
    snapshot = await asyncio.to_thread(rest.get_quotes, list(SHADOW_UNIVERSE))
    closes = {}
    for sym in SHADOW_UNIVERSE:
        entry = snapshot.get(sym) or {}
        quote = entry.get("quote") or {}
        if quote:
            sync.on_level1_quote({"symbol": sym, "quote": quote})
            closes[sym] = float(quote.get("closePrice") or 0.0)
    if closes.get("HYG", 0.0) > 0.0 and closes.get("IEF", 0.0) > 0.0:
        sync.set_credit_reference(closes["HYG"], closes["IEF"], source="PRIOR_CLOSE")

    # Cold plane: FRED anchor + NLP sentiment, HMM filter. Intraday proxy comes from the live HYG/IEF ratio.
    hmm = MetaGovernorHMM()
    macro_client = MacroClient(sync=sync, hmm=hmm)
    cold = await asyncio.to_thread(macro_client.run_premarket_cold_plane_pipeline)

    sink = ShadowTelemetrySink(session_telemetry_path(PROJECT_ROOT))
    ctx.shadow_sink = sink
    state0 = sync.get_live_portfolio_state()
    sink.write(
        "SHADOW_SESSION_START",
        account=f"***{str(target['securitiesAccount'].get('accountNumber', ''))[-4:]}",
        nlv=state0.net_liquidation_value,
        cash=state0.cash_buffer,
        open_positions=len(state0.positions),
        universe=list(SHADOW_UNIVERSE),
        credit_proxy=sync.get_credit_proxy_state(),
        hmm_posterior=list(cold.hmm_posterior_vector),
        dominant_regime=cold.dominant_regime,
        broker_submission="DISABLED",
        market_data="LIVE_SCHWAB_WEBSOCKET",
    )

    rebalancer = ShadowRebalancer(sync, sink, hmm, load_strategy_targets(PROJECT_ROOT))
    last_logged: dict = {}

    def on_tick(sym: str, fields: dict) -> None:
        update = sync.on_streamer_tick(sym, fields)
        px = update.mid_price if update.mid_price > 0.0 else update.last
        if px <= 0.0:
            return
        try:
            ledger.mark_price(sym.upper(), Decimal(str(px)))
        except Exception:
            logger.debug("Sandbox ledger mark skipped for %s", sym)
        ctx.mark_tick("LIVE")
        now = _time.monotonic()
        if now - last_logged.get(sym, 0.0) >= 2.0:
            last_logged[sym] = now
            extra = {}
            if sym.upper() in ("HYG", "IEF"):
                extra["credit_proxy"] = sync.get_credit_proxy_state()
            sink.write("TICK", symbol=sym.upper(), bid=update.bid, ask=update.ask, last=update.last,
                       mid=update.mid_price, microprice=update.microprice, bid_size=update.bid_size,
                       ask_size=update.ask_size, volume=update.volume, source="LIVE_SCHWAB_WEBSOCKET", **extra)

    streamer = LiveStreamer.from_config(cfg, rest)
    streamer.set_symbols(list(SHADOW_UNIVERSE))
    streamer.on_tick = on_tick
    streamer.start()
    handles.streamer = streamer

    async def shadow_cycle() -> None:
        from datetime import time as dtime
        from core.engine import now_et
        from core.session import TradingPhase, get_session_phase
        from core.shadow import run_drive_sync, run_eod_digest_and_sync

        await asyncio.sleep(5.0)
        eod_digest_fired = None
        drive_sync_task: Optional[asyncio.Task] = None
        last_drive_sync = 0.0
        while not stop.is_set():
            try:
                await asyncio.to_thread(rebalancer.evaluate)
                loop_now = time.monotonic()
                if loop_now - last_drive_sync >= DRIVE_SYNC_INTERVAL_S and (drive_sync_task is None or drive_sync_task.done()):
                    last_drive_sync = loop_now
                    drive_sync_task = asyncio.create_task(run_drive_sync(PROJECT_ROOT))
                now_val = now_et()
                today_val = now_val.date()
                phase_val = get_session_phase(now_val, cfg)
                if (now_val.time() >= dtime(15, 55) or phase_val in (TradingPhase.POST_CLOSE_REFLECTION, TradingPhase.OFFLINE)) and eod_digest_fired != today_val:
                    eod_digest_fired = today_val
                    logger.info("Automated EOD lifecycle trigger (%s, %s): executing telemetry digest and Drive sync...", now_val.time().strftime("%H:%M:%S"), phase_val)
                    asyncio.create_task(run_eod_digest_and_sync(PROJECT_ROOT, log_path=sink.path))
            except Exception:
                logger.exception("Shadow rebalance cycle failed")
            await asyncio.sleep(30.0)

    tasks.append(asyncio.create_task(shadow_cycle(), name="shadow-cycle"))
    logger.warning("SHADOW MODE: live Schwab Level 1 stream on %d ETFs; broker order submission DISABLED; "
                   "telemetry -> %s", len(SHADOW_UNIVERSE), sink.path)
    return engine


async def build_live(ctx: EngineContext, cfg: dict, settings: EngineSettings, loop: asyncio.AbstractEventLoop,
                     tasks: List[asyncio.Task], stop: asyncio.Event, handles: LiveHandles) -> LiveEngine:
    """Fails closed: any problem raises StartupError and the process exits."""
    from core.universe_mask import UniverseExclusionMask
    from data.streamer import SchwabStreamer as LiveStreamer
    from execution.order_manager import OrderManager
    from execution.strategies import StrategyEngine
    from services.broker_sync import BrokerSync

    auth, rest = await _bootstrap_auth(ctx, cfg, handles)
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

    # LIVE confirmation gate: settled funds must cover the $2,000 minimum, verified against the broker
    settled_funds = Decimal(str(ledger.settled_cash))
    if settled_funds < MIN_LIVE_SETTLED_FUNDS:
        raise StartupError(
            f"Live confirmation gate failed: broker-verified settled funds ${settled_funds:.2f} "
            f"< required ${MIN_LIVE_SETTLED_FUNDS:.2f}. Re-run with --mode shadow."
        )
    logger.info("Live confirmation gate passed: settled funds $%.2f >= $%.2f (broker verified).",
                float(settled_funds), float(MIN_LIVE_SETTLED_FUNDS))

    # FINRA Rule 4210 $2,000 Minimum Equity Check for Margin Eligibility
    MIN_MARGIN_EQUITY = Decimal("2000.00")
    if ledger.nlv < MIN_MARGIN_EQUITY:
        logger.warning(
            "Account equity ($%.2f) is below $2,000 minimum for margin eligibility. "
            "FINRA Rule 4210 requires $2,000 minimum equity for margin borrowing.",
            float(ledger.nlv),
        )
    else:
        logger.info(
            "Account equity ($%.2f) satisfies FINRA Rule 4210 ($2,000 minimum). "
            "Intraday Margin Framework active (up to 4x intraday / 2x overnight leverage).",
            float(ledger.nlv),
        )

    # Adopt existing broker holdings as actively managed inventory
    rec_cfg_data = cfg.get("recovery", {}) or {}
    rec_enabled = bool(rec_cfg_data.get("enabled", False))
    rec_sym = str(rec_cfg_data.get("symbol", "SOXL")).upper()
    rec_floor = float(rec_cfg_data.get("floor_price", 99.75))

    for bp_item in sync.get_positions():
        is_rec = rec_enabled and (bp_item.symbol.upper() == rec_sym)
        if is_rec:
            stop_val = rec_floor
            target_val = None
            is_locked = True
            logger.info("Adopting %s with Recovery Protocol: stop=$%.2f (floor safeguard), recovery_locked=True",
                        bp_item.symbol, stop_val)
        else:
            stop_val = round(float(bp_item.avg_cost) * (1.0 - float(cfg.get("risk", {}).get("tier2_stop_pct", 0.04))), 2)
            target_val = None
            is_locked = False

        ledger.adopt_position(
            symbol=bp_item.symbol,
            quantity=bp_item.quantity,
            entry_price=bp_item.avg_cost,
            current_price=bp_item.current_price,
            stop_price=stop_val,
            target_price=target_val,
            regime="A",
            simulated=False,
            recovery_locked=is_locked,
        )
    sync.set_managed_symbols(ledger.positions.keys())

    # Sync RiskEngine dual buying power pipeline with initial broker state
    if ctx.risk_manager is not None:
        ctx.risk_manager.sync_account_state(
            equity=float(ledger.nlv),
            cash=float(ledger.settled_cash),
            positions=ledger.positions,
        )

    # Pre-check ceiling for StrategyEngine: incorporates real-time Intraday Margin Buying Power
    def ceiling():
        if ctx.risk_manager is not None and ctx.risk_manager.is_margin_eligible:
            return Decimal(str(round(ctx.risk_manager.intraday_buying_power, 2)))
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

    # Autonomous Recovery Supervisor wiring
    if rec_enabled:
        try:
            from execution.recovery_supervisor import RecoveryConfig, RecoverySupervisor
            r_cfg = RecoveryConfig.from_dict(rec_cfg_data)
            basis_val = 110.42
            pos_soxl = ledger.positions.get(rec_sym)
            if pos_soxl:
                basis_val = float(pos_soxl.entry_price)
            supervisor = RecoverySupervisor(config=r_cfg, engine=engine, basis=basis_val)
            supervisor.state = "PREOPEN_ARMED"
            supervisor.start(loop)
            engine.recovery = supervisor
            ctx.recovery_supervisor = supervisor
            logger.info("Autonomous Recovery Supervisor wired for %s (basis=$%.2f, state=PREOPEN_ARMED).",
                        rec_sym, basis_val)
        except Exception as exc:
            logger.error("Failed to initialize RecoverySupervisor: %s", exc)

    logger.warning("LIVE MODE: real orders will be routed to Schwab account ...%s.",
                   str(cfg.get("account", {}).get("required_suffix", "")))
    return engine


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _handle_unhandled_exception(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    msg = context.get("message", "Unhandled exception in event loop")
    exc = context.get("exception")
    if exc is not None:
        logger.error("Asyncio unhandled exception: %s (%s)", msg, exc, exc_info=exc)
    else:
        logger.error("Asyncio unhandled exception: %s", msg)


async def _control_signal_poller(ctx: EngineContext, engine: Any, stop: asyncio.Event) -> None:
    from core.ipc import read_control_signal, acknowledge_control_signal
    while not stop.is_set():
        try:
            sig = await asyncio.to_thread(read_control_signal)
            if sig and not sig.get("acknowledged_by_trader"):
                action = str(sig.get("action", "")).upper()
                if action == "HALT":
                    ctx.is_halted = True
                    logger.warning("IPC CONTROL SIGNAL: Master Kill Switch ENGAGED.")
                elif action == "RESUME":
                    ctx.is_halted = False
                    logger.info("IPC CONTROL SIGNAL: Trading Engine RESUMED.")
                elif action == "LIQUIDATE_ALL":
                    ctx.is_halted = True
                    logger.warning("IPC CONTROL SIGNAL: Emergency LIQUIDATE_ALL requested.")
                    if engine is not None:
                        asyncio.create_task(engine.flatten_all("IPC_EMERGENCY_LIQUIDATE"))
                elif action == "SET_AVE_MODE":
                    mode = str(sig.get("mode", "shadow")).lower()
                    if engine is not None and hasattr(engine, "set_ave_mode"):
                        engine.set_ave_mode(mode)
                    logger.info("IPC CONTROL SIGNAL: Set AVE mode to %s", mode)
                elif action == "RECOVERY_ARM":
                    if hasattr(engine, "recovery") and engine.recovery is not None:
                        try:
                            from core.runtime import load_config
                            from execution.recovery_supervisor import RecoveryConfig
                            fresh_cfg = load_active_config()
                            if "recovery" in fresh_cfg:
                                engine.recovery.cfg = RecoveryConfig.from_dict(fresh_cfg["recovery"])
                                logger.info("Recovery supervisor config updated from config.yaml: trail_pct=%.3f, u=[%.3f, %.3f]",
                                            engine.recovery.cfg.trail_pct, engine.recovery.cfg.u_min, engine.recovery.cfg.u_max)
                        except Exception as cfg_err:
                            logger.warning("Could not refresh recovery config on ARM: %s", cfg_err)
                        engine.recovery.state = "PREOPEN_ARMED"
                        engine.recovery.persist_state()
                        logger.info("IPC CONTROL SIGNAL: Recovery supervisor ARMED.")
                elif action == "RECOVERY_DISARM":
                    if hasattr(engine, "recovery") and engine.recovery is not None:
                        engine.recovery.state = "DISARMED"
                        engine.recovery.persist_state()
                        logger.info("IPC CONTROL SIGNAL: Recovery supervisor DISARMED.")
                await asyncio.to_thread(acknowledge_control_signal)
        except Exception as exc:
            logger.debug("control_signal_poller error: %s", exc)
        await asyncio.sleep(0.25)


async def run(args: argparse.Namespace) -> int:
    if getattr(args, "headless_trader", False):
        args.no_api = True
    cfg = load_active_config()
    live = resolve_live(args, cfg)
    cfg.setdefault("engine", {})["live_trading"] = live
    if live and not getattr(args, "confirm_live", False):
        logger.critical("--mode live requires explicit confirmation: re-run with --confirm-live "
                        "(or use --mode shadow).")
        return 2
    if live and args.ignore_session:
        logger.critical("--ignore-session is refused in LIVE mode.")
        return 2

    ctx = EngineContext(cfg, live=live)
    ctx.risk_manager = RiskEngine()
    ctx.bracket_manager = MultiTrancheBracketManager()
    await asyncio.to_thread(ctx.load_persisted_policy)

    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_handle_unhandled_exception)
    stop = asyncio.Event()
    tasks: List[asyncio.Task] = []
    handles = LiveHandles()
    settings = EngineSettings.from_cfg(cfg)

    try:
        if live:
            engine = await build_live(ctx, cfg, settings, loop, tasks, stop, handles)
        else:
            engine = await build_shadow(ctx, cfg, settings, loop, tasks, stop, handles, args.ignore_session)
    except StartupError as exc:
        if "AUTH_LOCKED" in str(exc):
            logger.critical("LIVE startup refused due to AUTH_LOCKED. Entering idle quarantine.")
            ctx.system_state = "AUTH_LOCKED"
            if handles.auth is not None:
                handles.auth.stop()
            engine = None
        else:
            logger.critical("LIVE startup refused: %s", exc)
            for t in tasks:
                t.cancel()
            if handles.auth is not None:
                handles.auth.stop()
            return 3

    if engine is not None:
        ctx.live_engine = engine
        ctx.flatten_cb = engine.flatten_all
    else:
        ctx.live_engine = None
        ctx.flatten_cb = lambda: None

    # Verification ping to ntfy alert topic on startup
    try:
        from core.notifier import send_alert
        send_alert(
            title=f"[SYSTEM] Engine v2.4 {'Live' if live else 'Shadow'} - Telemetry Active",
            message="SchwabEngine Alert Pipe Active on schwab-trader VM",
            priority="default",
            tags=["rocket", "satellite"],
        )
    except Exception:
        pass

    from core.ipc import TraderIPCServer
    ipc_server = TraderIPCServer()
    try:
        await ipc_server.start()
    except Exception as exc:
        logger.warning("TraderIPCServer could not start: %s", exc)
        ipc_server = None

    if engine is not None:
        tasks.append(asyncio.create_task(_control_signal_poller(ctx, engine, stop), name="control-signal-poller"))
        if ipc_server is not None and handles.streamer is not None:
            orig_on_tick = handles.streamer.on_tick
            import time

            def broadcast_on_tick(sym: str, f: dict) -> None:
                orig_on_tick(sym, f)
                ipc_server.broadcast({
                    "type": "TICK",
                    "symbol": sym,
                    "last": f.get("last_price", 0.0),
                    "bid": f.get("bid_price", 0.0),
                    "ask": f.get("ask_price", 0.0),
                    "vol": f.get("total_volume", 0),
                    "ts": int(time.time() * 1000),
                })
            handles.streamer.on_tick = broadcast_on_tick

    governor = make_governor() if (live or args.governor) else None
    if engine is not None:
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
        if ipc_server is not None:
            await ipc_server.stop()
        if handles.streamer is not None:
            await asyncio.to_thread(handles.streamer.stop)
        if ctx.microstructure is not None:
            await asyncio.to_thread(ctx.microstructure.persist)
        if handles.auth is not None:
            handles.auth.stop()

        # Automated EOD digest and Drive sync hook on transition to OFFLINE
        try:
            from core.shadow import run_eod_digest_and_sync
            sink_path = getattr(ctx, "shadow_sink", None).path if getattr(ctx, "shadow_sink", None) else None
            logger.info("Transition to OFFLINE: compiling EOD telemetry digest and replicating to Google Drive...")
            await run_eod_digest_and_sync(PROJECT_ROOT, log_path=sink_path)
        except Exception as exc:
            logger.warning("EOD telemetry digest and Drive sync failed on shutdown: %s", exc)

        if engine is not None:
            open_positions = sorted(engine.ledger.positions.keys())
            if open_positions:
                logger.warning("Shutting down with open engine positions %s (broker-side catastrophe stops, if "
                               "placed, remain working).", open_positions)
        logger.info("Engine stopped.")
    return 0


def run_authenticate() -> int:
    """Verifies and rehydrates Schwab tokens from the encrypted vault."""
    from dotenv import load_dotenv
    from core.auth import SchwabAuthManager, SecurityVault
    from core.paths import STATE_DIR

    load_dotenv()
    client_id = os.getenv("SCHWAB_CLIENT_ID")
    client_secret = os.getenv("SCHWAB_CLIENT_SECRET")
    passphrase = os.getenv("VAULT_PASSPHRASE")

    missing = [k for k, v in (("SCHWAB_CLIENT_ID", client_id), ("SCHWAB_CLIENT_SECRET", client_secret),
                              ("VAULT_PASSPHRASE", passphrase)) if not v]
    if missing:
        logger.error("Missing environment variables: %s", ", ".join(missing))
        return 1

    cfg = load_active_config()
    auth_cfg = cfg.get("auth", {}) or {}
    vault_file_name = auth_cfg.get("vault_file", "schwab_tokens_vault.json")
    candidates = [
        PROJECT_ROOT / vault_file_name,
        STATE_DIR / vault_file_name,
        Path(vault_file_name),
    ]
    vault_path = next((p for p in candidates if p.exists() and p.stat().st_size > 0), None)
    if vault_path is None:
        logger.error("Token vault not found at any candidate path: %s", candidates)
        return 1

    logger.info("Found token vault at %s (%d bytes). Rehydrating...", vault_path, vault_path.stat().st_size)
    vault = SecurityVault(
        passphrase=passphrase,
        iterations=int(auth_cfg.get("pbkdf2_iterations", 600000)),
        vault_path=vault_path,
    )
    auth = SchwabAuthManager(client_id, client_secret, vault, cfg)
    try:
        auth.load_tokens()
        logger.info("Successfully decrypted tokens from vault.")
    except Exception as exc:
        logger.error("Could not decrypt token vault: %s", exc)
        return 1

    remaining = auth.refresh_seconds_remaining()
    if remaining is not None and remaining <= 0:
        logger.error("Refresh token has expired (%.1fs remaining). Run scripts/auth/manual_auth.py to re-authorize.", remaining)
        return 2

    logger.info("Tokens rehydrated. Refresh token valid for %.1f hours.", (remaining or 0) / 3600)
    try:
        auth.force_refresh()
        logger.info("Successfully refreshed access token against Schwab API.")
    except Exception as exc:
        logger.warning("Token refresh against Schwab failed (offline or network): %s", exc)

    return 0


def run_self_test() -> int:
    from api.server import build_app
    cfg = load_active_config()
    ctx = EngineContext(cfg, live=False)
    ctx.ledgers["sandbox"] = build_ledger(cfg, live=False)
    ctx.risk_manager = RiskEngine()
    ctx.bracket_manager = MultiTrancheBracketManager()
    bracket = ctx.risk_manager.create_breakout_bracket("SOXL", 100, 100.0)
    assert bracket is not None
    assert bracket.tranche_1_filled is False
    app = build_app(ctx)
    if app is None:
        return 1
    logger.info("Test mode: FastAPI app and MultiTrancheBracket contract verified.")
    return 0


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SchwabEngine")
    p.add_argument("--mode", choices=["shadow", "live"], default="shadow",
                   help="shadow (default): live market data, simulated execution, no broker orders. "
                        "live: routes real orders (requires --confirm-live and >= $2,000 settled funds).")
    p.add_argument("--config", type=str, default="config/config.yaml",
                   help="Path to the engine YAML configuration (default: config/config.yaml).")
    p.add_argument("--confirm-live", action="store_true",
                   help="Explicit confirmation required for --mode live.")
    p.add_argument("--live", action="store_true", help="Deprecated alias for --mode live.")
    p.add_argument("--headless-trader", action="store_true", help="Run high-priority headless execution daemon without API server.")
    p.add_argument("--ignore-session", action="store_true", help="Dry-run only: allow entries outside market hours.")
    p.add_argument("--governor", action="store_true", help="Dry-run: also run the Vertex AI governor schedule.")
    p.add_argument("--no-api", action="store_true", help="Do not start the HTTP API.")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--test", action="store_true", help="Build the API app and exit.")
    p.add_argument("--authenticate", action="store_true", help="Rehydrate or verify Schwab tokens from the encrypted vault.")
    return _post_parse(p.parse_args(argv))


def _post_parse(args: argparse.Namespace) -> argparse.Namespace:
    """Normalizes the legacy --live alias and publishes the selected config path."""
    global _ACTIVE_CONFIG_PATH
    if args.live:
        args.mode = "live"
    _ACTIVE_CONFIG_PATH = args.config
    return args


if __name__ == "__main__":
    os.chdir(PROJECT_ROOT)
    _args = parse_args()
    if _args.authenticate:
        sys.exit(run_authenticate())
    if _args.test:
        sys.exit(run_self_test())
    try:
        sys.exit(asyncio.run(run(_args)))
    except KeyboardInterrupt:
        logger.info("Manual interrupt.")
