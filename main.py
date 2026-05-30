"""
main.py
Entry point. Runs the live trading loop.

Usage:
    python main.py                    # live trading
    python main.py --paper            # paper mode (signals only, no orders)
    python main.py --train-only       # train model and exit
    python main.py --paper --dashboard  # paper mode + live dashboard
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime

import numpy as np
import yaml

from data.mt5_feed import MT5Feed
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
#  Prediction helper                                                   #
# ------------------------------------------------------------------ #

def predict(features_row: np.ndarray, state: dict, cfg: dict):
    model  = state["model"]
    scaler = state["scaler"]
    x      = scaler.transform(features_row.reshape(1, -1))
    proba  = model.predict_proba(x)[0]   # [P_SHORT, P_SKIP, P_LONG]
    cls    = int(np.argmax(proba))
    prob   = float(proba[cls])
    signal = {0: -1, 1: 0, 2: 1}[cls]
    return signal, prob


# ------------------------------------------------------------------ #
#  Paper trade simulator (SL/TP tracking without real orders)          #
# ------------------------------------------------------------------ #

class PaperTracker:
    """Tracks a single open paper trade and checks SL/TP each bar."""

    def __init__(self):
        self.direction = None
        self.entry     = None
        self.sl        = None   # absolute price
        self.tp        = None   # absolute price
        self.lots      = None
        self.open_time = None

    @property
    def is_open(self):
        return self.entry is not None

    def open(self, direction, entry, sl_pts, tp_pts, lots, bar_time, sw: "StateWriter"):
        self.direction = direction
        self.entry     = entry
        self.sl        = entry - sl_pts if direction == 1 else entry + sl_pts
        self.tp        = entry + tp_pts if direction == 1 else entry - tp_pts
        self.lots      = lots
        self.open_time = bar_time
        log.info("[PAPER] OPEN %s | entry=%.1f SL=%.1f TP=%.1f lots=%.2f",
                 "LONG" if direction==1 else "SHORT", entry, self.sl, self.tp, lots)
        sw.on_paper_open(bar_time, direction, entry, sl_pts, tp_pts, lots)

    def check(self, high, low, close, bar_time, sw: "StateWriter") -> bool:
        """Returns True if trade was closed this bar."""
        if not self.is_open:
            return False
        sw.on_paper_tick(close)

        hit_sl = (self.direction == 1 and low  <= self.sl) or \
                 (self.direction ==-1 and high >= self.sl)
        hit_tp = (self.direction == 1 and high >= self.tp) or \
                 (self.direction ==-1 and low  <= self.tp)

        if hit_sl:
            sw.on_paper_close(bar_time, self.sl, "SL")
            log.info("[PAPER] CLOSE SL | exit=%.1f", self.sl)
            self._reset()
            return True
        if hit_tp:
            sw.on_paper_close(bar_time, self.tp, "TP")
            log.info("[PAPER] CLOSE TP | exit=%.1f", self.tp)
            self._reset()
            return True
        return False

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
    paper_tracker = PaperTracker()

    if dashboard:
        from dashboard.server import start_background
        start_background()
        log.info("Dashboard -> http://localhost:8765")

    if not feed.connect():
        log.error("Cannot connect to MT5. Exiting.")
        sys.exit(1)

    log.info("Fetching historical data for initial training...")
    df     = feed.get_bars(n=cfg["data"]["lookback_bars"])
    htf_df = feed.get_htf_bars(n=400)

    if df is None or len(df) < cfg["data"]["min_bars_to_trade"]:
        log.error("Not enough bars to start. Exiting.")
        sys.exit(1)

    if trainer.model_exists():
        log.info("Loading existing model from disk...")
        model, scaler = trainer.load()
    else:
        log.info("Training new model...")
        model, scaler = trainer.train(df, htf_df, cfg)

    state = {"model": model, "scaler": scaler}

    retrain_sched = RetrainScheduler(cfg, feed, state)
    retrain_sched.start()

    open_tickets: dict = {}

    log.info("=" * 60)
    log.info("US100 ML Scalper started | paper=%s | dashboard=%s", paper, dashboard)
    log.info("=" * 60)

    while True:
        try:
            if not feed.wait_for_new_bar():
                log.warning("Lost MT5 connection - reconnecting...")
                if not feed.reconnect():
                    log.error("Reconnect failed. Exiting.")
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
            atr        = float(features.iloc[-1].get("atr14", 1.0))
            chop_index = float(features.iloc[-1].get("chop_index", 0.5))
            bar_time   = str(df.index[-1])
            price      = float(df["close"].iloc[-1])
            high       = float(df["high"].iloc[-1])
            low        = float(df["low"].iloc[-1])

            signal, prob = predict(last_row, state, cfg)

            log.info("Bar %s | price=%.1f | signal=%+d | prob=%.3f | atr=%.2f | chop=%.2f",
                     bar_time[11:16], price, signal, prob, atr, chop_index)

            # Check paper trade SL/TP
            if paper and paper_tracker.is_open:
                paper_tracker.check(high, low, price, bar_time, sw)

            # Check live positions
            if not paper:
                closed = []
                for ticket in list(open_tickets):
                    positions = sender.get_open_positions()
                    if not any(p.ticket == ticket for p in positions):
                        risk.record_trade_close(0.0)
                        closed.append(ticket)
                for t in closed:
                    del open_tickets[t]

            # Write dashboard state
            sw.on_bar(bar_time, price, signal, prob, atr, chop_index, paper)

            # Evaluate signal
            params = risk.evaluate(signal, prob, atr, chop_index)
            if params is None:
                continue

            if paper:
                if not paper_tracker.is_open:
                    paper_tracker.open(
                        params.direction, price,
                        params.sl_points, params.tp_points,
                        params.lots, bar_time, sw
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
    log.info("Scalper stopped.")


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
        feed.connect()
        df     = feed.get_bars(n=cfg["data"]["lookback_bars"])
        htf_df = feed.get_htf_bars(n=400)
        trainer.train(df, htf_df, cfg)
        feed.disconnect()
    else:
        run(cfg, paper=args.paper, dashboard=args.dashboard)
