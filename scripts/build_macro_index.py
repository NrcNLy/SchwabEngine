#!/usr/bin/env python3
"""
scripts/build_macro_index.py
============================
Automated historical macro data collector and Semiconductor Environment Score (SES)
generator for the Schwab Day-Trading Engine.

Ingests historical multi-timeframe data for semiconductor equity benchmarks, high-beta
derivatives, rates, tech volatility, and FX, computing normalized regime indicators
and the composite Semiconductor Environment Score (SES).

Ingested Instruments:
  - SOXX: iShares Semiconductor ETF benchmark
  - SOXL: Direxion Daily Semiconductor Bull 3x
  - QQQ:  Invesco QQQ Trust (broad tech benchmark)
  - ^TNX: CBOE 10-Year US Treasury Yield index
  - ^VXN: CBOE Nasdaq Volatility Index
  - UUP:  Invesco DB US Dollar Index Bullish Fund

Indicators:
  - Relative Strength Ratio: SOXX / QQQ vs. its rolling 20-period moving average.
  - Treasury Yield Delta: 5-period momentum change in ^TNX.
  - Volatility Regime Rank: Rolling 60-period percentile rank of ^VXN.
  - Trend Z-Score: Standard deviation distance of SOXX from its 50-period SMA.

Composite Metric (SES):
  - Bounded strictly in [-1.0, +1.0].
  - Categorization:
      * FAVORABLE: SES >= 0.35
      * DEFENSIVE: SES <= -0.35
      * CHOP:     -0.35 < SES < 0.35

Exports:
  - data/exports/macro_index_history.csv
  - data/exports/latest_ses_snapshot.json
"""

import argparse
import json
import logging
from pathlib import Path
from typing import Dict, Tuple

import numpy as np
import pandas as pd
from scipy import stats
import yfinance as yf

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("build_macro_index")

TICKERS = ["SOXX", "SOXL", "QQQ", "^TNX", "^VXN", "UUP"]


def get_default_export_dir() -> Path:
    """Resolves project root and returns the default exports directory."""
    project_root = Path(__file__).resolve().parent.parent
    export_dir = project_root / "data" / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    return export_dir


def fetch_raw_data() -> Tuple[Dict[str, pd.DataFrame], Dict[str, pd.DataFrame]]:
    """
    Downloads 2 years of daily data and 730 days of 1-hour intraday data
    for all target macro tickers via yfinance.
    """
    daily_data: Dict[str, pd.DataFrame] = {}
    hourly_data: Dict[str, pd.DataFrame] = {}

    for ticker in TICKERS:
        logger.info("Fetching yfinance data for %s (1d: 2y, 1h: 730d)...", ticker)
        # Daily: 2 years
        d_df = yf.download(ticker, period="2y", interval="1d", progress=False, auto_adjust=False)
        if isinstance(d_df.columns, pd.MultiIndex):
            # Flatten multi-index if returned
            d_df = d_df.xs(ticker, axis=1, level=1) if ticker in d_df.columns.levels[1] else d_df.droplevel(1, axis=1)
        daily_data[ticker] = d_df

        # Hourly: 730 days
        h_df = yf.download(ticker, period="730d", interval="1h", progress=False, auto_adjust=False)
        if isinstance(h_df.columns, pd.MultiIndex):
            h_df = h_df.xs(ticker, axis=1, level=1) if ticker in h_df.columns.levels[1] else h_df.droplevel(1, axis=1)
        hourly_data[ticker] = h_df

    return daily_data, hourly_data


