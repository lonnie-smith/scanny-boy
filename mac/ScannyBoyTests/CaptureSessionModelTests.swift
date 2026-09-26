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
    private static func makeModel<C: CaptureClock>(
        clock: C = TestCaptureClock(),
        across: Int = 2,
        down: Int = 1,
        camera: FakeTetherCamera = FakeTetherCamera()
    ) -> (CaptureSessionModel, FakeTetherCamera, C) {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/false"))
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
        return (model, camera, clock)
    }

    @Test("init stays absent until connect")
    func initStaysAbsentUntilConnect() async {
        let (model, _, _) = Self.makeModel()
        #expect(model.connectionState == .absent)
        #expect(model.exposure == nil)
        #expect(model.isSessionOpen == false)
    }

    @Test("connect opens the capture session when ready")
    func connectOpensSession() async {
        let (model, _, _) = Self.makeModel()
        #expect(model.isSessionOpen == false)
        await model.connect()
        #expect(model.connectionState == .ready)
        #expect(model.isSessionOpen == true)
    }

    @Test("disconnect closes the capture session")
    func disconnectClosesSession() async {
        let (model, _, _) = Self.makeModel()
        await model.connect()
        #expect(model.isSessionOpen == true)
        await model.disconnect()
        #expect(model.connectionState == .absent)
        #expect(model.isSessionOpen == false)
    }

    @Test("lost connection closes the capture session")
    func lostConnectionClosesSession() async throws {
        let camera = FakeTetherCamera(configuration: .init(dropOnRelease: true))
        let model = CaptureSessionModel(
            runner: CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/false")),
            camera: camera
        )
        await model.connect()
        #expect(model.isSessionOpen == true)
        do {
            try await camera.release()
        } catch is TetherCaptureError {
            // Expected when the fake camera drops the session after shutter.
        }
        #expect(model.connectionState == .lost)
        #expect(model.isSessionOpen == false)
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
        #expect(model.isSessionOpen == false)
        try await Task.sleep(for: .milliseconds(100))
        #expect(model.connectionState == .ready)
        #expect(model.exposure != nil)
        #expect(model.isSessionOpen == true)
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

    @Test("startBlockedReason names the first missing prerequisite")
    func startBlockedReasonNamesMissingPrerequisite() async {
        let (model, _, _) = Self.makeModel()
        model.flatField = nil
        await model.refreshConnection()
        #expect(model.startBlockedReason == "Capture a bare-light reference before capturing.")

        model.flatField = FlatFieldReference(fields: [
            "source_name": .string("bare-light.NEF"),
            "reference_width": .int(100),
            "reference_height": .int(100),
        ])
        model.gridProfileID = nil
        model.across = nil
        #expect(model.startBlockedReason == "Choose a grid before capturing.")
    }

    @Test("canToggleSequence is true while running or paused")
    func canToggleSequenceWhileActive() async {
        let (model, _, _) = Self.makeModel()
        await model.refreshConnection()
        #expect(model.canToggleSequence == true)

        model.handleSpace()
        #expect(model.sequencePhase == .running)
        #expect(model.canToggleSequence == true)

        model.handleSpace()
        #expect(model.sequencePhase == .paused)
        #expect(model.canToggleSequence == true)
    }

    @Test("canToggleSequence is false when idle setup is incomplete")
    func canToggleSequenceBlockedWhenIdle() async {
        let (model, _, _) = Self.makeModel()
        model.flatField = nil
        await model.refreshConnection()
        #expect(model.canToggleSequence == false)
        #expect(model.startBlockedReason != nil)
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
        await model.connect()
        await model.shootFlatFieldReference()
        #expect(model.flatField?.sourceName == "bare-light.NEF")
        #expect(model.referenceAperture != nil)
    }

    @Test("shootFlatFieldReference can replace an existing reference")
    func shootFlatFieldReferenceReplace() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let roll = directory.appending(path: "roll", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: roll, withIntermediateDirectories: true)
        let callCount = directory.appending(path: ".ff-calls").path
        let finished = TestEvents.line(#"{"event":"finished","status":"success","exit_status":0}"#)
        let script = """
            if [ "$1" = "roll" ] && [ "$2" = "set-flatfield-reference" ]; then
              n=0
              if [ -f '\(callCount)' ]; then
                n=$(cat '\(callCount)')
              fi
              n=$((n + 1))
              echo "$n" > '\(callCount)'
              echo '\(TestEvents.line(#"{"event":"started","command":"roll set-flatfield-reference"}"#))'
              echo '\(TestEvents.line(#"{"event":"flat_field_reference_set","roll_id":"roll-1","source_name":"bare-light-replace.NEF","reference_width":200,"reference_height":150,"rig_profile_id":null,"locked":false}"#))'
              echo '\(finished)'
              exit 0
            fi
            if [ "$1" = "roll" ] && [ "$2" = "info" ]; then
              echo '\(TestEvents.line(#"{"event":"started","command":"roll info"}"#))'
              echo '\(TestEvents.line(#"{"event":"roll_info","manifest":{"roll_id":"roll-1","roll_name":"Roll","created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z","runs":[],"negatives":[],"metadata":{},"film_kind":"colour","flat_field":{"source_name":"bare-light-replace.NEF","reference_width":200,"reference_height":150}}}"#))'
              echo '\(finished)'
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
        model.flatField = FlatFieldReference(fields: [
            "source_name": .string("bare-light-old.NEF"),
            "reference_width": .int(100),
            "reference_height": .int(100),
        ])
        await model.connect()
        await model.shootFlatFieldReference()
        #expect(model.flatField?.sourceName == "bare-light-replace.NEF")
        #expect(model.flatField?.referenceWidth == 200)
    }

    @Test("shootFlatFieldReference closes focus assist before the release")
    func shootFlatFieldReferenceClosesFocusAssist() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let roll = directory.appending(path: "roll", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: roll, withIntermediateDirectories: true)
        let jpeg = LiveViewFixtures.texturedJPEG(seed: 19)
        let frame = LiveViewFixtures.makeFrame(areaWidth: 512, jpeg: jpeg)
        let camera = FakeTetherCamera(configuration: .init(liveViewFrames: [frame]))
        let model = CaptureSessionModel(
            runner: CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/false")),
            camera: camera
        )
        model.rollURL = roll
        model.captureBaseFolder = directory
        await model.connect()
        model.focusAssist.open()
        try await Task.sleep(for: .milliseconds(80))
        #expect(model.focusAssist.isOpen)
        await model.shootFlatFieldReference()
        #expect(model.focusAssist.isOpen == false)
        let endCount = await camera.endLiveViewCallCount
        #expect(endCount >= 1)
    }

    @Test("shootFlatFieldReference saves leftover buffer frames to _unclaimed")
    func shootFlatFieldReferenceSavesLeftovers() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let roll = directory.appending(path: "roll", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: roll, withIntermediateDirectories: true)
        let leftover = BufferLeftover(
            handle: TetherTiming.bufferScanFirst,
            objectInfo: PTP.ObjectInfo(
                handle: TetherTiming.bufferScanFirst,
                storageID: 0,
                objectFormat: 0x3801,
                size: 4,
                width: 100,
                height: 100,
                filename: "old.NEF",
                captureDate: "20260911T120000"
            )
        )
        let camera = FakeTetherCamera(configuration: .init(initialLeftovers: [leftover]))
        let model = CaptureSessionModel(
            runner: CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/false")),
            camera: camera
        )
        model.rollURL = roll
        model.captureBaseFolder = directory
        await model.connect()
        await model.shootFlatFieldReference()
        let leftovers = await camera.leftovers
        #expect(leftovers.isEmpty)
        #expect(model.flatFieldReferenceError?.message.contains("buffer") != true)
        #expect(model.leftoverFrames.count == 1)
        let saved = try #require(model.leftoverFrames.first)
        #expect(saved.url.deletingLastPathComponent().lastPathComponent == "_unclaimed")
        #expect(FileManager.default.fileExists(atPath: saved.url.path))
    }

    @Test("waitForExposureEnd failure returns the fake camera to ready")
    func waitForExposureEndReturnsToReady() async throws {
        let camera = FakeTetherCamera(configuration: .init(failDeviceReady: true))
        let model = CaptureSessionModel(
            runner: CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/false")),
            camera: camera
        )
        await model.connect()
        try await camera.release()
        var caughtExposureTimeout = false
        do {
            try await camera.waitForExposureEnd()
        } catch is TetherCaptureError {
            caughtExposureTimeout = true
        }
        #expect(caughtExposureTimeout)
        let state = await camera.connectionState
        #expect(state == .ready)
    }

    @Test("shootBaseFrame can retry after a failed download")
    func shootBaseFrameRetriesAfterFailedDownload() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let roll = directory.appending(path: "roll", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: roll, withIntermediateDirectories: true)
        let marker = directory.appending(path: ".base-attached").path
        let finished = TestEvents.line(#"{"event":"finished","status":"success","exit_status":0}"#)
        let script = """
            if [ "$1" = "roll" ] && [ "$2" = "set-base-frame" ]; then
              touch '\(marker)'
              echo '\(TestEvents.line(#"{"event":"started","command":"roll set-base-frame"}"#))'
              echo '\(TestEvents.line(#"{"event":"base_frame_set","roll_id":"roll-1"}"#))'
              echo '\(finished)'
              exit 0
            fi
            if [ "$1" = "roll" ] && [ "$2" = "info" ]; then
              if [ -f '\(marker)' ]; then
                echo '\(TestEvents.line(#"{"event":"started","command":"roll info"}"#))'
                echo '\(TestEvents.line(#"{"event":"roll_info","manifest":{"roll_id":"roll-1","roll_name":"Roll","created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z","runs":[],"negatives":[],"metadata":{},"film_kind":"colour","film_base":{"density":[-0.4,-0.1,-0.9],"source_name":"base.NEF","populations":[]}}}"#))'
                echo '\(finished)'
              fi
              exit 0
            fi
            exit 1
            """
        let executable = try TestSupport.writeTestExecutable(script, in: directory)
        let runner = CLIRunner(executable: executable)
        var config = FakeTetherCamera.Configuration()
        config.failDownload = true
        let camera = FakeTetherCamera(configuration: config)
        let model = CaptureSessionModel(runner: runner, camera: camera)
        model.rollURL = roll
        model.captureBaseFolder = directory
        await model.connect()
        await model.shootBaseFrame()
        #expect(model.filmBase == nil)
        #expect(model.baseFrameError != nil)
        let stateAfterFailure = await camera.connectionState
        #expect(stateAfterFailure == .ready)
        await camera.setFailDownload(false)
        await model.shootBaseFrame()
        #expect(model.filmBase != nil)
        #expect(model.baseFrameError == nil)
    }

    @Test("shootBaseFrame can replace an existing base frame")
    func shootBaseFrameReplace() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let roll = directory.appending(path: "roll", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: roll, withIntermediateDirectories: true)
        let finished = TestEvents.line(#"{"event":"finished","status":"success","exit_status":0}"#)
        let script = """
            if [ "$1" = "roll" ] && [ "$2" = "set-base-frame" ]; then
              echo '\(TestEvents.line(#"{"event":"started","command":"roll set-base-frame"}"#))'
              echo '\(TestEvents.line(#"{"event":"base_frame_set","roll_id":"roll-1"}"#))'
              echo '\(finished)'
              exit 0
            fi
            if [ "$1" = "roll" ] && [ "$2" = "info" ]; then
              echo '\(TestEvents.line(#"{"event":"started","command":"roll info"}"#))'
              echo '\(TestEvents.line(#"{"event":"roll_info","manifest":{"roll_id":"roll-1","roll_name":"Roll","created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z","runs":[],"negatives":[],"metadata":{},"film_kind":"colour","film_base":{"density":[-0.2,-0.05,-0.5],"source_name":"base-replace.NEF","populations":[]}}}"#))'
              echo '\(finished)'
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
        model.filmBase = FilmBase(fields: [
            "density": .array([.double(-0.4), .double(-0.1), .double(-0.9)]),
            "source_name": .string("base-old.NEF"),
            "populations": .array([]),
        ])
        var attached: FilmBase?
        model.onFilmBaseAttached = { attached = $0 }
        await model.connect()
        await model.shootBaseFrame()
        #expect(model.filmBase?.sourceName == "base-replace.NEF")
        #expect(model.filmBase?.density == [-0.2, -0.05, -0.5])
        #expect(attached == model.filmBase)
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

    @Test("initial interval delays first release")
    func initialIntervalDelaysFirstRelease() async throws {
        let (model, _, _) = Self.makeModel(clock: ContinuousCaptureClock(), across: 1, down: 1)
        model.intervalSeconds = 10
        await model.connect()
        model.handleSpace()
        try await Task.sleep(for: .milliseconds(100))
        #expect(model.cellStates[0] == .next)
        #expect(!model.countdownText.isEmpty)
    }

    @Test("resuming after cell 1 fired still runs the interval countdown")
    func resumeRunsCountdownAfterCell1Fired() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let (model, _, _) = Self.makeModel(clock: ContinuousCaptureClock(), across: 2, down: 1)
        model.rollURL = directory.appending(path: "roll", directoryHint: .isDirectory)
        model.captureBaseFolder = directory
        // The operator's decision (§3.2): resume always counts down the
        // full interval, even though cell 1 has already fired — a resume is
        // a fresh "hold still" warning, not a continuation of a clock that
        // was running when the operator paused. A real clock is needed here
        // (not the manual `TestCaptureClock`, whose `sleep` never actually
        // waits) since the assertion below measures wall-clock elapsed time.
        model.intervalSeconds = 3
        await model.connect()
        try await Self.waitUntil { model.isSessionOpen }
        await model.waitForLeftoverClaim()

        // Generous timeouts throughout: the full suite runs its real-clock
        // tests in parallel, which can push a fake camera round trip past
        // the default 5 s.
        model.handleSpace()
        try await Self.waitUntil(
            { if case .filled = model.cellStates[0] { true } else { false } },
            timeout: .seconds(15)
        )
        model.handleSpace()
        #expect(model.sequencePhase == .paused)

        // Let the loop notice the pause and wind down, so the resume spawns
        // a fresh loop (the still-active-loop path has its own test below).
        try await Task.sleep(for: .milliseconds(300))
        let resumeStart = ContinuousClock.now
        model.handleSpace()
        try await Self.waitUntil(
            { if case .exposing = model.cellStates[1] { true } else { false } },
            timeout: .seconds(15)
        )
        let elapsed = ContinuousClock.now - resumeStart
        #expect(elapsed >= .milliseconds(2500))
    }

    @Test("resuming a still-active loop restarts its countdown in full")
    func resumeOnActiveLoopRestartsCountdown() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let (model, _, _) = Self.makeModel(clock: ContinuousCaptureClock(), across: 2, down: 1)
        model.rollURL = directory.appending(path: "roll", directoryHint: .isDirectory)
        model.captureBaseFolder = directory
        model.intervalSeconds = 3
        await model.connect()
        try await Self.waitUntil { model.isSessionOpen }
        await model.waitForLeftoverClaim()

        model.handleSpace()
        try await Self.waitUntil(
            { if case .filled = model.cellStates[0] { true } else { false } },
            timeout: .seconds(15)
        )
        // A second into the trailing interval, pause and resume back to
        // back: the loop never sees the pause, and without a restart the
        // release would land on the old interval's end, under 2 s away.
        try await Self.waitUntil({ !model.countdownText.isEmpty }, timeout: .seconds(15))
        try await Task.sleep(for: .milliseconds(1000))
        model.handleSpace()
        model.handleSpace()
        #expect(model.sequencePhase == .running)
        let resumeStart = ContinuousClock.now
        try await Self.waitUntil(
            { if case .exposing = model.cellStates[1] { true } else { false } },
            timeout: .seconds(15)
        )
        let elapsed = ContinuousClock.now - resumeStart
        #expect(elapsed >= .milliseconds(2500))
    }

    @Test("resume is allowed while a paused shot is still in flight")
    func resumeAllowedWhileCameraBusy() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let (model, _, _) = Self.makeModel(clock: ContinuousCaptureClock(), across: 2, down: 1)
        model.rollURL = directory.appending(path: "roll", directoryHint: .isDirectory)
        model.captureBaseFolder = directory
        model.intervalSeconds = 2
        await model.connect()
        try await Self.waitUntil { model.isSessionOpen }
        await model.waitForLeftoverClaim()

        model.handleSpace()
        try await Self.waitUntil(
            { model.connectionState == .busy }, timeout: .seconds(15)
        )
        model.handleSpace()
        #expect(model.sequencePhase == .paused)
        #expect(model.canToggleSequence)
        model.handleSpace()
        #expect(model.sequencePhase == .running)
    }

    @Test("initial interval replays when resuming before cell 1 fires")
    func initialIntervalReplaysBeforeCell1Fires() async throws {
        let (model, _, _) = Self.makeModel(clock: ContinuousCaptureClock(), across: 1, down: 1)
        model.intervalSeconds = 10
        await model.connect()
        model.handleSpace()
        try await Task.sleep(for: .milliseconds(100))
        model.handleSpace()
        #expect(model.sequencePhase == .paused)
        #expect(model.cellStates[0] == .next)

        model.handleSpace()
        try await Task.sleep(for: .milliseconds(100))
        #expect(model.cellStates[0] == .next)
        #expect(!model.countdownText.isEmpty)
    }

    // MARK: - Grid

    @Test("selectGridProfile updates across, down, and cell count")
    func selectGridProfileChangesDimensions() {
        let (model, _, _) = Self.makeModel(across: 2, down: 1)
        let profile = GridProfile(fields: [
            "profile_id": .string("grid-4x2"),
            "name": .string("4×2"),
            "across": .int(4),
            "down": .int(2),
        ])!
        model.selectGridProfile(profile)
        #expect(model.gridProfileID == "grid-4x2")
        #expect(model.across == 4)
        #expect(model.down == 2)
        #expect(model.cellStates.count == 8)
    }

    @Test("re-applying a grid mid-negative keeps the frames already shot")
    func applyGridDimensionsMidNegativeKeepsFrames() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let (model, _, _) = Self.makeModel(across: 2, down: 1)
        model.rollURL = directory.appending(path: "roll", directoryHint: .isDirectory)
        model.captureBaseFolder = directory
        await model.connect()
        try await Self.waitUntil { model.isSessionOpen }
        await model.waitForLeftoverClaim()

        model.handleSpace()
        try await Self.waitUntil {
            if case .filled = model.cellStates[0] { true } else { false }
        }
        model.handleSpace()
        #expect(model.sequencePhase == .paused)

        // What a roll rescan does when a background stitch publishes.
        for (across, down) in [(2, 1), (3, 1)] {
            let profile = GridProfile(fields: [
                "profile_id": .string("grid-\(across)x\(down)"),
                "name": .string("grid"),
                "across": .int(across),
                "down": .int(down),
            ])!
            model.applyGridDimensions(from: profile)
        }
        #expect(model.across == 2)
        #expect(model.cellStates.count == 2)
        #expect(model.cellStates[0].isFilled)

        model.handleSpace()
        try await Self.waitUntil { model.completedNegatives.count == 1 }
        #expect(model.completedNegatives[0].frameURLs.count == 2)
    }

    @Test("a finished negative's thumbnails stay until the next negative starts")
    func finishedNegativeThumbnailsStayUntilNextStart() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let (model, _, _) = Self.makeModel(across: 2, down: 1)
        model.rollURL = directory.appending(path: "roll", directoryHint: .isDirectory)
        model.captureBaseFolder = directory
        await model.connect()
        try await Self.waitUntil { model.isSessionOpen }
        await model.waitForLeftoverClaim()

        model.handleSpace()
        try await Self.waitUntil { model.completedNegatives.count == 1 }
        try await Task.sleep(for: .milliseconds(300))
        #expect(model.cellStates.allSatisfy { $0.isFilled })

        model.handleSpace()
        #expect(!model.cellStates.contains { $0.isFilled })
        #expect(model.cellStates.count == 2)
    }

    @Test("gridProfileID persists in UserDefaults")
    func gridProfileIDPersists() {
        let defaults = UserDefaults(suiteName: "scanny-boy-tests-\(UUID().uuidString)")!
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/false"))
        let camera = FakeTetherCamera()
        let first = CaptureSessionModel(runner: runner, camera: camera, defaults: defaults)
        first.gridProfileID = "saved-grid"
        let second = CaptureSessionModel(runner: runner, camera: camera, defaults: defaults)
        #expect(second.gridProfileID == "saved-grid")
    }

    @Test("discardUnpublishedCaptures clears completed negatives")
    func discardUnpublishedCaptures() async throws {
        let clock = TestCaptureClock()
        let (model, _, _) = Self.makeModel(clock: clock, across: 1, down: 1)
        await model.connect()
        model.handleSpace()
        try await Task.sleep(for: .milliseconds(3500))
        #expect(model.completedNegatives.count == 1)
        let urls = model.discardUnpublishedCaptures(publishedNegativeIDs: [])
        #expect(model.completedNegatives.isEmpty)
        #expect(!urls.isEmpty)
    }

    // MARK: - Leftovers

    private static func leftover(handle: UInt32 = TetherTiming.bufferScanFirst) -> BufferLeftover {
        BufferLeftover(
            handle: handle,
            objectInfo: PTP.ObjectInfo(
                handle: handle,
                storageID: 0,
                objectFormat: 0x3801,
                size: 4,
                width: 100,
                height: 100,
                filename: "DSC_0000.NEF",
                captureDate: "20260913T150938"
            )
        )
    }

    private static func waitUntil(
        _ condition: @MainActor () -> Bool,
        timeout: Duration = .seconds(5)
    ) async throws {
        let deadline = ContinuousClock.now + timeout
        while !condition() {
            guard ContinuousClock.now < deadline else {
                Issue.record("condition not met within \(timeout)")
                return
            }
            try await Task.sleep(for: .milliseconds(20))
        }
    }

    @Test("connect saves a leftover to _unclaimed and blocks capture until resolved")
    func connectClaimsLeftover() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let camera = FakeTetherCamera(configuration: .init(initialLeftovers: [Self.leftover()]))
        let (model, _, _) = Self.makeModel(camera: camera)
        model.rollURL = directory.appending(path: "roll", directoryHint: .isDirectory)
        model.captureBaseFolder = directory
        await model.connect()
        try await Self.waitUntil { !model.leftoverFrames.isEmpty }

        let frame = try #require(model.leftoverFrames.first)
        #expect(frame.url.lastPathComponent == "leftover-20260913-150938.NEF")
        #expect(FileManager.default.fileExists(atPath: frame.url.path))
        #expect(await camera.leftovers.isEmpty)
        #expect(model.runEnabled == false)
        #expect(model.startBlockedReason == CaptureSessionModel.leftoverBlockedReason)
        #expect(model.leftoverTargetCell == nil)

        model.keepLeftover(frame.id)
        #expect(model.leftoverFrames.isEmpty)
        #expect(FileManager.default.fileExists(atPath: frame.url.path))
        #expect(model.runEnabled)
    }

    @Test("discarding a leftover deletes its file")
    func discardLeftoverDeletesFile() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let camera = FakeTetherCamera(configuration: .init(initialLeftovers: [Self.leftover()]))
        let (model, _, _) = Self.makeModel(camera: camera)
        model.rollURL = directory.appending(path: "roll", directoryHint: .isDirectory)
        model.captureBaseFolder = directory
        await model.connect()
        try await Self.waitUntil { !model.leftoverFrames.isEmpty }

        let frame = try #require(model.leftoverFrames.first)
        model.discardLeftover(frame.id)
        #expect(model.leftoverFrames.isEmpty)
        #expect(FileManager.default.fileExists(atPath: frame.url.path) == false)
    }

    @Test("a sequence that finds a leftover pauses, and the leftover can fill its one cell")
    func sequenceLeftoverFillsCell() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let (model, camera, _) = Self.makeModel(across: 1, down: 1)
        model.rollURL = directory.appending(path: "roll", directoryHint: .isDirectory)
        model.captureBaseFolder = directory
        await model.connect()
        try await Self.waitUntil { model.isSessionOpen }
        await model.waitForLeftoverClaim()
        await camera.addLeftover(Self.leftover())

        model.handleSpace()
        try await Self.waitUntil { !model.leftoverFrames.isEmpty }
        #expect(model.sequencePhase == .paused)
        #expect(model.canToggleSequence == false)
        #expect(model.leftoverTargetCell == 0)

        let frame = try #require(model.leftoverFrames.first)
        model.useLeftover(frame.id)
        #expect(model.leftoverFrames.isEmpty)
        #expect(model.completedNegatives.count == 1)
        let url = try #require(model.completedNegatives.first?.frameURLs.first)
        #expect(url.lastPathComponent.hasSuffix("_01.NEF"))
        #expect(FileManager.default.fileExists(atPath: url.path))
        #expect(FileManager.default.fileExists(atPath: frame.url.path) == false)
    }

    @Test("consecutive cells each download, though the camera reuses the freed buffer handle")
    func consecutiveCellsReuseBufferHandle() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let (model, camera, _) = Self.makeModel(across: 3, down: 1)
        model.rollURL = directory.appending(path: "roll", directoryHint: .isDirectory)
        model.captureBaseFolder = directory
        await model.connect()
        try await Self.waitUntil { model.isSessionOpen }
        await model.waitForLeftoverClaim()

        model.handleSpace()
        try await Self.waitUntil({ model.completedNegatives.count == 1 }, timeout: .seconds(20))
        let frames = try #require(model.completedNegatives.first?.frameURLs)
        #expect(frames.map(\.lastPathComponent).sorted() == frames.map(\.lastPathComponent))
        #expect(Set(frames.map(\.lastPathComponent)).count == 3)
        #expect(try await camera.scanBuffer().isEmpty)
    }

    @Test("resume shoots only the cells a paused negative still needs")
    func resumeSkipsFilledCells() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let camera = FakeTetherCamera(configuration: .init(failDownload: true))
        let (model, _, _) = Self.makeModel(across: 2, down: 1, camera: camera)
        model.rollURL = directory.appending(path: "roll", directoryHint: .isDirectory)
        model.captureBaseFolder = directory
        await model.connect()
        try await Self.waitUntil { model.isSessionOpen }
        await model.waitForLeftoverClaim()

        model.handleSpace()
        try await Self.waitUntil { model.sequencePhase == .paused }
        #expect(model.cellStates.first.map { if case .failed = $0 { true } else { false } } == true)

        await camera.setFailDownload(false)
        model.handleSpace()
        try await Self.waitUntil { model.completedNegatives.count == 1 }
        let frames = try #require(model.completedNegatives.first?.frameURLs)
        #expect(frames.count == 2)
        #expect(Set(frames.map(\.lastPathComponent)).count == 2)
    }

    // MARK: - Cancellation (bug 1: stop must not fail a cell or leak state)

    @Test("stopping mid-shot leaves the sequence idle without failing a cell")
    func stopDuringShotLeavesNoFailedCell() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let (model, _, _) = Self.makeModel(across: 1, down: 1)
        model.rollURL = directory.appending(path: "roll", directoryHint: .isDirectory)
        model.captureBaseFolder = directory
        await model.connect()
        try await Self.waitUntil { model.isSessionOpen }
        await model.waitForLeftoverClaim()

        model.handleSpace()
        try await Self.waitUntil {
            if case .exposing = model.cellStates[0] { true } else { false }
        }
        model.handleEscape()
        #expect(model.sequencePhase == .idle)
        #expect(model.countdownText.isEmpty)

        // Give the cancelled runSequence time to reach the awaits it was
        // suspended in and their throws/catches; before the fix, that
        // clobbered the cell as `.failed` and flipped back to `.paused`.
        try await Task.sleep(for: .milliseconds(2500))
        #expect(model.sequencePhase == .idle)
        #expect(!model.cellStates.contains { if case .failed = $0 { true } else { false } })
    }

    @Test("a cancelled negative's task doesn't clobber a freshly started one")
    func cancelledTaskDoesNotClobberRestart() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let (model, _, _) = Self.makeModel(across: 1, down: 1)
        model.rollURL = directory.appending(path: "roll", directoryHint: .isDirectory)
        model.captureBaseFolder = directory
        await model.connect()
        try await Self.waitUntil { model.isSessionOpen }
        await model.waitForLeftoverClaim()

        model.handleSpace()
        try await Self.waitUntil {
            if case .exposing = model.cellStates[0] { true } else { false }
        }
        model.handleEscape()
        #expect(model.sequencePhase == .idle)

        // Wait for the camera to settle back to `.ready` (the cancelled
        // release/exposure wait resets it on its way out) before starting
        // again, same as a user waiting a beat before pressing Space —
        // the old task's catch, when it eventually fires, must not clobber
        // the new negative's cells or phase.
        try await Self.waitUntil { model.runEnabled }
        model.handleSpace()
        try await Self.waitUntil { model.completedNegatives.count == 1 }
        try await Task.sleep(for: .milliseconds(500))
        #expect(model.sequencePhase == .idle)
        #expect(!model.cellStates.contains { if case .failed = $0 { true } else { false } })
        #expect(model.completedNegatives.count == 1)
    }

    // MARK: - Countdown text (bug 2)

    @Test("countdown text clears when paused during an interval")
    func countdownClearsWhenPaused() async throws {
        let (model, _, _) = Self.makeModel(clock: ContinuousCaptureClock(), across: 1, down: 1)
        model.intervalSeconds = 10
        await model.connect()
        model.handleSpace()
        try await Task.sleep(for: .milliseconds(100))
        #expect(!model.countdownText.isEmpty)

        model.handleSpace()
        #expect(model.sequencePhase == .paused)
        try await Task.sleep(for: .milliseconds(150))
        #expect(model.countdownText.isEmpty)
    }

    // MARK: - One-shot / sequence guards (bug 4)

    @Test("runEnabled is false while a one-shot capture is in flight")
    func runEnabledBlockedDuringOneShot() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let (model, _, _) = Self.makeModel()
        model.rollURL = directory.appending(path: "roll", directoryHint: .isDirectory)
        model.captureBaseFolder = directory
        await model.connect()
        try await Self.waitUntil { model.isSessionOpen }
        await model.waitForLeftoverClaim()
        #expect(model.runEnabled == true)

        let shootTask = Task { await model.shootBaseFrame() }
        try await Self.waitUntil { model.isShootingBaseFrame }
        #expect(model.runEnabled == false)
        #expect(model.canToggleSequence == false)
        model.handleSpace()
        #expect(model.sequencePhase == .idle)
        _ = await shootTask.value
    }

    @Test("a running sequence blocks starting a one-shot capture")
    func oneShotBlockedDuringSequence() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let (model, _, _) = Self.makeModel(across: 1, down: 1)
        model.rollURL = directory.appending(path: "roll", directoryHint: .isDirectory)
        model.captureBaseFolder = directory
        await model.connect()
        try await Self.waitUntil { model.isSessionOpen }
        await model.waitForLeftoverClaim()

        model.handleSpace()
        try await Self.waitUntil { model.sequencePhase == .running }
        await model.shootBaseFrame()
        #expect(model.isShootingBaseFrame == false)
        #expect(model.baseFrameError == nil)
        try await Self.waitUntil { model.completedNegatives.count == 1 }
    }

    // MARK: - Dropped connection (bug 6)

    @Test("a dropped connection mid-negative pauses and keeps filled cells")
    func droppedConnectionKeepsFilledCells() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let (model, _, _) = Self.makeModel(across: 2, down: 1)
        model.rollURL = directory.appending(path: "roll", directoryHint: .isDirectory)
        model.captureBaseFolder = directory
        await model.connect()
        try await Self.waitUntil { model.isSessionOpen }
        await model.waitForLeftoverClaim()

        model.handleSpace()
        try await Self.waitUntil {
            if case .filled = model.cellStates[0] { true } else { false }
        }
        // The connection handler calls exactly this when the device
        // disappears mid-sequence (§2.5).
        model.closeSession()
        #expect(model.isSessionOpen == false)
        #expect(model.sequencePhase == .paused)
        #expect(model.cellStates[0].isFilled)
        #expect(model.cellStates[1] == .next)

        // Reconnecting must not wipe the filled cell.
        model.openSession()
        #expect(model.cellStates[0].isFilled)
        #expect(model.cellStates[1] == .next)

        // The fake camera's connection state was never dropped, so resuming
        // can finish the negative — once the cancelled task has actually
        // wound down (cancellation is cooperative and asynchronous; give it
        // a moment, as a user reconnecting takes far longer in practice).
        try await Task.sleep(for: .milliseconds(300))
        model.handleSpace()
        try await Self.waitUntil { model.completedNegatives.count == 1 }
        #expect(model.completedNegatives.first?.frameURLs.count == 2)
    }

    @Test("closing a session with no negative open goes straight to idle")
    func closeWithNoOpenNegativeStaysIdle() async throws {
        let (model, _, _) = Self.makeModel()
        await model.connect()
        #expect(model.isSessionOpen == true)
        model.closeSession()
        #expect(model.sequencePhase == .idle)
        #expect(model.isSessionOpen == false)
    }

    // MARK: - Retake (bug 7)

    @Test("retake trashes the old file, reuses its name, and leaves one next cell")
    func retakeReplacesFileInPlace() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        // A real clock, with the app's own default interval: the gap after
        // cell 0 (§3.2) must still have real time left when it fills —
        // frame arrival plus download alone take ~1.65s — so the paused
        // loop is actually suspended (not already past the guard that
        // would let it start cell 1) when the pause below reaches it.
        // Otherwise that still-active loop's own `pending` list, captured
        // before the retake, shoots over it instead of the retaken cell.
        let (model, _, _) = Self.makeModel(clock: ContinuousCaptureClock(), across: 2, down: 1)
        model.intervalSeconds = 4
        model.rollURL = directory.appending(path: "roll", directoryHint: .isDirectory)
        model.captureBaseFolder = directory
        await model.connect()
        try await Self.waitUntil { model.isSessionOpen }
        await model.waitForLeftoverClaim()

        model.handleSpace()
        try await Self.waitUntil(
            { if case .filled = model.cellStates[0] { true } else { false } },
            timeout: .seconds(10)
        )
        model.handleSpace()
        #expect(model.sequencePhase == .paused)
        try await Task.sleep(for: .milliseconds(300))
        guard case .filled(let oldURL) = model.cellStates[0] else {
            Issue.record("expected cell 0 filled before retake")
            return
        }

        model.handleDelete()
        #expect(model.cellStates[0] == .next)
        // Any other cell previously marked "up next" must be demoted, so
        // there is only ever one `.next` cell on screen.
        #expect(model.cellStates[1] == .empty)

        model.handleSpace()
        try await Self.waitUntil({ model.completedNegatives.count == 1 }, timeout: .seconds(20))
        let frames = try #require(model.completedNegatives.first?.frameURLs)
        // The reshoot reuses cell 0's original name — proof the old file was
        // moved out of the way rather than left behind under it.
        #expect(frames[0].lastPathComponent == oldURL.lastPathComponent)
    }

    // MARK: - Stopped negative (§3.2)

    @Test("Esc after a cell has filled stops the negative, keeping its cells")
    func escWithFilledCellStops() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let (model, _, _) = Self.makeModel(across: 2, down: 1)
        model.rollURL = directory.appending(path: "roll", directoryHint: .isDirectory)
        model.captureBaseFolder = directory
        await model.connect()
        try await Self.waitUntil { model.isSessionOpen }
        await model.waitForLeftoverClaim()

        var completed: CaptureSessionModel.CompletedNegative?
        model.onNegativeCompleted = { completed = $0 }

        model.handleSpace()
        try await Self.waitUntil {
            if case .filled = model.cellStates[0] { true } else { false }
        }
        model.handleEscape()
        #expect(model.sequencePhase == .stopped)
        #expect(model.cellStates[0].isFilled)
        #expect(model.cellStates[1] == .next)
        #expect(completed == nil)
        #expect(model.completedNegatives.isEmpty)
    }

    @Test("Esc before any cell fills goes straight to idle")
    func escWithNoFilledCellGoesIdle() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let (model, _, _) = Self.makeModel(clock: ContinuousCaptureClock(), across: 1, down: 1)
        model.intervalSeconds = 10
        model.rollURL = directory.appending(path: "roll", directoryHint: .isDirectory)
        model.captureBaseFolder = directory
        await model.connect()
        try await Self.waitUntil { model.isSessionOpen }
        await model.waitForLeftoverClaim()

        model.handleSpace()
        try await Task.sleep(for: .milliseconds(100))
        #expect(model.cellStates[0] == .next)
        model.handleEscape()
        #expect(model.sequencePhase == .idle)
    }

    @Test("resume from stopped shoots only the unfilled cells, reusing the same stamp")
    func resumeFromStoppedCompletesNegative() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        // A real clock: the assertion below measures wall-clock elapsed time
        // to confirm the countdown actually runs (the manual `TestCaptureClock`
        // used elsewhere never really waits).
        let (model, _, _) = Self.makeModel(clock: ContinuousCaptureClock(), across: 2, down: 1)
        model.rollURL = directory.appending(path: "roll", directoryHint: .isDirectory)
        model.captureBaseFolder = directory
        model.intervalSeconds = 2
        await model.connect()
        try await Self.waitUntil { model.isSessionOpen }
        await model.waitForLeftoverClaim()

        // Generous timeouts throughout: the full suite runs its real-clock
        // tests in parallel, which can push a fake camera round trip past
        // the default 5 s.
        model.handleSpace()
        try await Self.waitUntil(
            { if case .filled = model.cellStates[0] { true } else { false } },
            timeout: .seconds(15)
        )
        guard case .filled(let firstURL) = model.cellStates[0] else {
            Issue.record("expected cell 0 filled")
            return
        }
        model.handleEscape()
        #expect(model.sequencePhase == .stopped)

        // Give the cancelled sequence task time to actually wind down
        // before resuming, as the existing cancellation tests do.
        try await Task.sleep(for: .milliseconds(300))
        let resumeStart = ContinuousClock.now
        model.handleSpace()
        #expect(model.sequencePhase == .running)
        // Cell 0 must not be reshot: only the unfilled cell moves.
        try await Self.waitUntil(
            { if case .exposing = model.cellStates[1] { true } else { false } },
            timeout: .seconds(15)
        )
        let elapsed = ContinuousClock.now - resumeStart
        #expect(elapsed >= .milliseconds(1500))
        #expect(model.cellStates[0] == .filled(firstURL))

        try await Self.waitUntil({ model.completedNegatives.count == 1 }, timeout: .seconds(15))
        let frames = try #require(model.completedNegatives.first?.frameURLs)
        #expect(frames.count == 2)
        #expect(frames[0] == firstURL)
        // Same stamp: both frames share the timestamp prefix before "_cc.NEF".
        let stamp0 = frames[0].lastPathComponent.prefix(while: { $0 != "_" })
        let stamp1 = frames[1].lastPathComponent.prefix(while: { $0 != "_" })
        #expect(stamp0 == stamp1)
    }

    @Test("discard negative trashes exactly the filled files and returns to idle")
    func discardStoppedNegativeTrashesFiles() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let (model, _, _) = Self.makeModel(across: 2, down: 1)
        model.rollURL = directory.appending(path: "roll", directoryHint: .isDirectory)
        model.captureBaseFolder = directory
        await model.connect()
        try await Self.waitUntil { model.isSessionOpen }
        await model.waitForLeftoverClaim()

        model.handleSpace()
        try await Self.waitUntil {
            if case .filled = model.cellStates[0] { true } else { false }
        }
        guard case .filled(let url) = model.cellStates[0] else {
            Issue.record("expected cell 0 filled")
            return
        }
        model.handleEscape()
        #expect(model.sequencePhase == .stopped)

        model.discardStoppedNegative()
        #expect(model.sequencePhase == .idle)
        #expect(!model.cellStates.contains { $0.isFilled })
        // `NSWorkspace.recycle` moves the file asynchronously (fire-and-forget,
        // matching `retakeLastFilledCell`'s existing style), so give it a
        // moment rather than asserting immediately after the call returns.
        try await Self.waitUntil({ !FileManager.default.fileExists(atPath: url.path) }, timeout: .seconds(5))
    }

    @Test("start is blocked while a negative is stopped")
    func startBlockedWhileStopped() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let (model, _, _) = Self.makeModel(across: 2, down: 1)
        model.rollURL = directory.appending(path: "roll", directoryHint: .isDirectory)
        model.captureBaseFolder = directory
        await model.connect()
        try await Self.waitUntil { model.isSessionOpen }
        await model.waitForLeftoverClaim()

        model.handleSpace()
        try await Self.waitUntil {
            if case .filled = model.cellStates[0] { true } else { false }
        }
        model.handleEscape()
        #expect(model.sequencePhase == .stopped)
        #expect(model.runEnabled == false)

        // Space while stopped resumes the same negative rather than
        // starting a fresh one — its filled cell survives untouched.
        let filledBefore = model.cellStates[0]
        try await Task.sleep(for: .milliseconds(300))
        model.handleSpace()
        #expect(model.sequencePhase == .running)
        #expect(model.cellStates[0] == filledBefore)
    }

    @Test("a dropped connection while stopped keeps it stopped with cells kept")
    func droppedConnectionWhileStoppedStaysStopped() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let (model, _, _) = Self.makeModel(across: 2, down: 1)
        model.rollURL = directory.appending(path: "roll", directoryHint: .isDirectory)
        model.captureBaseFolder = directory
        await model.connect()
        try await Self.waitUntil { model.isSessionOpen }
        await model.waitForLeftoverClaim()

        model.handleSpace()
        try await Self.waitUntil {
            if case .filled = model.cellStates[0] { true } else { false }
        }
        model.handleEscape()
        #expect(model.sequencePhase == .stopped)

        model.closeSession()
        #expect(model.isSessionOpen == false)
        #expect(model.sequencePhase == .stopped)
        #expect(model.cellStates[0].isFilled)
        #expect(model.cellStates[1] == .next)

        model.openSession()
        #expect(model.sequencePhase == .stopped)
        #expect(model.cellStates[0].isFilled)
        #expect(model.cellStates[1] == .next)
    }

    @Test("discardUnpublishedCaptures includes a stopped negative's frames and leaves the model idle")
    func discardUnpublishedCapturesIncludesStoppedNegative() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let (model, _, _) = Self.makeModel(across: 2, down: 1)
        model.rollURL = directory.appending(path: "roll", directoryHint: .isDirectory)
        model.captureBaseFolder = directory
        await model.connect()
        try await Self.waitUntil { model.isSessionOpen }
        await model.waitForLeftoverClaim()

        model.handleSpace()
        try await Self.waitUntil {
            if case .filled = model.cellStates[0] { true } else { false }
        }
        guard case .filled(let url) = model.cellStates[0] else {
            Issue.record("expected cell 0 filled")
            return
        }
        model.handleEscape()
        #expect(model.sequencePhase == .stopped)

        let urls = model.discardUnpublishedCaptures(publishedNegativeIDs: [])
        #expect(model.sequencePhase == .idle)
        #expect(urls.contains(url))
        #expect(!model.cellStates.contains { $0.isFilled })
    }
}
