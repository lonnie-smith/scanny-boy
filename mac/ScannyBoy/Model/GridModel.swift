import Foundation
import Observation

/// The grid configuration preset list: refreshed by one `grid list`
/// invocation, and created and deleted through the CLI — never by touching
/// Application Support itself.
@MainActor
@Observable
final class GridModel {
    enum CreateResult {
        case success(GridProfile)
        case failure(CLICode, String)
    }

    enum DeleteResult {
        case success
        case failure(CLICode, String)
    }

    let runner: CLIRunner

    private(set) var profiles: [GridProfile] = []
    private(set) var isScanning = false

    @ObservationIgnored private var scanTask: Task<Void, Never>?

    init(runner: CLIRunner) {
        self.runner = runner
        refresh()
    }

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

    private static func fetchProfiles(runner: CLIRunner) async -> [GridProfile] {
        do {
            for await output in try await runner.session(for: .gridList()).start() {
                guard case .event(let event) = output else { continue }
                if event.kind == .gridList, let entries = event.gridProfiles {
                    return entries.compactMap(GridProfile.init(fields:))
                }
            }
        } catch {
            // Launch failure: the list simply stays as it was.
        }
        return []
    }

    func create(name: String, across: Int, down: Int) async -> CreateResult {
        var result: CreateResult = .failure(.unknown(""), "grid create produced no result")
        var outcome: CLIOutcome?
        do {
            for await output in try await runner.session(
                for: .gridCreate(name: name, across: across, down: down)
            ).start() {
                switch output {
                case .event(let event):
                    switch event.kind {
                    case .gridCreated:
                        guard let fields = event.gridProfile,
                            let profile = GridProfile(fields: fields)
                        else { continue }
                        result = .success(profile)
                    case .error:
                        let code = event.code ?? .unknown("")
                        result = .failure(code, event.message ?? "grid create failed")
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
                return .failure(.unknown(""), "grid create did not complete successfully")
            }
            refresh()
        }
        return result
    }

    func delete(_ profile: GridProfile) async -> DeleteResult {
        var result: DeleteResult = .failure(.unknown(""), "grid delete produced no result")
        var outcome: CLIOutcome?
        do {
            for await output in try await runner.session(
                for: .gridDelete(profile: profile.profileID)
            ).start() {
                switch output {
                case .event(let event):
                    switch event.kind {
                    case .gridDeleted:
                        result = .success
                    case .error:
                        let code = event.code ?? .unknown("")
                        result = .failure(code, event.message ?? "grid delete failed")
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
                return .failure(.unknown(""), "grid delete did not complete successfully")
            }
            refresh()
        }
        return result
    }

    func waitForScan() async {
        await scanTask?.value
    }
}
