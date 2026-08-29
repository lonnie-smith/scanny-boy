# Mac app <-> CLI contract

The Swift app invokes the packaged `scanny-boy` binary as a subprocess. This
document is the source of truth for that interface; update it whenever the
CLI's args or output shape change, and update `schema.json` alongside it.

This file summarises `docs/IMPLEMENTATION_PLAN.md` section 4. If the two ever
disagree, the plan is authoritative.

## Invocation

```text
scanny-boy probe \
  --input DIR [--files FILE [FILE ...]] [--per-negative 3]

scanny-boy convert \
  --input DIR --files FILE [FILE ...] \
  --out DIR --film-date YYYY-MM-DD --per-negative 3 \
  [--jobs N] [--overwrite]
```

`probe` is read-only and works at two levels of detail:

- **`--input` alone** returns the catalogue in canonical order, plus any
  sorting warnings. Swift calls this first, because it cannot name a
  selection before it knows the order. Swift never sorts files itself.
- **`--input` with `--files`** additionally validates the selection: the
  uninterrupted-range check, grouping, metadata consistency, output
  conflicts, and whether conversion may start.

`--out` may be given to `probe` alongside `--files` to include output-folder
validation and the overwrite-conflict preview.

`convert` repeats all important validation. It does not trust an earlier
probe result.

`--files` takes filenames relative to `--input`, not absolute paths. Reject a
selection above 5000 files with a usage error rather than letting the
operating system truncate the argument list.

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

### Event types

| Event | Meaning |
| --- | --- |
| `started` | The command began. Carries which command (`probe` or `convert`). |
| `probe_result` | The catalogue or selection validation result of `probe`. |
| `progress` | Work in progress. Carries a stable source index, the pipeline step, a completed work count, and a total. |
| `item_done` | A TIFF has been published in the output folder after its whole group completed successfully. |
| `group_done` | A negative's group finished, after that group's `item_done` events. |
| `group_failed` | A negative's group failed and its staging directory was removed. |
| `warning` | A non-fatal condition, identified by a stable code. |
| `error` | A fatal condition, identified by a stable code. |
| `finished` | The command ended. Carries final status and exit status. |

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
was not given. A bad output folder, a manifest that does not match this
selection, an invalid ICC profile, or insufficient disk space fails the
whole `probe` the same way it would fail `convert` — `output_conflicts` on
its own is never a failure; it is only ever a preview the app shows the user
before asking them to confirm `--overwrite`.

Parallel completion order need not match source order. The UI derives overall
progress from counts, never from the largest source index seen.

`progress` may report decoded or staged work. If a group fails or is
cancelled, it emits no `item_done` events for that group's staged files.

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
| `CAPTURE_SPAN_TOO_LONG` | Synthetic times would leave the film date |
| `UNREADABLE_RAW` | File exists but could not be decoded |
| `OUTPUT_SAME_AS_INPUT` | Output folder resolves to the input folder |
| `OUTPUT_NOT_WRITABLE` | Cannot write to the output folder |
| `OUTPUT_NOT_EMPTY` | Nonempty folder with no valid manifest |
| `OUTPUT_CONFLICT` | Existing outputs and no `--overwrite` |
| `INSUFFICIENT_DISK` | Free space below the section 3.9 estimate |
| `INSUFFICIENT_MEMORY` | Explicit `--jobs` exceeds the memory budget |
| `BAD_MANIFEST` | Manifest unreadable or fails its schema |
| `MANIFEST_MISMATCH` | Manifest valid but its run parameters differ |
| `ICC_PROFILE_INVALID` | Bundled profile missing or wrong SHA-256 |
| `TIFF_WRITE_FAILED` | A TIFF or metadata write failed |
| `CANCELLED` | Cooperative user cancellation |

## Exit status

- `0`: complete success.
- `1`: validation, conversion, or partial-run failure.
- `2`: invalid command usage.
- `143`: cooperative user cancellation, matching 128 + SIGTERM.

The event stream, not message text, is the app's machine-readable interface.
