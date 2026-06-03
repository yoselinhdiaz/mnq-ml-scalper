"""
check/verify_install.py
Verifica que la instalacion este completa y el modelo/historial esten intactos.
"""
import sys
import os

print("=" * 55)
print("  US100 ML Scalper — Verificacion de instalacion")
print("=" * 55)

errors   = 0
warnings = 0

# ── 1. Dependencias Python ──────────────────────────────────
print("\n[1] Dependencias Python")
deps = ["numpy", "pandas", "lightgbm", "sklearn", "joblib",
        "yaml", "MetaTrader5", "scipy"]
for dep in deps:
    mod = dep if dep != "sklearn" else "sklearn"
    try:
        __import__(mod)
        print(f"  OK  {dep}")
    except ImportError:
        print(f"  FALTA  {dep}  <- pip install {dep}")
        errors += 1

# ── 2. Archivos criticos ────────────────────────────────────
print("\n[2] Archivos criticos")
files = {
    "config.yaml"         : "Configuracion",
    "logs/model.joblib"   : "Modelo entrenado",
    "logs/scaler.joblib"  : "Scaler",
    "logs/scalper.db"     : "Base de datos historica",
    "main.py"             : "Bot principal",
    "run.ps1"             : "Watchdog",
    "stop.ps1"            : "Script de parada",
}
for path, label in files.items():
    size = os.path.getsize(path) / 1024 if os.path.exists(path) else 0
    if os.path.exists(path):
        print(f"  OK  {label:30s} ({size:.0f} KB)")
    else:
        print(f"  FALTA  {label}")
        errors += 1

# ── 3. Modelo ───────────────────────────────────────────────
print("\n[3] Modelo entrenado")
try:
    import joblib, numpy as np
    model  = joblib.load("logs/model.joblib")
    scaler = joblib.load("logs/scaler.joblib")
    n_feat  = model.n_features_in_
    n_est   = model.n_estimators
    print(f"  OK  LightGBM — {n_est} estimadores, {n_feat} features")
    print(f"  OK  Scaler    — {scaler.n_features_in_} features")
    if n_feat != scaler.n_features_in_:
        print(f"  ADVERTENCIA: modelo ({n_feat}) y scaler ({scaler.n_features_in_}) no coinciden")
        warnings += 1
except Exception as e:
    print(f"  ERROR: {e}")
    errors += 1

# ── 4. Base de datos ────────────────────────────────────────
print("\n[4] Base de datos historica")
try:
    import sqlite3
    conn = sqlite3.connect("logs/scalper.db")
    bars   = conn.execute("SELECT COUNT(*) FROM bars").fetchone()[0]
    trades = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    first  = conn.execute("SELECT MIN(time) FROM bars").fetchone()[0]
    last   = conn.execute("SELECT MAX(time) FROM bars").fetchone()[0]
    conn.close()
    print(f"  OK  {bars:,} barras ({first[:10]} a {last[:10]})")
    print(f"  OK  {trades} trades registrados")
    if bars < 50000:
        print(f"  ADVERTENCIA: pocas barras, el modelo puede ser menos preciso")
        warnings += 1
except Exception as e:
    print(f"  ERROR: {e}")
    errors += 1

# ── 5. Conexion MT5 ─────────────────────────────────────────
print("\n[5] Conexion MetaTrader 5")
try:
    import yaml, MetaTrader5 as mt5
    cfg = yaml.safe_load(open("config.yaml"))
    ok  = mt5.initialize(
        login    = cfg["mt5"]["login"],
        password = cfg["mt5"]["password"],
        server   = cfg["mt5"]["server"],
    )
    if ok:
        info = mt5.account_info()
        print(f"  OK  Cuenta {info.login} | Balance ${info.balance:,.2f}")
        mt5.shutdown()
    else:
        print(f"  FALLO: {mt5.last_error()} (verifica que MT5 este abierto)")
        warnings += 1
except Exception as e:
    print(f"  ERROR: {e}")
    warnings += 1

# ── 6. Tareas programadas ───────────────────────────────────
print("\n[6] Tareas programadas Windows")
import subprocess
result = subprocess.run(
    ["powershell", "-Command",
     "Get-ScheduledTask -TaskName 'MNQ-Bot-*' | Select-Object TaskName,State | ConvertTo-Csv -NoTypeInformation"],
    capture_output=True, text=True
)
if "MNQ-Bot-Start" in result.stdout:
    for line in result.stdout.strip().split("\n")[1:]:
        parts = line.replace('"','').split(",")
        if len(parts) == 2:
            print(f"  OK  {parts[0]:20s} ({parts[1]})")
else:
    print("  FALTA: tareas no instaladas — ejecuta .\\install-scheduler.ps1 como Admin")
    warnings += 1

# ── 7. Puerto dashboard ─────────────────────────────────────
print("\n[7] Dashboard puerto 8765")
import socket
try:
    s = socket.create_connection(("localhost", 8765), timeout=2)
    s.close()
    print("  OK  http://localhost:8765 accesible")
except:
    print("  FALLO: puerto 8765 no responde")
    print("  Fix:   New-NetFirewallRule -DisplayName 'MNQ Bot Dashboard' -Direction Inbound -Protocol TCP -LocalPort 8765 -Action Allow")
    warnings += 1

# ── Resumen ──────────────────────────────────────────────────
print("\n" + "=" * 55)
if errors == 0 and warnings == 0:
    print("  INSTALACION COMPLETA — todo listo para operar")
elif errors == 0:
    print(f"  LISTA CON {warnings} ADVERTENCIA(S) — revisa los items arriba")
else:
    print(f"  {errors} ERROR(ES) — corrige antes de arrancar el bot")
print("=" * 55)
