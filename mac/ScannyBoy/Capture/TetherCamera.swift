import Foundation
import ImageCaptureCore
import os

private let tetherLog = Logger(subsystem: "ScannyBoy", category: "Tether")

/// Actor wrapping `ICDeviceBrowser` and one `ICCameraDevice` for tethered
/// capture over raw PTP (docs/TETHER_PLAN.md §2).
actor TetherCamera: CameraControlling {
    private(set) var connectionState: TetherConnectionState = .absent
    private(set) var exposure: TetherExposureSettings?
    private(set) var leftovers: [BufferLeftover] = []
    var destination: CaptureDestination = .buffer

    private var bridge: TetherCameraBridge?
    private var handlesBeforeRelease: Set<UInt32> = []
    private var releasedAt: ContinuousClock.Instant?
    private var pushedHandles: [UInt32] = []
    private var captureCompleted = false
    private var ignoredHandles: Set<UInt32> = []
    private var lowestStorageID: UInt32?
    private var liveViewActive = false
    private var releaseInFlight = false
    private var connectionHandler: (@Sendable (TetherConnectionState, TetherExposureSettings?) -> Void)?

    init() {}

    func setConnectionHandler(
        _ handler: (@Sendable (TetherConnectionState, TetherExposureSettings?) -> Void)?
    ) {
        connectionHandler = handler
        publish()
    }

    func applyDestination(_ destination: CaptureDestination) async {
        self.destination = destination
    }

    func startBrowsing() async {
        switch connectionState {
        case .ready, .busy, .preparing:
            let bridge = await mainBridge()
            await MainActor.run { bridge.start() }
        default:
            updateState(.searching)
            let bridge = await mainBridge()
            await MainActor.run { bridge.start() }
        }
    }

    func stopBrowsing() async {
        if liveViewActive {
            await endLiveView()
        }
        // Keep the bridge: its browser still knows the camera, so the next
        // Connect reopens the session instead of waiting for re-enumeration.
        if let bridge {
            await bridge.stop()
        }
        exposure = nil
        leftovers = []
        releaseInFlight = false
        updateState(.absent)
    }

    private func updateState(_ state: TetherConnectionState) {
        connectionState = state
        publish()
    }

    private func updateExposure(_ settings: TetherExposureSettings?) {
        exposure = settings
        publish()
    }

    private func publish() {
        connectionHandler?(connectionState, exposure)
    }

    private func mainBridge() async -> TetherCameraBridge {
        if let bridge { return bridge }
        let created = await MainActor.run {
            TetherCameraBridge()
        }
        await MainActor.run {
            created.owner = self
        }
        bridge = created
        return created
    }

    // MARK: - CameraControlling

    func drainEvents() async throws {
        try await drainCheckEvent()
        await drainPushedEvents()
    }

    func scanBuffer() async throws -> [UInt32] {
        var found: [UInt32] = []
        for handle in TetherTiming.bufferScanFirst...TetherTiming.bufferScanLast {
            if try await objectInfo(handle: handle) != nil {
                found.append(handle)
            }
        }
        if !found.isEmpty, connectionState == .ready {
            let infos = try await fetchLeftovers(handles: found)
            leftovers = infos
            throw TetherCaptureError.leftoverPresent(infos)
        }
        return found
    }

    func release() async throws {
        guard connectionState == .ready else { throw TetherCaptureError.notConnected }
        if liveViewActive {
            await endLiveView()
            try await waitForDeviceReady(deadline: .seconds(10))
        }
        updateState(.busy)
        handlesBeforeRelease = Set(try await scanBufferQuiet())
        pushedHandles = []
        captureCompleted = false
        let opcode: UInt16
        let params: [UInt32]
        switch destination {
        case .buffer:
            opcode = PTP.nikonInitiateCaptureRecInSdram
            params = [0xFFFF_FFFF]
        case .card:
            opcode = PTP.nikonInitiateCaptureRecInMedia
            params = [0xFFFF_FFFF, 0]
        }
        do {
            releasedAt = ContinuousClock.now
            let response = try await sendPTP(opcode, params: params)
            guard PTP.responseCode(response) == PTP.responseOK else {
                updateState(.ready)
                throw TetherCaptureError.releaseFailed(
                    PTP.describeResponse(PTP.responseCode(response))
                )
            }
            releaseInFlight = true
            await setBridgeReleaseInFlight(true)
        } catch {
            updateState(.ready)
            throw error
        }
    }

    func waitForExposureEnd() async throws {
        defer {
            releaseInFlight = false
            updateState(.ready)
            Task { await setBridgeReleaseInFlight(false) }
        }
        guard let shutter = exposure?.shutter else {
            try await sleep(TetherTiming.readyPollInterval)
            return
        }
        let released = releasedAt ?? ContinuousClock.now
        let deadline = released + TetherTiming.exposureTimeout(shutterPTP: shutter)
        let exposureEnds = released + TetherTiming.exposureDuration(shutterPTP: shutter)
        var sawBusy = false
        while ContinuousClock.now < deadline {
            let response = try await sendPTP(PTP.nikonDeviceReady)
            let code = PTP.responseCode(response)
            if code == PTP.responseDeviceBusy { sawBusy = true }
            // A short exposure can finish before the first poll, so the camera
            // never answers busy; OK once the exposure has elapsed counts too.
            if code == PTP.responseOK, sawBusy || ContinuousClock.now >= exposureEnds {
                return
            }
            try await sleep(TetherTiming.readyPollInterval)
        }
        throw TetherCaptureError.exposureTimeout
    }

    func waitForFrame(after handlesBefore: Set<UInt32>) async throws -> UInt32 {
        let deadline = ContinuousClock.now + TetherTiming.frameArrivalTimeout
        while ContinuousClock.now < deadline {
            _ = try await drainCheckEvent()
            let current = Set(try await scanBufferQuiet())
            if let fresh = current.subtracting(handlesBefore).sorted().first {
                return fresh
            }
            if destination == .card {
                let pushed = pushedHandles.filter { !ignoredHandles.contains($0) }
                if let handle = pushed.first {
                    return handle
                }
                if captureCompleted, let handle = pushedHandles.first {
                    return handle
                }
            }
            try await sleep(TetherTiming.framePollInterval)
        }
        throw TetherCaptureError.frameArrivalTimeout
    }

    func download(handle: UInt32, to url: URL) async throws -> TetherCapturedFrame {
        updateState(.busy)
        defer { updateState(.ready) }
        guard let info = try await objectInfo(handle: handle) else {
            throw TetherCaptureError.downloadFailed("object \(handle) not found")
        }
        let partial = url.deletingPathExtension().appendingPathExtension("NEF.partial")
        let (dataPhase, response) = try await sendPTPPair(PTP.getObject, params: [handle])
        let bytes = PTP.objectPayload(
            dataPhase: dataPhase, response: response, expectedSize: info.size
        )
        guard bytes.count == Int(info.size) else {
            if PTP.responseCode(response) != PTP.responseOK {
                throw TetherCaptureError.downloadFailed(
                    PTP.describeResponse(PTP.responseCode(response))
                )
            }
            throw TetherCaptureError.downloadFailed(
                "size mismatch: expected \(info.size), got \(bytes.count)"
            )
        }
        try Data(bytes).write(to: partial, options: .atomic)
        try sync(partial)
        if FileManager.default.fileExists(atPath: url.path) {
            try FileManager.default.removeItem(at: url)
        }
        try FileManager.default.moveItem(at: partial, to: url)
        leftovers.removeAll { $0.handle == handle }
        return TetherCapturedFrame(url: url, handle: handle, objectInfo: info)
    }

    func confirmBufferCleared(handle: UInt32) async throws {
        if try await objectInfo(handle: handle) != nil {
            throw TetherCaptureError.bufferNotCleared(handle)
        }
    }

    func discardBufferFrame(handle: UInt32) async throws {
        let response = try await sendPTP(PTP.nikonDelImageSDRAM, params: [handle])
        guard PTP.responseCode(response) == PTP.responseOK else {
            throw TetherCaptureError.releaseFailed(
                PTP.describeResponse(PTP.responseCode(response))
            )
        }
        leftovers.removeAll { $0.handle == handle }
    }

    func startLiveView() async throws {
        guard connectionState == .ready else { throw TetherCaptureError.notConnected }
        let response = try await sendPTP(PTP.nikonStartLiveView)
        let code = PTP.responseCode(response)
        guard code == PTP.responseOK else {
            throw TetherCaptureError.liveViewRefused(code ?? 0)
        }
        let deadline = ContinuousClock.now + .seconds(10)
        while ContinuousClock.now < deadline {
            let ready = try await sendPTP(PTP.nikonDeviceReady)
            if PTP.responseCode(ready) == PTP.responseOK {
                liveViewActive = true
                return
            }
            try await sleep(TetherTiming.readyPollInterval)
        }
        throw TetherCaptureError.liveViewRefused(PTP.responseDeviceBusy)
    }

    func endLiveView() async {
        guard liveViewActive else { return }
        liveViewActive = false
        _ = try? await sendPTP(PTP.nikonEndLiveView)
    }

    func liveViewFrame() async throws -> LiveViewFrame {
        guard liveViewActive else { throw TetherCaptureError.notConnected }
        for attempt in 0..<FocusAssistTuning.liveViewBusyRetries {
            let (data, response) = try await sendPTPPair(PTP.nikonGetLiveViewImage)
            let code = PTP.responseCode(response)
            if code == PTP.responseDeviceBusy, attempt + 1 < FocusAssistTuning.liveViewBusyRetries {
                try await sleep(TetherTiming.readyPollInterval)
                continue
            }
            guard code == PTP.responseOK else {
                throw TetherCaptureError.liveViewRefused(code ?? 0)
            }
            let bytes = PTP.payload(data)
            guard let decoded = PTP.LiveViewHeader.decode(bytes) else {
                throw TetherCaptureError.liveViewFrameInvalid
            }
            return LiveViewFrame(header: decoded.header, jpegData: decoded.jpeg)
        }
        throw TetherCaptureError.liveViewRefused(PTP.responseDeviceBusy)
    }

    // MARK: - Bridge callbacks

    func bridgeDidUpdate(state: TetherConnectionState) {
        updateState(state)
    }

    func bridgeDidReadExposure(_ settings: TetherExposureSettings) {
        updateExposure(settings)
    }

    func bridgeDidReceivePushedEvent(code: UInt16, params: [UInt32]) {
        switch code {
        case 0x4002:
            if let handle = params.first, !ignoredHandles.contains(handle),
               !pushedHandles.contains(handle)
            {
                pushedHandles.append(handle)
            }
        case 0x400D, 0xC102:
            captureCompleted = true
        case 0x4006:
            if let property = params.first, property == 0xD10B { return }
            // Exposure watch pause is T-7; ignore here for partial chunk.
        default:
            break
        }
    }

    func bridgeDeviceLost() {
        if liveViewActive {
            liveViewActive = false
        }
        updateState(.lost)
    }

    // MARK: - PTP helpers

    private func sendPTP(_ opcode: UInt16, params: [UInt32] = []) async throws -> Data {
        let bridge = await mainBridge()
        return try await bridge.sendPTP(opcode, params: params)
    }

    private func sendPTPPair(_ opcode: UInt16, params: [UInt32] = []) async throws -> (Data, Data) {
        let bridge = await mainBridge()
        return try await bridge.sendPTPPair(opcode, params: params)
    }

    private func waitForDeviceReady(deadline: Duration) async throws {
        let end = ContinuousClock.now + deadline
        while ContinuousClock.now < end {
            let response = try await sendPTP(PTP.nikonDeviceReady)
            if PTP.responseCode(response) == PTP.responseOK { return }
            try await sleep(TetherTiming.readyPollInterval)
        }
        throw TetherCaptureError.liveViewRefused(PTP.responseDeviceBusy)
    }

    private func setBridgeReleaseInFlight(_ inFlight: Bool) async {
        let bridge = await mainBridge()
        await MainActor.run { bridge.releaseInFlight = inFlight }
    }

    private func objectInfo(handle: UInt32) async throws -> PTP.ObjectInfo? {
        let bridge = await mainBridge()
        let (data, response) = try await bridge.sendPTPPair(PTP.getObjectInfo, params: [handle])
        guard PTP.responseCode(response) == PTP.responseOK else { return nil }
        return PTP.ObjectInfo(handle: handle, payload: data)
    }

    private func scanBufferQuiet() async throws -> [UInt32] {
        var found: [UInt32] = []
        for handle in TetherTiming.bufferScanFirst...TetherTiming.bufferScanLast {
            if try await objectInfo(handle: handle) != nil {
                found.append(handle)
            }
        }
        return found
    }

    private func fetchLeftovers(handles: [UInt32]) async throws -> [BufferLeftover] {
        var result: [BufferLeftover] = []
        for handle in handles {
            if let info = try await objectInfo(handle: handle) {
                result.append(BufferLeftover(handle: handle, objectInfo: info))
            }
        }
        return result
    }

    @discardableResult
    private func drainCheckEvent() async throws -> [PTP.QueuedEvent] {
        var drained: [PTP.QueuedEvent] = []
        for _ in 0..<10 {
            let bridge = await mainBridge()
            let (data, response) = try await bridge.sendPTPPair(PTP.nikonCheckEvent)
            guard PTP.responseCode(response) == PTP.responseOK else { break }
            let events = PTP.decodeCheckEvents(data)
            guard !events.isEmpty else { break }
            drained.append(contentsOf: events)
            for event in events {
                tetherLog.debug(
                    "event \(String(format: "0x%04x param 0x%08x", event.code, event.param), privacy: .public)"
                )
                if event.code == 0x4002 || event.code == 0xC101 {
                    ignoredHandles.insert(event.param)
                }
            }
        }
        return drained
    }

    private func drainPushedEvents() async {
        // Pushed events arrive through `ptpEventHandler`; nothing to poll.
    }

    private func sleep(_ duration: Duration) async throws {
        try await Task.sleep(for: duration)
    }

    private func sync(_ url: URL) throws {
        let fd = open(url.path, O_RDONLY)
        guard fd >= 0 else { return }
        defer { close(fd) }
        fsync(fd)
    }

    fileprivate func finishConnect(deviceInfo: PTP.DeviceInfo) async throws {
        if destination == .buffer, !deviceInfo.supportsBufferCapture() {
            updateState(.unsupported)
            return
        }
        if destination == .card, !deviceInfo.supportsCardCapture() {
            updateState(.unsupported)
            return
        }
        _ = try await drainCheckEvent()
        let buffered = try await scanBufferQuiet()
        if !buffered.isEmpty {
            leftovers = try await fetchLeftovers(handles: buffered)
        }
        exposure = try await readExposure()
        updateState(.ready)
    }

    private func readExposure() async throws -> TetherExposureSettings {
        func prop(_ code: UInt16) async throws -> UInt32 {
            let bridge = await mainBridge()
            let (data, response) = try await bridge.sendPTPPair(
                PTP.getDevicePropValue, params: [UInt32(code)]
            )
            guard PTP.responseCode(response) == PTP.responseOK else { return 0 }
            let bytes = PTP.payload(data).prefix(4)
            return bytes.enumerated().reduce(UInt32(0)) {
                $0 | UInt32($1.element) << (8 * $1.offset)
            }
        }
        return TetherExposureSettings(
            program: try await prop(0x500E),
            shutter: try await prop(0x500D),
            aperture: try await prop(0x5007),
            iso: try await prop(0x500F),
            whiteBalance: try await prop(0x5005),
            focusMode: try await prop(0x500A)
        )
    }
}

