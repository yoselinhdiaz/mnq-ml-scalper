"""
model/retrain.py
Periodic retraining using accumulated live data.
Runs in a background thread, swaps model atomically.
"""

import logging
import threading
import time
from datetime import datetime

import pandas as pd

from model.train import _make_model, _save, load
from features.pipeline import build_features, make_labels
from sklearn.preprocessing import StandardScaler

log = logging.getLogger(__name__)


class RetrainScheduler:
    def __init__(self, cfg: dict, feed, shared_state: dict):
        """
        cfg          : full config dict
        feed         : MT5Feed instance
        shared_state : dict with keys 'model' and 'scaler' (shared with main loop)
        """
        self.cfg          = cfg
        self.feed         = feed
        self.state        = shared_state
        self.interval_h   = cfg["data"]["retrain_interval_hours"]
        self._stop_event  = threading.Event()
        self._thread      = None
        self._last_retrain = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info("Retrain scheduler started (every %dh)", self.interval_h)

    def stop(self):
        self._stop_event.set()
        # daemon=True so the thread dies with the process — no join needed

    def _loop(self):
        while not self._stop_event.is_set():
            time.sleep(60)  # check every minute
            now = datetime.utcnow()
            if self._should_retrain(now):
                self._retrain()
                self._last_retrain = now

    def _should_retrain(self, now: datetime) -> bool:
        if self._last_retrain is None:
            return False  # skip first run — model already trained at startup
        elapsed_h = (now - self._last_retrain).total_seconds() / 3600
        return elapsed_h >= self.interval_h

    def _retrain(self):
        log.info("Starting scheduled retrain…")
        try:
            w         = self.cfg["data"]["feature_window"]
            lookahead = self.cfg["model"]["label_lookahead"]
            thresh    = self.cfg["model"]["label_threshold_atr"]
            lookback  = self.cfg["data"]["lookback_bars"]

            df      = self.feed.get_bars(n=lookback)
            htf_df  = self.feed.get_htf_bars(n=200)
            if df is None or htf_df is None:
                log.warning("Retrain skipped — could not fetch bars")
                return

            features = build_features(df, htf_df, window=w)
            labels   = make_labels(df, lookahead=lookahead, threshold_atr=thresh)

            common = features.index.intersection(labels.index)
            X = features.loc[common].values
            y = labels.loc[common].map({-1: 0, 0: 1, 1: 2}).values

            import numpy as np
            counts = np.bincount(y)
            log.info("Retrain dataset: %d samples | dist: %s", len(y), counts.tolist())

            scaler  = StandardScaler()
            X_scaled = scaler.fit_transform(X)
            model   = _make_model()
            model.fit(X_scaled, y)

            # Atomic swap (GIL protects dict assignment)
            self.state["model"]  = model
            self.state["scaler"] = scaler
            _save(model, scaler)

            log.info("Retrain complete — model swapped")

        except Exception as e:
            log.exception("Retrain failed: %s", e)
