# US100 ML Scalper

Bot de scalping en tiempo real para US100 / MNQ (Nasdaq-100) usando MetaTrader 5 y LightGBM. Incluye dashboard web multi-sección, watchdog automático y reconciliación con MT5.

---

## Arquitectura

```
MT5 Feed → Feature Pipeline → LightGBM (3 clases) → Filtros de entrada → MT5 Execution
                ↑                                                               ↓
          SQLite DB (bars, signals, trades)  ←──── Reconciliación MT5 (cada 1h)
                ↓
          Dashboard Web (puerto 8765)
```

**Componentes principales:**

| Módulo | Descripción |
|---|---|
| `main.py` | Loop principal, filtros de entrada, gestión de posiciones |
| `data/mt5_feed.py` | Conexión MT5, barras M5/M15, ticks, reconexión automática |
| `data/database.py` | SQLite — bars, signals, trades + reconciliación |
| `features/pipeline.py` | Pipeline de features (momentum, volatilidad, estructura, HTF) |
| `model/train.py` | Entrenamiento LightGBM + walk-forward validation |
| `model/retrain.py` | Re-entrenamiento automático en background cada 4h |
| `execution/risk_manager.py` | Sizing por riesgo USD, límite diario, filtros chop/sesión |
| `execution/order_sender.py` | Apertura/cierre órdenes MT5 con precios de fill reales |
| `dashboard/server.py` | Servidor HTTP (stdlib), SPA multi-sección en puerto 8765 |
| `dashboard/state.py` | Escribe `logs/state.json` en cada barra (2 s de latencia) |
| `run.ps1` | Watchdog PowerShell — arranca/reinicia bot y MT5 automáticamente |
| `install-scheduler.ps1` | Registra `run.ps1` en Windows Task Scheduler (cada 5 min) |

---

## Setup

### 1. Instalar dependencias

```bash
pip install -r requirements.txt
```

### 2. Configurar credenciales

Copia y edita `config.yaml`:

```yaml
mt5:
  login: 202036
  password: "tu_password"
  server: "GNTCapital-Demo"
  symbol: "US100."         # incluye el punto si tu broker lo requiere
  timeframe: "M5"
  htf_timeframe: "M15"
  magic: 20250101
  contract_size: 10        # multiplicador del broker (verificado empíricamente)
```

### 3. Activar trading algorítmico en MT5

`Tools → Options → Expert Advisors → Allow algorithmic trading`

---

## Uso

```bash
# Solo entrenar modelo con histórico y salir
python main.py --train-only

# Paper trading + dashboard
python main.py --paper --dashboard

# Live trading (bot principal)
python main.py

# Dashboard disponible en: http://localhost:8765
```

### Iniciar con Watchdog (recomendado)

El watchdog arranca automáticamente el bot y MT5, maneja reinicios y respeta el horario de mercado (Dom 5:30 PM → Vie 5:30 PM hora local).

```powershell
# Instalar como tarea programada (requiere Admin)
.\install-scheduler.ps1

# O correr manualmente
.\run.ps1
```

---

## Dashboard Web

Accesible en `http://localhost:8765`. SPA de una sola página con 4 secciones:

### Dashboard
- P&L en tiempo real, Win Rate, ATR, precio actual
- Señal activa (LONG / SHORT / SKIP) con barra de confianza y umbral
- Widget **News Guard**: próximas noticias USD de alto impacto (ForexFactory), con indicador de zona bloqueada si hay evento en < 30 min
- Historial de trades de la sesión actual
- Distribución de señales (LONG / SHORT / SKIP) y barras procesadas

### Ajustes (Settings)
Formulario para editar `config.yaml` sin tocar el archivo directamente:
- **Cuenta MT5**: login, servidor, símbolo, timeframe, magic, contract_size
- **Training**: lookback_bars, feature_window, retrain_interval_hours, min_bars_to_trade
- **Estrategia**: device (auto/cuda/cpu), confidence_threshold, sr_proximity_pct, counter_trend_boost, chop_atr_ratio, etc.
- **Risk / SL / TP / BE**: risk_per_trade_usd, sl_atr_multiplier, tp_atr_multiplier, breakeven, trail_lock_usd, daily_loss_limit_pct
- El campo `password` se muestra como `****` y no se sobreescribe si no se modifica

