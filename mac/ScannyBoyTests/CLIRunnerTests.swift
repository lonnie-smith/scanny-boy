import XCTest
@testable import ScannyBoy

final class CLIRunnerTests: XCTestCase {
    func testScanResultDecodesFromJSON() throws {
        let json = """
        {"path": "/tmp/example", "ok": true}
        """.data(using: .utf8)!

        let result = try JSONDecoder().decode(ScanResult.self, from: json)

        XCTAssertEqual(result.path, "/tmp/example")
        XCTAssertTrue(result.ok)
    }
}
