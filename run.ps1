# run.ps1 --US100 ML Scalper Watchdog
# Inicia domingo 5:30 PM, se detiene viernes 5:30 PM (hora local)
# Ejecutado por Windows Task Scheduler cada 5 minutos.
# Valida horario, evita duplicados y asegura MT5 + bot.

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

function In-Trading-Window {
    $now = Get-Date
    $dow = [int]$now.DayOfWeek   # 0=Sun 1=Mon ... 5=Fri 6=Sat
    $hm  = $now.Hour * 60 + $now.Minute
    $cutoff_hm = 17 * 60 + 30   # 5:30 PM

    if ($dow -eq 6) { return $false }                       # Saturday: closed
    if ($dow -eq 0 -and $hm -lt $cutoff_hm) { return $false } # Sunday before open
    if ($dow -eq 5 -and $hm -ge $cutoff_hm) { return $false } # Friday after close
    return $true
}

function Should-Force-Stop {
    $now = Get-Date
    $dow = [int]$now.DayOfWeek
    $hm  = $now.Hour * 60 + $now.Minute
    $cutoff_hm = 17 * 60 + 30

    return ($dow -eq 5 -and $hm -ge $cutoff_hm) -or ($dow -eq 6)
}

function Get-RunWatchdogs {
    Get-Process "powershell" -ErrorAction SilentlyContinue | Where-Object {
        if ($_.Id -eq $PID) { return $false }
        try {
            $wmi = Get-WmiObject Win32_Process -Filter "ProcessId=$($_.Id)" -ErrorAction Stop
            $wmi -and $wmi.CommandLine -match "run\.ps1"
        } catch { $false }
    }
}

function Get-BotProcesses {
    Get-Process "python" -ErrorAction SilentlyContinue | Where-Object {
        try {
            $wmi = Get-WmiObject Win32_Process -Filter "ProcessId=$($_.Id)" -ErrorAction Stop
            $wmi -and $wmi.CommandLine -match "main\.py"
        } catch { $false }
    }
}

function Ensure-MT5 {
    $proc = Get-Process "terminal64" -ErrorAction SilentlyContinue
    if (-not $proc) {
        Write-Log "MT5 no esta corriendo --iniciando..."
        Start-Process $MT5_EXE
        Start-Sleep -Seconds 20
        Write-Log "MT5 iniciado"
        return $true   # recien iniciado, dar tiempo extra al bot
    }
    return $false
}

function Ensure-Bot {
    $botProcs = Get-BotProcesses
    if ($botProcs -and @($botProcs).Count -gt 1) {
        Write-Log "ADVERTENCIA: $(@($botProcs).Count) procesos bot detectados --matando duplicados..."
        $botProcs | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 3
        $botProcs = $null
    }
    if (-not $botProcs) {
        Write-Log "Bot no esta corriendo --iniciando..."
        Start-Process $PYTHON -ArgumentList $BOT_ARGS `
            -WorkingDirectory $BOT_DIR -WindowStyle Hidden
        Write-Log "Bot iniciado"
    }
}

function Stop-All {
    Write-Log "Deteniendo bot y MT5..."
    Get-BotProcesses | Stop-Process -Force -ErrorAction SilentlyContinue
    Get-Process "terminal64" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Write-Log "Todo detenido"
}

# ---------------------------------------------------------------
$otherWatchdogs = Get-RunWatchdogs
if ($otherWatchdogs) {
    Write-Log "Otro run.ps1 ya esta corriendo (PID: $($otherWatchdogs.Id -join ', ')) --saliendo"
    exit 0
}

Write-Log "===== Watchdog check iniciado ====="

if (-not (In-Trading-Window)) {
    Write-Log "Fuera de horario operativo"
    if (Should-Force-Stop) {
        Stop-All
    }
    Write-Log "===== Watchdog check finalizado ====="
    exit 0
}

$mt5_new = Ensure-MT5
if ($mt5_new) { Start-Sleep -Seconds 10 }
Ensure-Bot

Write-Log "===== Watchdog check finalizado ====="
