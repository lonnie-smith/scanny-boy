import Foundation
import Observation

/// The flat-field profile list's model: every profile in the library,
/// refreshed by one `flatfield list` invocation, and created and deleted
/// through the CLI (`flatfield create` / `flatfield delete`) — never by
/// touching Application Support itself. A gain map is app-private data with
/// a database row, not a user document, so unlike deleting a roll folder
/// there is no `NSWorkspace.recycle` path here.
@MainActor
@Observable
final class FlatFieldModel {
    enum CreateResult {
        case success(FlatFieldProfile)
        case failure(CLICode, String)
    }

    enum DeleteResult {
        case success
        case failure(CLICode, String)
    }

    /// One `flatfield_progress` event, decoded: a long calibration's
    /// phase and progress (protocol version 7). Phases: "detect", "fit",
    /// "chromatic", "reference".
    struct CreationProgress: Sendable, Hashable {
        let phase: String
        let completed: Int
        let total: Int
    }

    let runner: CLIRunner

    private(set) var profiles: [FlatFieldProfile] = []
    private(set) var isScanning = false
    /// Drives the New Profile sheet's determinate progress bar while a
    /// calibration runs; nil when nothing is in flight.
    private(set) var creationProgress: CreationProgress?

    @ObservationIgnored private var scanTask: Task<Void, Never>?

    init(runner: CLIRunner) {
        self.runner = runner
        refresh()
    }

    // MARK: - Scanning

    /// Re-reads the profile list from `flatfield list`. Called on init, after
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

    private static func fetchProfiles(runner: CLIRunner) async -> [FlatFieldProfile] {
        do {
            for await output in try await runner.session(for: .flatfieldList()).start() {
                guard case .event(let event) = output else { continue }
                if event.kind == .flatfieldList, let entries = event.flatFieldProfiles {
                    return entries.compactMap(FlatFieldProfile.init(fields:))
                }
            }
        } catch {
            // Launch failure: the list simply stays as it was, same posture
            // as `RollLibrary.scan()`'s.
        }
        return []
    }

    // MARK: - Create

    /// `flatfield create --reference --name [--calibration FILE ...]`.
    /// Building a flat-field-only profile decodes a RAW and takes seconds;
    /// with calibration frames the geometric fit runs for minutes, and the
    /// CLI's `flatfield_progress` events drive `creationProgress`.
    func create(
        reference: URL,
        name: String,
        calibrationFrames: [URL] = []
    ) async -> CreateResult {
        do {
            for await output in try await runner.session(
                for: .flatfieldCreate(
                    reference: reference, name: name, calibrationFrames: calibrationFrames
                )
            ).start() {
                guard case .event(let event) = output else { continue }
                switch event.kind {
                case .flatfieldCreated:
                    guard let fields = event.flatFieldProfile,
                        let profile = FlatFieldProfile(fields: fields)
                    else { continue }
                    refresh()
                    return .success(profile)
                case .flatfieldProgress:
                    if let phase = event.flatFieldPhase,
                        let completed = event.completed,
                        let total = event.total
                    {
                        creationProgress = CreationProgress(
                            phase: phase, completed: completed, total: total
                        )
                    }
                case .error:
                    let code = event.code ?? .unknown("")
                    return .failure(code, event.message ?? "flatfield create failed")
                default:
                    continue
                }
            }
        } catch {
            return .failure(.unknown(""), error.localizedDescription)
        }
        return .failure(.unknown(""), "flatfield create produced no result")
    }

    // MARK: - Delete

    /// `flatfield delete --profile`. The CLI refuses with
    /// `FLATFIELD_PROFILE_IN_USE` when any roll's invariants name the
    /// profile — the gain map is the only thing that could reproduce that
    /// roll — and the caller shows that refusal as an alert.
    func delete(_ profile: FlatFieldProfile) async -> DeleteResult {
        do {
            for await output in try await runner.session(
                for: .flatfieldDelete(profile: profile.profileID)
            ).start() {
                guard case .event(let event) = output else { continue }
                switch event.kind {
                case .flatfieldDeleted:
                    refresh()
                    return .success
                case .error:
                    let code = event.code ?? .unknown("")
                    return .failure(code, event.message ?? "flatfield delete failed")
                default:
                    continue
                }
            }
        } catch {
            return .failure(.unknown(""), error.localizedDescription)
        }
        return .failure(.unknown(""), "flatfield delete produced no result")
    }

    // MARK: - Testing

    /// Waits for the scan currently in flight, if any. Test-only: production
    /// code drives everything from `@Observable`'s change notifications.
    func waitForScan() async {
        await scanTask?.value
    }
}