#!/usr/bin/env bash
# =============================================================
#  Somnia — 发版脚本 (macOS / Linux)
# =============================================================
#  用法:
#    bash scripts/release.sh 0.2.0              # 正式发布
#    bash scripts/release.sh 0.2.0 --dry        # 预览
#
#  流程 (本地):
#    1. 检查工作区干净
#    2. 更新 VERSION → 同步版本号
#    3. 更新 CHANGELOG
#    4. git commit + tag
#    5. git push (触发 CI 自动发布)
#
#  CI 自动完成:
#    - PyPI 发布
#    - npm 发布
#    - GitHub Release 创建
# =============================================================

set -euo pipefail
cd "$(dirname "$0")/.."

if [ $# -lt 1 ]; then
  echo "用法: bash scripts/release.sh <version> [--dry]"
  echo "示例: bash scripts/release.sh 0.2.0"
  exit 1
fi

NEW_VERSION="$1"
DRY_RUN=false
if [ "${2:-}" = "--dry" ]; then DRY_RUN=true; fi

BOLD='\033[1m'
GREEN='\033[32m'
RED='\033[31m'
CYAN='\033[36m'
YELLOW='\033[33m'
RESET='\033[0m'

echo ""
echo -e "${BOLD}${CYAN}🚀 Somnia Release${RESET}"
echo ""

# ─── 1. 检查工作区干净 ──────────────────────────────────────
if [ -n "$(git status --porcelain)" ]; then
  echo -e "${RED}✗ 工作区有未提交的更改${RESET}"
  git status --short
  exit 1
fi
echo -e "${GREEN}✓${RESET} 工作区干净"

# ─── 2. 验证版本号 ───────────────────────────────────────────
if ! echo "$NEW_VERSION" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo -e "${RED}✗ 版本号格式错误: $NEW_VERSION (需要 semver: x.y.z)${RESET}"
  exit 1
fi

CURRENT_VERSION=$(cat VERSION | tr -d '[:space:]')
echo -e "  当前: ${YELLOW}$CURRENT_VERSION${RESET}  →  目标: ${GREEN}$NEW_VERSION${RESET}"
echo ""

if [ "$DRY_RUN" = true ]; then
  echo -e "${YELLOW}👀 DRY RUN${RESET}"
  echo ""
fi

# ─── 3. 更新 VERSION + 同步 ─────────────────────────────────
if [ "$DRY_RUN" = false ]; then
  echo "$NEW_VERSION" > VERSION
  bash scripts/sync-version.sh
fi

# ─── 4. 更新 CHANGELOG ──────────────────────────────────────
TODAY=$(date +%Y-%m-%d)
if [ "$DRY_RUN" = false ]; then
  sed -i.bak "s|# Changelog|# Changelog\n\n## $NEW_VERSION ($TODAY)\n\n- (请手动补充 changelog)\n|" CHANGELOG.md
  rm -f CHANGELOG.md.bak
  echo -e "${GREEN}✓${RESET} CHANGELOG.md"
fi

# ─── 5. Git commit + tag ─────────────────────────────────────
if [ "$DRY_RUN" = false ]; then
  git add VERSION openagent/__init__.py npm/package.json CHANGELOG.md
  git commit -m "release: v$NEW_VERSION"
  git tag "v$NEW_VERSION"
  echo -e "${GREEN}✓${RESET} git commit + tag v$NEW_VERSION"
fi

# ─── 6. 推送 → 触发 CI ──────────────────────────────────────
if [ "$DRY_RUN" = false ]; then
  echo ""
  echo -e "${CYAN}📤 推送到 GitHub ...${RESET}"
  git push origin main
  git push origin "v$NEW_VERSION"
  echo -e "${GREEN}✓${RESET} 推送完成"
fi

# ─── 完成 ─────────────────────────────────────────────────────
echo ""
if [ "$DRY_RUN" = true ]; then
  echo -e "${YELLOW}👀 DRY RUN 完成 — 去掉 --dry 即可执行${RESET}"
else
  echo -e "${GREEN}${BOLD}✅ v$NEW_VERSION 已推送！CI 将自动发布:${RESET}"
  echo ""
  echo "  📦 PyPI    → https://pypi.org/project/somnia/$NEW_VERSION/"
  echo "  📦 npm     → npm install somnia"
  echo "  📋 Release → GitHub Releases"
  echo ""
  echo "  查看 CI: GitHub → Actions"
fi
