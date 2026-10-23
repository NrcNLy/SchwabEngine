#!/usr/bin/env python3
"""
scripts/fetch_portfolio_hourly_data.py
======================================
Fetches 15 months of 1-hour interval historical market data for all 28 symbols
spanning the automated Robo portfolio and active engine universe, aggregates
into a single master CSV, and synchronizes to Google Drive.

Portfolio & Engine Universe (28 symbols):
- Robo Portfolio (22):
    GOVT, MBB, IGIB, EMLC, VUG, SCHR, FNDC, SCHH, HAUZ, SCHE, SCHG, SCHC,
    SCHI, SCHA, SPEM, SMBS, FNDA, FNDF, FNDE, SCHF, FNDX, SCHX
- Engine & Benchmarks (6):
    SOXL, TQQQ, TNA, SPY, QQQ, IWM
"""

from __future__ import annotations

import datetime
import logging
import os
from pathlib import Path
import shutil
import sys
import time
from typing import Any, Dict, List, Tuple

import pandas as pd
import yfinance as yf

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("fetch_portfolio_hourly_data")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 1. Define Complete Universe (28 Symbols)
ROBO_PORTFOLIO_SYMBOLS: List[str] = [
    "GOVT", "MBB", "IGIB", "EMLC", "VUG", "SCHR", "FNDC", "SCHH", "HAUZ",
    "SCHE", "SCHG", "SCHC", "SCHI", "SCHA", "SPEM", "SMBS", "FNDA", "FNDF",
    "FNDE", "SCHF", "FNDX", "SCHX",
]

ENGINE_AND_BENCHMARK_SYMBOLS: List[str] = [
    "SOXL", "TQQQ", "TNA", "SPY", "QQQ", "IWM",
]

ALL_SYMBOLS: List[str] = ROBO_PORTFOLIO_SYMBOLS + ENGINE_AND_BENCHMARK_SYMBOLS

# Google Drive sync paths to check and replicate
GDRIVE_TARGET_DIRS: List[Path] = [
    Path("G:/My Drive/Projects/data_exports"),
    Path("G:/My Drive/Projects/schwab_engine_research_261015/data_exports"),
    Path("G:/My Drive/Projects/schwab_engine_research_261015/data/exports"),
    Path("G:/My Drive/Projects/SchwabEngine/data/exports"),
]


def fetch_hourly_data_for_symbols(
    symbols: List[str],
    period: str = "15mo",
    interval: str = "1h",
) -> Tuple[pd.DataFrame, Dict[str, Dict[str, Any]]]:
    """
    Fetches hourly historical market data using yfinance for each symbol,
    flattens multi-index columns, and combines into a single dataframe.
    """
    status_records: Dict[str, Dict[str, Any]] = {}
    dataframes: List[pd.DataFrame] = []

    logger.info("Initiating hourly download across %d symbols (period=%s, interval=%s)...", len(symbols), period, interval)

    for idx, sym in enumerate(symbols, 1):
        sym_clean = sym.strip().upper()
        logger.info("[%d/%d] Fetching %s...", idx, len(symbols), sym_clean)
        t0 = time.time()
        try:
            raw_df = yf.download(
                sym_clean,
                period=period,
                interval=interval,
                progress=False,
                auto_adjust=False,
            )

            elapsed = time.time() - t0

            if raw_df is None or raw_df.empty:
                logger.warning("Empty data returned for %s", sym_clean)
                status_records[sym_clean] = {
                    "status": "FAILED_EMPTY",
                    "rows": 0,
                    "date_start": None,
                    "date_end": None,
                    "elapsed_sec": round(elapsed, 2),
                    "error": "No bars returned",
                }
                continue

            # Flatten multi-index columns if present
            df = raw_df.copy()
            if isinstance(df.columns, pd.MultiIndex):
                # Dropping or taking first level of MultiIndex
                df.columns = [str(c[0]) for c in df.columns]

            # Standardize index into Datetime column
            df = df.reset_index()
            dt_col = None
            for c in df.columns:
                if str(c).lower() in ("datetime", "date", "index"):
                    dt_col = c
                    break

            if dt_col is None:
                dt_col = df.columns[0]

            df = df.rename(columns={dt_col: "Datetime"})

            # Append Ticker / Symbol column
            df["Symbol"] = sym_clean

            # Organize standard columns: Datetime, Symbol, Open, High, Low, Close, Adj Close, Volume
            cols_preferred = ["Datetime", "Symbol", "Open", "High", "Low", "Close", "Adj Close", "Volume"]
            cols_present = [c for c in cols_preferred if c in df.columns] + [
                c for c in df.columns if c not in cols_preferred
            ]
            df = df[cols_present]

            rows_count = len(df)
            start_dt = str(df["Datetime"].iloc[0])
            end_dt = str(df["Datetime"].iloc[-1])

            dataframes.append(df)
            status_records[sym_clean] = {
                "status": "SUCCESS",
                "rows": rows_count,
                "date_start": start_dt,
                "date_end": end_dt,
                "elapsed_sec": round(elapsed, 2),
                "error": None,
            }
            logger.info("Successfully fetched %s: %d rows (%s -> %s) in %.2fs", sym_clean, rows_count, start_dt, end_dt, elapsed)

        except Exception as exc:
            elapsed = time.time() - t0
            logger.error("Exception fetching %s: %s", sym_clean, exc)
            status_records[sym_clean] = {
                "status": "ERROR",
                "rows": 0,
                "date_start": None,
                "date_end": None,
                "elapsed_sec": round(elapsed, 2),
                "error": str(exc),
            }

    if dataframes:
        master_df = pd.concat(dataframes, ignore_index=True)
    else:
        master_df = pd.DataFrame()

    return master_df, status_records


