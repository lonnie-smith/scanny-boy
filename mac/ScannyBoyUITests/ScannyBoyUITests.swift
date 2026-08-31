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

    /// Creates one roll through the sidebar's own "New Roll" flow and
    /// selects it — the only way Chunk P3-10's shell reaches the Add Scans
    /// workspace at all, now that nothing is preselected at launch.
    @MainActor
    private func createAndSelectRoll(named name: String, in app: XCUIApplication) {
        let newRoll = app.buttons["newRollButton"]
        XCTAssertTrue(newRoll.waitForExistence(timeout: 30))
        newRoll.click()

        let nameField = app.textFields["newRollNameField"]
        XCTAssertTrue(nameField.waitForExistence(timeout: 10))
        nameField.click()
        nameField.typeText(name)

        let create = app.buttons["createRollButton"]
        XCTAssertTrue(create.waitForExistence(timeout: 10))
        create.click()

        // The sheet dismisses once `roll_created` arrives and the sidebar
        // rescans; the new row's own name is what proves both happened.
        let row = app.staticTexts[name]
        XCTAssertTrue(row.waitForExistence(timeout: 30))
        row.click()
    }

    /// Chunk P2-9: the Run button now drives `run` (convert and stitch), and
    /// the configuration form offers a way to keep the intermediates a
    /// successful run would otherwise remove. Whether toggling it actually
    /// changes what gets passed to the CLI is `ConfigurationModelTests`'
    /// job, not this smoke test's. Chunk P3-10: reaching either now needs a
    /// selected roll first, since they live under the Add Scans tab of the
    /// workspace rather than at the app's root.
    @MainActor
    func testKeepIntermediatesToggleExists() {
        let app = launchedApp()
        createAndSelectRoll(named: "UI Test Roll \(UUID().uuidString.prefix(8))", in: app)

        // The Run button exists, and nothing has been configured, so it is
        // offered but not enabled.
        let run = app.buttons["Run"]
        XCTAssertTrue(run.waitForExistence(timeout: 30))
        XCTAssertFalse(run.isEnabled)

        // macOS renders a SwiftUI `Toggle` in a grouped `Form` as an unlabelled
        // `Switch` accessibility element (its text sits in a sibling static
        // text instead), so this looks it up by an explicit identifier rather
        // than the label.
        let toggle = app.switches["keepIntermediatesToggle"]
        XCTAssertTrue(toggle.waitForExistence(timeout: 30))
    }
}
