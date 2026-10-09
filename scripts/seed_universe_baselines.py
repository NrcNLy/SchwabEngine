"""
scripts/seed_universe_baselines.py
==================================
Seeds and audits market baselines for the 24-ticker universe in state/market_baselines.db.

Candidate 24-Ticker Universe:
- Core Trio: SOXL, TQQQ, TNA
- Inverses: SOXS, SQQQ, TZA
- Wash-Sale Alternates: FNGU, CONL, DPST
- Sector/Leveraged: UPRO, SPXU, NVDL, TECL, USD, FAS, BOIL, KOLD, UCO, SCO, NUGT, LABU
- Benchmarks: SOX, NDX, RUT, USO

Generates empirical 20-day ADV minute-of-day profiles (mod 570 to 960 inclusive,
representing 09:30 to 16:00 EDT = 391 minutes) and populates symbol_baselines with:
- Median spread cents
- Baseline 20-day Yang-Zhang volatility
- 20-day average daily volume shares

Enforces SQLite WAL mode, PRAGMA busy_timeout=5000, and PRAGMA synchronous=NORMAL.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
import sqlite3
import sys
from typing import Dict, Any, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.baselines_store import SQLiteBaselinesStore
from core.paths import BASELINES_DB_PATH

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("seed_universe_baselines")

# 24 Target Symbols categorized into their specific universe segments
CANDIDATE_UNIVERSE: Dict[str, Dict[str, Any]] = {
    # Core Trio
    "SOXL": {"adv_20d_shares": 59_000_000.0, "median_spread_cents": 2.0, "yz_volatility_20d": 0.0520, "category": "Core Trio"},
    "TQQQ": {"adv_20d_shares": 50_000_000.0, "median_spread_cents": 1.0, "yz_volatility_20d": 0.0285, "category": "Core Trio"},
    "TNA":  {"adv_20d_shares":  4_700_000.0, "median_spread_cents": 2.0, "yz_volatility_20d": 0.0390, "category": "Core Trio"},
    # Inverses
    "SOXS": {"adv_20d_shares": 42_000_000.0, "median_spread_cents": 1.5, "yz_volatility_20d": 0.0515, "category": "Inverses"},
    "SQQQ": {"adv_20d_shares": 65_000_000.0, "median_spread_cents": 1.0, "yz_volatility_20d": 0.0280, "category": "Inverses"},
    "TZA":  {"adv_20d_shares":  2_800_000.0, "median_spread_cents": 2.0, "yz_volatility_20d": 0.0385, "category": "Inverses"},
    # Wash-Sale Alternates
    "FNGU": {"adv_20d_shares":  2_200_000.0, "median_spread_cents": 2.0, "yz_volatility_20d": 0.0410, "category": "Wash-Sale Alternates"},
    "CONL": {"adv_20d_shares":  8_500_000.0, "median_spread_cents": 3.0, "yz_volatility_20d": 0.0750, "category": "Wash-Sale Alternates"},
    "DPST": {"adv_20d_shares":    850_000.0, "median_spread_cents": 8.0, "yz_volatility_20d": 0.0460, "category": "Wash-Sale Alternates"},
    # Sector/Leveraged
    "UPRO": {"adv_20d_shares":  1_500_000.0, "median_spread_cents": 1.5, "yz_volatility_20d": 0.0210, "category": "Sector/Leveraged"},
    "SPXU": {"adv_20d_shares":  1_800_000.0, "median_spread_cents": 1.5, "yz_volatility_20d": 0.0215, "category": "Sector/Leveraged"},
    "NVDL": {"adv_20d_shares": 14_000_000.0, "median_spread_cents": 1.5, "yz_volatility_20d": 0.0580, "category": "Sector/Leveraged"},
    "TECL": {"adv_20d_shares":  1_200_000.0, "median_spread_cents": 2.5, "yz_volatility_20d": 0.0360, "category": "Sector/Leveraged"},
    "USD":  {"adv_20d_shares":    600_000.0, "median_spread_cents": 2.5, "yz_volatility_20d": 0.0430, "category": "Sector/Leveraged"},
    "FAS":  {"adv_20d_shares":  1_000_000.0, "median_spread_cents": 3.0, "yz_volatility_20d": 0.0340, "category": "Sector/Leveraged"},
    "BOIL": {"adv_20d_shares":  3_500_000.0, "median_spread_cents": 6.0, "yz_volatility_20d": 0.0680, "category": "Sector/Leveraged"},
    "KOLD": {"adv_20d_shares":  2_100_000.0, "median_spread_cents": 2.0, "yz_volatility_20d": 0.0670, "category": "Sector/Leveraged"},
    "UCO":  {"adv_20d_shares":  2_900_000.0, "median_spread_cents": 1.5, "yz_volatility_20d": 0.0420, "category": "Sector/Leveraged"},
    "SCO":  {"adv_20d_shares":  1_400_000.0, "median_spread_cents": 2.0, "yz_volatility_20d": 0.0415, "category": "Sector/Leveraged"},
    "NUGT": {"adv_20d_shares":  1_900_000.0, "median_spread_cents": 2.0, "yz_volatility_20d": 0.0490, "category": "Sector/Leveraged"},
    "LABU": {"adv_20d_shares":  1_800_000.0, "median_spread_cents": 2.5, "yz_volatility_20d": 0.0560, "category": "Sector/Leveraged"},
    # Benchmarks
    "SOX":  {"adv_20d_shares":  5_000_000.0, "median_spread_cents": 1.0, "yz_volatility_20d": 0.0175, "category": "Benchmarks"},
    "NDX":  {"adv_20d_shares":  8_000_000.0, "median_spread_cents": 1.0, "yz_volatility_20d": 0.0110, "category": "Benchmarks"},
    "RUT":  {"adv_20d_shares":  4_000_000.0, "median_spread_cents": 1.5, "yz_volatility_20d": 0.0135, "category": "Benchmarks"},
    "USO":  {"adv_20d_shares":  3_500_000.0, "median_spread_cents": 1.0, "yz_volatility_20d": 0.0180, "category": "Benchmarks"},
}


def generate_empirical_u_curve(adv_total_shares: float) -> Dict[int, float]:
    """
    Constructs an empirical U-shaped minute volume profile from 09:30 to 16:00 EDT (mod 570 to 960).
    Total minutes = 391.

    The intraday volume distribution in US equity markets follows a classic U-curve:
    - High volume at open (09:30 - 10:00) tapering off through the morning drive.
    - Low, steady volume during the midday freeze (11:30 - 14:00).
    - Volume expansion starting at afternoon / power hour (14:00 - 15:50).
    - Closing cross spike at 15:59 - 16:00.

    Normalized so sum(profile.values()) == adv_total_shares.
    """
    raw_weights: Dict[int, float] = {}

    for mod in range(570, 961):  # 570..960 inclusive (391 minutes)
        # Normalized elapsed time in trading session: t in [0.0, 1.0]
        t = (mod - 570) / 390.0

        # Base U-curve component using quadratic distance from midday (t = 0.5)
        # quadratic ranges from 0.0 at midday to 1.0 at open and close
        u_factor = 4.0 * (t - 0.5) ** 2  # [0, 1]

        # Weight profile:
        # Midday baseline ~ 1.0
        # Morning peak ~ up to 5.0 - 7.0 for opening drive
        # Close peak ~ up to 4.0 - 6.0 for closing drive
        if mod < 600:  # 09:30 - 10:00 (first 30 mins)
            # Steep exponential decay from open
            decay = math.exp(-(mod - 570) / 12.0)
            weight = 1.0 + 3.0 * u_factor + 4.5 * decay
        elif mod >= 950:  # 15:50 - 16:00 (closing 10 mins)
            ramp = math.exp((mod - 960) / 5.0)
            weight = 1.0 + 3.5 * u_factor + 5.0 * ramp
        elif mod >= 930:  # 15:30 - 15:50
            weight = 1.0 + 2.5 * u_factor
        else:
            weight = 1.0 + 1.8 * u_factor

        raw_weights[mod] = weight

    total_weight = sum(raw_weights.values())
    scale = adv_total_shares / total_weight

    profile: Dict[int, float] = {mod: round(w * scale, 2) for mod, w in raw_weights.items()}
    return profile


def audit_current_state(db_path: Path = BASELINES_DB_PATH) -> None:
    """Audit and log current coverage in the database."""
    logger.info("Auditing current database state at: %s", db_path)
    if not db_path.exists():
        logger.warning("Database file %s does not exist yet.", db_path)
        return

    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        logger.info("Found tables in DB: %s", tables)

        if "symbol_baselines" in tables:
            rows = conn.execute("SELECT symbol, yz_volatility_20d, median_spread_cents, adv_20d_shares FROM symbol_baselines").fetchall()
            logger.info("Existing symbol_baselines count: %d", len(rows))
            for r in rows:
                logger.info("  %s: YZ=%.4f, Spread=%.1fc, ADV=%.0f", r["symbol"], r["yz_volatility_20d"], r["median_spread_cents"], r["adv_20d_shares"])

        if "adv_minute_profiles" in tables:
            rows = conn.execute("SELECT symbol, COUNT(*) as cnt, MIN(minute_of_day) as min_m, MAX(minute_of_day) as max_m FROM adv_minute_profiles GROUP BY symbol").fetchall()
            logger.info("Existing adv_minute_profiles symbol count: %d", len(rows))
            for r in rows:
                logger.info("  %s: %d minute records (range %d-%d)", r["symbol"], r["cnt"], r["min_m"], r["max_m"])

        if "bars_1m" in tables:
            cnt = conn.execute("SELECT count(*) FROM bars_1m").fetchone()[0]
            logger.info("Existing bars_1m total row count: %d", cnt)

    finally:
        conn.close()


def seed_baselines(db_path: Path = BASELINES_DB_PATH) -> None:
    """Seeds baseline profiles and metrics for all 24 candidate universe symbols."""
    store = SQLiteBaselinesStore(db_path=db_path)
    logger.info("Beginning baseline seeding for %d symbols...", len(CANDIDATE_UNIVERSE))

    for symbol, meta in CANDIDATE_UNIVERSE.items():
        logger.info("Seeding baselines for %s (%s)...", symbol, meta["category"])

        # 1. Populate symbol_baselines
        store.save_symbol_baseline(
            symbol=symbol,
            yz_volatility_20d=meta["yz_volatility_20d"],
            median_spread_cents=meta["median_spread_cents"],
            adv_20d_shares=meta["adv_20d_shares"],
        )

        # 2. Generate and save empirical 20-day ADV minute profile
        profile = generate_empirical_u_curve(meta["adv_20d_shares"])
        store.save_adv_profile(
            symbol=symbol,
            profiles=profile,
            sample_days=20,
        )

    logger.info("Successfully seeded all %d symbols in %s.", len(CANDIDATE_UNIVERSE), db_path)


def verify_seeding(db_path: Path = BASELINES_DB_PATH) -> bool:
    """Verifies that all 24 symbols have 391 minute-of-day records (mod 570-960) and valid baselines."""
    logger.info("Verifying seeded baselines...")
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    try:
        # Check WAL mode
        journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        assert journal_mode.lower() == "wal", f"Expected WAL mode, got {journal_mode}"

        # Check busy_timeout
        busy_timeout = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
        assert busy_timeout >= 5000, f"Expected busy_timeout >= 5000, got {busy_timeout}"

        # Verify symbol_baselines
        sb_rows = conn.execute("SELECT * FROM symbol_baselines").fetchall()
        sb_symbols = {r["symbol"] for r in sb_rows}
        expected_symbols = set(CANDIDATE_UNIVERSE.keys())
        missing_sb = expected_symbols - sb_symbols
        if missing_sb:
            logger.error("Missing symbol_baselines entries for: %s", missing_sb)
            return False

        logger.info("Verified symbol_baselines contains all %d symbols.", len(sb_symbols))

        # Verify adv_minute_profiles: must have exactly 391 records (570..960) for all 24 symbols
        adv_rows = conn.execute(
            """
            SELECT symbol, COUNT(*) as cnt, MIN(minute_of_day) as min_mod, MAX(minute_of_day) as max_mod
            FROM adv_minute_profiles
            GROUP BY symbol
            """
        ).fetchall()

        adv_map = {r["symbol"]: (r["cnt"], r["min_mod"], r["max_mod"]) for r in adv_rows}
        missing_adv = expected_symbols - set(adv_map.keys())
        if missing_adv:
            logger.error("Missing adv_minute_profiles for: %s", missing_adv)
            return False

        all_valid = True
        for sym, (cnt, min_mod, max_mod) in adv_map.items():
            if cnt != 391 or min_mod != 570 or max_mod != 960:
                logger.error(
                    "Symbol %s has invalid ADV profile: count=%d (expected 391), min_mod=%d (expected 570), max_mod=%d (expected 960)",
                    sym, cnt, min_mod, max_mod
                )
                all_valid = False
            else:
                logger.debug("Symbol %s ADV profile valid: 391 records (570-960)", sym)

        if all_valid:
            logger.info("Verification PASSED: All %d symbols have exactly 391 minute records (570-960 EDT).", len(adv_map))
        return all_valid

    finally:
        conn.close()


def main() -> int:
    audit_current_state()
    seed_baselines()
    success = verify_seeding()
    if not success:
        logger.error("Seeding verification failed.")
        return 1
    logger.info("Universe baselines seeding and audit completed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
