import Foundation

/// The preview's complete colour state — the keys the `color` op records.
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
    /// Global temperature in Kelvin — a layer under the CMY sliders, never
    /// written into them.
    public var temperature: Double

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
        separationDamping: 0,
        temperature: ColorTemperature.neutralKelvin
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
        separationDamping: Double,
        temperature: Double
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
        self.temperature = temperature
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
            separationDamping: colorSeparationDamping ?? 0,
            temperature: colorTemperature ?? ColorTemperature.neutralKelvin
        )
    }
}

/// The temperature layer's bounds, and the slider's mired-linear position —
/// mirrors `color.py`. The CLI owns the actual colour math.
enum ColorTemperature {
    static let neutralKelvin = 5500.0
    static let minKelvin = 3500.0
    static let maxKelvin = 12000.0

    /// Slider position: mired shift from neutral, positive warmer. Equal
    /// travel is equal warmth, and neutral sits near the middle.
    static func warmth(kelvin: Double) -> Double {
        1e6 / neutralKelvin - 1e6 / kelvin
    }

    static func kelvin(warmth: Double) -> Double {
        min(max(1e6 / (1e6 / neutralKelvin - warmth), minKelvin), maxKelvin)
    }

    static var warmthRange: ClosedRange<Double> {
        warmth(kelvin: minKelvin)...warmth(kelvin: maxKelvin)
    }
}
