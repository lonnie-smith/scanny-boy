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

Parallel completion order need not match source order. The UI derives overall
progress from counts, never from the largest source index seen.

`progress` may report decoded or staged work. If a group fails or is
cancelled, it emits no `item_done` events for that group's staged files.

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
