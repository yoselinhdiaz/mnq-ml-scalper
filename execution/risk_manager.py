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
        self._today           = date.today()
        self._blocked         = False
        self._open_tickets    = None

        self._daily_limit = cfg["risk"].get("daily_loss_limit_usd", 120.0)

    def set_open_tickets(self, open_tickets: dict):
        self._open_tickets = open_tickets

    def compute_daily_limit(self):
        """Call after MT5 is connected to set limit from account balance %."""
        if "daily_loss_limit_pct" in self.cfg["risk"]:
            balance = self.feed.get_balance()
            pct     = self.cfg["risk"]["daily_loss_limit_pct"]
            self._daily_limit = balance * pct
            log.info("Daily loss limit: $%.2f (%.0f%% of $%.2f balance)",
                     self._daily_limit, pct * 100, balance)
        else:
            log.info("Daily loss limit: $%.2f", self._daily_limit)

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    def evaluate(self,
                 signal: int,
                 probability: float,
                 atr: float,
                 chop_index: float,
                 mtf_trend: int = 0) -> Optional[TradeParams]:
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

        # Dynamic threshold: counter-trend trades need higher confidence
        base_threshold = self.cfg["model"]["confidence_threshold"]
        counter_boost  = self.cfg["model"].get("counter_trend_boost", 0.10)
        required_prob  = base_threshold if (mtf_trend == 0 or signal == mtf_trend) \
                         else base_threshold + counter_boost

        if probability < required_prob:
            log.debug("Trade blocked: prob %.3f < required %.3f (%s trend)",
                      probability, required_prob,
                      "with" if signal == mtf_trend else "counter")
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
        pass  # open count is derived from open_tickets dict

    def record_trade_close(self, pnl_usd: float):
        if pnl_usd < 0:
            self._daily_loss += abs(pnl_usd)
            log.info("Daily loss updated: -$%.2f (total: $%.2f / $%.2f)",
                     abs(pnl_usd),
                     self._daily_loss,
                     self._daily_limit)
            if self._daily_loss >= self._daily_limit:
                self._blocked = True
                log.warning("DAILY LOSS LIMIT HIT ($%.2f) — trading blocked for today", self._daily_limit)

    @property
    def is_blocked(self) -> bool:
        return self._blocked

    @property
    def open_trades(self) -> int:
        return len(self._open_tickets) if self._open_tickets is not None else 0

    @property
    def daily_loss(self) -> float:
        return self._daily_loss

    # ------------------------------------------------------------------ #
    #  Internal                                                            #
    # ------------------------------------------------------------------ #

    def _check_filters(self, prob: float, chop: float) -> Optional[str]:
        from datetime import datetime, timezone
        if self._blocked:
            return "daily loss limit"
        open_count = len(self._open_tickets) if self._open_tickets is not None else 0
        if open_count >= self.cfg["risk"]["max_simultaneous_trades"]:
            return f"max trades ({open_count})"
        if chop > 0.7:
            return f"chop detected ({chop:.2f})"

        # Session filter — only open trades during allowed UTC hours
        sessions = self.cfg["risk"].get("allowed_sessions", [])
        if sessions:
            hour = datetime.now(timezone.utc).hour + datetime.now(timezone.utc).minute / 60
            in_session = any(start <= hour < end for start, end in sessions)
            if not in_session:
                return f"outside session (UTC {hour:.1f}h)"

        return None

    def _size_trade(self, direction: int, atr: float) -> Optional[TradeParams]:
        risk_usd    = self.cfg["risk"]["risk_per_trade_usd"]
        sl_mult     = self.cfg["risk"]["sl_atr_multiplier"]
        tp_min      = self.cfg["risk"]["tp_atr_multiplier_min"]
        tp_max      = self.cfg["risk"]["tp_atr_multiplier_max"]

        tick_value     = self.feed.get_tick_value()  # USD per tick per lot
        point          = self.feed.get_point()
        contract_size  = self.cfg["mt5"].get("contract_size", 1)
        if tick_value == 0 or point == 0:
            return None

        sl_points   = sl_mult * atr
        usd_per_lot = (sl_points / point) * tick_value * contract_size
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
