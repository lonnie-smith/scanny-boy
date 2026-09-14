import Foundation
import Observation

/// Background prepare → capture check → serial stitch queue.
@MainActor
@Observable
final class StitchQueueModel {
    static let maxParallelPrepares = 2
    static let stateFilename = "stitch-queue.json"

    enum Step: String, Codable, Sendable {
        case waitingPrepare
        case preparing
        case waitingCheck
        case checking
        case waitingStitch
        case stitching
        case published
        case prepareFailed
        case checkFailed
        case stitchFailed
        case waitingForDisk
    }

    struct QueuedNegative: Identifiable, Codable, Sendable {
        let id: UUID
        let stamp: String
        let framePaths: [String]
        let workFolder: String
        var step: Step
        var failureMessage: String?
        var failureCode: String?
        let enqueuedAt: Date
        var publishedAt: Date?
        var outputFilename: String?
    }

    struct StepProgress: Equatable, Sendable {
        var completed: Int
        var total: Int
        var step: CLIPipelineStep?
        let startedAt: Date
    }

    struct PersistedState: Codable, Sendable {
        var rollPath: String
        var captureFolder: String
        var rigProfileID: String?
        var across: Int
        var down: Int
        var negatives: [QueuedNegative]
    }

    let runner: CLIRunner
    /// Fired after `rollRefresh` — the roll manifest on disk has just
    /// gained newly stitched negatives that other tabs (Edit, Library)
    /// have no other way to learn about.
    var onRollUpdated: (() -> Void)?
    private(set) var negatives: [QueuedNegative] = []
    private(set) var activePrepareCount = 0
    private(set) var isStitching = false
    private(set) var isRefreshing = false
    private(set) var progress: [UUID: StepProgress] = [:]

    private(set) var rollURL: URL?
    private var captureFolder: URL?
    private var rigProfileID: String?
    private var across: Int = 1
    private var down: Int = 1
    private var drainTask: Task<Void, Never>?
    private var sleepAssertion: NSObjectProtocol?

    init(runner: CLIRunner) {
        self.runner = runner
        restoreState()
    }

    var hasWork: Bool {
        negatives.contains { $0.step != .published }
    }

    var hasUnpublishedEntries: Bool { hasWork }

    var publishedNegativeIDs: Set<UUID> {
        Set(negatives.filter { $0.step == .published }.map(\.id))
    }

    var isQueueBusy: Bool { hasWork || isStitching || isRefreshing }

    func configure(
        roll: URL,
        captureFolder: URL,
        rigProfileID: String? = nil,
        across: Int,
        down: Int
    ) {
        rollURL = roll
        self.captureFolder = captureFolder
        self.rigProfileID = rigProfileID
        self.across = across
        self.down = down
    }

    func enqueue(_ negative: CaptureSessionModel.CompletedNegative) {
        guard let captureFolder else { return }
        let work = captureFolder
            .appending(path: ".work", directoryHint: .isDirectory)
            .appending(path: negative.stamp, directoryHint: .isDirectory)
        let entry = QueuedNegative(
            id: negative.id,
            stamp: negative.stamp,
            framePaths: negative.frameURLs.map(\.path),
            workFolder: work.path,
            step: .waitingPrepare,
            failureMessage: nil,
            failureCode: nil,
            enqueuedAt: negative.startedAt
        )
        negatives.append(entry)
        persistState()
        updateSleepAssertion()
        pump()
    }

    func endSession() {
        guard let rollURL, hasWork else { return }
        drainTask = Task { await drainAndRefresh(roll: rollURL) }
    }

    /// Marks one queue entry published — for unit tests only.
    func testingMarkPublished(id: UUID) {
        guard let index = negatives.firstIndex(where: { $0.id == id }) else { return }
        negatives[index].step = .published
    }

    /// Removes every queue entry that has not published and returns paths to
    /// recycle (frame NEFs and work folders).
    func discardUnpublished() -> [URL] {
        drainTask?.cancel()
        drainTask = nil
        isStitching = false
        isRefreshing = false
        var urls: [URL] = []
        let remaining = negatives.filter { entry in
            guard entry.step != .published else { return true }
            urls.append(contentsOf: entry.framePaths.map { URL(fileURLWithPath: $0) })
            urls.append(URL(fileURLWithPath: entry.workFolder))
            progress.removeValue(forKey: entry.id)
            return false
        }
        negatives = remaining
        activePrepareCount = 0
        if negatives.isEmpty {
            clearPersistedState()
        } else {
            persistState()
        }
        updateSleepAssertion()
        return urls
    }

    private func pump() {
        startPreparesIfNeeded()
        startNextStitchIfNeeded()
    }

    private func mutateEntry(id: UUID, _ mutate: (inout QueuedNegative) -> Void) {
        guard let index = negatives.firstIndex(where: { $0.id == id }) else { return }
        mutate(&negatives[index])
    }

