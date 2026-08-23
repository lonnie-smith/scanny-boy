import Foundation
import Observation

/// Configuration state for one prospective run against a selected roll:
/// input folder and catalogue, the user's contiguous selection, and the
/// roll it targets.
///
/// Phase 3 section 3.10: "Add Scans is Phase 2's `ContentView` with the
/// output-folder section and the film-date field deleted, the
/// shots-per-negative stepper moved to the roll, and the
/// overwrite-confirmation replaced by the overlap sheet." There is no
/// output-folder picker any more — every run targets whichever roll is
/// selected in the sidebar (`rollURL`), and `perNegative` reads the roll's
/// own `shots_per_negative` rather than being a setting of its own.
///
/// Swift never sorts files itself and never re-implements the CLI's
/// selection, grouping, or roll-invariant rules (section 3.2's vocabulary,
/// and `CONTRACT.md`'s `probe`). Every rule this type enforces beyond plain
/// UI bookkeeping — contiguity, divisibility, setting consistency, roll
/// overlap — is read back from a `probe --roll` call; this type only
/// decides *when* to call `probe` and how to fold its result into
/// `runEnabled`.
@MainActor
@Observable
final class ConfigurationModel {
    /// One `warning` or `error` event's stable code and message.
    struct Issue: Sendable, Hashable {
        let code: CLICode
        let message: String
    }

    static let lastInputFolderKey = "com.lonniesmith.scanny-boy.lastInputFolder"

    let runner: CLIRunner
    private let defaults: UserDefaults

    // MARK: - Input folder and catalogue

    var inputFolder: URL? {
        didSet {
            guard inputFolder != oldValue else { return }
            selectedFiles = []
            catalogue = []
            catalogueWarnings = []
            catalogueError = nil
            if let inputFolder {
                Self.save(inputFolder, forKey: Self.lastInputFolderKey, in: defaults)
                startCatalogueProbe(inputFolder: inputFolder)
            }
        }
    }

    private(set) var catalogue: [String] = []
    private(set) var catalogueWarnings: [Issue] = []
    private(set) var catalogueError: Issue?

    // MARK: - Selection and grouping

    var selectedFiles: Set<String> = [] {
        didSet {
            guard selectedFiles != oldValue else { return }
            scheduleValidation()
        }
    }

    private(set) var groups: [[String]] = []
    private(set) var selectionWarnings: [Issue] = []
    private(set) var selectionError: Issue?

    // MARK: - The roll this configuration targets

    /// Set by `ContentView` from the sidebar selection (section 3.10). Add
    /// Scans has no folder picker of its own — every run targets whichever
    /// roll is already selected.
    var rollURL: URL? {
        didSet {
            guard rollURL != oldValue else { return }
            roll = nil
            startRollFetch(rollURL: rollURL)
            scheduleValidation()
        }
    }

    /// `roll info` for `rollURL` (section 3.1: Swift never parses
    /// `scanny-boy-roll.json` itself), so `perNegative` and the roll's
    /// identity are only known once this finishes.
    private(set) var roll: RollManifest?
    @ObservationIgnored private var rollTask: Task<Void, Never>?

    /// `shots_per_negative` is the roll's own, locked once any run reaches
    /// `complete`/`partial` with a completed negative (section 3.4) and
    /// editable only from the Edit tab (Chunk P3-12) — Add Scans just reads
    /// it back. Falls back to the CLI's own default while the roll has not
    /// loaded yet, matching what `probe` itself defaults to.
    var perNegative: Int { roll?.shotsPerNegative ?? 3 }

    /// A `probe --roll` failure specific to the roll itself — missing,
    /// unreadable, unsupported, or invariant-mismatched — as opposed to one
    /// the selection alone caused (section 3.4's roll-invariant checks).
    private(set) var rollError: Issue?

    // MARK: - Status

    private(set) var isProbing = false

    @ObservationIgnored private var catalogueTask: Task<Void, Never>?
    @ObservationIgnored private var validationTask: Task<Void, Never>?

    init(runner: CLIRunner, defaults: UserDefaults = .standard) {
        self.runner = runner
        self.defaults = defaults
        inputFolder = Self.loadURL(forKey: Self.lastInputFolderKey, in: defaults)
        if let inputFolder {
            startCatalogueProbe(inputFolder: inputFolder)
        }
    }

    // MARK: - Derived state

    /// Section 3.4/3.5's roll-invariant and roll-folder codes — the ones
    /// only the roll itself can cause, as opposed to the selection.
    private static let rollRelatedCodes: Set<CLICode> = [
        .rollNotFound,
        .badManifest,
        .rollManifestUnsupported,
        .rollInvariantMismatch,
        .outputNotWritable,
    ]

    /// Every gate section 3.10 names: a contiguous, divisible selection with
    /// consistent settings, targeting a roll that validated. The Run button
    /// is offered from here.
    var runEnabled: Bool {
        !selectedFiles.isEmpty
            && selectionError == nil
            && rollError == nil
            && rollURL != nil
    }

