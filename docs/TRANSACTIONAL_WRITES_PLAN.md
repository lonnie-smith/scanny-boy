# Transactional roll writes plan

Every command that changes a roll today writes the **whole roll** back from
a copy it loaded earlier. Two writers that overlap therefore lose each
other's changes, and a lost negative row takes its edits with it. Only the
per-roll file lock (`roll_lock.py`) prevents that.

This plan replaces snapshot saves with **short read-modify-write
transactions against fresh state**. A writer does its slow work (decode,
composite, TIFF rewrite, preview render) outside the database. Then it opens
a transaction that reloads the roll, applies only its own change, and
commits. Concurrent writers then serialize inside SQLite instead of
overwriting each other.

Scope is the Python CLI only. The app never opens the database: it only
sets `SCANNY_BOY_LIBRARY_DB` (`AppEnvironment.swift`). Nothing changes on
the wire, so there is no protocol bump.

---

## 1. Today

### 1.1 Storage

The roll lives in the SQLite library database
(`~/Library/Application Support/ScannyBoy/library.db`, `library/db.py`). It
runs WAL, `foreign_keys=ON` and `busy_timeout=30000`. The tables are in
`library/models.py`: `rolls`, `runs`, `sources` and `negatives` keyed by
roll, and `edits` keyed by negative with `ON DELETE CASCADE`.

### 1.2 The one write path

`roll_manifest.write_roll_manifest(dir, manifest)` stamps `updated_at`,
recomputes every negative's `sequence` (`roll_sequence.sequence_negatives`)
and calls `library/repo.save_roll(dir, manifest)`. That function:

