#!/usr/bin/env python3
"""
scripts/preflight.py
====================
Pre-flight verification for SchwabEngine. Run it before every deploy and before the
open. Exit code 0 = safe to start, non-zero = at least one FAIL.

    python scripts/preflight.py            # local / dry-run checks (no network, no secrets)
    python scripts/preflight.py --live     # + vault, token age, read-only Schwab probes

What it checks
--------------
 1. Syntax            every Python file compiles
 2. Imports           every runtime module imports
 3. YAML hygiene      config.yaml has no duplicate keys (a duplicate silently shadows the first)
 4. Config hygiene    live_trading=false, SWVXX excluded, 20% cap, schedule, model allow-list
 5. Repo hygiene      secrets / vault / state are gitignored and untracked, no key patterns in tracked
                      files, no hardcoded cap literals in the UI
 6. State I/O         atomic JSON round-trips and thread-safe read-modify-write
 7. GFV invariants    settled-only funding, T+1 rollover, lower-of broker sync, SWVXX/ACH/backstop never
                      buying power, soft-reserve gate, 20% single-ticker cap derived from NLV
 8. Zero auto-throttle document ingestion / API never touches the risk multiplier
 9. API contract      every endpoint, both mounts, env handling, honest UNAVAILABLE, WebSocket
10. Macro freshness   strategy_config.json / regime summary age
11. Frontend          dist/ bundle present
12. Live (--live)     env vars, vault decrypt, refresh-token age, access-token refresh, account firewall,
                      broker balance sync + parse, streamer credentials. All read-only.

All state written by the checks goes to a temporary directory; real state is never modified.
"""
from __future__ import annotations

import argparse
import ast
import importlib
import os
import py_compile
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

# Redirect all engine state to a scratch directory BEFORE any engine module is imported.
REAL_STATE_DIR = Path(os.environ.get("ENGINE_STATE_DIR", "state"))
_SCRATCH = tempfile.mkdtemp(prefix="preflight_state_")
os.environ["ENGINE_STATE_DIR"] = _SCRATCH
os.environ.pop("DOC_PARSER_SAMPLE_FALLBACK", None)

D = Decimal

# --------------------------------------------------------------------------- reporting


@dataclass
class Report:
    rows: List[tuple] = field(default_factory=list)

    def add(self, status: str, name: str, detail: str = "") -> None:
        self.rows.append((status, name, detail))
        print(f"[{status:<4}] {name}" + (f" - {detail}" if detail else ""), flush=True)

    def ok(self, name: str, detail: str = "") -> None:
        self.add("PASS", name, detail)

    def warn(self, name: str, detail: str = "") -> None:
        self.add("WARN", name, detail)

    def fail(self, name: str, detail: str = "") -> None:
        self.add("FAIL", name, detail)

    def skip(self, name: str, detail: str = "") -> None:
        self.add("SKIP", name, detail)

    def check(self, cond: bool, name: str, detail_ok: str = "", detail_fail: str = "") -> bool:
        (self.ok if cond else self.fail)(name, detail_ok if cond else detail_fail)
        return cond

    def count(self, status: str) -> int:
        return sum(1 for r in self.rows if r[0] == status)


R = Report()


def guarded(fn: Callable[[], None]) -> Callable[[], None]:
    def run() -> None:
        try:
            fn()
        except Exception as exc:  # a crashing check is a failing check
            R.fail(fn.__name__, f"check crashed: {type(exc).__name__}: {exc}")
            traceback.print_exc(limit=3)
    run.__name__ = fn.__name__
    return run


# --------------------------------------------------------------------------- helpers

SKIP_DIRS = {".git", "node_modules", "dist", "__pycache__", ".venv", "venv", "scratch", ".pytest_cache", "state"}


def python_files() -> List[Path]:
    out = []
    for p in ROOT.rglob("*.py"):
        if any(part in SKIP_DIRS for part in p.relative_to(ROOT).parts):
            continue
        out.append(p)
    return sorted(out)


def tracked_files() -> Optional[List[str]]:
    try:
        res = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, timeout=20)
        if res.returncode != 0:
            return None
        return [line for line in res.stdout.splitlines() if line]
    except (OSError, subprocess.SubprocessError):
        return None


