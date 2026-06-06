"""
execution/order_sender.py
Sends and manages orders on MT5.
"""

import logging
from datetime import datetime
from typing import Optional

import MetaTrader5 as mt5

from execution.risk_manager import TradeParams

log = logging.getLogger(__name__)


class OrderSender:
    def __init__(self, cfg: dict, feed):
        self.symbol  = cfg["mt5"]["symbol"]
        self.magic   = cfg["mt5"]["magic"]
        self.feed    = feed

    # ------------------------------------------------------------------ #
    #  Open position                                                       #
    # ------------------------------------------------------------------ #

    def open_position(self, params: TradeParams) -> Optional[int]:
        """
        Opens a market order with SL and TP.
        Returns ticket number on success, None on failure.
        """
        tick  = self.feed.get_tick()
        if tick is None:
            log.error("Cannot open position — no tick data")
            return None

        point = self.feed.get_point()
        if params.direction == 1:   # LONG
            order_type = mt5.ORDER_TYPE_BUY
            price      = tick["ask"]
            sl         = price - params.sl_points
            tp         = price + params.tp_points if params.tp_points > 0 else 0.0
        else:                       # SHORT
            order_type = mt5.ORDER_TYPE_SELL
            price      = tick["bid"]
            sl         = price + params.sl_points
            tp         = price - params.tp_points if params.tp_points > 0 else 0.0

        request = {
            "action":        mt5.TRADE_ACTION_DEAL,
            "symbol":        self.symbol,
            "volume":        params.lots,
            "type":          order_type,
            "price":         round(price, 2),
            "sl":            round(sl, 2),
            "tp":            round(tp, 2),
            "deviation":     10,
            "magic":         self.magic,
            "comment":       f"ml_scalper_atr{params.atr:.1f}",
            "type_time":     mt5.ORDER_TIME_GTC,
            "type_filling":  mt5.ORDER_FILLING_FOK,
        }

        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            code = result.retcode if result else "None"
            log.error("order_send failed — retcode: %s | comment: %s",
                      code, result.comment if result else "")
            return None, None

        # Use actual MT5 fill price, fallback to requested price if not available
        fill_price = result.price if result.price else price
        log.info("Order opened | ticket=%d | %s | lots=%.2f | fill=%.2f | sl=%.2f | tp=%.2f",
                 result.order,
                 "LONG" if params.direction == 1 else "SHORT",
                 params.lots, fill_price, sl, tp)
        return result.order, fill_price

    # ------------------------------------------------------------------ #
    #  Close position                                                      #
    # ------------------------------------------------------------------ #

    def close_position(self, ticket: int) -> Optional[float]:
        """
        Close a position by ticket. Returns realized PnL in USD, or None.
        """
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            log.warning("close_position: ticket %d not found", ticket)
            return None

        pos   = positions[0]
        tick  = self.feed.get_tick()
        if tick is None:
            return None

        if pos.type == mt5.POSITION_TYPE_BUY:
            close_type = mt5.ORDER_TYPE_SELL
            price      = tick["bid"]
        else:
            close_type = mt5.ORDER_TYPE_BUY
            price      = tick["ask"]

        request = {
            "action":        mt5.TRADE_ACTION_DEAL,
            "symbol":        self.symbol,
            "volume":        pos.volume,
            "type":          close_type,
            "position":      ticket,
            "price":         round(price, 2),
            "deviation":     10,
            "magic":         self.magic,
            "comment":       "ml_scalper_close",
            "type_time":     mt5.ORDER_TIME_GTC,
            "type_filling":  mt5.ORDER_FILLING_FOK,
        }

        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            code = result.retcode if result else "None"
            log.error("close_position failed — retcode: %s", code)
            return None

        pnl = pos.profit
        log.info("Position closed | ticket=%d | pnl=%.2f USD", ticket, pnl)
        return pnl

    # ------------------------------------------------------------------ #
    #  Monitoring                                                          #
    # ------------------------------------------------------------------ #

    def get_open_positions(self) -> list:
        """Returns list of open positions for this bot's magic number."""
        positions = mt5.positions_get(symbol=self.symbol)
        if positions is None:
            return []
        return [p for p in positions if p.magic == self.magic]

    def close_all_positions(self) -> float:
        """Close all open positions — call before shutdown."""
        positions = self.get_open_positions()
        total_pnl = 0.0
        for pos in positions:
            pnl = self.close_position(pos.ticket)
            if pnl is not None:
                total_pnl += pnl
                log.info("Shutdown close | ticket=%d | pnl=%.2f", pos.ticket, pnl)
        if positions:
            log.info("Closed %d positions before shutdown | total pnl=%.2f", len(positions), total_pnl)
        return total_pnl

    def get_position_pnl(self, ticket: int) -> Optional[float]:
        positions = mt5.positions_get(ticket=ticket)
        return positions[0].profit if positions else None

    def get_position_info(self, ticket: int) -> Optional[dict]:
        """Returns profit, entry price, and volume for a live position."""
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            return None
        pos = positions[0]
        return {"profit": pos.profit, "entry": pos.price_open, "lots": pos.volume}

    def modify_sl(self, ticket: int, new_sl: float) -> bool:
        """Move stop loss (e.g. to breakeven)."""
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            return False
        pos = positions[0]
        request = {
            "action":   mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "sl":       round(new_sl, 2),
            "tp":       pos.tp,
        }
        result = mt5.order_send(request)
        ok = result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
        if ok:
            log.info("SL modified | ticket=%d | new_sl=%.2f", ticket, new_sl)
        return ok
