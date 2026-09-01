import Foundation
import Testing

@testable import ScannyBoy

/// Drives `ConfigurationModel` against a fake CLI executable that answers
/// `probe` and `roll info` from canned JSON, matching the pattern
/// `CLISessionTests` and `CLIIntegrationTests` already use for a synthetic
/// helper. No test here touches the real sample NEFs or the bundled helper —
/// those are exercised by the CLI's own test suite and by
/// `RunIntegrationTests`.
@Suite("Configuration model (Chunk 9, reworked onto rolls by Chunk P3-11)")
@MainActor
struct ConfigurationModelTests {
    private static func isolatedDefaults() -> UserDefaults {
        // A fresh, unique suite per test: never `.standard`, and never
        // shared between concurrently running tests (section 7).
        UserDefaults(suiteName: "scanny-boy-tests-\(UUID().uuidString)")!
    }

    /// `TestSupport.withTemporaryDirectory`'s closure-passing shape doesn't
    /// fit here: this suite is `@MainActor` (it drives an `@MainActor`
    /// model), and handing a `@MainActor`-isolated closure to that helper's
    /// plain nonisolated generic parameter trips Swift 6's "sending main
    /// actor-isolated value" diagnostic. A flat create-and-`defer` pair
    /// avoids crossing an isolation boundary at all.
    private static func makeTemporaryDirectory() throws -> URL {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "scanny-boy-tests", directoryHint: .isDirectory)
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    /// A `probe` stand-in that answers from `$*`: `--roll` present routes to
    /// `withFilesAndRoll`, `--files` alone (no `--roll`) routes to
    /// `withFiles`, and a bare `--input` routes to `catalogueOnly` — the same
    /// three call shapes `ConfigurationModel` actually makes. `roll info`
    /// (section 3.1: `roll` is read back through the CLI, never from disk)
    /// is distinguished by its own leading subcommand, `$1`, rather than
    /// folded into the `$*` routing below, which only ever sees `probe`
    /// invocations.
    private static func fakeProbeExecutable(
        in directory: URL,
        catalogueOnly: [String],
        withFiles: [String] = [],
        withFilesAndRoll: [String] = [],
        rollInfo: [String] = []
    ) throws -> URL {
        func echoLines(_ lines: [String]) -> String {
            lines.map { "echo '\($0)'" }.joined(separator: "\n")
        }
        let script = """
            if [ "$1" = "roll" ]; then
            \(echoLines(rollInfo))
            exit 0
            fi
            case "$*" in
              *--roll*)
            \(echoLines(withFilesAndRoll))
                ;;
              *--files*)
            \(echoLines(withFiles))
                ;;
              *)
            \(echoLines(catalogueOnly))
                ;;
            esac
            """
        return try TestSupport.writeTestExecutable(script, in: directory)
    }

    private static let started = #"{"protocol_version":5,"event":"started","command":"probe"}"#
    private static let finishedSuccess =
        #"{"protocol_version":5,"event":"finished","status":"success","exit_status":0}"#
    private static let finishedFailed =
        #"{"protocol_version":5,"event":"finished","status":"failed","exit_status":1}"#

    private static func errorEvent(code: String) -> String {
        #"{"protocol_version":5,"event":"error","code":"\#(code)","message":"synthetic failure"}"#
    }

    // Deliberately not alphabetical: proves the model displays `probe`'s
    // order verbatim instead of re-sorting (section 3.3: "Swift always uses
    // the order it is given and never sorts files itself").
    private static let catalogueUnsorted =
        #"{"protocol_version":5,"event":"probe_result","catalogue":["c.NEF","a.NEF","b.NEF"],"warnings":[],"groups":[]}"#

    private static let catalogueABC =
        #"{"protocol_version":5,"event":"probe_result","catalogue":["a.NEF","b.NEF","c.NEF"],"warnings":[],"groups":[]}"#

    // Raw string literals do not support backslash line-continuation (that
    // is a plain-string-literal escape only), so each of these stays on one
    // line rather than risk a stray literal backslash inside the JSON.
    private static let threeFileGroupNoRoll =
        #"{"protocol_version":5,"event":"probe_result","catalogue":["a.NEF","b.NEF","c.NEF"],"warnings":[],"groups":[["a.NEF","b.NEF","c.NEF"]]}"#

