"""
features/pipeline.py
Computes all features used by the ML model from raw OHLCV bars.
"""

import numpy as np
import pandas as pd


def build_features(df: pd.DataFrame,
                   htf_df: pd.DataFrame = None,
                   window: int = 30) -> pd.DataFrame:
    """
    Main entry point.
    Returns DataFrame with all features aligned to df's index.
    Drops NaN rows introduced by rolling calculations.
    """
    f = pd.DataFrame(index=df.index)

    f = _momentum(f, df, window)
    f = _volatility(f, df, window)
    f = _microstructure(f, df, window)
    f = _session(f, df)
    if htf_df is not None:
        f = _htf_context(f, df, htf_df)

    f.dropna(inplace=True)
    return f


# ------------------------------------------------------------------ #
#  Momentum features                                                   #
# ------------------------------------------------------------------ #

def _momentum(f: pd.DataFrame, df: pd.DataFrame, w: int) -> pd.DataFrame:
    close = df["close"]

    # Rate of change
    f["roc_3"]  = close.pct_change(3)
    f["roc_10"] = close.pct_change(10)

    # RSI
    f["rsi_14"] = _rsi(close, 14)

    # MACD histogram
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd  = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    f["macd_hist"] = macd - signal

    # EMA alignment (fast > slow = bullish)
    ema9  = close.ewm(span=9,  adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    f["ema_align"] = (
        (ema9 > ema21).astype(int) +
        (ema21 > ema50).astype(int) -
        (ema9 < ema21).astype(int) -
        (ema21 < ema50).astype(int)
    )  # range: -2 to +2

    # Consecutive bar direction
    direction = np.sign(close - close.shift(1))
    f["consec_bars"] = direction.rolling(5).sum()  # -5 to +5

    return f


# ------------------------------------------------------------------ #
#  Volatility features                                                 #
# ------------------------------------------------------------------ #

def _volatility(f: pd.DataFrame, df: pd.DataFrame, w: int) -> pd.DataFrame:
    high, low, close = df["high"], df["low"], df["close"]

    # ATR
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr14 = tr.ewm(span=14, adjust=False).mean()
    f["atr14"] = atr14

    # ATR normalised by price (relative volatility)
    f["atr_pct"] = atr14 / close

    # Bollinger Band width (normalised)
    sma20 = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    f["bb_width"] = (2 * 2 * std20) / sma20

    # BB %B position
    f["bb_pct_b"] = (close - (sma20 - 2 * std20)) / (4 * std20)

    # Chop index: ATR ratio over window (high value = chop)
    highest_high = high.rolling(w).max()
    lowest_low   = low.rolling(w).min()
    atr_sum      = tr.rolling(w).sum()
    range_w      = (highest_high - lowest_low).replace(0, np.nan)
    f["chop_index"] = atr_sum / range_w  # < 0.5 trending, > 0.7 choppy

    return f


# ------------------------------------------------------------------ #
#  Microstructure features                                             #
# ------------------------------------------------------------------ #

def _microstructure(f: pd.DataFrame, df: pd.DataFrame, w: int) -> pd.DataFrame:
    close  = df["close"]
    volume = df["volume"]
    high   = df["high"]
    low    = df["low"]

    # VWAP deviation (rolling intraday VWAP proxy)
    typical = (high + low + close) / 3
    vwap    = (typical * volume).rolling(w).sum() / volume.rolling(w).sum()
    f["vwap_dev"] = (close - vwap) / vwap

    # Volume ratio: current vs rolling average
    vol_ma = volume.rolling(w).mean()
    f["vol_ratio"] = volume / vol_ma.replace(0, np.nan)

    # Volume delta (approximate: bullish bar = +vol)
    direction = np.where(close >= close.shift(1), 1, -1)
    vol_delta = pd.Series(direction * volume.values, index=df.index)
    f["vol_delta_sum"] = vol_delta.rolling(w).sum() / volume.rolling(w).sum()

    # Bar body ratio (body / range) — quality of move
    body  = (close - df["open"]).abs()
    range_bar = (high - low).replace(0, np.nan)
    f["body_ratio"] = body / range_bar

    # Upper/lower wick ratio
    body_top    = pd.concat([close, df["open"]], axis=1).max(axis=1)
    body_bottom = pd.concat([close, df["open"]], axis=1).min(axis=1)
    f["upper_wick"] = (high - body_top)   / range_bar
    f["lower_wick"] = (body_bottom - low) / range_bar

    return f


# ------------------------------------------------------------------ #
#  Session / time features                                            #
# ------------------------------------------------------------------ #

def _session(f: pd.DataFrame, df: pd.DataFrame) -> pd.DataFrame:
    idx = df.index

    # Time as cyclic features (hour of day, UTC)
    hour = idx.hour + idx.minute / 60
    f["hour_sin"] = np.sin(2 * np.pi * hour / 24)
    f["hour_cos"] = np.cos(2 * np.pi * hour / 24)

    # Day of week cyclic
    dow = idx.dayofweek.astype(float)
    f["dow_sin"] = np.sin(2 * np.pi * dow / 5)
    f["dow_cos"] = np.cos(2 * np.pi * dow / 5)

    # Session flags (UTC times)
    f["session_london"]  = ((hour >= 7)  & (hour < 16)).astype(int)
    f["session_ny"]      = ((hour >= 13) & (hour < 20)).astype(int)
    f["session_overlap"] = ((hour >= 13) & (hour < 16)).astype(int)

    return f


# ------------------------------------------------------------------ #
#  Higher timeframe context (HTF)                                     #
# ------------------------------------------------------------------ #

def _htf_context(f: pd.DataFrame,
                 df: pd.DataFrame,
                 htf_df: pd.DataFrame) -> pd.DataFrame:
    """Merge HTF trend direction and ATR into LTF index via forward-fill."""
    close_htf = htf_df["close"]
    high_htf  = htf_df["high"]
    low_htf   = htf_df["low"]

    ema20_htf = close_htf.ewm(span=20, adjust=False).mean()
    tr_htf = pd.concat([
        high_htf - low_htf,
        (high_htf - close_htf.shift(1)).abs(),
        (low_htf  - close_htf.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr14_htf = tr_htf.ewm(span=14, adjust=False).mean()

    htf_trend = pd.Series(
        np.sign(close_htf - ema20_htf), index=htf_df.index, name="htf_trend"
    )
    htf_atr = pd.Series(atr14_htf.values, index=htf_df.index, name="htf_atr")

    # Reindex to LTF, forward-fill
    f["htf_trend"] = htf_trend.reindex(df.index, method="ffill")
    f["htf_atr"]   = htf_atr.reindex(df.index, method="ffill")

    return f


# ------------------------------------------------------------------ #
#  Label generation (for training)                                    #
# ------------------------------------------------------------------ #

def make_labels(df: pd.DataFrame,
                lookahead: int = 6,
                threshold_atr: float = 0.8) -> pd.Series:
    """
    3-class labels (vectorized — fast on large datasets):
      1 = LONG  — max(high) in next N bars - entry >= threshold * ATR
     -1 = SHORT — entry - min(low) in next N bars >= threshold * ATR
      0 = SKIP  — neither or both
    Uses high/low for TP detection (more realistic than close-only).
    """
    close = df["close"]
    high  = df["high"]
    low   = df["low"]

    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr    = tr.ewm(span=14, adjust=False).mean()
    thresh = threshold_atr * atr

    # Vectorized rolling max/min over the lookahead window (shifted forward)
    future_high = high.shift(-1).rolling(lookahead).max().shift(-(lookahead - 1))
    future_low  = low.shift(-1).rolling(lookahead).min().shift(-(lookahead - 1))

    up_move = future_high - close
    dn_move = close - future_low

    up_hit = up_move >= thresh
    dn_hit = dn_move >= thresh

    labels = pd.Series(0, index=df.index, name="label")
    labels[up_hit & ~dn_hit] = 1
    labels[dn_hit & ~up_hit] = -1

    return labels


# ------------------------------------------------------------------ #
#  Utility                                                             #
# ------------------------------------------------------------------ #

def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta  = series.diff()
    gain   = delta.clip(lower=0)
    loss   = (-delta).clip(lower=0)
    avg_g  = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_l  = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs     = avg_g / avg_l.replace(0, np.nan)
    return 100 - (100 / (1 + rs))
