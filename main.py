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
        model, scaler = trainer.train(df, htf_df, cfg)

    state = {"model": model, "scaler": scaler}

    retrain_sched = RetrainScheduler(cfg, feed, state, db)
    retrain_sched.start()

    open_tickets: dict = {}

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
            htf_df = feed.get_htf_bars(n=100)

            features = build_features(df, htf_df, window=cfg["data"]["feature_window"])
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

            # Check live positions closed by MT5
            if not paper:
                closed = []
                for ticket in list(open_tickets):
                    if not any(p.ticket == ticket for p in sender.get_open_positions()):
                        risk.record_trade_close(0.0)
                        closed.append(ticket)
                for t in closed:
                    del open_tickets[t]

            sw.on_bar(bar_time, price, signal, prob, atr, chop_index, paper)

            params = risk.evaluate(signal, prob, atr, chop_index)
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
                ticket = sender.open_position(params)
                if ticket:
                    open_tickets[ticket] = signal
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
        htf_df = feed.get_htf_bars(n=400)
        db.save_bars_bulk(df)
        log.info("DB has %d bars", db.bar_count())
        trainer.train(df, htf_df, cfg)
        feed.disconnect()
        db.close()
    else:
        run(cfg, paper=args.paper, dashboard=args.dashboard)
