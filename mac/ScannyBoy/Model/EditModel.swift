import Foundation
import Observation

/// State for the Edit tab (section 3.10): the selected roll's negatives in
/// sequence order and the dirty count Apply acts on. Apply itself is not
/// driven from here — it goes through the app's
/// one shared `RunModel`/`CLISession`, exactly like Run and re-stitch
/// (section 3.10: "There is one `RunModel` and one `CLISession`, as now").
/// This model only reads the roll back and reports what it sees.
///
/// The roll capture date and each negative's date override are read-only
/// here: no CLI command exists yet to write `metadata.roll_capture_date` or
/// a negative's `capture_time.date_override` (section 3.7/3.8), so Chunk
/// P3-12 stops short of letting the Edit tab set them. `shots_per_negative`
/// is read-only for the same reason — nothing updates an existing roll's
/// value, only `roll init` sets it once.
@MainActor
@Observable
final class EditModel {
    let runner: CLIRunner

    /// Set by `ContentView` from the sidebar selection, exactly like
    /// `ConfigurationModel.rollURL`.
    var rollURL: URL? {
        didSet {
            guard rollURL != oldValue else { return }
            roll = nil
            selectedNegativeID = nil
            selectedNegativeIDs = []
            startRollFetch(rollURL: rollURL)
        }
    }

    /// `roll info` for `rollURL` (section 3.1: Swift never parses
    /// `scanny-boy-roll.json` itself).
    private(set) var roll: RollManifest?
    @ObservationIgnored private var rollTask: Task<Void, Never>?

    /// The Edit tab's anchor selection: the negative shown large above the
    /// filmstrip. `nil` means "fall back to the first visible negative",
    /// which is also how a freshly loaded roll starts out.
    var selectedNegativeID: String?

    /// The full multi-selection the edit controls act on: shift-click
    /// extends a range, command-click toggles frames, Cmd-A selects all,
    /// Cmd-D deselects all. Empty means "just the anchor" — the controls
    /// then act on the single negative the preview pane shows.
    var selectedNegativeIDs: Set<String> = []

    /// Set while one `edit rotate` round trip is in flight. Rotate is its
    /// own short CLI session, deliberately not `RunModel`'s — but the
    /// one-helper-at-a-time discipline holds: the views gate on
    /// `run.isActive || edit.isRotating || edit.isDeleting`, and each flag
    /// refuses re-entry.
    private(set) var isRotating = false

    /// Set while one `edit delete` round trip is in flight, with the same
    /// one-helper-at-a-time discipline as `isRotating`.
    private(set) var isDeleting = false

    init(runner: CLIRunner) {
        self.runner = runner
    }

    // MARK: - Derived state

    /// Negatives to show, ordered by `sequence` (section 3.7) — unranked
    /// ones (`sequence == nil`, i.e. `pending`/`failed`) sort after every
    /// ranked one, in `negatives`' own append order among themselves, since
    /// section 3.7 gives them no rank to compare by.
    var visibleNegatives: [RollManifest.Negative] {
        (roll?.negatives ?? []).sorted { lhs, rhs in
            switch (lhs.sequence, rhs.sequence) {
            case let (left?, right?):
                return left < right
            case (nil, _?):
                return false
            case (_?, nil):
                return true
            case (nil, nil):
                return false
            }
        }
    }

    /// Section 3.8: dirty negatives eligible for Apply — completed, with an
    /// intended capture time that differs from what was last written.
    var dirtyNegatives: [RollManifest.Negative] {
        (roll?.negatives ?? []).filter { $0.isCompleted && $0.captureTime.isDirty }
    }

    var dirtyCount: Int { dirtyNegatives.count }

    /// Apply is offered only while there is something for it to do, and
    /// only while nothing else is already using the shared `RunModel`
    /// (section 3.10: one active run app-wide) — the caller is expected to
    /// also gate on `run.isActive`, exactly as the Run button does.
    var canApply: Bool { dirtyCount > 0 }

    /// `apply-metadata --roll DIR` for the selected roll, or `nil` when
    /// there is nothing to apply or no roll is selected.
    var applyCommand: CLICommand? {
        guard canApply, let rollURL else { return nil }
        return .applyMetadata(roll: rollURL)
    }

    /// The selected negative, or the first visible one when nothing (or
    /// something stale, e.g. after a roll switch) is selected.
    var selectedNegative: RollManifest.Negative? {
        let negatives = visibleNegatives
        if let selectedNegativeID,
            let match = negatives.first(where: { $0.negativeID == selectedNegativeID })
        {
            return match
        }
        return negatives.first
    }

    /// The negatives the rotate/flip/delete controls act on: the whole
    /// selection when one exists, else the single negative the preview
    /// pane shows. Filmstrip order, whatever order the clicks came in.
    var selectionTargets: [RollManifest.Negative] {
        let negatives = visibleNegatives
        if !selectedNegativeIDs.isEmpty {
            return negatives.filter { selectedNegativeIDs.contains($0.negativeID) }
        }
        return selectedNegative.map { [$0] } ?? []
    }