// MARK: - ImageCaptureCore bridge

/// ImageCaptureCore requires NSObject delegates on the main thread.
///
/// One bridge lives for the app's lifetime. Disconnect closes the session but
/// keeps the browser and the last device it reported, so Connect reopens the
/// session directly.
final class TetherCameraBridge: NSObject, @unchecked Sendable {
    weak var owner: TetherCamera?
    private let browser = ICDeviceBrowser()
    /// The camera whose session is open or opening.
    private var camera: ICCameraDevice?
    /// The last camera the browser reported, kept across Disconnect.
    private var knownDevice: ICCameraDevice?
    private var browsing = false
    private var wantsSession = false
    private var ptpPairContinuations: [UInt32: CheckedContinuation<(Data, Data), Error>] = [:]
    private var nextTransaction: UInt32 = 1
    private var closeContinuation: CheckedContinuation<Void, Never>?
    private var closeGeneration = 0
    var releaseInFlight = false

    override init() {
        super.init()
        browser.delegate = self
    }

    func start() {
        wantsSession = true
        if !browsing {
            browser.browsedDeviceTypeMask = ICDeviceTypeMask(
                rawValue: ICDeviceTypeMask.camera.rawValue
                    | ICDeviceLocationTypeMask.local.rawValue
            )!
            browser.start()
            browsing = true
        }
        if camera == nil, let knownDevice {
            tetherLog.info("reopening session on known device")
            adopt(knownDevice)
        }
    }

