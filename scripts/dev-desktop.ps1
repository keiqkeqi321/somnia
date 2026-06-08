<#
.SYNOPSIS
    Somnia Desktop one-click dev startup (sidecar + Tauri dev)
.DESCRIPTION
    Activates .local-tools Rust toolchain and llvm-mingw,
    starts the sidecar backend, then runs tauri dev.
.EXAMPLE
    powershell -File scripts\dev-desktop.ps1
#>

param(
    [int]$SidecarWaitSeconds = 2
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")

# -- 1. Activate workspace-local Rust toolchain --

$LocalCargoHome  = Join-Path $Root ".local-tools\cargo"
$LocalRustupHome = Join-Path $Root ".local-tools\rustup"
$LlvmMingwBin    = Join-Path $Root ".local-tools\llvm-mingw\bin"
$Toolchain       = "stable-x86_64-pc-windows-gnullvm"
$ToolchainBin    = Join-Path $LocalRustupHome "toolchains\$Toolchain\bin"

$cargoExe = Join-Path $LocalCargoHome "bin\cargo.exe"

if (-not (Test-Path $cargoExe)) {
    Write-Host @"
[Rust] .local-tools cargo.exe not found. Run release build once to install:
  powershell -File scripts\release-desktop.ps1

Or install Rust globally:
  winget install Rustlang.Rustup
"@ -ForegroundColor Yellow
    exit 1
}

$env:CARGO_HOME  = $LocalCargoHome
$env:RUSTUP_HOME = $LocalRustupHome
$env:RUSTUP_TOOLCHAIN = $Toolchain
$env:CARGO_BUILD_TARGET = "x86_64-pc-windows-gnullvm"

$linker = Join-Path $LlvmMingwBin "x86_64-w64-mingw32-clang.exe"
if (Test-Path $linker) {
    $env:CARGO_TARGET_X86_64_PC_WINDOWS_GNULLVM_LINKER = $linker
    $env:CC_x86_64_pc_windows_gnullvm = $linker
}

$oldPath = $env:PATH
$env:PATH = (@(
    (Join-Path $LocalCargoHome "bin"),
    $LlvmMingwBin,
    $ToolchainBin
) -join ";") + ";" + $oldPath

Write-Host "[Rust] $($env:RUSTUP_TOOLCHAIN)" -ForegroundColor Cyan
& $cargoExe --version | ForEach-Object { Write-Host "       $_" -ForegroundColor DarkCyan }

# -- 2. Start Tauri dev. Tauri will launch the managed sidecar. --

Set-Location (Join-Path $Root "desktop\ui")
Write-Host "[Tauri] npm run tauri:dev" -ForegroundColor Cyan
Write-Host ""

& npm run tauri:dev
Write-Host "[Done] Exited" -ForegroundColor DarkCyan
