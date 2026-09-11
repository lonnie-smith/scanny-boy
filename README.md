# Scanny Boy

A macOS desktop app for turning Nikon Z f RAW film-negative scans into
stitched, upright 16-bit RGB TIFFs — one file per negative, registered and
composited from however many overlapping frames it took to scan the whole
strip. The SwiftUI app itself does no image processing; it shells out to a
Python command-line program that does all file discovery, validation,
conversion, registration, compositing, and manifest work.

This is a local, single-user project. It targets this Apple-silicon Mac only.
There is no App Store distribution, no Developer ID distribution, no
sandboxing, no notarisation, and no Intel support. See
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how the program is built and
why; the design reasoning for anything non-obvious lives as a comment next to
the code it explains.

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
  It contains all file discovery, validation, sorting, grouping, RAW
  conversion, registration, compositing, and manifest logic.
- [`shared/contract/`](shared/contract/) — the interface between the two: the
  CLI's argument and JSON-event shape that both sides agree on
  (`CONTRACT.md`, `schema.json`), plus the two manifest formats it writes
  (`manifest.schema.json` for a conversion's work directory,
  `roll-manifest.schema.json` for a stitched roll's output folder).
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
   ./scripts/test-mac.sh
   ```

The finished app copies `ScannyBoyCLI.app` into
`ScannyBoy.app/Contents/Helpers/` at build time (a permitted nested-code
location — never `Contents/Resources`) and signs it before the outer app, so
the shipped app has no Python dependency of its own.

Ad-hoc signing ("Sign to Run Locally") is enough for this local-only release.
There is no notarisation or Developer ID step.

## Using the app

Everything in the app happens inside a **roll** — a durable, named folder
you return to and add to over time, not a one-shot conversion. The window
has a sidebar of rolls and, once one is selected, a workspace with four
tabs: **Add Scans**, **Edit**, **Metadata**, and **Export**.

**The library.** The sidebar lists every roll under the library base
(`~/Pictures/Scanny Boy` by default, relocatable from **Settings**), each
showing its name and how many negatives it holds. **+** creates a roll —
asking only for a name — and the sidebar's context menu renames or deletes
one. Renaming moves the roll's folder; deleting moves it to the Trash.

**Add Scans** — adding negatives to the selected roll:

1. **Choose an input folder** of `.NEF` files. The app lists them in
   canonical order (capture time, falling back to filename) and shows a
   thumbnail for each.
2. **Select one contiguous range.** Scans per negative is each run's own
   choice, set on the Add Scans stage as a grid: **Across** by **Down**
   (Down defaults to 1, a plain strip). The selected count must divide
   evenly by the product, and `min(across, down) <= 2` — every cell of the
   grid must show film rebate, which only holds when every cell touches
   the grid's outer boundary (the rule is stated in CONTRACT.md). The
   grouping preview shows how the selection splits into negatives.
3. **Choose a flat-field profile.** A copy-stand capture is darker at the
   corners than at the centre — lens falloff plus an uneven light source.
   **File > Flat-Field Profiles…** manages profiles, each measured once from
   a reference shot of the bare light source (no negative in the holder);
   the chosen profile's gain map evens that falloff back out of every frame
   of the run. The profile is required before Stitch is enabled, and a roll
   locks to one profile with its first run.
4. **Run.** One process converts every RAW frame, registers each negative's
   frames against each other, solves a shared layout, and composites one
   stitched TIFF per negative into the roll — reporting live progress, and
   each negative's result (published, or why it failed) when it ends. A
   selection that overlaps sources already in the roll is adopted in
   place automatically — same negative and same filename, its stitched
   TIFF replaced with the new result — with no per-overlap Skip/Replace
   choice offered yet.
5. **Re-stitch, if a negative needs tuning.** A run never keeps the work
   directory it creates — it is removed on every outcome — so to re-stitch
   you point the app at one you kept yourself (a run started with `--work`,
   or any work directory you have on disk). **File > Re-stitch…** re-runs
   just the stitch stage against it — no RAW decoding paid for twice.

**Edit** — a filmstrip of the roll's negatives in sequence along the bottom
and a large preview of the selected negative above it; **rotate left /
rotate right** buttons (and Option←/Option→ to move the selection) record a
nondestructive rotation per negative — the ops log lives in the library
database, the CLI re-renders the preview, and the published TIFF is never
touched. Its tone and colour controls likewise record nondestructive ops,
and the spot tool ("Find spots") proposes dust/hair/scratch candidates as
markers over the preview — reject the bad ones by clicking them, then
switch repair on and only what survived review is inpainted (the published
TIFF stays untouched; the repair lives in the ops log and reaches the
export). **Metadata** edits the roll's and each negative's film/camera/lens/
location/caption fields and capture-date override, and carries the dirty
count with its **Apply** button, which writes intended capture times into
the published TIFFs' EXIF tags (no pixel data is touched). **Export**
renders each negative as a positive with every edit applied and writes it
as a 16-bit lossless JPEG XL into a folder of your choosing.

Every roll is recorded in one library SQLite database
(`~/Library/Application Support/ScannyBoy/library.db`) — sources, every run,
per-negative layout and quality metrics, thresholds in force, and each
negative's ordered edit ops — while the roll folder holds the stitched
TIFFs themselves. The CLI renders each negative's small preview, so the app
only ever displays what Python produced.

## How frames are registered and blended

Two frames are far enough apart that neither can be assumed to be the "next"
one in capture order — the sequence may run right-to-left, or be shuffled —
so every pair of a negative's frames is matched, and a global layout is
solved from whichever pairs actually overlap. Each frame's pairwise fit is
rigid — rotation plus translation, scale forced to exactly 1 — but the
*global* layout solves one isotropic scale per frame on top of that,
because film does not sit at a constant height above the stage from frame
to frame; it is a similarity, never an affine and never a homography.
Several metrics per pair and per negative (inlier count and ratio,
reprojection residual, and — the one that actually measures whether pixels
line up — overlap MAD) are checked against thresholds measured from real
scans before a negative is allowed to publish. Overlapping regions are then
combined with a feather in linear light, narrowed to a band around the
overlap midline rather than spanning the whole overlap. See
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §8 for the full detail and the
reasoning behind each of these choices.

## Sample RAW files

Some tests exercise real Nikon Z f `.NEF` files rather than synthetic
fixtures, because rawpy's decoding path, the camera's real white-balance
multipliers, and real feature-rich film grain can't be faithfully faked.
Phase 1's original six frames (two negatives of three) and Phase 2's gate-B
scans (five more negatives of three, shot to exercise routine, rotated,
out-of-order, minimum-overlap, and non-overlapping registration) all belong
at `tests/fixtures/nef/`; `tests/fixtures/INVENTORY.md` records whichever of
them are present locally.

These files are **not** in Git: real scans run into the hundreds of
megabytes and this repository is public. `.gitignore` excludes
`tests/fixtures/nef/` and its local inventory; keep those rules. Without the
sample files, the tests that need them skip and print what they didn't test
— the rest of the suite still runs and still proves something.

If you're capturing your own scans to test with, the camera must be set to
**Lossless compressed** RAW. The Z f's other RAW option, High Efficiency (or
High Efficiency\*), cannot be decoded: it uses patented TicoRAW compression
that LibRaw does not support, and there is no workaround. Fixed manual
exposure, fixed manual white balance, one lens and focal length, and one
camera orientation are also required across a run — the CLI validates all of
this and stops with a clear error if it varies. Frames scanned for stitching
also need real overlap between neighbours — at least 20% on every
overlapping edge is the workflow's guarantee, and what the registration
gates are calibrated against.

## Running the tests

```bash
cd cli && uv run ruff check . && uv run pytest
```

```bash
./scripts/test-mac.sh
```

Both are also run in CI on every pull request (`.github/workflows/ci.yml`).

The Python run is parallel by default (`-n 8`, from `cli/pyproject.toml`) and
takes about a minute; add `-n 0` to run it serially while debugging a single
test. `scripts/test-mac.sh` regenerates the Xcode project before building —
`mac/ScannyBoy.xcodeproj` is generated and gitignored, so a newly added Swift
file is absent from it until you do — and runs `xcodebuild` with `-quiet`, so
a passing run prints a handful of lines rather than about 1700.

Both suites keep their expensive cases behind a flag: `uv run pytest --slow`
for the Python tier that decodes real RAW frames, stitches real scans, sweeps
the full tone parameter box, and runs the packaged app;
`SCANNY_BOY_SLOW_TESTS=1 ./scripts/test-mac.sh` for the Swift integration
scenarios. Run them when you touch those paths.


