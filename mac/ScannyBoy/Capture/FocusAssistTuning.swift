import Foundation

/// Every focus-assist constant lives here (docs/FOCUS_ASSIST_PLAN.md).
enum FocusAssistTuning {
    /// Bytes before the JPEG in a `GetLiveViewImage` payload on the Z f.
    static let liveViewHeaderLength = 384

    /// Retries when `GetLiveViewImage` answers device busy (`0x2019`).
    static let liveViewBusyRetries = 5

    /// Poll interval when the camera returns an unchanged JPEG (30 fps refresh).
    static let liveViewPollInterval: Duration = .milliseconds(15)

    /// Median window for the displayed score — three frames at 30 fps.
    static let scoreMedianFrames = 3

    /// Below this Laplacian score the meter reads "not enough detail here".
    /// Picked below the measured out-of-focus floor (~2.4) on one negative.
    static let scoreMinTexture = 1.5

    /// Meter bar turns green at or above this fraction of the held peak.
    static let peakBand = 0.97

    /// Sensor pixels across at the third magnify step — the focusing zoom.
    static let focusZoomAreaWidth = 512

    /// Full Z f sensor size, for zoom text and the area outline.
    static let sensorWidth = 6048
    static let sensorHeight = 4032

    /// Regions below this raw focus ratio are greyed out in the check-shot grid
    /// (matches `capture_analysis.FOCUS_MIN_TEXTURE`).
    static let focusMinTexture = 1e-4

    /// Live view frame size on the Z f, in pixels. Sizes the loupe's
    /// placeholder before the first frame arrives
    /// (docs/FOCUS_LOCK_PLAN.md §1.1).
    static let liveViewFrameWidth = 640
    static let liveViewFrameHeight = 424

    /// Frames, counted from a view change or R, whose centre-half luminance
    /// the grain view takes its levels from; after that the levels are held
    /// (docs/FOCUS_LOCK_PLAN.md §1.2).
    static let grainLevelFrames = 3

    /// Percentiles of the centre half's luminance that the grain view
    /// stretches to black and white.
    static let grainLowPercentile = 0.01
    static let grainHighPercentile = 0.99
}
