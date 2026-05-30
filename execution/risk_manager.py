"""
execution/risk_manager.py
Position sizing, daily loss tracking, and trade filters.
"""

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class TradeParams:
    direction:  int     # +1 LONG, -1 SHORT
    lots:       float
    sl_points:  float   # stop loss distance in points
    tp_points:  float   # take profit distance in points
    atr:        float   # ATR at entry time


class RiskManager:
    def __init__(self, cfg: dict, feed):
        self.cfg              = cfg
        self.feed             = feed
        self._daily_loss      = 0.0
        self._open_trades     = 0
        self._today           = date.today()
        self._blocked         = False      # True = daily limit hit

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    def evaluate(self,
                 signal: int,
                 probability: float,
                 atr: float,
                 chop_index: float) -> Optional[TradeParams]:
        """
        Returns TradeParams if trade is allowed, None otherwise.

        signal      : +1 LONG | -1 SHORT | 0 SKIP
        probability : model confidence for the predicted class
        atr         : current ATR in price units
        chop_index  : chop index (> 0.7 = avoid)
        """
        self._reset_if_new_day()

        if signal == 0:
            return None

        reason = self._check_filters(probability, chop_index)
        if reason:
            log.debug("Trade blocked: %s", reason)
            return None

        params = self._size_trade(signal, atr)
        if params is None:
            log.warning("Could not size trade (tick_value=0?)")
            return None

        return params

    def record_trade_open(self):
        self._open_trades += 1

    def record_trade_close(self, pnl_usd: float):
        self._open_trades = max(0, self._open_trades - 1)
        if pnl_usd < 0:
            self._daily_loss += abs(pnl_usd)
            log.info("Daily loss updated: -$%.2f (total: $%.2f / $%.2f)",
                     abs(pnl_usd),
                     self._daily_loss,
                     self.cfg["risk"]["daily_loss_limit_usd"])
            if self._daily_loss >= self.cfg["risk"]["daily_loss_limit_usd"]:
                self._blocked = True
                log.warning("DAILY LOSS LIMIT HIT — trading blocked for today")

    @property
    def is_blocked(self) -> bool:
        return self._blocked

    @property
    def open_trades(self) -> int:
        return self._open_trades

    @property
    def daily_loss(self) -> float:
        return self._daily_loss

    # ------------------------------------------------------------------ #
    #  Internal                                                            #
    # ------------------------------------------------------------------ #

    def _check_filters(self, prob: float, chop: float) -> Optional[str]:
        if self._blocked:
            return "daily loss limit"
        if self._open_trades >= self.cfg["risk"]["max_simultaneous_trades"]:
            return f"max trades ({self._open_trades})"
        if prob < self.cfg["model"]["confidence_threshold"]:
            return f"low confidence ({prob:.3f})"
        if chop > 0.7:
            return f"chop detected ({chop:.2f})"
        return None

    def _size_trade(self, direction: int, atr: float) -> Optional[TradeParams]:
        risk_usd    = self.cfg["risk"]["risk_per_trade_usd"]
        sl_mult     = self.cfg["risk"]["sl_atr_multiplier"]
        tp_min      = self.cfg["risk"]["tp_atr_multiplier_min"]
        tp_max      = self.cfg["risk"]["tp_atr_multiplier_max"]

        tick_value  = self.feed.get_tick_value()  # USD per tick per lot
        point       = self.feed.get_point()       # e.g. 0.25 for MNQ
        if tick_value == 0 or point == 0:
            return None

        sl_points   = sl_mult * atr
        usd_per_lot = (sl_points / point) * tick_value
        lots        = round(risk_usd / usd_per_lot, 2) if usd_per_lot > 0 else 0.01
        lots        = max(0.01, min(lots, 5.0))  # cap at 5 lots

        # Dynamic TP: scale with ATR, clamped
        tp_mult   = max(tp_min, min(tp_max, sl_mult * 1.5))
        tp_points = tp_mult * atr

        return TradeParams(
            direction=direction,
            lots=lots,
            sl_points=sl_points,
            tp_points=tp_points,
            atr=atr,
        )

    def _reset_if_new_day(self):
        today = date.today()
        if today != self._today:
            self._today      = today
            self._daily_loss = 0.0
            self._blocked    = False
            log.info("New trading day — risk counters reset")
