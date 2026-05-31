# US100 ML Scalper

Bot de scalping en tiempo real para US100 (Nasdaq-100 CFD) usando MetaTrader 5 y LightGBM.

## Arquitectura

```
MT5 Feed → Feature Pipeline → LightGBM Classifier → Risk Manager → MT5 Execution
                ↑
          SQLite DB (histórico acumulado)
```

- **Modelo**: LightGBM 3 clases (LONG / SHORT / SKIP)
- **Features**: 24 features — momentum (RSI, MACD, ROC), volatilidad (ATR, BB, chop), microestructura (VWAP dev, vol delta, body ratio), sesión (hora cíclica sin/cos, flags NYSE/London)
- **Labels**: vectorizados sobre high/low futuro × ATR threshold
- **TP/SL**: dinámico basado en ATR
- **Re-entrenamiento**: automático cada 4h usando datos acumulados en DB

## Setup

```bash
pip install -r requirements.txt
```

Edita `config.yaml` con tus credenciales MT5:

```yaml
mt5:
  login: TU_NUMERO_DE_CUENTA
  password: "tu_password"
  server: "GNTCapital-Demo"
  symbol: "US100"
```

## Uso

```bash
# Entrenar modelo con histórico
python main.py --train-only

# Paper mode con dashboard
python main.py --paper --dashboard

# Live trading
python main.py

# Dashboard en: http://localhost:8765
```

## Importar histórico desde otro proyecto

Si tienes velas históricas en otro SQLite (`market_candles`):

```bash
python tools/import_candles.py --src C:/ruta/al/runtime.sqlite3 --symbol US100. --tf 1m
```

Luego re-entrena:

```bash
python main.py --train-only
```

## Estructura

```
us100-ml-scalper/
├── main.py                     # Entry point + loop principal
├── config.yaml                 # Credenciales, riesgo, parámetros modelo
├── requirements.txt
├── data/
│   ├── mt5_feed.py             # Conexión MT5, barras, ticks, reconexión
│   └── database.py             # SQLite — bars, signals, trades
├── features/
│   └── pipeline.py             # 24 features + make_labels vectorizado
├── model/
│   ├── train.py                # Entrenamiento + walk-forward validation
│   └── retrain.py              # Re-entrenamiento automático en background
├── execution/
│   ├── risk_manager.py         # Sizing, daily loss limit, filtros
│   └── order_sender.py         # Apertura/cierre órdenes MT5
├── dashboard/
│   ├── server.py               # HTTP server puerto 8765
│   └── state.py                # Escribe logs/state.json cada barra
├── tools/
│   └── import_candles.py       # Importa histórico desde otro SQLite
└── logs/
    ├── scalper.db              # Base de datos SQLite
    ├── model.joblib            # Modelo entrenado
    ├── scaler.joblib           # Scaler
    └── trades.log              # Log de operaciones
```

## Parámetros clave (config.yaml)

| Parámetro | Valor actual | Descripción |
|---|---|---|
| `lookback_bars` | 100000 | Barras a pedir a MT5 en cada arranque |
| `feature_window` | 30 | Ventana rolling para features |
| `label_lookahead` | 6 | Barras adelante para calcular labels |
| `label_threshold_atr` | 0.4 | Movimiento mínimo = 0.4 × ATR (~20pts) |
| `confidence_threshold` | 0.62 | Probabilidad mínima para entrar |
| `risk_per_trade_usd` | 60 | Riesgo por trade en USD |
| `sl_atr_multiplier` | 1.2 | SL = ATR × 1.2 |
| `daily_loss_limit_usd` | 120 | Stop trading si se pierden $120 en el día |
| `max_simultaneous_trades` | 2 | Máximo trades abiertos a la vez |
| `retrain_interval_hours` | 4 | Re-entrena cada 4 horas |

## Ajustes comunes

**Muchos SKIP, pocas señales:**
```yaml
model:
  confidence_threshold: 0.55   # bajar de 0.62
```

**Chop bloquea todas las entradas:**
```yaml
# En execution/risk_manager.py línea ~55
if chop > 0.7:   # subir a 1.0 para desactivar
```

**Mejorar el modelo con más datos:**
```bash
# 1. Importar más histórico
python tools/import_candles.py --src ruta/runtime.sqlite3

# 2. Re-entrenar
python main.py --train-only
```

## Base de datos (logs/scalper.db)

| Tabla | Contenido |
|---|---|
| `bars` | OHLCV de cada barra procesada |
| `signals` | Señal + features de cada barra |
| `trades` | Trades paper/live cerrados con PnL |

Para inspeccionar:
```bash
python check/diagnose.py   # estadísticas rápidas
# O instala DB Browser for SQLite: https://sqlitebrowser.org
```

## Notas broker (GNTCapital Demo)

- Símbolo: `US100`
- Punto: `0.1`
- Tick value: `$1.0/lote`
- Lote mínimo: `0.1`
- Servidor: `GNTCapital-Demo`
- Activar en MT5: `Tools → Options → Expert Advisors → Allow algorithmic trading`

## Estado actual del modelo

Entrenado con ~95,000 barras (US100 1m, Feb-May 2026):
- Dataset: 94,970 samples
- Distribución: LONG 22% / SKIP 57% / SHORT 21%
- Walk-forward avg accuracy: 0.368
- LONG f1: ~0.26 | SHORT f1: ~0.26 (consistente en 5 folds)
