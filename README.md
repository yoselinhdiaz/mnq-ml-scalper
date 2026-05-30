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


## Accuracy 0.375 es bajo pero tiene explicación
El walk-forward está probando en períodos consecutivos del histórico, y el mercado cambia de régimen constantemente. Lo más importante es que los folds son inconsistentes:
Fold      LONG f1         SHORT f1
1         0.38            0.49
2         0.50            0.15
3         0.07            0.48
4         0.25            0.48
5         0.57            0.42

Fold 3 tiene LONG f1 de 0.07 — casi nada. Eso indica que ese período del histórico fue muy diferente al anterior (probablemente un régimen de mercado distinto — tendencia fuerte, evento macro, etc.).
Lo que esto significa en la práctica: el modelo aprende patrones de corto plazo que no siempre se repiten. Para scalping esto es normal — ningún modelo de scalping tiene accuracy estable en walk-forward largo.
Lo que podemos mejorar:
La solución más efectiva es reducir el label_lookahead — actualmente busca si el precio se mueve en 6 barras, que para scalping es mucho. Prueba con 3:

yaml
model:
  label_lookahead: 3        # era 6, bajamos a 3
  label_threshold_atr: 0.6  # era 0.8, un poco más permisivo

Y vuelve a correr --train-only. Con lookahead más corto el modelo aprende movimientos más inmediatos, que es exactamente lo que necesita el scalping. 
