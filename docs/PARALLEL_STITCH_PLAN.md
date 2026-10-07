# Parallel stitching plan

The capture queue stitches one negative at a time per roll, and a stitch
uses about 1.2 cores of this Mac's 8 performance cores. A backlog of checked
negatives therefore drains at ~25 s each while most of the machine idles.

This plan lets several stitches of one roll run at once, while keeping the
published result **identical to a serial run**: same pixels, names, ids
and clamp references.

1. The order-independent part of a stitch (registration, warp, blend,
   metering) moves into a lock-free **compose** step that can run several at
   a time.
2. The order-dependent part (the clamp, normalize, encode, TIFF write and
   record) becomes a short **commit**, still serial and in capture order.
3. Because roll writes are now transactional mutations
   ([`TRANSACTIONAL_WRITES_PLAN.md`](TRANSACTIONAL_WRITES_PLAN.md)), the work
   after the TIFF is published no longer has to sit behind a lock. Previews,
   edit seeds, the end-of-run write and auto-neutral all move out of it.

It reverses TETHER_PLAN's "a parallel stitch stage" rejection (§0.6, and
the rejected-alternatives list). The user now wants the backlog to drain
faster.

---

## 0. Measurements (2026-10-06)

Taken on one real 3-frame negative (`_DSC4638`–`_DSC4640`): this Mac,
64 GiB, 8P+2E cores, fast tier code at `544a12f`. The canvas is 7910 × 6132.

| Part of `run_stitch` | Time | Depends on earlier negatives? |
|---|---|---|
| Load, detect, match, solve | 1.4 s | No |
| Warp, blend, log, metering (`composite` before the clamp) | ~15 s | No |
| Clamp, normalize, extrema, encode (`composite` after the clamp) | ~2.0 s | **Yes**: the clamp's reference bounds |
| Overlap gate, auto-rotate/crop measure, `write_stitched_tiff` | ~3.8 s | **Yes**: the pixels come from the clamp; the name comes from publish order |
| Persist, scratch, deband, edit seeds, end-of-run write, previews | ~3.3 s | No (each is its own transaction) |
| **Total** | **~25.5 s** | |

- **Peak RSS is 9.0 GiB per stitch process** for this negative (prepare
  alone peaks at 1.1 GiB). CPU averages ~1.2 cores.
- **The order-dependent part is ~5.8 s of the ~25.5 s.**
- **The compose artifact** (§3.2) is the float32 log-density canvas, about
  **555 MiB** here, plus the coverage mask.

These are one negative's numbers, and a larger grid costs more of both time
and memory. The plan does not pick a concurrency limit from measurements.
The user chooses how many negatives compose at once, 1–4, on the Capture
tab (§3.8).

**Expected throughput.** The steady state is
`max(compose ÷ N, ordered part)`:

| Parallel stitches (N) | Per negative | Speed-up vs today |
|---|---|---|
| today (no split) | ~25 s | — |
| 1 | ~16.5 s | ~1.5× |
| 2 (default) | ~8.3 s | ~3× |
| 3 | ~6 s | ~4× |
| 4 | ~6 s | ~4× |

N = 1 still beats today: the next negative composes while the previous one
commits. From N = 3 the serial commit, not compose, is the bound, so N = 4
mostly adds memory pressure on this grid. It can still help on a larger
grid, where compose takes longer relative to the commit.

---

## 1. What the transactional writes already settled, and what they did not

**Settled.** Two writers can no longer delete or revert each other's
records. Every roll write is `mutate_roll_manifest`: `BEGIN IMMEDIATE`,
reload, apply only the writer's change, commit. The stitch's four writes
go through `stitch_pipeline._persist`, which copies only its own records,
run, removals and named fields. The highlight lock is recomputed from the
fresh roll.

**Not settled.** Each of these still assumes one stitch at a time per roll:

1. **Recovery cleanup assumes it is the only live run.** Under
   `ROLL_RULES`, `output_folder._plan_rerun` treats the roll's last run
   (`runs[-1]`) as the owner of staging directories. It reports any other
   run's staging directory as unrelated content (`OUTPUT_NOT_EMPTY`), and it
   lists any non-completed negative's existing output as stale.
   `apply_recovery_cleanup` then deletes those files. A second stitch
   planning while a first is mid-publish would delete the first one's
   files.
