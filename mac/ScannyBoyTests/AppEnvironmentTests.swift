import Foundation
import Testing

@testable import ScannyBoy

/// A test run must never touch the user's real preferences or Application
/// Support: the tests are hosted in the app, so they would share both.
@Suite("AppEnvironment")
struct AppEnvironmentTests {
    @Test("tests are recognised as tests")
    func recognisesTests() {
        #expect(AppEnvironment.isRunningTests)
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
}
