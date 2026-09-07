import AppKit
import Foundation
import ImageIO
import Observation

/// The Edit preview's display mode (protocol version 11's
/// positive/negative toggle). The positive is the CLI's inverted, tone-
/// graded encode — the look the filmstrip reads as a print. The negative
/// is the published TIFF's own un-inverted appearance, the flat density
/// view the tone adjustment never reaches, for judging negative densities.
enum PreviewDisplayMode: String {
    case positive
    case negative
}

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
    var selectedNegativeID: String? {
        didSet {
            guard selectedNegativeID != oldValue else { return }
            // The spot markers belong to the negative on screen; a new
            // selection fetches its set (SPOTTING_PLAN §8.2).
            fetchSpotsForSelection()
        }
    }

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

    /// Set while one `edit tone` round trip is in flight — a soft busy
    /// indicator for the tone panel; the sliders stay enabled so the user
    /// can keep dragging while a prior commit finishes or is cancelled.
    private(set) var isSettingTone = false

    /// Set while one `edit color` round trip is in flight.
    private(set) var isSettingColor = false

    /// Set while one `edit detect-spots` round trip is in flight —
    /// detection decodes a full published TIFF, so it can take a moment.
    private(set) var isDetectingSpots = false

    /// Set while one `edit spots` review round trip is in flight. A
    /// rejection must never lose the user's click, so unlike the sliders
    /// there is no debounce here.
    private(set) var isReviewingSpots = false

    /// The spot set of the negative the preview pane shows (protocol
    /// version 13): display-space rects straight from the CLI, refreshed
    /// by `list-spots` whenever the selection changes and after a roll
    /// refresh. `nil` — no set, or not loaded yet. Swift converts no
    /// coordinates; the rects it holds are the ones it draws.
    private(set) var spots: NegativeSpots?

    /// Whether the spots popover is showing. Markers stay visible while it
    /// is open even when repair is on — the point of turning repair on is
    /// to look at the result, and the popover is how.
    var showsSpotsPopover = false

    /// Debounces slider commits: a fast drag across many ISO-R steps fires
    /// one CLI round trip per pause, not one per step crossed.
    private static let toneDebounce = Duration.milliseconds(200)
    @ObservationIgnored private var toneScheduleTask: Task<Void, Never>?
    @ObservationIgnored private var toneCommitTask: Task<Void, Never>?
    @ObservationIgnored private var activeToneSession: CLISession?

    private static let colorDebounce = Duration.milliseconds(200)
    @ObservationIgnored private var colorScheduleTask: Task<Void, Never>?
    @ObservationIgnored private var colorCommitTask: Task<Void, Never>?
    @ObservationIgnored private var activeColorSession: CLISession?

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

    /// Section 3.8's dirty-count / Apply machinery is retired: metadata
    /// reaches TIFFs only at export now, and the export writes the intended
    /// capture time straight from the database's intent. Nothing applies
    /// into published TIFFs from this model any more.

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
        guard let rollURL, !isRotating, !isDeleting, !isSettingTone else { return }
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

    /// Queues a tone commit after [`toneDebounce`](EditModel.toneDebounce).
    /// Each new call cancels the prior scheduled commit; a superseding
    /// in-flight CLI session is cancelled when the debounced commit runs.
    func scheduleTone(
        _ targets: [RollManifest.Negative],
        adjustment: ToneAdjustment,
        auto: ToneAutoFlags = []
    ) {
        toneScheduleTask?.cancel()
        toneScheduleTask = Task { [weak self] in
            try? await Task.sleep(for: Self.toneDebounce)
            guard !Task.isCancelled, let self else { return }
            await self.commitTone(targets, adjustment: adjustment, auto: auto)
        }
    }

    /// Commits the tone adjustment immediately, cancelling any debounced
    /// commit and any in-flight `edit tone` session superseded by this one.
    func commitTone(
        _ targets: [RollManifest.Negative],
        adjustment: ToneAdjustment?,
        auto: ToneAutoFlags = []
    ) async {
        toneScheduleTask?.cancel()
        toneScheduleTask = nil
        if let toneCommitTask {
            toneCommitTask.cancel()
            await toneCommitTask.value
        }
        let task = Task<Void, Never> { [weak self] in
            guard let self else { return }
            await self.performToneCommit(
                targets, adjustment: adjustment, auto: auto
            )
        }
        toneCommitTask = task
        await task.value
    }

    /// Records the selected negatives' preview tone adjustment through the
    /// CLI, or `nil` for the reset to the flat look.
    func setTone(
        _ targets: [RollManifest.Negative],
        adjustment: ToneAdjustment?,
        auto: ToneAutoFlags = []
    ) async {
        await commitTone(targets, adjustment: adjustment, auto: auto)
    }

    private func performToneCommit(
        _ targets: [RollManifest.Negative],
        adjustment: ToneAdjustment?,
        auto: ToneAutoFlags
    ) async {
        guard let rollURL, !isRotating, !isDeleting, !targets.isEmpty else { return }

        if let activeToneSession {
            await activeToneSession.cancel()
            self.activeToneSession = nil
        }
        guard !Task.isCancelled else { return }

        isSettingTone = true
        defer { isSettingTone = false }

        let command = CLICommand.editTone(
            roll: rollURL,
            negatives: targets.map(\.negativeID),
            adjustment: adjustment,
            auto: auto
        )
        let session = runner.session(for: command)
        activeToneSession = session
        defer { activeToneSession = nil }

        do {
            for await output in try await session.start() {
                if Task.isCancelled {
                    await session.cancel()
                    return
                }
                if case .event(let event) = output, event.kind == .editRecorded,
                    let negativeID = event.negativeID
                {
                    applyEditRecorded(event, negativeID: negativeID)
                }
            }
        } catch {
            return
        }
    }

    func scheduleColor(
        _ targets: [RollManifest.Negative],
        adjustment: ColorAdjustment,
        auto: ColorAutoFlags = []
    ) {
        colorScheduleTask?.cancel()
        colorScheduleTask = Task { [weak self] in
            try? await Task.sleep(for: Self.colorDebounce)
            guard !Task.isCancelled, let self else { return }
            await self.commitColor(targets, adjustment: adjustment, auto: auto)
        }
    }

    func commitColor(
        _ targets: [RollManifest.Negative],
        adjustment: ColorAdjustment?,
        auto: ColorAutoFlags = []
    ) async {
        colorScheduleTask?.cancel()
        colorScheduleTask = nil
        if let colorCommitTask {
            colorCommitTask.cancel()
            await colorCommitTask.value
        }
        let task = Task<Void, Never> { [weak self] in
            guard let self else { return }
            await self.performColorCommit(targets, adjustment: adjustment, auto: auto)
        }
        colorCommitTask = task
        await task.value
    }

    func setColor(
        _ targets: [RollManifest.Negative],
        adjustment: ColorAdjustment?,
        auto: ColorAutoFlags = []
    ) async {
        await commitColor(targets, adjustment: adjustment, auto: auto)
    }

    private func performColorCommit(
        _ targets: [RollManifest.Negative],
        adjustment: ColorAdjustment?,
        auto: ColorAutoFlags = []
    ) async {
        guard let rollURL, !isRotating, !isDeleting, !targets.isEmpty else { return }

        if let activeColorSession {
            await activeColorSession.cancel()
            self.activeColorSession = nil
        }
        guard !Task.isCancelled else { return }

        isSettingColor = true
        defer { isSettingColor = false }

        let command = CLICommand.editColor(
            roll: rollURL,
            negatives: targets.map(\.negativeID),
            adjustment: adjustment,
            auto: auto
        )
        let session = runner.session(for: command)
        activeColorSession = session
        defer { activeColorSession = nil }

        do {
            for await output in try await session.start() {
                if Task.isCancelled {
                    await session.cancel()
                    return
                }
                if case .event(let event) = output, event.kind == .editRecorded,
                    let negativeID = event.negativeID
                {
                    applyEditRecorded(event, negativeID: negativeID)
                }
            }
        } catch {
            return
        }
    }

    // MARK: - Spotting (protocol version 13, SPOTTING_PLAN §8.2)

    /// Runs the detector over the whole selection — one `edit detect-spots`
    /// round trip — and refreshes the roll: the summary in `roll info` has
    /// changed. The published TIFFs are untouched; the ops gain proposals
    /// only, with `repair` preserved from the previous op.
    func detectSpots(
        _ targets: [RollManifest.Negative], sensitivity: Double
    ) async {
        guard let rollURL, !isDetectingSpots, !isReviewingSpots, !targets.isEmpty else {
            return
        }
        isDetectingSpots = true
        defer { isDetectingSpots = false }
        let command = CLICommand.editDetectSpots(
            roll: rollURL,
            negatives: targets.map(\.negativeID),
            sensitivity: sensitivity
        )
        do {
            for await output in try await runner.session(for: command).start() {
                if case .event(let event) = output, event.kind == .spotsReported {
                    applySpotsReported(event)
                }
            }
        } catch {
            return
        }
        refresh()
    }

    /// Records one rejection — a decision the user made, never a
    /// disappearance — and applies the confirmation to the local state
    /// without a refetch. Rejection is by `id`; Swift converts nothing.
    func rejectSpot(_ negative: RollManifest.Negative, id: Int) async {
        await reviewSpot(negative, reject: id)
    }

    /// Records one acceptance (un-rejection) by id.
    func acceptSpot(_ negative: RollManifest.Negative, id: Int) async {
        await reviewSpot(negative, accept: id)
    }

    private func reviewSpot(
        _ negative: RollManifest.Negative, reject rejectedID: Int? = nil,
        accept acceptedID: Int? = nil
    ) async {
        guard let rollURL, !isDetectingSpots, !isReviewingSpots else { return }
        isReviewingSpots = true
        defer { isReviewingSpots = false }
        let command = CLICommand.editSpots(
            roll: rollURL,
            negative: negative.negativeID,
            reject: rejectedID.map { [$0] } ?? [],
            accept: acceptedID.map { [$0] } ?? []
        )
        do {
            for await output in try await runner.session(for: command).start() {
                if case .event(let event) = output, event.kind == .spotsReported {
                    applySpotsReported(event)
                }
            }
        } catch {
            return
        }
        refresh()
    }

    /// Flips the whole-negative repair switch — the moment pixels change —
    /// and refreshes the roll.
    func setRepair(_ negative: RollManifest.Negative, on: Bool) async {
        guard let rollURL, !isDetectingSpots, !isReviewingSpots else { return }
        isReviewingSpots = true
        defer { isReviewingSpots = false }
        let command = CLICommand.editSpots(
            roll: rollURL, negative: negative.negativeID, repair: on
        )
        do {
            for await output in try await runner.session(for: command).start() {
                if case .event(let event) = output, event.kind == .spotsReported {
                    applySpotsReported(event)
                }
            }
        } catch {
            return
        }
        refresh()
    }

    /// Clears the negative's spot set entirely — repair off, no spots.
    func clearSpots(_ negative: RollManifest.Negative) async {
        guard let rollURL, !isDetectingSpots, !isReviewingSpots else { return }
        isReviewingSpots = true
        defer { isReviewingSpots = false }
        let command = CLICommand.editSpots(
            roll: rollURL, negative: negative.negativeID, clear: true
        )
        do {
            for await output in try await runner.session(for: command).start() {
                if case .event(let event) = output, event.kind == .spotsReported {
                    applySpotsReported(event)
                }
            }
        } catch {
            return
        }
        refresh()
    }

    /// The `list-spots` query for the negative the preview pane shows: a
    /// pure query — nothing recorded, no pixels touched — so it never
    /// calls `refresh()` (that would loop: the refresh fetches the roll,
    /// and the roll fetch loads spots).
    func loadSpots(_ negative: RollManifest.Negative) async {
        guard let rollURL else { return }
        let command = CLICommand.editListSpots(
            roll: rollURL, negative: negative.negativeID
        )
        do {
            for await output in try await runner.session(for: command).start() {
                if case .event(let event) = output, event.kind == .spotsReported,
                    event.spotsNegativeID == negative.negativeID
                {
                    applySpotsReported(event)
                }
            }
        } catch {
            return
        }
    }

    private func fetchSpotsForSelection() {
        guard let negative = selectedNegative else {
            spots = nil
            return
        }
        Task { [weak self] in
            await self?.loadSpots(negative)
        }
    }

    /// Applies a `spots_reported` payload to the local state: the full set
    /// to `spots`, and the matching summary into the in-memory manifest —
    /// the way `applyEditRecorded` does, so the UI moves without a `roll
    /// info` round trip. A stale set arrives as an empty list (SPOTTING_PLAN
    /// §1.5); the next `refresh()` reconciles the summary's `stale` flag.
    private func applySpotsReported(_ event: CLIEvent) {
        guard let loaded = NegativeSpots(event: event),
            let negativeID = event.spotsNegativeID
        else { return }
        spots = loaded
        guard let manifest = roll,
            let index = manifest.negatives.firstIndex(where: {
                $0.negativeID == negativeID
            })
        else { return }
        var updated = manifest.negatives[index]
        updated.spotsSummary = NegativeSpots.Summary(
            detectorVersion: loaded.detectorVersion,
            sensitivity: loaded.sensitivity,
            repair: loaded.repair,
            stale: false,
            count: loaded.spots.count,
            rejected: loaded.spots.count - loaded.accepted.count
        )
        roll = manifest.replacingNegative(updated)
    }

    /// Deletes the selected negatives through the CLI and refreshes the
    /// roll when the deletion is confirmed: each record (and its ops log)
    /// leaves the library database, each published TIFF leaves the roll
    /// folder, and each rendered preview leaves Application Support. The
    /// anchor moves to the neighbour after the last-deleted selection
    /// member — the next one, else the previous — so the user is left
    /// looking at something sensible instead of a stale id.
    func delete(_ targets: [RollManifest.Negative]) async {
        guard let rollURL, !isDeleting, !isRotating, !isSettingTone, !targets.isEmpty else { return }
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
    /// round trip: preview path, net transform (turns plus the mirror
    /// flag), and — for a `tone` op — the tone state are exactly what the
    /// event carries. The tone state rides the recorded op's `params`: a
    /// `tone` op always names both keys (explicit nulls for a reset),
    /// while the geometric ops carry none, leaving the state alone.
    private func applyEditRecorded(_ event: CLIEvent, negativeID: String) {
        guard let manifest = roll,
            let index = manifest.negatives.firstIndex(where: { $0.negativeID == negativeID }),
            let turns = event.rotationQuarterTurns
        else { return }
        let negative = manifest.negatives[index]
        var toneGradeR = negative.toneGradeR
        var toneSnapGamma = negative.toneSnapGamma
        var toneDensity = negative.toneDensity
        var toneShadowDensity = negative.toneShadowDensity
        var toneHighlightDensity = negative.toneHighlightDensity
        var toneToe = negative.toneToe
        var toneToeWidth = negative.toneToeWidth
        var toneShoulder = negative.toneShoulder
        var toneShoulderWidth = negative.toneShoulderWidth
        var colorWbCyan = negative.colorWbCyan
        var colorWbMagenta = negative.colorWbMagenta
        var colorWbYellow = negative.colorWbYellow
        var colorShadowCyan = negative.colorShadowCyan
        var colorShadowMagenta = negative.colorShadowMagenta
        var colorShadowYellow = negative.colorShadowYellow
        var colorHighlightCyan = negative.colorHighlightCyan
        var colorHighlightMagenta = negative.colorHighlightMagenta
        var colorHighlightYellow = negative.colorHighlightYellow
        var colorCastRemoval = negative.colorCastRemoval
        var colorCastRemovalHighlights = negative.colorCastRemovalHighlights
        var colorDyeSeparation = negative.colorDyeSeparation
        var colorSeparationDamping = negative.colorSeparationDamping
        var colorTemperature = negative.colorTemperature
        if let recorded = event.recordedTone {
            if let tone = recorded {
                toneGradeR = tone.gradeR
                toneSnapGamma = tone.snapGamma
                toneDensity = tone.density
                toneShadowDensity = tone.shadowDensity
                toneHighlightDensity = tone.highlightDensity
                toneToe = tone.toe
                toneToeWidth = tone.toeWidth
                toneShoulder = tone.shoulder
                toneShoulderWidth = tone.shoulderWidth
            } else {
                toneGradeR = nil
                toneSnapGamma = nil
                toneDensity = nil
                toneShadowDensity = nil
                toneHighlightDensity = nil
                toneToe = nil
                toneToeWidth = nil
                toneShoulder = nil
                toneShoulderWidth = nil
            }
        }
        if let recorded = event.recordedColor {
            if let color = recorded {
                colorWbCyan = color.wbCyan
                colorWbMagenta = color.wbMagenta
                colorWbYellow = color.wbYellow
                colorShadowCyan = color.shadowCyan
                colorShadowMagenta = color.shadowMagenta
                colorShadowYellow = color.shadowYellow
                colorHighlightCyan = color.highlightCyan
                colorHighlightMagenta = color.highlightMagenta
                colorHighlightYellow = color.highlightYellow
                colorCastRemoval = color.castRemoval
                colorCastRemovalHighlights = color.castRemovalHighlights
                colorDyeSeparation = color.dyeSeparation
                colorSeparationDamping = color.separationDamping
                colorTemperature = nil
            } else {
                colorWbCyan = nil
                colorWbMagenta = nil
                colorWbYellow = nil
                colorShadowCyan = nil
                colorShadowMagenta = nil
                colorShadowYellow = nil
                colorHighlightCyan = nil
                colorHighlightMagenta = nil
                colorHighlightYellow = nil
                colorCastRemoval = nil
                colorCastRemovalHighlights = nil
                colorDyeSeparation = nil
                colorSeparationDamping = nil
                colorTemperature = nil
            }
        }
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
                metadata: negative.metadata,
                globalRMSPixels: negative.globalRMSPixels,
                rebateDeviationPixels: negative.rebateDeviationPixels,
                previewPath: event.previewPath ?? negative.previewPath,
                rotationQuarterTurns: turns,
                flippedHorizontally: event.flippedHorizontally
                    ?? negative.flippedHorizontally,
                rectification: negative.rectification,
                toneGradeR: toneGradeR,
                toneSnapGamma: toneSnapGamma,
                toneDensity: toneDensity,
                toneShadowDensity: toneShadowDensity,
                toneHighlightDensity: toneHighlightDensity,
                toneToe: toneToe,
                toneToeWidth: toneToeWidth,
                toneShoulder: toneShoulder,
                toneShoulderWidth: toneShoulderWidth,
                colorWbCyan: colorWbCyan,
                colorWbMagenta: colorWbMagenta,
                colorWbYellow: colorWbYellow,
                colorShadowCyan: colorShadowCyan,
                colorShadowMagenta: colorShadowMagenta,
                colorShadowYellow: colorShadowYellow,
                colorHighlightCyan: colorHighlightCyan,
                colorHighlightMagenta: colorHighlightMagenta,
                colorHighlightYellow: colorHighlightYellow,
                colorCastRemoval: colorCastRemoval,
                colorCastRemovalHighlights: colorCastRemovalHighlights,
                colorDyeSeparation: colorDyeSeparation,
                colorSeparationDamping: colorSeparationDamping,
                colorTemperature: colorTemperature,
                errorCode: negative.errorCode,
                errorMessage: negative.errorMessage,
                maxOverlapMAD: negative.maxOverlapMAD,
                normalization: negative.normalization,
                usedClaheFallback: negative.usedClaheFallback,
                gridPitchRatio: negative.gridPitchRatio,
                gridAlignmentRatio: negative.gridAlignmentRatio,
                spotsSummary: negative.spotsSummary
            )
        )
    }

    // MARK: - Region rendering

    /// Renders one display-space region of `negative`'s published TIFF at
    /// 1:1 — `edit render-region` — in the display encode `mode` names
    /// (protocol version 9's inverted positive; protocol version 11's
    /// un-inverted negative, which no tone reaches), and loads the result
    /// for drawing at `rect.width/height` image pixels per physical screen
    /// pixel. A pure query backing the Edit tab's 100% zoom: the CLI
    /// records nothing and touches no pixels.
    func renderRegion(
        _ negative: RollManifest.Negative,
        rect: CGRect,
        mode: PreviewDisplayMode
    ) async -> Thumbnail? {
        guard let rollURL else { return nil }
        let output = Self.regionCacheURL(
            negativeID: negative.negativeID,
            generation: Self.renderGeneration(of: negative),
            mode: mode,
            rect: rect
        )
        let command = CLICommand.editRenderRegion(
            roll: rollURL,
            negative: negative.negativeID,
            x: Int(rect.minX),
            y: Int(rect.minY),
            width: Int(rect.width),
            height: Int(rect.height),
            output: output,
            mode: mode.rawValue
        )
        var rendered = false
        do {
            for await line in try await runner.session(for: command).start() {
                if case .event(let event) = line, event.kind == .regionRendered {
                    rendered = true
                }
            }
        } catch {
            return nil
        }
        guard rendered, let image = Self.fullResolutionImage(at: output) else {
            return nil
        }
        return Thumbnail(image: image)
    }

    /// Renders `negative`'s whole display image — `edit render-preview`,
    /// protocol version 11, downscaled to the managed preview's own longest
    /// edge — in the display encode `mode` names, and loads it for the Edit
    /// tab's fit view. The pure-query backing of the positive/negative
    /// toggle: the CLI records nothing and touches no pixels. The
    /// negative mode ignores the tone state, so its cache keying excludes
    /// it (`negativeViewGeneration`).
    func renderPreview(
        _ negative: RollManifest.Negative,
        mode: PreviewDisplayMode
    ) async -> Thumbnail? {
        guard let rollURL else { return nil }
        let generation = mode == .negative
            ? Self.negativeViewGeneration(of: negative)
            : Self.renderGeneration(of: negative)
        let output = Self.previewCacheURL(
            negativeID: negative.negativeID,
            generation: generation,
            mode: mode
        )
        let command = CLICommand.editRenderPreview(
            roll: rollURL,
            negative: negative.negativeID,
            mode: mode.rawValue,
            output: output
        )
        var rendered = false
        do {
            for await line in try await runner.session(for: command).start() {
                if case .event(let event) = line, event.kind == .previewRendered {
                    rendered = true
                }
            }
        } catch {
            return nil
        }
        guard rendered, let image = Self.fullResolutionImage(at: output) else {
            return nil
        }
        return Thumbnail(image: image)
    }

    private static var regionCacheDirectory: URL {
        URL.cachesDirectory.appending(path: "preview-regions", directoryHint: .isDirectory)
    }

    private static var previewCacheDirectory: URL {
        URL.cachesDirectory.appending(path: "rendered-previews", directoryHint: .isDirectory)
    }

    /// Everything the CLI's display encode folds into a rendered frame —
    /// the net transform, the tone state, and the spot repair — as one
    /// cache-generation token. The preview PNG and the 1:1 region renders
    /// are both keyed on it, so a new tone commit or a repair flip
    /// invalidates the old crops. The spot term changes whenever the
    /// rendered pixels change: repair flips, spots are rejected (a
    /// rejected spot's mask leaves the repair), or the set is
    /// re-detected.
    ///
    /// The CLI's own decoded-pixel cache (`previews.cached_preview_codes`,
    /// docs/OPTIMIZATION.md §3.1/§3.3) keys on the same idea minus the
    /// tone and colour terms — its array is pre-LUT, so tone and colour
    /// are encode steps there, not decode steps — and folds the whole
    /// spot set in hashed rather than summarized. The two sites cannot
    /// share one definition (one is Swift, one Python); they are
    /// commented at each other and must keep agreeing on the rule:
    /// everything that changes pixels before the display LUT is in, and
    /// nothing that comes after.
    static func renderGeneration(of negative: RollManifest.Negative) -> String {
        let tone: String
        if let adjustment = negative.toneAdjustment {
            tone = String(adjustment.hashValue)
        } else {
            tone = "flat"
        }
        let colour: String
        if let adjustment = negative.colorAdjustment {
            colour = String(adjustment.hashValue)
        } else {
            colour = "neutral"
        }
        return "\(negative.rotationQuarterTurns)#\(negative.flippedHorizontally)#\(tone)#\(colour)#\(spotsTerm(of: negative))"
    }

    /// The net-geometry part of `renderGeneration` — everything the
    /// negative view folds in. The tone state is deliberately absent: the
    /// negative view shows raw densities, and the tone adjustment never
    /// reaches it. The spot repair is deliberately present: what the user
    /// compares when they toggle repair on and off is the same in both
    /// views (SPOTTING_PLAN §3.3).
    static func negativeViewGeneration(of negative: RollManifest.Negative) -> String {
        "\(negative.rotationQuarterTurns)#\(negative.flippedHorizontally)#\(spotsTerm(of: negative))"
    }

    /// The spots half of a cache-generation token: `repair#count#rejected`
    /// is enough to change whenever the rendered pixels change. Getting
    /// this wrong is the "repair does nothing" bug — a cached PNG reused.
    private static func spotsTerm(of negative: RollManifest.Negative) -> String {
        guard let summary = negative.spotsSummary else { return "none" }
        return "\(summary.repair)#\(summary.count)#\(summary.rejected)"
    }

    private static func regionCacheURL(
        negativeID: String, generation: String, mode: PreviewDisplayMode, rect: CGRect
    ) -> URL {
        regionCacheDirectory.appending(
            path: "\(negativeID)-g\(generation.replacingOccurrences(of: "#", with: "-"))-\(mode.rawValue)-\(Int(rect.minX))-\(Int(rect.minY))-\(Int(rect.width))-\(Int(rect.height)).png"
        )
    }

    private static func previewCacheURL(
        negativeID: String, generation: String, mode: PreviewDisplayMode
    ) -> URL {
        previewCacheDirectory.appending(
            path: "\(negativeID)-g\(generation.replacingOccurrences(of: "#", with: "-"))-\(mode.rawValue).png"
        )
    }

    /// The PNG the CLI rendered, decoded at its native size — a pane-sized
    /// crop, so a bounded decode, same rule as `ThumbnailLoader`'s preview
    /// path.
    static func fullResolutionImage(at url: URL) -> NSImage? {
        let sourceOptions = [kCGImageSourceShouldCache: false] as CFDictionary
        guard let source = CGImageSourceCreateWithURL(url as CFURL, sourceOptions),
            let cgImage = CGImageSourceCreateImageAtIndex(source, 0, nil)
        else { return nil }
        return NSImage(
            cgImage: cgImage,
            size: NSSize(width: cgImage.width, height: cgImage.height)
        )
    }

    // MARK: - Metadata editing

    /// Set while one `metadata set` round trip is in flight, with the same
    /// one-helper-at-a-time discipline as `isRotating`/`isDeleting`.
    private(set) var isSettingMetadata = false

    /// The metadata-fields catalog: previously-entered values per field,
    /// most-recently-used first — the typeahead's suggestions. `caption` is
    /// deliberately absent: it is prose, not a canonical value, so the CLI
    /// never catalogs it.
    static let catalogedFields = ["film", "iso", "city", "state", "camera", "lens"]

    var catalog: [String: [String]] = [:]
    @ObservationIgnored private var catalogTask: Task<Void, Never>?

    /// Fetches the whole catalog through `metadata values`, one field at a
    /// time. Cheap (a handful of rows each) and only re-run on demand.
    func refreshCatalog() {
        catalogTask?.cancel()
        catalogTask = Task { [weak self, runner] in
            for field in Self.catalogedFields {
                var values: [String] = []
                do {
                    for await output in try await runner
                        .session(for: .metadataValues(field: field)).start()
                    {
                        if case .event(let event) = output,
                            event.kind == .metadataValues,
                            let fieldValues = event.metadataFieldValues
                        {
                            values = fieldValues
                        }
                    }
                } catch {
                    continue
                }
                guard let self, !Task.isCancelled else { return }
                self.catalog[field] = values
            }
        }
    }

    /// The negative's effective value for one field: its own explicit
    /// value, else the roll's fallback.
    func effectiveValue(of negative: RollManifest.Negative, field: String) -> String? {
        guard let roll else { return nil }
        return RollManifest.ImageMetadata.effective(
            negative.metadata, roll: roll.metadata, field: field
        )
    }

    /// The effective values a selection shows for one field. More than one
    /// distinct value (counting nils) means the form shows "<mixed values>".
    func effectiveValues(of targets: [RollManifest.Negative], field: String) -> Set<String?> {
        Set(targets.map { effectiveValue(of: $0, field: field) })
    }

    // The `metadata set` payloads below all funnel through one session
    // runner: the whole payload is validated by the CLI before anything is
    // written, and its `metadata_updated` confirmation carries the updated
    // manifest, which lands in memory the way an `edit_recorded` does — no
    // `roll info` round trip.

    /// Sets (or clears, with `nil`) the roll's capture date.
    func setRollCaptureDate(_ date: String?) async {
        await runMetadataSet(payload: ["roll": ["capture_date": date as String?]])
    }

    /// Sets (or clears, with `nil`) one roll-level metadata field.
    func setRollField(_ field: String, to value: String?) async {
        await runMetadataSet(payload: ["roll": [field: value as String?]])
        rememberCatalogValue(value, for: field)
    }

    /// Sets (or clears, with `nil`) one metadata field on every selected
    /// negative — one batched CLI invocation for the whole selection.
    func setNegativeField(
        _ targets: [RollManifest.Negative], field: String, to value: String?
    ) async {
        guard !targets.isEmpty else { return }
        var negatives: [String: [String: String?]] = [:]
        for target in targets {
            negatives[target.negativeID] = [field: value as String?]
        }
        await runMetadataSet(payload: ["negatives": negatives])
        rememberCatalogValue(value, for: field)
    }

    /// Sets (or clears, with `nil`) one negative's date override.
    func setNegativeCaptureDate(
        _ targets: [RollManifest.Negative], to date: String?
    ) async {
        guard !targets.isEmpty else { return }
        var negatives: [String: [String: String?]] = [:]
        for target in targets {
            negatives[target.negativeID] = ["capture_date": date as String?]
        }
        await runMetadataSet(payload: ["negatives": negatives])
    }

    private func runMetadataSet(payload: [String: Any]) async {
        guard let rollURL, !isSettingMetadata else { return }
        isSettingMetadata = true
        defer { isSettingMetadata = false }
        guard let data = try? JSONSerialization.data(withJSONObject: payload),
            let json = String(data: data, encoding: .utf8)
        else { return }
        do {
            for await output in try await runner
                .session(for: .metadataSet(roll: rollURL, payload: json)).start()
            {
                if case .event(let event) = output, event.kind == .metadataUpdated,
                    let fields = event.manifest
                {
                    roll = RollManifest(fields: fields)
                }
            }
        } catch {
            return
        }
    }

    /// Moves a just-committed value to the catalog's head so the typeahead
    /// offers it immediately; the next full refresh re-orders from the
    /// CLI's most-recently-used record.
    private func rememberCatalogValue(_ value: String?, for field: String) {
        guard let value, !value.isEmpty, Self.catalogedFields.contains(field) else { return }
        var values = catalog[field] ?? []
        values.removeAll { $0 == value }
        values.insert(value, at: 0)
        catalog[field] = values
    }

    // MARK: - Fetching

    /// Re-fetches the roll. Callers refresh after Apply finishes, exactly as
    /// `ConfigurationModel.clearValidationState()` does after a run.
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
            // The refreshed manifest carries fresh summaries; the displayed
            // negative's full spot list rides `list-spots` (SPOTTING_PLAN
            // §8.2: run on selection change and after a roll refresh).
            if let negative = self.selectedNegative {
                await self.loadSpots(negative)
            }
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

    /// Waits for any debounced or in-flight tone commit. Test-only.
    func waitForPendingTone() async {
        await toneScheduleTask?.value
        await toneCommitTask?.value
    }
}