2. **The clamp is order-dependent.** `_reference_bounds(roll)` reads the
   normalization blocks of the negatives already published, from the
   working copy.
3. **Names are order-dependent.** `allocate_output_name` takes tethered
   `<stamp>_NN` numbers "in the order they are first published"
   (`_tethered_output_stem`), and avoids names already claimed, using the
   working copy.
4. **Run ids are assigned against the working copy.** `append_run` picks a
   `short_id` that is unique among the working copy's runs.
5. **First-run seeding reads the working copy.** `processing_params`,
   `stitch_params`, `icc_profile` and `camera_color` are seeded when the
   working copy has no runs. `check_roll_invariants` compares against it
   otherwise.
6. **The first publish copies `film_base` wholesale.** When it sets
   `locked_at`, it copies the whole `film_base` and `flat_field` from the
   working copy (`roll_fields=locked_fields`).
7. **A non-deferred end-of-run re-renders only part of the roll.** When the
   lock changes, it calls `sync_previews(force=True)` over the *working
   copy's* negatives. A negative another stitch published in the meantime
   is missing from that list.

The commit (§3.3) closes 1–5 by doing planning, naming, the clamp and the
publish under one per-roll lock, against a working copy loaded *inside*
that lock. Then that copy is exact for everything order-dependent. Item 6
gets a verify-and-set (§3.5). Item 7 is avoided: parallel stitches always
defer the roll refresh (§3.4).

---

## 2. Goal and non-goals

**Goal.** In the capture queue, several negatives of one roll compose at
once. Each publishes in capture order with exactly the result a serial run
would have produced. The roll is locked only for the order-dependent
~6 s.

**Non-goals:**

- **`run` (Add Scans → Convert) and Re-stitch.** They keep today's
  single-process, exclusive-lock stitch. `run` could use compose
  subprocesses later (§8).
- **Letting Edit, metadata or delete run during a stitch** (relaxing
  TETHER_PLAN §4.2). The lock model below keeps those writers exclusive.
  §8 lists what that would additionally need.
- **Parallelism inside one process.** ARCHITECTURE §11's "parallelism never
  spans negatives" stays true per process. Every compose and commit is its
  own process, so memory goes back to the OS when it exits.
- **Changing any published pixel, name, id or record field.**

---

## 3. Design

### 3.1 Split `composite()` in two

In `composite.py`, `composite()` already runs in order: warp and blend,
`to_log_density`, the metering passes, the clamp, `measure_neutral_residual`,
`normalize_log_image`, extrema and headroom, and `encode_normalized`. The
clamp at `composite.py:931` is the first roll-dependent line.

- **`compose_negative(...) -> ComposedNegative`** runs everything before
  the clamp. It returns `img_log`, `covered`, `grid` and `keep` (needed by
  the residual), the unclamped `bounds`, every meter (`shadow_refs`,
  `highlight_refs`, `anchor`, `textural_range`, `rebate`, `dense_border`,
  `opaque`, `film_extent`), and the photometric results (`gains`,
  `overlap_mad*`, `overlap_fraction`).
- **`finish_negative(composed, reference_bounds) -> CompositeResult`** runs
  the clamp and everything after it.
- **`composite(...)` becomes `finish_negative(compose_negative(...), reference_bounds)`.**
  The serial path then runs literally the same code, so "identical to
  serial" is true by construction, not by testing alone.

### 3.2 The compose artifact

`stitch --work W --roll R --compose-only [--rig ID]` does the following:

- runs steps 1–5 and the solve;
- calls `compose_negative`;
- writes `W/.composed/<group_id>/`, then exits.

It writes **nothing to the roll** and takes no exclusive lock (§3.4).

The artifact:

- `log.npy` (float32, H×W×C, C = 1 on a mono roll after
  `collapse_to_mono`), `covered.npy`, `grid.npy`, `keep.npy`. Use plain
  `np.save`: no compression, and it is loadable with `mmap_mode`.
