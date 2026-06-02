# run.ps1 — US100 ML Scalper Watchdog
# Inicia domingo 5:30 PM, se detiene viernes 5:30 PM (hora local)
# Monitorea MT5 y el bot cada 60 segundos y los reinicia si caen

$BOT_DIR  = "C:\source\repos\mnq-ml-scalper"
$PYTHON   = "python"
$BOT_ARGS = "main.py --dashboard"
$MT5_EXE  = "C:\Program Files\MetaTrader 5\terminal64.exe"
$LOG      = "$BOT_DIR\logs\watchdog.log"

function Write-Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "$ts  $msg"
    Add-Content -Path $LOG -Value $line
    Write-Host $line
}

function Should-Stop {
    $now = Get-Date
    $dow = [int]$now.DayOfWeek   # 0=Sun 1=Mon ... 5=Fri 6=Sat
    $hm  = $now.Hour * 60 + $now.Minute
    $stop_hm = 17 * 60 + 30     # 5:30 PM
    return ($dow -eq 5 -and $hm -ge $stop_hm) -or ($dow -eq 6)
}

function Ensure-MT5 {
    $proc = Get-Process "terminal64" -ErrorAction SilentlyContinue
    if (-not $proc) {
        Write-Log "MT5 no esta corriendo — iniciando..."
        Start-Process $MT5_EXE
        Start-Sleep -Seconds 20
        Write-Log "MT5 iniciado"
        return $true   # recien iniciado, dar tiempo extra al bot
    }
    return $false
}

function Ensure-Bot {
    $procs = Get-Process "python" -ErrorAction SilentlyContinue
    if ($procs -and $procs.Count -gt 1) {
        Write-Log "ADVERTENCIA: $($procs.Count) procesos Python detectados — matando duplicados..."
        $procs | ForEach-Object { wmic process where "ProcessId=$($_.Id)" delete 2>$null }
        Start-Sleep -Seconds 3
        $procs = $null
    }
    if (-not $procs) {
        Write-Log "Bot no esta corriendo — iniciando..."
        Start-Process $PYTHON -ArgumentList $BOT_ARGS `
            -WorkingDirectory $BOT_DIR -WindowStyle Hidden
        Write-Log "Bot iniciado"
    }
}

function Stop-All {
    Write-Log "Deteniendo bot y MT5..."
    Get-Process "python"     -ErrorAction SilentlyContinue | ForEach-Object {
        wmic process where "ProcessId=$($_.Id)" delete 2>$null
    }
    Get-Process "terminal64" -ErrorAction SilentlyContinue | ForEach-Object {
        wmic process where "ProcessId=$($_.Id)" delete 2>$null
    }
    Write-Log "Todo detenido"
}

# ---------------------------------------------------------------
Write-Log "===== Watchdog iniciado ====="

# Primer arranque
$mt5_new = Ensure-MT5
if ($mt5_new) { Start-Sleep -Seconds 10 }
Ensure-Bot

# Loop principal — verifica cada 60 segundos
while ($true) {
    Start-Sleep -Seconds 60

    if (Should-Stop) {
        Write-Log "Hora de parada (viernes 5:30 PM)"
        Stop-All
        Write-Log "===== Watchdog finalizado ====="
        exit 0
    }

    Ensure-MT5
    Ensure-Bot
}
