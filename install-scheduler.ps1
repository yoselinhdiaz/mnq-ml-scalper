# install-scheduler.ps1 — Registra las tareas programadas en Windows
# Ejecutar UNA SOLA VEZ como Administrador

$BOT_DIR   = "C:\source\repos\mnq-ml-scalper"

# --- Firewall: abrir puerto 8765 para el dashboard ---
$fwRule = Get-NetFirewallRule -DisplayName "MNQ Bot Dashboard" -ErrorAction SilentlyContinue
if (-not $fwRule) {
    New-NetFirewallRule -DisplayName "MNQ Bot Dashboard" `
        -Direction Inbound -Protocol TCP -LocalPort 8765 -Action Allow | Out-Null
    Write-Host "Firewall: puerto 8765 abierto para el dashboard"
} else {
    Write-Host "Firewall: regla ya existe"
}
$SCRIPT    = "$BOT_DIR\run.ps1"
$PS_EXE    = "powershell.exe"
$PS_ARGS   = "-NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$SCRIPT`""

# --- Tarea START: repetir cada 5 minutos; run.ps1 aplica el horario operativo ---
$triggerStart = New-ScheduledTaskTrigger `
    -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration ([TimeSpan]::MaxValue)

$actionStart = New-ScheduledTaskAction `
    -Execute $PS_EXE -Argument $PS_ARGS -WorkingDirectory $BOT_DIR

$settingsStart = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -StartWhenAvailable

Register-ScheduledTask `
    -TaskName   "MNQ-Bot-Start" `
    -Trigger    $triggerStart `
    -Action     $actionStart `
    -Settings   $settingsStart `
    -RunLevel   Highest `
    -Force `
    -Description "Valida MT5 + bot US100 cada 5 minutos; run.ps1 aplica el horario operativo"

Write-Host "Tarea MNQ-Bot-Start registrada"

# --- Tarea STOP: cada viernes a las 5:30 PM ---
# El watchdog se detiene solo, pero esta tarea lo fuerza si el proceso sigue vivo

$stopScript = @"
Get-Process 'python'     -ErrorAction SilentlyContinue | Stop-Process -Force
Get-Process 'terminal64' -ErrorAction SilentlyContinue | Stop-Process -Force
"@

$stopFile = "$BOT_DIR\stop-bot.ps1"
Set-Content -Path $stopFile -Value $stopScript -Encoding UTF8

$triggerStop = New-ScheduledTaskTrigger `
    -Weekly -DaysOfWeek Friday -At "17:30"

$actionStop = New-ScheduledTaskAction `
    -Execute $PS_EXE `
    -Argument "-NonInteractive -ExecutionPolicy Bypass -File `"$stopFile`"" `
    -WorkingDirectory $BOT_DIR

Register-ScheduledTask `
    -TaskName   "MNQ-Bot-Stop" `
    -Trigger    $triggerStop `
    -Action     $actionStop `
    -Settings   (New-ScheduledTaskSettingsSet) `
    -RunLevel   Highest `
    -Force `
    -Description "Detiene MT5 + bot US100 cada viernes 5:30 PM"

Write-Host "Tarea MNQ-Bot-Stop registrada"
Write-Host ""
Write-Host "Listo. Para verificar: Get-ScheduledTask -TaskName 'MNQ-Bot-*'"
Write-Host "Para ejecutar ahora manualmente: Start-ScheduledTask -TaskName 'MNQ-Bot-Start'"