    /// Where one catalogue entry lives on disk, for display only.
    ///
    /// `name` must be a catalogue entry: the CLI found it, the CLI named it,
    /// and this only rejoins it to the folder the CLI was pointed at.
    /// Nothing here discovers, filters, or orders files (section 3.2).
    func fileURL(for name: String) -> URL? {
        inputFolder?.appending(path: name, directoryHint: .notDirectory)
    }

    /// The selection in canonical order. Filters the catalogue rather than
    /// iterating `selectedFiles`, whose `Set` has no meaningful order at all
    /// (section 3.3: Swift never sorts files itself).
    var selectedFilesInCanonicalOrder: [String] {
        catalogue.filter { selectedFiles.contains($0) }
    }

    /// The `run` invocation this configuration describes, or `nil` when it
    /// does not yet describe a runnable one. `skipSources` is always empty:
    /// every group in the selection runs and, per section 3.4, overwrites the
    /// file of any existing negative with the exact same source set, in place.
    func runCommand() -> CLICommand? {
        guard runEnabled, let inputFolder, let rollURL else { return nil }
        return .run(
            input: inputFolder,
            files: selectedFilesInCanonicalOrder,
            roll: rollURL,
            perNegative: perNegative,
            skipSources: []
        )
    }

    // MARK: - Probing

    /// Re-runs selection and roll validation. Chunk 10 calls this once a
    /// conversion has ended: the roll now holds negatives it did not
    /// before, so the selection may need re-validating.
    func refreshValidation() {
        scheduleValidation()
    }

    private func startCatalogueProbe(inputFolder: URL) {
        catalogueTask?.cancel()
        isProbing = true
        catalogueTask = Task { [weak self, runner] in
            let result = await Self.runProbe(runner: runner, command: .probe(input: inputFolder))
            guard let self, !Task.isCancelled else { return }
            self.catalogue = result.catalogue ?? []
            self.catalogueWarnings = result.warnings
            self.catalogueError = result.error
            self.isProbing = false
        }
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

    private func scheduleValidation() {
        validationTask?.cancel()
        guard let inputFolder, !selectedFiles.isEmpty else {
            groups = []
            selectionWarnings = []
            selectionError = nil
            rollError = nil
            return
        }

        let rollURL = rollURL
        let perNegative = perNegative
        let files = selectedFilesInCanonicalOrder

        isProbing = true
        validationTask = Task { [weak self, runner] in
            let result = await Self.runProbe(
                runner: runner,
                command: .probe(
                    input: inputFolder, files: files, roll: rollURL, perNegative: perNegative
                )
            )
            guard let self, !Task.isCancelled else { return }
            self.apply(result)
            self.isProbing = false
        }
    }

    /// `probe`'s single-outcome design (`CONTRACT.md`) means one call either
    /// produces a `probe_result` or an `error`, never a partial mix — so on
    /// failure every derived field here is cleared, not just the ones the
    /// failing step would have touched.
    private func apply(_ result: ProbeCallResult) {
        selectionWarnings = result.warnings

        if let error = result.error {
            groups = []
            if Self.rollRelatedCodes.contains(error.code) {
                selectionError = nil
                rollError = error
            } else {
                selectionError = error
                rollError = nil
            }
            return
        }

        selectionError = nil
        rollError = nil
        groups = result.groups ?? []
    }

    // MARK: - Folder memory

    private static func loadURL(forKey key: String, in defaults: UserDefaults) -> URL? {
        defaults.url(forKey: key)
    }

    private static func save(_ url: URL, forKey key: String, in defaults: UserDefaults) {
        defaults.set(url, forKey: key)
    }

    // MARK: - Running one probe call and reading its result

    struct ProbeCallResult: Sendable {
        var catalogue: [String]?
        var groups: [[String]]?
        var warnings: [Issue] = []
        var error: Issue?
    }

    private static func runProbe(runner: CLIRunner, command: CLICommand) async -> ProbeCallResult {
        var result = ProbeCallResult()
        do {
            let session = runner.session(for: command)
            for await output in try await session.start() {
                switch output {
                case .event(let event):
                    switch event.kind {
                    case .probeResult:
                        result.catalogue = event.catalogue
                        result.groups = event.groups
                    case .warning:
                        if let code = event.code, let message = event.message {
                            result.warnings.append(Issue(code: code, message: message))
                        }
                    case .error:
                        if let code = event.code, let message = event.message {
                            result.error = Issue(code: code, message: message)
                        }
                    default:
                        break
                    }
                case .log, .failure, .completed:
                    break
                }
            }
        } catch {
            // Launch failure: nothing more to report than "no result"; the
            // relevant state (catalogue/groups/etc.) simply stays empty.
        }
        return result
    }

    // MARK: - Testing

    /// Waits for any probe currently in flight to finish applying its
    /// result to this model's state. Test-only: production code drives
    /// everything from `@Observable`'s change notifications instead.
    func waitForPendingProbes() async {
        await catalogueTask?.value
        await rollTask?.value
        await validationTask?.value
    }
}
