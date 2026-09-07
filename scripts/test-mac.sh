#!/usr/bin/env bash
# Runs the Swift test target the way it should always be run.
#
# Two things this exists to stop happening.
#
# 1. `mac/ScannyBoy.xcodeproj` is generated and gitignored, so a Swift file
#    added since the last `xcodegen generate` is simply absent from the
#    project. The build then fails with "Cannot find type 'X' in scope"
#    pointing at app source that is perfectly correct — a stale project
#    reported as a source error. Regenerating first makes that impossible.
#
# 2. A bare `xcodebuild test` prints ~1700 lines (750 KB) per run: the whole
#    build transcript, every `export FOO=…` of the build environment, and a
#    started/passed pair for each of the ~250 tests. The one line naming the
#    failure is lost in it. `-quiet` prints warnings, errors, and the failure
#    summary only — 12 lines for a passing-but-for-one-test run — and it does
#    NOT swallow compile diagnostics: those still arrive with file, line,
#    column and caret.
#
# Arguments are forwarded to xcodebuild, so the narrow runs work:
#
#   ./scripts/test-mac.sh -only-testing:ScannyBoyTests/EditModelTests
#   ./scripts/test-mac.sh -only-testing:ScannyBoyTests/CLIEventTests/schemaVersionMatches
#
# Set SCANNY_BOY_SLOW_TESTS=1 to include the multi-minute integration
# scenarios (see mac/ScannyBoyTests/TestSupport.swift).
set -euo pipefail

cd "$(dirname "$0")/../mac"

xcodegen generate >/dev/null

# Pinning the arch silences the three-line "Using the first of multiple
# matching destinations" WARNING xcodebuild opens every run with, because a
# bare `platform=macOS` matches both this Mac's arm64 and x86_64 entries.
# Taken from `uname -m` rather than hard-coded, so this is also correct on an
# Intel CI runner — the two spellings agree (`arm64`, `x86_64`).
exec xcodebuild test \
  -quiet \
  -scheme ScannyBoy \
  -destination "platform=macOS,arch=$(uname -m)" \
  "$@"
