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
    /// `XCUIApplication` is main-actor isolated under Swift 6, so the test
    /// method has to be too.
    @MainActor
    func testAppLaunchesIntoTheConfigurationUI() {
        let app = XCUIApplication()
        app.launch()

        // The Run button exists, and nothing has been configured, so it is
        // offered but not enabled. Reaching it at all also proves the CLI
        // helper resolved: otherwise the app shows `HelperUnavailableView`
        // instead of any of this.
        let run = app.buttons["Run"]
        XCTAssertTrue(run.waitForExistence(timeout: 30))
        XCTAssertFalse(run.isEnabled)
        XCTAssertFalse(app.staticTexts["Scanny Boy's CLI helper is unavailable"].exists)
    }

    /// Chunk P2-9: the Run button now drives `run` (convert and stitch), and
    /// the configuration form offers a way to keep the intermediates a
    /// successful run would otherwise remove. Whether toggling it actually
    /// changes what gets passed to the CLI is `ConfigurationModelTests`'
    /// job, not this smoke test's.
    @MainActor
    func testKeepIntermediatesToggleExists() {
        let app = XCUIApplication()
        app.launch()

        // macOS renders a SwiftUI `Toggle` in a grouped `Form` as an unlabelled
        // `Switch` accessibility element (its text sits in a sibling static
        // text instead), so this looks it up by an explicit identifier rather
        // than the label.
        let toggle = app.switches["keepIntermediatesToggle"]
        XCTAssertTrue(toggle.waitForExistence(timeout: 30))
    }
}
