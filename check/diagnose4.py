# diagnose4.py
import sqlite3
import pandas as pd
import sys
sys.path.insert(0, '.')
from features.pipeline import build_features

conn = sqlite3.connect("logs/scalper.db")
df = pd.read_sql(
    "SELECT time, open, high, low, close, volume FROM bars ORDER BY time DESC LIMIT 95000",
    conn
)
df["time"] = pd.to_datetime(df["time"], format="mixed")
df.set_index("time", inplace=True)
df.sort_index(inplace=True)
df = df[~df.index.duplicated(keep="last")]
conn.close()

print(f"Input bars: {len(df)}")
print(f"Index dtype: {df.index.dtype}")
print(f"Columns: {df.columns.tolist()}")
print(f"Dtypes:\n{df.dtypes}")

f = build_features(df, htf_df=None, window=30)
print(f"\nOutput features: {len(f)}")
print(f"Dropped: {len(df) - len(f)}")