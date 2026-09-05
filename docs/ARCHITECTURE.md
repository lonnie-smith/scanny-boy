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
collected in a durable named folder called a **roll**. Published negatives
can then be edited **non-destructively** (rotation ops recorded in a library
database; pixels transformed only at export) and exported with their edits
applied.

Three vocabulary terms carry the whole design:

| Term | Meaning |
| --- | --- |
| **frame** | One `.NEF` capture. Several frames cover one physical negative. |
| **negative** | One physical film frame = one group of N consecutive frames = one published TIFF. |
| **roll** | A named folder holding many negatives, added to across many runs over time. |

`shots_per_negative` (1–12, typically 3) is each stitch batch's own choice —
required on `convert`/`run`, recorded in the work manifest, and never stored
on the roll, so one roll can hold negatives stitched from different scan
counts.

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
folder, never parses a manifest, never enumerates the library, never reads
the library database. Everything it
displays comes back from a CLI call. This is not stylistic — it is the
constraint that keeps validation from drifting between two implementations,
and it is enforced by convention in review, not by a compiler. If you find
yourself about to add a `sort`, a `filter`-that-decides, or a SQLite read in
Swift, that is the wrong place.

The two legal ways Swift learns anything:

- `probe` (catalogue, selection validation, grouping, roll overlap)
- `roll list` / `roll info` (the library and one roll's manifest)

The CLI is a **subprocess** emitting one JSON object per line on stdout.
stderr is human logs and is never parsed. See
[`shared/contract/CONTRACT.md`](../shared/contract/CONTRACT.md) — it is the
source of truth for args and event shape, with
`shared/contract/schema.json` as the authoritative JSON Schema for one event
line.

`PROTOCOL_VERSION` is **10** ([`events.py`](../cli/src/scanny_boy/events.py)).
Version 10 added 2D grid stitching: `--grid AxD` on `probe`/`prepare`/`run`
(mutually exclusive with `--per-negative`; a strip is the down=1 case, and
`min(across, down) <= 2` because every cell must show film rebate — the
rule's home is CONTRACT.md), the `INVALID_GRID` error code, and the
`STITCH_GRID_ORDER_UNEXPECTED` warning. Version 9 added the extended
metadata editing and 1:1 region rendering; version 8 added normalization
and the per-frame scale; version 7 added geometric calibration; version 6
added flat-field profiles: a `flatfield` command family
(`create`/`list`/`delete`), gain maps stored beside the library database, and
`--flatfield` on `convert`/`run`/`probe`, folded into `processing_params` as
the profile token. The profile is not a roll invariant: a roll does not lock
to one, and different runs into the same roll may each choose a different
profile (or none). Version 5 moved each roll's durable record from the roll
folder's `scanny-boy-roll.json` into a library SQLite database and added
`edit rotate` / `export`. A client that only understands an earlier version
must reject the stream rather than guess.

---

## 4. The CLI's command surface

```
roll init   --library DIR --name NAME
roll list   --library DIR
roll info   --roll DIR
roll rename --roll DIR --name NAME

probe   --input DIR [--files ...] [--per-negative N | --grid AxD] [--out DIR] [--roll DIR] [--flatfield ID]
prepare --input DIR --files ... --out DIR [--per-negative N | --grid AxD] [--jobs N] [--overwrite] [--flatfield ID]
stitch  --work DIR --roll DIR [--jobs N] [--overwrite] [--allow-partial] [--negatives ID ...]
run     --input DIR --files ... --roll DIR [--per-negative N | --grid AxD] [--jobs N]
        [--work DIR] [--skip-sources FILE ...] [--flatfield ID]
apply-metadata --roll DIR
edit rotate --roll DIR --negative ID --direction cw|ccw
edit delete --roll DIR --negative ID
export      --roll DIR --output DIR [--negatives ID ...]
flatfield create --reference FILE --name NAME
flatfield list
flatfield delete --profile ID
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

`prepare` (stage 1, renamed from `convert` — "Convert" is reserved for the
whole `run`; see DECISIONS.md's naming split) and `stitch` remain
independently usable; `prepare` is the only command that still writes to a
plain `--out` work directory rather than a roll. `stitch --overwrite` is accepted and **deliberately ignored** — a
stitch replaces a published file only by adopting the covered negative in
place, which needs no flag
([`stitch_pipeline.py`](../cli/src/scanny_boy/stitch_pipeline.py),
`run_stitch` docstring).

`edit rotate` and `edit flip` append a rotation or horizontal-mirror op to
the negative's ordered ops log in the
library database and regenerate the CLI-rendered preview — they **never
touch the published TIFF**, and each accepts a selection of negatives
validated up front. A flip does not commute with rotation, so the ops log
replays into a `(quarter_turns, flipped_horizontally)` pair
(`repo.net_edit_state`), the one shape every pixel consumer (preview
regeneration, export) drives from. `edit delete` is the one destructive
edit: it
drops the negatives' records (their edits logs cascade away), unlinks their
published TIFFs and previews, and renumbers the survivors. `export` is the
moment edits become pixels: it
replays each negative's ops log over the published TIFF and writes the
result into a separate output folder, never opening the roll's own files
for writing. Export carries the density profile and the `normalization`
block in its `ImageDescription`, but no EXIF yet (§14).

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
| `linear.py` | The linear transfer: plain fixed-point decode/encode between uint16 codes and [0, 1]. |

**Input side**
| Module | Role |
| --- | --- |
| `catalogue.py` | Discover `.nef` (case-insensitive, no recursion), read capture timestamps, compute canonical order. |
| `selection.py` | Pure functions: order a selection, check contiguity, chunk into groups. |
| `metadata.py` | Read EXIF settings, white balance, and the "digitized" source fields from a NEF. |
| `consistency.py` | Validate that a selection shares aperture/ISO/focal length/orientation/WB/lens, and that every file carries an exposure time (values are not compared — exposure may differ across a roll). Operates on `SourceSettings`, so it is testable without real NEFs. |
| `raw_decode.py` | `RAW_PARAMS` and the rawpy calls. |

**Flat field**
| Module | Role |
| --- | --- |
| `flatfield.py` | The gain-map maths (`compute_gain`, ported from NegPy), the `.npz` store beside the library database, banded in-place application, and the profile token. Every constant of the feature is defined here and nowhere else. `create_profile` moved to `calibration.py`; this module owns only the gain map. |
| `calibration.py` | `create_profile`'s calibrated path: board-format detection, full-res and half-size per-channel detection, the fit ordering, the deterministic held-out split, the report assembly, and the `flatfield_progress` events (protocol version 7). |

**Output side**
| Module | Role |
| --- | --- |
| `tiff_writer.py` | Pass 1: base TIFF via `tifffile`. |
| `tiff_exif.py` | Pass 2: nested EXIF directory via `tifftools`, addressed by numeric tag code. |
| `stitched_tiff.py` | The stitched variant of the same two-pass write. |
| `manifest.py` | `scanny-boy-manifest.json` (work directory, format version 1). |
| `roll_manifest.py` | One roll's durable record (format version 4) — the dataclasses, invariants, and naming rules; persisted in the library database, not a file. |
| `roll_folder.py` | The library folder: slugging, collision suffixes, create, rename, `roll list` from the database. |
| `roll_sequence.py` | A roll's display order and rank-based applied timestamps. Pure functions. |
| `output_folder.py` | Folder validation, rerun planning, recovery cleanup — parameterised over which manifest kind it reads. |

**The library database**
| Module | Role |
| --- | --- |
| `library/db.py` | The one SQLite store (`~/Library/Application Support/ScannyBoy/library.db`; `SCANNY_BOY_LIBRARY_DB` relocates it): engine cache, WAL/busy-timeout PRAGMAs, Alembic migrations applied programmatically on every open. |
| `library/models.py` | The SQLAlchemy rows: roll, run, source, negative, edit. |
| `library/repo.py` | `RollManifest` dataclasses to and from rows. `save_roll` upserts the whole manifest keyed by `roll_id`; children are diffed by key. Load and save are the only two shapes the rest of the program sees. |

**Editing and export**
| Module | Role |
| --- | --- |
| `edits.py` | `edit rotate`: append a rotation op, regenerate the preview, emit `edit_recorded`. `edit delete`: drop the negative's record, TIFF, and preview, emit `negative_deleted`. Never touches a surviving negative's published TIFF. |
| `previews.py` | Small lossless PNG previews of published TIFFs, under Application Support beside the database; rewritten whenever an edit changes the rendering. |
| `exporter.py` | `export`: replay a negative's ops log over its published TIFF into an output folder. Pixels only — no EXIF/ICC carry-over yet. |

**Stitching**
| Module | Role |
| --- | --- |
| `detection.py` | Build the small 8-bit greyscale detection image (downscale, percentile-normalise, optional CLAHE). |
| `charuco.py` | The two ChArUco calibration boards and everything corner-shaped around them: detection, sub-pixel refinement, board-format auto-detection, and the id-driven collinear-set grouping. |
| `geometry_fit.py` | The staged plumb-line distortion fit, its held-out evaluation, and the acceptance/magnitude gates. |
| `ca_fit.py` | The half-size per-channel chromatic-aberration fit, the `scale`/`maps` mode decision, and its acceptance gates. |
| `registration.py` | Feature detect, match, RANSAC, the rigid fit, the per-pair gates. |
| `layout.py` | The global least-squares solve, connectivity check, canvas size, valid rect — and the photometric counterpart `solve_gains`. |
| `composite.py` | Warp, solve and apply per-frame photometric gains, feather-blend in linear light, overlap MAD, encode. |

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
run --input IN --files ... --roll ROLL [--flatfield ID]
  │
  ├─ run_full (run_pipeline.py)
  │    ├─ work dir = ROLL/.work/<run_id>/   (created here; ALWAYS removed at the end)
  │    ├─ files -= skip_sources             (BEFORE grouping)
  │    │
  │    ├─ run_convert (pipeline.py) ────────────────── stage "prepare"
  │    │    load profile + gain map → validate selection → consistency → hash
  │    │    sources → disk check → write `running` work manifest
  │    │    → for each group: stage every frame, then publish the group atomically
  │    │        per frame: decode → CLIP FRACTIONS → FLAT FIELD → base TIFF
  │    │                   → nested EXIF   (3 progress steps)
  │    │    → work dir now holds one intermediate TIFF per frame + manifest
  │    │
  │    ├─ run_stitch (stitch_pipeline.py) ──────────── stage "stitch"
  │    │    verify every intermediate's size + SHA-256
  │    │    → check roll invariants, append this run to the roll record
  │    │    → SOLVE every negative's layout first (canvas sizes needed for disk check)
  │    │    → disk check on the roll's volume
  │    │    → for each negative: composite (warp → solve gains → blend
  │    │       → NORMALIZE: log conversion, bounds analysis, rebate detector,
  │    │         headroom encode — fused into composite()'s final encode)
  │    │       → gate on the post-gain overlap MAD → stage → publish
  │    │       (covered negatives are adopted in place or removed here —
  │    │        stitch_pipeline's replacement rule; nothing after the fact)
  │    └─ rmtree(work dir)
  │
  └─ finished
```

**Why flat-field correction sits inside the convert stage, before the
stitch's gain solve:** the stitch stage's per-frame per-channel gains
(`layout.solve_gains`, §8.3) are a global scalar per frame per channel and
cannot represent a spatial gradient. Correcting vignetting before they run
means the residual they are asked to explain is real exposure mismatch, not
lens falloff — which also makes `overlap_mad` a cleaner measurement, since
overlapping regions sit at different distances from each frame's own optical
centre and disagree *spatially* before correction. The stitch stage needs no
change at all: it reads `processing_params` off the work manifest already,
and the flat-field token rides inside it.

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
`STITCH_UNITS_PER_NEGATIVE = 10` — the flat-field-era 9 plus the
normalization pass, which is a downscale plus a handful of percentile sorts
on a 1024-grid and is expected to be nearly free; re-measure, don't assert)
by `_wrap_emit_for_stitch` in `run_pipeline.py`, so `completed`
advances monotonically across one combined `total`. The UI derives progress
from `completed`/`total` **only** — never from `source_index`, which with
`--jobs > 1` names "one frame in flight", not a queue position.

---

## 7. Colour: the one thing most likely to be got wrong

RAW decode uses `rawpy` with **`gamma=(1, 1)`, `output_color=raw` and unity
white balance** (`user_wb=[1, 1, 1, 1]`): linear sensor channels straight
from the demosaic, with no colour-matrix conversion into a colourimetric
space and no per-channel white-balance gain. This matches NegPy's decode
exactly — its pipeline treats the scan as a radiometric measurement of the
sensor's own channels and handles channel balance in film terms (see
`docs/DECISIONS.md`, "Linear decode for NegPy compatibility"). This
supersedes the earlier `gamma=(1.8, 16)` / ProPhoto decode, whose LibRaw
curve and colour conversion violated NegPy's assumption on three counts.

Consequences, all live in the code today:

- [`linear.py`](../cli/src/scanny_boy/linear.py) (formerly `romm.py`)
  holds plain fixed-point scaling: `decode_to_linear` is `code / 65535`,
  `encode_from_linear` is clip-and-round. The round trip is exact for every
  code — proved by a test, not assumed.
- The prepare stage's intermediates carry `ScannyBoy-Linear-ProPhoto-v1.icc`:
  ProPhoto primaries (byte-identical to the upstream ProPhoto-v4 source)
  with a **linear** TRC (parametric type 0, g = 1.0) — the truth about
  those pixels. Both profiles are generated deterministically by
  `cli/tools/generate_icc_profile.py`.
- `icc_profile.py` verifies each profile's SHA-256 on every load, and
  `tiff_writer.write_base_tiff` refuses to write a TIFF with an empty
  profile. An untagged file is never produced.
- All geometric and photometric work happens in **linear light**: decode to
  linear `float32`, warp, blend — then, once, convert to normalized log
  density (§7.1).
- Flat-field correction multiplies in the same linear light:
  `flatfield.apply_in_place` does `decode_to_linear → multiply →
  encode_from_linear` per band, a lossless fixed-point round trip (proved
  by a test — a gain map of exactly 1.0 round-trips a real decoded frame
  to byte-identical pixels). Where the correction would boost an
  already-bright pixel past full scale, `encode_from_linear`'s clip at 1.0
  loses the highlight — the pipeline emits `FLATFIELD_HIGHLIGHT_CLIPPED`
  when more than 0.1% of a frame's pixels clip rather than losing them
  silently. The old punchlist item asking for linear intermediates is
  resolved: the intermediates **are** linear.

`RAW_PARAMS` ([`raw_decode.py`](../cli/src/scanny_boy/raw_decode.py)) is
locked and every value was independently verified to matter — in particular
`no_auto_bright=True` and `adjust_maximum_thr=0.0`, which are what keep pixel
scaling identical across a negative's frames. Changing it invalidates every
roll's `processing_params` invariant.

### 7.1 Normalization: the published TIFF is a baked, normalized working intermediate

The stitch stage's `composite()` no longer encodes linear `uint16`. Its
final encode fuses the whole normalization pass (NegPy §2, ported in
[`normalization.py`](../cli/src/scanny_boy/normalization.py); the locked
decisions and their arguments are in `DECISIONS.md`'s "Normalization
decisions") on the float32
accumulator that already exists — blending, warping and the gain solve stay
in linear light, which is where they are physically correct:

1. `to_log_density`: `D = log10(clamp(I, 1e-6, 1))` — where a negative's
   picture information lives (a linear uint16 spends ~11.3 effective bits
   at the dense end; log-encoding is uniform 16 at the same file size).
2. `block_median_grid`: a b×b block-median prefilter to an `ANALYSIS_GRID`
   (1024)-bounded grid — isolated extremes vanish, and the statistics are
   nearly resolution-invariant.
3. The **rebate detector** (`detect_rebate`) excludes film rebate / clear
   base from the analysis region; the region itself is the caller's
   `largest_valid_rect` — see §12 for why it is applied now.
4. `analyze_bounds`: the two-axis meters (luma at `BASE_LUMA_CLIP`,
   colour at `BASE_COLOR_CLIP`, recombined with a **mean** on the luma axis
   and a **median** on the colour axis; the dense end reads one shared
   chroma-gated pixel set so a saturated highlight is never mistaken for
   film cast).
5. `normalize_log_image` + `encode_normalized`: per-channel affine stretch
   into `[0, 1]` — `floor → 0.0` (scene highlight, still dark), `ceil →
   1.0` (film base / scene shadow, still light) — **unclamped**, then
   encoded to `uint16` with asymmetric headroom
   (`NORMALIZED_HEADROOM_LOW = 0.15`, `HIGH = 0.10`) so the excursion the
   block median never saw stays representable for the edit stage's tone
   curve.

Three properties to hold onto:

- **The published TIFF is a working intermediate, not the deliverable** —
  the positive export does not exist yet, and the creative-edit stage reads
  this. The bake is arithmetically reversible: the per-channel floors and
  ceils are recorded, and `10 ** (floor + val * (ceil - floor))` recovers
  the linear composite to within quantization.
- **It stays a negative in appearance.** The published file's border is now
  **white** (`NORMALIZED_FILL` = 1.0 + `NORMALIZED_HEADROOM_HIGH` = code
  65535), and the file you open in Photoshop looks like a negative while
  the preview beside it looks positive. Both are correct (§3.11/§3.14 of
  the plan).
- **The meters read an analysis region, not the canvas.** Uncovered canvas
  pixels sit at `log10(1e-6) = -6.0` — a colossal outlier at exactly the
  dense end the floor percentile reads — so `largest_valid_rect`, computed
  before compositing, restricts the meters only. It never crops the output.

The published TIFF carries a **second** ICC profile,
`ScannyBoy-Density-ProPhoto-v1.icc`: ProPhoto primaries, parametric type 0
at g = 2.2 — a *viewing convention*, explicitly not a colorimetric claim
(a correct profile would decode the file back to un-normalized linear,
undoing the one thing this stage does). Every internal consumer —
previews, the edit stage, export, the future print stage — decodes through
`normalization.decode_normalized`, never through an ICC transform; a
grep-shaped guard test keeps the loader out of everything but the write
path.

- **Previews are display-decoded, then inverted.** The published TIFF is
  normalized log density, so `previews.py` downscales in code space,
  decodes through `decode_normalized`, takes `1 − val` so the Edit
  filmstrip reads as a positive, and scales to 8-bit — **no gamma**: log
  density is already roughly perceptually uniform, and an sRGB OETF would
  double-encode. Display encoding lives there and only there.
- **Metering is recorded, never acted on.** The per-channel shadow refs
  (P98), the exposure anchor (P50) and the textural range (P10–P90) are
  measured on the same prefiltered grid and stored in the roll record's
  `normalization` block for the print stage (Phase 4); nothing in this
  program reads them back yet.
- **`normalize` params are a roll invariant.** Every constant of the
  feature rides in `processing_params.normalize` (§3.8), so a roll can
  never mix normalized and un-normalized negatives — and retuning any
  constant invalidates existing rolls. Know that before you tune.

**TIFF format**, identical for frames and stitched output: three-channel
`uint16` with an embedded profile, `Orientation` always `1` (pixels
are already upright — never copy the source value), Deflate with horizontal
prediction (compression code `32946`, not `8`), written in two passes
(`tifffile` for the base, `tifftools` for the nested EXIF, base removed only
after the final file verifies). The four non-obvious `tifffile` rules
(`metadata=None`, `description=`/`software=` as keywords, `iccprofile=` as a
keyword, the Adobe Deflate code) are each documented in place and each
matters. *Transfer by stage:* intermediates are linear (§7), published
TIFFs are normalized log density (§7.1).

---

## 8. Registration and stitching

**The model.** A negative's frames form a one-dimensional strip, but **capture
order is never assumed to be spatial order.** Every pair of a negative's
frames is matched (O(n²), trivial at n≤12), and a global layout is solved from
whichever pairs actually overlap. Neighbour-chaining and order detection are
both rejected — the global solve makes order irrelevant for free.

**The pairwise geometry is rigid: rotation + translation, scale fixed at
exactly 1.** `cv2.estimateAffinePartial2D` is used only for the RANSAC
inlier mask and to *measure* scale drift; the transform actually used
against the acceptance gates is always re-fitted with closed-form Umeyama
with scale forced to 1 (`registration.rigid_from_correspondences`). Never
an affine, never a homography. The same inliers also get a closed-form
Umeyama fit *with* scale (`registration.similarity_from_correspondences`),
which the global layout solve reads (docs/STITCH_QUALITY_PLAN.md section 2)
— the pairwise gates are unaffected.

**The solve** ([`layout.py`](../cli/src/scanny_boy/layout.py)) is three
linear least-squares problems, not a bundle adjustment. Frame *i* maps
`p → sᵢR(θᵢ)p + tᵢ`; a pair gives `log sᵦ - log sₐ = log σ_ab`,
`θ_b = θ_a + φ_ab`, and `t_b = t_a + sₐR(θ_a)·u_ab`. Scales solve first
(log-space, `solve_gains`'s geometric-mean-1-anchor idiom), then rotations
(linear in the scalar θs), then translations (linear once s and θ are
known). **This is why SciPy is forbidden as a dependency.** Do not replace
it with a nonlinear optimiser. The model is a similarity — never an affine,
never a homography — because film does not sit at a constant height above
the stage from frame to frame; with scale forced to 1 that mismatch used to
be absorbed into rotation and translation instead.

**Blending** is a linear feather in linear light, ramped along the strip
axis only: each frame's weight is the distance from the nearer end of its
own extent along the strip's long axis (`layout.Layout.strip_axis`),
constant across the strip — an isotropic distance transform of the eroded
validity mask is kept only as the fallback for a layout with no trustworthy
axis. The isotropic version made a pixel's crossfade identical near the
strip's long borders and down its middle, but near those borders the
nearest mask edge is the border, not the seam, so both frames' weights
collapsed toward 50/50 there and residual misregistration smeared into a
curved band that widened toward the edges. Before the blend, per-frame
per-channel **photometric gains** (§8.3) reconcile lamp drift between
frames, so the feather only ever has to tolerate misregistration. A hard
midline seam (preserves grain, shows misregistration as a line), a band
around the overlap midline, and a multi-band Laplacian blend (hides
misalignment, softens grain, much heavier) were all considered and set
aside as named, deliberately deferred next steps.

Warp details that are load-bearing: `INTER_LANCZOS4` on `float32`, clamped to
`>= 0` immediately after (measured −0.088 undershoot); each frame warps into
its **own bounding box**, not the full canvas; the mask warps
`INTER_NEAREST` and erodes by `MASK_ERODE_PX = 5` with an **elliptical**
kernel (a repeated square kernel erodes by Chebyshev distance and
under-erodes exactly the diagonal edges a rotated frame has); `cv2.erode`
uses `BORDER_CONSTANT/0` so a frame's own corners actually erode.

Uncovered pixels get `FILL_COLOR`, currently black, recorded in the roll
record so a file is interpretable without knowing which build wrote it.

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
| `MAX_OVERLAP_MAD`, `MASK_ERODE_PX`, `MEMORY_SAFETY_FACTOR` | `composite.py` | `0.20` (post-gain residual — see §8.3), `5`, `3.5` |
| `MIN_GAIN_OVERLAP_PX`, `GAIN_DRIFT_WARN` | `composite.py` | `1000`, `0.05` — **both provisional and unmeasured** (§8.3) |
| `MAX_CANVAS_DIMENSION`, `MAX_STITCHED_BYTES` | `composite.py` | `30_000` (warn), `3.5 GiB` (fail) |

Most were measured from real scans and approved at "user gate C"; the two
gain constants are the exception (§8.3). Pixel
thresholds are **full-resolution** pixels — points are converted out of
detection space with `detection.to_full_resolution` before RANSAC.

`stitch_pipeline._stitch_params()` serialises this whole table into the roll
record, so a roll records every threshold that was in force when it was
built — and because `stitch_params` is a roll invariant, changing any
constant here will make existing rolls reject new runs with
`ROLL_INVARIANT_MISMATCH`. That is intended, but know it before you tune.

**Overlap MAD is the honest gate.** Inlier counts and reprojection residuals
measure whether the solver was pleased with itself; overlap MAD measures
whether the pixels actually line up. Since gain compensation now runs
before the measurement, the gate value is the **post-gain residual** — a
registration check, not a lamp-drift check — and it is computed *after*
compositing, in `_composite_and_publish`, so a negative can still fail
late, having done all the expensive work. The pre-gain MAD is recorded
beside it in the roll record as the diagnostic that explains why a gain
was applied.

**`rebate_deviation_px` is specified, recorded, and still always `null`.**
Chunk P2-1 found a generic straight-edge finder cannot reliably find the
same physical rebate edge across frames. Normalization's density-based
**rebate detector** (`normalization.detect_rebate`, §7.1) now finds and
excludes rebate for the meters' benefit; deriving the edge's deviation from
the solved strip axis from that mask — retiring the always-`null` field —
is on the punchlist, not implemented.

### 8.2 The CLAHE fallback (not in `DECISIONS.md`)

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

### 8.3 Photometric gain compensation (the newest feature)

Lamp drift between a negative's frames is reconciled by per-frame,
per-channel **gains**, solved before the blend and before the MAD gate.

**The solve** ([`layout.py`](../cli/src/scanny_boy/layout.py),
`solve_gains`) is the geometric twin of the layout solve: per channel, one
linear least-squares in log space — one row per usable pair
(`g_b − g_a = log(mean_a/mean_b)` over the pair's shared valid area,
weighted by `sqrt(shared_count)`, since a mean over N pixels has variance
∝ 1/N) plus one all-ones anchor row with rhs 0, so the solved gains have
**geometric mean 1**: no frame's lamp level is privileged, and the
worst-case gain excursion into the encode clamp is minimized. Names are
sorted internally, so compositing a layout forward or reversed produces
bitwise-identical gains. Rows whose channel means are degenerate are
dropped, not errored; a frame surviving in no row keeps gain 1.0. Like the
layout solve, this is `np.linalg.lstsq` on a deliberately linear system —
the "no SciPy / no nonlinear optimiser" rule applies here too.

**The application** ([`composite.py`](../cli/src/scanny_boy/composite.py))
restructures compositing into two passes. Nothing is accumulated during the
warp pass: the photometric stats need any pair's two frames side by side,
so **every warped frame stays resident** (bounding-box sized, cheap next to
the two canvas-sized accumulators — `estimate_peak_bytes` takes the frame
count for exactly this reason). Then: gather pairwise per-channel means and
the pre-gain overlap MAD, solve the gains, apply them in place to the
warped **linear float32** buffers — never to encoded uint16, never to the
canvas — measure the post-gain residual, and only then accumulate and free
each frame.

Consequences to know about:

- **`MAX_OVERLAP_MAD = 0.20` now gates the post-gain residual**, but the
  value was measured against *uncorrected* overlaps, so applied to the new
  semantics it is looser than a healthy capture's residual. `MIN_GAIN_OVERLAP_PX`
  and `GAIN_DRIFT_WARN` are the only unmeasured thresholds in the pipeline;
  `MIN_GAIN_OVERLAP_PX` borrows NegPy's measured 1000 px floor, and
  `GAIN_DRIFT_WARN` was chosen as the smallest bound that never fires on
  healthy synthetic fixtures.
- A solved gain deviating more than `GAIN_DRIFT_WARN` from unity warns
  `STITCH_GAIN_DRIFT`, by the same pattern as scale drift: it means
  something is wrong with the *capture*, not the solver.
- All three values are serialised into `stitch_params` (with
  `max_overlap_mad_semantics: "post-gain-residual"` naming the change), so
  they are roll invariants — changing them makes existing rolls reject new
  runs with `ROLL_INVARIANT_MISMATCH`.

### 8.4 Geometric calibration (protocol version 7)

A flat-field profile can now carry the rig's full optical description:
radial lens distortion and lateral chromatic aberration, fitted from ChArUco
board frames and applied inside the existing stitch warp
([`docs/GEOMETRIC_PLAN.md`](GEOMETRIC_PLAN.md)).

**The modules**: `charuco.py` owns the two boards (transcribed from
`calibration/lens_calibration_targets.pdf`, which stays the authoritative
artefact), full-resolution corner detection, and the collinear-set grouping
that turns `charucoId`s into straight-line families — rows, columns, and
both diagonals, the diagonals being what constrains the principal point.
`geometry_fit.py` is the staged plumb-line fit (`scipy.optimize.least_squares`,
the project's one nonlinear solver): `k1` alone, then `k1 k2`, then
`k1 k2 cx cy`, each stage kept only if the next does not beat it on
held-out residual. `ca_fit.py` fits each colour channel's radial scale
about its own centre on half-size decodes (`RAW_PARAMS_HALF_SIZE`, where
each pixel comes from one Bayer quad with no demosaic smear), and decides
between `"scale"` mode (rawpy decode scales, when the radial terms
contribute under `CA_SCALE_ONLY_PX`) and `"maps"` mode (per-channel maps at
composite). `calibration.py` orchestrates, and owns the load-bearing
ordering: in `"scale"` mode the flat-field reference itself is decoded with
the same CA scales production will use, or the gain map and the frames
disagree about geometry.

**Where the corrections apply**:

- *Distortion* lives entirely on the stitch side. `register_pair` pushes
  matched points through `cv2.undistortPoints` before RANSAC (so every
  existing pixel threshold keeps its units and its meaning), and
  `composite._warp_bands` folds the forward model into the warp: a banded
  `cv2.remap` whose map is the *closed-form forward* distortion — undistorted
  output pixel → distorted source pixel — generated
  `GEOMETRY_BAND_ROWS` rows at a time, so no frame-sized base map ever
  exists. Exactly one interpolation pass per output pixel; the validity
  mask is remapped with the green map at `INTER_NEAREST`.
- *CA in `"scale"` mode* lives on the convert side: `decode_raw` merges the
  profile's `chromatic_aberration` scales into `RAW_PARAMS` for the call,
  and `jsonable_raw_params` reports the merged params so
  `processing_params` describes the decode that actually happened.
- *CA in `"maps"` mode* lives at composite: the red and blue channels'
  band maps add the per-channel radial scale about each channel's own
  fitted centre; green is untouched.

**The gauge convention** (`K_new = K`, output frame the same size as the
source frame) means `frame_size` never changes: `layout.solve_layout`,
`largest_valid_rect`, `estimate_peak_bytes`, `disk_check.required_free_bytes`
and `_frame_bbox` are untouched. The cost — a 1–7 px border of unsampled
pixels at the frame edge under pincushion — is inside what
`MASK_ERODE_PX` already discards.

**The invariant buckets** split along the convert/stitch boundary:
`processing_params.flat_field` (unchanged) and
`processing_params.chromatic_aberration` (the decode scales, `"scale"`
mode only) on one side; `stitch_params.geometry` (profile id, geometry
object, and the CA object in `"maps"` mode only) on the other. Both are
absent, not null, when the profile carries nothing for that bucket, so a
geometry-free profile compares equal to a pre-geometry roll. A profile
whose geometry a roll depends on is undeletable exactly like one whose
gain map it depends on (`rolls_using_profile_geometry` unions into the
delete check). A profile's geometry is valid only for the frame dimensions
it was fitted at — `flatfield.check_geometry_frame_size` fails
`GEOMETRY_FRAME_SIZE_MISMATCH` at convert, probe, and stitch before
anything is written.

A fit that fails its acceptance gates is dropped, not carried: the profile
is still created (a perfectly good flat-field profile), `geometry` stays
null, and the reason is recorded in `calibration_report` — the profile
record is the one place a human decides whether the numbers are worth
keeping.

---

## 9. The roll: durable, additive, with replacement in place

This is the Phase 3 break, and the thing most likely to surprise you if you
carry Phase 2 intuitions. Since PR #58 there is a second break layered on
it: **each roll's durable record lives in one SQLite library database, not
in a file inside the roll folder.** And since PRs #52/#53, the original
"never in place" rule became "adopt in place": a rerun replaces a covered
negative by taking over its identity, not by publishing a rival.

- One library folder (`~/Pictures/Scanny Boy` by default, relocatable in
  Settings) holds every roll as a **direct child**. The folder holds only
  the published TIFFs (plus `.work/` during a run); the record — roll,
  runs, sources, negatives, edits — lives in the library database at
  `~/Library/Application Support/ScannyBoy/library.db` (§5's `library/`
  package; `SCANNY_BOY_LIBRARY_DB` relocates it). A roll "exists" exactly
  when it is registered there. `roll list` reports registered rolls from
  the database, and a registered roll whose folder has vanished (unmounted
  drive, manual delete) is reported `unreadable` with `ROLL_NOT_FOUND`
  rather than silently disappearing.
- `roll_id` is a UUID and never appears in a path. `roll_name` is free text;
  the folder name is a slug of it (NFC, `[A-Za-z0-9._-]`, whitespace runs →
  single `-`, 60 chars, case-insensitive collision suffixes). Rename moves
  the folder **first**, then saves the new name and location to the
  database — so a failed move leaves both untouched. Delete is two steps,
  in this order: the app moves the folder to the Trash with
  `NSWorkspace.recycle`, then `roll delete` removes the database
  registration (runs, sources, negatives, and edits cascade away with it)
  and unlinks the negatives' rendered previews. A crash between the steps
  leaves an orphan registration that `roll list` reports as `unreadable`,
  never a lost folder; a deleted roll disappears from the next `roll list`.
- **Roll invariants** (`RollInvariants`): `processing_params`, the ICC
  profile hash, `stitch_params`. Everything else — input folder, source
  list, order, grouping, the batch's `shots_per_negative`, and the
  flat-field profile — is *expected* to differ between runs and is **never
  compared**. A roll with no runs yet is unseeded: the last three are
  established by the first run. `processing_params` carries the
  **flat-field profile token** under the key `flat_field` when a profile
  was given, and `stitch_params` carries its optional geometric calibration
  under `geometry`; both keys are excluded from the invariant comparison
  (`ROLL_PROFILE_PROCESSING_PARAMS_KEYS`/`ROLL_PROFILE_STITCH_PARAMS_KEYS`
  in `roll_manifest.py`), so a roll does not lock to one profile — each run
  may choose a different one, or none. The key is absent, not null, when no
  profile is given, so a no-profile run still compares equal to a
  pre-flat-field roll. `name` is deliberately not in the token: renaming a
  profile must not invalidate a roll. This needs no new comparison code —
  the token rides in `processing_params`. Existing rolls (pre-flat-field)
  have no `flat_field` key and therefore refuse profile-carrying runs; same
  breakage the gain-normalization merge (#59) caused through
  `stitch_params`, same remedy: start a new roll.
- **Replacement happens in place, at publish.** A group whose members cover
  existing negatives **adopts** one of them (deterministically: the one whose
  first member matches the group's first member, else the first) — it keeps
  its `negative_id` and output name, and this run's identity, members,
  frames, output, and status replace the record's as the group publishes.
  Any other covered negative is **removed outright** at publish: record
  first, manifest write after, TIFF unlink best-effort (a failed delete
  warns `ORPHAN_FILE_NOT_REMOVED` and never fails the run) — so a crash
  leaves an orphan file, never a dangling record. A group covering nothing
  gets a fresh id and name exactly as before.
  (`stitch_pipeline._append_this_run` + `_remove_covered_negatives`;
  `allocate_output_name`'s `adoptable` set keeps the removed names free.)
  The subset test means an exact rescan adopts its predecessor's identity
  and a merge-regrouping adopts one part and removes the rest; a *split*
  regrouping adopts nothing.
- `negative_id` is `<run.short_id>-negative-NN`, where `short_id` is the
  first 6 hex chars of the run UUID, lengthening to 8, 10, then the whole
  UUID on collision within the roll. Assigned once by `append_run` and never
  recomputed, so ids are stable for the life of the roll — which is also
  what keeps a negative's edit history attached across a re-stitch.
- Output names: the stem of the group's first member in canonical order, plus
  `.tif`, with `-2`, `-3`, … on collision across runs.
  `roll_manifest.allocate_output_name` is the **only** place a published name
  is chosen.
- `sources` are keyed by **SHA-256**, so a renamed rescan of the same bytes is
  recognised as the same source and keeps the `run_id` that first contributed
  it.

There is **no migration** from the JSON-manifest era — neither from the
Phase 2 (`manifest_format_version: 1`) file nor from any later one, because
the app never shipped. `load_roll_manifest` reads the database only; a
folder holding a `scanny-boy-roll.json` is not a roll unless it is
registered.

### 9.1 Sequence and metadata

- A roll's negatives are ordered by the **real capture time** of each
  negative's first member, across every run, ascending. Ties break by run
  index then first filename. Only negatives that are actually published —
  `completed` with a real capture time — can hold a position
  (`roll_sequence._sequenceable`); `pending` and `failed` negatives are
  unsequenced. Since replacement is in place (§9), a re-scan of the same
  frames keeps the position its capture time dictates.
- `sequence` is recomputed on **every** `write_roll_manifest` call — the
  manifest writer mutates the manifest it is given. `roll_sequence.py` is the
  only computation of it.
- The applied timestamp is **rank-based**: `12:00:00 + (rank − 1)` seconds on
  the roll's capture date, or a negative's own date override, ranked within
  that date's negatives.
- **Intent lives in the record; the TIFF is the artefact.** A negative is
  *dirty* when `intended_datetime_original ≠ applied_datetime_original`.
  `apply-metadata` handles every dirty, completed, published negative:
  verify the published TIFF against the record's recorded size and hash
  (skip with `OUTPUT_MODIFIED_EXTERNALLY` rather than rewrite a file the roll
  no longer recognises), rewrite only the nested EXIF
  `DateTimeOriginal`/`SubSecTimeOriginal` via `tifftools` into a sibling temp
  file, verify the temp reads back correctly, rename over the original,
  re-hash, update the record. **No pixel data is ever read or written.**
- A re-stitch of a negative that already had metadata applied re-applies it
  automatically, without asking (`_maybe_reapply_metadata`), as the last step
  before the record write. A failed re-apply leaves the negative `completed`
  but dirty — recoverable with Apply — and never fails the stitch.

---

## 10. Failure, cancellation, cleanup

The same rule at both stages: **the unit fails alone.**

- A group that fails conversion has its whole staging directory deleted; the
  next group continues; the run ends `partial`. Nothing is ever published
  half a group.
- A negative that cannot be stitched is recorded `failed` in the roll
  record, reported via `negative_failed`, and the run continues.
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
`IccProfileError`, `CancelledError`, `EditFailure` (edit),
`ExportFailure` (export). **The `code` is the machine interface;
message text is not.** `stitch_pipeline._friendly_failure_message` deliberately
rewrites technical messages into user-facing sentences precisely because
nothing keys off them.

---

## 11. Concurrency, memory, disk

- `ThreadPoolExecutor` for RAW work — rawpy's LibRaw build releases the GIL.
  Default workers `min(len(files), os.process_cpu_count() or 1, 4)`, where
  `len(files)` is the run's total frame count, not one group's; the cap of 4
  exists because neither CPU-count API distinguishes P-cores from E-cores on
  Apple silicon. `--jobs 1` uses a fully serial path that never constructs an
  executor.
- Concurrency spans the whole run, not just one group: `pipeline.py`'s
  `run_convert` opens one pool for the entire run and submits every group's
  frames to it up front, in canonical order (`_submit_all_groups`). The
  pool's FIFO queue means `workers` frames run at a time and the run works
  through groups roughly in order, so a run of many single-shot negatives
  (`--per-negative 1`) — previously capped at one frame at a time no matter
  what `--jobs` was, because each group only ever had one frame to give a
  per-group pool — now actually uses the worker count. Publishing still
  walks one group at a time, in canonical order, on the main thread; a
  cancellation discards the group the loop is blocked on *and* any later
  group the pool had already raced ahead and finished staging in the
  background (`_discard_from`).
- **640 MiB per worker**, and the total must not exceed half of physical RAM.
  The computed default is silently *reduced* to fit; an explicit `--jobs` that
  exceeds it is *rejected* with `INSUFFICIENT_MEMORY`, because the user asked
  for a specific number. The measurement table justifying 640 MiB is in
  `concurrency.py`; re-measure with `scripts/measure-concurrency.py`.
- **Flat-field memory** stays inside that budget without re-measuring it: the
  full-resolution gain map is materialised **once per run** and shared
  read-only across workers (~294 MB for a 24.5MP frame, one allocation, not
  per worker), and the multiply is applied in horizontal bands of
  `FLATFIELD_BAND_ROWS = 512` rows — decode, multiply, re-encode each band
  back into the same `uint16` array in place — so the peak transient per
  worker is band-sized (~37 MB), not frame-sized.
- Parallelism **never spans negatives** — a negative is published all at once
  or not at all. In the stitch stage `--jobs` bounds feature detection only;
  compositing is one negative at a time, single-threaded through the
  accumulator.
- **Composite peak memory** is estimated before any allocation and multiplied
  by `MEMORY_SAFETY_FACTOR = 3.5`. That factor is measured, not padding:
  NumPy does not return freed arenas to the OS, so resident memory tracks the
  *sum* of successive allocation phases rather than their peak, and real
  three-frame stitches measured 2.5–3.4× a naive estimate. The estimate
  formula itself changed with gain compensation — every warped frame now
  stays resident until the gain solve is done (§8.3), and
  `estimate_peak_bytes` takes the frame count for exactly that — so the
  factor is overdue for re-measurement with `scripts/measure-registration.py`.
- **Disk** is estimated conservatively (compression assumed to save nothing,
  20% margin) and checked per volume. For `run`, the work directory and the
  roll may be on different volumes; each is checked separately against its own
  formula, never summed.

---

## 12. Manifests

The **work manifest** (`scanny-boy-manifest.json`, format version 1) keeps
the original discipline: write to a temp file, `fsync`, rename into place,
then `fsync` the directory — so a reader never sees a half-written manifest
— and it validates **structurally by hand**, not against the JSON Schema
file, because *the packaged CLI must never load a file outside
`cli/src/scanny_boy/` at runtime.*

The **roll record** no longer exists as a file at all: it is rows in the
library database (§9), written only by this program. The hand-written
structural validator that guarded against corrupt or foreign JSON is gone
with the file; what survives on load is the output-path containment check,
which is cheap and protects the pipelines from a tampered row.

The schema files in `shared/contract/` are authoritative for the shape and
are read **only by tests** (`manifest_schema_test_support.py`,
`roll_manifest_schema_test_support.py`, `schema_test_support.py`). If you
add a field to the roll record, you must update the dataclass, `to_dict`,
`library/models.py` + `library/repo.py` (rows to dataclasses and back), and
the schema file.

| | `scanny-boy-manifest.json` | the library database |
| --- | --- | --- |
| Lives in | the work directory | `~/Library/Application Support/ScannyBoy/library.db` |
| Format version | 1 | 4 |
| Scope | one `convert` run | many runs, forever |
| Records | sources + hashes, canonical order, groups, expected/completed outputs + hashes, `processing_params` | roll identity, invariants, every run, every source by hash, every negative's members / layout / per-frame gains / all quality metrics (pre- and post-gain MAD) / canvas / valid rect / fill colour / capture times / output hash, and each negative's ordered **edits ops log** |

The work manifest still carries a `film_date` field, now filled with the
calendar date of the selection's first *real* capture time. It is vestigial —
kept only because the schema has it and a rerun comparison still checks it.
`--film-date` is gone from every command.

The **valid rectangle** is computed, recorded, and — since normalization —
**applied once**: it is the analysis region the meters read (§7.1), moved
above the `composite()` call so the meters can be told where the fill is
not. It restricts the analysis only; the canvas is still the full union
bounding box and nothing captured is discarded. The rect still exists for a
future crop tool, which supersedes the rebate detector's region decision.

`output_folder.py` is parameterised over which manifest kind it reads via
`FolderRules` (`PREPARE_RULES` / `ROLL_RULES`) rather than being duplicated.
Under `ROLL_RULES` registration — a database check, not a filename check —
decides whether the folder holds a record, and a registered roll folder may
be genuinely empty, since its record lives in the database. A published
negative is neither a conflict nor a stale output; only recovery cleanup of
never-finished negatives still applies.

---

## 13. The Swift app

One window, `NavigationSplitView`:

```
RootView                    resolves the CLI helper once; shows why not if it can't
└─ ContentView              sidebar + workspace
   ├─ RollSidebar           every roll from one `roll list` call
   └─ workspace (per roll)
      ├─ Add Scans          input folder → contiguous selection → Run
      ├─ EditStageView      filmstrip of CLI-rendered previews, rotate cw/ccw
      ├─ MetadataStageView  roll info (read-only) + capture-time Apply
      └─ ExportStageView    choose an output folder → one `export` invocation
```

One active run **app-wide** disables the sidebar, the tab picker, and every
stage's controls; the Export button additionally waits for any in-flight
export — one helper invocation at a time.

**Models** (all `@MainActor @Observable`):

| Type | Role |
| --- | --- |
| `RollLibrary` | The library. Its only direct filesystem touch is `NSWorkspace.recycle` for the delete's Trash move; create/rename/list/delete all go through the CLI. |
| `FlatFieldModel` | The flat-field profile list. Every call is a CLI call: `flatfield list` to read, `flatfield create` / `flatfield delete` to change. |
| `ConfigurationModel` | Add Scans state. Every rule beyond UI bookkeeping is read back from `probe --roll`. `perNegative` is each stitch batch's own choice, set on Add Scans and required before a run can start. A flat-field profile is required (`flatFieldProfileID != nil` gates `runEnabled`); it is a per-run choice, not the roll's, defaulted from `UserDefaults` to whichever profile was used last. |
| `EditModel` | Edit + Metadata tab state, from `roll info`. Drives `edit rotate` and `edit delete` round trips (net rotation and `preview_path` come back in the event), derives `visibleNegatives`, `dirtyNegatives`, `applyCommand`. |
| `RunModel` | **One shared model** drives Run, re-stitch, *and* Apply — not three parallel mechanisms. |
| `ExportModel` | Export tab state. Drives its own CLI session rather than `RunModel`'s — `export` emits no `progress`, so the run-log machinery would be dead weight — collecting `export_done` per negative. |

Flat-field profiles are managed through a menu command ("Flat-Field
Profiles…", the same notification pattern `Re-stitch…` uses) opening the
`FlatFieldProfilesSheet`: the profile list with per-row delete (through the
CLI, which refuses `FLATFIELD_PROFILE_IN_USE` when a roll's invariants name
the profile — shown as an alert), plus New Profile… — an `NSOpenPanel`
limited to NEF, a name field, and Create with a spinner, since building a
profile decodes a RAW and takes seconds. A gain map is app-private data with
a database row, not a user document, so unlike a roll's folder — which the
app itself trashes with `NSWorkspace.recycle` before `roll delete`
unregisters it — this goes through the CLI alone.

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
path demosaics, which is why a folder of 40MP negatives fills in quickly.
The Edit tab displays neither of those: with edits in the picture, "what the
negative looks like" is **derived state** and only the CLI may derive it, so
the filmstrip loads the CLI-rendered PNG named by each negative's
`preview_path` (`previews.py`), re-fetching whenever rotation changes.

`ScannyBoyUITests` is **excluded from the test scheme**. Under CI's XCUITest
session the window opens but the `NavigationSplitView`'s content never
populates the accessibility tree. See the long comment in `project.yml`.

---

## 14. Known gaps — read this before you "fix" something

These are all real, all verified in the current code, and several are places
where the README or `DECISIONS.md` describes intent that is not implemented.

1. **The overlap sheet does not exist.** `probe --roll` correctly computes and
   emits `roll_overlap`, but nothing in Swift decodes it — the only
   occurrence of the string under `mac/ScannyBoy/` is a comment in
   `CLIRunner.swift`.
   `ConfigurationModel.runCommand()` passes `skipSources: []`
   unconditionally, so **every run adopts whatever it overlaps in place**,
   with no Skip/Replace choice offered. The README and
   `DECISIONS.md` both describe the sheet as shipped; it is not. The CLI side
   is ready for it.

2. **Nothing ever sets `intended_datetime_original`.**
   `roll_sequence.intended_times()` is fully implemented and tested but is
   **never called by production code**. `metadata.roll_capture_date` and a
   negative's `capture_time.date_override` have no CLI write path at all
   (the Metadata tab shows all three read-only). Consequently no negative
   is ever dirty in normal use, and the Metadata tab's Apply button is
   effectively unreachable — the only thing that ever writes an intended
   time is `_maybe_reapply_metadata`, propagating an already-applied value
   across a re-stitch. Wiring this up means: a CLI command (`roll
   set-date`, by analogy with `roll rename`), a call to `intended_times()`
   to populate the field, and Metadata-tab controls.

3. **`rebate_deviation_px` is always `null`.** See §8.1. The rebate
   *detector* exists (§7.1) and excludes rebate from the meters; the
   deviation field's retirement via that mask is punchlisted.

5. **The app can never keep a work directory.** `CLICommand.run` accepts a
   `work:` parameter, but `ConfigurationModel.runCommand()` never supplies one,
   so `run_full` always creates and then deletes `<roll>/.work/<run_id>/`. The
   README's re-stitch instructions ("a run started with `--work`") therefore
   describe a CLI-only workflow: to re-stitch from the app you must have
   produced a work directory by invoking the CLI yourself.

6. **`stitch --overwrite` is accepted and ignored** — intentionally, but it is
   still dead surface area.

7. **`export` is pixels-plus-density-tag.** The exported TIFF replays the
   pixels straight through and carries the density profile and the
   `normalization` block in its `ImageDescription` (§7.1) — but no EXIF:
   full EXIF carry-over from the published TIFF remains the
   deliberately-deferred next step, not half-done. What is still undecided
   is the profile the eventual **positive** export carries — that is the
   first colourimetric question that belongs to the export work itself.
   Rotation is also the **only** edit operation: the ops-log shape (`op` +
   `params`) is general, but `rotate` is the single op implemented.

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
- The library database migrates itself to head on every engine open
  (`library/db.py`), so schema changes are a new Alembic revision under
  `library/migrations/versions/`. Tests point `SCANNY_BOY_LIBRARY_DB` at a
  per-test file.
- Work is planned chunk-by-chunk; each chunk is one branch and one PR merged
  in order, `main` is protected, and CI must pass.
