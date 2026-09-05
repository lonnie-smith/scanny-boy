import Foundation
import Observation

/// Configuration state for one prospective run against a selected roll:
/// input folder and catalogue, the user's contiguous selection, the batch's
/// grid (`across` x `down`), and the roll it targets.
///
/// Phase 3 section 3.10: "Add Scans is Phase 2's `ContentView` with the
/// output-folder section and the film-date field deleted, the
/// shots-per-negative stepper moved to the roll, and the
/// overwrite-confirmation replaced by the overlap sheet." There is no
/// output-folder picker any more — every run targets whichever roll is
/// selected in the sidebar (`rollURL`). The roll no longer owns
/// `shots_per_negative` at all: the grouping is each stitch batch's own
/// choice (`across` x `down`), required before a run can start, so one roll
/// can hold negatives stitched from different scan counts. A strip is the
/// `down == 1` case (docs/GRID_STITCH_PLAN.md section 2.5).
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
    static let lastFlatFieldProfileKey = "com.lonniesmith.scanny-boy.lastFlatFieldProfile"

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
            } else {
                catalogueTask?.cancel()
                isCataloguing = false
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
            scheduleValidation()
        }
    }

    // MARK: - The batch's grouping

    /// The grid the batch was scanned in (protocol 10's `--grid AxD`):
    /// `across` runs left-to-right in capture space, `down` top-to-bottom.
    /// `across` is `nil` until the user picks one on the Add Scans stage —
    /// that is the "not chosen yet" state, and it gates `runEnabled`.
    /// Changing either stored dimension re-validates the selection, since
    /// grouping and divisibility depend on the product.
    var across: Int? {
        didSet {
            guard across != oldValue else { return }
            revalidateSelection()
        }
    }

    /// Defaults to 1 and is not optional: a plain strip run needs one
    /// selection (Across), not two. `down == 1` keeps the CLI command a
    /// `--per-negative` run, byte-identical to a pre-grid command line.
    var down: Int = 1 {
        didSet {
            guard down != oldValue else { return }
            // The product must stay within MAX_PER_NEGATIVE (12) and
            // min(across, down) <= 2; with down capped at 2 that reduces
            // to clamping Across to 12 / down.
            if let across, across * down > Self.maxPerNegative {
                self.across = Self.maxPerNegative / down
            }
            revalidateSelection()
        }
    }

    /// Scans stitched into each negative — the product the grouping
    /// preview uses. Computed from the stored grid dimensions; `nil` until
    /// Across is chosen.
    var perNegative: Int? {
        across.map { $0 * down }
    }

    static let maxPerNegative = 12

    private func revalidateSelection() {
        scheduleValidation()
    }

    // MARK: - Flat field

    /// The flat-field profile this run applies. Required before Stitch is
    /// offered. A roll does not lock to one profile — each run into it may
    /// choose a different one (or none) — so this is purely a per-run
    /// choice, defaulted from and persisted as the user's last one, the
    /// same as the input folder.
    var flatFieldProfileID: String? {
        didSet {
            guard flatFieldProfileID != oldValue else { return }
            if let flatFieldProfileID {
                defaults.set(flatFieldProfileID, forKey: Self.lastFlatFieldProfileKey)
            } else {
                defaults.removeObject(forKey: Self.lastFlatFieldProfileKey)
            }
            scheduleValidation()
        }
    }

    /// A `probe --roll` failure specific to the roll itself — missing,
    /// unreadable, unsupported, or invariant-mismatched — as opposed to one
    /// the selection alone caused (section 3.4's roll-invariant checks).
    private(set) var rollError: Issue?

    // MARK: - Status

    /// The catalogue probe and the selection/roll validation probe are
    /// independent round trips; each clears only its own flag when it
    /// finishes, so a UI gate reading a single shared flag could see "done"
    /// while the other probe is still in flight.
    private(set) var isCataloguing = false
    private(set) var isValidating = false
    var isProbing: Bool { isCataloguing || isValidating }

    @ObservationIgnored private var catalogueTask: Task<Void, Never>?
    @ObservationIgnored private var validationTask: Task<Void, Never>?

    init(runner: CLIRunner, defaults: UserDefaults = .standard) {
        self.runner = runner
        self.defaults = defaults
        inputFolder = Self.loadURL(forKey: Self.lastInputFolderKey, in: defaults)
        flatFieldProfileID = defaults.string(forKey: Self.lastFlatFieldProfileKey)
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

    /// Every gate section 3.10 names — a chosen scans-per-negative, a
    /// contiguous, divisible selection with consistent settings, targeting a
    /// roll that validated — plus the flat-field profile the app requires
    /// (docs/FLATFIELD_PLAN.md section 2.5). The Stitch button is offered
    /// from here.
    var runEnabled: Bool {
        perNegative != nil
            && !selectedFiles.isEmpty
            && selectionError == nil
            && rollError == nil
            && rollURL != nil
            && flatFieldProfileID != nil
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
    /// every group in the selection runs and adopts whatever it overlaps in
    /// the roll (the replacement rule). The flat-field profile rides along
    /// as `--flatfield`, freely chosen for this run — the roll does not
    /// lock to one.
    func runCommand() -> CLICommand? {
        guard runEnabled, let inputFolder, let rollURL, let across,
            let flatFieldProfileID
        else {
            return nil
        }
        return .run(
            input: inputFolder,
            files: selectedFilesInCanonicalOrder,
            roll: rollURL,
            across: across,
            down: down,
            skipSources: [],
            flatfield: flatFieldProfileID
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
        isCataloguing = true
        catalogueTask = Task { [weak self, runner] in
            let result = await Self.runProbe(runner: runner, command: .probe(input: inputFolder))
            guard let self, !Task.isCancelled else { return }
            self.catalogue = result.catalogue ?? []
            self.catalogueWarnings = result.warnings
            self.catalogueError = result.error
            self.isCataloguing = false
        }
    }

    /// Debounces `probe --roll` calls: a drag-select across many catalogue
    /// rows fires this once per row, and each one tore down and rebuilt the
    /// configuration form (see the fix note on `isProbing`'s consumers).
    private static let validationDebounce = Duration.milliseconds(200)

    private func scheduleValidation() {
        validationTask?.cancel()
        guard let inputFolder, !selectedFiles.isEmpty else {
            groups = []
            selectionWarnings = []
            selectionError = nil
            rollError = nil
            isValidating = false
            return
        }

        // Grouping and divisibility are per-batch: without a chosen
        // grid width there is nothing to validate against yet.
        guard let across else {
            groups = []
            selectionWarnings = []
            selectionError = nil
            rollError = nil
            isValidating = false
            return
        }

        let rollURL = rollURL
        let files = selectedFilesInCanonicalOrder
        let flatFieldProfileID = flatFieldProfileID
        let down = down

        isValidating = true
        validationTask = Task { [weak self, runner] in
            try? await Task.sleep(for: Self.validationDebounce)
            guard !Task.isCancelled else { return }
            let result = await Self.runProbe(
                runner: runner,
                command: .probe(
                    input: inputFolder,
                    files: files,
                    roll: rollURL,
                    across: across,
                    down: down,
                    flatfield: flatFieldProfileID
                )
            )
            guard let self, !Task.isCancelled else { return }
            self.apply(result)
            self.isValidating = false
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
        await validationTask?.value
    }
}
