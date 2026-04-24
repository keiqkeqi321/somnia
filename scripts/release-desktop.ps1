param(
    [string]$TargetTriple = "",
    [string]$Bundles = "all",
    [string]$Python = "python",
    [string]$Npm = "npm.cmd",
    [switch]$SkipNpmInstall
)

$ErrorActionPreference = "Stop"

$script:Root = Split-Path -Parent $PSScriptRoot
Set-Location $script:Root

function Get-DefaultTargetTriple {
    if ($env:CARGO_BUILD_TARGET) {
        return $env:CARGO_BUILD_TARGET
    }
    $rustcOutput = & rustc -vV 2>$null
    if ($LASTEXITCODE -eq 0) {
        foreach ($line in @($rustcOutput)) {
            if ("$line" -match '^host:\s*(.+)$') {
                return $Matches[1].Trim()
            }
        }
    }
    throw "Unable to determine the Rust target triple. Pass -TargetTriple explicitly."
}

if ([string]::IsNullOrWhiteSpace($TargetTriple)) {
    $TargetTriple = Get-DefaultTargetTriple
}

Write-Host ("==> Building bundled sidecar for {0}" -f $TargetTriple) -ForegroundColor Cyan
& $Python "scripts/release/build_desktop_sidecar.py" --target-triple $TargetTriple
if ($LASTEXITCODE -ne 0) {
    throw ("Bundled sidecar build failed with exit code {0}." -f $LASTEXITCODE)
}

Push-Location "desktop/ui"
try {
    if (-not $SkipNpmInstall) {
        Write-Host "==> Installing desktop UI dependencies" -ForegroundColor Cyan
        & $Npm ci
        if ($LASTEXITCODE -ne 0) {
            throw ("npm ci failed with exit code {0}." -f $LASTEXITCODE)
        }
    }

    Write-Host ("==> Building Tauri bundles ({0})" -f $Bundles) -ForegroundColor Cyan
    & $Npm run tauri:build -- --config src-tauri/tauri.bundle.conf.json --target $TargetTriple --bundles $Bundles --ci --no-sign
    if ($LASTEXITCODE -ne 0) {
        throw ("npm run tauri:build failed with exit code {0}." -f $LASTEXITCODE)
    }
}
finally {
    Pop-Location
}