- overwrites every column of the `rolls` row from the manifest;
- **deletes every `runs` and `negatives` row not in the manifest** (a
  negative's delete cascades its `edits`), then merges every remaining row;
- deletes all `sources` rows and re-inserts the manifest's list.

So `save_roll` is "make the database equal this copy". With a stale copy,
that silently reverts or deletes other writers' work.

`load_roll` and `save_roll` each open their own session
(`repo._session()`), so a load and a later save are never one transaction,
even when nothing slow happens in between.

### 1.3 Who writes (production call sites)

| Site | What it actually changes |
|---|---|
| `roll_folder.create_roll` | Inserts a new roll |
| `roll_folder.set_film_kind` | `film`, `published_icc_profile`; refuses if the roll has runs |
| `roll_folder.set_setup` | `setup` (merge) |
| `roll_folder.rename_roll` | `roll_name`, `folder_path` (after `os.rename`) |
| `cli._run_roll_set_base_frame` (~1599) | `film_base`; refuses if `film_base.locked_at` is set |
| `cli` set-flatfield-reference (~2158) | `flat_field`; replaces an unlocked gain-map file |
| `metadata_edit` (~228) | Roll and negative metadata fields; every negative's `capture_time.intended_*` via `apply_intended_times` |
| `apply_metadata` (~198) | Per negative: `output.size/sha256`, `capture_time.applied_*`; roll `metadata.last_applied_at` |
| `edits._refresh_preview` (~137) | One negative's `preview_path` |
| `edits.run_edit_delete` (~901) | Removes negatives (cascading edits); `highlight_lock`; `normalization.auto_neutral` |
| `previews.sync_previews` (~1682) | `preview_path` on many negatives |
| `roll_refresh.run_roll_refresh` (~101) | `highlight_lock`, `normalization.auto_neutral`, `refresh_pending` |
| `stitch_pipeline.run_stitch` (1428, 1570, 1823, 2477) | See §1.4 |

`repo.append_edit` and its typed wrappers (`append_tone_edit`, …) are
already single-transaction inserts and need no change.

### 1.4 The stitch

`run_stitch` loads the roll once, through `plan_rerun`, and keeps that copy
as its working state for the whole run (`roll`). It writes the whole copy
back:

1. **Step 7, the "running" write.** It seeds `processing_params`,
   `stitch_params`, `icc_profile` and `camera_color`, appends the run
   (`append_run` assigns `short_id`), merges sources, appends new negative
   records (names from `allocate_output_name`) and resets adopted ones to
   `pending`.
2. **`_record_failure`.** Sets one record's `status` and `error_*`.
3. **Each publish (2477).** Writes the record's full result: `output`,
   `normalization`, `canvas` and so on. On the roll's first publish it also
   sets `film_base.locked_at` and `flat_field.locked_at`. It removes the
   covered negatives that were not adopted (`_remove_covered_negatives`),
   and may re-apply metadata (`_maybe_reapply_metadata`).
4. **The end of the run (1570).** Sets the run's `status`, `finished_at`
   and `normalization_aggregate`, plus `highlight_lock` (or
   `refresh_pending` when deferred).

---

## 2. Goal and non-goals

**Goal.** No writer can lose another writer's committed change, *even with
the roll lock removed*. Every roll write becomes one SQLite transaction that:

1. takes the database write lock up front (`BEGIN IMMEDIATE`);
2. loads the roll fresh, inside that transaction;
3. applies only the caller's own change;
4. recomputes derived state (`updated_at`, `sequence`);
5. commits.

Preconditions the caller depends on (the film-base lock, "no runs yet", a
negative still existing) are re-checked inside the transaction.

**Non-goals for this plan:**

- **Removing or narrowing `roll_lock.py`.** The lock stays exactly as it
  is. It still guards things that are not database rows: staging
  directories and recovery cleanup (`output_folder.apply_recovery_cleanup`
  treats the last run as the owner), published TIFF files, and the
  stitch's order-dependent normalization clamp. Narrowing it is the later
  parallel-stitching work, and this plan is its prerequisite.
- **Schema changes or migrations.** None are needed.
- **App (Swift) changes.**
- **Faster saves.** A transaction still rewrites the whole roll's rows
  from the fresh copy. That is correct because the copy is fresh and the
  write lock is held, and at today's roll sizes (tens of negatives) it is
  fast. Writing only changed rows is a possible later optimization.

---

## 3. Design

### 3.1 `BEGIN IMMEDIATE` in `library/db.py`

Python's `sqlite3` driver normally begins a transaction lazily, at the
first INSERT, UPDATE or DELETE, not at the first SELECT. A read-then-write
inside one session therefore reads a WAL snapshot without holding the write
lock. If another connection commits in between, the later write fails with
`SQLITE_BUSY` ("database is locked"), and `busy_timeout` does **not** retry
a snapshot that has gone stale. Only `BEGIN IMMEDIATE` makes the read and
the write one atomic unit: it takes the write lock first, and waits up to
`busy_timeout` for it.

Implement SQLAlchemy's documented pysqlite recipe (SQLAlchemy 2.x docs,
"Serializable isolation / Savepoints / Transactional DDL" under the SQLite
dialect):

- In the existing `connect` listener (`_set_sqlite_pragmas`), set
  `dbapi_connection.isolation_level = None` so the driver stops emitting
  its own `BEGIN`.
- Add a `begin` engine event that emits the `BEGIN` itself:
  `BEGIN IMMEDIATE` when the connection's execution options carry a flag
  (for example `sqlite_immediate=True`), plain `BEGIN` otherwise.

Every existing session keeps working: each gets an explicit deferred
`BEGIN` where the driver used to begin implicitly. Alembic migrations run
through the same engine and must still pass (`repo_test` and
`library/migrations` tests exercise them).

### 3.2 `repo.mutate_roll`

Split the bodies of `load_roll` and `save_roll` into session-taking private
helpers, `_load_roll_rows(session, folder)` and
`_save_roll_rows(session, folder, manifest)`. Keep the public `load_roll`
and `save_roll` as thin wrappers.

Add:

```python
def mutate_roll(
    roll_dir: Path | None = None,
    fn: Callable[[RollManifest], T] = ...,
    *,
    roll_id: str | None = None,
    folder: Path | None = None,
) -> tuple[RollManifest, T]:
```

- Exactly one of `roll_dir` or `roll_id` identifies the roll. `roll_id` is
  for `rename_roll`, whose folder has already moved by the time it writes.
- `folder`, when given, is the `folder_path` to save. It defaults to the
  folder the roll was loaded from.
