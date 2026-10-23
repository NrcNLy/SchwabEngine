#!/usr/bin/env python3
"""
scripts/compress_portfolio_bars.py
==================================
Compresses Hourly Portfolio Bars for Downstream Analysis (#77).

Objectives:
1. Locate downloaded 15-month hourly bar CSV files in data/exports/.
2. Consolidate all 28 symbols into a single clean DataFrame:
   - Datetime: UTC timestamp (datetime64[ns, UTC])
   - Open, High, Low, Close, Adj Close: float32
   - Volume: int64
3. Generate two space-efficient master formats:
   - Parquet with zstd compression (preserving typed schemas)
   - Gzipped CSV (.csv.gz) for universal compatibility
4. Split master Parquet into two web-upload-optimized groupings:
   - Core engine hourly bars: SOXL, TQQQ, TNA, SPY, QQQ, IWM (6 symbols)
   - Robo portfolio hourly bars: 22 asset allocation symbols
5. Save locally in data/exports/ and sync to designated Google Drive paths.
"""

from __future__ import annotations

import argparse
import datetime
import logging
import os
from pathlib import Path
import shutil
import sys
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("compress_portfolio_bars")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Symbol Universes
CORE_ENGINE_SYMBOLS: List[str] = ["SOXL", "TQQQ", "TNA", "SPY", "QQQ", "IWM"]
ROBO_PORTFOLIO_SYMBOLS: List[str] = [
    "GOVT", "MBB", "IGIB", "EMLC", "VUG", "SCHR", "FNDC", "SCHH", "HAUZ",
    "SCHE", "SCHG", "SCHC", "SCHI", "SCHA", "SPEM", "SMBS", "FNDA", "FNDF",
    "FNDE", "SCHF", "FNDX", "SCHX",
]
ALL_28_SYMBOLS: List[str] = ROBO_PORTFOLIO_SYMBOLS + CORE_ENGINE_SYMBOLS

# Google Drive sync paths
GDRIVE_TARGET_DIRS: List[Path] = [
    Path("G:/My Drive/Projects/data_exports"),
    Path("G:/My Drive/Projects/schwab_engine_research_261015/data_exports"),
    Path("G:/My Drive/Projects/schwab_engine_research_261015/data/exports"),
    Path("G:/My Drive/Projects/SchwabEngine/data/exports"),
]


def find_latest_hourly_csv(exports_dir: Path) -> Path:
    """Finds the most recent hourly bars CSV in data/exports."""
    csv_candidates = list(exports_dir.glob("portfolio_hourly_bars_15mo_*.csv"))
    if not csv_candidates:
        csv_candidates = list(exports_dir.glob("*.csv"))
    if not csv_candidates:
        raise FileNotFoundError(f"No portfolio hourly bars CSV found in {exports_dir}")

    # Return the newest by modification time
    csv_candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return csv_candidates[0]


def process_and_standardize_data(csv_path: Path) -> pd.DataFrame:
    """
    Loads CSV, validates symbols, and casts schemas:
    - Datetime -> UTC
    - OHLC -> float32
    - Volume -> int64
    - Symbol -> category / string
    """
    logger.info("Reading source CSV: %s (%d bytes)...", csv_path.name, csv_path.stat().st_size)
    df = pd.read_csv(csv_path)

    logger.info("Raw schema: %d rows x %d columns. Columns: %s", len(df), len(df.columns), list(df.columns))

    # 1. Parse Datetime to UTC
    df["Datetime"] = pd.to_datetime(df["Datetime"], utc=True)

    # 2. Standardize Symbol column
    df["Symbol"] = df["Symbol"].astype(str).str.strip().str.upper()

    # 3. Downcast numeric columns
    numeric_float_cols = ["Open", "High", "Low", "Close", "Adj Close"]
    for col in numeric_float_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("float32")

    if "Volume" in df.columns:
        df["Volume"] = pd.to_numeric(df["Volume"], errors="coerce").fillna(0).astype("int64")

    # Sort deterministically: Symbol, Datetime
    df = df.sort_values(by=["Symbol", "Datetime"]).reset_index(drop=True)

    return df


def sync_files_to_gdrive(files: List[Path]) -> Dict[Path, List[Path]]:
    """Copies generated artifacts into Google Drive locations."""
    sync_results: Dict[Path, List[Path]] = {f: [] for f in files}

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
                    sync_results[f].append(dest)
                    logger.info("Synced %s -> %s (%d bytes)", f.name, target_dir, dest.stat().st_size)
        except Exception as exc:
            logger.warning("Could not sync to %s: %s", target_dir, exc)

    return sync_results