- `composed.json`: the meters and photometric results above, plus
  everything `_composite_and_publish` reads from the solve. That is the
  layout, `frame_size`, pairs, `ca_maps` and rectification records, and
  `valid_rect`.
- `inputs.json`, the fingerprint the commit checks: `COMPOSE_FORMAT_VERSION`,
  the work manifest's hash, the rig profile id and its content hash, the
  roll's film kind, the `film_base` `source_sha256` and density used as
  `base_refs`, the flat-field `gain_map_sha256`, and the scanny-boy version.

Written into a temporary directory and renamed into place, so a reader
never sees a partial artifact. Before writing, compose runs
`disk_check.check_disk_space` for the artifact's size, which is known once
the layout is solved. It fails `INSUFFICIENT_DISK` exactly like prepare, so
the queue's waiting-for-disk path handles it.

A compose failure (solve refused, `STITCH_UNDERCONSTRAINED`, and so on) is
written as `W/.composed/<group_id>/failure.json` and emitted as an
`error`. The **commit** still records it in the roll, as today, so a failed
negative's record looks the same either way.

### 3.3 The commit

`stitch` (no new flag) looks for `W/.composed/<group_id>/`.

- **A valid artifact** is one whose `inputs.json` matches the fresh roll
  and the work folder. The commit loads it instead of solving and
  compositing. A failure artifact is recorded with `_record_failure`.
- **No artifact, or a stale one,** means a full in-process compose, which
  is today's behaviour. **Correctness never depends on the artifact.** It
  only saves time.

Restructure `run_stitch` / `_composite_and_publish` so the **publish
section** is explicit and runs under the publish lock (§3.4):

1. Load the working copy **inside the lock**, then plan:
   `plan_rerun`, `apply_recovery_cleanup`, `append_run`, records, names,
   the step-7 `_persist`. Planning moves after the solve, which is already
   done. On the fallback path, the solve happens before the lock too.
2. `_reference_bounds(roll)`, then `finish_negative`, the overlap gate,
   the auto-rotate and auto-crop measurements, and `write_stitched_tiff`
   into staging.
3. `staged_path.replace(dest)`, the publish `_persist` (record, removals,
   verified locks per §3.5), then **remove the staging directory**. Today
   that removal is in the `finally` at the very end, after seeding. It must
   happen before release, or the next commit's plan sees a staging
   directory that does not belong to `runs[-1]` and fails
   `OUTPUT_NOT_EMPTY`.
4. **Release the publish lock**, then emit the new `negative_published`
   event (§3.6).

After release, as transactions against fresh state, nothing order-dependent
happens: scratch detection, the deband refit, the rotate and crop seeds
(`append_edit`), the end-of-run `_persist` (`refresh_pending=True`),
`sync_previews` for its own negatives (deferred mode), and `negative_done`.
The process still holds the shared roll lock until it exits.

Why this is exact: commits of a roll run one at a time, in capture order,
each against a copy loaded inside the lock. The clamp's references,
tethered `NN` numbers, collision suffixes, `short_id`s and first-run
seeding are what a serial run computes. Recovery cleanup only ever sees the
previous commit's negatives already completed and its staging gone.

### 3.4 Locks

There are two per-roll advisory locks (`roll_lock.py`). Both are
non-blocking and fail `ROLL_BUSY`, as today.

| Command | Roll lock (`<roll_id>.lock`) | Publish lock (`<roll_id>.publish.lock`, new) |
|---|---|---|
| `stitch --compose-only` | shared, whole process | — |
| `stitch --defer-roll-refresh` (a parallel commit) | shared, whole process | exclusive, publish section only (§3.3 steps 1–4) |
| `stitch` without `--defer-roll-refresh`, `stitch --negatives`, `run` | exclusive, whole process (today) | — |
| `export` | shared (today) | shared, whole process |
| every other roll writer (§4.3 of TETHER_PLAN) | exclusive (today) | — |

- **Exclusive-roll writers still exclude every stitch, and the reverse.**
  These are edit ops, delete, metadata, `roll refresh`, base frame and
  rename. This is TETHER_PLAN §4.2's app rule, still enforced by the CLI.
