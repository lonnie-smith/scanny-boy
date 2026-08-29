import XCTest

final class ScannyBoyUITests: XCTestCase {
    /// `XCUIApplication` is main-actor isolated under Swift 6, so the test
    /// method has to be too.
    @MainActor
    func testAppLaunches() {
        XCUIApplication().launch()
    }
}
