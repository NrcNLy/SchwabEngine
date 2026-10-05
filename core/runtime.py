"""
core/runtime.py
===============
Shared runtime container and small helpers used by both the engine (main.py)
and the API layer (api/server.py). Kept dependency-light so importing it never
touches the network or the broker.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from decimal import Decimal
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

import pytz
import yaml

from core.atomic_io import read_json
from core.paths import ANCHOR_ACTIVE_FILE, ANCHOR_SANDBOX_FILE, MACRO_STATE_FILE
from core.ledger import SettlementLedger
from core.liquidity_policy import (
    ZERO,
    BuyingPowerBreakdown,
    LiquidityPolicy,
    compute_buying_power,
    is_business_day,
    load_policy,
)
from core.nlv_anchor import NlvAnchor

logger = logging.getLogger("runtime")
_EDT = pytz.timezone("America/New_York")

CONFIG_PATH = Path("config/config.yaml")


def load_config(path: Path | str = CONFIG_PATH) -> Dict[str, Any]:
    """Loads config.yaml (UTF-8). Returns {} if the file is missing."""
    p = Path(path)
    if not p.exists():
        logger.warning("Config %s not found; using defaults.", p)
        return {}
    with open(p, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def parse_hhmm(value: str, default: str) -> dtime:
    text = str(value or default)
    parts = [int(x) for x in text.split(":")]
    while len(parts) < 3:
        parts.append(0)
    return dtime(parts[0], parts[1], parts[2])


def now_et() -> datetime:
    return datetime.now(_EDT)


from core.session import TradingPhase, get_session_phase


def lifecycle_phase(now: Optional[datetime] = None, cfg: Optional[Dict[str, Any]] = None) -> str:
    """
    Phase from the ET wall clock: PRE_MARKET 08:35-09:30, CORE_SESSION 09:30-eod,
    SWEEP eod-16:15, REFLECTION 16:15-17:00, otherwise OFFLINE (incl. weekends/holidays).
    """
    now = (now or now_et()).astimezone(_EDT)
    if not is_business_day(now.date()):
        return "OFFLINE"
    sched = (cfg or {}).get("schedule", {})
    eod = parse_hhmm(sched.get("eod_liquidation_time"), "15:50:00")
    t = now.time()
    if dtime(8, 35) <= t < dtime(9, 30):
        return "PRE_MARKET"
    if dtime(9, 30) <= t < eod:
        return "CORE_SESSION"
    if eod <= t < dtime(16, 15):
        return "SWEEP"
    if dtime(16, 15) <= t < dtime(17, 0):
        return "REFLECTION"
    return "OFFLINE"


def build_ledger(cfg: Dict[str, Any], *, live: bool) -> SettlementLedger:
    """
    Builds a ledger from config ratios. The sandbox ledger starts with the
    configured baseline; the live ledger starts empty and is adopted from the broker.
    """
    risk = cfg.get("risk", {})
    return SettlementLedger(
        baseline_settled=0 if live else risk.get("sandbox_baseline", 1000.00),
        single_ticker_cap_pct=risk.get("single_ticker_cap_pct", 0.20),
        risk_per_trade_pct=risk.get("max_risk_per_trade_pct", 0.01),
        daily_drawdown_pct=risk.get("daily_drawdown_pct", 0.03),
        cash_buffer=risk.get("cash_buffer", 10.00),
        data_source="LIVE_SCHWAB" if live else "SANDBOX_SIM",
    )


class EngineContext:
    """
    Holds references to all live engine objects for dependency injection into
    the FastAPI app. Populated by main.py.

    ``ledgers['active']`` is the broker-synced ledger (LIVE mode only);
    ``ledgers['sandbox']`` is the engine's dry-run/simulated ledger (DRY_RUN only).
    """

    def __init__(self, cfg: Optional[Dict[str, Any]] = None, live: bool = False):
        self.cfg: Dict[str, Any] = cfg or {}
        self.mode: str = "LIVE" if live else "DRY_RUN"

        self.auth_manager = None
        self.risk_manager = None          # execution.risk_manager.RiskEngine
        self.order_manager = None
        self.rest_client = None
        self.strategy_engine = None
        self.broker_sync = None           # services.broker_sync.BrokerSync (LIVE)
        self.portfolio_manager = None
        self.compression_agent = None
        self.news_aggregator = None
        self.reconciliation_monitor = None
        self.redirect_uri = "https://127.0.0.1"

        self.ledgers: Dict[str, Optional[SettlementLedger]] = {"active": None, "sandbox": None}
        self.anchors: Dict[str, NlvAnchor] = {
            "active": NlvAnchor(ANCHOR_ACTIVE_FILE),
            "sandbox": NlvAnchor(ANCHOR_SANDBOX_FILE),
        }
        self.policy: LiquidityPolicy = LiquidityPolicy()
        self._policy_lock = threading.RLock()

        self.is_halted: bool = False
        self.flatten_cb: Optional[Callable[[str], Awaitable[int]]] = None
        self.last_tick_at: Optional[float] = None
        self.tick_source: str = "NONE"    # SCHWAB | MOCK | NONE
        self.last_error: Optional[str] = None
        self.api_latency_ms: Optional[float] = None
        self.sim_regimes: Dict[str, str] = {}   # dry-run pipeline regime per symbol

        self._start_time = datetime.utcnow()

    # -- convenience -----------------------------------------------------

    @property
    def live(self) -> bool:
        return self.mode == "LIVE"

    @property
    def ledger(self) -> Optional[SettlementLedger]:
        """The ledger that gates trading in the current mode."""
        return self.ledgers["active"] if self.live else self.ledgers["sandbox"]

    def ledger_for(self, env: str) -> Optional[SettlementLedger]:
        return self.ledgers.get("active" if env == "active" else "sandbox")

    def uptime_seconds(self) -> int:
        return int((datetime.utcnow() - self._start_time).total_seconds())

    def get_policy(self) -> LiquidityPolicy:
        with self._policy_lock:
            return self.policy

    def set_policy(self, policy: LiquidityPolicy) -> None:
        with self._policy_lock:
            self.policy = policy

    def mark_tick(self, source: str) -> None:
        self.last_tick_at = time.time()
        self.tick_source = source

    def seconds_since_tick(self) -> Optional[float]:
        return None if self.last_tick_at is None else time.time() - self.last_tick_at

    def load_persisted_policy(self) -> None:
        self.set_policy(load_policy())

    def engine_regimes(self) -> Dict[str, str]:
        """Current regime code (A/B/C) per symbol; unclassified symbols are omitted."""
        regimes: Dict[str, str] = {}
        engine = self.strategy_engine
        if engine is not None:
            for sym in (self.cfg.get("engine", {}) or {}).get("symbols", []):
                code = engine.get_regime(sym).value
                if code in ("A", "B", "C"):
                    regimes[str(sym).upper()] = code
        regimes.update(self.sim_regimes)
        return regimes


def read_external_backstop() -> Decimal:
    """External liquid backstop recorded by document ingestion (informational; never buying power)."""
    doc = read_json(MACRO_STATE_FILE, default={}) or {}
    raw = doc.get("external_liquid_backstop", 0)
    try:
        return max(Decimal(str(raw)), ZERO)
    except Exception:
        return ZERO


def ledger_buying_power(
    ctx: EngineContext,
    env: str,
    regime: Optional[str] = None,
    posterior: Optional[float] = None,
    backstop: Optional[Decimal] = None,
) -> Optional[BuyingPowerBreakdown]:
    """
    Buying-power breakdown for one environment ('active' | 'sandbox'), or None when
    that environment has no ledger (e.g. 'active' while running DRY_RUN).
    Posterior defaults to the risk engine's Bayesian win-rate.
    """
    ledger = ctx.ledger_for(env)
    if ledger is None:
        return None
    if posterior is None and ctx.risk_manager is not None:
        posterior = ctx.risk_manager.posterior_win_rate()
    return compute_buying_power(
        nlv=ledger.nlv,
        settled_cash=ledger.settled,
        unsettled_cash=ledger.unsettled_total,
        policy=ctx.get_policy(),
        today=now_et().date(),
        swvxx_settled=ledger.swvxx_balance,
        swvxx_in_flight=ledger.swvxx_in_flight,
        pending_ach=ledger.pending_ach,
        external_backstop=read_external_backstop() if backstop is None else backstop,
        regime=regime,
        posterior=posterior,
    )
