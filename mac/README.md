# Scanny Boy (macOS app)

SwiftUI macOS app that runs the bundled `scanny-boy` command-line program as a
subprocess and reads its JSON event stream. The interface between them is
[`../shared/contract/CONTRACT.md`](../shared/contract/CONTRACT.md); the Swift
side of it lives in [`ScannyBoy/CLIBridge/`](ScannyBoy/CLIBridge).

## Building

`ScannyBoy.xcodeproj` is generated from `project.yml` and is not committed.
Generating it requires the CLI helper to exist first, because `project.yml`
names `ScannyBoy/Helpers/ScannyBoyCLI.app` in a copy-files phase and that
bundle is build output, ignored by Git and absent from a clean checkout:

```bash
./scripts/build-cli.sh
```

```bash
cd mac && xcodegen generate
```

Then open `ScannyBoy.xcodeproj`, or build and test from the command line:

```bash
cd mac && xcodebuild test -scheme ScannyBoy -destination 'platform=macOS'
```

Rerun `build-cli.sh` whenever the Python program changes: the app copies the
staged helper into `ScannyBoy.app/Contents/Helpers` with Code Sign On Copy, so
the helper is signed before the outer app.

## Finding the helper at run time

`CLILocator` resolves the executable at
`Contents/Helpers/ScannyBoyCLI.app/Contents/MacOS/scanny-boy`. A Debug build
also honours an **absolute** `SCANNY_BOY_CLI` path, which is useful for
pointing the app at a freshly built `cli/dist/` helper:

```bash
SCANNY_BOY_CLI=/absolute/path/to/scanny-boy open -a ScannyBoy
```

A relative override is rejected rather than resolved, and a Release build
ignores the variable entirely — nothing is ever found relative to the
process's current directory.

## Layout

- `ScannyBoy/App/`, `ScannyBoy/Views/` — the SwiftUI app.
- `ScannyBoy/CLIBridge/` — event decoding (`CLIEvent`), line reassembly
  (`LineAssembler`), the owned streaming session (`CLISession`), helper
  resolution (`CLILocator`), and argument construction (`CLIRunner`).
- `ScannyBoyTests/` — Swift Testing unit tests, plus end-to-end tests that
  drive the real helper and skip with a reason when the helper or the sample
  NEFs are absent.
- `ScannyBoyUITests/` — XCTest UI tests. Built on every test run but not in
  the `ScannyBoy` scheme's test targets: the XCUITest runner fails to start on
  this machine ("Test runner never began executing tests after launching"), so
  running it would make CI fail at random. Chunk 10 owns the run UI these
  belong to.
- `Scripts/check-staged-helper.sh` — pre-build check that fails legibly when
  the staged helper has been cleaned away.
