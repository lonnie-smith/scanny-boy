# Scanny Boy (macOS app)

SwiftUI macOS app that invokes the bundled `scanny-boy` CLI binary as a
subprocess (see `ScannyBoy/CLIBridge/CLIRunner.swift`) and the interface
contract at [`../shared/contract/CONTRACT.md`](../shared/contract/CONTRACT.md).

## Note

This directory currently has Swift source files scaffolded but no
`.xcodeproj` yet — create one in Xcode (New Project > macOS > App), name the
target `ScannyBoy`, and add the existing files under `ScannyBoy/`,
`ScannyBoyTests/`, and `ScannyBoyUITests/` to their respective targets,
rather than using Xcode's generated placeholders.

Add a "Run Script" build phase that runs `../scripts/build-cli.sh` before
"Copy Bundle Resources" so the CLI binary is present when the app builds.
