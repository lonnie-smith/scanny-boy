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
        /// The helper itself has exited and `finish()` is now reading the
        /// manifest back — a real CLI round trip on a large roll — before
        /// declaring the invocation `.finished`. Deliberately excluded from
        /// `isActive`/`canCancel`: `session` is about to be nilled, so there
        /// is nothing left to cancel, and `RunProgressView` would otherwise
        /// keep showing a progress bar frozen at 100%.
        case finishing
        case finished
    }

    /// Which subcommand this invocation started. Views key their own
    /// results sections on this rather than inferring the invocation from
    /// the *shape* of the results (M9) — e.g. an apply-metadata that failed
    /// outright produces zero applied and zero skipped, which is
    /// indistinguishable by shape from "no apply ever ran".
    enum Invocation: Sendable, Hashable {
        case convert
        case run
        case stitch
        case applyMetadata

        /// `command.arguments.first`, decoded — `nil` for anything this
        /// type does not name, in which case `RunModel` simply tracks no
        /// invocation-specific behaviour for it.
        init?(commandName: String?) {
            switch commandName {
            case "prepare": self = .convert
            case "run": self = .run
            case "stitch": self = .stitch
            case "apply-metadata": self = .applyMetadata
            default: return nil
            }
        }
    }

    private let runner: CLIRunner
    private let gracePeriod: Duration

    // MARK: - Observable state

    private(set) var phase: Phase = .idle
    private(set) var runID: String?

    /// Pipeline steps completed and expected, straight from `progress`.
    /// `run_pipeline.py` reports one span across both stages (section 3.9),
    /// so this needs no special handling to cover a `run`'s stitch stage too.
    private(set) var completedSteps = 0
    private(set) var totalSteps = 0
    private(set) var currentStep: CLIPipelineStep?
    /// The total negatives this invocation is expected to process, known
    /// upfront by the caller (`ConfigurationModel.groups.count`,
    /// `EditModel.dirtyCount`, or a kept work directory's manifest for a
    /// re-stitch) — `nil` when the caller didn't supply one.
    private(set) var totalNegatives: Int?
    /// The source file the most recent `progress` event named. With more than
    /// one worker this is "one of the frames in flight", not "the frame the
    /// run has reached".
    private(set) var currentFilename: String?
    /// `"prepare"` or `"stitch"`, straight from `progress.stage`. `nil` until
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

    /// One `negative_id` per `metadata_applied` event (section 3.8): `apply-
    /// metadata`'s own progress, distinct from `stitchedNegatives`, which is
    /// about publishing rather than metadata.
    private(set) var appliedNegativeIDs: [String] = []
    /// One entry per `metadata_skipped` event — `OUTPUT_MODIFIED_EXTERNALLY`
    /// is the only code section 3.8 defines for this, but the message is
    /// carried through unparsed regardless.
    private(set) var skippedMetadata: [FailedGroup] = []

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

    // MARK: - Private state

    @ObservationIgnored private var session: CLISession?
    @ObservationIgnored private var runTask: Task<Void, Never>?
    @ObservationIgnored private var forceTask: Task<Void, Never>?
    @ObservationIgnored private var cancelRequested = false
    /// Which subcommand this invocation started, captured at `start()`.
    /// Decides which manifest `finish()` reads back: `RunManifest` for a
    /// plain `convert`, `RollManifest` for anything that can reach the
    /// stitch stage. Exposed as observable, typed state (M9) rather than
    /// left as a private string, so views can key their own results
    /// sections on the actual invocation instead of inferring it from the
    /// shape of the results.
    private(set) var invocation: Invocation?
    /// The selection in canonical order, so a `source_index` can be named.
    @ObservationIgnored private var sourceNames: [String] = []
    /// The output folder the running or most recently finished invocation
    /// used — not necessarily `ConfigurationModel.outputFolder`, since
    /// re-stitch (Chunk P2-10) can target a folder of its own. Views read
    /// this back for Reveal in Finder rather than assuming the two coincide.
    private(set) var outputFolder: URL?

    init(
        runner: CLIRunner,
        gracePeriod: Duration = CLISession.defaultGracePeriod
    ) {
        self.runner = runner
        self.gracePeriod = gracePeriod
    }

    // MARK: - Derived state

    var isActive: Bool { phase == .running || phase == .cancelling }

    /// Cancel is offered exactly while there is something to cancel, and a
    /// second request is not one of those times.
    var canCancel: Bool { phase == .running && !cancelRequested }

    /// Section 4.2: derived from counts, never from a source index. Tracks
    /// `negativesCompleted`/`totalNegatives` rather than the pipeline's
    /// `completedSteps`/`totalSteps`, so the bar and the "N of M negative(s)"
    /// label next to it always agree — `nil` when the caller didn't supply a
    /// `totalNegatives` to divide by.
    var fractionComplete: Double? {
        guard let totalNegatives, totalNegatives > 0 else { return nil }
        return min(1, Double(negativesCompleted) / Double(totalNegatives))
    }

    /// Negatives finished so far, counted the same way `completionSummary`
    /// counts them at the end — by stitched/published negative, not by
    /// frame. `stage` tells `run` apart from its convert and stitch halves;
    /// `stitch` (re-stitch) has no convert stage at all.
    var negativesCompleted: Int {
        if invocation == .applyMetadata {
            return appliedNegativeIDs.count + skippedMetadata.count
        }
        if invocation == .stitch || stage == "stitch" {
            return stitchedNegatives.count + failedNegatives.count
        }
        return completedGroups.count + failedGroups.count
    }

    /// `run` and `stitch` can reach the stitch stage; `convert` cannot. This
    /// decides how `completionSummary` counts what happened — by stitched
    /// negative (`stitchedNegatives`/`failedNegatives`), never by
    /// intermediate frame.
    private var isStitchInvocation: Bool {
        invocation == .run || invocation == .stitch
    }

    /// `run`, `stitch`, and `apply-metadata` all end by touching
    /// `scanny-boy-roll.json`; only a plain `prepare` writes
    /// `scanny-boy-manifest.json` instead. Decides which manifest
    /// `finish()` reads back.
    private var touchesRollManifest: Bool {
        isStitchInvocation || invocation == .applyMetadata
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
        if invocation == .applyMetadata {
            switch outcome {
            case .success:
                return "Applied \(appliedNegativeIDs.count) negative(s)."
            case .failure:
                return "Applied \(appliedNegativeIDs.count) negative(s); "
                    + "\(skippedMetadata.count) skipped."
            case .cancelled, .usageError, .terminatedBySignal:
                break
            }
        }
        switch outcome {
        case .success:
            if isStitchInvocation {
                return "Converted \(stitchedNegatives.count) negative(s)."
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

    // MARK: - Result presentation

    /// One negative the invocation touched, merged from the parallel event
    /// lists (`completedGroups`, `failedGroups`, `stitchedNegatives`,
    /// `failedNegatives`, `appliedNegativeIDs`, `skippedMetadata`) so the
    /// results view can show one row per negative instead of one list per
    /// event kind. For a `run` the convert stage and the stitch stage
    /// report the same negative id, and the merge folds the two halves
    /// into a single row.
    struct NegativeResult: Identifiable, Hashable {
        enum Status: Hashable {
            case succeeded
            case failed
            /// `apply-metadata` left this negative's metadata untouched
            /// (`OUTPUT_MODIFIED_EXTERNALLY`).
            case skipped
        }

        /// How a published negative's registration quality reads, judged
        /// against the thresholds the CLI itself enforces
        /// (`MAX_GLOBAL_RMS_PX` = 12 px, `MAX_OVERLAP_MAD` = 0.20 —
        /// composite.py/layout.py). A published negative is always below
        /// those gates, so `.poor` should not occur; it is a guard, not a
        /// diagnosis.
        enum Quality: Hashable {
            case good
            case fair
            case poor
        }

        let id: String
        let status: Status
        /// The stitched TIFF's filename, for invocations that stitch. A
        /// plain `convert` publishes per-frame files with no single output
        /// to name, so this is nil there.
        let output: String?
        let dimensions: String?
        let quality: Quality?
        /// The exact numbers behind `quality`, for the expanded row.
        let qualityDetail: String?
        /// The failure, if any, with its stable code for the report. For a
        /// negative that failed in both stages (`run`), the stitch-stage
        /// failure wins — it is the one that decided the outcome.
        let failure: FailedGroup?
        /// Warnings whose message names this negative (`"{negative_id}: …"`
        /// — the stitch stage's convention). The convert stage prefixes
        /// warnings with a source filename instead, and those stay
        /// run-level.
        let warnings: [Issue]
    }

    /// The per-negative view of the run, in first-seen event order.
    var negativeResults: [NegativeResult] {
        var ids: [String] = []
        var seen = Set<String>()
        func note(_ id: String?) {
            guard let id, seen.insert(id).inserted else { return }
            ids.append(id)
        }
        completedGroups.forEach { note($0) }
        failedGroups.forEach { note($0.groupID) }
        stitchedNegatives.forEach { note($0.negativeID) }
        failedNegatives.forEach { note($0.groupID) }
        appliedNegativeIDs.forEach { note($0) }
        skippedMetadata.forEach { note($0.groupID) }

        let stitched = Dictionary(
            uniqueKeysWithValues: stitchedNegatives.map { ($0.negativeID, $0) }
        )
        var failureByID: [String: FailedGroup] = [:]
        failedGroups.forEach { failureByID[$0.groupID] = $0 }
        // Applied second so a stitch-stage failure replaces a convert-stage
        // one for the same id.
        failedNegatives.forEach { failureByID[$0.groupID] = $0 }
        let skippedByID = Dictionary(
            uniqueKeysWithValues: skippedMetadata.map { ($0.groupID, $0) }
        )

        return ids.map { id in
            let failure = failureByID[id]
            let skipped = skippedByID[id]
            let status: NegativeResult.Status =
                failure != nil ? .failed : skipped != nil ? .skipped : .succeeded
            let prefix = "\(id): "
            let attributed = warnings.filter { $0.message.hasPrefix(prefix) }
            let stitchedNegative = stitched[id]
            return NegativeResult(
                id: id,
                status: status,
                output: stitchedNegative?.output,
                dimensions: stitchedNegative.map { "\($0.width)×\($0.height)" },
                quality: stitchedNegative.map {
                    Self.quality(rms: $0.globalRMS, mad: $0.maxOverlapMAD)
                },
                qualityDetail: stitchedNegative.map {
                    String(
                        format: "RMS %.2f px, overlap MAD %.3f",
                        $0.globalRMS, $0.maxOverlapMAD
                    )
                },
                failure: failure ?? skipped,
                warnings: attributed
            )
        }
    }

    /// Warnings that name no negative the run touched. The convert stage
    /// prefixes its warnings with a source filename rather than a group id,
    /// and some codes prefix with nothing at all, so attribution is
    /// best-effort by design.
    var runLevelWarnings: [Issue] {
        let ids = Set(negativeResults.map(\.id))
        return warnings.filter { warning in
            !ids.contains { id in warning.message.hasPrefix("\(id): ") }
        }
    }

    /// Judged against the same gates the CLI enforces before publishing
    /// (12 px global RMS, 0.20 overlap MAD — composite.py/layout.py, not
    /// part of the event protocol, so mirrored here as constants): good is
    /// a quarter of the gate, fair is anything the gate admitted.
    private static let globalRMSGate = 12.0
    private static let overlapMADGate = 0.20

    private static func quality(rms: Double, mad: Double) -> NegativeResult.Quality {
        if rms <= globalRMSGate / 4 && mad <= overlapMADGate / 4 {
            return .good
        }
        if rms <= globalRMSGate && mad <= overlapMADGate {
            return .fair
        }
        return .poor
    }

    /// The full conversion log as copyable plain text — every negative with
    /// its status, stable codes, raw messages, and exact quality numbers.
    /// The UI summarizes; this is the escape hatch for a bug report.
    var reportText: String {
        var lines: [String] = []
        if let summary = completionSummary {
            lines.append(summary)
        }
        if let runID {
            lines.append("Run ID: \(runID)")
        }
        for negative in negativeResults {
            lines.append("")
            switch negative.status {
            case .succeeded: lines.append("\(negative.id): converted")
            case .failed: lines.append("\(negative.id): failed")
            case .skipped: lines.append("\(negative.id): skipped")
            }
            if let failure = negative.failure {
                lines.append("  [\(failure.code.name)] \(failure.message)")
            }
            if let output = negative.output {
                var detail = "  output: \(output)"
                if let dimensions = negative.dimensions {
                    detail += ", \(dimensions)"
                }
                if let qualityDetail = negative.qualityDetail {
                    detail += ", \(qualityDetail)"
                }
                lines.append(detail)
            }
            for warning in negative.warnings {
                lines.append("  [\(warning.code.name)] \(warning.message)")
            }
        }
        let runLevel = runLevelWarnings
        if !runLevel.isEmpty {
            lines.append("")
            lines.append("Warnings:")
            for warning in runLevel {
                lines.append("  [\(warning.code.name)] \(warning.message)")
            }
        }
        if !streamFailures.isEmpty {
            lines.append("")
            lines.append("Stream problems:")
            for failure in streamFailures {
                lines.append("  \(RunFailureText.string(failure))")
            }
        }
        return lines.joined(separator: "\n")
    }

    // MARK: - Running

    /// Starts one `convert`. `files` must be the selection in canonical order:
    /// it is what turns a `source_index` back into a filename, and section 3.3
    /// forbids this app from working that order out for itself.
    func start(
        command: CLICommand, files: [String], outputFolder: URL, totalNegatives: Int? = nil
    ) {
        guard !isActive else { return }
        reset()
        sourceNames = files
        self.outputFolder = outputFolder
        self.totalNegatives = totalNegatives
        invocation = Invocation(commandName: command.arguments.first)
        phase = .running

        let session = runner.session(for: command)
        self.session = session
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
        case .metadataApplied:
            if let negativeID = event.negativeID {
                appliedNegativeIDs.append(negativeID)
            }
        case .metadataSkipped:
            if let negativeID = event.negativeID, let code = event.code, let message = event.message {
                skippedMetadata.append(
                    FailedGroup(groupID: negativeID, code: code, message: message)
                )
            }
        case .started, .probeResult, .finished, .unknown,
             .rollCreated, .rollList, .rollInfo, .rollRenamed, .rollDeleted,
             .editRecorded, .negativeDeleted, .exportDone,
             .flatfieldCreated, .flatfieldList, .flatfieldDeleted, .flatfieldProgress:
            break
        }
    }

    private func finish() async {
        forceTask?.cancel()
        forceTask = nil
        phase = .finishing
        // `convert` writes `scanny-boy-manifest.json` into the output folder;
        // `run` and `stitch` write `scanny-boy-roll.json` there instead — the
        // work directory `scanny-boy-manifest.json` still lives in may
        // already be gone by the time this runs (section 3.5's cleanup).
        if touchesRollManifest {
            rollManifestReport = await Self.readRollManifest(
                runner: runner, roll: outputFolder, runID: runID
            )
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

    /// Clears the log of the last finished run (Chunk 10's results, the
    /// manifest reports, the phase) back to a fresh `.idle` state, used when
    /// the user switches rolls: the log belonged to the roll it stitched,
    /// not to the workspace. Never interrupts a run in flight — `reset`'s
    /// task cancellation is for `start`'s benefit; here a guard simply
    /// refuses while something is running.
    func clearResults() {
        guard !isActive else { return }
        reset()
    }

    private func reset() {
        runTask?.cancel()
        forceTask?.cancel()
        runTask = nil
        forceTask = nil
        session = nil
        cancelRequested = false
        invocation = nil
        phase = .idle
        runID = nil
        completedSteps = 0
        totalSteps = 0
        currentStep = nil
        totalNegatives = nil
        currentFilename = nil
        stage = nil
        publishedOutputs = []
        completedGroups = []
        failedGroups = []
        stitchedNegatives = []
        failedNegatives = []
        appliedNegativeIDs = []
        skippedMetadata = []
        warnings = []
        cliError = nil
        streamFailures = []
        outcome = nil
        manifestReport = nil
        rollManifestReport = nil
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

    /// Reads the roll manifest back through `roll info` (section 3.1: Swift
    /// never parses `scanny-boy-roll.json` itself) rather than from disk —
    /// unlike `readManifest`, this is a real CLI round trip, since a roll
    /// manifest has no `Decodable`-from-file counterpart any more.
    private static func readRollManifest(
        runner: CLIRunner, roll: URL?, runID: String?
    ) async -> RollManifestReport? {
        guard let roll else { return nil }
        let session = runner.session(for: .rollInfo(roll: roll))
        var result: RollManifestReport = .unavailable("roll info produced no result")
        do {
            for await output in try await session.start() {
                guard case .event(let event) = output else { continue }
                switch event.kind {
                case .rollInfo:
                    if let fields = event.manifest, let manifest = RollManifest(fields: fields) {
                        result = RollManifestReport(manifest: manifest, runID: runID)
                    } else {
                        result = .unavailable("the roll manifest could not be decoded")
                    }
                case .error:
                    if let message = event.message {
                        result = .unavailable(message)
                    }
                default:
                    continue
                }
            }
        } catch {
            return .unavailable(error.localizedDescription)
        }
        return result
    }

    // MARK: - Testing

    /// Waits for the run in flight to finish applying its final state.
    /// Test-only: the UI is driven by `@Observable`'s change notifications.
    func waitForCompletion() async {
        await runTask?.value
    }
}