    @MainActor
    func stop() async {
        wantsSession = false
        cancelPendingPTPCommands(throwing: .notConnected)
        guard let activeCamera = camera else { return }
        activeCamera.ptpEventHandler = { _ in }
        closeGeneration += 1
        let generation = closeGeneration
        tetherLog.info("closing session")
        await withCheckedContinuation { continuation in
            closeContinuation = continuation
            activeCamera.requestCloseSession()
            Task { @MainActor in
                try? await Task.sleep(for: TetherTiming.closeSessionTimeout)
                guard self.closeGeneration == generation, self.closeContinuation != nil else { return }
                tetherLog.error("didCloseSession did not arrive; tearing down anyway")
                self.finishClose()
            }
        }
    }

    private func finishClose() {
        guard let continuation = closeContinuation else { return }
        closeContinuation = nil
        detachCamera()
        continuation.resume()
    }

    private func detachCamera() {
        camera?.ptpEventHandler = { _ in }
        camera = nil
    }

    private func cancelPendingPTPCommands(throwing error: TetherCaptureError) {
        let waiting = ptpPairContinuations
        ptpPairContinuations.removeAll()
        for (_, continuation) in waiting {
            continuation.resume(throwing: error)
        }
    }

    /// The device vanished or ImageCaptureCore closed the session on its own.
    /// Fail in-flight commands now instead of leaving them to complete empty.
    private func cameraGone(_ reason: String) {
        tetherLog.error(
            "camera gone (\(reason, privacy: .public)); \(self.ptpPairContinuations.count) commands pending"
        )
        detachCamera()
        cancelPendingPTPCommands(throwing: .connectionDropped)
        guard wantsSession else { return }
        Task { await owner?.bridgeDeviceLost() }
    }