### Reportes
**Tab — Último Training:** métricas del último entrenamiento (fecha, accuracy walk-forward, tabla de folds con f1 por clase, distribución de labels).

**Tab — Operaciones:**
- **Hoy**: tabla paginada (10 / 20 / 50 filas por página), tarjetas Win% / Loss% / PnL neto
- **Semana**: labels resumen por semana del mes seleccionado — Win% / Loss% / PnL. Navegación mes anterior/siguiente
- **Mes**: grid de 12 meses del año seleccionado — Win% / Loss% / PnL por mes. Navegación año anterior/siguiente

### Perfil
Nombre, email, zona horaria y notas — guardado en `logs/profile.json`.

---

## Modelo ML

**Clasificador**: LightGBM 3 clases — `LONG (+1)` / `SKIP (0)` / `SHORT (-1)`

**Validación**: Walk-forward con 5 folds sobre datos históricos acumulados.

### Features por grupo

| Grupo | Features |
|---|---|
| Momentum | RSI-14, RSI-7, MACD (12/26/9), ROC-10, r_osc (oscilador rápido), EMA 5/9/21/50 alignment |
| EMA Pullback | ema5_above21, ema5_slope, dist_ema5, pullback_ema21, pullback_vwap, near_ema21 |
| Volatilidad | ATR-14, BB %b, BB ancho, chop_index, atr_consumed (% rango diario consumido) |
| Microestructura | VWAP deviation, vol_delta (diferencia volumen alza/baja), body_ratio, bar_size_ratio |
| Sesión | hora sin/cos, flags NYSE (09:30-16:00 ET), London (08:00-16:30 GMT) |
| Flujo institucional | MFI-14, net flow histogram, institutional bar detection, above_vwap |
| Estructura de precio | dist_kijun, price_pos_daily, atr_consumed, PDH/PDL distances |
| Mobius Scalper | Momentum de aceleración del precio (8 barras) |
| HTF Supply & Demand | Zonas de oferta/demanda en M15: dist_htf_res, dist_htf_sup, res_active, sup_active |
| Contexto HTF | MTF trend confirmado (M15): bullish/bearish/neutral |

**Labels**: Para cada barra se mira el high/low en los próximos `label_lookahead=15` barras. Si el movimiento neto supera `label_threshold_atr × ATR` con `label_momentum_bars=5` barras de momentum establecido → LONG o SHORT. Si no → SKIP.

---

## Filtros de entrada

El bot aplica múltiples capas de filtros antes de ejecutar una orden:

| Filtro | Descripción |
|---|---|
| **Confianza mínima** | `confidence_threshold = 0.45` (+ 0.25 extra si es contra-tendencia) |
| **Chop** | Bloquea si `chop_index < 0.6` (mercado lateral) |
| **Sesión** | Solo opera en `allowed_sessions = [[7, 10], [13, 17]]` UTC (Londres open + NYSE open) |
| **Límite diario** | Detiene trading si pérdida del día >= `daily_loss_limit_pct = 10%` del balance |
| **MTF Trend** | Bloquea entradas contra tendencia confirmada M15 (3 barras consecutivas) |
| **Momentum de barra** | Bloquea si los últimos 2 cierres van en contra de la dirección |
| **BB extremos** | Bloquea LONG si bb_pct_b > 0.92, SHORT si < 0.08 |
| **Momentum sobreextendido** | Bloquea si r_osc > 85 (largo) o < 15 (corto) |
| **Rango consumido** | Bloquea si > 75% del rango ATR diario ya fue consumido |
| **Barra de consolidación** | Bloquea si bar_size_ratio < 0.4 |
| **Proximidad S/R HTF** | Bloquea si el precio está a < 0.05% de una zona activa de oferta/demanda |
| **Alineación EMA 5/21** | Bloquea LONG si EMA5 < EMA21, SHORT si EMA5 > EMA21 |
| **Pullback EMA/VWAP** | Bloquea si precio no tocó EMA21 ni VWAP en las últimas 3 barras |
| **RSI7 agotamiento** | Bloquea LONG si RSI7 > 75, SHORT si RSI7 < 25 |
| **News Guard** | Bloquea en ventana de ±10/15 min alrededor de noticias USD de alto impacto |

