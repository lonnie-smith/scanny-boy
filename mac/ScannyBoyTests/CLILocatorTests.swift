import Foundation
import Testing

@testable import ScannyBoy

@Suite("Helper resolution")
struct CLILocatorTests {
    private static let appURL = URL(filePath: "/Applications/ScannyBoy.app")

    private static var bundledPath: String {
        "/Applications/ScannyBoy.app/Contents/Helpers/ScannyBoyCLI.app/Contents/MacOS/scanny-boy"
    }

    private static func locator(
        environment: [String: String] = [:],
        configuration: CLILocator.BuildConfiguration,
        executables: Set<String> = [CLILocatorTests.bundledPath]
    ) -> CLILocator {
        CLILocator(
            bundleURL: appURL,
            environment: environment,
            configuration: configuration,
            isExecutableFile: { executables.contains($0.path) }
        )
    }

    @Test("the bundled helper sits in Contents/Helpers, never Contents/Resources")
    func bundledPathIsInHelpers() throws {
        let located = try Self.locator(configuration: .release).locate()
        #expect(located.path == Self.bundledPath)
        #expect(!located.path.contains("/Contents/Resources/"))
    }

    @Test("a Debug build honours an absolute SCANNY_BOY_CLI override")
    func debugHonoursAnAbsoluteOverride() throws {
        let override = "/Users/someone/dev/scanny-boy/cli/dist/ScannyBoyCLI.app/Contents/MacOS/scanny-boy"
        let located = try Self.locator(
            environment: [CLILocator.overrideEnvironmentKey: override],
            configuration: .debug,
            executables: [Self.bundledPath, override]
        ).locate()
        #expect(located.path == override)
    }

    @Test("a Release build ignores the override and uses the bundled helper")
    func releaseIgnoresTheOverride() throws {
        let override = "/Users/someone/dev/scanny-boy/cli/dist/ScannyBoyCLI.app/Contents/MacOS/scanny-boy"
        let located = try Self.locator(
            environment: [CLILocator.overrideEnvironmentKey: override],
            configuration: .release,
            executables: [Self.bundledPath, override]
        ).locate()
        #expect(located.path == Self.bundledPath)
    }

    /// A relative override would be resolved against the process's current
    /// directory, which section 5.2 forbids outright.
    @Test(
        "a relative override is rejected rather than resolved",
        arguments: ["cli/dist/ScannyBoyCLI.app/Contents/MacOS/scanny-boy", "./scanny-boy", "../scanny-boy"]
    )
    func relativeOverrideIsRejected(path: String) {
        #expect(throws: CLILocatorError.overrideNotAbsolute(path)) {
            try Self.locator(
                environment: [CLILocator.overrideEnvironmentKey: path],
                configuration: .debug
            ).locate()
        }
    }

    @Test("an override pointing at nothing is reported, not silently ignored")
    func missingOverrideIsReported() {
        #expect(throws: CLILocatorError.self) {
            try Self.locator(
                environment: [CLILocator.overrideEnvironmentKey: "/nope/scanny-boy"],
                configuration: .debug
            ).locate()
        }
    }

    @Test("an empty override falls through to the bundled helper")
    func emptyOverrideFallsThroughToTheBundle() throws {
        let located = try Self.locator(
            environment: [CLILocator.overrideEnvironmentKey: ""],
            configuration: .debug
        ).locate()
        #expect(located.path == Self.bundledPath)
    }

    @Test("a missing bundled helper names the build script", arguments: [
        CLILocator.BuildConfiguration.debug, .release,
    ])
    func missingBundledHelperIsReported(configuration: CLILocator.BuildConfiguration) {
        #expect(throws: CLILocatorError.self) {
            try Self.locator(configuration: configuration, executables: []).locate()
        }
        let error = CLILocatorError.helperMissing(URL(filePath: Self.bundledPath))
        #expect(error.description.contains("./scripts/build-cli.sh"))
    }

    @Test(
        "the running app resolves its own nested helper",
        .enabled(if: HostBundle.isAvailable, HostBundle.unavailableComment)
    )
    func mainBundleResolvesTheNestedHelper() throws {
        let located = try CLILocator.mainBundle().locate()
        #expect(located.path.hasSuffix("/Contents/Helpers/ScannyBoyCLI.app/Contents/MacOS/scanny-boy"))
        #expect(FileManager.default.isExecutableFile(atPath: located.path))
    }
}
