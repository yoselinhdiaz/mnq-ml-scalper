"""
model/retrain.py
Periodic retraining. Uses DB bars when available, falls back to MT5 feed.
"""

import logging
import threading
import time
from datetime import datetime

from model.train import _make_model, _save, load
from features.pipeline import build_features, make_labels
from sklearn.preprocessing import StandardScaler

log = logging.getLogger(__name__)


class RetrainScheduler:
    def __init__(self, cfg: dict, feed, shared_state: dict, db=None):
        self.cfg           = cfg
        self.feed          = feed
        self.state         = shared_state
        self.db            = db
        self.interval_h    = cfg["data"]["retrain_interval_hours"]
        self._stop_event   = threading.Event()
        self._thread       = None
        self._last_retrain = datetime.utcnow()  # start clock from boot

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info("Retrain scheduler started (every %dh)", self.interval_h)

    def stop(self):
        self._stop_event.set()

    def _loop(self):
        while not self._stop_event.is_set():
            time.sleep(60)
            now = datetime.utcnow()
            if self._should_retrain(now):
                self._retrain()
                self._last_retrain = now

    def _should_retrain(self, now: datetime) -> bool:
        elapsed_h = (now - self._last_retrain).total_seconds() / 3600
        return elapsed_h >= self.interval_h

    def _retrain(self):
        log.info("Starting scheduled retrain...")
        try:
            w         = self.cfg["data"]["feature_window"]
            lookahead = self.cfg["model"]["label_lookahead"]
            thresh    = self.cfg["model"]["label_threshold_atr"]
            lookback  = self.cfg["data"]["lookback_bars"]

            # Prefer DB bars if we have enough accumulated
            df = None
            if self.db and self.db.bar_count() > lookback:
                log.info("Retraining from DB (%d bars)", self.db.bar_count())
                df = self.db.load_bars_df(limit=100000)
            
            if df is None:
                df = self.feed.get_bars(n=lookback)

            if df is None:
                log.warning("Retrain skipped - could not fetch bars")
                return

            features = build_features(df, None, window=w)
            labels   = make_labels(df, lookahead=lookahead, threshold_atr=thresh)

            common = features.index.intersection(labels.index)
            X = features.loc[common].values
            y = labels.loc[common].map({-1: 0, 0: 1, 1: 2}).values

            import numpy as np
            counts = np.bincount(y)
            log.info("Retrain dataset: %d samples | dist: %s", len(y), counts.tolist())

            scaler   = StandardScaler()
            X_scaled = scaler.fit_transform(X)
            model    = _make_model()
            model.fit(X_scaled, y)

            self.state["model"]  = model
            self.state["scaler"] = scaler
            _save(model, scaler)
            log.info("Retrain complete - model swapped")

        except Exception as e:
            log.exception("Retrain failed: %s", e)
