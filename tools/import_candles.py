"""
tools/import_candles.py
Imports market_candles from an existing SQLite database into scalper DB.

Usage:
    python tools/import_candles.py --src PATH_TO_runtime.sqlite3
    python tools/import_candles.py --src ../other-project/backend/runtime.sqlite3
    python tools/import_candles.py --src C:/path/to/runtime.sqlite3 --symbol US100. --tf 1m
"""

import argparse
import sqlite3
import os
import sys
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s"
)
log = logging.getLogger(__name__)

# Add project root to path so we can import Database
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data.database import Database


def import_candles(src_path: str, symbol: str, timeframe: str, dst_db: Database):
    if not os.path.exists(src_path):
        log.error("Source DB not found: %s", src_path)
        sys.exit(1)

    src = sqlite3.connect(src_path)
    src.row_factory = sqlite3.Row

    # Check table exists
    tables = [r[0] for r in src.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    if "market_candles" not in tables:
        log.error("Table 'market_candles' not found in %s", src_path)
        log.error("Available tables: %s", tables)
        sys.exit(1)

    # Count source rows
    total = src.execute(
        "SELECT COUNT(*) FROM market_candles WHERE symbol=? AND timeframe=?",
        (symbol, timeframe)
    ).fetchone()[0]
    log.info("Found %d candles for %s/%s in source DB", total, symbol, timeframe)

    if total == 0:
        log.warning("No candles found. Check --symbol and --tf values.")
        # Show what's available
        rows = src.execute(
            "SELECT symbol, timeframe, COUNT(*) as n FROM market_candles GROUP BY symbol, timeframe"
        ).fetchall()
        log.info("Available data:")
        for r in rows:
            log.info("  symbol=%-12s tf=%-6s bars=%d", r["symbol"], r["timeframe"], r["n"])
        sys.exit(0)

    # Fetch in batches
    BATCH = 10000
    imported = 0
    offset   = 0

    log.info("Importing into scalper DB...")
    while True:
        rows = src.execute("""
            SELECT time, open, high, low, close, volume
            FROM market_candles
            WHERE symbol=? AND timeframe=?
            ORDER BY time ASC
            LIMIT ? OFFSET ?
        """, (symbol, timeframe, BATCH, offset)).fetchall()

        if not rows:
            break

        # Insert batch
        dst_db._conn.executemany("""
            INSERT OR IGNORE INTO bars (time, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?)
        """, [(r["time"], r["open"], r["high"], r["low"], r["close"], r["volume"])
              for r in rows])
        dst_db._conn.commit()

        imported += len(rows)
        offset   += BATCH
        log.info("  %d / %d bars imported...", imported, total)

    src.close()

    final = dst_db.bar_count()
    log.info("Done. Scalper DB now has %d bars total.", final)


def main():
    parser = argparse.ArgumentParser(description="Import candles into scalper DB")
    parser.add_argument("--src",    required=True, help="Path to source runtime.sqlite3")
    parser.add_argument("--symbol", default="US100.", help="Symbol name (default: US100.)")
    parser.add_argument("--tf",     default="1m",    help="Timeframe (default: 1m)")
    parser.add_argument("--db",     default="logs/scalper.db", help="Scalper DB path")
    args = parser.parse_args()

    log.info("Source:  %s", args.src)
    log.info("Symbol:  %s", args.symbol)
    log.info("TF:      %s", args.tf)
    log.info("Dest DB: %s", args.db)

    dst = Database(path=args.db)
    log.info("Scalper DB before import: %d bars", dst.bar_count())

    import_candles(args.src, args.symbol, args.tf, dst)

    dst.close()


if __name__ == "__main__":
    main()
