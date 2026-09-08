# Mac app <-> CLI contract

The Swift app invokes the packaged `scanny-boy` binary as a subprocess. This
document is the source of truth for that interface; update it whenever the
CLI's args or output shape change, and update `schema.json` alongside it.

This file summarises `docs/IMPLEMENTATION_PLAN.md` section 4 for Phase 1,
`docs/PHASE2_IMPLEMENTATION_PLAN.md` section 3 for Phase 2, and
`docs/PHASE3_IMPLEMENTATION_PLAN.md` section 3.5 for Phase 3. If this file
and any plan ever disagree, the plan is authoritative.

Protocol version 19 keeps every event's shape and adds **the `crop` op**
(docs/CROP_PLAN.md): a new `edit crop` subcommand records a tilted crop
window per negative — a state op in the same family as `tone`/`color`/
`spots`, stored in published-TIFF pixels with `{"canvas", "x", "y", "w",
"h", "tilt_deg", "preset"}` params (`tilt_deg` counter-clockwise as
displayed, ±45 at the widest; the app's slider is ±10; `--reset` clears).
The op is nondestructive exactly like every other edit: the published TIFF
is never touched, previews fold the window in (the replay applies it right
after the spot repair, before the mirror and rotations), and the export
bakes it — the exported JXL contains only the window's pixels, and its
XMP provenance records the window. `edit_recorded` gains a `crop` field on
*every* edit confirmation — a display-space report
(`{width, height, tilt_deg, preset}` for a live crop, `null` otherwise) —
and `roll info`'s per-negative block gains the same field. A crop recorded
against a canvas a re-stitch has replaced degrades to `null` everywhere,
the spots-canvas rule. No new codes.

Protocol version 18 keeps every event's shape and adds **the resident
helper** (docs/OPTIMIZATION.md §2): a new `serve` command that reads
newline-delimited JSON *requests* on stdin — one object per line,
`{"request_id": "<uuid>", "command": [<the argv a one-shot invocation
would have received>]}` — and answers on this same stdout event stream.
Every event emitted while a served request is in flight gains an optional
`request_id` string field, and each request ends with a `finished` event
carrying its `request_id` and the exit status the one-shot CLI would have
returned. A request whose body is instead `{"request_id": "<uuid>",
"cancel": true}` asks the daemon to cancel **that one request** in band;
SIGTERM to the daemon keeps its one-shot meaning (shut down: cancel every
live token, answer the rest as cancelled at exit status 143, exit 0). The
decoder must treat `request_id` as optional: one-shot invocations, which
have no daemon to scope an event to, continue to emit events without it.
The app's ordinary SIGTERM-cancels-everything semantics belong only to the
one-shot path; a served request's cancellation is its own.

The same bump adds **the film-extent pass**
(docs/BLACK_POINT_REFINEMENT.md): the stitch stage now locates the film's
own extent on each negative and insets the meters' analysis region inside
it, so a negative carrier photographed beyond the film edge no longer owns
the black point. The published TIFF is never cropped — the pass restricts
the meters only, exactly like the analysis region it refines. Each
negative's `normalization` block gains an optional `film_extent` object:
`{detected, valley, lobe_fraction, mask_fraction, insets (4 integers: top,
bottom, left, right in **grid cells** — `analysis_block_px` in
processing_params multiplies to canvas pixels, while `analysis_rect` beside
it is already in canvas pixels), region_fraction, convergence_steps,
rebate_agrees (nullable boolean, recorded and read by nothing)}`. The same
bump declares the previously undeclared `opaque` block beside it. The same
block's `normalize` processing constants gain the `REBATE_*`, `OPAQUE_*` and
`FILM_EXTENT_*` families and `format_version` bumps 4 → 5, so a roll
stitched before this change refuses new runs with `ROLL_INVARIANT_MISMATCH`
(the upgrade shim absorbs the new keys for comparison; the recorded bounds
of a v4 roll are not comparable with a v5 one). Two new codes, both riding
the warning event channel: `NORMALIZE_FILM_EXTENT_WITHHELD` (informational:
a non-film border band was withheld from the metering; the message names
the four insets in canvas pixels) and `NORMALIZE_FILM_EXTENT_EXCESSIVE`
(warning: the withheld band kept less than half the analysis region — the
frame is unusual and the user should look at it). The Swift results view
labels the first informationally; neither code fails anything.

Protocol version 16 keeps version 15's roll model and adds **named grid
configuration presets**: the `grid create` / `grid list` / `grid delete`
command family and the `grid_created`, `grid_list`, and `grid_deleted`
events. Each preset is a user label for an `across` x `down` shape the app
picks when adding scans. Two new codes: `GRID_PROFILE_NOT_FOUND` and
`GRID_PROFILE_EXISTS`.

Protocol version 14 keeps version 13's roll model — the film-base
reference and spotting both stay as protocol 13 shipped them — and adds
**cast removal's second tie and auto solve** (docs/CAST_REMOVAL_PLAN.md):
`edit color` gains `--cast-removal-highlights V` (the highlight-end tie
strength, 0..1, 0 neutral — with a highlight reference recorded in the
negative's `normalization` block and a non-zero strength, the per-channel
tie becomes a genuine affine, gain *and* offset) and `--auto-cast` (solve
the global filtration from the negative's recorded neutral estimate;
exclusive with `--reset` and with an explicit `--cyan`, `--magenta` or
`--yellow`, which it would overwrite). The auto reads a stitch-time meter
(`neutral_residual` in the `normalization` block), so it is unavailable on
rolls stitched by an older build — that absence warns
`TONE_METERING_UNAVAILABLE` and records the state unchanged, as does a
non-zero tie strength on a negative with no reference for the end it
drives. `roll info` gains the derived `color_cast_removal_highlights`
field beside the other `color_*` fields. Global and regional CMY are now
**mean-removed**, so filtration changes hue and never the display's
channel mean — this changes how already-recorded colour ops render, which
is accepted because the op is preview-only. No new codes.

Protocol version 13 keeps version 12's roll model and adds **the film-base
reference** (docs/REBATE_ANCHORING.md).

**The film-base reference**: a new `roll set-base-frame --roll DIR --frame
FILE [--flatfield PROFILE_ID]` command attaches one measured per-roll
film-base reference — the per-channel median log density inside the film
rebate of one dedicated reference frame, shot once per roll showing as much
clear rebate as possible, exposed about two stops darker than the roll's
scans. The measurement is exposure-invariant (only per-channel deviations
from the median are consumed), so the base frame's exposure never has to
match the roll's; it does have to be the same film, the same light source,
the same camera body, and the same flat-field profile. The block is a new
optional top-level `film_base` object on the roll manifest, reported by
`roll info` verbatim and by `probe --roll` (as `film_base` on
`probe_result`) so the app can gate Convert without starting a run:
`{density (3-array), locked_at, attached_at, source_name, source_sha256,
flat_field_profile_id, camera_model, chosen_index, populations (array of
{density, luma, area_fraction, cells, spread}, thinnest first),
clipped_fractions, grid_cells, measure_version}`. `density` is always a
3-array, even on a monochrome roll (the block is provenance there, not
consumed). The state machine: ABSENT (`film_base` null) → ATTACHED
(`locked_at` null) → LOCKED (`locked_at` set). `set-base-frame` attaches or
replaces freely while unlocked and emits one `base_frame_set` event
(`roll_id`, `source_name`, `density`, `area_fraction`, `population_count`,
`locked` — always false); a locked roll refuses with `FILM_BASE_LOCKED`;
a gate failure emits the error and changes nothing on disk. The first
negative published against the reference sets `locked_at` in the same
manifest write; a run that fails before publishing anything leaves it null.
`run`/`stitch` on an ABSENT roll fail `FILM_BASE_REQUIRED` before any pixel
work. Ten new codes: `FILM_BASE_REQUIRED` (error), `FILM_BASE_LOCKED`
(error), `FILM_BASE_NOT_FOUND` (error), `FILM_BASE_TOO_SMALL` (error),
`FILM_BASE_CLIPPED` (error), `FILM_BASE_TOO_DARK` (error),
`FILM_BASE_AMBIGUOUS` (error), `ROLL_PREDATES_FILM_BASE` (error),
`FILM_BASE_CAMERA_CONFLICT` (warning), `FILM_BASE_FLATFIELD_CONFLICT`
(warning). Each negative's `normalization` block gains an optional
`base_check` object — `{level_offset, shape_residual}` — recorded whenever
the roll has a locked anchor and the negative's own rebate detector fired
unclipped, and read by nothing (§6). It also gains `highlight_refs` (the
dense end's same-pixel neutral reference, or `null` when the band held no
trustworthy neutrals) and `neutral_residual` (the frame's `(R-G, B-G)`
offset in normalized units, or `null` when there was no estimate) — both
recorded by the stitch stage and read only by `--auto-cast`
(docs/CAST_REMOVAL_PLAN.md §3). Roll manifest format version bumps
**7 → 8**: rolls stitched before this feature stay readable, editable and
exportable, but cannot take new negatives or a base frame
(`ROLL_PREDATES_FILM_BASE`). There is no migration.

The same protocol 13 also adds **spotting** (docs/SPOTTING_PLAN.md,
merged from origin/main).

**The spotting feature**: dust, hairs, water spots and scratches are
detected, reviewed, and repaired — the published TIFF is never touched, and
no pixel anywhere changes until an explicit repair is switched on. Three
new commands join the `edit` family: `edit detect-spots` takes a
`--negative` selection (repeatable) and an optional `--sensitivity 0..1`
(default 0.5, most-conservative 0.0), runs the detector over each
negative's published TIFF, and records one `spots` op per negative — a
state op, coalesced in place like `tone` and `color`, carrying the spots'
exact RLE masks in **published-TIFF pixels**. Re-detecting preserves
rejections made near the same coordinates and preserves an already-on
repair switch; when the detector finds more than 500 spots it keeps the
highest-scoring 500 and warns `SPOT_LIMIT_REACHED` (the remedy is a lower
`--sensitivity`). `edit spots` takes **exactly one** `--negative` and
records the review: `--reject N` / `--accept N` (repeatable, combinable, by
id — an unknown id fails `INVALID_EDIT` naming it), `--repair` /
`--no-repair` (the whole-negative switch), or `--clear` (empty set, repair
off). `edit list-spots` is a pure query: nothing recorded, no pixels
touched. All three emit `spots_reported` — and here is the rule the app
must not break: **every reported spot rect is display space, already
transformed by the net rotation/flip/fine angle; the app never converts
coordinates, rejects by `id` only, and never sees an RLE mask.** A spot set
recorded against a canvas a re-stitch has replaced is *stale*: it repairs
nothing, draws no markers, reports an empty list with a `SPOTS_STALE`
warning, and needs re-detecting. `roll info` gains a per-negative `spots`
**summary** (not the list): `{detector_version, sensitivity, repair, stale,
count, rejected}`, `null` for a negative with no spot set, counts zeroed
when stale. The export applies a live repair (before any geometry) and
records it in the XMP provenance's `rendered.spots`:
`{detector_version, sensitivity, repaired}`, `null` when none.

Protocol version 12 keeps version 11's roll model and adds **the preview
colour adjustment**.

**The preview's colour adjustment** (docs/COLOR_PLAN.md): a new `edit color`
command records white balance (global, shadow, and highlight CMY), cast
removal, dye separation, and separation damping as a `color` op in the
negative's ops log — preview-only, coalesced like `tone`, ignored by export.
Unlike `edit tone`, unspecified flags take the negative's currently recorded
value (partial updates). `--temperature` is a Kelvin lever over the named
region's magenta and yellow (mutually exclusive with that region's
`--magenta`); `--region` defaults to `global`. `roll info` reports twelve
derived `color_*` fields plus `color_temperature` (nominal Kelvin from
global M/Y) per negative, and `film_kind` on the roll. Colour edits are
refused on a monochrome roll except `--reset`.

