import Foundation
import Testing

@testable import ScannyBoy

/// The Chunk 10 run flow driven end to end: the real bundled helper, the real
/// sample NEFs, `ConfigurationModel` deciding what may run, and `RunModel`
/// running it. Chunk P3-11 ports every scenario onto a real roll (`roll
/// init`, then `run --roll`) rather than a bare output folder — every run
/// here targets a roll `Self.createRoll()` creates for real, through the CLI,
/// exactly as the app does.
///
/// These are the automated half of Chunk 10's manual-verification list. They
/// prove the behaviour; they do not stand in for the user's own sign-off on
/// replacement and cancellation in the finished app, which is approval point
/// 4 of section 8.
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
                the rerun that overlaps, and cooperative cancellation \
                keeping earlier negatives — did not run.
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

    /// `roll init` for real, through the CLI — every scenario below targets a
    /// roll it actually created, exactly as the app does (section 3.1: Swift
    /// never invents a roll folder of its own).
    private static func createRoll(perNegative: Int = 3) async throws -> URL {
        let library = try Self.makeTemporaryDirectory()
        let runner = try Self.runner()
        let session = runner.session(
            for: .rollInit(
                library: library, name: "Test Roll \(UUID().uuidString.prefix(8))",
                perNegative: perNegative
            )
        )
        for await output in try await session.start() {
            if case .event(let event) = output, event.kind == .rollCreated, let path = event.rollPath {
                return URL(filePath: path)
            }
        }
        Issue.record("roll init produced no roll_created event")
        throw CocoaError(.fileNoSuchFile)
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

    /// A configuration model pointed at the sample folder and a real roll,
    /// with its catalogue probe already applied.
    private static func configuredModel(
        roll: URL,
        select: [String]
    ) async throws -> ConfigurationModel {
        let model = ConfigurationModel(
            runner: try runner(), defaults: isolatedDefaults()
        )
        model.inputFolder = SampleFixtures.directory
        await model.waitForPendingProbes()
        model.rollURL = roll
        model.selectedFiles = Set(select)
        await model.waitForPendingProbes()
        return model
    }

    // MARK: - Six sample files at three per negative

    /// Chunk P2-9: the model's Run command is now `run` (convert *and*
    /// stitch), not `convert` alone. `SampleFixtures.files` are Phase 1's
    /// original conversion fixtures (appendix A) — real NEFs, but never shot
    /// to actually overlap, unlike the gate-B stitching scans of appendix C.
    /// `negative-01` (`_DSC4638`-`_DSC4640`) does overlap and stitches
    /// cleanly; `negative-02` (`_DSC4644`-`_DSC4646`) genuinely does not
    /// share film and is refused with `STITCH_UNDERCONSTRAINED` — exactly
    /// the real, already-observed behaviour this test asserts, rather than a
    /// six-for-six result these fixtures were never meant to produce.
    @Test(
        "six sample files at three per negative stitch one negative and refuse the other",
        .enabled(if: RunIntegrationTests.canRun, RunIntegrationTests.unavailable),
        .timeLimit(.minutes(5))
    )
    func sixFilesStitchOneNegativeAndRefuseTheOther() async throws {
        let roll = try await Self.createRoll()

        let model = try await Self.configuredModel(roll: roll, select: SampleFixtures.files)
        #expect(model.groups.count == 2)
        #expect(model.runEnabled)
        #expect(!model.needsOverlapReview)

        let run = RunModel(runner: try Self.runner())
        run.start(
            command: try #require(model.runCommand()),
            files: model.selectedFilesInCanonicalOrder,
            outputFolder: roll
        )
        await run.waitForCompletion()

        #expect(run.streamFailures.isEmpty)
        #expect(run.cliError == nil)
        // A negative failed, so the run as a whole is not a success — the
        // CLI's own `partial` status maps to exit 1 (section 3.5).
        #expect(run.outcome == .failure)
        // The convert stage itself succeeds for every frame; only the
        // stitch stage's registration fails, and only for one negative.
        #expect(run.failedGroups.isEmpty)
        #expect(run.completedGroups == ["negative-01", "negative-02"])

        #expect(run.stitchedNegatives.count == 1)
        let stitched = try #require(run.stitchedNegatives.first)
        #expect(stitched.negativeID == "negative-01")
        #expect(stitched.output == Self.tiffNames(SampleFixtures.files)[0])

        #expect(run.failedNegatives.count == 1)
        let failed = try #require(run.failedNegatives.first)
        #expect(failed.groupID == "negative-02")
        #expect(failed.code == .stitchUnderconstrained)

        let report = try #require(run.rollManifestReport)
        guard case .final(let manifest) = report else {
            Issue.record("expected a final roll manifest, got \(report)")
            return
        }
        #expect(manifest.runs.last?.status == "partial")
        #expect(manifest.publishedOutputs == [Self.tiffNames(SampleFixtures.files)[0]])

        #expect(
            FileManager.default.fileExists(
                atPath: roll.appending(path: Self.tiffNames(SampleFixtures.files)[0]).path
            ),
            "the stitched negative was not published"
        )
        #expect(try Self.stagingDirectories(in: roll).isEmpty)
        // A negative failed, so the work directory is kept (section 3.5).
        #expect(run.keptWorkDirectory != nil)
    }

    // MARK: - Blocked selections

    @Test(
        "a five-file selection at three per negative is blocked",
        .enabled(if: RunIntegrationTests.canRun, RunIntegrationTests.unavailable),
        .timeLimit(.minutes(5))
    )
    func fiveFileSelectionIsBlocked() async throws {
        let roll = try await Self.createRoll()
        let model = try await Self.configuredModel(
            roll: roll, select: Array(SampleFixtures.files.prefix(5))
        )

        #expect(model.selectionError?.code == .notDivisible)
        #expect(!model.runEnabled)
        #expect(model.runCommand() == nil)
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
        let roll = try await Self.createRoll()
        let withGap = [0, 1, 3, 4, 5].map { SampleFixtures.files[$0] }
        let model = try await Self.configuredModel(roll: roll, select: withGap)

        #expect(model.selectionError?.code == .nonContiguousSelection)
        #expect(!model.runEnabled)
        #expect(model.runCommand() == nil)
    }

    @Test(
        "a roll folder that was never created is blocked",
        .enabled(if: RunIntegrationTests.canRun, RunIntegrationTests.unavailable),
        .timeLimit(.minutes(5))
    )
    func missingRollIsBlocked() async throws {
        let directory = try Self.makeTemporaryDirectory()
        let notARoll = directory.appending(path: "not-a-roll", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: notARoll, withIntermediateDirectories: true)

        let model = try await Self.configuredModel(roll: notARoll, select: SampleFixtures.files)

        #expect(model.rollError?.code == .rollNotFound)
        #expect(!model.runEnabled)
        #expect(model.runCommand() == nil)
    }

    // MARK: - Rerunning against a roll that already holds the negative

    /// Section 3.4: a selection that overlaps a negative already in the roll
    /// is never rejected outright — the overlap sheet decides, defaulting to
    /// Skip. Left at that default, every overlapping source is skipped, so a
    /// rerun with nothing left to convert fails safely with `NO_FILES`
    /// rather than silently touching the first run's negative.
    @Test(
        "a rerun left at the overlap sheet's Skip default touches nothing",
        .enabled(if: RunIntegrationTests.canRun, RunIntegrationTests.unavailable),
        .timeLimit(.minutes(10))
    )
    func rerunLeftAtSkipDefaultTouchesNothing() async throws {
        let roll = try await Self.createRoll()
        let negativeOne = Array(SampleFixtures.files.prefix(3))

        let first = try await Self.configuredModel(roll: roll, select: negativeOne)
        let firstRun = RunModel(runner: try Self.runner())
        firstRun.start(
            command: try #require(first.runCommand()),
            files: first.selectedFilesInCanonicalOrder,
            outputFolder: roll
        )
        await firstRun.waitForCompletion()
        #expect(firstRun.outcome == .success)
        #expect(firstRun.stitchedNegatives.count == 1)

        // A second configuration over the same roll and selection reports
        // the overlap rather than rejecting it outright.
        let second = try await Self.configuredModel(roll: roll, select: negativeOne)
        #expect(second.rollError == nil)
        #expect(second.needsOverlapReview)
        #expect(second.rollOverlap.count == 1)
        #expect(second.rollOverlap.first?.overlappingSources.sorted() == negativeOne.sorted())

        // Left at the sheet's own Skip default (`OverlapReview`), every
        // overlapping source is skipped.
        let review = OverlapReview(entries: second.rollOverlap)
        let command = try #require(second.runCommand(skipSources: review.skipSources))
        #expect(!command.arguments.contains("--overwrite"))

        let secondRun = RunModel(runner: try Self.runner())
        secondRun.start(
            command: command,
            files: second.selectedFilesInCanonicalOrder,
            outputFolder: roll
        )
        await secondRun.waitForCompletion()

        // Nothing was left to convert once every source was skipped.
        #expect(secondRun.outcome == .failure)
        #expect(secondRun.cliError?.code == .noFiles)
        #expect(secondRun.stitchedNegatives.isEmpty)
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

        // A plain `convert`, not `run` — this test is about `RunModel`'s own
        // cancellation bookkeeping over `item_done`/`group_done`, not about
        // rolls at all, so it keeps Phase 1's bare output folder rather than
        // a roll `--out` no longer accepts for `run`.
        let command = CLICommand.convert(
            input: SampleFixtures.directory,
            files: SampleFixtures.files,
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
            files: SampleFixtures.files,
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

    // MARK: - Chunk P2-10's additions: re-stitch

    /// The whole point of a kept work directory: re-stitching it costs no
    /// RAW decoding at all. `--keep-intermediates` keeps the work directory
    /// from `run` even though every negative succeeds, so this test does not
    /// have to rely on a failure to get one to re-stitch.
    @Test(
        "re-stitching a kept work directory reuses its intermediates and stitches again",
        .enabled(if: RunIntegrationTests.canRun, RunIntegrationTests.unavailable),
        .timeLimit(.minutes(5))
    )
    func restitchReusesAKeptWorkDirectory() async throws {
        let firstRoll = try await Self.createRoll()
        let secondRoll = try await Self.createRoll()
        let negativeOne = Array(SampleFixtures.files.prefix(3))

        let model = try await Self.configuredModel(roll: firstRoll, select: negativeOne)
        model.keepIntermediates = true
        await model.waitForPendingProbes()

        let firstRun = RunModel(runner: try Self.runner())
        firstRun.start(
            command: try #require(model.runCommand()),
            files: model.selectedFilesInCanonicalOrder,
            outputFolder: firstRoll
        )
        await firstRun.waitForCompletion()
        #expect(firstRun.outcome == .success)
        #expect(firstRun.stitchedNegatives.count == 1)
        // Section 3.5: `--keep-intermediates` keeps the work directory even
        // on complete success.
        let workDirectory = try #require(firstRun.keptWorkDirectory)
        #expect(FileManager.default.fileExists(atPath: workDirectory))

        let restitch = RunModel(runner: try Self.runner())
        restitch.start(
            command: .stitch(work: URL(filePath: workDirectory), roll: secondRoll),
            files: [],
            outputFolder: secondRoll
        )
        await restitch.waitForCompletion()

        #expect(restitch.streamFailures.isEmpty)
        #expect(restitch.cliError == nil)
        #expect(restitch.outcome == .success)
        #expect(restitch.stitchedNegatives.count == 1)
        let stitched = try #require(restitch.stitchedNegatives.first)
        #expect(stitched.negativeID == "negative-01")
        #expect(stitched.output == Self.tiffNames(negativeOne)[0])
        #expect(
            FileManager.default.fileExists(
                atPath: secondRoll.appending(path: Self.tiffNames(negativeOne)[0]).path
            ),
            "the re-stitched negative was not published into the second roll"
        )

        let report = try #require(restitch.rollManifestReport)
        guard case .final(let manifest) = report else {
            Issue.record("expected a final roll manifest, got \(report)")
            return
        }
        #expect(manifest.runs.last?.status == "complete")
        // A plain `stitch` did not create the work directory, so it is never
        // this run's to remove — it must still be there afterwards.
        #expect(FileManager.default.fileExists(atPath: workDirectory))
    }

    /// The error path Chunk P2-10 asks for: a folder that never held a work
    /// manifest at all. `WORK_MANIFEST_UNUSABLE` is for a manifest that
    /// exists but is in the wrong state (`running`/`cancelled`, or `partial`
    /// without `--allow-partial`); a folder with no manifest file fails the
    /// same way any other missing/unreadable manifest does, `BAD_MANIFEST` —
    /// before `stitch` ever looks at whether `--roll` itself is real.
    @Test(
        "re-stitching a folder with no work manifest fails safely",
        .enabled(if: RunIntegrationTests.canRun, RunIntegrationTests.unavailable),
        .timeLimit(.minutes(1))
    )
    func restitchWithNoWorkManifestFails() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let notAWorkDirectory = directory.appending(path: "empty", directoryHint: .isDirectory)
        let out = directory.appending(path: "out", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(
            at: notAWorkDirectory, withIntermediateDirectories: true
        )
        try FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)

        let restitch = RunModel(runner: try Self.runner())
        restitch.start(
            command: .stitch(work: notAWorkDirectory, roll: out),
            files: [],
            outputFolder: out
        )
        await restitch.waitForCompletion()

        #expect(restitch.streamFailures.isEmpty)
        #expect(restitch.outcome == .failure)
        #expect(restitch.cliError?.code == .badManifest)
        #expect(restitch.stitchedNegatives.isEmpty)
    }
}
