import Foundation

/// Every timing constant for tethered capture lives here and nowhere else
/// (docs/TETHER_PLAN.md §2–§3).
enum TetherTiming {
    /// Poll interval for Nikon `DeviceReady` while waiting for exposure end.
    static let readyPollInterval: Duration = .milliseconds(50)

    /// Poll interval while scanning for a buffer frame after release.
    static let framePollInterval: Duration = .milliseconds(100)

    /// Maximum wait for `didCloseSession` on Disconnect.
    static let closeSessionTimeout: Duration = .seconds(5)

    /// Grace after the measured exposure before giving up on `DeviceReady`.
    static let exposureGrace: Duration = .seconds(10)

    /// Maximum wait for a frame handle to appear after release.
    static let frameArrivalTimeout: Duration = .seconds(10)

    /// Last buffer handle scanned before a release (`GetObjectInfo` walk).
    static let bufferScanLast: UInt32 = 0x0B00_0010

    /// First buffer handle the Z f uses once numbering restarts.
    static let bufferScanFirst: UInt32 = 0x0B00_0001

    /// Audible hold cue lead time before the next release.
    static let holdCueLead: TimeInterval = 1.0

    /// Computes the exposure timeout from a shutter speed in PTP units
    /// (seconds × 10_000; `0xFFFF_FFFF` = bulb).
    static func exposureTimeout(shutterPTP: UInt32) -> Duration {
        let exposure: TimeInterval
        if shutterPTP == 0xFFFF_FFFF {
            exposure = 30
        } else {
            exposure = max(Double(shutterPTP) / 10_000, 0.001)
        }
        return .seconds(exposure) + exposureGrace
    }
}
