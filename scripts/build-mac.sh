#!/usr/bin/env bash
# Builds the CLI helper, regenerates the Xcode project, and opens it.
#
# Typical dev workflow before working in Xcode:
#   ./scripts/build-mac.sh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

"$ROOT_DIR/scripts/build-cli.sh"

cd "$ROOT_DIR/mac"
xcodegen generate

open "$ROOT_DIR/mac/ScannyBoy.xcodeproj"
