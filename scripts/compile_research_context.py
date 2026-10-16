#!/usr/bin/env python3
"""
scripts/compile_research_context.py
===================================
Automated research context compilation utility for the Schwab Trading Engine.
Programmatically extracts scoped codebase logic, active strategy configurations,
systemd journalctl telemetry, and SQLite state databases into a unified,
grounded Markdown reference document (`research_context.md`).

Usage:
    .venv/bin/python scripts/compile_research_context.py --scope general --since "today" --output research_context.md
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import platform
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("compile_research_context")

ROOT_DIR = Path(__file__).resolve().parent.parent

# Subsystem file scopes and comprehensive categories
COMPREHENSIVE_CATEGORIES: Dict[str, List[Tuple[str, str]]] = {
    "Execution Subsystem": [
        ("execution/microstructure_sentinel.py", "Opening cross VWAP buffer & soft/hard triggers"),
        ("execution/adaptive_envelope.py", "Yang-Zhang volatility & asymptotic time-decay squeeze"),
        ("execution/almgren_chriss.py", "Optimal execution slicing & NBBO limit pegging"),
        ("execution/bracket_manager.py", "Multi-tranche profit ladder & breakeven ratchet"),
        ("execution/sizing_governor.py", "Bayesian Quarter-Kelly sizing & cash settlement clamp"),
        ("execution/order_manager.py", "Routing firewall & order state transitions"),
        ("execution/risk_manager.py", "Account risk controls & exposure governors"),
    ],
    "Core & Data Subsystem": [
        ("core/synthetic_divergence.py", "Multi-factor SES regime governor & relative strength"),
        ("core/rate_limiter.py", "Schwab REST token bucket, burst controller & daily quota"),
        ("core/event_ledger.py", "Zero-allocation SQLite WAL event ledger"),
        ("core/event_models.py", "Zero-allocation SQLite WAL event models & schemas"),
        ("core/ledger.py", "Three-bucket cash accounting & T+1 GFV state machine"),
        ("data/stream_resampler.py", "WebSocket Level 1 streaming tick resampler"),
    ],
    "Web & Operational Scripts": [
        ("web/websocket_manager.py", "FastAPI 1Hz real-time telemetry gateway"),
        ("scripts/preflight.py", "12 pre-market invariant latch gates"),
        ("scripts/verify_friday_settlement.py", "Automated T+1 settlement auditor"),
        ("scripts/export_research_datasets.py", "Market data packaging pipeline"),
    ],
    "Configuration & Production Metadata": [
        ("strategy_config.json", "Production strategy and regime configuration"),
        ("metadata.json", "Engine deployment metadata & hardened release state"),
    ],
}

SCOPE_DEFINITIONS: Dict[str, List[str]] = {
    "comprehensive": [item[0] for sublist in COMPREHENSIVE_CATEGORIES.values() for item in sublist],
    "general": [
        "core/ledger.py",
        "core/synthetic_divergence.py",
        "core/auth.py",
        "execution/order_manager.py",
        "execution/risk_manager.py",
        "strategy_config.json",
        "metadata.json",
    ],
    "execution": [
        "execution/order_manager.py",
        "execution/router.py",
        "execution/order_client.py",
        "core/ledger.py",
        "core/auth.py",
        "core/rate_limiter.py",
        "strategy_config.json",
    ],
    "risk": [
        "execution/risk_manager.py",
        "core/synthetic_divergence.py",
        "core/liquidity_models.py",
        "governor.py",
        "strategy_config.json",
    ],
    "recovery": [
        "scripts/autonomous_recovery_soxl.py",
        "execution/recovery_supervisor.py",
        "strategy_config.json",
        "state/recovery_plan.json",
        "state/control_signal.json",
    ],
    "telemetry": [
        "core/telemetry.py",
        "core/trade_store.py",
        "core/baselines_store.py",
    ],
    "all": [
        "core/ledger.py",
        "core/synthetic_divergence.py",
        "core/auth.py",
        "core/rate_limiter.py",
        "core/telemetry.py",
        "core/trade_store.py",
        "core/baselines_store.py",
        "core/liquidity_models.py",
        "execution/order_manager.py",
        "execution/risk_manager.py",
        "execution/router.py",
        "execution/order_client.py",
        "execution/recovery_supervisor.py",
        "governor.py",
        "scripts/autonomous_recovery_soxl.py",
        "strategy_config.json",
        "metadata.json",
        "state/recovery_plan.json",
        "state/control_signal.json",
    ],
}


def run_command(cmd: List[str], timeout: int = 20) -> Tuple[int, str]:
    """Executes a subprocess command safely returning exit code and stdout/stderr."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT_DIR),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = proc.stdout.strip()
        if proc.returncode != 0 and proc.stderr.strip():
            if output:
                output += "\n" + proc.stderr.strip()
            else:
                output = proc.stderr.strip()
        return proc.returncode, output
    except Exception as exc:
        return 1, f"[ERROR executing {' '.join(cmd)}: {exc}]"


