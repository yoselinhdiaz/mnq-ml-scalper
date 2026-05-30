# check_db_times.py
import sqlite3

conn = sqlite3.connect("logs/scalper.db")

print("=== Primeras 5 filas ===")
for r in conn.execute("SELECT time, open, close FROM bars LIMIT 5"):
    print(r)

print("\n=== Últimas 5 filas ===")
for r in conn.execute("SELECT time, open, close FROM bars ORDER BY rowid DESC LIMIT 5"):
    print(r)

print("\n=== Tipo del campo time ===")
for r in conn.execute("SELECT typeof(time) FROM bars LIMIT 3"):
    print(r)

print("\n=== Total bars ===")
print(conn.execute("SELECT COUNT(*) FROM bars").fetchone()[0])
conn.close()