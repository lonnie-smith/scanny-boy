import Foundation
import Testing

@testable import ScannyBoy

/// `docs/IMPLEMENTATION_PLAN.md` section 5.2: the helper is signed on copy,
/// before the outer app, and both must verify strictly. These run against the
/// app bundle hosting the tests, which is the app Xcode has just built and
/// signed.
@Suite("Code signing")
struct CodeSigningTests {
    @Test(
        "the built app passes codesign --verify --strict",
        .enabled(if: HostBundle.isAvailable, HostBundle.unavailableComment)
    )
    func builtAppVerifiesStrictly() throws {
        let app = try #require(HostBundle.appURL)
        let result = try runTool("/usr/bin/codesign", ["--verify", "--strict", "--verbose=2", app.path])
        #expect(result.status == 0, "codesign rejected \(app.path):\n\(result.output)")
    }

    @Test(
        "the nested helper passes codesign --verify --strict",
        .enabled(if: HostBundle.isAvailable, HostBundle.unavailableComment)
    )
    func nestedHelperVerifiesStrictly() throws {
        let app = try #require(HostBundle.appURL)
        let helper = app.appending(path: "Contents/Helpers/ScannyBoyCLI.app")
        #expect(FileManager.default.fileExists(atPath: helper.path))
        let result = try runTool("/usr/bin/codesign", ["--verify", "--strict", "--verbose=2", helper.path])
        #expect(result.status == 0, "codesign rejected \(helper.path):\n\(result.output)")
    }

    /// `Contents/Helpers` is a permitted nested-code location;
    /// `Contents/Resources` is not.
    @Test(
        "the helper is not staged anywhere in Contents/Resources",
        .enabled(if: HostBundle.isAvailable, HostBundle.unavailableComment)
    )
    func helperIsNotInResources() throws {
        let app = try #require(HostBundle.appURL)
        let resources = app.appending(path: "Contents/Resources/ScannyBoyCLI.app")
        #expect(!FileManager.default.fileExists(atPath: resources.path))
    }
}
