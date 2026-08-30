import Foundation
import Observation

/// The state of one `convert` invocation, from Run through progress,
/// cancellation, and completion.
///
/// `docs/IMPLEMENTATION_PLAN.md` Chunk 10. Three rules from section 4.2 and
/// section 3.6 shape almost everything here:
///
/// - Overall progress comes from `progress`'s `completed` and `total` counts,
///   never from the largest `source_index` seen. Frames finish out of order
///   once `--jobs` is above one, so an index is not a position in a queue.
/// - Only `item_done` means a file was published. Staged work reported by
///   `progress` belongs to a group that may still be discarded whole.
/// - A stream failure — a pipe that could not be read, a line that was not a
///   usable event, a helper that would not launch — is a different thing from
///   an `error` event, and the two are kept apart rather than merged into one
///   "something went wrong" string.
@MainActor
@Observable
final class RunModel {
    /// One `warning` or `error` event's stable code and message.
    struct Issue: Sendable, Hashable {
        let code: CLICode
        let message: String
    }

    /// One `group_failed` event. A cancelled group is deliberately not one of
    /// these: `CONTRACT.md` says a cancelled negative emits no `group_failed`,
    /// because it was abandoned rather than failed.
    ///
    /// Also holds `negative_failed` events (Chunk P2-9): the two events carry
    /// the identical `(id, code, message)` shape, so one type serves both
    /// rather than a near-duplicate `FailedNegative`.
    struct FailedGroup: Sendable, Hashable {
        let groupID: String
        let code: CLICode
        let message: String
    }

    /// One `negative_done` event: a stitched TIFF was published, with the
    /// section 3.4 quality numbers it was published with.
    struct StitchedNegative: Sendable, Hashable {
        let negativeID: String
        let output: String
        let width: Int
        let height: Int
        let globalRMS: Double
        let maxOverlapMAD: Double
    }

    enum Phase: Sendable, Hashable {
        case idle
        case running
        /// Cancellation has been requested and the helper has not stopped yet.
        case cancelling
        case finished
    }

    private let runner: CLIRunner
    private let gracePeriod: Duration
    private let now: @Sendable () -> Date

    // MARK: - Observable state

    private(set) var phase: Phase = .idle
    private(set) var runID: String?

    /// Pipeline steps completed and expected, straight from `progress`.
    /// `run_pipeline.py` reports one span across both stages (section 3.9),
    /// so this needs no special handling to cover a `run`'s stitch stage too.
    private(set) var completedSteps = 0
    private(set) var totalSteps = 0
    private(set) var currentStep: CLIPipelineStep?
    /// The source file the most recent `progress` event named. With more than
    /// one worker this is "one of the frames in flight", not "the frame the
    /// run has reached".
    private(set) var currentFilename: String?
    /// `"convert"` or `"stitch"`, straight from `progress.stage`. `nil` until
    /// the first `progress` event arrives.
    private(set) var stage: String?

    /// Files published into the output folder, in `item_done` order. For
    /// `convert`, these are the final TIFFs. For `run`, these are the
    /// per-frame *intermediates* the convert stage writes into the work
    /// directory — not what ends up in the output folder, which is
    /// `stitchedNegatives` below.
    private(set) var publishedOutputs: [String] = []
    private(set) var completedGroups: [String] = []
    private(set) var failedGroups: [FailedGroup] = []
    /// One stitched TIFF per `negative_done`, in the order the CLI published
    /// them.
    private(set) var stitchedNegatives: [StitchedNegative] = []
    /// One `negative_failed` per negative the stitch stage could not
    /// publish. A cancelled negative is deliberately not one of these, for
    /// the same reason a cancelled group is not in `failedGroups`.
    private(set) var failedNegatives: [FailedGroup] = []
    /// The work directory's path, when `run` reported `INTERMEDIATES_KEPT`.
    /// `nil` means either the run has not ended, or the work directory was
    /// removed.
    private(set) var keptWorkDirectory: String?

    private(set) var warnings: [Issue] = []
    /// The CLI's own fatal `error` event, if it sent one.
    private(set) var cliError: Issue?
    /// Launch, read, and decode failures. Never mixed with `cliError`.
    private(set) var streamFailures: [CLISessionFailure] = []

