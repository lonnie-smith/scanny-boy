# Swift UI fix plan

A review of `mac/ScannyBoy` (Views, view-facing models, and the model/CLI
boundary the views depend on) as of branch `fix/ui`. Findings only — nothing
in this pass was fixed. Each item is written to be handed to an agent on its
own: file and line, what is wrong, why it is wrong, the suggested fix, and how
to tell it worked.

Ordering is by severity. Items P1–P5 are user-visible defects; M1–M10 are
correctness/robustness issues that mostly bite in edge cases; L1–L6 are
polish.

Grouping hint for parallel handoff: **P1, P2** are `EditStageView` only.
**P3, M2, M3** are `ConfigurationModel` + `ContentView`. **P4, P5** touch
`ContentView` and the models it owns. **M4** is a repeated pattern across five
files and is best done as one pass.

---

## P1 — Invisible keyboard-shortcut buttons swallow clicks in the Edit tab

**Where:** `mac/ScannyBoy/Views/EditStageView.swift:44-46` (`.overlay { selectionShortcuts }`),
definition at `mac/ScannyBoy/Views/EditStageView.swift:59`.

**What:** `selectionShortcuts` is two real `Button`s hidden with `.opacity(0)`
and stacked in an `.overlay` over the whole tab. `.opacity(0)` does not remove
a view from hit-testing (unlike `.hidden()` or `.allowsHitTesting(false)`), so
two invisible, label-sized buttons sit centred over the preview area and
intercept mouse clicks landing there. The user clicks the middle of the
preview and silently advances or rewinds the selection.

Secondary: the overlay is attached *after* `.disabled(run.isActive)`, so the
Option-arrow shortcuts stay live while a run is in progress, unlike every
other control on this tab.

**Fix:**
- Add `.allowsHitTesting(false)` to the `Group` in `selectionShortcuts` (keep
  `.opacity(0)`; keyboard shortcuts still register — they go through command
  registration, not hit-testing).
- Move the overlay above the `.disabled(run.isActive)` modifier, or add
  `.disabled(run.isActive)` to the shortcut group, so selection cannot move
  mid-run.
- Prefer `.background { ... }` over `.overlay { ... }` for a purely functional
  layer, so it can never paint or capture over content.

**Verify:** click dead-centre on the large preview with 3+ negatives in the
roll — the selection must not change. Option-Left / Option-Right must still
move it.

---

## P2 — Stale thumbnail shown after selecting a different negative or rotating

