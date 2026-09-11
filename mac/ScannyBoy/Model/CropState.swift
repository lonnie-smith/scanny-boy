import Foundation

/// The net `crop` op as `roll info` and every `edit_recorded` report it
/// (protocol version 19): the cropped display image's final dimensions
/// plus the stored tilt and the ratio-preset label. When the report also
/// carries the window's origin and the full uncropped canvas, the app can
/// re-enter crop mode on the whole frame with the saved rect
/// superimposed. `nil` means no live crop: none recorded, `--reset`, or
/// one a re-stitch made stale.
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
    /// The crop window's origin on the full uncropped display frame.
    let x: Int?
    let y: Int?
    /// The full uncropped display image's dimensions, quarter turns folded in.
    let canvasWidth: Int?
    let canvasHeight: Int?

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
            preset: fields["preset"]?.stringValue,
            x: fields["x"]?.intValue,
            y: fields["y"]?.intValue,
            canvasWidth: fields["canvas_width"]?.intValue,
            canvasHeight: fields["canvas_height"]?.intValue
        )
    }

    init(
        width: Int,
        height: Int,
        tiltDegrees: Double,
        preset: String?,
        x: Int? = nil,
        y: Int? = nil,
        canvasWidth: Int? = nil,
        canvasHeight: Int? = nil
    ) {
        self.width = width
        self.height = height
        self.tiltDegrees = tiltDegrees
        self.preset = preset
        self.x = x
        self.y = y
        self.canvasWidth = canvasWidth
        self.canvasHeight = canvasHeight
    }

    /// The saved crop window on the full uncropped display, when the
    /// report carries enough geometry to restore it.
    var editingRect: CGRect? {
        guard let x, let y else { return nil }
        return CGRect(x: x, y: y, width: width, height: height)
    }

    /// The full uncropped display canvas, when the report carries it.
    var canvasSize: CGSize? {
        guard let canvasWidth, let canvasHeight else { return nil }
        return CGSize(width: canvasWidth, height: canvasHeight)
    }
}
