#!/usr/bin/env bash
# One-time setup for new contributors: Python venv + dev dependencies.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

python3 -m venv "$ROOT_DIR/cli/.venv"
source "$ROOT_DIR/cli/.venv/bin/activate"
pip install -e "$ROOT_DIR/cli[dev]"

echo "Python venv ready at cli/.venv. Activate with: source cli/.venv/bin/activate"
echo "Open mac/ScannyBoy.xcodeproj in Xcode for the macOS app."