Protocol version 11 keeps version 10's roll model and adds four features.

**The positive/negative display toggle** for the Edit tab's preview: `edit
render-region` gains `--mode positive|negative`, and a new `edit
render-preview` command renders a negative's whole display image — the ops
log's net transform folded in, downscaled like the cached preview — into a
caller-named path, emitting `preview_rendered` with the written PNG's pixel
dimensions. `"positive"` (the default) is the inverted look the cached
preview holds, with the tone adjustment composed in; `"negative"` is the
un-inverted density view — the published TIFF's own appearance — which the
tone adjustment never reaches (grading is a positive-view judgement aid,
and a graded density is not the density). Both are pure rendering queries:
nothing is recorded, the published TIFF is never modified. The managed,
on-disk preview stays a positive in every mode.

**Monochrome film support** (docs/MONOCHROME_PLAN.md): `roll init` requires
`--film-kind {colour,monochrome}` — the user chooses once at roll creation.
`"colour"` is the path for colour negatives and chromogenic B&W (XP2, BW400CN,
stained pyro); `"monochrome"` is silver B&W only. `run` and `stitch` read the
frozen `film.kind` from the roll manifest; an unseeded roll with no `film`
block fails `FILM_KIND_REQUIRED`. Legacy rolls that already have runs but no
`film` block are treated as frozen colour. A monochrome roll's published
TIFFs are single-channel (`photometric=minisblack`), tagged with
`ScannyBoy-Density-Grey-v1.icc` instead of the colour density profile; every
per-channel roll-manifest field (`floors`, `ceils`, `shadow_refs`,
`observed_min`/`_max`, the headroom-clip fractions, `unclamped_floors`/`_ceils`)
carries one entry instead of three, and `rebate.base_density` is `null`, a
1-array, or a 3-array. The per-frame `gain` array is unaffected and stays
3-wide even on a monochrome roll's negatives, since the photometric solve that
produces it still runs in linear light on three channels. `roll info` reports
`film_kind` (the `kind` field only).

**The colour-managed export** (docs/EXPORT_PLAN.md): `export` renders each
negative as a positive in Adobe RGB (1998)-compatible colour — the
negative's recorded tone op baked in — written as a 16-bit lossless JPEG
XL with its ICC profile embedded and Exif/XMP boxes at encode time. A mono
roll's export is single-channel with the grey export profile and no colour
matrix. The roll manifest gains an optional top-level `camera_color` block
(the capturing body's colour matrix, written by the stitch stage's first
run and frozen thereafter), the work manifest's curated block gains
`rgb_xyz_matrix`/`camera_model`, and three event codes: `CAMERA_MATRIX_MISSING`
(error), `CAMERA_MATRIX_CONFLICT` (warning), `JXL_ENCODER_UNAVAILABLE`
(error). `METADATA_WRITE_FAILED` becomes **reserved**: protocol 11
removes the exporter's raiser (metadata is built at encode time; there is
no second write to fail), but the code stays in the shipped protocol —
`apply-metadata` still raises it — and removing an event code is a
breaking change for zero benefit.

**Extended preview tone adjustment** (docs/DENSITY_PLAN.md): seven new curve
controls on `edit tone` (print density, zone density, toe/shoulder and
their widths), `--auto-density` and `--auto-grade` (solve once from the
negative's recorded normalization and write the value — not a persistent
mode), the matching seven `tone_*` derived fields on `roll info`'s
negatives, and the `TONE_METERING_UNAVAILABLE` warning code.

Protocol version 10 keeps version 9's roll model and adds two features.

**2D grid stitching** (docs/GRID_STITCH_PLAN.md): `probe`, `prepare`, and
`run` accept `--grid AxD` (e.g. `--grid 3x2`) — five across, two down —
naming the 2D arrangement of one negative's scans, mutually exclusive with
`--per-negative`. Exactly one of the two flags is required on `prepare` and
`run`, and on `probe` when `--files` is given; omitting both is a usage
error naming both flags. A strip is the `down == 1` case:
`--per-negative N` and `--grid Nx1` declare the same batch. The batch shape
is constrained by **`min(across, down) <= 2`**: every cell of the grid must
show film rebate, which only holds when every cell touches the grid's outer
boundary — a 3x3 or larger square cannot satisfy it. This rule is stated
here once and referenced elsewhere. `across * down` remains capped at 12.
Note that **`2x5` is legal and CLI-only**: the Mac app's Down picker caps at
2, so `--grid 2x5` is reachable only on the command line. A well-formed
grid that breaks a rule fails with `INVALID_GRID`; a malformed one
(anything not of the `AxD` form) is a usage error.

**The preview's nondestructive tone adjustment**: the new `edit tone`
command records an ISO-R paper grade (`--grade`, 50–180) plus a midtone
snap (`--snap`, −0.5…0.5) — or `--reset` — as a `tone` op in the negative's
ops log (a state, not a transform: the latest op wins and a trailing one
coalesces in place). The published TIFF is never touched; the preview's
display encode composes the curve in, and the export's render bakes the
same curve into the exported pixels (docs/EXPORT_PLAN.md §4.6). `roll
info` reports the net tone per negative as `tone_grade_r`/`tone_snap_gamma`
(both `null` when flat).

Roll manifest format version 7 keeps version 6's shape and adds one
optional per-negative field: `rectification`, the fitted rig-tilt
rectification (docs/RECTIFICATION_PLAN.md section 7) — `l` (two numbers,
1/px, acting on coordinates centred at `centre`), `centre`, `frame_size`,
the fit's `rms_before_px`/`rms_after_px`/`relative_improvement` diagnostics,
and `pair_count`. It is `null` when the fit was rejected, the negative
failed before it ran, or the build predates the field. The stitch-params
record also changed — the feather is recorded as `axis-separable` for
every roll, strip or grid, three new grid threshold keys landed, and
`stitch_params` gains `rectification_model` (always `"global-2-param"`)
plus the three rectification gate constants — and it remains a roll
invariant, so **every roll written by an earlier build refuses new runs
with `ROLL_INVARIANT_MISMATCH`**; there is no migration, and the remedy is
to delete the old roll folders.

Protocol version 9 keeps version 8's roll model and adds **1:1 region
rendering** for the app's 100% zoom: the new `edit render-region` command
renders one display-space region of a negative's published TIFF at 1:1 — the
ops log's net rotation folded in, the same inverted display encode as the
cached preview, no downscale — into a caller-named path as a lossless PNG,
emitting `region_rendered` with the rect actually rendered (post-clamp). It
is a pure rendering query: nothing is recorded, the published TIFF is never
modified. Only the TIFF strips the region overlaps are decoded. It also
added the extended-metadata editing feature (the `metadata` command family
— `metadata_updated`, `metadata_values`, the `INVALID_METADATA` code — and
the roll/negative extended-metadata fields in the roll manifest).

Protocol version 8 keeps version 7's roll model and makes two changes:
it adds a **per-frame scale** to the layout solve
(docs/STITCH_QUALITY_PLAN.md section 2) and **scan normalization**
(docs/DECISIONS.md, "Normalization decisions"), renaming the prepare stage
and adding a per-negative normalization record. The layout change: the
global layout is now a similarity (rotation, translation, and one isotropic
scale per frame) rather than a rigid transform, because film does not sit
at a constant height above the stage from frame to frame. Each frame record
gains `scale` (positive number; geometric mean 1 across a negative's
frames, the same gauge convention as `gain`). The pairwise fit and its
acceptance gates (`rms_residual_px`, `scale_drift`) are unchanged — they
still measure the scale-1 rigid fit; only the global layout's placement
model changed. The normalization change: stage 1 is renamed — the `convert`
subcommand is now `prepare` (the UI's "Convert" is reserved, unambiguously,
for the whole `run`), and `progress` gains stage value `prepare` in place
of `convert`. The stitch stage emits a new `normalize` step between `blend`
and `write_stitched`, and the published TIFF is a normalized log-density
working intermediate tagged with a second ICC profile — the roll manifest
gains `published_icc_profile` and `check_roll_invariants` compares it
alongside the intermediates' linear profile. Three new codes:
`SCAN_CLIPPED` (a warning, per frame in the prepare stage),
`NORMALIZE_DEGENERATE_BOUNDS`, and `NORMALIZE_HEADROOM_CLIPPED` (a
warning). The work manifest's sources gain per-frame
`scan_clip_fractions`; the roll manifest's negatives gain a `normalization`
block and `normalized_fill`, its runs a `normalization_aggregate`, and its
sources `scan_clip_fractions`. Every existing roll refuses new runs with
`ROLL_INVARIANT_MISMATCH` (the processing-params invariant now carries the
`normalize` bucket); the remedy is a new roll.

Protocol version 7 keeps version 6's roll model and adds **geometric
calibration** (docs/GEOMETRIC_PLAN.md): `flatfield create` gains
`--calibration FILE [FILE ...]`, and a profile becomes the complete optical
description of one rig configuration — gain map, radial distortion, and
lateral chromatic aberration fitted from ChArUco frames. The distortion is
applied inside the stitch warp (registration and compositing work in
undistorted pixels); the CA is applied at decode in `"scale"` mode (rawpy's
`chromatic_aberration` scales) or at composite in `"maps"` mode (per-channel
maps). A profile's geometry is only valid for the frame dimensions it was
fitted at — `GEOMETRY_FRAME_SIZE_MISMATCH` fails the run before anything is
written. The `--flatfield` flag now names a whole calibration profile; the
name is historical and unchanged.

Protocol version 6 kept version 5's roll model and added **flat-field
correction**: gain maps measured once from a reference shot of the bare
light source (`.NEF` only), stored beside the library database and managed
through a new `flatfield` command family (`create`, `list`, `delete`). A
profile chosen with `--flatfield` on `convert`, `run`, or `probe` is applied
per frame in the convert stage and folded into `processing_params` under
`flat_field`. The profile is not a roll invariant — it is excluded from the
`processing_params`/`stitch_params` comparison a roll's later runs are held
to, so different runs into the same roll may each choose a different
profile, or none. The key is absent, not null, when no profile is given, so
pre-flat-field rolls still accept no-profile runs.

Protocol version 5 kept version 4's roll model and added **nondestructive
editing**: each roll's durable record moved from the roll folder's
`scanny-boy-roll.json` into a library SQLite database (one row per roll,
negative, run, and source, plus an ordered per-negative **edits ops log**),
the CLI renders each negative's preview, and `edit rotate` records a
rotation without ever touching a published TIFF. `roll info`'s payload keeps
the roll-manifest shape; each negative additionally carries
`preview_path` (the CLI-rendered preview) and `rotation_quarter_turns` +
`flipped_horizontally` + `fine_rotation_deg` (the ops log's net effect,
derived rather than stored). A client that only
understands an earlier protocol version must reject a newer stream rather
than guess at the new fields.

## Invocation

```text
scanny-boy roll init   --library DIR --name NAME --film-kind {colour,monochrome}
scanny-boy roll list   --library DIR
scanny-boy roll info   --roll DIR
scanny-boy roll rename --roll DIR --name NAME
scanny-boy roll delete --roll DIR
scanny-boy roll set-base-frame --roll DIR --frame FILE [--flatfield PROFILE_ID]

