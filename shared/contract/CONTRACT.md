# Mac app <-> CLI contract

The Swift app invokes the packaged `scanny-boy` binary as a subprocess. This
document is the source of truth for that interface; update it whenever the
CLI's args or output shape change, and update `schema.json` alongside it.

This file summarises `docs/IMPLEMENTATION_PLAN.md` section 4 for Phase 1,
`docs/PHASE2_IMPLEMENTATION_PLAN.md` section 3 for Phase 2, and
`docs/PHASE3_IMPLEMENTATION_PLAN.md` section 3.5 for Phase 3. If this file
and any plan ever disagree, the plan is authoritative.

Protocol version 4 replaces the single-run output folder with a durable
**roll** — a named folder holding many runs — and adds library management,
overlap detection, and a metadata-apply stage. It also makes replacement
in-place: a rerun adopts the covered negative (same `negative_id` and output
name) instead of publishing a replacement and leaving a tombstone behind. A
client that only understands an earlier protocol version must reject a newer
stream rather than guess at the new fields.

## Invocation

```text
scanny-boy roll init   --library DIR --name NAME --per-negative N
scanny-boy roll list   --library DIR
scanny-boy roll info   --roll DIR
scanny-boy roll rename --roll DIR --name NAME

scanny-boy probe      --input DIR [--files FILE [FILE ...]] [--per-negative N] [--roll DIR]

scanny-boy convert    --input DIR --files FILE [FILE ...] --out DIR [--per-negative N]
                      [--jobs N] [--overwrite]

scanny-boy stitch     --work DIR --roll DIR [--jobs N] [--overwrite] [--allow-partial]
                      [--negatives ID ...]

scanny-boy run        --input DIR --files FILE [FILE ...] --roll DIR [--jobs N]
                      [--skip-sources FILE ...] [--work DIR]

scanny-boy apply-metadata --roll DIR
```

`--roll` replaces `--out` on `stitch` and `run`. `convert` keeps `--out`,
because it still writes a work directory rather than a roll.

`--film-date` is removed from every command. Synthetic capture times are
assigned in the metadata stage, not at convert time.

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

`convert` repeats all important validation. It does not trust an earlier
probe result.

`--files` takes filenames relative to `--input`, not absolute paths. Reject a
selection above 5000 files with a usage error rather than letting the
operating system truncate the argument list.

`--skip-sources` names filenames, relative to `--input`, to exclude from a
`run`. Excluded files are removed from the selection **before** grouping, so
a skip must remove a whole group's worth or the run fails
`NON_CONTIGUOUS_SELECTION`.

`--negatives` on `stitch` restricts a re-stitch to named `negative_id`s.

`roll init` creates a folder under `--library` (slug + collision rule) and
writes an empty v3 roll manifest. It emits `roll_created` carrying `roll_id`,
`roll_name`, and `path`.

`roll list` performs a one-level scan of `--library` and emits a single
`roll_list` event.

`roll info` loads and validates one roll manifest and emits it as a
`roll_info` event. Swift never parses `scanny-boy-roll.json` itself and never
enumerates the library itself — `roll list` and `roll info` are the only two
ways in.

`roll rename` moves the roll's folder to a slug of `--name` and, only after a
successful move, writes the new `roll_name` into the manifest. It emits
`roll_renamed` carrying `roll_id`, `roll_name`, and `path` (the roll's new
location). It does not enforce "refused while any run is active" — the CLI
is stateless between invocations, so the app checks that itself before
issuing the command.

`apply-metadata` writes intended capture times from the roll manifest into
published TIFFs. See Phase 3 section 3.8.

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
`min(shots_per_negative, logical CPUs, 4)`, reduced silently if this
machine's memory budget is smaller. An explicit value is accepted from 1 to
12; 1 uses the serial path. Values outside that range are a usage error
(exit 2).

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
`roll-manifest.schema.json` is the authoritative schema for
`scanny-boy-roll.json`, the roll folder's durable record (Phase 3 section
3.3, format version 3).

### Event types

