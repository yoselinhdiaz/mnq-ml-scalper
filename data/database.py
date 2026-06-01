"""
data/database.py
SQLite storage for bars, model signals, and paper trades.
No external dependencies — sqlite3 is part of Python stdlib.
"""

import sqlite3
import os
import logging
from datetime import datetime
from typing import Optional
import json

log = logging.getLogger(__name__)

DB_PATH = "logs/scalper.db"


class Database:
    def __init__(self, path: str = DB_PATH):
        os.makedirs("logs", exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._create_tables()
        log.info("Database ready -> %s", path)

    # ------------------------------------------------------------------ #
    #  Schema                                                              #
    # ------------------------------------------------------------------ #

    def _create_tables(self):
        self._conn.executescript("""
        CREATE TABLE IF NOT EXISTS bars (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            time        TEXT    NOT NULL UNIQUE,
            open        REAL,
            high        REAL,
            low         REAL,
            close       REAL,
            volume      REAL
        );

        CREATE TABLE IF NOT EXISTS signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            time        TEXT    NOT NULL UNIQUE,
            price       REAL,
            signal      INTEGER,   -- +1 LONG | -1 SHORT | 0 SKIP
            prob        REAL,
            atr         REAL,
            chop        REAL,
            features    TEXT       -- JSON blob of all feature values
        );

        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            open_time   TEXT,
            close_time  TEXT,
            direction   TEXT,      -- LONG | SHORT
            entry       REAL,
            exit        REAL,
            sl          REAL,
            tp          REAL,
            lots        REAL,
            pnl         REAL,
            reason      TEXT,      -- TP | SL | MANUAL
            paper       INTEGER    -- 1 = paper, 0 = live
        );

        CREATE INDEX IF NOT EXISTS idx_signals_time ON signals(time);
        CREATE INDEX IF NOT EXISTS idx_trades_open  ON trades(open_time);
        """)
        self._conn.commit()

    # ------------------------------------------------------------------ #
    #  Bar storage                                                         #
    # ------------------------------------------------------------------ #

    def save_bar(self, time: str, o: float, h: float,
                 l: float, c: float, v: float):
        try:
            self._conn.execute("""
                INSERT OR IGNORE INTO bars (time, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (time, o, h, l, c, v))
            self._conn.commit()
        except Exception as e:
            log.debug("save_bar error: %s", e)

    def save_bars_bulk(self, df):
        """Save a DataFrame of bars efficiently."""
        if df is None or len(df) == 0:
            log.warning("save_bars_bulk: empty dataframe, skipping")
            return
        rows = [
            (str(idx), row.open, row.high, row.low, row.close, row.volume)
            for idx, row in df.iterrows()
        ]
        self._conn.executemany("""
            INSERT OR IGNORE INTO bars (time, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?)
        """, rows)
        self._conn.commit()
        log.info("Saved %d bars to database", len(rows))

    # ------------------------------------------------------------------ #
    #  Signal storage                                                      #
    # ------------------------------------------------------------------ #

    def save_signal(self, time: str, price: float, signal: int,
                    prob: float, atr: float, chop: float,
                    features: Optional[dict] = None):
        try:
            self._conn.execute("""
                INSERT OR REPLACE INTO signals
                    (time, price, signal, prob, atr, chop, features)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (time, price, signal, prob, atr, chop,
                  json.dumps(features) if features else None))
            self._conn.commit()
        except Exception as e:
            log.debug("save_signal error: %s", e)

    # ------------------------------------------------------------------ #
    #  Trade storage                                                       #
    # ------------------------------------------------------------------ #

    def save_trade(self, open_time: str, close_time: str,
                   direction: str, entry: float, exit: float,
                   sl: float, tp: float, lots: float,
                   pnl: float, reason: str, paper: bool = True):
        self._conn.execute("""
            INSERT INTO trades
                (open_time, close_time, direction, entry, exit,
                 sl, tp, lots, pnl, reason, paper)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (open_time, close_time, direction, entry, exit,
              sl, tp, lots, pnl, reason, int(paper)))
        self._conn.commit()

    # ------------------------------------------------------------------ #
    #  Queries for retraining                                              #
    # ------------------------------------------------------------------ #

    def load_bars_df(self, limit: int = 20000):
        """Load bars as pandas DataFrame for retraining."""
        import pandas as pd
        df = pd.read_sql(
            f"SELECT time, open, high, low, close, volume FROM bars ORDER BY time DESC LIMIT {limit}",
            self._conn
        )
        df["time"] = pd.to_datetime(df["time"], format="mixed")
        df.set_index("time", inplace=True)
        df.sort_index(inplace=True)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df.dropna(inplace=True)

        # Remove duplicate timestamps — keep last (most recent data wins)
        dupes = df.index.duplicated(keep="last").sum()
        if dupes > 0:
            log.info("Removing %d duplicate timestamps from bars", dupes)
            df = df[~df.index.duplicated(keep="last")]

        return df

    def bar_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM bars").fetchone()
        return row[0]

    def trade_count(self, paper: bool = True) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM trades WHERE paper=?", (int(paper),)
        ).fetchone()
        return row[0]

    def recent_trades(self, n: int = 20, paper: bool = True):
        return self._conn.execute("""
            SELECT * FROM trades WHERE paper=?
            ORDER BY open_time DESC LIMIT ?
        """, (int(paper), n)).fetchall()

    def win_rate(self, paper: bool = True) -> Optional[float]:
        rows = self._conn.execute(
            "SELECT pnl FROM trades WHERE paper=?", (int(paper),)
        ).fetchall()
        if not rows:
            return None
        wins = sum(1 for r in rows if r["pnl"] > 0)
        return round(wins / len(rows) * 100, 1)

    def total_pnl(self, paper: bool = True) -> float:
        row = self._conn.execute(
            "SELECT SUM(pnl) FROM trades WHERE paper=?", (int(paper),)
        ).fetchone()
        return round(row[0] or 0.0, 2)

    def close(self):
        self._conn.close()
