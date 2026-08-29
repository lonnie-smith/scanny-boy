import Foundation
import Testing

@testable import ScannyBoy

/// The Chunk 10 run flow driven end to end: the real bundled helper, the real
/// sample NEFs, `ConfigurationModel` deciding what may run, and `RunModel`
/// running it.
///
/// These are the automated half of Chunk 10's manual-verification list. They
/// prove the behaviour; they do not stand in for the user's own sign-off on
/// overwrite and cancellation in the finished app, which is approval point 4
/// of section 8.
///
/// Both prerequisites — the built app hosting these tests, and the sample
/// files at `tests/fixtures/nef/` — are absent from CI, so the suite skips
/// there with a reason that names what went untested.
@Suite("Run flow, end to end (Chunk 10)")
@MainActor
struct RunIntegrationTests {
    // `nonisolated`: `.enabled(if:)` evaluates its condition in a Sendable
    // closure, which cannot reach a main-actor-isolated property.
    private nonisolated static var canRun: Bool {
        HostBundle.isAvailable && SampleFixtures.areAvailable
    }

    private nonisolated static var unavailable: Comment {
        var reasons: [String] = []
        if !HostBundle.isAvailable {
            reasons.append(HostBundle.unavailableComment.rawValue)
        }
        if !SampleFixtures.areAvailable {
            reasons.append(
                """
                The real sample NEFs are not present at tests/fixtures/nef/ \
                (see docs/IMPLEMENTATION_PLAN.md appendix A). The Chunk 10 \
                run flow — a six-frame conversion, the blocked selections, \
                the rerun that requires confirmation, and cooperative \
                cancellation keeping earlier negatives — did not run.
                """
            )
        }
        return Comment(rawValue: reasons.joined(separator: "\n"))
    }

