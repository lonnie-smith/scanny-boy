import Foundation

/// The preview's complete tone state — the nine keys the `tone` op records.
public struct ToneAdjustment: Equatable, Sendable, Hashable {
    public var gradeR: Double
    public var snapGamma: Double
    public var density: Double
    public var shadowDensity: Double
    public var highlightDensity: Double
    public var toe: Double
    public var toeWidth: Double
    public var shoulder: Double
    public var shoulderWidth: Double

    public static let neutral = ToneAdjustment(
        gradeR: 115,
        snapGamma: 0,
        density: 1,
        shadowDensity: 0,
        highlightDensity: 0,
        toe: 0,
        toeWidth: 2.5,
        shoulder: 0,
        shoulderWidth: 2.5
    )
    public init(
        gradeR: Double,
        snapGamma: Double,
        density: Double,
        shadowDensity: Double,
        highlightDensity: Double,
        toe: Double,
        toeWidth: Double,
        shoulder: Double,
        shoulderWidth: Double
    ) {
        self.gradeR = gradeR
        self.snapGamma = snapGamma
        self.density = density
        self.shadowDensity = shadowDensity
        self.highlightDensity = highlightDensity
        self.toe = toe
        self.toeWidth = toeWidth
        self.shoulder = shoulder
        self.shoulderWidth = shoulderWidth
    }
}

/// Auto Density / Auto Grade are momentary commit requests, not persisted state.
public struct ToneAutoFlags: OptionSet, Sendable {
    public let rawValue: Int

    public init(rawValue: Int) {
        self.rawValue = rawValue
    }

    public static let density = ToneAutoFlags(rawValue: 1 << 0)
    public static let grade = ToneAutoFlags(rawValue: 1 << 1)
}

extension RollManifest.Negative {
    var toneAdjustment: ToneAdjustment? {
        guard let toneGradeR, let toneSnapGamma else { return nil }
        return ToneAdjustment(
            gradeR: toneGradeR,
            snapGamma: toneSnapGamma,
            density: toneDensity ?? ToneAdjustment.neutral.density,
            shadowDensity: toneShadowDensity ?? 0,
            highlightDensity: toneHighlightDensity ?? 0,
            toe: toneToe ?? 0,
            toeWidth: toneToeWidth ?? ToneAdjustment.neutral.toeWidth,
            shoulder: toneShoulder ?? 0,
            shoulderWidth: toneShoulderWidth ?? ToneAdjustment.neutral.shoulderWidth
        )
    }
}
