import Foundation
import Observation

/// State for the Export tab (protocol version 5): one `scanny-boy export`
/// invocation, from the chosen output folder through per-negative results.
///
/// Export drives its own CLI session rather than `RunModel`'s — `export`
/// emits no `progress`, so the run log's machinery would be dead weight —
/// but the one-helper-at-a-time discipline still holds: views gate the
/// Export button on `run.isActive || export.isExporting`, and
/// `isExporting` refuses re-entry here.
@MainActor
@Observable
final class ExportModel {
    /// One `export_done` event: a negative's edits were applied and its
    /// TIFF written into the output folder.
    struct ExportedNegative: Sendable, Hashable {
        let negativeID: String
        let output: String
        let width: Int
        let height: Int
    }

    enum Phase: Sendable, Hashable {
        case idle
        case running
        case finished
    }

    private let runner: CLIRunner

    private(set) var phase: Phase = .idle
    /// Set by the view's folder picker; the export writes into it.
    var outputDirectory: URL?
    /// Per `export_done`, in the order the CLI reported them.
    private(set) var exportedNegatives: [ExportedNegative] = []
    private(set) var warnings: [String] = []
    /// The CLI's fatal `error` event, if it sent one.
    private(set) var failureMessage: String?
    private(set) var outcome: CLIOutcome?

    @ObservationIgnored private var exportTask: Task<Void, Never>?
    /// Bumped on every `export(...)` and captured by that call's completion
    /// closure, so a superseded invocation's tail cannot overwrite a later
    /// one's `phase`. Unreachable today — `exportTask` is never cancelled and
    /// a second `export(...)` is refused by `canExport` — but the guard
    /// costs nothing and matches the pattern `RunModel` already establishes.
    @ObservationIgnored private var exportGeneration = 0

    init(runner: CLIRunner) {
        self.runner = runner
    }

    // MARK: - Derived state

    var isExporting: Bool { phase == .running }

    var canExport: Bool {
        outputDirectory != nil && !isExporting
    }

    /// What to tell the user once the export has ended.
    var completionSummary: String? {
        guard phase == .finished else { return nil }
        if let failureMessage {
            return "The export failed: \(failureMessage)"
        }
        guard let outcome else {
            return warnings.isEmpty ? nil : "The export ended early: \(warnings.joined(separator: "; "))"
        }
        switch outcome {
        case .success:
            return "Exported \(exportedNegatives.count) negative(s) to the chosen folder."
        case .failure:
            return "Exported \(exportedNegatives.count) negative(s) with warnings; check the list above."
        case .cancelled, .usageError, .terminatedBySignal:
            return "The export did not finish."
        }
    }

    // MARK: - Exporting

    /// Runs one export. `negatives` is the roll's visible negatives; every
    /// one is exported (the CLI's `--negatives` selection is a later
    /// refinement), and `onNegative` is consulted per negative so the UI
    /// can offer a per-negative progress hint without parsing events itself.
    func export(roll: URL, output: URL) {
        guard canExport else { return }
        phase = .running
        outputDirectory = output
        exportedNegatives = []
        warnings = []
        failureMessage = nil
        outcome = nil

        exportGeneration += 1
        let generation = exportGeneration
        exportTask = Task { [weak self, runner] in
            let session = runner.session(for: .export(roll: roll, output: output))
            do {
                for await outputLine in try await session.start() {
                    self?.apply(outputLine)
                }
            } catch {
                self?.failureMessage = error.localizedDescription
            }
            guard let self, self.exportGeneration == generation else { return }
            self.phase = .finished
        }
    }

    /// Waits for the export in flight. Test-only, matching
    /// `RunModel.waitForCompletion`.
    func waitForCompletion() async {
        await exportTask?.value
    }

    /// Clears the last export's results (Chunk P4-5): the export summary and
    /// exported-file list belong to the roll they were exported from, the
    /// same reasoning `RunModel.clearResults()` applies to the run log.
    /// `outputDirectory` is left alone — it is a convenience default for the
    /// next export, not part of what "belongs to" a roll. Never interrupts
    /// an export in flight, matching `RunModel.clearResults()`'s posture.
    func clearResults() {
        guard !isExporting else { return }
        exportGeneration += 1
        exportTask = nil
        phase = .idle
        exportedNegatives = []
        warnings = []
        failureMessage = nil
        outcome = nil
    }

    private func apply(_ output: CLISessionOutput) {
        switch output {
        case .event(let event):
            apply(event)
        case .log:
            break
        case .failure(let failure):
            warnings.append(String(describing: failure))
        case .completed(let completion):
            outcome = completion.outcome
        }
    }

    private func apply(_ event: CLIEvent) {
        switch event.kind {
        case .exportDone:
            if let negativeID = event.negativeID, let output = event.output,
                let width = event.width, let height = event.height
            {
                exportedNegatives.append(
                    ExportedNegative(
                        negativeID: negativeID, output: output, width: width, height: height
                    )
                )
            }
        case .warning:
            if let code = event.code, let message = event.message {
                warnings.append("\(code.name): \(message)")
            }
        case .error:
            if let message = event.message {
                failureMessage = message
            }
        case .editRecorded, .negativeDeleted:
            // An export emits none of these; kept for exhaustiveness.
            break
        case .started, .probeResult, .progress, .itemDone, .groupDone, .groupFailed,
            .finished, .negativeDone, .negativeFailed, .rollCreated, .rollList,
            .rollInfo, .rollRenamed, .rollDeleted, .metadataApplied, .metadataSkipped,
            .flatfieldCreated, .flatfieldList, .flatfieldDeleted, .flatfieldProgress,
            .unknown:
            break
        }
    }
}
