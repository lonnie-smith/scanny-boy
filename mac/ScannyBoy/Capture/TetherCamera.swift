import Foundation
import ImageCaptureCore

/// Actor wrapping `ICDeviceBrowser` and one `ICCameraDevice` for tethered
/// capture over raw PTP (docs/TETHER_PLAN.md §2).
actor TetherCamera: CameraControlling {
    private(set) var connectionState: TetherConnectionState = .absent
    private(set) var exposure: TetherExposureSettings?
    private(set) var leftovers: [BufferLeftover] = []
    var destination: CaptureDestination = .buffer

    private var bridge: TetherCameraBridge?
    private var handlesBeforeRelease: Set<UInt32> = []
    private var pushedHandles: [UInt32] = []
    private var captureCompleted = false
    private var ignoredHandles: Set<UInt32> = []
    private var lowestStorageID: UInt32?
    private var liveViewActive = false
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
        let activeBridge = bridge
        await MainActor.run { activeBridge?.stop() }
        bridge = nil
        exposure = nil
        leftovers = []
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
        let response = try await sendPTP(opcode, params: params)
        guard PTP.responseCode(response) == PTP.responseOK else {
            updateState(.ready)
            throw TetherCaptureError.releaseFailed(PTP.describeResponse(PTP.responseCode(response)))
        }
    }

    func waitForExposureEnd() async throws {
        guard let shutter = exposure?.shutter else {
            try await sleep(TetherTiming.readyPollInterval)
            updateState(.ready)
            return
        }
        let deadline = ContinuousClock.now + TetherTiming.exposureTimeout(shutterPTP: shutter)
        var sawBusy = false
        while ContinuousClock.now < deadline {
            let response = try await sendPTP(PTP.nikonDeviceReady)
            let code = PTP.responseCode(response)
            if code == PTP.responseDeviceBusy { sawBusy = true }
            if sawBusy, code == PTP.responseOK {
                updateState(.ready)
                return
            }
            try await sleep(TetherTiming.readyPollInterval)
        }
        updateState(.ready)
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
        let objectData = try await getObject(handle: handle)
        let bytes = PTP.payload(objectData)
        guard bytes.count == Int(info.size) else {
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

    private func getObject(handle: UInt32) async throws -> Data {
        let bridge = await mainBridge()
        return try await bridge.sendPTP(PTP.getObject, params: [handle], expectData: true)
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
final class TetherCameraBridge: NSObject, @unchecked Sendable {
    weak var owner: TetherCamera?
    private let browser = ICDeviceBrowser()
    private var camera: ICCameraDevice?
    private var ptpPairContinuations: [UInt32: CheckedContinuation<(Data, Data), Error>] = [:]
    private var nextTransaction: UInt32 = 1

    override init() {
        super.init()
        browser.delegate = self
    }

    func start() {
        browser.browsedDeviceTypeMask = ICDeviceTypeMask(
            rawValue: ICDeviceTypeMask.camera.rawValue
                | ICDeviceLocationTypeMask.local.rawValue
        )!
        browser.start()
    }

    func stop() {
        browser.stop()
        if let camera {
            camera.ptpEventHandler = { _ in }
            camera.requestCloseSession()
        }
        camera = nil
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
        guard let camera else { throw TetherCaptureError.notConnected }
        let transaction = nextTransaction
        nextTransaction &+= 1
        return try await withCheckedThrowingContinuation { continuation in
            ptpPairContinuations[transaction] = continuation
            // ImageCaptureCore calls this off the main thread. Keep it
            // @Sendable (not @MainActor) so Swift does not synchronously hop
            // back to main while `requestSendPTPCommand` is still on the stack.
            let finish: @Sendable (Data, Data, (any Error)?) -> Void = { [weak self] data, response, error in
                Task { @MainActor in
                    guard let self else { return }
                    let key = PTP.responseParameter(response, 0)
                        ?? PTP.responseParameter(data, 0)
                        ?? transaction
                    guard let waiting = self.ptpPairContinuations.removeValue(forKey: key)
                        ?? self.ptpPairContinuations.removeValue(forKey: transaction)
                    else { return }
                    if let error {
                        waiting.resume(throwing: error)
                    } else {
                        waiting.resume(returning: PTP.orderedCommandResult(
                            data: data, response: response
                        ))
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

    private func adopt(_ device: ICDevice) {
        guard camera == nil, let found = device as? ICCameraDevice else { return }
        if found.transportType == ICDeviceTransport.transportTypeMassStorage.rawValue {
            Task { await owner?.bridgeDidUpdate(state: .massStorage) }
            return
        }
        camera = found
        let cameraOwner = owner
        found.ptpEventHandler = { data in
            guard let event = PTP.decodeEvent(data) else { return }
            Task { await cameraOwner?.bridgeDidReceivePushedEvent(code: event.code, params: event.params) }
        }
        found.delegate = self
        Task { await owner?.bridgeDidUpdate(state: .preparing) }
        found.requestOpenSession()
    }
}

extension TetherCameraBridge: ICDeviceBrowserDelegate {
    func deviceBrowser(_: ICDeviceBrowser, didAdd device: ICDevice, moreComing _: Bool) {
        adopt(device)
    }

    func deviceBrowser(_: ICDeviceBrowser, didRemove device: ICDevice, moreGoing _: Bool) {
        guard device === camera else { return }
        camera = nil
        Task { await owner?.bridgeDeviceLost() }
    }
}

extension TetherCameraBridge: ICCameraDeviceDelegate {
    func didRemove(_: ICDevice) {}

    func device(_: ICDevice, didOpenSessionWithError error: Error?) {
        if error != nil {
            Task { await owner?.bridgeDidUpdate(state: .unavailable) }
            return
        }
        Task { await connectAfterOpen() }
    }

    func device(_: ICDevice, didCloseSessionWithError _: Error?) {}

    func deviceDidBecomeReady(withCompleteContentCatalog _: ICCameraDevice) {}

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
        do {
            let (data, response) = try await sendPTPPair(PTP.getDeviceInfo)
            guard PTP.responseCode(response) == PTP.responseOK,
                  let info = PTP.DeviceInfo(payload: data)
            else {
                await owner.bridgeDidUpdate(state: .unsupported)
                return
            }
            _ = info
            try await owner.finishConnect(deviceInfo: info)
        } catch {
            await owner.bridgeDidUpdate(state: .unavailable)
        }
    }
}
