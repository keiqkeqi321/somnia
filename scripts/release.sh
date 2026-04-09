#!/usr/bin/env bash
# =============================================================
#  Somnia — 发版脚本 (macOS / Linux)
# =============================================================
#  用法:
#    bash scripts/release.sh 0.2.0              # 正式发布
#    bash scripts/release.sh 0.2.0 --dry        # 预览
#    bash scripts/release.sh                    # 自动推断版本后正式发布
#    bash scripts/release.sh --dry              # 自动推断版本后预览
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

parse_semver() {
  local input="${1#v}"
  if [[ "$input" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
    printf '%s %s %s %s\n' "$input" "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "${BASH_REMATCH[3]}"
    return 0
  fi
  return 1
}

compare_semver() {
  local left_major="$1"
  local left_minor="$2"
  local left_patch="$3"
  local right_major="$4"
  local right_minor="$5"
  local right_patch="$6"
  if (( left_major != right_major )); then
    (( left_major > right_major )) && return 0 || return 1
  fi
  if (( left_minor != right_minor )); then
    (( left_minor > right_minor )) && return 0 || return 1
  fi
  (( left_patch > right_patch ))
}

get_latest_semver() {
  local latest_version=""
  local latest_major=0
  local latest_minor=0
  local latest_patch=0
  local found=false
  local parsed version major minor patch

  if [ -f "VERSION" ]; then
    parsed=$(parse_semver "$(tr -d '[:space:]' < VERSION)" || true)
    if [ -n "$parsed" ]; then
      read -r version major minor patch <<<"$parsed"
      latest_version="$version"
      latest_major="$major"
      latest_minor="$minor"
      latest_patch="$patch"
      found=true
    fi
  fi

  while IFS= read -r tag; do
    [ -n "$tag" ] || continue
    parsed=$(parse_semver "$tag" || true)
    if [ -z "$parsed" ]; then
      continue
    fi
    read -r version major minor patch <<<"$parsed"
    if [ "$found" = false ] || compare_semver "$major" "$minor" "$patch" "$latest_major" "$latest_minor" "$latest_patch"; then
      latest_version="$version"
      latest_major="$major"
      latest_minor="$minor"
      latest_patch="$patch"
      found=true
    fi
  done < <(git tag -l "v*" 2>/dev/null || true)

  if [ -f "CHANGELOG.md" ]; then
    while IFS= read -r version; do
      parsed=$(parse_semver "$version" || true)
      if [ -z "$parsed" ]; then
        continue
      fi
      read -r version major minor patch <<<"$parsed"
      if [ "$found" = false ] || compare_semver "$major" "$minor" "$patch" "$latest_major" "$latest_minor" "$latest_patch"; then
        latest_version="$version"
        latest_major="$major"
        latest_minor="$minor"
        latest_patch="$patch"
        found=true
      fi
    done < <(sed -nE 's/^##[[:space:]]+([0-9]+\.[0-9]+\.[0-9]+)\b.*/\1/p' CHANGELOG.md)
  fi

  if [ "$found" = true ]; then
    printf '%s %s %s %s\n' "$latest_version" "$latest_major" "$latest_minor" "$latest_patch"
  fi
}

get_next_version() {
  local major="$1"
  local minor="$2"
  local patch="$3"
  if (( patch >= 9 )); then
    minor=$((minor + 1))
    patch=0
  else
    patch=$((patch + 1))
  fi
  printf '%s.%s.%s\n' "$major" "$minor" "$patch"
}

NEW_VERSION=""
DRY_RUN=false
for arg in "$@"; do
  case "$arg" in
    --dry)
      DRY_RUN=true
      ;;
    "")
      ;;
    *)
      if [ -z "${NEW_VERSION:-}" ]; then
        NEW_VERSION="$arg"
      else
        echo "用法: bash scripts/release.sh [version] [--dry]"
        echo "示例: bash scripts/release.sh 0.2.0"
        echo "示例: bash scripts/release.sh --dry"
        exit 1
      fi
      ;;
  esac
done
NEW_VERSION="${NEW_VERSION:-}"

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
CURRENT_VERSION=$(cat VERSION | tr -d '[:space:]')
if [ -z "$NEW_VERSION" ]; then
  LATEST_INFO=$(get_latest_semver)
  if [ -n "$LATEST_INFO" ]; then
    read -r latest_version latest_major latest_minor latest_patch <<<"$LATEST_INFO"
    NEW_VERSION=$(get_next_version "$latest_major" "$latest_minor" "$latest_patch")
    echo -e "  未传版本号，自动推断: ${YELLOW}$latest_version${RESET}  →  ${GREEN}$NEW_VERSION${RESET}"
  else
    NEW_VERSION="0.1.0"
    echo -e "  未检测到历史版本，默认使用: ${GREEN}$NEW_VERSION${RESET}"
  fi
fi

if ! echo "$NEW_VERSION" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo -e "${RED}✗ 版本号格式错误: $NEW_VERSION (需要 semver: x.y.z)${RESET}"
  exit 1
fi

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

# ─── 4. 更新 CHANGELOG (根据 git 自动生成) ──────────────────
TODAY=$(date +%Y-%m-%d)
if [ "$DRY_RUN" = false ]; then
  PREV_TAG=$(git describe --tags --match "v*" --abbrev=0 2>/dev/null || true)
  if [ -n "$PREV_TAG" ]; then
    CHANGE_LINES=$(git log "${PREV_TAG}..HEAD" --no-merges --pretty=format:'- %s (%h)' || true)
  else
    CHANGE_LINES=$(git log HEAD --no-merges --pretty=format:'- %s (%h)' || true)
  fi
  if [ -z "$CHANGE_LINES" ]; then
    CHANGE_LINES="- Maintenance release."
  fi

  EXISTING_BODY=$(sed '1{/^# Changelog$/d;}' CHANGELOG.md)
  {
    echo "# Changelog"
    echo
    echo "## $NEW_VERSION ($TODAY)"
    echo
    printf "%s\n" "$CHANGE_LINES"
    echo
    printf "%s\n" "$EXISTING_BODY"
  } > CHANGELOG.md.tmp
  mv CHANGELOG.md.tmp CHANGELOG.md

  if [ -n "$PREV_TAG" ]; then
    echo -e "${GREEN}✓${RESET} CHANGELOG.md (from $PREV_TAG..HEAD)"
  else
    echo -e "${GREEN}✓${RESET} CHANGELOG.md (from full history)"
  fi
fi

# ─── 5. Git commit + tag ─────────────────────────────────────
if [ "$DRY_RUN" = false ]; then
  git add VERSION open_somnia/__init__.py npm/package.json CHANGELOG.md
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
