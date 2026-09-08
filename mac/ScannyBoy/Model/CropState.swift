import Foundation

/// The net `crop` op as `roll info` and every `edit_recorded` report it
/// (protocol version 19, docs/CROP_PLAN.md): the cropped display image's
/// final dimensions plus the stored tilt and the ratio-preset label. The
/// rect itself never crosses the wire — the preview the app shows is
/// already cropped, and a fresh crop session draws a new rect over it.
/// `nil` means no live crop: none recorded, `--reset`, or one a re-stitch
/// made stale.
public struct CropState: Sendable, Hashable {
    /// The cropped display image's width, quarter turns folded in.
    let width: Int
    /// The cropped display image's height, quarter turns folded in.
    let height: Int
    /// The window's counter-clockwise tilt as displayed, in degrees.
    let tiltDegrees: Double
    /// The film-format ratio preset the rect was constrained with — a
    /// label for the sidebar's continuity, never read back for geometry.
    let preset: String?

    init?(fields: [String: JSONValue]) {
        guard
            let width = fields["width"]?.intValue,
            let height = fields["height"]?.intValue,
            let tiltDegrees = fields["tilt_deg"]?.doubleValue
        else { return nil }
        self.init(
            width: width,
            height: height,
            tiltDegrees: tiltDegrees,
            preset: fields["preset"]?.stringValue
        )
    }

    init(width: Int, height: Int, tiltDegrees: Double, preset: String?) {
        self.width = width
        self.height = height
        self.tiltDegrees = tiltDegrees
        self.preset = preset
    }
}
