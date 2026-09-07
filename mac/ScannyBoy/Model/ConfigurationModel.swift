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
/// decides *when* to call `probe` and how to fold its result into the UI.
/// Selection, grid, roll, and flat-field profile are inert until Convert:
/// `validateSelection()` runs the probe once, immediately before a run.
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
            clearValidationState()
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
            clearValidationState()
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
            clearValidationState()
        }
    }

    // MARK: - The batch's grouping

    /// The grid the batch was scanned in (protocol 10's `--grid AxD`):
    /// `across` runs left-to-right in capture space, `down` top-to-bottom.
    /// `across` is `nil` until the user picks one on the Add Scans stage —
    /// that is the "not chosen yet" state, and it gates `runEnabled`.
    var across: Int? {
        didSet {
            guard across != oldValue else { return }
            clearValidationState()
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
            clearValidationState()
        }
    }

    /// Scans stitched into each negative — the product the grouping
    /// preview uses. Computed from the stored grid dimensions; `nil` until
    /// Across is chosen.
    var perNegative: Int? {
        across.map { $0 * down }
    }

    static let maxPerNegative = 12

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
            clearValidationState()
        }
    }

    /// A `probe --roll` failure specific to the roll itself — missing,
    /// unreadable, unsupported, or invariant-mismatched — as opposed to one
    /// the selection alone caused (section 3.4's roll-invariant checks).
    private(set) var rollError: Issue?

    // MARK: - Status

    /// The catalogue probe and the Convert-time validation probe are
    /// independent round trips; each clears only its own flag when it
    /// finishes.
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

    /// Form completeness for Convert — not validation success. Invalid
    /// selections are discovered by clicking Convert, which calls
    /// `validateSelection()`.
    var runEnabled: Bool {
        perNegative != nil
            && !selectedFiles.isEmpty
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

    /// Cmd-A: every catalogue entry joins the selection.
    func selectAll() {
        selectedFiles = Set(catalogue)
    }

    /// Cmd-D: the selection empties out.
    func deselectAll() {
        selectedFiles = []
    }

    /// The `run` invocation this configuration describes from its current
    /// form fields, or `nil` when the form is incomplete. Does not require
    /// prior validation — `ContentView` validates before starting a run.
    func buildRunCommand() -> CLICommand? {
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

    /// Backward-compatible alias for tests and call sites that expect the
    /// old name.
    func runCommand() -> CLICommand? {
        buildRunCommand()
    }

    // MARK: - Probing

    /// Clears grouping preview and validation results from a prior Convert
    /// attempt. Called when any inert-until-Convert setting changes.
    func clearValidationState() {
        validationTask?.cancel()
        validationTask = nil
        groups = []
        selectionWarnings = []
        selectionError = nil
        rollError = nil
        isValidating = false
    }

    /// Runs `probe --files` with the current selection, grid, roll, and
    /// flat-field profile. Returns `true` when the selection is runnable.
    /// Populates `groups`, warnings, and errors for the UI.
    @discardableResult
    func validateSelection() async -> Bool {
        validationTask?.cancel()
        guard let inputFolder, !selectedFiles.isEmpty, let across else {
            clearValidationState()
            return false
        }

        let rollURL = rollURL
        let files = selectedFilesInCanonicalOrder
        let flatFieldProfileID = flatFieldProfileID
        let down = down

        isValidating = true
        let task = Task { [runner] () -> ProbeCallResult in
            await Self.runProbe(
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
        }
        validationTask = Task {
            _ = await task.value
        }
        let result = await task.value
        guard !Task.isCancelled else { return false }
        apply(result)
        isValidating = false
        return selectionError == nil && rollError == nil
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
