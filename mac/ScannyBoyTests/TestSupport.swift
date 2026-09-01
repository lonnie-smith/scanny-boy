import Foundation
import Testing

@testable import ScannyBoy

/// Builds CLI event lines at the protocol version the app supports, so a
/// protocol bump changes one constant (plus `events.py` and `schema.json`)
/// instead of dozens of fixture literals. Deliberate wrong-version fixtures
/// (rejection tests in `CLIEventTests`) keep their literals.
enum TestEvents {
    static let version = CLIEvent.supportedProtocolVersion

    /// `json` is a full JSON object *without* `protocol_version`; the
    /// version is spliced in right after the opening brace.
    static func line(_ json: String) -> String {
        guard let firstBrace = json.firstIndex(of: "{") else {
            fatalError("TestEvents.line requires a JSON object starting with '{'")
        }
        let afterBrace = json.index(after: firstBrace)
        return String(json[json.startIndex...firstBrace])
            + "\"protocol_version\":\(version),"
            + String(json[afterBrace...])
    }
}

/// Shared helpers for the Swift tests.
///
/// Paths are resolved from this source file's own location, never from the
/// current working directory, so the tests behave the same however
/// `xcodebuild` was invoked — the same rule `docs/IMPLEMENTATION_PLAN.md`
/// section 7 sets for the Python tests.
enum TestSupport {
    /// The repository root: `mac/ScannyBoyTests/` is two levels below it.
    static let repositoryRoot: URL = URL(filePath: #filePath)
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()

    /// Runs `body` with a fresh, uniquely named temporary directory and
    /// removes it afterwards. Section 7: isolated temporary directories, and
    /// no filename shared between concurrently running tests.
    static func withTemporaryDirectory<T>(
        _ body: (URL) async throws -> T
    ) async throws -> T {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "scanny-boy-tests", directoryHint: .isDirectory)
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(
            at: directory,
            withIntermediateDirectories: true
        )
        defer { try? FileManager.default.removeItem(at: directory) }
        return try await body(directory)
    }

    /// Writes an executable `/bin/sh` script and returns its path.
    ///
    /// The session tests drive a real child process through real pipes; a
    /// script is the cheapest executable that can produce exactly the bytes,
    /// timing, signal handling, and exit status each case needs.
    @discardableResult
    static func writeTestExecutable(
        _ script: String,
        named name: String = "fake-scanny-boy",
        in directory: URL
    ) throws -> URL {
        let url = directory.appending(path: name, directoryHint: .notDirectory)
        let contents = "#!/bin/sh\n" + script
        try contents.write(to: url, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o755],
            ofItemAtPath: url.path
        )
        return url
    }

    /// Collects a session's whole output stream.
    static func drain(
        _ stream: AsyncStream<CLISessionOutput>
    ) async -> [CLISessionOutput] {
        var collected: [CLISessionOutput] = []
        for await output in stream {
            collected.append(output)
        }
        return collected
    }
}

extension [CLISessionOutput] {
    var events: [CLIEvent] {
        compactMap { if case .event(let event) = $0 { event } else { nil } }
    }

    var logs: [String] {
        compactMap { if case .log(let line) = $0 { line } else { nil } }
    }

    var failures: [CLISessionFailure] {
        compactMap { if case .failure(let failure) = $0 { failure } else { nil } }
    }

    var completions: [CLICompletion] {
        compactMap { if case .completed(let completion) = $0 { completion } else { nil } }
    }

    /// The single completion the stream must end with, or `nil` if the stream
    /// did not end that way.
    var terminalCompletion: CLICompletion? {
        guard completions.count == 1, case .completed(let completion) = last else {
            return nil
        }
        return completion
    }
}

/// The real sample NEFs, which are ignored by Git and absent from CI.
enum SampleFixtures {
    static let directory: URL = TestSupport.repositoryRoot
        .appending(path: "tests/fixtures/nef", directoryHint: .isDirectory)

    static let files = [
        "_DSC4638.NEF",
        "_DSC4639.NEF",
        "_DSC4640.NEF",
        "_DSC4644.NEF",
        "_DSC4645.NEF",
        "_DSC4646.NEF",
    ]

    static var areAvailable: Bool {
        files.allSatisfy {
            FileManager.default.isReadableFile(
                atPath: directory.appending(path: $0).path
            )
        }
    }

    /// Why a test that needs them was skipped, naming what went untested.
    static let unavailableComment: Comment = """
        The real sample NEFs are not present at tests/fixtures/nef/ (see \
        docs/IMPLEMENTATION_PLAN.md appendix A). The end-to-end run of the \
        bundled helper — a forced stop, the `running` manifest and staging \
        directory it leaves behind, and the rerun that recovers them — did \
        not run.
        """
}

/// The app bundle hosting these tests, and the helper nested inside it.
enum HostBundle {
    static var appURL: URL? {
        let url = Bundle.main.bundleURL
        return url.pathExtension == "app" ? url : nil
    }

    static var helperExecutableURL: URL? {
        guard let appURL else { return nil }
        return CLILocator(
            bundleURL: appURL,
            environment: [:],
            configuration: .release
        ).bundledExecutableURL
    }

    static var isAvailable: Bool {
        guard let helperExecutableURL else { return false }
        return FileManager.default.isExecutableFile(atPath: helperExecutableURL.path)
    }

    static let unavailableComment: Comment = """
        These tests must be hosted by the built ScannyBoy.app with the CLI \
        helper staged inside Contents/Helpers. Run ./scripts/build-cli.sh, \
        regenerate the project, and test again.
        """
}

/// Runs a command and returns its exit status and combined output. Used only
/// to invoke `codesign`.
@discardableResult
func runTool(_ launchPath: String, _ arguments: [String]) throws -> (
    status: Int32, output: String
) {
    let process = Process()
    process.executableURL = URL(filePath: launchPath)
    process.arguments = arguments
    let pipe = Pipe()
    process.standardOutput = pipe
    process.standardError = pipe
    try process.run()
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
    return (process.terminationStatus, String(decoding: data, as: UTF8.self))
}