    private static let threeFileGroupNoOverlap =
        #"{"protocol_version":5,"event":"probe_result","catalogue":["a.NEF","b.NEF","c.NEF"],"warnings":[],"groups":[["a.NEF","b.NEF","c.NEF"]],"roll_overlap":[]}"#

    private static let threeFileGroupWithOverlap =
        #"{"protocol_version":5,"event":"probe_result","catalogue":["a.NEF","b.NEF","c.NEF"],"warnings":[],"groups":[["a.NEF","b.NEF","c.NEF"]],"roll_overlap":[{"negative_id":"r-negative-01","expected_output":"a.tif","run_id":"r","overlapping_sources":["a.NEF","b.NEF","c.NEF"],"group_index":0}]}"#

    private static let sixFileNames = ["n1.NEF", "n2.NEF", "n3.NEF", "n4.NEF", "n5.NEF", "n6.NEF"]

    private static let sixFileTwoGroups =
        #"{"protocol_version":5,"event":"probe_result","catalogue":["n1.NEF","n2.NEF","n3.NEF","n4.NEF","n5.NEF","n6.NEF"],"warnings":[],"groups":[["n1.NEF","n2.NEF","n3.NEF"],["n4.NEF","n5.NEF","n6.NEF"]]}"#

    /// A `roll_info` event carrying just enough of `roll-manifest.schema.json`
    /// to be read back by `RollManifest`. Nothing in this suite needs it any
    /// more — the roll record no longer carries a grouping — but the fake
    /// executable still answers `roll info` like the real CLI does.
    private static func rollInfoEvent(rollID: String = "roll-1", rollName: String = "Test Roll") -> String {
        let manifest = """
            {"manifest_format_version":5,"manifest_kind":"roll","scanny_boy_version":"0.3.0",\
            "roll_id":"\(rollID)","roll_name":"\(rollName)",\
            "created_at":"2026-08-02T00:00:00Z","updated_at":"2026-08-02T00:00:00Z",\
            "processing_params":{},\
            "icc_profile":{"name":"x.icc","sha256":"\(String(repeating: "b", count: 64))"},\
            "stitch_params":{},"runs":[],"sources":[],"negatives":[],\
            "metadata":{"roll_capture_date":null,"last_applied_at":null}}
            """
        return #"{"protocol_version":5,"event":"roll_info","manifest":\#(manifest)}"#
    }

    // MARK: - Model state follows probe results

    @Test("The catalogue reflects probe's order verbatim; nothing here re-sorts it")
    func catalogueFollowsProbeOrder() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let executable = try Self.fakeProbeExecutable(
            in: directory,
            catalogueOnly: [Self.started, Self.catalogueUnsorted, Self.finishedSuccess]
        )
        let model = ConfigurationModel(
            runner: CLIRunner(executable: executable), defaults: Self.isolatedDefaults()
        )

        model.inputFolder = directory
        await model.waitForPendingProbes()

