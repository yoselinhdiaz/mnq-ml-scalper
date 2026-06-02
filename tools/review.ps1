# ============================================================
# DevAssistant — review.ps1
# Opción A: Copia el contenido de un archivo o feature
#            al clipboard listo para pegar en Copilot Chat.
#
# USO:
#   .\review.ps1 src\features\users\usersSagas.js
#   .\review.ps1 src\features\users\usersSagas.js -Type "fix"
#   .\review.ps1 src\features\orders\ -Feature
#   .\review.ps1 src\features\orders\ -Feature -Type "audit feature orders"
#
# TIPOS DISPONIBLES (se pasan con -Type):
#   review        — auditoría completa 7 categorías (default)
#   fix           — corregir todos los problemas encontrados
#   optimize      — auditoría de performance
#   explain       — explicar el código
# ============================================================

param(
    [Parameter(Mandatory, Position=0)]
    [string]$Path,

    [string]$Type = "review",

    [switch]$Feature,   # -Feature: revisa todos los archivos de la carpeta

    [switch]$NoClip,    # -NoClip: imprime en pantalla en vez de clipboard

    [switch]$IncludeTests  # -IncludeTests: incluir archivos .test.js
)

$ErrorActionPreference = "Stop"

# ── Helpers ───────────────────────────────────────────────────────────────────

function Format-FileBlock {
    param([string]$FilePath, [string]$Content)
    $rel = $FilePath.Replace($PWD.Path, "").TrimStart("\").TrimStart("/")
    return "=== $rel ===`n``````javascript`n$Content`n```````n"
}

function Get-FilesSafe {
    param([string]$Dir)
    $filter = if ($IncludeTests) { "*.js", "*.jsx" } else { "*.js", "*.jsx" }
    $files = Get-ChildItem -Path $Dir -Recurse -Include "*.js","*.jsx" -File |
        Where-Object {
            $keep = $true
            if (-not $IncludeTests -and $_.Name -match "\.test\.(js|jsx)$") { $keep = $false }
            if ($_.FullName -match "node_modules|dist|build|coverage") { $keep = $false }
            $keep
        }
    return $files
}

# ── Recopilar contenido ───────────────────────────────────────────────────────

$resolvedPath = Resolve-Path $Path -ErrorAction SilentlyContinue
if (-not $resolvedPath) {
    Write-Error "Path not found: $Path"
    exit 1
}

$blocks = @()
$fileCount = 0
$totalLines = 0

if ($Feature -or (Test-Path $resolvedPath -PathType Container)) {
    # Modo carpeta / feature completo
    $files = Get-FilesSafe -Dir $resolvedPath
    if ($files.Count -eq 0) {
        Write-Error "No .js/.jsx files found in: $resolvedPath"
        exit 1
    }
    foreach ($file in $files) {
        $content = Get-Content $file.FullName -Raw -Encoding UTF8
        $lines   = ($content -split "`n").Count
        $blocks  += Format-FileBlock -FilePath $file.FullName -Content $content
        $fileCount++
        $totalLines += $lines
    }
    $featureName = Split-Path $resolvedPath -Leaf
    $header = "audit feature $featureName"
} else {
    # Modo archivo único
    $content = Get-Content $resolvedPath -Raw -Encoding UTF8
    $lines   = ($content -split "`n").Count
    $blocks  += Format-FileBlock -FilePath $resolvedPath -Content $content
    $fileCount = 1
    $totalLines = $lines
    $header = $Type
}

# ── Construir prompt ──────────────────────────────────────────────────────────

$body   = $blocks -join "`n"
$prompt = "$header`:`n`n$body"

# Stats al pie
$stats = "---`nFiles: $fileCount | Lines: $totalLines | $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
$prompt = "$prompt`n$stats"

# ── Output ────────────────────────────────────────────────────────────────────

if ($NoClip) {
    Write-Output $prompt
} else {
    $prompt | Set-Clipboard
    Write-Host ""
    Write-Host "✓ Prompt copiado al clipboard" -ForegroundColor Green
    Write-Host "  Archivos : $fileCount" -ForegroundColor Cyan
    Write-Host "  Líneas   : $totalLines" -ForegroundColor Cyan
    Write-Host "  Tipo     : $header" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  → Pega en Copilot Chat y DevAssistant hará el análisis." -ForegroundColor Yellow
    Write-Host ""
}
