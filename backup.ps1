# backup.ps1 — Exporta el bot completo para transferir a otra maquina

$SRC    = "C:\source\repos\mnq-ml-scalper"
$DEST   = "$env:USERPROFILE\Desktop\mnq-bot-backup"
$ZIP    = "$env:USERPROFILE\Desktop\mnq-bot-backup.zip"

Write-Host "Creando backup en $ZIP ..."

# Copiar todo el proyecto
if (Test-Path $DEST) { Remove-Item $DEST -Recurse -Force }
Copy-Item $SRC $DEST -Recurse

# Excluir __pycache__ y .git para reducir tamaño
Get-ChildItem $DEST -Recurse -Filter "__pycache__" | Remove-Item -Recurse -Force
Get-ChildItem $DEST -Recurse -Filter "*.pyc"       | Remove-Item -Force
Remove-Item "$DEST\.git" -Recurse -Force -ErrorAction SilentlyContinue

# Comprimir
if (Test-Path $ZIP) { Remove-Item $ZIP -Force }
Compress-Archive -Path $DEST -DestinationPath $ZIP

# Limpieza
Remove-Item $DEST -Recurse -Force

$size = [math]::Round((Get-Item $ZIP).Length / 1MB, 1)
Write-Host "Listo: $ZIP ($size MB)"
Write-Host ""
Write-Host "En la nueva maquina:"
Write-Host "  1. Instalar Python 3.12 + pip install -r requirements.txt"
Write-Host "  2. Instalar MetaTrader5"
Write-Host "  3. Extraer el zip en C:\source\repos\mnq-ml-scalper"
Write-Host "  4. Abrir PowerShell como Admin y correr: .\install-scheduler.ps1"
