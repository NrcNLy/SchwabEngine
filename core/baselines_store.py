"""
core/baselines_store.py
=======================
Persistent SQLite market baseline repository with Write-Ahead Logging (WAL) mode.
Maintains historical 1-minute bars, rolling 20-day ADV minute curves, and Yang-Zhang
volatility metrics for zero-latency, zero-API morning initialization.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
import sqlite3
import threading
from typing import Any, Dict, List, Optional, Sequence, Union
import pytz

from core.paths import BASELINES_DB_PATH

logger = logging.getLogger(__name__)
_EDT = pytz.timezone("America/New_York")


class SQLiteBaselinesStore:
    """
    Thread-safe SQLite baseline repository storing bars and ADV profiles in
    STATE_DIR / "market_baselines.db" under PRAGMA journal_mode=WAL.
    """

    def __init__(self, db_path: Union[str, Path] = BASELINES_DB_PATH):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _get_connection(self, read_only: bool = False) -> sqlite3.Connection:
        if read_only and self.db_path != ":memory:":
            uri_path = Path(self.db_path).resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(
                uri_path,
                uri=True,
                timeout=5.0,
                check_same_thread=False,
                isolation_level=None,
            )
        else:
            conn = sqlite3.connect(
                self.db_path,
                timeout=5.0,
                check_same_thread=False,
                isolation_level=None,  # autocommit
            )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._get_connection(read_only=False)
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS bars_1m (
                        symbol TEXT NOT NULL,
                        timestamp INTEGER NOT NULL,
                        datetime_edt TEXT NOT NULL,
                        open REAL NOT NULL,
                        high REAL NOT NULL,
                        low REAL NOT NULL,
                        close REAL NOT NULL,
                        volume INTEGER NOT NULL,
                        PRIMARY KEY (symbol, timestamp)
                    ) WITHOUT ROWID;
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_bars_sym_time
                    ON bars_1m (symbol, timestamp DESC);
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS adv_minute_profiles (
                        symbol TEXT NOT NULL,
                        minute_of_day INTEGER NOT NULL,
                        adv_volume REAL NOT NULL,
                        sample_days INTEGER NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (symbol, minute_of_day)
                    ) WITHOUT ROWID;
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS symbol_baselines (
                        symbol TEXT PRIMARY KEY,
                        yz_volatility_20d REAL NOT NULL,
                        median_spread_cents REAL NOT NULL,
                        adv_20d_shares REAL NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    """
                )
            finally:
                conn.close()

    def upsert_bars(self, symbol: str, bars: Sequence[Dict[str, Any]]) -> int:
        """
        Upsert a batch of 1-minute bars for a symbol.
        Each bar dict must have: timestamp (ms or int), open, high, low, close, volume.
        """
        if not bars:
            return 0
        sym = symbol.upper()
        records = []
        for b in bars:
            ts = int(b["timestamp"] if "timestamp" in b else b.get("datetime", 0))
            if "datetime_edt" in b:
                dt_str = str(b["datetime_edt"])
            else:
                dt_val = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc).astimezone(_EDT)
                dt_str = dt_val.isoformat()
            records.append((
                sym,
                ts,
                dt_str,
                float(b["open"]),
                float(b["high"]),
                float(b["low"]),
                float(b["close"]),
                int(b.get("volume", 0)),
            ))

        with self._lock:
            conn = self._get_connection(read_only=False)
            try:
                conn.execute("BEGIN IMMEDIATE;")
                conn.executemany(
                    """
                    INSERT INTO bars_1m (symbol, timestamp, datetime_edt, open, high, low, close, volume)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol, timestamp) DO UPDATE SET
                        datetime_edt=excluded.datetime_edt,
                        open=excluded.open,
                        high=excluded.high,
                        low=excluded.low,
                        close=excluded.close,
                        volume=excluded.volume;
                    """,
                    records,
                )
                conn.execute("COMMIT;")
                return len(records)
            except Exception:
                conn.execute("ROLLBACK;")
                raise
            finally:
                conn.close()

    def get_latest_bar_timestamp(self, symbol: str) -> Optional[int]:
        """Returns the highest timestamp (epoch ms) stored for the symbol, or None."""
        sym = symbol.upper()
        conn = self._get_connection(read_only=True)
        try:
            cursor = conn.execute(
                "SELECT MAX(timestamp) AS max_ts FROM bars_1m WHERE symbol = ?;",
                (sym,),
            )
            row = cursor.fetchone()
            if row and row["max_ts"] is not None:
                return int(row["max_ts"])
            return None
        finally:
            conn.close()

    def get_bars(
        self,
        symbol: str,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieve sorted bars for symbol."""
        sym = symbol.upper()
        query = "SELECT * FROM bars_1m WHERE symbol = ?"
        params: List[Any] = [sym]

        if start_ts is not None:
            query += " AND timestamp >= ?"
            params.append(start_ts)
        if end_ts is not None:
            query += " AND timestamp <= ?"
            params.append(end_ts)

        query += " ORDER BY timestamp ASC"
        if limit is not None:
            query += f" LIMIT {int(limit)}"

        conn = self._get_connection(read_only=True)
        try:
            cursor = conn.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def save_adv_profile(self, symbol: str, profiles: Dict[int, float], sample_days: int) -> None:
        """Saves minute-of-day (0-1439) average daily volume curves."""
        sym = symbol.upper()
        now_iso = datetime.now(timezone.utc).isoformat()
        records = [
            (sym, int(minute), float(vol), int(sample_days), now_iso)
            for minute, vol in profiles.items()
        ]
        with self._lock:
            conn = self._get_connection(read_only=False)
            try:
                conn.execute("BEGIN IMMEDIATE;")
                conn.executemany(
                    """
                    INSERT INTO adv_minute_profiles (symbol, minute_of_day, adv_volume, sample_days, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(symbol, minute_of_day) DO UPDATE SET
                        adv_volume=excluded.adv_volume,
                        sample_days=excluded.sample_days,
                        updated_at=excluded.updated_at;
                    """,
                    records,
                )
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise
            finally:
                conn.close()

    def get_adv_profile(self, symbol: str) -> Dict[int, float]:
        """Returns map of minute_of_day -> average volume."""
        sym = symbol.upper()
        conn = self._get_connection(read_only=True)
        try:
            cursor = conn.execute(
                "SELECT minute_of_day, adv_volume FROM adv_minute_profiles WHERE symbol = ?;",
                (sym,),
            )
            return {int(row["minute_of_day"]): float(row["adv_volume"]) for row in cursor.fetchall()}
        finally:
            conn.close()

    def save_symbol_baseline(
        self,
        symbol: str,
        yz_volatility_20d: float,
        median_spread_cents: float,
        adv_20d_shares: float,
    ) -> None:
        sym = symbol.upper()
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock:
            conn = self._get_connection(read_only=False)
            try:
                conn.execute(
                    """
                    INSERT INTO symbol_baselines (symbol, yz_volatility_20d, median_spread_cents, adv_20d_shares, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(symbol) DO UPDATE SET
                        yz_volatility_20d=excluded.yz_volatility_20d,
                        median_spread_cents=excluded.median_spread_cents,
                        adv_20d_shares=excluded.adv_20d_shares,
                        updated_at=excluded.updated_at;
                    """,
                    (sym, float(yz_volatility_20d), float(median_spread_cents), float(adv_20d_shares), now_iso),
                )
            finally:
                conn.close()

    def get_symbol_baseline(self, symbol: str) -> Optional[Dict[str, Any]]:
        sym = symbol.upper()
        conn = self._get_connection(read_only=True)
        try:
            cursor = conn.execute(
                "SELECT * FROM symbol_baselines WHERE symbol = ?;",
                (sym,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
