#!/usr/bin/env python3
"""
scripts/parse_session_telemetry.py
==================================
Compiles high-signal markdown session replay and telemetry digests (<100 KB)
from session_telemetry_<YYMMDD>.jsonl for quantitative analysis, post-close
reflection, and LLM reasoning.

Outputs:
  docs/{YYMMDD}.16-session-replay-analysis.md (EOD)
  or docs/{YYMMDD}.{HHMM}-session-digest.md (Intraday)
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import statistics
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
logger = logging.getLogger("parse_session_telemetry")

ET = ZoneInfo("America/New_York")
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def get_git_commit() -> str:
    """Gets the current git commit SHA if available."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout.strip()
    except Exception:
        return "UNKNOWN"


def format_currency(val: Optional[float]) -> str:
    if val is None:
        return "N/A"
    return f"${val:,.2f}"


def format_pct(val: Optional[float], decimals: int = 2) -> str:
    if val is None:
        return "N/A"
    sign = "+" if val > 0 else ""
    return f"{sign}{val:.{decimals}f}%"


def parse_telemetry_file(log_path: Path) -> Dict[str, Any]:
    """
    Parses the JSONL session telemetry file in a single streaming pass.
    """
    if not log_path.exists():
        raise FileNotFoundError(f"Telemetry log not found: {log_path}")

    header_metadata: Dict[str, Any] = {
        "session_date": None,
        "commit_sha": None,
        "active_account": "***4015",
        "starting_nlv": None,
        "ending_nlv": None,
    }

    recent_ticks: Dict[str, Dict[str, Any]] = {}
    spy_first_px: Optional[float] = None
    spy_last_px: Optional[float] = None
    qqq_first_px: Optional[float] = None
    qqq_last_px: Optional[float] = None

    summaries: List[Dict[str, Any]] = []
    pending_orders: List[Dict[str, Any]] = []
    executed_orders: List[Dict[str, Any]] = []

    first_ts: Optional[str] = None
    last_ts: Optional[str] = None

    with open(log_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue

            ev = rec.get("event")
            ts = rec.get("timestamp_iso")
            if ts:
                if first_ts is None:
                    first_ts = ts
                last_ts = ts

            if ev == "PRE_MARKET_SESSION_PRIMING":
                if rec.get("session_date"):
                    header_metadata["session_date"] = rec["session_date"]
                if rec.get("git_commit"):
                    header_metadata["commit_sha"] = rec["git_commit"]
                if rec.get("active_account"):
                    header_metadata["active_account"] = rec["active_account"]
                if rec.get("starting_nlv") is not None:
                    header_metadata["starting_nlv"] = float(rec["starting_nlv"])

            elif ev == "SHADOW_SESSION_START":
                if rec.get("account"):
                    header_metadata["active_account"] = rec["account"]
                if header_metadata["starting_nlv"] is None and rec.get("nlv") is not None:
                    header_metadata["starting_nlv"] = float(rec["nlv"])

            elif ev == "TICK":
                sym = rec.get("symbol", "").upper()
                px = rec.get("mid") or rec.get("last") or rec.get("microprice")
                if px and px > 0:
                    recent_ticks[sym] = rec
                    if sym == "SPY":
                        if spy_first_px is None:
                            spy_first_px = px
                        spy_last_px = px
                    elif sym == "QQQ":
                        if qqq_first_px is None:
                            qqq_first_px = px
                        qqq_last_px = px

            elif ev == "SHADOW_REBALANCE_ORDER":
                sym = rec.get("symbol", "").upper()
                order_copy = dict(rec)
                tick = recent_ticks.get(sym, {})
                micro = tick.get("microprice")
                order_copy["microprice"] = micro
                if micro and micro > 0:
                    order_copy["microprice_delta"] = round(order_copy["price"] - micro, 4)
                else:
                    order_copy["microprice_delta"] = 0.0
                pending_orders.append(order_copy)

            elif ev == "SHADOW_REBALANCE_SUMMARY":
                summaries.append(rec)
                decision = rec.get("decision", "UNKNOWN")
                for o in pending_orders:
                    o["trigger"] = decision
                    executed_orders.append(o)
                pending_orders.clear()

    # Flush any trailing pending orders if summary wasn't written
    for o in pending_orders:
        o["trigger"] = o.get("trigger", "REBALANCE")
        executed_orders.append(o)
    pending_orders.clear()

    # Date resolution: prefer log filename stamp, then last_ts date, then priming record
    match = re.search(r"session_telemetry_(\d{2})(\d{2})(\d{2})", log_path.name)
    if match:
        y, m, d = match.groups()
        header_metadata["session_date"] = f"20{y}-{m}-{d}"
    elif last_ts:
        header_metadata["session_date"] = last_ts[:10]
    elif not header_metadata["session_date"]:
        header_metadata["session_date"] = datetime.now(ET).strftime("%Y-%m-%d")

    # Commit fallback
    if not header_metadata["commit_sha"]:
        header_metadata["commit_sha"] = get_git_commit()

    # NLV values
    if summaries:
        if header_metadata["starting_nlv"] is None:
            header_metadata["starting_nlv"] = float(summaries[0].get("nlv", 0.0))
        header_metadata["ending_nlv"] = float(summaries[-1].get("nlv", 0.0))
    elif header_metadata["starting_nlv"] is not None:
        header_metadata["ending_nlv"] = header_metadata["starting_nlv"]

    return {
        "header_metadata": header_metadata,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "spy_bench": {"first": spy_first_px, "last": spy_last_px},
        "qqq_bench": {"first": qqq_first_px, "last": qqq_last_px},
        "summaries": summaries,
        "orders": executed_orders,
    }


def build_markdown_digest(data: Dict[str, Any]) -> str:
    """
    Constructs structured markdown document under 100 KB optimized for LLM ingestion.
    """
    meta = data["header_metadata"]
    start_nlv = meta["starting_nlv"] or 0.0
    end_nlv = meta["ending_nlv"] or start_nlv
    net_return_dollars = end_nlv - start_nlv
    net_return_pct = (net_return_dollars / start_nlv * 100.0) if start_nlv > 0 else 0.0

    # Benchmark computations
    spy = data["spy_bench"]
    spy_ret_pct: Optional[float] = None
    spy_delta_pct: Optional[float] = None
    spy_delta_bps: Optional[float] = None
    if spy["first"] and spy["last"] and spy["first"] > 0:
        spy_ret_pct = ((spy["last"] - spy["first"]) / spy["first"]) * 100.0
        spy_delta_pct = net_return_pct - spy_ret_pct
        spy_delta_bps = spy_delta_pct * 100.0

    qqq = data["qqq_bench"]
    qqq_ret_pct: Optional[float] = None
    qqq_delta_pct: Optional[float] = None
    qqq_delta_bps: Optional[float] = None
    if qqq["first"] and qqq["last"] and qqq["first"] > 0:
        qqq_ret_pct = ((qqq["last"] - qqq["first"]) / qqq["first"]) * 100.0
        qqq_delta_pct = net_return_pct - qqq_ret_pct
        qqq_delta_bps = qqq_delta_pct * 100.0

    summaries = data["summaries"]
    total_evals = len(summaries)

    # Regime distribution & Credit metrics
    regime_counts: Dict[str, int] = {}
    credit_spreads: List[float] = []
    hyg_ief_ratios: List[float] = []

    inside_ntz_count = 0
    upper_breach_count = 0
    lower_breach_count = 0
    decision_counts: Dict[str, int] = {}

    for s in summaries:
        regime = s.get("dominant_regime", "Unknown")
        regime_counts[regime] = regime_counts.get(regime, 0) + 1

        dec = s.get("decision", "UNKNOWN")
        decision_counts[dec] = decision_counts.get(dec, 0) + 1
        if dec == "HOLD_INSIDE_NTZ":
            inside_ntz_count += 1

        band = s.get("ntz_band")
        w_sat = s.get("satellite_weight", 0.0)
        if band and len(band) == 2:
            lo, hi = float(band[0]), float(band[1])
            if w_sat > hi:
                upper_breach_count += 1
            elif w_sat < lo and dec != "INITIAL_DEPLOYMENT" and (w_sat > 0 or s.get("core_weight", 0.0) > 0):
                lower_breach_count += 1

        cp = s.get("credit_proxy") or {}
        if cp.get("hybrid_credit_spread_bps") is not None:
            credit_spreads.append(float(cp["hybrid_credit_spread_bps"]))
        if cp.get("hyg_ief_ratio") is not None:
            hyg_ief_ratios.append(float(cp["hyg_ief_ratio"]))

    inside_ntz_pct = (inside_ntz_count / total_evals * 100.0) if total_evals > 0 else 0.0

    # Credit distribution stats
    min_spread = min(credit_spreads) if credit_spreads else 0.0
    max_spread = max(credit_spreads) if credit_spreads else 0.0
    median_spread = statistics.median(credit_spreads) if credit_spreads else 0.0

    min_ratio = min(hyg_ief_ratios) if hyg_ief_ratios else 0.0
    max_ratio = max(hyg_ief_ratios) if hyg_ief_ratios else 0.0

    # Turnover metrics
    orders = data["orders"]
    total_order_notional = sum(float(o.get("notional", 0.0)) for o in orders)
    total_order_shares = sum(int(o.get("shares", 0)) for o in orders)
    turnover_pct = (total_order_notional / start_nlv * 100.0) if start_nlv > 0 else 0.0

    # 4. Downsampled Trajectory (5-minute intervals)
    sampled_trajectory: List[Dict[str, Any]] = []
    last_bucket = None
    for s in summaries:
        ts_str = s.get("timestamp_iso", "")
        try:
            dt = datetime.fromisoformat(ts_str)
            bucket = (dt.date(), dt.hour, dt.minute // 5)
        except Exception:
            bucket = ts_str[:16]
        if bucket != last_bucket:
            sampled_trajectory.append(s)
            last_bucket = bucket

    if summaries and (not sampled_trajectory or sampled_trajectory[-1] != summaries[-1]):
        sampled_trajectory.append(summaries[-1])

    # Assemble Document
    lines: List[str] = []
    lines.append(f"# SchwabEngine Session Telemetry Digest: {meta['session_date']}")
    lines.append("")
    lines.append(f"> **Generated:** {datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S %Z')} | **Commit:** `{meta['commit_sha'][:10]}` | **Account:** `{meta['active_account']}`")
    lines.append("")

    # 1. Header Metadata
    lines.append("## 1. Header Metadata & Financial Summary")
    lines.append("")
    lines.append("| Metric | Value | Reference / Delta |")
    lines.append("| :--- | :--- | :--- |")
    lines.append(f"| **Session Date** | `{meta['session_date']}` | ET Trading Session |")
    lines.append(f"| **Commit SHA** | `{meta['commit_sha']}` | Engine Build |")
    lines.append(f"| **Active Account** | `{meta['active_account']}` | Margin / Dual-Sleeve |")
    lines.append(f"| **Starting NLV** | {format_currency(start_nlv)} | Hot-Boot Baseline |")
    lines.append(f"| **Ending NLV** | {format_currency(end_nlv)} | Mark-to-Market |")
    lines.append(f"| **Net Return ($)** | {format_currency(net_return_dollars)} | {format_pct(net_return_pct)} |")
    if spy_ret_pct is not None and spy_delta_pct is not None:
        lines.append(f"| **SPY Benchmark** | {format_pct(spy_ret_pct)} (Start: {format_currency(spy['first'])}, End: {format_currency(spy['last'])}) | Delta: **{format_pct(spy_delta_pct)}** ({spy_delta_bps:+.1f} bps) |")
    else:
        lines.append("| **SPY Benchmark** | Insufficient tick data | N/A |")
    if qqq_ret_pct is not None and qqq_delta_pct is not None:
        lines.append(f"| **QQQ Benchmark** | {format_pct(qqq_ret_pct)} (Start: {format_currency(qqq['first'])}, End: {format_currency(qqq['last'])}) | Delta: **{format_pct(qqq_delta_pct)}** ({qqq_delta_bps:+.1f} bps) |")
    else:
        lines.append("| **QQQ Benchmark** | Insufficient tick data | N/A |")
    lines.append("")

    # 2. Regime & Credit Distribution
    lines.append("## 2. Regime & Credit Distribution")
    lines.append("")
    lines.append("### Macro Regime Occupancy")
    lines.append("| Regime | Interval Count | Time Percentage | Dominant Target $W_{sat}$ |")
    lines.append("| :--- | :--- | :--- | :--- |")
    for r_name in ["Goldilocks", "Uncertainty", "Shock"]:
        cnt = regime_counts.get(r_name, 0)
        pct = (cnt / total_evals * 100.0) if total_evals > 0 else 0.0
        target_str = "0.25 (Band: 21-29%)" if r_name == "Goldilocks" else ("0.10 (Band: 8-12%)" if r_name == "Uncertainty" else "0.00 (Flat Cash)")
        lines.append(f"| **{r_name}** | {cnt:,} | {pct:.2f}% | {target_str} |")
    # Capture any unexpected regime
    for r_name, cnt in regime_counts.items():
        if r_name not in ["Goldilocks", "Uncertainty", "Shock"]:
            pct = (cnt / total_evals * 100.0) if total_evals > 0 else 0.0
            lines.append(f"| **{r_name}** | {cnt:,} | {pct:.2f}% | Dynamic |")
    lines.append("")

    lines.append("### Hybrid Credit Spread & HYG/IEF Proxy")
    lines.append("| Metric | Min | Max | Median / Current |")
    lines.append("| :--- | :--- | :--- | :--- |")
    lines.append(f"| **Hybrid Credit Spread** | {min_spread:.2f} bps | {max_spread:.2f} bps | {median_spread:.2f} bps |")
    lines.append(f"| **Live HYG/IEF Ratio** | {min_ratio:.6f} | {max_ratio:.6f} | Range: {(max_ratio - min_ratio):.6f} |")
    lines.append("")

    # 3. Leland NTZ Performance
    lines.append("## 3. Leland No-Trade Zone (NTZ) Performance")
    lines.append("")
    lines.append("| Metric | Value | Percentage / Context |")
    lines.append("| :--- | :--- | :--- |")
    lines.append(f"| **Total Evaluated Intervals** | {total_evals:,} | 30s Rebalance Sweeps |")
    lines.append(f"| **Hold Inside NTZ (`HOLD_INSIDE_NTZ`)** | {inside_ntz_count:,} | **{inside_ntz_pct:.2f}%** efficiency |")
    lines.append(f"| **Upper Harvest Breaches ($W_{{sat}} > hi$)** | {upper_breach_count:,} | {(upper_breach_count / total_evals * 100.0) if total_evals else 0.0:.2f}% |")
    lines.append(f"| **Lower Accumulate Breaches ($W_{{sat}} < lo$)** | {lower_breach_count:,} | {(lower_breach_count / total_evals * 100.0) if total_evals else 0.0:.2f}% |")
    lines.append(f"| **Simulated Orders Emitted** | {len(orders)} orders | {total_order_shares} total shares |")
    lines.append(f"| **Session Turnover Notional** | {format_currency(total_order_notional)} | {turnover_pct:.2f}% of baseline NLV |")
    lines.append("")

    lines.append("### Decision Breakdown")
    lines.append("| Decision Trigger | Occurrences | Percentage |")
    lines.append("| :--- | :--- | :--- |")
    for dec, count in sorted(decision_counts.items(), key=lambda x: -x[1]):
        pct = (count / total_evals * 100.0) if total_evals > 0 else 0.0
        lines.append(f"| `{dec}` | {count:,} | {pct:.2f}% |")
    lines.append("")

    # 4. Downsampled Trajectory
    lines.append("## 4. Downsampled 5-Minute Trajectory")
    lines.append("")
    lines.append("| Timestamp (ET) | NLV ($) | Active $W_{sat}$ | Target $W_{sat}$ | NTZ Band | Dominant Regime | Credit Spread (bps) |")
    lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
    for row in sampled_trajectory:
        raw_ts = row.get("timestamp_iso", "")
        # Shorten timestamp to cleaner format
        try:
            dt = datetime.fromisoformat(raw_ts).astimezone(ET)
            ts_disp = dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            ts_disp = raw_ts[:19].replace("T", " ")

        nlv_disp = format_currency(row.get("nlv"))
        w_sat = row.get("satellite_weight", 0.0)
        tgt_sat = row.get("target_satellite_weight", 0.0)
        band = row.get("ntz_band") or [0.0, 0.0]
        band_disp = f"[{band[0]:.4f}, {band[1]:.4f}]" if len(band) == 2 else str(band)
        regime = row.get("dominant_regime", "Unknown")
        cp = row.get("credit_proxy") or {}
        spread = cp.get("hybrid_credit_spread_bps")
        spread_disp = f"{spread:.2f}" if spread is not None else "N/A"

        lines.append(f"| {ts_disp} | {nlv_disp} | {w_sat:.4f} | {tgt_sat:.4f} | {band_disp} | {regime} | {spread_disp} |")
    lines.append("")

    # 5. Simulated Execution Summary
    lines.append("## 5. Simulated Execution Summary")
    lines.append("")
    if not orders:
        lines.append("> *No simulated rebalance orders were triggered during this session.*")
    else:
        lines.append("| Timestamp (ET) | Order ID | Symbol | Sleeve | Side | Qty | Price ($) | Microprice ($) | Microprice Delta | Notional ($) | Trigger |")
        lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
        for o in orders:
            raw_ts = o.get("timestamp_iso", "")
            try:
                dt = datetime.fromisoformat(raw_ts).astimezone(ET)
                ts_disp = dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                ts_disp = raw_ts[:19].replace("T", " ")

            oid = o.get("order_id", "N/A")
            sym = o.get("symbol", "N/A")
            sleeve = o.get("sleeve", "N/A")
            side = o.get("side", "N/A")
            qty = o.get("shares", 0)
            px = o.get("price", 0.0)
            micro = o.get("microprice")
            micro_disp = f"${micro:.4f}" if micro is not None else "N/A"
            delta = o.get("microprice_delta", 0.0)
            sign = "+" if delta > 0 else ""
            delta_disp = f"{sign}${delta:.4f}" if micro is not None else "N/A"
            notional = o.get("notional", px * qty)
            trigger = o.get("trigger", "REBALANCE")

            lines.append(f"| {ts_disp} | `{oid}` | **{sym}** | {sleeve} | `{side}` | {qty} | ${px:.2f} | {micro_disp} | {delta_disp} | ${notional:,.2f} | `{trigger}` |")

    lines.append("")
    lines.append("---")
    lines.append("*SchwabEngine Autonomous Telemetry Pipeline v2.4 (Argon) — Automated End-of-Day Replication Engine*")
    lines.append("")

    return "\n".join(lines)


def get_default_output_path(log_path: Path, session_date: str) -> Path:
    """
    Returns default destination:
      docs/{YYMMDD}.16-session-replay-analysis.md (if EOD >= 16:00 ET)
      or docs/{YYMMDD}.{HHMM}-session-digest.md (if Intraday)
    """
    now_et = datetime.now(ET)
    # Extract YYMMDD
    clean_date = session_date.replace("-", "")[2:] if len(session_date) >= 8 else session_date
    if now_et.strftime("%y%m%d") == clean_date and now_et.hour < 16:
        tag = now_et.strftime("%H")
        return PROJECT_ROOT / "docs" / f"{clean_date}.{tag}-intraday-session-digest.md"
    return PROJECT_ROOT / "docs" / f"{clean_date}.16-session-replay-analysis.md"


def generate_session_digest(log_path: Path, output_path: Optional[Path] = None) -> Path:
    """
    Parses the telemetry log and writes the markdown digest to output_path.
    """
    logger.info("Parsing telemetry log: %s", log_path)
    data = parse_telemetry_file(log_path)
    digest_md = build_markdown_digest(data)

    if output_path is None:
        session_date = data["header_metadata"]["session_date"] or datetime.now(ET).strftime("%Y-%m-%d")
        output_path = get_default_output_path(log_path, session_date)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(digest_md)

    size_kb = output_path.stat().st_size / 1024.0
    logger.info("Successfully generated digest at %s (%.2f KB, %d lines)", output_path, size_kb, len(digest_md.splitlines()))
    if size_kb > 100.0:
        logger.warning("Digest size (%.2f KB) exceeds 100 KB limit!", size_kb)

    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse session telemetry into compact LLM replay digest.")
    parser.add_argument(
        "--log",
        type=str,
        default=None,
        help="Path to session_telemetry_<YYMMDD>.jsonl (defaults to today's active session log).",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Optional path for output markdown (defaults to docs/{YYMMDD}.16-session-replay-analysis.md).",
    )
    args = parser.parse_args()

    if args.log:
        log_path = Path(args.log)
    else:
        stamp = datetime.now(ET).strftime("%y%m%d")
        log_path = PROJECT_ROOT / "logs" / f"session_telemetry_{stamp}.jsonl"
        if not log_path.exists():
            # Find latest jsonl in logs
            candidates = sorted((PROJECT_ROOT / "logs").glob("session_telemetry_*.jsonl"))
            if candidates:
                log_path = candidates[-1]

    if not log_path.exists():
        logger.error("Session telemetry log not found: %s", log_path)
        return 1

    out_path = Path(args.output) if args.output else None
    try:
        final_dest = generate_session_digest(log_path, out_path)
        print(f"Digest generated: {final_dest}")
        return 0
    except Exception as exc:
        logger.exception("Failed to generate telemetry digest: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
