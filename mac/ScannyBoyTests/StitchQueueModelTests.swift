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
        let queue = StitchQueueModel(runner: runner)
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

    @Test("stitches run one at a time")
    func serialStitch() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let script = """
                if [ "$1" = "capture" ]; then
                  echo '\(TestEvents.line(#"{"event":"started","command":"capture check"}"#))'
                  echo '\(TestEvents.line(#"{"event":"capture_checked","passed":true,"code":null,"message":null,"global_rms_px":1.0,"used_clahe_fallback":false}"#))'
                  echo '\(TestEvents.line(#"{"event":"finished","status":"success","exit_status":0}"#))'
                  exit 0
                fi
                if [ "$1" = "stitch" ]; then
                  echo '\(TestEvents.line(#"{"event":"started","command":"stitch"}"#))'
                  sleep 0.2
                  echo '\(TestEvents.line(#"{"event":"negative_done","negative_id":"n1","output":"out.tif","width":1,"height":1,"global_rms_px":1.0,"max_overlap_mad":1.0}"#))'
                  echo '\(TestEvents.line(#"{"event":"finished","status":"success","exit_status":0}"#))'
                  exit 0
                fi
                echo '\(TestEvents.line(#"{"event":"started","command":"prepare"}"#))'
                echo '\(TestEvents.line(#"{"event":"finished","status":"success","exit_status":0}"#))'
                """
            let executable = try Self.makeExecutable(in: directory, script: script)
            let runner = CLIRunner(executable: executable)
            let queue = StitchQueueModel(runner: runner)
            let captureFolder = directory.appending(path: "capture", directoryHint: .isDirectory)
            try FileManager.default.createDirectory(at: captureFolder, withIntermediateDirectories: true)
            queue.configure(
                roll: directory.appending(path: "roll", directoryHint: .isDirectory),
                captureFolder: captureFolder,
                rigProfileID: "rig-1",
                across: 1,
                down: 1
            )
            queue.enqueue(
                CaptureSessionModel.CompletedNegative(
                    id: UUID(), stamp: "a", frameURLs: [], startedAt: Date()
                )
            )
        try await Task.sleep(for: .milliseconds(50))
        #expect(queue.isStitching == true || queue.negatives.first?.step != .waitingStitch)
    }

    @Test("a checked negative waiting behind a stitch is not reset to waitingCheck")
    func checkedNegativeKeepsWaitingStitch() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let script = """
            if [ "$1" = "capture" ]; then
              echo '\(TestEvents.line(#"{"event":"started","command":"capture check"}"#))'
              echo '\(TestEvents.line(#"{"event":"capture_checked","passed":true,"code":null,"message":null,"global_rms_px":1.0,"used_clahe_fallback":false}"#))'
              echo '\(TestEvents.line(#"{"event":"finished","status":"success","exit_status":0}"#))'
              exit 0
            fi
            if [ "$1" = "stitch" ]; then
              echo '\(TestEvents.line(#"{"event":"started","command":"stitch"}"#))'
              sleep 3
              echo '\(TestEvents.line(#"{"event":"finished","status":"success","exit_status":0}"#))'
              exit 0
            fi
            echo '\(TestEvents.line(#"{"event":"started","command":"prepare"}"#))'
            echo '\(TestEvents.line(#"{"event":"finished","status":"success","exit_status":0}"#))'
            """
        let executable = try Self.makeExecutable(in: directory, script: script)
        let queue = StitchQueueModel(runner: CLIRunner(executable: executable))
        let captureFolder = directory.appending(path: "capture", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: captureFolder, withIntermediateDirectories: true)
        queue.configure(
            roll: directory.appending(path: "roll", directoryHint: .isDirectory),
            captureFolder: captureFolder,
            across: 1,
            down: 1
        )
        for index in 0..<3 {
            queue.enqueue(
                CaptureSessionModel.CompletedNegative(
                    id: UUID(), stamp: "neg-\(index)", frameURLs: [], startedAt: Date()
                )
            )
        }
        // Long enough for every prepare and check to finish, well short of
        // the first stitch.
        try await Task.sleep(for: .milliseconds(1500))
        let steps = queue.negatives.map(\.step)
        #expect(steps.filter { $0 == .stitching }.count == 1)
        #expect(steps.allSatisfy { $0 == .stitching || $0 == .waitingStitch })
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
        let queue = StitchQueueModel(runner: CLIRunner(executable: executable))
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
        try await Task.sleep(for: .milliseconds(2000))
        #expect(queue.negatives.map(\.step) == [.published, .checkFailed, .published])
        #expect(notifications == ["published", "published", "rollUpdated"])
        #expect(!queue.hasWork)
        #expect(queue.hasUnpublishedEntries)
        _ = queue.discardUnpublished()
    }

    /// A fake CLI whose checks pass, whose prepares take `slowPrepare`
    /// seconds for negatives stamped `slow*` (instantly otherwise), and whose
    /// stitches take `stitch` seconds and append their stamp to `stitchLog`.
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
          basename "$3" >> '\(stitchLog.path)'
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

    @Test("a checked negative stitches while a later negative is still preparing")
    func stitchStartsAheadOfLaterPrepare() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let script = Self.orderingScript(
            slowPrepare: 3, stitch: 3, stitchLog: directory.appending(path: "stitches.log")
        )
        let executable = try Self.makeExecutable(in: directory, script: script)
        let queue = StitchQueueModel(runner: CLIRunner(executable: executable))
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
        #expect(queue.negatives.map(\.step) == [.stitching, .preparing])
        #expect(queue.isStitching)
        _ = queue.discardUnpublished()
    }

    @Test("a later negative does not stitch ahead of an earlier one on its roll")
    func laterNegativeWaitsForEarlierSameRoll() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let stitchLog = directory.appending(path: "stitches.log")
        let script = Self.orderingScript(slowPrepare: 1, stitch: 0.2, stitchLog: stitchLog)
        let executable = try Self.makeExecutable(in: directory, script: script)
        let queue = StitchQueueModel(runner: CLIRunner(executable: executable))
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
        // The second negative is checked; the first is still preparing.
        try await Task.sleep(for: .milliseconds(500))
        #expect(queue.negatives.map(\.step) == [.preparing, .waitingStitch])
        #expect(!queue.isStitching)
        try await Task.sleep(for: .milliseconds(2500))
        #expect(queue.negatives.map(\.step) == [.published, .published])
        let order = try String(contentsOf: stitchLog, encoding: .utf8)
            .split(separator: "\n").map(String.init)
        #expect(order == ["slow", "fast"])
    }

    @Test("a negative on another roll does not wait for this roll's prepares")
    func otherRollDoesNotBlockStitch() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let script = Self.orderingScript(
            slowPrepare: 3, stitch: 3, stitchLog: directory.appending(path: "stitches.log")
        )
        let executable = try Self.makeExecutable(in: directory, script: script)
        let queue = StitchQueueModel(runner: CLIRunner(executable: executable))
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
        #expect(queue.negatives.map(\.step) == [.preparing, .stitching])
        _ = queue.discardUnpublished()
    }

    @Test("discardUnpublished removes queue entries and returns their paths")
    func discardUnpublished() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/false"))
        let queue = StitchQueueModel(runner: runner)
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
        let queue = StitchQueueModel(runner: runner)
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
        let queue = StitchQueueModel(runner: runner)
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

    // MARK: - Parallel stitches setting

    private static func scratchDefaults() -> UserDefaults {
        UserDefaults(suiteName: "scanny-boy-tests-\(UUID().uuidString)")!
    }

    private static func makeQueue(defaults: UserDefaults) -> StitchQueueModel {
        StitchQueueModel(
            runner: CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/true")),
            defaults: defaults
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

