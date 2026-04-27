param(
    [string]$TargetTriple = "",
    [string]$Bundles = "all",
    [string]$Python = "python",
    [string]$Npm = "npm.cmd",
    [switch]$SkipNpmInstall
)

$ErrorActionPreference = "Stop"

$script:Root = Split-Path -Parent $PSScriptRoot
$script:LocalToolsRoot = Join-Path $script:Root ".local-tools"
$script:LocalCargoHome = Join-Path $script:LocalToolsRoot "cargo"
$script:LocalRustupHome = Join-Path $script:LocalToolsRoot "rustup"
$script:LocalLlvmMingwRoot = Join-Path $script:LocalToolsRoot "llvm-mingw"
$script:DownloadCacheRoot = Join-Path $script:LocalToolsRoot "downloads"
$script:TempRoot = Join-Path $script:Root ".tmp-tests\desktop-release"
$script:Cargo = $null
$script:LlvmMingw = $null
$script:Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$script:WebRequestHeaders = @{
    "User-Agent" = "somnia-desktop-release"
}
if (-not [string]::IsNullOrWhiteSpace($env:GITHUB_TOKEN)) {
    $script:WebRequestHeaders["Authorization"] = ("Bearer {0}" -f $env:GITHUB_TOKEN.Trim())
} elseif (-not [string]::IsNullOrWhiteSpace($env:GH_TOKEN)) {
    $script:WebRequestHeaders["Authorization"] = ("Bearer {0}" -f $env:GH_TOKEN.Trim())
}

try {
    $securityProtocol = [System.Net.ServicePointManager]::SecurityProtocol
    $tls12 = [System.Net.SecurityProtocolType]::Tls12
    if (($securityProtocol -band $tls12) -eq 0) {
        [System.Net.ServicePointManager]::SecurityProtocol = $securityProtocol -bor $tls12
    }
} catch {
}

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