**Where:** `mac/ScannyBoy/Views/EditStageView.swift:166` (`PreviewPane`'s
`.task(id: previewIdentity)`) and `mac/ScannyBoy/Views/EditStageView.swift:259`
(`FilmstripCell`'s `.task(id:)`).

**What:** `@State private var thumbnail` is only cleared on the
`previewURL == nil` path. When the task id changes — a different negative
selected, or the same negative re-rendered after `edit rotate` — the old
`NSImage` stays on screen for the whole duration of the new load. On a rotate
this is actively misleading: the button appears to have done nothing for a
second, then the image jumps.

**Fix:** set `thumbnail = nil` as the first statement inside the `.task`
closure, before the `await`. If the resulting blank flash is unacceptable,
keep the old image but overlay a `ProgressView`/dimming while loading —
either way the currently displayed pixels must not be attributed to the newly
selected negative.

**Verify:** with a roll of several negatives, click between filmstrip cells
quickly; no cell's image should ever appear in the large preview while a
different cell is highlighted. Rotate a negative and confirm the preview does
not show the pre-rotation orientation after the spinner ends.

---

## P3 — A roll's locked flat-field profile is not applied when switching rolls

**Where:** `mac/ScannyBoy/Model/ConfigurationModel.swift:246-250` (in
`startRollFetch`), and the picker at `mac/ScannyBoy/Views/ContentView.swift:252`.

**What:** two connected problems.

1. The pre-select is `if let locked = ..., flatFieldProfileID == nil` — it
   only fills a *gap*. `flatFieldProfileID` is persisted in `UserDefaults`
   and survives roll switches, so it is almost never `nil` in practice.
   Switching from a roll locked to profile A to a roll locked to profile B
   leaves A selected while the caption underneath reads "This roll is locked
   to *B*". `runCommand()` then passes `--flatfield A` and the CLI refuses the
   run with `ROLL_INVARIANT_MISMATCH`. The UI has told the user the right
   thing and sent the wrong thing.
2. The `Picker` is never disabled when `isRollLockedToFlatFieldProfile` is
   true. The caption says the choice is fixed; the control says otherwise.

**Fix:**
- In `startRollFetch`, when the fetched manifest names a locked profile,
  assign it unconditionally (`flatFieldProfileID = locked`), not only when
  the current value is `nil`. Keep the "explicit choice wins" behaviour only
  for rolls that are *not* locked.
- Disable the profile `Picker` when `model.isRollLockedToFlatFieldProfile`.
- Consider not persisting `lastFlatFieldProfileKey` back to defaults when the
  value was set by a lock rather than by the user, so an unlocked roll does
  not inherit the previous roll's lock as its default.

**Verify:** two rolls locked to different profiles; select A, then B — the
picker must show B's profile and be disabled, and Convert must reach the CLI
with B.

---

## P4 — "One helper at a time" is only enforced against `RunModel`

**Where:** every `runIsActive:` / `.disabled(run.isActive)` gate —
`mac/ScannyBoy/Views/ContentView.swift:55`, `:71`, `:211`, `:232`, `:318`,
`mac/ScannyBoy/Views/RollSidebar.swift:37`, `:39`, `:70`, `:137`,
`mac/ScannyBoy/Views/EditStageView.swift:44`,
`mac/ScannyBoy/Views/MetadataStageView.swift:20`,
`mac/ScannyBoy/Views/ExportStageView.swift:62`.

**What:** `EditModel.rotate`/`delete`, `ExportModel.export`, and
`FlatFieldModel.create` each spawn their own `CLISession`. Their doc comments
(e.g. `EditModel.swift:43-48`, `ExportModel.swift:8-13`) state the invariant that
the *views* gate on the union of these flags — but no view does. Every gate in
the app reads `run.isActive` alone. So today the user can:

- start a Convert while an export or a flat-field calibration is running;
- rename or Trash a roll while an export against it is in flight;
- switch rolls mid-export (see P5);
- delete a flat-field profile while a calibration is running.

`RollLibrary.renameRoll(_:to:runIsActive:)` faithfully refuses on
`runIsActive`, which makes it look enforced when it is not.

**Fix:** introduce one derived source of truth rather than adding flags at
every call site — e.g. a small `AppActivity` observable (or a computed
`isHelperBusy` on a coordinating type) that ORs
`run.isActive || edit.isRotating || edit.isDeleting || export.isExporting ||
flatField.isCreating`. Note `FlatFieldModel` has no `isCreating` today (the
sheet holds it in local `@State` at `FlatFieldProfilesSheet.swift:19`) — move
it onto the model. Then replace every `run.isActive` gate with the derived
value, and change `renameRoll`'s parameter to take it.

**Verify:** start an export of a large roll, then confirm Convert, New Roll,
Rename, Delete, and the rotate/trash buttons are all disabled until it ends.

---

## P5 — Export state is never cleared when the selected roll changes

**Where:** `mac/ScannyBoy/Views/ContentView.swift:86`
(`.onChange(of: model.rollURL) { run.clearResults() }`) and
`mac/ScannyBoy/Model/ExportModel.swift`.

**What:** `ContentView` deliberately clears the *run* log on a roll switch,
with a comment explaining why the log belongs to the roll it converted. The
same reasoning applies to `ExportModel` and is not implemented:
`exportedNegatives`, `warnings`, `failureMessage`, `outcome`, and
`outputDirectory` all survive the switch. Select roll A, export it, select
roll B: the Export tab shows A's completion summary and A's exported filenames
as though they belonged to B.

Worse, an export started against roll A keeps running after the user switches
to B and continues writing its results into the shared `ExportModel`, which
the Export tab now presents under B's heading.

**Fix:**
- Add `ExportModel.clearResults()` mirroring `RunModel.clearResults()` (same
  `guard !isExporting` posture) and call it from the same
  `.onChange(of: model.rollURL)` handler.
- Decide whether `outputDirectory` should persist across rolls (arguably yes,
  as a convenience) — if so, exclude it from the clear and document that.
- With P4 in place, switching rolls mid-export becomes impossible, which
  closes the second half.

**Verify:** export roll A, select roll B, open Export — no summary, no
"Exported" list.

---

## M1 — Stale sidebar selection when the selected roll disappears

**Where:** `mac/ScannyBoy/Views/ContentView.swift:167` (`resolveSelectedRoll`),
`mac/ScannyBoy/Views/ContentView.swift:60` (`if selection != nil`).

**What:** `resolveSelectedRoll` sets `model.rollURL` / `edit.rollURL` to `nil`
when `selection` matches no roll, but leaves `selection` itself set. Because
the detail branch keys on `selection != nil`, the full workspace stays
mounted, pointed at no roll: `navigationSubtitle` is empty, Convert is
disabled with no explanation, and Metadata/Export render blank sections. This
is reachable by deleting the roll's folder in the Finder and letting a rescan
land, by a `roll list` scan that reclassifies a roll as `unreadable` (its `id`
changes from `rollID` to `path.path` — see `Roll.id`, `Model/Roll.swift:35`),
and by relocating the library base in Settings (`SettingsView.swift:30-39`).

**Fix:** in `resolveSelectedRoll`, when `selection != nil` and no roll matches,
set `selection = nil` so the empty state shows. Guard against clearing during
the known-transient window the `library.rolls` `onChange` exists to cover
(a roll created by `NewRollSheet` and selected before the rescan lands) — e.g.
only clear when `library.isScanning == false`.

**Verify:** select a roll, delete its folder in the Finder, trigger a rescan
(create another roll) — the detail pane must fall back to "No Roll Selected".

---

## M2 — `isProbing` is one flag shared by two independent probes

**Where:** `mac/ScannyBoy/Model/ConfigurationModel.swift:223-234` (catalogue probe)
and `:270-310` (validation probe).

**What:** `startCatalogueProbe` and `scheduleValidation` both set `isProbing =
true` and both set it to `false` when *they* finish. Whichever finishes first
clears it, so the flag reports "done" while the other probe is still in
flight. `ContentView.detailColumn` swaps the whole configuration UI on this
flag, and `runEnabled` is read while it is stale.

There is also a smaller leak: `inputFolder`'s `didSet` only starts a probe
when the new value is non-`nil`, so setting it to `nil` would leave
`isProbing == true` permanently. No UI path does this today, but the `didSet`
should be symmetric.

**Fix:** replace with two flags (`isCataloguing`, `isValidating`) and expose
`var isProbing: Bool { isCataloguing || isValidating }`. Clear each flag in
the task that owns it, including on the cancelled path.

**Verify:** with a large folder, change the input folder and immediately
select files; the spinner/gating must not settle until both round trips
return.

---

## M3 — The configuration form is replaced by a spinner on every selection change

**Where:** `mac/ScannyBoy/Views/ContentView.swift:220`
(`if model.isProbing { ... } else { configurationSections; runSection }`).

**What:** every change to `selectedFiles` (i.e. every click in the catalogue
list) calls `scheduleValidation()`, which sets `isProbing`, which tears down
the Flat Field and Grouping sections and the Convert button and replaces them
with a centred `ProgressView`. Selecting a range of twelve files makes the
right-hand pane flash and reflow twelve times, and the scans-per-negative
picker is unreachable while any probe is running.

The comment justifies this for *correctness* ("the Stitch button's enablement
is not yet trustworthy"), which is right — but the remedy is too broad.

**Fix:** keep the sections mounted and instead
- disable just the Convert button while `isProbing`, and
- show a small inline `ProgressView` (e.g. in the Grouping section header, or
  beside the Convert button) rather than replacing the form.

Debouncing `scheduleValidation` by ~150–250 ms would also cut the probe
storm during a drag-select; do that in `ConfigurationModel`, not the view.

**Verify:** drag-select ten files — the form must not disappear, and exactly
one probe should run once the drag settles (if debounce is added).

---

## M4 — Breaking out of a CLI session's stream SIGTERMs the helper mid-exit

**Where (pattern, seven sites):**
- `mac/ScannyBoy/Model/RollLibrary.swift:185` (`createRoll`, `return .success`)
- `mac/ScannyBoy/Model/RollLibrary.swift:226` (`renameRoll`, `return renamed`)
- `mac/ScannyBoy/Model/RollLibrary.swift:282` (`deleteRoll`, `return`)
- `mac/ScannyBoy/Model/FlatFieldModel.swift:102` (`create`, `return .success`)
- `mac/ScannyBoy/Model/FlatFieldModel.swift:140` (`delete`, `return .success`)
- `mac/ScannyBoy/Model/ConfigurationModel.swift:261` (`fetchRollManifest`)
- `mac/ScannyBoy/Model/EditModel.swift:254` (`fetchRollManifest`)

**What:** each of these returns from inside `for await output in try await
session.start()` as soon as the interesting event arrives. That abandons the
`AsyncStream`, which fires `CLISession`'s `continuation.onTermination`
(`CLIBridge/CLISession.swift:169-179`), which cancels both reader tasks and calls
`stopChildIfNeeded()` — a SIGTERM to a child that is, at that exact moment,
in the middle of its own clean exit.

In practice the helper has usually already written its last line and is
tearing down, so this is mostly benign — but it is a race, it means the
`.completed` outcome and any trailing `warning` events are never observed, and
for `roll delete` / `flatfield delete` it means the app declares success on
the strength of an event without ever seeing the exit status.

**Fix:** in each site, record the result in a local and let the `for await`
loop run to completion, returning after it. Where a trailing `.completed`
outcome would change the answer (delete and rename especially), fold it in:
a non-`.success` outcome after a success event should downgrade the result
rather than be discarded.

**Verify:** add a test that drives a fake session emitting the success event
followed by extra `warning` lines and a `.completed`, and assert the model
observed all of them.

---

## M5 — `FlatFieldModel.creationProgress` is never reset

**Where:** `mac/ScannyBoy/Model/FlatFieldModel.swift:108` (set, never cleared),
consumed at `mac/ScannyBoy/Views/FlatFieldProfilesSheet.swift:174`.

**What:** `creationProgress` is assigned on every `flatfield_progress` event
and never returned to `nil`. After one calibration finishes, the next
"Create" shows the *previous* run's completed progress bar (typically full)
until the first new event arrives, i.e. the bar starts at 100% and jumps
backwards.

**Fix:** set `creationProgress = nil` at the top of `create(...)` and in a
`defer` at its end.

**Verify:** create two profiles with calibration frames in one sheet session;
the second bar must start empty.

---

## M6 — Duplicated `isCreating` state in the flat-field sheet

**Where:** `mac/ScannyBoy/Views/FlatFieldProfilesSheet.swift:19` and
`mac/ScannyBoy/Model/FlatFieldModel.swift`.

**What:** the sheet tracks `isCreating` in local `@State` while the model that
actually owns the session tracks nothing. Any other view (or the app-wide
busy gate from P4) has no way to know a calibration is running. If the sheet
is dismissed and reopened mid-calibration, the new instance shows no spinner
and its Create button is enabled.

**Fix:** move `isCreating` onto `FlatFieldModel` as `private(set) var`, set in
`create(...)` with a `defer`, and have the sheet read it. Fold into P4's
busy gate.

---

## M7 — `RestitchSheet` seeds `@State` from init parameters

**Where:** `mac/ScannyBoy/Views/RestitchSheet.swift:36-37`
(`@State var workDirectory: URL?` / `@State var outputFolder: URL?`, passed
from `ContentView.swift:99-104`).

**What:** SwiftUI applies a `@State` property's memberwise-init value only the
first time that view identity appears. The sheet happens to work today
because `.sheet(isPresented:)` tears the content down on dismiss, but the
pattern is silently wrong: if the sheet is ever kept alive, reused, or
converted to `.sheet(item:)`, the second presentation will keep the first
presentation's folders and quietly re-stitch the wrong directory.

**Fix:** take the seeds as plain `let` properties (`initialWorkDirectory`,
`initialOutputFolder`), keep separate `@State` for the editable values, and
copy across in `.task`/`.onAppear`. Or use an explicit
`init(... ) { _workDirectory = State(initialValue: ...) }` with a comment
naming the constraint.

**Also here:** `startRestitch` (`RestitchSheet.swift:125`) calls
`RunManifest.read(inOutputFolder:)` synchronously on the main actor.
`RunModel.readManifest` (`Model/RunModel.swift:508`) already does this off-main
via `Task.detached` for exactly this reason. Match it — the result only feeds
`totalNegatives`, so it can be resolved after `run.start`.

---

## M8 — Unreadable rolls are selectable despite the `.disabled`

**Where:** `mac/ScannyBoy/Views/RollSidebar.swift:183`
(`.disabled(roll.status == .unreadable)` on `RollRow`'s body).

**What:** `.disabled` applied to a row's *content* dims the content and blocks
its own controls; it does not remove the row from the `List`'s selection.
Section 3.10's "unreadable rolls shown disabled" is therefore only half
implemented — the user can select one and land in a workspace pointed at a
roll the CLI could not read, where every probe fails with an unexplained
error.

**Fix:** use `.selectionDisabled(roll.status == .unreadable)` on the row
(macOS 14+), keeping the visual `.disabled` for the dimming.

**Verify:** corrupt a roll's `scanny-boy-roll.json`, rescan, and confirm the
row cannot be selected.

---

## M9 — Metadata Apply results are detected by a fragile heuristic

**Where:** `mac/ScannyBoy/Views/MetadataStageView.swift:66-71`.

**What:** the condition for showing the Apply summary is
`run.phase == .finished && summary != nil && run.stitchedNegatives.isEmpty &&
(!run.appliedNegativeIDs.isEmpty || !run.skippedMetadata.isEmpty)` — i.e.
"infer that the last run was an apply-metadata by the shape of its results".
It fails in two real cases: an apply that failed outright (a CLI `error`
event, zero applied, zero skipped) shows nothing at all, and an apply of zero
negatives shows nothing.

Symmetrically, `ContentView.swift:235-236` shows a section literally
titled **"Convert Results"** whenever `run.phase != .idle`, including for an
apply-metadata run started from the Metadata tab — the Add Scans tab reports
a metadata apply as a conversion.

**Fix:** expose the invocation on `RunModel` (it already stores
`invokedCommandName`, `Model/RunModel.swift:143`, as `@ObservationIgnored
private`) as an observable, typed value — e.g.
`enum Invocation { case convert, run, stitch, applyMetadata }` with
`private(set) var invocation: Invocation?`. Then key both views on it: the
Metadata tab shows its summary when `invocation == .applyMetadata`, and
Add Scans shows "Convert Results" only when it is not.

Note `invokedCommandName` is compared against string literals in three
derived properties (`negativesCompleted`, `isStitchInvocation`,
`touchesRollManifest`) — the enum removes those stringly-typed comparisons
too.

---

## M10 — A finished run keeps reporting "in progress" during the manifest read-back

**Where:** `mac/ScannyBoy/Model/RunModel.swift:412` (`finish()`).

**What:** `finish()` awaits a full `roll info` CLI round trip *before* setting
`phase = .finished`. Until it returns, `isActive` is still true, so the UI
keeps showing `RunProgressView` — a progress bar frozen at 100% with an
"Estimating…"/stale remaining time — and Cancel stays enabled while `session`
is about to be nilled. On a large roll the round trip is not instant.

**Fix:** introduce a distinct terminal-but-reconciling state (e.g. add
`case finishing` to `Phase`, excluded from `isActive` and from `canCancel`),
set it before the `await`, and move to `.finished` after. Views can show
"Finishing…" for that window.

---

## L1 — `ExportStageView.canExport` contains a dead check

`mac/ScannyBoy/Views/ExportStageView.swift:79`: `!rollURL.path.isEmpty` can
never be false for a URL that came from a roll. Drop it; the meaningful
conditions are `export.canExport`, `outputDirectory != nil`, and
`edit.roll != nil`.

## L2 — Icon-only buttons have `.help` but no accessibility label

`mac/ScannyBoy/Views/EditStageView.swift:111`, `:119`, `:140` — the rotate and
trash buttons are `Image(systemName:)` only. `.help` is a tooltip, not an
accessibility label. Add `.accessibilityLabel(...)`. (The flat-field sheet's
trash button, `FlatFieldProfilesSheet.swift:115`, gets this right — copy it.)
The stage `Picker` at `ContentView.swift:123` uses `.labelsHidden()` with no
accessibility label either.

## L3 — Sheet sizing uses `idealHeight`

`mac/ScannyBoy/Views/FlatFieldProfilesSheet.swift:57`:
`.frame(minWidth: 460, idealHeight: 480)`. Sheets do not adopt `idealHeight`;
with many profiles the sheet grows unbounded. Use `minHeight`/`maxHeight` and
let the inner `ScrollView` do the work.

## L4 — `ExportModel.export`'s completion is not guarded against supersession

`mac/ScannyBoy/Model/ExportModel.swift:99`: the task sets `phase = .finished`
unconditionally at its tail. `exportTask` is never cancelled and a second
`export(...)` is refused by `canExport`, so this is currently unreachable —
but the guard costs nothing and `RunModel` already establishes the pattern.
Also `outputDirectory = output` at `:84` duplicates the assignment the view
already made at `ExportStageView.swift:95`.

## L5 — `NewRollSheet` is presented from two places with divergent behaviour

`mac/ScannyBoy/Views/ContentView.swift:43` + `:107` (empty-state button) and
`mac/ScannyBoy/Views/RollSidebar.swift:17` + `:71` (toolbar **+**). Two
`@State` flags, two closures, and only the sidebar's routes through
`onRollCreated`. They happen to do the same thing today. Consider hoisting
presentation to `ContentView` and passing a binding down, so the post-create
behaviour lives in one place.

## L6 — `NSOpenPanel.runModal()` blocks the main run loop

`mac/ScannyBoy/Views/ContentView.swift:409` (`pickFolder`),
`mac/ScannyBoy/Views/SettingsView.swift:37`,
`mac/ScannyBoy/Views/FlatFieldProfilesSheet.swift:225` and `:238`. Acceptable
on macOS and unlikely to be worth changing, but `beginSheetModal(for:)`
attaches the panel to the window and keeps the app responsive. Only worth
doing if a picker is ever opened while a run is streaming events.

---

## Not bugs (checked, no action)

- `RunModel.completionSummary`'s apply-metadata branch falls through to the
  general switch for `.cancelled`/`.usageError`/`.terminatedBySignal` — that
  is deliberate and correct.
- `RollLibrary.deleteRoll` recycles the folder before unregistering; the
  ordering comment is right and the crash window it describes is the benign
  one.
- `ThumbnailLoader`'s `inFlight` coalescing and negative caching are sound;
  a cancelled `.task` awaiting `task.value` does not orphan the detached
  generation.
- `ConfigurationModel.selectedFilesInCanonicalOrder` filters the catalogue
  rather than iterating the `Set` — correct per section 3.3.
- `EditModel.moveSelection`'s clamping at both ends is intentional and
  consistent with `selectedNegative`'s "fall back to first" behaviour.
