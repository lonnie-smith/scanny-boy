import XCTest

/// A launch smoke test for the built app.
///
/// Chunk 8 built this target but left it out of the scheme's test targets: its
/// runner would not start on the development machine, and a required check
/// that fails at random is worse than no check. Chunk 10 owns it, and the
/// runner now starts, so it is part of the test action again.
///
/// It deliberately stays a smoke test. Everything the run UI actually decides
/// — progress from counts, what counts as published, cancellation, the
/// manifest — is decided in `RunModel` and `ConfigurationModel` and is tested
/// directly there, against real events and the real helper. Driving
/// `NSOpenPanel` through XCUITest would buy assertions about AppKit rather
/// than about Scanny Boy.
final class ScannyBoyUITests: XCTestCase {
    /// Section 4: "Never test the library against the real `~/Pictures`."
    /// The app honours `SCANNY_BOY_LIBRARY_BASE` (a Debug-only override,
    /// mirroring `CLILocator`'s `SCANNY_BOY_CLI`) so this suite can point it
    /// at a fresh temporary directory instead of the real library.
    @MainActor
    private func launchedApp() -> XCUIApplication {
        let app = XCUIApplication()
        let tempLibrary = FileManager.default.temporaryDirectory
            .appending(path: "scanny-boy-ui-tests-\(UUID().uuidString)", directoryHint: .isDirectory)
        app.launchEnvironment["SCANNY_BOY_LIBRARY_BASE"] = tempLibrary.path
        app.launch()
        return app
    }

    /// `XCUIApplication` is main-actor isolated under Swift 6, so the test
    /// method has to be too.
    @MainActor
    func testAppLaunchesIntoTheLibrarySidebar() {
        let app = launchedApp()

        // Chunk P3-10: the app now launches into the library sidebar, not
        // directly into a configuration form — no roll is selected yet, so
        // there is nothing to configure. The "New Roll" toolbar button
        // existing (and being enabled) is what proves the CLI helper
        // resolved: otherwise the app shows `HelperUnavailableView` instead
        // of any of this.
        let newRoll = app.buttons["newRollButton"]
        XCTAssertTrue(newRoll.waitForExistence(timeout: 30))
        XCTAssertTrue(newRoll.isEnabled)
        XCTAssertFalse(app.staticTexts["Scanny Boy's CLI helper is unavailable"].exists)
    }

}
