#!/usr/bin/env python3
"""
scripts/extract_full_portfolio_data.py
======================================
Full Portfolio Data Extraction & Export Pipeline (#78).

Objectives:
1. Define the complete 28-ticker universe:
   - 22 Robo portfolio symbols
   - 6 Core engine & benchmark symbols
2. Fetch 15 months of 1-hour interval historical bars via yfinance.
3. Validate downloaded frames (non-empty), flatten multi-index columns, append symbol identifier.
4. Concatenate into a single master dataset and assert total row count > 0.
5. Export with exact base filename: Full_Portfolio_15mo
   - Full_Portfolio_15mo.parquet (snappy compression)
   - Full_Portfolio_15mo.csv.gz (gzipped CSV)
6. Sync both files to the configured Google Drive paths.
7. Output final row count, unique symbol count, and exact file sizes in MB.
"""

from __future__ import annotations

import argparse
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
logger = logging.getLogger("extract_full_portfolio_data")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 1. 28-Ticker Universe Definition
ROBO_PORTFOLIO_SYMBOLS: List[str] = [
    "GOVT", "MBB", "IGIB", "EMLC", "VUG", "SCHR", "FNDC", "SCHH", "HAUZ",
    "SCHE", "SCHG", "SCHC", "SCHI", "SCHA", "SPEM", "SMBS", "FNDA", "FNDF",
    "FNDE", "SCHF", "FNDX", "SCHX",
]

CORE_ENGINE_SYMBOLS: List[str] = [
    "SOXL", "TQQQ", "TNA", "SPY", "QQQ", "IWM",
]

ALL_28_SYMBOLS: List[str] = ROBO_PORTFOLIO_SYMBOLS + CORE_ENGINE_SYMBOLS

# Google Drive Target Sync Paths
GDRIVE_TARGET_DIRS: List[Path] = [
    Path("G:/My Drive/Projects/data_exports"),
    Path("G:/My Drive/Projects/schwab_engine_research_261015/data_exports"),
    Path("G:/My Drive/Projects/schwab_engine_research_261015/data/exports"),
    Path("G:/My Drive/Projects/SchwabEngine/data/exports"),
]


