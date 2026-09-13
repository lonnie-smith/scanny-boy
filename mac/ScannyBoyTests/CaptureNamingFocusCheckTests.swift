import Darwin
import Foundation
import Testing

@testable import ScannyBoy

@Suite("CaptureNaming focus check")
struct CaptureNamingFocusCheckTests {
    @Test("focus check filenames are exclusive")
    func focusCheckURL() async throws {
        try await TestSupport.withTemporaryDirectory { directory in
            let stamp = Date(timeIntervalSinceReferenceDate: 0)
            let first = try CaptureNaming.focusCheckURL(in: directory, at: stamp)
            try Data().write(to: first)
            let second = try CaptureNaming.focusCheckURL(in: directory, at: stamp)
            #expect(first.lastPathComponent.hasPrefix("focus-check-"))
            #expect(first != second)
        }
    }
}
