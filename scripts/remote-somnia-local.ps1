[CmdletBinding()]
param(
    [string]$Workspace,
    [string]$Project = "default-project",
    [string]$AdminUsername = "admin",
    [int]$RelayPort = 8787,
    [int]$SidecarPort = 18765,
    [int]$WebPort = 4173,
    [switch]$SkipBuild,
    [switch]$Rebind
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $Workspace) {
    $Workspace = (Get-Location).Path
}
$Workspace = (Resolve-Path $Workspace).Path
$dbDirectory = Join-Path $repo ".scratch\remote-somnia"
$dbPath = (Resolve-Path $dbDirectory).Path.Replace("\", "/")
$identityPath = Join-Path $HOME ".open_somnia\remote\device-identity.json"
$pyLauncher = Get-Command py -ErrorAction SilentlyContinue
if ($pyLauncher) {
    $python = (& $pyLauncher.Source -3 -c "import sys; print(sys.executable)").Trim()
    if ($LASTEXITCODE -ne 0 -or -not $python) {
        throw "Unable to resolve the system Python through py -3."
    }
} else {
    $python = (Get-Command python -ErrorAction Stop).Source
}
Write-Host "Using Python: $python" -ForegroundColor DarkGray

function Clear-OwnedListener([int]$Port, [string]$ExpectedCommand) {
    $listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    foreach ($listener in $listeners) {
        $processId = [int]$listener.OwningProcess
        if ($processId -le 0) {
            continue
        }
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
        if (-not $process) {
            # The TCP table can briefly outlive a process that just exited.
            continue
        }
        $commandLine = [string]$process.CommandLine
        if (-not $commandLine) {
            throw "Port $Port is already used by PID $processId, but its command line could not be inspected."
        }
        if ($commandLine -notmatch $ExpectedCommand) {
            throw "Port $Port is already used by $($process.Name) (PID $processId)."
        }
        Write-Host "Stopping the previous Somnia process on port $Port..." -ForegroundColor DarkGray
        Stop-Process -Id $processId -Force
        Wait-Process -Id $processId -Timeout 5 -ErrorAction SilentlyContinue
    }
}


if (-not $env:SOMNIA_ADMIN_PASSWORD) {
    $secure = Read-Host "Relay administrator password" -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $env:SOMNIA_ADMIN_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    }
}

$env:SOMNIA_ADMIN_USERNAME = $AdminUsername
$env:SOMNIA_RELAY_DATABASE_URL = "sqlite:///$dbPath/relay.db"

Write-Host "Checking Python dependencies..." -ForegroundColor Cyan
& $python -c "import argon2, cryptography, sqlalchemy, starlette, uvicorn, websockets"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing Somnia and its Python dependencies..." -ForegroundColor Yellow
    & $python -m pip install -e $repo
    if ($LASTEXITCODE -ne 0) { throw "Python dependency installation failed." }
}

$uiDirectory = Join-Path $repo "desktop\ui"
if (-not (Test-Path (Join-Path $uiDirectory "node_modules"))) {
    Write-Host "Installing Web dependencies..." -ForegroundColor Yellow
    Push-Location $uiDirectory
    try {
        npm install
        if ($LASTEXITCODE -ne 0) { throw "Web dependency installation failed." }
    } finally {
        Pop-Location
    }
}

if (-not $SkipBuild) {
    Write-Host "Building the Web client..." -ForegroundColor Cyan
    Push-Location $uiDirectory
    try { npm run build } finally { Pop-Location }
}

Clear-OwnedListener $RelayPort "open_somnia\.remote\.cli relay"
Clear-OwnedListener $WebPort "(preview_server\.py|http\.server)"
Clear-OwnedListener $SidecarPort "desktop\.backend\.bootstrap"

Write-Host "Starting the supervised local stack..." -ForegroundColor Cyan
$supervisorArgs = @(
    (Join-Path $PSScriptRoot "remote_somnia_supervisor.py"),
    "--repo", $repo,
    "--workspace", $Workspace,
    "--project", $Project,
    "--relay-port", $RelayPort,
    "--sidecar-port", $SidecarPort,
    "--web-port", $WebPort,
    "--identity", $identityPath
)
if ($Rebind) { $supervisorArgs += "--rebind" }
& $python @supervisorArgs
$exitCode = $LASTEXITCODE
$env:SOMNIA_ADMIN_PASSWORD = $null
if ($exitCode -ne 0) { throw "Remote Somnia supervisor failed with exit code $exitCode." }
