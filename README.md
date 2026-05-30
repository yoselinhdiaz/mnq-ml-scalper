# MNQ ML Scalper

Real-time ML scalping bot for MNQ (Micro E-mini Nasdaq-100) using MetaTrader 5.

## Architecture

```
MT5 Feed → Feature Pipeline → LightGBM Classifier → Risk Manager → MT5 Execution
```

- **Model**: LightGBM 3-class classifier (LONG / SHORT / SKIP)
- **Features**: 30-bar rolling window — momentum, volatility, microstructure, session context
- **TP/SL**: Dynamic, ATR-based regression model
- **Retraining**: Auto every 4 hours using live data

## Setup

```bash
pip install -r requirements.txt
```

Edit `config.yaml` with your MT5 credentials and risk parameters, then:

```bash
python main.py
```

## Project Structure

```
mnq-ml-scalper/
├── main.py                  # Entry point, main loop
├── config.yaml              # All parameters (credentials, risk, model)
├── requirements.txt
├── data/
│   └── mt5_feed.py          # MT5 connection, bar + tick collection
├── features/
│   └── pipeline.py          # Feature engineering (momentum, vol, microstructure)
├── model/
│   ├── train.py             # Initial training + walk-forward validation
│   └── retrain.py           # Online retraining scheduler
├── execution/
│   ├── risk_manager.py      # Position sizing, daily loss limit, trade filter
│   └── order_sender.py      # MT5 order placement + management
└── logs/                    # Trade log, model metrics
```

## Risk Controls

- Max 2 simultaneous trades
- Daily loss limit (configurable)
- Min confidence threshold: 0.62
- Chop filter: blocks entries in ranging markets
- SL = ATR × 1.2 | TP = dynamic via regression model