    /// Whether the filmstrip should highlight `negativeID`: a member of the
    /// multi-selection, or — when the selection is empty — the negative the
    /// preview pane resolves to (the anchor, or the first visible one on a
    /// freshly loaded roll, exactly as the single-selection tab always
    /// showed).
    func isSelected(_ negativeID: String) -> Bool {
        if !selectedNegativeIDs.isEmpty {
            return selectedNegativeIDs.contains(negativeID)
        }
        return selectedNegative?.negativeID == negativeID
    }

    /// Filmstrip clicks land here. A plain click selects one frame; a
    /// shift-click extends a contiguous range from the anchor (last
    /// clicked, else first visible); a command-click toggles the frame in
    /// or out of the selection. The anchor always ends on the clicked
    /// frame, except for a range extension, which keeps it so the user can
    /// widen or shrink the same range.
    func select(
        _ negativeID: String, additive: Bool = false, extendingRange: Bool = false
    ) {
        let negatives = visibleNegatives
        guard let targetIndex = negatives.firstIndex(where: { $0.negativeID == negativeID })
        else { return }

        if extendingRange {
            let anchorIndex = selectedNegativeID.flatMap { anchor in
                negatives.firstIndex { $0.negativeID == anchor }
            } ?? negatives.startIndex
            let lower = min(anchorIndex, targetIndex)
            let upper = max(anchorIndex, targetIndex)
            selectedNegativeIDs = Set(negatives[lower...upper].map(\.negativeID))
            return
        }

        if additive {
            if selectedNegativeIDs.contains(negativeID) {
                selectedNegativeIDs.remove(negativeID)
            } else {
                selectedNegativeIDs.insert(negativeID)
            }
        } else {
            selectedNegativeIDs = [negativeID]
        }
        selectedNegativeID = negativeID
    }

    /// Cmd-A: every visible frame joins the selection. The anchor (and so
    /// the preview pane) stays where it was.
    func selectAll() {
        selectedNegativeIDs = Set(visibleNegatives.map(\.negativeID))
    }

    /// Cmd-D: the multi-selection empties out. The anchor stays — the
    /// preview pane keeps showing the frame the user was looking at — so
    /// the next rotate/flip/delete acts on that one, exactly as the tab
    /// behaved before multi-select existed.
    func deselectAll() {
        selectedNegativeIDs = []
    }

    /// Option-left/Option-right and filmstrip clicks land here. The filmstrip
    /// shows every negative in `visibleNegatives` order, so selection moves
    /// through that same order. Keyboard navigation is single-frame: it
    /// collapses any multi-selection to the frame landed on.
    func selectNext() {
        moveSelection(+1)
    }

    func selectPrevious() {
        moveSelection(-1)
    }

    private func moveSelection(_ delta: Int) {
        let negatives = visibleNegatives
        guard !negatives.isEmpty else { return }
        let current = selectedNegativeID.flatMap { id in
            negatives.firstIndex { $0.negativeID == id }
        } ?? negatives.startIndex
        let next = max(negatives.startIndex, min(negatives.index(before: negatives.endIndex), current + delta))
        selectedNegativeID = negatives[next].negativeID
        selectedNegativeIDs = [selectedNegativeID!]
    }

    // MARK: - Editing

    /// Records one 90-degree rotation per selected negative through the CLI
    /// and refreshes the roll when the edits are confirmed. The published
    /// TIFFs are never touched — only the ops logs and the CLI-rendered
    /// previews. One CLI session for the whole selection; the CLI validates
    /// it up front, so a bad frame fails the batch before anything records.
    func rotate(_ targets: [RollManifest.Negative], clockwise: Bool) async {
        guard !targets.isEmpty else { return }
        await recordTransform(targets) { rollURL in
            .editRotate(roll: rollURL, negatives: targets.map(\.negativeID), clockwise: clockwise)
        }
    }

    /// Records a horizontal mirror of the pixels as they currently render —
    /// *after* any recorded rotations — per selected negative. Same
    /// contract as `rotate(_:clockwise:)`.
    func flip(_ targets: [RollManifest.Negative]) async {
        guard !targets.isEmpty else { return }
        await recordTransform(targets) { rollURL in
            .editFlip(roll: rollURL, negatives: targets.map(\.negativeID))
        }
    }

    /// One `edit` session per selection: streams the confirmation events,
    /// applying each `edit_recorded` to the in-memory roll as it arrives so
    /// the previews update progressively, then reconciles with a refresh.
    private func recordTransform(
        _ targets: [RollManifest.Negative], command: (URL) -> CLICommand
    ) async {
        guard let rollURL, !isRotating, !isDeleting else { return }
        isRotating = true
        defer { isRotating = false }
        do {
            for await output in try await runner.session(for: command(rollURL)).start() {
                if case .event(let event) = output, event.kind == .editRecorded,
                    let negativeID = event.negativeID
                {
                    applyEditRecorded(event, negativeID: negativeID)
                }
            }
        } catch {
            return
        }
        // The in-place updates above are what the user sees; the refresh
        // reconciles anything the events' fields did not carry.
        refresh()
    }

