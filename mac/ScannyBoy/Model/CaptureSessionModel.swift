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

    var isFilled: Bool {
        if case .filled = self { true } else { false }
    }
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

    /// A frame found in the camera buffer before a release (§2.5). It is
    /// downloaded into `_unclaimed/` as soon as it is found — the download is
    /// what yields its embedded preview, and it clears the frame from the
    /// camera — so nothing is lost while the operator decides.
    struct LeftoverFrame: Identifiable, Sendable {
        let id: UUID
        let url: URL
        let capturedAt: Date?
        let preview: Thumbnail?
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
    var gridProfileID: String? {
        didSet {
            guard gridProfileID != oldValue else { return }
            if let gridProfileID {
                defaults.set(gridProfileID, forKey: ConfigurationModel.lastGridProfileKey)
            } else {
                defaults.removeObject(forKey: ConfigurationModel.lastGridProfileKey)
            }
        }
    }
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
    private(set) var leftoverFrames: [LeftoverFrame] = []
    private(set) var leftoverError: String?
    private(set) var isClaimingLeftovers = false
    private var leftoverClaim: Task<Void, Never>?
    /// The open negative's first release, kept across pause and resume so
    /// every cell of the negative shares one file stamp.
    private var negativeFirstRelease: Date?
    private var sequenceLoopActive = false

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
        gridProfileID = defaults.string(forKey: ConfigurationModel.lastGridProfileKey)
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
            && !hasUnresolvedLeftovers
    }

    /// A leftover is being downloaded or is waiting for the operator's choice.
    var hasUnresolvedLeftovers: Bool {
        isClaimingLeftovers || !leftoverFrames.isEmpty
    }

    /// First unmet prerequisite blocking start from idle, for button help and hints.
    var startBlockedReason: String? {
        guard sequencePhase == .idle else { return nil }
        if connectionState != .ready {
            return "Connect the camera before capturing."
        }
        if hasUnresolvedLeftovers {
            return Self.leftoverBlockedReason
        }
        if exposure?.isManualProgram == false {
            return "Switch the camera to Manual exposure before capturing."
        }
        if exposure?.isManualFocus == false {
            return "Switch the camera to manual focus before capturing."
        }
        if perNegative == nil {
            return "Choose a grid before capturing."
        }
        if rollURL == nil {
            return "Select a roll before capturing."
        }
        if flatField == nil {
            return "Capture a bare-light reference before capturing."
        }
        if filmKind == nil {
            return "Choose a film type in Setup before capturing."
        }
        if filmBase == nil {
            return "Capture a base frame before capturing."
        }
        return nil
    }

    /// Whether the play/pause button and Space should be active.
    var canToggleSequence: Bool {
        if focusAssist.isOpen { return false }
        switch sequencePhase {
        case .running:
            return true
        case .paused:
            return !hasUnresolvedLeftovers
        case .idle:
            return runEnabled
        case .waitingForDownload:
            return false
        }
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
                    if state == .ready {
                        self.openSession()
                    } else if self.isSessionOpen, state == .lost || state == .absent {
                        self.closeSession()
                    }
                }
            }
            connectionHandlerInstalled = true
        }
        await camera.applyDestination(destination)
        await camera.startBrowsing()
    }

    func disconnect() async {
        closeSession()
        await camera.stopBrowsing()
    }

    func refreshConnection() async {
        await connect()
    }

    func openSession() {
        guard !isSessionOpen else { return }
        isSessionOpen = true
        resetCells()
        claimLeftovers()
    }

    func closeSession() {
        guard isSessionOpen else { return }
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
        negativeFirstRelease = nil
        sequenceTask = Task {
            await focusAssist.close()
            await runSequence()
        }
    }

    private func runSequence() async {
        guard let folder = captureFolder else { return }
        sequenceLoopActive = true
        defer { sequenceLoopActive = false }
        await leftoverClaim?.value
        let firstRelease = negativeFirstRelease ?? clock.now()
        negativeFirstRelease = firstRelease
        var handlesBefore = Set<UInt32>()
        do {
            try await camera.drainEvents()
            handlesBefore = Set(try await camera.scanBuffer())
        } catch TetherCaptureError.leftoverPresent {
            sequencePhase = .paused
            focusAssist.updateSequencePhase(sequencePhase)
            claimLeftovers()
            return
        } catch {
            markNextFailed(error.localizedDescription)
            return
        }

        // A resumed negative shoots only the cells it still needs.
        let pending = cellStates.indices.filter { !cellStates[$0].isFilled }
        for (position, cellIndex) in pending.enumerated() {
            // Pause takes effect between shots; the in-flight one finishes.
            guard !Task.isCancelled, sequencePhase == .running else { return }

            let shot = cellIndex + 1

            do {
                if cellIndex == 0, case .next = cellStates[0] {
                    let intervalEnd = clock.now().addingTimeInterval(TimeInterval(intervalSeconds))
                    try await waitForInterval(end: intervalEnd)
                    guard sequencePhase == .running else { return }
                    playHoldCue(at: intervalEnd.addingTimeInterval(-TetherTiming.holdCueLead))
                }

                cellStates[cellIndex] = .exposing
                markNext(after: cellIndex)
                try await camera.release()
                playMoveCue()
                try await camera.waitForExposureEnd()

                if position + 1 < pending.count {
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
                // Not added to handlesBefore: the download cleared the handle,
                // and the Z f reuses the lowest free one for the next frame.
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
        negativeFirstRelease = nil
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
        guard sequencePhase == .paused, !hasUnresolvedLeftovers else { return }
        sequencePhase = .running
        focusAssist.updateSequencePhase(sequencePhase)
        pausedAfterCell = false
        // Paused before its in-flight shot finished: that loop carries on.
        guard !sequenceLoopActive else { return }
        sequenceTask = Task { await runSequence() }
    }

    private func stopNegative() {
        sequenceTask?.cancel()
        negativeFirstRelease = nil
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
        await leftoverClaim?.value
        try await camera.drainEvents()
        let handlesBefore: Set<UInt32>
        do {
            handlesBefore = Set(try await camera.scanBuffer())
        } catch TetherCaptureError.leftoverPresent {
            // A leftover must not block a shot the operator just asked for.
            // Save it to `_unclaimed/` for them to decide on, then go ahead.
            claimLeftovers()
            await leftoverClaim?.value
            handlesBefore = Set(try await camera.scanBuffer())
        }
        try await camera.release()
        try await camera.waitForExposureEnd()
        let handle = try await camera.waitForFrame(after: handlesBefore)
        let frame = try await camera.download(handle: handle, to: url)
        try await camera.confirmBufferCleared(handle: frame.handle)
    }

    // MARK: - Leftovers (§2.5)

    static let leftoverBlockedReason =
        "Choose what to do with the frame left in the camera before capturing."

    /// Waits for the buffer scan and any leftover download in progress.
    func waitForLeftoverClaim() async {
        await leftoverClaim?.value
    }

    /// The one cell an open negative still needs, when there is exactly one.
    var leftoverTargetCell: Int? {
        guard sequencePhase == .paused, !sequenceLoopActive, negativeFirstRelease != nil else {
            return nil
        }
        let unfilled = cellStates.indices.filter { !cellStates[$0].isFilled }
        return unfilled.count == 1 ? unfilled[0] : nil
    }

    /// Scans the buffer and downloads every leftover into `_unclaimed/`.
    /// One claim runs at a time; releases wait for it.
    private func claimLeftovers() {
        guard leftoverClaim == nil else { return }
        leftoverError = nil
        leftoverClaim = Task {
            defer {
                isClaimingLeftovers = false
                leftoverClaim = nil
            }
            do {
                try await camera.drainEvents()
                _ = try await camera.scanBuffer()
            } catch TetherCaptureError.leftoverPresent(let found) {
                // Only a real leftover blocks capture; an empty scan must not.
                isClaimingLeftovers = true
                await saveToUnclaimed(found)
            } catch {
                leftoverError = error.localizedDescription
            }
        }
    }

    private func saveToUnclaimed(_ found: [BufferLeftover]) async {
        guard let captureFolder else {
            leftoverError = "A frame is still in the camera buffer. Select a roll so it can be saved."
            return
        }
        let scale = NSScreen.main?.backingScaleFactor ?? 2
        for leftover in found {
            let capturedAt = leftover.objectInfo.capturedAt
            do {
                let url = try CaptureNaming.unclaimedURL(
                    in: captureFolder, capturedAt: capturedAt ?? clock.now()
                )
                let frame = try await camera.download(handle: leftover.handle, to: url)
                let preview = await Task.detached {
                    ThumbnailLoader.embeddedPreview(
                        url: url, pointSize: CGSize(width: 120, height: 90), scale: scale
                    )
                }.value
                leftoverFrames.append(
                    LeftoverFrame(id: UUID(), url: url, capturedAt: capturedAt, preview: preview)
                )
                try await camera.confirmBufferCleared(handle: frame.handle)
            } catch {
                leftoverError = error.localizedDescription
                return
            }
        }
    }

    /// Fills the open negative's one remaining cell with the leftover.
    func useLeftover(_ id: LeftoverFrame.ID) {
        guard let cell = leftoverTargetCell, let firstRelease = negativeFirstRelease,
              let folder = captureFolder,
              let index = leftoverFrames.firstIndex(where: { $0.id == id })
        else { return }
        do {
            let url = try CaptureNaming.exclusiveURL(
                in: folder, firstRelease: firstRelease, shotNumber: cell + 1
            )
            try FileManager.default.moveItem(at: leftoverFrames[index].url, to: url)
            leftoverFrames.remove(at: index)
            cellStates[cell] = .filled(url)
            analyzeFrame(url, cellIndex: cell)
            finishNegative(firstRelease: firstRelease)
        } catch {
            leftoverError = error.localizedDescription
        }
    }

    /// Leaves the leftover in `_unclaimed/`, attached to nothing.
    func keepLeftover(_ id: LeftoverFrame.ID) {
        leftoverFrames.removeAll { $0.id == id }
    }

    /// Deletes the downloaded leftover — the operator's choice, never the app's.
    func discardLeftover(_ id: LeftoverFrame.ID) {
        guard let frame = leftoverFrames.first(where: { $0.id == id }) else { return }
        do {
            try FileManager.default.removeItem(at: frame.url)
            leftoverFrames.removeAll { $0.id == id }
        } catch {
            leftoverError = error.localizedDescription
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

    /// Applies one saved grid preset when the sequence is idle.
    func selectGridProfile(_ profile: GridProfile) {
        guard sequencePhase == .idle else { return }
        gridProfileID = profile.profileID
        applyGridDimensions(from: profile)
    }

    /// Clears the grid choice and empties the mini-view when idle.
    func clearGridSelection() {
        guard sequencePhase == .idle else { return }
        gridProfileID = nil
        across = nil
        resetCells()
    }

    /// Whether there is unpublished capture work to discard.
    func hasUnpublishedCaptureWork(publishedNegativeIDs: Set<UUID>) -> Bool {
        if cellStates.contains(where: \.isFilled) { return true }
        if completedNegatives.contains(where: { !publishedNegativeIDs.contains($0.id) }) {
            return true
        }
        return false
    }

    /// Clears in-progress and unpublished session state. Returns NEF URLs from
    /// filled cells and unpublished completed negatives for the caller to recycle.
    func discardUnpublishedCaptures(publishedNegativeIDs: Set<UUID>) -> [URL] {
        guard sequencePhase == .idle, !hasUnresolvedLeftovers else { return [] }
        var urls: [URL] = []
        for state in cellStates {
            if case .filled(let url) = state { urls.append(url) }
        }
        for negative in completedNegatives where !publishedNegativeIDs.contains(negative.id) {
            urls.append(contentsOf: negative.frameURLs)
        }
        completedNegatives.removeAll { !publishedNegativeIDs.contains($0.id) }
        baselineFrameURLs = completedNegatives.first?.frameURLs ?? []
        currentNegativeIndex = completedNegatives.count
        negativeFirstRelease = nil
        countdownText = ""
        resetCells()
        return urls
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
