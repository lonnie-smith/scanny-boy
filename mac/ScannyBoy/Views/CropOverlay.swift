import SwiftUI

/// The film-format ratio presets the crop mode constrains the rect with —
/// the *actual* film gate sizes, not the nominal ones:
/// 35mm is 24×36mm, and the 120 roll's gates are 56mm across the
/// film, with the long side set by the camera maker (6×7 is the Mamiya
/// RB67/RZ67's 56×69.5mm; Pentax 67's is 55×70). The ratios are
/// landscape; a portrait display image gets the sides swapped
/// (`CropSession.orientedRatio`). Free leaves the rect unconstrained.
enum CropPreset: String, CaseIterable, Identifiable {
    case free
    case film35 = "35mm"
    case film645 = "645"
    case film6x6 = "6x6"
    case film6x7 = "6x7"
    case film6x9 = "6x9"

    var id: String { rawValue }

    var label: String {
        switch self {
        case .free: "Free"
        case .film35: "35mm"
        case .film645: "6×4.5"
        case .film6x6: "6×6"
        case .film6x7: "6×7"
        case .film6x9: "6×9"
        }
    }

    /// The preset's width : height ratio in landscape orientation, or nil
    /// for Free.
    var ratio: Double? {
        switch self {
        case .free: nil
        case .film35: 36.0 / 24.0
        case .film645: 56.0 / 41.5
        case .film6x6: 1.0
        case .film6x7: 56.0 / 69.5
        case .film6x9: 56.0 / 84.0
        }
    }
}

/// One crop-mode editing session: the rect (display space, the
/// axis-aligned frame the `crop` op names), the tilt (counter-clockwise
/// as displayed, what the op stores), and the ratio preset. The preview
/// pane owns the session; the Geometry sidebar's crop section and the
/// overlay both drive it.
@Observable
final class CropSession {
    static let tiltRange: ClosedRange<Double> = -10...10
    static let tiltStep: Double = 0.1

    /// Whether the overlay is up. Leaving the tab, changing negatives,
    /// or Apply/Cancel clears it.
    var isActive = false
    /// The crop rect in display space — the axis-aligned frame, which the
    /// tilt then rotates about its own centre (the `crop` op's semantics).
    var rect: CGRect = .zero
    var tiltDegrees: Double = 0
    var preset: CropPreset = .free
    /// The rect a drag started from; nil between gestures.
    var dragBaseRect: CGRect?

    func begin(displaySize: CGSize) {
        guard displaySize.width > 0, displaySize.height > 0 else { return }
        let ratio = preset.ratio.map {
            displaySize.width >= displaySize.height ? $0 : 1.0 / $0
        }
        rect = CropGeometry.defaultRect(in: displaySize, ratio: ratio)
        isActive = true
    }

    func end() {
        isActive = false
        dragBaseRect = nil
    }

    /// The preset's ratio oriented to the display image: a portrait
    /// display swaps the landscape ratio's sides, so one preset per film
    /// format serves both orientations.
    var orientedRatio: Double? {
        guard let ratio = preset.ratio else { return nil }
        return rect.width >= rect.height ? ratio : 1.0 / ratio
    }

    /// Re-shapes the current rect to the newly-chosen preset's ratio,
    /// keeping its centre — the picker's immediate feedback.
    func applyPreset() {
        guard let ratio = orientedRatio else { return }
        var width = rect.width
        var height = rect.height
        if width / height > ratio {
            width = height * ratio
        } else {
            height = width / ratio
        }
        rect = CGRect(
            x: rect.midX - width / 2,
            y: rect.midY - height / 2,
            width: width,
            height: height
        )
    }
}

/// Pure geometry for the crop overlay — display-space rects and points,
/// the same space the `crop` op's `--x/--y/--width/--height` name. All
/// functions clamp into `bounds` and hold the 16px floor the CLI
/// validates (`repo.CROP_MIN_SIZE_PX`).
enum CropGeometry {
    static let minSize: CGFloat = 16

    /// A centred default rect covering ~80% of the image, shaped to
    /// `ratio` (already oriented to the image), clamped to the bounds.
    static func defaultRect(in bounds: CGSize, ratio: Double?) -> CGRect {
        var width = bounds.width * 0.8
        var height = bounds.height * 0.8
        if let ratio {
            if width / height > ratio {
                width = height * ratio
            } else {
                height = width / ratio
            }
        }
        width = min(max(width, min(minSize, bounds.width)), bounds.width)
        height = min(max(height, min(minSize, bounds.height)), bounds.height)
        return CGRect(
            x: (bounds.width - width) / 2,
            y: (bounds.height - height) / 2,
            width: width,
            height: height
        )
    }

