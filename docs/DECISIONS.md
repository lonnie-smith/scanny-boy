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
  `min(shots_per_negative, os.process_cpu_count() or 1, 4)`, where
  `shots_per_negative` is the batch's own value. `--jobs 1` uses a fully
  serial path.
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
- **Blending is a linear feather in linear light, ramped along the strip
  axis only**: per-frame weight is the distance from the nearer end of the
  frame's own extent along the strip's long axis (published on `Layout` as
  `strip_axis`, a unit vector from the same SVD `strip_spread_ratio`
  already computed), constant across the strip, floored so a covered pixel
  always contributes; the output is the weighted average wherever any frame
  contributes weight. A distance transform of the eroded mask (isotropic in
  every direction) is kept only as the fallback when a layout has no
  trustworthy strip axis. The isotropic version was replaced, not merely
  revisited: it made a pixel's crossfade identical near the strip's long
  borders and down its middle, but near those borders the nearest mask edge
  is the border itself, not the seam, so both frames' weights collapsed
  toward 50/50 there regardless of the true seam position, and residual
  misregistration smeared into a curved band that widened toward the edges.
  See the README's "How frames are registered and blended" for the
  reasoning and the alternatives (a hard seam, an overlap-midline band, a
  multi-band Laplacian blend) kept as named, deliberately deferred next
  steps.
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
  never enumerates the library or parses a roll manifest itself.
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
  inventing a write path; see `punchlist.md`.

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

- **Setting the roll capture date or a per-negative date override from the
  app** — see "Sequence and metadata" above and `punchlist.md`.
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

Consequences, all intended:

- **`romm.py` became `linear.py`.** The 65,536-entry LibRaw-curve decode LUT
  is gone; `decode_to_linear`/`encode_from_linear` are plain fixed-point
  scaling and the round trip is exact for every code. This supersedes the
  Phase 2 amendment above ("The colour decode curve"), which locked LibRaw's
  measured curve into these helpers; the curve measurement was correct but
  the curve itself is no longer wanted.
- **A new ICC profile** (`ScannyBoy-Linear-ProPhoto-v1.icc`, generated by
  the same deterministic tool) declares ProPhoto primaries — carried over
  byte-identical from the upstream ProPhoto-v4 source — with a **linear**
  TRC (parametric type 0, g = 1.0): the truth about the pixels. This
  supersedes `ScannyBoy-ROMM-LibRaw-v4.icc`. NegPy reads an input profile's
  primaries only, never its declared TRC, so the boundary behaves as its
  doc assumes.
- **Previews are display-encoded.** The published TIFF is linear, so
  `previews.py` now 16→8-bit encodes through an sRGB LUT (after downscaling
  in linear light) — an untagged 8-bit PNG is assumed sRGB, and SwiftUI
  displays it as-is. The TIFF is never touched.
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

# Normalization decisions (protocol version 8)

Scan normalization ("Convert") is implemented per docs/NORMALIZATION_PLAN.md
(deleted after landing, per the repo's convention); this section is the
durable record of its locked decisions.

## The published TIFF is a baked, normalized working intermediate

The deliverable is a positive export that does not exist yet; the published
TIFF is the working intermediate the creative-edit stage reads. After
deciding that, everything else in this section follows.

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
  (dark), `val = 1` the scene shadow (light). Inversion is the print
  stage's (Phase 4). The preview displays `1 - val` purely so the Edit
  filmstrip is legible — the file in Photoshop looks like a negative and
  the preview beside it looks positive. Both are correct.

## Colour negative only

NegPy's `ProcessMode` is not ported: no E-6 branch, no swapped percentiles,
no fixed-range fallback, no dead flag. This rig photographs colour
negative film under white light; if transparency support is ever wanted it
is a new feature with its own plan.

## Two ICC profiles, and the profile is never load-bearing (D-2)

The prepare stage's intermediates stay linear; the published TIFF does not.
One profile used to assert both; the split is now explicit:

| | Intermediates (prepare) | Published TIFF (stitch) |
| --- | --- | --- |
| Profile | `ScannyBoy-Linear-ProPhoto-v1.icc` | `ScannyBoy-Density-ProPhoto-v1.icc` |
| TRC | parametric type 0, g = 1.0 | parametric type 0, **g = 2.2** |
| Claim | true — the pixels are linear | a **viewing convention** |

g = 2.2 is deliberately not accurate: a normalized log encoding over ~2
decades is closer to gamma 3.3, and no ICC parametric type expresses it. A
*correct* profile would decode the file back to un-normalized linear —
undoing the one thing normalization does. The tag's only job is legibility
in external viewers while debugging the edit stage.

**The load-bearing rule:** the profile must never become load-bearing for
the render. Every internal consumer decodes through
`normalization.decode_normalized`, never through an ICC transform; a
grep-shaped guard test keeps the loader out of everything but the write
path. `RollInvariants` grew `published_icc_profile_sha256` beside the
intermediates' hash, and `check_roll_invariants` compares both.

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
the same way it derives the turns.

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
(P0.1 luma), border-touching components gated on area (both ways — too
small is not a stripe, too large is scene content), thinness (a stripe's
bounding box is thin perpendicular to its border; scene content spans the
frame), flatness *along* its length (contamination is featureless along
the border; edge fog fades across its thickness, so the test runs on the
along-length medians), and separation from the scene's own dense tail (the
gate that makes "no stripe at all" return cleanly). The detector
re-anchors up to `DENSE_BORDER_MAX_PASSES`: a gradient stripe is eaten
band by band, converging when the residue reaches scene density. The
documented false positive is a genuinely dense, thin, featureless
border-touching scene object: scene highlights map slightly brighter —
mild degradation, never invented data. Withheld fractions are recorded per
negative (`normalization.dense_border`).

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
layout can still fit its own pairs well). Capture order buys little — pair
discovery is exhaustive and the solve needs no seed — and it is fragile: a
rescan, a rename, or an out-of-order pick would feed a wrong cell map into
the feather. So serpentine order is a *documented assumption used only for
one warning* (`STITCH_GRID_ORDER_UNEXPECTED`): the solved geometry always
wins, and the warning never changes behaviour. Cell assignment is derived
from the solved geometry, never from member order.

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

## Unmeasured constants awaiting a real-scan gate

`GRID_PITCH_RATIO_MIN = 0.6`, `GRID_ALIGNMENT_RATIO_MAX = 0.25` (layout.py)
and `_FEATHER_FLOOR_FRACTION = 1e-3` (composite.py) are unmeasured starting
values, recorded in the roll manifest's `stitch_params`
(`grid_pitch_ratio_min`, `grid_alignment_ratio_max`,
`feather_floor_fraction`) and per negative
(`grid_pitch_ratio`/`grid_alignment_ratio`), to be revisited at a user gate
once there are real scans to measure against — the same discipline the
quality gates' constants follow. `GRID_ALIGNMENT_RATIO_MAX` is the looser
guess of the pair and the more likely to need moving.

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
## The preview's tone adjustment: a preview-only `tone` op (protocol version 10)

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

- **The published TIFF never carries it.** The op is a state the display
  encode consumes, not a transform: the exporter's replay ignores it, and
  the Phase 4 print stage — which will own pixel-level tone at export —
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
