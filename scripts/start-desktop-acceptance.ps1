param(
    [switch]$SkipChecks,
    [switch]$ChecksOnly,
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8765,
    [string]$Provider = "",
    [string]$Model = ""
)

$ErrorActionPreference = "Stop"

$script:Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$script:UiDir = Join-Path $script:Root "desktop\ui"
$script:TauriDir = Join-Path $script:UiDir "src-tauri"
$script:LogDir = Join-Path $script:Root ".tmp-tests\desktop-acceptance"
$script:Python = $null
$script:Npm = $null
$script:Cargo = $null
$script:LlvmMingw = $null

function Write-Info {
    param([string]$Message)
    Write-Host $Message -ForegroundColor Cyan
}

function Write-Success {
    param([string]$Message)
    Write-Host $Message -ForegroundColor Green
}

function Write-Warn {
    param([string]$Message)
    Write-Host $Message -ForegroundColor Yellow
}

function Write-Fail {
    param([string]$Message)
    Write-Host $Message -ForegroundColor Red
}

function Resolve-PythonCommand {
    $python = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($python) {
        return [PSCustomObject]@{
            FilePath = $python.Source
            BaseArgs = @()
            Label    = "python"
        }
    }

    $launcher = Get-Command py -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($launcher) {
        return [PSCustomObject]@{
            FilePath = $launcher.Source
            BaseArgs = @("-3")
            Label    = "py -3"
        }
    }

    throw "Python 3 was not found in PATH."
}

function Resolve-NpmCommand {
    foreach ($candidate in @("npm.cmd", "npm")) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($command) {
            return [PSCustomObject]@{
                FilePath = $command.Source
                Label    = $candidate
            }
        }
    }

    throw "npm was not found in PATH."
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
        Root       = $root
        BinDir     = $binDir
        ClangPath  = $clangPath
        Label      = "workspace-local llvm-mingw"
    }
}

function Invoke-CheckedCommand {
    param(
        [string]$Title,
        [string]$WorkingDirectory,
        [string]$FilePath,
        [string[]]$Arguments
    )

    Write-Info ""
    Write-Info ("==> {0}" -f $Title)

    Push-Location $WorkingDirectory
    try {
        & $FilePath @Arguments
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }

    if ($exitCode -ne 0) {
        throw ("{0} failed with exit code {1}." -f $Title, $exitCode)
    }

    Write-Success ("[ok] {0}" -f $Title)
}

function Invoke-CapturedCommand {
    param(
        [string]$WorkingDirectory,
        [string]$FilePath,
        [string[]]$Arguments
    )

    Push-Location $WorkingDirectory
    try {
        $output = & $FilePath @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }

    return [PSCustomObject]@{
        ExitCode = $exitCode
        Output   = @($output | ForEach-Object { "$_" })
    }
}

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
            Prepend-PathEntry $CargoCommand.ToolchainBin
            Prepend-PathEntry $CargoCommand.SelfContainedBin
            $env:CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER = $CargoCommand.LinkerPath
            $env:CC_x86_64_pc_windows_gnu = $CargoCommand.LinkerPath
        }
    }
}

function Ensure-UiDependencies {
    $nodeModulesDir = Join-Path $script:UiDir "node_modules"
    if (Test-Path $nodeModulesDir) {
        return
    }

    Invoke-CheckedCommand `
        -Title "Installing desktop UI dependencies" `
        -WorkingDirectory $script:UiDir `
        -FilePath $script:Npm.FilePath `
        -Arguments @("install")
}

function Run-AcceptanceChecks {
    Invoke-CheckedCommand `
        -Title "Running desktop service and sidecar tests" `
        -WorkingDirectory $script:Root `
        -FilePath $script:Python.FilePath `
        -Arguments ($script:Python.BaseArgs + @(
            "-m",
            "unittest",
            "tests.test_app_service",
            "tests.test_sidecar_server"
        ))

    Invoke-CheckedCommand `
        -Title "Running CLI regression gate" `
        -WorkingDirectory $script:Root `
        -FilePath $script:Python.FilePath `
        -Arguments ($script:Python.BaseArgs + @(
            "-m",
            "unittest",
            "tests.test_cli_resume",
            "tests.test_process_output",
            "tests.test_repl_todo",
            "tests.test_runtime_tool_output"
        ))

    Invoke-CheckedCommand `
        -Title "Running desktop UI typecheck" `
        -WorkingDirectory $script:UiDir `
        -FilePath $script:Npm.FilePath `
        -Arguments @("run", "typecheck")
}

