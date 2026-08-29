# Scanny Boy

A macOS desktop app for converting Nikon Z f RAW film-negative scans into
16-bit RGB TIFFs, ready for a later stitching phase. The SwiftUI app itself
does no image processing; it shells out to a Python command-line program that
does all file discovery, validation, conversion, and manifest work.

This is a local, single-user project. It targets this Apple-silicon Mac only.
There is no App Store distribution, no Developer ID distribution, no
sandboxing, no notarisation, and no Intel support. See
[docs/DECISIONS.md](docs/DECISIONS.md) for the reasoning behind this and
every other locked decision, and
[docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md) for the full plan.

## Licence

This repository is public, but the project's own code is **all rights
reserved** — see [`LICENSE`](LICENSE). Public visibility does not grant reuse
rights: no part of this repository may be used, copied, modified, or
distributed without the copyright holder's prior written permission.
Third-party components bundled with the app (LibRaw, an embedded ICC colour
profile, and the Python dependencies themselves) keep their own licences; see
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

## Layout

- [`mac/`](mac/) — the SwiftUI macOS app. Its Xcode project is generated, not
  committed; see "Building the app" below.
- [`cli/`](cli/) — the Python command-line program, packaged as `scanny-boy`.
  It contains all file discovery, validation, sorting, grouping, conversion,
  manifest, and progress logic.
- [`shared/contract/`](shared/contract/) — the interface between the two: the
  CLI's argument and JSON-event shape that both sides agree on
  (`CONTRACT.md`, `schema.json`, `manifest.schema.json`).
- [`scripts/`](scripts/) — repo-wide dev scripts (`bootstrap.sh`,
  `build-cli.sh`).
- [`tests/fixtures/`](tests/fixtures/) — shared test fixtures. The real
  sample NEFs used across both the Python and Swift test suites go here; see
  "Sample RAW files" below.

## What you need before you start

- An Apple-silicon Mac running macOS 14 or later.
- Xcode 16.2, with the command-line tools selected.
- [`uv`](https://docs.astral.sh/uv/) for Python 3.13.
- [XcodeGen](https://github.com/yonaskolb/XcodeGen) 2.46 or newer
  (`brew install xcodegen`).
- A Nikon Z f, if you intend to produce your own scans — see "Sample RAW
  files" below for what the camera must be set to.

## Building the app from a clean clone

The app has two halves, and both must be built once before Xcode has
anything to open.

1. **Set up the Python environment:**

   ```bash
   ./scripts/bootstrap.sh
   ```

   This runs `uv sync` inside `cli/`, creating `cli/.venv`.

2. **Freeze the CLI and stage it for the app:**

   ```bash
   ./scripts/build-cli.sh
   ```

   This uses PyInstaller to build `cli/dist/ScannyBoyCLI.app` — a
   self-contained copy of the CLI with no Python dependency at run time — and
   copies it to `mac/ScannyBoy/Helpers/ScannyBoyCLI.app`. This staged copy is
   build output, not source, and is not committed; run this script again
   whenever the Python program changes.

3. **Generate the Xcode project:**

   ```bash
   cd mac && xcodegen generate
   ```

   `mac/project.yml` is the source of truth for the project; the generated
   `mac/ScannyBoy.xcodeproj` is not committed and must be regenerated after
   pulling changes that touch it. Generation fails if step 2 hasn't run yet,
   because `project.yml` names the staged helper app in a copy-files build
   phase.

4. **Open and run it:**

   ```bash
   open mac/ScannyBoy.xcodeproj
   ```

   Or build and test from the command line:

   ```bash
   cd mac && xcodebuild test -scheme ScannyBoy -destination 'platform=macOS'
   ```

The finished app copies `ScannyBoyCLI.app` into
`ScannyBoy.app/Contents/Helpers/` at build time (a permitted nested-code
location — never `Contents/Resources`) and signs it before the outer app, so
the shipped app has no Python dependency of its own.

Ad-hoc signing ("Sign to Run Locally") is enough for this local-only release.
There is no notarisation or Developer ID step.

## Sample RAW files

Some tests exercise real Nikon Z f `.NEF` files rather than synthetic
fixtures, because rawpy's decoding path and the camera's real white-balance
multipliers can't be faithfully faked. Six real NEFs — two negatives of three
frames each — belong at `tests/fixtures/nef/`.

These files are **not** in Git: they're about 190 MB and this repository is
public. `.gitignore` excludes `tests/fixtures/nef/` and its local inventory;
keep those rules. Without the sample files, the tests that need them skip
and print what they didn't test — the rest of the suite still runs and still
proves something.

If you're capturing your own scans to test with, the camera must be set to
**Lossless compressed** RAW. The Z f's other RAW option, High Efficiency (or
High Efficiency\*), cannot be decoded: it uses patented TicoRAW compression
that LibRaw does not support, and there is no workaround. Fixed manual
exposure, fixed manual white balance, one lens and focal length, and one
camera orientation are also required across a run — the CLI validates all of
this and stops with a clear error if it varies.

## Running the tests

```bash
cd cli && uv run ruff check . && uv run pytest
```

```bash
cd mac && xcodebuild test -scheme ScannyBoy -destination 'platform=macOS'
```

Both are also run in CI on every pull request (`.github/workflows/ci.yml`).

## Contributing

This is a one-person project; see [`CONTRIBUTING.md`](CONTRIBUTING.md) for
how it's worked on day to day. Being public doesn't make it open source —
see "Licence" above.
