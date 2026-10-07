import Foundation
import Observation

/// Background prepare → capture check → compose → commit queue.
///
/// Negatives of one roll compose (the order-independent half of a stitch)
/// several at a time, up to `parallelStitches`, then commit (publish) one at
/// a time in capture order. Different rolls commit concurrently, since their
/// locks differ (PARALLEL_STITCH_PLAN §3.7).
@MainActor
@Observable
final class StitchQueueModel {
    static let maxParallelPrepares = 2
    static let stateFilename = "stitch-queue.json"
    static let parallelStitchesKey = "com.lonniesmith.scanny-boy.parallelStitches"
    static let parallelStitchesChoices = 1...4
    static let defaultParallelStitches = 2

    enum Step: String, Codable, Sendable {
        case waitingPrepare
        case preparing
        case waitingCheck
        case checking
        case waitingStitch
        /// Composing the negative ahead of its commit (`stitch --compose-only`).
        case composing
        /// Composed (or its compose failed): waiting for its turn to publish.
        case waitingCommit
        /// The commit: `stitch` publishing the negative into the roll.
        case stitching
        case published
        case prepareFailed
        case checkFailed
        case stitchFailed
        case waitingForDisk

        /// Published or failed: nothing more will run for this entry until
        /// the user acts on it.
        var isTerminal: Bool {
            switch self {
            case .published, .prepareFailed, .checkFailed, .stitchFailed: true
            default: false
            }
        }

        /// Not yet composing: the entry has not started its compose, and a
        /// later negative of its roll must not start one ahead of it.
        var precedesCompose: Bool {
            switch self {
            case .waitingPrepare, .preparing, .waitingCheck, .checking, .waitingForDisk,
                .waitingStitch: true
            default: false
            }
        }
    }

    /// Where an entry belongs, snapshotted when it is enqueued. The queue
    /// outlives a roll switch (only a live capture session pins the
    /// sidebar), so an entry must never read the *current* roll, capture
    /// folder or grid — it would prepare and stitch against the wrong roll.
    struct EntryContext: Codable, Sendable {
        var rollPath: String
        var captureFolder: String
        var rigProfileID: String?
        var across: Int
        var down: Int

        var rollURL: URL { URL(fileURLWithPath: rollPath) }
        var captureFolderURL: URL { URL(fileURLWithPath: captureFolder) }
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
        /// Nil only for entries persisted before contexts existed; restore
        /// fills those in from the state file's header.
        var context: EntryContext?
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
    /// Fired after each stitch publishes. The negative is already in the roll
    /// manifest; only the deferred highlight-lock refresh is still to come.
    var onNegativePublished: (() -> Void)?
    private(set) var negatives: [QueuedNegative] = []
    private(set) var activePrepareCount = 0
    /// Normalized paths of the rolls with a commit running that has not yet
    /// published. A roll publishes one negative at a time, so it is in here
    /// from the start of its commit until `negative_published` (or the
    /// process exit, whichever comes first).
    private(set) var committingRolls: Set<String> = []
    /// Commits that have published their negative but whose process is still
    /// running its tail (scratch detection, edit seeds, previews, the
    /// end-of-run write), keyed by entry id, valued by normalized roll path.
    /// The negative is in the roll, but the process still holds the roll lock
    /// shared, so the queue is not drained and `roll refresh` cannot start.
    private(set) var tailingCommits: [UUID: String] = [:]
    private(set) var isRefreshing = false
    private(set) var progress: [UUID: StepProgress] = [:]

    private(set) var rollURL: URL?
    private var captureFolder: URL?
    private var rigProfileID: String?
    private var across: Int = 1
    private var down: Int = 1
    private var drainTask: Task<Void, Never>?
    private var sleepAssertion: NSObjectProtocol?
    /// Rolls a stitch published to with the roll refresh deferred; each is
    /// removed when `rollRefresh` runs for it.
    private var rollsNeedingRefresh: Set<String> = []
    private let defaults: UserDefaults
    private let stateDirectory: URL
    /// Compose sessions in flight, so Discard can cancel them.
    private var composeSessions: [UUID: CLISession] = [:]
    /// Entries that hit `ROLL_BUSY`, and the earliest time to try again.
    private var retryNotBefore: [UUID: Date] = [:]
    /// How long an entry that found its roll busy waits before it retries.
    var busyRetryDelay: Duration = .milliseconds(500)

