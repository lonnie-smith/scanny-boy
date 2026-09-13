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
        model.rigProfileID = "profile-1"
        model.flatField = FlatFieldReference(fields: [
            "source_name": .string("bare-light.NEF"),
            "reference_width": .int(100),
            "reference_height": .int(100),
        ])
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

    @Test("init stays absent until connect")
    func initStaysAbsentUntilConnect() async {
        let (model, _, _) = Self.makeModel()
        #expect(model.connectionState == .absent)
        #expect(model.exposure == nil)
    }

    @Test("connect publishes ready state and exposure")
    func connectPublishesReady() async {
        let (model, _, _) = Self.makeModel()
        await model.connect()
        #expect(model.connectionState == .ready)
        #expect(model.exposure != nil)
    }

    @Test("delayed connect publishes ready without a second snapshot")
    func delayedConnectPublishesReady() async throws {
        var config = FakeTetherCamera.Configuration()
        config.connectDelay = .milliseconds(50)
        let camera = FakeTetherCamera(configuration: config)
        let model = CaptureSessionModel(
            runner: CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/false")),
            camera: camera
        )
        await model.connect()
        #expect(model.connectionState == .searching)
        try await Task.sleep(for: .milliseconds(100))
        #expect(model.connectionState == .ready)
        #expect(model.exposure != nil)
    }

    @Test("disconnect returns to absent and clears exposure")
    func disconnectClearsConnection() async {
        let (model, _, _) = Self.makeModel()
        await model.connect()
        await model.disconnect()
        #expect(model.connectionState == .absent)
        #expect(model.exposure == nil)
    }

    @Test("runEnabled requires manual exposure and focus")
    func runEnabledGates() async {
        let (model, _, _) = Self.makeModel()
        await model.refreshConnection()
        #expect(model.runEnabled == true)
    }

    @Test("runEnabled requires a flat-field reference")
    func runEnabledRequiresFlatField() async {
        let (model, _, _) = Self.makeModel()
        model.flatField = nil
        await model.refreshConnection()
        #expect(model.runEnabled == false)
    }

    private static func makeTemporaryDirectory() throws -> URL {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "scanny-boy-tests", directoryHint: .isDirectory)
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    @Test("shootFlatFieldReference attaches the roll reference")
    func shootFlatFieldReference() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let roll = directory.appending(path: "roll", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: roll, withIntermediateDirectories: true)
        let marker = directory.appending(path: ".ff-attached").path
        let finished = TestEvents.line(#"{"event":"finished","status":"success","exit_status":0}"#)
        let script = """
            if [ "$1" = "roll" ] && [ "$2" = "set-flatfield-reference" ]; then
              touch '\(marker)'
              echo '\(TestEvents.line(#"{"event":"started","command":"roll set-flatfield-reference"}"#))'
              echo '\(TestEvents.line(#"{"event":"flat_field_reference_set","roll_id":"roll-1","source_name":"bare-light.NEF","reference_width":100,"reference_height":100,"rig_profile_id":null,"locked":false}"#))'
              echo '\(finished)'
              exit 0
            fi
            if [ "$1" = "roll" ] && [ "$2" = "info" ]; then
              if [ -f '\(marker)' ]; then
                echo '\(TestEvents.line(#"{"event":"started","command":"roll info"}"#))'
                echo '\(TestEvents.line(#"{"event":"roll_info","manifest":{"roll_id":"roll-1","roll_name":"Roll","created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z","runs":[],"negatives":[],"metadata":{},"film_kind":"colour","flat_field":{"source_name":"bare-light.NEF","reference_width":100,"reference_height":100}}}"#))'
                echo '\(finished)'
              fi
              exit 0
            fi
            exit 1
            """
        let executable = try TestSupport.writeTestExecutable(script, in: directory)
        let runner = CLIRunner(executable: executable)
        let camera = FakeTetherCamera()
        let model = CaptureSessionModel(runner: runner, camera: camera)
        model.rollURL = roll
        model.captureBaseFolder = directory
        model.across = 1
        model.down = 1
        model.filmKind = "colour"
        model.filmBase = FilmBase(fields: [
            "density": .array([.double(-0.4), .double(-0.1), .double(-0.9)]),
            "source_name": .string("base.NEF"),
            "populations": .array([]),
        ])
        model.sessionOpen = true
        await model.connect()
        await model.shootFlatFieldReference()
        #expect(model.flatField?.sourceName == "bare-light.NEF")
        #expect(model.referenceAperture != nil)
    }

    @Test("interval starts after exposure end")
    func intervalTiming() async throws {
        let clock = TestCaptureClock()
        let (model, _, _) = Self.makeModel(clock: clock, across: 1, down: 1)
        await model.connect()
        model.handleSpace()
        try await Task.sleep(for: .milliseconds(3500))
        #expect(model.completedNegatives.count == 1)
    }
}
