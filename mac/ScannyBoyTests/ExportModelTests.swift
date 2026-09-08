import Foundation
import Testing

@testable import ScannyBoy

/// Drives `ExportModel` against a fake CLI executable, in the same style
/// as `RunModelTests` and `ConfigurationModelTests`: a `/bin/sh` script is
/// the cheapest thing that can emit exactly the bytes and record the argv
/// each case needs. The real helper is exercised by `CLIIntegrationTests`
/// and the CLI's own suite.
@Suite("Export model")
@MainActor
struct ExportModelTests {
    private static func isolatedDefaults() -> UserDefaults {
        UserDefaults(suiteName: "scanny-boy-tests-\(UUID().uuidString)")!
    }

    private static func makeTemporaryDirectory() throws -> URL {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "scanny-boy-tests", directoryHint: .isDirectory)
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    /// Appends the invocation's argv to `argvPath` when given, then emits a
    /// clean one-negative export stream.
    private static func fakeExportExecutable(
        argvPath: String?,
        in directory: URL
    ) throws -> URL {
        var script = ""
        if let argvPath {
            script += "printf '%s\\n' \"$@\" >> '\(argvPath)'\n"
        }
        script += """
            echo '\(TestEvents.line(#"{"event":"started","command":"export"}"#))'
            echo '\(TestEvents.line(
                #"{"event":"export_done","negative_id":"n1","output":"n1.jxl","width":6048,"height":4032}"#
            ))'
            echo '\(TestEvents.line(#"{"event":"finished","status":"success","exit_status":0}"#))'
            exit 0
            """
        return try TestSupport.writeTestExecutable(script, in: directory)
    }

    @Test("The downsampling choice defaults to none")
    func defaultsToNoDownsample() {
        let model = ExportModel(
            runner: CLIRunner(executable: URL(filePath: "/bin/true"))
        )
        #expect(model.downsampleLongEdge == nil)
    }

    @Test("An explicit choice survives a relaunch")
    func choiceIsPersisted() {
        let defaults = Self.isolatedDefaults()
        let executable = URL(filePath: "/bin/true")

        let first = ExportModel(runner: CLIRunner(executable: executable), defaults: defaults)
        first.downsampleLongEdge = 6048

        let second = ExportModel(runner: CLIRunner(executable: executable), defaults: defaults)
        #expect(second.downsampleLongEdge == 6048)

        second.downsampleLongEdge = nil
        let third = ExportModel(runner: CLIRunner(executable: executable), defaults: defaults)
        #expect(third.downsampleLongEdge == nil)
    }

    @Test("The chosen long edge reaches the export invocation")
    func downsampleReachesTheCommand() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let argvPath = directory.appending(path: "argv", directoryHint: .notDirectory)
        let executable = try Self.fakeExportExecutable(argvPath: argvPath.path, in: directory)
        let model = ExportModel(
            runner: CLIRunner(executable: executable), defaults: Self.isolatedDefaults()
        )
        model.downsampleLongEdge = 9072

        let roll = directory.appending(path: "roll", directoryHint: .isDirectory)
        let output = directory.appending(path: "export", directoryHint: .isDirectory)
        model.outputDirectory = output
        model.export(roll: roll, output: output)
        await model.waitForCompletion()

        let argv = try String(contentsOf: argvPath, encoding: .utf8)
        #expect(argv.contains("--downsample"))
        #expect(argv.contains("9072"))
    }

    @Test("No choice, no --downsample on the invocation")
    func noDownsampleOmitsTheFlag() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let argvPath = directory.appending(path: "argv", directoryHint: .notDirectory)
        let executable = try Self.fakeExportExecutable(argvPath: argvPath.path, in: directory)
        let model = ExportModel(
            runner: CLIRunner(executable: executable), defaults: Self.isolatedDefaults()
        )

        let roll = directory.appending(path: "roll", directoryHint: .isDirectory)
        let output = directory.appending(path: "export", directoryHint: .isDirectory)
        model.outputDirectory = output
        model.export(roll: roll, output: output)
        await model.waitForCompletion()

        let argv = try String(contentsOf: argvPath, encoding: .utf8)
        #expect(!argv.contains("--downsample"))
    }
}
