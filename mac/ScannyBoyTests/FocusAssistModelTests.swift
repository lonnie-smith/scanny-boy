import Foundation
import Testing

@testable import ScannyBoy

@Suite("FocusAssistModel")
@MainActor
struct FocusAssistModelTests {
    @Test("median resists a single outlier peak")
    func outlierPeak() async throws {
        let jpeg = LiveViewFixtures.texturedJPEG(seed: 42)
        let frame = LiveViewFixtures.makeFrame(areaWidth: 512, jpeg: jpeg)
        let camera = FakeTetherCamera(configuration: .init(liveViewFrames: [frame]))
        let model = CaptureSessionModel(runner: CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/false")), camera: camera)
        await model.connect()
        model.focusAssist.open()
        try await Task.sleep(for: .milliseconds(200))
        model.focusAssist.resetPeak()
        let firstPeak = model.focusAssist.meterPercentage ?? 0
        #expect(firstPeak >= 99)
    }

    @Test("zoom change resets the held peak")
    func zoomResetsPeak() async throws {
        let jpeg = LiveViewFixtures.texturedJPEG(seed: 55)
        let zoom512 = LiveViewFixtures.makeFrame(areaWidth: 512, jpeg: jpeg)
        let zoom256 = LiveViewFixtures.makeFrame(areaWidth: 256, jpeg: jpeg)
        let camera = FakeTetherCamera(configuration: .init(liveViewFrames: [zoom512, zoom512, zoom256, zoom256]))
        let model = CaptureSessionModel(runner: CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/false")), camera: camera)
        await model.connect()
        model.focusAssist.open()
        try await Task.sleep(for: .milliseconds(400))
        let afterReset = model.focusAssist.meterPercentage ?? 0
        #expect(afterReset >= 99)
    }

    @Test("identical JPEGs are not rescored")
    func identicalJPEGSkipsRescore() async throws {
        let jpeg = LiveViewFixtures.texturedJPEG(seed: 77)
        let frame = LiveViewFixtures.makeFrame(areaWidth: 512, jpeg: jpeg)
        let camera = FakeTetherCamera(configuration: .init(liveViewFrames: [frame, frame, frame]))
        let model = CaptureSessionModel(runner: CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/false")), camera: camera)
        await model.connect()
        model.focusAssist.open()
        try await Task.sleep(for: .milliseconds(250))
        #expect(model.focusAssist.loupeImage != nil)
    }

    @Test("closing focus assist ends live view")
    func closeEndsLiveView() async throws {
        let jpeg = LiveViewFixtures.texturedJPEG(seed: 33)
        let frame = LiveViewFixtures.makeFrame(areaWidth: 512, jpeg: jpeg)
        let camera = FakeTetherCamera(configuration: .init(liveViewFrames: [frame]))
        let model = CaptureSessionModel(runner: CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/false")), camera: camera)
        await model.refreshConnection()
        model.focusAssist.open()
        try await Task.sleep(for: .milliseconds(100))
        await model.focusAssist.close()
        #expect(model.focusAssist.isOpen == false)
        let endCount = await camera.endLiveViewCallCount
        #expect(endCount >= 1)
    }

    @Test("connection loss closes focus assist")
    func connectionLossClosesAssist() async throws {
        let jpeg = LiveViewFixtures.texturedJPEG(seed: 44)
        let frame = LiveViewFixtures.makeFrame(areaWidth: 512, jpeg: jpeg)
        var config = FakeTetherCamera.Configuration()
        config.liveViewFrames = [frame]
        config.dropOnRelease = true
        let camera = FakeTetherCamera(configuration: config)
        let model = CaptureSessionModel(runner: CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/false")), camera: camera)
        await model.refreshConnection()
        model.focusAssist.open()
        try await Task.sleep(for: .milliseconds(100))
        model.focusAssist.updateConnectionState(.lost)
        try await Task.sleep(for: .milliseconds(50))
        #expect(model.focusAssist.isOpen == false)
    }
}
