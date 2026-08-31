# Architecture

An orientation map for someone — human or agent — who needs to work on this
code without reading all of it first.

**Status of this file.** It describes what the code *does*, verified against
the code at the time of writing, not what the plans say it should do. Where
the two differ, this file says so explicitly and names the code. The
authority chain for *decisions* is unchanged:
[`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) /
[`PHASE2_IMPLEMENTATION_PLAN.md`](PHASE2_IMPLEMENTATION_PLAN.md) /
[`PHASE3_IMPLEMENTATION_PLAN.md`](PHASE3_IMPLEMENTATION_PLAN.md) section 3 >
[`DECISIONS.md`](DECISIONS.md) > this file. But for *"what does the program
actually do today"*, the code is the source of truth and this file is a
summary of it. `DECISIONS.md` is organised by phase and includes decisions
that were later amended; this file is organised by subsystem and describes
only the current state.

---

## 1. What the program is

Turn a strip of 35mm film negatives, scanned frame-by-frame on a Nikon Z f as
overlapping RAW captures, into one stitched 16-bit RGB TIFF per negative,
collected in a durable named folder called a **roll**.

Three vocabulary terms carry the whole design:

| Term | Meaning |
| --- | --- |
| **frame** | One `.NEF` capture. Several frames cover one physical negative. |
| **negative** | One physical film frame = one group of N consecutive frames = one published TIFF. |
| **roll** | A named folder holding many negatives, added to across many runs over time. |

`shots_per_negative` (1–12, typically 3) is fixed per roll at creation.

---

## 2. Repository shape

```
cli/src/scanny_boy/     Python. ALL logic lives here.
mac/ScannyBoy/          SwiftUI app. Interface only.
shared/contract/        The interface between them (CONTRACT.md + 3 JSON Schemas).
scripts/                bootstrap.sh, build-cli.sh, measure-*.py
docs/                   Plans, DECISIONS.md, punchlist.md, this file.
tests/fixtures/nef/     Real sample NEFs — gitignored, tests skip without them.
```

Tests are **co-located** with the code they test: `pipeline.py` /
`pipeline_test.py`, in `cli/src/scanny_boy/`. `pytest` `testpaths = ["src"]`.
Swift tests are in `mac/ScannyBoyTests/`.

`mac/ScannyBoy.xcodeproj` is generated from `mac/project.yml` by XcodeGen and
is **never committed**. `mac/ScannyBoy/Helpers/ScannyBoyCLI.app` is a staged
PyInstaller build product, also never committed — and `xcodegen generate`
*fails* without it, because `project.yml` names it in a copy-files phase. So
the clean-clone order is always: `scripts/bootstrap.sh` →
`scripts/build-cli.sh` → `cd mac && xcodegen generate`.

Checks (identical to CI in `.github/workflows/ci.yml`):

```bash
cd cli && uv run ruff check . && uv run pytest
```

```bash
cd mac && xcodebuild test -scheme ScannyBoy -destination 'platform=macOS'
```

---

## 3. The central architectural rule

**Python owns every decision. Swift owns no logic at all.**

Swift never sorts files, never groups negatives, never judges an output
folder, never parses a manifest, never enumerates the library. Everything it
displays comes back from a CLI call. This is not stylistic — it is the
constraint that keeps validation from drifting between two implementations,
and it is enforced by convention in review, not by a compiler. If you find
yourself about to add a `sort`, a `filter`-that-decides, or a JSON decode of
`scanny-boy-roll.json` in Swift, that is the wrong place.

The two legal ways Swift learns anything:

- `probe` (catalogue, selection validation, grouping, roll overlap)
- `roll list` / `roll info` (the library and one roll's manifest)

The CLI is a **subprocess** emitting one JSON object per line on stdout.
stderr is human logs and is never parsed. See
[`shared/contract/CONTRACT.md`](../shared/contract/CONTRACT.md) — it is the
source of truth for args and event shape, with
`shared/contract/schema.json` as the authoritative JSON Schema for one event
line.

`PROTOCOL_VERSION` is **3** ([`events.py`](../cli/src/scanny_boy/events.py)).
A client that only understands 2 must reject the stream rather than guess.

---

## 4. The CLI's command surface

```
roll init   --library DIR --name NAME --per-negative N
roll list   --library DIR
roll info   --roll DIR
roll rename --roll DIR --name NAME

probe   --input DIR [--files ...] [--per-negative N] [--out DIR] [--roll DIR]
convert --input DIR --files ... --out DIR [--per-negative N] [--jobs N] [--overwrite]
stitch  --work DIR --roll DIR [--jobs N] [--overwrite] [--allow-partial] [--negatives ID ...]
run     --input DIR --files ... --roll DIR [--per-negative N] [--jobs N]
        [--work DIR] [--skip-sources FILE ...]
apply-metadata --roll DIR
```

[`cli.py`](../cli/src/scanny_boy/cli.py) is pure argparse plumbing plus
event bracketing (`Started` … `Finished`). Every subcommand has the same
shape: emit `started`, call one pipeline function, translate its exception
type into an `error` event and an exit status, emit `finished`. Exit codes:
`0` success, `1` validation/partial failure, `2` usage, `143` cancellation
(128 + SIGTERM).

`run` is what the app's Run button drives: it calls `run_convert`
**in-process** and then `run_stitch`, one process, one event stream, one
cancellation. It never spawns a subprocess of itself.

`convert` and `stitch` remain independently usable; `convert` is the only
command that still writes to a plain `--out` work directory rather than a
roll. `stitch --overwrite` is accepted and **deliberately ignored** — a roll
is additive and a stitch never replaces a published file
([`stitch_pipeline.py`](../cli/src/scanny_boy/stitch_pipeline.py),
`run_stitch` docstring).

---

## 5. Module map (Python)

Read in this order if you are new; the dependency direction runs roughly top
to bottom.

**Foundation**
| Module | Role |
| --- | --- |
| `events.py` | The whole protocol: `EventType`, `Code`, `PipelineStep`, `Stage`, typed dataclass events, `EventWriter`. Every stable error/warning code is an enum member here. |
| `cancellation.py` | `CancellationToken` (a `threading.Event`) + the SIGTERM handler. The handler does exactly one thing: set the flag. |
| `hashing.py`, `disk_check.py`, `concurrency.py` | SHA-256 streaming; the free-space formula; worker-count and memory-budget policy. |
| `icc_profile.py` | Loads and SHA-256-verifies the bundled ICC profile before **every** use. |
| `romm.py` | LibRaw's transfer curve, as a 65,536-entry `float32` decode LUT. |

**Input side**
| Module | Role |
| --- | --- |
| `catalogue.py` | Discover `.nef` (case-insensitive, no recursion), read capture timestamps, compute canonical order. |
| `selection.py` | Pure functions: order a selection, check contiguity, chunk into groups. |
| `metadata.py` | Read EXIF settings, white balance, and the "digitized" source fields from a NEF. |
| `consistency.py` | Validate that a selection shares exposure/aperture/ISO/focal length/orientation/WB/lens. Operates on `SourceSettings`, so it is testable without real NEFs. |
| `raw_decode.py` | `RAW_PARAMS` and the rawpy calls. |

**Output side**
| Module | Role |
| --- | --- |
| `tiff_writer.py` | Pass 1: base TIFF via `tifffile`. |
| `tiff_exif.py` | Pass 2: nested EXIF directory via `tifftools`, addressed by numeric tag code. |
| `stitched_tiff.py` | The stitched variant of the same two-pass write. |
| `manifest.py` | `scanny-boy-manifest.json` (work directory, format version 1). |
| `roll_manifest.py` | `scanny-boy-roll.json` (roll folder, format version 2). |
| `roll_folder.py` | The library: slugging, collision suffixes, create, rename, one-level scan. |
| `roll_sequence.py` | A roll's display order and rank-based applied timestamps. Pure functions. |
| `output_folder.py` | Folder validation, rerun planning, recovery cleanup — parameterised over which manifest kind it reads. |

**Stitching**
| Module | Role |
| --- | --- |
| `detection.py` | Build the small 8-bit greyscale detection image (downscale, percentile-normalise, optional CLAHE). |
| `registration.py` | Feature detect, match, RANSAC, the rigid fit, the per-pair gates. |
| `layout.py` | The global least-squares solve, connectivity check, canvas size, valid rect. |
| `composite.py` | Warp, feather-blend in linear light, overlap MAD, encode. |

**Orchestration**
| Module | Role |
| --- | --- |
| `probe.py` | `probe`'s three levels of detail. |
| `pipeline.py` | `run_convert`: the group-by-group conversion pipeline. |
| `stitch_pipeline.py` | `run_stitch`: solve everything, then composite and publish per negative. |
| `run_pipeline.py` | `run_full`: convert then stitch, with combined progress and supersession. |
| `apply_metadata.py` | `apply-metadata`: rewrite EXIF dates in published TIFFs. |

---

## 6. Data flow, end to end

### 6.1 `run` (the app's normal path)

```
run --input IN --files ... --roll ROLL
  │
  ├─ run_full (run_pipeline.py)
  │    ├─ work dir = ROLL/.work/<run_id>/   (created here; ALWAYS removed at the end)
  │    ├─ files -= skip_sources             (BEFORE grouping)
  │    │
  │    ├─ run_convert (pipeline.py) ────────────────── stage "convert"
  │    │    validate selection → consistency → hash sources → disk check
  │    │    → write `running` work manifest
  │    │    → for each group: stage every frame, then publish the group atomically
  │    │        per frame: decode → base TIFF → nested EXIF   (3 progress steps)
  │    │    → work dir now holds one intermediate TIFF per frame + manifest
  │    │
  │    ├─ run_stitch (stitch_pipeline.py) ──────────── stage "stitch"
  │    │    verify every intermediate's size + SHA-256
  │    │    → check roll invariants, append this run to the roll manifest
  │    │    → SOLVE every negative's layout first (canvas sizes needed for disk check)
  │    │    → disk check on the roll's volume
  │    │    → for each negative: composite → gate on overlap MAD → stage → publish
  │    │
  │    ├─ _supersede_this_run: mark covered negatives superseded, delete their TIFFs
  │    └─ rmtree(work dir)
  │
  └─ finished
```

**Why layouts are all solved before compositing:** the section-3.8 free-space
formula needs `canvas_width × canvas_height`, and a canvas does not exist
until its layout is solved. Solving is cheap and allocates nothing
canvas-sized, so the disk guard still runs before anything large happens.
This is a deliberate deviation from the plan's step ordering, documented in
`stitch_pipeline.py`'s module docstring.

### 6.2 Progress accounting

One `progress` event stream spans both stages of a `run`. Conversion counts
real steps (3 per frame). The stitch stage's own step count is **rescaled**
into a time-weighted share (`STITCH_UNITS_PER_FRAME = 2`,
`STITCH_UNITS_PER_NEGATIVE = 9`, derived from measured wall-clock in the
Phase 2 plan) by `_wrap_emit_for_stitch` in `run_pipeline.py`, so `completed`
advances monotonically across one combined `total`. The UI derives progress
from `completed`/`total` **only** — never from `source_index`, which with
`--jobs > 1` names "one frame in flight", not a queue position.

---

## 7. Colour: the one thing most likely to be got wrong

RAW decode uses `rawpy` with `gamma=(1.8, 16)`, which is **not** the ROMM /
ProPhoto transfer curve. It is LibRaw's own generalised curve: same gamma and
toe slope, different breakpoints, plus an offset term ROMM does not have.
Measured error against true ROMM: 3.1% of linear light on average.

Consequences, all live in the code today:

- [`romm.py`](../cli/src/scanny_boy/romm.py) implements **LibRaw's** curve,
  not the registry's. Do not substitute ROMM's `0.03125` / `0.001953125`
  breakpoints.
- Decode/encode go through `DECODE_LUT`, a 65,536-entry `float32` table —
  never per-pixel `numpy.power`.
- The embedded profile is `ScannyBoy-ROMM-LibRaw-v4.icc`, generated to
  declare LibRaw's curve. This **fixed** the Phase 1/2 profile-vs-pixels
  mismatch (see the struck-through first item of
  [`punchlist.md`](punchlist.md)). No pixel values changed; every TIFF's bytes
  and hash did.
- `icc_profile.py` verifies the profile's SHA-256 on every load, and
  `tiff_writer.write_base_tiff` refuses to write a TIFF with an empty
  profile. An untagged ROMM file is never produced.
- All geometric and photometric work happens in **linear light**: decode to
  linear `float32`, warp, blend, then encode back to `uint16` exactly once.

`RAW_PARAMS` ([`raw_decode.py`](../cli/src/scanny_boy/raw_decode.py)) is
locked and every value was independently verified to matter — in particular
`no_auto_bright=True` and `adjust_maximum_thr=0.0`, which are what keep pixel
scaling identical across a negative's frames. Changing it invalidates every
roll's `processing_params` invariant.

**TIFF format**, identical for frames and stitched output: three-channel
`uint16`, ROMM with the embedded profile, `Orientation` always `1` (pixels
are already upright — never copy the source value), Deflate with horizontal
prediction (compression code `32946`, not `8`), written in two passes
(`tifffile` for the base, `tifftools` for the nested EXIF, base removed only
after the final file verifies). The four non-obvious `tifffile` rules
(`metadata=None`, `description=`/`software=` as keywords, `iccprofile=` as a
keyword, the Adobe Deflate code) are each documented in place and each
matters.

---

## 8. Registration and stitching

**The model.** A negative's frames form a one-dimensional strip, but **capture
order is never assumed to be spatial order.** Every pair of a negative's
frames is matched (O(n²), trivial at n≤12), and a global layout is solved from
whichever pairs actually overlap. Neighbour-chaining and order detection are
both rejected — the global solve makes order irrelevant for free.

**The geometry is rigid: rotation + translation, scale fixed at exactly 1.**
`cv2.estimateAffinePartial2D` is used only for the RANSAC inlier mask and to
*measure* scale drift; the transform actually used is always re-fitted with
closed-form Umeyama with scale forced to 1
(`registration.rigid_from_correspondences`). Never an affine, never a
homography.

**The solve** ([`layout.py`](../cli/src/scanny_boy/layout.py)) is two linear
least-squares problems, not a bundle adjustment. Frame *i* maps `p → R(θᵢ)p +
tᵢ`; a pair gives `θ_b = θ_a + φ_ab` and `t_b = t_a + R(θ_a)·u_ab`. Rotations
solve first (linear in the scalar θs), translations second (linear once θ is
known). **This is why SciPy is forbidden as a dependency.** Do not replace it
with a nonlinear optimiser.

**Blending** is a linear feather in linear light: each frame's weight is a
distance transform of its own eroded validity mask, and the output is the
weighted average wherever any frame contributes. This is safe *because*
exposure and white balance are locked across a roll — there is no exposure
mismatch to hide, only misregistration, which a feather tolerates gracefully.
It is deliberate but **provisional**; a hard midline seam (preserves grain,
shows misregistration as a line) and a multi-band Laplacian blend (hides
misalignment, softens grain, much heavier) were both considered and set
aside.

Warp details that are load-bearing: `INTER_LANCZOS4` on `float32`, clamped to
`>= 0` immediately after (measured −0.088 undershoot); each frame warps into
its **own bounding box**, not the full canvas; the mask warps
`INTER_NEAREST` and erodes by `MASK_ERODE_PX = 5` with an **elliptical**
kernel (a repeated square kernel erodes by Chebyshev distance and
under-erodes exactly the diagonal edges a rotated frame has); `cv2.erode`
uses `BORDER_CONSTANT/0` so a frame's own corners actually erode.

Uncovered pixels get `FILL_COLOR`, currently black, recorded in the roll
manifest so a file is interpretable without knowing which build wrote it.

### 8.1 Quality gates — where the numbers live

Every threshold is defined in **exactly one module** and read from nowhere
else. Production code must never re-declare one.

| Constant | Module | Value |
| --- | --- | --- |
| `DETECTION_LONG_EDGE`, `USE_CLAHE` | `detection.py` | `2000`, `False` |
| `DETECTOR`, `RATIO_TEST`, `RANSAC_REPROJ_PX` | `registration.py` | `AKAZE`, `0.75`, `3.0` |
| `MIN_PAIR_INLIERS`, `MIN_PAIR_INLIER_RATIO`, `MAX_PAIR_RMS_PX` | `registration.py` | `40`, `0.25`, `6.0` |
| `SCALE_DRIFT_WARN`, `SCALE_DRIFT_FAIL` | `registration.py` | `0.005`, `0.01` |
| `MAX_GLOBAL_RMS_PX`, `STRIP_SPREAD_RATIO` | `layout.py` | `12.0`, `0.15` |
| `MAX_OVERLAP_MAD`, `MASK_ERODE_PX`, `MEMORY_SAFETY_FACTOR` | `composite.py` | `0.20`, `5`, `3.5` |
| `MAX_CANVAS_DIMENSION`, `MAX_STITCHED_BYTES` | `composite.py` | `30_000` (warn), `3.5 GiB` (fail) |

All were measured from real scans and approved at "user gate C". Pixel
thresholds are **full-resolution** pixels — points are converted out of
detection space with `detection.to_full_resolution` before RANSAC.

`stitch_pipeline._stitch_params()` serialises this whole table into the roll
manifest, so a roll records every threshold that was in force when it was
built — and because `stitch_params` is a roll invariant, changing any
constant here will make existing rolls reject new runs with
`ROLL_INVARIANT_MISMATCH`. That is intended, but know it before you tune.

**Overlap MAD is the honest gate.** Inlier counts and reprojection residuals
measure whether the solver was pleased with itself; overlap MAD measures
whether the pixels actually line up. It can only be computed once both frames
are warped, so it is checked *after* compositing, in `_composite_and_publish`
— a negative can therefore fail late, having done all the expensive work.

**`rebate_deviation_px` is specified, recorded, and never implemented.** The
field exists in the contract and the manifest and is always `null`. Chunk P2-1
found a generic straight-edge finder cannot reliably find the same physical
rebate edge across frames. `layout.py` deliberately does not define a
`REBATE_DEVIATION_WARN`. A purpose-built detector is on the punchlist.

### 8.2 The CLAHE fallback (newest feature — not in `DECISIONS.md`)

`USE_CLAHE` is `False`, so the first registration pass runs on the plain
detection image. If that pass fails a negative with
`STITCH_UNDERCONSTRAINED` or `STITCH_RESIDUAL_TOO_HIGH`
(`_CLAHE_RETRY_CODES`), `_solve_negative` retries **once** with
`clahe=True` (`clipLimit=2.0`, `tileGridSize=(8,8)`), emitting the warning
`STITCH_CLAHE_FALLBACK_USED`.

Two details that matter if you touch this:

- The retry spends **no** progress budget (`progress=None` on the second
  `_attempt_solve`), so a negative needing the retry cannot overrun the run's
  declared step total.
- `STITCH_OUTPUT_TOO_LARGE` and `INSUFFICIENT_MEMORY` are deliberately **not**
  retryable: a sharper detection image does not shrink a canvas.

Recorded per negative as `used_clahe_fallback`; the roll-level policy is
recorded as `clahe_fallback_enabled` in `stitch_params`.

---

## 9. The roll: additive, never in place

This is the Phase 3 break, and the thing most likely to surprise you if you
carry Phase 2 intuitions.

- One library folder (`~/Pictures/Scanny Boy` by default, relocatable in
  Settings) holds every roll as a **direct child**. The filesystem is the only
  source of truth — no index, no registry. `roll list` scans one level deep
  for `scanny-boy-roll.json`.
- `roll_id` is a UUID and never appears in a path. `roll_name` is free text;
  the folder name is a slug of it (NFC, `[A-Za-z0-9._-]`, whitespace runs →
  single `-`, 60 chars, case-insensitive collision suffixes). Rename moves the
  folder **first**, then writes `roll_name` — so a failed move leaves both
  untouched. Delete is `NSWorkspace.recycle` in Swift, with no CLI
  involvement at all.
- **Roll invariants** (`RollInvariants`): `shots_per_negative`,
  `processing_params`, the ICC profile hash, `stitch_params`. Everything else
  — input folder, source list, order, grouping — is *expected* to differ
  between runs and is **never compared**. A roll with no runs yet is unseeded:
  the last three are established by the first run.
- **Replacement is additive, never in place.** A run may include sources
  already in the roll. The new negative publishes under its own identity;
  once it completes, every existing non-superseded negative whose members are
  a **subset** of the new one's is superseded — `superseded_by` set,
  `sequence` cleared, its TIFF deleted. The manifest is written *before* any
  file is touched, so a crash leaves an orphan file rather than a dangling
  record. Superseded negatives are never removed from the manifest and their
  output names stay claimed forever, so a name is never reissued.
  (`roll_manifest.mark_superseded` + `run_pipeline._supersede_this_run`.)
  The subset test means an exact rescan supersedes its predecessor and a
  merge-regrouping supersedes its parts; a *split* regrouping supersedes
  nothing.
- `negative_id` is `<run.short_id>-negative-NN`, where `short_id` is the
  first 6 hex chars of the run UUID, lengthening to 8, 10, then the whole
  UUID on collision within the roll. Assigned once by `append_run` and never
  recomputed, so ids are stable for the life of the roll.
- Output names: the stem of the group's first member in canonical order, plus
  `.tif`, with `-2`, `-3`, … on collision across runs.
  `roll_manifest.allocate_output_name` is the **only** place a published name
  is chosen.
- `sources` are keyed by **SHA-256**, so a renamed rescan of the same bytes is
  recognised as the same source and keeps the `run_id` that first contributed
  it.

There is **no migration** from the Phase 2 (`manifest_format_version: 1`) roll
manifest. A v1 folder is not importable and is reported
`ROLL_MANIFEST_UNSUPPORTED`.

### 9.1 Sequence and metadata

- A roll's negatives are ordered by the **real capture time** of each
  negative's first member, across every run, ascending. Ties break by run
  index then first filename. Superseded negatives are excluded entirely
  (`sequence: null`), so a replacement takes its predecessor's position rather
  than shifting later negatives.
- `sequence` is recomputed on **every** `write_roll_manifest` call — the
  manifest writer mutates the manifest it is given. `roll_sequence.py` is the
  only computation of it.
- The applied timestamp is **rank-based**: `12:00:00 + (rank − 1)` seconds on
  the roll's capture date, or a negative's own date override, ranked within
  that date's negatives.
- **Intent lives in the manifest; the TIFF is the artefact.** A negative is
  *dirty* when `intended_datetime_original ≠ applied_datetime_original`.
  `apply-metadata` handles every dirty, completed, non-superseded negative:
  verify the published TIFF against the manifest's recorded size and hash
  (skip with `OUTPUT_MODIFIED_EXTERNALLY` rather than rewrite a file the roll
  no longer recognises), rewrite only the nested EXIF
  `DateTimeOriginal`/`SubSecTimeOriginal` via `tifftools` into a sibling temp
  file, verify the temp reads back correctly, rename over the original,
  re-hash, update the manifest. **No pixel data is ever read or written.**
- A re-stitch of a negative that already had metadata applied re-applies it
  automatically, without asking (`_maybe_reapply_metadata`), as the last step
  before the manifest write. A failed re-apply leaves the negative `completed`
  but dirty — recoverable with Apply — and never fails the stitch.

---

## 10. Failure, cancellation, cleanup

The same rule at both stages: **the unit fails alone.**

- A group that fails conversion has its whole staging directory deleted; the
  next group continues; the run ends `partial`. Nothing is ever published
  half a group.
- A negative that cannot be stitched is recorded `failed` in the roll
  manifest, reported via `negative_failed`, and the run continues.
- A **cancelled** unit is *abandoned, not failed* — no `group_failed` /
  `negative_failed` event. A rerun will simply do it again.

Cancellation is cooperative via SIGTERM. The signal handler sets a flag and
does nothing else; every deletion, manifest update, and final event happens
later on the main thread through ordinary control flow. Workers check the flag
only at step boundaries, never mid-decode. `_stage_group`'s `finally` shuts
the pool down with `wait=True, cancel_futures=True` — queued frames dropped,
running frames allowed to finish, and only once every worker has stopped is
the staging directory deleted. ("Never delete a directory while a worker may
still write to it.") A forced kill after the grace period can leave a
`running` manifest and an orphaned staging directory; the next run detects and
cleans that up (`output_folder.plan_rerun` + `apply_recovery_cleanup`).

**Work directories.** A work directory the run created itself
(`<roll>/.work/<run_id>/`) is removed on **every** outcome — success, failure,
and cancellation — because a rerun regenerates it. A directory the caller
named with `--work` is *never* deleted by cleanup, because deleting a folder
the user pointed at is not this program's decision.

> Note: `DECISIONS.md` describes the default work directory as "a fresh
> temporary directory". It is now `<roll>/.work/<run_id>/`
> (`run_pipeline.run_full`).

**Errors are typed exceptions carrying a stable `Code`**, translated at the
`cli.py` boundary: `ConvertFailure` (convert), `StitchError` (stitch/
registration/layout/composite), `RunFailure` (run, unifying both),
`ProbeFailure`, `ApplyMetadataFailure`, `RollFolderError`,
`OutputFolderError`, `BadManifestError`, `RollManifestUnsupportedError`,
`RollInvariantMismatchError`, `MemoryBudgetError`, `DiskCheckError`,
`IccProfileError`, `CancelledError`. **The `code` is the machine interface;
message text is not.** `stitch_pipeline._friendly_failure_message` deliberately
rewrites technical messages into user-facing sentences precisely because
nothing keys off them.

---

## 11. Concurrency, memory, disk

- `ThreadPoolExecutor` for RAW work — rawpy's LibRaw build releases the GIL.
  Default workers `min(shots_per_negative, os.process_cpu_count() or 1, 4)`;
  the cap of 4 exists because neither CPU-count API distinguishes P-cores from
  E-cores on Apple silicon. `--jobs 1` uses a fully serial path that never
  constructs an executor.
- **640 MiB per worker**, and the total must not exceed half of physical RAM.
  The computed default is silently *reduced* to fit; an explicit `--jobs` that
  exceeds it is *rejected* with `INSUFFICIENT_MEMORY`, because the user asked
  for a specific number. The measurement table justifying 640 MiB is in
  `concurrency.py`; re-measure with `scripts/measure-concurrency.py`.
- Parallelism **never spans negatives** — a negative is published all at once
  or not at all. In the stitch stage `--jobs` bounds feature detection only;
  compositing is one negative at a time, single-threaded through the
  accumulator.
- **Composite peak memory** is estimated before any allocation and multiplied
  by `MEMORY_SAFETY_FACTOR = 3.5`. That factor is measured, not padding:
  NumPy does not return freed arenas to the OS, so resident memory tracks the
  *sum* of successive allocation phases rather than their peak, and real
  three-frame stitches measured 2.5–3.4× a naive estimate. Re-measure with
  `scripts/measure-registration.py` whenever the composite's allocation
  pattern changes.
- **Disk** is estimated conservatively (compression assumed to save nothing,
  20% margin) and checked per volume. For `run`, the work directory and the
  roll may be on different volumes; each is checked separately against its own
  formula, never summed.

---

## 12. Manifests

Both use identical discipline: write to a temp file, `fsync`, rename into
place, then `fsync` the directory — so a reader never sees a half-written
manifest. Both validate **structurally by hand**, not against the JSON Schema
file, because *the packaged CLI must never load a file outside
`cli/src/scanny_boy/` at runtime.* The schema files in `shared/contract/` are
authoritative for the shape and are read **only by tests**
(`manifest_schema_test_support.py`, `roll_manifest_schema_test_support.py`,
`schema_test_support.py`). If you add a field, you must update the dataclass,
`to_dict`, the hand-written validator, the `_from_dict` reader, and the schema
file.

| | `scanny-boy-manifest.json` | `scanny-boy-roll.json` |
| --- | --- | --- |
| Lives in | the work directory | the roll folder |
| Format version | 1 | 2 |
| Scope | one `convert` run | many runs, forever |
| Records | sources + hashes, canonical order, groups, expected/completed outputs + hashes, `processing_params` | roll identity, invariants, every run, every source by hash, every negative's members / layout / all quality metrics / canvas / valid rect / fill colour / capture times / output hash |

The work manifest still carries a `film_date` field, now filled with the
calendar date of the selection's first *real* capture time. It is vestigial —
kept only because the schema has it and a rerun comparison still checks it.
`--film-date` is gone from every command.

The **valid rectangle** is computed and recorded but never applied. The canvas
is always the full union bounding box; nothing captured is discarded. The
valid rect exists for a future crop tool.

`output_folder.py` is parameterised over which manifest kind it reads via
`FolderRules` (`CONVERT_RULES` / `ROLL_RULES`) rather than being duplicated.
Under `ROLL_RULES` a published negative is neither a conflict nor a stale
output — a nonempty roll folder is normal, not `OUTPUT_NOT_EMPTY`. Only
recovery cleanup of never-finished negatives still applies.

---

## 13. The Swift app

One window, `NavigationSplitView`:

```
RootView                    resolves the CLI helper once; shows why not if it can't
└─ ContentView              sidebar + workspace
   ├─ RollSidebar           every roll from one `roll list` call
   └─ workspace (per roll)
      ├─ Add Scans          input folder → contiguous selection → Run
      └─ EditStageView      negatives in sequence, thumbnails, metrics, Apply
```

One active run **app-wide** disables the sidebar, the tab picker, and both
stages' controls.

**Models** (all `@MainActor @Observable`):

| Type | Role |
| --- | --- |
| `RollLibrary` | The library. Its only direct filesystem touch is `NSWorkspace.recycle` for delete; create/rename/list all go through the CLI. |
| `ConfigurationModel` | Add Scans state. Every rule beyond UI bookkeeping is read back from `probe --roll`. `perNegative` is the roll's own, read-only. |
| `EditModel` | Edit tab state, from `roll info`. Derives `visibleNegatives`, `dirtyNegatives`, `applyCommand`. |
| `RunModel` | **One shared model** drives Run, re-stitch, *and* Apply — not three parallel mechanisms. |

**CLI bridge** (`CLIBridge/`): `CLILocator` finds the helper
(`Contents/Helpers/ScannyBoyCLI.app`; a Debug build additionally honours an
**absolute** `SCANNY_BOY_CLI` override; a Release build never falls back to
the repo). `CLICommand` builds argument arrays. `CLISession` is an actor
owning one invocation. `LineAssembler` + `CLIEvent` turn bytes into typed
events.

Two hard-won details in `CLISession` worth not undoing:

- It uses `read(2)` directly rather than `FileHandle.read(upToCount:)`, which
  is not a streaming read — it blocks until it has the full count or EOF, so a
  long conversion would deliver nothing until 64 KiB piled up.
- It signals by **pid** and tracks termination itself rather than consulting
  `Process.isRunning`, which was observed reporting `false` for a child that
  was still running, silently turning cancellation into a no-op.

Cancellation: SIGTERM, then a 5-second grace period, then a forced kill. A
user-requested cancellation is treated as cancelled whether the helper exits
143 or is reported as terminated by signal 15.

`ThumbnailLoader` (an actor, app-wide singleton) renders catalogue previews:
QuickLook first (same machinery as the Finder, with a system-wide on-disk
cache), ImageIO reading the NEF's embedded JPEG preview as fallback. Neither
path demosaics, which is why a folder of 40MP negatives fills in quickly. The
Edit tab uses a QuickLook-skipping path tuned for large published TIFFs
instead.

`ScannyBoyUITests` is **excluded from the test scheme**. Under CI's XCUITest
session the window opens but the `NavigationSplitView`'s content never
populates the accessibility tree. See the long comment in `project.yml`.

---

## 14. Known gaps — read this before you "fix" something

These are all real, all verified in the current code, and several are places
where the README or `DECISIONS.md` describes intent that is not implemented.

1. **The overlap sheet does not exist.** `probe --roll` correctly computes and
   emits `roll_overlap`, but nothing in Swift decodes it — the string
   `roll_overlap` does not appear anywhere under `mac/ScannyBoy/`.
   `ConfigurationModel.runCommand()` passes `skipSources: []`
   unconditionally, so **every run replaces (supersedes) whatever it
   overlaps**, with no Skip/Replace choice offered. The README and
   `DECISIONS.md` both describe the sheet as shipped; it is not. The CLI side
   is ready for it.

2. **Nothing ever sets `intended_datetime_original`.**
   `roll_sequence.intended_times()` is fully implemented and tested but is
   **never called by production code**. `metadata.roll_capture_date` and a
   negative's `capture_time.date_override` have no CLI write path at all.
   Consequently no negative is ever dirty in normal use, and the Edit tab's
   Apply button is effectively unreachable — the only thing that ever writes
   an intended time is `_maybe_reapply_metadata`, propagating an
   already-applied value across a re-stitch. Wiring this up means: a CLI
   command (`roll set-date`, by analogy with `roll rename`), a call to
   `intended_times()` to populate the field, and Edit-tab controls.

3. **`PER_NEGATIVE_LOCKED` is declared but never raised.** The code exists in
   `events.py`, `CONTRACT.md`, `schema.json`, and `CLIEvent.swift`, but no
   Python code path emits it — because nothing can change an existing roll's
   `shots_per_negative` in the first place. `roll init` sets it once.

4. **`rebate_deviation_px` is always `null`.** See §8.1.

5. **The app can never keep a work directory.** `CLICommand.run` accepts a
   `work:` parameter, but `ConfigurationModel.runCommand()` never supplies one,
   so `run_full` always creates and then deletes `<roll>/.work/<run_id>/`. The
   README's re-stitch instructions ("a run started with `--work`") therefore
   describe a CLI-only workflow: to re-stitch from the app you must have
   produced a work directory by invoking the CLI yourself.

6. **`stitch --overwrite` is accepted and ignored** — intentionally, but it is
   still dead surface area.

---

## 15. Scope this project does not cover

No App Store, no Developer ID signing, no notarisation, no sandboxing, no
Intel build. Ad-hoc signing ("Sign to Run Locally") is enough for this local,
single-user, single-machine release. macOS 14+, Apple silicon, Xcode 16.2,
Swift 6, Python 3.13 pinned.

Deferred with an attachment point recorded on
[`punchlist.md`](punchlist.md), none scheduled: crop from manifest data, white
balance / base neutralisation, extended metadata (location, camera, lens, film
stock), a contrasting fill colour, manual negative reordering, deleting a
negative outright, and the rebate detector. **Negative inversion is Phase 4**
— the program currently produces stitched *negatives*, not positives.

---

## 16. Practical notes for changing things

- **Tests are co-located** (`foo.py` / `foo_test.py`). Add tests next to the
  code.
- Tests needing real NEFs **skip loudly** when `tests/fixtures/nef/` is empty,
  printing what they did not test. The rest of the suite still proves
  something. Real scans are gitignored (hundreds of MB, public repo).
- `synthetic_scene_support.py`, `fake_nef_support.py`,
  `tiff_fingerprint_support.py`, `sample_nef_support.py`, and
  `packaged_app_support.py` are the shared test-fixture helpers.
- After changing anything in `cli/`, re-run `scripts/build-cli.sh` before the
  Swift tests will see it — `mac/ScannyBoy/Helpers/ScannyBoyCLI.app` is a
  staged copy, not a live reference.
- Changing a measured threshold in `detection.py` / `registration.py` /
  `layout.py` / `composite.py` changes `stitch_params`, which is a roll
  invariant — existing rolls will refuse new runs with
  `ROLL_INVARIANT_MISMATCH`. Same for `RAW_PARAMS` (`processing_params`) and
  the ICC profile (its hash).
- Adding an event or code means touching, together: `events.py`,
  `shared/contract/CONTRACT.md`, `shared/contract/schema.json`, and
  `mac/ScannyBoy/CLIBridge/CLIEvent.swift` (which has an exhaustive
  string↔case mapping, tested for completeness in `CLIEventTests.swift`).
- Work is planned chunk-by-chunk; each chunk is one branch and one PR merged
  in order, `main` is protected, and CI must pass. See
  [`CONTRIBUTING.md`](../CONTRIBUTING.md).
