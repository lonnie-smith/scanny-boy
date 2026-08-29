import Foundation

/// Finds the `scanny-boy` executable the app should run.
///
/// `docs/IMPLEMENTATION_PLAN.md` section 5.2: the shipped location is the
/// helper bundle inside `Contents/Helpers`, Debug builds additionally honour
/// an *absolute* `SCANNY_BOY_CLI` override, and a Release build never falls
/// back to the repository. Nothing here is resolved relative to the process's
/// current directory, which is why a relative override is rejected outright
/// instead of being interpreted.
public struct CLILocator: Sendable {
    public enum BuildConfiguration: Sendable, Hashable {
        case debug
        case release
    }

    public static let overrideEnvironmentKey = "SCANNY_BOY_CLI"
    public static let helperBundleName = "ScannyBoyCLI.app"
    public static let helperExecutableName = "scanny-boy"

    private let bundleURL: URL
    private let environment: [String: String]
    private let configuration: BuildConfiguration
    private let isExecutableFile: @Sendable (URL) -> Bool

    public init(
        bundleURL: URL,
        environment: [String: String],
        configuration: BuildConfiguration,
        isExecutableFile: @escaping @Sendable (URL) -> Bool = {
            FileManager.default.isExecutableFile(atPath: $0.path)
        }
    ) {
        self.bundleURL = bundleURL
        self.environment = environment
        self.configuration = configuration
        self.isExecutableFile = isExecutableFile
    }

    /// A locator for the running app: its own bundle, its own environment,
    /// and the configuration it was built in.
    public static func mainBundle() -> CLILocator {
        #if DEBUG
        let configuration = BuildConfiguration.debug
        #else
        let configuration = BuildConfiguration.release
        #endif
        return CLILocator(
            bundleURL: Bundle.main.bundleURL,
            environment: ProcessInfo.processInfo.environment,
            configuration: configuration
        )
    }

    /// Where the helper lives inside an app bundle. `Contents/Helpers` is a
    /// permitted nested-code location; `Contents/Resources` is not.
    public var bundledExecutableURL: URL {
        bundleURL
            .appending(path: "Contents/Helpers", directoryHint: .isDirectory)
            .appending(path: Self.helperBundleName, directoryHint: .isDirectory)
            .appending(path: "Contents/MacOS", directoryHint: .isDirectory)
            .appending(path: Self.helperExecutableName, directoryHint: .notDirectory)
    }

    public func locate() throws -> URL {
        if configuration == .debug, let override = environment[Self.overrideEnvironmentKey],
           !override.isEmpty
        {
            guard override.hasPrefix("/") else {
                throw CLILocatorError.overrideNotAbsolute(override)
            }
            let url = URL(filePath: override, directoryHint: .notDirectory)
            guard isExecutableFile(url) else {
                throw CLILocatorError.overrideNotExecutable(url)
            }
            return url
        }

        let bundled = bundledExecutableURL
        guard isExecutableFile(bundled) else {
            throw CLILocatorError.helperMissing(bundled)
        }
        return bundled
    }
}

public enum CLILocatorError: Error, Sendable, Hashable {
    case overrideNotAbsolute(String)
    case overrideNotExecutable(URL)
    case helperMissing(URL)
}

extension CLILocatorError: CustomStringConvertible {
    public var description: String {
        switch self {
        case .overrideNotAbsolute(let path):
            """
            \(CLILocator.overrideEnvironmentKey) must be an absolute path; \
            got "\(path)"
            """
        case .overrideNotExecutable(let url):
            """
            \(CLILocator.overrideEnvironmentKey) points at \(url.path), which \
            is not an executable file
            """
        case .helperMissing(let url):
            """
            the bundled command-line helper is missing at \(url.path); build \
            it with ./scripts/build-cli.sh and rebuild the app
            """
        }
    }
}