- **Two stitches of one roll hold the roll lock shared together.** Only one
  at a time holds the publish lock.
- **Export takes the publish lock shared.** A commit then cannot replace a
  TIFF an export is reading (adoption), and the CLI does not rely on the
  app having disabled Export.
- **Only deferred stitches run in parallel.** This sidesteps §1 item 7: a
  stitch that recomputes the highlight lock and force-re-renders previews
  keeps the exclusive lock. The capture queue always passes
  `--defer-roll-refresh`.

### 3.5 Verified first-publish locks

Replace the wholesale `roll_fields=["film_base"]` / `["flat_field"]` copy
with a closure inside the publish `_persist`. It checks that
`fresh.film_base["source_sha256"]` and density equal the values this
negative was normalized against; if they differ it raises `FILM_BASE_CHANGED`
(new). Only then does it set `locked_at` when null. The same applies to
`flat_field` by `gain_map_sha256`.

The roll lock already excludes `set-base-frame`, so this is defence in
depth. It costs one comparison, and a stale `base_refs` in a compose
artifact can never be published silently.

### 3.6 Protocol

Bump the protocol by one (`shared/contract/CONTRACT.md`):

- `stitch --compose-only`;
- the `negative_composed` event (group id, canvas, artifact bytes);
- the `negative_published` event (negative id, output), emitted after the
  publish lock is released;
- the `FILM_BASE_CHANGED` code.

### 3.7 The app queue (`StitchQueueModel`)

- **Steps.** Add `composing` and `waitingCommit` between `waitingStitch` and
  `stitching`. `stitching` now means the commit. Rows read
  "Compositing · *step*", "Waiting to publish" and "Publishing"
  (CAPTURE_QUEUE_PROGRESS_PLAN).
- **Compose scheduling.** Up to `parallelStitches` (the user's setting,
  §3.8) at once, in capture order across rolls. A checked entry composes
  only while its roll has fewer than `parallelStitches`
  composed-but-uncommitted entries, which bounds the artifacts on disk to
  the same number. The value is read each time the queue schedules work.
- **Commit scheduling.** This is the quick fix's rule (`67fa543`) applied to
  commits. An entry commits once no earlier same-roll entry is still short
  of publishing. `isStitching: Bool` becomes `committingRolls: Set<String>`,
  so different rolls also commit concurrently (their locks differ).
- **The next commit of a roll starts on `negative_published`,** not on
  process exit, so it overlaps the previous commit's post-publish work.
- **Restore.** `composing` returns to `waitingStitch`; `waitingCommit` and
  `stitching` return to `waitingCommit`. The commit falls back to a full
  stitch if the artifact is gone or stale.
- **Discard** cancels composing sessions. The work folder, artifact
  included, is recycled as today.
- **`finishDrainIfNeeded` and `isQueueBusy`** wait for every composing and
  committing entry. A roll's `roll refresh` still runs only once that roll
  has no work.

### 3.8 The "Parallel stitches" setting

The user picks the concurrency instead of the app deriving it.

- **Where.** A `Picker("Parallel stitches", …)` in the Capture tab's
  **Setup** section (`CaptureStageView.setupSection`), below Interval. It
  offers 1, 2, 3 and 4.
- **Help text** (`.help`): "How many negatives are stitched at the same
  time. Each one can use several GB of memory while it stitches."
- **Default and stickiness.** The default is **2**. It is stored in
  `AppEnvironment.defaults` under
  `com.lonniesmith.scanny-boy.parallelStitches` and restored at launch, the
  same pattern as `CaptureSessionModel`'s interval (`lastIntervalKey`). A
  stored value outside 1–4, or a missing one, reads as 2.
- **Owner.** `StitchQueueModel.parallelStitches`, a stored property whose
  `didSet` writes the default. The queue is what reads it. `StitchQueueModel`
  takes a `defaults: UserDefaults = AppEnvironment.defaults` parameter like
  the other models, so tests use a scratch suite.
