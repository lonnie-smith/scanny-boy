import CoreGraphics
import Foundation
import ImageIO
import Observation

/// One cell in the check-shot 3×3 focus grid.
struct FocusCheckCell: Sendable, Hashable, Identifiable {
    let id: Int
    let percentage: Double?
}

/// Live view loop, focus meter, and check shot for the Capture tab.
@MainActor
@Observable
final class FocusAssistModel {
    private let camera: any CameraControlling
    private let runner: CLIRunner

    private(set) var isOpen = false
    private(set) var loupeImage: CGImage?
    private(set) var zoomText = ""
    private(set) var zoomHint: String?
    private(set) var areaSize = PTP.LiveViewHeader.SizePair(width: 0, height: 0)
    private(set) var areaCentre = PTP.LiveViewHeader.PointPair(x: 0, y: 0)
    private(set) var meterPercentage: Double?
    private(set) var peakPercentage: Double = 0
    private(set) var meterMessage: String?
    private(set) var liveViewError: String?
    private(set) var canRetryLiveView = false
    private(set) var checkShotCells: [FocusCheckCell] = []
    private(set) var checkShotDiagnosis: String?
    private(set) var isCheckShotBusy = false

    private var frameTask: Task<Void, Never>?
    private var lastJPEG = Data()
    private var recentScores: [Double] = []
    private var peakScore = 0.0
    private var lastAreaKey = ""

    init(camera: any CameraControlling, runner: CLIRunner) {
        self.camera = camera
        self.runner = runner
    }

    var focusAssistAvailable: Bool {
        sequencePhase == .idle || sequencePhase == .paused || sequencePhase == .stopped
    }

    private var sequencePhase: CaptureSequencePhase = .idle

    func updateSequencePhase(_ phase: CaptureSequencePhase) {
        sequencePhase = phase
    }

    func updateConnectionState(_ state: TetherConnectionState) {
        if state == .lost || state == .absent {
            Task { await close() }
        }
    }

    func toggle() {
        if isOpen {
            Task { await close() }
        } else {
            open()
        }
    }

    func open() {
        guard !isOpen, focusAssistAvailable else { return }
        isOpen = true
        liveViewError = nil
        canRetryLiveView = false
        checkShotCells = []
        checkShotDiagnosis = nil
        resetMeterState()
        frameTask = Task { await runFrameLoop() }
    }

    func close() async {
        guard isOpen else { return }
        frameTask?.cancel()
        frameTask = nil
        isOpen = false
        loupeImage = nil
        await camera.endLiveView()
    }

    func retryLiveView() {
        guard isOpen else { return }
        liveViewError = nil
        canRetryLiveView = false
        frameTask?.cancel()
        frameTask = Task { await runFrameLoop() }
    }

    func resetPeak() {
        peakScore = 0
        peakPercentage = 0
        recentScores.removeAll()
    }

    func takeCheckShot(captureFolder: URL?) async {
        guard isOpen, !isCheckShotBusy, let captureFolder else { return }
        isCheckShotBusy = true
        checkShotCells = []
        checkShotDiagnosis = nil
        frameTask?.cancel()
        frameTask = nil
        await camera.endLiveView()

        defer {
            isCheckShotBusy = false
            if isOpen {
                frameTask = Task { await runFrameLoop() }
            }
        }

        do {
            let url = try CaptureNaming.focusCheckURL(in: captureFolder)
            try await camera.drainEvents()
            _ = try await camera.scanBuffer()
            try await camera.release()
            try await camera.waitForExposureEnd()
            let handle = try await camera.waitForFrame(after: [])
            let frame = try await camera.download(handle: handle, to: url)
            try await camera.confirmBufferCleared(handle: frame.handle)
            let log = captureFolder.appendingPathComponent("capture-log.jsonl")
            let session = runner.session(for: .captureAnalyze(frame: url, log: log, baselines: []))
            for await output in try await session.start() {
                guard case .event(let event) = output, event.kind == .frameAnalyzed else { continue }
                applyCheckShot(regions: event.focusRegions ?? [])
            }
        } catch {
            checkShotDiagnosis = error.localizedDescription
        }
    }

