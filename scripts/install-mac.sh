#!/usr/bin/env bash
# Builds a Release ScannyBoy.app and installs it into /Applications.
#
#   ./scripts/install-mac.sh                 # install to /Applications
#   ./scripts/install-mac.sh ~/Applications  # install somewhere else
#
# Set SIGN_IDENTITY to re-sign the installed app with a stable identity (for
# example "Apple Development: Name (TEAMID)"). Ad-hoc signatures change on
# every rebuild, so macOS asks again for folder and camera permissions after
# each reinstall; a real identity keeps those grants.
#
# This is the local-install path only. There is no Developer ID signing or
# notarisation here (docs/ARCHITECTURE.md section 15).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST_DIR="${1:-/Applications}"
BUILD_DIR="$ROOT_DIR/mac/build"
BUILT_APP="$BUILD_DIR/Build/Products/Release/ScannyBoy.app"
DEST_APP="$DEST_DIR/ScannyBoy.app"

"$ROOT_DIR/scripts/build-cli.sh"

cd "$ROOT_DIR/mac"
xcodegen generate

xcodebuild build \
  -quiet \
  -scheme ScannyBoy \
  -configuration Release \
  -destination "platform=macOS,arch=$(uname -m)" \
  -derivedDataPath "$BUILD_DIR"

if [[ ! -d "$BUILT_APP" ]]; then
  echo "error: Release build did not produce $BUILT_APP" >&2
  exit 1
fi

# Sign the helper first, then the outer app, so the nested bundle's signature
# is never invalidated by signing the container.
if [[ -n "${SIGN_IDENTITY:-}" ]]; then
  echo "Re-signing with: $SIGN_IDENTITY"
  codesign --force --sign "$SIGN_IDENTITY" \
    "$BUILT_APP/Contents/Helpers/ScannyBoyCLI.app"
  codesign --force --sign "$SIGN_IDENTITY" "$BUILT_APP"
fi

echo
echo "Verifying the built app's signature:"
codesign --verify --deep --strict --verbose=1 "$BUILT_APP"

HELPER="$BUILT_APP/Contents/Helpers/ScannyBoyCLI.app/Contents/MacOS/scanny-boy"
if [[ ! -x "$HELPER" ]]; then
  echo "error: helper missing from the built app: $HELPER" >&2
  exit 1
fi

# Quit a running copy so the replacement is not swapped out from under it.
if pgrep -x ScannyBoy >/dev/null; then
  echo "ScannyBoy is running; asking it to quit."
  osascript -e 'tell application "ScannyBoy" to quit' || true
  for _ in {1..20}; do
    pgrep -x ScannyBoy >/dev/null || break
    sleep 0.5
  done
  if pgrep -x ScannyBoy >/dev/null; then
    echo "error: ScannyBoy did not quit; close it and rerun." >&2
    exit 1
  fi
fi

mkdir -p "$DEST_DIR"
rm -rf "$DEST_APP"
# ditto keeps the bundle's signature, symlinks and extended attributes intact.
ditto "$BUILT_APP" "$DEST_APP"

codesign --verify --deep --strict "$DEST_APP"

echo
echo "Installed $DEST_APP"
echo "Launch it from Finder, Spotlight, or: open -a \"$DEST_APP\""
