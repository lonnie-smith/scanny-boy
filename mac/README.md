# Scanny Boy (macOS app)

SwiftUI macOS app that runs the bundled `scanny-boy` command-line program as a
subprocess and reads its JSON event stream. The interface between them is
[`../shared/contract/CONTRACT.md`](../shared/contract/CONTRACT.md); the Swift
side of it lives in [`ScannyBoy/CLIBridge/`](ScannyBoy/CLIBridge).

Local-only Apple-silicon build: ad-hoc signing, no sandboxing, no
notarisation, no Intel support. See
[`../docs/DECISIONS.md`](../docs/DECISIONS.md) for why.

## Building

Requires Xcode 16.2 and [XcodeGen](https://github.com/yonaskolb/XcodeGen)
2.46 or newer (`brew install xcodegen`), plus a working Python setup in
`../cli` (see the root README's "Building the app from a clean clone").

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

- `ScannyBoy/App/` — `ScannyBoyApp`, including the Re-stitch menu command.
  `ScannyBoy/Views/` — the SwiftUI app: folder selection, the grouping
  preview, Run's progress and results, and `RestitchSheet` for re-stitching a
  kept work directory.
- `ScannyBoy/Model/` — `ConfigurationModel` (what a `run` may do) and
  `RunModel` (one `convert`/`run`/`stitch` invocation: progress,
  cancellation, and the manifest it left behind), plus `RunManifest` (the
  read-back half of `../shared/contract/manifest.schema.json`, for a plain
  `convert`'s work directory) and `RollManifest` (the read-back half of
  `../shared/contract/roll-manifest.schema.json`, for a `run`/`stitch`'s
  output folder), and `ThumbnailLoader`, which renders the catalogue's
  previews.
- `ScannyBoy/CLIBridge/` — event decoding (`CLIEvent`), line reassembly
  (`LineAssembler`), the owned streaming session (`CLISession`), helper
  resolution (`CLILocator`), and argument construction (`CLIRunner`), which
  builds all four command invocations: `probe`, `convert`, `run`, `stitch`.
- `ScannyBoyTests/` — Swift Testing unit tests, plus end-to-end tests that
  drive the real helper and skip with a reason when the helper or the sample
  NEFs are absent.
- `ScannyBoyUITests/` — an XCTest launch smoke test. Chunk 8 kept this target
  out of the scheme's test targets because its runner would not start on this
  machine; it starts now, so Chunk 10 put it back. Everything the run UI
  decides is tested directly against `RunModel` and `ConfigurationModel`
  instead of through XCUITest — this target has proven intermittently flaky
  at actually launching the app under `xcodebuild test` on this development
  machine, independent of app changes; a failure here that isn't an assertion
  failure (`Failed to activate application ... Running Background`) is that,
  not a regression.
- `Scripts/check-staged-helper.sh` — pre-build check that fails legibly when
  the staged helper has been cleaned away.

**Known, deliberate limitation:** `probe --out` has no notion of
`scanny-boy-roll.json`, so `ConfigurationModel` and `RestitchSheet` cannot
show an itemized preview of what a rerun or re-stitch into an
already-published output folder would replace, the way they do for a plain
`convert`/`run`. Both ask for one general, explicit acknowledgement instead
and pass `--overwrite` unconditionally once it's given; real conflict
enforcement happens for real, server-side, in `run_stitch`. See
`docs/DECISIONS.md`'s Phase 2 section.
