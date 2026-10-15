#!/usr/bin/env python3
"""
scripts/export_research_datasets.py
===================================
Exports research datasets for Google Colab and Google AI Studio analysis.
- Ingests data/exports/macro_index_history.csv
- Extracts daily and hourly aggregates covering SOXL, SOXX, QQQ, SES, and regime tags.
- Queries state/trades.db to extract historical order executions and fill stamps
  (including Order 1008303563072).
- Exports:
    1. /home/nicho/schwab_state/macro_research_matrix.parquet (and .json fallback)
    2. /home/nicho/schwab_state/backtest_dataset.json (compact format for AI Studio)
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("export_research_datasets")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.paths import STATE_DIR, TRADES_DB_PATH


def resolve_output_dir(custom_dir: Optional[str] = None) -> Path:
    """Resolves output directory, prioritizing /home/nicho/schwab_state."""
    if custom_dir:
        out = Path(custom_dir)
    else:
        vm_dir = Path("/home/nicho/schwab_state")
        out = vm_dir if vm_dir.is_dir() else STATE_DIR
    out.mkdir(parents=True, exist_ok=True)
    return out


def resolve_csv_path() -> Path:
    """Finds macro_index_history.csv in standard locations."""
    candidates = [
        PROJECT_ROOT / "data" / "exports" / "macro_index_history.csv",
        PROJECT_ROOT / "data" / "macro_index_history.csv",
        Path("data/exports/macro_index_history.csv"),
    ]
    for c in candidates:
        if c.exists() and c.stat().st_size > 0:
            return c
    raise FileNotFoundError(f"macro_index_history.csv not found in candidate paths: {candidates}")


def load_trades_history() -> List[Dict[str, Any]]:
    """Extracts all historical executions from SQLite trades.db and enriches with Order 1008303563072."""
    db_candidates = [
        Path("/home/nicho/schwab_state/trades.db"),
        TRADES_DB_PATH,
        PROJECT_ROOT / "state" / "trades.db",
    ]
    db_path = next((p for p in db_candidates if p.exists()), None)
    trades: List[Dict[str, Any]] = []

    if db_path:
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='trades';")
            if cursor.fetchone():
                cursor.execute(
                    """
                    SELECT id, trade_id, symbol, side, quantity, price, cost_basis,
                           realized_pnl, disallowed_loss, timestamp, regime, simulated, env, created_at
                    FROM trades
                    ORDER BY timestamp ASC;
                    """
                )
                for row in cursor.fetchall():
                    trades.append(dict(row))
            conn.close()
            logger.info("Extracted %d historical trades from %s", len(trades), db_path)
        except Exception as exc:
            logger.warning("Could not read trades from %s: %exc", db_path, exc)

    # Ensure today's Order 1008303563072 fill stamps are explicitly catalogued
    order_1008303563072 = {
        "order_id": "1008303563072",
        "symbol": "SOXL",
        "side": "SELL",
        "quantity": 18,
        "order_type": "MARKET",
        "status": "FILLED",
        "entered_time": "2026-10-15T13:30:01+0000",
        "closed_time": "2026-10-15T13:30:08+0000",
        "fill_price": 98.7632,
        "net_proceeds": 1777.70,
        "unsettled_cash": 1777.70,
        "settlement_date": "2026-10-16",
        "exit_reason": "RECOVERY_FLOOR",
        "account_suffix": "015",
        "note": "Floor breach at $98.73 executed via Market Sell with zero slippage relative to bid.",
    }

    # Match or append to trades list
    found_order = False
    for t in trades:
        if "1008303563072" in str(t.get("trade_id", "")) or (
            t.get("symbol") == "SOXL" and "2026-10-15" in str(t.get("timestamp", ""))
        ):
            t["schwab_order_id"] = "1008303563072"
            t["schwab_fill_price"] = 98.7632
            t["schwab_entered_time"] = "2026-10-15T13:30:01+0000"
            t["schwab_closed_time"] = "2026-10-15T13:30:08+0000"
            t["exit_reason"] = "RECOVERY_FLOOR"
            found_order = True

    if not found_order:
        trades.append(order_1008303563072)

    return trades


def export_datasets(output_dir: Path) -> Dict[str, Any]:
    """Generates the macro research matrix and backtest dataset."""
    import pandas as pd

    csv_path = resolve_csv_path()
    logger.info("Ingesting macro index history from: %s", csv_path)

    df = pd.read_csv(csv_path)
    df["timestamp_edt"] = df["timestamp_edt"].astype(str)
    df["session_date"] = df["timestamp_edt"].str.slice(0, 10)

    # Sort chronologically
    df = df.sort_values(by="timestamp_edt").reset_index(drop=True)

    unique_sessions = df["session_date"].unique().tolist()
    total_sessions = len(unique_sessions)
    logger.info("Parsed %d hourly records spanning %d unique trading sessions", len(df), total_sessions)

    # Build Daily Session Aggregates
    daily_aggregates: List[Dict[str, Any]] = []
    for s_date, grp in df.groupby("session_date", sort=True):
        soxl_vals = grp["soxl_close"].dropna().values
        soxx_vals = grp["soxx_close"].dropna().values
        qqq_vals = grp["qqq_close"].dropna().values
        ses_vals = grp["ses"].dropna().values

        daily_aggregates.append({
            "session_date": s_date,
            "soxl_open": round(float(soxl_vals[0]), 4) if len(soxl_vals) else None,
            "soxl_high": round(float(soxl_vals.max()), 4) if len(soxl_vals) else None,
            "soxl_low": round(float(soxl_vals.min()), 4) if len(soxl_vals) else None,
            "soxl_close": round(float(soxl_vals[-1]), 4) if len(soxl_vals) else None,
            "soxx_open": round(float(soxx_vals[0]), 4) if len(soxx_vals) else None,
            "soxx_close": round(float(soxx_vals[-1]), 4) if len(soxx_vals) else None,
            "qqq_open": round(float(qqq_vals[0]), 4) if len(qqq_vals) else None,
            "qqq_close": round(float(qqq_vals[-1]), 4) if len(qqq_vals) else None,
            "ses_mean": round(float(ses_vals.mean()), 4) if len(ses_vals) else None,
            "ses_close": round(float(ses_vals[-1]), 4) if len(ses_vals) else None,
            "regime": str(grp["regime"].iloc[-1]) if "regime" in grp else "UNKNOWN",
            "hourly_bars": int(len(grp)),
        })

    daily_df = pd.DataFrame(daily_aggregates)

    # Export 1: macro_research_matrix.parquet & macro_research_matrix.json
    parquet_path = output_dir / "macro_research_matrix.parquet"
    json_matrix_path = output_dir / "macro_research_matrix.json"

    # Select research matrix columns
    export_cols = [
        "timestamp_edt",
        "session_date",
        "soxx_close",
        "soxl_close",
        "qqq_close",
        "tnx_close",
        "vxn_close",
        "uup_close",
        "rs_ratio",
        "rs_spread",
        "ses",
        "regime",
        "score_trend",
        "score_rs",
        "score_vxn",
        "score_tnx",
    ]
    matrix_df = df[[c for c in export_cols if c in df.columns]].copy()

    try:
        matrix_df.to_parquet(parquet_path, index=False, engine="pyarrow")
        logger.info("Saved Parquet matrix: %s (size: %d bytes)", parquet_path, parquet_path.stat().st_size)
    except Exception as exc:
        logger.warning("Could not export Parquet (falling back to JSON only): %s", exc)

    # Web fallback JSON (split-oriented or records)
    matrix_df.to_json(json_matrix_path, orient="records", date_format="iso")
    logger.info("Saved JSON matrix fallback: %s (size: %d bytes)", json_matrix_path, json_matrix_path.stat().st_size)

    # Export 2: backtest_dataset.json (compact format for AI Studio / Colab)
    trades = load_trades_history()
    regimes_dist = df["regime"].value_counts().to_dict()

    backtest_data = {
        "metadata": {
            "title": "SchwabEngine Historical Research Matrix & Backtest Dataset",
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "source_file": str(csv_path.name),
            "total_trading_sessions": total_sessions,
            "total_hourly_records": len(df),
            "date_range": {
                "start": unique_sessions[0] if unique_sessions else None,
                "end": unique_sessions[-1] if unique_sessions else None,
            },
            "symbols_covered": ["SOXL", "SOXX", "QQQ", "TNX", "VXN", "UUP"],
            "regimes_distribution": regimes_dist,
        },
        "historical_order_executions": trades,
        "daily_sessions": daily_aggregates,
    }

    backtest_json_path = output_dir / "backtest_dataset.json"
    with open(backtest_json_path, "w", encoding="utf-8") as f:
        json.dump(backtest_data, f, indent=2)

    logger.info(
        "Saved Backtest Dataset: %s (size: %d bytes, sessions: %d, trades: %d)",
        backtest_json_path,
        backtest_json_path.stat().st_size,
        len(daily_aggregates),
        len(trades),
    )

    return {
        "parquet_path": str(parquet_path),
        "parquet_size": parquet_path.stat().st_size if parquet_path.exists() else 0,
        "json_matrix_path": str(json_matrix_path),
        "json_matrix_size": json_matrix_path.stat().st_size if json_matrix_path.exists() else 0,
        "backtest_json_path": str(backtest_json_path),
        "backtest_json_size": backtest_json_path.stat().st_size if backtest_json_path.exists() else 0,
        "total_sessions": total_sessions,
        "total_hourly_records": len(df),
        "total_trades": len(trades),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Export research datasets for Colab and AI Studio.")
    parser.add_argument("--output-dir", default=None, help="Target output directory")
    args = parser.parse_args()

    out_dir = resolve_output_dir(args.output_dir)
    res = export_datasets(out_dir)

    print("\n" + "=" * 70)
    print(" RESEARCH DATASET EXPORT COMPLETE")
    print("=" * 70)
    print(f" Output Directory:       {out_dir}")
    print(f" Total Sessions:         {res['total_sessions']}")
    print(f" Total Hourly Records:   {res['total_hourly_records']}")
    print(f" Historical Trades:      {res['total_trades']}")
    print("-" * 70)
    print(f" Parquet Matrix:         {res['parquet_path']} ({res['parquet_size']:,} bytes)")
    print(f" JSON Matrix Fallback:   {res['json_matrix_path']} ({res['json_matrix_size']:,} bytes)")
    print(f" AI Studio Backtest:     {res['backtest_json_path']} ({res['backtest_json_size']:,} bytes)")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()
