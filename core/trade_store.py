"""
core/trade_store.py
===================
Persistent SQLite trade repository with Write-Ahead Logging (WAL) mode.
Maintains intraday execution history and supports cross-reboot rehydration.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
import sqlite3
import threading
from typing import List, Optional, Union
import uuid
import pytz

from core.paths import TRADES_DB_PATH

_EDT = pytz.timezone("America/New_York")


class SQLiteTradeStore:
    """
    Thread-safe SQLite trade repository storing all executions in STATE_DIR / "trades.db"
    under PRAGMA journal_mode=WAL.
    """

    def __init__(self, db_path: Union[str, Path] = TRADES_DB_PATH):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
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
            conn = self._get_connection()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS trades (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        trade_id TEXT UNIQUE NOT NULL,
                        symbol TEXT NOT NULL,
                        side TEXT NOT NULL,
                        quantity INTEGER NOT NULL,
                        price REAL NOT NULL,
                        cost_basis REAL NOT NULL,
                        realized_pnl REAL DEFAULT 0.0,
                        disallowed_loss REAL DEFAULT 0.0,
                        timestamp TEXT NOT NULL,
                        regime TEXT DEFAULT '',
                        simulated INTEGER NOT NULL DEFAULT 0,
                        env TEXT NOT NULL DEFAULT 'active',
                        created_at TEXT NOT NULL DEFAULT (datetime('now'))
                    );
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_trades_created_at
                    ON trades (created_at);
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_trades_env_created
                    ON trades (env, created_at);
                    """
                )
            finally:
                conn.close()

    def record_trade(
        self,
        symbol: str,
        side: str,
        quantity: int,
        price: Decimal | float,
        cost_basis: Decimal | float,
        timestamp: datetime,
        simulated: bool,
        realized_pnl: Decimal | float = 0.0,
        disallowed_loss: Decimal | float = 0.0,
        regime: str = "",
        trade_id: Optional[str] = None,
        env: str = "active",
    ) -> str:
        tid = trade_id or str(uuid.uuid4())
        ts_str = timestamp.isoformat()
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO trades (
                        trade_id, symbol, side, quantity, price, cost_basis,
                        realized_pnl, disallowed_loss, timestamp, regime, simulated, env
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tid,
                        symbol.upper(),
                        side.upper(),
                        int(quantity),
                        float(price),
                        float(cost_basis),
                        float(realized_pnl),
                        float(disallowed_loss),
                        ts_str,
                        regime,
                        1 if simulated else 0,
                        env,
                    ),
                )
            finally:
                conn.close()
        return tid

    def fetch_trades_today(
        self,
        env: str = "active",
        target_date: Optional[date] = None,
    ) -> List[dict]:
        """
        Retrieves today's hydrated executions matching the current EDT calendar day.
        """
        day = target_date or datetime.now(_EDT).date()
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.execute(
                    """
                    SELECT id, trade_id, symbol, side, quantity, price, cost_basis,
                           realized_pnl, disallowed_loss, timestamp, regime, simulated, env, created_at
                    FROM trades
                    WHERE env = ?
                    ORDER BY id ASC
                    """,
                    (env,),
                )
                rows = cur.fetchall()
            finally:
                conn.close()

        results = []
        for row in rows:
            ts_str = row["timestamp"]
            try:
                dt = datetime.fromisoformat(ts_str)
                if dt.tzinfo is None:
                    dt = _EDT.localize(dt)
                row_date = dt.astimezone(_EDT).date()
            except Exception:
                row_date = None

            if row_date == day or (
                row["created_at"] and row["created_at"].startswith(day.isoformat())
            ):
                results.append(dict(row))
        return results

    def get_realized_pnl_today(
        self,
        env: str = "active",
        target_date: Optional[date] = None,
    ) -> Decimal:
        trades = self.fetch_trades_today(env, target_date)
        total = Decimal("0.00")
        for t in trades:
            if t["side"] == "SELL":
                total += Decimal(str(t["realized_pnl"]))
        return total
