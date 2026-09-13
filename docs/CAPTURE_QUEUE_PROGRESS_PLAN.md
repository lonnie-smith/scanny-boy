# Capture queue progress: one row per negative

While a roll is being shot, the stitch queue works through each negative in
the background: prepare, check, stitch. Today the Capture tab shows that as a
strip of 64×48 tiles: a colour, an icon and a stamp, with the step and any
failure message hidden in a hover tooltip. There is no way to tell how far
along a negative is, or whether it is moving at all.

This plan replaces the strip with a list: one row per negative, each with its
status in words, a progress bar for the step it is in, and how long that step
has been running. Published negatives keep their row, with a thumbnail of the
stitched TIFF.

It follows `docs/TETHER_PLAN.md`'s conventions: numbered chunks, each
independently green. It amends TETHER_PLAN §3.4 (the strip) and §8.2.

---

## 0. Why this shape

### 0.1 What the CLI already reports

`StitchQueueModel.runCommand` reads `error` and `finished`, and drops every
`progress` event. The data is already on the wire for two of the three steps:

| Step | `progress` today | Steps per negative (n = across × down) |
|---|---|---|
| `prepare` | yes, `stage: prepare` — decode, write_tiff, add_metadata | 3n |
| `capture check` | **no** — `_StitchProgress(total=1, emit=lambda _event: None)` in `capture_check.py` | 2n + 2 (load, detect per frame; match, solve) |
| `stitch` | yes, `stage: stitch` — load … write_stitched | 3n + 5 (`_STEPS_PER_FRAME`, `_STEPS_PER_NEGATIVE`) |

Because the queue runs one negative per CLI invocation, each invocation's
`completed / total` is exactly that negative's progress through that step. No
per-negative bookkeeping is needed on the Swift side.

### 0.2 One bar per step, not one bar per negative

The bar fills for the step the negative is in, and resets when the next
begins; the status text names the step. A single bar across all three steps
would need weights, and the steps are nothing alike — a stitch's warp and
blend dwarf a check's solve, and the ratio changes with the grid. A bar that
crawls for the stitch after racing through the check says less than one that
restarts under a new label.

### 0.3 Elapsed time, not an ETA

Each active row shows how long its current step has run. `RunProgressView`
already sets the rule this follows: the bar is driven by counts, never
extrapolated from time, because per-negative durations vary too much. A
ticking clock next to a moving bar shows the rate without promising a finish.

### 0.4 A published row's thumbnail is the stitched TIFF