- It opens a session whose connection carries the immediate flag, loads the
  roll with `_load_roll_rows`, calls `fn(manifest)`, recomputes derived
  state (§3.3), saves with `_save_roll_rows` in the **same** session and
  commits.
- It returns the committed manifest and `fn`'s return value.
- If `fn` raises, the transaction rolls back and the exception propagates
  unchanged. Domain errors such as `RollFolderError` or `FilmBaseError`
  reach the caller exactly as they do today.
- `RollNotRegisteredError` is raised when the roll is gone.

`fn` **must be fast and must not do file I/O or image work**. It runs while
holding the database-wide write lock, and every other writer of any roll
waits behind it (for up to 30 s, then fails). It should copy precomputed
values into the fresh manifest and nothing more.

Then add `roll_manifest.mutate_roll_manifest(output_dir, fn, **kw)`. It
calls `repo.mutate_roll` and runs `_validate_output_paths_within` on the
result, the same check `load_roll_manifest` makes. Production code calls
this, not `repo.mutate_roll` directly.

### 3.3 Derived state moves into the transaction

`write_roll_manifest` currently stamps `updated_at` and recomputes
`sequence` before saving. Move both into `mutate_roll`, after `fn` and
before `_save_roll_rows`. `sequence` depends on every negative and every
run, so it must be computed from the fresh manifest. Keep a single
implementation for both entry points.

### 3.4 Creating a roll

Add `repo.insert_roll(roll_dir, manifest)`. It raises if the `roll_id` or
`folder_path` already exists, then writes with `_save_roll_rows` in one
transaction. `roll_folder.create_roll` uses it, with the derived-state
stamp from §3.3.

### 3.5 What becomes of `write_roll_manifest`

After §4 it has **no production callers**. The test suite uses it about 80
times to build fixtures, so keep it as a fixture-building helper, and say
so in its docstring: a snapshot save that must never run against a roll
another writer may be using.

Add a fast test, `transactional_writes_test.py`, that walks every
non-test, non-`_support` module under `cli/src/scanny_boy` with `ast`. It
fails if any module calls `write_roll_manifest` or `repo.save_roll`,
outside `roll_manifest.py` and `library/repo.py` where they are defined.
This keeps the old pattern from coming back.

### 3.6 How to convert a writer

Each call site follows the same shape:

```python
# Before
roll = load_roll_manifest(roll_dir)
... slow work reading roll ...
roll.some_field = value
write_roll_manifest(roll_dir, roll)

# After
roll = load_roll_manifest(roll_dir)          # a read-only snapshot for the slow work
... slow work reading roll ...
def apply(fresh: RollManifest) -> None:      # fast; touches only this writer's fields
    fresh.some_field = value
roll, _ = mutate_roll_manifest(roll_dir, apply)
```

Rules:

- **Re-check preconditions inside `apply`.** "Refuses once the roll has
  runs" and "refuses once `film_base.locked_at` is set" must raise from
  inside `apply`, not only from the early read. Keep the early check too,
  so the user gets the error before the slow measurement.
- **Find negatives by `negative_id` in `fresh`, never by object identity
  from the snapshot.** If one has disappeared (deleted since the snapshot),
  skip it. Raise only where the command's contract already says "not
  found".
- **Merge, don't replace, for dicts the writer only partly owns.**
  `normalization["auto_neutral"]` sets that one key and leaves the rest of
  `normalization` alone. `output["size"]` and `output["sha256"]` set only
  those two keys.
- **Rebind the snapshot to the returned manifest** whenever the caller
  keeps reading after the write (previews, events, `sequence`).
  `write_roll_manifest` used to mutate the caller's copy in place; the
  returned manifest is the replacement.

---

## 4. The conversions

### 4.1 Small writers (chunk TW-2)

