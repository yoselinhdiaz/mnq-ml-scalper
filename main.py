"""
main.py
Entry point. Runs the live trading loop.

Usage:
    python main.py                       # live trading
    python main.py --paper               # paper mode
    python main.py --paper --dashboard   # paper + live dashboard
    python main.py --train-only          # train model and exit
"""

import argparse
import logging
import os
import sys
import threading
import time

import numpy as np
import yaml

from data.mt5_feed import MT5Feed
from data.database import Database
from execution.order_sender import OrderSender
from execution.risk_manager import RiskManager
from features.pipeline import build_features
from model import train as trainer
from model.retrain import RetrainScheduler

# ------------------------------------------------------------------ #
#  Logging                                                             #
# ------------------------------------------------------------------ #

def setup_logging(cfg: dict):
    os.makedirs("logs", exist_ok=True)
    level = getattr(logging, cfg["logging"]["level"].upper(), logging.INFO)
    fmt   = "%(asctime)s %(levelname)-8s %(name)s - %(message)s"
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(cfg["logging"]["log_file"], encoding="utf-8"),
    ]
    logging.basicConfig(level=level, format=fmt, handlers=handlers)

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
#  Prediction                                                          #
# ------------------------------------------------------------------ #

def predict(features_row: np.ndarray, state: dict):
    model  = state["model"]
    scaler = state["scaler"]
    x      = scaler.transform(features_row.reshape(1, -1))
    proba  = model.predict_proba(x)[0]
    cls    = int(np.argmax(proba))
    prob   = float(proba[cls])
    signal = {0: -1, 1: 0, 2: 1}[cls]
    return signal, prob


# ------------------------------------------------------------------ #
#  Paper trade simulator                                               #
# ------------------------------------------------------------------ #

class PaperTracker:
    def __init__(self):
        self.direction = None
        self.entry     = None
        self.sl        = None
        self.tp        = None
        self.lots      = None
        self.open_time = None

    @property
    def is_open(self):
        return self.entry is not None

    def open(self, direction, entry, sl_pts, tp_pts, lots, bar_time, sw, db):
        self.direction = direction
        self.entry     = entry
        self.sl        = entry - sl_pts if direction == 1 else entry + sl_pts
        self.tp        = entry + tp_pts if direction == 1 else entry - tp_pts
        self.lots      = lots
        self.open_time = bar_time
        log.info("[PAPER] OPEN %s | entry=%.1f SL=%.1f TP=%.1f lots=%.2f",
                 "LONG" if direction == 1 else "SHORT", entry, self.sl, self.tp, lots)
        sw.on_paper_open(bar_time, direction, entry, sl_pts, tp_pts, lots)

    def check(self, high, low, close, bar_time, sw, db) -> bool:
        if not self.is_open:
            return False
        sw.on_paper_tick(close)

        hit_sl = (self.direction == 1 and low  <= self.sl) or \
                 (self.direction == -1 and high >= self.sl)
        hit_tp = (self.direction == 1 and high >= self.tp) or \
                 (self.direction == -1 and low  <= self.tp)

        if hit_sl:
            self._close(bar_time, self.sl, "SL", sw, db)
            return True
        if hit_tp:
            self._close(bar_time, self.tp, "TP", sw, db)
            return True
        return False

    def _close(self, bar_time, exit_price, reason, sw, db):
        d   = 1 if self.direction == 1 else -1
        pnl = round((exit_price - self.entry) * d * self.lots * 10, 2)
        log.info("[PAPER] CLOSE %s | exit=%.1f | pnl=$%.2f", reason, exit_price, pnl)
        sw.on_paper_close(bar_time, exit_price, reason)
        db.save_trade(
            open_time  = self.open_time,
            close_time = bar_time,
            direction  = "LONG" if self.direction == 1 else "SHORT",
            entry      = self.entry,
            exit       = exit_price,
            sl         = self.sl,
            tp         = self.tp,
            lots       = self.lots,
            pnl        = pnl,
            reason     = reason,
            paper      = True,
        )
        self._reset()

    def _reset(self):
        self.direction = self.entry = self.sl = self.tp = self.lots = self.open_time = None