scanny-boy probe      --input DIR [--files FILE [FILE ...]] [--per-negative N | --grid AxD] [--roll DIR]
                      [--flatfield ID]

scanny-boy prepare    --input DIR --files FILE [FILE ...] --out DIR
                      [--per-negative N | --grid AxD]
                      [--jobs N] [--overwrite] [--flatfield ID]

scanny-boy stitch     --work DIR --roll DIR [--jobs N] [--overwrite] [--allow-partial]
                      [--negatives ID ...] [--flatfield ID]

scanny-boy run        --input DIR --files FILE [FILE ...] --roll DIR
                      [--per-negative N | --grid AxD]
                      [--jobs N] [--skip-sources FILE ...] [--work DIR] [--flatfield ID]

scanny-boy apply-metadata --roll DIR

scanny-boy metadata set    --roll DIR --payload JSON
scanny-boy metadata values --field FIELD

scanny-boy edit rotate --roll DIR --negative ID [ID ...] --direction cw|ccw
scanny-boy edit flip   --roll DIR --negative ID [ID ...]
scanny-boy edit tone   --roll DIR --negative ID [ID ...] (--grade R | --auto-grade) --snap G [--density D | --auto-density] [--shadow-density D] [--highlight-density D] [--toe T] [--toe-width W] [--shoulder S] [--shoulder-width W] | --reset
scanny-boy edit color  --roll DIR --negative ID [ID ...] [--cyan V] [--magenta V] [--yellow V] [--shadow-cyan V] [--shadow-magenta V] [--shadow-yellow V] [--highlight-cyan V] [--highlight-magenta V] [--highlight-yellow V] [--temperature K [--region {global,shadows,highlights}]] [--cast-removal V] [--cast-removal-highlights V] [--auto-cast] [--dye-separation V] [--separation-damping V] | --reset
scanny-boy edit delete --roll DIR --negative ID [ID ...]
scanny-boy edit render-region --roll DIR --negative ID --x PX --y PX --width PX --height PX --output PATH
                              [--mode positive|negative]
scanny-boy edit render-preview --roll DIR --negative ID --output PATH
                               [--mode positive|negative]
scanny-boy edit detect-spots   --roll DIR --negative ID [ID ...]
                               [--sensitivity S]
scanny-boy edit spots          --roll DIR --negative ID [--reject N ...]
                               [--accept N ...] [--repair | --no-repair] [--clear]
scanny-boy edit list-spots     --roll DIR --negative ID

scanny-boy export      --roll DIR --output DIR [--negatives ID ...]
                       [--downsample {none,6048,9072}]

scanny-boy flatfield create --reference FILE --name NAME
                            [--calibration FILE [FILE ...]]
scanny-boy flatfield list
scanny-boy flatfield delete --profile ID

