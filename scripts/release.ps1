# release.ps1 — Somnia 发版脚本 (Windows PowerShell)
# =============================================================
#  用法:
#    powershell -File scripts\release.ps1 0.2.0
#    powershell -File scripts\release.ps1 0.2.0 -Dry
#
#  流程 (本地):
#    1. 检查工作区干净
#    2. 更新 VERSION → 同步版本号
#    3. 更新 CHANGELOG
#    4. git commit + tag
#    5. git push (触发 CI 自动发布 PyPI + npm + GitHub Release)
#
#  CI 自动完成:
#    - PyPI 发布
#    - npm 发布
#    - GitHub Release 创建
# =============================================================

param(
    [Parameter(Mandatory=$true)]
    [string]$Version,

    [switch]$Dry
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

Write-Host ""
Write-Host "🚀 Somnia Release" -ForegroundColor Cyan
Write-Host ""

# ─── 1. 检查工作区干净 ──────────────────────────────────────
$status = git status --porcelain
if ($status) {
    Write-Host "✗ 工作区有未提交的更改，请先 commit 或 stash" -ForegroundColor Red
    git status --short
    exit 1
}
Write-Host "✓ 工作区干净" -ForegroundColor Green

# ─── 2. 验证版本号格式 ──────────────────────────────────────
if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    Write-Host "✗ 版本号格式错误: $Version (需要 semver: x.y.z)" -ForegroundColor Red
    exit 1
}

$currentVersion = (Get-Content "VERSION" -Raw).Trim()
Write-Host "  当前版本: $currentVersion" -ForegroundColor Yellow
Write-Host "  目标版本: $Version" -ForegroundColor Green
Write-Host ""

if ($Dry) {
    Write-Host "👀 DRY RUN — 不会实际修改任何内容" -ForegroundColor Yellow
    Write-Host ""
}

# ─── 3. 更新 VERSION 文件 ────────────────────────────────────
if (-not $Dry) {
    Set-Content "VERSION" $Version -NoNewline
    Write-Host "✓ VERSION → $Version" -ForegroundColor Green
}

# ─── 4. 同步版本号 ───────────────────────────────────────────
if (-not $Dry) {
    & powershell -File "scripts\sync-version.ps1"
}

# ─── 5. 更新 CHANGELOG.md ────────────────────────────────────
$today = Get-Date -Format "yyyy-MM-dd"
if (-not $Dry) {
    $changelog = Get-Content "CHANGELOG.md" -Raw
    $newEntry = "# Changelog`n`n## $Version ($today)`n`n- (请手动补充 changelog 条目)`n"
    $changelog = $changelog -replace "^# Changelog", $newEntry
    Set-Content "CHANGELOG.md" $changelog -NoNewline
    Write-Host "✓ CHANGELOG.md 已添加 $Version 条目" -ForegroundColor Green
}

# ─── 6. Git commit + tag ─────────────────────────────────────
if (-not $Dry) {
    # 删除已有同名 tag（如果有）
    $existingTag = git tag -l "v$Version" 2>$null
    if ($existingTag) {
        git tag -d "v$Version" 2>$null
        Write-Host "⚠ 删除已有本地 tag v$Version" -ForegroundColor Yellow
    }

    git add VERSION open_somnia/__init__.py npm/package.json CHANGELOG.md
    git commit -m "release: v$Version"
    git tag "v$Version"
    Write-Host "✓ git commit + tag v$Version" -ForegroundColor Green
}

# ─── 7. 推送到 GitHub → 触发 CI ─────────────────────────────
if (-not $Dry) {
    Write-Host ""
    Write-Host "📤 推送到远程仓库 ..." -ForegroundColor Cyan

    # 自动检测远程名（优先 github.com，其次 origin，再取第一个）
    $remote = $null
    $remotes = (git remote) -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ }
    
    foreach ($r in $remotes) {
        $url = (git remote get-url $r 2>$null)
        if ($url -match "github") {
            $remote = $r
            break
        }
    }
    if (-not $remote) {
        foreach ($r in $remotes) {
            if ($r -eq "origin") { $remote = $r; break }
        }
    }
    if (-not $remote -and $remotes) {
        $remote = $remotes[0]
    }

    if (-not $remote) {
        Write-Host "✗ 找不到 git remote，请手动推送:" -ForegroundColor Red
        Write-Host "  git push <remote> main"
        Write-Host "  git push <remote> v$Version"
        exit 1
    }

    $branch = (git branch --show-current)
    git push $remote $branch
    git push $remote "v$Version"
    Write-Host "✓ 已推送到 $remote ($branch + v$Version)" -ForegroundColor Green
}

# ─── 完成 ─────────────────────────────────────────────────────
Write-Host ""
if ($Dry) {
    Write-Host "👀 DRY RUN 完成 — 去掉 -Dry 即可实际执行" -ForegroundColor Yellow
} else {
    Write-Host "✅ v$Version 已推送！CI 将自动执行:" -ForegroundColor Green
    Write-Host ""
    Write-Host "  📦 PyPI  →  https://pypi.org/project/somnia/$Version/"
    Write-Host "  📦 npm   →  npm install somnia"
    Write-Host "  📋 Release → GitHub Releases 页面"
    Write-Host ""
    Write-Host "  查看 CI 进度: GitHub → Actions 标签页"
}
