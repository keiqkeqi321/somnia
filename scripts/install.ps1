# =============================================================
#  Somnia — 一键安装脚本 (Windows PowerShell)
# =============================================================
#  用法:
#    irm https://raw.githubusercontent.com/your-org/somnia/main/scripts/install.ps1 | iex
# =============================================================

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "🤖 Somnia Installer" -ForegroundColor Cyan
Write-Host ""

# ─── Step 1: Find Python ─────────────────────────────────────
$pythonCmd = $null

foreach ($cmd in @("python", "python3")) {
    try {
        $out = & $cmd --version 2>&1
        if ($out -match "Python (\d+)\.(\d+)") {
            $major = [int]$Matches[1]
            $minor = [int]$Matches[2]
            if ($major -eq 3 -and $minor -ge 11) {
                $pythonCmd = $cmd
                Write-Host "✓ Found $($out.Trim()) ($cmd)" -ForegroundColor Green
                break
            }
        }
    } catch { }
}

if (-not $pythonCmd) {
    Write-Host "✗ Python 3.11+ not found" -ForegroundColor Red
    Write-Host ""
    Write-Host "  Please install Python 3.11+ first:" 
    Write-Host ""
    Write-Host "    winget:  " -NoNewline; Write-Host "winget install Python.Python.3.12" -ForegroundColor Cyan
    Write-Host "    choco:   " -NoNewline; Write-Host "choco install python312" -ForegroundColor Cyan
    Write-Host "    Download:" -NoNewline; Write-Host " https://www.python.org/downloads/" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  Then re-run this script."
    exit 1
}

# ─── Step 2: Install somnia ──────────────────────────────────
Write-Host ""
Write-Host "📦 Installing somnia ..." -ForegroundColor Cyan
& $pythonCmd -m pip install --upgrade somnia

# ─── Step 3: Verify ──────────────────────────────────────────
Write-Host ""
try {
    & somnia --help | Out-Null
    Write-Host "✅ Somnia installed successfully!" -ForegroundColor Green
    Write-Host ""
    Write-Host "  Run:"
    Write-Host "    somnia"              -ForegroundColor Cyan -NoNewline; Write-Host "              # interactive REPL"
    Write-Host "    somnia chat 'hello'"  -ForegroundColor Cyan -NoNewline; Write-Host "  # one-shot"
    Write-Host ""
} catch {
    Write-Host "⚠  Installation completed but command not in PATH." -ForegroundColor Yellow
    Write-Host "  Try: " -NoNewline; Write-Host "python -m open_somnia" -ForegroundColor Cyan
}
