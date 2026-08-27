# Scanny Boy

A macOS desktop app (Swift) that shells out to a Python CLI to do its work.

## Layout

- [`mac/`](mac/) — the SwiftUI macOS app. Open `ScannyBoy.xcodeproj` in Xcode.
- [`cli/`](cli/) — the Python CLI, packaged as `scanny-boy`.
- [`shared/contract/`](shared/contract/) — the interface between the two:
  CLI argument/output shape both sides agree on.
- [`scripts/`](scripts/) — repo-wide dev scripts.

## Getting started

```bash
./scripts/bootstrap.sh   # sets up the Python venv
```

Then open `mac/ScannyBoy.xcodeproj` in Xcode for the macOS app.

## Building the CLI into the app

The macOS app doesn't run Python directly — it invokes a frozen, standalone
`scanny-boy` binary bundled as a resource. Build and copy it in with:

```bash
./scripts/build-cli.sh
```

This is normally wired up as an Xcode "Run Script" build phase so it happens
automatically before the app target builds.