    /// Translates `rect` by `delta`, clamped so the frame stays inside
    /// the bounds. Translation commutes with the tilt, so a tilted rect
    /// moves with the same drag.
    static func translated(
        _ rect: CGRect, by delta: CGSize, in bounds: CGSize
    ) -> CGRect {
        let x = min(max(rect.minX + delta.width, 0), bounds.width - rect.width)
        let y = min(max(rect.minY + delta.height, 0), bounds.height - rect.height)
        return CGRect(origin: CGPoint(x: x, y: y), size: rect.size)
    }

    /// Resizes `rect` by dragging `handle` to `point` (display space),
    /// anchoring the opposite side. With a `ratio`, the dominant dragged
    /// axis drives and the other follows. A tilted rect's resize is
    /// refused by the caller — a rotated frame's handle math is not worth
    /// its own edge cases.
    static func resized(
        _ rect: CGRect,
        handle: Handle,
        to point: CGPoint,
        ratio: Double?,
        in bounds: CGSize
    ) -> CGRect {
        switch handle {
        case .topLeft, .topRight, .bottomRight, .bottomLeft:
            let anchor: CGPoint
            switch handle {
            case .topLeft: anchor = CGPoint(x: rect.maxX, y: rect.maxY)
            case .topRight: anchor = CGPoint(x: rect.minX, y: rect.maxY)
            case .bottomRight: anchor = CGPoint(x: rect.minX, y: rect.minY)
            case .bottomLeft: anchor = CGPoint(x: rect.maxX, y: rect.minY)
            default: return rect
            }
            return cornerResized(anchor: anchor, dragged: point, ratio: ratio, bounds: bounds)
        case .top, .bottom:
            let y: CGFloat
            if handle == .top {
                y = min(max(point.y, 0), rect.maxY - minSize)
            } else {
                y = min(max(point.y, rect.minY + minSize), bounds.height)
            }
            let height = handle == .top ? rect.maxY - y : y - rect.minY
            let width: CGFloat
            if let ratio {
                width = max(CGFloat(ratio) * height, minSize)
            } else {
                width = rect.width
            }
            return clamped(
                CGRect(x: rect.minX, y: y, width: width, height: height),
                in: bounds
            )
        case .left, .right:
            let x: CGFloat
            if handle == .left {
                x = min(max(point.x, 0), rect.maxX - minSize)
            } else {
                x = min(max(point.x, rect.minX + minSize), bounds.width)
            }
            let width = handle == .left ? rect.maxX - x : x - rect.minX
            let height: CGFloat
            if let ratio {
                height = max(CGFloat(width / ratio), minSize)
            } else {
                height = rect.height
            }
            return clamped(
                CGRect(x: x, y: rect.minY, width: width, height: height),
                in: bounds
            )
        }
    }

    /// The frame between a fixed anchor corner and a dragged point, the
    /// dominant axis driving when a ratio constrains the shape.
    private static func cornerResized(
        anchor: CGPoint, dragged: CGPoint, ratio: Double?, bounds: CGSize
    ) -> CGRect {
        var dx = dragged.x - anchor.x
        var dy = dragged.y - anchor.y
        if let ratio {
            if abs(dx) >= abs(dy) {
                dy = (dx >= 0 ? 1 : -1) * max(abs(dx) / ratio, minSize)
            } else {
                dx = (dy >= 0 ? 1 : -1) * max(abs(dy) * ratio, minSize)
            }
        }
        let width = max(abs(dx), minSize)
        let height = max(abs(dy), minSize)
        let x = dx >= 0 ? anchor.x : anchor.x - width
        let y = dy >= 0 ? anchor.y : anchor.y - height
        return clamped(CGRect(x: x, y: y, width: width, height: height), in: bounds)
    }

    /// Keeps `frame` inside the bounds without changing its size — the
    /// resize math above can push an edge past them.
    private static func clamped(_ frame: CGRect, in bounds: CGSize) -> CGRect {
        let width = min(frame.width, bounds.width)
        let height = min(frame.height, bounds.height)
        let x = min(max(frame.minX, 0), bounds.width - width)
        let y = min(max(frame.minY, 0), bounds.height - height)
        return CGRect(x: x, y: y, width: width, height: height)
    }

    /// The tilted rect's four corners, in display space: the frame
    /// rotated counter-clockwise by `tiltDegrees` about its own centre —
    /// exactly the window the `crop` op's warp samples. In y-down display
    /// coordinates a visual counter-clockwise rotation maps (1, 0) to
    /// (cos θ, −sin θ).
    static func corners(of rect: CGRect, tiltDegrees: Double) -> [CGPoint] {
        let radians = tiltDegrees * Double.pi / 180
        let cosine = CGFloat(cos(radians))
        let sine = CGFloat(sin(radians))
        let centre = CGPoint(x: rect.midX, y: rect.midY)
        let halfWidth = rect.width / 2
        let halfHeight = rect.height / 2
        var points: [CGPoint] = []
        let offsets = [
            (-halfWidth, -halfHeight), (halfWidth, -halfHeight),
            (halfWidth, halfHeight), (-halfWidth, halfHeight),
        ]
        for (dx, dy) in offsets {
            points.append(
                CGPoint(
                    x: centre.x + dx * cosine + dy * sine,
                    y: centre.y - dx * sine + dy * cosine
                )
            )
        }
        return points
    }