def get_git_metadata() -> Dict[str, str]:
    """Extracts git branch, commit hash, and working tree cleanliness."""
    _, branch = run_command(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    _, commit = run_command(["git", "rev-parse", "HEAD"])
    _, status = run_command(["git", "status", "--porcelain"])
    return {
        "branch": branch or "unknown",
        "commit": commit[:10] if commit else "unknown",
        "dirty": "dirty" if status else "clean",
    }


def query_journalctl(unit: str, since: str, lines: int) -> str:
    """Queries systemd journalctl for the specified service unit."""
    if not shutil.which("journalctl"):
        return f"[WARN: journalctl binary not found on {platform.system()}]"

    cmd = [
        "journalctl",
        "-u",
        unit,
        "--since",
        since,
        "-n",
        str(lines),
        "--no-pager",
    ]
    code, out = run_command(cmd, timeout=30)
    if code != 0 or not out:
        return f"[No journalctl logs recorded for {unit} since '{since}']"
    return out


def query_systemctl_status(unit: str) -> str:
    """Queries systemctl status for a service unit."""
    if not shutil.which("systemctl"):
        return "systemctl unavailable"
    code, out = run_command(["systemctl", "is-active", unit])
    return out if code == 0 else f"inactive ({out})"


def inspect_sqlite_db(db_path: Path) -> Dict[str, Any]:
    """Inspects SQLite database for WAL pragma, size, tables and row counts."""
    is_rel = False
    try:
        is_rel = db_path.is_relative_to(ROOT_DIR)
    except Exception:
        pass
    info: Dict[str, Any] = {
        "path": str(db_path.relative_to(ROOT_DIR)) if is_rel else str(db_path),
        "exists": db_path.exists(),
        "size_kb": round(db_path.stat().st_size / 1024, 2) if db_path.exists() else 0,
        "journal_mode": "unknown",
        "tables": {},
    }
    if not db_path.exists():
        return info

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode;")
        row = cursor.fetchone()
        if row:
            info["journal_mode"] = str(row[0]).lower()

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
        tables = [t[0] for t in cursor.fetchall()]
        for table in tables:
            try:
                cursor.execute(f"SELECT COUNT(*) FROM \"{table}\";")
                cnt_row = cursor.fetchone()
                info["tables"][table] = cnt_row[0] if cnt_row else 0
            except Exception:
                info["tables"][table] = "err"
        conn.close()
    except Exception as exc:
        info["error"] = str(exc)

    return info


def sanitize_env_status() -> Dict[str, str]:
    """Inspects .env variables presence without revealing sensitive secrets."""
    env_file = ROOT_DIR / ".env"
    status: Dict[str, str] = {}
    critical_keys = [
        "SCHWAB_API_KEY",
        "SCHWAB_APP_SECRET",
        "SCHWAB_REDIRECT_URI",
        "SCHWAB_ACCOUNT_ID",
        "GEMINI_API_KEY",
        "STATE_DIR",
        "ENGINE_STATE_DIR",
    ]
    if not env_file.exists():
        return {"status": "NOT FOUND (.env missing)"}

    present_keys = set()
    try:
        for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k = line.split("=", 1)[0].strip()
                present_keys.add(k)
    except Exception as exc:
        return {"status": f"READ_ERROR ({exc})"}

    for key in critical_keys:
        status[key] = "CONFIGURED (SET)" if key in present_keys else "MISSING / UNSET"
    return status


def extract_code_file(rel_path: str, subtitle: Optional[str] = None) -> str:
    """Reads and formats a source code or config file into a fenced Markdown block."""
    full_path = ROOT_DIR / rel_path
    if not full_path.exists():
        header = f"#### `{rel_path}`\n" if not subtitle else f"#### `{rel_path}` - {subtitle}\n"
        return f"{header}\n> [!WARNING] File not found at `{full_path}`\n\n"

    try:
        content = full_path.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines()
        line_count = len(lines)
        size_bytes = full_path.stat().st_size

        ext = full_path.suffix.lstrip(".").lower()
        fence = "json" if ext == "json" else "python" if ext in ("py", "pyw") else "text"

        header = f"#### `{rel_path}` ({line_count} lines, {size_bytes} bytes)\n"
        if subtitle:
            header = f"#### `{rel_path}` - {subtitle} ({line_count} lines, {size_bytes} bytes)\n"
        return f"{header}\n```{fence}\n{content}\n```\n\n"
    except Exception as exc:
        header = f"#### `{rel_path}`\n" if not subtitle else f"#### `{rel_path}` - {subtitle}\n"
        return f"{header}\n> [!ERROR] Could not read file: {exc}\n\n"


def compile_research_context(
    scope: str = "general",
    since: str = "today",
    lines: int = 50,
    units: Optional[List[str]] = None,
) -> str:
    """Compiles the full research context document."""
    if units is None:
        units = [
            "schwab-trader.service",
            "schwab-web.service",
            "schwab-recovery-watchdog.service",
        ]

    now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    now_local = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S %Z")
    git_meta = get_git_metadata()
    env_status = sanitize_env_status()

    # Load strategy configuration
    strat_cfg_path = ROOT_DIR / "strategy_config.json"
    strat_cfg_str = "{}"
    if strat_cfg_path.exists():
        try:
            strat_cfg_str = json.dumps(json.loads(strat_cfg_path.read_text(encoding="utf-8")), indent=2)
        except Exception:
            strat_cfg_str = strat_cfg_path.read_text(encoding="utf-8")

    # Inspect state databases
    db_candidates = [
        ROOT_DIR / "state" / "trades.db",
        ROOT_DIR / "state" / "telemetry.db",
        ROOT_DIR / "state" / "market_baselines.db",
        ROOT_DIR / "data" / "schwab_telemetry.db",
        Path("/home/nicho/schwab_state/trades.db"),
        Path("/home/nicho/schwab_state/telemetry.db"),
        Path("/home/nicho/schwab_state/market_baselines.db"),
    ]
    seen_db = set()
    unique_dbs = []
    for p in db_candidates:
        try:
            resolved = str(p.resolve())
            if p.exists() and resolved not in seen_db:
                seen_db.add(resolved)
                unique_dbs.append(p)
        except Exception:
            if p.exists():
                unique_dbs.append(p)

    db_reports = [inspect_sqlite_db(p) for p in unique_dbs]

    # Collect source modules for the specified scope
    if scope == "comprehensive":
        sections_output = []
        sec_idx = 1
        for cat_name, file_tuples in COMPREHENSIVE_CATEGORIES.items():
            sections_output.append(f"### 3.{sec_idx} {cat_name}\n\n")
            for rel_path, desc in file_tuples:
                sections_output.append(extract_code_file(rel_path, subtitle=desc))
            sec_idx += 1
        code_blocks_content = "".join(sections_output)
    else:
        scoped_files = SCOPE_DEFINITIONS.get(scope, SCOPE_DEFINITIONS["general"])
        code_blocks_content = "".join([extract_code_file(f) for f in scoped_files])

    # Collect journalctl logs
    service_logs: Dict[str, Dict[str, str]] = {}
    for unit in units:
        status = query_systemctl_status(unit)
        logs = query_journalctl(unit, since=since, lines=lines)
        service_logs[unit] = {"status": status, "logs": logs}

    # Build Markdown document
    doc = rf"""# Production Engine Research Context Document
**System Identifier:** Schwab Algorithmic Day-Trading Engine (`schwab_engine`)  
**Generated Timestamp:** {now_utc} (Local: `{now_local}`)  
**Execution Node:** `{platform.node()}` ({platform.system()} {platform.release()})  
**Active Scope:** `{scope}`  
**Log Query Horizon:** `--since "{since}"` (Max `{lines}` lines/unit)  
**VCS Metadata:** Branch `{git_meta['branch']}` @ `{git_meta['commit']}` ({git_meta['dirty']})  

---

## 1. Executive Summary & Runtime Environment

### 1.1 Operating Topology
* **Core Execution Daemon:** `schwab-trader.service` pinned to CPU Core 0 (`CPUAffinity=0`, `Nice=-10`).
* **Web Dashboard & API Gateway:** `schwab-web.service` pinned to CPU Core 1 (`CPUAffinity=1`).
* **Autonomous Recovery Watchdog:** `schwab-recovery-watchdog.service` supervising SOXL dynamic recovery.
* **Settlement Discipline:** SEC Regulation T § 220.8 T+1 Long-Only Cash Account. Strict GFV pre-trade gating and forced overnight holds.

### 1.2 Environment & Secrets Configuration
| Environment Variable | Status | Description |
| :--- | :--- | :--- |
| `SCHWAB_API_KEY` | `{env_status.get('SCHWAB_API_KEY', 'UNKNOWN')}` | Schwab Trader API App Key |
| `SCHWAB_APP_SECRET` | `{env_status.get('SCHWAB_APP_SECRET', 'UNKNOWN')}` | Schwab Trader API Secret |
| `SCHWAB_REDIRECT_URI` | `{env_status.get('SCHWAB_REDIRECT_URI', 'UNKNOWN')}` | OAuth Callback URI |
| `SCHWAB_ACCOUNT_ID` | `{env_status.get('SCHWAB_ACCOUNT_ID', 'UNKNOWN')}` | Encrypted Schwab Target Account |
| `GEMINI_API_KEY` | `{env_status.get('GEMINI_API_KEY', 'UNKNOWN')}` | Vertex AI / Gemini Macro Governor |

---

## 2. Active Strategy & Regime Configuration (`strategy_config.json`)

```json
{strat_cfg_str}
```

---

## 3. Scoped Codebase Logic & Mathematical Formulations (Scope: `{scope}`)

"""
    doc += code_blocks_content

    doc += rf"""
---

## 4. Live Daemon Health & Journalctl Telemetry

"""
    for unit, data in service_logs.items():
        doc += f"### Unit: `{unit}`\n"
        doc += f"* **Daemon Status:** `{data['status']}`\n\n"
        doc += f"```text\n{data['logs']}\n```\n\n"

    doc += rf"""
---

## 5. State Store & Database Telemetry

| Database Path | Exists | Size (KB) | Journal Mode | Tables (Row Counts) |
| :--- | :---: | :---: | :---: | :--- |
"""
    for rep in db_reports:
        tbl_summary = ", ".join([f"{k}: {v}" for k, v in rep.get("tables", {}).items()]) or "None"
        doc += f"| `{rep['path']}` | {rep['exists']} | {rep['size_kb']} | `{rep['journal_mode']}` | {tbl_summary} |\n"

    doc += rf"""
---

## 6. Synthesis & Operational Guidance
1. **Cash Accounting Invariant (I1):** All candidate orders must reserve cleared funds strictly from `settled_cash`.
2. **Dynamic Stop Scaling:** Yang-Zhang trailing stops must incorporate the intraday root time scaling scalar $\sqrt{{\frac{{1/390}}{{252}}}}$.
3. **GFV Veto Enforcement:** Same-day exits of unsettled lots must trip `GFV_SELL_BLOCKED` and defer to overnight holding.
4. **Rate Limit Throttling:** Observe 3,500 daily call limit ceiling; back off polling during high message density.
"""

    return doc


def main() -> None:
    parser = argparse.ArgumentParser(description="Compile Schwab Engine Research Context Snapshot.")
    parser.add_argument(
        "--scope",
        default="general",
        choices=list(SCOPE_DEFINITIONS.keys()),
        help="Subsystem extraction scope (default: general)",
    )
    parser.add_argument(
        "--since",
        default="today",
        help="Journalctl time horizon query (default: 'today')",
    )
    parser.add_argument(
        "--output",
        default="research_context.md",
        help="Target output markdown path (default: research_context.md)",
    )
    parser.add_argument(
        "--lines",
        type=int,
        default=50,
        help="Max log lines to extract per systemd unit (default: 50)",
    )
    parser.add_argument(
        "--units",
        default="schwab-trader.service,schwab-web.service,schwab-recovery-watchdog.service",
        help="Comma-separated systemd service units to query",
    )
    args = parser.parse_args()

    units_list = [u.strip() for u in args.units.split(",") if u.strip()]

    logger.info("Starting research context compilation...")
    logger.info("  Scope: %s", args.scope)
    logger.info("  Since: %s", args.since)
    logger.info("  Output target: %s", args.output)

    markdown_content = compile_research_context(
        scope=args.scope,
        since=args.since,
        lines=args.lines,
        units=units_list,
    )

    out_path = Path(args.output)
    if not out_path.is_absolute():
        out_path = ROOT_DIR / out_path

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown_content, encoding="utf-8")

    total_lines = len(markdown_content.splitlines())
    size_bytes = out_path.stat().st_size

    logger.info("Compilation complete!")
    logger.info("  File: %s", out_path)
    logger.info("  Lines: %d", total_lines)
    logger.info("  Size: %d bytes (%.2f KB)", size_bytes, size_bytes / 1024)

    # Print header preview
    print("\n--- [HEADER PREVIEW] ---")
    for line in markdown_content.splitlines()[:25]:
        print(line)
    print("------------------------\n")


if __name__ == "__main__":
    main()
