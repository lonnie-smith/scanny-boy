import Foundation
import Testing

@testable import ScannyBoy

/// A test run must never touch the user's real preferences, Application
/// Support, caches or Pictures folders: the tests are hosted in the app, so
/// they would share all of them.
@Suite("AppEnvironment")
struct AppEnvironmentTests {
    @Test("tests are recognised as tests")
    func recognisesTests() {
        #expect(AppEnvironment.isRunningTests)
        #expect(AppEnvironment.profile == .test)
        #expect(AppEnvironment.isIsolated)
    }

    @Test("preferences are a throwaway suite, not the app's own domain")
    func defaultsAreIsolated() {
        let key = "com.lonniesmith.scanny-boy.isolationProbe"
        AppEnvironment.defaults.set("probe", forKey: key)
        #expect(UserDefaults.standard.string(forKey: key) == nil)
        AppEnvironment.defaults.removeObject(forKey: key)
    }

    @Test("state lives in a temp directory, not ~/Library/Application Support")
    func supportDirectoryIsTemporary() {
        let path = AppEnvironment.supportDirectory.standardizedFileURL.path
        let real = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appending(path: "ScannyBoy").standardizedFileURL.path
        #expect(path != real)
        #expect(!path.hasPrefix(real))
        #expect(path.hasPrefix(FileManager.default.temporaryDirectory.standardizedFileURL.path))
    }

    @Test("the models default to the isolated preferences")
    @MainActor
    func modelsUseIsolatedDefaults() {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/false"))
        let model = CaptureSessionModel(runner: runner, camera: FakeTetherCamera())
        let folder = FileManager.default.temporaryDirectory
            .appending(path: "isolation-probe-\(UUID().uuidString)", directoryHint: .isDirectory)
        model.captureBaseFolder = folder
        #expect(
            UserDefaults.standard.url(forKey: CaptureSessionModel.captureBaseKey) != folder
        )
    }

    @Test("caches and default folders live in temp directories, not ~/Library or ~/Pictures")
    func defaultLocationsAreTemporary() {
        let temporary = FileManager.default.temporaryDirectory.standardizedFileURL.path
        let locations = [
            AppEnvironment.cachesDirectory,
            AppEnvironment.defaultLibraryBase(),
            AppEnvironment.defaultCaptureBase(),
        ]
        for location in locations {
            #expect(location.standardizedFileURL.path.hasPrefix(temporary))
        }
        #expect(PreviewCache.shared.cachesDirectory == AppEnvironment.cachesDirectory)
    }

    @Test("the CLI helper is pointed at the isolated library database")
    func helperDatabaseIsIsolated() {
        AppEnvironment.isolateIfNeeded()
        let path = ProcessInfo.processInfo.environment["SCANNY_BOY_LIBRARY_DB"]
        #expect(path == AppEnvironment.supportDirectory.appending(path: "library.db").path)
    }
}