def sync_to_google_drive(source_file: Path) -> List[Path]:
    """
    Copies the generated CSV into Google Drive sync locations.
    """
    synced_locations: List[Path] = []
    if not source_file.exists():
        logger.error("Source file does not exist for Google Drive sync: %s", source_file)
        return synced_locations

    for target_dir in GDRIVE_TARGET_DIRS:
        # Check if parent Drive drive (G:) is available
        drive_root = Path(target_dir.parts[0] + "/") if target_dir.is_absolute() else None
        if drive_root and not drive_root.exists():
            continue

        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            dest_file = target_dir / source_file.name
            shutil.copy2(source_file, dest_file)
            if dest_file.exists() and dest_file.stat().st_size == source_file.stat().st_size:
                synced_locations.append(dest_file)
                logger.info("Successfully synced to Google Drive: %s (%d bytes)", dest_file, dest_file.stat().st_size)
            else:
                logger.warning("Verification failed for sync target: %s", dest_file)
        except Exception as exc:
            logger.warning("Could not sync to %s: %s", target_dir, exc)

    return synced_locations


def main() -> None:
    timestamp_tag = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_filename = f"portfolio_hourly_bars_15mo_{timestamp_tag}.csv"

    # Local export directory
    local_export_dir = PROJECT_ROOT / "data" / "exports"
    local_export_dir.mkdir(parents=True, exist_ok=True)
    local_csv_path = local_export_dir / output_filename

    # Execute fetch
    master_df, status_records = fetch_hourly_data_for_symbols(ALL_SYMBOLS, period="15mo", interval="1h")

    total_rows = len(master_df)
    success_count = sum(1 for r in status_records.values() if r["status"] == "SUCCESS")
    failed_count = len(status_records) - success_count

    # Save local CSV
    if total_rows > 0:
        master_df.to_csv(local_csv_path, index=False)
        file_size_bytes = local_csv_path.stat().st_size
        logger.info("Master dataset written locally to: %s (%d bytes, %d rows)", local_csv_path, file_size_bytes, total_rows)
    else:
        file_size_bytes = 0
        logger.error("No data collected across any symbols!")

    # Synchronize to Google Drive
    synced_gdrive_paths = sync_to_google_drive(local_csv_path) if local_csv_path.exists() else []

    # Display Executive Summary Table
    print("\n" + "=" * 90)
    print(" PORTFOLIO & ENGINE 15-MONTH HOURLY DATA EXTRACTION & DRIVE SYNC")
    print("=" * 90)
    print(f" Execution Timestamp:     {datetime.datetime.now().isoformat()}")
    print(f" Target Symbols Universe: {len(ALL_SYMBOLS)} instruments (22 Robo + 6 Active/Benchmark)")
    print(f" Total Rows Downloaded:   {total_rows:,} records")
    print(f" Successfully Downloaded: {success_count}/{len(ALL_SYMBOLS)} symbols")
    print(f" Failed / Empty Symbols:  {failed_count}/{len(ALL_SYMBOLS)} symbols")
    print(f" Local CSV Destination:   {local_csv_path} ({file_size_bytes:,} bytes)")
    print("-" * 90)
    print(f"{'Ticker':<8} | {'Category':<15} | {'Status':<12} | {'Rows':<8} | {'Timespan':<38} | {'Elapsed'}")
    print("-" * 90)

    for sym in ALL_SYMBOLS:
        rec = status_records.get(sym, {})
        category = "Robo Portfolio" if sym in ROBO_PORTFOLIO_SYMBOLS else "Engine/Benchmark"
        status = rec.get("status", "UNKNOWN")
        rows = rec.get("rows", 0)
        d_start = rec.get("date_start", "")
        d_end = rec.get("date_end", "")
        elapsed = f"{rec.get('elapsed_sec', 0.0):.1f}s"
        
        timespan = f"{d_start[:10]} to {d_end[:10]}" if (d_start and d_end) else "N/A"
        print(f"{sym:<8} | {category:<15} | {status:<12} | {rows:<8} | {timespan:<38} | {elapsed}")

    print("-" * 90)
    print(" GOOGLE DRIVE SYNCHRONIZATION STATUS:")
    if synced_gdrive_paths:
        for p in synced_gdrive_paths:
            print(f"  [CONFIRMED] {p} ({p.stat().st_size:,} bytes)")
    else:
        print("  [WARNING] No Google Drive locations were synced. Please verify G: mount.")
    print("=" * 90 + "\n")


if __name__ == "__main__":
    main()