The CLI-rendered preview (TETHER_PLAN §3.4's original wording) does not exist
yet while the session runs: the queue stitches with `--defer-roll-refresh`,
and previews are rendered by `roll refresh` at the end (§4.4). What does exist
the moment a stitch publishes is the TIFF itself, at
`<roll>/<negative_done.output>` (`stitch_pipeline._composite_and_publish`
writes `out_dir / record.expected_output`, and `out_dir` is the roll).
`ThumbnailLoader.thumbnail(forStitchedTIFF:)` already bounds that decode with
`kCGImageSourceThumbnailMaxPixelSize`. It shows the raw stitched negative, not
the inverted positive; that is expected, and it is what just landed.

---

## 1. The row

```
 ⚙  0913-142210   Stitching · Blending                ▓▓▓▓▓▓▓░░░   0:41
 ⚙  0913-142305   Checking · Detecting features       ▓▓▓░░░░░░░   0:06
 ◷  0913-142350   Waiting to stitch                   ░░░░░░░░░░
 ✕  0913-142012   Check failed — too little overlap between frames
 ▣  0913-141807   Published                            3:12 total
```

| Column | Content |
|---|---|
| Leading | Active/waiting/failed: the existing icon and colour from `CaptureQueueTile`. Published: a 48×32 thumbnail (§0.4), falling back to the checkmark while loading or if none. |
| Stamp | `negative.stamp`, monospaced digits. |
| Status | Step in words, then `RunStepName.string(step)` for the latest `progress` event when active. Failed: `CLICode` friendly name or the CLI message, one line, truncated, full text in `.help`. |
| Bar | `ProgressView(value: completed / total)` while active; indeterminate before the first event (and for the check until chunk Q-2); an empty bar while waiting; none when published or failed. |
| Trailing | Active: elapsed for the current step, via `TimelineView(.periodic(from: .now, by: 1))`. Published: total time from enqueue to publish. |

Status text per `Step`:

| Step | Text |
|---|---|
| `waitingPrepare` | Waiting to prepare |
| `preparing` | Preparing · *step* |
| `waitingCheck` | Waiting to check |
| `checking` | Checking · *step* |
| `waitingStitch` | Waiting to stitch |
| `stitching` | Stitching · *step* |
| `published` | Published |
| `prepareFailed` / `checkFailed` / `stitchFailed` | Prepare / Check / Stitch failed — *message* |
| `waitingForDisk` | Waiting for disk space |

**Order.** Newest first, so the negative just shot is at the top and the
published history runs down below it. Within the same session the queue
order and capture order match, so this is simply `negatives.reversed()`.

**Size.** The list sits where the strip was, under the form, capped at about
five rows and scrolling beyond. Rows are a fixed height so the form above
does not jump as statuses change.

---

## 2. Model changes (`StitchQueueModel`)

### 2.1 Live progress, kept off disk

```swift
struct StepProgress: Equatable, Sendable {
    var completed: Int
    var total: Int
    var step: CLIPipelineStep?
    let startedAt: Date
}
private(set) var progress: [UUID: StepProgress] = [:]
```

It is not a field of `QueuedNegative`, which is `Codable` and written to
`stitch-queue.json` on every change. Progress is transient: after a relaunch
`restoreState` resets active steps to waiting anyway.

- When an entry enters `.preparing`, `.checking` or `.stitching`, set
  `progress[id] = StepProgress(completed: 0, total: 0, step: nil, startedAt: .now)`.
- `runCommand` and `runCaptureCheck` take the entry's `id`. On
  `.event(e)` with `e.kind == .progress` and both counts present, update
  `completed = min(e.completed, e.total)`, `total`, `step`. The cap matters
  for the check: `_attempt_solve`'s CLAHE retry advances load/detect a second
  time.
- When the step ends (any outcome), remove `progress[id]`.

### 2.2 Timestamps and the output path, kept on disk

Add to `QueuedNegative`, all optional so an existing `stitch-queue.json`
still decodes:

- `publishedAt: Date?` — set on `.published`; with `enqueuedAt`, gives the
  published row's total time.
- `outputFilename: String?` — from the stitch's `negative_done.output`,
  captured in `runCommand` (return it on `CommandResult`). The view resolves
  it against `rollURL`.

### 2.3 Look entries up by id after every await

`runPrepare`, `runCheck` and the stitch `Task` hold an array `index` across
`await`s and write through it afterwards. `discardUnpublished()` removes
entries while those are suspended, so a finishing prepare can mark the wrong
negative, or skip its own. Progress updates arriving mid-command make this
window far busier. Replace every post-await `negatives[index]` with a lookup
by `id` (a small `mutateEntry(id:_:)` helper), and ignore results for an id
no longer present.

---

## 3. CLI change (`capture check`)

`run_capture_check` gains an `emit` argument and builds
`_StitchProgress(total=len(group.members) * 2 + 2, emit=emit, run_id=run_id)`
with the command's real run id. `cli.py` passes `writer.write`.

- The events are ordinary `progress` events with `stage: stitch`; no new
  event type, field or code. `CONTRACT.md` and TETHER_PLAN §7.2 gain one line
  saying `capture check` emits them. No protocol version bump: consumers that
  ignore `progress` for this command are unaffected.
- `_StitchProgress.advance` clamps `completed` to `total`, for the CLAHE retry.
  This is harmless for `stitch`, whose count never over-runs.
- The standalone `stitch` then re-runs load/detect/match/solve from scratch;
  that is unchanged, and it is why the stitch bar starts at zero rather than
  continuing from the check.

---

## 4. View (`CaptureQueueList`)

Rename `Views/CaptureQueueStrip.swift` to `Views/CaptureQueueList.swift`.

- Takes `stitchQueue: StitchQueueModel` (for `progress` and `rollURL`)
  instead of `[QueuedNegative]`.
- `CaptureQueueRow(negative:progress:rollURL:)` renders §1. The status string
  is a pure `static func statusText(for:progress:) -> String` so it can be
  unit-tested without a view.
- The thumbnail loads in `.task(id: negative.outputFilename)` through
  `ThumbnailLoader.shared.thumbnail(forStitchedTIFF:pointSize:scale:)`.
- `CaptureStageView` swaps the strip for the list, with
  `.frame(maxHeight:)` for about five rows.

---

## 5. Chunks

### Q-1 — model: progress, timestamps, id lookups

§2 in full. Tests in `StitchQueueModelTests`, against a fake runner:

- `progress` events during a prepare fill `progress[id]` for that entry and
  no other; the entry is cleared when the step ends.
- `completed` above `total` is capped.
- `negative_done` sets `outputFilename`; publishing sets `publishedAt`.
- An old `stitch-queue.json` without the new fields restores.
- Discarding while a prepare is suspended leaves the remaining entries'
  steps untouched (§2.3).

### Q-2 — CLI: `capture check` progress

§3. `cli_test`: `capture check` on a fixture work folder emits at least one
`progress` event, every one with `completed ≤ total`, followed by
`capture_checked`. `CONTRACT.md` and TETHER_PLAN §7.2 updated.

### Q-3 — the list

§1 and §4. Tests for `statusText` across every `Step`, with and without
progress. Manual check on the stand: shoot three negatives back to back and
watch rows go waiting → preparing → checking → waiting to stitch →
stitching → published with a thumbnail; force a check failure (cover a
frame) and confirm the message is readable without hovering.

### Q-4 — docs

TETHER_PLAN §3.4 describes rows instead of tiles and the stitched-TIFF
thumbnail; §8.2's file table points at `CaptureQueueList.swift`.

Q-1 and Q-2 are independent. Q-3 needs Q-1; without Q-2 the check row shows
an indeterminate bar, which is acceptable if Q-2 slips.

---

## 6. Rejected alternatives

- **One bar across all three steps.** Needs per-step weights that depend on
  the grid and the machine (§0.2).
- **An ETA.** Contradicts `RunProgressView`'s rule against time-based
  extrapolation (§0.3).
- **Collapsing published negatives into a count.** Loses the at-a-glance
  confirmation that each negative landed and looks right.
- **Waiting for the CLI-rendered preview for the thumbnail.** It does not
  exist until `roll refresh` at the end of the session (§0.4).
- **Persisting progress in `QueuedNegative`.** It would rewrite
  `stitch-queue.json` on every step event for state that is reset on restore.

## 7. Open questions

- Is the stitched TIFF's embedded thumbnail (if the writer includes one) good
  enough, or does `CGImageSourceCreateThumbnailAtIndex` with
  `kCGImageSourceCreateThumbnailFromImageAlways` decode enough of a large
  TIFF to stall the row? Measure on a 4×3 negative in Q-3; if it is slow,
  load thumbnails only for rows on screen, which `List` already gives.
- Should a failed row's Reshoot (TETHER_PLAN §4.5) land in this row as a
  button? Out of scope here, but the row leaves room for it.
