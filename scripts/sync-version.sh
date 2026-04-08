#!/usr/bin/env bash
# =============================================================
#  sync-version.sh — 从 VERSION 文件同步版本号到所有位置
# =============================================================
#  用法: bash scripts/sync-version.sh
#
#  版本号单点维护: somnia/VERSION
#  同步到:
#    1. open_somnia/__init__.py  (__version__)
#    2. npm/package.json       (version)
# =============================================================

set -euo pipefail
cd "$(dirname "$0")/.."

VERSION=$(cat VERSION | tr -d '[:space:]')

echo "📌 版本号: $VERSION"

# ─── 1. sync __init__.py ─────────────────────────────────────
INIT_FILE="open_somnia/__init__.py"
if [ -f "$INIT_FILE" ]; then
  sed -i.bak "s/__version__ = \".*\"/__version__ = \"$VERSION\"/" "$INIT_FILE"
  rm -f "${INIT_FILE}.bak"
  echo "  ✅ $INIT_FILE"
else
  echo "  ⚠️  $INIT_FILE not found"
fi

# ─── 2. sync npm/package.json ────────────────────────────────
NPM_FILE="npm/package.json"
if [ -f "$NPM_FILE" ]; then
  # cross-platform: use python for JSON editing
  python -c "
import json, sys
with open('$NPM_FILE', 'r') as f:
    pkg = json.load(f)
pkg['version'] = '$VERSION'
with open('$NPM_FILE', 'w') as f:
    json.dump(pkg, f, indent=2)
    f.write('\n')
"
  echo "  ✅ $NPM_FILE"
else
  echo "  ⚠️  $NPM_FILE not found"
fi

echo ""
echo "✅ 所有版本号已同步为 $VERSION"