# ------------------------------------------------------------------ #
#  Breakeven monitor (background thread, checks every 10s)            #
# ------------------------------------------------------------------ #

class BreakevenMonitor:
    """
    Checks every second: when floating profit >= trigger_usd,
    moves SL to a price that locks in lock_usd profit.
    """
    def __init__(self, cfg: dict, sender, feed, open_tickets: dict):
        self.cfg          = cfg
        self.sender       = sender
        self.feed         = feed
        self.open_tickets = open_tickets
        self._stop        = threading.Event()
        self._thread      = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            self._check()
            self._stop.wait(1)

    def _check(self):
        min_profit  = self.cfg["risk"].get("breakeven_min_profit_usd", 25.0)
        tick_value  = self.feed.get_tick_value()
        point       = self.feed.get_point()
        contract_sz = self.cfg["mt5"].get("contract_size", 1)

        if tick_value == 0 or point == 0:
            return

        for ticket, info in list(self.open_tickets.items()):
            if info.get("be_done"):
                continue

            pos_info = self.sender.get_position_info(ticket)
            if pos_info is None:
                continue

            spread_cost = info.get("spread_cost", 0.0)
            trigger     = min_profit + spread_cost          # e.g. $25 + $2 = $27
            profit      = pos_info["profit"]

            if profit < trigger:
                continue

            # Move SL to entry + spread_cost in price pts (net breakeven after spread)
            direction        = info["direction"]
            entry            = pos_info["entry"]
            lots             = pos_info["lots"]
            profit_per_point = lots * (tick_value / point) * contract_sz

            if profit_per_point == 0:
                continue

            lock_pts = spread_cost / profit_per_point       # pts to cover spread cost
            new_sl   = round(entry + lock_pts * direction, 2)

            if self.sender.modify_sl(ticket, new_sl):
                info["be_done"] = True
                log.info("B/E activated | ticket=%d | profit=%.2f >= trigger=%.2f | sl=%.2f (covers spread $%.2f)",
                         ticket, profit, trigger, new_sl, spread_cost)


# ------------------------------------------------------------------ #
#  Main loop                                                           #
# ------------------------------------------------------------------ #

