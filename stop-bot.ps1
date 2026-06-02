Get-Process 'python'     -ErrorAction SilentlyContinue | Stop-Process -Force
Get-Process 'terminal64' -ErrorAction SilentlyContinue | Stop-Process -Force
