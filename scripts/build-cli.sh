#!/usr/bin/env bash
# Freezes the Python CLI into `ScannyBoyCLI.app` and stages it for the macOS
# app, so the shipped app has no Python dependency.
#
# See docs/IMPLEMENTATION_PLAN.md section 5.2. PyInstaller also writes a plain
# `cli/dist/scanny-boy/` directory; that one is ignored on purpose — only the
# `.app` is shipped, because a bundle is what Xcode can copy into
# `Contents/Helpers` with Code Sign On Copy.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLI_DIR="$ROOT_DIR/cli"
APP_NAME="ScannyBoyCLI.app"
BUILT_APP="$CLI_DIR/dist/$APP_NAME"
DEST_DIR="$ROOT_DIR/mac/ScannyBoy/Helpers"
DEST_APP="$DEST_DIR/$APP_NAME"

cd "$CLI_DIR"
uv run pyinstaller build/scanny_boy.spec \
  --distpath dist \
  --workpath build/pyinstaller-work \
  --noconfirm

# PyInstaller ad-hoc signs the bundle it produces. Verify that before the
# helper can be copied into the outer app: a bundle that fails here would
# fail again, less legibly, inside Xcode's Code Sign On Copy phase.
echo
echo "Verifying the helper's signature:"
codesign --verify --strict --verbose=1 "$BUILT_APP"

# Inspect what the bundle actually links against rather than assuming no
# PyInstaller hook is missing. Everything should resolve inside the bundle
# (@rpath, @loader_path) or to a system location.
echo
echo "Library dependencies of the helper executable:"
otool -L "$BUILT_APP/Contents/MacOS/scanny-boy"
echo
echo "Library dependencies of the bundled LibRaw:"
otool -L "$(find "$BUILT_APP/Contents/Frameworks" -name 'libraw_r.*.dylib' | head -n 1)"

rm -rf "$DEST_APP"
mkdir -p "$DEST_DIR"
ditto "$BUILT_APP" "$DEST_APP"

echo
echo "Built $BUILT_APP"
echo "Staged $DEST_APP for the macOS app's Contents/Helpers"