    enum Handle: CaseIterable {
        case topLeft, top, topRight, right, bottomRight, bottom, bottomLeft, left

        /// The handle's position on the *untilted* frame; the overlay
        /// rotates the drawn handles with the rect, and resize is only
        /// live at zero tilt.
        func point(on rect: CGRect) -> CGPoint {
            switch self {
            case .topLeft: CGPoint(x: rect.minX, y: rect.minY)
            case .top: CGPoint(x: rect.midX, y: rect.minY)
            case .topRight: CGPoint(x: rect.maxX, y: rect.minY)
            case .right: CGPoint(x: rect.maxX, y: rect.midY)
            case .bottomRight: CGPoint(x: rect.maxX, y: rect.maxY)
            case .bottom: CGPoint(x: rect.midX, y: rect.maxY)
            case .bottomLeft: CGPoint(x: rect.minX, y: rect.maxY)
            case .left: CGPoint(x: rect.minX, y: rect.midY)
            }
        }
    }
}

/// The crop-mode overlay: a dark scrim outside the tilted window, its
/// border, a move surface, and the resize handles. All drawing happens in
/// the fit rect's pane coordinates — the display-space rect maps in
/// through `fitRect` the same way the preview image does; Apply sends
/// display-space values to the CLI, so Swift converts nothing on the way
/// out either.
struct CropOverlayView: View {
    let session: CropSession
    let fitRect: CGRect
    let displaySize: CGSize

    private var scale: CGFloat {
        displaySize.width > 0 ? fitRect.width / displaySize.width : 1
    }

    private var frame: CGRect { session.rect }
    private var bounds: CGSize { displaySize }
    private var windowPath: Path {
        var path = Path()
        let corners = CropGeometry
            .corners(of: frame, tiltDegrees: session.tiltDegrees)
            .map(panePoint)
        path.addLines(corners + [corners[0]])
        return path
    }

    private func panePoint(_ display: CGPoint) -> CGPoint {
        CGPoint(
            x: fitRect.minX + display.x * scale,
            y: fitRect.minY + display.y * scale
        )
    }

    private func displayPoint(_ pane: CGPoint) -> CGPoint {
        CGPoint(
            x: (pane.x - fitRect.minX) / scale,
            y: (pane.y - fitRect.minY) / scale
        )
    }

    /// A tilted rect's resize is refused; move still works.
    private var allowsResize: Bool { abs(session.tiltDegrees) < 0.05 }

    var body: some View {
        ZStack {
            // The scrim: the whole fit rect minus the tilted window,
            // even-odd filled.
            Path { path in
                path.addRect(fitRect)
                path.addPath(windowPath)
            }
            .fill(Color.black.opacity(0.55), style: FillStyle(eoFill: true))

            windowPath
                .stroke(Color.yellow, style: StrokeStyle(lineWidth: 1.5))

            windowPath
                .fill(Color.white.opacity(0.001))
                .contentShape(windowPath)
                .gesture(moveGesture)

            if allowsResize {
                ForEach(CropGeometry.Handle.allCases, id: \.self) { handle in
                    Circle()
                        .fill(Color.yellow)
                        .frame(width: 10, height: 10)
                        .position(panePoint(handle.point(on: frame)))
                        .gesture(resizeGesture(handle))
                }
            }
        }
        .allowsHitTesting(session.isActive)
    }

    private var moveGesture: some Gesture {
        DragGesture(minimumDistance: 1)
            .onChanged { value in
                if session.dragBaseRect == nil {
                    session.dragBaseRect = frame
                }
                session.rect = CropGeometry.translated(
                    session.dragBaseRect ?? frame,
                    by: CGSize(
                        width: value.translation.width / scale,
                        height: value.translation.height / scale
                    ),
                    in: bounds
                )
            }
            .onEnded { _ in session.dragBaseRect = nil }
    }

    private func resizeGesture(_ handle: CropGeometry.Handle) -> some Gesture {
        DragGesture(minimumDistance: 1)
            .onChanged { value in
                if session.dragBaseRect == nil {
                    session.dragBaseRect = frame
                }
                session.rect = CropGeometry.resized(
                    session.dragBaseRect ?? frame,
                    handle: handle,
                    to: displayPoint(value.location),
                    ratio: session.orientedRatio,
                    in: bounds
                )
            }
            .onEnded { _ in session.dragBaseRect = nil }
    }
}
