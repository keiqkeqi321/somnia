#!/usr/bin/env bash
# =============================================================
#  Somnia — build & publish to PyPI
# =============================================================
#  Usage:
#    # Test publish (TestPyPI)
#    bash scripts/publish-pypi.sh --test
#
#    # Production publish (PyPI)
#    bash scripts/publish-pypi.sh
#
#  Prerequisites:
#    pip install build twine
#    # For TestPyPI:  twine register on https://test.pypi.org/
#    # For PyPI:      twine register on https://pypi.org/
# =============================================================

set -euo pipefail
cd "$(dirname "$0")/.."

TEST_MODE=false
if [[ "${1:-}" == "--test" ]]; then
  TEST_MODE=true
fi

echo "🧹 Cleaning old dist/ ..."
rm -rf dist/

echo "📦 Building package ..."
python -m build

echo ""
echo "📋 Built artifacts:"
ls -lh dist/

echo ""
echo "🔍 Checking with twine ..."
twine check dist/*

if $TEST_MODE; then
  echo ""
  echo "🚀 Uploading to TestPyPI ..."
  twine upload --repository testpypi dist/*
  echo ""
  echo "✅ Done! Install from TestPyPI with:"
  echo "   pip install somnia"
else
  echo ""
  echo "🚀 Uploading to PyPI ..."
  twine upload dist/*
  echo ""
  echo "✅ Done! Install with:"
  echo "   pip install somnia"
fi