    private(set) var outcome: CLIOutcome?
    /// Read back after `convert`. `nil` for `run` and `stitch`, which write
    /// `scanny-boy-roll.json` into the output folder instead — see
    /// `rollManifestReport`.
    private(set) var manifestReport: ManifestReport?
    /// Read back after `run` or `stitch`. `nil` for `convert`.
    private(set) var rollManifestReport: RollManifestReport?

    private(set) var startedAt: Date?
    /// Refreshed on a timer while running, and frozen when the run ends.
    private(set) var elapsed: TimeInterval = 0

    // MARK: - Private state

    @ObservationIgnored private var session: CLISession?
    @ObservationIgnored private var runTask: Task<Void, Never>?
    @ObservationIgnored private var tickTask: Task<Void, Never>?
    @ObservationIgnored private var forceTask: Task<Void, Never>?
    @ObservationIgnored private var cancelRequested = false
    /// The subcommand this invocation started (`"convert"`, `"run"`, or
    /// `"stitch"`) — `command.arguments.first`, captured at `start()`. Decides
    /// which manifest `finish()` reads back: `RunManifest` for a plain
    /// `convert`, `RollManifest` for anything that can reach the stitch
    /// stage.
    @ObservationIgnored private var invokedCommandName: String?
    /// The selection in canonical order, so a `source_index` can be named.
    @ObservationIgnored private var sourceNames: [String] = []
    /// The output folder the running or most recently finished invocation
    /// used — not necessarily `ConfigurationModel.outputFolder`, since
    /// re-stitch (Chunk P2-10) can target a folder of its own. Views read
    /// this back for Reveal in Finder rather than assuming the two coincide.
    private(set) var outputFolder: URL?

    init(
        runner: CLIRunner,
        gracePeriod: Duration = CLISession.defaultGracePeriod,
        now: @escaping @Sendable () -> Date = Date.init
    ) {
        self.runner = runner
        self.gracePeriod = gracePeriod
        self.now = now
    }

    // MARK: - Derived state

    var isActive: Bool { phase == .running || phase == .cancelling }

    /// Cancel is offered exactly while there is something to cancel, and a
    /// second request is not one of those times.
    var canCancel: Bool { phase == .running && !cancelRequested }

    /// Section 4.2: derived from counts, never from a source index.
    var fractionComplete: Double? {
        guard totalSteps > 0 else { return nil }
        return min(1, Double(completedSteps) / Double(totalSteps))
    }

    var estimatedRemaining: TimeInterval? {
        Self.estimatedRemaining(elapsed: elapsed, completed: completedSteps, total: totalSteps)
    }

    /// Split out as a pure function so the estimate can be tested without a
    /// real clock or a real subprocess.
    static func estimatedRemaining(
        elapsed: TimeInterval,
        completed: Int,
        total: Int
    ) -> TimeInterval? {
        guard completed > 0, total > completed, elapsed > 0 else { return nil }
        return elapsed / Double(completed) * Double(total - completed)
    }

    /// `run` and `stitch` can reach the stitch stage; `convert` cannot. This
    /// decides both which manifest `finish()` reads back and how
    /// `completionSummary` counts what happened — by stitched negative
    /// (`stitchedNegatives`/`failedNegatives`), never by intermediate frame.
    private var isStitchInvocation: Bool {
        invokedCommandName == "run" || invokedCommandName == "stitch"
    }

