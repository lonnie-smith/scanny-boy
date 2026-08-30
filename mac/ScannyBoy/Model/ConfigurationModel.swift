import Foundation
import Observation

/// Configuration state for one prospective conversion run: input folder and
/// catalogue, the user's contiguous selection, shots per negative, film
/// date, and output folder — everything `docs/IMPLEMENTATION_PLAN.md`
/// Chunk 9 asks the app to validate before Run can be enabled.
///
/// Swift never sorts files itself and never re-implements the CLI's
/// selection, grouping, output-folder, or setting-consistency rules
/// (section 3.2's vocabulary, and `CONTRACT.md`'s `probe`). Every rule this
/// type enforces beyond plain UI bookkeeping — contiguity, divisibility,
/// setting consistency, output conflicts, disk space — is read back from a
/// `probe` call; this type only decides *when* to call `probe` and how to
/// fold its result into `runEnabled`. `convert` itself, and the actual Run
/// action, belong to Chunk 10.
@MainActor
@Observable
final class ConfigurationModel {
    /// One `warning` or `error` event's stable code and message.
    struct Issue: Sendable, Hashable {
        let code: CLICode
        let message: String
    }

    static let lastInputFolderKey = "com.lonniesmith.scanny-boy.lastInputFolder"
    static let lastOutputFolderKey = "com.lonniesmith.scanny-boy.lastOutputFolder"

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

    var perNegative: Int = 3 {
        didSet {
            guard perNegative != oldValue else { return }
            scheduleValidation()
        }
    }

    /// Required, and starts blank (section 6, Chunk 9).
    var filmDate: String = ""

    /// Section 3.5: kept, with an `INTERMEDIATES_KEPT` warning, when true —
    /// even on an otherwise complete success. Off by default, matching the
    /// CLI's own default.
    var keepIntermediates = false

    private(set) var groups: [[String]] = []
    private(set) var selectionWarnings: [Issue] = []
    private(set) var selectionError: Issue?

    // MARK: - Output folder, disk estimate, and overwrite preview

    var outputFolder: URL? {
        didSet {
            guard outputFolder != oldValue else { return }
            overwriteConfirmed = false
            existingRoll = outputFolder.flatMap { try? RollManifest.read(inOutputFolder: $0) }
            if let outputFolder {
                Self.save(outputFolder, forKey: Self.lastOutputFolderKey, in: defaults)
            }
            scheduleValidation()
        }
    }

    private(set) var outputConflicts: [String] = []
    private(set) var estimatedRequiredBytes: Int?
    private(set) var availableBytes: Int?
    private(set) var outputError: Issue?

    /// A completed roll from a prior `run`/`stitch`, when the chosen output
    /// folder holds one.
    ///
    /// `probe --out` validates only against `scanny-boy-manifest.json`
    /// (Phase 1's convert manifest); it has no notion of
    /// `scanny-boy-roll.json` at all, so it reports a folder holding only a
    /// roll manifest as `OUTPUT_NOT_EMPTY` — unrelated content, not a rerun.
    /// This is read directly, client-side, so `apply(_:outputFolderWasGiven:)`
    /// can recognise that specific case rather than surface a raw,
    /// misleading error. Real conflict detection for a rerun into this
    /// folder still happens for real, server-side, in `run_stitch`'s own
    /// `plan_rerun(rules: ROLL_RULES)` when the run is actually started.
    private(set) var existingRoll: RollManifest?

    /// Set once the user has agreed to replace `outputConflicts`. Reset
    /// whenever the output folder changes, so a stale confirmation from a
    /// previous folder can never silently authorise a different one.
    var overwriteConfirmed = false

    // MARK: - Status

    private(set) var isProbing = false

    @ObservationIgnored private var catalogueTask: Task<Void, Never>?
    @ObservationIgnored private var validationTask: Task<Void, Never>?

    init(runner: CLIRunner, defaults: UserDefaults = .standard) {
        self.runner = runner
        self.defaults = defaults
        inputFolder = Self.loadURL(forKey: Self.lastInputFolderKey, in: defaults)
        outputFolder = Self.loadURL(forKey: Self.lastOutputFolderKey, in: defaults)
        if let inputFolder {
            startCatalogueProbe(inputFolder: inputFolder)
        }
    }

    // MARK: - Derived state

    /// Section 4.1's error-code table splits into two families: ones a
    /// selection can cause on its own, and ones only an output folder can
    /// cause. One `probe --files --out` call can only fail with one of them
    /// at a time — the CLI validates the selection before it ever looks at
    /// `--out` — so the code alone tells the two apart.
    private static let outputRelatedCodes: Set<CLICode> = [
        .outputSameAsInput,
        .outputNotWritable,
        .outputNotEmpty,
        .insufficientDisk,
        .badManifest,
        .manifestMismatch,
        .iccProfileInvalid,
    ]

    private static func isValidFilmDate(_ text: String) -> Bool {
        guard text.count == 10 else { return false }
        let formatter = DateFormatter()
        formatter.calendar = Calendar(identifier: .iso8601)
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "yyyy-MM-dd"
        formatter.isLenient = false
        guard let date = formatter.date(from: text) else { return false }
        // `isLenient = false` still accepts some out-of-range values by
        // rolling them into the next unit; round-tripping catches those.
        return formatter.string(from: date) == text
    }

