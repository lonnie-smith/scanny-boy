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
            && BareLightReference.isAvailable
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
        if !BareLightReference.isAvailable {
            reasons.append(BareLightReference.unavailableComment.rawValue)
        }
        return Comment(rawValue: reasons.joined(separator: "\n"))
    }

    /// One per-process temporary library database, shared by every session
    /// this suite starts. Without this, the real bundled CLI helper falls
    /// back to `SCANNY_BOY_LIBRARY_DB`'s default — the user's actual
    /// `~/Library/Application Support/ScannyBoy/library.db` — and every run
    /// of this suite would register real rolls and flatfield profiles into
    /// it, which is how a developer ends up with a library full of
    /// `"Integration <uuid>"` profiles from nothing but running tests.
    private static let libraryDatabaseURL: URL = {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "scanny-boy-tests", directoryHint: .isDirectory)
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory.appending(path: "library.db")
    }()

    private static func runner() throws -> CLIRunner {
        CLIRunner(
            executable: try #require(HostBundle.helperExecutableURL),
            environmentOverrides: ["SCANNY_BOY_LIBRARY_DB": libraryDatabaseURL.path]
        )
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
    private static func createRoll() async throws -> URL {
        let library = try Self.makeTemporaryDirectory()
        let runner = try Self.runner()
        let session = runner.session(
            for: .rollInit(library: library, name: "Test Roll \(UUID().uuidString.prefix(8))")
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

    /// `flatfield create` for real, through the CLI, from the synthetic
    /// bare-light DNG (`BareLightReference`) — the app requires a profile on
    /// Add Scans, so every scenario below runs against one that actually
    /// exists. A real film frame must not stand in: its scene content
    /// survives the gain map's smoothing and corrupts the correction, which
    /// is what failed these runs with `STITCH_RESIDUAL_TOO_HIGH`. Nothing
    /// here asserts on falloff.
    private static func createFlatFieldProfile() async throws -> String {
        let runner = try Self.runner()
        let session = runner.session(
            for: .flatfieldCreate(
                reference: BareLightReference.url,
                name: "Integration \(UUID().uuidString.prefix(8))"
            )
        )
        for await output in try await session.start() {
            if case .event(let event) = output, event.kind == .flatfieldCreated,
                let fields = event.flatFieldProfile,
                let profile = FlatFieldProfile(fields: fields)
            {
                return profile.profileID
            }
        }
        Issue.record("flatfield create produced no flatfield_created event")
        throw CocoaError(.fileNoSuchFile)
    }

    /// A configuration model pointed at a staged sample folder (only the
    /// six sample files, so a selection is contiguous in its catalogue) and
    /// a real roll, with its catalogue probe already applied and a real
    /// profile chosen.
    private static func configuredModel(
        roll: URL,
        select: [String]
    ) async throws -> ConfigurationModel {
        let model = ConfigurationModel(
            runner: try runner(), defaults: isolatedDefaults()
        )
        model.inputFolder = try SampleFixtures.stagedDirectory()
        await model.waitForPendingProbes()
        model.rollURL = roll
        model.selectedFiles = Set(select)
        // These scenarios test run/stitch behaviour, not the Add Scans
        // grouping picker, so they choose the grouping up front.
        model.across = 3
        // The roll fetch pre-selects the profile a first run locked the roll
        // to — the app does not let the user choose differently. Only a roll
        // with no profile yet (the first run into it) gets a fresh one.
        await model.waitForPendingProbes()
        if model.flatFieldProfileID == nil {
            model.flatFieldProfileID = try await Self.createFlatFieldProfile()
        }
        await model.waitForPendingProbes()
        return model
    }

    // MARK: - Six sample files at three per negative

    /// Chunk P2-9: the model's Run command is now `run` (convert *and*
    /// stitch), not `convert` alone. `SampleFixtures.files` are Phase 1's
    /// original conversion fixtures (appendix A) — real NEFs, but never shot
    /// to actually overlap, unlike the gate-B stitching scans of appendix C.
    /// Both negatives stitch: `negative-01` (`_DSC4638`-`_DSC4640`) shares
    /// enough film to register plainly, and `negative-02`
    /// (`_DSC4644`-`_DSC4646`) — whose frames share little film — is rescued
    /// by the CLAHE retry (PR #49), which is why this asserts a six-for-six
    /// result today where the pre-CLAHE helper refused `negative-02` with
    /// `STITCH_UNDERCONSTRAINED`.
    @Test(
        "six sample files at three per negative stitch both negatives",
        .enabled(if: RunIntegrationTests.canRun, RunIntegrationTests.unavailable),
        .enabled(if: SlowTests.isEnabled, SlowTests.disabledComment),
        .timeLimit(.minutes(5))
    )
    func sixFilesStitchBothNegatives() async throws {
        let roll = try await Self.createRoll()

        let model = try await Self.configuredModel(roll: roll, select: SampleFixtures.files)
        #expect(model.groups.count == 2)
        #expect(model.runEnabled)

        let run = RunModel(runner: try Self.runner())
        run.start(
            command: try #require(model.runCommand()),
            files: model.selectedFilesInCanonicalOrder,
            outputFolder: roll
        )
        await run.waitForCompletion()

        #expect(run.streamFailures.isEmpty)
        #expect(run.cliError == nil)
        #expect(run.outcome == .success)
        #expect(run.failedGroups.isEmpty)
        #expect(run.failedNegatives.isEmpty)
        #expect(run.completedGroups == ["negative-01", "negative-02"])

        #expect(run.stitchedNegatives.count == 2)
        // Roll negative ids are `<run short id>-negative-NN`, so ids are
        // matched by suffix rather than pinned to a bare `negative-01`.
        // Publishing follows group order: `negative-01`'s first member is
        // `_DSC4638.NEF`, `negative-02`'s is `_DSC4644.NEF`.
        for (index, suffix) in ["-negative-01", "-negative-02"].enumerated() {
            let stitched = run.stitchedNegatives[index]
            #expect(stitched.negativeID.hasSuffix(suffix))
            #expect(stitched.output == Self.tiffNames(SampleFixtures.files)[index * 3])
        }

        let report = try #require(run.rollManifestReport)
        guard case .final(let manifest) = report else {
            Issue.record("expected a final roll manifest, got \(report)")
            return
        }
        #expect(manifest.runs.last?.status == "complete")
        // One stitched TIFF per negative, each named for its first member.
        let published = Self.tiffNames([SampleFixtures.files[0], SampleFixtures.files[3]])
        #expect(manifest.publishedOutputs == published)

        for name in published {
            #expect(
                FileManager.default.fileExists(atPath: roll.appending(path: name).path),
                "the stitched negative \(name) was not published"
            )
        }
        #expect(try Self.stagingDirectories(in: roll).isEmpty)
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

    /// The replacement rule: a selection that overlaps a negative already in
    /// the roll is never rejected outright, and the rerun adopts it in
    /// place — same `negative_id`, same output name.
    @Test(
        "a rerun over the same selection adopts the earlier negative in place",
        .enabled(if: RunIntegrationTests.canRun, RunIntegrationTests.unavailable),
        .enabled(if: SlowTests.isEnabled, SlowTests.disabledComment),
        .timeLimit(.minutes(10))
    )
    func rerunAdoptsTheEarlierNegative() async throws {
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
        let firstNegativeID = try #require(firstRun.stitchedNegatives.first?.negativeID)

        // A second configuration over the same roll and selection overlaps
        // the negative the first run just published.
        let second = try await Self.configuredModel(roll: roll, select: negativeOne)
        #expect(second.rollError == nil)
        let command = try #require(second.runCommand())
        #expect(!command.arguments.contains("--skip-sources"))

        let secondRun = RunModel(runner: try Self.runner())
        secondRun.start(
            command: command,
            files: second.selectedFilesInCanonicalOrder,
            outputFolder: roll
        )
        await secondRun.waitForCompletion()

        #expect(secondRun.outcome == .success)
        #expect(secondRun.stitchedNegatives.count == 1)

        let report = try #require(secondRun.rollManifestReport)
        guard case .final(let manifest) = report else {
            Issue.record("expected a final roll manifest, got \(report)")
            return
        }
        // The same negative survives the rerun: same id, updated by the new
        // run, no tombstone sibling beside it.
        let adopted = try #require(
            manifest.negatives.first { $0.negativeID == firstNegativeID }
        )
        #expect(adopted.status == "completed")
        #expect(adopted.runID == secondRun.runID ?? "")
        #expect(manifest.negatives.count == 1)
    }

    // MARK: - Cancel retains earlier groups and discards the current one

    /// Section 3.6: "Completed groups remain after cancellation. The group
    /// being processed is not published."
    @Test(
        "cancelling after the first negative keeps it and discards the second",
        .enabled(if: RunIntegrationTests.canRun, RunIntegrationTests.unavailable),
        .enabled(if: SlowTests.isEnabled, SlowTests.disabledComment),
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
        // a roll `--out` no longer accepts for `run`. The input is staged:
        // only the six sample files, so the selection is contiguous.
        let command = try CLICommand.prepare(
            input: SampleFixtures.stagedDirectory(),
            files: SampleFixtures.files,
            out: out,
            across: 3,
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
    /// RAW decoding at all. A `run` never keeps the work directory it
    /// creates itself (on any outcome), so this test supplies its own
    /// `--work` directory — the one kind of work directory `run` never
    /// deletes, since deleting a folder the caller pointed at is never this
    /// program's decision — to get one to re-stitch.
    @Test(
        "re-stitching a supplied work directory reuses its intermediates and stitches again",
        .enabled(if: RunIntegrationTests.canRun, RunIntegrationTests.unavailable),
        .enabled(if: SlowTests.isEnabled, SlowTests.disabledComment),
        .timeLimit(.minutes(5))
    )
    func restitchReusesAKeptWorkDirectory() async throws {
        let firstRoll = try await Self.createRoll()
        let secondRoll = try await Self.createRoll()
        let negativeOne = Array(SampleFixtures.files.prefix(3))
        let workDirectory = try Self.makeTemporaryDirectory()

        let model = try await Self.configuredModel(roll: firstRoll, select: negativeOne)

        let firstRun = RunModel(runner: try Self.runner())
        firstRun.start(
            command: CLICommand.run(
                input: SampleFixtures.directory,
                files: model.selectedFilesInCanonicalOrder,
                roll: firstRoll,
                across: model.across,
                skipSources: [],
                work: workDirectory
            ),
            files: model.selectedFilesInCanonicalOrder,
            outputFolder: firstRoll
        )
        await firstRun.waitForCompletion()
        #expect(firstRun.outcome == .success)
        #expect(firstRun.stitchedNegatives.count == 1)
        // A caller-supplied `--work` directory survives regardless of
        // outcome — it's the one kind `run` never deletes.
        #expect(FileManager.default.fileExists(atPath: workDirectory.path))

        let restitch = RunModel(runner: try Self.runner())
        restitch.start(
            command: .stitch(work: workDirectory, roll: secondRoll),
            files: [],
            outputFolder: secondRoll
        )
        await restitch.waitForCompletion()

        #expect(restitch.streamFailures.isEmpty)
        #expect(restitch.cliError == nil)
        #expect(restitch.outcome == .success)
        #expect(restitch.stitchedNegatives.count == 1)
        let stitched = try #require(restitch.stitchedNegatives.first)
        // Same rule: the roll prefixes the run's short id to `negative-01`.
        #expect(stitched.negativeID.hasSuffix("-negative-01"))
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
        #expect(FileManager.default.fileExists(atPath: workDirectory.path))
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
