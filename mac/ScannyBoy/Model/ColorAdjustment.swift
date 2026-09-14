import Foundation

/// The preview's complete colour state — the keys the `color` op records.
public struct ColorAdjustment: Equatable, Sendable, Hashable {
    public var warmth: Double
    public var tint: Double
    public var curveRed25: Double
    public var curveRed50: Double
    public var curveRed75: Double
    public var curveGreen25: Double
    public var curveGreen50: Double
    public var curveGreen75: Double
    public var curveBlue25: Double
    public var curveBlue50: Double
    public var curveBlue75: Double
    public var castRemoval: Double
    public var castRemovalHighlights: Double
    public var dyeSeparation: Double
    public var separationDamping: Double

    public static let neutral = ColorAdjustment(
        warmth: 0,
        tint: 0,
        curveRed25: 0,
        curveRed50: 0,
        curveRed75: 0,
        curveGreen25: 0,
        curveGreen50: 0,
        curveGreen75: 0,
        curveBlue25: 0,
        curveBlue50: 0,
        curveBlue75: 0,
        castRemoval: 0,
        castRemovalHighlights: 0,
        dyeSeparation: 1,
        separationDamping: 0
    )

    public init(
        warmth: Double,
        tint: Double,
        curveRed25: Double,
        curveRed50: Double,
        curveRed75: Double,
        curveGreen25: Double,
        curveGreen50: Double,
        curveGreen75: Double,
        curveBlue25: Double,
        curveBlue50: Double,
        curveBlue75: Double,
        castRemoval: Double,
        castRemovalHighlights: Double,
        dyeSeparation: Double,
        separationDamping: Double
    ) {
        self.warmth = warmth
        self.tint = tint
        self.curveRed25 = curveRed25
        self.curveRed50 = curveRed50
        self.curveRed75 = curveRed75
        self.curveGreen25 = curveGreen25
        self.curveGreen50 = curveGreen50
        self.curveGreen75 = curveGreen75
        self.curveBlue25 = curveBlue25
        self.curveBlue50 = curveBlue50
        self.curveBlue75 = curveBlue75
        self.castRemoval = castRemoval
        self.castRemovalHighlights = castRemovalHighlights
        self.dyeSeparation = dyeSeparation
        self.separationDamping = separationDamping
    }
}

/// Auto Balance is a momentary commit request, not persisted state.
public struct ColorAutoFlags: OptionSet, Sendable {
    public let rawValue: Int

    public init(rawValue: Int) {
        self.rawValue = rawValue
    }

    public static let balance = ColorAutoFlags(rawValue: 1 << 0)
}

extension RollManifest.Negative {
    var colorAdjustment: ColorAdjustment? {
        guard colorWarmth != nil else { return nil }
        return ColorAdjustment(
            warmth: colorWarmth ?? 0,
            tint: colorTint ?? 0,
            curveRed25: colorCurveRed25 ?? 0,
            curveRed50: colorCurveRed50 ?? 0,
            curveRed75: colorCurveRed75 ?? 0,
            curveGreen25: colorCurveGreen25 ?? 0,
            curveGreen50: colorCurveGreen50 ?? 0,
            curveGreen75: colorCurveGreen75 ?? 0,
            curveBlue25: colorCurveBlue25 ?? 0,
            curveBlue50: colorCurveBlue50 ?? 0,
            curveBlue75: colorCurveBlue75 ?? 0,
            castRemoval: colorCastRemoval ?? 0,
            castRemovalHighlights: colorCastRemovalHighlights ?? 0,
            dyeSeparation: colorDyeSeparation ?? ColorAdjustment.neutral.dyeSeparation,
            separationDamping: colorSeparationDamping ?? 0
        )
    }
}
