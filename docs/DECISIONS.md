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
  never written.  (Update: superseded — the decode is now linear sensor
  channels (`gamma=(1, 1)`, `output_color=raw`), the intermediates carry the
  linear `ScannyBoy-Linear-v1.icc`, and the published TIFF is normalized log
  density under `ScannyBoy-Density-v1.icc`. See "Linear decode for NegPy
  compatibility" and "Normalization decisions" below.)
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
  `min(shots_per_negative, os.process_cpu_count() or 1, 4)`, where
  `shots_per_negative` is the batch's own value. `--jobs 1` uses a fully
  serial path. (Update: the **convert** stage's default now keys off the
  run's total frame count — `min(len(files), cpus, 4)` — since `run_convert`
  opens one pool for the whole run, not one per group; the stitch stage's
  detection pool still keys off the recorded `shots_per_negative`. See
  `ARCHITECTURE.md` §11.)
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

**Amendment (protocol version 8): the layout solves a per-frame scale.**
The pairwise fit still produces a rigid transform, and that rigid fit is
still what the acceptance gates measure. The *global layout* now places
each frame with a similarity — rotation, translation, and one isotropic
scale — solved from pairwise similarity scales as a log-space linear
least-squares problem with a geometric-mean-1 anchor, structurally
identical to `solve_gains`. Still three linear solves, still no SciPy in
`layout.py`, still never an affine and never a homography.

Why: film does not sit at a constant height above the stage, so a strip is
not one magnification. With scale locked at 1 that mismatch was absorbed
into rotation and translation, where it surfaced as residual
misregistration at frame borders — the error the isotropic feather was
hiding rather than showing. It could not be modelled honestly before radial
distortion was corrected, because distortion produced a position-dependent
apparent scale that a per-frame constant would have fitted wrongly.

**Amendment (roll manifest format version 7): the stitch stage rectifies
a measured rig tilt.** The pairwise fit is still rigid and the layout is
still a similarity — both unchanged, both still what the acceptance gates
measure. Before the layout solves, the stitch stage fits one rectifying
homography `W = [[1,0,0],[0,1,0],[l1,l2,1]]` per negative, shared by every
pair, from the accepted pairs' own inliers — two parameters,
`scipy.optimize.least_squares` with each pair's similarity re-fit in closed
form inside the residual. If it passes its acceptance gates (support,
plausibility, measured improvement), all downstream geometry works in
`W`-rectified coordinates and the canvas is rectified space. This is not a
homographic placement: no pair and no frame is ever placed by a homography.
`W` is a measured property of the rig applied in the same slot as the
radial undistortion — a re-parameterisation of image coordinates under
which the inter-frame maps really are the similarities the layout already
solves.

Why: the film plane is not fronto-parallel — measured at −0.10° to −0.38°
across the strip on every manually-shot negative examined
(`scripts/measure-tilt.py`), varying between sessions and absent in a
burst — so the true frame-to-frame map is a homography, and a similarity
fitted to it leaves a systematic residual per pair that accumulates along a
strip into visibly curved film edges. The per-pair homography alternative
was measured and rejected: eight free parameters per pair, fitted from a
thin overlap band and extrapolated across the frame, degrade as overlap
narrows, where the two-parameter rig model holds. The residual a single
global tilt does not explain (~0.2 px, per-pair film-height variation) is
recorded in the manifest, not corrected.

## Colour, resampling, and blending

- All geometric and photometric work happens in **linear light** — decode to
  linear `float32` before warping or blending, encode back to 16-bit once at
  the end.
- Warp with `INTER_LANCZOS4` on `float32`, clamp to `>= 0` immediately after.
  Each frame warps into its own bounding box, not the full canvas. The
  validity mask warps with `INTER_NEAREST` and is eroded by 5 pixels (Lanczos4's
  support radius, plus one pixel of insurance).
- **Blending is a linear feather in linear light, ramped along every axis
  the layout's feather participates on**: per-axis weight is the distance
  from the nearer end of the frame's own extent along that axis
  (`Layout.feather_axes()` — the strip's long axis for a strip, both grid
  axes for a grid), normalised to `[0, 1]`; the per-axis ramps multiply
  into one separable product, floored so a covered pixel always
  contributes, then raised to `FEATHER_EXPONENT` (see "The feather's
  exponent narrows the crossfade" below); the output is the weighted
  average wherever any frame contributes weight. A distance transform of
  the eroded mask (isotropic in every direction, unpowered) is kept only as
  the fallback when a layout has no trustworthy feather axes. The isotropic
  version was replaced, not merely revisited: it made a pixel's crossfade
  identical near the strip's long borders and down its middle, but near
  those borders the nearest mask edge is the border itself, not the seam,
  so both frames' weights collapsed toward 50/50 there regardless of the
  true seam position, and residual misregistration smeared into a curved
  band that widened toward the edges. See the README's "How frames are
  registered and blended" for the reasoning and the alternatives (a hard
  seam, a multi-band Laplacian blend) kept as named, deliberately deferred
  next steps.
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
scans and lives in exactly one place — plan section 3.12 — that production
code reads from and nowhere else.

Before the MAD gate runs, per-frame per-channel **photometric gains**
reconcile lamp drift between a negative's frames: the pairwise mean ratios
over each used pair's shared area feed a global least-squares solve in log
space (one row per usable pair, weighted by overlap area, anchored so the
solved gains have geometric mean 1 — no frame's lamp level is privileged,
and the worst-case gain excursion into the encode clamp is minimized), and
the gains are applied to the warped linear buffers before the blend. The
MAD gate is thereby re-pointed at the *post-gain residual*: it checks
registration, not lamp drift. The pre-gain MAD is recorded beside it as the
diagnostic that explains why a gain was applied, and a solved gain far from
unity warns as `STITCH_GAIN_DRIFT`. **Three constants here are not
measured against real scans**: `MIN_GAIN_OVERLAP_PX` (a pair's shared area
below this is dropped from the gain solve; it borrows NegPy's measured
1000px floor) and `GAIN_DRIFT_WARN` are provisional and unmeasured, and
`MAX_OVERLAP_MAD`'s value (0.20) was measured against *uncorrected*
overlaps, so applied to the post-gain residual it is looser than intended.

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
- The work directory a run creates itself is removed on every outcome —
  failure and cancellation no longer keep it, since a rerun regenerates
  it. A directory the user named with `--work` is never deleted by cleanup,
  whatever happens.

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
negative's first frame in canonical order — a deterministic choice, not a
claim that Phase 1 proves the frames' settings match, since exposure is
required per file but its value is not compared across a roll.

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
  never enumerates the library or parses a roll manifest itself. (Update:
  superseded — the durable record now lives in the library SQLite database
  and `roll list` reports registered rolls from it, not from the presence of
  `scanny-boy-roll.json`; a registered roll whose folder vanishes reports
  `unreadable`. See `ARCHITECTURE.md` §9.)
- `roll_id` is a UUID, generated once, never in a path. `roll_name` is free
  text; the folder name is a slug of it (NFC-normalised,
  `[A-Za-z0-9._-]` plus single-dash whitespace runs, 60 characters,
  case-insensitive collision suffixes). Renaming moves the folder to a new
  slug, then writes `roll_name` — refused while a run is active, enforced
  client-side since the CLI is stateless between invocations. Deleting is
  two steps: the folder moves to the Trash via `NSWorkspace.recycle`, then
  `roll delete` removes the database registration, so `roll list` drops the
  roll instead of reporting it as `unreadable`.

## Roll invariants and additive runs

- `processing_params`, the ICC profile hash, and `stitch_params` are
  roll-invariant across every run in a roll; anything else (input folder,
  source list, order, grouping, and each batch's `shots_per_negative`) is
  expected to differ and is never compared. Originally `shots_per_negative`
  was a fourth invariant, set at roll creation and locked once any run
  reached `complete`/`partial` with a completed negative; that constraint
  is retired — the grouping is each stitch batch's own choice, recorded in
  its work manifest, and the roll record no longer stores it.
- `negative_id` is `<run.short_id>-negative-NN`; `short_id` starts at the
  first six hex characters of the run's UUID and lengthens on collision.
  Output names keep Phase 2's first-member-stem rule, with a `-2`, `-3`, …
  suffix on collision across runs.

## Replacement is in-place and invisible

- **A rerun adopts the covered negative in place.** A run's group that
  covers existing negatives (the same subset test the supersession rule
  used) adopts one of them — keeping its `negative_id` and
  `expected_output` — and updates that record in place with the new run's
  data. Any other covered negatives are removed outright: record dropped
  from the manifest, TIFF unlinked best-effort (`ORPHAN_FILE_NOT_REMOVED`
  on failure, never fatal). A group that covers nothing gets a fresh id
  and name as before; a splitting regroup covers only part of an existing
  negative, so it coexists under `-2` suffixes as it always did.
- **There is no tombstone.** `superseded_by`, the
  `negative_superseded` event, and the "Show replaced negatives" toggle
  are all gone. A replaced negative is indistinguishable from any other
  negative: same record, same name, file replaced atomically by the
  staged-then-`os.replace` publish. The names a removed covered negative
  held are freed for future allocation — no never-reissue rule.
- **Adopted ids keep the old run's `short_id`** while `run_id` points at
  the new run. Ids are opaque; the schema's comment says so and does not
  claim otherwise.
- **Adoption happens at publish.** The adopt-or-remove decision is made
  when the run's groups are appended, but the record's old output,
  capture time, and rank data stay in place until the new publish
  replaces them, so a crash before the staged `os.replace` leaves the roll
  describing exactly what was there before; a crash after it leaves the
  same "new file, stale record" story any publish has always had.
- **History, reversed.** The rule this replaces — "replacement is
  additive, never in-place", with `superseded_by` tombstones above — is
  the previous, and now discarded, design. Sequence ranking and
  `apply-metadata` simply treat every negative: a completed negative
  ranks by capture time whether it was adopted or fresh, and every dirty
  completed negative is eligible for Apply.

## Command surface (protocol version 4)

- `roll init/list/info/rename` manage the library; `roll rename` is a
  P3-10 addition to the original plan (§5.5) — the plan named only
  `init`/`list`/`info` until Chunk P3-10 found renaming had no CLI path at
  all, despite `roll_folder.rename_roll` already existing.
- `probe` gains `--roll`, validating the selection against the roll's
  invariants and reporting `roll_overlap` — one entry per prospective
  negative that shares sources with a negative already in the roll —
  without rejecting the overlap outright; the app's overlap sheet decides.
- `run` and `stitch` take `--roll` in place of `--out`; `--film-date` and
  `run --overwrite` are both gone. Replacement is expressed by *not*
  skipping its sources (`run --skip-sources` remains the way to redo a
  scan without touching an existing negative), and the replacement itself
  is the adopt-in-place rule.
- `apply-metadata --roll DIR` is new: section "Metadata and Apply" below.

## Sequence and metadata

- A roll's negatives are ordered by real capture time of each negative's
  first member, across every run, ascending; a negative that never
  published (`pending`/`failed`) is excluded from the order
  (`sequence: null`), so a rerun's adopted negative keeps its predecessor's
  position rather than shifting later negatives.
- The applied timestamp is **rank-based**: `12:00:00 + (rank − 1)` seconds
  on the roll's capture date, or on a negative's own date override when it
  has one, ranked within that date's negatives. One computation,
  `roll_sequence.py`, and nothing else recomputes it.
- **Intent lives in the manifest; the TIFF is the artefact.** A negative is
  dirty when `intended_datetime_original` differs from
  `applied_datetime_original`. `apply-metadata` processes every dirty,
  completed negative: verifies the published TIFF against
  the manifest's recorded size/hash (skips with `OUTPUT_MODIFIED_
  EXTERNALLY` rather than rewriting a file the roll no longer recognises),
  rewrites the nested EXIF `DateTimeOriginal`/`SubSecTimeOriginal` with
  `tifftools`, re-hashes, and updates the manifest. No pixel data is ever
  touched. A re-stitch of a negative that already had metadata applied
  re-applies it automatically, without asking.
- **Not yet wired to the app (§5.6):** no CLI command writes
  `metadata.roll_capture_date` or a negative's `capture_time.date_override`
  — not even a library-level function exists to wrap, unlike the rename
  gap above. Chunk P3-12 shows both read-only in the Edit tab rather than
  inventing a write path; see `punchlist.md`. (Update: closed — the
  `metadata set` / `metadata values` command family (protocol 9) writes both,
  `roll_sequence.apply_intended_times` re-derives intent on every write, and
  the Metadata tab drives it. See `ARCHITECTURE.md` §4/§14.)

## The app (Swift)

- **One window**, `NavigationSplitView`: a sidebar of rolls (name, negative
  count, unreadable rolls shown disabled with their reason — all from one
  `roll list` call) and a workspace with **Add Scans** and **Edit** tabs
  for whichever roll is selected. One active run app-wide disables the
  sidebar, the tab picker, and both stages' controls. (Update: the workspace
  now has four tabs — Add Scans, Edit, Metadata, Export.)
- **Add Scans** lost the output-folder and film-date fields Phase 2 had;
  shots per negative is the roll's own, shown read-only. The
  overwrite-confirmation dialog is replaced by the overlap sheet — one row
  per `roll_overlap` entry, Skip (default) or Replace. (Update: the overlap
  sheet is **not implemented** — nothing in Swift decodes `roll_overlap`, so
  every run adopts whatever it overlaps in place; see `ARCHITECTURE.md`
  §14.1.)
- **Edit** is new: negatives in sequence order with thumbnails (read via a
  QuickLook-skipping `ThumbnailLoader` path tuned for large published
  TIFFs, not RAW previews), source frames, quality metrics, the dirty
  count, and Apply — driven through the same shared `RunModel`/
  `CLISession` as Run and re-stitch, not a parallel mechanism. (Update: the
  tab now also exposes the Geometry (rotate/flip/crop), Tone, Color and Heal
  (spotting) panels, and small edits ride the resident `serve` daemon.)
- Swift reads a roll only through `roll list` and `roll info`, never by
  parsing `scanny-boy-roll.json` or walking the library itself.

## Scope Phase 3 does not cover

- **Setting the roll capture date or a per-negative date override from the
  app** — see "Sequence and metadata" above and `punchlist.md`. (Update:
  closed; `metadata set` writes both and the Metadata tab drives it.)
- Crop from manifest data, white balance/base neutralisation, extended
  metadata (location, camera, lens, film stock), the cyan fill colour,
  manual negative reordering, and deleting a negative outright are all
  deferred with an attachment point recorded on `punchlist.md`; none is
  scheduled. (Update: several have since landed or changed —
  **extended metadata** is implemented (`metadata set`/`metadata values`,
  protocol 9); **deleting a negative outright** is the `edit delete` op;
  **user crop editing** is `edit crop` (protocol 19), so what remains
  deferred from that line is only the automatic crop from the recorded
  `valid_rect`; and the fill is no longer black — `NORMALIZED_FILL` makes
  the published margin white (§3.14), so the punchlist item is a
  *contrasting* fill, not the cyan one. Manual negative reordering and
  white balance / base neutralisation remain deferred.)
- Negative inversion is Phase 4. (Update: the export's render took it, with the tone at full resolution — docs/EXPORT_PLAN.md §4; a print curve distinct from grade/snap, soft-proofing, paper simulation, and printing itself remain Phase 4's.)
- The rebate-deviation detector (Phase 2's punchlist item) is untouched by
  Phase 3.
- Everything Phase 1 and 2's own "Scope ... does not cover" sections say
  still applies unchanged.

# Flat-field decisions

These are the locked decisions of
[`FLATFIELD_PLAN.md`](FLATFIELD_PLAN.md) section 2, modelled on NegPy's
flat-field feature and adapted to this program's architecture — in
particular to the rule that **Python owns every decision** and to the fact
that a roll's `processing_params` is an invariant. The plan is
authoritative; this section only makes the decisions findable.

## The correction itself

- **Multiplicative gain only**, measured once from a reference shot of the
  **bare light source with no negative in the holder**. No black-frame
  subtraction — same as NegPy.
- **Where it sits**: inside the convert stage, per frame, immediately after
  `raw_decode.decode_raw` and before the intermediate TIFF is written —
  ahead of the stitch stage's photometric gain solve. The stitch's
  per-frame per-channel gains are a global scalar per frame per channel and
  cannot represent a spatial gradient; correcting vignetting first means
  the residual they explain is real exposure mismatch, not falloff, and
  `overlap_mad` becomes a cleaner measurement. `stitch_pipeline.py` needs
  no change at all.
- **The gain map** (port of NegPy's `compute_gain`, values unchanged):
  downsample with `INTER_AREA` so `max(h, w) <= 256`, Gaussian-blur each
  channel with `sigma = max(h, w) / 16`, gain = mean ÷ blurred per channel,
  clipped to `[0.25, 4.0]`. Every constant lives in `flatfield.py` and
  nowhere else.
- **White balance**: the reference is decoded with the project's locked
  `RAW_PARAMS`, which **is** NegPy's decode — linear sensor channels,
  unity white balance (see "Linear decode for NegPy compatibility" below).
  Step 4 divides each channel by its own mean, so any constant per-channel
  scale cancels identically regardless: the gain map is independent of the
  reference's as-shot white balance, and reusing the one decode path the
  project treats as load-bearing beats a second decode configuration.

## The profile

- **`.NEF` references only.** One decode path, one colour story; a JPEG
  reference would have to be guessed into linear light. Non-RAW references
  are a punchlist item.
- The profile records the reference's full-resolution **aspect ratio**; a
  run whose frames differ from it by more than 1% warns
  `FLATFIELD_ASPECT_MISMATCH` — a warning, not a failure.
- **Storage**: gain maps live beside the library database and previews in
  Application Support (`flatfield_root()` = `library_db_path().parent /
  "flatfield"`, mirroring `previews_root()`), so `SCANNY_BOY_LIBRARY_DB`
  relocates them and tests get isolation for free. One float32
  `(h, w, 3)` array plus a format version in an `.npz`. The profile is
  **self-contained**: once created, the reference file can move or be
  deleted; its path is provenance only and is never read again.
- Profile metadata is a row in the library database (table
  `flatfield_profiles`, Alembic revision `0003`), not a JSON sidecar —
  Swift is forbidden from reading the library's storage directly, so
  profiles come back through CLI events either way.
- **Commands**: `flatfield create --reference FILE --name NAME`,
  `flatfield list`, `flatfield delete --profile ID`. `delete` refuses with
  `FLATFIELD_PROFILE_IN_USE` when any roll's
  `processing_params.flat_field.profile_id` names the profile — the gain
  map is the only thing that could reproduce that roll.

## The profile is a roll invariant

**Reversed** (see below): the roll invariant this section describes made a
roll refuse to mix profiles at all, including simply picking a different
one on a later run — reported by users as "profiles get locked to a roll,"
not as the "can't mix corrected and uncorrected negatives" guarantee this
was meant to provide. `flat_field`/`chromatic_aberration` in
`processing_params` and `geometry` in `stitch_params` are now excluded from
`roll_manifest.check_roll_invariants`'s comparison
(`ROLL_PROFILE_PROCESSING_PARAMS_KEYS`/`ROLL_PROFILE_STITCH_PARAMS_KEYS`),
so a roll no longer locks to one profile. What follows is the original
reasoning, kept for context.

`flatfield.profile_token(profile)` — `{"profile_id", "gain_map_sha256",
"params"}` — is folded into `processing_params` under `flat_field`, which
`roll_manifest.check_roll_invariants` used to compare. No new comparison
code. Three consequences, all intended at the time:

- **A roll can never mix corrected and uncorrected negatives.** That is
  the point.
- **Existing rolls refuse new runs** that carry a profile (their
  `processing_params` has no `flat_field` key) — the same breakage the
  gain-normalization merge (#59) caused through `stitch_params`; the
  remedy is the same: start a new roll.
- The key is **absent, not `null`**, when no profile is given, so a
  no-profile run still compares equal to a pre-flat-field roll. CLI users
  without `--flatfield` are unaffected.

`name` is deliberately **not** in the token: renaming a profile must not
invalidate a roll.

## Required in the app, optional in the CLI

`--flatfield` is an optional flag on `convert`, `run`, and `probe`. The
app always passes one and disables Stitch until a profile is chosen; the
CLI stays a general tool and its existing tests keep working unchanged.
(Update: `convert` was renamed `prepare`; the flag now rides `prepare`,
`run`, `probe` and `stitch` — see "Naming: Convert in the UI, prepare
inside the CLI" above.)

## Cost and memory

- **No new progress step**: the correction happens inside the existing
  `PipelineStep.DECODE` boundary. `STEPS_PER_FRAME` stays 3, so
  `run_pipeline`'s calibrated `STITCH_UNITS_PER_FRAME` /
  `STITCH_UNITS_PER_NEGATIVE` constants are untouched.
- **Banded application, one shared map**: the full-resolution gain map is
  materialised once per run and shared read-only across workers (~294 MB,
  one allocation); the multiply runs in horizontal bands of
  `FLATFIELD_BAND_ROWS = 512` rows, decoding/multiplying/re-encoding each
  band back into the same `uint16` array in place. Peak transient per
  worker is ~37 MB instead of ~294 MB, so the 640 MiB per-worker budget
  needs no re-measurement.

## The fixed-point round trip

The decoded frame is **linear** `uint16`; the correction is multiplicative
and valid in linear light, so applying it costs one
`decode_to_linear → multiply → encode_from_linear` round trip, which is
plain fixed-point scaling (`linear.py`). It is exact for every code —
**proved, not assumed**: a test asserts a gain map of exactly 1.0
round-trips a real decoded frame to byte-identical pixels. Where the
correction boosts an already-bright pixel past full scale it clips; the
pipeline emits `FLATFIELD_HIGHLIGHT_CLIPPED` when more than 0.1% of a
frame's pixels clip, rather than losing highlights silently.

## Deliberate differences from NegPy

| NegPy | Here | Why |
| --- | --- | --- |
| Per-image "Apply Flat Field" toggle | Per-roll, by construction | A roll's invariants exist to stop one roll holding inconsistently processed negatives. A per-negative toggle would defeat them. |
| Reference may be RAW or an ordinary image | `.NEF` only | One decode path, one colour story. |
| `flatfield_token()` invalidates a render cache | The same token invalidates a **roll** | There is no render cache here; the equivalent guarantee is the invariant check. |
| Correction applied at render time, skipped for stitched composites and applied per tile instead | Applied once at convert time, per frame | This program's frames *are* the tiles; the intermediate TIFF is the natural place. |

## Scope flat-field does not cover

Non-RAW references, a per-image/per-negative toggle, black-frame
subtraction, and re-measuring `MAX_OVERLAP_MAD` now that overlaps arrive
de-vignetted are all on [`punchlist.md`](punchlist.md).

# Linear decode for NegPy compatibility

NegPy's pipeline (`NegPy/docs/PIPELINE.md`, "Color handling") works on
**linear RGB straight from the raw decode** — `output_color=raw`,
`gamma=(1, 1)`, unity white balance, `adjust_maximum_thr=0.0` — because it
treats the scan as a radiometric measurement of the sensor's own channels
and handles channel balance in film terms. This program fed it the opposite:
`gamma=(1.8, 16)` (LibRaw's generalised curve), `output_color=ProPhoto`
(camera primaries through LibRaw's colour matrix), and
`use_camera_wb=True`. The flat-field, gain and stitch maths happened to run
in linear light, but the written TIFFs were curve-encoded, colour-converted
and white-balanced — violating NegPy's assumption on three counts.

The decode now matches NegPy's exactly. `RAW_PARAMS` is `gamma=(1, 1)`,
`output_color=raw`, `user_wb=[1, 1, 1, 1]` (LibRaw's `user_mul`),
`use_camera_wb=False`, `adjust_maximum_thr=0.0`; everything else is
unchanged. Every TIFF this program writes is linear sensor-channel data.
(Update: the **intermediates** are still linear; the **published** TIFF is
since normalized log density — protocol 8, "Normalization decisions" below —
so read that sentence as the prepare stage's output.)

Consequences, all intended:

- **`romm.py` became `linear.py`.** The 65,536-entry LibRaw-curve decode LUT
  is gone; `decode_to_linear`/`encode_from_linear` are plain fixed-point
  scaling and the round trip is exact for every code. This supersedes the
  Phase 2 amendment above ("The colour decode curve"), which locked LibRaw's
  measured curve into these helpers; the curve measurement was correct but
  the curve itself is no longer wanted.
- **A new ICC profile** (`ScannyBoy-Linear-ProPhoto-v1.icc` at the time;
  renamed `ScannyBoy-Linear-v1.icc`, its primaries claim retired — see
  "Two ICC profiles, and the profile is never load-bearing" (D-2) and
  docs/PROFILE_HONESTY_PLAN.md), generated by
  the same deterministic tool, carried ProPhoto primaries over
  byte-identical from the upstream ProPhoto-v4 source, with a **linear**
  TRC (parametric type 0, g = 1.0): the truth about the pixels' transfer.
  This
  supersedes `ScannyBoy-ROMM-LibRaw-v4.icc`. NegPy reads an input profile's
  primaries only, never its declared TRC, so the boundary behaves as its
  doc assumes.
- **Previews are display-encoded.** The published TIFF is linear, so
  `previews.py` now 16→8-bit encodes through an sRGB LUT (after downscaling
  in linear light) — an untagged 8-bit PNG is assumed sRGB, and SwiftUI
  displays it as-is. The TIFF is never touched. (Update: this was true only
  while the published TIFF was linear. Previews now run the shared positive
  render, `render.encode_positive_uint8` — decode through
  `decode_normalized`, invert, Adobe RGB gamma, the recorded camera matrix,
  and the user's tone/colour ops — and a second un-inverted negative mode
  serves the app's toggle; see `ARCHITECTURE.md` §7.1.)
- **This resolves the punchlist item** that asked for linear ("gamma 1, 1")
  TIFFs, and removes flat-field's curve round trip.
- **Compatibility break**: every existing roll's manifest pins the old
  profile hash and the old `processing_params`, and its TIFFs are
  curve-encoded in a colourimetric space. Re-running `convert` or `stitch`
  against them is refused by the recorded mismatch; existing rolls must be
  reconverted. There is no migration.

Deliberately **not** changed: the demosaic (AHD), `no_auto_bright`,
`output_bps=16`, `highlight_mode=Clip`, the stitch and flat-field maths
(already linear), and the export stage's pixels-only behaviour.

## Geometric calibration (protocol version 7)

`docs/GEOMETRIC_PLAN.md` is the plan; the decisions that shape the code it
produced:

- **A profile is the complete optical description of one rig
  configuration.** The calibration is folded into the existing
  `flatfield_profiles` record — one profile, one `--flatfield` flag, one
  UI — rather than a second table or a second command family. The flag's
  name is stale (it names a whole calibration profile now) and is left as
  a cosmetic follow-up, because a rename would reach the contract, the
  schema, and the app's stored defaults and bury the substance under
  churn.
- **The gauge convention is `K_new = K` and an output frame identical in
  size to the source frame.** Plumb-line straightness is scale-invariant,
  so `K` only sets the numeric scale of `k1`; holding it fixed
  (dimension-derived, recorded in the profile) keeps coefficients
  comparable across sessions and means nothing downstream — layout, disk
  check, memory estimate, bounding boxes — ever learns about distortion.
  The cost is a 1–7 px unsampled border at the frame edge, which
  `MASK_ERODE_PX` already discards. A profile is valid only for the frame
  dimensions it was fitted at; a dimension change means a different
  decode, and a silently rescaled calibration would be worse than none
  (`GEOMETRY_FRAME_SIZE_MISMATCH`).
- **The distortion correction lives in the stitch warp, not the convert
  stage.** Undistorting in convert would resample every frame an extra
  time (a second interpolation pass and its softening) and would move
  flat-field's per-sensor-pixel gain map onto the wrong pixels. Folding it
  into the stitch warp gives one interpolation pass per output pixel, and
  registration — which benefits most — gets it for free: matched points
  are undistorted before RANSAC at zero resampling and zero memory cost.
- **Points are undistorted, not images.** Feature detection runs on the
  existing luminance detection image; the matched *points* go through
  `cv2.undistortPoints` before RANSAC. Undistorting images before
  detection would resample twice and move every gate-C-measured
  detection constant onto new ground. The sub-pixel CA the keypoints
  carry is bounded well under `RANSAC_REPROJ_PX`; it is measured
  (`detection_channel_ca_px` in the calibration report) rather than acted
  on, so the detect-on-green question can be settled later with a number.
- **CA is fitted after undistortion, on half-size decodes, in normalised
  coordinates.** Fitting the per-channel radial scale against raw observed
  corners conflates CA with the (itself radial) distortion polynomial.
  Each output pixel of a `half_size` decode comes from one Bayer quad, so
  the per-channel geometry is true rather than demosaic-smeared; because
  `K_half = K_full / 2`, normalised coordinates are identical at both
  resolutions and nothing is ever scaled back up.
- **The fit that does not measurably help is dropped, automatically.**
  Held-out acceptance gates (relative and absolute improvement, a
  plausible-magnitude band for distortion, residual + improvement for CA)
  decide whether a correction is applied at all. A rejected fit is not an
  error: the profile is still created, the correction is left out, and the
  reason is recorded in `calibration_report` — where the app shows it, so
  the discipline is visible instead of silent.
- **SciPy is a runtime dependency** (`scipy.optimize.least_squares`, the
  project's first nonlinear solver), added for the staged plumb-line fit;
  the bundle carries `scipy/optimize` and excludes its test suites.

# The distortion gate accepts a fit that repeats, not one that flatters (docs/STABILITY_GATE.md)

The staged plumb-line fit is untouched; what changed is the question its
acceptance gate asks.

**Why the question changed.** The old gate accepted a fit only when
undistorting visibly straightened the held-out lines (≥30% relative and
≥0.3 px absolute improvement). On this rig that question cannot be
answered: the held-out straightness floor is **2.54 px of printed-target
error** — 15 µm on the board at the rig's ~168 px/mm — not lens error,
and not something undistortion can remove. Radial distortion's own
contribution to a line residual is mostly absorbed by that line's best
fit; only the curvature survives, a small fraction of a pixel for a 0.4%
distortion. So the fit's own point estimate — **14.5 px of corner
displacement (0.398% of the half-diagonal)** — was rejected by a metric
that measured the target, not the lens. An entirely independent
instrument agrees with the fit: one frame-centred radial field shared
across all ten negatives of the `Six7-after-feather-adjustment` roll,
fitted against 153,520 stitch correspondences with no calibration target
involved, gives **16.8 px (0.46%)** and removes 35.9% of the roll's
global registration residual. Two instruments with no shared data,
objective, or failure mode, landing 15% apart. A synthetic sweep at the
rig's magnification confirmed the mechanism: every board density
recovers a known 15 px distortion correctly at the rig's measured 2.5 px
corner noise, and every one is rejected by the improvement metric — a
five-fold increase in corner count moves that metric from 2.4% to 3.1%,
a five-fold reduction in corner noise moves it to 37.4%. **The binding
constraint is target accuracy, not board density.**

**The new gate is a jackknife.** Leave one calibration frame's
collinear sets out, refit the staged fit, repeat over every frame, and
gate on the relative standard error of the resulting corner
displacements, `SE / mean` with `SE = sqrt((n-1)/n · Σ(θᵢ − θ̄)²)`.
Target error is random across frames and averages out across subsets;
lens distortion is fixed across frames and stays — agreement between
subsets separates the two without the lens signal ever having to
dominate a single measurement, which is why the statistic is nearly
independent of the noise floor that defeats the improvement metric.
Jackknife rather than bootstrap because it is deterministic: no seed,
no resampling draw, and the `(n-1)/n` factor makes the statistic
comparable across frame counts. The improvement numbers are still
computed and still recorded — a genuine diagnostic — but they are no
longer the acceptance criterion. `GEOMETRY_MIN_IMPROVEMENT_FRACTION`
and `GEOMETRY_MIN_IMPROVEMENT_PX` remain in `geometry_fit.py` as that
diagnostic's constants, deliberately unreferenced by the acceptance
branch.

- **`GEOMETRY_MAX_RELATIVE_SE = 0.25`**, from the synthetic sweep of
  `scripts/measure-stability-gate.py` (plan section 6.1): at the rig's
  2.5 px corner noise, 16 frames, the relative SE reads 174% at true
  zero and 17.0% at a true 15 px on the 4.0 mm board (95% → 4.0% on the
  2.0 mm board), so 25% separates a real distortion from none on both.
  On the 4.0 mm board the margin is thin — the finer 2.0 mm board is
  what gives the gate room to work, which is an independent reason to
  print `calibration/lens_calibration_targets.pdf`, but **it was not
  what unblocked the gate**; the gate was wrong, not the board.
- **`MAGNITUDE_EXPECTED_MAX_PERCENT` raised 0.2 → 0.6.** The lens
  measures 0.398% and 0.46% by the two independent methods, so the old
  band would have flagged the known truth suspect on every future
  calibration — a warning that fires on the known truth is a warning
  that gets ignored. The hard bounds (0.01–1.0%) do not move.
- **No manifest or contract version change.** `calibration_report` is a
  profile-level diagnostic blob; the jackknife keys and the judged
  threshold (`max_relative_se`) are recorded beside the existing
  distortion keys so a stored profile reads without knowing which build
  wrote it. Existing profiles are unaffected and stay rejected —
  nothing retroactively accepts a stored fit; geometry stays null until
  the user recalibrates, and the CLI's rejection warning says so.
- **Measured cost:** the jackknife is one extra staged fit per training
  frame, and a staged fit is the three `least_squares` solves the
  pre-gate call already did once. On synthetic 4.0 mm-board data (12
  training frames, 40 corners each) one staged fit measures 0.36 s and
  the gated fit 5.13 s — a **14.1× multiplier against the ideal n+1 =
  13×**, the remainder being the leave-one-out displacement
  evaluations. Calibration is a deliberate, occasional operation; the
  fallback (a fixed-count jackknife over frame *groups*) was not
  needed.

**What this gate does not prove, recorded so nobody later believes it
does.** A stability gate is blind to *systematic* target error: a board
uniformly stretched toward its edges would make every subset agree on a
distortion that is the printer's, not the lens's — and the old
straightness gate was equally blind to that. The only real defence is
an independent measurement, which here is the stitch correspondences
agreeing at 16.8 px, and that defence is *not* part of the gate. The
frames also share one sitting (focus, placement, thermal state), so the
SE is optimistic to an unknown degree — another reason the threshold
carries margin (25% against a 17% reading at the rig's worst-case
board). And the estimator is biased upward by noise (fitted ≈ truth +
noise inflation: ~1.6 px on the 4.0 mm board, per the sweep), so the
recorded 14.5 px may be nearer 13 px of real distortion; recorded in
the report's terms, **not debiased** — the bias is smaller than the
disagreement between the two independent measurements. The final
confirmation is outstanding: recalibrate against the printed 2 mm
target and check the accepted coefficient still lands near 15–17 px.
`GEOMETRY_MAX_RELATIVE_SE` is listed in the 2D-grid section's
"Unmeasured constants awaiting a real-scan gate" below, alongside
`GRID_PITCH_RATIO_MIN` and friends, exactly as the plan requires.

# Normalization decisions (protocol version 8)

Scan normalization ("Convert") is implemented per docs/NORMALIZATION_PLAN.md
(deleted after landing, per the repo's convention); this section is the
durable record of its locked decisions.

## The published TIFF is a baked, normalized working intermediate

The positive now exists — the export is a rendered positive in Adobe RGB
(docs/EXPORT_PLAN.md §5) — but the published TIFF's role is unchanged: it
is still the working intermediate the creative-edit stage reads, still a
negative, still normalized log density. What changed is where the positive
comes from: not "later, in a print stage", but at export, by rendering
these same pixels. After deciding that, everything else in this section
still follows.

- **The bake is the fidelity-preserving choice, not a compromise (D-1).**
  Normalized log density in `uint16` is the *most* precision-efficient
  16-bit container available: linear `uint16` spends its resolution where
  the light is, and a negative's picture information is where the light is
  not — about 11.3 effective bits at the dense end against a uniform 16
  once log-encoded, at identical file size.
- **The bake is arithmetically reversible.** The per-channel floors and
  ceils are recorded in the roll record's `normalization` block, so
  `10 ** (floor + val * (ceil - floor))` recovers the linear composite to
  within quantization. `normalization.decode_normalized` is the single
  inverse of the encode; everything downstream goes through it.
- **It stays a negative in appearance.** `val = 0` is the scene highlight
  (dark), `val = 1` the scene shadow (light). Inversion used to be the
  print stage's (Phase 4); the export's render now does it (docs/
  EXPORT_PLAN.md §4), and the preview still displays `1 - val` so the
  Edit filmstrip is legible — the file in Photoshop looks like a negative
  and the preview beside it looks positive. Both are correct.

## E-6 and `ProcessMode` are not ported

NegPy's `ProcessMode` is not ported: no E-6 branch, no swapped percentiles,
no fixed-range fallback, no dead flag. This rig photographs negative film
under white light; if transparency support is ever wanted it is a new
feature with its own plan. (This is narrower than it once read: monochrome
*negative* film — silver B&W — is supported; see "Monochrome film" below.
Chromogenic B&W and stained negatives are not E-6 either and are covered
there too.)

## Monochrome film: detect at the roll, freeze, never flip (docs/MONOCHROME_PLAN.md)

A silver B&W negative's three Bayer-CFA channels record the same image,
differing only by a per-channel gain and offset. Stripping that affine
(subtract each channel's median, divide by its MAD) and reading the P90 of
the surviving per-pixel spread separates the two classes cleanly: near
zero on a silver negative, well above it on anything with real colour
content — including a chromogenic B&W's orange mask or a pyro/PMK stain,
both of which correctly normalize down the colour path rather than being
mistaken for silver B&W.

**Detect at the roll, not the negative.** A roll is one film stock; a
per-negative decision would eventually flip on a snow scene or a grey wall
and produce a roll where one negative is single-channel and the rest are
not — the same failure `clamp_bounds` already exists to prevent for
bounds. The decision is therefore frozen once, on the roll's first stitch
run, into a top-level `film` manifest block, and **never changes**
afterward except by re-stitching the whole roll from scratch. A later
run's fresh evidence that disagrees only warns
(`MONO_DECISION_CONFLICT`) — it does not raise and does not flip. An
ambiguous first-run statistic (between the two pinned thresholds) resolves
to colour and warns (`MONO_DETECT_AMBIGUOUS`): colour is the lossless
choice, since a colour roll is never wrong to publish as three channels,
while a mono roll wrongly published as three channels is not obviously
wrong either — it is simply not what a reader would expect. The
thresholds themselves are pinned constants, measured from real rolls and
approved before being written down, per this project's house rule that no
threshold ships ungrounded.

**The collapse runs between the log transfer and the bounds analysis**,
never before the log and never after the bounds. Averaging in linear
light weights by intensity, not density, and biases the merge toward the
film base; averaging after `analyze_bounds` would let the colour axis
solve for an orange mask that a mono negative does not have.
`analyze_bounds`' chroma gate still runs on the collapsed one-channel
image — it degenerates harmlessly, since with no colour there is no
colour deviation to add back, and that degeneracy is `analyze_bounds`'
generalised arithmetic reaching the right answer on its own, not a
special case.

**The merge weights are a minimum-variance estimator, not a luma curve —
the single most likely thing here for a future reader to "correct" back
to Rec.709.** Three channels are three noisy measurements of one physical
quantity, silver density; the merge weights them `(0.25, 0.50, 0.25)`
because a Bayer CFA has twice as many green sites and green carries about
twice the photons. Rec.709's coefficients model the eye's response to
display primaries for perceived scene brightness — the right job for
`analyze_bounds`' luma axis (a proxy for "how bright", which is what a
black point should track), the wrong job for combining three redundant
measurements of density.

**The merge does not bracket its inputs, and the absolute tolerances
survive anyway.** A weighted mean is narrower than its inputs' envelope by
construction; re-centering it at the weighted mean of the inputs' medians
does not change that. What it guarantees is that the merged channel sits
at the right density *level* — close enough that `REBATE_DENSITY_TOLERANCE`
and `DENSE_BORDER_TOLERANCE`'s absolute log-density thresholds keep
meaning what they were measured to mean on a colour composite.

**The published mono TIFF is single-channel, and that is final.** Toning
is not a goal of this project, so there is no downstream consumer that
would ever need the per-channel record back — the collapse is a one-way
publish decision, not a reversible colour-to-mono preview mode.

## Two ICC profiles, and the profile is never load-bearing (D-2)

The prepare stage's intermediates stay linear; the published TIFF does not;
the export is colour-managed. Three profiles used to be two; the split is
now explicit (docs/EXPORT_PLAN.md §9):

| | Intermediates (prepare) | Published TIFF (stitch) | Export |
| --- | --- | --- | --- |
| Profile | `ScannyBoy-Linear-v1.icc` | `ScannyBoy-Density-v1.icc` (grey: `-Density-Grey-`) | `ScannyBoy-Export-AdobeRGB-v1.icc` (grey: `-Export-Grey-`) |
| TRC | parametric type 0, g = 1.0 | parametric type 0, **g = 2.2** | `curv` g = 563/256 |
| TRC claim | true — the pixels are linear | a **viewing convention** | true (Adobe RGB) |
| Primaries | a **wide container**, not a measurement | a **wide container**, not a measurement | **true** — the render's matrix conversion (§ below) put the pixels there |

g = 2.2 is deliberately not accurate: a normalized log encoding over ~2
decades is closer to gamma 3.3, and no ICC parametric type expresses it. A
*correct* profile would decode the file back to un-normalized linear —
undoing the one thing normalization does. The tag's only job is legibility
in external viewers while debugging the edit stage.

**Primaries: the wide container, not a measurement** (docs/
PROFILE_HONESTY_PLAN.md). The old linear profile's description asserted
"ProPhoto primaries", and the table's former `Claim` row ("true — the
pixels are linear") conflated the TRC claim with a primaries claim the
pixels never supported: `raw_decode.RAW_PARAMS` decodes the camera's own
filter responses (`output_color=raw`, `user_wb=[1, 1, 1, 1]`), so they
have never been converted into any colorimetric space, and no primaries
could be true of them. ICC has no "unknown primaries" encoding — a
matrix/TRC profile must carry `rXYZ`/`gXYZ`/`bXYZ`, and the tag cannot be
dropped (an untagged file reads as sRGB, a *different* false claim) — so
the program stopped asserting and started labelling: the profiles were
renamed without "ProPhoto", and the descriptions now state that the
colorants are a deliberately wide container, chosen so a viewer applying
the profile does not clip camera-native values, not a measurement of this
camera's primaries. The colorant, white point and `chad` bytes were
deliberately left alone — byte-identical to the vendored ProPhoto-v4
source — because a wide container is the right choice for a debugging tag
and changing them would alter how every existing viewer renders the
intermediates for no gain. **This breaks both bundled-profile invariants**:
every roll recorded before the rename fails `check_roll_invariants` with
`ROLL_INVARIANT_MISMATCH` on `icc_profile_sha256` (and the published
field), which is the intended loud break; existing rolls must be
reconverted. There is no migration.

**Known remaining half-truth** (on `punchlist.md`): the intermediates'
profile could be made fully true by generating it per camera from
`camera_color.rgb_xyz_matrix` (the sensor's real primaries, linear TRC),
with the attachment point at `probe.py`'s invariant seeding. It is
deferred: it only half-helps the published TIFF (whose TRC is normalized
log density, which no ICC TRC expresses, and whose per-channel
normalization moved its effective white point), and it converts two
build-time constants into runtime-generated bytes.

**The load-bearing rule:** the profile must never become load-bearing for
the intermediates or the published TIFF. Every internal consumer decodes
through `normalization.decode_normalized`, never through an ICC transform;
a grep-shaped guard test keeps the loader out of everything but the write
path. `RollInvariants` grew `published_icc_profile_sha256` beside the
intermediates' hash, and `check_roll_invariants` compares both.

The export profiles are the deliberate exception, and the reason "never
load-bearing" needed splitting (docs/EXPORT_PLAN.md §9): for the export,
the profile is genuinely descriptive — the render's matrix conversion
really put the pixels in the tagged space, and a program that honours the
profile is doing the right thing. The guard test's rule stands because the
export *writes* a tagged file: `render.py` reaches for the pinned gamma
constant, never for the loader.

## The transfer, the bounds, and the headroom

Ported from NegPy's `normalization.py` unchanged: `D_log = log10(clamp(I,
1e-6, 1.0))`, then a per-channel affine stretch with `floor` (the low log
percentile — dense film, scene highlight) mapping to `0.0` and `ceil`
(thin film / base) to `1.0`. Bounds are sampled on **two independent axes**
and recombined — luma at `BASE_LUMA_CLIP = 0.01` fixing the floor/ceil
*mean*, colour at `BASE_COLOR_CLIP = 1.0` fixing each channel's *deviation*
— with NegPy's asymmetry kept: **mean** on the luma axis, **median** on the
colour axis. The dense end reads one shared, chroma-gated pixel set drawn
from the luma-extreme band (independent per-channel percentiles read a
different scene object per channel and mistake coloured highlights for film
cast); the thin end reads plain percentiles, physically anchored at film
base. The constants are pinned, not exposed: normalization is automatic,
and `normalize` rides in `processing_params` as a **roll invariant** — the
key is always present, there is no `--no-normalize`, and retuning any
constant invalidates existing rolls (same breakage as flat-field and the
gain-normalization merges; start a new roll).

**Encoding with asymmetric headroom (§3.6).** `uint16` cannot keep NegPy's
unclamped float, so the encode reserves
`NORMALIZED_HEADROOM_LOW = 0.15` at the dense end (speculars a block
median never saw) and `0.10` at the thin end (physically bounded by clear
base). Excursions past the rails clip — documented, not accidental; the
observed pre-clip extrema and the clipped fraction are recorded per
negative so the constants can be tuned from real scans, and
`NORMALIZE_HEADROOM_CLIPPED` warns when they clip too much.

**The headroom is now used, not merely reserved (docs/HEADROOM.md).** The
display render carries inverted values on `[0, DISPLAY_CEILING]` — where
`DISPLAY_CEILING = 1 + NORMALIZED_HEADROOM_LOW` — from the `1 - val` flip
through the matrix encode and into the tone curve, which compresses the
extra range back to display white. Flat renders without a tone op keep the
old `[0, 1]` clip; `_clipped_fractions` on export now counts only
genuinely out-of-gamut excursions past `DISPLAY_CEILING`, not recoverable
highlight detail.

## The analysis cell is pinned to source pixels, not derived from the canvas (`normalize` format_version 4)

**`ANALYSIS_BLOCK_PX = 6`.** `block_median_grid`'s block used to be
`b = ceil(max(h, w) / 1024)` — a grid bounded on its long side, which ties
the cell to the canvas's **aspect ratio** rather than to anything on the
film. That was invisible while a negative was one frame or a short strip.
At the grid workload of docs/GRID_STITCH_PLAN.md §7.1 it is not: one
6000×4000 frame gives b = 6 (36 µm at the reference rig) while a 5×2's
22000×6667 canvas gives b = 22 (132 µm) — a 13× larger cell over a grid
holding **2.2× fewer** samples, because the long-side bound makes cell
count fall as the canvas elongates.

**Measured, on one real frame tiled to each canvas size** so the film
content per unit area is identical and the only variable is the cell: the
floor lifted 0.049 log10 D and the span contracted 0.057 (3.8%) from 1×1
to 5×2, monotonically — roughly 0.19 stop of black point, on the same
negative, decided by the grid it happened to be shot in. Nothing caught
it: `CLAMP_MIN_WINDOW` is 0.5 log10 D, nine times too coarse to see it.
With b pinned the same sweep moves 0.004 across 2×2…5×2. The thin end was
never the problem (−0.006 log10 D across b = 6…26), which is why
`film_base`'s anchor kept working; the dense end is where the small-sample
percentile lives.

**Rejected: making the cell a function of the grid configuration.** That
keeps the shape dependence and adds plumbing. Every consumer — the two
meters, both border detectors, the neutral residual's 3×3 neighbourhood —
is a statement about a physical scale on film, so the fix is to remove the
variable, not parameterise it. Pinning also makes docs/REBATE_ANCHORING.md
§2.2's claim that `film_base` uses "the same reduction the per-negative
path uses" literally true; it was comparing a base frame at b = 6 against
a 5×2 negative at b = 22.

**It costs nothing.** On a 22000×6667 canvas the reduction runs 6.9 s at
b = 6 against 7.5 s at b = 22 — the cost is the whole-canvas copy either
way — and the grid grows from 3.5 MiB to 47 MiB against a 23.8 GB
estimated peak. (That copy, ~1× the log-density array, is not modelled in
`estimate_peak_bytes`. It is b-independent and the warp branch dominates,
so it does not bind; noted, not fixed.)

`ANALYSIS_PASSTHROUGH_PX = 1024` keeps the old "already at analysis
resolution, pass through unreduced" behaviour under its own name, and
`analysis_grid_block_sizes` reports block 1 below it so a caller mapping
canvas coordinates onto cells stays consistent with what the reduction
actually did. Production never approaches the threshold — the smallest
canvas is one 6000 px frame — so the step from block 1 to block 6 at the
boundary is a property of synthetic inputs alone.

## The region gates are absolute, floored by the fractions they replaced

Pinning the cell makes a cell count **an area on film**, which is what let
the second half of the same change happen. Three gates were fractions of
the analysis region, so their physical meaning scaled with the negative's
area while the features they gate scale with the *edge they run along*:

- `REBATE_MIN_AREA_FRACTION` (0.02) demanded 17 mm² on one frame and
  106 mm² on a 5×2. A 1.5 mm rebate band across the short ends of a
  132×40 mm canvas is 60 mm² — it cleared 2% of a 2×2 region and missed
  it on a 5×2. Now `REBATE_MIN_AREA_CELLS = 13_340`.
- `DENSE_BORDER_MIN_AREA_FRACTION` (0.005), likewise, now
  `DENSE_BORDER_MIN_AREA_CELLS = 3_335` (4.3 mm²).
- `DENSE_BORDER_MAX_AREA_FRACTION` (0.05) and
  `DENSE_BORDER_MAX_BBOX_FRACTION` (0.05) both became
  `DENSE_BORDER_MAX_WIDTH_CELLS = 33` (1.2 mm), applied to the stripe's
  **mean width** (`area / bbox long side`) and to its bounding box's thin
  axis. A stripe's area is its width times the border it runs along, so
  capping the area as a fraction of the region admitted a 265 mm² component
  on a 5×2 where one frame admitted 43; and 5% of each grid axis called
  anything up to 6.6 mm wide a sliver on a 22000 px canvas. Capping the
  width says what both constants always meant, and is *tighter* than the
  area fraction on a short component — the direction that keeps scene
  content out.

**The fractions survive as small-region guards, not as the gate.** One
helper, `_region_limit(absolute, fraction, extent)`, takes whichever is
**smaller**. On any region at or above a frame's worth of film the
absolute binds; below it — a degenerate stitch, a synthetic grid — an
absolute cell count is not a meaningful piece of film and the old fraction
takes over. So the change is never stricter than the fraction rule was on
a small region, never looser than it was on a large one, and every
absolute is calibrated to reproduce single-frame behaviour exactly by
construction.

Every number here is still **provisional and unmeasured** in the sense the
`REBATE_*` and `DENSE_BORDER_*` constants always were — this change fixes
how they *scale*, not what they are worth. They stay on the punchlist.

**`NORMALIZE_FORMAT_VERSION` → 4**, and this is the first bump where the
meters' *arithmetic* moved rather than the recorded constant set: a v3
roll re-stitched under v4 gets different bounds on any canvas that is not
one frame. `upgrade_normalize_params` gained a strip list alongside its
`setdefault` injection, because a key whose **value** changed cannot be
absorbed by `setdefault` — `analysis_grid`,
`dense_border_max_area_fraction` and `dense_border_max_bbox_fraction` are
removed by name, or every pre-v4 roll fails the exact-dict invariant
comparison. (Update: the current value is **5**; the film-extent pass
(docs/BLACK_POINT_REFINEMENT.md) folded the `REBATE_*`, `OPAQUE_*` and
`FILM_EXTENT_*` families into `build_params()`, so a v4 roll's recorded
bounds are not comparable with a v5 one.)

## Naming: "Convert" in the UI, `prepare` inside the CLI (§3.9)

**`run` stays `run`.** The user-facing verb is "Convert" everywhere in
Swift — the button, the results section, the empty state. CLI stage 1 is
renamed `convert` → `prepare` (subcommand and `Stage.PREPARE`); stage 2
keeps `stitch`, which is still exactly what it does. "Convert" is then
reserved, unambiguously, for the whole `run`.

## The analysis region, and the rebate detector (D-3, §3.13)

Captures **usually** include the film rebate, sometimes not — and it is the
variability that hurts, not the rebate. Rebate is the thinnest thing in the
capture, so a per-frame framing accident would otherwise decide whether a
negative is normalized against base or against its own darkest shadow.

The meters therefore read an **analysis region**, resolved as: explicit
crop ROI (does not exist yet) → valid rect → whole grid — as a flat boolean
over the prefiltered grid, so every pass provably reads the identical
pixel set. `layout.largest_valid_rect` finally has its first real use: it
restricts the meters only, never crops the output, and keeps the
uncovered-canvas fill (log10(1e-6) = -6.0, a colossal outlier at the dense
end) out of the floor percentile.

The **rebate detector** (D-3) works on density and border connectivity, not
geometry: on a negative, base is strictly the thinnest thing on the film,
so the thinnest border-touching featureless population that is *separated*
from the scene distribution is rebate. Detected cells are excluded from the
meters and the measured `base_density` is recorded raw (no exposure-time
correction — that belongs to the consumer; one stop of exposure shifts it
by 0.30 in log D), `None` when the base is sensor-clipped (clipped base is
worthless base). The documented false positive is a genuinely deep,
featureless border-touching shadow: mild degradation, never invented data.
Using the base roll-wide (D-4's staging step 2) is on the punchlist.

## The film extent, and the black point (E-1…E-4)

**The analysis region is not maximal** (docs/BLACK_POINT_REFINEMENT.md §0.1).
It has to be entirely film and representative of the scene; losing 10% of
the film costs a percentile meter measured over three million cells
nothing, and admitting 0.01% of non-film destroys it. That is the opposite
of the disposition `withhold_dense_border` was written with, and
deliberately so: that detector withholds a *mask*, and a false positive
silently deletes picture, so its gates exist to keep it off real scene
content. The film-extent pass withholds a *rectangle* at the region's
edge, where a false positive costs a slice of ordinary film and nothing
else — so every one of its rules is biased toward shrinking.

**The rectangle is the instrument, and that rests on a rig fact**: the
user's negative carriers have sharp 90° corners (stated 2026-09-07), so the
incursion is a set of edge-parallel bands at most slightly rotated by
registration. A per-edge inset driven by each edge's deepest incursion is
the correct estimator for that geometry. **A user with a different carrier
geometry — rounded corners, a glass carrier, corner wedges — invalidates
this** and needs the mask fallback this plan deliberately did not write.

**The rebate detector cannot carry this, and the reason is measured**
(§0.5): along a stitched canvas's rebate band the base density drifts
monotonically 0.088 decades across the canvas against a
`REBATE_DENSITY_TOLERANCE` of 0.10 — the tolerance is almost entirely
consumed by flat-field residual before any real base variation. A global
thin anchor cannot trace a film boundary at that drift; any future
rebate-traced boundary needs a *locally* anchored thin reference. On
`_DSC5280`'s right edge there is additionally no thin landmark at all —
the film's own edge content there is *denser* than base, so
`REBATE_ANCHOR_PERCENTILE − REBATE_DENSITY_TOLERANCE` can never reach it.
Rebate's value here is corroboration only: `film_extent.rebate_agrees`
records whether the two mechanisms agree where both fire, and nothing
reads it (§5.3) — it is the evidence a later plan needs before promoting
rebate to a hard outer bound.

**`withhold_dense_border` was measured and found insufficient — do not
re-litigate loosening its constants** (§0.2). The carrier band on
`_DSC5280` fails two gates: thinness (bbox 37 cells against 33, inflated by
a 0.46° slant its *mean* width of 26 does not have) and flatness (the band
fades in partway down the canvas, spread 0.252 against 0.05). With
`MAX_WIDTH = 40, MAX_SPREAD = 0.30` the detector fires and withholds 2.29%
of the region — and the floor moves only −3.14 → −2.84, still more than a
decade wrong. The reason is the transition ramp: the film→carrier boundary
is a monotone fall through every density film legitimately occupies, so
whatever a density threshold removes, the ramp behind it still owns the
floor percentile. Hence the two-part instrument: the gap statistic
locates the incursion, the rectangle clears it (§0.3 — after the mask, the
residual floor-setting cells were 100% within 60 cells of the region's
edge, on all three negatives measured).

**Nothing here crops output.** Like the analysis region it refines, the
film-extent rect restricts the meters only; `valid_rect` and
`coverage_fraction` remain the machine-readable coverage answer. And the
no-op path is load-bearing: most negatives have no carrier in frame, and a
change that makes the pass fire on a clean frame is a regression even if
the resulting picture looks fine.

## Per-negative bounds (D-4), and the uncovered canvas (§3.14)

**Ship per-negative bounds on both axes**: every frame self-normalizes, no
cross-run coupling, matching the publish-once model. The run's aggregate
(per-channel median) is recorded in the database too, so the data for a
roll-consistency feature exists from day one; the colour axis is the one
that actually wants to be roll-wide (`--colour-bounds run-median` is the
likely shape).

**The uncovered canvas fills at the top of the encodable range**:
`NORMALIZED_FILL = 1.0 + NORMALIZED_HEADROOM_HIGH`, code 65535. **Expect
the published file's border to flip from black to white** — it looks like a
regression the first time and is not: a fill of `0.0` in a negative-looking
file becomes a white border in the eventual positive, and the second one
loses. The fill value is a cosmetic hint, not a sentinel, and nothing in
the render path may key off it; the machine-readable coverage answer is
`valid_rect` plus `coverage_fraction`.

## Rebate anchoring: the thin end from a roll-level base frame (protocol version 13)

Each negative's thin-end colour deviation (`Bounds.ceils`) used to come from
per-channel percentiles of scene content — wrong on high-key frames with no
shadows. **A dedicated base frame per roll now supplies the orange mask**
(docs/REBATE_ANCHORING.md): one leader-style shot, attached before the
first convert, locked when the first negative publishes, measured by
`film_base.load` and gated before it is ever written.

- **Only the colour axis changes.** `mean_lc` still comes from the
  negative's own luma percentile; the base frame fixes per-channel
  *deviation*, not level. §0.2's exposure invariance — a common-mode shift
  in the measured base cancels in the recombination — was verified on real
  film before B-5 landed.
- **Monochrome rolls record but do not consume.** The base frame is still
  required (one rule, no branches); `len(base_refs) == channels` makes a
  3-array fall back on the collapsed 1-channel image.
- **`clamp_bounds` needs no change.** With a fixed anchor every negative's
  ceils deviations agree, the population MAD collapses, and
  `CLAMP_MIN_WINDOW = 0.5` floors the window — the clamp stays inert on
  legitimate exposure variation.
- **Gate constants** (`FILM_BASE_MIN_AREA_FRACTION = 0.20`,
  `FILM_BASE_MAX_COMPONENT_SPREAD = 0.05`, and the rest in
  `film_base.py`) were pinned from the v1 slim §11 measurement on
  leader-style real film, 2026-09-06.
- **`NORMALIZE_FORMAT_VERSION` does not move** — no constant in
  `normalization.build_params()` changed; `analyze_bounds` with
  `base_refs=None` is byte-identical to before B-5.

## Auto-rotation: the rebate squared, as an edit (protocol version 7)

A stitched canvas comes out in whatever orientation the strip was scanned.
**Auto-rotation is a nondestructive edit, never a pixel change at stitch
time**: the stitch stage measures one rebate-squaring angle on the encoded
composite (`scanny_boy/auto_rotate.py`), seeds one `rotate_fine` ops-log
entry — params `{"angle_deg": number, "source": "auto"}`, emitted as
`edit_recorded` — on each *newly published* negative, and stops there. The
published TIFF is never rotated; the pixels are transformed only where the
ops log meets pixels, at preview generation and export, exactly like a
user's quarter turns. A re-stitch adopts the existing negative and never
re-seeds (no double rotation, no trampling of user edits), and
`--no-auto-rotate` turns the seeding off.

**The angle is density-based, not edge-detection-based**: the rebate is
strictly the thinnest thing on the film (D-3's discriminator, reused in
normalized space — the published file's per-image stretch puts the rebate
at the thin rail and the empty-canvas fill above it, both separable from
scene), and one minimum-area enclosing rectangle of the picture area gives
a single clockwise angle that squares the rebate's frame boundary with the
canvas. That rectangle *is* the "split the difference": the rebate's four
edges are neither straight nor parallel, and the minimum-area compromise
across all four sides is the best estimate of what square means. The
detector refuses to invent a rotation — no rebate, too little scene, or a
tilt beyond the clamps seeds nothing.

**What the rotation uncovers fills the way stitching fills**: the fine
rotation keeps the canvas dimensions, and pixels whose source falls
outside get the `NORMALIZED_FILL` sentinel — the same code, the same thin
rail, rendered black in previews, nothing new downstream.

**The ops log's net state becomes a triple**:
`(rotation_quarter_turns, flipped_horizontally, fine_rotation_deg)`, the
canonical replay being mirror, then fine warp, then quarter turns. A flip
negates the fine angle along with the turn count, because `flip ∘ rot =
rot^-1 ∘ flip` holds for rotations of any angle; quarter turns commute
with the fine warp. The seeded angle is recorded nowhere else — the ops
log is the single source of truth, and `roll info` derives the net angle
the same way it derives the turns. (Update: the geometric triple is now
`repo.EditState`'s `quarter_turns`/`flipped`/`fine_angle_deg`, carried
alongside the coalesced state ops `tone`, `color`, `spots` and `crop`; the
log remains the single source of truth.)

## The dense-end defenses, learned from roll R1 (protocol version 8, revised)

Roll R1's frames 6-8 published nearly black previews, their negatives
looking no denser than their neighbours'. The post-mortem found **two
independent ways the max-density anchor (the `floors`) latches
contamination instead of scene content**, and this decision adds one
defense against each plus a safety net:

**Coverage intersection (§1.5, revised).** Inward rounding of the valid
rect is not enough: the blend's `covered` mask can hold *interior* holes
the layout's `largest_valid_rect` never saw (a stitch's coverage is
geometric, the blend's is per-pixel). Negative 8's meters read fill cells
(log10(1e-6) = -6.0) inside its own valid rect and produced floors of
exactly -6.0 — the failure §1.5's comment predicted, arriving through the
gap between two coverage notions. Every candidate region — the rect's, the
outward-rounded fallback's, and the whole grid's — is now intersected with
the blocks the blend actually covered; a fully-uncovered candidate falls
back to the covered blocks, never to the unfiltered grid.

**The dense-border detector (the rebate detector's mirror).** Negative 7
(and partially 6) carried a dark, featureless stripe along the canvas's
top border — a partially-lit sliver beyond the film edge that stitching
reported as covered. Raw dense-end percentiles have no defense: the block
median only removes extremes smaller than one block, and the rebate
detector withholds *thin* border junk only. The mirror gates on density,
not geometry: candidates within tolerance of the region's dense-end anchor
(P0.1 luma), border-touching components gated on area (too small is not a
stripe), thickness (a stripe is thin perpendicular to its border, by
bounding box and by mean width; scene content dense enough to matter spans
the frame), flatness *along* its length (contamination is featureless along
the border; edge fog fades across its thickness, so the test runs on the
along-length medians), and separation from the scene's own dense tail (the
gate that makes "no stripe at all" return cleanly). The detector
re-anchors up to `DENSE_BORDER_MAX_PASSES`: a gradient stripe is eaten
band by band, converging when the residue reaches scene density. The
documented false positive is a genuinely dense, thin, featureless
border-touching scene object: scene highlights map slightly brighter —
mild degradation, never invented data. Withheld fractions are recorded per
negative (`normalization.dense_border`).

**The opaque-holder gate (the detector that needs no geometry).** Scans
that catch a section of the negative holder put a *completely opaque* region
in the analysis rect. Neither existing detector helps: the rebate detector
withholds thin junk only, and the dense-border mirror reads the film's
maximum off the frame's own dense tail, so once it has its candidate band
every remaining gate is about the contaminant's *shape* — border-touching,
thin, featureless, bounded in area — and a holder section is none of those.
It is arbitrarily large, arbitrarily shaped, and can sit anywhere the film
does not.

It is also the one contaminant with an **absolute** discriminator, so it
needs none of those gates. The holder passes no light, so `to_log_density`'s
clamp lands it at `log10(_DENSITY_FLOOR) = -6.0`, while a colour negative's
Dmax runs about 2.0–2.5 above base and a black-and-white negative's about
2.5–3.0. More than `OPAQUE_MAX_DENSITY_BELOW_BASE` (3.2) decades below the
thin end is not film at any shape or size. The anchor is the region's
`REBATE_ANCHOR_PERCENTILE` **thin**-end luma, and the choice of end is the
point: the holder contaminates the dense tail only, so a dense-end anchor
would move with the very thing it is measuring, while the thin end is
untouchable by it. `OPAQUE_DILATE_CELLS` (1) withholds the straddlers — a
block median across the holder boundary is a median over both populations
and lands between them, above the gate and contaminated.

**Why it runs before both other detectors** (`composite.py`, immediately
after `_region_keep`): the holder owns every dense-end percentile it
touches. `DENSE_BORDER_ANCHOR_PERCENTILE` (P0.1) lands *inside* the holder
and `DENSE_BORDER_TOLERANCE`'s 0.2-wide band then covers holder only, so the
edge fog the mirror exists to catch sits three decades outside it,
undetected — the holder was blinding the mirror as well as pinning the
floor. Before `detect_rebate` too, so the gate's own anchor still reads the
film base rather than the thinnest scene content, which is the physical
statement the constant is written against. The scale of the floor damage it
prevents: `BASE_LUMA_CLIP` is 0.01 percent of the region, a few hundred
cells on a real negative's grid, and a one-cell-wide sliver along one grid
edge is several times that — deliberately stated against the region rather
than a particular prefilter geometry, since the block rule has changed once
already. It does not take much holder to move the floor from a real Dmax near
−3.0 to −6.0, roughly doubling the span and squeezing the picture into the
top half of `val`.

**Measured against real film** (roll `3f8c78d0`, Nikon Z f, colour negative,
two negatives): reconstructing each published TIFF's log grid through
`decode_normalized` and the recorded bounds puts the thin-end anchor at
−0.629/−0.671 and the P0.01 luma floor at −3.140/−3.208 — a film depth of
**2.51 and 2.54 decades**, against the 3.2 the constant allows. The gate
fires on neither. Its threshold lands 0.59/0.57 decades below the densest
cell either negative contains and 2.17 decades above the −6.0 the holder
clamps to, which is the separation the constant is trading off. Neither
negative carries holder, so this measures the false-positive margin only; the
failure itself is still unconfirmed against a scan that has it.

A *wholly* opaque region is the case the relative gate structurally cannot
see — with nothing but holder there is no thin end for the holder to be
decades below, and the anchor is the holder itself — so
`OPAQUE_MIN_ANCHOR_ABOVE_CLAMP` (1.0 decade above the clamp) is an absolute
check that raises `NormalizationError` naming the layout. Left to fall
through the case does still fail, with `analyze_bounds` reporting a
degenerate channel, but that message sends the reader after the meters when
the fault is the rect sitting on the holder. The known false positive is
scene content at literally zero transmission, which real film cannot reach —
the block-median prefilter already absorbs isolated clipped pixels, so only
a whole block of them fires the gate. Withheld fractions and the absolute
threshold are recorded per negative (`normalization.opaque`). All three
constants are provisional and unmeasured, like the `REBATE_*` and
`DENSE_BORDER_*` sets they join.

**The roll-population clamp (D-4's safety net, not a policy change).**
Both detectors can miss a contaminant the per-frame statistics cannot see;
the run's own already-published negatives are the corrective signal D-4
recorded from day one and nothing read. Before encoding, a negative's
bounds are clamped per channel toward its reference population — every
completed negative's manifest block on the roll, plus this run's
publishes — into `median ± max(CLAMP_K_MAD × MAD, CLAMP_MIN_WINDOW)`. The
window floor is in log D, wide enough that a stop or two of legitimate
per-frame exposure shift never clamps (percentile bounds are rank-based
and self-normalize exposure only within a frame; across frames, density
shifts are real), tight enough that a latched outlier cannot survive.
Fewer than `CLAMP_MIN_SAMPLES` references clamps nothing; a clamp that
would degenerate a channel is discarded whole. Clamping is recorded
(`clamped`, `unclamped_floors`, `unclamped_ceils`) so a bad window is
auditable, and the published pixels always reflect the bounds recorded in
`floors`/`ceils`.


# 2D grid stitching decisions (protocol version 10)

Implemented per docs/GRID_STITCH_PLAN.md (kept, not deleted — the real-scan
validation is still outstanding); this section is the durable record of the
locked decisions.

## Dims are required; capture order is not trusted

The grid's dimensions (`--grid AxD`) are user-specified because they buy
two things nothing else can supply: a defensible *separable* feather (a
two-axis ramp needs to know which axis is which and how many cells sit on
each — the placed centres alone tell neither), and a structural sanity gate
(the solved centres must form a bijection onto the R×C cells with roughly
uniform pitch, which catches a frame that slid half a cell or more — a
failure mode `global_rms_px` is blind to, since a consistently-wrong
layout can still fit its own pairs well). Capture order is not used — pair
discovery is exhaustive, the solve needs no seed, and trusting import order
would be fragile (rescans, renames, out-of-order picks). Cell assignment is
derived from the solved geometry, never from member order.

## The feather is a separable product of two ramps; axes come from the solved rotations

A strip's feather ramps along one axis; a grid's is the *product* of two
1-D ramps, one along each grid axis, each scaled to [0, 1] and the floor
applied once to the product — so a pixel's crossfade profile across a
vertical seam is the same at the top of the canvas as in the middle, and
likewise for horizontal seams, and a four-way corner (7.3% of a 5×2 canvas
at 1/3 overlap) blends smoothly instead of collapsing to the isotropic
distance transform's 50/50 border. The axes are the frames' solved
rotations (circular mean), not an SVD of the centre cloud: the frames were
stepped along the camera's own sensor axes, so the rotation-derived axes
are exact at any grid shape and cell count, where the SVD is conditional
on a capture geometry nothing checks and yields no cell counts even when
it holds. The SVD is kept as a cross-check only, applied while its
singular values are well separated. Cell assignment snaps each centre to
its nearest declared pitch (not gap-cutting): sub-cell drift keeps a clean
bijection and stays measurable by the alignment check; half a cell or more
snaps into a neighbour and fails the bijection outright. Rejected
alternatives: a single axis fitted by SVD (conditional, no cell counts),
and per-pair midline blend bands (needs overlap geometry the accumulate
pass does not carry).

## The feather's exponent narrows the crossfade to a band around the midline

`FEATHER_EXPONENT` (composite.py, docs/NARROW_FEATHER.md) raises the
separable ramp product to a power before the floor is applied. A real
negative's pairwise registration was measured at 1.3-1.9 px RMS — healthy —
while the *global* solved layout carried 3.7 px RMS against those same
correspondences, because no single rigid-plus-isotropic-scale layout
satisfies every pair at once; that residual is model error, out of this
plan's scope. The wide, full-overlap feather this project shipped with
smears that 3.7 px of residual across whatever fraction of the canvas two
frames overlap — 42.9% of the image more than 10% blended, 8.9% within
0.05 of a straight 50/50 average, on the measured negative — which reads as
soft doubling scattered across arbitrary parts of the picture rather than
as a seam anyone could point at.

A power was chosen over a true overlap-midline band (`docs/
STITCH_QUALITY_PLAN.md` section 1.5's first deferred alternative, and
this plan's whole reason to exist) or a smoothstep/logistic function for
one reason: it is the only pointwise function under which the crossfade
stays *exactly* separable. `w(u) = (1-u)^p / ((1-u)^p + u^p)` keeps
`w(0.5) = 0.5` for every `p` — the seam never moves — and
`(r_x * r_y)^p = r_x^p * r_y^p`, so a two-axis product raised to a power is
still the product of two independently-powered per-axis ramps; a
smoothstep or logistic does not distribute over a product this way, and
recovering the true midline band would need the pair overlap geometry the
accumulate pass declines to carry (the reason section 1.5 rejected it the
first time). The exponent reaches the same place — a band around the
midline — from data every frame already computes alone.

`FEATHER_EXPONENT` is bounded to `[1, 8]`: the floor
(`_FEATHER_FLOOR_FRACTION`) must be applied *before* the power, so the
floored region stays exactly the same set of pixels for every exponent —
and the floored region's weight is then `_FEATHER_FLOOR_FRACTION ** p`,
which starts leaving the normal float32 range past `p = 8`. Raising the
bound needs the floor redesigned first, not just a wider assert.

## Unmeasured constants awaiting a real-scan gate

`GRID_PITCH_RATIO_MIN = 0.6`, `GRID_ALIGNMENT_RATIO_MAX = 0.25` (layout.py),
`_FEATHER_FLOOR_FRACTION = 1e-3` and `FEATHER_EXPONENT = 4` (composite.py)
are unmeasured starting values, recorded in the roll manifest's
`stitch_params` (`grid_pitch_ratio_min`, `grid_alignment_ratio_max`,
`feather_floor_fraction`, `feather_exponent`) and per negative
(`grid_pitch_ratio`/`grid_alignment_ratio`), to be revisited at a user gate
once there are real scans to measure against — the same discipline the
quality gates' constants follow. `GRID_ALIGNMENT_RATIO_MAX` is the looser
guess of the pair and the more likely to need moving.

Joining them: `GEOMETRY_MAX_RELATIVE_SE = 0.25` (geometry_fit.py, the
distortion stability gate of docs/STABILITY_GATE.md, recorded in the
profile's `calibration_report.distortion` as `max_relative_se`). It is
chosen from the synthetic sweep of `scripts/measure-stability-gate.py`
(174% at true zero against 17.0% at a true 15 px on the 4.0 mm board at
the rig's 2.5 px corner noise), but its real-scan gate — the
recalibration against the printed 2 mm target — is still outstanding.

## The memory estimate's frame_bbox_size is a per-frame box

`_attempt_solve` passed the whole canvas to `estimate_peak_bytes` as every
frame's `frame_bbox_size`, charging `frame_count × canvas` where the
compositor allocates `frame_count` frame-sized boxes — a factor of nine at
the 5×2 target workload (198 GB demanded of a 64 GB machine, refusing
every grid above 2×2 before a single frame was warped). It now computes
the real per-frame bounding boxes (via `composite.frame_bbox`, promoted
from a private helper for exactly this use) and passes the per-axis max —
an upper bound on every frame, which is what the `frame_count ×`
multiplier assumes. A 5×2 now fits with 26% headroom. The bug was in the
estimate's *inputs*, not its formula.
# The colour-managed export (protocol version 11)

Implemented per docs/EXPORT_PLAN.md; this section is the durable record of
the locked decisions.

## What the camera colour matrix does and does not do

Every camera sensor sees colour slightly differently. Its red photosite is
not measuring "red"; it is measuring "whatever got through this particular
red filter", and the same is true of green and blue. Point two different
camera bodies at the same negative under the same light and they record
different numbers for the same colour. Neither is wrong — they are just
two different instruments reporting in their own units.

Up to now this program treated those three sensor numbers *as if* they
were already red, green and blue in a known colour space. That was a label
with nothing behind it, and a large part of why exports were awkward in
other software: a program that honours the profile applies an interpretation
to numbers that were never interpreted.

The matrix is the correction, and it comes from the camera itself. LibRaw
ships, for every body it supports, a small measured table describing how
much of each real-world primary each sensor channel actually responded to.
Multiplying the three sensor numbers by that table converts them out of
"what this Nikon's filters recorded" and into a device-independent
description of the colour (CIE XYZ); a second, fixed table converts XYZ
into Adobe RGB's red, green and blue. Composed, the result is a single 3x3
matrix per camera body: sensor RGB in, Adobe RGB out. Nine numbers, one
matrix multiply per pixel.

What it does to a picture: mostly it corrects **saturation and hue**.
Camera filters overlap, so a pure red in the scene leaks a little into the
green channel and comes back reading slightly orange and slightly washed
out; the matrix subtracts that leakage back out. It is a rotation and mild
stretch of the colour cube. It is **not** a brightness, contrast, or
white-balance change — those are the tone curve's and the stitch stage's
per-channel normalization's jobs, and the matrix must not be allowed to
duplicate either.

One thing it deliberately does not do: **move neutrals.** The stitch
stage's per-channel normalization is what removes the orange mask and sets
grey to grey; applied raw, the matrix would tint that carefully
established neutral. So the matrix is **row-normalized** — each row scaled
so that its three entries sum to one — which guarantees that an
equal-parts input `(x, x, x)` comes out as `(x, x, x)`. The matrix then
acts purely on the *difference* between the channels, which is exactly the
sensor-crosstalk part it is qualified to fix, and leaves the part the
normalization already decided alone. `render.export_matrix` asserts this
invariant, and a test holds the line: **do not simplify it back into a
plain matrix multiply** — that would silently re-tint every export.

**The honest caveat, recorded and not quietly dropped.** The matrix
characterizes the camera's response to *light*. The export applies it
after the negative has been per-channel normalized, inverted, and
linearized against the output TRC — not the same quantity the matrix was
measured against. It is a first-order correction, not a colorimetric
characterization of the film: colour negative film's dye densities are not
a colorimetric measurement of the original scene, and no 3x3 matrix makes
them one. What the matrix removes is the part that *is* well defined and
*is* per-camera — the sensor's own crosstalk. Removing it is strictly
better than pretending it is not there, which was the status quo. "It is
approximate" is not an argument for the previous state (an invented
label); it is the reason the correction is kept first-order.

## The rest of protocol 11, one paragraph each

**The export is a rendered positive; the published TIFF is not** (§0.2,
§4.1 of the plan): the export inverts, matrixes and tones, and is written
as a 16-bit lossless JPEG XL tagged with the Adobe RGB (1998)-compatible
export profile (grey companion for a mono roll). The published TIFF stays
exactly what the section above says it is.

**The matrix is recorded data, not a roll invariant** (§3.3): it lives in
the roll manifest's optional top-level `camera_color` block, written by the
stitch stage's first run and frozen thereafter, and never in
`processing_params` — whose exact-dict equality is the
`ROLL_INVARIANT_MISMATCH` check, and which a value no published pixel
depends on has no business breaking. The matrix affects no published
pixel; it is read only at export. A colour roll predating the block fails
the export (`CAMERA_MATRIX_MISSING`) rather than falling back to an
identity matrix — a silent identity would produce a file that claims
Adobe RGB and is not, which is precisely the bug this protocol exists to
remove.

**Adobe RGB over ProPhoto** (§0.1): because the destination is Lightroom,
and the ProPhoto claim was never true of the pixels anyway — so this is
not a gamut reduction from a correct state but the first state that is
honest about the pixels at all. The generated profile is an independent
one with the published colorimetry; see `THIRD_PARTY_NOTICES.md`.

**The tone op stopped being preview-only** (§4.6): the export bakes it,
through the same `tone.curve_values` the preview uses, and `tone.py`,
`previews.py`, and this file were corrected together. The published TIFF
still never carries it.

**There is no `--format` flag** (§6 of the plan): JPEG XL replaces TIFF
outright for the export; a format flag is surface with no chosen use, and
an unused flag is a maintenance cost plus a second set of tests.

## The preview's tone adjustment: the `tone` op (protocol version 10)

The Edit tab's flat preview — decode, `1 - val`, bare 8-bit scaling — is
honest but hard to judge a print by, so the tab offers a nondestructive
tone adjustment: an ISO-R paper grade (50–180; lower is harder, matching
NegPy's print-module vocabulary) plus a midtone snap trim (−0.5…0.5,
NegPy's variable midtone gamma). It is recorded as a `tone` op in the
negative's ops log (`repo.TONE_OP`) and composed into the preview's
display LUT (`tone.py`) — a simplified port of NegPy's H&D print curve:
a straight slope about the midtone pivot with softplus toe/shoulder
knees, endpoints pinned to display black and white.

Three deliberate boundaries:

- **The published TIFF never carries it.** The op is a state, not a
  transform — but it is no longer preview-only: the export's render bakes
  the same curve into the exported pixels at full resolution, through the
  same `tone.curve_values` the preview's LUT is built from (docs/
  EXPORT_PLAN.md §4.6), so preview and export cannot drift apart. What
  remains of the Phase 4 print stage (a print curve distinct from
  grade/snap, soft-proofing, paper simulation) is deferred, and it
  inherits the vocabulary (grade, snap) without inheriting this
  implementation. `roll info` reports the net tone per negative so Swift
  can key its preview caches.
- **The op is a state, not a transform**, so unlike the geometric ops it
  is not append-only: the latest `tone` op wins, and a trailing one is
  coalesced in place (`append_tone_edit`). Slider commits would otherwise
  pile up dozens of dead rows per frame. This is the log's one sanctioned
  exception.
- **The grade's reference slope is not NegPy's.** NegPy's R115 is a real
  paper grade against a paper-white baseline; our baseline is already the
  flat linear mapping of the normalized density, so the slope reference
  (`GRADE_SLOPE_REF`, at R115) is chosen to land the default grade at a
  print-like midtone contrast with the softest end of the range near the
  flat look. The numbers are a judgement aid, not a calibrated paper.

### Protocol version 11: density, zone density, toe/shoulder, auto metering

Seven more controls join grade and snap on the same preview-only `tone` op,
with NegPy's user-facing ranges kept verbatim (same status as the grade
reference — vocabulary, not calibrated paper):

- **Print density** (0.0–2.0, neutral 1.0) offsets the curve's input pivot
  before the grade rotation, so brightness and contrast decouple.
- **Zone density** (shadows ±0.9, highlights ±0.5) applies mid-sparing
  sigmoid offsets on the quarter tones, read on the post-Snap value.
- **Toe / shoulder** (−1…1) and their **widths** (0.1–5.0, neutral 2.5)
  parameterise the softplus knees already in the curve.

The math is re-derived, not transcribed from NegPy: our axis is flipped
(normalized log density → positive display value) and rescaled, so
NegPy's density coordinates cannot be mapped linearly onto ours. Zone
centres are placed by position on our own curve (quarter and
three-quarter tones) rather than by NegPy's absolute density anchors.

Endpoint rescale reads its anchors with every shaping control at rest
(grade and snap only). Without that rule, print density would be nearly
inert and toe/shoulder would be completely inert, because moving the
endpoints is precisely what they do.

**Auto Density** and **Auto Grade** are buttons, not modes: the display LUT
has no image, so a persistent auto mode is not representable. Each press
solves from the negative's recorded `normalization` block (already written
at stitch time) and records the computed value as ordinary op state.
Re-stitching does not re-run auto.

**Negative shoulder sharpening** is a deliberate deviation from NegPy: in
NegPy a negative shoulder is inert because `d_min_eff` clamps at the
paper's physical Dmin; our ceiling is display white with no paper model,
so the same sharpening branch used for negative toe is applied to the
shoulder too.

## The preview's colour adjustment: the `color` op (protocol version 12)

Six controls from NegPy's Colour panel — temperature (a Kelvin lever over
magenta and yellow, derived never stored), global/shadow/highlight CMY,
cast removal, dye separation, and separation damping — land as a second
op (`repo.COLOR_OP`), sibling to `tone`. The same boundaries apply as
tone after the colour-managed export landed: the published TIFF never
carries it; the op is a state coalesced in place; the numbers are
judgement aids, not calibrated colorimetry; and **the export bakes it**
through the same `render_positive_float` the preview uses (after the
camera matrix and Adobe RGB encode on colour rolls).

What is not obvious from the code:

1. **Input domain is NegPy's; output is flipped.** Global CMY and cast
   removal act on normalized log exposure (our TIFF values before the
   `1 - val` flip) and port verbatim. Regional CMY and dye separation act
   on display values and need our 0…1 scale (see COLOR_PLAN §0.2).
2. **Why a second op, not more keys on `tone`.** Tone and colour compose
   in a fixed order inside the display encode; separate ops keep the ops
   log readable and let each panel reset independently.
3. **Cast removal defaults to 0 here, 0.5 in NegPy.** Our normalization
   already defeated the mask NegPy's default assumes; neutral must mean
   no cast correction.
4. **Endpoint rescale is shared across channels.** Without that, per-channel
   cast removal and CMY would self-cancel when endpoints move.
5. **Dye separation is the one control that is not a LUT.** When
   `dye_separation != 1.0` (or damping is non-zero), the preview path
   applies per-pixel spread after the LUT; otherwise three 1-D tables
   suffice.
6. **Cast removal ports the shadow-tie branch only.** We do not measure
   the neutral-axis refs NegPy's other branch needs.

# The spotting feature (protocol version 13)

## Three principles hold the whole design together (docs/SPOTTING_PLAN.md)

1. **The published TIFF is never touched.** Detection and repair are a
   `spots` op in the negative's ops log, exactly like `tone` and `color` —
   except that the export (and the previews) replay it into pixels. Every
   repair is reversible by clearing one op.
2. **Nothing is repaired that the user has not seen.** The detector's
   output is a *proposal*. Pixels change only after an explicit repair
   switch, and only inside masks the user had the chance to reject.
3. **The gates err toward missing crud, never toward eating detail.** A
   missed speck costs a manual spot; a false positive silently destroys
   real information in a scan the user may never re-make. Wherever a
   threshold could go either way, it goes conservative.

## Why the exclusions are exclusions

- **Infrared cleaning (Digital ICE and cousins)** needs a second capture
  under IR illumination, which a copy-stand rig with an unmodified Z f
  cannot make — and it fails on silver-halide black-and-white film by
  construction, so it would not cover this program's monochrome rolls even
  with the hardware.
- **Polarized dark-field capture** is the right escalation if the top-hat
  detector proves too blunt, but it needs a second exposure per frame and
  two polarizers in the rig. Punchlist.
- **Learned inpainting** reconstructs plausible detail rather than
  interpolating measured detail — precisely the failure mode principle 3
  exists to prevent. Not on an archival scan.
- **Multi-frame consensus across a negative's overlapping scan frames
  cannot see emulsion crud**, which is the kind of thing that looks like an
  obvious win to a reader who has not thought it through: crud stuck to the
  emulsion is imaged by *every* frame covering that patch of film, so it
  agrees between frames and is invisible to an outlier test. What consensus
  *would* catch is optical-path defects (sensor dust, a mote on a lens
  element), which move relative to film content between frames — a real
  and separate feature whose natural home is the flat-field reference
  capture, which already photographs the bare light source with no negative
  in the holder.
- **No cross-negative context**: each negative is detected on its own
  evidence.

## The neutrality test, and why it multiplies by the channel spans

Crud is neutral: a speck of dust is a broadband attenuator, the same
additive offset in every channel's log density. But each channel is then
normalized by its own span, so the equal log offset arrives in `val` space
scaled by `1 / (ceil_ch - floor_ch)` — unequal across channels. The gate
therefore compares per-channel residuals *after multiplying each by its
channel's span* (`color.read_metering(...).ranges`), not the raw residuals.
A monochrome roll has one channel and nothing to agree with, so its
threshold runs stricter instead (`MONO_K_BONUS`) — an honest statement, not
a fudge factor, until Chunk S-6 measures it.

## The one way this feature could damage a scan, closed structurally

A re-stitch keeps a negative's id — and therefore its whole ops log — while
solving a fresh layout: the canvas can change size and content moves to new
coordinates. TIFF-space spot geometry recorded against the old canvas would
repair arbitrary parts of the new image. The op therefore records the
`canvas` it was detected against, `apply_repair` and `is_repairing` no-op
on a mismatch, markers report an empty list, `list-spots` warns
`SPOTS_STALE`, and `roll info` marks the summary stale with zeroed counts.
Re-detecting clears the condition by recording a fresh canvas.

## Spots are TIFF space in the ops log, display space on the wire

A spot must survive a later rotation, so the op stores the published TIFF's
own pixel grid. The app, however, draws over a display-space image and does
no coordinate math — so the CLI converts on the way out
(`previews.tiff_rect_to_display`, the exact forward map of the canonical
replay), and rejection is by `id`, never by coordinate. Never the reverse,
never both in the same structure.

## Cast removal and filtration (docs/CAST_REMOVAL_PLAN.md)

Seven decisions, in the order the plan's §7.4 lists them.

**1. CMY is mean-removed, and the mean is removed after the range
division.** Global and regional CMY change hue and never the display's
channel mean — Print Density and the zone controls keep sole ownership of
lightness. The mean is removed *after* the per-channel range division
because the offsets are added to the normalized input and the curve applies
the same slope to every channel near the pivot, so the display shift's
channel mean is proportional to the mean of the post-division values;
zeroing that is what holds lightness. Removing the mean of the *sliders*
before the division would make only the equal-slider case a no-op and
leave a lightness shift on every unequal move — the case that actually
matters. The mean is arithmetic, not Rec.709-weighted: equal weights keep
the three sliders symmetric, which a perceived-lightness weighting would
not.

**2. Already-recorded colour ops render differently, and that is
accepted.** Mean removal changes the meaning of every recorded non-zero
CMY value; recorded numbers are not migrated and the op is not versioned.
Accepted because the `color` op is preview-only — it never reached a
published TIFF or an export — so the blast radius is "some previews look
slightly different, and better".

**3. The tie has two points because one cannot correct an offset.** The
one-point solve rotates each channel's line about the anchor — one degree
of freedom, which corrects a crossover but cannot null a plain per-channel
offset cast (that requires moving the line without rotating it). With a
highlight reference and a non-zero `cast_removal_highlights` the line is
determined by both ends — a genuine affine, gain *and* offset,
`negadoctor`'s `wb_high`/`wb_low` pair in our coordinates. The one-point
branch is kept verbatim as the fallback: at `strength_h = 0` the two-point
formula gives a different number, so the branches are separate by design,
not by limit.

**4. In the two-point branch the anchor is no longer pinned for R and B,
deliberately.** That is the second degree of freedom being used, not a
regression to patch out. Exposure stays anchored because green is never
touched, and green carries 0.7152 of the Rec.709 luma; a correction that
moves R and B is supposed to move the image's colour.

**5. The highlight reference reuses `_same_pixel_color_floor_refs`, not a
mirrored percentile.** Independent per-channel percentiles at the dense end
read a different scene object per channel and mistake coloured highlights
for film cast — exactly why the shared, chroma-gated, same-pixel set
exists. Its `None` is load-bearing: it says the dense end was *not* tied on
measured neutrals, which is precisely when a user-driven highlight tie has
the most to do.

**6. COLOR_PLAN §0.6 is superseded, not extended.** It justified a single
shadow tie by saying the dense end was tied on measured neutrals while the
thin end rested on percentiles, leaving the residual crossover at the
*shadow* end. With the thin end now anchored on the measured film base
(docs/REBATE_ANCHORING.md), the reasoning inverts: the thin end is tied on
a measured base, strictly better than any percentile, while the dense end
still rests on the gated set and its fallback. The highlight tie is the one
doing the work and the shadow tie is the trim. The plan's post-R-1
calibration checks are written for this expectation, and if the shadow tie
still measures larger, this paragraph — not the panel — is wrong.

**7. `TONE_METERING_UNAVAILABLE` is reused for the colour-only conditions
rather than renamed.** COLOR_PLAN §7.2 proposed renaming it to
`METERING_UNAVAILABLE` before it shipped; it has shipped. Renaming a live
contract code costs more than the wart, so the auto-cast and cast-removal
metering absences warn with the historical name.

# The Edit tab's latency decisions (docs/OPTIMIZATION.md)

Three choices from the optimization plan a later reader would otherwise
re-litigate. **OPTIMIZATION.md is authoritative**; this file just makes
them findable.

**The transport stays stdio; a socket buys nothing here.** §0.4. The
newline-delimited JSON pipe, its `LineAssembler`, its drained-both-pipes
session, and its event contract were already built and tested; `serve` only
reverses which pipe carries the conversation. A socket adds port
allocation, an auth story, orphaned-daemon cleanup, and sandbox friction in
a signed app, to buy multiple concurrent clients and reconnect-after-crash
— neither of which one app talking to its own bundled helper needs. XPC was
refused for the same reason plus a second service target and a second
signing story, at the price of abandoning the event contract both sides'
tests are written against.

**Startup is imports, not `fork`.** §0.1. Process creation measures 20 ms;
`import scanny_boy.cli` measured ~0.70 s, nearly all of it pulling the
whole application eagerly (cv2 through `film_base`, scipy through
`calibration`, alembic through `library.db`). This is what made the
cheap stage worth doing first: §1's lazy imports recover a real fraction of
the startup for a few hours of work and no architectural risk, and the same
work shortens the daemon's cold start. After §1 a bare `import
scanny_boy.cli` is ~0.05 s; what remains on render commands is cv2 and
SQLAlchemy, reached through the subcommand modules at dispatch time, and §2
makes even that a once-per-process cost.

**The daemon caches preview-resolution pixels, not full ones.** §3.1. A
decoded negative is 712 MB; an LRU of two is 1.4 GB imposed on a machine
also holding the app's own buffers. The fit view — where every slider
lives — only ever needs the 1024-edge preview cut (~5.4 MB per negative),
and 100% zoom keeps the strip reader, which is already good (§0.3). The
full-resolution single-entry cache stays on the punchlist, gated on
measuring that sitting on one negative at 100% with repair on is the real
inspection workflow. The preview cache is keyed on geometry plus the TIFF's
mtime — tone and colour are <1 ms LUTs applied after it, so a slider drag
must hit it, and does: 38–54 ms per render, served.
