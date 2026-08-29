import Foundation
import Testing

@testable import ScannyBoy

/// Drives `RunModel` against fake CLI executables, in the same style as
/// `CLISessionTests` and `ConfigurationModelTests`: a `/bin/sh` script is the
/// cheapest thing that can emit exactly the bytes, signal behaviour, and exit
/// status each case needs. The real helper and the real sample NEFs are
/// exercised by `CLIIntegrationTests` and by the CLI's own suite.
@Suite("Run model (Chunk 10)")
@MainActor
struct RunModelTests {
    // MARK: - Fixtures

    /// A flat create-and-`defer` pair rather than
    /// `TestSupport.withTemporaryDirectory`, for the reason
    /// `ConfigurationModelTests` records: handing a `@MainActor`-isolated
    /// closure to that helper's nonisolated generic parameter trips Swift 6's
    /// "sending main actor-isolated value" diagnostic.
    private static func makeTemporaryDirectory() throws -> URL {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "scanny-boy-tests", directoryHint: .isDirectory)
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    private static func fakeConvertExecutable(
        emitting lines: [String],
        exitStatus: Int = 0,
        in directory: URL
    ) throws -> URL {
        let script = lines.map { "echo '\($0)'" }.joined(separator: "\n")
            + "\nexit \(exitStatus)\n"
        return try TestSupport.writeTestExecutable(script, in: directory)
    }

    private static let runID = "run-0001"

    private static let started =
        #"{"protocol_version":2,"event":"started","command":"convert","run_id":"run-0001"}"#

    private static func finished(status: String, exitStatus: Int) -> String {
        #"{"protocol_version":2,"event":"finished","run_id":"run-0001","status":"\#(status)","exit_status":\#(exitStatus)}"#
    }

    private static func progress(
        sourceIndex: Int, step: String, completed: Int, total: Int
    ) -> String {
        #"{"protocol_version":2,"event":"progress","run_id":"run-0001","source_index":\#(sourceIndex),"step":"\#(step)","completed":\#(completed),"total":\#(total)}"#
    }

    private static func itemDone(sourceIndex: Int, output: String) -> String {
        #"{"protocol_version":2,"event":"item_done","run_id":"run-0001","source_index":\#(sourceIndex),"output":"\#(output)"}"#
    }

    private static func groupDone(_ groupID: String) -> String {
        #"{"protocol_version":2,"event":"group_done","run_id":"run-0001","group_id":"\#(groupID)"}"#
    }

    private static func groupFailed(_ groupID: String, code: String, message: String) -> String {
        #"{"protocol_version":2,"event":"group_failed","run_id":"run-0001","group_id":"\#(groupID)","code":"\#(code)","message":"\#(message)"}"#
    }

    private static func errorEvent(code: String, message: String) -> String {
        #"{"protocol_version":2,"event":"error","run_id":"run-0001","code":"\#(code)","message":"\#(message)"}"#
    }

    private static let sixFiles = [
        "a.NEF", "b.NEF", "c.NEF", "d.NEF", "e.NEF", "f.NEF",
    ]

    /// Starts a run against `executable` and waits for it to end.
    private static func runToCompletion(
        executable: URL,
        outputFolder: URL,
        files: [String] = RunModelTests.sixFiles
    ) async -> RunModel {
        let run = RunModel(runner: CLIRunner(executable: executable))
        run.start(
            command: CLICommand(arguments: ["convert"]),
            files: files,
            outputFolder: outputFolder
        )
        await run.waitForCompletion()
        return run
    }

    // MARK: - Synthetic out-of-order events produce correct progress

    @Test("Progress comes from the completed/total counts, not from source order")
    func outOfOrderProgressIsCountedCorrectly() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        // Three workers reporting in an order no single-threaded run would
        // produce: the last source index arrives first, and the counts are the
        // only monotonic thing in the stream.
        let executable = try Self.fakeConvertExecutable(
            emitting: [
                Self.started,
                Self.progress(sourceIndex: 2, step: "decode", completed: 1, total: 18),
                Self.progress(sourceIndex: 0, step: "decode", completed: 2, total: 18),
                Self.progress(sourceIndex: 1, step: "write_tiff", completed: 3, total: 18),
                Self.progress(sourceIndex: 0, step: "add_metadata", completed: 4, total: 18),
                Self.finished(status: "success", exitStatus: 0),
            ],
            in: directory
        )

