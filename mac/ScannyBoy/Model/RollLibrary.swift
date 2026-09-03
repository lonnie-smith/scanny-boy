import AppKit
import Foundation
import Observation

/// The library sidebar's model: every roll under the library base, refreshed
/// by one `roll list` invocation. Section 3.1: "The filesystem is the source
/// of truth... That scan is the CLI's, not Swift's" — this type does no
/// directory enumeration and no manifest parsing of its own. `scan()` is the
/// only way it learns what is in the library.
///
/// Renaming (section 5.5), creating a roll, and unregistering a deleted
/// roll all go through the CLI — `roll rename`, `roll init`, and
/// `roll delete` — so the only thing this type ever touches on disk
/// directly is moving a roll's folder to the Trash, via `NSWorkspace.recycle`
/// (section 3.10), which needs no server-side cooperation at all.
@MainActor
@Observable
final class RollLibrary {
    static let libraryBaseKey = "com.lonniesmith.scanny-boy.libraryBaseFolder"

    enum RenameError: Error, LocalizedError {
        /// Section 3.2: refused while any run is active. The CLI has no
        /// notion of this — it is stateless between invocations — so the
        /// app enforces it before ever building the command.
        case runInProgress
        case failed(String)

        var errorDescription: String? {
            switch self {
            case .runInProgress:
                "Renaming is refused while a run is in progress."
            case .failed(let message):
                message
            }
        }
    }

    let runner: CLIRunner
    private let defaults: UserDefaults
    private let fileManager: FileManager

    private(set) var rolls: [Roll] = []
    private(set) var isScanning = false
    private(set) var scanError: String?

    var libraryBase: URL {
        didSet {
            guard libraryBase != oldValue else { return }
            Self.save(libraryBase, in: defaults)
            scan()
        }
    }

    @ObservationIgnored private var scanTask: Task<Void, Never>?

    /// `libraryBase` defaults to `~/Pictures/Scanny Boy` (section 3.1) when
    /// not given; tests always inject one explicitly, and must never fall
    /// through to `.picturesDirectory`.
    init(
        runner: CLIRunner,
        libraryBase: URL? = nil,
        defaults: UserDefaults = .standard,
        fileManager: FileManager = .default
    ) {
        self.runner = runner
        self.defaults = defaults
        self.fileManager = fileManager
        self.libraryBase =
            libraryBase
            ?? Self.loadLibraryBase(defaults: defaults, fileManager: fileManager)
    }

    private static func loadLibraryBase(defaults: UserDefaults, fileManager: FileManager) -> URL {
        if let saved = defaults.url(forKey: libraryBaseKey) {
            return saved
        }
        let pictures =
            fileManager.urls(for: .picturesDirectory, in: .userDomainMask).first
            ?? fileManager.homeDirectoryForCurrentUser.appending(
                path: "Pictures", directoryHint: .isDirectory
            )
        return pictures.appending(path: "Scanny Boy", directoryHint: .isDirectory)
    }

    private static func save(_ url: URL, in defaults: UserDefaults) {
        defaults.set(url, forKey: libraryBaseKey)
    }

    // MARK: - Scanning

    /// Re-scans the library from `roll list`. Section 3.1: created on first
    /// launch if it does not exist yet — `roll list` itself never creates
    /// anything, it just reports an empty library for a missing folder.
    func scan() {
        scanTask?.cancel()
        isScanning = true
        scanError = nil
        let libraryBase = libraryBase
        let fileManager = fileManager
        scanTask = Task { [weak self, runner] in
            try? fileManager.createDirectory(
                at: libraryBase, withIntermediateDirectories: true
            )
            let result = await Self.fetchRollList(runner: runner, libraryBase: libraryBase)
            guard let self, !Task.isCancelled else { return }
            switch result {
            case .success(let rolls):
                self.rolls = rolls
                self.scanError = nil
            case .failure(let message):
                self.scanError = message
            }
            self.isScanning = false
        }
    }

    private enum ScanResult {
        case success([Roll])
        case failure(String)
    }

    private static func fetchRollList(runner: CLIRunner, libraryBase: URL) async -> ScanResult {
        do {
            for await output in try await runner.session(for: .rollList(library: libraryBase))
                .start()
            {
                switch output {
                case .event(let event):
                    if event.kind == .rollList, let entries = event.rolls {
                        return .success(entries.compactMap(Roll.init(fields:)))
                    }
                    if event.kind == .error {
                        return .failure(event.message ?? "roll list failed")
                    }
                default:
                    continue
                }
            }
        } catch let failure as CLISessionFailure {
            return .failure(Self.describe(failure))
        } catch {
            return .failure(error.localizedDescription)
        }
        return .failure("roll list produced no result")
    }

