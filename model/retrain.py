"""
model/retrain.py
Periodic retraining. Uses DB bars when available, falls back to MT5 feed.
"""

import logging
import threading
import time
from datetime import datetime

from model.train import _make_model, _save, _compute_weights, _resolve_device, load
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
            mom_bars  = self.cfg["model"].get("label_momentum_bars", 5)
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

            # Filter to active sessions only
            sessions = self.cfg["risk"].get("allowed_sessions", [])
            if sessions:
                utc_offset = 3
                df = df[df.index.to_series().apply(
                    lambda ts: any((s + utc_offset) % 24 <= ts.hour < (e + utc_offset) % 24
                                   for s, e in sessions)
                )]

            features = build_features(df, None, window=w)
            labels   = make_labels(df, lookahead=lookahead, threshold_atr=thresh, momentum_bars=mom_bars)

            common = features.index.intersection(labels.index)
            X = features.loc[common].values
            y = labels.loc[common].map({-1: 0, 0: 1, 1: 2}).values

            import numpy as np
            counts = np.bincount(y)
            log.info("Retrain dataset: %d samples | dist: %s", len(y), counts.tolist())

            scaler   = StandardScaler()
            X_scaled = scaler.fit_transform(X)
            device   = _resolve_device(self.cfg["model"].get("device", "auto"))
            model    = _make_model(device)
            model.fit(X_scaled, y, sample_weight=_compute_weights(y))

            self.state["model"]  = model
            self.state["scaler"] = scaler
            _save(model, scaler)
            log.info("Retrain complete - model swapped")

        except Exception as e:
            log.exception("Retrain failed: %s", e)
