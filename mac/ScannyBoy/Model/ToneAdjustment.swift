import Foundation

/// The preview's tone state — the four keys the `tone` op records.
public struct ToneAdjustment: Equatable, Sendable, Hashable {
    public var snapGamma: Double
    public var density: Double
    public var shadowDensity: Double
    public var highlightDensity: Double

    public static let neutral = ToneAdjustment(
        snapGamma: 0.15,
        density: 1,
        shadowDensity: 0,
        highlightDensity: 0
    )
    public init(
        snapGamma: Double,
        density: Double,
        shadowDensity: Double,
        highlightDensity: Double
    ) {
        self.snapGamma = snapGamma
        self.density = density
        self.shadowDensity = shadowDensity
        self.highlightDensity = highlightDensity
    }
}

/// Auto Density is a momentary commit request, not persisted state.
public struct ToneAutoFlags: OptionSet, Sendable {
    public let rawValue: Int

    public init(rawValue: Int) {
        self.rawValue = rawValue
    }

    public static let density = ToneAutoFlags(rawValue: 1 << 0)
}

extension RollManifest.Negative {
    var toneAdjustment: ToneAdjustment? {
        guard let toneSnapGamma else { return nil }
        return ToneAdjustment(
            snapGamma: toneSnapGamma,
            density: toneDensity ?? ToneAdjustment.neutral.density,
            shadowDensity: toneShadowDensity ?? 0,
            highlightDensity: toneHighlightDensity ?? 0
        )
    }
}
