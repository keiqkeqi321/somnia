param(
    [string]$TargetTriple = "",
    [string]$Bundles = "all",
    [string]$Python = "python",
    [string]$Npm = "npm.cmd",
    [switch]$SkipNpmInstall
)

$ErrorActionPreference = "Stop"

$script:Root = Split-Path -Parent $PSScriptRoot
$script:Cargo = $null
$script:LlvmMingw = $null
$script:Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
Set-Location $script:Root

function Prepend-PathEntry {
    param([string]$Entry)

    if ([string]::IsNullOrWhiteSpace($Entry) -or -not (Test-Path $Entry)) {
        return
    }

    $existing = @($env:PATH -split ";" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if ($existing | Where-Object { $_.TrimEnd("\") -ieq $Entry.TrimEnd("\") }) {
        return
    }

    if ([string]::IsNullOrWhiteSpace($env:PATH)) {
        $env:PATH = $Entry
    } else {
        $env:PATH = "{0};{1}" -f $Entry, $env:PATH
    }
}

function Resolve-CargoCommand {
    $command = Get-Command cargo -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) {
        return [PSCustomObject]@{
            FilePath          = $command.Source
            Label             = "cargo"
            Scope             = "global"
            CargoHome         = $null
            RustupHome        = $null
            ToolchainBin      = $null
            SelfContainedBin  = $null
            LinkerPath        = $null
        }
    }

    $cargoHome = Join-Path $script:Root ".local-tools\cargo"
    $rustupHome = Join-Path $script:Root ".local-tools\rustup"
    $cargoExe = Join-Path $cargoHome "bin\cargo.exe"
    if (-not (Test-Path $cargoExe) -or -not (Test-Path $rustupHome)) {
        return $null
    }

    $toolchainsDir = Join-Path $rustupHome "toolchains"
    $toolchain = Get-ChildItem -LiteralPath $toolchainsDir -Directory -ErrorAction SilentlyContinue |
        Sort-Object @{ Expression = { if ($_.Name -like "stable*") { 0 } else { 1 } } }, Name |
        Select-Object -First 1
    if (-not $toolchain) {
        return $null
    }

    $toolchainBin = Join-Path $toolchain.FullName "bin"
    $selfContainedBin = Join-Path $toolchain.FullName "lib\rustlib\x86_64-pc-windows-gnu\bin\self-contained"
    $linkerPath = Join-Path $selfContainedBin "x86_64-w64-mingw32-gcc.exe"

    return [PSCustomObject]@{
        FilePath         = $cargoExe
        Label            = "workspace-local cargo"
        Scope            = "local"
        CargoHome        = $cargoHome
        RustupHome       = $rustupHome
        ToolchainBin     = $(if (Test-Path $toolchainBin) { $toolchainBin } else { $null })
        SelfContainedBin = $(if (Test-Path $selfContainedBin) { $selfContainedBin } else { $null })
        LinkerPath       = $(if (Test-Path $linkerPath) { $linkerPath } else { $null })
    }
}

function Resolve-LlvmMingw {
    $root = Join-Path $script:Root ".local-tools\llvm-mingw"
    $binDir = Join-Path $root "bin"
    $clangPath = Join-Path $binDir "x86_64-w64-mingw32-clang.exe"
    if (-not (Test-Path $clangPath)) {
        return $null
    }

    return [PSCustomObject]@{
        Root      = $root
        BinDir    = $binDir
        ClangPath = $clangPath
        Label     = "workspace-local llvm-mingw"
    }
}

function Enable-CargoEnvironment {
    param([object]$CargoCommand)

    if (-not $CargoCommand) {
        return
    }

    if ($CargoCommand.Scope -eq "local") {
        $env:CARGO_HOME = $CargoCommand.CargoHome
        $env:RUSTUP_HOME = $CargoCommand.RustupHome

        $gnullvmRustc = Join-Path $CargoCommand.RustupHome "toolchains\stable-x86_64-pc-windows-gnullvm\bin\rustc.exe"
        $gnullvmToolchainBin = Split-Path -Parent $gnullvmRustc
        Prepend-PathEntry (Join-Path $CargoCommand.CargoHome "bin")

        if ($script:LlvmMingw -and (Test-Path $gnullvmRustc)) {
            $env:RUSTUP_TOOLCHAIN = "stable-x86_64-pc-windows-gnullvm"
            $env:CARGO_BUILD_TARGET = "x86_64-pc-windows-gnullvm"
            Prepend-PathEntry $script:LlvmMingw.BinDir
            Prepend-PathEntry $gnullvmToolchainBin
            $env:CARGO_TARGET_X86_64_PC_WINDOWS_GNULLVM_LINKER = $script:LlvmMingw.ClangPath
            $env:CC_x86_64_pc_windows_gnullvm = $script:LlvmMingw.ClangPath
            Remove-Item Env:CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER -ErrorAction SilentlyContinue
            Remove-Item Env:CC_x86_64_pc_windows_gnu -ErrorAction SilentlyContinue
        } elseif ($CargoCommand.LinkerPath) {
            Remove-Item Env:CARGO_BUILD_TARGET -ErrorAction SilentlyContinue
            Remove-Item Env:RUSTUP_TOOLCHAIN -ErrorAction SilentlyContinue
            Remove-Item Env:CARGO_TARGET_X86_64_PC_WINDOWS_GNULLVM_LINKER -ErrorAction SilentlyContinue
            Remove-Item Env:CC_x86_64_pc_windows_gnullvm -ErrorAction SilentlyContinue
            if ($CargoCommand.ToolchainBin) {
                Prepend-PathEntry $CargoCommand.ToolchainBin
            }
            if ($CargoCommand.SelfContainedBin) {
                Prepend-PathEntry $CargoCommand.SelfContainedBin
            }
            $env:CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER = $CargoCommand.LinkerPath
            $env:CC_x86_64_pc_windows_gnu = $CargoCommand.LinkerPath
        }
    }
}

function Get-DefaultTargetTriple {
    if ($env:CARGO_BUILD_TARGET) {
        return $env:CARGO_BUILD_TARGET
    }

    $rustcPath = $null
    $rustcCommand = Get-Command rustc -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($rustcCommand) {
        $rustcPath = $rustcCommand.Source
    } elseif ($script:Cargo -and $script:Cargo.ToolchainBin) {
        $candidate = Join-Path $script:Cargo.ToolchainBin "rustc.exe"
        if (Test-Path $candidate) {
            $rustcPath = $candidate
        }
    }

    if ($rustcPath) {
        $rustcOutput = & $rustcPath -vV 2>$null
        if ($LASTEXITCODE -eq 0) {
            foreach ($line in @($rustcOutput)) {
                if ("$line" -match '^host:\s*(.+)$') {
                    return $Matches[1].Trim()
                }
            }
        }
    }

    throw "Unable to determine the Rust target triple. Pass -TargetTriple explicitly."
}

function Get-TargetPlatform {
    param([string]$ResolvedTargetTriple)

    $normalized = "$ResolvedTargetTriple".Trim().ToLowerInvariant()
    if ($normalized -match 'windows') {
        return "windows"
    }
    if ($normalized -match 'darwin|apple') {
        return "macos"
    }
    if ($normalized -match 'linux') {
        return "linux"
    }
    throw ("Unsupported target triple: {0}" -f $ResolvedTargetTriple)
}

function Resolve-Bundles {
    param(
        [string]$RequestedBundles,
        [string]$ResolvedTargetTriple
    )

    if (-not [string]::IsNullOrWhiteSpace($RequestedBundles) -and $RequestedBundles -ne "all") {
        return $RequestedBundles
    }

    switch (Get-TargetPlatform -ResolvedTargetTriple $ResolvedTargetTriple) {
        "windows" { return "msi" }
        "macos" { return "app,dmg" }
        default { return "all" }
    }
}

function Write-TextFile {
    param(
        [string]$Path,
        [string]$Content
    )

    $directory = Split-Path -Parent $Path
    if ($directory -and -not (Test-Path $directory)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }

    [System.IO.File]::WriteAllText($Path, $Content, $script:Utf8NoBom)
}

function Resolve-RuntimeResources {
    param([string]$ResolvedTargetTriple)

    $resources = [ordered]@{}
    if ((Get-TargetPlatform -ResolvedTargetTriple $ResolvedTargetTriple) -ne "windows") {
        return $resources
    }

    if ($ResolvedTargetTriple -notmatch 'gnullvm') {
        return $resources
    }

    if (-not $script:LlvmMingw) {
        return $resources
    }

    foreach ($dllName in @("libunwind.dll", "libc++.dll", "libwinpthread-1.dll")) {
        $sourcePath = Join-Path $script:LlvmMingw.BinDir $dllName
        if (Test-Path $sourcePath) {
            $resources[$sourcePath] = $dllName
        }
    }

    return $resources
}

function New-GeneratedBundleConfig {
    param(
        [string]$BaseConfigPath,
        [hashtable]$RuntimeResources
    )

    if (-not $RuntimeResources -or $RuntimeResources.Count -eq 0) {
        return $BaseConfigPath
    }

    $config = Get-Content -Path $BaseConfigPath -Raw | ConvertFrom-Json
    if (-not $config.bundle) {
        $config | Add-Member -MemberType NoteProperty -Name bundle -Value ([PSCustomObject]@{})
    }

    $resourceObject = [PSCustomObject]@{}
    foreach ($entry in $RuntimeResources.GetEnumerator()) {
        $resourceObject | Add-Member -MemberType NoteProperty -Name $entry.Key -Value $entry.Value
    }

    $config.bundle | Add-Member -MemberType NoteProperty -Name resources -Value $resourceObject -Force

    $generatedConfigPath = Join-Path $script:Root ".tmp-tests\desktop-release\tauri.bundle.generated.json"
    Write-TextFile -Path $generatedConfigPath -Content ($config | ConvertTo-Json -Depth 32)
    return $generatedConfigPath
}

$script:LlvmMingw = Resolve-LlvmMingw
$script:Cargo = Resolve-CargoCommand

if (-not $script:Cargo) {
    throw @"
Rust/Cargo is required to build desktop bundles, but no usable cargo executable was found.

Install Rust globally, or restore the workspace-local toolchain under:
  D:\Project\Git\somnia\.local-tools
"@
}

if ($script:Cargo.Scope -eq "local") {
    Write-Host "Using workspace-local Rust toolchain from .local-tools." -ForegroundColor Cyan
    if ($script:LlvmMingw) {
        Write-Host ("Using workspace-local llvm-mingw from {0}." -f $script:LlvmMingw.Root) -ForegroundColor Cyan
    }
}

Enable-CargoEnvironment -CargoCommand $script:Cargo

if ([string]::IsNullOrWhiteSpace($TargetTriple)) {
    $TargetTriple = Get-DefaultTargetTriple
}

$Bundles = Resolve-Bundles -RequestedBundles $Bundles -ResolvedTargetTriple $TargetTriple
$bundleConfigPath = Join-Path $script:Root "desktop\ui\src-tauri\tauri.bundle.conf.json"
$runtimeResources = Resolve-RuntimeResources -ResolvedTargetTriple $TargetTriple
$resolvedBundleConfigPath = New-GeneratedBundleConfig -BaseConfigPath $bundleConfigPath -RuntimeResources $runtimeResources

if ($runtimeResources.Count -gt 0) {
    Write-Host "Bundling additional Windows runtime DLLs:" -ForegroundColor Cyan
    foreach ($item in $runtimeResources.GetEnumerator()) {
        Write-Host ("  {0} -> {1}" -f $item.Key, $item.Value) -ForegroundColor Cyan
    }
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
    & $Npm run tauri:build -- --config $resolvedBundleConfigPath --target $TargetTriple --bundles $Bundles --ci --no-sign
    if ($LASTEXITCODE -ne 0) {
        throw ("npm run tauri:build failed with exit code {0}." -f $LASTEXITCODE)
    }
}
finally {
    Pop-Location
}
