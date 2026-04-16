param(
    [string]$Version = "",
    [switch]$Dry
)

$ErrorActionPreference = "Stop"

$script:Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$script:Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $script:Root

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

function Read-TextFile {
    param([string]$Path)
    if (-not (Test-Path $Path)) {
        return ""
    }
    return [System.IO.File]::ReadAllText((Resolve-Path $Path), [System.Text.Encoding]::UTF8)
}

function Write-TextFile {
    param(
        [string]$Path,
        [string]$Content,
        [switch]$NoNewline
    )
    $resolvedPath = Join-Path $script:Root $Path
    $directory = Split-Path -Parent $resolvedPath
    if ($directory -and -not (Test-Path $directory)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }
    $text = $Content
    if (-not $NoNewline) {
        $text += [Environment]::NewLine
    }
    [System.IO.File]::WriteAllText($resolvedPath, $text, $script:Utf8NoBom)
}

function Parse-SemVer {
    param([string]$InputVersion)
    if ($InputVersion -notmatch '^\s*v?(\d+)\.(\d+)\.(\d+)\s*$') {
        return $null
    }
    $major = [int]$Matches[1]
    $minor = [int]$Matches[2]
    $patch = [int]$Matches[3]
    return [PSCustomObject]@{
        Version = "$major.$minor.$patch"
        Major   = $major
        Minor   = $minor
        Patch   = $patch
    }
}

function Compare-SemVer {
    param(
        [object]$Left,
        [object]$Right
    )
    if ($Left.Major -ne $Right.Major) {
        return $Left.Major - $Right.Major
    }
    if ($Left.Minor -ne $Right.Minor) {
        return $Left.Minor - $Right.Minor
    }
    return $Left.Patch - $Right.Patch
}

function Get-LatestSemVer {
    $candidates = New-Object System.Collections.Generic.List[object]

    if (Test-Path "VERSION") {
        $parsed = Parse-SemVer ((Read-TextFile "VERSION").Trim())
        if ($parsed) {
            [void]$candidates.Add($parsed)
        }
    }

    $tags = git tag -l "v*" 2>$null
    foreach ($tag in @($tags)) {
        $trimmed = "$tag".Trim()
        if (-not $trimmed) {
            continue
        }
        $parsed = Parse-SemVer $trimmed
        if ($parsed) {
            [void]$candidates.Add($parsed)
        }
    }

    if (Test-Path "CHANGELOG.md") {
        $changelog = Read-TextFile "CHANGELOG.md"
        $matches = [regex]::Matches($changelog, '(?m)^##\s+(\d+\.\d+\.\d+)\b')
        foreach ($match in $matches) {
            $parsed = Parse-SemVer $match.Groups[1].Value
            if ($parsed) {
                [void]$candidates.Add($parsed)
            }
        }
    }

    if ($candidates.Count -eq 0) {
        return $null
    }

    $latest = $candidates[0]
    for ($i = 1; $i -lt $candidates.Count; $i++) {
        if ((Compare-SemVer -Left $candidates[$i] -Right $latest) -gt 0) {
            $latest = $candidates[$i]
        }
    }
    return $latest
}

function Get-NextVersion {
    param([object]$BaseVersion)
    $major = $BaseVersion.Major
    $minor = $BaseVersion.Minor
    $patch = $BaseVersion.Patch
    if ($patch -ge 9) {
        $minor += 1
        $patch = 0
    } else {
        $patch += 1
    }
    return "$major.$minor.$patch"
}

function Require-Success {
    param(
        [int]$Code,
        [string]$Message
    )
    if ($Code -ne 0) {
        throw $Message
    }
}

Write-Host ""
Write-Info "Somnia Release"
Write-Host ""

$status = git status --porcelain
Require-Success $LASTEXITCODE "Failed to inspect git status."
if ($status) {
    Write-Fail "Working tree is not clean. Please commit or stash changes first."
    git status --short
    exit 1
}
Write-Success "Working tree is clean."

$currentVersion = ""
if (Test-Path "VERSION") {
    $currentVersion = (Read-TextFile "VERSION").Trim()
}

if ([string]::IsNullOrWhiteSpace($Version)) {
    $latest = Get-LatestSemVer
    if ($latest) {
        $Version = Get-NextVersion $latest
        Write-Info ("Auto-detected next version: {0} -> {1}" -f $latest.Version, $Version)
    } else {
        $Version = "0.1.0"
        Write-Info ("No previous version found, defaulting to {0}" -f $Version)
    }
}

if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    Write-Fail ("Invalid version format: {0}. Expected semver x.y.z" -f $Version)
    exit 1
}

Write-Warn ("Current version: {0}" -f $currentVersion)
Write-Success ("Target version: {0}" -f $Version)
Write-Host ""

