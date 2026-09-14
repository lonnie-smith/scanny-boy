# Per-roll flat-field reference

The bare-light gain map is attached per roll, not baked into the scanning-rig
profile. A roll with no reference converts with zero flat-field correction,
exactly like omitting `--rig` on a run that has no attached block.

This document mirrors `REBATE_ANCHORING.md` §3 for the `flat_field` manifest
block.

## 3.1 The manifest block

`RollManifest.flat_field` is a nullable JSON object on the roll row (migration
0016). Add it to `to_dict()` / `load_roll` the same way as `film_base`.

Block shape (written by `roll set-flatfield-reference`):

```json
{
  "gain_map_path": "/…/flatfield/rolls/{roll_id}.npz",
  "gain_map_sha256": "…",
  "source_name": "_DSC5001.NEF",
  "source_sha256": "…",
  "reference_width": 6064,
  "reference_height": 4040,
  "rig_profile_id": "a1b2c3d4-…",
  "params": { "gain_map_max_edge": 256, "…": "…" },
  "locked_at": null,
  "attached_at": "2026-09-13T18:04:11Z"
}
```

- `rig_profile_id` is nullable — it records whose CA scales decoded the
  reference when the mode is `"scale"`.
- `locked_at` is `null` while replaceable, an ISO-8601 UTC timestamp once
  locked. **This is the only lock state.**

## 3.2 The state machine

```
       roll init
           │
           ▼
   ┌───────────────┐   roll set-flatfield-reference
   │    ABSENT     │ ─────────────────────────────────────────┐
   │ flat_field=None│                                         │
   └───────────────┘                                         ▼
           │                                       ┌───────────────────┐
           │ run / probe (no correction)           │     ATTACHED      │
           ▼                                       │  locked_at = null │
    (optional — not an error)                      └───────────────────┘
                                                     │        │       ▲
                          run publishes ────────────┘        │       │
                          the roll's first negative            │       │
                                   │                           │       │
                                   ▼           roll set-flatfield ────┘
                          ┌───────────────────┐   (replaces freely)
                          │      LOCKED       │
                          │ locked_at = <ts>  │
                          └───────────────────┘
                                   │
                     roll set-flatfield-reference → FLATFIELD_REFERENCE_LOCKED
```

Rules:

1. **`roll set-flatfield-reference` on ABSENT or ATTACHED**: decode the bare-light
   frame, build the gain map, write the `.npz`, write the block with
   `locked_at: null`, emit `flat_field_reference_set`. While replacing an
   ATTACHED block, delete the orphaned `.npz` first.
2. **`roll set-flatfield-reference` on LOCKED**: error `FLATFIELD_REFERENCE_LOCKED`.
3. **Lock write**: in `stitch_pipeline`'s publish path, alongside the
   `film_base` lock — when the first negative publishes, set
   `flat_field["locked_at"]` if the block exists and is still unlocked.

## 3.3 One writer

`roll set-flatfield-reference` is the **only** writer of `flat_field`. Runs
read the attached block; they never take a `--flatfield` flag for the gain map.

`roll set-base-frame` auto-uses `roll.flat_field`'s gain map when present
(`gain_map=None` otherwise).

## 3.4 Run-time checks

- **`FLATFIELD_REFERENCE_RIG_CONFLICT`** (warning): when `run`/`probe --roll`
  and `roll.flat_field.rig_profile_id` differs from this run's `--rig`. Warn,
  do not block.
- **`FLATFIELD_ASPECT_MISMATCH`** (warning): compare frame aspect against
  `reference_width` / `reference_height` on the roll block.
- **`FLATFIELD_GAIN_MAP_MISSING`**: the `.npz` is absent or corrupt.