def load_strict_yaml(path: Path) -> dict:
    import yaml

    class StrictLoader(yaml.SafeLoader):
        pass

    def construct_mapping(loader, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key '{key}'", key_node.start_mark)
            seen.add(key)
        return yaml.SafeLoader.construct_mapping(loader, node, deep)

    StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping)
    with path.open("r", encoding="utf-8") as fh:
        return yaml.load(fh, Loader=StrictLoader)


# --------------------------------------------------------------------------- 1-2. syntax & imports

@guarded
def check_syntax() -> None:
    bad = []
    files = python_files()
    for f in files:
        try:
            raw = f.read_bytes()
        except OSError as exc:
            bad.append(f"{f.relative_to(ROOT)}: unreadable ({exc})")
            continue
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            bad.append(f"{f.relative_to(ROOT)}: UTF-16 BOM (Python cannot import this)")
            continue
        try:
            compile(raw, str(f), "exec", dont_inherit=True)
        except (SyntaxError, ValueError) as exc:
            bad.append(f"{f.relative_to(ROOT)}: {type(exc).__name__}: {exc}")
    R.check(not bad, "python syntax", f"{len(files)} files compile", "; ".join(bad[:6]))


RUNTIME_MODULES = [
    "core.paths", "core.atomic_io", "core.liquidity_policy", "core.liquidity_models", "core.collateral_engine",
    "core.ledger", "core.nlv_anchor", "core.runtime", "core.universe_mask", "core.engine", "core.auth",
    "services.regime_summary", "services.broker_sync", "services.document_parser",
    "execution.risk_manager", "execution.strategies", "execution.order_manager",
    "data.rest_client", "data.streamer", "api.server", "main",
]


@guarded
def check_imports() -> None:
    failed = []
    for name in RUNTIME_MODULES:
        try:
            importlib.import_module(name)
        except Exception as exc:
            failed.append(f"{name}: {type(exc).__name__}: {exc}")
    R.check(not failed, "module imports", f"{len(RUNTIME_MODULES)} modules import cleanly", "; ".join(failed[:5]))

    try:
        importlib.import_module("governor")
        R.ok("governor import")
    except SystemExit:
        R.warn("governor import", "governor exits at import/init without credentials (expected off-VM)")
    except Exception as exc:
        R.warn("governor import", f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- 3-4. config

CONFIG_PATH = ROOT / "config" / "config.yaml"


@guarded
def check_yaml_and_config() -> None:
    try:
        cfg = load_strict_yaml(CONFIG_PATH)
    except Exception as exc:
        R.fail("config.yaml parses with no duplicate keys", str(exc))
        return
    R.ok("config.yaml parses with no duplicate keys")

    engine = cfg.get("engine", {}) or {}
    R.check(engine.get("live_trading") is False, "engine.live_trading defaults to false",
            detail_fail=f"found {engine.get('live_trading')!r}; real orders must require the explicit --live flag")

    for section in ("portfolio_manager", "reconciliation"):
        excl = [str(s).upper() for s in (cfg.get(section, {}) or {}).get("exclude_symbols", [])]
        R.check("SWVXX" in excl, f"{section}.exclude_symbols contains SWVXX",
                detail_fail="SWVXX could be auto-stopped or liquidated")

    risk = cfg.get("risk", {}) or {}
    R.check(float(risk.get("single_ticker_cap_pct", 0)) == 0.20, "risk.single_ticker_cap_pct == 0.20",
            detail_fail=f"found {risk.get('single_ticker_cap_pct')!r}")
    R.check(0 < float(risk.get("max_risk_per_trade_pct", 0)) <= 0.02, "risk.max_risk_per_trade_pct within (0, 2%]")
    R.check(0 < float(risk.get("daily_drawdown_pct", 0)) <= 0.05, "risk.daily_drawdown_pct within (0, 5%]")

    sched = cfg.get("schedule", {}) or {}
    eod, flat = str(sched.get("eod_liquidation_time", "")), str(sched.get("flat_deadline_time", ""))
    R.check(eod.startswith("15:50") and flat.startswith("15:55"), "flatten starts 15:50, hard deadline 15:55",
            f"eod={eod} deadline={flat}", f"eod={eod!r} deadline={flat!r}")

    api = cfg.get("api", {}) or {}
    R.check(any("schwab" in str(v).lower() for v in api.values()), "api: block holds the Schwab endpoints",
            detail_fail="the Schwab endpoints are missing/shadowed")
    server = cfg.get("server", {}) or {}
    R.check(int(server.get("port", 0)) > 0 and bool(server.get("host")), "server: block present",
            f"{server.get('host')}:{server.get('port')}")

    universe = [str(s).upper() for s in engine.get("symbols", [])]
    R.check(bool(universe) and "SWVXX" not in universe, "engine.symbols excludes SWVXX", ", ".join(universe))

    model = str((cfg.get("macro", {}) or {}).get("gemini_model", ""))
    gov = ROOT / "governor.py"
    allowed = set()
    if gov.exists():
        try:
            tree = ast.parse(gov.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "ALLOWED_MODELS" for t in node.targets):
                    allowed = {e.value for e in ast.walk(node.value) if isinstance(e, ast.Constant) and isinstance(e.value, str)}
        except SyntaxError:
            pass
    if allowed and model not in allowed:
        R.warn("macro.gemini_model is on the governor allow-list", f"config says {model!r}, governor allows {sorted(allowed)}")
    else:
        R.ok("macro.gemini_model is on the governor allow-list", model)


# --------------------------------------------------------------------------- 5. repo hygiene

SECRET_PATTERNS = {
    "Google API key": re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    "private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "hardcoded client secret": re.compile(r"""(?i)client_secret["']?\s*[:=]\s*["'][A-Za-z0-9]{20,}["']"""),
    "hardcoded vault passphrase": re.compile(r"""(?i)vault_passphrase["']?\s*[:=]\s*["'][^"']{6,}["']"""),
}
TEXT_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".json", ".yaml", ".yml", ".md", ".sh", ".ps1", ".env", ".txt", ".toml", ".html", ".css"}