        let run = await Self.runToCompletion(executable: executable, outputFolder: directory)

        #expect(run.completedSteps == 4)
        #expect(run.totalSteps == 18)
        // 4/18, not 3/3 — the largest source index seen says nothing about how
        // much work is done (section 4.2).
        let fraction = try #require(run.fractionComplete)
        #expect(abs(fraction - 4.0 / 18.0) < 1e-9)
        #expect(run.currentStep == .addMetadata)
        #expect(run.currentFilename == "a.NEF")
        #expect(run.runID == Self.runID)
        #expect(run.streamFailures.isEmpty)
    }

    @Test("A source index outside the selection does not name a file or crash")
    func outOfRangeSourceIndexIsIgnored() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let executable = try Self.fakeConvertExecutable(
            emitting: [
                Self.started,
                Self.progress(sourceIndex: 99, step: "decode", completed: 1, total: 3),
                Self.finished(status: "success", exitStatus: 0),
            ],
            in: directory
        )

        let run = await Self.runToCompletion(
            executable: executable, outputFolder: directory, files: ["a.NEF"]
        )

        #expect(run.completedSteps == 1)
        #expect(run.currentFilename == nil)
    }

    // MARK: - Partial group work is not presented as published output

    @Test("Staged work in a failed group is never reported as published output")
    func partialGroupWorkIsNotPublished() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        // The first negative gets all the way through its steps and then
        // fails, so its staging directory is discarded: no `item_done`, and
        // therefore nothing published. The second negative publishes normally.
        let executable = try Self.fakeConvertExecutable(
            emitting: [
                Self.started,
                Self.progress(sourceIndex: 0, step: "decode", completed: 1, total: 18),
                Self.progress(sourceIndex: 1, step: "decode", completed: 2, total: 18),
                Self.progress(sourceIndex: 2, step: "write_tiff", completed: 3, total: 18),
                Self.groupFailed(
                    "negative-01", code: "TIFF_WRITE_FAILED", message: "synthetic write failure"
                ),
                Self.progress(sourceIndex: 3, step: "decode", completed: 4, total: 18),
                Self.itemDone(sourceIndex: 3, output: "d.tif"),
                Self.itemDone(sourceIndex: 4, output: "e.tif"),
                Self.itemDone(sourceIndex: 5, output: "f.tif"),
                Self.groupDone("negative-02"),
                Self.finished(status: "partial", exitStatus: 1),
            ],
            exitStatus: 1,
            in: directory
        )

        let run = await Self.runToCompletion(executable: executable, outputFolder: directory)

        #expect(run.publishedOutputs == ["d.tif", "e.tif", "f.tif"])
        for staged in ["a.tif", "b.tif", "c.tif"] {
            #expect(!run.publishedOutputs.contains(staged))
        }
        #expect(run.completedGroups == ["negative-02"])
        #expect(run.failedGroups.count == 1)
        #expect(run.failedGroups.first?.groupID == "negative-01")
        #expect(run.failedGroups.first?.code == .tiffWriteFailed)
        #expect(run.outcome == .failure)
    }

    @Test("A cancelled group publishes nothing and is not reported as a failure")
    func cancelledGroupIsNotAFailure() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        // CONTRACT.md: "A cancelled negative emits no `group_failed`: it was
        // abandoned, not failed."
        let executable = try Self.fakeConvertExecutable(
            emitting: [
                Self.started,
                Self.itemDone(sourceIndex: 0, output: "a.tif"),
                Self.itemDone(sourceIndex: 1, output: "b.tif"),
                Self.itemDone(sourceIndex: 2, output: "c.tif"),
                Self.groupDone("negative-01"),
                Self.progress(sourceIndex: 3, step: "decode", completed: 10, total: 18),
                Self.errorEvent(code: "CANCELLED", message: "cancelled by request"),
                Self.finished(status: "cancelled", exitStatus: 143),
            ],
            exitStatus: 143,
            in: directory
        )

        let run = await Self.runToCompletion(executable: executable, outputFolder: directory)

        #expect(run.publishedOutputs == ["a.tif", "b.tif", "c.tif"])
        #expect(run.completedGroups == ["negative-01"])
        #expect(run.failedGroups.isEmpty)
        #expect(run.outcome == .cancelled(forced: false))
        #expect(run.cliError?.code == .cancelled)
    }

    // MARK: - Repeated Cancel requests have no extra effect

    @Test("Repeated Cancel requests send exactly one SIGTERM", .timeLimit(.minutes(1)))
    func repeatedCancelSendsOneSignal() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let signalLog = directory.appending(path: "signals.log", directoryHint: .notDirectory)
        // Records every SIGTERM it receives, then behaves like the real CLI
        // does on a cooperative cancellation.
        let script = """
            trap 'echo term >> "\(signalLog.path)"; STOP=1' TERM
            echo '\(Self.started)'
            i=0
            while [ "$STOP" != 1 ] && [ $i -lt 200 ]; do
              sleep 0.05
              i=$((i + 1))
            done
            echo '\(Self.errorEvent(code: "CANCELLED", message: "cancelled by request"))'
            echo '\(Self.finished(status: "cancelled", exitStatus: 143))'
            exit 143
            """
        let executable = try TestSupport.writeTestExecutable(script, in: directory)

        // A grace period longer than this test can take: what is under test is
        // the SIGTERM count, and a forced SIGKILL would end the child before
        // it could log anything more.
        let run = RunModel(
            runner: CLIRunner(executable: executable), gracePeriod: .seconds(120)
        )
        run.start(
            command: CLICommand(arguments: ["convert"]),
            files: Self.sixFiles,
            outputFolder: directory
        )

        // Wait until the child is genuinely running and its trap is installed;
        // its own `started` event is the signal that it is.
        try await Self.waitUntil { run.runID != nil }

        #expect(run.canCancel)
        run.cancel()
        #expect(!run.canCancel)
        run.cancel()
        run.cancel()

        await run.waitForCompletion()

        let logged = (try? String(contentsOf: signalLog, encoding: .utf8)) ?? ""
        let lines = logged.split(separator: "\n")
        #expect(lines.count == 1, "the helper received \(lines.count) SIGTERMs, expected 1")
        #expect(run.outcome == .cancelled(forced: false))
        #expect(run.phase == .finished)
    }

    @Test("Cancel does nothing before a run starts or after it has ended")
    func cancelOutsideARunIsInert() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let executable = try Self.fakeConvertExecutable(
            emitting: [Self.started, Self.finished(status: "success", exitStatus: 0)],
            in: directory
        )
        let run = RunModel(runner: CLIRunner(executable: executable))

        #expect(!run.canCancel)
        run.cancel()
        #expect(run.phase == .idle)

        run.start(
            command: CLICommand(arguments: ["convert"]),
            files: Self.sixFiles,
            outputFolder: directory
        )
        await run.waitForCompletion()

        #expect(run.phase == .finished)
        #expect(!run.canCancel)
        run.cancel()
        #expect(run.phase == .finished)
    }

    // MARK: - Terminal stream errors and CLI errors are distinct

    @Test("An unreadable stdout line is a stream failure, not a CLI error")
    func malformedLineIsAStreamFailure() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let executable = try Self.fakeConvertExecutable(
            emitting: [
                Self.started,
                "{not json at all",
                Self.finished(status: "success", exitStatus: 0),
            ],
            in: directory
        )

        let run = await Self.runToCompletion(executable: executable, outputFolder: directory)

        #expect(run.cliError == nil)
        #expect(run.streamFailures.count == 1)
        let failure = try #require(run.streamFailures.first)
        guard case .decode = failure else {
            Issue.record("expected a decode failure, got \(failure)")
            return
        }
    }

    @Test("A CLI error event is a CLI error, not a stream failure")
    func errorEventIsACLIError() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let executable = try Self.fakeConvertExecutable(
            emitting: [
                Self.started,
                Self.errorEvent(code: "TIFF_WRITE_FAILED", message: "could not write a TIFF"),
                Self.finished(status: "failed", exitStatus: 1),
            ],
            exitStatus: 1,
            in: directory
        )

        let run = await Self.runToCompletion(executable: executable, outputFolder: directory)

        #expect(run.streamFailures.isEmpty)
        #expect(run.cliError?.code == .tiffWriteFailed)
        #expect(run.cliError?.message == "could not write a TIFF")
        #expect(run.outcome == .failure)
        #expect(run.completionSummary == "The run failed: could not write a TIFF")
    }

    @Test("A helper that cannot be launched is a stream failure with no outcome")
    func launchFailureIsAStreamFailure() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let missing = directory.appending(path: "not-installed", directoryHint: .notDirectory)
        let run = RunModel(runner: CLIRunner(executable: missing))
        run.start(
            command: CLICommand(arguments: ["convert"]),
            files: Self.sixFiles,
            outputFolder: directory
        )
        await run.waitForCompletion()

        #expect(run.cliError == nil)
        #expect(run.outcome == nil)
        #expect(run.streamFailures.count == 1)
        let failure = try #require(run.streamFailures.first)
        guard case .launch = failure else {
            Issue.record("expected a launch failure, got \(failure)")
            return
        }
        #expect(run.phase == .finished)
        #expect(run.completionSummary == "The command-line helper could not be run.")
    }

    // MARK: - The manifest a run leaves behind

    @Test("A complete manifest is read back as a final one")
    func finalManifestIsRead() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        try Self.writeManifest(status: "complete", groupStatus: "completed", in: directory)
        let executable = try Self.fakeConvertExecutable(
            emitting: [
                Self.started,
                Self.itemDone(sourceIndex: 0, output: "a.tif"),
                Self.groupDone("negative-01"),
                Self.finished(status: "success", exitStatus: 0),
            ],
            in: directory
        )

        let run = await Self.runToCompletion(executable: executable, outputFolder: directory)

        let report = try #require(run.manifestReport)
        guard case .final(let manifest) = report else {
            Issue.record("expected a final manifest, got \(report)")
            return
        }
        #expect(manifest.status == "complete")
        #expect(manifest.runID == Self.runID)
        #expect(manifest.publishedOutputs == ["a.tif"])
        #expect(manifest.groups.first?.isCompleted == true)
    }

    /// Section 3.8: a forced stop "cannot clean files, update the manifest, or
    /// emit a final event", so the manifest is still `running`. Chunk 10:
    /// accept it, say cleanup was incomplete, and say the next run recovers it.
    @Test("A manifest left as running is accepted and reported as incomplete cleanup")
    func staleRunningManifestIsAccepted() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        try Self.writeManifest(status: "running", groupStatus: "pending", in: directory)
        let executable = try Self.fakeConvertExecutable(
            emitting: [Self.started, Self.progress(
                sourceIndex: 0, step: "decode", completed: 1, total: 18
            )],
            exitStatus: 137,
            in: directory
        )

        let run = await Self.runToCompletion(executable: executable, outputFolder: directory)

        let report = try #require(run.manifestReport)
        guard case .cleanupIncomplete(let manifest) = report else {
            Issue.record("expected an incomplete-cleanup manifest, got \(report)")
            return
        }
        #expect(manifest.isRunning)
        #expect(report.summary.contains("The next run removes it"))
        // Accepted, not treated as unreadable: the run's own state survives.
        #expect(run.completedSteps == 1)
    }

    @Test("A missing manifest is reported as unavailable, not as a final one")
    func missingManifestIsUnavailable() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let executable = try Self.fakeConvertExecutable(
            emitting: [Self.started, Self.finished(status: "success", exitStatus: 0)],
            in: directory
        )

        let run = await Self.runToCompletion(executable: executable, outputFolder: directory)

        let report = try #require(run.manifestReport)
        guard case .unavailable = report else {
            Issue.record("expected an unavailable manifest, got \(report)")
            return
        }
        #expect(report.manifest == nil)
    }

    // MARK: - Elapsed and remaining time

    @Test(
        "The remaining-time estimate scales the elapsed time by the work left",
        arguments: [
            (elapsed: 10.0, completed: 0, total: 18, expected: TimeInterval?.none),
            (elapsed: 0.0, completed: 4, total: 18, expected: TimeInterval?.none),
            (elapsed: 10.0, completed: 18, total: 18, expected: TimeInterval?.none),
            (elapsed: 10.0, completed: 5, total: 10, expected: TimeInterval?.some(10)),
            (elapsed: 9.0, completed: 3, total: 18, expected: TimeInterval?.some(45)),
        ]
    )
    func remainingTimeEstimate(
        _ scenario: (elapsed: TimeInterval, completed: Int, total: Int, expected: TimeInterval?)
    ) throws {
        let estimate = RunModel.estimatedRemaining(
            elapsed: scenario.elapsed, completed: scenario.completed, total: scenario.total
        )
        if let expected = scenario.expected {
            let value = try #require(estimate)
            #expect(abs(value - expected) < 1e-9)
        } else {
            #expect(estimate == nil)
        }
    }

    @Test("Elapsed time is measured from the start of the run")
    func elapsedTimeIsMeasuredFromTheStart() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let executable = try Self.fakeConvertExecutable(
            emitting: [Self.started, Self.finished(status: "success", exitStatus: 0)],
            in: directory
        )
        // A clock that advances 30 seconds every time it is read, so the
        // elapsed figure is exact rather than dependent on how fast the test
        // machine runs a shell script.
        let clock = SteppingClock(step: 30)
        let run = RunModel(runner: CLIRunner(executable: executable), now: clock.next)
        run.start(
            command: CLICommand(arguments: ["convert"]),
            files: Self.sixFiles,
            outputFolder: directory
        )
        await run.waitForCompletion()

        #expect(run.elapsed >= 30)
        #expect(run.elapsed.truncatingRemainder(dividingBy: 30) == 0)
    }

    // MARK: - Chunk 10's additions to the configuration model

    @Test("Run is offered before the overwrite confirmation, and asks for it")
    func overwriteConfirmationIsAskedForAtRunTime() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let outputDir = directory.appending(path: "out", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: outputDir, withIntermediateDirectories: true)

        let catalogue =
            #"{"protocol_version":2,"event":"probe_result","catalogue":["a.NEF","b.NEF","c.NEF"],"warnings":[],"groups":[]}"#
        let withConflicts =
            #"{"protocol_version":2,"event":"probe_result","catalogue":["a.NEF","b.NEF","c.NEF"],"warnings":[],"groups":[["a.NEF","b.NEF","c.NEF"]],"output_conflicts":["a.tif"],"estimated_required_bytes":1000,"available_bytes":50000}"#
        let script = """
            case "$*" in
              *--out*)
            echo '\(withConflicts)'
                ;;
              *)
            echo '\(catalogue)'
                ;;
            esac
            """
        let executable = try TestSupport.writeTestExecutable(script, in: directory)
        let model = ConfigurationModel(
            runner: CLIRunner(executable: executable),
            defaults: UserDefaults(suiteName: "scanny-boy-tests-\(UUID().uuidString)")!
        )

        model.inputFolder = directory
        await model.waitForPendingProbes()
        model.outputFolder = outputDir
        model.selectedFiles = ["a.NEF", "b.NEF", "c.NEF"]
        model.filmDate = "2026-08-02"
        await model.waitForPendingProbes()

        // The button is live, but pressing it asks rather than converting.
        #expect(model.isReadyPendingOverwriteConfirmation)
        #expect(model.needsOverwriteConfirmation)
        #expect(!model.runEnabled)
        #expect(model.convertCommand == nil)

        model.confirmOverwrite()

        #expect(!model.needsOverwriteConfirmation)
        #expect(model.runEnabled)
        let command = try #require(model.convertCommand)
        #expect(command.arguments.contains("--overwrite"))
        #expect(command.arguments.contains("--film-date"))
        #expect(command.arguments.contains("2026-08-02"))
    }

    @Test("A run with nothing to replace never passes --overwrite")
    func noConflictsMeansNoOverwriteFlag() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let outputDir = directory.appending(path: "out", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: outputDir, withIntermediateDirectories: true)

        let catalogue =
            #"{"protocol_version":2,"event":"probe_result","catalogue":["a.NEF","b.NEF","c.NEF"],"warnings":[],"groups":[]}"#
        let clean =
            #"{"protocol_version":2,"event":"probe_result","catalogue":["a.NEF","b.NEF","c.NEF"],"warnings":[],"groups":[["a.NEF","b.NEF","c.NEF"]],"output_conflicts":[],"estimated_required_bytes":1000,"available_bytes":50000}"#
        let script = """
            case "$*" in
              *--out*)
            echo '\(clean)'
                ;;
              *)
            echo '\(catalogue)'
                ;;
            esac
            """
        let executable = try TestSupport.writeTestExecutable(script, in: directory)
        let model = ConfigurationModel(
            runner: CLIRunner(executable: executable),
            defaults: UserDefaults(suiteName: "scanny-boy-tests-\(UUID().uuidString)")!
        )

        model.inputFolder = directory
        await model.waitForPendingProbes()
        model.outputFolder = outputDir
        model.selectedFiles = ["a.NEF", "b.NEF", "c.NEF"]
        model.filmDate = "2026-08-02"
        await model.waitForPendingProbes()

        #expect(!model.needsOverwriteConfirmation)
        let command = try #require(model.convertCommand)
        #expect(!command.arguments.contains("--overwrite"))
        // Canonical order, straight from the catalogue.
        #expect(model.selectedFilesInCanonicalOrder == ["a.NEF", "b.NEF", "c.NEF"])
    }

    // MARK: - Helpers

    /// Polls `condition` until it holds. Used only where the thing being
    /// waited for is a real child process reaching a real state.
    private static func waitUntil(
        timeout: Duration = .seconds(30),
        _ condition: @MainActor () -> Bool
    ) async throws {
        let deadline = ContinuousClock.now + timeout
        while ContinuousClock.now < deadline {
            if condition() { return }
            try await Task.sleep(for: .milliseconds(20))
        }
        Issue.record("timed out waiting for the helper to reach the expected state")
    }

    /// A manifest with just enough of `manifest.schema.json` to be read back.
    private static func writeManifest(
        status: String,
        groupStatus: String,
        in folder: URL
    ) throws {
        let outputs = groupStatus == "completed"
            ? #"[{"name":"a.tif","size":123,"sha256":"\#(String(repeating: "a", count: 64))"}]"#
            : "[]"
        let json = """
            {
              "manifest_format_version": 1,
              "scanny_boy_version": "0.1.0",
              "run_id": "\(runID)",
              "status": "\(status)",
              "input_folder": "/tmp/in",
              "film_date": "2026-08-02",
              "shots_per_negative": 3,
              "processing_params": {},
              "icc_profile": {"name": "ProPhoto-v4.icc", "sha256": "\(String(repeating: "b", count: 64))"},
              "source_order": ["a.NEF", "b.NEF", "c.NEF"],
              "sources": [],
              "curated_metadata": {},
              "groups": [
                {
                  "group_id": "negative-01",
                  "members": ["a.NEF", "b.NEF", "c.NEF"],
                  "expected_outputs": ["a.tif", "b.tif", "c.tif"],
                  "status": "\(groupStatus)",
                  "outputs": \(outputs),
                  "error_code": null,
                  "error_message": null
                }
              ],
              "started_at": "2026-08-02T12:00:00",
              "finished_at": \(status == "running" ? "null" : "\"2026-08-02T12:05:00\"")
            }
            """
        try json.write(
            to: folder.appending(path: RunManifest.filename, directoryHint: .notDirectory),
            atomically: true,
            encoding: .utf8
        )
    }
}

/// A clock that advances by a fixed step every time it is read. Deterministic
/// where a real one would make the assertion depend on machine speed.
private final class SteppingClock: @unchecked Sendable {
    private let lock = NSLock()
    private let step: TimeInterval
    private var reading: Date

    init(step: TimeInterval, start: Date = Date(timeIntervalSince1970: 0)) {
        self.step = step
        self.reading = start
    }

    var next: @Sendable () -> Date {
        { [self] in
            lock.lock()
            defer { lock.unlock() }
            let value = reading
            reading = reading.addingTimeInterval(step)
            return value
        }
    }
}
