"""
dashboard/state.py
Writes bot state to logs/state.json after every bar.
The dashboard server reads this file to display live data.
"""

import json
import os
from datetime import datetime
from typing import Optional


class StateWriter:
    def __init__(self, path: str = "logs/state.json", device: str = "cpu"):
        self.path        = path
        self._device     = device
        self._trades     = []   # list of closed paper trades
        self._open_trade = None
        self._equity     = 0.0
        self._peak       = 0.0
        self._bars       = 0
        self._signals    = {"LONG": 0, "SHORT": 0, "SKIP": 0}
        os.makedirs("logs", exist_ok=True)

    # ------------------------------------------------------------------ #
    #  Called from main loop                                               #
    # ------------------------------------------------------------------ #

    def on_bar(self,
               bar_time: str,
               price:    float,
               signal:   int,
               prob:     float,
               atr:      float,
               chop:     float,
               paper:    bool):
        self._bars += 1
        sig_name = {1: "LONG", -1: "SHORT", 0: "SKIP"}[signal]
        self._signals[sig_name] += 1
        self._flush(bar_time, price, signal, prob, atr, chop, paper)

    def on_paper_open(self, bar_time: str, direction: int,
                      entry: float, sl: float, tp: float, lots: float):
        self._open_trade = {
            "time":      bar_time,
            "direction": "LONG" if direction == 1 else "SHORT",
            "entry":     entry,
            "sl":        sl,
            "tp":        tp,
            "lots":      lots,
            "pnl":       0.0,
        }

    def on_paper_tick(self, current_price: float):
        """Update floating PnL of open paper trade."""
        if self._open_trade is None:
            return
        d   = 1 if self._open_trade["direction"] == "LONG" else -1
        pts = (current_price - self._open_trade["entry"]) * d
        self._open_trade["pnl"] = round(pts * self._open_trade["lots"] * 10, 2)

    def on_paper_close(self, bar_time: str, exit_price: float, reason: str):
        if self._open_trade is None:
            return
        d   = 1 if self._open_trade["direction"] == "LONG" else -1
        pts = (exit_price - self._open_trade["entry"]) * d
        pnl = round(pts * self._open_trade["lots"] * 10, 2)

        closed = {**self._open_trade, "exit": exit_price,
                  "exit_time": bar_time, "pnl": pnl, "reason": reason}
        self._trades.append(closed)
        self._equity += pnl
        self._peak    = max(self._peak, self._equity)
        self._open_trade = None

    # ------------------------------------------------------------------ #
    #  Write JSON                                                          #
    # ------------------------------------------------------------------ #

    def _flush(self, bar_time, price, signal, prob, atr, chop, paper):
        dd = self._equity - self._peak
        state = {
            "updated":     datetime.utcnow().isoformat(),
            "bar_time":    bar_time,
            "price":       price,
            "signal":      {1: "LONG", -1: "SHORT", 0: "SKIP"}[signal],
            "prob":        round(prob, 4),
            "atr":         round(atr, 2),
            "chop":        round(chop, 3),
            "paper":       paper,
            "bars_seen":   self._bars,
            "signals":     self._signals,
            "equity":      round(self._equity, 2),
            "drawdown":    round(dd, 2),
            "open_trade":  self._open_trade,
            "trades":      self._trades[-50:],   # last 50
            "win_rate":    self._win_rate(),
            "total_trades": len(self._trades),
            "device":      self._device,
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, self.path)   # atomic write

    def _win_rate(self) -> Optional[float]:
        if not self._trades:
            return None
        wins = sum(1 for t in self._trades if t["pnl"] > 0)
        return round(wins / len(self._trades) * 100, 1)
