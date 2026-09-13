import AppKit
import Foundation
import Observation

/// Injectable clock for sequence timing tests.
protocol CaptureClock: Sendable {
    func now() -> Date
    func sleep(until: Date) async throws
}

struct ContinuousCaptureClock: CaptureClock {
    func now() -> Date { Date() }
    func sleep(until target: Date) async throws {
        let interval = target.timeIntervalSinceNow
        if interval > 0 {
            try await Task.sleep(for: .seconds(interval))
        }
    }
}

enum CaptureCellState: Sendable, Equatable {
    case empty
    case next
    case exposing
    case downloading
    case filled(URL)
    case failed(String)
}

enum CaptureSequencePhase: Sendable, Equatable {
    case idle
    case running
    case paused
    case waitingForDownload
}

/// Setup, sequence state machine, interval clock, and cues for the Capture tab.
@MainActor
@Observable
final class CaptureSessionModel {
    static let lastIntervalKey = "com.lonniesmith.scanny-boy.captureInterval"
    static let cuesEnabledKey = "com.lonniesmith.scanny-boy.captureCuesEnabled"
    static let destinationKey = "com.lonniesmith.scanny-boy.captureDestination"
    static let captureBaseKey = "com.lonniesmith.scanny-boy.captureBaseFolder"

    struct CompletedNegative: Identifiable, Sendable {
        let id: UUID
        let stamp: String
        let frameURLs: [URL]
        let startedAt: Date
    }

    let runner: CLIRunner
    private let camera: any CameraControlling
    private let clock: any CaptureClock
    private let defaults: UserDefaults
    private var connectionHandlerInstalled = false
    private var sequenceTask: Task<Void, Never>?

    var rollURL: URL?
    var filmKind: String?
    var filmBase: FilmBase?
    var rigProfileID: String?
    var flatField: FlatFieldReference?
    var gridProfileID: String?
    var across: Int?
    var down: Int = 1
    var intervalSeconds: Int {
        didSet { defaults.set(intervalSeconds, forKey: Self.lastIntervalKey) }
    }

    var cuesEnabled: Bool {
        didSet { defaults.set(cuesEnabled, forKey: Self.cuesEnabledKey) }
    }

    var destination: CaptureDestination {
        didSet {
            defaults.set(destination.rawValue, forKey: Self.destinationKey)
            Task { await camera.applyDestination(destination) }
        }
    }

    var captureBaseFolder: URL {
        didSet { defaults.set(captureBaseFolder, forKey: Self.captureBaseKey) }
    }

    private(set) var connectionState: TetherConnectionState = .absent
    private(set) var exposure: TetherExposureSettings?
    private(set) var cellStates: [CaptureCellState] = []
    private(set) var sequencePhase: CaptureSequencePhase = .idle
    private(set) var countdownText = ""
    private(set) var completedNegatives: [CompletedNegative] = []
    private(set) var isSessionOpen = false
    private(set) var isShootingBaseFrame = false
    private(set) var baseFrameError: ConfigurationModel.Issue?
    private(set) var isShootingFlatFieldReference = false
    private(set) var flatFieldReferenceError: ConfigurationModel.Issue?
    private(set) var referenceAperture: UInt32?
    var onFlatFieldReferenceAttached: ((FlatFieldReference) -> Void)?
    private(set) var currentNegativeIndex = 0
    private(set) var pausedAfterCell = false
    private(set) var cellWarnings: [Int: [String]] = [:]
    private var baselineFrameURLs: [URL] = []
    private(set) var focusAssist: FocusAssistModel

    var sessionOpen: Bool {
        get { isSessionOpen }
        set {
            guard newValue != isSessionOpen else { return }
            if newValue { openSession() } else { closeSession() }
        }
    }

    init(
        runner: CLIRunner,
        camera: any CameraControlling,
        clock: any CaptureClock = ContinuousCaptureClock(),
        defaults: UserDefaults = .standard
    ) {
        self.runner = runner
        self.camera = camera
        self.clock = clock
        self.defaults = defaults
        intervalSeconds = defaults.object(forKey: Self.lastIntervalKey) as? Int ?? 4
        cuesEnabled = defaults.object(forKey: Self.cuesEnabledKey) as? Bool ?? true
        destination = CaptureDestination(
            rawValue: defaults.string(forKey: Self.destinationKey) ?? CaptureDestination.buffer.rawValue
        ) ?? .buffer
        captureBaseFolder = defaults.url(forKey: Self.captureBaseKey)
            ?? FileManager.default.homeDirectoryForCurrentUser
                .appending(path: "Pictures/Scanny Boy Captures", directoryHint: .isDirectory)
        focusAssist = FocusAssistModel(camera: camera, runner: runner)
    }

    var perNegative: Int? {
        across.map { $0 * down }
    }

