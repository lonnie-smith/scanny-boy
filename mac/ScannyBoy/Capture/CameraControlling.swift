import Foundation

/// Where a tethered release writes its frame.
enum CaptureDestination: String, Sendable, CaseIterable {
    case buffer
    case card
}

/// Exposure snapshot read at connect (docs/TETHER_PLAN.md §2.4).
struct TetherExposureSettings: Sendable, Hashable {
    var program: UInt32
    var shutter: UInt32
    var aperture: UInt32
    var iso: UInt32
    var whiteBalance: UInt32
    var focusMode: UInt32

    var isManualProgram: Bool { program == 1 }
    var isManualFocus: Bool { focusMode == 1 }

    var programDescription: String {
        PTP.decodePropertyValue(0x500E, raw: program)
    }

    var shutterDescription: String {
        PTP.decodePropertyValue(0x500D, raw: shutter)
    }
}

/// One frame waiting in the camera buffer on connect (§2.5).
struct BufferLeftover: Sendable, Hashable, Identifiable {
    var id: UInt32 { handle }
    let handle: UInt32
    let objectInfo: PTP.ObjectInfo
}

/// Connection states shown in the Capture tab (§2.1).
enum TetherConnectionState: Sendable, Equatable {
    case absent
    case searching
    case massStorage
    case unavailable
    case preparing
    case unsupported
    case ready
    case busy
    case lost
}

enum TetherCaptureError: Error, Sendable, LocalizedError {
    case notConnected
    case exposureTimeout
    case frameArrivalTimeout
    case downloadFailed(String)
    case bufferNotCleared(UInt32)
    case releaseFailed(String)
    case sessionDroppedAfterShutter
    case leftoverPresent([BufferLeftover])
    case liveViewRefused(UInt16)
    case liveViewFrameInvalid

    var errorDescription: String? {
        switch self {
        case .notConnected:
            "The camera is not connected."
        case .exposureTimeout:
            "The camera did not finish the exposure in time."
        case .frameArrivalTimeout:
            "The shutter fired, but no frame arrived from the camera."
        case .sessionDroppedAfterShutter:
            "The camera dropped the USB session after the shutter fired. Reconnect and try again."
        case .downloadFailed(let detail):
            "The frame could not be downloaded (\(detail))."
        case .bufferNotCleared(let handle):
            String(format: "The camera kept buffer frame 0x%08x after download.", handle)
        case .releaseFailed(let detail):
            "The shutter fired, but the camera refused the release (\(detail))."
        case .leftoverPresent(let leftovers):
            leftovers.count == 1
                ? "A frame is still in the camera buffer. Discard it, then shoot again."
                : "Frames are still in the camera buffer. Discard them, then shoot again."
        case .liveViewRefused(let code):
            String(format: "The camera refused live view (0x%04x).", code)
        case .liveViewFrameInvalid:
            "The camera sent an unreadable live view frame."
        }
    }
}

/// One live view frame from `GetLiveViewImage` (`0x9203`).
struct LiveViewFrame: Sendable, Hashable {
    let header: PTP.LiveViewHeader
    let jpegData: Data
}

/// Result of one cell's release → download cycle.
struct TetherCapturedFrame: Sendable {
    let url: URL
    let handle: UInt32
    let objectInfo: PTP.ObjectInfo
}

/// Abstraction over `TetherCamera` and test fakes.
protocol CameraControlling: Actor {
    var connectionState: TetherConnectionState { get }
    var exposure: TetherExposureSettings? { get }
    var leftovers: [BufferLeftover] { get }
    var destination: CaptureDestination { get set }

    func setConnectionHandler(
        _ handler: (@Sendable (TetherConnectionState, TetherExposureSettings?) -> Void)?
    ) async
    func applyDestination(_ destination: CaptureDestination) async
    func startBrowsing() async
    func stopBrowsing() async
    func drainEvents() async throws
    func scanBuffer() async throws -> [UInt32]
    func release() async throws
    func waitForExposureEnd() async throws
    func waitForFrame(after handlesBefore: Set<UInt32>) async throws -> UInt32
    func download(handle: UInt32, to url: URL) async throws -> TetherCapturedFrame
    func confirmBufferCleared(handle: UInt32) async throws
    func discardBufferFrame(handle: UInt32) async throws
    func startLiveView() async throws
    func endLiveView() async
    func liveViewFrame() async throws -> LiveViewFrame
}
