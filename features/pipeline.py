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
    Returns DataFrame with all features aligned to df's index.sql
    Drops NaN rows introduced by rolling calculations.
    """
    f = pd.DataFrame(index=df.index)

    f = _momentum(f, df, window)
    f = _volatility(f, df, window)
    f = _microstructure(f, df, window)
    f = _session(f, df)
    f = _institutional_flow(f, df)
    f = _structure(f, df)
    f = _mobius_scalper(f, df)
    f = _htf_supply_demand(f, df)
    if htf_df is not None:
        f = _htf_context(f, df, htf_df)

    f.dropna(inplace=True)
    return f


# ------------------------------------------------------------------ #
#  Momentum features                                                   #
# ------------------------------------------------------------------ #

def _momentum(f: pd.DataFrame, df: pd.DataFrame, w: int) -> pd.DataFrame:
    close = df["close"]
    high  = df["high"]
    low   = df["low"]

    # Rate of change
    f["roc_3"]  = close.pct_change(3)
    f["roc_10"] = close.pct_change(10)

    # RSI
    rsi = _rsi(close, 14)
    f["rsi_14"] = rsi

    # RSI momentum — direction of RSI
    f["rsi_slope"] = rsi - rsi.shift(3)

    # MACD histogram
    ema12  = close.ewm(span=12, adjust=False).mean()
    ema26  = close.ewm(span=26, adjust=False).mean()
    macd   = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    f["macd_hist"]  = macd - signal
    f["macd_slope"] = (macd - signal) - (macd - signal).shift(2)  # accelerating/decelerating

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

    # Distance from EMA9 (how stretched price is)
    f["dist_ema9"] = (close - ema9) / ema9

    # Consecutive bar direction
    direction = np.sign(close - close.shift(1))
    f["consec_bars"] = direction.rolling(5).sum()  # -5 to +5

    # Bar size relative to recent average (momentum quality)
    bar_size = high - low
    f["bar_size_ratio"] = bar_size / bar_size.rolling(10).mean().replace(0, np.nan)

    # EMA 5 — faster trend confirmation
    ema5  = close.ewm(span=5,  adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()
    f["ema5_slope"]   = ema5.diff(2) / (close * 0.0001)
    f["ema5_above21"] = (ema5 > ema21).astype(int)
    f["dist_ema5"]    = (close - ema5) / ema5

    # RSI 7 — faster exhaustion detection for pullback entries
    f["rsi_7"] = _rsi(close, 7)

    # ADX — trend strength (14 period)
    f["adx"] = _adx(high, low, close, 14)

    # Price position in rolling range (0=bottom 1=top)
    roll_high = high.rolling(20).max()
    roll_low  = low.rolling(20).min()
    roll_range = (roll_high - roll_low).replace(0, np.nan)
    f["price_position"] = (close - roll_low) / roll_range

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

    # Chop index: normalized 0-1 (classic formula). < 0.5 trending, > 0.7 choppy.
    highest_high = high.rolling(w).max()
    lowest_low   = low.rolling(w).min()
    atr_sum      = tr.rolling(w).sum()
    range_w      = (highest_high - lowest_low).replace(0, np.nan)
    raw_ratio    = (atr_sum / range_w).clip(lower=1e-9)
    f["chop_index"] = np.log10(raw_ratio) / np.log10(w)

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

    # Pullback: price touched EMA21 or VWAP in last 3 bars
    ema21_ms = close.ewm(span=21, adjust=False).mean()
    vwap_ms  = (close * volume).rolling(30).sum() / volume.rolling(30).sum()
    f["pullback_ema21"] = (low.rolling(3).min() <= ema21_ms).astype(int)
    f["pullback_vwap"]  = (
        (low.rolling(3).min() <= vwap_ms) & (high.rolling(3).max() >= vwap_ms)
    ).astype(int)
    f["near_ema21"] = (
        (close - ema21_ms).abs() / ema21_ms < 0.001
    ).astype(int)

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
#  Institutional flow (Follow The Money)                              #
# ------------------------------------------------------------------ #

def _institutional_flow(f: pd.DataFrame, df: pd.DataFrame) -> pd.DataFrame:
    """
    MFI, net flow histogram, institutional bar detection.
    Derived from 'Follow The Money' TOS indicator system.
    """
    close  = df["close"]
    high   = df["high"]
    low    = df["low"]
    volume = df["volume"]

    # --- MFI (Money Flow Index) — true formula ---
    typical   = (high + low + close) / 3
    raw_flow  = typical * volume
    pos_flow  = raw_flow.where(typical > typical.shift(1), 0.0)
    neg_flow  = raw_flow.where(typical < typical.shift(1), 0.0)
    sum_pos   = pos_flow.rolling(14).sum()
    sum_neg   = neg_flow.rolling(14).sum()
    ratio     = sum_pos / sum_neg.replace(0, np.nan)
    f["mfi"]  = 100 - (100 / (1 + ratio))

    # --- Net money flow histogram (aceleración del flujo) ---
    avg_vol      = volume.rolling(20).mean().replace(0, np.nan)
    net_flow     = (sum_pos - sum_neg) / avg_vol
    signal_line  = net_flow.ewm(span=9, adjust=False).mean()
    f["flow_hist"]       = net_flow - signal_line   # + = dinero entrando
    f["flow_bull_cross"] = ((net_flow > signal_line) &
                            (net_flow.shift(1) <= signal_line.shift(1))).astype(int)
    f["flow_bear_cross"] = ((net_flow < signal_line) &
                            (net_flow.shift(1) >= signal_line.shift(1))).astype(int)

    # --- Institutional bar detection ---
    bar_range = (high - low).replace(0, np.nan)
    close_pct = (close - low) / bar_range
    is_high_vol = volume >= avg_vol * 1.5
    f["inst_bar"] = np.where(is_high_vol & (close_pct >= 0.6),  1,   # accumulation
                    np.where(is_high_vol & (close_pct <= 0.4), -1,   # distribution
                    0))

    # --- VWAP binary (above = bullish institutional side) ---
    typical_x_vol = typical * volume
    vwap_cum = typical_x_vol.rolling(390, min_periods=1).sum() / \
               volume.rolling(390, min_periods=1).sum()
    f["above_vwap"] = (close > vwap_cum).astype(int)

    return f


# ------------------------------------------------------------------ #
#  Market structure (Murrey Math + Kijun)                             #
# ------------------------------------------------------------------ #

def _structure(f: pd.DataFrame, df: pd.DataFrame) -> pd.DataFrame:
    """
    Price position within daily range (Murrey Math) and Kijun-sen.
    """
    close = df["close"]
    high  = df["high"]
    low   = df["low"]

    # --- Kijun-sen: midpoint of 26-bar range (Ichimoku baseline) ---
    kijun          = (high.rolling(26).max() + low.rolling(26).min()) / 2
    f["dist_kijun"] = (close - kijun) / kijun.replace(0, np.nan)

    # --- Daily price position (approx session range via 390-bar rolling) ---
    session_high   = high.rolling(390, min_periods=30).max()
    session_low    = low.rolling(390, min_periods=30).min()
    session_range  = (session_high - session_low).replace(0, np.nan)
    f["price_pos_daily"] = (close - session_low) / session_range  # 0=bottom 1=top

    # --- ATR daily context: how much of daily ATR has been consumed ---
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    daily_atr        = tr.rolling(390, min_periods=30).sum()
    current_range    = session_high - session_low
    f["atr_consumed"] = (current_range / daily_atr.replace(0, np.nan)).clip(0, 1)

    return f


# ------------------------------------------------------------------ #
#  Mobius Scalper oscillator                                          #
# ------------------------------------------------------------------ #

def _mobius_scalper(f: pd.DataFrame, df: pd.DataFrame, n: int = 8) -> pd.DataFrame:
    """
    Adaptive momentum efficiency oscillator by Mobius.
    R = EMA(b-b[1]) / EMA(|b-b[1]|)  where b is a power-adaptive smoother.
    Scaled 0-100: >90 overbought (SHORT), <10 oversold (LONG).
    Non-repainting — ideal for ML features.
    """
    close = df["close"].values
    b = np.empty(len(close))
    b[0] = close[0]
    for i in range(1, len(close)):
        ratio = close[i] / b[i-1] if b[i-1] != 0 else 1.0
        b[i]  = b[i-1] + (close[i] - b[i-1]) / (n * (ratio ** 4))

    b_series = pd.Series(b, index=df.index)
    diff     = b_series - b_series.shift(1)
    k        = 2 / (n + 1)
    avg      = diff.ewm(alpha=k, adjust=False).mean()
    abs_avg  = diff.abs().ewm(alpha=k, adjust=False).mean()
    R        = avg / abs_avg.replace(0, np.nan)
    r_osc    = (50 * (R + 1)).clip(0, 100)

    f["r_osc"]          = r_osc
    f["r_bull"]         = (r_osc > 50).astype(int)
    # Crosses: oversold recovery (buy) and overbought exhaustion (sell)
    f["r_cross_long"]   = ((r_osc > 10) & (r_osc.shift(1) <= 10)).astype(int)
    f["r_cross_short"]  = ((r_osc < 90) & (r_osc.shift(1) >= 90)).astype(int)

    return f


# ------------------------------------------------------------------ #
#  HTF Supply & Demand with Reactivity (TOS replica)                  #
# ------------------------------------------------------------------ #

def _htf_supply_demand(f: pd.DataFrame, df: pd.DataFrame,
                       swing_bars: int = 600,
                       react_pct: float = 0.0003,
                       touch_window: int = 100,
                       min_touches: int = 2) -> pd.DataFrame:
    """
    Replica el indicador HTF Supply & Demand de TOS.
    swing_bars=600 ≈ 10 horas en M1.
    Solo marca nivel como activo si fue tocado >= min_touches veces.
    """
    close = df["close"]
    high  = df["high"]
    low   = df["low"]
    idx   = df.index

    # HTF Resistance y Support (rolling max/min — no repinta)
    htf_res = high.rolling(swing_bars, min_periods=60).max()
    htf_sup = low.rolling(swing_bars,  min_periods=60).min()

    # Distancias al nivel (0 = en el nivel, > 0 = lejos)
    f["dist_htf_res"] = (htf_res - close) / close.replace(0, np.nan)
    f["dist_htf_sup"] = (close - htf_sup)  / close.replace(0, np.nan)

    # Reactivity: cuántas veces tocó cada nivel en los últimos touch_window bars
    proximity   = react_pct * close
    touched_res = ((high - htf_res).abs() <= proximity).astype(float)
    touched_sup = ((low  - htf_sup).abs() <= proximity).astype(float)
    f["res_touches"] = touched_res.rolling(touch_window, min_periods=1).sum()
    f["sup_touches"] = touched_sup.rolling(touch_window, min_periods=1).sum()

    # Nivel "activo" si fue tocado >= min_touches
    f["res_active"]   = (f["res_touches"] >= min_touches).astype(int)
    f["sup_active"]   = (f["sup_touches"] >= min_touches).astype(int)

    # HTF Pressure: +1 resistencia activa, -1 soporte activo, 0 balanced
    f["htf_pressure"] = f["res_active"] - f["sup_active"]

    # Prior Day High/Low
    try:
        daily_h = high.resample("D").max()
        daily_l = low.resample("D").min()
        pdh = daily_h.shift(1).reindex(idx, method="ffill")
        pdl = daily_l.shift(1).reindex(idx, method="ffill")
        if pdh.isna().all():
            raise ValueError("no prior day data in window")
        f["dist_pdh"]  = (pdh - close) / close.replace(0, np.nan)
        f["dist_pdl"]  = (close - pdl) / close.replace(0, np.nan)
        f["above_pdh"] = (close > pdh).astype(int)
        f["below_pdl"] = (close < pdl).astype(int)
    except Exception:
        pdh_roll = high.rolling(390, min_periods=30).max().shift(1)
        pdl_roll = low.rolling(390, min_periods=30).min().shift(1)
        f["dist_pdh"]  = (pdh_roll - close) / close.replace(0, np.nan)
        f["dist_pdl"]  = (close - pdl_roll) / close.replace(0, np.nan)
        f["above_pdh"] = (close > pdh_roll).astype(int)
        f["below_pdl"] = (close < pdl_roll).astype(int)

    return f


# ------------------------------------------------------------------ #
#  Label generation (for training)                                    #
# ------------------------------------------------------------------ #

def make_labels(df: pd.DataFrame,
                lookahead: int = 15,
                threshold_atr: float = 0.4,
                momentum_bars: int = 5) -> pd.Series:
    """
    Trend-continuation labels.

    LONG  = momentum already UP at T (last momentum_bars bars positive)
            AND net close at T+lookahead is > threshold
            AND >=60% of lookahead bars close above T's close

    SHORT = same logic inverted

    SKIP  = everything else

    This prevents labeling entries at the END of trends: the model only
    learns to enter when momentum is established AND continues sustainedly.
    """
    close = df["close"]
    high  = df["high"]
    low   = df["low"]

    # ATR threshold
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr    = tr.ewm(span=14, adjust=False).mean()
    thresh = threshold_atr * atr

    # Momentum at T: net price change over last N bars
    momentum = close - close.shift(momentum_bars)

    # Net move at lookahead
    net_future = close.shift(-lookahead) - close

    # Fraction of lookahead bars that close above/below T's close (vectorized)
    close_vals = close.values
    n = len(close_vals)
    frac_above = np.zeros(n)
    frac_below = np.zeros(n)

    if n > lookahead:
        windows   = np.lib.stride_tricks.sliding_window_view(close_vals, lookahead)
        valid_n   = n - lookahead
        base      = close_vals[:valid_n]
        fut_wins  = windows[1:valid_n + 1]   # fut_wins[i] = close[i+1 : i+lookahead+1]
        frac_above[:valid_n] = (fut_wins > base[:, np.newaxis]).mean(axis=1)
        frac_below[:valid_n] = (fut_wins < base[:, np.newaxis]).mean(axis=1)

    frac_above = pd.Series(frac_above, index=df.index)
    frac_below = pd.Series(frac_below, index=df.index)

    labels = pd.Series(0, index=df.index, name="label")
    labels[(momentum > 0) & (net_future > thresh)  & (frac_above >= 0.60)] = 1
    labels[(momentum < 0) & (net_future < -thresh) & (frac_below >= 0.60)] = -1
    labels.iloc[-lookahead:] = 0   # can't label last N bars

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


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average Directional Index — measures trend strength (0-100, >25 = strong trend)."""
    prev_high  = high.shift(1)
    prev_low   = low.shift(1)
    prev_close = close.shift(1)

    up_move   = high - prev_high
    down_move = prev_low - low

    plus_dm  = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr    = tr.ewm(span=period, adjust=False).mean()
    plus_di  = 100 * pd.Series(plus_dm,  index=high.index).ewm(span=period, adjust=False).mean() / atr.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=high.index).ewm(span=period, adjust=False).mean() / atr.replace(0, np.nan)

    dx  = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    adx = dx.ewm(span=period, adjust=False).mean()
    return adx
