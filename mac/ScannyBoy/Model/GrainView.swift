import CoreGraphics
import Foundation

/// Black and white points for the grain view, as 8-bit luminance values.
struct GrainLevels: Sendable, Equatable {
    let low: UInt8
    let high: UInt8
}

/// Collects the centre half's luminance over the first
/// `FocusAssistTuning.grainLevelFrames` frames of a view and turns it into
/// `GrainLevels`. Once complete it ignores further frames, so the levels are
/// held: re-stretching every frame would pump the brightness while the ring
/// turns (docs/FOCUS_LOCK_PLAN.md §1.2).
struct GrainLevelSampler: Sendable {
    private var histogram = [Int](repeating: 0, count: 256)
    private(set) var frameCount = 0

    var isComplete: Bool { frameCount >= FocusAssistTuning.grainLevelFrames }

    mutating func add(_ grey: FocusScore.GreyFrame) {
        guard !isComplete else { return }
        for y in (grey.height / 4)..<(grey.height * 3 / 4) {
            for x in (grey.width / 4)..<(grey.width * 3 / 4) {
                histogram[Int(grey.pixels[y * grey.width + x])] += 1
            }
        }
        frameCount += 1
    }

    /// The levels so far; nil before any frame. Provisional until `isComplete`.
    var levels: GrainLevels? {
        let total = histogram.reduce(0, +)
        guard total > 0 else { return nil }
        func value(atPercentile percentile: Double) -> UInt8 {
            let target = Int((Double(total - 1) * percentile).rounded())
            var seen = 0
            for (value, count) in histogram.enumerated() {
                seen += count
                if seen > target { return UInt8(value) }
            }
            return 255
        }
        return GrainLevels(
            low: value(atPercentile: FocusAssistTuning.grainLowPercentile),
            high: value(atPercentile: FocusAssistTuning.grainHighPercentile)
        )
    }
}

/// The grain view's picture: grey, inverted (the negative shown as a
/// positive), stretched between `GrainLevels`. Display only — the score
/// always reads the untouched grey frame (docs/FOCUS_LOCK_PLAN.md §1.2).
enum GrainView {
    static func render(_ grey: FocusScore.GreyFrame, levels: GrainLevels) -> CGImage? {
        let low = Int(levels.low)
        let span = max(1, Int(levels.high) - low)
        let lookup = (0..<256).map { value -> UInt8 in
            let stretched = min(255, max(0, (value - low) * 255 / span))
            return UInt8(255 - stretched)
        }
        let pixels = grey.pixels.map { lookup[Int($0)] }
        guard let provider = CGDataProvider(data: Data(pixels) as CFData) else { return nil }
        return CGImage(
            width: grey.width,
            height: grey.height,
            bitsPerComponent: 8,
            bitsPerPixel: 8,
            bytesPerRow: grey.width,
            space: CGColorSpaceCreateDeviceGray(),
            bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.none.rawValue),
            provider: provider,
            decode: nil,
            shouldInterpolate: false,
            intent: .defaultIntent
        )
    }
}
