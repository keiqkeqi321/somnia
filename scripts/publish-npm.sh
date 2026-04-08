#!/usr/bin/env bash
# =============================================================
#  Somnia — publish npm wrapper package
# =============================================================
#  Usage:
#    # Dry run
#    bash scripts/publish-npm.sh --dry
#
#    # Publish
#    bash scripts/publish-npm.sh
#
#  Prerequisites:
#    npm login
# =============================================================

set -euo pipefail
cd "$(dirname "$0")/../npm"

DRY_RUN=false
if [[ "${1:-}" == "--dry" ]]; then
  DRY_RUN=true
fi

echo "📦 Packing npm package ..."
npm pack --dry-run 2>&1

if $DRY_RUN; then
  echo ""
  echo "👀 Dry run complete. Files that would be published:"
  echo ""
  npm pack --dry-run 2>&1 | tail -n +2
  echo ""
  echo "To actually publish, run: bash scripts/publish-npm.sh"
else
  echo ""
  echo "🚀 Publishing to npm ..."
  npm publish --access public
  echo ""
  echo "✅ Done! Users can now run:"
  echo "   npx somnia"
  echo ""
  echo "   # or install globally:"
  echo "   npm install -g somnia"
fi