function Ensure-TauriToolchain {
    Write-Info ""
    Write-Info "==> Checking Tauri desktop prerequisites"

    if (-not $script:Cargo) {
        throw @"
Rust/Cargo is required to launch the Tauri desktop shell, but 'cargo' was not found in PATH.

Install Rust first, then reopen PowerShell and rerun this script.

Recommended on Windows:
  winget install Rustlang.Rustup

Alternative:
  https://rustup.rs/
"@
    }

    if ($script:Cargo.Scope -eq "local") {
        Write-Info "Using workspace-local Rust toolchain from .local-tools."
        if ($script:LlvmMingw) {
            Write-Info ("Using workspace-local llvm-mingw from {0}." -f $script:LlvmMingw.Root)
        }
    }

    Enable-CargoEnvironment -CargoCommand $script:Cargo

    $versionResult = Invoke-CapturedCommand `
        -WorkingDirectory $script:Root `
        -FilePath $script:Cargo.FilePath `
        -Arguments @("--version")

    if ($versionResult.ExitCode -ne 0) {
        $details = ($versionResult.Output -join [Environment]::NewLine).Trim()
        if (-not $details) {
            $details = "cargo --version returned a non-zero exit code."
        }
        throw ("Rust/Cargo is installed but not usable.`n`n{0}" -f $details)
    }

    $metadataResult = Invoke-CapturedCommand `
        -WorkingDirectory $script:TauriDir `
        -FilePath $script:Cargo.FilePath `
        -Arguments @("metadata", "--no-deps", "--format-version", "1")

    if ($metadataResult.ExitCode -ne 0) {
        $details = ($metadataResult.Output -join [Environment]::NewLine).Trim()
        if (-not $details) {
            $details = "cargo metadata returned a non-zero exit code."
        }
        throw ("Cargo is present, but the Tauri Rust workspace is not ready.`n`n{0}" -f $details)
    }

    $versionLine = ($versionResult.Output -join [Environment]::NewLine).Trim()
    if ($versionLine) {
        Write-Success ("[ok] {0}" -f $versionLine)
    } else {
        Write-Success "[ok] cargo"
    }
}

function Ensure-TauriProjectAssets {
    $iconPath = Join-Path $script:TauriDir "icons\icon.ico"
    if (-not (Test-Path $iconPath)) {
        throw ("Tauri project asset is missing: {0}`nAdd the Windows icon before launching desktop acceptance." -f $iconPath)
    }
}

function Invoke-JsonGet {
    param([string]$Uri)

    try {
        return Invoke-RestMethod -Uri $Uri -Method Get -TimeoutSec 2
    } catch {
        return $null
    }
}

function Test-SidecarReady {
    param([string]$BaseUrl)

    $payload = Invoke-JsonGet -Uri ("{0}/health" -f $BaseUrl)
    return $null -ne $payload -and $payload.status -eq "ready"
}

function Start-Sidecar {
    param(
        [string]$HostName,
        [int]$PortNumber
    )

    New-Item -ItemType Directory -Force -Path $script:LogDir | Out-Null

    $stdoutPath = Join-Path $script:LogDir "sidecar-stdout.log"
    $stderrPath = Join-Path $script:LogDir "sidecar-stderr.log"

    Remove-Item -Force -ErrorAction SilentlyContinue $stdoutPath, $stderrPath

    $arguments = $script:Python.BaseArgs + @(
        "-m",
        "desktop.backend.bootstrap",
        "--workspace",
        ".",
        "--host",
        $HostName,
        "--port",
        $PortNumber.ToString(),
        "--quiet"
    )

    if ($Provider.Trim()) {
        $arguments += @("--provider", $Provider.Trim())
    }

    if ($Model.Trim()) {
        $arguments += @("--model", $Model.Trim())
    }

    Write-Info ""
    Write-Info ("==> Starting sidecar at http://{0}:{1}" -f $HostName, $PortNumber)
    $process = Start-Process `
        -FilePath $script:Python.FilePath `
        -ArgumentList $arguments `
        -WorkingDirectory $script:Root `
        -NoNewWindow `
        -PassThru `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath

    return [PSCustomObject]@{
        Process    = $process
        StdoutPath = $stdoutPath
        StderrPath = $stderrPath
    }
}

