#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

TARGET_TRIPLE="${1:-${CARGO_BUILD_TARGET:-}}"
BUNDLES="${2:-${BUNDLES:-all}}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
NPM_BIN="${NPM_BIN:-npm}"

if [[ -z "$TARGET_TRIPLE" ]]; then
  TARGET_TRIPLE="$(rustc -vV | awk '/^host:/ { print $2 }')"
fi

if [[ -z "$TARGET_TRIPLE" ]]; then
  echo "Unable to determine the Rust target triple. Pass it as the first argument." >&2
  exit 1
fi

echo "==> Building bundled sidecar for $TARGET_TRIPLE"
"$PYTHON_BIN" scripts/release/build_desktop_sidecar.py --target-triple "$TARGET_TRIPLE"

cd "$ROOT/desktop/ui"
echo "==> Installing desktop UI dependencies"
"$NPM_BIN" ci

echo "==> Building Tauri bundles ($BUNDLES)"
"$NPM_BIN" run tauri:build -- --config src-tauri/tauri.bundle.conf.json --target "$TARGET_TRIPLE" --bundles "$BUNDLES" --ci --no-sign