def align_hourly_series(hourly_data: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    """
    Aligns multi-asset hourly series on Eastern Time equity trading timestamps.
    Uses backward-looking merge to eliminate lookahead bias across disparate exchange hours.
    """
    cleaned_series: Dict[str, pd.Series] = {}

    for ticker, df in hourly_data.items():
        if df.empty or "Close" not in df.columns:
            raise ValueError(f"Missing Close data for ticker {ticker}")
        close = df["Close"].copy()
        if close.index.tz is None:
            close.index = close.index.tz_localize("UTC")
        close.index = close.index.tz_convert("America/New_York")
        # De-duplicate timestamps
        close = close[~close.index.duplicated(keep="first")]
        cleaned_series[ticker] = close.dropna()

    # Base index aligns with benchmark equity trading hours (SOXX)
    base_idx = cleaned_series["SOXX"].index.sort_values()
    df_aligned = pd.DataFrame(index=base_idx)

    for ticker in TICKERS:
        s = cleaned_series[ticker]
        s_df = pd.DataFrame({ticker: s.values}, index=s.index).sort_index()
        # Merge backward in time to avoid lookahead bias
        m = pd.merge_asof(df_aligned, s_df, left_index=True, right_index=True, direction="backward")
        df_aligned[ticker] = m[ticker]

    # Fill any gaps (e.g. prior to index trading hours)
    df_aligned = df_aligned.bfill().ffill()
    return df_aligned


def compute_features_and_ses(df: pd.DataFrame) -> pd.DataFrame:
    """
    Computes mathematical features and Semiconductor Environment Score (SES).

    Features:
      - Relative Strength Ratio: SOXX / QQQ vs. 20-period moving average
      - Treasury Yield Delta: 5-period momentum change in ^TNX
      - Volatility Regime Rank: Rolling 60-period percentile rank of ^VXN
      - Trend Z-Score: Distance of SOXX from its 50-period SMA in units of standard deviation

    Composite SES:
      - Weighted combination bounded strictly in [-1.0, 1.0]
      - Regime: FAVORABLE (>= 0.35), DEFENSIVE (<= -0.35), CHOP (otherwise)
    """
    out = df.copy()

    # 1. Relative Strength Ratio: SOXX / QQQ
    out["rs_ratio"] = out["SOXX"] / out["QQQ"]
    out["rs_sma20"] = out["rs_ratio"].rolling(window=20, min_periods=5).mean()
    out["rs_spread"] = (out["rs_ratio"] - out["rs_sma20"]) / out["rs_sma20"]

    # 2. Treasury Yield Delta: 5-period momentum change in ^TNX
    out["tnx_delta_5"] = out["^TNX"] - out["^TNX"].shift(5)

    # 3. Volatility Regime Rank: Rolling 60-period percentile rank of ^VXN
    def _pct_rank_last(window: np.ndarray) -> float:
        valid = window[~np.isnan(window)]
        if len(valid) < 5:
            return 0.5
        return float(stats.percentileofscore(valid, valid[-1], kind="mean") / 100.0)

    out["vxn_rank_60"] = out["^VXN"].rolling(window=60, min_periods=10).apply(
        _pct_rank_last, raw=True
    )

    # 4. Trend Z-Score: Distance of SOXX from its 50-period SMA
    out["soxx_sma50"] = out["SOXX"].rolling(window=50, min_periods=10).mean()
    out["soxx_std50"] = out["SOXX"].rolling(window=50, min_periods=10).std().replace(0, np.nan)
    out["soxx_trend_zscore"] = (out["SOXX"] - out["soxx_sma50"]) / out["soxx_std50"]

    # Fill rolling warmup gaps cleanly
    out["rs_spread"] = out["rs_spread"].bfill().fillna(0.0)
    out["tnx_delta_5"] = out["tnx_delta_5"].bfill().fillna(0.0)
    out["vxn_rank_60"] = out["vxn_rank_60"].bfill().fillna(0.5)
    out["soxx_trend_zscore"] = out["soxx_trend_zscore"].bfill().fillna(0.0)

    # Sub-component scores mapped to [-1.0, 1.0]
    out["score_trend"] = np.tanh(out["soxx_trend_zscore"] / 2.0)
    out["score_rs"] = np.tanh(out["rs_spread"] / 0.02)
    # High VXN rank (elevated volatility) -> negative score; low VXN rank (calm) -> positive score
    out["score_vxn"] = 1.0 - 2.0 * out["vxn_rank_60"]
    # Spiking rates (positive tnx delta) -> negative score; easing rates -> positive score
    out["score_tnx"] = -np.tanh(out["tnx_delta_5"] / 0.15)

    # Composite SES: 35% Trend, 30% Relative Strength, 20% Volatility, 15% Rates
    composite = (
        0.35 * out["score_trend"] +
        0.30 * out["score_rs"] +
        0.20 * out["score_vxn"] +
        0.15 * out["score_tnx"]
    )
    out["ses"] = composite.clip(-1.0, 1.0).round(4)

    # Regime Categorization
    conditions = [
        out["ses"] >= 0.35,
        out["ses"] <= -0.35,
    ]
    choices = ["FAVORABLE", "DEFENSIVE"]
    out["regime"] = np.select(conditions, choices, default="CHOP")

    return out


def build_macro_dataset(export_dir: Path) -> Tuple[pd.DataFrame, Dict]:
    """Orchestrates data fetching, processing, and export."""
    logger.info("Initiating historical macro data collection across 6 instruments...")
    daily_data, hourly_data = fetch_raw_data()

    logger.info("Aligning hourly time series across NYSE, CBOE, and index calendars...")
    df_aligned = align_hourly_series(hourly_data)

    logger.info("Calculating indicator features and Semiconductor Environment Score...")
    df_processed = compute_features_and_ses(df_aligned)

    # Prepare export dataframe with clean naming
    rename_cols = {
        "SOXX": "soxx_close",
        "SOXL": "soxl_close",
        "QQQ": "qqq_close",
        "^TNX": "tnx_close",
        "^VXN": "vxn_close",
        "UUP": "uup_close",
    }
    export_df = df_processed.rename(columns=rename_cols).copy()
    export_df.index.name = "timestamp_edt"

    # Export CSV
    csv_path = export_dir / "macro_index_history.csv"
    export_df.to_csv(csv_path, index=True)
    logger.info("Saved merged time series to: %s", csv_path)

    # Latest Snapshot
    latest_row = export_df.iloc[-1]
    latest_ts = str(export_df.index[-1])
    latest_ses = float(latest_row["ses"])
    latest_regime = str(latest_row["regime"])

    snapshot = {
        "timestamp": latest_ts,
        "ses": latest_ses,
        "regime": latest_regime,
        "raw_prices": {
            "soxx_close": float(round(latest_row["soxx_close"], 2)),
            "soxl_close": float(round(latest_row["soxl_close"], 2)),
            "qqq_close": float(round(latest_row["qqq_close"], 2)),
            "tnx_close": float(round(latest_row["tnx_close"], 3)),
            "vxn_close": float(round(latest_row["vxn_close"], 2)),
            "uup_close": float(round(latest_row["uup_close"], 2)),
        },
        "indicators": {
            "rs_ratio": float(round(latest_row["rs_ratio"], 4)),
            "rs_sma20": float(round(latest_row["rs_sma20"], 4)),
            "rs_spread": float(round(latest_row["rs_spread"], 4)),
            "tnx_delta_5": float(round(latest_row["tnx_delta_5"], 4)),
            "vxn_rank_60": float(round(latest_row["vxn_rank_60"], 4)),
            "soxx_trend_zscore": float(round(latest_row["soxx_trend_zscore"], 4)),
        },
        "component_scores": {
            "score_trend": float(round(latest_row["score_trend"], 4)),
            "score_rs": float(round(latest_row["score_rs"], 4)),
            "score_vxn": float(round(latest_row["score_vxn"], 4)),
            "score_tnx": float(round(latest_row["score_tnx"], 4)),
        },
        "regime_thresholds": {
            "favorable_threshold": 0.35,
            "defensive_threshold": -0.35,
        },
    }

    json_path = export_dir / "latest_ses_snapshot.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)
    logger.info("Saved latest SES snapshot to: %s", json_path)

    return export_df, snapshot


def main():
    parser = argparse.ArgumentParser(description="Build Macro Index and SES for Schwab Engine")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory to save CSV and JSON exports",
    )
    args = parser.parse_args()

    export_dir = args.output_dir or get_default_export_dir()
    export_dir.mkdir(parents=True, exist_ok=True)

    df, snapshot = build_macro_dataset(export_dir)

    total_rows = len(df)
    start_date = str(df.index[0])
    end_date = str(df.index[-1])
    latest_ses = snapshot["ses"]
    latest_regime = snapshot["regime"]
    csv_path = (export_dir / "macro_index_history.csv").resolve()
    json_path = (export_dir / "latest_ses_snapshot.json").resolve()

    # Terminal confirmation output
    print("\n=======================================================")
    print("MACRO INDEX & SEMICONDUCTOR ENVIRONMENT SCORE (SES) GENERATION COMPLETE")
    print("=======================================================")
    print(f"Total Rows:     {total_rows}")
    print(f"Date Range:     {start_date} -> {end_date}")
    print(f"Latest SES:     {latest_ses:+.4f} [{latest_regime}]")
    print(f"CSV Export:     {csv_path}")
    print(f"JSON Snapshot:  {json_path}")
    print("=======================================================\n")


if __name__ == "__main__":
    main()
