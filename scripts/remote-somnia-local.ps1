[CmdletBinding()]
param(
    [string]$Workspace,
    [string]$Project = "default-project",
    [string]$AdminUsername = "admin",
    [int]$RelayPort = 8787,
    [int]$SidecarPort = 8765,
    [int]$WebPort = 4173,
    [switch]$SkipBuild
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
$pythonCommand = "& '$python'"
Write-Host "Using Python: $python" -ForegroundColor DarkGray

function Start-Terminal([string]$Title, [string]$Command, [string]$WorkingDirectory) {
    $body = "`$Host.UI.RawUI.WindowTitle = '$Title'; Set-Location -LiteralPath '$WorkingDirectory'; $Command"
    Start-Process powershell.exe -ArgumentList @("-NoExit", "-ExecutionPolicy", "Bypass", "-Command", $body)
}

function Clear-OwnedListener([int]$Port, [string]$ExpectedCommand) {
    $listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    foreach ($listener in $listeners) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)"
        if ($process.CommandLine -notmatch $ExpectedCommand) {
            throw "Port $Port is already used by $($process.Name) (PID $($process.ProcessId))."
        }
        Write-Host "Stopping the previous Somnia process on port $Port..." -ForegroundColor DarkGray
        Stop-Process -Id $process.ProcessId -Force
        Wait-Process -Id $process.ProcessId -Timeout 5 -ErrorAction SilentlyContinue
    }
}

function Wait-HttpReady([string]$Name, [string]$Url, [int]$TimeoutSeconds = 60) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing $Url -TimeoutSec 2
            if ($response.StatusCode -eq 200) {
                Write-Host "$Name is ready." -ForegroundColor DarkGray
                return
            }
        } catch {
            Start-Sleep -Milliseconds 300
        }
    }
    throw "$Name did not become ready at $Url. Check its Somnia window for the startup error."
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

Write-Host "Starting Relay, Web preview, and Sidecar..." -ForegroundColor Cyan
Start-Terminal "Somnia Relay" "$pythonCommand -m open_somnia.remote.cli relay --host 127.0.0.1 --port $RelayPort" $repo
Start-Terminal "Somnia Web" "$pythonCommand scripts/preview_server.py --host 127.0.0.1 --port $WebPort" $uiDirectory
Start-Terminal "Somnia Sidecar" "$pythonCommand -m desktop.backend.bootstrap --workspace '$Workspace' --host 127.0.0.1 --port $SidecarPort" $repo

Wait-HttpReady "Relay" "http://127.0.0.1:$RelayPort/health"
Wait-HttpReady "Web preview" "http://127.0.0.1:$WebPort/?remote=1"
Wait-HttpReady "Sidecar" "http://127.0.0.1:$SidecarPort/health"
Start-Process "http://127.0.0.1:$WebPort/?remote=1"
Write-Host "`n1. Sign in at the opened Web page." -ForegroundColor Yellow
Write-Host "2. Create a Device pairing code." -ForegroundColor Yellow
$pairingCode = Read-Host "Paste the pairing code here"
if (-not $pairingCode) { throw "A pairing code is required." }

Write-Host "Pairing this computer..." -ForegroundColor Cyan
& $python -m open_somnia.remote.cli connector pair --relay "http://127.0.0.1:$RelayPort" --code $pairingCode --identity $identityPath

Start-Terminal "Somnia Connector" "$pythonCommand -m open_somnia.remote.cli connector run --project '$Project' --sidecar 'http://127.0.0.1:$SidecarPort' --identity '$identityPath'" $repo

# Do not leave the password in the shell that launched this helper.
$env:SOMNIA_ADMIN_PASSWORD = $null
Write-Host "`nReady. Sign in again to refresh the Device list, select the paired Device, and Connect." -ForegroundColor Green
Write-Host "Close the four Somnia windows to stop the local stack." -ForegroundColor DarkGray
