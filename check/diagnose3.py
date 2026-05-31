# diagnose3.py
import sqlite3
import pandas as pd

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

# Check gaps
diff = df.index.to_series().diff().dropna()
print("=== GAP ANALYSIS ===")
print(f"Total bars: {len(df)}")
print(f"Expected 1-min gaps: {len(df)-1}")
print(f"Actual 1-min gaps:   {(diff == pd.Timedelta('1min')).sum()}")
print(f"Gaps > 1 min:        {(diff > pd.Timedelta('1min')).sum()}")
print(f"Gaps > 1 hour:       {(diff > pd.Timedelta('1h')).sum()}")
print(f"Gaps > 1 day:        {(diff > pd.Timedelta('1d')).sum()}")
print(f"\nLargest 5 gaps:")
print(diff.nlargest(5))