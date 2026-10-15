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
                        tranche_1_filled INTEGER DEFAULT 0,
                        tranche_2_filled INTEGER DEFAULT 0,
                        breakeven_latched INTEGER DEFAULT 0,
                        posterior_alpha REAL DEFAULT 10.0,
                        posterior_beta REAL DEFAULT 10.0,
                        kelly_fraction REAL DEFAULT 0.0,
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

                # Migration for existing databases
                cur = conn.execute("PRAGMA table_info(trades);")
                existing_cols = {row[1] for row in cur.fetchall()}
                if "tranche_1_filled" not in existing_cols:
                    conn.execute("ALTER TABLE trades ADD COLUMN tranche_1_filled INTEGER DEFAULT 0;")
                if "tranche_2_filled" not in existing_cols:
                    conn.execute("ALTER TABLE trades ADD COLUMN tranche_2_filled INTEGER DEFAULT 0;")
                if "breakeven_latched" not in existing_cols:
                    conn.execute("ALTER TABLE trades ADD COLUMN breakeven_latched INTEGER DEFAULT 0;")
                if "posterior_alpha" not in existing_cols:
                    conn.execute("ALTER TABLE trades ADD COLUMN posterior_alpha REAL DEFAULT 10.0;")
                if "posterior_beta" not in existing_cols:
                    conn.execute("ALTER TABLE trades ADD COLUMN posterior_beta REAL DEFAULT 10.0;")
                if "kelly_fraction" not in existing_cols:
                    conn.execute("ALTER TABLE trades ADD COLUMN kelly_fraction REAL DEFAULT 0.0;")

                # Position brackets table for multi-tranche tracking
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS position_brackets (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        symbol TEXT NOT NULL,
                        total_shares INTEGER NOT NULL,
                        entry_price REAL NOT NULL,
                        s0_reference REAL NOT NULL,
                        current_stop_price REAL NOT NULL,
                        remaining_shares INTEGER NOT NULL,
                        tranche_1_filled INTEGER DEFAULT 0,
                        tranche_2_filled INTEGER DEFAULT 0,
                        breakeven_latched INTEGER DEFAULT 0,
                        is_risk_free INTEGER DEFAULT 0,
                        realized_pnl REAL DEFAULT 0.0,
                        status TEXT NOT NULL DEFAULT 'ACTIVE',
                        created_at TEXT NOT NULL DEFAULT (datetime('now')),
                        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                    );
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
        tranche_1_filled: bool = False,
        tranche_2_filled: bool = False,
        breakeven_latched: bool = False,
        posterior_alpha: Optional[float] = None,
        posterior_beta: Optional[float] = None,
        kelly_fraction: Optional[float] = None,
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
                        realized_pnl, disallowed_loss, timestamp, regime, simulated, env,
                        tranche_1_filled, tranche_2_filled, breakeven_latched,
                        posterior_alpha, posterior_beta, kelly_fraction
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        1 if tranche_1_filled else 0,
                        1 if tranche_2_filled else 0,
                        1 if breakeven_latched else 0,
                        float(posterior_alpha) if posterior_alpha is not None else 10.0,
                        float(posterior_beta) if posterior_beta is not None else 10.0,
                        float(kelly_fraction) if kelly_fraction is not None else 0.0,
                    ),
                )
            finally:
                conn.close()
        return tid

    def record_tranche_progression(
        self,
        symbol: str,
        tranche_1_filled: bool,
        tranche_2_filled: bool,
        breakeven_latched: bool,
        trade_id: Optional[str] = None,
        env: str = "active",
    ) -> None:
        """Updates tranche progression flags for an active position in SQLite trades.db."""
        t1 = 1 if tranche_1_filled else 0
        t2 = 1 if tranche_2_filled else 0
        be = 1 if breakeven_latched else 0
        with self._lock:
            conn = self._get_connection()
            try:
                if trade_id:
                    conn.execute(
                        """
                        UPDATE trades
                        SET tranche_1_filled = ?, tranche_2_filled = ?, breakeven_latched = ?
                        WHERE trade_id = ?
                        """,
                        (t1, t2, be, trade_id),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE trades
                        SET tranche_1_filled = ?, tranche_2_filled = ?, breakeven_latched = ?
                        WHERE id = (
                            SELECT id FROM trades
                            WHERE symbol = ? AND env = ?
                            ORDER BY id DESC LIMIT 1
                        )
                        """,
                        (t1, t2, be, symbol.upper(), env),
                    )
            finally:
                conn.close()

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
                           realized_pnl, disallowed_loss, timestamp, regime, simulated, env,
                           tranche_1_filled, tranche_2_filled, breakeven_latched, created_at,
                           posterior_alpha, posterior_beta, kelly_fraction
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
