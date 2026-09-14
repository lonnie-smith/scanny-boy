import Foundation
import Testing

@testable import ScannyBoy

@Suite("CaptureNaming")
struct CaptureNamingTests {
    @Test("formats timestamped names")
    func filenameFormat() {
        var components = DateComponents()
        components.year = 2026
        components.month = 9
        components.day = 11
        components.hour = 12
        components.minute = 33
        components.second = 25
        let date = Calendar.current.date(from: components)!
        #expect(CaptureNaming.filename(firstRelease: date, shotNumber: 1) == "20260911-123325_01.NEF")
        #expect(CaptureNaming.filename(firstRelease: date, shotNumber: 6) == "20260911-123325_06.NEF")
    }

    @Test("exclusiveURL avoids collisions")
    func exclusiveCollisionSuffix() async throws {
        try await TestSupport.withTemporaryDirectory { directory in
            var components = DateComponents()
            components.year = 2026
            components.month = 9
            components.day = 11
            components.hour = 12
            components.minute = 33
            components.second = 25
            let date = Calendar.current.date(from: components)!
            let first = try CaptureNaming.exclusiveURL(in: directory, firstRelease: date, shotNumber: 1)
            try Data().write(to: first)
            let second = try CaptureNaming.exclusiveURL(in: directory, firstRelease: date, shotNumber: 1)
            #expect(second.lastPathComponent == "20260911-123325-2_01.NEF")
        }
    }
}