    private func runFrameLoop() async {
        do {
            try await camera.startLiveView()
        } catch TetherCaptureError.liveViewRefused(let code) {
            liveViewError =
                "The camera refused live view — close any menu or playback on the camera"
            canRetryLiveView = true
            _ = code
            return
        } catch {
            liveViewError = error.localizedDescription
            canRetryLiveView = true
            return
        }

        while !Task.isCancelled, isOpen {
            do {
                let frame = try await camera.liveViewFrame()
                if frame.jpegData == lastJPEG {
                    try await Task.sleep(for: FocusAssistTuning.liveViewPollInterval)
                    continue
                }
                lastJPEG = frame.jpegData
                let header = frame.header
                areaSize = header.areaSize
                areaCentre = header.areaCentre
                updateZoomText(header: header)
                resetPeakIfAreaChanged(header: header)

                let imageAndScore: (CGImage?, Double?) = await Task.detached {
                    let image = Self.decodeJPEG(frame.jpegData)
                    let score = image.map { FocusScore.score(image: $0) }
                    return (image, score)
                }.value

                loupeImage = imageAndScore.0
                if let score = imageAndScore.1 {
                    updateMeter(score: score)
                }
            } catch {
                if !Task.isCancelled {
                    liveViewError = error.localizedDescription
                    canRetryLiveView = true
                }
                break
            }
        }
    }

    private func resetMeterState() {
        lastJPEG = Data()
        recentScores.removeAll()
        peakScore = 0
        peakPercentage = 0
        meterPercentage = nil
        meterMessage = nil
        lastAreaKey = ""
    }

    private func resetPeakIfAreaChanged(header: PTP.LiveViewHeader) {
        let key = "\(header.areaSize.width),\(header.areaSize.height),"
            + "\(header.areaCentre.x),\(header.areaCentre.y)"
        if key != lastAreaKey {
            lastAreaKey = key
            resetPeak()
        }
    }

    private func updateZoomText(header: PTP.LiveViewHeader) {
        let areaWidth = Int(header.areaSize.width)
        let ratio = Double(header.frameSize.width) / Double(areaWidth)
        zoomText = "\(areaWidth) of \(FocusAssistTuning.sensorWidth) px · "
            + String(format: "%.2f×", ratio)
        if areaWidth == FocusAssistTuning.focusZoomAreaWidth {
            zoomHint = nil
        } else {
            zoomHint = "Magnify 3 steps on the camera for focusing."
        }
    }

    private func updateMeter(score: Double) {
        if score < FocusAssistTuning.scoreMinTexture {
            meterPercentage = nil
            meterMessage = "Not enough detail here"
            return
        }
        meterMessage = nil
        recentScores.append(score)
        if recentScores.count > FocusAssistTuning.scoreMedianFrames {
            recentScores.removeFirst(recentScores.count - FocusAssistTuning.scoreMedianFrames)
        }
        let median = Self.median(recentScores)
        if median > peakScore {
            peakScore = median
        }
        peakPercentage = 100
        meterPercentage = peakScore > 0 ? min(100, median / peakScore * 100) : nil
    }

    private func applyCheckShot(regions: [Double?]) {
        let values = regions.compactMap(\.self)
        guard let best = values.max(), best > 0 else {
            checkShotCells = regions.enumerated().map { FocusCheckCell(id: $0.offset, percentage: nil) }
            return
        }
        checkShotCells = regions.enumerated().map { index, value in
            let percentage = value.map { min(100, $0 / best * 100) }
            return FocusCheckCell(id: index, percentage: percentage)
        }
        checkShotDiagnosis = Self.diagnoseCheckShot(regions: regions, best: best)
    }

    private static func diagnoseCheckShot(regions: [Double?], best: Double) -> String? {
        guard regions.count == 9, best > 0 else { return nil }
        let threshold = best * 0.7
        func regionIsSoft(_ index: Int) -> Bool {
            guard let value = regions[index] else { return true }
            return value < threshold
        }
        for row in 0..<3 where (0..<3).allSatisfy({ regionIsSoft(row * 3 + $0) }) {
            return "A whole row is soft — the film or camera may not be parallel."
        }
        for col in 0..<3 where (0..<3).allSatisfy({ regionIsSoft($0 * 3 + col) }) {
            return "A whole column is soft — the film or camera may not be parallel."
        }
        let corners = [0, 2, 6, 8]
        let lowCorners = corners.filter { corner in
            guard let value = regions[corner] else { return true }
            return value < threshold
        }
        if lowCorners.count == 1 {
            return "One corner is soft — the film may not be flat there."
        }
        return nil
    }

    private static func median(_ values: [Double]) -> Double {
        guard !values.isEmpty else { return 0 }
        let sorted = values.sorted()
        let middle = sorted.count / 2
        if sorted.count.isMultiple(of: 2) {
            return (sorted[middle - 1] + sorted[middle]) / 2
        }
        return sorted[middle]
    }

    nonisolated private static func decodeJPEG(_ jpeg: Data) -> CGImage? {
        guard let source = CGImageSourceCreateWithData(jpeg as CFData, nil) else { return nil }
        return CGImageSourceCreateImageAtIndex(source, 0, nil)
    }
}