def main() -> None:
    exports_dir = PROJECT_ROOT / "data" / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)

    source_csv = find_latest_hourly_csv(exports_dir)
    raw_size_bytes = source_csv.stat().st_size
    logger.info("Found source hourly dataset: %s", source_csv)

    # Standardize data
    df = process_and_standardize_data(source_csv)
    total_rows = len(df)
    unique_symbols = sorted(df["Symbol"].unique().tolist())
    symbol_counts = df["Symbol"].value_counts().to_dict()

    # Base naming prefix
    stem = source_csv.stem

    # 1. Master Compressed Parquet (zstd)
    master_parquet_path = exports_dir / f"{stem}.parquet"
    logger.info("Writing master Parquet with zstd compression...")
    df.to_parquet(master_parquet_path, engine="pyarrow", compression="zstd", index=False)
    master_parquet_size = master_parquet_path.stat().st_size

    # 2. Master Gzipped CSV
    master_gz_csv_path = exports_dir / f"{stem}.csv.gz"
    logger.info("Writing master gzipped CSV...")
    df.to_csv(master_gz_csv_path, index=False, compression="gzip")
    master_gz_size = master_gz_csv_path.stat().st_size

    # 3. Core Engine Subset Parquet (SOXL, TQQQ, TNA, SPY, QQQ, IWM)
    df_core = df[df["Symbol"].isin(CORE_ENGINE_SYMBOLS)].copy().reset_index(drop=True)
    core_parquet_path = exports_dir / f"core_engine_hourly_bars_15mo.parquet"
    df_core.to_parquet(core_parquet_path, engine="pyarrow", compression="zstd", index=False)
    core_parquet_size = core_parquet_path.stat().st_size

    # 4. Robo Portfolio Subset Parquet (22 symbols)
    df_robo = df[df["Symbol"].isin(ROBO_PORTFOLIO_SYMBOLS)].copy().reset_index(drop=True)
    robo_parquet_path = exports_dir / f"robo_portfolio_hourly_bars_15mo.parquet"
    df_robo.to_parquet(robo_parquet_path, engine="pyarrow", compression="zstd", index=False)
    robo_parquet_size = robo_parquet_path.stat().st_size

    # Sync all 4 generated files to Google Drive
    generated_files = [
        master_parquet_path,
        master_gz_csv_path,
        core_parquet_path,
        robo_parquet_path,
    ]
    sync_manifest = sync_files_to_gdrive(generated_files)

    # Calculate compression ratios
    parquet_reduction = (1.0 - (master_parquet_size / raw_size_bytes)) * 100.0
    gz_reduction = (1.0 - (master_gz_size / raw_size_bytes)) * 100.0

    # Print Executive Summary Table
    print("\n" + "=" * 90)
    print(" HOURLY PORTFOLIO BARS COMPRESSION & DOWNSTREAM PARTITIONING (#77)")
    print("=" * 90)
    print(f" Source CSV Input:        {source_csv.name} ({raw_size_bytes:,} bytes, {total_rows:,} rows)")
    print(f" Total Symbols Processed: {len(unique_symbols)} / 28 symbols")
    print(f" Datetime Range:          {df['Datetime'].min()} -> {df['Datetime'].max()}")
    print("-" * 90)
    print(" FILE SPECIFICATION & COMPRESSION PERFORMANCE:")
    print("-" * 90)
    print(f"{'Artifact Description':<32} | {'Format':<10} | {'Rows':<8} | {'File Size':<12} | {'Space Reduction'}")
    print("-" * 90)
    print(f"{'Source Uncompressed CSV':<32} | {'CSV':<10} | {total_rows:<8} | {raw_size_bytes:>10,} B | {'Baseline (0.0%)'}")
    print(f"{'Master Parquet (Typed Schema)':<32} | {'Parquet/zstd':<10} | {total_rows:<8} | {master_parquet_size:>10,} B | {parquet_reduction:.1f}% reduction")
    print(f"{'Master Gzipped CSV':<32} | {'CSV.GZ':<10} | {total_rows:<8} | {master_gz_size:>10,} B | {gz_reduction:.1f}% reduction")
    print(f"{'Core Engine Subset (6 symbols)':<32} | {'Parquet/zstd':<10} | {len(df_core):<8} | {core_parquet_size:>10,} B | {'Engine Optimized'}")
    print(f"{'Robo Portfolio Subset (22 symbols)':<32} | {'Parquet/zstd':<10} | {len(df_robo):<8} | {robo_parquet_size:>10,} B | {'Robo Optimized'}")
    print("-" * 90)

    print(" SYMBOL DISTRIBUTION BREAKDOWN:")
    print("-" * 90)
    print(f" - Core Engine & Benchmarks ({len(CORE_ENGINE_SYMBOLS)} symbols, {len(df_core):,} rows):")
    core_summary = ", ".join([f"{s} ({symbol_counts.get(s, 0)})" for s in CORE_ENGINE_SYMBOLS])
    print(f"   {core_summary}")
    print(f" - Robo Asset Allocation ({len(ROBO_PORTFOLIO_SYMBOLS)} symbols, {len(df_robo):,} rows):")
    robo_summary = ", ".join([f"{s} ({symbol_counts.get(s, 0)})" for s in ROBO_PORTFOLIO_SYMBOLS])
    print(f"   {robo_summary}")
    print("-" * 90)

    print(" GOOGLE DRIVE SYNCHRONIZATION STATUS:")
    for f in generated_files:
        destinations = sync_manifest.get(f, [])
        print(f" Artifact: {f.name} ({f.stat().st_size:,} bytes)")
        for d in destinations:
            print(f"   [SYNCED] {d}")
    print("=" * 90 + "\n")


if __name__ == "__main__":
    main()