    /// Deletes the selected negatives through the CLI and refreshes the
    /// roll when the deletion is confirmed: each record (and its ops log)
    /// leaves the library database, each published TIFF leaves the roll
    /// folder, and each rendered preview leaves Application Support. The
    /// anchor moves to the neighbour after the last-deleted selection
    /// member — the next one, else the previous — so the user is left
    /// looking at something sensible instead of a stale id.
    func delete(_ targets: [RollManifest.Negative]) async {
        guard let rollURL, !isDeleting, !isRotating, !targets.isEmpty else { return }
        isDeleting = true
        defer { isDeleting = false }

        // Computed before the manifest changes anywhere: the neighbour in
        // `visibleNegatives` order, past the last-deleted selection member.
        let negatives = visibleNegatives
        let targetIDs = Set(targets.map(\.negativeID))
        let lastIndex = negatives.lastIndex { targetIDs.contains($0.negativeID) }
        let neighbour: String? = lastIndex.flatMap { index in
            if index + 1 < negatives.count {
                return negatives[index + 1].negativeID
            }
            return index > 0 ? negatives[index - 1].negativeID : nil
        }

        let command = CLICommand.editDelete(roll: rollURL, negatives: targets.map(\.negativeID))
        var deletedIDs = Set<String>()
        do {
            for await output in try await runner.session(for: command).start() {
                if case .event(let event) = output, event.kind == .negativeDeleted,
                    let negativeID = event.negativeID
                {
                    deletedIDs.insert(negativeID)
                }
            }
        } catch {
            return
        }
        // The refresh reconciles the roll either way; the selection only
        // moves when the deletion actually happened.
        refresh()
        if !deletedIDs.isEmpty {
            selectedNegativeIDs.subtract(deletedIDs)
            if targetIDs.contains(selectedNegativeID ?? "") {
                selectedNegativeID = neighbour
            }
        }
    }

    /// Applies an `edit_recorded` event to the in-memory roll without a
    /// round trip: preview path and net transform (turns plus the mirror
    /// flag) are exactly what the event carries.
    private func applyEditRecorded(_ event: CLIEvent, negativeID: String) {
        guard let manifest = roll,
            let index = manifest.negatives.firstIndex(where: { $0.negativeID == negativeID }),
            let turns = event.rotationQuarterTurns
        else { return }
        let negative = manifest.negatives[index]
        roll = manifest.replacingNegative(
            RollManifest.Negative(
                negativeID: negative.negativeID,
                runID: negative.runID,
                sequence: negative.sequence,
                members: negative.members,
                expectedOutput: negative.expectedOutput,
                status: negative.status,
                output: negative.output,
                captureTime: negative.captureTime,
                globalRMSPixels: negative.globalRMSPixels,
                rebateDeviationPixels: negative.rebateDeviationPixels,
                previewPath: event.previewPath ?? negative.previewPath,
                rotationQuarterTurns: turns,
                flippedHorizontally: event.flippedHorizontally
                    ?? negative.flippedHorizontally
            )
        )
    }

    // MARK: - Fetching

    // MARK: - Fetching

    /// Re-fetches the roll. Callers refresh after Apply finishes, exactly as
    /// `ConfigurationModel.refreshValidation()` does after a run.
    func refresh() {
        startRollFetch(rollURL: rollURL)
    }

    private func startRollFetch(rollURL: URL?) {
        rollTask?.cancel()
        guard let rollURL else { return }
        rollTask = Task { [weak self, runner] in
            let manifest = await Self.fetchRollManifest(runner: runner, roll: rollURL)
            guard let self, !Task.isCancelled else { return }
            self.roll = manifest
        }
    }

    private static func fetchRollManifest(runner: CLIRunner, roll: URL) async -> RollManifest? {
        // Recorded rather than returned immediately (M4): see
        // `RollLibrary.createRoll` — returning from inside the loop abandons
        // the stream and can SIGTERM a helper that is in the middle of its
        // own clean exit.
        var manifest: RollManifest?
        do {
            for await output in try await runner.session(for: .rollInfo(roll: roll)).start() {
                guard case .event(let event) = output, event.kind == .rollInfo,
                    let fields = event.manifest
                else { continue }
                manifest = RollManifest(fields: fields)
            }
        } catch {
            return nil
        }
        return manifest
    }

    // MARK: - Testing

    /// Waits for any roll fetch currently in flight. Test-only: production
    /// code drives everything from `@Observable`'s change notifications.
    func waitForPendingFetch() async {
        await rollTask?.value
    }
}