    private static func describe(_ failure: CLISessionFailure) -> String {
        switch failure {
        case .launch(let message): message
        case .read(_, let message): message
        case .decode(_, let reason): reason
        }
    }

    // MARK: - Create

    enum CreateResult {
        case success(Roll)
        case failure(CLICode, String)
    }

    /// `roll init --library --name`. Rescans on success, so the sidebar
    /// picks up the new roll immediately.
    func createRoll(name: String) async -> CreateResult {
        let session = runner.session(
            for: .rollInit(library: libraryBase, name: name)
        )
        do {
            for await output in try await session.start() {
                guard case .event(let event) = output else { continue }
                switch event.kind {
                case .rollCreated:
                    guard let path = event.rollPath, let rollID = event.rollID,
                        let rollName = event.rollName
                    else { continue }
                    let roll = Roll(
                        path: URL(filePath: path),
                        status: .ok,
                        reason: nil,
                        rollID: rollID,
                        rollName: rollName,
                        negativeCount: 0
                    )
                    scan()
                    return .success(roll)
                case .error:
                    let code = event.code ?? .unknown("")
                    return .failure(code, event.message ?? "roll init failed")
                default:
                    continue
                }
            }
        } catch {
            return .failure(.unknown(""), error.localizedDescription)
        }
        return .failure(.unknown(""), "roll init produced no result")
    }

    // MARK: - Rename

    /// `roll rename --roll --name` (section 5.5). `runIsActive` is the
    /// app's own one-run-at-a-time state (section 3.10); the CLI has no
    /// notion of it, so this is checked here, before the command is ever
    /// built.
    func renameRoll(_ roll: Roll, to newName: String, runIsActive: Bool) async throws -> Roll {
        guard !runIsActive else { throw RenameError.runInProgress }

        let session = runner.session(for: .rollRename(roll: roll.path, name: newName))
        do {
            for await output in try await session.start() {
                guard case .event(let event) = output else { continue }
                switch event.kind {
                case .rollRenamed:
                    guard let path = event.rollPath, let rollID = event.rollID,
                        let rollName = event.rollName
                    else { continue }
                    let renamed = Roll(
                        path: URL(filePath: path),
                        status: .ok,
                        reason: nil,
                        rollID: rollID,
                        rollName: rollName,
                        negativeCount: roll.negativeCount
                    )
                    scan()
                    return renamed
                case .error:
                    throw RenameError.failed(event.message ?? "roll rename failed")
                default:
                    continue
                }
            }
        } catch let error as RenameError {
            throw error
        } catch {
            throw RenameError.failed(error.localizedDescription)
        }
        throw RenameError.failed("roll rename produced no result")
    }

    // MARK: - Delete

    enum DeleteError: Error, LocalizedError {
        case failed(String)

        var errorDescription: String? {
            switch self {
            case .failed(let message):
                message
            }
        }
    }

    /// Moves the roll's folder to the Trash and unregisters it, so the next
    /// `roll list` drops it. Two steps, in this order: the folder goes first
    /// via `NSWorkspace.recycle` (section 3.10: pure Swift, no server-side
    /// cooperation; a failed move leaves both the folder and the
    /// registration untouched), then `roll delete` removes the database
    /// registration — with the folder already gone, a crash between the two
    /// steps leaves an orphan registration that reads as `unreadable`, never
    /// a lost folder. Rescans on success so the sidebar drops the roll
    /// immediately.
    func deleteRoll(_ roll: Roll) async throws {
        if fileManager.fileExists(atPath: roll.path.path) {
            try await withCheckedThrowingContinuation {
                (continuation: CheckedContinuation<Void, Error>) in
                NSWorkspace.shared.recycle([roll.path]) { _, error in
                    if let error {
                        continuation.resume(throwing: error)
                    } else {
                        continuation.resume()
                    }
                }
            }
        }

        let session = runner.session(for: .rollDelete(roll: roll.path))
        do {
            for await output in try await session.start() {
                guard case .event(let event) = output else { continue }
                switch event.kind {
                case .rollDeleted:
                    scan()
                    return
                case .error:
                    throw DeleteError.failed(event.message ?? "roll delete failed")
                default:
                    continue
                }
            }
        } catch let error as DeleteError {
            throw error
        } catch {
            throw DeleteError.failed(error.localizedDescription)
        }
        throw DeleteError.failed("roll delete produced no result")
    }

    // MARK: - Testing

    /// Waits for the scan currently in flight, if any. Test-only: production
    /// code drives everything from `@Observable`'s change notifications.
    func waitForScan() async {
        await scanTask?.value
    }
}