def run(cfg: dict, paper: bool = False, dashboard: bool = False):
    from dashboard.state import StateWriter

    feed   = MT5Feed(cfg)
    risk   = RiskManager(cfg, feed)
    sender = OrderSender(cfg, feed)
    sw     = StateWriter()
    db     = Database()
    paper_tracker = PaperTracker()

    if dashboard:
        from dashboard.server import start_background
        start_background()
        log.info("Dashboard -> http://localhost:8765")

    if not feed.connect():
        log.error("Cannot connect to MT5. Exiting.")
        sys.exit(1)

    risk.compute_daily_limit()

    log.info("Fetching historical data...")
    df     = feed.get_bars(n=cfg["data"]["lookback_bars"])
    htf_df = feed.get_htf_bars(n=400)

    if df is None or len(df) < cfg["data"]["min_bars_to_trade"]:
        log.error("Not enough bars. Exiting.")
        sys.exit(1)

    # Save historical bars to DB
    db.save_bars_bulk(df)
    log.info("DB has %d bars total", db.bar_count())

    if trainer.model_exists():
        log.info("Loading existing model...")
        model, scaler = trainer.load()
    else:
        log.info("Training new model...")
        model, scaler = trainer.train(df, htf_df, cfg, db=db)

    state = {"model": model, "scaler": scaler}

    retrain_sched = RetrainScheduler(cfg, feed, state, db)
    retrain_sched.start()

    open_tickets: dict = {}
    risk.set_open_tickets(open_tickets)

    # Re-load any positions already open in MT5 (e.g. after restart)
    if not paper:
        existing = sender.get_open_positions()
        for pos in existing:
            features_now = build_features(df, None, window=cfg["data"]["feature_window"])
            atr_now = float(features_now.iloc[-1].get("atr14", 10.0)) if len(features_now) else 10.0
            open_tickets[pos.ticket] = {
                "direction": 1 if pos.type == 0 else -1,  # 0=BUY, 1=SELL
                "entry":     pos.price_open,
                "atr":       atr_now,
                "be_done":   False,
            }
            log.info("Loaded existing position | ticket=%d | %s @ %.2f",
                     pos.ticket, "LONG" if pos.type == 0 else "SHORT", pos.price_open)

    be_monitor = BreakevenMonitor(cfg, sender, feed, open_tickets)
    if not paper:
        be_monitor.start()

    log.info("=" * 60)
    log.info("US100 ML Scalper | paper=%s | dashboard=%s", paper, dashboard)
    log.info("DB: %d bars | %d paper trades", db.bar_count(), db.trade_count())
    log.info("=" * 60)

    while True:
        try:
            if not feed.wait_for_new_bar():
                log.warning("Lost MT5 connection - reconnecting...")
                if not feed.reconnect():
                    break
                continue

            df = feed.get_bars(n=cfg["data"]["lookback_bars"])
            if df is None:
                continue

            features = build_features(df, None, window=cfg["data"]["feature_window"])
            if len(features) == 0:
                continue

            last_row   = features.iloc[-1].values
            feat_dict  = features.iloc[-1].to_dict()
            atr        = float(features.iloc[-1].get("atr14", 1.0))
            chop_index = float(features.iloc[-1].get("chop_index", 0.5))
            bar_time   = str(df.index[-1])
            price      = float(df["close"].iloc[-1])
            high       = float(df["high"].iloc[-1])
            low        = float(df["low"].iloc[-1])

            signal, prob = predict(last_row, state)

            log.info("Bar %s | price=%.1f | signal=%+d | prob=%.3f | atr=%.2f | chop=%.2f",
                     bar_time[11:16], price, signal, prob, atr, chop_index)

            # Save bar + signal to DB
            db.save_bar(bar_time, df["open"].iloc[-1], high, low, price, df["volume"].iloc[-1])
            db.save_signal(bar_time, price, signal, prob, atr, chop_index, feat_dict)

            # Check paper SL/TP
            if paper and paper_tracker.is_open:
                paper_tracker.check(high, low, price, bar_time, sw, db)

            # Detect live positions closed by MT5 (SL/TP hit)
            if not paper:
                open_pos_tickets = {p.ticket for p in sender.get_open_positions()}
                closed = [t for t in open_tickets if t not in open_pos_tickets]
                for t in closed:
                    info = open_tickets[t]
                    # Recover PnL from MT5 deal history
                    import MetaTrader5 as _mt5
                    deals = _mt5.history_deals_get(position=t)
                    pnl = sum(d.profit for d in deals) if deals else 0.0
                    exit_price = deals[-1].price if deals else 0.0
                    risk.record_trade_close(pnl)
                    db.save_trade(
                        open_time  = str(info.get("open_time", bar_time)),
                        close_time = bar_time,
                        direction  = "LONG" if info["direction"] == 1 else "SHORT",
                        entry      = info["entry"],
                        exit       = exit_price,
                        sl         = 0.0,
                        tp         = 0.0,
                        lots       = info.get("lots", 0.0),
                        pnl        = pnl,
                        reason     = "MT5",
                        paper      = False,
                    )
                    log.info("Trade closed | ticket=%d | pnl=%.2f", t, pnl)
                    del open_tickets[t]

            sw.on_bar(bar_time, price, signal, prob, atr, chop_index, paper)

            mtf_trend = feed.get_mtf_trend() if not paper else 0
            if mtf_trend != 0:
                log.info("MTF trend: %s", "BULLISH" if mtf_trend == 1 else "BEARISH")

            params = risk.evaluate(signal, prob, atr, chop_index, mtf_trend)
            if params is None:
                continue

            if paper:
                if not paper_tracker.is_open:
                    paper_tracker.open(
                        params.direction, price,
                        params.sl_points, params.tp_points,
                        params.lots, bar_time, sw, db
                    )
            else:
                # Handle opposite direction positions
                flip_threshold = cfg["model"].get("flip_confidence_threshold", 0.60)
                conflicting = [
                    t for t, info in list(open_tickets.items())
                    if isinstance(info, dict) and info.get("direction") == -params.direction
                ]
                if conflicting:
                    if prob >= flip_threshold:
                        # High confidence: close opposite and flip
                        for t in conflicting:
                            pnl = sender.close_position(t) or 0.0
                            risk.record_trade_close(pnl)
                            del open_tickets[t]
                            log.info("Momentum flip: closed %s ticket=%d pnl=%.2f",
                                     "LONG" if -params.direction == 1 else "SHORT", t, pnl)
                    else:
                        # Low confidence: don't open — would create opposite positions
                        log.debug("Trade blocked: opposite position open, confidence %.3f < flip threshold %.3f",
                                  prob, flip_threshold)
                        continue

                ticket = sender.open_position(params)
                if ticket:
                    # Spread cost in USD: spread_pts (in ticks) × tick_value × lots × contract_size
                    spread_pts   = feed.get_spread_points()   # in ticks (e.g. 280 ticks)
                    tick_value   = feed.get_tick_value()       # USD per tick per lot
                    contract_sz  = cfg["mt5"].get("contract_size", 1)
                    spread_cost  = spread_pts * tick_value * params.lots * contract_sz

                    open_tickets[ticket] = {
                        "direction":   params.direction,
                        "entry":       price,
                        "atr":         atr,
                        "lots":        params.lots,
                        "open_time":   bar_time,
                        "spread_cost": spread_cost,
                        "be_done":     False,
                    }
                    log.info("Spread cost for ticket=%d: $%.2f", ticket, spread_cost)
                    risk.record_trade_open()

        except KeyboardInterrupt:
            log.info("Stopped by user")
            break
        except Exception as e:
            log.exception("Loop error: %s", e)
            time.sleep(5)

    try:
        retrain_sched.stop()
    except Exception:
        pass
    try:
        be_monitor.stop()
    except Exception:
        pass
    try:
        feed.disconnect()
    except Exception:
        pass
    try:
        db.close()
    except Exception:
        pass
    log.info("Scalper stopped. DB: %d bars | %d paper trades | PnL: $%.2f",
             db.bar_count(), db.trade_count(), db.total_pnl())


# ------------------------------------------------------------------ #
#  CLI                                                                 #
# ------------------------------------------------------------------ #

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="US100 ML Scalper")
    parser.add_argument("--paper",      action="store_true")
    parser.add_argument("--dashboard",  action="store_true")
    parser.add_argument("--train-only", action="store_true")
    parser.add_argument("--config",     default="config.yaml")
    args = parser.parse_args()

    with open(args.config) as f:
        cfg = yaml.safe_load(f)

    setup_logging(cfg)

    if args.train_only:
        feed = MT5Feed(cfg)
        db   = Database()
        feed.connect()
        df     = feed.get_bars(n=cfg["data"]["lookback_bars"])
        htf_df = feed.get_htf_bars(n=400)  # may be None if MT5 unavailable
        db.save_bars_bulk(df)
        log.info("DB has %d bars total", db.bar_count())
        trainer.train(df, htf_df, cfg, db=db)
        feed.disconnect()
        db.close()
    else:
        run(cfg, paper=args.paper, dashboard=args.dashboard)