    @MainActor
    func sendPTP(_ opcode: UInt16, params: [UInt32] = [], expectData: Bool = false) async throws -> Data {
        if expectData {
            let (data, _) = try await sendPTPPair(opcode, params: params)
            return data
        }
        let (_, response) = try await sendPTPPair(opcode, params: params)
        return response
    }

    @MainActor
    func sendPTPPair(_ opcode: UInt16, params: [UInt32] = []) async throws -> (Data, Data) {
        guard let camera else {
            if releaseInFlight {
                throw TetherCaptureError.sessionDroppedAfterShutter
            }
            throw TetherCaptureError.notConnected
        }
        let transaction = nextTransaction
        nextTransaction &+= 1
        let started = ContinuousClock.now
        return try await withCheckedThrowingContinuation { continuation in
            ptpPairContinuations[transaction] = continuation
            // ImageCaptureCore calls this off the main thread. Keep it
            // @Sendable (not @MainActor) so Swift does not synchronously hop
            // back to main while `requestSendPTPCommand` is still on the stack.
            // Copy the buffers here — a deferred MainActor hop can arrive after
            // IC has released a multi-megabyte GetObject wrapper.
            let finish: @Sendable (Data, Data, (any Error)?) -> Void = { [weak self] data, response, error in
                let copiedData = Data(data)
                let copiedResponse = Data(response)
                Task { @MainActor in
                    let ordered = PTP.orderedCommandResult(
                        data: copiedData, response: copiedResponse
                    )
                    let line = String(
                        format: "PTP 0x%04x #%u: %@, data %d B, response %@",
                        opcode, transaction, "\(ContinuousClock.now - started)",
                        ordered.0.count, PTP.describeResponse(PTP.responseCode(ordered.1))
                    )
                    tetherLog.debug("\(line, privacy: .public) \(error?.localizedDescription ?? "", privacy: .public)")
                    // Route by our own transaction: the response's first
                    // parameter is a command result, not a transaction ID.
                    guard let self,
                          let waiting = self.ptpPairContinuations.removeValue(forKey: transaction)
                    else { return }
                    if let error {
                        waiting.resume(throwing: error)
                    } else if copiedData.isEmpty, copiedResponse.isEmpty {
                        // No response container at all: the session went away
                        // mid-command (didRemove may not have arrived yet).
                        waiting.resume(throwing: TetherCaptureError.connectionDropped)
                    } else {
                        waiting.resume(returning: ordered)
                    }
                }
            }
            camera.requestSendPTPCommand(
                PTP.command(opcode, params: params, transaction: transaction),
                outData: nil,
                completion: finish
            )
        }
    }

