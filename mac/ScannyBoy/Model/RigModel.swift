import Foundation
import Observation

/// The rig profile list's model: every profile in the library,
/// refreshed by one `rig list` invocation, and created and deleted
/// through the CLI (`rig create` / `rig delete`) — never by
/// touching Application Support itself.
@MainActor
@Observable
final class RigModel {
    enum CreateResult {
        case success(RigProfile)
        case failure(CLICode, String)
    }

    enum DeleteResult {
        case success
        case failure(CLICode, String)
    }

    /// One `rig_progress` event, decoded: a long calibration's
    /// phase and progress. Phases: "detect", "fit", "chromatic".
    struct CreationProgress: Sendable, Hashable {
        let phase: String
        let completed: Int
        let total: Int
    }

    let runner: CLIRunner

    private(set) var profiles: [RigProfile] = []
    private(set) var isScanning = false
    /// Set for the duration of one `create(...)` round trip. Lives here
    /// rather than in the sheet's local `@State` so the app-wide busy gate
    /// (`AppActivity`) — and any other view — can see a calibration is
    /// running even if the sheet that started it was dismissed and reopened.
    private(set) var isCreating = false
    /// Drives the New Profile sheet's determinate progress bar while a
    /// calibration runs; nil when nothing is in flight.
    private(set) var creationProgress: CreationProgress?

    @ObservationIgnored private var scanTask: Task<Void, Never>?

    init(runner: CLIRunner) {
        self.runner = runner
        refresh()
    }

    // MARK: - Scanning

    /// Re-reads the profile list from `rig list`. Called on init, after
    /// a create, and after a delete, so the dropdown and the sheet never
    /// drift from what the CLI reports.
    func refresh() {
        scanTask?.cancel()
        isScanning = true
        scanTask = Task { [weak self, runner] in
            let result = await Self.fetchProfiles(runner: runner)
            guard let self, !Task.isCancelled else { return }
            self.profiles = result
            self.isScanning = false
        }
    }

    private static func fetchProfiles(runner: CLIRunner) async -> [RigProfile] {
        do {
            for await output in try await runner.session(for: .rigList()).start() {
                guard case .event(let event) = output else { continue }
                if event.kind == .rigList, let entries = event.rigProfiles {
                    return entries.compactMap(RigProfile.init(fields:))
                }
            }
        } catch {
            // Launch failure: the list simply stays as it was, same posture
            // as `RollLibrary.scan()`'s.
        }
        return []
    }

    // MARK: - Create

    /// `rig create --name [--calibration FILE ...]`.
    /// With calibration frames the geometric fit runs for minutes, and the
    /// CLI's `rig_progress` events drive `creationProgress`.
    func create(
        name: String,
        calibrationFrames: [URL]
    ) async -> CreateResult {
        isCreating = true
        creationProgress = nil
        defer {
            isCreating = false
            creationProgress = nil
        }
        var result: CreateResult = .failure(.unknown(""), "rig create produced no result")
        var outcome: CLIOutcome?
        do {
            for await output in try await runner.session(
                for: .rigCreate(name: name, calibrationFrames: calibrationFrames)
            ).start() {
                switch output {
                case .event(let event):
                    switch event.kind {
                    case .rigCreated:
                        guard let fields = event.rigProfile,
                            let profile = RigProfile(fields: fields)
                        else { continue }
                        result = .success(profile)
                    case .rigProgress:
                        if let phase = event.rigPhase,
                            let completed = event.completed,
                            let total = event.total
                        {
                            creationProgress = CreationProgress(
                                phase: phase, completed: completed, total: total
                            )
                        }
                    case .error:
                        let code = event.code ?? .unknown("")
                        result = .failure(code, event.message ?? "rig create failed")
                    default:
                        continue
                    }
                case .completed(let completion):
                    outcome = completion.outcome
                case .log, .failure:
                    continue
                }
            }
        } catch {
            return .failure(.unknown(""), error.localizedDescription)
        }
        if case .success = result {
            if let outcome, outcome != .success {
                return .failure(.unknown(""), "rig create did not complete successfully")
            }
            refresh()
        }
        return result
    }

    // MARK: - Delete

    /// `rig delete --profile`. The CLI refuses with
    /// `RIG_PROFILE_IN_USE` when any roll's invariants name the
    /// profile — the caller shows that refusal as an alert.
    func delete(_ profile: RigProfile) async -> DeleteResult {
        var result: DeleteResult = .failure(.unknown(""), "rig delete produced no result")
        var outcome: CLIOutcome?
        do {
            for await output in try await runner.session(
                for: .rigDelete(profile: profile.profileID)
            ).start() {
                switch output {
                case .event(let event):
                    switch event.kind {
                    case .rigDeleted:
                        result = .success
                    case .error:
                        let code = event.code ?? .unknown("")
                        result = .failure(code, event.message ?? "rig delete failed")
                    default:
                        continue
                    }
                case .completed(let completion):
                    outcome = completion.outcome
                case .log, .failure:
                    continue
                }
            }
        } catch {
            return .failure(.unknown(""), error.localizedDescription)
        }
        if case .success = result {
            if let outcome, outcome != .success {
                return .failure(.unknown(""), "rig delete did not complete successfully")
            }
            refresh()
        }
        return result
    }

    // MARK: - Testing

    /// Waits for the scan currently in flight, if any. Test-only: production
    /// code drives everything from `@Observable`'s change notifications.
    func waitForScan() async {
        await scanTask?.value
    }
}
