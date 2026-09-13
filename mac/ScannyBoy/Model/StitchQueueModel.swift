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
    private(set) var negatives: [QueuedNegative] = []
    private(set) var activePrepareCount = 0
    private(set) var isStitching = false
    private(set) var isRefreshing = false

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

    private func pump() {
        startPreparesIfNeeded()
        startNextStitchIfNeeded()
    }

    private func startPreparesIfNeeded() {
        guard activePrepareCount < Self.maxParallelPrepares else { return }
        guard let captureFolder else { return }
        let waiting = negatives.filter { $0.step == .waitingPrepare || $0.step == .waitingForDisk }
        for entry in waiting.prefix(Self.maxParallelPrepares - activePrepareCount) {
            guard let index = negatives.firstIndex(where: { $0.id == entry.id }) else { continue }
            negatives[index].step = .preparing
            activePrepareCount += 1
            let id = entry.id
            Task {
                await runPrepare(
                    index: index,
                    captureFolder: captureFolder,
                    rigProfileID: rigProfileID
                )
                activePrepareCount -= 1
                if let idx = negatives.firstIndex(where: { $0.id == id }) {
                    negatives[idx].step = .waitingCheck
                    pump()
                }
            }
        }
    }

    private func runPrepare(index: Int, captureFolder: URL, rigProfileID: String?) async {
        let entry = negatives[index]
        let files = entry.framePaths.map { URL(fileURLWithPath: $0).lastPathComponent }
        let work = URL(fileURLWithPath: entry.workFolder)
        let command = CLICommand.prepare(
            input: captureFolder,
            files: files,
            out: work,
            across: across,
            down: down,
            rig: rigProfileID
        )
        let result = await runCommand(command)
        guard negatives.indices.contains(index) else { return }
        if result.insufficientDisk {
            negatives[index].step = .waitingForDisk
            persistState()
            return
        }
        if result.failed {
            negatives[index].step = .prepareFailed
            negatives[index].failureCode = result.code?.name
            negatives[index].failureMessage = result.message
            persistState()
            return
        }
        negatives[index].step = .waitingCheck
        await runCheck(index: index, rigProfileID: rigProfileID)
        persistState()
        pump()
    }

    private func runCheck(index: Int, rigProfileID: String?) async {
        negatives[index].step = .checking
        let work = URL(fileURLWithPath: negatives[index].workFolder)
        let command = CLICommand.captureCheck(work: work, rig: rigProfileID)
        let result = await runCaptureCheck(command)
        guard negatives.indices.contains(index) else { return }
        if result.passed {
            negatives[index].step = .waitingStitch
        } else {
            negatives[index].step = .checkFailed
            negatives[index].failureCode = result.code?.name
            negatives[index].failureMessage = result.message
        }
        persistState()
        pump()
    }

    private func startNextStitchIfNeeded() {
        guard !isStitching, let rollURL else { return }
        guard negatives.allSatisfy({ $0.step != .preparing && $0.step != .checking }) else { return }
        guard let index = negatives.firstIndex(where: { $0.step == .waitingStitch }) else { return }
        isStitching = true
        negatives[index].step = .stitching
        let work = URL(fileURLWithPath: negatives[index].workFolder)
        Task {
            let command = CLICommand.stitch(
                work: work, roll: rollURL, rig: rigProfileID, deferRollRefresh: true
            )
            let result = await runCommand(command)
            if negatives.indices.contains(index) {
                if result.failed {
                    negatives[index].step = .stitchFailed
                    negatives[index].failureCode = result.code?.name
                    negatives[index].failureMessage = result.message
                } else {
                    negatives[index].step = .published
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
        _ = await runCommand(.rollRefresh(roll: roll))
        clearPersistedState()
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
            if copy.step == .preparing || copy.step == .checking || copy.step == .stitching {
                copy.step = copy.step == .stitching ? .waitingStitch : .waitingPrepare
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
    }

    private struct CheckResult {
        var passed = false
        var code: CLICode?
        var message: String?
    }

    private func runCommand(_ command: CLICommand) async -> CommandResult {
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

    private func runCaptureCheck(_ command: CLICommand) async -> CheckResult {
        var result = CheckResult()
        do {
            for await output in try await runner.session(for: command).start() {
                guard case .event(let event) = output else { continue }
                if event.kind == .captureChecked {
                    result.passed = event.captureCheckPassed ?? false
                    result.code = event.code
                    result.message = event.message
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