---

## Risk Management

- **Sizing**: `lots = risk_per_trade_usd / (sl_points × tick_value × contract_size)`
- **SL**: `ATR × sl_atr_multiplier` (0.8) → ~48 pts con ATR típico de 60
- **TP**: `SL × tp_rr_ratio` (2.0) → ~96 pts, ratio 1:2 fijo
- **Breakeven/Trailing**: cuando el trade alcanza `breakeven_min_profit_usd = $20`, el SL se mueve para bloquear `trail_lock_usd = $20` de ganancia y sigue al precio (lo que llegue primero entre TP y trailing SL)
- **Máximo simultáneo**: 1 trade abierto
- **Pérdidas consecutivas**: bloqueo de dirección tras 2 SL seguidos, cooldown de 5 min
- **Cierre diario**: fuerza cierre de todas las posiciones a las `daily_close_utc = 20:00` UTC (4 PM ET)
- **Flip de dirección**: si llega señal contraria con prob >= `flip_confidence_threshold = 0.52`, cierra la posición actual y abre en sentido contrario

---

## Reconciliación con MT5

Cada 60 minutos el bot ejecuta `_reconcile_with_mt5()`:

1. Consulta `mt5.history_deals_get()` filtrado por `magic` y `symbol` de los últimos 30 días
2. Agrupa deals por `position_id` para obtener precios reales de apertura y cierre
3. **Actualiza** registros existentes en `scalper.db` con precios/PnL reales de MT5
4. **Inserta** trades que hayan quedado sin registrar (ej. si el bot reinició antes del cierre)

Además, al abrir cada orden, el bot guarda el precio de fill real de MT5 (`result.price`) en vez del precio estimado de la barra.

---

## Base de datos (logs/scalper.db)

| Tabla | Columnas principales |
|---|---|
| `bars` | time, open, high, low, close, volume |
| `signals` | time, price, signal (+1/-1/0), prob, atr, chop, features (JSON) |
| `trades` | open_time, close_time, direction, entry, exit, sl, tp, lots, pnl, reason, paper, **ticket** |

La columna `ticket` vincula cada trade de `scalper.db` con el position ticket de MT5, lo que permite reconciliación exacta.

```bash
# Diagnóstico rápido
python check/diagnose.py

# O con DB Browser for SQLite
# https://sqlitebrowser.org
```

---

## News Guard

El widget consulta ForexFactory vía proxy server-side (evita CORS):

```
https://nfs.faireconomy.media/ff_calendar_thisweek.json
```

Filtra eventos `country = "USD"` e `impact = "High"`. La respuesta se cachea 15 minutos.

Configuración en `config.yaml`:

```yaml
news:
  enabled: true
  pre_news_minutes: 10    # zona bloqueada X min antes del evento
  post_news_minutes: 15   # zona bloqueada X min después del evento
  impact_filter: "High"   # High | Medium
```

---

## Watchdog (Windows)

`run.ps1` es ejecutado por Windows Task Scheduler cada 5 minutos:

- **Horario**: Dom 5:30 PM → Vie 5:30 PM (hora local)
- Verifica si MT5 está corriendo → si no, lo inicia y espera 20 s
- Verifica si el bot está corriendo → si no, lo inicia
- Si hay duplicados del bot → los mata y reinicia uno limpio
- Al llegar el cierre de viernes → detiene bot y MT5 limpiamente
- Log en `logs/watchdog.log`

```powershell
# Instalar tarea (una sola vez, como Admin)
.\install-scheduler.ps1

# Parar todo manualmente
.\stop.ps1
```

---

## Parámetros clave (config.yaml actual)