- **When it is disabled.** Whenever a capture or stitch is in flight:
  `capture.sequencePhase != .idle || stitchQueue.isQueueBusy`. That covers a
  running, paused, stopped or downloading sequence, and any queue work on
  any roll (preparing, checking, composing, committing, the end-of-queue
  `roll refresh`). It is a global setting and the queue is global, so work
  on another roll also disables it. The Setup section's existing
  `.disabled(isRollLocked)` applies on top, unchanged.
- **Changing it mid-queue cannot happen through the UI.** If the queue is
  restored busy at launch, the picker is disabled from the start. The queue
  still reads the value at scheduling time, so a lower value never cancels
  running composes; it only stops new ones starting.
- **No automatic memory guard.** The choice is the user's. §6 records what
  each step costs so the help text and any later warning are grounded.

---

## 4. Testing

The fast tier covers everything except real-sample equality. Run `--slow`
for PS-1 to PS-3, since they touch registration, stitching and TIFF writing
(AGENTS.md).

**PS-1:**
- `finish_negative(compose_negative(x))` equals today's `composite(x)`,
  bit for bit, on `synthetic_scene` (colour and mono, with and without
  reference bounds that clamp).
- The same holds after an `np.save` / `np.load` round trip of every
  `ComposedNegative` field.

**PS-2:**
- `--compose-only` leaves the library database byte-identical.
- A commit from an artifact publishes the same TIFF hash and record as a
  plain stitch of the same work folder.
- Each fingerprint field, changed alone, makes the commit fall back, with
  identical output.
- A failure artifact produces the same failed record and `negative_failed`
  as today.
- `INSUFFICIENT_DISK` before writing.

**PS-3:**
- A second commit fails `ROLL_BUSY` while the first holds the publish lock.
  An exclusive-roll writer fails while any stitch runs. Export blocks a
  commit, and a commit blocks export.
- **The ordering proof:** three negatives (a `work_dir` with injected gains,
  and `CLAMP_MIN_SAMPLES` monkeypatched low enough that the clamp engages).
  Compose all three in parallel processes, then commit in order. The roll
  records, TIFF hashes and tethered names equal a serial stitch of the same
  three.
- Commit k+1 runs during commit k's post-publish tail (inject via `emit`).
  Both runs' writes survive, recovery cleanup deletes nothing of k's, and
  no `OUTPUT_NOT_EMPTY` is raised.
- `FILM_BASE_CHANGED` when the base changes between compose and commit.
- Slow: the same ordering proof on real samples (`stage_samples`).

**PS-4 (Swift, `StitchQueueModelTests` with the fake runner):**
- Composes run concurrently up to `parallelStitches`, for 1 and for 4.
- The composed-ahead cap holds.
- `parallelStitches` defaults to 2 on an empty defaults suite. A chosen
  value survives a new `StitchQueueModel` on the same suite. A stored 0 or
  7 reads as 2.
- Lowering `parallelStitches` while composes run cancels none of them.
  Raising it starts more on the next pump.
- Commits are serial and in capture order per roll, and concurrent across
  rolls.
- The next commit starts on `negative_published` before the previous
  process exits.
- Restore mapping; discard while composing.
- Slow (`SCANNY_BOY_SLOW_TESTS=1`): three real negatives through the
  bundled helper.

---

## 5. Order of work

Each chunk leaves both fast tiers green and is committed on its own.

- **PS-1: split `composite()`.** §3.1. A pure refactor; `--slow` must be
  identical.
- **PS-2: compose artifact and fallback.** §3.2, plus the artifact-consuming
  half of §3.3, still under today's exclusive lock. Bump the protocol.
- **PS-3: the publish section and locks.** The rest of §3.3, plus §3.4 and
  §3.5. Depends on PS-2.
- **PS-4: the app queue and the setting.** §3.7 and §3.8. Depends on PS-3.
  The picker can land first, on its own, with nothing reading it yet. It is
  independent of PS-1 to PS-3.
- **PS-5: docs.**
  - TETHER_PLAN: §0.6 (stitches of one roll now overlap in compose), §4.1,
    §4.3's lock table, and remove "a parallel stitch stage" from the
    rejected list.
  - A DECISIONS.md entry.
  - ARCHITECTURE §11.
  - CONTRACT.md.

