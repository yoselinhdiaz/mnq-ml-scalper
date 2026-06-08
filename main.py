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
            self._session_close()
            self._check()
            self._stop.wait(1)

    def _session_close(self):
        """Close all positions 30 seconds before session ends."""
        sessions = self.cfg["risk"].get("allowed_sessions", [])
        if not sessions or not self.open_tickets:
            return
        from datetime import datetime, timezone
        now    = datetime.now(timezone.utc)
        utc_h  = now.hour + now.minute / 60 + now.second / 3600
        buffer = 30 / 3600  # 30 seconds
        in_safe = any(s <= utc_h < e - buffer for s, e in sessions)
        if not in_safe:
            import logging
            log = logging.getLogger(__name__)
            log.info("Session ending -- closing all positions (30s rule)")
            self.sender.close_all_positions()
            self.open_tickets.clear()

    def _check(self):
        trail_lock_usd  = self.cfg["risk"].get("trail_lock_usd", 20.0)  # lock this much behind max profit
        trigger_usd     = self.cfg["risk"].get("breakeven_min_profit_usd", 20.0)
        tick_value      = self.feed.get_tick_value()
        point           = self.feed.get_point()
        contract_sz     = self.cfg["mt5"].get("contract_size", 1)

        if tick_value == 0 or point == 0:
            return

        for ticket, info in list(self.open_tickets.items()):
            pos_info = self.sender.get_position_info(ticket)
            if pos_info is None:
                continue

            profit           = pos_info["profit"]
            direction        = info["direction"]
            entry            = pos_info["entry"]
            lots             = pos_info["lots"]
            spread_cost      = info.get("spread_cost", 0.0)
            profit_per_point = lots * (tick_value / point) * contract_sz
            if profit_per_point == 0:
                continue

            # Only start trailing once profit reaches trigger
            if profit < trigger_usd + spread_cost:
                continue

            # Track max profit seen
            max_profit = info.get("max_profit", profit)
            if profit > max_profit:
                info["max_profit"] = profit
                max_profit = profit

            # SL = price that locks in (max_profit - trail_lock_usd)
            locked_profit = max(0.0, max_profit - trail_lock_usd)
            lock_pts      = locked_profit / profit_per_point
            new_sl        = round(entry + lock_pts * direction, 2)

            # Only move SL forward (never backward)
            import MetaTrader5 as _mt5
            positions = _mt5.positions_get(ticket=ticket)
            if not positions:
                continue
            current_sl = positions[0].sl
            should_move = (direction == 1  and new_sl > current_sl + point) or \
                          (direction == -1 and new_sl < current_sl - point)
            if should_move:
                if self.sender.modify_sl(ticket, new_sl):
                    info["be_done"] = True
                    log.info("Trail SL | ticket=%d | max_profit=%.2f | locked=%.2f | sl=%.2f",
                             ticket, max_profit, locked_profit, new_sl)


# ------------------------------------------------------------------ #
#  MT5 reconciliation                                                  #
# ------------------------------------------------------------------ #