    /// How many negatives are stitched at the same time. The queue caps
    /// concurrent composes, and a roll's composed-but-unpublished entries
    /// (see `startComposesIfNeeded`), at this value (PARALLEL_STITCH_PLAN
    /// §3.7–3.8). It is read each time the queue schedules work, so lowering
    /// it never cancels a running compose; it only stops new ones starting.
    /// Sticky across launches. The Capture tab's picker disables it while a
    /// capture sequence or the queue is busy.
    var parallelStitches: Int {
        didSet { defaults.set(parallelStitches, forKey: Self.parallelStitchesKey) }
    }

    init(
        runner: CLIRunner,
        defaults: UserDefaults = AppEnvironment.defaults,
        stateDirectory: URL = AppEnvironment.supportDirectory
    ) {
        self.runner = runner
        self.defaults = defaults
        self.stateDirectory = stateDirectory
        let stored = defaults.integer(forKey: Self.parallelStitchesKey)
        self.parallelStitches =
            Self.parallelStitchesChoices.contains(stored) ? stored : Self.defaultParallelStitches
        restoreState()
    }

    /// Entries still moving through prepare, check, compose or commit, and
    /// commit processes still finishing after their negative published. A
    /// failed entry is not work: it holds no roll lock and must not block the
    /// end-of-queue roll refresh.
    var hasWork: Bool {
        negatives.contains { !$0.step.isTerminal } || !tailingCommits.isEmpty
    }

    /// Everything not yet published, failures included — what Discard removes.
    var hasUnpublishedEntries: Bool {
        negatives.contains { $0.step != .published }
    }

    var publishedNegativeIDs: Set<UUID> {
        Set(negatives.filter { $0.step == .published }.map(\.id))
    }

    var isQueueBusy: Bool { hasWork || !committingRolls.isEmpty || isRefreshing }

    /// The entries that belong to `roll`, oldest first. The queue holds
    /// entries for every roll it has worked on, but a roll's Capture tab
    /// must only show its own.
    func negatives(for roll: URL?) -> [QueuedNegative] {
        guard let roll else { return [] }
        return negatives.filter { entry in
            entry.context.map { Self.isSameRoll($0.rollURL, roll) } ?? false
        }
    }

    func hasUnpublishedEntries(for roll: URL?) -> Bool {
        negatives(for: roll).contains { $0.step != .published }
    }

    func hasWork(on roll: URL) -> Bool {
        hasWork(onRollPath: Self.normalizedPath(roll))
    }