    private func adopt(_ device: ICCameraDevice) {
        guard wantsSession, camera == nil else { return }
        if device.transportType == ICDeviceTransport.transportTypeMassStorage.rawValue {
            Task { await owner?.bridgeDidUpdate(state: .massStorage) }
            return
        }
        camera = device
        let cameraOwner = owner
        device.ptpEventHandler = { data in
            guard let event = PTP.decodeEvent(data) else { return }
            Task { await cameraOwner?.bridgeDidReceivePushedEvent(code: event.code, params: event.params) }
        }
        device.delegate = self
        tetherLog.info("opening session")
        Task { await owner?.bridgeDidUpdate(state: .preparing) }
        device.requestOpenSession()
    }
}

extension TetherCameraBridge: ICDeviceBrowserDelegate {
    func deviceBrowser(_: ICDeviceBrowser, didAdd device: ICDevice, moreComing _: Bool) {
        guard let found = device as? ICCameraDevice else { return }
        tetherLog.info("browser added camera")
        knownDevice = found
        adopt(found)
    }

    func deviceBrowser(_: ICDeviceBrowser, didRemove device: ICDevice, moreGoing _: Bool) {
        tetherLog.info("browser removed device")
        if device === knownDevice {
            knownDevice = nil
        }
        guard device === camera else { return }
        if closeContinuation != nil {
            finishClose()
        } else {
            cameraGone("browser removed device")
        }
    }
}