| Site | `apply` does | Notes |
|---|---|---|
| `roll_folder.create_roll` | Calls `repo.insert_roll` (§3.4) instead of a mutation | |
| `roll_folder.set_film_kind` | Raises `FILM_KIND_LOCKED` if `fresh.runs`; sets `film` and `published_icc_profile` | |
| `roll_folder.set_setup` | Merges the given keys into `fresh.setup` | The merge base must be `fresh.setup`, not the snapshot's |
| `roll_folder.rename_roll` | Sets `roll_name`, `roll_id=` plus `folder=new_path` | `os.rename` first, as today |
| `cli._run_roll_set_base_frame` | Raises `FILM_BASE_LOCKED` if `fresh.film_base.locked_at`; sets `film_base` | Keeps the early check; the camera-conflict warning still reads the snapshot |
| `cli` set-flatfield-reference | Raises if `fresh.flat_field.locked_at` (mirror the existing early check); sets `flat_field` | Gain-map file I/O stays outside `apply`, as today |
| `metadata_edit` | `_apply_roll_fields` and `_apply_negative_fields`, then `apply_intended_times(fresh)` | Validation stays outside. Unknown negative ids fail exactly as today |
| `apply_metadata` | Per applied negative: `output.size/sha256` and `capture_time.applied_datetime_original`; sets `metadata.last_applied_at` | TIFF rewrites stay outside. Values go in by `negative_id` |
| `edits._refresh_preview` | Sets `preview_path` on that one negative | |
| `edits.run_edit_delete` | Removes the selected negatives from `fresh.negatives`, then recomputes `highlight_lock` from `fresh` | See below |
| `previews.sync_previews` | Sets each rendered negative's `preview_path` | One mutation after the render loop. Rendering stays outside |
| `roll_refresh.run_roll_refresh` | Sets `highlight_lock`, each measured negative's `normalization["auto_neutral"]` and `refresh_pending=False` | Measuring stays outside, on the snapshot |

**`edits.run_edit_delete` and auto-neutral.** `compute_roll_highlight_lock`
is pure computation over the manifest, so it may run inside `apply`.
`auto_neutral.recompute_roll_auto_neutral` reads TIFFs, so it runs outside:

1. In `apply`, remove the negatives and recompute the lock. Return whether
   the lock changed.
2. Outside, measure auto-neutral against the returned manifest.
3. If anything changed, write it with a second small mutation.

A delete was already two separate saves' worth of work. What matters is
that neither save reverts anything.

### 4.2 The stitch (chunk TW-3)

`run_stitch` keeps its working copy `roll`. It still drives planning,
naming, the clamp's reference bounds and the previews, and the roll lock
still guarantees no other stitch is running. Every
`write_roll_manifest(out_dir, roll)` becomes a mutation that copies **only
this run's own pieces** from the working copy into `fresh`.

Add one private helper in `stitch_pipeline.py`:

```python
def _persist(out_dir, roll, *, records=(), run=None, removed=(), roll_fields=()):
    """Copy this run's own state from the working copy into a fresh roll."""
```

- **`records`:** replace the same-id negative in `fresh` with a deep copy
  of the working record, or append it if absent, keeping the working
  copy's list order for new ones.
- **`run`:** replace or append the run by `run_id`.
- **`removed`:** remove those `negative_id`s from `fresh`.
- **`roll_fields`:** copy the named roll attributes (`processing_params`,
  `stitch_params`, `icc_profile`, `camera_color`, `film_base`,
  `flat_field`, `highlight_lock`, `refresh_pending`, `sources`).

The `sources` merge is order-sensitive and keyed by `sha256`: re-run
`merge_sources(fresh, work_manifest.sources, run_id)` rather than copying
the list.

The mapping:

1. **Step 7.** `run`; every record in `records_by_group`; `roll_fields`
   for the seeded params and `camera_color`; sources via `merge_sources`.
2. **`_record_failure`.** That one record.
3. **Each publish.** The record; `removed` (the covered negatives just
   dropped); `film_base` and `flat_field` when this publish set
   `locked_at`.
4. **The end of the run.** `run`; `highlight_lock` or `refresh_pending`.
   The highlight lock must be recomputed **inside** `apply`, from `fresh`
   (pure computation, §4.1), not copied from the working copy. It is a
   whole-roll aggregate, and `fresh` is the truth.

After each `_persist`, keep using the working copy for the rest of the run
(the lock makes it accurate). Take any field the database derives, such as
`sequence`, from the returned manifest if the stitch reports it.