scanny-boy grid create --name NAME --across N --down N
scanny-boy grid list
scanny-boy grid delete --profile ID
```

`--roll` replaces `--out` on `stitch` and `run`. `prepare` keeps `--out`,
because it still writes a work directory rather than a roll.

`--film-date` is removed from every command. Synthetic capture times are
assigned in the metadata stage, not at prepare time.

`--overwrite` is removed from `run`. Re-running over sources already in the
roll adopts the covered negative in place: its `negative_id` and output name
are kept, its record is updated with the new run's data, and its TIFF is
replaced atomically. `--skip-sources` remains the way to redo a scan without
touching an existing negative.

`probe` is read-only and works at two levels of detail:

- **`--input` alone** returns the catalogue in canonical order, plus any
  sorting warnings. Swift calls this first, because it cannot name a
  selection before it knows the order. Swift never sorts files itself.
- **`--input` with `--files`** additionally validates the selection: the
  uninterrupted-range check, grouping, metadata consistency, and whether
  conversion may start.

`--out` may be given to `probe` alongside `--files` to include output-folder
validation and the overwrite-conflict preview (Phase 1/2 convert path).

`--roll` may be given to `probe` alongside `--files` to include roll-aware
validation: roll-invariant checks and an overlap preview for sources already
present in the roll.

`prepare` repeats all important validation. It does not trust an earlier
probe result.

`--files` takes filenames relative to `--input`, not absolute paths. Reject a
selection above 5000 files with a usage error rather than letting the
operating system truncate the argument list.

`--skip-sources` names filenames, relative to `--input`, to exclude from a
`run`. Excluded files are removed from the selection **before** grouping, so
a skip must remove a whole group's worth or the run fails
`NON_CONTIGUOUS_SELECTION`.

`--negatives` on `stitch` restricts a re-stitch to named `negative_id`s.

`--downsample` on `export` reduces each export to the chosen long edge
(`none` is the default and keeps full resolution). The resize happens
inside the render, on the *linear-light* values after the gamut clip and
before the display re-encode — a true Lanczos3 resample
(`scanny_boy.resample`), not a resample of the finished gamma-encoded
pixels, which would darken midtones along high-contrast edges. It never
upscales: an image already at or below the target is skipped silently,
and what was applied is recorded in the XMP's `scannyboy:provenance`
`rendered.downsample` block. An image smaller than the target exports
at full resolution; no warning, no error.

`--flatfield` on `convert`, `run`, `stitch`, and `probe` names a calibration
profile built by `flatfield create` — a profile may carry a gain map only,
or a gain map plus a distortion fit and a chromatic aberration fit (the
flag's name is historical; it names the whole profile). The gain-map
correction is multiplicative gain only, applied per frame immediately after
RAW decode. A profile whose CA mode is `"scale"` additionally decodes every
frame with rawpy's `chromatic_aberration` scales. An unknown profile id
fails with `FLATFIELD_PROFILE_NOT_FOUND` before anything is written; a
profile whose geometry was fitted at other frame dimensions fails with
`GEOMETRY_FRAME_SIZE_MISMATCH`. A frame whose correction pushes more than
0.1% of its pixels past full scale warns with `FLATFIELD_HIGHLIGHT_CLIPPED`;
a profile whose reference aspect ratio differs from the frames' by more than
1% warns with `FLATFIELD_ASPECT_MISMATCH` but proceeds.

`--flatfield` is optional on `stitch`. A roll whose `stitch_params` carry a
`geometry` bucket (because its first stitch ran with a calibrated profile)
refuses a `stitch` without the same profile:
`ROLL_INVARIANT_MISMATCH`, through the existing check. The bucket is absent,
not null, when the profile carries no geometry.

`flatfield create` decodes `--reference` (a `.NEF` of the bare light source
with no negative in the holder), builds and stores the gain map, and inserts
the profile; it emits `flatfield_created` carrying the profile (`profile_id`,
`name`, `reference_width`, `reference_height`, `source_path`,
`created_at`, `board_key`, `has_geometry`, `chromatic_aberration_mode`,
`calibration_report`). With `--calibration FILE [FILE ...]` (absolute paths,
at least 12 ChArUco board frames), the profile additionally carries the
distortion fit, the CA fit, and the human-readable `calibration_report`; a
fit that fails its acceptance gates is recorded as rejected in the report
and left out of the profile, with a `warning`. Fewer than 12 usable frames
fails `GEOMETRY_INSUFFICIENT_FRAMES`; fewer than 16 warns
`GEOMETRY_FEW_FRAMES`. The command runs for minutes when calibrating and
reports `flatfield_progress` events carrying `phase` (`detect`, `fit`,
`chromatic`, or `reference`), `completed`, and `total`. A duplicate name
fails with `FLATFIELD_PROFILE_EXISTS`. `flatfield list` emits
`flatfield_list` carrying `profiles`, an array of the same shape.
`flatfield delete --profile ID` refuses with `FLATFIELD_PROFILE_IN_USE` when
any roll's invariants name the profile — in either invariant bucket,
`processing_params.flat_field` or `stitch_params.geometry` — and otherwise
removes the row and the `.npz`, emitting `flatfield_deleted` carrying
`profile_id`. Each command brackets like `roll init`/`roll list` and carries
no `run_id`; none is a pipeline run.

`grid create --name NAME --across N --down N` validates the shape with the
same rules as `--grid AxD` (`INVALID_GRID` when refused), refuses duplicate
names with `GRID_PROFILE_EXISTS`, inserts the preset, and emits
`grid_created` carrying the profile (`profile_id`, `name`, `across`, `down`,
`created_at`). `grid list` emits `grid_list` carrying `profiles`, an array
of the same shape. `grid delete --profile ID` refuses unknown ids with
`GRID_PROFILE_NOT_FOUND`, removes the row, and emits `grid_deleted` carrying
`profile_id`. Each command brackets like `flatfield list` and carries no
`run_id`.

`roll init` creates a folder under `--library` (slug + collision rule) and
registers an empty v5 roll in the library database. It emits `roll_created`
carrying `roll_id`, `roll_name`, and `path`. A roll records no grouping of
its own: `--per-negative`/`--grid` is each stitch batch's choice, so one roll can
hold negatives stitched from different scan counts.

`--grid AxD` (protocol 10) names the 2D arrangement of one negative's
scans: `across` frames left-to-right in capture space, `down`
top-to-bottom, `across * down` scans per negative. It is accepted wherever
`--per-negative` is (`probe` with `--files`, `prepare`, `run`) and is
mutually exclusive with it; a strip is the `down == 1` case, so
`--per-negative N` and `--grid Nx1` declare the same batch and the strip
path is the R=1 case of the grid path, not a separate one. The shape
constraints are the ones stated in the protocol-10 paragraph above:
`min(across, down) <= 2` because every cell must show film rebate, and
`across * down <= 12`. A batch declared with `--grid` records the grid on
its work manifest alongside `shots_per_negative == across * down`, and the
roll manifest's negatives record the declared grid, the solved per-frame
cell assignment, and the solved grid's regularity measures. `stitch` takes
neither flag — it reads the grouping from the work manifest.

`roll list` reports the rolls registered under `--library` from the library
database and emits a single `roll_list` event. A registered roll whose
folder has vanished is reported as `"unreadable"` with `ROLL_NOT_FOUND`
rather than silently disappearing.

`roll info` loads one roll from the library database and emits it as a
`roll_info` event, each negative augmented with `preview_path`,
`rotation_quarter_turns`, `flipped_horizontally`, and `fine_rotation_deg`.
Swift never reads the
library database itself and
never enumerates the library itself — `roll list` and `roll info` are the
only two ways in.

`roll rename` moves the roll's folder to a slug of `--name` and, only after a
successful move, saves the new `roll_name` and folder location to the
library database. It emits
`roll_renamed` carrying `roll_id`, `roll_name`, and `path` (the roll's new
location). It does not enforce "refused while any run is active" — the CLI
is stateless between invocations, so the app checks that itself before
issuing the command.

`roll delete` unregisters the roll from the library database — its runs,
sources, negatives, and edits rows cascade away with it — and unlinks the
negatives' rendered previews, emitting `roll_deleted` carrying `roll_id` and
`path`. It never touches the roll's folder: the app moves that to the Trash
itself (`NSWorkspace.recycle`) and then calls this command, so the next
`roll list` no longer reports the roll. It fails with `ROLL_NOT_FOUND` for
an unregistered roll.

`apply-metadata` writes intended capture times from the roll's record into
published TIFFs. See Phase 3 section 3.8.

`metadata set` applies one metadata payload to the roll's record in the
library database — it never touches a TIFF. The payload is
`{"roll": {field: value}, "negatives": {negative_id: {field: value}}}`:
roll fields are `capture_date` (the roll capture date), `film`, `iso`
(roll-only — no negative columns, no per-image override), plus `city`,
`state`, `camera`, `lens`, `caption`; negative fields are the same five
shared extended-metadata fields plus `capture_date` (the negative's date
override). A key that is absent leaves the field untouched; a key present
with `null` or `""` clears it (a cleared negative field then inherits the
roll-level fallback; the extended metadata uses live-fallback semantics,
never copying roll values onto negatives). Every capture-date change
recomputes each negative's intended capture time by the rank formula (noon
+ rank − 1 seconds on the negative's effective date, ranked within that
date in roll order), so the stored intent always preserves roll order.
Non-empty `film`, `iso`, `city`, `state`, `camera`, and `lens` values are
remembered in the metadata-values catalog (`caption` never is). It emits
one `metadata_updated` event carrying the updated `manifest`, and fails with
`INVALID_METADATA` for an unknown field, a non-`YYYY-MM-DD` date, or a
malformed payload, `ROLL_NOT_FOUND` for an unregistered roll, and
`NEGATIVE_NOT_FOUND` for an unknown negative id — the whole payload is
validated before anything is written.

`metadata values` lists the catalog of previously-entered values for one
field (`film`, `iso`, `city`, `state`, `camera`, or `lens`), most-recently-used first, as
a `metadata_values` event. It fails with `INVALID_METADATA` for any other
field.

`edit rotate` records a 90-degree rotation of one or more negatives —
`cw` clockwise,
`ccw` counter-clockwise — by appending to each negative's ordered edits ops
log in the library database. The published TIFF is never modified. It
regenerates each CLI-rendered preview (a lossless PNG under Application
Support, path recorded on the negative) and emits `edit_recorded` per
negative carrying
`negative_id`, the `edit` row (`id`, `negative_id`, `position`, `op`,
`params`, `created_at`), `rotation_quarter_turns` (the ops log's net effect,
0–3), `flipped_horizontally` (whether the ops log's net transform includes a
horizontal mirror), `fine_rotation_deg` (the ops log's net clockwise fine
rotation in degrees, from the auto-seeded `rotate_fine` op below composed
with any user ops), `crop` (the net crop as a display-space report —
`{width, height, tilt_deg, preset}` — or `null`; carried by every
`edit_recorded`, protocol 19), and `preview_path`. It fails with
`INVALID_EDIT` for an
unknown direction, `ROLL_NOT_FOUND` for an unregistered roll, and
`NEGATIVE_NOT_FOUND` for an unknown or unstitched negative — the whole
selection is validated before any op is appended, so a batch either records
or fails without partial effects.

`edit render-region` renders one display-space region of a negative's
published TIFF at 1:1 — the ops log's net transform (rotation, flip, and
the auto-seeded fine angle) folded in, the 8-bit display encode `--mode`
names (protocol 11): `"positive"` — the default — is the inverted look the
cached preview holds, with the tone adjustment composed in; `"negative"`
is the un-inverted density view, which no tone reaches — no
downscale — into `--output` as a lossless PNG. Display space is the
published TIFF's pixels with the net transform applied: what `roll info`'s
`preview_path` shows, and the coordinate space the app's 100% zoom works
in. The region is clamped against the image bounds, and the emitted
`region_rendered` carries `negative_id`, `path`, and the rect actually
rendered (`x`, `y`, `width`, `height`, post-clamp). A pure rendering query:
nothing is recorded, the published TIFF and the ops log are untouched. It
fails with `INVALID_EDIT` for a non-positive size or an empty region
against the bounds, `ROLL_NOT_FOUND` for an unregistered roll, and
`NEGATIVE_NOT_FOUND` for an unknown or unstitched negative.

`edit render-preview` renders a negative's whole display image — the same
net transform folded in, downscaled to the cached preview's own longest
edge — in the display encode `--mode` names (protocol 11; see the
positive/negative toggle paragraph at the top) into `--output` as a
lossless PNG, emitting `preview_rendered` with `negative_id`, `path`, and
the written PNG's `width`/`height`. The pure-query backing of the app's
positive/negative toggle: nothing is recorded, the published TIFF and the
ops log are untouched. It fails with the same roll/negative codes as
`edit render-region`, and with `INVALID_EDIT` for an unknown mode.

`edit flip` records a horizontal mirror of one or more negatives — a flip of
the pixels as they currently render, *after* any recorded rotations — by
appending a `flip` op to each negative's ordered edits ops log. Like
`edit rotate` it never touches the published TIFF; it regenerates the
previews and emits `edit_recorded` per negative with the same fields. The
ops log's net transform does not collapse to a rotation alone: a flip and a
rotation do not commute, so consumers replay the log into a
`(rotation_quarter_turns, flipped_horizontally, fine_rotation_deg)` triple.
It fails with the
same codes as `edit rotate`.

`edit crop` records a tilted crop window for exactly one negative —
`--x/--y/--width/--height` name the rect in display space (the image as it
currently renders, live crop included), `--tilt` is the window's
counter-clockwise tilt as displayed (−45…45, default 0), and `--preset` is
an optional label for the ratio preset the app constrained the rect with;
`--reset` clears the crop. The op is a state, not a transform — the latest
`crop` op wins — and it stores the **fully-composed** window in
published-TIFF pixels: the drawn rect is mapped backwards through the net
state (`previews.display_crop_window_to_tiff`), so a re-crop over an
already-cropped preview composes into one window. The published TIFF is
never touched (the preview folds the window in; the export bakes it, and
the exported file holds only the window's pixels); each preview is
regenerated with the window applied first in the replay, before the mirror
and rotations. `edit_recorded` is emitted for the negative with the net
`crop` report. It fails with `INVALID_EDIT` for a rect smaller than
16×16, one that does not fit the display image, or a tilt beyond ±45, and
with the same roll/negative codes as `edit rotate`. `roll info` reports
the net crop per negative as `crop` (`{width, height, tilt_deg, preset}`,
null when none) — `width`/`height` are the cropped display image's
dimensions, the coordinate space `edit render-region` then works in, and
a crop whose canvas no longer matches the published TIFF (a re-stitch)
reports as `null`. While a live crop exists, spot sets report no markers
(the repair itself is replayed before the crop and still applies).

`edit tone` records a preview tone adjustment for one or more negatives: an
ISO-R paper grade (`--grade`, 50–180, or `--auto-grade` to solve from the
negative's recorded normalization; lower is harder), a midtone snap
(`--snap`, −0.5…0.5), print density (`--density`, 0.0–2.0, neutral 1.0,
higher is denser, or `--auto-density`), zone density offsets
(`--shadow-density` ±0.9, `--highlight-density` ±0.5; positive adds
density), and toe/shoulder shaping (`--toe` / `--shoulder` −1…1,
`--toe-width` / `--shoulder-width` 0.1–5.0, neutral 2.5), or `--reset` for
the flat linear look. `--density` and `--auto-density` are mutually
exclusive, as are `--grade` and `--auto-grade`. Auto flags solve once per
negative and record the computed value — they are not a persistent mode.
The op is a state, not a transform — the latest `tone` op wins, and a
trailing `tone` op is updated in place rather than appended behind. The
published TIFF is never touched (the export's render bakes the curve into
the exported pixels instead); each preview is regenerated from its TIFF
with the tone curve composed into the display encode, and `edit_recorded`
is emitted per negative — the `edit` row's
`params` carry all nine tone keys (all `null` for a reset). It fails with
`INVALID_EDIT` for out-of-range or mismatched parameters, emits
`TONE_METERING_UNAVAILABLE` per negative when auto is requested but
metering is absent, and fails with the same roll/negative codes as
`edit rotate`. `roll info` reports the net tone state per negative as
`tone_grade_r`, `tone_snap_gamma`, `tone_density`, `tone_shadow_density`,
`tone_highlight_density`, `tone_toe`, `tone_toe_width`, `tone_shoulder`,
and `tone_shoulder_width` (all `null` when no adjustment is recorded).

`edit color` records a preview colour adjustment for one or more negatives:
global, shadow, and highlight cyan/magenta/yellow enlarger filtration
(±1.0 each, 0 neutral), cast removal (0.0–1.0, 0 neutral), dye separation
(0.5–1.5, 1.0 neutral), separation damping (0.0–1.0, 0 neutral), or
`--reset` to remove the op. Unlike `edit tone`, **unspecified flags take
the negative's currently recorded value**, not the neutral default — a
single-slider change need not resend all twelve keys. Validation runs on the
merged twelve-key state. `--temperature` (3000–12000 K, 5500 K neutral) is
a Kelvin lever over the named region's magenta and yellow, resolved before
validation via `kelvin_to_wb`; it is mutually exclusive with that region's
`--magenta` (and `--shadow-magenta` / `--highlight-magenta` when
`--region` is `shadows` / `highlights`). `--region` defaults to `global`
and only applies with `--temperature`. Cyan is untouched by temperature.
The op is a state, not a transform — the latest `color` op wins and a
trailing one coalesces in place. The published TIFF and export are
untouched; previews are regenerated with per-channel display LUTs plus
optional per-pixel dye separation. `edit_recorded` is emitted per negative
with all thirteen keys in `params` (`null` for reset). Refused on a
monochrome roll (`INVALID_EDIT`) except `--reset`. Emits
`TONE_METERING_UNAVAILABLE` per negative when a cast-removal strength is
non-zero but the metering that end needs is absent, or when `--auto-cast`
found no recorded neutral estimate (the op is still recorded; the
filtration is left unchanged). `--auto-cast` is exclusive with `--reset`
and with an explicit `--cyan`/`--magenta`/`--yellow`. `roll info`
reports `color_wb_cyan`, `color_wb_magenta`, `color_wb_yellow`,
`color_shadow_cyan`, `color_shadow_magenta`, `color_shadow_yellow`,
`color_highlight_cyan`, `color_highlight_magenta`, `color_highlight_yellow`,
`color_cast_removal`, `color_cast_removal_highlights`,
`color_dye_separation`, `color_separation_damping`,
and derived `color_temperature` (null when no op), plus `film_kind` on the
roll. The auto cast solve reads a stitch-time meter, so it is unavailable
on rolls stitched by an older build.

**Spotting (protocol 13).** `edit detect-spots` runs the defect detector
over each selected negative's published TIFF and records one `spots` op per
negative: `{"detector_version", "sensitivity", "repair", "canvas", "spots"}`.
Each spot entry carries `id` (assigned in raster order, top-left to
bottom-right, stable for a given detection), `kind` (`"blob"` or
`"streak"`), `polarity` (`"dense"` — crud blocking light, the print reads
locally white — or `"thin"` — a scratch through the emulsion, the print
reads locally black), `bbox` `[x, y, width, height]` in **published-TIFF
pixels** (TIFF space: the stored geometry survives later rotations), `rle`
(the component's exact mask, run-length encoded over its own bounding box,
alternating runs starting with a run of zeros, summing to `width * height`),
`area`, `score` (the peak response in robust sigmas), and `rejected`
(*absent means accepted* — the user rejects, never accepts). The op is a
state, not a transform: the latest `spots` op wins and a trailing one
coalesces in place. It is also the only op whose replay **synthesizes**
pixel values — the export and the previews apply a live repair (`repair:
true`) before any geometry. Re-detecting preserves rejections (a new
proposal within 8 px of a previously rejected one is born rejected) and
preserves an on repair switch. `edit spots` reviews one negative's set:
`--reject`/`--accept` by id (unknown id → `INVALID_EDIT` naming the id),
`--repair`/`--no-repair`, or `--clear` (empty set, repair off). `edit
list-spots` is the pure query. All three emit `spots_reported` with
`negative_id`, `detector_version`, `sensitivity`, `repair`, `spots`
(**display-space** rects `{id, kind, polarity, rect, score, rejected}` —
the app never converts coordinates, never rejects by position, and never
sees the `rle`), `found` (before the 500-spot cap), and `preview_path`
(null for `list-spots`). Failures: `INVALID_EDIT` (bad sensitivity,
unknown id, no set, nothing to do), `ROLL_NOT_FOUND`,
`NEGATIVE_NOT_FOUND` (including an unstitched negative). Warnings:
`SPOT_LIMIT_REACHED` (the cap bit; remedy: lower `--sensitivity`) and
`SPOTS_STALE` (a re-stitch changed the canvas; the set repairs nothing and
needs re-detecting — `roll info`'s summary reports `"stale": true` with
zeroed counts). `roll info` reports the per-negative `spots` **summary**
(`{detector_version, sensitivity, repair, stale, count, rejected}`), `null`
for a negative with no spot set — the full list is `edit list-spots`' job.

**Auto-rotation (`rotate_fine`).** At stitch time the CLI estimates the
rebate tilt of each *newly published* negative's composite — the density
discriminator (film base is the thinnest thing on the film) locates the
rebate, and one minimum-area enclosing rectangle of the picture area gives
a single clockwise angle that squares the rebate's frame boundary with the
canvas, splitting the difference across the rebate's not-quite-parallel
edges (`scanny_boy/auto_rotate.py` owns every threshold). When the
estimate survives the clamps, the stitch seeds one `rotate_fine` op —
params `{"angle_deg": number, "source": "auto"}` — to the negative's
ordered edits ops log, exactly like a user edit, and emits `edit_recorded`
for it (with `run_id`). The published TIFF is never rotated by the stitch:
like every edit, the rotation's pixels are transformed only at preview
generation and export, replayed through the same net-state triple. A
re-stitch adopts the existing negative and never re-seeds (no double
rotation); `stitch --no-auto-rotate` (and `run --no-auto-rotate`) turns
the seeding off. A user quarter-turn composes with the fine angle through
the ordinary ops-log replay — a flip negates the fine angle along with the
turn count, because `flip ∘ rot = rot^-1 ∘ flip` for rotations of any
angle.

`edit delete` removes one or more negatives outright, whatever their
status: each record
(and its edits ops log, by cascade) is deleted from the library database,
its published TIFF is unlinked from the roll folder, and its rendered
preview PNG is unlinked from Application Support. The records go first, so
a crash leaves an orphan file rather than a dangling record; a failed
unlink warns with `ORPHAN_FILE_NOT_REMOVED` and never fails the command. It
emits `negative_deleted` per negative carrying `negative_id` and `output`
(the deleted
TIFF's name, null when the negative had never been stitched). The
surviving negatives' `sequence` values are renumbered. Each negative's run
row and source rows are kept — a later run over the same NEFs re-creates
the negative. It fails with `ROLL_NOT_FOUND` for an unregistered roll and
`NEGATIVE_NOT_FOUND` for an unknown negative — again validating the whole
selection before removing anything.

`export` renders each negative's published TIFF into a **positive in Adobe
RGB (1998)-compatible colour — the negative's recorded tone op baked in —
written as a 16-bit lossless JPEG XL** named after the negative (`.jxl`),
into `--output` (docs/EXPORT_PLAN.md). The ops log's geometric ops are
replayed over the published pixels exactly as before; the render then
inverts, matrixes the colour into Adobe RGB via the roll's recorded
`camera_color` matrix, and applies the tone curve — the same curve the
preview shows, at 16 bits. A mono roll's export is single-channel, tagged
with the grey export profile and rendered without a colour matrix. Each
file carries its export ICC profile embedded, plus Exif and XMP boxes at
encode time (metadata reaches the file in the same write — there is no
second pass); the XMP carries a `scannyboy:provenance` record making the
file interpretable without the database. The roll's own files are never
touched. A colour roll whose manifest predates the `camera_color` block
fails the export outright with `CAMERA_MATRIX_MISSING` — raised once,
before anything is written; a mono roll without the block exports fine.
libjxl being unreachable fails the whole export with
`JXL_ENCODER_UNAVAILABLE` (a packaging failure, not a user error). It
emits `export_done` per negative (`negative_id`, `output`, `width`,
`height`); a negative that has not been stitched is skipped with a
`warning` (`NEGATIVE_NOT_FOUND`) and fails the command's exit status,
while a per-negative failure warns with `EXPORT_FAILED` and does not stop
the rest.

### `--version`

`scanny-boy --version` prints one plain-text line (`scanny-boy 0.1.0`) and
exits 0. It is a diagnostic, not part of the event stream: the app never
calls it, and it is the packaged build's cheapest check that the frozen
program starts and can read its own package metadata. Every other
invocation emits only JSON event lines on stdout.

### `--jobs`

`--jobs` sets how many frames of one negative are converted at once;
parallelism never spans negatives, because a negative is published all at
once or not at all. Omitting it uses
`min(shots_per_negative, logical CPUs, 4)` — where `shots_per_negative` is
the batch's own count, `across * down` from `--per-negative`/`--grid` or
the work manifest — reduced
silently if this machine's memory budget is smaller. An explicit value is
accepted from 1 to 12; 1 uses the serial path. Values outside that range are
a usage error (exit 2).

Each worker is budgeted a fixed amount of memory, and the total must not
exceed half of physical RAM. An explicit `--jobs` above that limit is
rejected with `INSUFFICIENT_MEMORY` and exit 1 — not a usage error, since
the command is well formed and only this machine cannot honour it. The
computed default is never rejected this way, only lowered.

## Output transport

- stdout contains one UTF-8 JSON object per line and flushes after every
  line.
- stderr contains human-readable logs and is never parsed.
- stdout and stderr must both be drained while the process is running.
- Every event includes `protocol_version` and `event`, and includes `run_id`
  when the event belongs to a conversion run.

`schema.json` is the authoritative JSON Schema for one event line.
`manifest.schema.json` is the authoritative schema for
`scanny-boy-manifest.json`, the work directory's conversion record.
`roll-manifest.schema.json` is the authoritative schema for a roll's durable
record as delivered by `roll info` (format version 7; now persisted in the
library database rather than a JSON file in the roll folder).

### `serve`

`scanny-boy serve` is the resident helper behind the app's Edit tab
(docs/OPTIMIZATION.md §2). It reads one JSON request object per stdin line
and writes the ordinary event stream to stdout; it emits nothing of its
own, so every line on stdout belongs to exactly one request, identified by
its `request_id`:

    {"request_id": "0f8…", "command": ["edit", "list-spots", "--roll", "…", …]}
    {"request_id": "0f8…", "cancel": true}

Requests are answered strictly one at a time, in arrival order; a `cancel`
line is answered immediately against the request it names, whether that
request is running or still queued. A request the daemon will not run —
because the helper was shut down under it, or its `command` was not a
usable argv — is still answered: a cancelled one with the one-shot SIGTERM
shape (`error` carrying `CANCELLED`, then `finished` at exit status 143),
a malformed one with `finished` at exit status 2. Closing stdin is the
ordinary stop; the daemon lets the in-flight request finish, then exits 0.
SIGTERM is the backstop: it cancels every live token, answers the queued
requests as cancelled, lets the in-flight request finish, and exits 0.

### Event types

| Event | Meaning |
| --- | --- |
| `started` | The command began. Carries which command. |
| `probe_result` | The catalogue or selection validation result of `probe`. |
| `progress` | Work in progress. Carries a stable source index, the pipeline step, a completed work count, a total, and which stage (`prepare` or `stitch`) it belongs to. |
| `item_done` | A TIFF has been published in the output folder after its whole group completed successfully. |
| `group_done` | A negative's group finished, after that group's `item_done` events. |
| `group_failed` | A negative's group failed and its staging directory was removed. |
| `negative_done` | A stitched TIFF has been published for one negative. Carries `negative_id`, `output`, `width`, `height`, `global_rms_px`, and `max_overlap_mad` (the worst post-gain overlap residual). |
| `negative_failed` | A negative could not be stitched. Carries `negative_id`, `code`, and `message`. |
| `roll_created` | A new roll folder was created. Carries `roll_id`, `roll_name`, and `path`. |
| `roll_list` | The library scan result of `roll list`. Carries `rolls`. |
| `roll_info` | One roll manifest, loaded and validated. Carries `manifest`. |
| `roll_renamed` | A roll's folder was renamed. Carries `roll_id`, `roll_name`, and `path`. |
| `roll_deleted` | A roll was unregistered. Carries `roll_id` and `path`. |
| `metadata_applied` | A published TIFF's capture time was written. Carries `negative_id`. |
| `metadata_skipped` | A dirty negative was not rewritten. Carries `negative_id`, `code`, and `message`. |
| `metadata_updated` | A `metadata set` payload was applied. Carries `manifest` (the updated roll manifest). |
| `metadata_values` | The catalog answer to `metadata values`. Carries `field` and `values` (most-recently-used first). |
| `edit_recorded` | A rotate or flip op was recorded for one negative. Carries `negative_id`, `edit`, `rotation_quarter_turns`, `flipped_horizontally`, `fine_rotation_deg`, the net `crop` report (`{width, height, tilt_deg, preset}` or `null`), and `preview_path`. |
| `negative_deleted` | A negative was deleted by `edit delete`. Carries `negative_id` and `output`. |
| `region_rendered` | A display-space region of one negative's published TIFF was rendered at 1:1 by `edit render-region`. Carries `negative_id`, `path`, `x`, `y`, `width`, and `height`. Carries no `run_id`. |
| `preview_rendered` | A negative's whole display image was rendered by `edit render-preview` in the requested display mode, downscaled like the cached preview. Carries `negative_id`, `path`, `width`, and `height`. Carries no `run_id`. |
| `export_done` | One negative's edits were applied and written to the export folder. Carries `negative_id`, `output`, `width`, and `height`. |
| `flatfield_created` | A flat-field profile was created. Carries `profile`. |
| `flatfield_list` | The flat-field profile list. Carries `profiles`. |
| `flatfield_deleted` | A flat-field profile was deleted. Carries `profile_id`. |
| `flatfield_progress` | A long `flatfield create` is progressing. Carries `phase`, `completed`, `total`. Carries no `run_id`. |
| `grid_created` | A grid configuration preset was created. Carries `profile`. |
| `grid_list` | The grid configuration preset list. Carries `profiles`. |
| `grid_deleted` | A grid configuration preset was deleted. Carries `profile_id`. |
| `spots_reported` | A negative's spot set was reported by `edit detect-spots`, `edit spots`, or `edit list-spots`: display-space rects (the app converts nothing), `found` before the cap, `preview_path` null for the pure query. Carries no `run_id`. |
| `warning` | A non-fatal condition, identified by a stable code. |
| `error` | A fatal condition, identified by a stable code. |
| `finished` | The command ended. Carries final status and exit status. |

The pipeline step carried by `progress` is one of `decode`, `write_tiff`,
`add_metadata` (the prepare stage) or `load`, `detect`, `match`, `solve`,
`warp`, `blend`, `normalize`, `write_stitched` (the stitch stage). `stage`
defaults to `prepare` and is `stitch` only during the stitch stage of
`stitch` or `run`.

`probe_result` carries `catalogue` (the full input folder's `.nef` filenames
in canonical order — section 3.3 — regardless of whether `--files` was
given), `warnings` (the stable codes of any `warning` events emitted during
this probe, as a convenience rollup), and `groups` (present only when
`--files` was given and validated: the selection's filenames in canonical
order, chunked into `across * down`-sized negatives; empty otherwise).

When `--out` is also given alongside a validated `--files` selection,
`probe_result` additionally carries `output_conflicts` (output filenames
that already exist and would be replaced by a matching rerun — the
confirmation list section 3.6 requires before `convert --overwrite`),
`estimated_required_bytes` (the section 3.9 disk estimate for this run), and
`available_bytes` (free space on the output volume at probe time). All three
are absent (`output_conflicts` empty, the byte fields `null`) when `--out`
was not given.

When `--roll` is also given alongside a validated `--files` selection,
`probe_result` additionally carries `roll_overlap` — an array of
`{negative_id, expected_output, run_id, overlapping_sources, group_index}`
describing which of *this* selection's prospective groups collide with
negatives already in the roll.

Parallel completion order need not match source order. The UI derives overall
progress from counts, never from the largest source index seen.

`progress` may report decoded or staged work. If a group fails or is
cancelled, it emits no `item_done` events for that group's staged files.

`roll_list` carries `rolls`, an array of `{path, status, reason, roll_id,
roll_name, negative_count}`. `status` is `"ok"` or `"unreadable"`. `reason`
is `{code, message}` for an unreadable roll and null otherwise; the
remaining fields are null when unreadable. `negative_count` is the number of
negatives in the roll.

### Cancellation

The app requests cancellation with SIGTERM. The CLI stops submitting new
frames, lets frames already running finish their current step, discards the
negative in progress along with its staging directory, and leaves every
already-published negative in place. It then records the manifest as
`cancelled` and ends the stream with an `error` carrying `CANCELLED`,
followed by `finished` with status `cancelled` and `exit_status` 143.

A cancelled negative emits no `group_failed`: it was abandoned, not failed,
and a rerun will convert it normally. Swift treats a user-requested
cancellation as cancelled whether the helper exits 143 or is reported as
terminated by signal 15. A forced termination after the grace period cannot
clean files, update the manifest, or emit a final event; the next `probe`
or `convert` detects the manifest left as `running`, removes that run's
staging directories, and reruns the incomplete negative.

### Stable error and warning codes

| Code | Meaning |
| --- | --- |
| `NO_FILES` | No `.nef` files, or none selected |
| `NON_CONTIGUOUS_SELECTION` | Selection has a gap in canonical order |
| `NOT_DIVISIBLE` | Selected count not divisible by the batch's shots per negative |
| `INVALID_PER_NEGATIVE` | Shots per negative outside 1–12 |
| `INVALID_GRID` | A well-formed `--grid AxD` whose shape breaks `min(across, down) <= 2` (every cell must show rebate) or the 12-scan cap |
| `MISSING_CAPTURE_TIME` | A catalogue file has no usable capture timestamp |
| `FILENAME_SORT_USED` | Warning: whole catalogue fell back to filename order |
| `UNSUPPORTED_RAW` | LibRaw cannot read the file, typically HE/HE\* |
| `CAPTURE_METADATA_MISSING` | A required EXIF tag is absent |
| `CAPTURE_SETTINGS_DIFFER` | Exposure, white balance, lens, or orientation varies |
| `UNREADABLE_RAW` | File exists but could not be decoded |
| `OUTPUT_SAME_AS_INPUT` | Output folder resolves to the input folder |
| `OUTPUT_NOT_WRITABLE` | Cannot write to the output folder |
| `OUTPUT_NOT_EMPTY` | Nonempty folder with no valid manifest |
| `OUTPUT_CONFLICT` | Existing outputs and no `--overwrite` |
| `INSUFFICIENT_DISK` | Free space below the section 3.9 estimate |
| `INSUFFICIENT_MEMORY` | Explicit `--jobs` exceeds the memory budget |
| `BAD_MANIFEST` | Manifest unreadable or fails its schema |
| `MANIFEST_MISMATCH` | Work manifest valid but its run parameters differ |
| `ICC_PROFILE_INVALID` | Bundled profile missing or wrong SHA-256 |
| `TIFF_WRITE_FAILED` | A TIFF or metadata write failed |
| `CANCELLED` | Cooperative user cancellation |
| `WORK_SAME_AS_OUTPUT` | `--work` resolves to `--out` or `--roll` |
| `WORK_MANIFEST_UNUSABLE` | Work manifest is `running`/`cancelled`, or `partial` without `--allow-partial` |
| `INTERMEDIATE_MISSING` | An intermediate named by the work manifest is absent |
| `INTERMEDIATE_CHANGED` | An intermediate's size or SHA-256 differs from the work manifest |
| `STITCH_INSUFFICIENT_MATCHES` | A pair fell below the inlier count or ratio gate |
| `STITCH_UNDERCONSTRAINED` | The pair graph is disconnected; a frame cannot be placed |
| `STITCH_RESIDUAL_TOO_HIGH` | A residual or overlap gate was exceeded |
| `STITCH_OUTPUT_TOO_LARGE` | Estimated stitched file exceeds 3.5 GiB |
| `STITCH_FAILED` | Any other failure while stitching one negative |
| `STITCH_SCALE_DRIFT` | Warning: similarity fit's scale left `SCALE_DRIFT_WARN` |
| `STITCH_GAIN_DRIFT` | Warning: a frame's solved photometric gain left `GAIN_DRIFT_WARN` from unity |
| `STITCH_LAYOUT_UNEXPECTED` | Warning: solved layout is not strip-shaped (strips), or does not match the declared grid / is not a regular grid (grids) |
| `STITCH_REBATE_CHECK_FAILED` | Warning: rebate edges not collinear, or not found |
| `STITCH_CLAHE_FALLBACK_USED` | Warning: retrying registration with CLAHE after `STITCH_UNDERCONSTRAINED` or `STITCH_RESIDUAL_TOO_HIGH` |
| `OUTPUT_DIMENSIONS_LARGE` | Warning: a canvas dimension exceeds 30,000 px |
| `ROLL_NOT_FOUND` | `--roll` is not a registered roll, or a listed roll's folder is gone |
| `ROLL_MANIFEST_UNSUPPORTED` | Roll record is not `manifest_format_version: 8` |
| `ROLL_EXISTS` | `roll init` or `roll rename` could not find a free folder name |
| `ROLL_RENAME_FAILED` | `roll rename`'s folder move failed; neither the folder nor the manifest changed |
| `ROLL_INVARIANT_MISMATCH` | Run parameters differ from the roll's invariants |
| `OUTPUT_MODIFIED_EXTERNALLY` | A published TIFF's hash differs from the manifest at apply time |
| `METADATA_WRITE_FAILED` | Reserved (see below) |
| `ORPHAN_FILE_NOT_REMOVED` | Warning: a removed covered negative's TIFF could not be deleted |
| `NEGATIVE_NOT_FOUND` | The named `negative_id` does not exist, or has not been stitched |
| `INVALID_EDIT` | An `edit` subcommand got a direction or argument it does not accept |
| `INVALID_METADATA` | A `metadata` subcommand got an unknown field, a non-`YYYY-MM-DD` date, or a malformed payload |
| `EXPORT_FAILED` | Writing one negative's export failed |
| `JXL_ENCODER_UNAVAILABLE` | libjxl could not be reached from the export process — a packaging failure, not a user error; the whole export stops |
| `CAMERA_MATRIX_MISSING` | A colour roll's manifest predates the `camera_color` block; the export is refused (a mono roll is not) |
| `CAMERA_MATRIX_CONFLICT` | Warning: a later stitch run's source reports a different camera colour matrix than the roll's frozen one; the frozen value is kept |
| `PREVIEW_FAILED` | Warning: a preview could not be generated or rotated; the edit itself was kept |
| `FLATFIELD_PROFILE_NOT_FOUND` | No flat-field profile with the given id |
| `FLATFIELD_PROFILE_EXISTS` | A flat-field profile with that name already exists |
| `GRID_PROFILE_NOT_FOUND` | No grid configuration preset with the given id |
| `GRID_PROFILE_EXISTS` | A grid configuration preset with that name already exists |
| `FLATFIELD_PROFILE_IN_USE` | The profile is locked into a roll's invariants and cannot be deleted |
| `FLATFIELD_GAIN_MAP_MISSING` | The profile's `.npz` is missing or corrupt |
| `FLATFIELD_ASPECT_MISMATCH` | Warning: the reference's aspect ratio differs from the frames' by more than 1% |
| `FLATFIELD_HIGHLIGHT_CLIPPED` | Warning: the correction pushed more than 0.1% of a frame's pixels past full scale |
| `GEOMETRY_INSUFFICIENT_FRAMES` | Too few usable calibration frames |
| `GEOMETRY_BOARD_NOT_DETECTED` | Neither calibration board detected, or the read is ambiguous |
| `GEOMETRY_FRAME_SIZE_MISMATCH` | The profile was fitted at other frame dimensions |
| `GEOMETRY_FIT_REJECTED` | Warning: the distortion fit did not clear its acceptance gates; it is not applied |
| `GEOMETRY_MAGNITUDE_SUSPECT` | Warning: the fitted distortion is outside the expected 0.03–0.2% band; it is applied |
| `GEOMETRY_FEW_FRAMES` | Warning: under 16 calibration frames |
| `CHROMATIC_FIT_REJECTED` | Warning: the CA fit did not clear its acceptance gates; it is not applied |
| `SCAN_CLIPPED` | Warning: more than 1% of one channel's pixels decoded at or above sensor white; their highlights are clipped and no reconstruction is attempted |
| `NORMALIZE_DEGENERATE_BOUNDS` | The bounds meters produced a degenerate (non-finite or zero-span) bound; the negative fails |
| `NORMALIZE_HEADROOM_CLIPPED` | Warning: the encode's headroom clipped more than 0.1% of one channel's pixels; the headroom constants are likely too tight |
| `NORMALIZE_FILM_EXTENT_WITHHELD` | Informational: the film-extent pass withheld a non-film border band (likely the negative carrier) from the metering; the message names the four insets in canvas pixels. The published pixels are never cropped |
| `NORMALIZE_FILM_EXTENT_EXCESSIVE` | Warning: the withheld border band kept less than half of the analysis region — the frame is unusual, and the user should look at what the metering region is on |
| `TONE_METERING_UNAVAILABLE` | Warning: `--auto-density` or `--auto-grade` was requested but the negative's `normalization` record is missing or incomplete; the op still records with the explicitly-given or neutral value |
| `FILM_KIND_REQUIRED` | The roll has no `film.kind`; create a new roll with `--film-kind` |
| `FILM_BASE_REQUIRED` | The roll has no film-base reference; `run`/`stitch` refuse before any pixel work |
| `FILM_BASE_LOCKED` | The roll's film-base reference is locked (its first negative was converted) and cannot be replaced |
| `FILM_BASE_NOT_FOUND` | No flat film-base region was found in the base frame |
| `FILM_BASE_TOO_SMALL` | The chosen flat population is below the area or cell floor |
| `FILM_BASE_CLIPPED` | The base frame's rebate is sensor-clipped |
| `FILM_BASE_TOO_DARK` | A channel's median inside the rebate is below the per-channel floor |
| `FILM_BASE_AMBIGUOUS` | Two large flat populations of different density; the frame is refused rather than guessed at |
| `ROLL_PREDATES_FILM_BASE` | This roll was stitched before film-base anchoring and cannot take new negatives or a base frame |
| `FILM_BASE_CAMERA_CONFLICT` | Warning: the base frame's EXIF camera model differs from the roll's; the measurement may still be fine |
| `FILM_BASE_FLATFIELD_CONFLICT` | Warning: the run's flat-field profile differs from the one the base frame was measured with |
| `SPOT_LIMIT_REACHED` | Warning: the spot detector found more than 500 proposals on a negative and kept the highest-scoring 500; the remedy is a lower `--sensitivity` |
| `SPOTS_STALE` | Warning: the negative's spot set was detected against a canvas a re-stitch has replaced; it repairs nothing and needs re-detecting |
| `LIBRARY_DB_UNSUPPORTED` | The library database sits at a migration revision this helper does not know — written by a newer Scanny Boy |
| `INTERNAL_ERROR` | An unexpected exception reached the top of a command; the message names it. Bug-report material |

## Exit status

- `0`: complete success.
- `1`: validation, conversion, or partial-run failure.
- `2`: invalid command usage.
- `143`: cooperative user cancellation, matching 128 + SIGTERM.

The event stream, not message text, is the app's machine-readable interface.