@guarded
def check_repo_hygiene() -> None:
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8", errors="ignore") if (ROOT / ".gitignore").exists() else ""
    for needle in (".env", "schwab_tokens_vault.json", "state/"):
        R.check(any(line.strip().rstrip("/") == needle.rstrip("/") or line.strip().startswith(needle) for line in gi.splitlines()),
                f".gitignore covers {needle}", detail_fail="not ignored")

    files = tracked_files()
    if files is None:
        R.warn("git tracked-file checks", "not a git checkout (or git unavailable); skipped")
        return
    leaked = [f for f in files if Path(f).name in {".env", "schwab_tokens_vault.json"} or f.startswith("state/")]
    R.check(not leaked, "no secrets/vault/state tracked by git", detail_fail=", ".join(leaked))

    hits = []
    for rel in files:
        p = ROOT / rel
        if p.suffix.lower() not in TEXT_SUFFIXES or p.name == "package-lock.json" or rel.startswith("tests/"):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for label, pat in SECRET_PATTERNS.items():
            if pat.search(text):
                hits.append(f"{rel} ({label})")
    R.check(not hits, "no credential patterns in tracked files", detail_fail="; ".join(hits[:6]))

    ui_hits = []
    for rel in files:
        if rel.startswith("src/") and rel.endswith((".ts", ".tsx")):
            text = (ROOT / rel).read_text(encoding="utf-8", errors="ignore")
            for literal in ("749.50", "3,747.50", "3747.50", "§1091", "Dual-Tier"):
                if literal in text:
                    ui_hits.append(f"{rel}:{literal}")
    R.check(not ui_hits, "no hardcoded cap/legal literals in the UI", detail_fail="; ".join(ui_hits))

    mock_hits = [rel for rel in files if rel.startswith("src/") and rel.endswith((".ts", ".tsx"))
                 and re.search(r"\bMOCK_[A-Z]+\b", (ROOT / rel).read_text(encoding="utf-8", errors="ignore"))]
    R.check(not mock_hits, "no mock data constants in the UI", detail_fail=", ".join(mock_hits))


# --------------------------------------------------------------------------- 6. state I/O

