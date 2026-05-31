# diagnose2.py
import sqlite3
import pandas as pd
import numpy as np
import sys
sys.path.insert(0, '.')

from features.pipeline import _momentum, _volatility, _microstructure, _session

conn = sqlite3.connect("logs/scalper.db")
df = pd.read_sql("SELECT time, open, high, low, close, volume FROM bars ORDER BY time DESC LIMIT 95000", conn)
df["time"] = pd.to_datetime(df["time"], format="mixed")
df.set_index("time", inplace=True)
df.sort_index(inplace=True)
df = df[~df.index.duplicated(keep="last")]
conn.close()

print(f"Bars loaded: {len(df)}")
print(f"Columns: {df.columns.tolist()}")

w = 30
f = pd.DataFrame(index=df.index)

f = _momentum(f, df, w)
nan_after = f.isna().any(axis=1).sum()
print(f"After momentum  — NaN rows: {nan_after}")

f = _volatility(f, df, w)
nan_after = f.isna().any(axis=1).sum()
print(f"After volatility — NaN rows: {nan_after}")

f = _microstructure(f, df, w)
nan_after = f.isna().any(axis=1).sum()
print(f"After microstructure — NaN rows: {nan_after}")

# Which columns have the most NaN?
print("\nNaN por columna (solo las que tienen):")
nan_cols = f.isna().sum()
print(nan_cols[nan_cols > 0].sort_values(ascending=False))