        #expect(model.catalogue == ["c.NEF", "a.NEF", "b.NEF"])
        #expect(model.catalogueError == nil)
    }

    // MARK: - A valid six-file, three-per-negative selection shows two groups

    @Test("A valid six-file, three-per-negative selection shows two groups")
    func sixFileSelectionShowsTwoGroups() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let executable = try Self.fakeProbeExecutable(
            in: directory,
            catalogueOnly: [
                Self.started,
                #"{"protocol_version":5,"event":"probe_result","catalogue":["n1.NEF","n2.NEF","n3.NEF","n4.NEF","n5.NEF","n6.NEF"],"warnings":[],"groups":[]}"#,
                Self.finishedSuccess,
            ],
            withFiles: [Self.started, Self.sixFileTwoGroups, Self.finishedSuccess]
        )
        let model = ConfigurationModel(
            runner: CLIRunner(executable: executable), defaults: Self.isolatedDefaults()
        )

        model.inputFolder = directory
        await model.waitForPendingProbes()
        model.selectedFiles = Set(Self.sixFileNames)
        model.perNegative = 3
        await model.waitForPendingProbes()

        #expect(model.groups == [
            ["n1.NEF", "n2.NEF", "n3.NEF"],
            ["n4.NEF", "n5.NEF", "n6.NEF"],
        ])
        #expect(model.selectionError == nil)
    }

    // MARK: - Run remains disabled

    @Test(
        "Run is disabled while the selection itself is invalid",
        arguments: [
            ("NON_CONTIGUOUS_SELECTION", CLICode.nonContiguousSelection),
            ("NOT_DIVISIBLE", CLICode.notDivisible),
            ("CAPTURE_SETTINGS_DIFFER", CLICode.captureSettingsDiffer),
        ]
    )
    func runDisabledForSelectionError(_ scenario: (code: String, expected: CLICode)) async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let executable = try Self.fakeProbeExecutable(
            in: directory,
            catalogueOnly: [Self.started, Self.catalogueABC, Self.finishedSuccess],
            withFiles: [Self.started, Self.errorEvent(code: scenario.code), Self.finishedFailed]
        )
        let model = ConfigurationModel(
            runner: CLIRunner(executable: executable), defaults: Self.isolatedDefaults()
        )

        model.inputFolder = directory
        await model.waitForPendingProbes()
        model.perNegative = 3
        model.selectedFiles = ["a.NEF", "b.NEF", "c.NEF"]
        await model.waitForPendingProbes()

        #expect(model.selectionError?.code == scenario.expected)
        #expect(model.rollError == nil)
        #expect(model.runEnabled == false)
    }

    @Test("Run is disabled until a roll is selected and a grouping is chosen")
    func runDisabledUntilRollSelected() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let rollDir = directory.appending(path: "roll", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: rollDir, withIntermediateDirectories: true)
        let executable = try Self.fakeProbeExecutable(
            in: directory,
            catalogueOnly: [Self.started, Self.catalogueABC, Self.finishedSuccess],
            withFiles: [Self.started, Self.threeFileGroupNoRoll, Self.finishedSuccess],
            withFilesAndRoll: [Self.started, Self.threeFileGroupNoOverlap, Self.finishedSuccess],
            rollInfo: [Self.rollInfoEvent()]
        )
        let model = ConfigurationModel(
            runner: CLIRunner(executable: executable), defaults: Self.isolatedDefaults()
        )

        model.inputFolder = directory
        await model.waitForPendingProbes()
        model.selectedFiles = ["a.NEF", "b.NEF", "c.NEF"]
        await model.waitForPendingProbes()

        #expect(model.rollURL == nil)
        #expect(model.runEnabled == false)

        model.rollURL = rollDir
        await model.waitForPendingProbes()

        // A validated roll and selection is not enough: the batch's
        // scans-per-negative must be chosen first.
        #expect(model.runEnabled == false)

        model.perNegative = 3
        await model.waitForPendingProbes()

        #expect(model.runEnabled == true)
    }

    @Test("Choosing scans-per-negative re-validates the selection")
    func changingPerNegativeRevalidates() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let executable = try Self.fakeProbeExecutable(
            in: directory,
            catalogueOnly: [
                Self.started,
                #"{"protocol_version":5,"event":"probe_result","catalogue":["n1.NEF","n2.NEF","n3.NEF","n4.NEF","n5.NEF","n6.NEF"],"warnings":[],"groups":[]}"#,
                Self.finishedSuccess,
            ],
            withFiles: [Self.started, Self.sixFileTwoGroups, Self.finishedSuccess]
        )
        let model = ConfigurationModel(
            runner: CLIRunner(executable: executable), defaults: Self.isolatedDefaults()
        )

        model.inputFolder = directory
        await model.waitForPendingProbes()
        model.selectedFiles = Set(Self.sixFileNames)
        await model.waitForPendingProbes()

        // No grouping chosen: no groups to preview and nothing validated.
        #expect(model.groups.isEmpty)
        #expect(model.isProbing == false)

        model.perNegative = 3
        await model.waitForPendingProbes()

        #expect(model.groups == [
            ["n1.NEF", "n2.NEF", "n3.NEF"],
            ["n4.NEF", "n5.NEF", "n6.NEF"],
        ])
    }

    @Test("A roll-related probe failure blocks Run without touching the selection error")
    func runDisabledForRollError() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let rollDir = directory.appending(path: "not-a-roll", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: rollDir, withIntermediateDirectories: true)
        let executable = try Self.fakeProbeExecutable(
            in: directory,
            catalogueOnly: [Self.started, Self.catalogueABC, Self.finishedSuccess],
            withFiles: [Self.started, Self.threeFileGroupNoRoll, Self.finishedSuccess],
            withFilesAndRoll: [
                Self.started, Self.errorEvent(code: "ROLL_NOT_FOUND"), Self.finishedFailed,
            ]
        )
        let model = ConfigurationModel(
            runner: CLIRunner(executable: executable), defaults: Self.isolatedDefaults()
        )

        model.inputFolder = directory
        await model.waitForPendingProbes()
        model.rollURL = rollDir
        model.perNegative = 3
        model.selectedFiles = ["a.NEF", "b.NEF", "c.NEF"]
        await model.waitForPendingProbes()

        #expect(model.rollError?.code == .rollNotFound)
        #expect(model.selectionError == nil)
        #expect(model.runEnabled == false)
    }

    // MARK: - Chunk P3-11's additions: rolls and the overlap sheet

    @Test("The run command targets --roll at the batch's chosen grouping, never --film-date or --out")
    func testRunCommandOmitsFilmDateAndOutputFolder() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let rollDir = directory.appending(path: "roll", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: rollDir, withIntermediateDirectories: true)
        let executable = try Self.fakeProbeExecutable(
            in: directory,
            catalogueOnly: [Self.started, Self.catalogueABC, Self.finishedSuccess],
            withFiles: [Self.started, Self.threeFileGroupNoRoll, Self.finishedSuccess],
            withFilesAndRoll: [Self.started, Self.threeFileGroupNoOverlap, Self.finishedSuccess]
        )
        let model = ConfigurationModel(
            runner: CLIRunner(executable: executable), defaults: Self.isolatedDefaults()
        )

        model.inputFolder = directory
        await model.waitForPendingProbes()
        model.rollURL = rollDir
        model.selectedFiles = ["a.NEF", "b.NEF", "c.NEF"]
        model.perNegative = 3
        await model.waitForPendingProbes()

        let command = try #require(model.runCommand())
        #expect(command.arguments.contains("--roll"))
        #expect(command.arguments.contains(rollDir.path))
        #expect(!command.arguments.contains("--film-date"))
        #expect(!command.arguments.contains("--out"))
        #expect(!command.arguments.contains("--overwrite"))
        #expect(command.arguments.contains("--per-negative"))
        let perNegativeIndex = try #require(command.arguments.firstIndex(of: "--per-negative"))
        #expect(command.arguments[perNegativeIndex + 1] == "3")
    }

    @Test("An overlapping selection still runs, and names no --skip-sources")
    func overlappingSelectionRunsWithoutSkipSources() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let rollDir = directory.appending(path: "roll", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: rollDir, withIntermediateDirectories: true)
        let executable = try Self.fakeProbeExecutable(
            in: directory,
            catalogueOnly: [Self.started, Self.catalogueABC, Self.finishedSuccess],
            withFiles: [Self.started, Self.threeFileGroupNoRoll, Self.finishedSuccess],
            withFilesAndRoll: [Self.started, Self.threeFileGroupWithOverlap, Self.finishedSuccess],
            rollInfo: [Self.rollInfoEvent()]
        )
        let model = ConfigurationModel(
            runner: CLIRunner(executable: executable), defaults: Self.isolatedDefaults()
        )

        model.inputFolder = directory
        await model.waitForPendingProbes()
        model.rollURL = rollDir
        model.selectedFiles = ["a.NEF", "b.NEF", "c.NEF"]
        model.perNegative = 3
        await model.waitForPendingProbes()

        // Overlapping a negative already in the roll is never a reason to
        // withhold the Run command — every group runs and supersedes
        // whatever it overlaps.
        let command = try #require(model.runCommand())
        #expect(!command.arguments.contains("--skip-sources"))
    }

    // MARK: - Last-folder memory

    @Test("The input folder is remembered across model instances")
    func inputFolderIsPersisted() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let executable = try Self.fakeProbeExecutable(
            in: directory,
            catalogueOnly: [Self.started, Self.catalogueABC, Self.finishedSuccess]
        )
        let defaults = Self.isolatedDefaults()

        let first = ConfigurationModel(runner: CLIRunner(executable: executable), defaults: defaults)
        first.inputFolder = directory
        await first.waitForPendingProbes()

        let second = ConfigurationModel(runner: CLIRunner(executable: executable), defaults: defaults)
        await second.waitForPendingProbes()

        #expect(second.inputFolder?.standardizedFileURL == directory.standardizedFileURL)
        #expect(second.catalogue == ["a.NEF", "b.NEF", "c.NEF"])
    }
}
