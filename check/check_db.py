# check_db.py
import sqlite3

conn = sqlite3.connect("logs/scalper.db")

print("=== BARS ===")
row = conn.execute("SELECT COUNT(*), MIN(time), MAX(time) FROM bars").fetchone()
print(f"  Total: {row[0]} | Desde: {row[1]} | Hasta: {row[2]}")

print("\n=== SIGNALS ===")
row = conn.execute("SELECT COUNT(*) FROM signals").fetchone()
print(f"  Total: {row[0]}")

print("\n=== TRADES ===")
rows = conn.execute("SELECT direction, entry, exit, pnl, reason FROM trades ORDER BY open_time DESC LIMIT 10").fetchall()
for r in rows:
    print(f"  {r[0]} | entry={r[1]:.1f} exit={r[2]:.1f} pnl=${r[3]:.2f} [{r[4]}]")

conn.close()