    private func startPreparesIfNeeded() {
        guard activePrepareCount < Self.maxParallelPrepares else { return }
        guard let captureFolder else { return }
        let waiting = negatives.filter { $0.step == .waitingPrepare || $0.step == .waitingForDisk }
        for entry in waiting.prefix(Self.maxParallelPrepares - activePrepareCount) {
            guard negatives.firstIndex(where: { $0.id == entry.id }) != nil else { continue }
            mutateEntry(id: entry.id) { $0.step = .preparing }
            activePrepareCount += 1
            progress[entry.id] = StepProgress(
                completed: 0, total: 0, step: nil, startedAt: .now
            )
            let id = entry.id
            Task {
                await runPrepare(id: id, captureFolder: captureFolder, rigProfileID: rigProfileID)
                activePrepareCount = max(0, activePrepareCount - 1)
                progress.removeValue(forKey: id)
                mutateEntry(id: id) { $0.step = .waitingCheck }
                pump()
            }
        }
    }

    private func runPrepare(id: UUID, captureFolder: URL, rigProfileID: String?) async {
        guard negatives.firstIndex(where: { $0.id == id }) != nil else { return }
        let entry = negatives.first(where: { $0.id == id })!
        let files = entry.framePaths.map { URL(fileURLWithPath: $0).lastPathComponent }
        let work = URL(fileURLWithPath: entry.workFolder)
        try? FileManager.default.createDirectory(at: work, withIntermediateDirectories: true)
        let command = CLICommand.prepare(
            input: captureFolder,
            files: files,
            out: work,
            across: across,
            down: down,
            rig: rigProfileID
        )
        let result = await runCommand(id: id, command)
        guard negatives.firstIndex(where: { $0.id == id }) != nil else { return }
        if result.insufficientDisk {
            mutateEntry(id: id) { $0.step = .waitingForDisk }
            persistState()
            return
        }
        if result.failed {
            mutateEntry(id: id) {
                $0.step = .prepareFailed
                $0.failureCode = result.code?.name
                $0.failureMessage = result.message
            }
            persistState()
            return
        }
        mutateEntry(id: id) { $0.step = .waitingCheck }
        await runCheck(id: id, rigProfileID: rigProfileID)
        persistState()
        pump()
    }

    private func runCheck(id: UUID, rigProfileID: String?) async {
        guard negatives.firstIndex(where: { $0.id == id }) != nil else { return }
        mutateEntry(id: id) { $0.step = .checking }
        progress[id] = StepProgress(
            completed: 0, total: 0, step: nil, startedAt: .now
        )
        let work = URL(fileURLWithPath: negatives.first(where: { $0.id == id })!.workFolder)
        let command = CLICommand.captureCheck(work: work, rig: rigProfileID)
        let result = await runCaptureCheck(id: id, command)
        progress.removeValue(forKey: id)
        guard negatives.firstIndex(where: { $0.id == id }) != nil else { return }
        if result.passed {
            mutateEntry(id: id) { $0.step = .waitingStitch }
        } else {
            mutateEntry(id: id) {
                $0.step = .checkFailed
                $0.failureCode = result.code?.name
                $0.failureMessage = result.message
            }
        }
        persistState()
        pump()
    }

    private func startNextStitchIfNeeded() {
        guard !isStitching, let rollURL else { return }
        guard negatives.allSatisfy({ $0.step != .preparing && $0.step != .checking }) else { return }
        guard let entry = negatives.first(where: { $0.step == .waitingStitch }) else { return }
        isStitching = true
        let id = entry.id
        mutateEntry(id: id) { $0.step = .stitching }
        progress[id] = StepProgress(
            completed: 0, total: 0, step: nil, startedAt: .now
        )
        let work = URL(fileURLWithPath: entry.workFolder)
        Task {
            let command = CLICommand.stitch(
                work: work, roll: rollURL, rig: rigProfileID, deferRollRefresh: true
            )
            let result = await runCommand(id: id, command)
            progress.removeValue(forKey: id)
            if negatives.firstIndex(where: { $0.id == id }) != nil {
                if result.failed {
                    mutateEntry(id: id) {
                        $0.step = .stitchFailed
                        $0.failureCode = result.code?.name
                        $0.failureMessage = result.message
                    }
                } else {
                    mutateEntry(id: id) {
                        $0.step = .published
                        $0.publishedAt = Date()
                        $0.outputFilename = result.outputFilename
                    }
                    try? FileManager.default.removeItem(at: work)
                }
                persistState()
            }
            isStitching = false
            pump()
            if !hasWork { await finishDrainIfNeeded() }
        }
    }

    private func drainAndRefresh(roll: URL) async {
        while hasWork {
            pump()
            try? await Task.sleep(for: .milliseconds(200))
        }
        await rollRefresh(roll: roll)
    }