---

## 6. Risks

- **Memory is the user's to manage.** Each compose peaks at ~9 GiB for a
  3-frame negative and grows with the canvas. At 4 parallel stitches that
  is ~36 GiB of compose peaks, plus a commit and two prepares, on a 64 GiB
  Mac; a larger grid can push that into swap. Every process also sizes its
  own worker budget (`concurrency.py`) to half of RAM without knowing about
  the others. Swapping makes the queue slow, never wrong. The remedy is a
  lower setting, which is why the help text names the memory cost. If this
  bites in practice, add a warning under the picker when
  `parallelStitches × estimated peak` exceeds physical RAM. Do not add a
  silent cap.
- **Disk.** ~600 MiB per composed-but-uncommitted negative here, so up to
  ~2.4 GiB at 4. The composed-ahead cap (= `parallelStitches`) bounds it,
  and compose's own disk check turns a full disk into waiting.
- **A stale artifact.** The fingerprint (§3.2) and the fallback (§3.3) make
  it a lost optimization, never a wrong result. Keep the fingerprint
  conservative: when in doubt, add the field.
- **The publish section's staging removal moves.** If a crash lands
  between the publish `_persist` and the removal, a completed negative is
  left with a stray staging directory. The next plan then fails
  `OUTPUT_NOT_EMPTY` once another run has been appended. The recovery
  cleanup should treat a staging directory whose unit is completed as
  stale and delete it, for any run. Add that in PS-3.
- **SQLite write contention.** More short transactions per minute share the
  database-wide write lock. Each `apply` is a splice, `busy_timeout` is
  30 s, and none of the new writers hold the transaction across I/O.
- **The app starting a commit early.** `negative_published` is emitted only
  after the publish lock is released, and a commit that still finds it held
  fails fast with `ROLL_BUSY`. The queue treats that as "retry shortly", not
  as a failed stitch.

---

## 7. Review notes on the transactional-writes work

Reviewed at `544a12f`: TW-1 to TW-4, `1e570fe`, `a0f3364`, and the quick fix
`67fa543`. The fast tier passes (1763 passed, 43 skipped). No defects affect
today's behaviour, because the roll lock still excludes concurrent writers.
These notes matter once it narrows:

- **`_persist` replaces whole negative records.** That is right for the
  stitch's own records. But `metadata_edit`'s `apply_intended_times`
  rewrites *every* negative's `capture_time.intended_*`, pending ones
  included. A metadata edit landing between a stitch's running write and its
  publish would be reverted by the publish. That is harmless while metadata
  takes the exclusive roll lock (it still does under §3.4), and it is the
  first thing to fix if §8's relaxation is attempted.
- **The first publish copies `film_base` / `flat_field` wholesale.** §3.5
  replaces this.
- **The non-deferred end-of-run** forces previews over the working copy's
  negatives only (§1 item 7). §3.4 keeps that path exclusive.
- **`roll refresh` may store auto-neutral blocks measured under a lock that
  has since moved.** It correctly leaves `refresh_pending` set in that case
  (`1e570fe`), so the next refresh reconciles. Acceptable as is.
- **The quick fix's commit message** says other rolls no longer hold a
  stitch back. They no longer block its readiness, but `isStitching` is
  still one flag, so stitches of different rolls still never overlap. §3.7
  makes it per roll.

---

## 8. Later

- **Edit, metadata and delete during a capture session** (relax
  TETHER_PLAN §4.2). These writers could take the roll lock shared once the
  stitch merges its records field by field instead of replacing them. In
  particular, `capture_time.intended_*` and `preview_path` must stay the
  fresh roll's. Delete must also refuse a negative a pending commit adopts.
- **`run` with compose subprocesses.** The same split lets Add Scans →
  Convert compose several negatives at once and commit them in order.
- **A shorter publish section.** The TIFF write (~3.7 s) could move out of
  the lock, which would need a liveness-aware recovery cleanup (per-run
  lease locks) instead of `runs[-1]` ownership. Worth it only if the commit
  turns out to be the bound at the settings people actually use (on the
  3-frame sample it already is from 3).