def _reconcile_with_mt5(db, cfg: dict):
    """
    Sync scalper.db with actual MT5 deal history.
    Corrects entry/exit prices and PnL for all live trades that have a ticket.
    Also inserts any MT5 deals for this magic number not yet in the DB.
    """
    import MetaTrader5 as _mt5
    from datetime import datetime, timezone, timedelta

    magic       = cfg["mt5"]["magic"]
    symbol      = cfg["mt5"]["symbol"]
    since       = datetime.now(timezone.utc) - timedelta(days=30)
    deals       = _mt5.history_deals_get(since, datetime.now(timezone.utc))
    if not deals:
        return

    # Group deals by position ticket
    by_position: dict = {}
    for d in deals:
        if d.magic != magic or d.symbol != symbol:
            continue
        by_position.setdefault(d.position_id, []).append(d)

    known_tickets = db.get_live_tickets()
    updated = inserted = 0

    for pos_id, pos_deals in by_position.items():
        pos_deals.sort(key=lambda d: d.time)
        open_deal  = next((d for d in pos_deals if d.entry == 0), None)
        close_deal = next((d for d in pos_deals if d.entry == 1), None)
        if not close_deal:
            continue  # position still open

        entry_price = open_deal.price  if open_deal  else 0.0
        exit_price  = close_deal.price
        pnl         = sum(d.profit for d in pos_deals)
        open_time   = str(datetime.fromtimestamp(open_deal.time))  if open_deal  else ""
        close_time  = str(datetime.fromtimestamp(close_deal.time))
        lots        = close_deal.volume
        reason      = "TP" if close_deal.reason == 3 else \
                      "SL" if close_deal.reason == 4 else "MT5"
        direction   = "LONG" if (open_deal and open_deal.type == 0) else "SHORT"

        if pos_id in known_tickets:
            rows = db.reconcile_trade(pos_id, entry_price, exit_price,
                                      pnl, open_time, close_time, lots, reason)
            if rows:
                updated += 1
        else:
            # Deal executed outside current bot session — insert it
            db.save_trade(
                open_time=open_time, close_time=close_time,
                direction=direction, entry=entry_price, exit=exit_price,
                sl=0.0, tp=0.0, lots=lots, pnl=pnl,
                reason=reason, paper=False, ticket=pos_id,
            )
            inserted += 1

    if updated or inserted:
        log.info("MT5 reconcile: %d updated, %d inserted", updated, inserted)


# ------------------------------------------------------------------ #
#  Main loop                                                           #
# ------------------------------------------------------------------ #

