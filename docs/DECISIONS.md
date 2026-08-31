# Decisions

This is a readable summary of the locked decisions in
[`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) section 3 (Phase 1: RAW
conversion) and [`PHASE2_IMPLEMENTATION_PLAN.md`](PHASE2_IMPLEMENTATION_PLAN.md)
section 3 (Phase 2: registration and stitching). **The relevant plan is
authoritative.** If this file and a plan ever disagree, the plan wins — that
mismatch is a bug in this file, not a licence to follow whichever one is
convenient. Changing any decision below means updating the plan first, and
only after asking the user (each plan's own section 3 rule); this file just
makes those decisions easier to find without reading the whole plan.

# Phase 1 decisions

## Product and repository

- Python owns all logic: file discovery, validation, sorting, grouping,
  conversion, manifest, and progress reporting. Swift is the interface only,
  and starts the Python program as a subprocess — it never re-sorts or
  re-validates on its own.
- The repository is public, but the project's own code is all rights
  reserved (see [`LICENSE`](../LICENSE)). No open-source licence, no SPDX
  identifier, no open-source badge. Public visibility is for reference, not
  reuse.
- Bundled third-party assets (LibRaw, the embedded ICC profile) keep their
  own licences, recorded in [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md).
- `mac/project.yml` is the source of truth for the Xcode project; the
  generated `.xcodeproj` is never committed.
- `main` requires a pull request and passing status checks, and a branch
  must be up to date with `main` before merging. No required approval count
  — this is a one-person project. Force-push and branch deletion are
  blocked.
- Each implementation chunk is one branch and one pull request, merged in
  order.

## Input rules

- Accept `.nef` files case-insensitively from one folder, no recursion.
  Reject duplicates and anything outside the chosen folder.
- The selection must be one uninterrupted range of the catalogue in
  canonical order.
- Shots per negative: 1–12, default 3. The selected count must divide evenly
  by it.
- The camera workflow requires lossless-compressed NEF (never High
  Efficiency or High Efficiency\*), fixed manual exposure, fixed manual white
  balance, one lens and focal length, and one camera orientation across the
  whole selection.
- White balance is validated from `raw.camera_whitebalance` itself —
  normalised, compared with a `1e-6` tolerance — not from an EXIF Manual/Auto
  flag.

## Sorting

- Sort by NEF `DateTimeOriginal` (with `SubSecTimeOriginal` when present);
  break exact ties with natural filename order.
- If any file in the whole catalogue lacks a usable timestamp, the entire
  catalogue falls back to natural filename order and a warning is emitted —
  even if the affected file is outside the selection.
- Never mix timestamp and filename comparisons within one sort.
- No warning for uneven time gaps between frames; there's no measured
  threshold to base one on.

## Pixel output

- One TIFF per source frame, three-channel unsigned 16-bit RGB, named from
  the source (`DSC_0042.NEF` → `DSC_0042.tif`).
- Decode with the source orientation applied so pixels are upright, then
  write TIFF `Orientation` as `1` always — never the source value.
- Encode in ROMM RGB (ProPhoto RGB), standard transfer curve. Every TIFF
  embeds a vetted, checksum-verified ICC profile; an untagged ROMM file is
  never written.
- Lossless Deflate compression with horizontal prediction, one compression
  worker per outer RAW worker.
- Fixed exposure is preserved by disabling both auto-brightness and
  content-dependent maximum adjustment (`no_auto_bright`,
  `adjust_maximum_thr=0.0`).
- The exact `RAW_PARAMS` dict and the four `tifffile` writing rules
  (`metadata=None`, `description=`/`software=` keywords, `iccprofile=`
  keyword, compression code `32946`) are in plan section 3.4 and must not
  drift — each was independently verified to matter.

## Metadata

- The user supplies a film date, not a time. Synthetic ordering times start
  at noon on that date and add each frame's elapsed scan time (or one second
  per frame, if sorting fell back to filenames), strictly increasing.
  Leaving the film date fails with `CAPTURE_SPAN_TOO_LONG`.
- IFD0 and EXIF tags are curated, not copied wholesale — see plan section 3.5
  for the full table. Required tags (exposure, aperture, ISO, focal length)
  stop conversion if missing; optional tags warn and are omitted. Nikon
  MakerNotes, serial numbers, and thumbnails are never copied.
- The nested EXIF directory is written with `tifftools` in a second pass,
  addressed entirely by numeric tag code (its name constants don't match
  plan section 3.5's names for two tags). The base file is removed only
  after the final file is verified.

## Output folder, overwriting, and grouping

- One output folder holds one run. It must differ from the input folder
  (`OUTPUT_SAME_AS_INPUT`).
- An empty folder is valid. A nonempty folder needs a valid Scanny Boy
  manifest to be accepted; dot-files (`.DS_Store`, AppleDouble files, etc.)
  are always ignored when judging this.
- A rerun must match the previous run's sources, hashes, order, grouping,
  film date, processing settings, and ICC hash, or it's rejected as
  `MANIFEST_MISMATCH`. The CLI rejects conflicts by default; `--overwrite`
  is explicit and the app only passes it after the user confirms.
- Each negative is staged as a group and published atomically: if any frame
  in a group fails, the whole group's staging directory is deleted and the
  next group continues. Completed groups survive cancellation; the group in
  progress does not.

## Manifest

- `scanny-boy-manifest.json` is written to a temp file, fsynced, then
  renamed into place, so readers never see a half-written manifest.
  `shared/contract/manifest.schema.json` is the authoritative format.
- The manifest records enough to make Phase 2 safe to build on: run status,
  every source's path/size/mtime/hash, canonical order, groups, expected and
  completed outputs with their hashes, and processing settings.

## Concurrency and cancellation

- `ThreadPoolExecutor` for parallel RAW work (rawpy's LibRaw build releases
  the GIL). Default workers:
  `min(shots_per_negative, os.process_cpu_count() or 1, 4)`. `--jobs 1` uses
  a fully serial path.
- A 640 MiB per-worker memory budget (measured in Chunk 6, see plan section
  3.8's table) silently reduces the *default* worker count but rejects an
  *explicit* `--jobs` with `INSUFFICIENT_MEMORY`.
- Cancellation is cooperative via SIGTERM: stop submitting work, let running
  workers finish their current step, wait for them, then clean up and exit
  143. A forced kill after a grace period can leave a `running` manifest and
  an orphaned staging directory; the next run detects and cleans that up.

## Disk checks

- Required free space is computed conservatively from pixel dimensions,
  compression-free size assumptions, the largest group size, and estimated
  manifest size, then padded 20%. The exact formula is in plan section 3.9.

## Scope this project does not cover

- **Distribution:** no App Store, no Developer ID signing, no notarisation,
  no Intel build. Ad-hoc signing is enough for this local, single-user
  release.
- **Stitching:** Phase 1 produces one upright TIFF per frame and a manifest;
  it does not register, stitch, crop, or invert negatives. That's Phase 2,
  which Phase 1's manifest is deliberately built to support (plan section
  10) without committing to a registration model yet.

---

# Phase 2 decisions

This mirrors [`PHASE2_IMPLEMENTATION_PLAN.md`](PHASE2_IMPLEMENTATION_PLAN.md)
section 3 the same way the section above mirrors Phase 1's — readable, not
authoritative. **The Phase 2 plan wins on any disagreement.** Phase 1's
decisions above are still in force; Phase 2 amends exactly two of them, both
called out below.

## Amendments to the Phase 1 plan

Two Phase 1 decisions are explicitly, user-approvedly changed by Phase 2 —
everything else above stands unmodified:

- **The colour decode curve (amends Phase 1 section 3.4).** rawpy's
  `gamma=(1.8, 16)` does not decode to true ROMM/ProPhoto linear light, as
  Phase 1 assumed — it decodes to LibRaw's own generalised curve, off true
  linear by 3.1% on average. Phase 2 measured LibRaw's actual curve (plan
  section 2.3.1) and uses it — as a 65,536-entry `float32` lookup table — for
  every linear-light decode a stitch performs. Phase 1's own pixel output is
  **not** touched: `RAW_PARAMS` is unchanged, `convert` keeps its exact
  meaning, and the resulting mismatch between Phase 1's embedded ICC profile
  (true ROMM) and Phase 1's actual pixel curve (LibRaw's) is a known,
  recorded Phase 1 imperfection — see `punchlist.md` — that Phase 2 reads
  around rather than fixes.
- **Manifest completeness for stitching (amends Phase 1 section 3.7).** Phase
  1 says a later phase must reject a manifest that is not `complete`. Taken
  literally, one failed negative in the conversion stage would throw away
  every negative that succeeded. `stitch` therefore accepts a `complete`
  manifest by default, and a `partial` one under `--allow-partial`,
  stitching only the groups the manifest marks `completed`. A `running` or
  `cancelled` manifest is still rejected outright; every other Phase 1
  manifest guarantee — missing output, wrong size, wrong SHA-256 — is
  enforced exactly as written.

Phase 1 section 3.6's output-folder rules (empty is valid, nonempty needs a
valid manifest, a rerun must match or is rejected, conflicts need explicit
confirmation) are not amended — they are **generalised** to apply to the new
roll manifest as well as the conversion manifest, by parameterising
`output_folder.py` over which manifest it reads rather than duplicating it.

## Registration model

- A negative's frames are a **one-dimensional strip**, but capture order is
  never assumed to be spatial order. Every pair of a negative's frames is
  matched; a global layout is solved from whichever pairs actually overlap.
  Neighbour-chaining and order-detection are both explicitly rejected —
  the global solve makes order irrelevant for free.
- The geometric model is **rigid: rotation plus translation, scale fixed at
  exactly 1**. `estimateAffinePartial2D` may find RANSAC inliers, but the
  transform actually used is always re-fitted rigidly from those inliers
  (closed-form Umeyama, scale forced to 1) — never an affine or a homography.
- Rotation between frames may run to several degrees; resampling is not
  optional. Overlap is guaranteed at least 20% on every overlapping edge by
  the capture workflow, treated as a validation expectation rather than
  something the solver assumes.

## Colour, resampling, and blending

- All geometric and photometric work happens in **linear light** — decode to
  linear `float32` before warping or blending, encode back to 16-bit once at
  the end.
- Warp with `INTER_LANCZOS4` on `float32`, clamp to `>= 0` immediately after.
  Each frame warps into its own bounding box, not the full canvas. The
  validity mask warps with `INTER_NEAREST` and is eroded by 5 pixels (Lanczos4's
  support radius, plus one pixel of insurance).
- **Blending is a linear feather in linear light**: per-frame weight is a
  distance transform of the eroded mask, and the output is the weighted
  average wherever any frame contributes weight. This is deliberate and
  provisional — see the README's "How frames are registered and blended" for
  the reasoning and the alternatives (a hard seam, a multi-band Laplacian
  blend) that were set aside for now, worth revisiting with real rolls in
  hand.
- Pixels covered by no frame are `FILL_COLOR`, one named constant, initially
  black — recorded in the roll manifest so a file can be interpreted without
  knowing which build wrote it. `punchlist.md` already contemplates a
  contrasting fill colour for Phase 3.

## Quality gates

Every stitched negative is proved correct, not merely finished: per-pair
inlier count, inlier ratio, RMS reprojection residual, and scale drift; per
pair and per negative, overlap MAD — the honest gate, since it is the only
metric that measures whether pixels actually line up rather than whether the
solver was pleased with itself. A disconnected pair graph fails a negative
outright as `STITCH_UNDERCONSTRAINED`. Every threshold was measured from real
scans at user gate C and lives in exactly one place — plan section 3.12 —
that production code reads from and nowhere else.

`rebate_deviation_px` (checking that the film rebate's edges stay collinear
across a negative) is specified in the contract and recorded, but **never
gated in Phase 2 and not implemented at all**: Chunk P2-1 found the rebate is
not cleanly detectable with a generic straight-edge finder, so the field is
always written `null`. A purpose-built detector is a Phase 3 question,
recorded on `punchlist.md`.

## Failure, cancellation, and cleanup

- A negative that cannot be stitched fails alone: the run continues with the
  next negative and ends `partial`, mirroring Phase 1's group-failure rule.
- A cancelled negative is abandoned, not failed — no `negative_failed`
  event, exactly as Phase 1 treats a cancelled group.
- The work directory is removed only on complete success. It is kept, with
  its path reported through an `INTERMEDIATES_KEPT` warning, when any
  negative failed, the run was cancelled, or `--keep-intermediates` was
  given — and a directory the user named with `--work` is never deleted by
  cleanup, whatever happens.

## Command surface

- `stitch --work DIR --out DIR` is the re-stitch path: it reads the work
  directory's Phase 1 manifest, verifies every intermediate's size and
  SHA-256, and stitches — without paying for RAW decoding again.
- `run --input DIR --files ... --out DIR --film-date ...` is the app's normal
  path: one process, one event stream, one cancellation, from a selection of
  NEFs to finished stitched negatives. It calls Phase 1's conversion
  in-process, then stitches into `--out`, and cleans up. It never spawns a
  subprocess of itself.
- `--work` without a value is a fresh temporary directory, discarded per the
  cleanup rules above; given a value, that directory is used and never
  deleted by cleanup. `--work` and `--out` must differ
  (`WORK_SAME_AS_OUTPUT`).
- `--jobs` bounds RAW conversion workers as in Phase 1; in the stitch stage
  it bounds feature detection only — compositing is always one negative at a
  time, single-threaded through the accumulator.

## Output folder and the roll manifest

- One output folder holds one stitched roll. Each negative's TIFF is named
  after the first frame of its group, by canonical order.
- The output folder's record is **`scanny-boy-roll.json`**
  (`shared/contract/roll-manifest.schema.json`) — a new file. Phase 1's
  `scanny-boy-manifest.json` is not renamed; it simply now lives in the work
  directory rather than the output folder.
- The roll manifest is self-describing without the work directory: sources
  and hashes, film date, conversion and stitch parameters, every threshold
  in force, and per negative its members, solved layout, every quality
  metric, canvas size, valid rectangle, fill colour, and output hash.
- The canvas is the full union bounding box; nothing captured is discarded.
  The valid rectangle is computed and recorded but never applied — it exists
  for Phase 3's crop tool.
- A canvas dimension above 30,000 px warns (`OUTPUT_DIMENSIONS_LARGE`); an
  estimated file above 3.5 GiB fails the negative
  (`STITCH_OUTPUT_TOO_LARGE`) rather than silently switching to BigTIFF.
  Composite peak memory is estimated before any allocation and checked
  against the section 3.8 budget.

## Disk and memory arithmetic

Composite peak memory is checked before allocating anything, using a formula
that accounts for the live source frame, the warped bounding box and its
mask, and the accumulator — then multiplied by a **3.5 safety factor**. That
factor is not padding: measured directly, allocator behaviour (NumPy does
not return freed arenas to the OS) makes resident memory track the *sum* of
successive allocation phases rather than their peak, and real three-frame
stitches measured 2.5–3.4× a naive nominal estimate. `peak_bytes` must not
exceed half of physical RAM, or the run fails with `INSUFFICIENT_MEMORY`
reporting both numbers. Re-measure the factor with
`scripts/measure-registration.py` whenever the composite's allocation
pattern changes — plan section 3.8.1 has the full reasoning and the
measurements it rests on.

For `run`, the work directory and the output folder may be on different
volumes; each is checked separately against its own required-space formula,
never summed and checked once.

## Event protocol and stable codes

`PROTOCOL_VERSION` is **2**. `progress` gained `stage` (`"convert"` or
`"stitch"`); `PipelineStep` gained the stitch steps (`load`, `detect`,
`match`, `solve`, `warp`, `blend`, `write_stitched`); and two new events,
`negative_done` and `negative_failed`, describe the stitch stage's per-
negative results the way `group_done`/`group_failed` describe the
conversion stage's. New stable codes: `WORK_SAME_AS_OUTPUT`,
`WORK_MANIFEST_UNUSABLE`, `INTERMEDIATE_MISSING`, `INTERMEDIATE_CHANGED`,
`STITCH_INSUFFICIENT_MATCHES`, `STITCH_UNDERCONSTRAINED`,
`STITCH_RESIDUAL_TOO_HIGH`, `STITCH_OUTPUT_TOO_LARGE`, `STITCH_FAILED`, and
the warnings `STITCH_SCALE_DRIFT`, `STITCH_LAYOUT_UNEXPECTED`,
`STITCH_REBATE_CHECK_FAILED`, `OUTPUT_DIMENSIONS_LARGE`,
`INTERMEDIATES_KEPT`. Full table: plan section 3.10.

## Stitched TIFF format

Identical to Phase 1's TIFF rules (three-channel `uint16`, ROMM with the
embedded checksum-verified ICC profile, `Orientation` always 1, Deflate with
horizontal prediction, the two-pass `tifftools` EXIF write) with three
differences: dimensions are the canvas, not one frame; `ImageDescription`
names the negative's sources and says it is stitched
(`"_DSC4638.NEF+2: stitched scan"`); and curated EXIF comes from the
negative's first frame in canonical order, since Phase 1 already proves every
frame of a negative shares identical exposure, aperture, ISO, focal length,
lens, and white balance.

## The app (Swift)

- Swift never sorts files, groups negatives, or judges an output folder
  itself — every one of those decisions comes back from a `probe` call, the
  same rule Phase 1 locked for `convert`.
- `run` is what the app's Run button drives — convert and stitch in one
  invocation. Re-stitch drives `stitch` directly against a kept work
  directory, reusing the identical `RunModel`, progress view, and results
  view.
- **Known, deliberate limitation:** `probe --out` was never extended to
  understand `scanny-boy-roll.json`, so it cannot compute an itemized list of
  what a rerun or re-stitch into an already-published output folder would
  replace. The app works around the resulting false `OUTPUT_NOT_EMPTY` for a
  folder that legitimately holds a prior roll, and asks for one general,
  explicit acknowledgement before passing `--overwrite` rather than an
  itemized one. Real conflict enforcement, as everywhere else in this app,
  happens for real, server-side, in `run_stitch`. Extending `probe --out` to
  the roll manifest — which would let the app show an itemized preview here,
  the same way it already does for a plain `convert`/`run` — is recorded on
  `punchlist.md`.

## Scope Phase 2 does not cover

- **The rebate-deviation check** is specified in the contract but not
  implemented; see "Quality gates" above.
- **Itemized overwrite/rerun previews for the roll manifest** are not
  implemented in the app; see "The app (Swift)" above.
- Everything Phase 1's "Scope this project does not cover" already says
  still applies unchanged — no App Store, no Developer ID signing, no
  notarisation, no Intel build.

---

# Phase 3 decisions

This mirrors [`PHASE3_IMPLEMENTATION_PLAN.md`](PHASE3_IMPLEMENTATION_PLAN.md)
section 3 the same way the sections above mirror Phases 1 and 2 — readable,
not authoritative. **The Phase 3 plan wins on any disagreement.** Phases 1
and 2's decisions above stand except where named below.

## What changed, and why it's a break

The Phase 2 roll manifest was single-run by construction: one `run_id`, one
`film_date`, one `source_order`, and a rerun rule that rejects anything that
differs from what's recorded. An additive roll must be allowed to change
exactly those things, so Phase 3 is `manifest_format_version: 2`, a
rewritten `roll_manifest.py`, and **protocol version 3** — not a patch to
the old format. There is no migration: a Phase 2 folder is not importable,
and the app refuses a protocol-2 event stream.

Three consequences: `--film-date` is removed from the CLI entirely (dates
move to the metadata stage, below); a freshly stitched TIFF now carries its
first frame's **real** capture timestamp, and only gets a synthetic,
ordered one once metadata is applied; and Phase 1's long-standing
profile/curve mismatch is fixed by replacing the embedded ICC profile
(`ScannyBoy-ROMM-LibRaw-v4.icc`) — every TIFF's bytes and hash change, no
pixel value does (`punchlist.md`).

## The library and rolls

- One library folder (`~/Pictures/Scanny Boy` by default, relocatable
  through Settings) holds every roll as a direct child. The filesystem is
  the only source of truth — no index, no registry — and `roll list`
  scans it one level deep for `scanny-boy-roll.json`, server-side; the app
  never enumerates the library or parses a roll manifest itself.
- `roll_id` is a UUID, generated once, never in a path. `roll_name` is free
  text; the folder name is a slug of it (NFC-normalised,
  `[A-Za-z0-9._-]` plus single-dash whitespace runs, 60 characters,
  case-insensitive collision suffixes). Renaming moves the folder to a new
  slug, then writes `roll_name` — refused while a run is active, enforced
  client-side since the CLI is stateless between invocations. Deleting
  moves the folder to the Trash via `NSWorkspace.recycle`, no CLI
  involvement at all.

## Roll invariants and additive runs

- `shots_per_negative`, `processing_params`, the ICC profile hash, and
  `stitch_params` are roll-invariant across every run in a roll; anything
  else (input folder, source list, order, grouping) is expected to differ
  and is never compared. `shots_per_negative` locks once any run reaches
  `complete`/`partial` with a completed negative.
- `negative_id` is `<run.short_id>-negative-NN`; `short_id` starts at the
  first six hex characters of the run's UUID and lengthens on collision.
  Output names keep Phase 2's first-member-stem rule, with a `-2`, `-3`, …
  suffix on collision across runs.
- **Replacement is additive, never in-place.** A run may include sources
  already in the roll; the new negative publishes under its own identity,
  and once it completes, every existing negative whose members are a
  subset of the new one's is superseded: `superseded_by` set,
  `sequence` cleared, its TIFF deleted (manifest write first, so a crash
  leaves an orphan file rather than a dangling record). Superseded
  negatives are never removed from the manifest and their output names
  stay claimed.

## Command surface (protocol version 3)

- `roll init/list/info/rename` manage the library; `roll rename` is a
  P3-10 addition to the original plan (§5.5) — the plan named only
  `init`/`list`/`info` until Chunk P3-10 found renaming had no CLI path at
  all, despite `roll_folder.rename_roll` already existing.
- `probe` gains `--roll`, validating the selection against the roll's
  invariants and reporting `roll_overlap` — one entry per prospective
  negative that shares sources with a negative already in the roll —
  without rejecting the overlap outright; the app's overlap sheet decides.
- `run` and `stitch` take `--roll` in place of `--out`; `--film-date` and
  `run --overwrite` are both gone. Replacing a negative is expressed by
  *not* skipping its sources (`run --skip-sources`), which the app derives
  from the overlap sheet, never by an overwrite flag.
- `apply-metadata --roll DIR` is new: section "Metadata and Apply" below.

## Sequence and metadata

- A roll's negatives are ordered by real capture time of each negative's
  first member, across every run, ascending; a superseded negative is
  excluded from the order entirely (`sequence: null`), so a replacement
  takes its predecessor's position rather than shifting later negatives.
- The applied timestamp is **rank-based**: `12:00:00 + (rank − 1)` seconds
  on the roll's capture date, or on a negative's own date override when it
  has one, ranked within that date's negatives. One computation,
  `roll_sequence.py`, and nothing else recomputes it.
- **Intent lives in the manifest; the TIFF is the artefact.** A negative is
  dirty when `intended_datetime_original` differs from
  `applied_datetime_original`. `apply-metadata` processes every dirty,
  completed, non-superseded negative: verifies the published TIFF against
  the manifest's recorded size/hash (skips with `OUTPUT_MODIFIED_
  EXTERNALLY` rather than rewriting a file the roll no longer recognises),
  rewrites the nested EXIF `DateTimeOriginal`/`SubSecTimeOriginal` with
  `tifftools`, re-hashes, and updates the manifest. No pixel data is ever
  touched. A re-stitch of a negative that already had metadata applied
  re-applies it automatically, without asking.
- **Not yet wired to the app (§5.6):** no CLI command writes
  `metadata.roll_capture_date` or a negative's `capture_time.date_override`
  — not even a library-level function exists to wrap, unlike the rename
  gap above. Chunk P3-12 shows both, and an unlocked roll's
  `shots_per_negative`, read-only in the Edit tab rather than inventing a
  write path; see `punchlist.md`.

## The app (Swift)

- **One window**, `NavigationSplitView`: a sidebar of rolls (name, negative
  count, unreadable rolls shown disabled with their reason — all from one
  `roll list` call) and a workspace with **Add Scans** and **Edit** tabs
  for whichever roll is selected. One active run app-wide disables the
  sidebar, the tab picker, and both stages' controls.
- **Add Scans** lost the output-folder and film-date fields Phase 2 had;
  shots per negative is the roll's own, shown read-only. The
  overwrite-confirmation dialog is replaced by the overlap sheet — one row
  per `roll_overlap` entry, Skip (default) or Replace.
- **Edit** is new: negatives in sequence order with thumbnails (read via a
  QuickLook-skipping `ThumbnailLoader` path tuned for large published
  TIFFs, not RAW previews), source frames, quality metrics, the dirty
  count, and Apply — driven through the same shared `RunModel`/
  `CLISession` as Run and re-stitch, not a parallel mechanism.
- Swift reads a roll only through `roll list` and `roll info`, never by
  parsing `scanny-boy-roll.json` or walking the library itself.

## Scope Phase 3 does not cover

- **Setting the roll capture date or a per-negative date override, and
  editing an unlocked roll's `shots_per_negative`, from the app** — see
  "Sequence and metadata" above and `punchlist.md`.
- Crop from manifest data, white balance/base neutralisation, extended
  metadata (location, camera, lens, film stock), the cyan fill colour,
  manual negative reordering, and deleting a negative outright are all
  deferred with an attachment point recorded on `punchlist.md`; none is
  scheduled.
- Negative inversion is Phase 4.
- The rebate-deviation detector (Phase 2's punchlist item) is untouched by
  Phase 3.
- Everything Phase 1 and 2's own "Scope ... does not cover" sections say
  still applies unchanged.
