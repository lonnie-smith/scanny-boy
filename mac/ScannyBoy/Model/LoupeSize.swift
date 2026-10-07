import CoreGraphics

/// The loupe's three sizes, each a whole number of screen pixels per frame
/// pixel so nearest-neighbour drawing never resamples
/// (docs/FOCUS_LOCK_PLAN.md §1.1). Pure — no view dependency.
enum LoupeSize: Int, CaseIterable, Sendable {
    case small = 1
    case medium = 2
    case large = 3

    /// Screen pixels per frame pixel.
    var pixelsPerFramePixel: Int { rawValue }

    var label: String {
        switch self {
        case .small: "S"
        case .medium: "M"
        case .large: "L"
        }
    }

    /// S → M → L → S.
    var next: LoupeSize {
        switch self {
        case .small: .medium
        case .medium: .large
        case .large: .small
        }
    }

    /// Length in points of `framePixels` frame pixels at this size on a
    /// display with `displayScale` screen pixels per point.
    func points(framePixels: Int, displayScale: CGFloat) -> CGFloat {
        CGFloat(framePixels * pixelsPerFramePixel) / displayScale
    }

    /// The largest size no bigger than `self` whose frame fits in
    /// `availableWidth` points; S when even that does not fit. Never a
    /// fractional scale.
    func fitting(framePixels: Int, displayScale: CGFloat, availableWidth: CGFloat) -> LoupeSize {
        Self.allCases.reversed().first {
            $0.rawValue <= rawValue
                && $0.points(framePixels: framePixels, displayScale: displayScale) <= availableWidth
        } ?? .small
    }
}