if ($Dry) {
    Write-Warn "DRY RUN mode enabled. No files or git state will be changed."
    Write-Host ""
}

if (-not $Dry) {
    Write-TextFile -Path "VERSION" -Content $Version -NoNewline
    Write-Success ("Updated VERSION to {0}" -f $Version)
}

if (-not $Dry) {
    & powershell.exe -ExecutionPolicy Bypass -File "scripts\sync-version.ps1"
    Require-Success $LASTEXITCODE "sync-version.ps1 failed."
}

$today = Get-Date -Format "yyyy-MM-dd"
if (-not $Dry) {
    $previousTag = ""
    $previousTagRaw = git describe --tags --match "v*" --abbrev=0 2>$null
    if ($LASTEXITCODE -eq 0 -and $previousTagRaw) {
        $previousTag = "$previousTagRaw".Trim()
    }

    $logRange = "HEAD"
    if ($previousTag) {
        $logRange = "$previousTag..HEAD"
    }

    $logLines = git log $logRange --no-merges --pretty=format:"- %s (%h)"
    Require-Success $LASTEXITCODE "Failed to build changelog from git log."
    $logLines = @($logLines | Where-Object { "$_".Trim() -ne "" })
    if (-not $logLines -or $logLines.Count -eq 0) {
        $logLines = @("- Maintenance release.")
    }

    $changes = ($logLines -join "`n")
    $existing = Read-TextFile "CHANGELOG.md"
    if ([string]::IsNullOrWhiteSpace($existing)) {
        $existing = "# Changelog`n"
    }
    $body = [regex]::Replace($existing, '^# Changelog\s*', '')
    $newEntry = "# Changelog`n`n## $Version ($today)`n`n$changes`n`n"
    Write-TextFile -Path "CHANGELOG.md" -Content ($newEntry + $body.TrimStart()) -NoNewline

    if ($previousTag) {
        Write-Success ("Updated CHANGELOG.md from {0}..HEAD" -f $previousTag)
    } else {
        Write-Success "Updated CHANGELOG.md from full git history."
    }
}

if (-not $Dry) {
    $existingTag = git tag -l "v$Version" 2>$null
    Require-Success $LASTEXITCODE "Failed to inspect existing git tags."
    if ($existingTag) {
        git tag -d "v$Version" 2>$null | Out-Null
        Require-Success $LASTEXITCODE ("Failed to delete existing local tag v{0}" -f $Version)
        Write-Warn ("Deleted existing local tag v{0}" -f $Version)
    }

    git add VERSION open_somnia/__init__.py npm/package.json CHANGELOG.md
    Require-Success $LASTEXITCODE "git add failed."
    git commit -m "release: v$Version"
    Require-Success $LASTEXITCODE "git commit failed."
    git tag "v$Version"
    Require-Success $LASTEXITCODE ("Failed to create tag v{0}" -f $Version)
    Write-Success ("Created release commit and tag v{0}" -f $Version)
}

if (-not $Dry) {
    Write-Host ""
    Write-Info "Pushing to remote repository..."

    $remote = $null
    $remotes = (git remote) -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ }
    Require-Success $LASTEXITCODE "Failed to inspect git remotes."

    foreach ($r in $remotes) {
        $url = git remote get-url $r 2>$null
        if ($LASTEXITCODE -eq 0 -and "$url" -match "github") {
            $remote = $r
            break
        }
    }

    if (-not $remote) {
        foreach ($r in $remotes) {
            if ($r -eq "origin") {
                $remote = $r
                break
            }
        }
    }

    if (-not $remote -and $remotes) {
        $remote = $remotes[0]
    }

    if (-not $remote) {
        Write-Fail "No git remote found. Push manually with:"
        Write-Host '  git push <remote> main'
        Write-Host ("  git push <remote> v{0}" -f $Version)
        exit 1
    }

    $branch = (git branch --show-current).Trim()
    Require-Success $LASTEXITCODE "Failed to detect current branch."
    git push $remote $branch
    Require-Success $LASTEXITCODE ("Failed to push branch {0} to {1}" -f $branch, $remote)
    git push $remote "v$Version"
    Require-Success $LASTEXITCODE ("Failed to push tag v{0} to {1}" -f $Version, $remote)
    Write-Success ("Pushed to {0} ({1} and v{2})" -f $remote, $branch, $Version)
}

Write-Host ""
if ($Dry) {
    Write-Warn "DRY RUN complete. Re-run without -Dry to execute the release."
} else {
    Write-Success ("Release v{0} pushed. CI should start automatically." -f $Version)
    Write-Host ""
    Write-Host ("  PyPI:   https://pypi.org/project/somnia/{0}/" -f $Version)
    Write-Host "  npm:    npm install somnia"
    Write-Host "  GitHub: Releases page"
    Write-Host "  CI:     GitHub Actions"
}
