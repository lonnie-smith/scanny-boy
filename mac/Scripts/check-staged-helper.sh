#!/usr/bin/env bash
# Fails the build with a legible message when the staged CLI helper is
# missing.
#
# `xcodegen generate` already refuses to run without it, because project.yml
# names the bundle in a copy-files phase. This catches the other order: a
# project generated while the helper existed, then built after `cli/dist/` or
# `mac/ScannyBoy/Helpers/` was cleaned. Xcode's own error for that case names
# a path inside DerivedData and does not mention `build-cli.sh`.
set -euo pipefail

HELPER="$SRCROOT/ScannyBoy/Helpers/ScannyBoyCLI.app/Contents/MacOS/scanny-boy"

if [ ! -x "$HELPER" ]; then
  echo "error: the staged CLI helper is missing at $HELPER" >&2
  echo "note: run ./scripts/build-cli.sh from the repository root, then build again." >&2
  exit 1
fi
