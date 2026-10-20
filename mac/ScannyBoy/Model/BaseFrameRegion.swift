import CoreGraphics
import Foundation

/// A rectangle of a base frame, in fractions of its width and height —
/// `roll set-base-frame --region X0,Y0,X1,Y1`. For a frame whose clear
/// rebate is only a strip along an edge: the CLI then measures, and gates,
/// that area alone instead of the whole frame.
struct BaseFrameRegion: Equatable, Sendable {
    let x0: Double
    let y0: Double
    let x1: Double
    let y1: Double

    /// `nil` unless `rect`, clamped to the unit square, still has an area
    /// the `--region` argument's four decimal places can represent.
    init?(normalizedRect rect: CGRect) {
        let x0 = Double(max(rect.minX, 0))
        let y0 = Double(max(rect.minY, 0))
        let x1 = Double(min(rect.maxX, 1))
        let y1 = Double(min(rect.maxY, 1))
        guard x1 - x0 >= 0.001, y1 - y0 >= 0.001 else { return nil }
        self.x0 = x0
        self.y0 = y0
        self.x1 = x1
        self.y1 = y1
    }

    /// The `--region` argument.
    var argument: String {
        [x0, y0, x1, y1].map { String(format: "%.4f", $0) }.joined(separator: ",")
    }
}
