#!/usr/bin/env bash
# Freezes the Python CLI into a standalone binary and copies it into the
# macOS app's bundle resources so the shipped app has no Python dependency.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLI_DIR="$ROOT_DIR/cli"
DEST_DIR="$ROOT_DIR/mac/ScannyBoy/Resources/cli"

cd "$CLI_DIR"
pyinstaller build/scanny_boy.spec --distpath dist --workpath build/pyinstaller-work --noconfirm

mkdir -p "$DEST_DIR"
cp dist/scanny-boy "$DEST_DIR/scanny-boy"

echo "Built CLI copied to $DEST_DIR/scanny-boy"
