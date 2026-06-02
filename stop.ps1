# stop.ps1 — Detiene el bot y MetaTrader 5 manualmente

$LOG = "C:\source\repos\mnq-ml-scalper\logs\watchdog.log"

function Write-Log($msg) {
    $ts   = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "$ts  $msg"
    Add-Content -Path $LOG -Value $line
    Write-Host $line
}

Write-Log "===== Stop manual iniciado ====="

$bot = Get-Process "python" -ErrorAction SilentlyContinue
if ($bot) {
    $bot | ForEach-Object { wmic process where "ProcessId=$($_.Id)" delete 2>$null }
    Write-Log "Bot detenido (PID: $($bot.Id -join ', '))"
} else {
    Write-Log "Bot no estaba corriendo"
}

$mt5 = Get-Process "terminal64" -ErrorAction SilentlyContinue
if ($mt5) {
    $mt5 | ForEach-Object { wmic process where "ProcessId=$($_.Id)" delete 2>$null }
    Write-Log "MT5 detenido (PID: $($mt5.Id -join ', '))"
} else {
    Write-Log "MT5 no estaba corriendo"
}

Write-Log "===== Stop completo ====="