def extract_symbol_data(
    symbol: str,
    period: str = "15mo",
    interval: str = "1h",
) -> Tuple[Optional[pd.DataFrame], Dict[str, Any]]:
    """
    Fetches 1h historical bars for a single symbol, flattens multi-index columns,
    and returns a standardized dataframe.
    """
    sym = symbol.strip().upper()
    t0 = time.time()
    try:
        raw_df = yf.download(
            sym,
            period=period,
            interval=interval,
            progress=False,
            auto_adjust=False,
        )
        elapsed = time.time() - t0

        if raw_df is None or raw_df.empty:
            logger.warning("Empty dataset returned for ticker %s", sym)
            return None, {
                "symbol": sym,
                "status": "FAILED_EMPTY",
                "rows": 0,
                "elapsed": elapsed,
            }

        df = raw_df.copy()

        # Flatten multi-index columns if present
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [str(col[0]) for col in df.columns]

        # Reset index to extract Datetime
        df = df.reset_index()

        # Find and normalize Datetime column
        dt_col = next((c for c in df.columns if str(c).lower() in ("datetime", "date", "index")), df.columns[0])
        df = df.rename(columns={dt_col: "Datetime"})
        df["Datetime"] = pd.to_datetime(df["Datetime"], utc=True)

        # Append Symbol column
        df["Symbol"] = sym

        # Standardize numeric columns & downcast
        for col in ["Open", "High", "Low", "Close", "Adj Close"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").astype("float32")

        if "Volume" in df.columns:
            df["Volume"] = pd.to_numeric(df["Volume"], errors="coerce").fillna(0).astype("int64")

        # Preferred column order
        preferred_order = ["Datetime", "Symbol", "Open", "High", "Low", "Close", "Adj Close", "Volume"]
        final_cols = [c for c in preferred_order if c in df.columns] + [
            c for c in df.columns if c not in preferred_order
        ]
        df = df[final_cols]

        return df, {
            "symbol": sym,
            "status": "SUCCESS",
            "rows": len(df),
            "start": str(df["Datetime"].min()),
            "end": str(df["Datetime"].max()),
            "elapsed": elapsed,
        }

    except Exception as exc:
        elapsed = time.time() - t0
        logger.error("Error fetching %s: %s", sym, exc)
        return None, {
            "symbol": sym,
            "status": "ERROR",
            "rows": 0,
            "elapsed": elapsed,
            "error": str(exc),
        }


def sync_files_to_gdrive(files: List[Path]) -> Dict[Path, List[Path]]:
    """Mirrors exported files to accessible Google Drive sync locations."""
    results: Dict[Path, List[Path]] = {f: [] for f in files}

    for target_dir in GDRIVE_TARGET_DIRS:
        drive_root = Path(target_dir.parts[0] + "/") if target_dir.is_absolute() else None
        if drive_root and not drive_root.exists():
            continue

        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            for f in files:
                dest = target_dir / f.name
                shutil.copy2(f, dest)
                if dest.exists() and dest.stat().st_size == f.stat().st_size:
                    results[f].append(dest)
                    logger.info("Synced %s -> %s (%.2f MB)", f.name, target_dir, dest.stat().st_size / (1024 * 1024))
        except Exception as exc:
            logger.warning("Could not sync to %s: %s", target_dir, exc)

    return results


def main() -> None:
    exports_dir = PROJECT_ROOT / "data" / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)

    base_name = "Full_Portfolio_15mo"
    parquet_path = exports_dir / f"{base_name}.parquet"
    csv_gz_path = exports_dir / f"{base_name}.csv.gz"

    logger.info("Commencing Full Portfolio 15-Month Hourly Data Extraction across 28 tickers...")
    collected_frames: List[pd.DataFrame] = []
    symbol_reports: List[Dict[str, Any]] = []

    for idx, sym in enumerate(ALL_28_SYMBOLS, 1):
        logger.info("[%2d/28] Extracting %s...", idx, sym)
        frame, meta = extract_symbol_data(sym, period="15mo", interval="1h")
        symbol_reports.append(meta)
        if frame is not None and not frame.empty:
            collected_frames.append(frame)

    # Validate that we have valid data and rows > 0
    if not collected_frames:
        logger.critical("Fatal: Zero frames were successfully downloaded! Aborting export.")
        sys.exit(1)

    master_df = pd.concat(collected_frames, ignore_index=True)
    master_df = master_df.sort_values(by=["Symbol", "Datetime"]).reset_index(drop=True)

    total_rows = len(master_df)
    unique_symbols = master_df["Symbol"].nunique()
    symbols_list = sorted(master_df["Symbol"].unique().tolist())

    logger.info("Extraction complete: %d total rows across %d unique symbols.", total_rows, unique_symbols)

    # Explicit validation check to prevent generating empty schema-only files
    if total_rows <= 0:
        logger.critical("Assertion Failure: Consolidated dataset has 0 rows. Aborting export.")
        sys.exit(1)

    # Export 1: Snappy-compressed Parquet
    logger.info("Exporting to snappy-compressed Parquet: %s", parquet_path)
    master_df.to_parquet(
        parquet_path,
        engine="pyarrow",
        compression="snappy",
        index=False,
    )
    parquet_bytes = parquet_path.stat().st_size
    parquet_mb = parquet_bytes / (1024 * 1024)

    # Export 2: Gzipped CSV
    logger.info("Exporting to gzipped CSV: %s", csv_gz_path)
    master_df.to_csv(
        csv_gz_path,
        index=False,
        compression="gzip",
    )
    csv_gz_bytes = csv_gz_path.stat().st_size
    csv_gz_mb = csv_gz_bytes / (1024 * 1024)

    # Sync to Google Drive
    synced_locations = sync_files_to_gdrive([parquet_path, csv_gz_path])

    # Final Output Summary
    print("\n" + "=" * 90)
    print(" FULL PORTFOLIO 15-MONTH HOURLY EXTRACTION AUDIT (#78)")
    print("=" * 90)
    print(f" Timestamp:              {datetime.datetime.now().isoformat()}")
    print(f" Base Filename:          {base_name}")
    print(f" Total Rows:             {total_rows:,} rows")
    print(f" Unique Symbols:         {unique_symbols} / 28 symbols")
    print(f" Datetime Range (UTC):   {master_df['Datetime'].min()} -> {master_df['Datetime'].max()}")
    print("-" * 90)
    print(" EXPORTED ARTIFACT SPECIFICATIONS:")
    print("-" * 90)
    print(f" 1. Snappy Parquet:      {parquet_path.name}")
    print(f"    - Full Path:         {parquet_path}")
    print(f"    - Compression:       snappy")
    print(f"    - Exact Size:        {parquet_bytes:,} bytes ({parquet_mb:.2f} MB)")
    print(f" 2. Gzipped CSV:         {csv_gz_path.name}")
    print(f"    - Full Path:         {csv_gz_path}")
    print(f"    - Compression:       gzip")
    print(f"    - Exact Size:        {csv_gz_bytes:,} bytes ({csv_gz_mb:.2f} MB)")
    print("-" * 90)
    print(" GOOGLE DRIVE SYNCHRONIZATION STATUS:")
    for src_file in [parquet_path, csv_gz_path]:
        dest_list = synced_locations.get(src_file, [])
        file_mb = src_file.stat().st_size / (1024 * 1024)
        print(f" - {src_file.name} ({file_mb:.2f} MB):")
        for dst in dest_list:
            print(f"    [CONFIRMED] {dst}")
    print("-" * 90)
    print(" SYMBOL EXTRACTION BREAKDOWN:")
    print("-" * 90)
    for rep in symbol_reports:
        sym = rep["symbol"]
        status = rep["status"]
        rows = rep.get("rows", 0)
        elapsed = f"{rep.get('elapsed', 0.0):.2f}s"
        cat = "Robo Portfolio" if sym in ROBO_PORTFOLIO_SYMBOLS else "Core Engine"
        print(f" {sym:<6} | {cat:<15} | {status:<10} | {rows:>5} rows | {elapsed}")
    print("=" * 90 + "\n")


if __name__ == "__main__":
    main()