| Event | Meaning |
| --- | --- |
| `started` | The command began. Carries which command. |
| `probe_result` | The catalogue or selection validation result of `probe`. |
| `progress` | Work in progress. Carries a stable source index, the pipeline step, a completed work count, a total, and which stage (`convert` or `stitch`) it belongs to. |
| `item_done` | A TIFF has been published in the output folder after its whole group completed successfully. |
| `group_done` | A negative's group finished, after that group's `item_done` events. |
| `group_failed` | A negative's group failed and its staging directory was removed. |
| `negative_done` | A stitched TIFF has been published for one negative. Carries `negative_id`, `output`, `width`, `height`, `global_rms_px`, and `max_overlap_mad`. |
| `negative_failed` | A negative could not be stitched. Carries `negative_id`, `code`, and `message`. |
| `roll_created` | A new roll folder was created. Carries `roll_id`, `roll_name`, and `path`. |
| `roll_list` | The library scan result of `roll list`. Carries `rolls`. |
| `roll_info` | One roll manifest, loaded and validated. Carries `manifest`. |
| `roll_renamed` | A roll's folder was renamed. Carries `roll_id`, `roll_name`, and `path`. |
| `metadata_applied` | A published TIFF's capture time was written. Carries `negative_id`. |
| `metadata_skipped` | A dirty negative was not rewritten. Carries `negative_id`, `code`, and `message`. |
| `warning` | A non-fatal condition, identified by a stable code. |
| `error` | A fatal condition, identified by a stable code. |
| `finished` | The command ended. Carries final status and exit status. |

The pipeline step carried by `progress` is one of `decode`, `write_tiff`,
`add_metadata` (the conversion stage) or `load`, `detect`, `match`, `solve`,
`warp`, `blend`, `write_stitched` (the stitch stage). `stage` defaults to
`convert` and is `stitch` only during the stitch stage of `stitch` or `run`.

`probe_result` carries `catalogue` (the full input folder's `.nef` filenames
in canonical order — section 3.3 — regardless of whether `--files` was
given), `warnings` (the stable codes of any `warning` events emitted during
this probe, as a convenience rollup), and `groups` (present only when
`--files` was given and validated: the selection's filenames in canonical
order, chunked into `shots_per_negative`-sized negatives; empty otherwise).

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
| `NOT_DIVISIBLE` | Selected count not divisible by shots per negative |
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
| `STITCH_LAYOUT_UNEXPECTED` | Warning: solved layout is not strip-shaped |
| `STITCH_REBATE_CHECK_FAILED` | Warning: rebate edges not collinear, or not found |
| `STITCH_CLAHE_FALLBACK_USED` | Warning: retrying registration with CLAHE after `STITCH_UNDERCONSTRAINED` or `STITCH_RESIDUAL_TOO_HIGH` |
| `OUTPUT_DIMENSIONS_LARGE` | Warning: a canvas dimension exceeds 30,000 px |
| `ROLL_NOT_FOUND` | `--roll` has no readable `scanny-boy-roll.json` |
| `ROLL_MANIFEST_UNSUPPORTED` | Roll manifest is not `manifest_format_version: 3` |
| `ROLL_EXISTS` | `roll init` or `roll rename` could not find a free folder name |
| `ROLL_RENAME_FAILED` | `roll rename`'s folder move failed; neither the folder nor the manifest changed |
| `ROLL_INVARIANT_MISMATCH` | Run parameters differ from the roll's invariants |
| `PER_NEGATIVE_LOCKED` | Attempt to change `shots_per_negative` after a run published |
| `OUTPUT_MODIFIED_EXTERNALLY` | A published TIFF's hash differs from the manifest at apply time |
| `METADATA_WRITE_FAILED` | The EXIF rewrite or its verification failed |
| `ORPHAN_FILE_NOT_REMOVED` | Warning: a removed covered negative's TIFF could not be deleted |

## Exit status

- `0`: complete success.
- `1`: validation, conversion, or partial-run failure.
- `2`: invalid command usage.
- `143`: cooperative user cancellation, matching 128 + SIGTERM.

The event stream, not message text, is the app's machine-readable interface.
