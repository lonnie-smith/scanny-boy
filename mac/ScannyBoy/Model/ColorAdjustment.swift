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

/// PCHIP (monotone cubic) interpolation of a per-channel curve through its
/// three offset control points, plus the clamp that keeps a curve valid.
/// Matches the Python `color.channel_curve`
/// (`scipy.interpolate.PchipInterpolator`) to double-precision rounding —
/// see `docs/COLOR_BALANCE_CURVES_PLAN.md` §2.2, and `ChannelCurveTests`
/// for the scipy reference values this is checked against.
public enum ChannelCurve {
    /// Each offset is bounded to this either side of neutral
    /// (`color.CURVE_OFFSET_MAX`).
    public static let offsetMax = 0.2
    /// Consecutive knots must be at least this far apart in y, so a curve
    /// can never reverse (`color.CURVE_MIN_GAP`).
    public static let minGap = 0.02

    public static let xKnots: [Double] = [0, 0.25, 0.5, 0.75, 1]

    public static func yKnots(_ offsets: (q1: Double, mid: Double, q3: Double)) -> [Double] {
        [0, 0.25 + offsets.q1, 0.5 + offsets.mid, 0.75 + offsets.q3, 1]
    }

    /// PCHIP (Fritsch–Carlson) tangents at each knot, matching scipy's
    /// `PchipInterpolator`: interior points use a weighted harmonic mean of
    /// the adjacent secant slopes (zero where they disagree in sign or
    /// either is zero), and each end uses the three-point shape-preserving
    /// formula. Compute this once per curve, not once per sample point.
    public static func slopes(x: [Double], y: [Double]) -> [Double] {
        let n = x.count
        precondition(n >= 3 && x.count == y.count)
        var h = [Double](repeating: 0, count: n - 1)
        var delta = [Double](repeating: 0, count: n - 1)
        for i in 0..<(n - 1) {
            h[i] = x[i + 1] - x[i]
            delta[i] = (y[i + 1] - y[i]) / h[i]
        }
        var d = [Double](repeating: 0, count: n)
        for i in 1..<(n - 1) {
            if delta[i - 1] * delta[i] <= 0 {
                d[i] = 0
            } else {
                let w1 = 2 * h[i] + h[i - 1]
                let w2 = h[i] + 2 * h[i - 1]
                d[i] = (w1 + w2) / (w1 / delta[i - 1] + w2 / delta[i])
            }
        }
        d[0] = endpointSlope(h0: h[0], h1: h[1], delta0: delta[0], delta1: delta[1])
        d[n - 1] = endpointSlope(
            h0: h[n - 2], h1: h[n - 3], delta0: delta[n - 2], delta1: delta[n - 3]
        )
        return d
    }

    private static func endpointSlope(h0: Double, h1: Double, delta0: Double, delta1: Double) -> Double {
        var d0 = ((2 * h0 + h1) * delta0 - h0 * delta1) / (h0 + h1)
        if sign(d0) != sign(delta0) {
            d0 = 0
        } else if sign(delta0) != sign(delta1) && abs(d0) > abs(3 * delta0) {
            d0 = 3 * delta0
        }
        return d0
    }

    private static func sign(_ v: Double) -> Double {
        v > 0 ? 1 : (v < 0 ? -1 : 0)
    }

    /// Evaluates the Hermite spline at `t` given precomputed knots and
    /// slopes. `t` is expected in `[0, 1]`; the curve is identity above 1
    /// (display headroom passes through untouched).
    public static func evaluate(t: Double, x: [Double], y: [Double], slopes d: [Double]) -> Double {
        if t <= 0 { return 0 }
        if t >= 1 { return t }
        var k = 0
        for i in 0..<(x.count - 1) where t >= x[i] && t <= x[i + 1] {
            k = i
            break
        }
        let h = x[k + 1] - x[k]
        guard h > 0 else { return y[k] }
        let s = (t - x[k]) / h
        let h00 = (1 + 2 * s) * (1 - s) * (1 - s)
        let h10 = s * (1 - s) * (1 - s)
        let h01 = s * s * (3 - 2 * s)
        let h11 = s * s * (s - 1)
        return h00 * y[k] + h10 * h * d[k] + h01 * y[k + 1] + h11 * h * d[k + 1]
    }

    /// Single-shot evaluation for callers that don't already have
    /// precomputed knots/slopes. Prefer `evaluate(t:x:y:slopes:)` with
    /// slopes computed once when sampling many points off the same curve.
    public static func evaluate(t: Double, offsets: (q1: Double, mid: Double, q3: Double)) -> Double {
        let y = yKnots(offsets)
        return evaluate(t: t, x: xKnots, y: y, slopes: slopes(x: xKnots, y: y))
    }

    /// Clamps a proposed offset for one control point (0 = q1, 1 = mid,
    /// 2 = q3) to `±offsetMax` and to the ordering rule against the other
    /// two points, which are held fixed — so the app can never send an
    /// invalid curve, whether the edit came from a slider or a drag.
    public static func clampOffset(
        _ proposed: Double,
        at index: Int,
        other offsets: (q1: Double, mid: Double, q3: Double)
    ) -> Double {
        let y = yKnots(offsets)
        let knot = index + 1
        let base = xKnots[knot]
        let bounded = min(offsetMax, max(-offsetMax, proposed))
        let lower = y[knot - 1] + minGap
        let upper = y[knot + 1] - minGap
        let candidate = min(upper, max(lower, base + bounded))
        return candidate - base
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