    private func finishDrainIfNeeded() async {
        updateSleepAssertion()
        guard let rollURL, !hasWork else { return }
        await rollRefresh(roll: rollURL)
    }

    func rollRefresh(roll: URL) async {
        isRefreshing = true
        defer { isRefreshing = false }
        // rollRefresh is not tied to a specific negative; pass a dummy id.
        _ = await runCommand(id: UUID(), .rollRefresh(roll: roll))
        clearPersistedState()
        onRollUpdated?()
    }

    // MARK: - Persistence

    private var stateFileURL: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appending(path: "ScannyBoy", directoryHint: .isDirectory)
            .appending(path: Self.stateFilename)
    }

    private func persistState() {
        guard let rollURL, let captureFolder else { return }
        let state = PersistedState(
            rollPath: rollURL.path,
            captureFolder: captureFolder.path,
            rigProfileID: rigProfileID,
            across: across,
            down: down,
            negatives: negatives
        )
        let url = stateFileURL
        try? FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true
        )
        if let data = try? JSONEncoder().encode(state) {
            try? data.write(to: url, options: .atomic)
        }
    }

    private func restoreState() {
        guard let data = try? Data(contentsOf: stateFileURL),
              let state = try? JSONDecoder().decode(PersistedState.self, from: data)
        else { return }
        let rollURL = URL(fileURLWithPath: state.rollPath)
        let captureFolder = URL(fileURLWithPath: state.captureFolder)
        guard FileManager.default.fileExists(atPath: rollURL.path),
              FileManager.default.fileExists(atPath: captureFolder.path)
        else {
            clearPersistedState()
            return
        }
        self.rollURL = rollURL
        self.captureFolder = captureFolder
        rigProfileID = state.rigProfileID
        across = state.across
        down = state.down
        negatives = state.negatives.map { entry in
            var copy = entry
            switch copy.step {
            case .stitching:
                copy.step = .waitingStitch
            case .preparing, .checking, .waitingCheck:
                copy.step = .waitingPrepare
            default:
                break
            }
            return copy
        }
        updateSleepAssertion()
        pump()
    }

    private func clearPersistedState() {
        try? FileManager.default.removeItem(at: stateFileURL)
    }

    private func updateSleepAssertion() {
        if hasWork, sleepAssertion == nil {
            sleepAssertion = ProcessInfo.processInfo.beginActivity(
                options: .idleSystemSleepDisabled,
                reason: "Scanny Boy stitch queue"
            )
        } else if !hasWork, let token = sleepAssertion {
            ProcessInfo.processInfo.endActivity(token)
            sleepAssertion = nil
        }
    }

    // MARK: - CLI helpers

    private struct CommandResult {
        var failed = false
        var insufficientDisk = false
        var code: CLICode?
        var message: String?
        var outputFilename: String?
    }

    private struct CheckResult {
        var passed = false
        var code: CLICode?
        var message: String?
    }

    private func runCommand(id: UUID, _ command: CLICommand) async -> CommandResult {
        var result = CommandResult()
        do {
            for await output in try await runner.session(for: command).start() {
                switch output {
                case .event(let event):
                    if event.kind == .error, let code = event.code {
                        result.failed = true
                        result.code = code
                        result.message = event.message
                        result.insufficientDisk = code == .insufficientDisk
                    } else if event.kind == .progress,
                        let completed = event.completed,
                        let total = event.total
                    {
                        let cappedCompleted = min(completed, total)
                        progress[id] = StepProgress(
                            completed: cappedCompleted,
                            total: total,
                            step: event.step,
                            startedAt: progress[id]?.startedAt ?? .now
                        )
                    } else if event.kind == .negativeDone {
                        result.outputFilename = event.output
                    }
                case .completed(let completion):
                    if completion.outcome != .success { result.failed = true }
                case .log, .failure:
                    result.failed = true
                }
            }
        } catch {
            result.failed = true
            result.message = error.localizedDescription
        }
        return result
    }

    private func runCaptureCheck(id: UUID, _ command: CLICommand) async -> CheckResult {
        var result = CheckResult()
        do {
            for await output in try await runner.session(for: command).start() {
                guard case .event(let event) = output else { continue }
                if event.kind == .captureChecked {
                    result.passed = event.captureCheckPassed ?? false
                    result.code = event.code
                    result.message = event.message
                } else if event.kind == .progress,
                    let completed = event.completed,
                    let total = event.total
                {
                    let cappedCompleted = min(completed, total)
                    progress[id] = StepProgress(
                        completed: cappedCompleted,
                        total: total,
                        step: event.step,
                        startedAt: progress[id]?.startedAt ?? .now
                    )
                } else if event.kind == .error, let code = event.code {
                    result.code = code
                    result.message = event.message
                }
            }
        } catch {
            result.message = error.localizedDescription
        }
        return result
    }
}
