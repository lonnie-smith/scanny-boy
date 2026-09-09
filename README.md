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
[docs/DECISIONS.md](docs/DECISIONS.md) for the reasoning behind this and
every other locked decision, and
[docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md) (Phase 1: RAW
conversion), [docs/PHASE2_IMPLEMENTATION_PLAN.md](docs/PHASE2_IMPLEMENTATION_PLAN.md)
(Phase 2: registration and stitching), and
[docs/PHASE3_IMPLEMENTATION_PLAN.md](docs/PHASE3_IMPLEMENTATION_PLAN.md)
(Phase 3: rolls, staged workflow, and metadata editing) for the full plans.

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
has a sidebar of rolls and, once one is selected, a workspace with two
tabs: **Add Scans** and **Edit**.

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
   selection that overlaps sources already in the roll shows a sheet first,
   one row per overlapping prospective negative, defaulting to **Skip**;
   choosing **Replace** adopts the existing negative in place — same
   negative and same filename, its stitched TIFF replaced with the new
   result.
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
touched. **Metadata** carries the roll's info and the dirty count with its
**Apply** button, which writes intended capture times into the published
TIFFs' EXIF tags (no pixel data is touched). The Edit tab's tone and colour
controls likewise record nondestructive ops, and the spot tool
("Find spots") proposes dust/hair/scratch candidates as markers over the
preview — reject the bad ones by clicking them, then switch repair on and
only what survived review is inpainted (the published TIFF stays
untouched; the repair lives in the ops log and reaches the export).
**Export** writes TIFFs with
the edits applied into a folder of your choosing. The roll capture date and
each negative's date override are shown but are not yet editable from the
app — see `docs/punchlist.md`.

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
still rigid — rotation plus translation, scale forced to exactly 1, and
still what the acceptance gates measure — but the *global* layout solves one
isotropic scale per frame on top of that, because film does not sit at a
constant height above the stage from frame to frame. It is a similarity,
never an affine and never a homography. Before that layout solves, the
stitch stage also fits one *rectifying homography* per negative — two
parameters, shared by every pair, estimated from the pairs' own inliers and
accepted only when it measurably helps — that undoes the slight tilt
between the camera and the film plane. The pairwise fit and the layout are
still exactly what they were; the rectification is a measured reparameterisation
of image coordinates, applied in the same slot as the lens-distortion
correction, under which the inter-frame maps really are similarities. See
[docs/RECTIFICATION_PLAN.md](docs/RECTIFICATION_PLAN.md) for the
measurement behind it. Several metrics per pair and per
negative (inlier count and ratio, reprojection residual, and — the one that
actually measures whether pixels line up — overlap MAD) are checked against
thresholds measured from real scans before a negative is allowed to publish;
see [docs/PHASE2_IMPLEMENTATION_PLAN.md](docs/PHASE2_IMPLEMENTATION_PLAN.md)
section 3.4.

**Blending.** Overlapping regions are combined with a feather in linear
light: along each axis a frame participates on (the strip axis for a strip,
both axes of the grid for a grid), a frame's contribution ramps from the
nearer end of its own extent on that axis, normalised to `[0, 1]`; the
per-axis ramps multiply into a single separable product, floored so a
covered pixel always contributes. A pixel's crossfade profile across a
vertical seam is identical at the top of the canvas, the middle, and the
bottom — and likewise for a horizontal seam — because each axis's ramp
depends only on position along that axis. An earlier version weighted a
pixel by its distance to the nearest edge of the frame's own mask in every
direction, which is isotropic and therefore identical near a strip's long
borders and down its middle — but near those borders the nearest edge is
the border itself, not the seam, so both frames' weights collapsed toward
50/50 there regardless of where the seam actually was, and residual
misregistration smeared into a curved band that grew toward the edges
instead of showing up as a clean step at the seam. The separable ramp
removes that collapse.

The normalised ramp product is then raised to `FEATHER_EXPONENT` (an
integer in `[1, 8]`, starting value 4) before the floor is applied — see
[docs/NARROW_FEATHER.md](docs/NARROW_FEATHER.md). This narrows the
crossfade to a band around the overlap midline instead of spanning the
whole overlap, without moving the seam (the crossover stays at exactly
`0.5` for every exponent) and without breaking separability (`(r_x *
r_y)^p = r_x^p * r_y^p`). Wide feathers still average two frames' detail
across nearly half the picture even when registration is good to a couple
of pixels, which reads as scattered soft doubling rather than the bounded
step the strip-axis ramp alone was meant to produce; narrowing the band
keeps most of the canvas free of any blend at all, at the cost of making
the residual step more locally visible in the band that remains.

This is safe specifically *because* exposure and white balance are locked
across a roll — there is no exposure mismatch a blend needs to hide, only
misregistration, and a feather tolerates a little of that gracefully instead
of showing it as a hard line. A band around the overlap midline was
considered and set aside once already — it needs the pair's overlap
geometry at blend time, which the compositing pass does not carry — and
this exponent is that idea arriving by another route: the midline falls out
of the ramp arithmetic itself, needing no overlap geometry at all. Two
alternatives remain set aside:

- **A hard seam at the overlap midline.** Preserves grain exactly, since no
  pixel is ever a blend of two frames — but any misregistration shows up as a
  visible line at the seam, with nothing to soften it. Named as the next step
  if a measured step at the seam turns out small enough to cut through rather
  than fade.
- **A multi-band Laplacian blend.** Hides misalignment better than a linear
  feather does, at the cost of softening fine grain in the blended region and
  a meaningfully heavier compositing stage.

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


