import Foundation
import Testing

@testable import ScannyBoy

@Suite("StitchQueueModel")
@MainActor
struct StitchQueueModelTests {
    private static func makeExecutable(in directory: URL, script: String) throws -> URL {
        try TestSupport.writeTestExecutable(script, in: directory)
    }

    private static func makeTemporaryDirectory() throws -> URL {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "scanny-boy-tests", directoryHint: .isDirectory)
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    // MARK: - Fake CLI

    private static func event(_ json: String) -> String {
        "echo '\(TestEvents.line(json))'"
    }

    private static let started = event(#"{"event":"started","command":"stitch"}"#)
    private static let finishedOK = event(#"{"event":"finished","status":"success","exit_status":0}"#)
    private static let finishedFailed = event(#"{"event":"finished","status":"failed","exit_status":1}"#)
    private static let published = event(
        #"{"event":"negative_published","negative_id":"n1","output":"out.tif"}"#
    )

    private static func errorEvent(_ code: String) -> String {
        event(#"{"event":"error","code":"\#(code)","message":"scripted \#(code)"}"#)
    }

    /// A successful compose: logs its start and end around `seconds` of work.
    private static func composeOK(seconds: Double) -> String {
        """
        \(started)
        echo "compose-start $name" >> "$LOG"
        sleep \(seconds)
        echo "compose-end $name" >> "$LOG"
        \(event(#"{"event":"negative_composed","group_id":"g","width":1,"height":1,"artifact_bytes":1}"#))
        \(finishedOK)
        exit 0
        """
    }

    /// A compose that fails with `code` after logging its start and end.
    private static func composeFails(_ code: String) -> String {
        """
        \(started)
        echo "compose-start $name" >> "$LOG"
        echo "compose-end $name" >> "$LOG"
        \(errorEvent(code))
        \(finishedFailed)
        exit 1
        """
    }

    /// A commit that works for `seconds`, publishes, then spends `tail`
    /// seconds on its post-publish work before it exits.
    private static func commitOK(
        seconds: Double = 0, tail: Double = 0, exitFailed: Bool = false
    ) -> String {
        """
        \(started)
        echo "commit-start $name" >> "$LOG"
        sleep \(seconds)
        echo "commit-published $name" >> "$LOG"
        \(published)
        sleep \(tail)
        echo "commit-end $name" >> "$LOG"
        \(exitFailed ? "\(errorEvent("STITCH_FAILED"))\n\(finishedFailed)\nexit 1" : "\(finishedOK)\nexit 0")
        """
    }

    /// A commit that finds the publish lock held: fails `ROLL_BUSY` with
    /// nothing written.
    private static func commitRollBusy() -> String {
        """
        \(started)
        echo "commit-start $name" >> "$LOG"
        echo "commit-busy $name" >> "$LOG"
        \(errorEvent("ROLL_BUSY"))
        \(finishedFailed)
        exit 1
        """
    }

    /// A fake CLI. Prepares and checks succeed at once. `compose` and
    /// `commit` are shell snippets (with `$name` set to the stamp and `$LOG`
    /// to the log file) that run for `stitch --compose-only` and `stitch`.
    /// Every `roll refresh` is logged as `refresh`.
    private static func queueScript(log: URL, compose: String, commit: String) -> String {
        """
        LOG='\(log.path)'
        if [ "$1" = "capture" ]; then
          \(event(#"{"event":"started","command":"capture check"}"#))
          \(event(#"{"event":"capture_checked","passed":true,"code":null,"message":null,"global_rms_px":1.0,"used_clahe_fallback":false}"#))
          \(finishedOK)
          exit 0
        fi
        if [ "$1" = "roll" ]; then
          echo "refresh" >> "$LOG"
          \(event(#"{"event":"started","command":"roll refresh"}"#))
          \(finishedOK)
          exit 0
        fi
        if [ "$1" = "stitch" ]; then
          name=$(basename "$3")
          case "$*" in
            *--compose-only*)
              \(compose)
              ;;
          esac
          \(commit)
        fi
        \(event(#"{"event":"started","command":"prepare"}"#))
        \(finishedOK)
        """
    }

    private static func makeQueue(
        script: String, in directory: URL, parallel: Int = 2
    ) throws -> StitchQueueModel {
        let executable = try makeExecutable(in: directory, script: script)
        let defaults = scratchDefaults()
        defaults.set(parallel, forKey: StitchQueueModel.parallelStitchesKey)
        let queue = StitchQueueModel(
            runner: CLIRunner(executable: executable),
            defaults: defaults,
            stateDirectory: directory.appending(path: "support", directoryHint: .isDirectory)
        )
        queue.busyRetryDelay = .milliseconds(150)
        return queue
    }

    /// Configures `queue` for the roll folder `roll` under `directory` and
    /// enqueues one negative per stamp.
    private static func enqueue(
        _ stamps: [String], on queue: StitchQueueModel, in directory: URL, roll: String = "roll"
    ) {
        let captureFolder = directory.appending(path: "capture-\(roll)", directoryHint: .isDirectory)
        try? FileManager.default.createDirectory(at: captureFolder, withIntermediateDirectories: true)
        queue.configure(
            roll: directory.appending(path: roll, directoryHint: .isDirectory),
            captureFolder: captureFolder,
            across: 1,
            down: 1
        )
        for stamp in stamps {
            queue.enqueue(
                CaptureSessionModel.CompletedNegative(
                    id: UUID(), stamp: stamp, frameURLs: [], startedAt: Date()
                )
            )
        }
    }

    private static func waitFor(
        timeout: Double = 10, _ condition: () -> Bool
    ) async -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if condition() { return true }
            try? await Task.sleep(for: .milliseconds(20))
        }
        return condition()
    }

    private static func logLines(_ log: URL) -> [String] {
        ((try? String(contentsOf: log, encoding: .utf8)) ?? "")
            .split(separator: "\n").map(String.init)
    }

    /// The most `start` lines that were open at once, given `end` lines that
    /// close them.
    private static func maxOverlap(_ lines: [String], start: String, end: String) -> Int {
        var open = 0
        var most = 0
        for line in lines {
            if line.hasPrefix(start) { open += 1; most = max(most, open) }
            if line.hasPrefix(end) { open -= 1 }
        }
        return most
    }

    private static func steps(_ queue: StitchQueueModel, _ step: StitchQueueModel.Step) -> Int {
        queue.negatives.filter { $0.step == step }.count
    }

    @Test("prepare concurrency stays at cap")
    func prepareCap() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let script = """
            echo '\(TestEvents.line(#"{"event":"started","command":"prepare"}"#))'
            sleep 0.3
            echo '\(TestEvents.line(#"{"event":"finished","status":"success","exit_status":0}"#))'
            """
        let executable = try Self.makeExecutable(in: directory, script: script)
        let runner = CLIRunner(executable: executable)
        let queue = StitchQueueModel(runner: runner, stateDirectory: directory.appending(path: "support", directoryHint: .isDirectory))
        let captureFolder = directory.appending(path: "capture", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: captureFolder, withIntermediateDirectories: true)
        queue.configure(
            roll: directory.appending(path: "roll", directoryHint: .isDirectory),
            captureFolder: captureFolder,
            rigProfileID: "rig-1",
            across: 2,
            down: 1
        )
        for index in 0..<3 {
            queue.enqueue(
                CaptureSessionModel.CompletedNegative(
                    id: UUID(),
                    stamp: "neg-\(index)",
                    frameURLs: [captureFolder.appending(path: "a.NEF")],
                    startedAt: Date()
                )
            )
        }
        try await Task.sleep(for: .milliseconds(100))
        #expect(queue.activePrepareCount <= StitchQueueModel.maxParallelPrepares)
    }

    @Test("a lone negative is composed first, then committed")
    func composeThenCommit() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let log = directory.appending(path: "queue.log")
        let script = Self.queueScript(
            log: log, compose: Self.composeOK(seconds: 0.2), commit: Self.commitOK(seconds: 0.1)
        )
        let queue = try Self.makeQueue(script: script, in: directory)
        Self.enqueue(["a"], on: queue, in: directory)
        #expect(await Self.waitFor { queue.negatives.first?.step == .composing })
        #expect(await Self.waitFor { queue.negatives.first?.step == .published })
        #expect(Self.logLines(log).filter { $0.hasPrefix("compose-start") || $0.hasPrefix("commit-start") }
            == ["compose-start a", "commit-start a"])
    }

    @Test("a checked negative waiting behind a compose is not reset to waitingCheck")
    func checkedNegativeKeepsWaitingStitch() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let log = directory.appending(path: "queue.log")
        let script = Self.queueScript(
            log: log, compose: Self.composeOK(seconds: 3), commit: Self.commitOK()
        )
        // One compose at a time, so two checked negatives have to wait.
        let queue = try Self.makeQueue(script: script, in: directory, parallel: 1)
        Self.enqueue(["neg-0", "neg-1", "neg-2"], on: queue, in: directory)
        let expected: [StitchQueueModel.Step] = [.composing, .waitingStitch, .waitingStitch]
        #expect(await Self.waitFor { queue.negatives.map(\.step) == expected })
        try await Task.sleep(for: .milliseconds(500))
        #expect(queue.negatives.map(\.step) == expected)
        _ = queue.discardUnpublished()
    }

    @Test("each publish notifies at once, and a failed check does not block the roll refresh")
    func publishNotifiesAndFailureDoesNotBlockRefresh() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let script = """
            if [ "$1" = "capture" ]; then
              echo '\(TestEvents.line(#"{"event":"started","command":"capture check"}"#))'
              case "$*" in
                *bad*) echo '\(TestEvents.line(#"{"event":"capture_checked","passed":false,"code":"STITCH_UNDERCONSTRAINED","message":"too little overlap","global_rms_px":null,"used_clahe_fallback":false}"#))' ;;
                *) echo '\(TestEvents.line(#"{"event":"capture_checked","passed":true,"code":null,"message":null,"global_rms_px":1.0,"used_clahe_fallback":false}"#))' ;;
              esac
              echo '\(TestEvents.line(#"{"event":"finished","status":"success","exit_status":0}"#))'
              exit 0
            fi
            if [ "$1" = "stitch" ]; then
              echo '\(TestEvents.line(#"{"event":"started","command":"stitch"}"#))'
              sleep 0.3
              echo '\(TestEvents.line(#"{"event":"negative_done","negative_id":"n1","output":"out.tif","width":1,"height":1,"global_rms_px":1.0,"max_overlap_mad":1.0}"#))'
              echo '\(TestEvents.line(#"{"event":"finished","status":"success","exit_status":0}"#))'
              exit 0
            fi
            echo '\(TestEvents.line(#"{"event":"started","command":"prepare"}"#))'
            echo '\(TestEvents.line(#"{"event":"finished","status":"success","exit_status":0}"#))'
            """
        let executable = try Self.makeExecutable(in: directory, script: script)
        let queue = StitchQueueModel(runner: CLIRunner(executable: executable), stateDirectory: directory.appending(path: "support", directoryHint: .isDirectory))
        var notifications: [String] = []
        queue.onNegativePublished = { notifications.append("published") }
        queue.onRollUpdated = { notifications.append("rollUpdated") }
        let captureFolder = directory.appending(path: "capture", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: captureFolder, withIntermediateDirectories: true)
        queue.configure(
            roll: directory.appending(path: "roll", directoryHint: .isDirectory),
            captureFolder: captureFolder,
            across: 1,
            down: 1
        )
        for stamp in ["good-1", "bad", "good-2"] {
            queue.enqueue(
                CaptureSessionModel.CompletedNegative(
                    id: UUID(), stamp: stamp, frameURLs: [], startedAt: Date()
                )
            )
        }
        #expect(await Self.waitFor { !queue.isQueueBusy && notifications.contains("rollUpdated") })
        #expect(queue.negatives.map(\.step) == [.published, .checkFailed, .published])
        #expect(notifications == ["published", "published", "rollUpdated"])
        #expect(!queue.hasWork)
        #expect(queue.hasUnpublishedEntries)
        _ = queue.discardUnpublished()
    }

    /// A fake CLI whose checks pass, whose prepares take `slowPrepare`
    /// seconds for negatives stamped `slow*` (instantly otherwise), and whose
    /// composes and commits take `stitch` seconds each and append
    /// `compose <stamp>` or `commit <stamp>` to `stitchLog`.
    private static func orderingScript(
        slowPrepare: Double, stitch: Double, stitchLog: URL
    ) -> String {
        """
        if [ "$1" = "capture" ]; then
          echo '\(TestEvents.line(#"{"event":"started","command":"capture check"}"#))'
          echo '\(TestEvents.line(#"{"event":"capture_checked","passed":true,"code":null,"message":null,"global_rms_px":1.0,"used_clahe_fallback":false}"#))'
          echo '\(TestEvents.line(#"{"event":"finished","status":"success","exit_status":0}"#))'
          exit 0
        fi
        if [ "$1" = "stitch" ]; then
          echo '\(TestEvents.line(#"{"event":"started","command":"stitch"}"#))'
          case "$*" in
            *--compose-only*) echo "compose $(basename "$3")" >> '\(stitchLog.path)' ;;
            *) echo "commit $(basename "$3")" >> '\(stitchLog.path)' ;;
          esac
          sleep \(stitch)
          echo '\(TestEvents.line(#"{"event":"negative_done","negative_id":"n1","output":"out.tif","width":1,"height":1,"global_rms_px":1.0,"max_overlap_mad":1.0}"#))'
          echo '\(TestEvents.line(#"{"event":"finished","status":"success","exit_status":0}"#))'
          exit 0
        fi
        echo '\(TestEvents.line(#"{"event":"started","command":"prepare"}"#))'
        case "$*" in
          */.work/slow*) sleep \(slowPrepare) ;;
        esac
        echo '\(TestEvents.line(#"{"event":"finished","status":"success","exit_status":0}"#))'
        """
    }

    @Test("a checked negative composes while a later negative is still preparing")
    func stitchStartsAheadOfLaterPrepare() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let script = Self.orderingScript(
            slowPrepare: 3, stitch: 3, stitchLog: directory.appending(path: "stitches.log")
        )
        let executable = try Self.makeExecutable(in: directory, script: script)
        let queue = StitchQueueModel(runner: CLIRunner(executable: executable), stateDirectory: directory.appending(path: "support", directoryHint: .isDirectory))
        let captureFolder = directory.appending(path: "capture", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: captureFolder, withIntermediateDirectories: true)
        queue.configure(
            roll: directory.appending(path: "roll", directoryHint: .isDirectory),
            captureFolder: captureFolder,
            across: 1,
            down: 1
        )
        for stamp in ["fast", "slow"] {
            queue.enqueue(
                CaptureSessionModel.CompletedNegative(
                    id: UUID(), stamp: stamp, frameURLs: [], startedAt: Date()
                )
            )
        }
        // Long enough for the first negative's prepare and check, well short
        // of the second's prepare.
        try await Task.sleep(for: .milliseconds(1000))
        #expect(queue.negatives.map(\.step) == [.composing, .preparing])
        _ = queue.discardUnpublished()
    }

    @Test("a later negative does not compose or commit ahead of an earlier one on its roll")
    func laterNegativeWaitsForEarlierSameRoll() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let stitchLog = directory.appending(path: "stitches.log")
        let script = Self.orderingScript(slowPrepare: 1, stitch: 0.2, stitchLog: stitchLog)
        let executable = try Self.makeExecutable(in: directory, script: script)
        let queue = StitchQueueModel(runner: CLIRunner(executable: executable), stateDirectory: directory.appending(path: "support", directoryHint: .isDirectory))
        let captureFolder = directory.appending(path: "capture", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: captureFolder, withIntermediateDirectories: true)
        queue.configure(
            roll: directory.appending(path: "roll", directoryHint: .isDirectory),
            captureFolder: captureFolder,
            across: 1,
            down: 1
        )
        for stamp in ["slow", "fast"] {
            queue.enqueue(
                CaptureSessionModel.CompletedNegative(
                    id: UUID(), stamp: stamp, frameURLs: [], startedAt: Date()
                )
            )
        }
        // The second negative is checked; the first is still preparing, so
        // the second waits: composes start in capture order on a roll.
        try await Task.sleep(for: .milliseconds(700))
        #expect(queue.negatives.map(\.step) == [.preparing, .waitingStitch])
        #expect(queue.committingRolls.isEmpty)
        #expect(await Self.waitFor { queue.negatives.map(\.step) == [.published, .published] })
        let order = try String(contentsOf: stitchLog, encoding: .utf8)
            .split(separator: "\n").map(String.init)
        #expect(order.filter { $0.hasPrefix("commit") } == ["commit slow", "commit fast"])
    }

    @Test("a negative on another roll does not wait for this roll's prepares")
    func otherRollDoesNotBlockStitch() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let script = Self.orderingScript(
            slowPrepare: 3, stitch: 3, stitchLog: directory.appending(path: "stitches.log")
        )
        let executable = try Self.makeExecutable(in: directory, script: script)
        let queue = StitchQueueModel(runner: CLIRunner(executable: executable), stateDirectory: directory.appending(path: "support", directoryHint: .isDirectory))
        let folderA = directory.appending(path: "captureA", directoryHint: .isDirectory)
        let folderB = directory.appending(path: "captureB", directoryHint: .isDirectory)
        for url in [folderA, folderB] {
            try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        }
        queue.configure(
            roll: directory.appending(path: "rollA", directoryHint: .isDirectory),
            captureFolder: folderA, across: 1, down: 1
        )
        queue.enqueue(
            CaptureSessionModel.CompletedNegative(
                id: UUID(), stamp: "slow", frameURLs: [], startedAt: Date()
            )
        )
        queue.configure(
            roll: directory.appending(path: "rollB", directoryHint: .isDirectory),
            captureFolder: folderB, across: 1, down: 1
        )
        queue.enqueue(
            CaptureSessionModel.CompletedNegative(
                id: UUID(), stamp: "fast", frameURLs: [], startedAt: Date()
            )
        )
        try await Task.sleep(for: .milliseconds(1000))
        #expect(queue.negatives.map(\.step) == [.preparing, .composing])
        _ = queue.discardUnpublished()
    }

    @Test("discardUnpublished removes queue entries and returns their paths")
    func discardUnpublished() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/false"))
        let queue = StitchQueueModel(runner: runner, stateDirectory: directory.appending(path: "support", directoryHint: .isDirectory))
        let captureFolder = directory.appending(path: "capture", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: captureFolder, withIntermediateDirectories: true)
        let frame = captureFolder.appending(path: "scan.NEF")
        FileManager.default.createFile(atPath: frame.path, contents: Data([0x01]))
        queue.configure(
            roll: directory.appending(path: "roll", directoryHint: .isDirectory),
            captureFolder: captureFolder,
            across: 2,
            down: 1
        )
        let id = UUID()
        queue.enqueue(
            CaptureSessionModel.CompletedNegative(
                id: id,
                stamp: "neg-1",
                frameURLs: [frame],
                startedAt: Date()
            )
        )
        #expect(queue.hasUnpublishedEntries)
        let urls = queue.discardUnpublished()
        #expect(queue.negatives.isEmpty)
        #expect(!queue.hasUnpublishedEntries)
        #expect(urls.contains(frame))
    }

    @Test("entries stay bound to the roll they were captured for after a roll switch")
    func entriesStayBoundToTheirRoll() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/false"))
        let queue = StitchQueueModel(runner: runner, stateDirectory: directory.appending(path: "support", directoryHint: .isDirectory))
        let rollA = directory.appending(path: "rollA", directoryHint: .isDirectory)
        let rollB = directory.appending(path: "rollB", directoryHint: .isDirectory)
        let folderA = directory.appending(path: "captureA", directoryHint: .isDirectory)
        let folderB = directory.appending(path: "captureB", directoryHint: .isDirectory)
        for url in [folderA, folderB] {
            try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        }

        queue.configure(roll: rollA, captureFolder: folderA, across: 2, down: 1)
        let idA = UUID()
        queue.enqueue(
            CaptureSessionModel.CompletedNegative(
                id: idA, stamp: "a", frameURLs: [folderA.appending(path: "a.NEF")], startedAt: Date()
            )
        )
        queue.testingMarkPublished(id: idA)

        queue.configure(roll: rollB, captureFolder: folderB, across: 3, down: 1)
        let idB = UUID()
        queue.enqueue(
            CaptureSessionModel.CompletedNegative(
                id: idB, stamp: "b", frameURLs: [folderB.appending(path: "b.NEF")], startedAt: Date()
            )
        )

        #expect(queue.negatives(for: rollA).map(\.id) == [idA])
        #expect(queue.negatives(for: rollB).map(\.id) == [idB])
        #expect(queue.negatives(for: nil).isEmpty)
        let contextA = try #require(queue.negatives(for: rollA).first?.context)
        #expect(contextA.rollPath == rollA.path)
        #expect(contextA.captureFolder == folderA.path)
        #expect(contextA.across == 2)
        let contextB = try #require(queue.negatives(for: rollB).first?.context)
        #expect(contextB.rollPath == rollB.path)
        #expect(contextB.across == 3)

        // Discarding from roll A must not touch roll B's queued negative.
        _ = queue.discardUnpublished(for: rollA)
        #expect(queue.negatives(for: rollB).map(\.id) == [idB])
        #expect(queue.negatives(for: rollA).map(\.id) == [idA])
    }

    @Test("discardUnpublished keeps published entries")
    func discardUnpublishedKeepsPublished() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/false"))
        let queue = StitchQueueModel(runner: runner, stateDirectory: directory.appending(path: "support", directoryHint: .isDirectory))
        let captureFolder = directory.appending(path: "capture", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: captureFolder, withIntermediateDirectories: true)
        queue.configure(
            roll: directory.appending(path: "roll", directoryHint: .isDirectory),
            captureFolder: captureFolder,
            across: 1,
            down: 1
        )
        let publishedID = UUID()
        let unpublishedID = UUID()
        queue.enqueue(
            CaptureSessionModel.CompletedNegative(
                id: unpublishedID,
                stamp: "queued",
                frameURLs: [captureFolder.appending(path: "queued.NEF")],
                startedAt: Date()
            )
        )
        queue.enqueue(
            CaptureSessionModel.CompletedNegative(
                id: publishedID,
                stamp: "published",
                frameURLs: [captureFolder.appending(path: "published.NEF")],
                startedAt: Date()
            )
        )
        queue.testingMarkPublished(id: publishedID)
        let urls = queue.discardUnpublished()
        #expect(queue.negatives.count == 1)
        #expect(queue.negatives.first?.id == publishedID)
        #expect(queue.publishedNegativeIDs.contains(publishedID))
        #expect(urls.contains(captureFolder.appending(path: "queued.NEF")))
        #expect(!urls.contains(captureFolder.appending(path: "published.NEF")))
    }

    // MARK: - Parallel compose and ordered commit

    @Test("composes run concurrently up to the parallel stitches setting", arguments: [1, 4])
    func composesRunUpToTheSetting(parallel: Int) async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let log = directory.appending(path: "queue.log")
        let script = Self.queueScript(
            log: log, compose: Self.composeOK(seconds: 0.5), commit: Self.commitOK()
        )
        let queue = try Self.makeQueue(script: script, in: directory, parallel: parallel)
        let stamps = (0..<(parallel + 2)).map { "n\($0)" }
        Self.enqueue(stamps, on: queue, in: directory)
        var seenComposing = 0
        #expect(await Self.waitFor {
            seenComposing = max(seenComposing, Self.steps(queue, .composing))
            return queue.negatives.allSatisfy { $0.step == .published }
        })
        #expect(seenComposing == parallel)
        let lines = Self.logLines(log)
        #expect(Self.maxOverlap(lines, start: "compose-start", end: "compose-end") == parallel)
    }

    @Test("a roll never has more than parallel stitches negatives composed ahead of its commit")
    func composedAheadCap() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let log = directory.appending(path: "queue.log")
        // The first commit is slow, so composed negatives pile up behind it.
        let commit = """
            case "$name" in
              n0) \(Self.commitOK(seconds: 1.0)) ;;
              *) \(Self.commitOK()) ;;
            esac
            """
        let script = Self.queueScript(
            log: log, compose: Self.composeOK(seconds: 0.1), commit: commit
        )
        let queue = try Self.makeQueue(script: script, in: directory, parallel: 2)
        Self.enqueue(["n0", "n1", "n2", "n3", "n4"], on: queue, in: directory)
        var mostAhead = 0
        var sawCapped = false
        #expect(await Self.waitFor {
            let ahead = Self.steps(queue, .composing) + Self.steps(queue, .waitingCommit)
            mostAhead = max(mostAhead, ahead)
            if Self.steps(queue, .stitching) == 1, Self.steps(queue, .waitingCommit) == 2,
               Self.steps(queue, .waitingStitch) == 2
            {
                sawCapped = true
            }
            return queue.negatives.allSatisfy { $0.step == .published }
        })
        #expect(sawCapped)
        #expect(mostAhead <= 2)
    }

    @Test("commits are serial and in capture order on a roll, whatever order composes finish")
    func commitsSerialInCaptureOrder() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let log = directory.appending(path: "queue.log")
        // The first negative composes slowest, so the others are ready first.
        let compose = """
            case "$name" in
              a) \(Self.composeOK(seconds: 0.8)) ;;
              *) \(Self.composeOK(seconds: 0.1)) ;;
            esac
            """
        let script = Self.queueScript(
            log: log, compose: compose, commit: Self.commitOK(seconds: 0.15)
        )
        let queue = try Self.makeQueue(script: script, in: directory, parallel: 3)
        Self.enqueue(["a", "b", "c"], on: queue, in: directory)
        #expect(await Self.waitFor { queue.negatives.allSatisfy { $0.step == .published } })
        let lines = Self.logLines(log)
        #expect(lines.filter { $0.hasPrefix("commit-start") } == ["commit-start a", "commit-start b", "commit-start c"])
        // Each commit starts only after the one before it published.
        for (earlier, later) in [("a", "b"), ("b", "c")] {
            let published = try #require(lines.firstIndex(of: "commit-published \(earlier)"))
            let startedLater = try #require(lines.firstIndex(of: "commit-start \(later)"))
            #expect(published < startedLater)
        }
    }

    @Test("commits of different rolls run at the same time")
    func commitsOverlapAcrossRolls() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let log = directory.appending(path: "queue.log")
        let script = Self.queueScript(
            log: log, compose: Self.composeOK(seconds: 0.1), commit: Self.commitOK(seconds: 0.8)
        )
        let queue = try Self.makeQueue(script: script, in: directory, parallel: 2)
        Self.enqueue(["a1", "a2"], on: queue, in: directory, roll: "rollA")
        Self.enqueue(["b1", "b2"], on: queue, in: directory, roll: "rollB")
        #expect(await Self.waitFor { queue.negatives.allSatisfy { $0.step == .published } })
        let lines = Self.logLines(log)
        // a1 and b1 both start before either publishes.
        let firstPublished = try #require(lines.firstIndex { $0.hasPrefix("commit-published") })
        let startsBefore = Set(lines[..<firstPublished].filter { $0.hasPrefix("commit-start") })
        #expect(startsBefore == ["commit-start a1", "commit-start b1"])
        // Within a roll, still one at a time and in order.
        let order = lines.filter { $0.hasPrefix("commit-start") }
        #expect(order.firstIndex(of: "commit-start a1")! < order.firstIndex(of: "commit-start a2")!)
        #expect(order.firstIndex(of: "commit-start b1")! < order.firstIndex(of: "commit-start b2")!)
        #expect(
            lines.firstIndex(of: "commit-published a1")! < lines.firstIndex(of: "commit-start a2")!
        )
    }

    @Test("the next commit of a roll starts on negative_published, before the previous process exits")
    func nextCommitStartsOnPublished() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let log = directory.appending(path: "queue.log")
        let script = Self.queueScript(
            log: log, compose: Self.composeOK(seconds: 0.1), commit: Self.commitOK(tail: 1.5)
        )
        let queue = try Self.makeQueue(script: script, in: directory, parallel: 2)
        Self.enqueue(["a", "b"], on: queue, in: directory)
        #expect(await Self.waitFor { Self.logLines(log).contains("commit-start b") })
        let lines = Self.logLines(log)
        #expect(!lines.contains("commit-end a"))
        #expect(queue.negatives.first?.step == .published)
        #expect(queue.tailingCommits.count >= 1)
        #expect(await Self.waitFor { queue.negatives.allSatisfy { $0.step == .published } })
        #expect(await Self.waitFor { !queue.isQueueBusy })
    }

    @Test("an entry stays published when its process fails after negative_published")
    func publishedSurvivesAFailedTail() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let log = directory.appending(path: "queue.log")
        let script = Self.queueScript(
            log: log, compose: Self.composeOK(seconds: 0.1),
            commit: Self.commitOK(tail: 0.3, exitFailed: true)
        )
        let queue = try Self.makeQueue(script: script, in: directory)
        var publishedCount = 0
        queue.onNegativePublished = { publishedCount += 1 }
        Self.enqueue(["a"], on: queue, in: directory)
        let work = URL(fileURLWithPath: try #require(queue.negatives.first?.workFolder))
        #expect(await Self.waitFor { queue.negatives.first?.step == .published })
        #expect(queue.negatives.first?.outputFilename == "out.tif")
        #expect(queue.negatives.first?.publishedAt != nil)
        // Still running its tail: the work folder is kept until it exits.
        #expect(queue.hasWork)
        #expect(await Self.waitFor { !queue.hasWork })
        #expect(queue.negatives.first?.step == .published)
        #expect(queue.negatives.first?.failureMessage == nil)
        #expect(publishedCount == 1)
        #expect(!FileManager.default.fileExists(atPath: work.path))
    }

    @Test("a commit that finds the roll busy goes back to waiting and is retried")
    func commitRollBusyIsRetried() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let log = directory.appending(path: "queue.log")
        let commit = """
            if [ ! -e '\(directory.path)/busy-once' ]; then
              touch '\(directory.path)/busy-once'
              \(Self.commitRollBusy())
            fi
            \(Self.commitOK())
            """
        let script = Self.queueScript(log: log, compose: Self.composeOK(seconds: 0.1), commit: commit)
        let queue = try Self.makeQueue(script: script, in: directory)
        Self.enqueue(["a"], on: queue, in: directory)
        var sawFailure = false
        var sawWaitingAfterBusy = false
        #expect(await Self.waitFor {
            let step = queue.negatives.first?.step
            if step == .stitchFailed { sawFailure = true }
            if step == .waitingCommit, Self.logLines(log).contains("commit-busy a") {
                sawWaitingAfterBusy = true
            }
            return step == .published
        })
        #expect(!sawFailure)
        #expect(sawWaitingAfterBusy)
        #expect(Self.logLines(log).filter { $0 == "commit-start a" }.count == 2)
        #expect(Self.logLines(log).contains("commit-busy a"))
    }

    @Test("a compose that finds the roll busy goes back to waiting and is retried")
    func composeRollBusyIsRetried() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let log = directory.appending(path: "queue.log")
        let compose = """
            if [ ! -e '\(directory.path)/busy-once' ]; then
              touch '\(directory.path)/busy-once'
              \(Self.composeFails("ROLL_BUSY"))
            fi
            \(Self.composeOK(seconds: 0.1))
            """
        let script = Self.queueScript(log: log, compose: compose, commit: Self.commitOK())
        let queue = try Self.makeQueue(script: script, in: directory)
        Self.enqueue(["a"], on: queue, in: directory)
        #expect(await Self.waitFor { queue.negatives.first?.step == .published })
        let lines = Self.logLines(log)
        #expect(lines.filter { $0 == "compose-start a" }.count == 2)
        // The retry composed; the first attempt's failure did not skip it.
        #expect(lines.firstIndex(of: "commit-start a")! > lines.lastIndex(of: "compose-end a")!)
    }

    @Test("a failed compose still proceeds to the commit", arguments: ["STITCH_UNDERCONSTRAINED", "INSUFFICIENT_DISK"])
    func failedComposeStillCommits(code: String) async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let log = directory.appending(path: "queue.log")
        let script = Self.queueScript(
            log: log, compose: Self.composeFails(code), commit: Self.commitOK()
        )
        let queue = try Self.makeQueue(script: script, in: directory)
        Self.enqueue(["a"], on: queue, in: directory)
        var sawFailed = false
        #expect(await Self.waitFor {
            if queue.negatives.first?.step == .stitchFailed { sawFailed = true }
            return queue.negatives.first?.step == .published
        })
        #expect(!sawFailed)
        #expect(Self.logLines(log).contains("commit-start a"))
    }

    @Test("the queue stays busy while a published commit finishes, and roll refresh waits for it")
    func tailKeepsQueueBusyAndHoldsRefresh() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let log = directory.appending(path: "queue.log")
        let script = Self.queueScript(
            log: log, compose: Self.composeOK(seconds: 0.1), commit: Self.commitOK(tail: 1.0)
        )
        let queue = try Self.makeQueue(script: script, in: directory)
        Self.enqueue(["a"], on: queue, in: directory)
        let roll = directory.appending(path: "roll", directoryHint: .isDirectory)
        #expect(await Self.waitFor { queue.negatives.first?.step == .published })
        #expect(queue.isQueueBusy)
        #expect(queue.hasWork(on: roll))
        try await Task.sleep(for: .milliseconds(300))
        #expect(queue.isQueueBusy)
        #expect(!Self.logLines(log).contains("refresh"))
        #expect(await Self.waitFor { Self.logLines(log).contains("refresh") })
        let lines = Self.logLines(log)
        #expect(lines.firstIndex(of: "commit-end a")! < lines.firstIndex(of: "refresh")!)
        #expect(await Self.waitFor { !queue.isQueueBusy })
    }

    @Test("a state file restores composing as waiting to stitch and committing as waiting to publish")
    func restoreMapsSteps() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let log = directory.appending(path: "queue.log")
        // Both stages park, so the restored steps can be read.
        let park = "\(Self.started)\nexec sleep 3"
        let script = Self.queueScript(
            log: log,
            compose: "echo \"compose-start $name\" >> \"$LOG\"\n\(park)",
            commit: "echo \"commit-start $name\" >> \"$LOG\"\n\(park)"
        )
        let roll = directory.appending(path: "roll", directoryHint: .isDirectory)
        let captureFolder = directory.appending(path: "capture", directoryHint: .isDirectory)
        for folder in [roll, captureFolder] {
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        }
        let context = StitchQueueModel.EntryContext(
            rollPath: roll.path, captureFolder: captureFolder.path, rigProfileID: nil, across: 1, down: 1
        )
        func entry(_ stamp: String, _ step: StitchQueueModel.Step) -> StitchQueueModel.QueuedNegative {
            .init(
                id: UUID(), stamp: stamp, framePaths: [],
                workFolder: captureFolder.appending(path: ".work/\(stamp)").path,
                step: step, failureMessage: nil, failureCode: nil, enqueuedAt: Date(),
                publishedAt: nil, outputFilename: nil, context: context
            )
        }
        let state = StitchQueueModel.PersistedState(
            rollPath: roll.path, captureFolder: captureFolder.path, rigProfileID: nil,
            across: 1, down: 1,
            negatives: [entry("a", .composing), entry("b", .waitingCommit), entry("c", .stitching)]
        )
        let support = directory.appending(path: "support", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: support, withIntermediateDirectories: true)
        try JSONEncoder().encode(state).write(
            to: support.appending(path: StitchQueueModel.stateFilename)
        )
        let defaults = Self.scratchDefaults()
        defaults.set(1, forKey: StitchQueueModel.parallelStitchesKey)
        let queue = StitchQueueModel(
            runner: CLIRunner(executable: try Self.makeExecutable(in: directory, script: script)),
            defaults: defaults,
            stateDirectory: support
        )
        // `a` went back to waiting to stitch and composes again; `b` and `c`
        // are both waiting to publish, behind it. (Had `c` been restored as
        // waiting to stitch it would be waiting for a compose slot instead.)
        #expect(queue.negatives.map(\.stamp) == ["a", "b", "c"])
        #expect(queue.negatives.map(\.step) == [.composing, .waitingCommit, .waitingCommit])
        #expect(await Self.waitFor { Self.logLines(log) == ["compose-start a"] })
        _ = queue.discardUnpublished()
    }

    @Test("discarding a composing negative cancels its compose")
    func discardCancelsCompose() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let log = directory.appending(path: "queue.log")
        let compose = """
            trap 'echo "compose-cancelled $name" >> "$LOG"; exit 143' TERM
            echo "compose-start $name" >> "$LOG"
            sleep 20 >/dev/null 2>&1 &
            wait
            """
        let script = Self.queueScript(log: log, compose: compose, commit: Self.commitOK())
        let queue = try Self.makeQueue(script: script, in: directory)
        Self.enqueue(["a"], on: queue, in: directory)
        #expect(await Self.waitFor { Self.logLines(log).contains("compose-start a") })
        #expect(queue.negatives.first?.step == .composing)
        _ = queue.discardUnpublished()
        #expect(queue.negatives.isEmpty)
        #expect(await Self.waitFor { Self.logLines(log).contains("compose-cancelled a") })
        #expect(await Self.waitFor { !queue.isQueueBusy })
    }

    @Test("lowering parallel stitches cancels no running compose")
    func loweringCancelsNothing() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let log = directory.appending(path: "queue.log")
        let script = Self.queueScript(
            log: log, compose: Self.composeOK(seconds: 0.8), commit: Self.commitOK()
        )
        let queue = try Self.makeQueue(script: script, in: directory, parallel: 3)
        Self.enqueue(["a", "b", "c", "d"], on: queue, in: directory)
        #expect(await Self.waitFor { Self.steps(queue, .composing) == 3 })
        queue.parallelStitches = 1
        try await Task.sleep(for: .milliseconds(200))
        #expect(Self.steps(queue, .composing) == 3)
        #expect(await Self.waitFor { queue.negatives.allSatisfy { $0.step == .published } })
        // The fourth waited until every compose that was running had ended.
        let lines = Self.logLines(log)
        let fourth = try #require(lines.firstIndex(of: "compose-start d"))
        #expect(lines[..<fourth].filter { $0.hasPrefix("compose-end") }.count == 3)
        #expect(Self.logLines(log).filter { $0.hasPrefix("compose-cancelled") }.isEmpty)
    }

    @Test("raising parallel stitches starts more composes on the next pump")
    func raisingStartsMore() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let log = directory.appending(path: "queue.log")
        let script = Self.queueScript(
            log: log, compose: Self.composeOK(seconds: 1.5), commit: Self.commitOK()
        )
        let queue = try Self.makeQueue(script: script, in: directory, parallel: 1)
        Self.enqueue(["a", "b", "c"], on: queue, in: directory)
        #expect(await Self.waitFor { Self.steps(queue, .composing) == 1 && Self.steps(queue, .waitingStitch) == 2 })
        queue.parallelStitches = 3
        // Nothing has pumped yet; the next enqueue does.
        Self.enqueue(["d"], on: queue, in: directory)
        #expect(await Self.waitFor(timeout: 1) { Self.steps(queue, .composing) == 3 })
        _ = queue.discardUnpublished()
    }

    // MARK: - Parallel stitches setting

    private static func scratchDefaults() -> UserDefaults {
        UserDefaults(suiteName: "scanny-boy-tests-\(UUID().uuidString)")!
    }

    private static func makeQueue(defaults: UserDefaults) -> StitchQueueModel {
        StitchQueueModel(
            runner: CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/true")),
            defaults: defaults,
            stateDirectory: FileManager.default.temporaryDirectory
                .appending(path: "scanny-boy-tests-\(UUID().uuidString)", directoryHint: .isDirectory)
        )
    }

    @Test("parallel stitches defaults to 2 on an empty suite")
    func parallelStitchesDefault() {
        let queue = Self.makeQueue(defaults: Self.scratchDefaults())
        #expect(queue.parallelStitches == 2)
        #expect(StitchQueueModel.defaultParallelStitches == 2)
    }

    @Test("parallel stitches is sticky across models on the same suite")
    func parallelStitchesSticky() {
        let defaults = Self.scratchDefaults()
        let first = Self.makeQueue(defaults: defaults)
        first.parallelStitches = 4
        #expect(Self.makeQueue(defaults: defaults).parallelStitches == 4)
        first.parallelStitches = 1
        #expect(Self.makeQueue(defaults: defaults).parallelStitches == 1)
    }

    @Test("a stored parallel stitches value outside 1...4 reads as 2", arguments: [0, 7, -1])
    func parallelStitchesOutOfRange(stored: Int) {
        let defaults = Self.scratchDefaults()
        defaults.set(stored, forKey: StitchQueueModel.parallelStitchesKey)
        #expect(Self.makeQueue(defaults: defaults).parallelStitches == 2)
    }
}

