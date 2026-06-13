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

        self._daily_limit  = cfg["risk"].get("daily_loss_limit_usd", 120.0)
        self._daily_trades = 0

        # Consecutive SL blocker — per direction
        self._sl_streak:      dict = {1: 0, -1: 0}   # consecutive SL hits per direction
        self._sl_block_until: dict = {1: None, -1: None}  # datetime when unblocked

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

        # Consecutive SL cooldown check
        sl_block = self._sl_block_until.get(signal)
        if sl_block is not None:
            from datetime import datetime, timezone
            if datetime.now(timezone.utc) < sl_block:
                remaining = int((sl_block - datetime.now(timezone.utc)).total_seconds() / 60)
                log.debug("Trade blocked: SL cooldown %s (%d min left)",
                          "LONG" if signal == 1 else "SHORT", remaining)
                return None
            else:
                # Cooldown expired — reset
                self._sl_block_until[signal] = None
                self._sl_streak[signal] = 0

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
        self._daily_trades += 1

    def record_trade_close(self, pnl_usd: float, direction: int = 0, reason: str = ""):
        if pnl_usd < 0:
            self._daily_loss += abs(pnl_usd)
            log.info("Daily loss updated: -$%.2f (total: $%.2f / $%.2f)",
                     abs(pnl_usd), self._daily_loss, self._daily_limit)
            if self._daily_loss >= self._daily_limit:
                self._blocked = True
                log.warning("DAILY LOSS LIMIT HIT ($%.2f) — trading blocked for today", self._daily_limit)

        # Consecutive SL tracker
        if direction != 0:
            sl_limit   = self.cfg["risk"].get("consecutive_sl_limit", 2)
            cooldown_m = self.cfg["risk"].get("sl_cooldown_minutes", 30)
            is_sl_hit  = pnl_usd < 0  # any loss counts (SL or trailing SL)
            if is_sl_hit:
                self._sl_streak[direction] = self._sl_streak.get(direction, 0) + 1
                if self._sl_streak[direction] >= sl_limit:
                    from datetime import datetime, timezone, timedelta
                    unblock = datetime.now(timezone.utc) + timedelta(minutes=cooldown_m)
                    self._sl_block_until[direction] = unblock
                    dir_name = "LONG" if direction == 1 else "SHORT"
                    log.warning("SL blocker: %d consecutive losses %s — blocked for %d min until %s",
                                self._sl_streak[direction], dir_name, cooldown_m,
                                unblock.strftime("%H:%M UTC"))
            else:
                # Winning trade resets streak for that direction
                self._sl_streak[direction] = 0

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
        max_daily = self.cfg["risk"].get("max_trades_per_day", 999)
        if self._daily_trades >= max_daily:
            return f"max daily trades ({self._daily_trades})"
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
        sl_mult = self.cfg["risk"]["sl_atr_multiplier"]
        tp_min  = self.cfg["risk"]["tp_atr_multiplier_min"]
        tp_max  = self.cfg["risk"]["tp_atr_multiplier_max"]

        # Soporte para equity-relative (risk_per_trade_pct) o fixed USD
        if "risk_per_trade_pct" in self.cfg["risk"]:
            balance  = self.feed.get_balance()
            risk_pct = self.cfg["risk"]["risk_per_trade_pct"]
            risk_usd = max(balance * risk_pct, 1.0)
            log.debug("Sizing (equity): balance=%.2f pct=%.4f risk_usd=%.2f",
                      balance, risk_pct, risk_usd)
        else:
            risk_usd = self.cfg["risk"]["risk_per_trade_usd"]

        tick_value    = self.feed.get_tick_value()
        point         = self.feed.get_point()
        contract_size = self.cfg["mt5"].get("contract_size", 1)
        if tick_value == 0 or point == 0:
            return None

        sl_points   = sl_mult * atr
        usd_per_lot = (sl_points / point) * tick_value * contract_size
        lots        = round(risk_usd / usd_per_lot, 2) if usd_per_lot > 0 else 0.01
        lots        = max(0.01, min(lots, 5.0))

        tp_ratio  = self.cfg["risk"].get("tp_rr_ratio", 2.0)
        tp_points = sl_points * tp_ratio

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
            self._today        = today
            self._daily_loss   = 0.0
            self._daily_trades = 0
            self._blocked      = False
            log.info("New trading day — risk counters reset")
