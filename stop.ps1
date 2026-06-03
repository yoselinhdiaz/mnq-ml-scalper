# stop.ps1 — Detiene el bot y MetaTrader 5 manualmente

$LOG = "C:\source\repos\mnq-ml-scalper\logs\watchdog.log"

function Write-Log($msg) {
    $ts   = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "$ts  $msg"
    Add-Content -Path $LOG -Value $line
    Write-Host $line
}

Write-Log "===== Stop manual iniciado ====="

# Dar tiempo al bot para cerrar posiciones via SIGTERM (KeyboardInterrupt)
$bot = Get-Process "python" -ErrorAction SilentlyContinue
if ($bot) {
    Write-Log "Enviando senal de cierre al bot (cerrando posiciones)..."
    # Run close-all script first
    $closeScript = @"
import yaml, MetaTrader5 as mt5, sys
sys.path.insert(0, r'C:\source\repos\mnq-ml-scalper')
from execution.order_sender import OrderSender
from data.mt5_feed import MT5Feed
cfg = yaml.safe_load(open(r'C:\source\repos\mnq-ml-scalper\config.yaml'))
feed = MT5Feed(cfg)
feed.connect()
sender = OrderSender(cfg, feed)
pnl = sender.close_all_positions()
print(f'Posiciones cerradas | PnL total: {pnl:.2f}')
feed.disconnect()
"@
    $tmpFile = "$env:TEMP\close_positions.py"
    Set-Content -Path $tmpFile -Value $closeScript -Encoding UTF8
    python $tmpFile 2>$null
    Remove-Item $tmpFile -ErrorAction SilentlyContinue

    Start-Sleep -Seconds 3
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