    var captureFolder: URL? {
        guard let rollURL else { return nil }
        return captureBaseFolder.appending(path: rollURL.lastPathComponent, directoryHint: .isDirectory)
    }

    var runEnabled: Bool {
        connectionState == .ready
            && (exposure?.isManualProgram ?? false)
            && (exposure?.isManualFocus ?? false)
            && perNegative != nil
            && rollURL != nil
            && flatField != nil
            && filmKind != nil
            && filmBase != nil
            && sequencePhase == .idle
    }

    var apertureMismatchWarning: String? {
        guard let referenceAperture, let exposure else { return nil }
        guard exposure.aperture != referenceAperture else { return nil }
        let expected = PTP.decodePropertyValue(0x5007, raw: referenceAperture)
        let current = PTP.decodePropertyValue(0x5007, raw: exposure.aperture)
        return "Aperture is \(current); bare-light reference was shot at \(expected)."
    }

    func connect() async {
        if !connectionHandlerInstalled {
            await camera.setConnectionHandler { [weak self] state, exposure in
                Task { @MainActor in
                    guard let self else { return }
                    self.connectionState = state
                    self.exposure = exposure
                    self.focusAssist.updateConnectionState(state)
                }
            }
            connectionHandlerInstalled = true
        }
        await camera.applyDestination(destination)
        await camera.startBrowsing()
    }

    func disconnect() async {
        await camera.stopBrowsing()
    }

    func refreshConnection() async {
        await connect()
    }

    func openSession() {
        guard !isSessionOpen else { return }
        isSessionOpen = true
        resetCells()
    }

    func closeSession() {
        Task { await focusAssist.close() }
        sequenceTask?.cancel()
        sequenceTask = nil
        isSessionOpen = false
        sequencePhase = .idle
        countdownText = ""
        onSessionClosed?()
    }

    func handleSpace() {
        if focusAssist.isOpen { return }
        switch sequencePhase {
        case .idle where runEnabled:
            startNegative()
        case .running:
            pauseAfterCurrentShot()
        case .paused:
            resumeSequence()
        default:
            break
        }
    }

    func handleDelete() {
        guard sequencePhase == .paused else { return }
        retakeLastFilledCell()
    }

    func handleEscape() {
        guard sequencePhase == .running || sequencePhase == .paused else { return }
        stopNegative()
    }

    func shootFlatFieldReference() async {
        guard flatField?.lockedAt == nil, let rollURL, let captureFolder else { return }
        isShootingFlatFieldReference = true
        flatFieldReferenceError = nil
        defer { isShootingFlatFieldReference = false }
        do {
            await focusAssist.close()
            let url = try CaptureNaming.bareLightURL(in: captureFolder, at: clock.now())
            try await captureOneFrame(to: url)
            referenceAperture = exposure?.aperture
            let result = await Self.runSetFlatFieldReference(
                runner: runner,
                roll: rollURL,
                frame: url,
                rig: rigProfileID
            )
            if let error = result.error {
                flatFieldReferenceError = error
            } else if let flatField = result.flatField {
                self.flatField = flatField
                onFlatFieldReferenceAttached?(flatField)
            }
        } catch {
            flatFieldReferenceError = ConfigurationModel.Issue(
                code: .internalError, message: error.localizedDescription
            )
        }
    }

    func shootBaseFrame() async {
        guard filmBase?.lockedAt == nil, let rollURL, let captureFolder else { return }
        isShootingBaseFrame = true
        baseFrameError = nil
        defer { isShootingBaseFrame = false }
        do {
            await focusAssist.close()
            let url = try CaptureNaming.exclusiveURL(
                in: captureFolder, firstRelease: clock.now(), shotNumber: 1
            )
            try await captureOneFrame(to: url)
            let result = await Self.runSetBaseFrame(
                runner: runner, roll: rollURL, frame: url
            )
            if let error = result.error {
                baseFrameError = error
            } else {
                filmBase = result.filmBase
            }
        } catch {
            baseFrameError = ConfigurationModel.Issue(code: .internalError, message: error.localizedDescription)
        }
    }

    private func startNegative() {
        guard let count = perNegative else { return }
        resetCells()
        for index in 0..<count {
            cellStates[index] = index == 0 ? .next : .empty
        }
        sequencePhase = .running
        focusAssist.updateSequencePhase(sequencePhase)
        currentNegativeIndex = completedNegatives.count
        sequenceTask = Task {
            await focusAssist.close()
            await runSequence()
        }
    }

