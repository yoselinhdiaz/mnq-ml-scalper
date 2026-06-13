"""
model/train.py
Initial training with walk-forward cross-validation.
Saves model + scaler to disk.
"""

import logging
import os
from typing import Tuple

import joblib
import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.metrics import classification_report
from sklearn.preprocessing import StandardScaler
from sklearn.utils.class_weight import compute_sample_weight

import features
from features.pipeline import build_features, make_labels

log = logging.getLogger(__name__)

MODEL_PATH  = "logs/model.joblib"
SCALER_PATH = "logs/scaler.joblib"
MODEL_VERSIONS_MAX = 3  # Mantener los últimos N modelos versionados


# ------------------------------------------------------------------ #
#  Public API                                                          #
# ------------------------------------------------------------------ #


def train(
    df: pd.DataFrame, htf_df: pd.DataFrame, cfg: dict, db=None
) -> Tuple[xgb.XGBClassifier, StandardScaler]:
    """
    Full training pipeline:
      1. Build features + labels (uses DB if available and has more data)
      2. Walk-forward validation (print metrics)
      3. Final fit on all data
      4. Save model + scaler
    """
    w         = cfg["data"]["feature_window"]
    lookahead = cfg["model"]["label_lookahead"]
    thresh    = cfg["model"]["label_threshold_atr"]
    mom_bars  = cfg["model"].get("label_momentum_bars", 5)

    # Use DB bars if df is None or DB has more data
    if db is not None and (df is None or db.bar_count() > len(df)):
        log.info("Using DB bars for training (%d bars)", db.bar_count())
        df = db.load_bars_df(limit=100000)
        htf_df = None    # ← agrega esta línea

    if df is None or len(df) == 0:
        raise RuntimeError("No bar data available for training")

    # Filter to active trading sessions only (same hours the bot will trade)
    sessions = cfg["risk"].get("allowed_sessions", [])
    if sessions:
        # DB timestamps are in broker time (UTC+offset); allowed_sessions is UTC
        utc_offset = cfg.get("mt5", {}).get("broker_utc_offset", 3)
        def in_session(ts):
            broker_h = ts.hour
            return any((s * 1.0 + utc_offset) % 24 <= broker_h < (e * 1.0 + utc_offset) % 24
                       for s, e in sessions)
        before = len(df)
        df = df[df.index.to_series().apply(in_session)]
        log.info("Session filter: kept %d / %d bars (08:00-22:00 broker time)", len(df), before)

    log.info("Building features from %d bars...", len(df))
    log.info("Date range: %s to %s", df.index[0], df.index[-1])

    features = build_features(df, htf_df, window=w)
    log.info(
        "Features after dropna: %d rows (dropped %d)",
        len(features),
        len(df) - len(features),
    )

    # Compute labels only on rows that survived feature dropna
    df_clean = df.loc[features.index]
    labels = make_labels(df_clean, lookahead=lookahead, threshold_atr=thresh, momentum_bars=mom_bars)

    log.info(
        "Labels: LONG=%d SHORT=%d SKIP=%d",
        (labels == 1).sum(),
        (labels == -1).sum(),
        (labels == 0).sum(),
    )

    # Align — now both have same index, intersection is trivial
    common = features.index.intersection(labels.index)
    X = features.loc[common].values
    y = labels.loc[common].map({-1: 0, 0: 1, 1: 2}).values  # LGB needs 0-indexed

    log.info("Dataset: %d samples | class dist: %s", len(y), np.bincount(y).tolist())

    device = _resolve_device(cfg["model"].get("device", "auto"))

    # Walk-forward validation
    _walk_forward(X, y, device, n_splits=5)

    # Final model on all data
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    model = _make_model(device)
    model.fit(X_scaled, y, sample_weight=_compute_weights(y))

    _save(model, scaler)
    log.info("Model saved → %s", MODEL_PATH)
    return model, scaler


def load() -> Tuple[xgb.XGBClassifier, StandardScaler]:
    model = joblib.load(MODEL_PATH)
    scaler = joblib.load(SCALER_PATH)
    return model, scaler


def model_exists() -> bool:
    return os.path.exists(MODEL_PATH) and os.path.exists(SCALER_PATH)


# ------------------------------------------------------------------ #
#  Walk-forward validation                                             #
# ------------------------------------------------------------------ #


def _walk_forward(X: np.ndarray, y: np.ndarray, device: str, n_splits: int = 5):
    fold_size = len(X) // (n_splits + 1)
    reports = []

    for i in range(n_splits):
        train_end = fold_size * (i + 1)
        test_end = train_end + fold_size

        X_tr, y_tr = X[:train_end], y[:train_end]
        X_te, y_te = X[train_end:test_end], y[train_end:test_end]

        scaler = StandardScaler()
        X_tr_s = scaler.fit_transform(X_tr)
        X_te_s = scaler.transform(X_te)

        m = _make_model(device)
        m.fit(X_tr_s, y_tr, sample_weight=_compute_weights(y_tr))
        preds = m.predict(X_te_s)

        report = classification_report(
            y_te,
            preds,
            target_names=["SHORT", "SKIP", "LONG"],
            output_dict=True,
            zero_division=0,
        )
        reports.append(report)
        log.info(
            "Fold %d/%d — LONG f1: %.3f | SHORT f1: %.3f | acc: %.3f",
            i + 1,
            n_splits,
            report["LONG"]["f1-score"],
            report["SHORT"]["f1-score"],
            report["accuracy"],
        )

    avg_acc = np.mean([r["accuracy"] for r in reports])
    log.info("Walk-forward avg accuracy: %.3f", avg_acc)


# ------------------------------------------------------------------ #
#  Internal helpers                                                    #
# ------------------------------------------------------------------ #


def _resolve_device(device: str) -> str:
    if device in ("cuda", "cpu"):
        return device
    try:
        xgb.XGBClassifier(n_estimators=1, device="cuda", verbosity=0).fit(
            [[1, 2], [3, 4]], [0, 1]
        )
        log.info("Device: cuda (GPU detected)")
        return "cuda"
    except Exception:
        log.warning("CUDA not available -- falling back to CPU")
        return "cpu"


def _make_model(device: str = "cpu") -> xgb.XGBClassifier:
    return xgb.XGBClassifier(
        n_estimators=600,
        learning_rate=0.03,
        max_depth=7,
        subsample=0.75,
        colsample_bytree=0.75,
        reg_alpha=0.1,
        reg_lambda=0.2,
        objective="multi:softprob",
        num_class=3,
        tree_method="hist",
        device=device,
        eval_metric="mlogloss",
        n_jobs=-1,
        verbosity=0,
    )


def _compute_weights(y: np.ndarray) -> np.ndarray:
    return compute_sample_weight("balanced", y)


def _save(model, scaler):
    import glob as _glob
    from datetime import datetime as _dt
    os.makedirs("logs", exist_ok=True)
    joblib.dump(model, MODEL_PATH)
    joblib.dump(scaler, SCALER_PATH)

    # Guardar copia versionada y limpiar versiones antiguas
    ts = _dt.utcnow().strftime("%Y%m%d_%H%M%S")
    joblib.dump(model,  f"logs/model_{ts}.joblib")
    joblib.dump(scaler, f"logs/scaler_{ts}.joblib")

    for pattern, keep in (("logs/model_????????_??????.joblib", MODEL_VERSIONS_MAX),
                           ("logs/scaler_????????_??????.joblib", MODEL_VERSIONS_MAX)):
        files = sorted(_glob.glob(pattern))
        for old in files[:-keep]:
            try:
                os.remove(old)
            except OSError:
                pass
