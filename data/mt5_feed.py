"""
data/mt5_feed.py
Handles MT5 connection, bar fetching, and live tick streaming.
"""

import logging
import time
from datetime import datetime
from typing import Optional

import MetaTrader5 as mt5
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


class MT5Feed:
    def __init__(self, cfg: dict):
        self.login    = cfg["mt5"]["login"]
        self.password = cfg["mt5"]["password"]
        self.server   = cfg["mt5"]["server"]
        self.symbol   = cfg["mt5"]["symbol"]
        self.tf_str   = cfg["mt5"]["timeframe"]
        self.htf_str  = cfg["mt5"]["htf_timeframe"]
        self.tf       = self._parse_tf(self.tf_str)
        self.htf      = self._parse_tf(self.htf_str)
        self._connected = False

    # ------------------------------------------------------------------ #
    #  Connection                                                          #
    # ------------------------------------------------------------------ #

    def connect(self) -> bool:
        if not mt5.initialize(login=self.login,
                              password=self.password,
                              server=self.server):
            log.error("MT5 initialize failed: %s", mt5.last_error())
            return False
        info = mt5.account_info()
        if info is None:
            log.error("MT5 account_info failed: %s", mt5.last_error())
            return False
        log.info("MT5 connected — account %s, balance %.2f", info.login, info.balance)

        # Activate symbol so data requests work immediately
        if not mt5.symbol_select(self.symbol, True):
            log.error("symbol_select failed for %s: %s", self.symbol, mt5.last_error())
            return False
        log.info("Symbol %s ready", self.symbol)

        self._connected = True
        return True

    def disconnect(self):
        mt5.shutdown()
        self._connected = False
        log.info("MT5 disconnected")

    def is_connected(self) -> bool:
        return self._connected and mt5.account_info() is not None

    def reconnect(self, retries: int = 5, delay: float = 5.0) -> bool:
        for i in range(retries):
            log.warning("Reconnect attempt %d/%d", i + 1, retries)
            mt5.shutdown()
            time.sleep(delay)
            if self.connect():
                return True
        return False

    # ------------------------------------------------------------------ #
    #  Bar data                                                            #
    # ------------------------------------------------------------------ #

    def get_bars(self, n: int = 500, tf=None) -> Optional[pd.DataFrame]:
        """Fetch last n closed bars as DataFrame."""
        tf = tf or self.tf
        rates = mt5.copy_rates_from_pos(self.symbol, tf, 0, n + 1)
        if rates is None or len(rates) == 0:
            log.error("copy_rates_from_pos failed: %s", mt5.last_error())
            return None
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        df.set_index("time", inplace=True)
        df.rename(columns={"tick_volume": "volume"}, inplace=True)
        return df.iloc[:-1]  # drop incomplete current bar

    def get_htf_bars(self, n: int = 100) -> Optional[pd.DataFrame]:
        return self.get_bars(n=n, tf=self.htf)

    def get_latest_bar(self) -> Optional[pd.Series]:
        """Return the last completed bar."""
        df = self.get_bars(n=2)
        return df.iloc[-1] if df is not None and len(df) >= 1 else None

    # ------------------------------------------------------------------ #
    #  Tick / spread                                                       #
    # ------------------------------------------------------------------ #

    def get_tick(self) -> Optional[dict]:
        tick = mt5.symbol_info_tick(self.symbol)
        if tick is None:
            return None
        return {
            "bid":    tick.bid,
            "ask":    tick.ask,
            "spread": round(tick.ask - tick.bid, 5),
            "time":   datetime.fromtimestamp(tick.time),
        }

    def get_spread_points(self) -> float:
        info = mt5.symbol_info(self.symbol)
        tick = mt5.symbol_info_tick(self.symbol)
        if info is None or tick is None:
            return 0.0
        return round((tick.ask - tick.bid) / info.point, 1)

    # ------------------------------------------------------------------ #
    #  Symbol info                                                         #
    # ------------------------------------------------------------------ #

    def get_symbol_info(self) -> Optional[object]:
        return mt5.symbol_info(self.symbol)

    def get_point(self) -> float:
        info = mt5.symbol_info(self.symbol)
        return info.point if info else 0.25  # MNQ default tick = 0.25

    def get_tick_value(self) -> float:
        """Value in account currency per 1 tick move for 1 lot."""
        info = mt5.symbol_info(self.symbol)
        return info.trade_tick_value if info else 0.5  # MNQ: $0.50/tick

    # ------------------------------------------------------------------ #
    #  Volume delta proxy (no real L2 from MT5, approximate from bars)    #
    # ------------------------------------------------------------------ #

    @staticmethod
    def compute_volume_delta(df: pd.DataFrame) -> pd.Series:
        """
        Approximate buy/sell volume delta using bar direction.
        Bullish bar → +volume, bearish bar → -volume.
        Not true order-flow but useful as a momentum proxy.
        """
        direction = np.where(df["close"] >= df["open"], 1, -1)
        return pd.Series(direction * df["volume"].values,
                         index=df.index, name="vol_delta")

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_tf(tf_str: str) -> int:
        mapping = {
            "M1":  mt5.TIMEFRAME_M1,
            "M5":  mt5.TIMEFRAME_M5,
            "M15": mt5.TIMEFRAME_M15,
            "M30": mt5.TIMEFRAME_M30,
            "H1":  mt5.TIMEFRAME_H1,
        }
        return mapping.get(tf_str.upper(), mt5.TIMEFRAME_M1)

    def wait_for_new_bar(self, poll_secs: float = 0.5) -> bool:
        """Block until a new 1m bar opens. Returns False if disconnected."""
        last = self.get_latest_bar()
        if last is None:
            return False
        last_time = last.name
        while True:
            time.sleep(poll_secs)
            if not self.is_connected():
                return False
            current = self.get_latest_bar()
            if current is not None and current.name != last_time:
                return True
