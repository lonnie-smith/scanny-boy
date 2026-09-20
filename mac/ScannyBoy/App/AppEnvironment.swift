import Foundation

/// Where the app keeps its preferences and state — and the guarantee that a
/// test run never touches the real ones.
///
/// The unit tests are hosted inside the app, so without this they share the
/// user's actual `UserDefaults` domain and `~/Library/Application Support`.
/// A test that set `captureBaseFolder` to a temp directory then left the
/// user's next real capture saving its raw NEFs there, and a hosted app
/// instance restored (and re-ran) the user's persisted stitch queue.
enum AppEnvironment {
    static let isRunningTests: Bool = {
        let environment = ProcessInfo.processInfo.environment
        return environment["XCTestConfigurationFilePath"] != nil
            || environment["XCTestBundlePath"] != nil
            || environment["XCTestSessionIdentifier"] != nil
    }()

    /// The preferences every model defaults to. A throwaway suite, wiped on
    /// first use, under test.
    /// `UserDefaults` is thread-safe; it just isn't declared `Sendable`.
    nonisolated(unsafe) static let defaults: UserDefaults = {
        guard isRunningTests else { return .standard }
        let name = "com.lonniesmith.scanny-boy.tests"
        let suite = UserDefaults(suiteName: name) ?? .standard
        suite.removePersistentDomain(forName: name)
        return suite
    }()

    /// `~/Library/Application Support/ScannyBoy`, or a fresh temp directory
    /// under test.
    static let supportDirectory: URL = {
        if isRunningTests {
            let directory = FileManager.default.temporaryDirectory
                .appending(path: "scanny-boy-test-support-\(UUID().uuidString)", directoryHint: .isDirectory)
            try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            return directory
        }
        return FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appending(path: "ScannyBoy", directoryHint: .isDirectory)
    }()

    /// Points the CLI helper's library database at the throwaway support
    /// directory, so a hosted app instance can't read or write the real one.
    /// Tests that need a specific database pass their own override.
    static func isolateForTestsIfNeeded() {
        guard isRunningTests else { return }
        setenv("SCANNY_BOY_LIBRARY_DB", supportDirectory.appending(path: "library.db").path, 1)
    }
}
