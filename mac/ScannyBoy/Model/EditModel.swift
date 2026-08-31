import Foundation
import Observation

/// State for the Edit tab (section 3.10): the selected roll's negatives in
/// sequence order, the dirty count Apply acts on, and the superseded-toggle
/// filter. Apply itself is not driven from here — it goes through the app's
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
            startRollFetch(rollURL: rollURL)
        }
    }

    /// `roll info` for `rollURL` (section 3.1: Swift never parses
    /// `scanny-boy-roll.json` itself).
    private(set) var roll: RollManifest?
    @ObservationIgnored private var rollTask: Task<Void, Never>?

    /// Section 3.10: "Superseded negatives are hidden behind a 'Show
    /// replaced negatives' toggle."
    var showSupersededNegatives = false

    init(runner: CLIRunner) {
        self.runner = runner
    }

    // MARK: - Derived state

    /// Negatives to show, filtered by `showSupersededNegatives` and ordered
    /// by `sequence` (section 3.7) — superseded ones (`sequence == nil`)
    /// sort after every ranked one, in `negatives`' own append order among
    /// themselves, since section 3.7 gives them no rank to compare by.
    var visibleNegatives: [RollManifest.Negative] {
        let source = showSupersededNegatives ? (roll?.negatives ?? []) : (roll?.liveNegatives ?? [])
        return source.sorted { lhs, rhs in
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

    /// Section 3.8: dirty negatives eligible for Apply — completed, not
    /// superseded, with an intended capture time that differs from what was
    /// last written.
    var dirtyNegatives: [RollManifest.Negative] {
        (roll?.liveNegatives ?? []).filter { $0.isCompleted && $0.captureTime.isDirty }
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
        do {
            for await output in try await runner.session(for: .rollInfo(roll: roll)).start() {
                if case .event(let event) = output, event.kind == .rollInfo,
                    let fields = event.manifest
                {
                    return RollManifest(fields: fields)
                }
            }
        } catch {
            return nil
        }
        return nil
    }

    // MARK: - Testing

    /// Waits for any roll fetch currently in flight. Test-only: production
    /// code drives everything from `@Observable`'s change notifications.
    func waitForPendingFetch() async {
        await rollTask?.value
    }
}
