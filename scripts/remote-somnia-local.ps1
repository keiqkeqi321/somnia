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
$python = (Get-Command python -ErrorAction Stop).Source
$pythonCommand = "& '$python'"

function Start-Terminal([string]$Title, [string]$Command, [string]$WorkingDirectory) {
    $body = "`$Host.UI.RawUI.WindowTitle = '$Title'; Set-Location -LiteralPath '$WorkingDirectory'; $Command"
    Start-Process powershell.exe -ArgumentList @("-NoExit", "-ExecutionPolicy", "Bypass", "-Command", $body)
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

if (-not $SkipBuild) {
    Write-Host "Building the Web client..." -ForegroundColor Cyan
    Push-Location (Join-Path $repo "desktop\ui")
    try { npm run build } finally { Pop-Location }
}

Write-Host "Starting Relay, Web preview, and Sidecar..." -ForegroundColor Cyan
Start-Terminal "Somnia Relay" "$pythonCommand -m open_somnia.remote.cli relay --host 127.0.0.1 --port $RelayPort" $repo
Start-Terminal "Somnia Web" "npm run preview" (Join-Path $repo "desktop\ui")
Start-Terminal "Somnia Sidecar" "$pythonCommand -m desktop.backend.bootstrap --workspace '$Workspace' --host 127.0.0.1 --port $SidecarPort" $repo

Start-Sleep -Seconds 3
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
