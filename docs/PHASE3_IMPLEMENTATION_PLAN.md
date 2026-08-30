# Scanny Boy — Phase 3 implementation plan

**Rolls, staged workflow, and metadata editing.**

Phase 1 built RAW conversion. Phase 2 built registration and stitching. Both
were organised around a single *run*: one input folder, one selection, one
output folder, one command, one manifest. Phase 3 replaces that organising
idea with a **roll** — a durable, named, additive thing you return to — and
splits the app into three stages around it.

This plan is written the way Phase 2's was: to be executed rather than
interpreted. Section 3 is **locked**. Every module, signature, field, error
code, and test name a chunk needs is written down in that chunk's entry. An
agent that finds itself inventing one has left the plan and must stop — see
section 5.1.

Read alongside:

- [`docs/IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) — Phase 1. Its
  section 3 decisions remain in force except where section 2 below names an
  amendment.
- [`docs/PHASE2_IMPLEMENTATION_PLAN.md`](PHASE2_IMPLEMENTATION_PLAN.md) —
  Phase 2. Its sections 2 (verified facts), 3.2–3.5 (registration, colour,
  gates, failure), 3.8 (disk and memory), 3.11 (stitched TIFF format), and
  3.12 (measured constants) are **unchanged and still authoritative**.
  Phase 3 amends its 3.6 (command surface) and 3.7 (output folder and roll
  manifest) substantially, and says so.
- [`shared/contract/CONTRACT.md`](../shared/contract/CONTRACT.md) — the
  CLI/app interface, moving to protocol version 3.
- [`docs/DECISIONS.md`](DECISIONS.md) — gains a Phase 3 section in the final
  chunk.

---

## 0. What is actually changing, and why it is a break

The existing roll manifest is single-run **by construction**. It carries one
`run_id`, one `convert_run_id`, one `input_folder`, one `film_date`, and one
`source_order`; and `check_roll_rerun_matches` rejects any rerun whose
sources, order, grouping, or film date differ from what is recorded. Those
are exactly the things an additive roll must be allowed to change.

So this is not a patch to the roll manifest. It is
`manifest_format_version: 2`, a rewritten `roll_manifest.py`, and
**protocol version 3**.

Three consequences that must be understood before any chunk starts:

1. **`--film-date` disappears from the CLI.** It exists today to seed
   synthetic `DateTimeOriginal` values at convert time. Phase 3 moves date
   assignment to the metadata stage, so the conversion stage no longer has
   anything to do with dates. `film_date.py`, `CAPTURE_SPAN_TOO_LONG`, and
   the app's film-date field all retire.
2. **Stitched TIFFs get a real, truthful date at publish time** — the first
   frame's actual camera capture timestamp — and a synthetic, ordered one
   only when the user applies metadata. A TIFF is therefore never missing
   `DateTimeOriginal`, and never carries a plausible-looking wrong film date
   it did not earn.
3. **There is no migration.** Folders written by Phase 2 are not importable.
   They are test output; regenerate them. Nothing in Phase 3 reads
   `manifest_format_version: 1` of the roll manifest, and the app refuses a
   protocol-2 event stream.

4. **The embedded ICC profile changes, but no pixel does.** Phase 1's
   long-standing profile/curve mismatch is corrected in P3-1 by replacing the
   profile, not by re-encoding anything — see §3.13. Every TIFF's bytes and
   hash change; every TIFF's pixel *values* do not.

### 0.1 Vocabulary added to Phase 2's section 1.1

| Term | Meaning |
| --- | --- |
| **Library** | The one folder holding every roll. `~/Pictures/Scanny Boy` by default; relocatable. |
| **Roll** | One folder in the library, holding a `scanny-boy-roll.json` and the stitched TIFFs of one roll of film. Identified by a UUID; displayed by a user-set name. |
| **Run** | One invocation of `run` or `stitch` that adds negatives to a roll. A roll has one or more, forever. |
| **Stage** | One of the app's three tabs within a roll: **Add Scans**, **Edit**, and (later phases) more. |
| **Sequence** | A negative's position in the roll, derived from capture time across every run. Determines applied timestamps. |
| **Apply** | Writing the metadata stage's intended values into the published TIFFs. |
| **Overlap** | A selected source file whose SHA-256 already appears in the roll. |
| **Supersede** | What Replace does to an overlap: a newly published negative takes over from an existing one whose members it covers, and the old one's TIFF is deleted. §3.4. |

---

## 1. Goal

When Phase 3 is done:

- Launching the app shows a sidebar of rolls, scanned from the library, with
  a **+** to create one. Creating a roll asks for a name and a
  shots-per-negative, and nothing else.
- Selecting a roll opens a workspace with **Add Scans** and **Edit** tabs.
- **Add Scans** is Phase 2's flow, minus the output-folder and film-date
  fields: choose an input folder, select a contiguous range, review the
  grouping, run. Output always goes to the roll. Running again adds more.
- A run that overlaps sources already in the roll shows a per-negative sheet
  before it starts, defaulting to skipping the overlaps.
- **Edit** shows the roll's negatives in sequence with thumbnails of the
  published TIFFs, a roll capture date, per-negative date overrides, and an
  **Apply** button that writes those dates into the TIFFs.
- Re-stitching a negative that had metadata applied re-applies it
  automatically.
- Renaming a roll renames its folder. Deleting one moves the folder to the
  Trash.

Out of scope, with hooks left where they attach: crop from manifest data,
white balance, extended metadata fields (location, camera, lens, film
stock), the cyan fill colour, negative inversion. See section 3.11.

---

## 2. Amendments to Phases 1 and 2

Each of these overrides the named section. Nothing else in either plan
changes.

| Amended | Was | Now |
| --- | --- | --- |
| P1 §3.5 / DECISIONS "Metadata" | User supplies a film date; synthetic noon-plus-elapsed times written at convert | No film date at convert. Intermediates and freshly stitched TIFFs carry the **real** capture time. Synthetic times are assigned in the metadata stage — §3.7 |
| P1 §3.7 / DECISIONS "Output folder" | One output folder holds one run; a rerun must match the previous run's sources, order, grouping, and film date | One roll folder holds **many** runs. The match rule reduces to §3.4's roll-invariants |
| P2 §3.6 | `--out DIR`, `--film-date` required on `convert`/`run` | `--roll DIR`; `--film-date` removed everywhere — §3.5 |
| P2 §3.7 | One output folder holds one stitched roll; TIFF named after the group's first frame | Still first-frame naming, but with collision suffixes across runs — §3.4 |
| P2 §3.11 | `DateTimeOriginal` is the synthetic time Phase 1 computed | `DateTimeOriginal` is the first frame's real capture time until metadata is applied |
| P1 §3.4 / DECISIONS "Pixel output" | Every TIFF embeds `ProPhoto-v4.icc` (true ROMM curve) | Every TIFF embeds a profile whose curve is LibRaw's, matching the pixels. **No pixel value changes** — §3.13 |
| P1 §3.6 code `CAPTURE_SPAN_TOO_LONG` | Raised when synthetic times leave the film date | **Retired.** Rank-based times cannot overflow a day below 43,200 negatives |

---

## 3. Decisions implementation agents must preserve

**This section is locked.** If it looks wrong, say so and wait; do not
improve it in passing.

### 3.1 The library

- The library base is one folder holding every roll as a direct child. Its
  default is `~/Pictures/Scanny Boy`, created on first launch if absent.
  `~/Pictures` is where macOS image applications conventionally put
  user-visible output, and unlike `~/Documents` it is not iCloud-synced by
  default — which matters for multi-gigabyte TIFFs.
- The base is relocatable through a Settings window (`UserDefaults` key
  `libraryBaseFolder`, storing a file URL). Relocating changes where the app
  looks; it never moves files.
- **The filesystem is the source of truth.** There is no index, registry, or
  recents file anywhere. The roll list is produced by scanning the base
  exactly one level deep for child directories containing
  `scanny-boy-roll.json`. Nothing to corrupt, nothing to reconcile, and a
  roll folder moved or deleted in Finder simply is or is not there next
  scan.
- **That scan is the CLI's, not Swift's.** `roll list` (§3.5) performs it and
  reports one entry per child directory, readable or not. The app never walks
  the library itself, never opens a `scanny-boy-roll.json`, and therefore has
  no second copy of the scan rule to drift from this one.
- **Every roll lives under the base.** Roll creation never asks for a
  location. A roll on an external drive is achieved by relocating the whole
  library, not by scattering rolls.
- A child directory holding a `scanny-boy-roll.json` that fails to load or
  validate is listed as an **unreadable roll** with the reason, and cannot be
  opened. It is never hidden and never repaired automatically.
- The app is not sandboxed (Phase 1 decision, unchanged), so plain file URLs
  suffice. No security-scoped bookmarks.

### 3.2 Roll identity and naming

- `roll_id` is a lowercase UUID string, generated at creation, stored **only
  in the manifest**. It never appears in a path. It is the roll's identity
  and never changes.
- `roll_name` is free text the user types. It is not required to be unique.
- The **folder name is a slug of the name**: Unicode-normalised to NFC,
  characters outside `[A-Za-z0-9._-]` and whitespace runs replaced with a
  single `-`, leading/trailing `-` and `.` stripped, truncated to 60
  characters, and lowercased-compared against siblings for collisions.
  `Tri-X, Portland 1998` → `Tri-X-Portland-1998`. A slug that comes out
  empty becomes `roll`. A collision appends `-2`, `-3`, … until free.
- **Renaming a roll renames the folder** to the new slug, with the same
  collision rule. This is why:
  - renaming is refused while any run is active;
  - the rename is a single `Foundation` move, and if it fails the name
    change is not committed either;
  - the manifest's `roll_name` is written **after** the successful move.
- Deleting a roll moves its folder to the Trash with `NSWorkspace.recycle`,
  after one confirmation naming the folder and the number of published
  TIFFs.

### 3.3 The roll manifest, format version 2

`scanny-boy-roll.json`, schema at
`shared/contract/roll-manifest.schema.json`, `manifest_format_version: 2`,
`manifest_kind: "roll"` (was `"stitch"`). The Phase 1 work manifest
(`scanny-boy-manifest.json`, `manifest.schema.json`) is **unchanged**.

Top level:

| Field | Type | Notes |
| --- | --- | --- |
| `manifest_format_version` | `2` | const |
| `manifest_kind` | `"roll"` | const |
| `scanny_boy_version` | string | writer's version |
| `roll_id` | string | UUID, immutable |
| `roll_name` | string | display name |
| `shots_per_negative` | 1–12 | roll-level; §3.4 |
| `created_at` | string | ISO 8601 |
| `updated_at` | string | ISO 8601, rewritten on every write |
| `processing_params` | object | roll-invariant; §3.4 |
| `icc_profile` | `{name, sha256}` | roll-invariant |
| `stitch_params` | object | roll-invariant |
| `runs` | array of `run` | append-only, chronological |
| `sources` | array of `source` | append-only, unique by `sha256` |
| `negatives` | array of `negative` | append-only |
| `metadata` | object | roll-level metadata state |

`run`:

| Field | Type | Notes |
| --- | --- | --- |
| `run_id` | string | as today |
| `short_id` | string | the abbreviation used in `negative_id`; assigned by `append_run`, unique within the roll; §3.4 |
| `kind` | `"run"` \| `"stitch"` | which command produced it |
| `status` | `running` \| `partial` \| `cancelled` \| `complete` | as today |
| `convert_run_id` | string \| null | null for a `stitch` |
| `input_folder` | string \| null | absolute; null for a `stitch` |
| `source_order` | array of string | this run's selection, canonical order |
| `work_dir` | string \| null | set when intermediates were kept |
| `started_at`, `finished_at` | string, string \| null | |

`source` — as Phase 2, plus `run_id` naming the run that first contributed
it. `sources` is keyed by `sha256`: a file already present is never
appended twice, even from a different folder or under a different name.

`negative` — every Phase 2 field (`members`, `expected_output`, `status`,
`output`, `frames`, `pairs`, `global_rms_px`, `canvas`, `valid_rect`,
`fill_color`, `rebate_deviation_px`, `error_code`, `error_message`) plus:

| Field | Type | Notes |
| --- | --- | --- |
| `negative_id` | string | `<run.short_id>-negative-NN`; §3.4 |
| `run_id` | string | the run that produced it |
| `sequence` | integer \| null | 1-based position in the roll; null when superseded; §3.7 |
| `superseded_by` | string \| null | `negative_id` of the negative that replaced it; §3.4 |
| `capture_time` | object | `{source_datetime_original, intended_datetime_original, applied_datetime_original, date_override}`, each string or null |

`metadata` (roll level): `{roll_capture_date, last_applied_at}`, each string
or null. `roll_capture_date` is `YYYY-MM-DD`.

Writes remain temp-file + fsync + rename, exactly as Phase 1 specified, so a
reader never sees a partial manifest.

### 3.4 Roll invariants, additive runs, and naming

- **Roll-invariant** across every run: `shots_per_negative`,
  `processing_params`, `icc_profile.sha256`, `stitch_params`. A run whose
  parameters differ from the manifest's is `ROLL_INVARIANT_MISMATCH` —
  `MANIFEST_MISMATCH` stays with the Phase 1 work manifest, per §3.12.
  Everything
  else — input folder, source list, order, grouping — is expected to differ
  and is never compared. This replaces `check_roll_rerun_matches` entirely.
- `shots_per_negative` is set at roll creation and **locked once any run
  reaches `complete` or `partial`** with at least one completed negative.
  Before that it is editable in the Edit tab.
- `negative_id` is `<run.short_id>-negative-NN`, where `NN` is the existing
  per-run two-digit index. Readable in a log and obviously grouped by run.
  **Uniqueness is enforced, not assumed.** `run_id` is a UUID, so its first
  six characters are six hex digits and two runs can collide; `append_run`
  therefore assigns `short_id` by taking the first six characters of `run_id`
  and, while that value is already held by another run in this roll,
  lengthening to eight, then ten, and finally to the whole `run_id`. The
  chosen value is stored on the run record and never recomputed, so
  `negative_id`s are stable for the life of the roll.
- **Output naming** keeps Phase 2's rule — the stem of the group's first
  member in canonical order, plus `.tif` — with one addition: if that name is
  already claimed by a *different* `negative_id` in the manifest, append
  `-2`, `-3`, … until free. Names are assigned once, at publish, and are
  **never** changed afterwards by reordering, renaming, or re-stitching.
- Nothing in a roll is ever renumbered or renamed as a side effect of adding
  a run. `sequence` changes; filenames do not.

**Replacement.** §3.5 removes `--overwrite` from `run` and says replacing a
negative is expressed by not skipping its sources. That is only true because
of the following rule, which is what actually makes Replace replace something.

- A run may include sources already present in the roll. The new negative is
  published normally: its own `negative_id`, its own name under the collision
  rule above. Nothing is overwritten in place, ever.
- Once that new negative reaches `completed`, every existing non-superseded
  negative whose members' SHA-256s are all present among the new negative's
  members is **superseded** by it: its `superseded_by` is set to the new
  `negative_id`, its `sequence` becomes null, and its published TIFF is
  deleted — in that order, manifest first, so a crash leaves an orphan file
  rather than a dangling record.
- The subset test means an exact rescan supersedes its predecessor, and so
  does a regrouping that merges negatives. A regrouping that *splits* one
  negative into several covers only part of it, so nothing is superseded and
  both the old and the new negatives remain; Phase 3 does not clean that up,
  and §3.11 records where a delete-negative action would attach.
- A superseded negative's record is never removed — `negatives` stays
  append-only — and its output name stays claimed, so `allocate_output_name`
  never reissues it.
- If the TIFF delete fails, the manifest is already correct: the negative is
  superseded and the file is a stray. Emit the `SUPERSEDED_FILE_NOT_REMOVED`
  warning naming the path and carry on. A failed delete never fails a run.

### 3.5 Command surface and protocol version 3

```text
scanny-boy roll init  --library DIR --name NAME --per-negative N
scanny-boy roll list  --library DIR
scanny-boy roll info  --roll DIR

scanny-boy probe      --input DIR [--files FILE ...] [--per-negative N] [--roll DIR]

scanny-boy convert    --input DIR --files FILE ... --out DIR [--per-negative N]
                      [--jobs N] [--overwrite]

scanny-boy stitch     --work DIR --roll DIR [--jobs N] [--overwrite] [--allow-partial]
                      [--negatives ID ...]

scanny-boy run        --input DIR --files FILE ... --roll DIR [--jobs N]
                      [--skip-sources FILE ...] [--work DIR] [--keep-intermediates]

scanny-boy apply-metadata --roll DIR
```

- `--out` becomes `--roll` on `stitch` and `run`. `convert` keeps `--out`,
  because it still writes a work directory rather than a roll.
- **`--film-date` is removed from every command.** `film_date.py` is deleted
  along with its tests and the `CAPTURE_SPAN_TOO_LONG` code.
- **`--overwrite` is removed from `run`.** In a roll, replacing an existing
  negative is expressed by *not* skipping it (`--skip-sources`), which the
  app derives from the overlap sheet: the new negative publishes alongside
  the old one and then supersedes it, per §3.4's replacement rule. `stitch`
  keeps `--overwrite` for the re-stitch path.
- `roll init` creates the folder under `--library` (slug + collision rule)
  and writes an empty v2 manifest. It emits `roll_created` carrying
  `roll_id`, `roll_name`, and `path`. It fails with `ROLL_EXISTS` only if the
  computed folder exists and is not creatable after 99 suffix attempts.
- `roll list` performs §3.1's one-level scan of `--library` and emits a single
  `roll_list` event: an array of `{path, status, reason, roll_id, roll_name,
  negative_count}`, one entry per child directory holding a
  `scanny-boy-roll.json`. `status` is `"ok"` or `"unreadable"`; `reason` is
  the §3.12 code and message for an unreadable roll and null otherwise, and
  the remaining fields are null when unreadable. `negative_count` excludes
  superseded negatives. This carries everything §3.10's sidebar draws, so the
  app builds the whole list from one invocation rather than one `roll info`
  per roll.
- `roll info` loads and validates one roll manifest and emits it as a
  `roll_info` event. This is how Swift reads a roll: **Swift never parses
  `scanny-boy-roll.json` itself, and never enumerates the library itself
  either** — `roll list` and `roll info` are the only two ways in.
- `probe --roll DIR` adds roll-aware validation: the selection is hashed and
  compared against the roll's `sources`, and `probe_result` gains
  `roll_overlap` — an array of `{negative_id, expected_output, run_id,
  overlapping_sources: [filename], group_index}` describing which of *this*
  selection's prospective groups collide with negatives already in the roll.
  It also validates the roll invariants of §3.4 and replaces `--out`'s
  `OUTPUT_NOT_EMPTY` false positive (see `punchlist.md`) by reading the roll
  manifest properly.
- `--skip-sources` names filenames, relative to `--input`, to exclude from
  the run. Excluded files are removed from the selection **before** grouping,
  so a skip must remove a whole group's worth or the run fails
  `NON_CONTIGUOUS_SELECTION` exactly as it would otherwise — the app only
  ever passes whole groups.
- `--negatives` on `stitch` restricts a re-stitch to named `negative_id`s.
- `apply-metadata` is §3.8.
- **Protocol version 3.** The app rejects a version-2 stream. New events:
  `roll_created`, `roll_list`, `roll_info`, `negative_superseded`,
  `metadata_applied`, `metadata_skipped`. Removed: nothing.

### 3.6 Work directories

- The default work directory for `run` is `<roll>/.work/<run_id>/`.
- It is dot-prefixed, so the existing "dot-files are always ignored when
  judging an output folder" rule already skips it, and Finder does not show
  it.
- Retention is Phase 2's rule unchanged: removed on a fully successful run
  unless `--keep-intermediates`; kept on failure or cancellation, with an
  `INTERMEDIATES_KEPT` warning naming the path.
- Because kept work directories now live in the roll, **re-stitch targets are
  discoverable**: the Edit and Add Scans tabs list `<roll>/.work/*` entries
  whose work manifest loads, instead of asking the user to remember a temp
  path.
- `--work` given explicitly still wins and is still never deleted.
  `WORK_SAME_AS_OUTPUT` becomes `WORK_INSIDE_ROLL_REQUIRED`? **No** — the
  check is unchanged and keeps its name and meaning: `--work` must not
  resolve to `--roll` itself. `<roll>/.work/...` is a different directory and
  is fine.

### 3.7 Sequence and the timestamp algorithm

- A roll's negatives are ordered by the **real camera capture time of each
  negative's first member, across every run**, ascending. Ties break by run
  index, then by first member's filename. `sequence` is the 1-based position
  in that order and is recomputed on every manifest write.
- **Superseded negatives are not in the order at all.** A negative with a
  non-null `superseded_by` is excluded before ranking and its `sequence` is
  null, so a replacement takes the position its predecessor held instead of
  pushing every later negative down by one.
- **This is a deliberate trade.** A negative rescanned days later sorts to
  the end of the roll rather than back into strip position. That is accepted:
  the common case is one scanning session in strip order, and the alternative
  (a manual reorder UI) is state the workflow has not yet shown it needs.
  Section 3.11 records where a manual order would attach.
- The applied timestamp is **rank-based, not elapsed-based**: negative with
  `sequence` *n* gets `12:00:00 + (n − 1)` seconds on the roll's capture
  date. Noon for Phase 1's reason (time-zone headroom around the day
  boundary); one second per negative because only the *order* was ever
  meaningful for a film frame, and a rank cannot overflow a day below 43,200
  negatives.
- A negative with a `date_override` uses noon + (its rank *within that
  date's* negatives) seconds instead.
- The whole computation lives in one new module, `roll_sequence.py`, and no
  other module recomputes it.

### 3.8 The metadata stage and `apply-metadata`

- **Intent lives in the manifest; the TIFF is the artefact.** Setting a roll
  capture date or a per-negative override writes
  `capture_time.intended_datetime_original` for the affected negatives and
  changes nothing on disk beyond the manifest.
- A negative is **dirty** when `intended_datetime_original` differs from
  `applied_datetime_original`. The Edit tab shows the count.
- `apply-metadata --roll DIR` processes every dirty negative with
  `status == "completed"`, a null `superseded_by`, and an existing output. A
  superseded negative is never dirty, never counted, and never written to —
  its file is gone:
  1. Verify the published TIFF's size and SHA-256 against
     `negative.output`. On mismatch, emit `metadata_skipped` with
     `OUTPUT_MODIFIED_EXTERNALLY` and move on — **never** rewrite a file the
     roll no longer recognises, and never fail the whole roll for one.
  2. Rewrite `DateTimeOriginal` (36867) and `SubSecTimeOriginal` (37521) in
     the nested EXIF directory using `tifftools`, the same numeric-tag,
     two-pass approach `tiff_exif.py` already uses. Write to a sibling temp
     file, verify it reads back with the expected tags, then rename over the
     original.
  3. Re-hash, update `negative.output.{size,sha256}` and
     `capture_time.applied_datetime_original`, and emit `metadata_applied`.
  4. After the last negative, set `metadata.last_applied_at` and write the
     manifest once.
- No pixel data is read, decoded, or rewritten. This is a tag rewrite.
- `apply-metadata` exits 0 when every dirty negative applied, and 1 when any
  was skipped — with the skipped ones named in the event stream.

### 3.9 Re-stitch and re-apply

A `stitch` that republishes a negative whose
`capture_time.applied_datetime_original` was non-null **re-applies it
automatically** as the final step for that negative, before the manifest is
written. The manifest already records the intent, so the intent is what the
republished file gets. The user is not asked, and nothing is left dirty.

If that re-apply fails, the negative is left `completed` with
`applied_datetime_original` cleared — dirty, visible in the Edit tab, and
recoverable with Apply. A stitch is never failed by a metadata problem.

### 3.10 The app

- **One window.** `NavigationSplitView`: sidebar of rolls, detail is the
  selected roll's workspace.
- Sidebar: rolls sorted by name, each showing name and negative count; a
  **+** toolbar button; context menu with Rename and Delete; unreadable rolls
  shown disabled with their reason. Its whole contents come from one
  `roll list` invocation (§3.5) — name, count, and reason are all in that
  event, and Swift does no filesystem enumeration of its own.
- Workspace: a `Picker(.segmented)` with **Add Scans** and **Edit**, sized so
  more tabs fit later without redesign.
- **Add Scans** is Phase 2's `ContentView` with the output-folder section and
  the film-date field deleted, the shots-per-negative stepper moved to the
  roll, and the overwrite-confirmation replaced by the overlap sheet.
- **Edit** is new: an ordered list of negatives with thumbnails rendered from
  the published TIFFs, the roll capture date, per-negative overrides, the
  dirty count, and Apply. Also the roll's name, folder path, run history,
  and `shots_per_negative` while it is still editable. Superseded negatives
  are hidden; a "Show replaced negatives" toggle reveals them, greyed, naming
  what replaced each, with no controls of their own.
- **One active run app-wide.** While a run is active the sidebar, the tab
  picker, and both stages' controls are disabled, exactly as the
  configuration is already disabled today. There is one `RunModel` and one
  `CLISession`, as now.
- Swift reads rolls only through `roll list` and `roll info`.
  `RollManifest.swift` becomes a decoder for those two event payloads, not a
  file reader and not a directory walker.

### 3.11 What Phase 3 does not cover, and where it would attach

| Deferred | Attaches at |
| --- | --- |
| Crop from manifest data | A `crop` object on `negative`; a third workspace tab; `valid_rect` is already recorded |
| White balance / base neutralisation | `processing_params` (roll-invariant) plus a per-negative override; a fourth tab |
| Extended metadata (location, camera, lens, film stock) | `negative.metadata` and a roll-level default; `apply-metadata` already owns the EXIF write path |
| Cyan fill colour | `fill_color` is already per negative in the manifest |
| Manual negative reordering | An optional `sequence_override` on `negative`, consumed by `roll_sequence.py` ahead of capture time |
| Deleting a negative outright | The Edit tab, reusing §3.4's supersede mechanism with a null replacement |
| Negative inversion | Phase 4 |
| The ICC/gamma mismatch in `punchlist.md` | Unchanged and still open; it changes Phase 1 pixel output and belongs to neither phase |

### 3.12 New and changed stable codes

| Code | Meaning |
| --- | --- |
| `ROLL_NOT_FOUND` | `--roll` has no readable `scanny-boy-roll.json` |
| `ROLL_MANIFEST_UNSUPPORTED` | Manifest is not `manifest_format_version: 2` |
| `ROLL_EXISTS` | `roll init` could not find a free folder name |
| `ROLL_INVARIANT_MISMATCH` | Run parameters differ from the roll's invariants (§3.4) |
| `PER_NEGATIVE_LOCKED` | Attempt to change `shots_per_negative` after a run published |
| `OUTPUT_MODIFIED_EXTERNALLY` | A published TIFF's hash differs from the manifest at apply time |
| `METADATA_WRITE_FAILED` | The EXIF rewrite or its verification failed |
| `SUPERSEDED_FILE_NOT_REMOVED` | Warning: a superseded negative's TIFF could not be deleted; the manifest is correct and the file is a stray (§3.4) |
| `CAPTURE_SPAN_TOO_LONG` | **Retired** |
| `MANIFEST_MISMATCH` | Retained for the work manifest; the roll's variant is `ROLL_INVARIANT_MISMATCH` |

---

### 3.13 The ICC profile matches LibRaw's curve

Phase 1 embeds `ProPhoto-v4.icc`, whose transfer curve is true ROMM, in files
whose pixels carry LibRaw's generalised curve (Phase 2 section 2.3.1). A
viewer honouring the profile therefore renders every Scanny Boy TIFF too dark
in the shadows — 360 LSB at a linear value of 0.05, which is −5.2%, rising to
−20.8% at 0.005 and vanishing at white. Phase 3 fixes it by replacing the
profile, not the pixels.

- **No pixel value changes.** `RAW_PARAMS` keeps `gamma=(1.8, 16)`; `convert`,
  `stitch`, and `run` keep their exact meanings; Phase 2's linearisation is
  already LibRaw's curve and is untouched. What changes is the profile bytes
  embedded in each TIFF, and therefore the TIFF hashes the manifests record.
- **The replacement profile is the bundled one with its TRC swapped.** Every
  other tag — `wtpt`, `chad`, `rXYZ`, `gXYZ`, `bXYZ` — is copied byte for byte
  from `ProPhoto-v4.icc`. The primaries and white point were never wrong;
  only the curve was.
- **LibRaw's curve is exactly expressible as an ICC `parametricCurveType`,
  function type 4** — `Y = (aX + b)^g + c` for `X ≥ d`, `Y = eX + f` below —
  which is the same tag type the bundled profile already uses (it carries
  function type 3, the true-ROMM form). This is authoring a correct profile,
  not approximating one.
- **Parameters, in the decoding direction the ICC TRC requires** (device code
  → linear), derived from Phase 2 section 2.3.1's `offset = 0.006763376143`
  and `encoded breakpoint = 0.008454220179`:

  | Param | Exact | As stored (s15Fixed16) | Raw int |
  | --- | --- | --- | --- |
  | `g` | 1.8 | 1.8000030517578125 | 117965 |
  | `a` | 1 / (1 + offset) | 0.9932861328125 | 65096 |
  | `b` | offset / (1 + offset) | 0.0067138671875 | 440 |
  | `c` | 0 | 0 | 0 |
  | `d` | encoded breakpoint | 0.008453369140625 | 554 |
  | `e` | 1 / 16 | 0.0625 | 4096 |
  | `f` | 0 | 0 | 0 |

- **The s15Fixed16 quantisation is negligible and measured:** across all
  65,536 codes, the stored parameters reproduce LibRaw's exact inverse curve
  to a maximum of **0.179 LSB** and a maximum relative error of **0.049%**,
  against the 360 LSB and 5.2% the mismatch currently causes. The existing
  profile already quantises `g` to 1.8000030517578125, so this is the same
  precedent, not a new compromise. **(measured 2026-08-30)**
- **The profile is generated, committed, and hash-gated.** A deterministic
  generator script produces the bytes; the bytes are committed as a resource;
  a test asserts the generator still reproduces the committed file exactly.
  `icc_profile.py` keeps `PROFILE_FILENAME`, `PROFILE_SHA256`,
  `verify_icc_profile`, and `load_icc_profile` with their current signatures
  and semantics — only the two constants' values change.
- **The profile ID must be recomputed.** The bundled profile carries a
  correct ICC profile ID in header bytes 84–99 (the MD5 of the profile with
  bytes 44–47, 64–67, and 84–99 zeroed). Changing the TRC changes it, and the
  tag table offsets shift because the `para` tag grows from 32 to 40 bytes.
- **This lands early, at P3-1, deliberately.** `icc_profile.sha256` is a roll
  invariant (§3.4): a roll created under the old profile would reject every
  later run with `ROLL_INVARIANT_MISMATCH`. Landing the new profile before
  any roll exists avoids the problem instead of handling it.
- **The old profile is removed**, not kept alongside. Nothing reads it.

## 4. Test rules

Phase 2's section 6 rules all still apply. Added:

- **Never test the library against the real `~/Pictures`.** Every library
  test uses a temporary base directory injected as a parameter. A test that
  reads `FileManager.default.urls(for: .picturesDirectory…)` has gone wrong.
- **Every multi-run behaviour needs a genuine second run**, not a manifest
  hand-edited to look like one had happened. Overlap detection, sequencing,
  naming collisions, and supersession are all cross-run properties and are
  only proved by running twice. "A genuine run" means the roll manifest was
  produced by the real writing code path, never by hand-authored JSON. Before
  P3-4, `run --roll` does not exist yet, so a second run means a second
  `stitch`; from P3-4 on, prefer `run`.
- **Supersession tests assert the whole rule**: the replacement publishes
  under its own name, the predecessor's `superseded_by` and null `sequence`
  are recorded, its TIFF is gone, its name is still claimed, and the
  negatives around it keep their sequence positions. A split regroup must
  supersede nothing.
- **Apply tests must assert the tag round-trips** — write, re-read with
  `tifftools`, compare — and must assert the file's other tags, its ICC
  profile, and its pixel hash are **unchanged**. An apply that quietly
  rewrites pixels is the failure this catches.
- **Test the skips**: an externally-modified TIFF must be skipped and named,
  and the negatives around it must still apply.
- Slug tests cover Unicode, punctuation, emptiness, length, and collision.

---

## 5. Chunks

Fourteen chunks. One branch and one pull request each, merged in order,
using [`docs/PHASE3_CHUNK_PROMPT.md`](PHASE3_CHUNK_PROMPT.md).

**Why this order.** The v2 manifest lands at P3-2, before anything that
writes or reads one: `roll init` writes an empty v2 manifest and `probe
--roll` reads a populated one, so both would otherwise have to hand-author
the v2 shape and hope it matched the module that arrived later. Putting it
first also puts the two Opus chunks together, which costs two model-change
stops for the whole phase rather than four.

### 5.1 The rule that makes this plan safe to execute

Unchanged from Phase 2 section 5.1. If a chunk needs a decision this plan
does not make, **stop and report — do not decide.** Stop if you would have
to name a module, field, event, or code not written here; pick a threshold
not written here; change a signature this plan gives; or relax a test
assertion to make something pass.

### 5.2 Auto-advance

Each chunk carries an **Auto-advance** line.

- **`yes`** — on a green PR the agent may merge it and begin the next chunk
  in the same session, without asking. Merge with `gh pr merge --squash`
  after CI passes; never force-push; never push to `main`.
- **`no`** — stop after the PR is open and report. The reason is always one
  of: the next chunk needs a different model, or the chunk is an approval
  point in section 6.

An agent must also stop, regardless of the marker, if CI fails twice on the
same cause, if a Phase 1 or Phase 2 test would have to change beyond what the
chunk names, or if section 5.1 applies.

### 5.3 Model per chunk

| Chunk | Model | Auto-advance | Why |
| --- | --- | --- | --- |
| P3-0 Contract and protocol v3 | Sonnet 5 | yes | Wide but shallow; every edit written out |
| P3-1 ICC profile matching LibRaw's curve | Sonnet 5 | **no** — model change | Self-contained; every parameter and byte rule given in §3.13 |
| P3-2 Roll manifest v2 | **Opus 5** | yes | Rewrites a module Phase 2's tests cover; highest regression risk in Phase 3 |
| P3-3 Roll-aware `output_folder` and `probe --roll` | **Opus 5** | **no** — model change | Refactors shared Phase 1/2 code that must keep passing |
| P3-4 Library, slugs, `roll init`/`list`/`info` | Sonnet 5 | yes | New modules, given signatures |
| P3-5 `run`/`stitch` against a roll | Sonnet 5 | yes | Orchestration; flags and removals enumerated |
| P3-6 Sequencing and capture times | Sonnet 5 | yes | One small module, formula given |
| P3-7 `apply-metadata` | Sonnet 5 | yes | Follows `tiff_exif.py`'s existing two-pass write |
| P3-8 Re-apply after re-stitch | Sonnet 5 | yes | Small, and P3-7 built the machinery |
| P3-9 Package and verify | Sonnet 5, escalate to Opus 5 on failure | **no** — possible model change | Ordinary until PyInstaller emits an opaque error |
| P3-10 App: library sidebar and roll CRUD | Sonnet 5 | **no** — approval point 6.1 | New IA; worth looking at before building on it |
| P3-11 App: Add Scans stage and overlap sheet | Sonnet 5 | yes | Reworks existing views |
| P3-12 App: Edit stage | Sonnet 5 | yes | Largest Swift chunk; every control specified |
| P3-13 Documentation and v0.3 sign-off | Sonnet 5 | — | Prose |

**Haiku 4.5 is not recommended for any Phase 3 chunk.**

---

### Chunk P3-0 — Contract, protocol v3, and the v2 roll schema

Branch: `p3-chunk-00-contract` · **Model: Sonnet 5** · **Auto-advance: yes**

| File | Change |
| --- | --- |
| `cli/src/scanny_boy/events.py` | `PROTOCOL_VERSION = 3`; add `RollCreated`, `RollList`, `RollInfo`, `NegativeSuperseded`, `MetadataApplied`, `MetadataSkipped`; add §3.12 codes; remove `CAPTURE_SPAN_TOO_LONG` |
| `cli/src/scanny_boy/events_test.py` | cover every addition and the removal |
| `shared/contract/roll-manifest.schema.json` | rewrite for §3.3, including `run.short_id` and `negative.superseded_by` |
| `shared/contract/schema.json` | new events, new `probe_result.roll_overlap`, new codes, removed code |
| `shared/contract/CONTRACT.md` | document §3.5's full surface and §3.12 |
| `cli/src/scanny_boy/roll_manifest_schema_test_support.py` | v2 fixtures |
| `mac/ScannyBoy/CLIBridge/CLIEvent.swift` | version 3, six new kinds |
| `mac/ScannyBoyTests/CLIEventTests.swift` | update every literal |

**Do:** only the contract. No behaviour. `roll_manifest.py` still writes v1
at the end of this chunk and its tests still pass — P3-2 changes that.

**Tests:** `events_test.py::test_protocol_version_is_three`,
`::test_new_event_kinds_round_trip`, `::test_retired_code_absent`;
`CLIEventTests.testRejectsProtocolVersionTwo`.

---

### Chunk P3-1 — An ICC profile that matches LibRaw's curve

Branch: `p3-chunk-01-icc-profile` · **Model: Sonnet 5** · **Auto-advance: no** (P3-2 is Opus)

Implements §3.13. **This chunk changes no pixel value.** If a test that
asserts pixel data starts failing, something is wrong — stop and report.

| File | Change |
| --- | --- |
| `cli/tools/generate_icc_profile.py` | **new** — deterministic generator |
| `cli/src/scanny_boy/resources/ScannyBoy-ROMM-LibRaw-v4.icc` | **new** — its committed output |
| `cli/src/scanny_boy/resources/ProPhoto-v4.icc` | **delete** |
| `cli/src/scanny_boy/icc_profile.py` | new `PROFILE_FILENAME` and `PROFILE_SHA256` values; add the seven curve constants |
| `cli/src/scanny_boy/icc_profile_test.py` | extend |
| `cli/src/scanny_boy/packaging_test.py` | the `rglob` for the bundled profile follows the new name |
| `cli/src/scanny_boy/probe_test.py` | fixture profile name |
| `THIRD_PARTY_NOTICES.md` | replace the `ProPhoto-v4.icc` entry with a derivation note |
| `docs/punchlist.md` | strike the ICC entry's first bullet as done |

**`generate_icc_profile.py` does exactly this and nothing more:**

1. Read the committed `ProPhoto-v4.icc` bytes, vendored into the script as a
   base64 constant so the generator keeps working after the file is deleted.
2. Copy every tag except `rTRC`/`gTRC`/`bTRC` byte for byte.
3. Emit one `para` tag, function type 4, with §3.13's seven s15Fixed16
   parameters, shared by all three TRC tags exactly as the original shares
   its type-3 tag.
4. Rebuild the tag table with corrected offsets, set the `desc` tag to
   `Scanny Boy ROMM RGB (LibRaw transfer curve)`, keep `cprt` as the
   upstream CC0 dedication, and pad to a 4-byte boundary.
5. Recompute the header size field and the ICC profile ID (MD5 with bytes
   44–47, 64–67, and 84–99 zeroed) per §3.13.
6. Write the file. Given the same input it must produce byte-identical
   output every time — no timestamps, no randomness, and the `dtim` header
   field copied from the source rather than set to now.

**`icc_profile.py` adds these constants and nothing else**, so the curve
lives in exactly one place:

```python
PROFILE_FILENAME = "ScannyBoy-ROMM-LibRaw-v4.icc"
PROFILE_SHA256 = "<the generated file's hash>"

# Section 3.13. ICC parametricCurveType function type 4, decoding direction.
TRC_FUNCTION_TYPE = 4
TRC_G = 117965
TRC_A = 65096
TRC_B = 440
TRC_C = 0
TRC_D = 554
TRC_E = 4096
TRC_F = 0
```

**Tests** — `icc_profile_test.py`:

- `test_generator_reproduces_the_committed_profile` — run the generator,
  compare bytes exactly. This is what keeps the committed binary honest.
- `test_committed_profile_hash_matches_the_constant`
- `test_trc_tag_is_parametric_type_four_with_the_section_313_parameters` —
  parse the emitted `para` tag and assert all seven raw integers.
- `test_three_trc_tags_share_one_offset` — as the original does.
- `test_primaries_white_point_and_chad_are_byte_identical_to_prophoto` —
  against the base64 constant.
- `test_profile_id_is_the_specified_md5`
- `test_decoded_curve_matches_libraw_within_quarter_of_an_lsb` — evaluate
  the stored parameters against LibRaw's exact inverse over all 65,536
  codes and assert max error < 0.25 LSB, which §3.13 measured at 0.179.
- `test_decoded_curve_is_monotonic_and_spans_zero_to_one`
- `test_load_icc_profile_still_verifies_and_returns_bytes` — unchanged
  behaviour, new file.

**Verification to paste in the PR:** convert one real NEF before and after
the change and show that the pixel data is byte-identical while the embedded
profile differs — e.g. compare `tifffile` page arrays for equality and the
extracted `iccprofile` bytes for inequality. If the pixels differ at all,
the chunk has gone wrong.

---

### Chunk P3-2 — The v2 roll manifest

Branch: `p3-chunk-02-roll-manifest` · **Model: Opus 5** · **Auto-advance: yes**

| File | Change |
| --- | --- |
| `cli/src/scanny_boy/roll_manifest.py` | rewrite for §3.3 |
| `cli/src/scanny_boy/roll_manifest_test.py` | rewrite |
| `cli/src/scanny_boy/stitch_pipeline.py` | write v2 records; `negative_id` and output-name rules of §3.4 |
| `cli/src/scanny_boy/stitch_pipeline_test.py` | update |

Add `RunRecord` (with `short_id`); extend `NegativeRecord` with `run_id`,
`sequence`, `superseded_by`, `capture_time`; add `CaptureTime`. Replace
`check_roll_rerun_matches` with:

```python
def check_roll_invariants(manifest: RollManifest, candidate_params: RollInvariants) -> None: ...
def append_run(manifest: RollManifest, run: RunRecord) -> None: ...
def merge_sources(manifest: RollManifest, sources: list[SourceRecord], run_id: str) -> None: ...
def allocate_output_name(manifest: RollManifest, first_member: str, negative_id: str) -> str: ...
def format_negative_id(short_id: str, index: int) -> str: ...
def mark_superseded(manifest: RollManifest, replacement: NegativeRecord) -> list[NegativeRecord]: ...
```

`merge_sources` deduplicates by `sha256`. `allocate_output_name` implements
§3.4's suffix rule and is the **only** place output names are chosen; a
superseded negative still holds its name, so a name is never reissued.
`append_run` assigns `short_id` per §3.4, lengthening it until it is free
within the roll. `mark_superseded` applies §3.4's subset rule, sets
`superseded_by`, nulls `sequence`, and **returns the records it superseded
without touching the filesystem** — deleting their TIFFs and emitting
`negative_superseded` is P3-5's job.

**This chunk is the regression risk of Phase 3.** Phase 2's stitch tests
must keep proving what they proved; where a test asserts a v1 manifest shape
it is updated to the v2 shape and nothing else. A test whose *meaning* has
to change is a stop-and-report.

**Tests:** `test_v2_round_trips`, `test_rejects_format_version_one`,
`test_merge_sources_deduplicates_by_hash`,
`test_append_run_preserves_earlier_negatives`,
`test_append_run_lengthens_a_colliding_short_id`,
`test_allocate_output_name_suffixes_on_collision`,
`test_allocate_output_name_is_stable_across_reordering`,
`test_allocate_output_name_never_reissues_a_superseded_name`,
`test_check_roll_invariants_rejects_changed_per_negative`,
`test_check_roll_invariants_ignores_changed_input_folder`,
`test_negative_ids_unique_across_two_runs`,
`test_mark_superseded_covers_an_exact_member_match`,
`test_mark_superseded_covers_a_merging_regroup`,
`test_mark_superseded_ignores_a_splitting_regroup`,
`test_mark_superseded_nulls_the_sequence`.

---

### Chunk P3-3 — Roll-aware output folder and `probe --roll`

Branch: `p3-chunk-03-probe-roll` · **Model: Opus 5** · **Auto-advance: no** (P3-4 is Sonnet)

| File | Change |
| --- | --- |
| `cli/src/scanny_boy/output_folder.py` | `ROLL_RULES` under additive semantics; dot-dir skip covers `.work` |
| `cli/src/scanny_boy/output_folder_test.py` | update and extend |
| `cli/src/scanny_boy/probe.py` | `--roll`, overlap detection, `roll_overlap` |
| `cli/src/scanny_boy/probe_test.py` | extend |
| `cli/src/scanny_boy/cli.py` | `probe --roll` |

`FolderRules` already generalises this module; **extend it, do not fork it.**
Additive rolls change one thing: a nonempty roll folder holding published
outputs from earlier runs is **normal**, not `OUTPUT_NOT_EMPTY`. Also closes
`punchlist.md`'s `probe --out` false positive.

Overlap detection hashes the selection, compares against `manifest.sources`
by `sha256`, and reports per prospective group.

The rolls these tests probe are built by a genuine `stitch` through P3-2's
writer, per §4 — `run --roll` does not exist until P3-5, and a hand-authored
manifest proves nothing about overlap.

**Tests:** `test_roll_folder_with_prior_outputs_is_valid`,
`test_roll_overlap_empty_for_fresh_sources`,
`test_roll_overlap_names_the_prior_negative`,
`test_roll_overlap_detects_renamed_file_by_hash`,
`test_roll_overlap_detects_regrouped_sources`,
`test_probe_rejects_changed_shots_per_negative`.

---

### Chunk P3-4 — Library, slugs, and the roll commands

Branch: `p3-chunk-04-library` · **Model: Sonnet 5** · **Auto-advance: yes**

| File | Change |
| --- | --- |
| `cli/src/scanny_boy/roll_folder.py` | **new** |
| `cli/src/scanny_boy/roll_folder_test.py` | **new** |
| `cli/src/scanny_boy/cli.py` | `roll init`, `roll list`, `roll info` subcommands |
| `cli/src/scanny_boy/cli_test.py` | argument-parsing coverage |

`roll_folder.py`:

```python
SLUG_MAX_LENGTH = 60
FALLBACK_SLUG = "roll"

@dataclasses.dataclass(frozen=True)
class RollListing:
    path: Path
    status: str                    # "ok" | "unreadable"
    reason: tuple[str, str] | None # (code, message) when unreadable
    roll_id: str | None
    roll_name: str | None
    negative_count: int | None

def slugify(name: str) -> str: ...
def unique_folder_name(library: Path, slug: str) -> str: ...
def create_roll(library: Path, name: str, shots_per_negative: int) -> Path: ...
def rename_roll(roll_dir: Path, new_name: str) -> Path: ...
def list_rolls(library: Path) -> list[Path]: ...
def scan_library(library: Path) -> list[RollListing]: ...
```

`create_roll` writes an empty v2 manifest through P3-2's writer — no
negatives, no runs, no sources — and returns the roll directory.
`rename_roll` moves the folder first, then writes `roll_name`, and raises
without changing either on failure. `list_rolls` scans one level deep and
returns directories holding a `scanny-boy-roll.json`, unsorted; it does not
load them. `scan_library` loads each one and is what the `roll list` event
of §3.5 is assembled from; `negative_count` excludes superseded negatives,
and a manifest that fails to load becomes an `"unreadable"` listing carrying
its §3.12 code rather than raising.

**Tests:** `roll_folder_test.py` covering `test_slugify_*` (unicode,
punctuation, empty, overlong, whitespace runs), `test_unique_folder_name_*`
(free, single collision, many collisions, exhaustion → `ROLL_EXISTS`),
`test_create_roll_writes_empty_v2_manifest`,
`test_rename_roll_moves_folder_and_updates_name`,
`test_rename_roll_leaves_everything_alone_on_move_failure`,
`test_list_rolls_ignores_directories_without_a_manifest`,
`test_scan_library_reports_ok_and_unreadable_side_by_side`,
`test_scan_library_negative_count_excludes_superseded`,
`test_roll_list_emits_one_event_for_the_whole_library`.

---

### Chunk P3-5 — `run` and `stitch` against a roll

Branch: `p3-chunk-05-run-roll` · **Model: Sonnet 5** · **Auto-advance: yes**

| File | Change |
| --- | --- |
| `cli/src/scanny_boy/cli.py` | `--roll`, `--skip-sources`, `--negatives`; remove `--film-date` and `run --overwrite` |
| `cli/src/scanny_boy/run_pipeline.py` | roll semantics; `<roll>/.work/<run_id>` default; §3.4's supersede step |
| `cli/src/scanny_boy/stitch_pipeline.py` | `--negatives` restriction |
| `cli/src/scanny_boy/pipeline.py` | drop film-date plumbing |
| `cli/src/scanny_boy/metadata.py`, `tiff_exif.py` | write the **real** capture time |
| `cli/src/scanny_boy/film_date.py`, `film_date_test.py` | **delete** |
| all affected `*_test.py` | update |

**Supersession is executed here.** After a negative publishes, call
`mark_superseded` (P3-2), then for each record it returns delete the
published TIFF and emit `negative_superseded` naming the old and new
`negative_id`s. Manifest first, file second; a failed delete emits
`SUPERSEDED_FILE_NOT_REMOVED` and never fails the run.

**Tests:** `test_run_appends_to_an_existing_roll`,
`test_run_default_work_dir_is_inside_the_roll`,
`test_skip_sources_excludes_whole_groups`,
`test_skip_sources_partial_group_is_a_usage_error`,
`test_stitched_tiff_carries_real_capture_time`,
`test_film_date_argument_is_rejected`,
`test_unskipped_overlap_supersedes_the_prior_negative`,
`test_superseded_tiff_is_deleted_and_its_name_stays_claimed`,
`test_superseded_negative_leaves_neighbours_sequence_unchanged`,
`test_failed_supersede_delete_warns_but_does_not_fail_the_run`.

---

### Chunk P3-6 — Sequencing and capture times

Branch: `p3-chunk-06-sequence` · **Model: Sonnet 5** · **Auto-advance: yes**

| File | Change |
| --- | --- |
| `cli/src/scanny_boy/roll_sequence.py` | **new** |
| `cli/src/scanny_boy/roll_sequence_test.py` | **new** |
| `cli/src/scanny_boy/roll_manifest.py` | recompute `sequence` on write |

```python
NOON = datetime.time(12, 0, 0)

def sequence_negatives(manifest: RollManifest) -> list[str]: ...
def intended_times(manifest: RollManifest) -> dict[str, datetime.datetime]: ...
```

`sequence_negatives` returns `negative_id`s ordered by §3.7's rule.
`intended_times` applies noon + rank seconds, honouring `date_override`.
Both are pure functions of the manifest.

**Tests:** `test_sequence_orders_by_capture_time_across_runs`,
`test_sequence_ties_break_by_run_then_filename`,
`test_intended_times_are_one_second_apart`,
`test_date_override_reranks_within_its_own_date`,
`test_sequence_is_stable_when_nothing_changed`,
`test_superseded_negatives_are_excluded_from_the_order`.

---

### Chunk P3-7 — `apply-metadata`

Branch: `p3-chunk-07-apply-metadata` · **Model: Sonnet 5** · **Auto-advance: yes**

| File | Change |
| --- | --- |
| `cli/src/scanny_boy/apply_metadata.py` | **new** |
| `cli/src/scanny_boy/apply_metadata_test.py` | **new** |
| `cli/src/scanny_boy/cli.py` | `apply-metadata` subcommand |

Implements §3.8 exactly, reusing `tiff_exif.py`'s numeric-tag two-pass
write. Temp file, verify, rename, re-hash, update manifest, one manifest
write at the end.

**Tests:** `test_applies_intended_time_and_rehashes`,
`test_other_tags_and_icc_profile_are_unchanged`,
`test_pixel_data_is_byte_identical_after_apply`,
`test_externally_modified_tiff_is_skipped_and_named`,
`test_skip_does_not_block_other_negatives`,
`test_clean_negatives_are_not_rewritten`,
`test_exit_status_one_when_anything_was_skipped`.

---

### Chunk P3-8 — Re-apply after re-stitch

Branch: `p3-chunk-08-reapply` · **Model: Sonnet 5** · **Auto-advance: yes**

`stitch_pipeline.py` calls `apply_metadata` for a republished negative whose
`applied_datetime_original` was non-null, per §3.9, and clears the field
rather than failing the stitch if that fails.

**Tests:** `test_restitch_reapplies_metadata`,
`test_restitch_of_never_applied_negative_does_not_apply`,
`test_failed_reapply_leaves_negative_dirty_not_failed`.

---

### Chunk P3-9 — Package and verify

Branch: `p3-chunk-09-package` · **Model: Sonnet 5, escalate to Opus 5** · **Auto-advance: no**

Rebuild the frozen CLI; verify `roll init`, `roll list`, `roll info`,
`probe --roll`, `run --roll`, and `apply-metadata` all work from
`cli/dist/ScannyBoyCLI.app`, not just from source. Paste real command output
in the PR.

---

### Chunk P3-10 — App: library sidebar and roll CRUD

Branch: `p3-chunk-10-app-library` · **Model: Sonnet 5** · **Auto-advance: no** (approval point 6.1)

| File | Change |
| --- | --- |
| `mac/ScannyBoy/Model/RollLibrary.swift` | **new** — list via `roll list`; create, rename, delete |
| `mac/ScannyBoy/Model/Roll.swift` | **new** — decoded `roll_list` and `roll_info` payloads |
| `mac/ScannyBoy/Views/RollSidebar.swift` | **new** |
| `mac/ScannyBoy/Views/NewRollSheet.swift` | **new** |
| `mac/ScannyBoy/Views/SettingsView.swift` | **new** — library base |
| `mac/ScannyBoy/App/ScannyBoyApp.swift` | `Settings` scene |
| `mac/ScannyBoy/Views/ContentView.swift` | `NavigationSplitView` shell with the tab picker |
| `mac/ScannyBoy/Model/RollManifest.swift` | becomes the `roll_info` decoder |
| `mac/ScannyBoyTests/RollLibraryTests.swift` | **new** |

`RollLibrary.swift` builds the sidebar from one `roll list` invocation and
**does no directory enumeration of its own** — no `contentsOfDirectory`, no
manifest parsing. Rename and delete stay in Swift (`Foundation` move,
`NSWorkspace.recycle`); reading does not. The library base is injected, never
read from `.picturesDirectory` in tests. Delete confirms first, naming the
folder and the TIFF count.

**Tests:** `testScanFindsOnlyDirectoriesWithAManifest`,
`testUnreadableRollIsListedWithItsReason`,
`testLibraryIsBuiltFromRollListWithoutEnumeratingTheFilesystem`,
`testRenameMovesTheFolder`, `testRenameIsRefusedDuringARun`,
`testDeleteRecyclesTheFolder`.

---

### Chunk P3-11 — App: Add Scans stage and the overlap sheet

Branch: `p3-chunk-11-app-add-scans` · **Model: Sonnet 5** · **Auto-advance: yes**

`ConfigurationModel` loses `filmDate`, `outputFolder`, `outputConflicts`,
`existingRoll`, `overwriteConfirmed`, and `perNegative` (now the roll's) and
gains `roll` and `rollOverlap`. `OverlapSheet.swift` is new: one row per
overlapping prospective negative with a Skip/Replace toggle **defaulting to
Skip**, and non-overlapping groups always proceeding. The sheet's decisions
become `--skip-sources`.

**Replace must say what it does.** Choosing it supersedes the named
negative and deletes its TIFF (§3.4), so the row names the negative being
replaced and the sheet's footer states how many files will be deleted.
Skip is the default precisely because Replace is destructive.

**Tests:** `testOverlapSheetDefaultsToSkip`,
`testSkipDecisionsBecomeSkipSourcesArguments`,
`testReplaceOmitsTheSourcesFromSkipSources`,
`testSheetReportsTheCountOfFilesReplaceWouldDelete`,
`testNonOverlappingGroupsAlwaysRun`,
`testRunCommandOmitsFilmDateAndOutputFolder`.

---

### Chunk P3-12 — App: Edit stage

Branch: `p3-chunk-12-app-edit` · **Model: Sonnet 5** · **Auto-advance: yes**

| File | Change |
| --- | --- |
| `mac/ScannyBoy/Views/EditStageView.swift` | **new** |
| `mac/ScannyBoy/Model/EditModel.swift` | **new** |
| `mac/ScannyBoy/Model/ThumbnailLoader.swift` | extend to downsampled TIFFs via `CGImageSourceCreateThumbnailAtIndex` with `kCGImageSourceThumbnailMaxPixelSize` |
| `mac/ScannyBoyTests/EditModelTests.swift`, `ThumbnailLoaderTests.swift` | extend |

Contents: negatives in `sequence` order with thumbnails, source frames, and
quality metrics; roll capture date picker; per-negative date override; dirty
count; **Apply**, which invokes `apply-metadata` through the existing
`RunModel`/`CLISession` path and reports applied and skipped negatives;
roll name field; folder path; run history; `shots_per_negative` while
unlocked, with `PER_NEGATIVE_LOCKED` surfaced when it is not. Superseded
negatives are hidden behind a "Show replaced negatives" toggle, per §3.10.

**Tests:** `testDirtyCountReflectsIntendedVersusApplied`,
`testApplyIsDisabledWhenNothingIsDirty`,
`testSkippedNegativesAreReportedByName`,
`testSupersededNegativesAreHiddenUntilTheToggleIsOn`,
`testSupersededNegativesAreNotCountedAsDirty`,
`testThumbnailLoadsFromAStitchedTIFFWithoutDecodingFullResolution`.

---

### Chunk P3-13 — Documentation and v0.3 sign-off

Branch: `p3-chunk-13-documentation` · **Model: Sonnet 5**

`README.md` "Using the app" rewritten around rolls and the three stages;
`DECISIONS.md` gains a Phase 3 section mirroring section 3 here; version to
`0.3.0`.

`punchlist.md` is updated to match what §3.11 actually says, and no further:

- The ICC entry closed in P3-1 and the `probe --out` entry closes in P3-3;
  strike both.
- §3.11's deferred rows — crop, white balance, extended metadata, cyan fill,
  manual reordering, deleting a negative — come out of the "Phase 3" list and
  are re-filed **unscheduled**, each carrying the attachment point §3.11
  gives it. §3.11 assigns them no phase, so neither does the punchlist.
- **Negative inversion is the only item §3.11 puts in Phase 4**, and it is
  already filed there. Leave it.
- The purpose-built rebate-deviation detector is not in §3.11 at all. It is
  untouched by Phase 3; leave it exactly where it is.

---

## 6. Approval and pause points

### 6.1 Approval points — hard stops

- **After P3-10**, before the stages are built on it: the user looks at the
  sidebar, creates a roll, renames it, deletes it. The IA is cheap to change
  here and expensive to change after P3-11 and P3-12.
- **Before P3-2 merges**, if any Phase 2 stitch test's *meaning* would have
  to change rather than its manifest fixture shape.

### 6.2 Pause points

- After P3-5, run one real roll end to end before building the metadata
  stage on top of it.
- After P3-7, confirm on a real file — in whatever application you actually
  import into — that the applied date reads correctly and the image is
  untouched.

---

## 7. Risks

| Risk | Mitigation |
| --- | --- |
| P3-2 breaks Phase 2's stitching subtly | Opus 5; the rule that a test whose meaning changes is a stop-and-report |
| A Replace deletes a TIFF the user wanted | Skip is the default, the sheet names every file Replace would delete, and the manifest is written before the file is removed so a superseded record is never dangling |
| The new ICC profile silently changes pixel output | P3-1's before/after byte-identical pixel check, and a generator test that pins the committed bytes |
| The EXIF rewrite corrupts a published TIFF | Temp file, verify-then-rename, hash gate, and a test asserting pixels are byte-identical |
| Global capture-time ordering surprises on a rescan | Documented as an accepted trade in §3.7, with the attachment point for a manual order in §3.11 |
| Folder rename leaves the app and disk disagreeing | Move first, write the name second, refuse during a run |
| Thumbnailing a multi-gigabyte TIFF stalls the UI | `CGImageSourceCreateThumbnailAtIndex` with a max pixel size; never a full decode; a test asserts it |
| Auto-advance merges a chunk that quietly widened scope | The Auto-advance rule requires green CI and forbids touching anything outside the chunk's file table |
