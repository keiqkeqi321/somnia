# sync-version.ps1 — 从 VERSION 文件同步版本号到所有位置
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

$version = (Get-Content "VERSION" -Raw).Trim()

Write-Host ""
Write-Host "📌 版本号: $version" -ForegroundColor Cyan

# ─── 1. sync __init__.py ─────────────────────────────────────
$initFile = Join-Path $root "openagent\__init__.py"
if (Test-Path $initFile) {
    $content = Get-Content $initFile -Raw
    $content = $content -replace '__version__ = ".*"', "__version__ = `"$version`""
    Set-Content $initFile $content -NoNewline
    Write-Host "  ✅ openagent\__init__.py" -ForegroundColor Green
} else {
    Write-Host "  ⚠️  $initFile not found" -ForegroundColor Yellow
}

# ─── 2. sync npm/package.json ────────────────────────────────
$npmFile = Join-Path $root "npm\package.json"
if (Test-Path $npmFile) {
    $pkg = Get-Content $npmFile -Raw | ConvertFrom-Json
    $pkg.version = $version
    $pkg | ConvertTo-Json -Depth 10 | Set-Content $npmFile -Encoding UTF8
    Write-Host "  ✅ npm\package.json" -ForegroundColor Green
} else {
    Write-Host "  ⚠️  $npmFile not found" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "✅ 所有版本号已同步为 $version" -ForegroundColor Green
