# diagnose.py
import sqlite3
import pandas as pd
import numpy as np

conn = sqlite3.connect("logs/scalper.db")
df = pd.read_sql("SELECT * FROM bars ORDER BY time DESC LIMIT 100000", conn)
df["time"] = pd.to_datetime(df["time"], format="mixed")
df.set_index("time", inplace=True)
df.sort_index(inplace=True)
df = df[~df.index.duplicated(keep="last")]

print(f"Bars: {len(df)}")
print(f"NaN en columnas:\n{df.isna().sum()}")
print(f"Ceros en close: {(df['close'] == 0).sum()}")
print(f"Ceros en open:  {(df['open'] == 0).sum()}")
print(f"Inf en df: {np.isinf(df.select_dtypes('number')).sum().sum()}")
print(f"\nPrimeras 3 filas:\n{df.head(3)}")
print(f"\nEstadísticas close:\n{df['close'].describe()}")
conn.close()