import Foundation
import Testing

@testable import ScannyBoy

/// Drives `ConfigurationModel` against a fake CLI executable that answers
/// `probe` from canned JSON, matching the pattern `CLISessionTests` and
/// `CLIIntegrationTests` already use for a synthetic helper. No test here
/// touches the real sample NEFs or the bundled helper — those are exercised
/// by the CLI's own test suite and by `CLIIntegrationTests`.
@Suite("Configuration model (Chunk 9)")
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

    /// A `probe` stand-in that answers from `$*`: `--out` present routes to
    /// `withFilesAndOut`, `--files` alone (no `--out`) routes to
    /// `withFiles`, and a bare `--input` routes to `catalogueOnly` — the same
    /// three call shapes `ConfigurationModel` actually makes.
    private static func fakeProbeExecutable(
        in directory: URL,
        catalogueOnly: [String],
        withFiles: [String] = [],
        withFilesAndOut: [String] = []
    ) throws -> URL {
        func echoLines(_ lines: [String]) -> String {
            lines.map { "echo '\($0)'" }.joined(separator: "\n")
        }
        let script = """
            case "$*" in
              *--out*)
            \(echoLines(withFilesAndOut))
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

    private static let started = #"{"protocol_version":1,"event":"started","command":"probe"}"#
    private static let finishedSuccess =
        #"{"protocol_version":1,"event":"finished","status":"success","exit_status":0}"#
    private static let finishedFailed =
        #"{"protocol_version":1,"event":"finished","status":"failed","exit_status":1}"#

    private static func errorEvent(code: String) -> String {
        #"{"protocol_version":1,"event":"error","code":"\#(code)","message":"synthetic failure"}"#
    }

    // Deliberately not alphabetical: proves the model displays `probe`'s
    // order verbatim instead of re-sorting (section 3.3: "Swift always uses
    // the order it is given and never sorts files itself").
    private static let catalogueUnsorted =
        #"{"protocol_version":1,"event":"probe_result","catalogue":["c.NEF","a.NEF","b.NEF"],"warnings":[],"groups":[]}"#

    private static let catalogueABC =
        #"{"protocol_version":1,"event":"probe_result","catalogue":["a.NEF","b.NEF","c.NEF"],"warnings":[],"groups":[]}"#

    // Raw string literals do not support backslash line-continuation (that
    // is a plain-string-literal escape only), so each of these stays on one
    // line rather than risk a stray literal backslash inside the JSON.
    private static let threeFileGroupNoOut =
        #"{"protocol_version":1,"event":"probe_result","catalogue":["a.NEF","b.NEF","c.NEF"],"warnings":[],"groups":[["a.NEF","b.NEF","c.NEF"]]}"#

    private static let threeFileGroupNoConflicts =
        #"{"protocol_version":1,"event":"probe_result","catalogue":["a.NEF","b.NEF","c.NEF"],"warnings":[],"groups":[["a.NEF","b.NEF","c.NEF"]],"output_conflicts":[],"estimated_required_bytes":1000,"available_bytes":50000}"#

    private static let threeFileGroupWithConflicts =
        #"{"protocol_version":1,"event":"probe_result","catalogue":["a.NEF","b.NEF","c.NEF"],"warnings":[],"groups":[["a.NEF","b.NEF","c.NEF"]],"output_conflicts":["a.tif"],"estimated_required_bytes":1000,"available_bytes":50000}"#

    private static let sixFileNames = ["n1.NEF", "n2.NEF", "n3.NEF", "n4.NEF", "n5.NEF", "n6.NEF"]

    private static let sixFileTwoGroups =
        #"{"protocol_version":1,"event":"probe_result","catalogue":["n1.NEF","n2.NEF","n3.NEF","n4.NEF","n5.NEF","n6.NEF"],"warnings":[],"groups":[["n1.NEF","n2.NEF","n3.NEF"],["n4.NEF","n5.NEF","n6.NEF"]]}"#

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
                #"{"protocol_version":1,"event":"probe_result","catalogue":["n1.NEF","n2.NEF","n3.NEF","n4.NEF","n5.NEF","n6.NEF"],"warnings":[],"groups":[]}"#,
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
        model.selectedFiles = ["a.NEF", "b.NEF", "c.NEF"]
        await model.waitForPendingProbes()

        #expect(model.selectionError?.code == scenario.expected)
        #expect(model.outputError == nil)
        #expect(model.runEnabled == false)
    }

    @Test("Run is disabled while the film date is blank, and enables once it is well formed")
    func runDisabledForBlankFilmDate() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let outputDir = directory.appending(path: "out", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: outputDir, withIntermediateDirectories: true)
        let executable = try Self.fakeProbeExecutable(
            in: directory,
            catalogueOnly: [Self.started, Self.catalogueABC, Self.finishedSuccess],
            withFiles: [Self.started, Self.threeFileGroupNoOut, Self.finishedSuccess],
            withFilesAndOut: [Self.started, Self.threeFileGroupNoConflicts, Self.finishedSuccess]
        )
        let model = ConfigurationModel(
            runner: CLIRunner(executable: executable), defaults: Self.isolatedDefaults()
        )

        model.inputFolder = directory
        await model.waitForPendingProbes()
        model.outputFolder = outputDir
        model.selectedFiles = ["a.NEF", "b.NEF", "c.NEF"]
        await model.waitForPendingProbes()

        #expect(model.filmDate.isEmpty)
        #expect(model.runEnabled == false)

        model.filmDate = "2026-08-02"
        #expect(model.runEnabled == true)

        model.filmDate = "not-a-date"
        #expect(model.runEnabled == false)
    }

    @Test("Choosing an output folder equal to the input folder is blocked")
    func runDisabledForOutputFolderSameAsInput() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let executable = try Self.fakeProbeExecutable(
            in: directory,
            catalogueOnly: [Self.started, Self.catalogueABC, Self.finishedSuccess],
            withFiles: [Self.started, Self.threeFileGroupNoOut, Self.finishedSuccess],
            withFilesAndOut: [
                Self.started, Self.errorEvent(code: "OUTPUT_SAME_AS_INPUT"), Self.finishedFailed,
            ]
        )
        let model = ConfigurationModel(
            runner: CLIRunner(executable: executable), defaults: Self.isolatedDefaults()
        )

        model.inputFolder = directory
        await model.waitForPendingProbes()
        model.outputFolder = directory
        model.selectedFiles = ["a.NEF", "b.NEF", "c.NEF"]
        await model.waitForPendingProbes()

        #expect(model.outputError?.code == .outputSameAsInput)
        #expect(model.selectionError == nil)
        #expect(model.runEnabled == false)
    }

    @Test("Run is disabled until an overwrite conflict is confirmed")
    func runDisabledUntilOverwriteConfirmed() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let outputDir = directory.appending(path: "out", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: outputDir, withIntermediateDirectories: true)
        let executable = try Self.fakeProbeExecutable(
            in: directory,
            catalogueOnly: [Self.started, Self.catalogueABC, Self.finishedSuccess],
            withFiles: [Self.started, Self.threeFileGroupNoOut, Self.finishedSuccess],
            withFilesAndOut: [Self.started, Self.threeFileGroupWithConflicts, Self.finishedSuccess]
        )
        let model = ConfigurationModel(
            runner: CLIRunner(executable: executable), defaults: Self.isolatedDefaults()
        )

        model.inputFolder = directory
        await model.waitForPendingProbes()
        model.outputFolder = outputDir
        model.selectedFiles = ["a.NEF", "b.NEF", "c.NEF"]
        model.filmDate = "2026-08-02"
        await model.waitForPendingProbes()

        #expect(model.outputConflicts == ["a.tif"])
        #expect(model.runEnabled == false)

        model.confirmOverwrite()
        #expect(model.runEnabled == true)
    }

    // MARK: - Last-folder memory

    @Test("The input and output folders are remembered across model instances")
    func foldersArePersisted() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let outputDir = directory.appending(path: "out", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: outputDir, withIntermediateDirectories: true)
        let executable = try Self.fakeProbeExecutable(
            in: directory,
            catalogueOnly: [Self.started, Self.catalogueABC, Self.finishedSuccess]
        )
        let defaults = Self.isolatedDefaults()

        let first = ConfigurationModel(runner: CLIRunner(executable: executable), defaults: defaults)
        first.inputFolder = directory
        first.outputFolder = outputDir
        await first.waitForPendingProbes()

        let second = ConfigurationModel(runner: CLIRunner(executable: executable), defaults: defaults)
        await second.waitForPendingProbes()

        #expect(second.inputFolder?.standardizedFileURL == directory.standardizedFileURL)
        #expect(second.outputFolder?.standardizedFileURL == outputDir.standardizedFileURL)
        #expect(second.catalogue == ["a.NEF", "b.NEF", "c.NEF"])
    }
}