    var isFilmDateValid: Bool { Self.isValidFilmDate(filmDate) }

    /// Every gate Chunk 9 names except the overwrite confirmation: a
    /// contiguous, divisible selection with consistent settings; a valid
    /// output folder; and a well-formed film date.
    ///
    /// Separate from `runEnabled` because Chunk 10 asks for the confirmation
    /// at the moment Run is pressed rather than as a checkbox the user has to
    /// find first. The Run button is offered from here; `runEnabled` is still
    /// what decides whether pressing it starts a conversion or raises the
    /// confirmation dialog.
    var isReadyPendingOverwriteConfirmation: Bool {
        !selectedFiles.isEmpty
            && selectionError == nil
            && outputError == nil
            && isFilmDateValid
            && outputFolder != nil
    }

    /// Every gate Chunk 9 names: a contiguous, divisible selection with
    /// consistent settings; a valid, non-conflicting (or confirmed) output
    /// folder; and a well-formed film date.
    var runEnabled: Bool {
        isReadyPendingOverwriteConfirmation
            && (outputConflicts.isEmpty || overwriteConfirmed)
    }

    /// True when the only thing left is the user agreeing to replace
    /// `outputConflicts` (section 3.6).
    var needsOverwriteConfirmation: Bool {
        isReadyPendingOverwriteConfirmation && !outputConflicts.isEmpty && !overwriteConfirmed
    }

    func confirmOverwrite() {
        overwriteConfirmed = true
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
    /// does not yet describe a runnable one.
    ///
    /// Chunk P2-9: the app's Run button drives `run` — convert and stitch in
    /// one process — not `convert` alone. `--work` is left unset, so the CLI
    /// uses a fresh temporary directory (section 3.6); choosing a specific
    /// work directory belongs to Chunk P2-10's re-stitch flow. `--overwrite`
    /// is passed only after the user has confirmed the replacements; the CLI
    /// rejects conflicts by default (section 3.6).
    var runCommand: CLICommand? {
        guard runEnabled, let inputFolder, let outputFolder else { return nil }
        return .run(
            input: inputFolder,
            files: selectedFilesInCanonicalOrder,
            out: outputFolder,
            filmDate: filmDate,
            perNegative: perNegative,
            overwrite: !outputConflicts.isEmpty && overwriteConfirmed,
            keepIntermediates: keepIntermediates
        )
    }

    // MARK: - Probing

    /// Re-runs selection and output validation. Chunk 10 calls this once a
    /// conversion has ended: the output folder now holds files it did not
    /// before, so the conflict preview, the disk estimate, and any previous
    /// overwrite agreement are all out of date.
    func refreshValidation() {
        overwriteConfirmed = false
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

    private func scheduleValidation() {
        validationTask?.cancel()
        guard let inputFolder, !selectedFiles.isEmpty else {
            groups = []
            selectionWarnings = []
            selectionError = nil
            outputConflicts = []
            estimatedRequiredBytes = nil
            availableBytes = nil
            outputError = nil
            return
        }

        let outputFolder = outputFolder
        let perNegative = perNegative
        let files = selectedFilesInCanonicalOrder

        isProbing = true
        validationTask = Task { [weak self, runner] in
            let result = await Self.runProbe(
                runner: runner,
                command: .probe(
                    input: inputFolder, files: files, out: outputFolder, perNegative: perNegative
                )
            )
            guard let self, !Task.isCancelled else { return }
            self.apply(result, outputFolderWasGiven: outputFolder != nil)
            self.isProbing = false
        }
    }

    /// `probe`'s single-outcome design (`CONTRACT.md`) means one call either
    /// produces a `probe_result` or a `error`, never a partial mix — so on
    /// failure every derived field here is cleared, not just the ones the
    /// failing step would have touched.
    private func apply(_ result: ProbeCallResult, outputFolderWasGiven: Bool) {
        selectionWarnings = result.warnings

        if let error = result.error {
            groups = []
            outputConflicts = []
            estimatedRequiredBytes = nil
            availableBytes = nil
            // A folder holding only a roll manifest is a legitimate rerun
            // target, not unrelated content — `probe` has no way to know
            // that, so this is the one output error `existingRoll`
            // overrides. Every other output error still blocks Run.
            if error.code == .outputNotEmpty, existingRoll != nil {
                selectionError = nil
                outputError = nil
            } else if Self.outputRelatedCodes.contains(error.code) {
                selectionError = nil
                outputError = error
            } else {
                selectionError = error
                outputError = nil
            }
            return
        }

        selectionError = nil
        outputError = nil
        groups = result.groups ?? []
        if outputFolderWasGiven {
            outputConflicts = result.outputConflicts ?? []
            estimatedRequiredBytes = result.estimatedRequiredBytes
            availableBytes = result.availableBytes
        } else {
            outputConflicts = []
            estimatedRequiredBytes = nil
            availableBytes = nil
        }
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
        var outputConflicts: [String]?
        var estimatedRequiredBytes: Int?
        var availableBytes: Int?
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
                        result.outputConflicts = event.outputConflicts
                        result.estimatedRequiredBytes = event.estimatedRequiredBytes
                        result.availableBytes = event.availableBytes
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
        await validationTask?.value
    }
}
