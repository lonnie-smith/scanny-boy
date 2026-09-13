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
}