    /// What to tell the user once the run has ended. Deliberately built from
    /// `outcome` rather than from message text, which section 4.2 says is not
    /// the machine-readable interface.
    ///
    /// Counts **negatives**, not frames (Chunk P2-9): for `run` and `stitch`,
    /// `publishedOutputs` names per-frame intermediates that may not even
    /// survive the run, so the summary is built from `stitchedNegatives` and
    /// `failedNegatives` instead.
    var completionSummary: String? {
        guard phase == .finished, let outcome else {
            // A run that ended without ever producing a completion: the helper
            // could not be launched at all.
            return streamFailures.isEmpty ? nil : "The command-line helper could not be run."
        }
        switch outcome {
        case .success:
            if isStitchInvocation {
                return "Stitched \(stitchedNegatives.count) negative(s)."
            }
            return "Converted \(publishedOutputs.count) file(s) in "
                + "\(completedGroups.count) negative(s)."
        case .cancelled(let forced):
            let keptCount = isStitchInvocation ? stitchedNegatives.count : completedGroups.count
            let kept = "\(keptCount) completed negative(s) were kept; "
                + "the negative in progress was discarded."
            return forced
                ? "Cancelled by force after the grace period. \(kept)"
                : "Cancelled. \(kept)"
        case .usageError:
            return "The helper rejected the command as invalid usage."
        case .failure:
            if let cliError {
                return "The run failed: \(cliError.message)"
            }
            let failedCount = isStitchInvocation
                ? failedGroups.count + failedNegatives.count
                : failedGroups.count
            return failedCount == 0
                ? "The run failed."
                : "The run finished with \(failedCount) failed negative(s)."
        case .terminatedBySignal(let signal):
            return "The helper was terminated by signal \(signal)."
        }
    }

    // MARK: - Running