    private static func runner() throws -> CLIRunner {
        CLIRunner(executable: try #require(HostBundle.helperExecutableURL))
    }

    private static func makeTemporaryDirectory() throws -> URL {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "scanny-boy-tests", directoryHint: .isDirectory)
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    private static func isolatedDefaults() -> UserDefaults {
        UserDefaults(suiteName: "scanny-boy-tests-\(UUID().uuidString)")!
    }

    /// A configuration model pointed at the sample folder, with its catalogue
    /// probe already applied.
    private static func configuredModel(
        outputFolder: URL,
        select: [String],
        perNegative: Int = 3,
        filmDate: String = "2026-08-02"
    ) async throws -> ConfigurationModel {
        let model = ConfigurationModel(
            runner: try runner(), defaults: isolatedDefaults()
        )
        model.inputFolder = SampleFixtures.directory
        await model.waitForPendingProbes()
        model.outputFolder = outputFolder
        model.perNegative = perNegative
        model.filmDate = filmDate
        model.selectedFiles = Set(select)
        await model.waitForPendingProbes()
        return model
    }

    private static func tiffNames(_ nefNames: [String]) -> [String] {
        nefNames.map { ($0 as NSString).deletingPathExtension + ".tif" }
    }

    private static func stagingDirectories(in folder: URL) throws -> [URL] {
        try FileManager.default
            .contentsOfDirectory(at: folder, includingPropertiesForKeys: nil)
            .filter { $0.lastPathComponent.hasSuffix(".scanny-staging") }
    }

    private static func waitUntil(
        timeout: Duration = .seconds(120),
        _ condition: @MainActor () -> Bool
    ) async throws {
        let deadline = ContinuousClock.now + timeout
        while ContinuousClock.now < deadline {
            if condition() { return }
            try await Task.sleep(for: .milliseconds(50))
        }
        Issue.record("timed out waiting for the run to reach the expected state")
    }

    // MARK: - Six sample files at three per negative

    @Test(
        "six sample files at three per negative produce six TIFFs and a complete manifest",
        .enabled(if: RunIntegrationTests.canRun, RunIntegrationTests.unavailable),
        .timeLimit(.minutes(5))
    )
    func sixFilesProduceSixTIFFsAndACompleteManifest() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let out = directory.appending(path: "out", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)

        let model = try await Self.configuredModel(
            outputFolder: out, select: SampleFixtures.files
        )
        #expect(model.groups.count == 2)
        #expect(model.runEnabled)
        #expect(!model.needsOverwriteConfirmation)

        let run = RunModel(runner: try Self.runner())
        run.start(
            command: try #require(model.convertCommand),
            files: model.selectedFilesInCanonicalOrder,
            outputFolder: out
        )
        await run.waitForCompletion()

        #expect(run.streamFailures.isEmpty)
        #expect(run.cliError == nil)
        #expect(run.outcome == .success)
        #expect(run.failedGroups.isEmpty)
        #expect(run.completedGroups == ["negative-01", "negative-02"])
        #expect(run.publishedOutputs == Self.tiffNames(SampleFixtures.files))
        // Progress reached its own total, and the total is three steps a frame.
        #expect(run.totalSteps == SampleFixtures.files.count * 3)
        #expect(run.completedSteps == run.totalSteps)

        let report = try #require(run.manifestReport)
        guard case .final(let manifest) = report else {
            Issue.record("expected a final manifest, got \(report)")
            return
        }
        #expect(manifest.status == "complete")
        #expect(manifest.shotsPerNegative == 3)
        #expect(manifest.filmDate == "2026-08-02")
        #expect(manifest.publishedOutputs == Self.tiffNames(SampleFixtures.files))

        for name in Self.tiffNames(SampleFixtures.files) {
            #expect(
                FileManager.default.fileExists(
                    atPath: out.appending(path: name).path
                ),
                "\(name) was not published"
            )
        }
        #expect(try Self.stagingDirectories(in: out).isEmpty)
    }

    // MARK: - Blocked selections

    @Test(
        "a five-file selection at three per negative is blocked",
        .enabled(if: RunIntegrationTests.canRun, RunIntegrationTests.unavailable),
        .timeLimit(.minutes(5))
    )
    func fiveFileSelectionIsBlocked() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let out = directory.appending(path: "out", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)

        let model = try await Self.configuredModel(
            outputFolder: out, select: Array(SampleFixtures.files.prefix(5))
        )

        #expect(model.selectionError?.code == .notDivisible)
        #expect(!model.runEnabled)
        #expect(model.convertCommand == nil)
    }

    /// Appendix A: the break between frames 4640 and 4644 is *not* a catalogue
    /// gap, so a gap has to be made by leaving a catalogue member out —
    /// frames 1, 2, 4, 5, 6.
    @Test(
        "a selection with a gap is blocked",
        .enabled(if: RunIntegrationTests.canRun, RunIntegrationTests.unavailable),
        .timeLimit(.minutes(5))
    )
    func selectionWithAGapIsBlocked() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let out = directory.appending(path: "out", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)

        let withGap = [0, 1, 3, 4, 5].map { SampleFixtures.files[$0] }
        let model = try await Self.configuredModel(outputFolder: out, select: withGap)

        #expect(model.selectionError?.code == .nonContiguousSelection)
        #expect(!model.runEnabled)
        #expect(model.convertCommand == nil)
    }

    @Test(
        "unrelated content in the output folder is blocked",
        .enabled(if: RunIntegrationTests.canRun, RunIntegrationTests.unavailable),
        .timeLimit(.minutes(5))
    )
    func unrelatedOutputFolderIsBlocked() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let out = directory.appending(path: "out", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)
        try "not ours".write(
            to: out.appending(path: "holiday-snap.jpg"), atomically: true, encoding: .utf8
        )

        let model = try await Self.configuredModel(
            outputFolder: out, select: SampleFixtures.files
        )

        #expect(model.outputError?.code == .outputNotEmpty)
        #expect(!model.runEnabled)
        #expect(model.convertCommand == nil)
    }

    // MARK: - Rerunning a valid output folder requires confirmation

    @Test(
        "rerunning a completed output folder requires confirmation and then passes --overwrite",
        .enabled(if: RunIntegrationTests.canRun, RunIntegrationTests.unavailable),
        .timeLimit(.minutes(10))
    )
    func rerunRequiresConfirmation() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let out = directory.appending(path: "out", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)

        let first = try await Self.configuredModel(
            outputFolder: out, select: SampleFixtures.files
        )
        let firstRun = RunModel(runner: try Self.runner())
        firstRun.start(
            command: try #require(first.convertCommand),
            files: first.selectedFilesInCanonicalOrder,
            outputFolder: out
        )
        await firstRun.waitForCompletion()
        #expect(firstRun.outcome == .success)

        // A second configuration over the same folder now sees the previous
        // run's outputs as the exact files a rerun would replace.
        let second = try await Self.configuredModel(
            outputFolder: out, select: SampleFixtures.files
        )
        #expect(second.outputConflicts == Self.tiffNames(SampleFixtures.files))
        #expect(second.needsOverwriteConfirmation)
        #expect(!second.runEnabled)
        #expect(second.convertCommand == nil)

        second.confirmOverwrite()
        let command = try #require(second.convertCommand)
        #expect(command.arguments.contains("--overwrite"))

        let secondRun = RunModel(runner: try Self.runner())
        secondRun.start(
            command: command,
            files: second.selectedFilesInCanonicalOrder,
            outputFolder: out
        )
        await secondRun.waitForCompletion()

        #expect(secondRun.outcome == .success)
        #expect(secondRun.publishedOutputs == Self.tiffNames(SampleFixtures.files))
        #expect(secondRun.manifestReport?.manifest?.status == "complete")
    }

    // MARK: - Cancel retains earlier groups and discards the current one

    /// Section 3.6: "Completed groups remain after cancellation. The group
    /// being processed is not published."
    @Test(
        "cancelling after the first negative keeps it and discards the second",
        .enabled(if: RunIntegrationTests.canRun, RunIntegrationTests.unavailable),
        .timeLimit(.minutes(10))
    )
    func cancelKeepsCompletedGroups() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let out = directory.appending(path: "out", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)

        let model = try await Self.configuredModel(
            outputFolder: out, select: SampleFixtures.files
        )
        // Serial, so the second negative is unmistakably still in progress
        // when cancellation arrives.
        let command = CLICommand.convert(
            input: SampleFixtures.directory,
            files: model.selectedFilesInCanonicalOrder,
            out: out,
            filmDate: "2026-08-02",
            perNegative: 3,
            jobs: 1
        )
        // A grace period long enough that this test is about cooperative
        // cancellation rather than a race with the forced-stop timer.
        let run = RunModel(runner: try Self.runner(), gracePeriod: .seconds(120))
        run.start(
            command: command,
            files: model.selectedFilesInCanonicalOrder,
            outputFolder: out
        )

        try await Self.waitUntil { run.completedGroups.count == 1 }
        run.cancel()
        await run.waitForCompletion()

        #expect(run.outcome == .cancelled(forced: false))
        #expect(run.cliError?.code == .cancelled)
        // Abandoned, not failed: CONTRACT.md says a cancelled negative emits
        // no `group_failed`.
        #expect(run.failedGroups.isEmpty)
        #expect(run.completedGroups == ["negative-01"])

        let kept = Self.tiffNames(Array(SampleFixtures.files.prefix(3)))
        let discarded = Self.tiffNames(Array(SampleFixtures.files.suffix(3)))
        #expect(run.publishedOutputs == kept)
        for name in kept {
            #expect(
                FileManager.default.fileExists(atPath: out.appending(path: name).path),
                "\(name) should have been kept"
            )
        }
        for name in discarded {
            #expect(
                !FileManager.default.fileExists(atPath: out.appending(path: name).path),
                "\(name) belonged to the cancelled negative and must not be published"
            )
        }
        // The cancelled group's staging directory is gone, and the manifest is
        // final rather than left as `running`.
        #expect(try Self.stagingDirectories(in: out).isEmpty)
        let report = try #require(run.manifestReport)
        guard case .final(let manifest) = report else {
            Issue.record("expected a final manifest, got \(report)")
            return
        }
        #expect(manifest.status == "cancelled")
    }
}
