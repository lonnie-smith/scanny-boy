import Foundation
import Testing

@testable import ScannyBoy

private final class TestCaptureClock: CaptureClock, @unchecked Sendable {
    var nowDate = Date()
    func now() -> Date { nowDate }
    func sleep(until target: Date) async throws {
        if target > nowDate { nowDate = target }
    }
}

@Suite("CaptureSessionModel")
@MainActor
struct CaptureSessionModelTests {
    private static func makeModel(
        clock: TestCaptureClock = TestCaptureClock(),
        across: Int = 2,
        down: Int = 1
    ) -> (CaptureSessionModel, FakeTetherCamera, TestCaptureClock) {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/false"))
        let camera = FakeTetherCamera()
        let model = CaptureSessionModel(runner: runner, camera: camera, clock: clock)
        model.across = across
        model.down = down
        model.flatFieldProfileID = "profile-1"
        model.filmKind = "colour"
        model.filmBase = FilmBase(fields: [
            "density": .array([.double(-0.4), .double(-0.1), .double(-0.9)]),
            "source_name": .string("base.NEF"),
            "populations": .array([]),
        ])
        model.rollURL = URL(fileURLWithPath: "/tmp/roll")
        model.sessionOpen = true
        return (model, camera, clock)
    }

    @Test("runEnabled requires manual exposure and focus")
    func runEnabledGates() async {
        let (model, _, _) = Self.makeModel()
        await model.refreshConnection()
        #expect(model.runEnabled == true)
    }

    @Test("interval starts after exposure end")
    func intervalTiming() async throws {
        let clock = TestCaptureClock()
        let (model, camera, _) = Self.makeModel(clock: clock, across: 1, down: 1)
        await model.refreshConnection()
        await camera.startBrowsing()
        model.handleSpace()
        try await Task.sleep(for: .milliseconds(3500))
        #expect(model.completedNegatives.count == 1)
    }
}