    /// Whether `path` (a normalized roll path) has an unfinished entry, a
    /// commit in flight, or a published commit's process still running.
    private func hasWork(onRollPath path: String) -> Bool {
        negatives.contains { entry in
            !entry.step.isTerminal
                && (entry.context.map { Self.normalizedPath($0.rollURL) == path } ?? false)
        }
            || committingRolls.contains(path)
            || tailingCommits.values.contains(path)
    }

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
        guard let rollURL, let captureFolder else { return }
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
            enqueuedAt: negative.startedAt,
            context: EntryContext(
                rollPath: rollURL.path,
                captureFolder: captureFolder.path,
                rigProfileID: rigProfileID,
                across: across,
                down: down
            )
        )
        negatives.append(entry)
        persistState()
        updateSleepAssertion()
        pump()
    }

    func endSession() {
        guard hasWork || !rollsNeedingRefresh.isEmpty else { return }
        drainTask = Task { await drainAndRefresh() }
    }

    /// Marks one queue entry published — for unit tests only.
    func testingMarkPublished(id: UUID) {
        guard let index = negatives.firstIndex(where: { $0.id == id }) else { return }
        negatives[index].step = .published
    }

    /// Removes every queue entry that has not published and returns paths to
    /// recycle (frame NEFs and work folders). With `roll`, only that roll's
    /// entries go; other rolls' queued work is left running.
    func discardUnpublished(for roll: URL? = nil) -> [URL] {
        let leavesOtherWork = roll.map { roll in
            negatives.contains { entry in
                guard !entry.step.isTerminal else { return false }
                return !(entry.context.map { Self.isSameRoll($0.rollURL, roll) } ?? false)
            }
        } ?? false
        if !leavesOtherWork {
            drainTask?.cancel()
            drainTask = nil
            isRefreshing = false
        }
        var urls: [URL] = []
        let remaining = negatives.filter { entry in
            guard entry.step != .published else { return true }
            if let roll, !(entry.context.map { Self.isSameRoll($0.rollURL, roll) } ?? false) {
                return true
            }
            urls.append(contentsOf: entry.framePaths.map { URL(fileURLWithPath: $0) })
            urls.append(URL(fileURLWithPath: entry.workFolder))
            progress.removeValue(forKey: entry.id)
            retryNotBefore.removeValue(forKey: entry.id)
            if let session = composeSessions.removeValue(forKey: entry.id) {
                Task { await session.cancel() }
            }
            return false
        }
        negatives = remaining
        if !leavesOtherWork { activePrepareCount = 0 }
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
        // Commits before composes: a commit leaving `waitingCommit` frees a
        // slot in its roll's composed-ahead bound.
        startCommitsIfNeeded()
        startComposesIfNeeded()
    }

    private func mutateEntry(id: UUID, _ mutate: (inout QueuedNegative) -> Void) {
        guard let index = negatives.firstIndex(where: { $0.id == id }) else { return }
        mutate(&negatives[index])
    }

    private func startPreparesIfNeeded() {
        guard activePrepareCount < Self.maxParallelPrepares else { return }
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
                // `runPrepare` carries the entry through its check and leaves
                // it at its outcome — nothing here may overwrite `step`.
                await runPrepare(id: id)
                activePrepareCount = max(0, activePrepareCount - 1)
                pump()
                // A failed prepare or check can be the last thing the queue
                // was waiting on.
                await finishDrainIfNeeded()
            }
        }
    }

    private func runPrepare(id: UUID) async {
        guard let entry = negatives.first(where: { $0.id == id }),
              let context = entry.context
        else { return }
        let files = entry.framePaths.map { URL(fileURLWithPath: $0).lastPathComponent }
        let work = URL(fileURLWithPath: entry.workFolder)
        try? FileManager.default.createDirectory(at: work, withIntermediateDirectories: true)
        let command = CLICommand.prepare(
            input: context.captureFolderURL,
            files: files,
            out: work,
            across: context.across,
            down: context.down,
            rig: context.rigProfileID
        )
        let result = await runCommand(id: id, command)
        progress.removeValue(forKey: id)
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
        await runCheck(id: id)
        persistState()
        pump()
    }

    private func runCheck(id: UUID) async {
        guard let context = negatives.first(where: { $0.id == id })?.context else { return }
        mutateEntry(id: id) { $0.step = .checking }
        progress[id] = StepProgress(
            completed: 0, total: 0, step: nil, startedAt: .now
        )
        let work = URL(fileURLWithPath: negatives.first(where: { $0.id == id })!.workFolder)
        let command = CLICommand.captureCheck(work: work, rig: context.rigProfileID)
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

    // MARK: - Compose

    /// Starts composes for entries waiting on one, in capture order.
    ///
    /// A compose is the order-independent half of a stitch, so several run at
    /// once. An entry starts when
    /// - no earlier entry of its roll is still short of composing (still
    ///   preparing, checking, waiting for disk, or waiting for its own turn),
    ///   so a roll's composes start in capture order and an early negative
    ///   never waits behind a later one's compose,
    /// - fewer than `parallelStitches` composes are running in all, and
    /// - fewer than `parallelStitches` entries of its roll are composing or
    ///   composed and waiting to commit, which bounds the artifacts on disk
    ///   to the same number. The entry next to publish on its roll (the
    ///   earliest unfinished one) is exempt from this bound, so a bound full
    ///   of later negatives, which a restored queue can hold, can never
    ///   starve the one they are waiting for.
    ///
    /// `parallelStitches` is read here, at scheduling time.
    private func startComposesIfNeeded() {
        // `roll refresh` holds the roll lock exclusively; a compose started
        // now would fail ROLL_BUSY.
        guard !isRefreshing else { return }
        let limit = parallelStitches
        var composing = 0
        var composedAhead: [String: Int] = [:]
        for entry in negatives {
            guard let context = entry.context else { continue }
            if entry.step == .composing { composing += 1 }
            if entry.step == .composing || entry.step == .waitingCommit {
                composedAhead[Self.normalizedPath(context.rollURL), default: 0] += 1
            }
        }
        // Rolls with an earlier entry still short of composing, and rolls
        // with any earlier entry not yet finished.
        var heldRolls: Set<String> = []
        var openRolls: Set<String> = []
        for entry in negatives {
            guard let context = entry.context else { continue }
            let roll = Self.normalizedPath(context.rollURL)
            defer { if !entry.step.isTerminal { openRolls.insert(roll) } }
            guard entry.step == .waitingStitch else {
                if entry.step.precedesCompose { heldRolls.insert(roll) }
                continue
            }
            guard !heldRolls.contains(roll) else { continue }
            // The entry next to publish on its roll is exempt from the
            // per-roll bound, so a bound full of later negatives (a restored
            // queue can hold them) can never starve the one they wait for.
            let underRollBound = composedAhead[roll, default: 0] < limit || !openRolls.contains(roll)
            guard composing < limit, underRollBound, isRetryDue(entry.id) else {
                // It has to wait, so nothing behind it on its roll may start.
                heldRolls.insert(roll)
                continue
            }
            startCompose(id: entry.id, context: context, workFolder: entry.workFolder)
            composing += 1
            composedAhead[roll, default: 0] += 1
        }
    }

    private func startCompose(id: UUID, context: EntryContext, workFolder: String) {
        mutateEntry(id: id) { $0.step = .composing }
        progress[id] = StepProgress(completed: 0, total: 0, step: nil, startedAt: .now)
        persistState()
        Task {
            await runCompose(id: id, context: context, workFolder: workFolder)
            pump()
            await finishDrainIfNeeded()
        }
    }

    private func runCompose(id: UUID, context: EntryContext, workFolder: String) async {
        // Discarded between scheduling and this task running.
        guard negatives.contains(where: { $0.id == id }) else { return }
        let command = CLICommand.stitch(
            work: URL(fileURLWithPath: workFolder),
            roll: context.rollURL,
            rig: context.rigProfileID,
            composeOnly: true
        )
        let result = await runCommand(
            id: id, command,
            onSession: { [weak self] session in self?.composeSessions[id] = session }
        )
        composeSessions.removeValue(forKey: id)
        progress.removeValue(forKey: id)
        guard negatives.contains(where: { $0.id == id }) else { return }
        if result.failed, result.code == .rollBusy {
            // Something else holds the roll; try again shortly.
            mutateEntry(id: id) { $0.step = .waitingStitch }
            scheduleRetry(for: id)
        } else {
            // Success, or a failure the commit will redo and report itself:
            // it recomposes when the artifact is missing, so a failed compose
            // is never fatal here.
            mutateEntry(id: id) { $0.step = .waitingCommit }
        }
        persistState()
    }

    // MARK: - Commit

    /// Starts commits for entries that are composed and next in line.
    ///
    /// A commit publishes into the roll, so one roll commits one negative at a
    /// time, in capture order: an entry waits while any earlier entry of its
    /// roll is still short of published or failed (TETHER_PLAN §4.1). Different
    /// rolls commit concurrently, since their locks differ. The roll frees up
    /// for the next commit when the running one emits `negative_published`,
    /// not when its process exits.
    private func startCommitsIfNeeded() {
        // `roll refresh` holds the roll lock; a commit started now would fail.
        guard !isRefreshing else { return }
        for index in negatives.indices {
            let entry = negatives[index]
            guard entry.step == .waitingCommit, let context = entry.context,
                  isRetryDue(entry.id)
            else { continue }
            let roll = Self.normalizedPath(context.rollURL)
            guard !committingRolls.contains(roll) else { continue }
            let blocked = negatives[..<index].contains { earlier in
                !earlier.step.isTerminal
                    && (earlier.context.map { Self.normalizedPath($0.rollURL) == roll } ?? true)
            }
            guard !blocked else { continue }
            startCommit(id: entry.id, context: context, workFolder: entry.workFolder)
        }
    }

    private func startCommit(id: UUID, context: EntryContext, workFolder: String) {
        let roll = Self.normalizedPath(context.rollURL)
        committingRolls.insert(roll)
        mutateEntry(id: id) { $0.step = .stitching }
        progress[id] = StepProgress(completed: 0, total: 0, step: nil, startedAt: .now)
        persistState()
        Task {
            await runCommit(id: id, context: context, workFolder: URL(fileURLWithPath: workFolder))
            pump()
            await finishDrainIfNeeded()
        }
    }

    private func runCommit(id: UUID, context: EntryContext, workFolder: URL) async {
        let roll = Self.normalizedPath(context.rollURL)
        let command = CLICommand.stitch(
            work: workFolder, roll: context.rollURL, rig: context.rigProfileID,
            deferRollRefresh: true
        )
        let result = await runCommand(
            id: id, command,
            onEvent: { [weak self] event in
                guard event.kind == .negativePublished else { return }
                self?.commitPublished(id: id, roll: roll, output: event.output)
            }
        )
        progress.removeValue(forKey: id)
        let published = tailingCommits.removeValue(forKey: id) != nil
        // A published commit freed the roll at `negative_published`, and the
        // roll's next commit may hold it now; only an unpublished exit owns it.
        if !published { committingRolls.remove(roll) }
        if published {
            // In the roll whatever the exit status says; only the tail failed.
            try? FileManager.default.removeItem(at: workFolder)
        } else if negatives.contains(where: { $0.id == id }) {
            if result.failed, result.code == .rollBusy {
                // The publish lock was held: nothing was written. Retry.
                mutateEntry(id: id) { $0.step = .waitingCommit }
                scheduleRetry(for: id)
            } else if result.failed {
                mutateEntry(id: id) {
                    $0.step = .stitchFailed
                    $0.failureCode = result.code?.name
                    $0.failureMessage = result.message
                }
            } else {
                // A CLI that does not emit `negative_published`: success at
                // exit is the publish.
                markPublished(id: id, roll: roll, output: result.outputFilename)
                try? FileManager.default.removeItem(at: workFolder)
            }
        }
        persistState()
    }

    /// The commit's `negative_published` event: the negative is in the roll.
    /// Frees the roll for its next commit while this process runs its tail.
    private func commitPublished(id: UUID, roll: String, output: String?) {
        tailingCommits[id] = roll
        markPublished(id: id, roll: roll, output: output)
        committingRolls.remove(roll)
        persistState()
        pump()
    }

    private func markPublished(id: UUID, roll: String, output: String?) {
        if negatives.contains(where: { $0.id == id }) {
            mutateEntry(id: id) {
                $0.step = .published
                $0.publishedAt = Date()
                $0.outputFilename = output
            }
        }
        rollsNeedingRefresh.insert(roll)
        onNegativePublished?()
    }

    // MARK: - Retry

    private func isRetryDue(_ id: UUID) -> Bool {
        guard let date = retryNotBefore[id] else { return true }
        return date <= Date()
    }

    /// Holds `id` back for `busyRetryDelay`, then pumps. The delay also
    /// keeps the completion's own `pump()` from respawning the process at once.
    private func scheduleRetry(for id: UUID) {
        let delay = busyRetryDelay
        retryNotBefore[id] = Date().addingTimeInterval(
            Double(delay.components.seconds) + Double(delay.components.attoseconds) / 1e18
        )
        Task {
            try? await Task.sleep(for: delay)
            retryNotBefore.removeValue(forKey: id)
            pump()
        }
    }

    private func drainAndRefresh() async {
        while hasWork {
            pump()
            try? await Task.sleep(for: .milliseconds(200))
        }
        await finishDrainIfNeeded()
    }

    /// Runs the refresh this queue's publishes deferred, for each roll that
    /// has nothing left in flight: no unfinished entry, no commit, and no
    /// published commit's process still running. Failed entries do not hold
    /// it back.
    private func finishDrainIfNeeded() async {
        updateSleepAssertion()
        for path in rollsNeedingRefresh.sorted() where !hasWork(onRollPath: path) {
            await rollRefresh(roll: URL(fileURLWithPath: path))
        }
    }

    /// TETHER_PLAN §4.4: brings a roll whose refresh was deferred up to date
    /// when its Edit or Export tab opens. Skipped while this queue is still
    /// working on that roll — its own end-of-queue refresh covers it.
    func refreshDeferredRoll(_ roll: URL) async {
        if hasWork(on: roll) { return }
        await rollRefresh(roll: roll)
    }

    func rollRefresh(roll: URL) async {
        guard !isRefreshing else { return }
        isRefreshing = true
        let refreshedQueuedRoll = rollsNeedingRefresh.remove(Self.normalizedPath(roll)) != nil
        let isQueueRoll = refreshedQueuedRoll || (rollURL.map { Self.isSameRoll($0, roll) } ?? false)
        // rollRefresh is not tied to a specific negative; pass a dummy id.
        _ = await runCommand(id: UUID(), .rollRefresh(roll: roll))
        // Only this queue's roll owns `stitch-queue.json`, and failed entries
        // stay saved until they are discarded.
        if isQueueRoll {
            if hasUnpublishedEntries { persistState() } else { clearPersistedState() }
        }
        isRefreshing = false
        onRollUpdated?()
        pump()
    }

    private static func isSameRoll(_ lhs: URL, _ rhs: URL) -> Bool {
        normalizedPath(lhs) == normalizedPath(rhs)
    }

    private static func normalizedPath(_ url: URL) -> String {
        url.standardizedFileURL.path
    }

    // MARK: - Persistence

    private var stateFileURL: URL {
        stateDirectory.appending(path: Self.stateFilename)
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
            copy.context = entry.context ?? EntryContext(
                rollPath: state.rollPath,
                captureFolder: state.captureFolder,
                rigProfileID: state.rigProfileID,
                across: state.across,
                down: state.down
            )
            switch copy.step {
            case .composing:
                copy.step = .waitingStitch
            case .waitingCommit, .stitching:
                // The commit falls back to a full stitch if the compose
                // artifact is gone or stale.
                copy.step = .waitingCommit
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

    /// Runs `command` to completion. `onSession` receives the session before
    /// it starts, so a caller can cancel it; `onEvent` sees every event as it
    /// arrives, before the command has finished.
    private func runCommand(
        id: UUID,
        _ command: CLICommand,
        onSession: ((CLISession) -> Void)? = nil,
        onEvent: ((CLIEvent) -> Void)? = nil
    ) async -> CommandResult {
        var result = CommandResult()
        do {
            let session = runner.session(for: command)
            onSession?(session)
            for await output in try await session.start() {
                switch output {
                case .event(let event):
                    onEvent?(event)
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
