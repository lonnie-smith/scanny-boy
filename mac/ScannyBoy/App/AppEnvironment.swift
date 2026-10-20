import Foundation

/// Where the app keeps its preferences, state, caches and default folders —
/// and the guarantee that neither a development build nor a test run ever
/// touches the ones belonging to the app installed in `~/Applications`.
///
/// There are three profiles:
///
/// - **production** — a Release build (what `scripts/install-mac.sh`
///   installs). The real `UserDefaults` domain,
///   `~/Library/Application Support/ScannyBoy`, `~/Pictures/Scanny Boy`.
/// - **development** — any Debug build run from Xcode or `xcodebuild`. Its
///   own preferences suite, `~/Library/Application Support/ScannyBoy-Dev`,
///   `~/Library/Caches/ScannyBoy-Dev`, `~/Pictures/Scanny Boy Dev` and
///   `~/Pictures/Scanny Boy Dev Captures`. It shares nothing with the
///   installed app, so the two can be used side by side.
/// - **test** — the unit tests, which are hosted inside the app. Everything
///   lives under throwaway temp directories and a throwaway preferences
///   suite. Without this a test that set `captureBaseFolder` to a temp
///   directory left the user's next real capture saving its raw NEFs there,
///   and a hosted app instance restored (and re-ran) the user's persisted
///   stitch queue.
///
/// The CLI helper is told where its library database lives through
/// `SCANNY_BOY_LIBRARY_DB` (`isolateIfNeeded`), and derives its previews,
/// flat-field gain maps and roll locks from that path's folder, so one
/// variable keeps all of its state in the profile too.
enum AppEnvironment {
    enum Profile {
        case production
        case development
        case test
    }

    static let isRunningTests: Bool = {
        let environment = ProcessInfo.processInfo.environment
        return environment["XCTestConfigurationFilePath"] != nil
            || environment["XCTestBundlePath"] != nil
            || environment["XCTestSessionIdentifier"] != nil
    }()

    static let profile: Profile = {
        if isRunningTests { return .test }
        #if DEBUG
        return .development
        #else
        return .production
        #endif
    }()

    /// Whether this process keeps its data away from the installed app's.
    static var isIsolated: Bool { profile != .production }

    /// The preferences every model defaults to. The app's own domain in
    /// production; a separate suite in development; a throwaway suite,
    /// wiped on first use, under test.
    /// `UserDefaults` is thread-safe; it just isn't declared `Sendable`.
    nonisolated(unsafe) static let defaults: UserDefaults = {
        let name: String
        switch profile {
        case .production: return .standard
        case .development: name = "com.lonniesmith.scanny-boy.dev"
        case .test: name = "com.lonniesmith.scanny-boy.tests"
        }
        guard let suite = UserDefaults(suiteName: name) else {
            preconditionFailure("Cannot open the isolated preferences suite \(name)")
        }
        if profile == .test { suite.removePersistentDomain(forName: name) }
        return suite
    }()

    /// `~/Library/Application Support/ScannyBoy` (`ScannyBoy-Dev` in
    /// development), or a fresh temp directory under test.
    static let supportDirectory: URL = {
        switch profile {
        case .production:
            return userDirectory(.applicationSupportDirectory, named: "ScannyBoy")
        case .development:
            return userDirectory(.applicationSupportDirectory, named: "ScannyBoy-Dev")
        case .test:
            return makeTemporaryDirectory("scanny-boy-test-support")
        }
    }()

    /// The system caches directory `PreviewCache.shared` lives in. Its own
    /// `ScannyBoy` folder sits inside whatever this returns, so development
    /// gets `~/Library/Caches/ScannyBoy-Dev/ScannyBoy`.
    static let cachesDirectory: URL = {
        switch profile {
        case .production:
            return .cachesDirectory
        case .development:
            return userDirectory(.cachesDirectory, named: "ScannyBoy-Dev")
        case .test:
            return makeTemporaryDirectory("scanny-boy-test-caches")
        }
    }()

    /// Where the roll library lives until the user picks another folder in
    /// Settings: `~/Pictures/Scanny Boy` (`Scanny Boy Dev` in development).
    static func defaultLibraryBase(fileManager: FileManager = .default) -> URL {
        defaultFolder(named: "Scanny Boy", developmentName: "Scanny Boy Dev", fileManager: fileManager)
    }

    /// Where tethered captures are saved until the user picks another
    /// folder: `~/Pictures/Scanny Boy Captures` (`Scanny Boy Dev Captures`
    /// in development).
    static func defaultCaptureBase(fileManager: FileManager = .default) -> URL {
        defaultFolder(
            named: "Scanny Boy Captures", developmentName: "Scanny Boy Dev Captures",
            fileManager: fileManager)
    }

    /// The test profile's stand-in for `~/Pictures`, created once per run so
    /// every default folder a hosted instance resolves stays inside it.
    private static let testPictures: URL = makeTemporaryDirectory("scanny-boy-test-pictures")

    private static func defaultFolder(
        named name: String, developmentName: String, fileManager: FileManager
    ) -> URL {
        let pictures: URL
        switch profile {
        case .test:
            return testPictures.appending(path: name, directoryHint: .isDirectory)
        case .production, .development:
            pictures =
                fileManager.urls(for: .picturesDirectory, in: .userDomainMask).first
                ?? fileManager.homeDirectoryForCurrentUser.appending(
                    path: "Pictures", directoryHint: .isDirectory)
        }
        return pictures.appending(
            path: profile == .development ? developmentName : name, directoryHint: .isDirectory)
    }

    private static func userDirectory(
        _ directory: FileManager.SearchPathDirectory, named name: String
    ) -> URL {
        FileManager.default.urls(for: directory, in: .userDomainMask)[0]
            .appending(path: name, directoryHint: .isDirectory)
    }

    private static func makeTemporaryDirectory(_ prefix: String) -> URL {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "\(prefix)-\(UUID().uuidString)", directoryHint: .isDirectory)
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    /// Points the CLI helper's library database at this profile's support
    /// directory, so a development or hosted-test app instance can't read or
    /// write the installed app's. The helper inherits this process's
    /// environment. Under test the value is forced; in development an
    /// explicit `SCANNY_BOY_LIBRARY_DB` (as `ScannyBoyUITests` sets) wins.
    /// Tests that need a specific database pass their own override.
    static func isolateIfNeeded() {
        switch profile {
        case .production:
            return
        case .development:
            setenv("SCANNY_BOY_LIBRARY_DB", supportDirectory.appending(path: "library.db").path, 0)
        case .test:
            setenv("SCANNY_BOY_LIBRARY_DB", supportDirectory.appending(path: "library.db").path, 1)
        }
    }
}