    /// Starts one `convert`. `files` must be the selection in canonical order:
    /// it is what turns a `source_index` back into a filename, and section 3.3
    /// forbids this app from working that order out for itself.
    func start(command: CLICommand, files: [String], outputFolder: URL) {
        guard !isActive else { return }
        reset()
        sourceNames = files
        self.outputFolder = outputFolder
        invokedCommandName = command.arguments.first
        phase = .running
        startedAt = now()

        let session = runner.session(for: command)
        self.session = session
        tickTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .milliseconds(500))
                guard !Task.isCancelled else { return }
                self?.refreshElapsed()
            }
        }
        runTask = Task { [weak self] in
            await self?.consume(session)
        }
    }

    private func consume(_ session: CLISession) async {
        do {
            for await output in try await session.start() {
                apply(output)
            }
        } catch let failure as CLISessionFailure {
            streamFailures.append(failure)
        } catch {
            streamFailures.append(.launch(error.localizedDescription))
        }
        await finish()
    }

    private func apply(_ output: CLISessionOutput) {
        refreshElapsed()
        switch output {
        case .event(let event):
            apply(event)
        case .log:
            // stderr is human-readable and is never parsed (section 4.2).
            break
        case .failure(let failure):
            streamFailures.append(failure)
        case .completed(let completion):
            outcome = completion.outcome
        }
    }

    private func apply(_ event: CLIEvent) {
        if runID == nil { runID = event.runID }
        switch event.kind {
        case .progress:
            if let completed = event.completed, let total = event.total {
                completedSteps = completed
                totalSteps = total
            }
            currentStep = event.step
            stage = event.stage
            if let index = event.sourceIndex, sourceNames.indices.contains(index) {
                currentFilename = sourceNames[index]
            }
        case .itemDone:
            // The only event that means a file reached the output folder.
            if let output = event.output {
                publishedOutputs.append(output)
            }
        case .groupDone:
            if let groupID = event.groupID {
                completedGroups.append(groupID)
            }
        case .groupFailed:
            if let groupID = event.groupID, let code = event.code, let message = event.message {
                failedGroups.append(
                    FailedGroup(groupID: groupID, code: code, message: message)
                )
            }
        case .warning:
            if let code = event.code, let message = event.message {
                warnings.append(Issue(code: code, message: message))
                // Section 3.5: the one place the kept work directory's path is
                // reported. No dedicated field exists for it — `WarningEvent`
                // carries only `code` and `message` — so this is the CLI's own
                // fixed message text (`run_pipeline.py`), parsed the one way it
                // is ever produced.
                if code == .intermediatesKept,
                    let path = message.range(of: "intermediates kept at ")
                {
                    keptWorkDirectory = String(message[path.upperBound...])
                }
            }
        case .error:
            if let code = event.code, let message = event.message {
                cliError = Issue(code: code, message: message)
            }
        case .negativeDone:
            if let negativeID = event.negativeID, let output = event.output,
                let width = event.width, let height = event.height,
                let globalRMS = event.globalRMS, let maxOverlapMAD = event.maxOverlapMAD
            {
                stitchedNegatives.append(
                    StitchedNegative(
                        negativeID: negativeID,
                        output: output,
                        width: width,
                        height: height,
                        globalRMS: globalRMS,
                        maxOverlapMAD: maxOverlapMAD
                    )
                )
            }
        case .negativeFailed:
            if let negativeID = event.negativeID, let code = event.code, let message = event.message {
                failedNegatives.append(
                    FailedGroup(groupID: negativeID, code: code, message: message)
                )
            }
        case .started, .probeResult, .finished, .unknown:
            break
        }
    }

    private func finish() async {
        tickTask?.cancel()
        tickTask = nil
        forceTask?.cancel()
        forceTask = nil
        refreshElapsed()
        // `convert` writes `scanny-boy-manifest.json` into the output folder;
        // `run` and `stitch` write `scanny-boy-roll.json` there instead — the
        // work directory `scanny-boy-manifest.json` still lives in may
        // already be gone by the time this runs (section 3.5's cleanup).
        if isStitchInvocation {
            rollManifestReport = await Self.readRollManifest(in: outputFolder)
        } else {
            manifestReport = await Self.readManifest(in: outputFolder)
        }
        session = nil
        phase = .finished
    }

    // MARK: - Cancellation

    /// Section 3.8: request cooperative cancellation with SIGTERM, then force
    /// the issue if the helper has not stopped by the end of the grace period.
    ///
    /// Repeated requests do nothing extra. A second SIGTERM would not help —
    /// the CLI's handler has already set its flag — and a second grace-period
    /// task would shorten the deadline the first one is still counting down.
    func cancel() {
        guard canCancel, let session else { return }
        cancelRequested = true
        phase = .cancelling
        Task { await session.requestCancellation() }
        forceTask = Task { [gracePeriod] in
            try? await Task.sleep(for: gracePeriod)
            guard !Task.isCancelled else { return }
            await session.forceTerminate()
        }
    }

    // MARK: - Housekeeping

    private func reset() {
        runTask?.cancel()
        tickTask?.cancel()
        forceTask?.cancel()
        runTask = nil
        tickTask = nil
        forceTask = nil
        session = nil
        cancelRequested = false
        invokedCommandName = nil
        phase = .idle
        runID = nil
        completedSteps = 0
        totalSteps = 0
        currentStep = nil
        currentFilename = nil
        stage = nil
        publishedOutputs = []
        completedGroups = []
        failedGroups = []
        stitchedNegatives = []
        failedNegatives = []
        keptWorkDirectory = nil
        warnings = []
        cliError = nil
        streamFailures = []
        outcome = nil
        manifestReport = nil
        rollManifestReport = nil
        startedAt = nil
        elapsed = 0
    }

    private func refreshElapsed() {
        guard let startedAt else { return }
        elapsed = max(0, now().timeIntervalSince(startedAt))
    }

    /// Reads the manifest off the main actor: it is small, but it is still
    /// file I/O on the path that ends a run.
    private static func readManifest(in folder: URL?) async -> ManifestReport? {
        guard let folder else { return nil }
        return await Task.detached {
            do {
                return ManifestReport(manifest: try RunManifest.read(inOutputFolder: folder))
            } catch {
                return .unavailable(error.localizedDescription)
            }
        }.value
    }

    /// Reads the roll manifest off the main actor, for the same reason
    /// `readManifest` does.
    private static func readRollManifest(in folder: URL?) async -> RollManifestReport? {
        guard let folder else { return nil }
        return await Task.detached {
            do {
                return RollManifestReport(manifest: try RollManifest.read(inOutputFolder: folder))
            } catch {
                return .unavailable(error.localizedDescription)
            }
        }.value
    }

    // MARK: - Testing

    /// Waits for the run in flight to finish applying its final state.
    /// Test-only: the UI is driven by `@Observable`'s change notifications.
    func waitForCompletion() async {
        await runTask?.value
    }
}