function Wait-ForSidecarReady {
    param(
        [string]$BaseUrl,
        [object]$ManagedSidecar,
        [int]$TimeoutSeconds = 25
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-SidecarReady -BaseUrl $BaseUrl) {
            return
        }

        Start-Sleep -Milliseconds 400
    }

    $stderrTail = ""
    $stdoutTail = ""
    $processSummary = ""
    if ($ManagedSidecar -and $ManagedSidecar.Process) {
        try {
            $ManagedSidecar.Process.Refresh()
            if ($ManagedSidecar.Process.HasExited) {
                $processSummary = ("Sidecar launcher process exited with code {0}." -f $ManagedSidecar.Process.ExitCode)
            }
        } catch {
        }
    }

    if ($ManagedSidecar -and (Test-Path $ManagedSidecar.StderrPath)) {
        $stderrTail = ((Get-Content -Path $ManagedSidecar.StderrPath -Tail 40) -join [Environment]::NewLine).Trim()
    }
    if ($ManagedSidecar -and (Test-Path $ManagedSidecar.StdoutPath)) {
        $stdoutTail = ((Get-Content -Path $ManagedSidecar.StdoutPath -Tail 40) -join [Environment]::NewLine).Trim()
    }

    if ($stderrTail) {
        if ($processSummary) {
            throw ("Sidecar failed to become ready.`n`n{0}`n`n{1}" -f $processSummary, $stderrTail)
        }
        throw ("Sidecar failed to become ready.`n`n{0}" -f $stderrTail)
    }

    if ($stdoutTail) {
        if ($processSummary) {
            throw ("Sidecar failed to become ready.`n`n{0}`n`nStdout:`n{1}" -f $processSummary, $stdoutTail)
        }
        throw ("Sidecar failed to become ready.`n`nStdout:`n{0}" -f $stdoutTail)
    }

    if ($processSummary) {
        throw ("Sidecar failed to become ready at {0}.`n`n{1}" -f $BaseUrl, $processSummary)
    }

    throw ("Sidecar failed to become ready at {0}." -f $BaseUrl)
}

function Stop-Sidecar {
    param([object]$ManagedSidecar)

    if (-not $ManagedSidecar -or -not $ManagedSidecar.Process) {
        return
    }

    try {
        if (-not $ManagedSidecar.Process.HasExited) {
            Stop-Process -Id $ManagedSidecar.Process.Id -Force
        }
    } catch {
        Write-Warn ("Failed to stop sidecar process cleanly: {0}" -f $_.Exception.Message)
    }
}

function Start-TauriDev {
    Write-Info ""
    Write-Info "==> Launching Tauri desktop shell"
    Write-Info "Close the desktop window or press Ctrl+C here to stop the session."

    Push-Location $script:UiDir
    try {
        & $script:Npm.FilePath run tauri:dev
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }

    if ($exitCode -ne 0) {
        throw ("npm run tauri:dev failed with exit code {0}." -f $exitCode)
    }
}

try {
    Set-Location $script:Root
    $script:Python = Resolve-PythonCommand
    $script:Npm = Resolve-NpmCommand
    $script:Cargo = Resolve-CargoCommand
    $script:LlvmMingw = Resolve-LlvmMingw

    Write-Host ""
    Write-Info "Somnia Desktop Acceptance Launcher"
    Write-Host ""
    Write-Info ("Repo root : {0}" -f $script:Root)
    Write-Info ("Python    : {0}" -f $script:Python.Label)
    Write-Info ("npm       : {0}" -f $script:Npm.Label)
    Write-Info ("cargo     : {0}" -f $(if ($script:Cargo) { $script:Cargo.Label } else { "not found" }))
    Write-Info ("llvm      : {0}" -f $(if ($script:LlvmMingw) { $script:LlvmMingw.Label } else { "not found" }))

    Ensure-UiDependencies

    if (-not $SkipChecks) {
        Run-AcceptanceChecks
    } else {
        Write-Warn "Skipping automated acceptance checks."
    }

    if ($ChecksOnly) {
        Write-Host ""
        Write-Success "Acceptance checks completed."
        exit 0
    }

    Ensure-TauriToolchain
    Ensure-TauriProjectAssets

    # Keep desktop acceptance launches from mutating the user's global hook bootstrap state.
    $env:OPEN_SOMNIA_SKIP_BUILTIN_NOTIFY_BOOTSTRAP = "1"
    $env:PYTHON = $script:Python.FilePath
    $env:SOMNIA_DESKTOP_PYTHON_ARGS = ($script:Python.BaseArgs -join [char]0x1f)

    $baseUrl = "http://{0}:{1}" -f $BindHost, $Port
    $managedSidecar = $null
    $reusedExistingSidecar = $false

    try {
        if (Test-SidecarReady -BaseUrl $baseUrl) {
            $reusedExistingSidecar = $true
            Write-Warn ""
            Write-Warn ("Reusing existing sidecar at {0}." -f $baseUrl)
            if ($Provider.Trim() -or $Model.Trim()) {
                Write-Warn "Provider/model overrides are ignored while reusing an existing sidecar."
            }
        } else {
            $managedSidecar = Start-Sidecar -HostName $BindHost -PortNumber $Port
            Wait-ForSidecarReady -BaseUrl $baseUrl -ManagedSidecar $managedSidecar
            Write-Success ("Sidecar ready at {0}" -f $baseUrl)
            Write-Info ("Sidecar logs: {0}" -f $script:LogDir)
        }

        Start-TauriDev
    } finally {
        if ($managedSidecar) {
            Write-Info ""
            Write-Info "Stopping sidecar started by this script."
            Stop-Sidecar -ManagedSidecar $managedSidecar
        } elseif ($reusedExistingSidecar) {
            Write-Info ""
            Write-Info "Leaving reused sidecar running."
        }
    }
} catch {
    Write-Host ""
    Write-Fail $_.Exception.Message
    exit 1
}
