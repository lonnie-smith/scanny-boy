# Mac app <-> CLI contract

The Swift app invokes the packaged `scanny-boy` binary as a subprocess. This
document is the source of truth for that interface; update it whenever the
CLI's args or output shape change, and update `schema.json` alongside it.

This file summarises `docs/IMPLEMENTATION_PLAN.md` section 4 for Phase 1,
`docs/PHASE2_IMPLEMENTATION_PLAN.md` section 3 for Phase 2, and
`docs/PHASE3_IMPLEMENTATION_PLAN.md` section 3.5 for Phase 3. If this file
and any plan ever disagree, the plan is authoritative.

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
>>>>>>> origin/main

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
`flipped_horizontally` (the ops log's net effect, derived rather than
stored). A client that only
understands an earlier protocol version must reject a newer stream rather
than guess at the new fields.

## Invocation

```text
scanny-boy roll init   --library DIR --name NAME
scanny-boy roll list   --library DIR
scanny-boy roll info   --roll DIR
scanny-boy roll rename --roll DIR --name NAME
scanny-boy roll delete --roll DIR

scanny-boy probe      --input DIR [--files FILE [FILE ...]] [--per-negative N] [--roll DIR]
                      [--flatfield ID]

scanny-boy prepare    --input DIR --files FILE [FILE ...] --out DIR --per-negative N
                      [--jobs N] [--overwrite] [--flatfield ID]

scanny-boy stitch     --work DIR --roll DIR [--jobs N] [--overwrite] [--allow-partial]
                      [--negatives ID ...] [--flatfield ID]

scanny-boy run        --input DIR --files FILE [FILE ...] --roll DIR --per-negative N
                      [--jobs N] [--skip-sources FILE ...] [--work DIR] [--flatfield ID]

scanny-boy apply-metadata --roll DIR

scanny-boy metadata set    --roll DIR --payload JSON
scanny-boy metadata values --field FIELD

scanny-boy edit rotate --roll DIR --negative ID [ID ...] --direction cw|ccw
scanny-boy edit flip   --roll DIR --negative ID [ID ...]
scanny-boy edit delete --roll DIR --negative ID [ID ...]

scanny-boy export      --roll DIR --output DIR [--negatives ID ...]

scanny-boy flatfield create --reference FILE --name NAME
                            [--calibration FILE [FILE ...]]
scanny-boy flatfield list
scanny-boy flatfield delete --profile ID
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

`roll init` creates a folder under `--library` (slug + collision rule) and
registers an empty v5 roll in the library database. It emits `roll_created`
carrying `roll_id`, `roll_name`, and `path`. A roll records no grouping of
its own: `--per-negative` is each stitch batch's choice, so one roll can
hold negatives stitched from different scan counts.

`roll list` reports the rolls registered under `--library` from the library
database and emits a single `roll_list` event. A registered roll whose
folder has vanished is reported as `"unreadable"` with `ROLL_NOT_FOUND`
rather than silently disappearing.

`roll info` loads one roll from the library database and emits it as a
`roll_info` event, each negative augmented with `preview_path`,
`rotation_quarter_turns`, and `flipped_horizontally`. Swift never reads the
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
roll fields are `capture_date` (the roll capture date) plus `city`,
`state`, `camera`, `lens`, `caption`; negative fields are the same five
plus `capture_date` (the negative's date override). A key that is absent
leaves the field untouched; a key present with `null` or `""` clears it (a
cleared negative field then inherits the roll-level fallback; the
extended metadata uses live-fallback semantics, never copying roll values
onto negatives). Every capture-date change recomputes each negative's
intended capture time by the rank formula (noon + rank − 1 seconds on the
negative's effective date, ranked within that date in roll order), so the
stored intent always preserves roll order. Non-empty `city`, `state`,
`camera`, and `lens` values are remembered in the metadata-values catalog
(`caption` never is). It emits one `metadata_updated` event carrying the
updated `manifest`, and fails with `INVALID_METADATA` for an unknown field,
a non-`YYYY-MM-DD` date, or a malformed payload, `ROLL_NOT_FOUND` for an
unregistered roll, and `NEGATIVE_NOT_FOUND` for an unknown negative id —
the whole payload is validated before anything is written.

`metadata values` lists the catalog of previously-entered values for one
field (`city`, `state`, `camera`, or `lens`), most-recently-used first, as
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
horizontal mirror), and `preview_path`. It fails with `INVALID_EDIT` for an
unknown direction, `ROLL_NOT_FOUND` for an unregistered roll, and
`NEGATIVE_NOT_FOUND` for an unknown or unstitched negative — the whole
selection is validated before any op is appended, so a batch either records
or fails without partial effects.

`edit flip` records a horizontal mirror of one or more negatives — a flip of
the pixels as they currently render, *after* any recorded rotations — by
appending a `flip` op to each negative's ordered edits ops log. Like
`edit rotate` it never touches the published TIFF; it regenerates the
previews and emits `edit_recorded` per negative with the same fields. The
ops log's net transform does not collapse to a rotation alone: a flip and a
rotation do not commute, so consumers replay the log into a
`(rotation_quarter_turns, flipped_horizontally)` pair. It fails with the
same codes as `edit rotate`.

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

`export` writes each negative's TIFF into `--output` with the negative's
edits applied — the ops log replayed over the published pixels, named after
the negative and never touching the roll's own files. It emits `export_done`
per negative (`negative_id`, `output`, `width`, `height`); a negative that
has not been stitched is skipped with a `warning` (`NEGATIVE_NOT_FOUND`) and
fails the command's exit status, while a write failure warns with
`EXPORT_FAILED`. A failed write per negative does not stop the rest.

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
the batch's own value, from `--per-negative` or the work manifest — reduced
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
record as delivered by `roll info` (format version 6; now persisted in the
library database rather than a JSON file in the roll folder).

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
| `edit_recorded` | A rotate or flip op was recorded for one negative. Carries `negative_id`, `edit`, `rotation_quarter_turns`, `flipped_horizontally`, and `preview_path`. |
| `negative_deleted` | A negative was deleted by `edit delete`. Carries `negative_id` and `output`. |
| `export_done` | One negative's edits were applied and written to the export folder. Carries `negative_id`, `output`, `width`, and `height`. |
| `flatfield_created` | A flat-field profile was created. Carries `profile`. |
| `flatfield_list` | The flat-field profile list. Carries `profiles`. |
| `flatfield_deleted` | A flat-field profile was deleted. Carries `profile_id`. |
| `flatfield_progress` | A long `flatfield create` is progressing. Carries `phase`, `completed`, `total`. Carries no `run_id`. |
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
order, chunked into `--per-negative`-sized negatives; empty otherwise).

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
| `STITCH_LAYOUT_UNEXPECTED` | Warning: solved layout is not strip-shaped |
| `STITCH_REBATE_CHECK_FAILED` | Warning: rebate edges not collinear, or not found |
| `STITCH_CLAHE_FALLBACK_USED` | Warning: retrying registration with CLAHE after `STITCH_UNDERCONSTRAINED` or `STITCH_RESIDUAL_TOO_HIGH` |
| `OUTPUT_DIMENSIONS_LARGE` | Warning: a canvas dimension exceeds 30,000 px |
| `ROLL_NOT_FOUND` | `--roll` is not a registered roll, or a listed roll's folder is gone |
| `ROLL_MANIFEST_UNSUPPORTED` | Roll record is not `manifest_format_version: 6` |
| `ROLL_EXISTS` | `roll init` or `roll rename` could not find a free folder name |
| `ROLL_RENAME_FAILED` | `roll rename`'s folder move failed; neither the folder nor the manifest changed |
| `ROLL_INVARIANT_MISMATCH` | Run parameters differ from the roll's invariants |
| `OUTPUT_MODIFIED_EXTERNALLY` | A published TIFF's hash differs from the manifest at apply time |
| `METADATA_WRITE_FAILED` | The EXIF rewrite or its verification failed |
| `ORPHAN_FILE_NOT_REMOVED` | Warning: a removed covered negative's TIFF could not be deleted |
| `NEGATIVE_NOT_FOUND` | The named `negative_id` does not exist, or has not been stitched |
| `INVALID_EDIT` | An `edit` subcommand got a direction or argument it does not accept |
| `INVALID_METADATA` | A `metadata` subcommand got an unknown field, a non-`YYYY-MM-DD` date, or a malformed payload |
| `EXPORT_FAILED` | Writing one negative's export failed |
| `PREVIEW_FAILED` | Warning: a preview could not be generated or rotated; the edit itself was kept |
| `FLATFIELD_PROFILE_NOT_FOUND` | No flat-field profile with the given id |
| `FLATFIELD_PROFILE_EXISTS` | A flat-field profile with that name already exists |
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
| `LIBRARY_DB_UNSUPPORTED` | The library database sits at a migration revision this helper does not know — written by a newer Scanny Boy |
| `INTERNAL_ERROR` | An unexpected exception reached the top of a command; the message names it. Bug-report material |

## Exit status

- `0`: complete success.
- `1`: validation, conversion, or partial-run failure.
- `2`: invalid command usage.
- `143`: cooperative user cancellation, matching 128 + SIGTERM.

The event stream, not message text, is the app's machine-readable interface.