`run_pipeline.run_full` calls `run_stitch` and never writes the roll
itself, so it needs no change.

**Never write `lambda fresh: <copy the whole working roll>`.** That is the
snapshot save again under a new name.

---

## 5. Testing

The fast tier is enough for everything here except the end-to-end stitch.
Run `uv run pytest --slow` once at the end, since `stitch_pipeline` is
touched (AGENTS.md).

**TW-1 (`library/repo_test.py`, `roll_manifest_test.py`):**

- **The mutation commits only `fn`'s change.** A field set in `fn`
  persists; untouched fields and other negatives' edits survive.
- **No lost update with a stale snapshot.** Load snapshot A; add a negative
  with `mutate_roll`; mutate a roll field starting from snapshot A's
  values. The added negative and its edits must survive.
- **Writers serialize.** Thread 1's `fn` blocks on a `threading.Event`
  after reading. Thread 2 calls `mutate_roll` on the same roll and must
  not finish until thread 1 commits. Thread 2's `fn` must then see thread
  1's change, and both changes persist. Use a short `busy_timeout` override
  or generous timeouts so the test cannot hang.
- **A raise inside `fn` rolls back** and propagates the same exception.
- **Derived state.** `sequence` and `updated_at` are recomputed by
  `mutate_roll`.
- **`insert_roll` refuses a duplicate** `roll_id` and a duplicate folder.
- **A `BEGIN IMMEDIATE` probe.** While one connection holds an immediate
  transaction, a second connection's immediate begin waits; with a tiny
  timeout it fails with "database is locked". This proves the flag
  reaches SQLite.

**TW-2 and TW-3:**

- Every existing test for the converted modules must still pass unchanged,
  except where a test asserted in-place mutation of a caller's object.
  Update those to read the returned manifest, and say so in the chunk
  report.
- For each converted writer, add one **lost-update regression test** in its
  module's test file: commit a concurrent change between the writer's
  snapshot read and its write, and assert both survive. Patch the writer's
  slow step, or use a `fn` hook, to inject the concurrent change at the
  right moment.
  - For the stitch, use a fast-tier `work_dir` fixture and inject from an
    `emit` callback during compositing. The injected change can be a
    metadata edit on another negative, or a `setup` change. Assert it
    survives the stitch's later writes.
- `transactional_writes_test.py` from §3.5.

---

## 6. Order of work (chunks for implementers)

Each chunk leaves the fast tier green and is committed on its own.

- **TW-1: primitive.** §3.1–§3.4 and the TW-1 tests in §5. No call sites
  change, except that `write_roll_manifest` delegates its derived-state
  stamp to the shared helper.
- **TW-2: small writers.** §4.1 and their regression tests. Depends on
  TW-1.
- **TW-3: stitch.** §4.2 and its regression test, then `--slow`. Depends on
  TW-1. Independent of TW-2: it touches different files.
- **TW-4: lock-in.** §3.5's docstring and AST guard test. Update the docs:
  - TETHER_PLAN §0.6's first bullet: the record is no longer overwritten,
    and the remaining reasons for serial stitches still stand.
  - A DECISIONS.md entry for "roll writes are transactional mutations
    against fresh state".
  - ARCHITECTURE.md, wherever it describes the manifest save.

  Depends on TW-2 and TW-3.

---

## 7. Risks

- **`isolation_level=None` changes every session's begin.** It is the
  documented recipe, but it changes behaviour globally. The full fast tier
  is the check. Watch Alembic migrations and anything that relied on
  autocommit-ish reads.
- **Long `fn` bodies stall every writer.** The write lock is
  database-wide, not per roll. Review each `apply` for file or image work.
  `sync_previews`, `roll_refresh` and auto-neutral are the tempting
  places.
- **Callers that read the old in-place mutation.** `write_roll_manifest`
  set `sequence` and `updated_at` on the caller's object. Any code reading
  those afterwards must use the returned manifest.
- **Stitch working copy drift.** The working copy is only accurate because
  the roll lock still excludes other writers. Note that in a comment at
  the top of `run_stitch`, because the parallel-stitch work will remove
  that assumption.
