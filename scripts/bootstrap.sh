#!/usr/bin/env bash
# One-time setup for new contributors: Python environment + dev dependencies.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$ROOT_DIR/cli"
uv sync

echo "Python environment ready at cli/.venv (managed by uv)."
echo "Open mac/ScannyBoy.xcodeproj in Xcode for the macOS app."