| Parámetro | Valor | Descripción |
|---|---|---|
| `lookback_bars` | 2000 | Barras M5 a pedir en arranque (~7 días) |
| `feature_window` | 30 | Ventana rolling para features |
| `retrain_interval_hours` | 4 | Re-entrenamiento automático |
| `min_bars_to_trade` | 100 | Barras mínimas antes del primer trade |
| `label_lookahead` | 15 | Barras adelante para labels |
| `label_momentum_bars` | 5 | Barras de momentum requerido para label |
| `label_threshold_atr` | 0.4 | Movimiento mínimo = 0.4 × ATR |
| `confidence_threshold` | 0.45 | Probabilidad mínima (con tendencia) |
| `counter_trend_boost` | 0.25 | Extra de confianza requerido contra tendencia |
| `flip_confidence_threshold` | 0.52 | Prob mínima para flip de dirección |
| `sr_proximity_pct` | 0.0005 | Bloqueo S/R: < 0.05% (~15 pts) |
| `chop_atr_ratio` | 0.6 | Umbral de chop (< = lateral, no operar) |
| `risk_per_trade_usd` | 30 | Riesgo por trade en USD |
| `max_simultaneous_trades` | 1 | Máximo trades abiertos |
| `consecutive_sl_limit` | 2 | Bloqueo de dirección tras N SL consecutivos |
| `sl_cooldown_minutes` | 5 | Minutos de cooldown tras bloqueo por SL consecutivos |
| `daily_loss_limit_pct` | 10% | Stop si pérdida diaria >= 10% del balance |
| `sl_atr_multiplier` | 0.8 | SL = ATR × 0.8 (~48 pts) |
| `tp_rr_ratio` | 2.0 | TP = SL × 2.0 (~96 pts, ratio 1:2) |
| `breakeven_min_profit_usd` | 20 | Activar trailing desde $20 de ganancia |
| `trail_lock_usd` | 20 | SL bloquea $20 detrás del máximo |
| `allowed_sessions` | [[7,10],[13,17]] | Londres open (7-10 UTC) + NYSE open (13-17 UTC) |
| `daily_close_utc` | 20 | Cierre forzado 4 PM ET |

---

## Estructura del proyecto

```
mnq-ml-scalper/
├── main.py                     # Loop principal + reconciliación MT5
├── config.yaml                 # Configuración completa (sin credenciales en git)
├── requirements.txt
├── run.ps1                     # Watchdog PowerShell
├── install-scheduler.ps1       # Registra watchdog en Task Scheduler
├── stop.ps1                    # Para bot y MT5
├── backup.ps1                  # Backup de DB y modelo
├── data/
│   ├── mt5_feed.py             # Conexión MT5, barras M5/M15, ticks
│   └── database.py             # SQLite con reconciliación MT5
├── features/
│   └── pipeline.py             # 9 grupos de features + labels vectorizados
├── model/
│   ├── train.py                # LightGBM + walk-forward validation
│   └── retrain.py              # Re-entrenamiento automático en background
├── execution/
│   ├── risk_manager.py         # Sizing, daily loss limit, filtros de sesión
│   └── order_sender.py         # Órdenes MT5 con fill price real
├── dashboard/
│   ├── server.py               # HTTP server (stdlib) + SPA HTML/CSS/JS embebido
│   └── state.py                # Escribe logs/state.json cada barra
├── check/
│   └── diagnose.py             # Herramientas de diagnóstico
├── tools/
│   └── import_candles.py       # Importa histórico desde otro SQLite
└── logs/
    ├── scalper.db              # Base de datos SQLite
    ├── model.joblib            # Modelo LightGBM entrenado
    ├── scaler.joblib           # StandardScaler
    ├── state.json              # Estado en tiempo real (leído por dashboard)
    ├── trades.log              # Log de operaciones
    ├── model_metrics.log       # Métricas de entrenamiento por fold
    ├── watchdog.log            # Log del watchdog
    └── profile.json            # Perfil del usuario (dashboard)
```

---

## Importar histórico desde otro proyecto

```bash
python tools/import_candles.py --src C:/ruta/al/runtime.sqlite3 --symbol US100. --tf 1m
python main.py --train-only
```

---

## Notas broker (GNTCapital Demo)

- Símbolo: `US100.` (con punto)
- Punto: `0.1`
- Tick value: `$1.0/lote`
- Lote mínimo: `0.01`
- Contract size: `10` (multiplicador empírico verificado)
- Servidor: `GNTCapital-Demo`