def run(cfg: dict, paper: bool = False, dashboard: bool = False):
    from dashboard.state import StateWriter
    from model.train import _resolve_device

    device = _resolve_device(cfg["model"].get("device", "auto"))

    feed   = MT5Feed(cfg)
    risk   = RiskManager(cfg, feed)
    sender = OrderSender(cfg, feed)
    sw     = StateWriter(device=device)
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
    mtf_history:       list  = []
    daily_close_done:  set   = set()  # tracks dates where daily close was executed
    last_reconcile:    float = 0.0    # timestamp of last MT5 reconciliation
    RECONCILE_EVERY          = 3600   # reconcile every 60 min
    MTF_CONFIRM  = 3         # bars the trend must hold before allowing entry

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

            # Force-close all positions when session ends
            from datetime import datetime, timezone, date as dt_date
            now_utc    = datetime.now(timezone.utc)
            close_hour = cfg["risk"].get("daily_close_utc", 20)
            today      = dt_date.today()
            if (now_utc.weekday() < 5 and
                now_utc.hour == close_hour and
                now_utc.minute >= 59 and
                today not in daily_close_done):
                log.info("Daily close 4:59 PM ET -- closing all positions")
                sender.close_all_positions()
                for t in list(open_tickets.keys()):
                    del open_tickets[t]
                daily_close_done.add(today)
                risk._daily_trades = 0


            # Check paper SL/TP
            if paper and paper_tracker.is_open:
                paper_tracker.check(high, low, price, bar_time, sw, db)

            # Detect live positions closed by MT5 (SL/TP hit)
            if not paper:
                open_pos_tickets = {p.ticket for p in sender.get_open_positions()}
                closed = [t for t in open_tickets if t not in open_pos_tickets]
                for t in closed:
                    info = open_tickets[t]
                    # Recover real prices and PnL from MT5 deal history
                    import MetaTrader5 as _mt5
                    deals = _mt5.history_deals_get(position=t)
                    if deals:
                        open_deal  = next((d for d in deals if d.entry == 0), None)  # DEAL_ENTRY_IN
                        close_deal = next((d for d in deals if d.entry == 1), None)  # DEAL_ENTRY_OUT
                        entry_price = open_deal.price  if open_deal  else info["entry"]
                        exit_price  = close_deal.price if close_deal else 0.0
                        open_time   = str(datetime.fromtimestamp(open_deal.time))  if open_deal  else str(info.get("open_time", bar_time))
                        close_time  = str(datetime.fromtimestamp(close_deal.time)) if close_deal else bar_time
                        lots        = close_deal.volume if close_deal else info.get("lots", 0.0)
                        reason      = "TP" if (close_deal and close_deal.reason == 3) else \
                                      "SL" if (close_deal and close_deal.reason == 4) else "MT5"
                    else:
                        entry_price = info["entry"]
                        exit_price  = 0.0
                        open_time   = str(info.get("open_time", bar_time))
                        close_time  = bar_time
                        lots        = info.get("lots", 0.0)
                        reason      = "MT5"
                    pnl = sum(d.profit for d in deals) if deals else 0.0
                    risk.record_trade_close(pnl, direction=info["direction"], reason=reason)
                    db.save_trade(
                        open_time  = open_time,
                        close_time = close_time,
                        direction  = "LONG" if info["direction"] == 1 else "SHORT",
                        entry      = entry_price,
                        exit       = exit_price,
                        sl         = 0.0,
                        tp         = 0.0,
                        lots       = lots,
                        pnl        = pnl,
                        reason     = reason,
                        paper      = False,
                        ticket     = t,
                    )
                    log.info("Trade closed | ticket=%d | entry=%.2f | exit=%.2f | pnl=%.2f | reason=%s",
                             t, entry_price, exit_price, pnl, reason)
                    del open_tickets[t]

            sw.on_bar(bar_time, price, signal, prob, atr, chop_index, paper)

            # Periodic reconciliation with MT5 deal history
            if not paper and (time.time() - last_reconcile) >= RECONCILE_EVERY:
                _reconcile_with_mt5(db, cfg)
                last_reconcile = time.time()

            mtf_trend = feed.get_mtf_trend()
            mtf_history.append(mtf_trend)
            if len(mtf_history) > MTF_CONFIRM:
                mtf_history.pop(0)

            # Solo usar el trend si se mantiene estable N barras consecutivas
            stable_trend = mtf_trend if (
                len(mtf_history) == MTF_CONFIRM and
                all(t == mtf_trend for t in mtf_history)
            ) else 0

            if stable_trend != 0:
                log.info("MTF trend: %s (confirmed %d bars)",
                         "BULLISH" if stable_trend == 1 else "BEARISH", MTF_CONFIRM)

            params = risk.evaluate(signal, prob, atr, chop_index, stable_trend)
            if params is None:
                continue

            # Hard block: confirmed trend must agree with entry direction
            if stable_trend != 0 and params.direction != stable_trend:
                log.debug("Entry blocked: %s vs confirmed %s trend",
                          "LONG" if params.direction == 1 else "SHORT",
                          "BULLISH" if stable_trend == 1 else "BEARISH")
                continue

            # Bar momentum check: last 2 closes must support the direction
            # Blocks entries against immediate price recovery/reversal
            if len(df) >= 3:
                last2_bullish = df["close"].iloc[-1] > df["close"].iloc[-2] > df["close"].iloc[-3]
                last2_bearish = df["close"].iloc[-1] < df["close"].iloc[-2] < df["close"].iloc[-3]
                if params.direction == 1 and last2_bearish:
                    log.debug("Entry blocked: LONG but last 2 bars closing down")
                    continue
                if params.direction == -1 and last2_bullish:
                    log.debug("Entry blocked: SHORT but last 2 bars closing up")
                    continue

            # Gate: exhaustion -- don't enter when price is at BB extreme or momentum overextended
            if len(features) > 0:
                last_f   = features.iloc[-1]
                bb_pct_b = float(last_f.get("bb_pct_b", 0.5))
                r_osc    = float(last_f.get("r_osc", 50))
                if params.direction == 1 and bb_pct_b > 0.92:
                    log.debug("Entry blocked: LONG at upper BB (bb_pct_b=%.2f)", bb_pct_b)
                    continue
                if params.direction == -1 and bb_pct_b < 0.08:
                    log.debug("Entry blocked: SHORT at lower BB (bb_pct_b=%.2f)", bb_pct_b)
                    continue
                if params.direction == 1 and r_osc > 85:
                    log.debug("Entry blocked: LONG momentum overbought (r_osc=%.1f)", r_osc)
                    continue
                if params.direction == -1 and r_osc < 15:
                    log.debug("Entry blocked: SHORT momentum oversold (r_osc=%.1f)", r_osc)
                    continue

            # Gate: late entry -- don't enter after large move consumed daily range
            if len(features) > 0:
                atr_consumed   = float(last_f.get("atr_consumed", 0.0))
                bar_size_ratio = float(last_f.get("bar_size_ratio", 1.0))
                if atr_consumed > 0.75:
                    log.debug("Entry blocked: daily range %.0f%% consumed", atr_consumed * 100)
                    continue
                if bar_size_ratio < 0.4:
                    log.debug("Entry blocked: consolidation bar (bar_size_ratio=%.2f)", bar_size_ratio)
                    continue

            # HTF Supply & Demand filter -- only block near ACTIVE levels (>=2 touches)
            if len(features) > 0:
                last_f     = features.iloc[-1]
                res_active = int(last_f.get("res_active", 0))
                sup_active = int(last_f.get("sup_active", 0))
                dist_res   = float(last_f.get("dist_htf_res", 1.0))
                dist_sup   = float(last_f.get("dist_htf_sup", 1.0))
                sr_buf     = cfg["model"].get("sr_proximity_pct", 0.0005)
                if params.direction == 1 and res_active and dist_res < sr_buf:
                    log.info("Entry blocked: LONG near active HTF resistance (dist=%.4f)", dist_res)
                    continue
                if params.direction == -1 and sup_active and dist_sup < sr_buf:
                    log.info("Entry blocked: SHORT near active HTF support (dist=%.4f)", dist_sup)
                    continue

            # EMA5/21 alignment filter
            if len(features) > 0:
                ema5_above = int(features.iloc[-1].get("ema5_above21", 0))
                if params.direction == 1 and ema5_above == 0:
                    log.debug("Entry blocked: LONG but EMA5 below EMA21")
                    continue
                if params.direction == -1 and ema5_above == 1:
                    log.debug("Entry blocked: SHORT but EMA5 above EMA21")
                    continue

            # Pullback filter: price must have touched EMA21 or VWAP in last 3 bars
            if len(features) > 0:
                pb_ema  = int(features.iloc[-1].get("pullback_ema21", 0))
                pb_vwap = int(features.iloc[-1].get("pullback_vwap", 0))
                if not (pb_ema or pb_vwap):
                    log.debug("Entry blocked: no pullback to EMA21 or VWAP in last 3 bars")
                    continue

            # RSI7 exhaustion filter
            if len(features) > 0:
                rsi7 = float(features.iloc[-1].get("rsi_7", 50))
                if params.direction == 1 and rsi7 > 75:
                    log.debug("Entry blocked: LONG with RSI7 overbought (%.1f)", rsi7)
                    continue
                if params.direction == -1 and rsi7 < 25:
                    log.debug("Entry blocked: SHORT with RSI7 oversold (%.1f)", rsi7)
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
                            flip_dir = open_tickets[t].get("direction", 0)
                            risk.record_trade_close(pnl, direction=flip_dir, reason="FLIP")
                            del open_tickets[t]
                            log.info("Momentum flip: closed %s ticket=%d pnl=%.2f",
                                     "LONG" if -params.direction == 1 else "SHORT", t, pnl)
                    else:
                        # Low confidence: don't open — would create opposite positions
                        log.debug("Trade blocked: opposite position open, confidence %.3f < flip threshold %.3f",
                                  prob, flip_threshold)
                        continue

                ticket, fill_price = sender.open_position(params)
                if ticket:
                    # Spread cost in USD: spread_pts (in ticks) × tick_value × lots × contract_size
                    spread_pts   = feed.get_spread_points()   # in ticks (e.g. 280 ticks)
                    tick_value   = feed.get_tick_value()       # USD per tick per lot
                    contract_sz  = cfg["mt5"].get("contract_size", 1)
                    spread_cost  = spread_pts * tick_value * params.lots * contract_sz

                    open_tickets[ticket] = {
                        "direction":   params.direction,
                        "entry":       fill_price or price,   # real MT5 fill price
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

    # Close all open positions before shutdown
    if not paper:
        try:
            sender.close_all_positions()
        except Exception:
            pass
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