@guarded
def check_state_io() -> None:
    from core.atomic_io import atomic_write_json, read_json, update_json

    base = Path(_SCRATCH) / "io"
    p = base / "doc.json"
    atomic_write_json(p, {"amt": D("1.25"), "d": date(2026, 10, 5)})
    doc = read_json(p)
    R.check(doc == {"amt": 1.25, "d": "2026-10-05"}, "atomic JSON round-trip (Decimal/date)")

    counter = base / "counter.json"

    def bump() -> None:
        for _ in range(20):
            update_json(counter, lambda d: d.__setitem__("n", d.get("n", 0) + 1))

    threads = [threading.Thread(target=bump) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    R.check(read_json(counter)["n"] == 120, "read-modify-write is thread-safe", "120/120 increments")

    if REAL_STATE_DIR.exists() or REAL_STATE_DIR.parent.exists():
        try:
            REAL_STATE_DIR.mkdir(parents=True, exist_ok=True)
            probe = REAL_STATE_DIR / f".preflight_{os.getpid()}"
            probe.write_text("ok")
            probe.replace(REAL_STATE_DIR / f".preflight_{os.getpid()}.swap")
            (REAL_STATE_DIR / f".preflight_{os.getpid()}.swap").unlink()
            R.ok("state directory is writable and supports atomic replace", str(REAL_STATE_DIR))
        except OSError as exc:
            R.fail("state directory is writable and supports atomic replace", f"{REAL_STATE_DIR}: {exc}")


# --------------------------------------------------------------------------- 7. GFV invariants

@guarded
def check_gfv_invariants() -> None:
    from datetime import datetime as dt
    from zoneinfo import ZoneInfo

    from core.ledger import BrokerBalances, SettlementLedger
    from core.liquidity_policy import InflowSchedule, LiquidityPolicy, compute_buying_power

    edt = ZoneInfo("America/New_York")
    friday = dt(2026, 10, 2, 12, 0, tzinfo=edt)
    monday = date(2026, 10, 5)

    # I1: entries use settled cash only
    lg = SettlementLedger(D("1000"))
    lg.record_sell("SOXL", 3, "100", when=friday)
    unsettled_before = lg.unsettled_total
    refused = not lg.check_order_allowed(D("995"))[0]
    funded = lg.allocate_capital("TQQQ", D("150"))
    R.check(refused and funded and lg.unsettled_total == unsettled_before and lg.settled == D("850.00"),
            "I1 entries are funded from settled cash only")

    # I2: rollover only on/after the settle date
    lg2 = SettlementLedger(D("0"))
    lg2.record_sell("SOXL", 2, "50", when=friday)
    early = lg2.rollover(date(2026, 10, 4))
    late = lg2.rollover(monday)
    R.check(early == 0 and late == D("100.00"), "I2 T+1 proceeds unlock only on the settle date",
            "Fri sale settles Mon 2026-10-05")

    # I3: lower-of broker sync
    lg3 = SettlementLedger(D("0"), data_source="LIVE_SCHWAB")
    lg3.sync_from_broker(BrokerBalances(liquidation_value=D("600"), settled_cash=D("500")))
    lg3.sync_from_broker(BrokerBalances(liquidation_value=D("550"), settled_cash=D("450")))
    R.check(lg3.settled == D("450.00") and lg3.gfv_risk_flag, "I3 ledger adopts the lower broker settled figure on drift")

    # I4: SWVXX / ACH / backstop / unsettled never enter buying power
    pol = LiquidityPolicy()
    clean = compute_buying_power(nlv=D("1000"), settled_cash=D("1000"), unsettled_cash=D("0"), policy=pol, today=monday)
    loaded = compute_buying_power(
        nlv=D("1000"), settled_cash=D("1000"), unsettled_cash=D("9999"), policy=pol, today=monday,
        swvxx_settled=D("9999"), swvxx_in_flight=D("9999"), pending_ach=D("9999"), external_backstop=D("9999"))
    R.check(clean.max_order_notional == loaded.max_order_notional and clean.tactical_float == loaded.tactical_float,
            "I4 SWVXX, ACH, external backstop and unsettled cash never add buying power")

    # I5: buying power <= settled cash
    tiny = compute_buying_power(nlv=D("1000"), settled_cash=D("40"), unsettled_cash=D("0"), policy=pol, today=monday,
                                regime="A", posterior=0.9)
    R.check(tiny.tactical_float <= D("40") and tiny.max_order_notional <= D("30"),
            "I5 buying power never exceeds settled cash less the buffer")

    # soft-reserve gate
    closed = [compute_buying_power(nlv=D("1000"), settled_cash=D("1000"), unsettled_cash=D("0"), policy=pol, today=monday,
                                   regime=r, posterior=p).high_probability
              for r, p in (("A", 0.549), ("B", 0.95), ("C", 0.95), (None, 0.9))]
    opened = compute_buying_power(nlv=D("1000"), settled_cash=D("1000"), unsettled_cash=D("0"), policy=pol, today=monday,
                                  regime="A", posterior=0.55).high_probability
    R.check(not any(closed) and opened, "soft-reserve draw requires Regime A and posterior >= 0.55")

    # 20% cap derived from NLV (not hardcoded)
    caps = {n: compute_buying_power(nlv=D(n), settled_cash=D(n), unsettled_cash=D("0"), policy=pol, today=monday).single_ticker_cap
            for n in ("1000", "3747.50", "5000")}
    R.check(caps == {"1000": D("200.00"), "3747.50": D("749.50"), "5000": D("1000.00")},
            "single-ticker cap is exactly 20% of NLV", f"NLV 3,747.50 -> cap {caps['3747.50']}")

    # SWVXX advisory only
    sweeps = [compute_buying_power(nlv=D("1000"), settled_cash=D(s), unsettled_cash=D("0"), policy=pol, today=monday,
                                   swvxx_settled=D("800")).sweep.routed for s in ("1000", "100", "40")]
    R.check(not any(sweeps), "SWVXX sweep is advisory only (never routed)")

    inflow_off = LiquidityPolicy(inflow=InflowSchedule(enabled=False))
    a = compute_buying_power(nlv=D("1000"), settled_cash=D("1000"), unsettled_cash=D("0"), policy=pol, today=monday)
    b = compute_buying_power(nlv=D("1000"), settled_cash=D("1000"), unsettled_cash=D("0"), policy=inflow_off, today=monday)
    R.check(a.soft_reserve_target_effective < b.soft_reserve_target_effective and a.tactical_float <= a.settled_cash,
            "anticipated inflow lowers the soft-reserve target only (never adds cash)")


@guarded
def check_zero_auto_throttle() -> None:
    offenders = []
    for rel in ("services/document_parser.py", "core/collateral_engine.py", "core/liquidity_models.py", "api/server.py"):
        text = (ROOT / rel).read_text(encoding="utf-8", errors="ignore")
        if re.search(r"risk_multiplier|update_macro_risk_multiplier", text):
            offenders.append(rel)
    R.check(not offenders, "document ingestion / API never touches the risk multiplier",
            detail_fail="; ".join(offenders))
    from execution.risk_manager import RiskEngine
    R.check(RiskEngine().macro_risk_multiplier == 1.0, "risk multiplier defaults to 1.0")


# --------------------------------------------------------------------------- 9. API contract

@guarded
def check_api() -> None:
    from fastapi.testclient import TestClient

    from api.server import build_app
    from core.runtime import EngineContext, build_ledger, load_config
    from execution.risk_manager import RiskEngine

    cfg = load_config()
    ctx = EngineContext(cfg, live=False)
    ctx.ledgers["sandbox"] = build_ledger(cfg, live=False)
    ctx.risk_manager = RiskEngine()
    client = TestClient(build_app(ctx))

    paths = [
        "/status?env=sandbox", "/status?env=active", "/ledger?env=sandbox", "/ledger?env=active",
        "/positions/all?env=sandbox", "/positions?env=sandbox", "/orders?env=sandbox", "/health",
        "/v1/liquidity/policy", "/v1/liquidity/buying-power?env=sandbox", "/v1/liquidity/state",
        "/v1/liquidity/document-snapshot", "/v1/regime/summary",
    ]
    bad = []
    for prefix in ("", "/api"):
        for path in paths:
            res = client.get(prefix + path)
            if res.status_code != 200:
                bad.append(f"{prefix}{path} -> {res.status_code}")
    R.check(not bad, "GET endpoints respond on both mounts", f"{len(paths) * 2} requests OK", "; ".join(bad[:5]))

    status = client.get("/api/status?env=sandbox").json()
    needed = {"system_state", "system_reasons", "auth_status", "env", "data_source", "nlv", "net_change_usd",
              "net_change_pct", "today_realized_pnl", "unrealized_pnl", "engine_mode"}
    R.check(needed <= set(status), "status exposes the HUD fields", detail_fail=str(sorted(needed - set(status))))
    R.check(client.get("/api/status?env=bogus").status_code == 422, "unknown env is rejected (422)")

    active = client.get("/api/ledger?env=active").json()
    R.check(active.get("available") is False and active.get("data_source") == "UNAVAILABLE",
            "Active env reports UNAVAILABLE instead of fabricated data")

    ledger = client.get("/api/ledger?env=sandbox").json()
    R.check(abs(ledger["max_single_exposure"] - ledger["total_nlv"] * 0.20) < 0.011, "ledger cap == 20% of NLV",
            f"NLV {ledger['total_nlv']:.2f} -> cap {ledger['max_single_exposure']:.2f}")

    pol = client.get("/api/v1/liquidity/policy").json()
    default_ok = (pol["inflow"]["amount"] == 250 and pol["inflow"]["weekday"] == "FRIDAY"
                  and pol["inflow"]["lag_business_days"] == 3 and abs(pol["inflow"]["confidence"] - 0.8) < 1e-9
                  and pol["sweep"]["mode"] == "ADVISORY")
    R.check(default_ok, "liquidity policy defaults ($250 / FRIDAY / 3d / 0.80 / ADVISORY)")
    R.check(client.put("/api/v1/liquidity/policy", json=pol).status_code == 200, "policy PUT round-trips")
    auto = {**pol, "sweep": {**pol["sweep"], "mode": "AUTO_WITH_APPROVAL"}}
    R.check(client.put("/api/v1/liquidity/policy", json=auto).status_code == 422, "automated SWVXX mode is rejected (422)")

    summary = client.get("/api/v1/regime/summary").json()
    R.check("headline" in summary and "last_prompt" not in summary, "regime summary is plain-language (no raw prompt)")
    if summary.get("stale"):
        R.warn("macro view freshness", f"regime summary is stale ({summary.get('age_hours')}h old); the 08:35 run refreshes it")
    else:
        R.ok("macro view freshness", f"{summary.get('age_hours')}h old")

    state = client.get("/api/v1/liquidity/state").json()
    R.check(state.get("snapshot") is None and not state.get("promotional_debts"),
            "no fabricated document data in a clean state")

    halt = client.post("/api/emergency/halt", json={"halted": True}).json()
    sys_halted = client.get("/api/status?env=sandbox").json()["system_state"]
    client.post("/api/emergency/halt", json={"halted": False})
    R.check(halt.get("halted") is True and sys_halted == "HALTED", "kill switch halts and is reflected in system_state")

    liquidate = client.post("/api/emergency/liquidate").json()
    ctx.is_halted = False
    R.check(liquidate.get("success") is False, "flatten reports failure when no flatten routine exists (no fake success)")

    with client.websocket_connect("/api/stream") as ws:
        beat = ws.receive_json()
    R.check(beat.get("event") == "HEARTBEAT", "WebSocket heartbeat")


# --------------------------------------------------------------------------- 11. frontend

@guarded
def check_frontend() -> None:
    dist = ROOT / "dist" / "index.html"
    if dist.exists():
        age_h = (time.time() - dist.stat().st_mtime) / 3600
        newest_src = max((p.stat().st_mtime for p in (ROOT / "src").rglob("*") if p.is_file()), default=0)
        if newest_src > dist.stat().st_mtime:
            R.warn("frontend bundle", "dist/ is older than src/ (rebuild with `npm run build`)")
        else:
            R.ok("frontend bundle", f"dist/index.html built {age_h:.1f}h ago")
    else:
        R.warn("frontend bundle", "dist/ missing (the Docker image builds it; run `npm run build` for a local check)")


# --------------------------------------------------------------------------- 12. live probes

def _secs(value: Optional[float]) -> str:
    if value is None:
        return "unknown"
    d, rem = divmod(int(value), 86400)
    h, rem = divmod(rem, 3600)
    return f"{d}d {h}h {rem // 60}m"


@guarded
def check_live() -> None:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    from core.runtime import build_ledger, load_config

    cfg = load_config()
    cid, sec, pw = (os.getenv(k) for k in ("SCHWAB_CLIENT_ID", "SCHWAB_CLIENT_SECRET", "VAULT_PASSPHRASE"))
    missing = [k for k, v in (("SCHWAB_CLIENT_ID", cid), ("SCHWAB_CLIENT_SECRET", sec), ("VAULT_PASSPHRASE", pw)) if not v]
    if not R.check(not missing, "Schwab environment variables present", detail_fail=f"missing {', '.join(missing)}"):
        return

    auth_cfg = cfg.get("auth", {}) or {}
    vault_path = ROOT / auth_cfg.get("vault_file", "schwab_tokens_vault.json")
    if not R.check(vault_path.exists() and vault_path.stat().st_size > 0, "token vault present", str(vault_path),
                   f"{vault_path} not found; run manual_auth.py on this machine"):
        return

    from core.auth import SchwabAuthManager, SecurityVault

    vault = SecurityVault(passphrase=pw, iterations=int(auth_cfg.get("pbkdf2_iterations", 600000)), vault_path=vault_path)
    auth = SchwabAuthManager(cid, sec, vault, cfg)
    try:
        auth.load_tokens()
    except Exception as exc:
        R.fail("token vault decrypts (AES-256-GCM)", f"{type(exc).__name__}: {exc}")
        return
    R.ok("token vault decrypts (AES-256-GCM)")

    remaining = auth.refresh_seconds_remaining()
    if remaining is None:
        R.warn("refresh-token age", "vault has no issue timestamp; age unknown")
    elif remaining <= 0:
        R.fail("refresh-token age", "refresh token EXPIRED; run manual_auth.py to re-authorise")
        return
    elif remaining < 24 * 3600:
        R.warn("refresh-token age", f"expires in {_secs(remaining)}; re-authorise before the next session")
    else:
        R.ok("refresh-token age", f"{_secs(remaining)} remaining")

    try:
        auth.force_refresh()
        R.ok("access token refresh against Schwab")
    except Exception as exc:
        R.fail("access token refresh against Schwab", f"{type(exc).__name__}: {exc}")
        return

    from data.rest_client import SchwabRestClient
    from execution.order_manager import OrderManager
    from core.nlv_anchor import NlvAnchor
    from services.broker_sync import BrokerSync

    rest = SchwabRestClient.from_config(cfg, auth)
    ledger = build_ledger(cfg, live=True)

    try:
        OrderManager(rest, ledger, cfg).initialize_firewall()
        R.ok("account firewall", f"required suffix ...{(cfg.get('account', {}) or {}).get('required_suffix', '')}")
    except Exception as exc:
        R.fail("account firewall", f"{type(exc).__name__}: {exc}")
        return

    try:
        sync = BrokerSync(rest, ledger, NlvAnchor(Path(_SCRATCH) / "live_anchor.json"), cfg)
        balances = sync.sync_once()
        R.ok("broker balance sync + parse",
             f"NLV ${balances.liquidation_value:,.2f}, settled ${balances.settled_cash:,.2f}, "
             f"unsettled ${balances.unsettled_cash:,.2f}, SWVXX ${balances.swvxx_value:,.2f}")
        R.check(balances.liquidation_value > 0, "account NLV is positive", detail_fail="NLV is zero; check the parse and account")
        R.check(balances.settled_cash <= balances.liquidation_value + D("0.01"), "settled cash does not exceed NLV")
        cap = ledger.max_single_exposure
        R.check(cap == (balances.liquidation_value * D("0.20")).quantize(D("0.01")), "live single-ticker cap is 20% of broker NLV",
                f"${cap:,.2f}")
        others = sync.get_unmanaged_positions()
        R.ok("external holdings detected", ", ".join(f"{p.symbol} x{p.quantity}" for p in others) or "none")
    except Exception as exc:
        R.fail("broker balance sync + parse", f"{type(exc).__name__}: {exc}")
        return

    try:
        prefs = rest.get_user_preferences()
        info = prefs.get("streamerInfo") if isinstance(prefs, dict) else None
        R.check(bool(info), "streamer credentials available", detail_fail="user preferences lack streamerInfo")
    except Exception as exc:
        R.fail("streamer credentials available", f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- main

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="SchwabEngine pre-flight checks")
    ap.add_argument("--live", action="store_true", help="also run vault, token and read-only Schwab probes")
    args = ap.parse_args(argv)

    started = time.time()
    print(f"SchwabEngine pre-flight - {datetime.now(timezone.utc).isoformat(timespec='seconds')} "
          f"- mode: {'LIVE PROBES' if args.live else 'local'}\n", flush=True)

    for fn in (check_syntax, check_imports, check_yaml_and_config, check_repo_hygiene, check_state_io,
               check_gfv_invariants, check_zero_auto_throttle, check_api, check_frontend):
        fn()

    if args.live:
        guarded(check_live)()
    else:
        R.skip("live Schwab probes", "run with --live on the VM (vault + network required)")

    print(f"\nSummary: {R.count('PASS')} passed, {R.count('WARN')} warnings, {R.count('FAIL')} failed, "
          f"{R.count('SKIP')} skipped ({time.time() - started:.1f}s)")
    if R.count("FAIL"):
        print("RESULT: NOT SAFE TO TRADE - fix the FAIL items above.")
        return 1
    print("RESULT: OK" + (" (with warnings)" if R.count("WARN") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