    private func runSequence() async {
        guard let count = perNegative, let folder = captureFolder else { return }
        let firstRelease = clock.now()
        var handlesBefore = Set<UInt32>()
        do {
            try await camera.drainEvents()
            handlesBefore = Set(try await camera.scanBuffer())
        } catch TetherCaptureError.leftoverPresent {
            sequencePhase = .paused
            focusAssist.updateSequencePhase(sequencePhase)
            return
        } catch {
            markNextFailed(error.localizedDescription)
            return
        }

        for shot in 1...count {
            guard !Task.isCancelled else { return }
            let cellIndex = shot - 1
            cellStates[cellIndex] = .exposing
            markNext(after: cellIndex)

            do {
                try await camera.release()
                playMoveCue()
                try await camera.waitForExposureEnd()

                if shot < count {
                    let intervalEnd = clock.now().addingTimeInterval(TimeInterval(intervalSeconds))
                    try await waitForInterval(end: intervalEnd)
                    playHoldCue(at: intervalEnd.addingTimeInterval(-TetherTiming.holdCueLead))
                }

                cellStates[cellIndex] = .downloading
                let url = try CaptureNaming.exclusiveURL(
                    in: folder, firstRelease: firstRelease, shotNumber: shot
                )
                let handle = try await camera.waitForFrame(after: handlesBefore)
                let frame = try await camera.download(handle: handle, to: url)
                try await camera.confirmBufferCleared(handle: frame.handle)
                cellStates[cellIndex] = .filled(url)
                handlesBefore.insert(handle)
                analyzeFrame(url, cellIndex: cellIndex)
            } catch {
                cellStates[cellIndex] = .failed(error.localizedDescription)
                sequencePhase = .paused
                focusAssist.updateSequencePhase(sequencePhase)
                return
            }
        }
        finishNegative(firstRelease: firstRelease)
    }

    private func finishNegative(firstRelease: Date) {
        let stamp = CaptureNaming.filename(firstRelease: firstRelease, shotNumber: 1)
            .replacingOccurrences(of: "_01.NEF", with: "")
        let frames = cellStates.compactMap { state -> URL? in
            if case .filled(let url) = state { return url }
            return nil
        }
        completedNegatives.append(
            CompletedNegative(id: UUID(), stamp: stamp, frameURLs: frames, startedAt: firstRelease)
        )
        if baselineFrameURLs.isEmpty {
            baselineFrameURLs = frames
        }
        sequencePhase = .idle
        focusAssist.updateSequencePhase(sequencePhase)
        countdownText = ""
        onNegativeCompleted?(completedNegatives.last!)
    }

    var onNegativeCompleted: ((CompletedNegative) -> Void)?
    var onSessionClosed: (() -> Void)?

    private func waitForInterval(end: Date) async throws {
        while clock.now() < end {
            if sequencePhase == .paused { return }
            if case .downloading = cellStates.first(where: { if case .downloading = $0 { true } else { false } }) {
                countdownText = "waiting for download"
                try await clock.sleep(until: clock.now().addingTimeInterval(0.1))
                continue
            }
            let remaining = end.timeIntervalSince(clock.now())
            countdownText = String(format: "%.1f s", max(0, remaining))
            try await clock.sleep(until: min(end, clock.now().addingTimeInterval(0.1)))
        }
        countdownText = ""
    }

    private func playMoveCue() {
        guard cuesEnabled else { return }
        NSSound(named: "Tink")?.play()
    }

    private func playHoldCue(at _: Date) {
        guard cuesEnabled else { return }
        NSSound(named: "Pop")?.play()
    }

    private func pauseAfterCurrentShot() {
        pausedAfterCell = true
        sequencePhase = .paused
        focusAssist.updateSequencePhase(sequencePhase)
    }

    private func resumeSequence() {
        guard sequencePhase == .paused else { return }
        sequencePhase = .running
        focusAssist.updateSequencePhase(sequencePhase)
        pausedAfterCell = false
        sequenceTask = Task { await runSequence() }
    }

    private func stopNegative() {
        sequenceTask?.cancel()
        sequencePhase = .idle
        focusAssist.updateSequencePhase(sequencePhase)
        countdownText = ""
    }

    private func retakeLastFilledCell() {
        guard let index = cellStates.lastIndex(where: {
            if case .filled = $0 { true } else { false }
        }) else { return }
        cellStates[index] = .next
    }

    private func captureOneFrame(to url: URL) async throws {
        try await camera.drainEvents()
        let handlesBefore = Set(try await scanBufferClearingLeftovers())
        try await camera.release()
        try await camera.waitForExposureEnd()
        let handle = try await camera.waitForFrame(after: handlesBefore)
        let frame = try await camera.download(handle: handle, to: url)
        try await camera.confirmBufferCleared(handle: frame.handle)
    }

    /// A leftover from a previous failed release must not block a shot the
    /// operator just asked for. Sequence cells still refuse leftovers
    /// (`runSequence`); a single-frame reference or base shot discards them.
    private func scanBufferClearingLeftovers() async throws -> [UInt32] {
        do {
            return try await camera.scanBuffer()
        } catch TetherCaptureError.leftoverPresent(let leftovers) {
            for leftover in leftovers {
                try await camera.discardBufferFrame(handle: leftover.handle)
            }
            return try await camera.scanBuffer()
        }
    }

