import Foundation

/// The preview's complete colour state — the thirteen keys the `color` op records.
public struct ColorAdjustment: Equatable, Sendable, Hashable {
    public var wbCyan: Double
    public var wbMagenta: Double
    public var wbYellow: Double
    public var shadowCyan: Double
    public var shadowMagenta: Double
    public var shadowYellow: Double
    public var highlightCyan: Double
    public var highlightMagenta: Double
    public var highlightYellow: Double
    public var castRemoval: Double
    public var castRemovalHighlights: Double
    public var dyeSeparation: Double
    public var separationDamping: Double

    public static let neutral = ColorAdjustment(
        wbCyan: 0,
        wbMagenta: 0,
        wbYellow: 0,
        shadowCyan: 0,
        shadowMagenta: 0,
        shadowYellow: 0,
        highlightCyan: 0,
        highlightMagenta: 0,
        highlightYellow: 0,
        castRemoval: 0,
        castRemovalHighlights: 0,
        dyeSeparation: 1,
        separationDamping: 0
    )

    public init(
        wbCyan: Double,
        wbMagenta: Double,
        wbYellow: Double,
        shadowCyan: Double,
        shadowMagenta: Double,
        shadowYellow: Double,
        highlightCyan: Double,
        highlightMagenta: Double,
        highlightYellow: Double,
        castRemoval: Double,
        castRemovalHighlights: Double,
        dyeSeparation: Double,
        separationDamping: Double
    ) {
        self.wbCyan = wbCyan
        self.wbMagenta = wbMagenta
        self.wbYellow = wbYellow
        self.shadowCyan = shadowCyan
        self.shadowMagenta = shadowMagenta
        self.shadowYellow = shadowYellow
        self.highlightCyan = highlightCyan
        self.highlightMagenta = highlightMagenta
        self.highlightYellow = highlightYellow
        self.castRemoval = castRemoval
        self.castRemovalHighlights = castRemovalHighlights
        self.dyeSeparation = dyeSeparation
        self.separationDamping = separationDamping
    }
}

/// Auto Cast is a momentary commit request, not persisted state.
public struct ColorAutoFlags: OptionSet, Sendable {
    public let rawValue: Int

    public init(rawValue: Int) {
        self.rawValue = rawValue
    }

    public static let cast = ColorAutoFlags(rawValue: 1 << 0)
}

extension RollManifest.Negative {
    var colorAdjustment: ColorAdjustment? {
        guard colorWbMagenta != nil else { return nil }
        return ColorAdjustment(
            wbCyan: colorWbCyan ?? 0,
            wbMagenta: colorWbMagenta ?? 0,
            wbYellow: colorWbYellow ?? 0,
            shadowCyan: colorShadowCyan ?? 0,
            shadowMagenta: colorShadowMagenta ?? 0,
            shadowYellow: colorShadowYellow ?? 0,
            highlightCyan: colorHighlightCyan ?? 0,
            highlightMagenta: colorHighlightMagenta ?? 0,
            highlightYellow: colorHighlightYellow ?? 0,
            castRemoval: colorCastRemoval ?? 0,
            castRemovalHighlights: colorCastRemovalHighlights ?? 0,
            dyeSeparation: colorDyeSeparation ?? ColorAdjustment.neutral.dyeSeparation,
            separationDamping: colorSeparationDamping ?? 0
        )
    }
}

/// Nominal Kelvin readout and temperature lever — mirrors `color.py`.
enum ColorTemperature {
    static let neutralKelvin = 5500.0
    static let minKelvin = 3000.0
    static let maxKelvin = 12000.0

    private static let refKelvin = 5500.0
    private static let kMagenta = 0.0029
    private static let kYellow = 0.0057

    static func kelvin(magenta: Double, yellow: Double) -> Double {
        let dmu = (kMagenta * magenta + kYellow * yellow) / (kMagenta * kMagenta + kYellow * kYellow)
        let mu = min(
            max(1e6 / refKelvin + dmu, 1e6 / maxKelvin),
            1e6 / minKelvin
        )
        return 1e6 / mu
    }

    /// Move (M, Y) along the Planckian direction to `kelvin`, preserving
    /// the off-locus tint. Anchor the inputs for a whole drag — re-projecting
    /// an already-clipped pair on every tick corrupts the tint component.
    static func whiteBalance(
        kelvin: Double,
        anchorMagenta: Double,
        anchorYellow: Double
    ) -> (magenta: Double, yellow: Double) {
        let clamped = min(max(kelvin, minKelvin), maxKelvin)
        let dmuCurrent = (kMagenta * anchorMagenta + kYellow * anchorYellow)
            / (kMagenta * kMagenta + kYellow * kYellow)
        let delta = (1e6 / clamped - 1e6 / refKelvin) - dmuCurrent
        let magenta = min(max(anchorMagenta + kMagenta * delta, -1), 1)
        let yellow = min(max(anchorYellow + kYellow * delta, -1), 1)
        return (magenta, yellow)
    }
}
