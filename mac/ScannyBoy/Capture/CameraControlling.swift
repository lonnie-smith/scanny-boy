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

enum TetherCaptureError: Error, Sendable {
    case notConnected
    case exposureTimeout
    case frameArrivalTimeout
    case downloadFailed(String)
    case bufferNotCleared(UInt32)
    case releaseFailed(String)
    case leftoverPresent([BufferLeftover])
    case liveViewRefused(UInt16)
    case liveViewFrameInvalid
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