extension TetherCameraBridge: ICCameraDeviceDelegate {
    func didRemove(_: ICDevice) {
        tetherLog.info("device didRemove")
    }

    func device(_: ICDevice, didOpenSessionWithError error: Error?) {
        if let error {
            tetherLog.error("open session failed: \(error.localizedDescription, privacy: .public)")
            // Drop the device so Retry adopts it again.
            detachCamera()
            Task { await owner?.bridgeDidUpdate(state: .unavailable) }
            return
        }
        tetherLog.info("session open")
        Task { await connectAfterOpen() }
    }

    func device(_ device: ICDevice, didCloseSessionWithError error: Error?) {
        tetherLog.info("session closed: \(error?.localizedDescription ?? "no error", privacy: .public)")
        if closeContinuation != nil {
            finishClose()
        } else if device === camera {
            cameraGone("session closed by ImageCaptureCore")
        }
    }

    func deviceDidBecomeReady(withCompleteContentCatalog _: ICCameraDevice) {
        tetherLog.info("content catalog complete")
    }

    func cameraDevice(_: ICCameraDevice, didAdd _: [ICCameraItem]) {}

    func cameraDevice(_: ICCameraDevice, didRemove _: [ICCameraItem]) {}

    func cameraDevice(_: ICCameraDevice, didRenameItems _: [ICCameraItem]) {}

    func cameraDevice(_: ICCameraDevice, didCompleteDeleteFilesWithError _: Error?) {}

    func cameraDeviceDidChangeCapability(_: ICCameraDevice) {}

    func cameraDevice(
        _: ICCameraDevice, didReceiveThumbnail _: CGImage?, for _: ICCameraItem, error _: Error?
    ) {}

    func cameraDevice(
        _: ICCameraDevice, didReceiveMetadata _: [AnyHashable: Any]?,
        for _: ICCameraItem, error _: Error?
    ) {}

    func cameraDevice(_: ICCameraDevice, didReceivePTPEvent _: Data) {}

    func cameraDeviceDidRemoveAccessRestriction(_: ICDevice) {}

    func cameraDeviceDidEnableAccessRestriction(_: ICDevice) {}

    @MainActor
    private func connectAfterOpen() async {
        guard let owner else { return }
        let started = ContinuousClock.now
        do {
            let (data, response) = try await sendPTPPair(PTP.getDeviceInfo)
            tetherLog.info("first PTP reply after \(ContinuousClock.now - started, privacy: .public)")
            guard PTP.responseCode(response) == PTP.responseOK,
                  let info = PTP.DeviceInfo(payload: data)
            else {
                await owner.bridgeDidUpdate(state: .unsupported)
                return
            }
            try await owner.finishConnect(deviceInfo: info)
            tetherLog.info("ready after \(ContinuousClock.now - started, privacy: .public)")
        } catch {
            tetherLog.error("connect failed: \(error.localizedDescription, privacy: .public)")
            await owner.bridgeDidUpdate(state: .unavailable)
        }
    }
}