    private func resetCells() {
        guard let count = perNegative else {
            cellStates = []
            cellWarnings = [:]
            return
        }
        cellStates = Array(repeating: .empty, count: count)
        cellWarnings = [:]
    }

    private func analyzeFrame(_ url: URL, cellIndex: Int) {
        guard let folder = captureFolder else { return }
        let log = folder.appendingPathComponent("capture-log.jsonl")
        Task {
            do {
                let session = runner.session(
                    for: .captureAnalyze(frame: url, log: log, baselines: baselineFrameURLs)
                )
                for await output in try await session.start() {
                    guard case .event(let event) = output, event.kind == .frameAnalyzed else {
                        continue
                    }
                    let warnings = event.warnings ?? []
                    if !warnings.isEmpty {
                        cellWarnings[cellIndex] = warnings
                    }
                }
            } catch {
                // Advisory analysis — never block capture.
            }
        }
    }

    private func markNext(after index: Int) {
        guard index + 1 < cellStates.count else { return }
        if case .empty = cellStates[index + 1] {
            cellStates[index + 1] = .next
        }
    }

    private func markNextFailed(_ message: String) {
        if let index = cellStates.firstIndex(where: { $0 == .next || $0 == .exposing }) {
            cellStates[index] = .failed(message)
        }
        sequencePhase = .paused
        focusAssist.updateSequencePhase(sequencePhase)
    }

    func applyGridDimensions(from profile: GridProfile) {
        down = profile.down
        across = profile.across
        resetCells()
    }

    private struct SetBaseFrameResult: Sendable {
        var filmBase: FilmBase?
        var error: ConfigurationModel.Issue?
    }

    private struct SetFlatFieldReferenceResult: Sendable {
        var flatField: FlatFieldReference?
        var error: ConfigurationModel.Issue?
    }

    private static func runSetFlatFieldReference(
        runner: CLIRunner,
        roll: URL,
        frame: URL,
        rig: String?
    ) async -> SetFlatFieldReferenceResult {
        var result = SetFlatFieldReferenceResult()
        var succeeded = false
        do {
            let session = runner.session(
                for: .rollSetFlatFieldReference(roll: roll, frame: frame, rig: rig)
            )
            for await output in try await session.start() {
                switch output {
                case .event(let event):
                    switch event.kind {
                    case .flatFieldReferenceSet: succeeded = true
                    case .error:
                        if let code = event.code, let message = event.message {
                            result.error = ConfigurationModel.Issue(code: code, message: message)
                        }
                    default: break
                    }
                case .completed(let completion):
                    if completion.outcome == .success { succeeded = true }
                case .log, .failure: break
                }
            }
        } catch {
            result.error = ConfigurationModel.Issue(
                code: .internalError, message: error.localizedDescription
            )
            return result
        }
        if succeeded, result.error == nil {
            result.flatField = await fetchFlatField(runner: runner, roll: roll)
        }
        return result
    }

    private static func fetchFlatField(runner: CLIRunner, roll: URL) async -> FlatFieldReference? {
        do {
            for await output in try await runner.session(for: .rollInfo(roll: roll)).start() {
                guard case .event(let event) = output, event.kind == .rollInfo,
                    let fields = event.manifest
                else { continue }
                return RollManifest(fields: fields)?.flatField
            }
        } catch {
            return nil
        }
        return nil
    }

    private static func runSetBaseFrame(
        runner: CLIRunner,
        roll: URL,
        frame: URL
    ) async -> SetBaseFrameResult {
        var result = SetBaseFrameResult()
        var succeeded = false
        do {
            let session = runner.session(
                for: .rollSetBaseFrame(roll: roll, frame: frame)
            )
            for await output in try await session.start() {
                switch output {
                case .event(let event):
                    switch event.kind {
                    case .baseFrameSet: succeeded = true
                    case .error:
                        if let code = event.code, let message = event.message {
                            result.error = ConfigurationModel.Issue(code: code, message: message)
                        }
                    default: break
                    }
                case .completed(let completion):
                    if completion.outcome == .success { succeeded = true }
                case .log, .failure: break
                }
            }
        } catch {
            return result
        }
        if succeeded, result.error == nil {
            result.filmBase = await fetchFilmBase(runner: runner, roll: roll)
        }
        return result
    }

    private static func fetchFilmBase(runner: CLIRunner, roll: URL) async -> FilmBase? {
        do {
            for await output in try await runner.session(for: .rollInfo(roll: roll)).start() {
                guard case .event(let event) = output, event.kind == .rollInfo,
                      let fields = event.manifest
                else { continue }
                return RollManifest(fields: fields)?.filmBase
            }
        } catch {}
        return nil
    }
}
