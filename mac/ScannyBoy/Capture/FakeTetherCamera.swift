import Foundation

/// Test double replaying measured Z f behaviours (docs/TETHER_PLAN.md §8.3).
actor FakeTetherCamera: CameraControlling {
    struct Configuration: Sendable {
        var connectionState: TetherConnectionState = .ready
        var exposure: TetherExposureSettings = TetherExposureSettings(
            program: 1, shutter: 50_000, aperture: 280, iso: 400,
            whiteBalance: 2, focusMode: 1
        )
        var initialLeftovers: [BufferLeftover] = []
        var releaseDelay: Duration = .milliseconds(10)
        var exposureEndDelay: Duration = .milliseconds(550)
        var frameArrivalDelay: Duration = .milliseconds(1100)
        var downloadDelay: Duration = .milliseconds(550)
        var nextBufferHandle: UInt32 = TetherTiming.bufferScanFirst
        var dropOnRelease = false
        var failFrameArrival = false
        var bufferSurvivesDownload = false
        var destination: CaptureDestination = .buffer
        var liveViewStartRefused: UInt16?
        var liveViewFrames: [LiveViewFrame] = []
        var liveViewFrameDelay: Duration = .zero
    }

    private(set) var connectionState: TetherConnectionState
    private(set) var exposure: TetherExposureSettings?
    private(set) var leftovers: [BufferLeftover]
    var destination: CaptureDestination

    private var config: Configuration
    private var bufferHandles: Set<UInt32> = []
    private var handlesBeforeLastRelease: Set<UInt32> = []
    private var released = false
    private(set) var liveViewActive = false
    private(set) var endLiveViewCallCount = 0
    private var liveViewFrameIndex = 0

    init(configuration: Configuration = Configuration()) {
        config = configuration
        connectionState = configuration.connectionState
        exposure = configuration.exposure
        leftovers = configuration.initialLeftovers
        destination = configuration.destination
        bufferHandles = Set(configuration.initialLeftovers.map(\.handle))
    }

    func startBrowsing() async {
        connectionState = config.connectionState
        exposure = config.exposure
        leftovers = config.initialLeftovers
        bufferHandles = Set(config.initialLeftovers.map(\.handle))
    }

    func stopBrowsing() async {
        if liveViewActive {
            await endLiveView()
        }
        connectionState = .absent
        exposure = nil
    }

    func drainEvents() async throws {}

    func scanBuffer() async throws -> [UInt32] {
        let handles = bufferHandles.sorted()
        if !leftovers.isEmpty, !handles.isEmpty {
            throw TetherCaptureError.leftoverPresent(leftovers)
        }
        return handles
    }

    func release() async throws {
        guard connectionState == .ready else { throw TetherCaptureError.notConnected }
        if config.dropOnRelease {
            connectionState = .lost
            throw TetherCaptureError.notConnected
        }
        connectionState = .busy
        handlesBeforeLastRelease = bufferHandles
        released = false
        try await Task.sleep(for: config.releaseDelay)
        released = true
    }

    func waitForExposureEnd() async throws {
        try await Task.sleep(for: config.exposureEndDelay)
        connectionState = .ready
    }

    func waitForFrame(after handlesBefore: Set<UInt32>) async throws -> UInt32 {
        if config.failFrameArrival {
            try await Task.sleep(for: TetherTiming.frameArrivalTimeout)
            throw TetherCaptureError.frameArrivalTimeout
        }
        try await Task.sleep(for: config.frameArrivalDelay)
        let handle = config.nextBufferHandle
        config.nextBufferHandle &+= 1
        bufferHandles.insert(handle)
        return handle
    }

    func download(handle: UInt32, to url: URL) async throws -> TetherCapturedFrame {
        try await Task.sleep(for: config.downloadDelay)
        let info = PTP.ObjectInfo(
            handle: handle,
            storageID: 0,
            objectFormat: 0x3801,
            size: 4,
            width: 100,
            height: 100,
            filename: url.lastPathComponent,
            captureDate: "20260911T120000"
        )
        try Data([0, 1, 2, 3]).write(to: url)
        if !config.bufferSurvivesDownload {
            bufferHandles.remove(handle)
        }
        connectionState = .ready
        return TetherCapturedFrame(url: url, handle: handle, objectInfo: info)
    }

    func confirmBufferCleared(handle: UInt32) async throws {
        if bufferHandles.contains(handle) {
            throw TetherCaptureError.bufferNotCleared(handle)
        }
    }

    func discardBufferFrame(handle: UInt32) async throws {
        bufferHandles.remove(handle)
        leftovers.removeAll { $0.handle == handle }
    }

    func startLiveView() async throws {
        guard connectionState == .ready else { throw TetherCaptureError.notConnected }
        if let code = config.liveViewStartRefused {
            throw TetherCaptureError.liveViewRefused(code)
        }
        liveViewActive = true
        liveViewFrameIndex = 0
    }

    func endLiveView() async {
        liveViewActive = false
        endLiveViewCallCount += 1
    }

    func liveViewFrame() async throws -> LiveViewFrame {
        guard liveViewActive else { throw TetherCaptureError.notConnected }
        if config.liveViewFrameDelay > .zero {
            try await Task.sleep(for: config.liveViewFrameDelay)
        }
        guard !config.liveViewFrames.isEmpty else {
            throw TetherCaptureError.liveViewFrameInvalid
        }
        let frame = config.liveViewFrames[liveViewFrameIndex % config.liveViewFrames.count]
        liveViewFrameIndex += 1
        return frame
    }
}

extension PTP.ObjectInfo {
    init(
        handle: UInt32,
        storageID: UInt32,
        objectFormat: UInt16,
        size: UInt32,
        width: UInt32,
        height: UInt32,
        filename: String,
        captureDate: String
    ) {
        self.handle = handle
        self.storageID = storageID
        self.objectFormat = objectFormat
        self.size = size
        self.width = width
        self.height = height
        self.filename = filename
        self.captureDate = captureDate
    }
}
