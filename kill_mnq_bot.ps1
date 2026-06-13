# kill_mnq_bot.ps1
# Desconecta el bot MNQ de MT5 y mata todos sus procesos Python.
# Correr como Administrador.

Write-Host "=== MNQ Bot Shutdown ===" -ForegroundColor Cyan

# Desconectar MT5 desde Python antes de matar el proceso
$disconnectScript = @'
import MetaTrader5 as mt5
import sys
r = mt5.initialize()
if r:
    positions = mt5.positions_get()
    if positions:
        print(f"ADVERTENCIA: hay {len(positions)} posicion(es) abierta(s):")
        for p in positions:
            print(f"  ticket={p.ticket} symbol={p.symbol} profit={p.profit:.2f}")
    else:
        print("Sin posiciones abiertas.")
    mt5.shutdown()
    print("MT5 desconectado.")
else:
    print("MT5 ya estaba desconectado o no disponible.")
'@

Write-Host "`n[1] Verificando posiciones abiertas en MT5..." -ForegroundColor Yellow
$pythonExe = "C:\Users\yosel\AppData\Local\Programs\Python\Python312\python.exe"
& $pythonExe -c $disconnectScript

# Matar procesos Python que corren main.py desde mnq-ml-scalper
Write-Host "`n[2] Buscando procesos del bot MNQ..." -ForegroundColor Yellow
$botProcs = Get-WmiObject Win32_Process | Where-Object {
    $_.Name -eq "python.exe" -and $_.CommandLine -match "main\.py"
}

if ($botProcs) {
    foreach ($proc in $botProcs) {
        Write-Host "  Matando PID $($proc.ProcessId): $($proc.CommandLine)" -ForegroundColor Red
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Write-Host "  Procesos terminados." -ForegroundColor Green
} else {
    Write-Host "  No se encontraron procesos del bot." -ForegroundColor Green
}

# Verificar que no queden procesos Python pesados (>100MB) fuera de VSCode
Write-Host "`n[3] Procesos Python restantes:" -ForegroundColor Yellow
Get-Process python -ErrorAction SilentlyContinue | Select-Object Id,
    @{N="MB";E={[int]($_.WorkingSet64/1MB)}},
    @{N="CMD";E={try{(Get-WmiObject Win32_Process -Filter "ProcessId=$($_.Id)").CommandLine}catch{"?"}}} |
    Format-Table -AutoSize

Write-Host "`n=== Listo ===" -ForegroundColor Cyan
