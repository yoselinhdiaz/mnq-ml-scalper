# watchdog-guard.ps1
# OBSOLETE: run.ps1 is now the single watchdog entrypoint.
# Re-run install-scheduler.ps1 as Administrator so Windows Task Scheduler
# executes run.ps1 every 5 minutes during the trading window.

$BOT_DIR = "C:\source\repos\mnq-ml-scalper"
$LOG     = "$BOT_DIR\logs\watchdog.log"

function Write-Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $LOG -Value "$ts  [guard obsolete] $msg"
}

Write-Log "watchdog-guard.ps1 is obsolete; use scheduled run.ps1 checks instead"
exit 0