function New-Directory {
    param([string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        return $null
    }

    if (-not (Test-Path $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }

    return $Path
}

function Get-NormalizedPath {
    param([string]$Path)

    return [System.IO.Path]::GetFullPath($Path).TrimEnd("\")
}

function Assert-PathWithin {
    param(
        [string]$Path,
        [string]$Parent
    )

    $normalizedPath = Get-NormalizedPath -Path $Path
    $normalizedParent = Get-NormalizedPath -Path $Parent
    $parentPrefix = if ($normalizedParent.EndsWith("\")) { $normalizedParent } else { $normalizedParent + "\" }
    $comparison = [System.StringComparison]::OrdinalIgnoreCase

    if ($normalizedPath -eq $normalizedParent) {
        return
    }

    if (-not $normalizedPath.StartsWith($parentPrefix, $comparison)) {
        throw ("Refusing to operate on path outside {0}: {1}" -f $normalizedParent, $normalizedPath)
    }
}

function Remove-TreeSafely {
    param(
        [string]$Path,
        [string]$AllowedParent
    )

    if (-not (Test-Path $Path)) {
        return
    }

    Assert-PathWithin -Path $Path -Parent $AllowedParent
    Remove-Item -LiteralPath $Path -Recurse -Force
}

function Invoke-DownloadFile {
    param(
        [string]$Url,
        [string]$DestinationPath,
        [string]$Label
    )

    $destinationDir = Split-Path -Parent $DestinationPath
    if ($destinationDir) {
        New-Directory -Path $destinationDir | Out-Null
    }

    if (Test-Path $DestinationPath) {
        Write-Host ("Using cached {0}: {1}" -f $Label, $DestinationPath) -ForegroundColor DarkCyan
        return $DestinationPath
    }

    for ($attempt = 1; $attempt -le 3; $attempt++) {
        Write-Host ("==> Downloading {0} (attempt {1}/3)" -f $Label, $attempt) -ForegroundColor Cyan
        Write-Host ("    {0}" -f $Url) -ForegroundColor DarkCyan

        $request = @{
            Uri     = $Url
            OutFile = $DestinationPath
            Headers = $script:WebRequestHeaders
        }
        if ($PSVersionTable.PSVersion.Major -lt 6) {
            $request.UseBasicParsing = $true
        }

        try {
            Invoke-WebRequest @request
            return $DestinationPath
        }
        catch {
            Remove-Item -LiteralPath $DestinationPath -Force -ErrorAction SilentlyContinue
            if ($attempt -ge 3) {
                throw
            }

            $delaySeconds = 2 * $attempt
            Write-Host ("Download failed for {0}; retrying in {1}s." -f $Label, $delaySeconds) -ForegroundColor DarkYellow
            Start-Sleep -Seconds $delaySeconds
        }
    }

    return $DestinationPath
}

function Invoke-JsonRequest {
    param([string]$Url)

    $request = @{
        Uri     = $Url
        Headers = $script:WebRequestHeaders
    }
    if ($PSVersionTable.PSVersion.Major -lt 6) {
        $request.UseBasicParsing = $true
    }

    return Invoke-RestMethod @request
}

function Resolve-GitHubLatestReleaseTag {
    param(
        [string]$Repository,
        [string]$FallbackTag = ""
    )

    if ([string]::IsNullOrWhiteSpace($Repository)) {
        throw "Repository is required."
    }

    $latestReleaseUrl = "https://github.com/{0}/releases/latest" -f $Repository.Trim()
    $request = @{
        Uri     = $latestReleaseUrl
        Headers = $script:WebRequestHeaders
    }
    if ($PSVersionTable.PSVersion.Major -lt 6) {
        $request.UseBasicParsing = $true
    }

    try {
        $response = Invoke-WebRequest @request
        $responseUri = $null
        if ($response.BaseResponse -and $response.BaseResponse.ResponseUri) {
            $responseUri = $response.BaseResponse.ResponseUri
        } elseif ($response.Headers -and $response.Headers.Location) {
            $responseUri = [System.Uri]$response.Headers.Location
        }

        if ($responseUri) {
            $segments = @($responseUri.AbsolutePath.TrimEnd("/") -split "/")
            if ($segments.Length -ge 2 -and $segments[-2] -eq "tag" -and -not [string]::IsNullOrWhiteSpace($segments[-1])) {
                return $segments[-1]
            }
        }
    }
    catch {
        if ([string]::IsNullOrWhiteSpace($FallbackTag)) {
            throw
        }
    }

    if (-not [string]::IsNullOrWhiteSpace($FallbackTag)) {
        return $FallbackTag.Trim()
    }

    throw ("Unable to resolve latest GitHub release tag for {0}." -f $Repository)
}

function Resolve-LocalCargoCommand {
    $cargoExe = Join-Path $script:LocalCargoHome "bin\cargo.exe"
    if (-not (Test-Path $cargoExe) -or -not (Test-Path $script:LocalRustupHome)) {
        return $null
    }

    $toolchainsDir = Join-Path $script:LocalRustupHome "toolchains"
    if (-not (Test-Path $toolchainsDir)) {
        return $null
    }

    $toolchain = Get-ChildItem -LiteralPath $toolchainsDir -Directory -ErrorAction SilentlyContinue |
        Sort-Object @{
            Expression = {
                if ($_.Name -eq "stable-x86_64-pc-windows-gnullvm") { 0 }
                elseif ($_.Name -like "stable*") { 1 }
                else { 2 }
            }
        }, Name |
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
        CargoHome        = $script:LocalCargoHome
        RustupHome       = $script:LocalRustupHome
        ToolchainBin     = $(if (Test-Path $toolchainBin) { $toolchainBin } else { $null })
        SelfContainedBin = $(if (Test-Path $selfContainedBin) { $selfContainedBin } else { $null })
        LinkerPath       = $(if (Test-Path $linkerPath) { $linkerPath } else { $null })
    }
}

function Resolve-GlobalCargoCommand {
    $command = Get-Command cargo -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $command) {
        return $null
    }

    return [PSCustomObject]@{
        FilePath         = $command.Source
        Label            = "cargo"
        Scope            = "global"
        CargoHome        = $null
        RustupHome       = $null
        ToolchainBin     = $null
        SelfContainedBin = $null
        LinkerPath       = $null
    }
}

function Resolve-CargoCommand {
    $local = Resolve-LocalCargoCommand
    if ($local) {
        return $local
    }

    return Resolve-GlobalCargoCommand
}

function Test-TcpEndpoint {
    param(
        [string]$HostName,
        [int]$Port,
        [int]$TimeoutMilliseconds = 1000
    )

    $client = $null
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $connect = $client.BeginConnect($HostName, $Port, $null, $null)
        if (-not $connect.AsyncWaitHandle.WaitOne($TimeoutMilliseconds, $false)) {
            return $false
        }

        $client.EndConnect($connect)
        return $true
    }
    catch {
        return $false
    }
    finally {
        if ($client) {
            $client.Close()
        }
    }
}

function Disable-UnavailableLocalCargoProxyConfig {
    param([object]$CargoCommand)

    if (-not $CargoCommand -or $CargoCommand.Scope -ne "local") {
        return
    }

    if ($env:SOMNIA_KEEP_LOCAL_CARGO_CONFIG -eq "1") {
        return
    }

    $configPath = Join-Path $CargoCommand.CargoHome "config.toml"
    if (-not (Test-Path $configPath)) {
        return
    }

    $content = Get-Content -LiteralPath $configPath -Raw
    if ($content -notmatch 'replace-with\s*=\s*"local-proxy"') {
        return
    }

    if ($content -notmatch 'registry\s*=\s*"sparse\+http://127\.0\.0\.1:(\d+)/index/"') {
        return
    }

    $port = [int]$Matches[1]
    if (Test-TcpEndpoint -HostName "127.0.0.1" -Port $port) {
        return
    }

    $backupPath = "{0}.disabled-{1}" -f $configPath, (Get-Date -Format "yyyyMMddHHmmss")
    Move-Item -LiteralPath $configPath -Destination $backupPath -Force
    Write-Host ("Disabled unavailable workspace-local Cargo registry proxy config: {0}" -f $configPath) -ForegroundColor DarkYellow
    Write-Host ("Backup written to: {0}" -f $backupPath) -ForegroundColor DarkYellow
    Write-Host "Set SOMNIA_KEEP_LOCAL_CARGO_CONFIG=1 to keep this config unchanged." -ForegroundColor DarkYellow
}

function Resolve-LlvmMingw {
    $root = $script:LocalLlvmMingwRoot
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

function Get-WindowsNativeArchitecture {
    $architecture = $null

    try {
        $processor = Get-CimInstance -ClassName Win32_Processor -ErrorAction Stop | Select-Object -First 1
        if ($processor) {
            switch ([int]$processor.Architecture) {
                12 { return "arm64" }
                9 { return "x86_64" }
                0 { return "i686" }
            }
        }
    } catch {
    }

    foreach ($value in @($env:PROCESSOR_ARCHITEW6432, $env:PROCESSOR_ARCHITECTURE)) {
        if ([string]::IsNullOrWhiteSpace($value)) {
            continue
        }

        switch ($value.Trim().ToUpperInvariant()) {
            "ARM64" { return "arm64" }
            "AMD64" { return "x86_64" }
            "X86" { return "i686" }
        }
    }

    return "x86_64"
}

function Resolve-RustupInitDownload {
    if (-not [string]::IsNullOrWhiteSpace($env:SOMNIA_RUSTUP_INIT_URL)) {
        $url = $env:SOMNIA_RUSTUP_INIT_URL.Trim()
        $fileName = [System.IO.Path]::GetFileName(([System.Uri]$url).AbsolutePath)
        if ([string]::IsNullOrWhiteSpace($fileName)) {
            $fileName = "rustup-init-custom.exe"
        }

        return [PSCustomObject]@{
            Url          = $url
            FileName     = $fileName
            Architecture = "custom"
            Source       = "SOMNIA_RUSTUP_INIT_URL"
        }
    }

    $architecture = Get-WindowsNativeArchitecture
    $urlSegment = switch ($architecture) {
        "arm64" { "aarch64" }
        "i686" { "i686" }
        default { "x86_64" }
    }

    return [PSCustomObject]@{
        Url          = ("https://win.rustup.rs/{0}" -f $urlSegment)
        FileName     = ("rustup-init-{0}.exe" -f $urlSegment)
        Architecture = $architecture
        Source       = ("rustup-init {0}" -f $urlSegment)
    }
}

function Resolve-LlvmMingwDownload {
    if (-not [string]::IsNullOrWhiteSpace($env:SOMNIA_LLVM_MINGW_URL)) {
        $url = $env:SOMNIA_LLVM_MINGW_URL.Trim()
        $fileName = [System.IO.Path]::GetFileName(([System.Uri]$url).AbsolutePath)
        if ([string]::IsNullOrWhiteSpace($fileName)) {
            $fileName = "llvm-mingw-ucrt-x86_64.zip"
        }

        return [PSCustomObject]@{
            Url      = $url
            FileName = $fileName
            Source   = "SOMNIA_LLVM_MINGW_URL"
        }
    }

    $tag = Resolve-GitHubLatestReleaseTag -Repository "mstorsjo/llvm-mingw"
    $fileName = "llvm-mingw-{0}-ucrt-x86_64.zip" -f $tag

    return [PSCustomObject]@{
        Url      = ("https://github.com/mstorsjo/llvm-mingw/releases/download/{0}/{1}" -f $tag, $fileName)
        FileName = $fileName
        Source   = ("llvm-mingw {0}" -f $tag)
    }
}

function Install-WorkspaceRustToolchain {
    if (Resolve-LocalCargoCommand) {
        return
    }

    New-Directory -Path $script:LocalToolsRoot | Out-Null
    New-Directory -Path $script:DownloadCacheRoot | Out-Null

    $rustupInitDownload = Resolve-RustupInitDownload
    $rustupInitExe = Join-Path $script:DownloadCacheRoot $rustupInitDownload.FileName
    Invoke-DownloadFile -Url $rustupInitDownload.Url -DestinationPath $rustupInitExe -Label $rustupInitDownload.Source | Out-Null

    if ((Test-Path $script:LocalCargoHome) -or (Test-Path $script:LocalRustupHome)) {
        Write-Host "Removing incomplete workspace-local Rust toolchain before reinstall." -ForegroundColor DarkYellow
        Remove-TreeSafely -Path $script:LocalCargoHome -AllowedParent $script:LocalToolsRoot
        Remove-TreeSafely -Path $script:LocalRustupHome -AllowedParent $script:LocalToolsRoot
    }

    New-Directory -Path $script:LocalCargoHome | Out-Null
    New-Directory -Path $script:LocalRustupHome | Out-Null

    $previousCargoHome = $env:CARGO_HOME
    $previousRustupHome = $env:RUSTUP_HOME
    $previousRustupInitSkipPathCheck = $env:RUSTUP_INIT_SKIP_PATH_CHECK

    try {
        $env:CARGO_HOME = $script:LocalCargoHome
        $env:RUSTUP_HOME = $script:LocalRustupHome
        $env:RUSTUP_INIT_SKIP_PATH_CHECK = "yes"

        Write-Host "==> Bootstrapping workspace-local Rust toolchain" -ForegroundColor Cyan
        for ($attempt = 1; $attempt -le 2; $attempt++) {
            try {
                & $rustupInitExe `
                    -y `
                    --no-modify-path `
                    --profile minimal `
                    --default-host x86_64-pc-windows-gnullvm `
                    --default-toolchain stable-x86_64-pc-windows-gnullvm
                if ($LASTEXITCODE -ne 0) {
                    throw ("rustup-init.exe failed with exit code {0}." -f $LASTEXITCODE)
                }

                break
            }
            catch {
                if (($attempt -ge 2) -or -not (Test-Path $rustupInitExe)) {
                    throw
                }

                Write-Host ("Cached {0} appears unusable; re-downloading." -f $rustupInitDownload.Source) -ForegroundColor DarkYellow
                Remove-Item -LiteralPath $rustupInitExe -Force -ErrorAction SilentlyContinue
                Invoke-DownloadFile -Url $rustupInitDownload.Url -DestinationPath $rustupInitExe -Label $rustupInitDownload.Source | Out-Null
            }
        }
    }
    finally {
        if ($null -eq $previousCargoHome) {
            Remove-Item Env:CARGO_HOME -ErrorAction SilentlyContinue
        } else {
            $env:CARGO_HOME = $previousCargoHome
        }

        if ($null -eq $previousRustupHome) {
            Remove-Item Env:RUSTUP_HOME -ErrorAction SilentlyContinue
        } else {
            $env:RUSTUP_HOME = $previousRustupHome
        }

        if ($null -eq $previousRustupInitSkipPathCheck) {
            Remove-Item Env:RUSTUP_INIT_SKIP_PATH_CHECK -ErrorAction SilentlyContinue
        } else {
            $env:RUSTUP_INIT_SKIP_PATH_CHECK = $previousRustupInitSkipPathCheck
        }
    }

    if (-not (Resolve-LocalCargoCommand)) {
        throw "Workspace-local Rust toolchain bootstrap completed, but cargo.exe is still unavailable."
    }
}

function Install-WorkspaceLlvmMingw {
    if (Resolve-LlvmMingw) {
        return
    }

    New-Directory -Path $script:LocalToolsRoot | Out-Null
    New-Directory -Path $script:DownloadCacheRoot | Out-Null
    New-Directory -Path $script:TempRoot | Out-Null

    $download = Resolve-LlvmMingwDownload
    $archivePath = Join-Path $script:DownloadCacheRoot $download.FileName
    Invoke-DownloadFile -Url $download.Url -DestinationPath $archivePath -Label ("{0} archive" -f $download.Source) | Out-Null

    for ($attempt = 1; $attempt -le 2; $attempt++) {
        $extractRoot = Join-Path $script:TempRoot ("llvm-mingw-extract-{0}" -f [System.Guid]::NewGuid().ToString("N"))
        New-Directory -Path $extractRoot | Out-Null

        try {
            if (Test-Path $script:LocalLlvmMingwRoot) {
                Remove-TreeSafely -Path $script:LocalLlvmMingwRoot -AllowedParent $script:LocalToolsRoot
            }

            Write-Host "==> Bootstrapping workspace-local llvm-mingw" -ForegroundColor Cyan
            Expand-Archive -LiteralPath $archivePath -DestinationPath $extractRoot -Force

            $entries = @(Get-ChildItem -LiteralPath $extractRoot -Force)
            $payloadRoot = if ($entries.Count -eq 1 -and $entries[0].PSIsContainer) {
                $entries[0].FullName
            } else {
                $extractRoot
            }

            New-Directory -Path $script:LocalLlvmMingwRoot | Out-Null
            Copy-Item -Path (Join-Path $payloadRoot "*") -Destination $script:LocalLlvmMingwRoot -Recurse -Force
            break
        }
        catch {
            if (Test-Path $script:LocalLlvmMingwRoot) {
                Remove-TreeSafely -Path $script:LocalLlvmMingwRoot -AllowedParent $script:LocalToolsRoot
            }

            if (($attempt -ge 2) -or -not (Test-Path $archivePath)) {
                throw
            }

            Write-Host ("Cached {0} archive appears unusable; re-downloading." -f $download.Source) -ForegroundColor DarkYellow
            Remove-Item -LiteralPath $archivePath -Force -ErrorAction SilentlyContinue
            Invoke-DownloadFile -Url $download.Url -DestinationPath $archivePath -Label ("{0} archive" -f $download.Source) | Out-Null
        }
        finally {
            Remove-TreeSafely -Path $extractRoot -AllowedParent $script:TempRoot
        }
    }

    if (-not (Resolve-LlvmMingw)) {
        throw "Workspace-local llvm-mingw bootstrap completed, but x86_64-w64-mingw32-clang.exe is still unavailable."
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

function Should-BootstrapWorkspaceWindowsToolchain {
    param([string]$RequestedTargetTriple)

    if ($env:OS -ne "Windows_NT") {
        return $false
    }

    if ([string]::IsNullOrWhiteSpace($RequestedTargetTriple)) {
        return $true
    }

    return ("$RequestedTargetTriple".Trim().ToLowerInvariant() -eq "x86_64-pc-windows-gnullvm")
}

function Ensure-WorkspaceWindowsToolchain {
    Install-WorkspaceRustToolchain
    Install-WorkspaceLlvmMingw
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

function Assert-DesktopUiDependenciesCanBeReinstalled {
    $nodeModulesDir = Join-Path $script:Root "desktop\ui\node_modules"
    if (-not (Test-Path $nodeModulesDir)) {
        return
    }

    $normalizedNodeModulesDir = Get-NormalizedPath -Path $nodeModulesDir
    $nodeModulesPrefix = if ($normalizedNodeModulesDir.EndsWith("\")) { $normalizedNodeModulesDir } else { $normalizedNodeModulesDir + "\" }
    $comparison = [System.StringComparison]::OrdinalIgnoreCase
    $blockingProcesses = @()

    foreach ($process in Get-Process) {
        $processPath = $null
        try {
            $processPath = $process.Path
        }
        catch {
            continue
        }

        if ([string]::IsNullOrWhiteSpace($processPath)) {
            continue
        }

        try {
            $normalizedProcessPath = Get-NormalizedPath -Path $processPath
        }
        catch {
            continue
        }

        if ($normalizedProcessPath.StartsWith($nodeModulesPrefix, $comparison)) {
            $blockingProcesses += [PSCustomObject]@{
                Id   = $process.Id
                Name = $process.ProcessName
                Path = $normalizedProcessPath
            }
        }
    }

    if ($blockingProcesses.Count -eq 0) {
        return
    }

    $details = ($blockingProcesses | ForEach-Object {
        "  PID {0} {1}: {2}" -f $_.Id, $_.Name, $_.Path
    }) -join [Environment]::NewLine

    throw @"
Cannot run npm ci because desktop UI node_modules contains executables that are currently running.

$details

Close the desktop dev server/app that owns these processes, or stop them manually, then rerun this script.
If dependencies are already installed and you only need to build, rerun with -SkipNpmInstall.
"@
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

if (Should-BootstrapWorkspaceWindowsToolchain -RequestedTargetTriple $TargetTriple) {
    Ensure-WorkspaceWindowsToolchain
}

$script:LlvmMingw = Resolve-LlvmMingw
$script:Cargo = Resolve-CargoCommand

if (-not $script:Cargo) {
    throw @"
Rust/Cargo is required to build desktop bundles, but no usable cargo executable was found.

Run this script on Windows without -TargetTriple to bootstrap the workspace-local toolchain under:
  $script:LocalToolsRoot

Or install Rust globally and pass a compatible -TargetTriple explicitly.
"@
}

if ($script:Cargo.Scope -eq "local") {
    Write-Host "Using workspace-local Rust toolchain from .local-tools." -ForegroundColor Cyan
    if ($script:LlvmMingw) {
        Write-Host ("Using workspace-local llvm-mingw from {0}." -f $script:LlvmMingw.Root) -ForegroundColor Cyan
    }
}

Disable-UnavailableLocalCargoProxyConfig -CargoCommand $script:Cargo
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
        Assert-DesktopUiDependenciesCanBeReinstalled
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
