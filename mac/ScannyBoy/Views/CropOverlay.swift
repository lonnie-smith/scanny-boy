import AppKit
import SwiftUI

/// The film-format ratio presets the crop mode constrains the rect with —
/// the *actual* film gate sizes, not the nominal ones:
/// 35mm is 24×36mm, and the 120 roll's gates are 56mm across the
/// film, with the long side set by the camera maker (6×7 is the Mamiya
/// RB67/RZ67's 56×69.5mm; Pentax 67's is 55×70). The ratios are
/// landscape (width ≥ height); roll-film gates like 6×7 store the
/// portrait-native ratio (width < height). A portrait display image
/// swaps when the stored ratio's orientation disagrees with the image's
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
/// as displayed, what the op stores — the preview rotates the image by
/// this amount about the rect's centre), and the ratio preset. The preview
/// pane owns the session; the Geometry sidebar's crop section and the
/// overlay both drive it.
@Observable
final class CropSession {
    static let tiltRange: ClosedRange<Double> = -10...10
    static let tiltStep: Double = 0.1

    /// Whether the overlay is up. Leaving the tab, changing negatives,
    /// or Apply/Cancel clears it.
    var isActive = false
    /// The crop rect in display space — the axis-aligned frame the overlay
    /// draws and the `crop` op stores; tilt rotates the image beneath it.
    var rect: CGRect = .zero
    var tiltDegrees: Double = 0
    var preset: CropPreset = .free
    /// The rect a drag started from; nil between gestures.
    var dragBaseRect: CGRect?
    /// The display image's size when the session began — the reference
    /// for orienting ratio presets to the image, not the current rect.
    private(set) var displaySize: CGSize = .zero

    func begin(
        displaySize: CGSize,
        rect seedRect: CGRect? = nil,
        tiltDegrees seedTilt: Double? = nil,
        preset seedPreset: CropPreset? = nil
    ) {
        guard displaySize.width > 0, displaySize.height > 0 else { return }
        self.displaySize = displaySize
        if let seedPreset { preset = seedPreset }
        if let seedRect, seedRect.width > 0, seedRect.height > 0 {
            rect = CropGeometry.clampFrame(seedRect, in: displaySize)
            tiltDegrees = seedTilt ?? 0
        } else {
            rect = CropGeometry.defaultRect(in: displaySize, ratio: orientedRatio)
            tiltDegrees = 0
        }
        isActive = true
    }

    func end() {
        isActive = false
        dragBaseRect = nil
    }

    /// The preset's width : height ratio oriented to the display image.
    /// Swap when the stored ratio's orientation (landscape when ≥ 1)
    /// disagrees with the image's, so 35mm and 6×7 both follow the frame.
    var orientedRatio: Double? {
        guard let ratio = preset.ratio else { return nil }
        let displayIsLandscape = displaySize.width >= displaySize.height
        let ratioIsLandscape = ratio >= 1.0
        return displayIsLandscape == ratioIsLandscape ? ratio : 1.0 / ratio
    }

    /// Re-shapes the current rect to the newly-chosen preset's ratio,
    /// keeping its centre — the picker's immediate feedback. The result
    /// stays inside the previous crop box.
    func applyPreset() {
        guard orientedRatio != nil else { return }
        guard rect.width > 0, rect.height > 0 else { return }
        let previous = rect
        rect = CropGeometry.maxInscribedRect(
            in: previous.size, ratio: orientedRatio, origin: previous.origin
        )
    }

    /// Resets the session's rect to the full image and clears the ratio —
    /// "Original". A local edit like the ratio picker; nothing is recorded
    /// until Apply.
    func resetToOriginal() {
        preset = .free
        rect = CropGeometry.maxInscribedRect(in: displaySize, ratio: nil)
    }
}

/// Pure geometry for the crop overlay — display-space rects and points,
/// the same space the `crop` op's `--x/--y/--width/--height` name. All
/// functions clamp into `bounds` and hold the 16px floor the CLI
/// validates (`repo.CROP_MIN_SIZE_PX`).
enum CropGeometry {
    static let minSize: CGFloat = 16

    /// A centred default rect as large as possible in the image, shaped to
    /// `ratio` (already oriented to the image), clamped to the bounds.
    static func defaultRect(in bounds: CGSize, ratio: Double?) -> CGRect {
        maxInscribedRect(in: bounds, ratio: ratio)
    }

    /// Largest axis-aligned rect with optional `ratio` that fits in `size`,
    /// centred in a `(0, 0)`-origin box of `size` unless `origin` is set.
    static func maxInscribedRect(
        in size: CGSize, ratio: Double?, origin: CGPoint = .zero
    ) -> CGRect {
        var width = size.width
        var height = size.height
        if let ratio {
            if width / height > ratio {
                width = height * ratio
            } else {
                height = width / ratio
            }
        }
        width = min(max(width, min(minSize, size.width)), size.width)
        height = min(max(height, min(minSize, size.height)), size.height)
        return CGRect(
            x: origin.x + (size.width - width) / 2,
            y: origin.y + (size.height - height) / 2,
            width: width,
            height: height
        )
    }

    /// Translates `rect` by `delta`, clamped so the frame stays inside
    /// the bounds.
    static func translated(
        _ rect: CGRect, by delta: CGSize, in bounds: CGSize
    ) -> CGRect {
        let x = min(max(rect.minX + delta.width, 0), bounds.width - rect.width)
        let y = min(max(rect.minY + delta.height, 0), bounds.height - rect.height)
        return CGRect(origin: CGPoint(x: x, y: y), size: rect.size)
    }

    /// Resizes `rect` by dragging `handle` to `point` (display space),
    /// anchoring the opposite side. Corners use the dominant dragged axis;
    /// edges keep the opposite edge fixed on the dragged axis and centre
    /// the coupled axis unless a bound forces a slide.
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
            if let ratio {
                return verticalEdgeResized(
                    rect, handle: handle, to: point, ratio: ratio, in: bounds
                )
            }
            let originY: CGFloat
            let height: CGFloat
            if handle == .top {
                let top = min(max(point.y, 0), rect.maxY - minSize)
                originY = top
                height = rect.maxY - top
            } else {
                let bottom = min(max(point.y, rect.minY + minSize), bounds.height)
                originY = rect.minY
                height = bottom - rect.minY
            }
            return clamped(
                CGRect(x: rect.minX, y: originY, width: rect.width, height: height),
                in: bounds
            )
        case .left, .right:
            if let ratio {
                return horizontalEdgeResized(
                    rect, handle: handle, to: point, ratio: ratio, in: bounds
                )
            }
            let originX: CGFloat
            let width: CGFloat
            if handle == .left {
                let left = min(max(point.x, 0), rect.maxX - minSize)
                originX = left
                width = rect.maxX - left
            } else {
                let right = min(max(point.x, rect.minX + minSize), bounds.width)
                originX = rect.minX
                width = right - rect.minX
            }
            return clamped(
                CGRect(x: originX, y: rect.minY, width: width, height: rect.height),
                in: bounds
            )
        }
    }

    /// Top/bottom handle with ratio lock: the dragged axis anchors the
    /// opposite edge; width follows height and stays centred unless a bound
    /// blocks one side (then the crop grows only on the free side).
    private static func verticalEdgeResized(
        _ rect: CGRect,
        handle: Handle,
        to point: CGPoint,
        ratio: Double,
        in bounds: CGSize
    ) -> CGRect {
        let originY: CGFloat
        let height: CGFloat
        if handle == .top {
            let top = min(max(point.y, 0), rect.maxY - minSize)
            originY = top
            height = rect.maxY - top
        } else {
            let bottom = min(max(point.y, rect.minY + minSize), bounds.height)
            originY = rect.minY
            height = bottom - rect.minY
        }

        var width = max(CGFloat(ratio) * height, minSize)
        var finalHeight = height
        var finalOriginY = originY

        if width > bounds.width {
            width = bounds.width
            finalHeight = max(width / CGFloat(ratio), minSize)
            finalOriginY = handle == .top
                ? rect.maxY - finalHeight
                : rect.minY
        }

        var originX = rect.midX - width / 2
        originX = min(max(originX, 0), bounds.width - width)

        return clamped(
            CGRect(x: originX, y: finalOriginY, width: width, height: finalHeight),
            in: bounds
        )
    }

    /// Left/right handle with ratio lock: the dragged axis anchors the
    /// opposite edge; height follows width and stays centred unless a bound
    /// blocks one side (then the crop grows only on the free side).
    private static func horizontalEdgeResized(
        _ rect: CGRect,
        handle: Handle,
        to point: CGPoint,
        ratio: Double,
        in bounds: CGSize
    ) -> CGRect {
        let originX: CGFloat
        let width: CGFloat
        if handle == .left {
            let left = min(max(point.x, 0), rect.maxX - minSize)
            originX = left
            width = rect.maxX - left
        } else {
            let right = min(max(point.x, rect.minX + minSize), bounds.width)
            originX = rect.minX
            width = right - rect.minX
        }

        var height = max(width / CGFloat(ratio), minSize)
        var finalWidth = width
        var finalOriginX = originX

        if height > bounds.height {
            height = bounds.height
            finalWidth = max(height * CGFloat(ratio), minSize)
            finalOriginX = handle == .left
                ? rect.maxX - finalWidth
                : rect.minX
        }

        var originY = rect.midY - height / 2
        originY = min(max(originY, 0), bounds.height - height)

        return clamped(
            CGRect(x: finalOriginX, y: originY, width: finalWidth, height: height),
            in: bounds
        )
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
                dy = (dy >= 0 ? 1 : -1) * max(abs(dx) / ratio, minSize)
            } else {
                dx = (dx >= 0 ? 1 : -1) * max(abs(dy) * ratio, minSize)
            }
        }
        let width = max(abs(dx), minSize)
        let height = max(abs(dy), minSize)
        let x = dx >= 0 ? anchor.x : anchor.x - width
        let y = dy >= 0 ? anchor.y : anchor.y - height
        return clamped(CGRect(x: x, y: y, width: width, height: height), in: bounds)
    }

    /// Keeps `frame` inside the bounds, capping its size when needed — the
    /// resize and preset math above can push an edge past them.
    static func clampFrame(_ frame: CGRect, in bounds: CGSize) -> CGRect {
        clamped(frame, in: bounds)
    }

    private static func clamped(_ frame: CGRect, in bounds: CGSize) -> CGRect {
        let width = min(frame.width, bounds.width)
        let height = min(frame.height, bounds.height)
        let x = min(max(frame.minX, 0), bounds.width - width)
        let y = min(max(frame.minY, 0), bounds.height - height)
        return CGRect(x: x, y: y, width: width, height: height)
    }

    enum Handle: CaseIterable {
        case topLeft, top, topRight, right, bottomRight, bottom, bottomLeft, left

        /// Edge handles first, corners last — corners sit above edges in the
        /// overlay so a click on a corner hits the corner, not the edge.
        static let overlayOrder: [Handle] = [
            .top, .right, .bottom, .left,
            .topLeft, .topRight, .bottomRight, .bottomLeft,
        ]

        /// The handle's position on the axis-aligned crop frame.
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

        /// The resize cursor conventional for this handle.
        @MainActor
        var resizeCursor: NSCursor {
            if #available(macOS 15.0, *) {
                switch self {
                case .topLeft:
                    return .frameResize(position: .topLeft, directions: [.outward])
                case .top:
                    return .frameResize(position: .top, directions: [.outward])
                case .topRight:
                    return .frameResize(position: .topRight, directions: [.outward])
                case .right:
                    return .frameResize(position: .right, directions: [.outward])
                case .bottomRight:
                    return .frameResize(position: .bottomRight, directions: [.outward])
                case .bottom:
                    return .frameResize(position: .bottom, directions: [.outward])
                case .bottomLeft:
                    return .frameResize(position: .bottomLeft, directions: [.outward])
                case .left:
                    return .frameResize(position: .left, directions: [.outward])
                }
            }
            switch self {
            case .top: return .resizeUp
            case .bottom: return .resizeDown
            case .left: return .resizeLeft
            case .right: return .resizeRight
            case .topLeft, .bottomRight: return CropOverlayView.diagonalNWSECursor
            case .topRight, .bottomLeft: return CropOverlayView.diagonalNESWCursor
            }
        }
    }
}

/// The crop-mode overlay: a dark scrim outside the axis-aligned crop
/// window, its border, a move surface, and the resize handles. The preview
/// image rotates beneath; this overlay stays square to the screen. All
/// drawing happens in the fit rect's pane coordinates — the display-space
/// rect maps in through `fitRect` the same way the preview image does;
/// Apply sends display-space values to the CLI, so Swift converts nothing
/// on the way out either.
struct CropOverlayView: View {
    let session: CropSession
    let fitRect: CGRect
    let displaySize: CGSize

    @State private var isMoving = false
    @State private var resizingHandle: CropGeometry.Handle?
    @State private var hoveredHandle: CropGeometry.Handle?
    @State private var isHoveringMove = false

    /// Hit target around each drawn handle — the visible marker is 10 pt.
    static let handleHitSize: CGFloat = 18

    fileprivate static let diagonalNWSECursor = Self.symbolCursor(
        "arrow.up.left.and.arrow.down.right", hotSpot: CGPoint(x: 8, y: 8)
    )
    fileprivate static let diagonalNESWCursor = Self.symbolCursor(
        "arrow.up.right.and.arrow.down.left", hotSpot: CGPoint(x: 8, y: 8)
    )

    private static func symbolCursor(
        _ symbol: String, hotSpot: CGPoint
    ) -> NSCursor {
        guard let image = NSImage(
            systemSymbolName: symbol,
            accessibilityDescription: nil
        ) else { return .crosshair }
        return NSCursor(
            image: image,
            hotSpot: NSPoint(x: hotSpot.x, y: hotSpot.y)
        )
    }

    private var scale: CGFloat {
        displaySize.width > 0 ? fitRect.width / displaySize.width : 1
    }

    private var frame: CGRect { session.rect }
    private var bounds: CGSize { displaySize }
    private var windowPath: Path {
        Path(paneRect(frame))
    }

    private func paneRect(_ display: CGRect) -> CGRect {
        CGRect(
            x: fitRect.minX + display.minX * scale,
            y: fitRect.minY + display.minY * scale,
            width: display.width * scale,
            height: display.height * scale
        )
    }

    private func panePoint(_ display: CGPoint) -> CGPoint {
        CGPoint(
            x: fitRect.minX + display.x * scale,
            y: fitRect.minY + display.y * scale
        )
    }

    /// Where a handle is drawn and hit-tested — on the axis-aligned border.
    private func handleCentre(_ handle: CropGeometry.Handle) -> CGPoint {
        panePoint(handle.point(on: frame))
    }

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .topLeading) {
                // The scrim: the whole fit rect minus the crop window,
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
                    .onContinuousHover { phase in
                        switch phase {
                        case .active:
                            isHoveringMove = true
                            syncCursor()
                        case .ended:
                            isHoveringMove = false
                            syncCursor()
                        }
                    }

                ForEach(CropGeometry.Handle.overlayOrder, id: \.self) { handle in
                    let centre = handleCentre(handle)
                    Circle()
                        .fill(Color.clear)
                        .frame(
                            width: Self.handleHitSize,
                            height: Self.handleHitSize
                        )
                        .overlay {
                            Circle()
                                .fill(Color.yellow)
                                .stroke(Color.black.opacity(0.6), lineWidth: 1)
                                .frame(width: 10, height: 10)
                        }
                        .contentShape(Circle())
                        .offset(
                            x: centre.x - Self.handleHitSize / 2,
                            y: centre.y - Self.handleHitSize / 2
                        )
                        .gesture(resizeGesture(handle))
                        .onContinuousHover { phase in
                            switch phase {
                            case .active:
                                hoveredHandle = handle
                                syncCursor()
                            case .ended:
                                if hoveredHandle == handle {
                                    hoveredHandle = nil
                                }
                                syncCursor()
                            }
                        }
                }
            }
            .frame(width: geo.size.width, height: geo.size.height, alignment: .topLeading)
            .coordinateSpace(name: "cropOverlay")
            .onChange(of: isMoving) { syncCursor() }
            .onChange(of: resizingHandle) { syncCursor() }
        }
        .allowsHitTesting(session.isActive)
    }

    private func syncCursor() {
        if isMoving {
            NSCursor.closedHand.set()
        } else if let resizingHandle {
            resizingHandle.resizeCursor.set()
        } else if let hoveredHandle {
            hoveredHandle.resizeCursor.set()
        } else if isHoveringMove {
            NSCursor.openHand.set()
        } else {
            NSCursor.arrow.set()
        }
    }

    private var moveGesture: some Gesture {
        DragGesture(minimumDistance: 1, coordinateSpace: .named("cropOverlay"))
            .onChanged { value in
                if session.dragBaseRect == nil {
                    session.dragBaseRect = frame
                    isMoving = true
                }
                session.rect = CropGeometry.translated(
                    session.dragBaseRect ?? frame,
                    by: CGSize(
                        width: value.translation.width / scale,
                        height: value.translation.height / scale
                    ),
                    in: bounds
                )
                syncCursor()
            }
            .onEnded { _ in
                session.dragBaseRect = nil
                isMoving = false
                syncCursor()
            }
    }

    private func resizeGesture(_ handle: CropGeometry.Handle) -> some Gesture {
        DragGesture(minimumDistance: 1, coordinateSpace: .named("cropOverlay"))
            .onChanged { value in
                if session.dragBaseRect == nil {
                    session.dragBaseRect = frame
                    resizingHandle = handle
                }
                let base = session.dragBaseRect ?? frame
                let start = handle.point(on: base)
                let dragged = CGPoint(
                    x: start.x + value.translation.width / scale,
                    y: start.y + value.translation.height / scale
                )
                session.rect = CropGeometry.resized(
                    base,
                    handle: handle,
                    to: dragged,
                    ratio: session.orientedRatio,
                    in: bounds
                )
                syncCursor()
            }
            .onEnded { _ in
                session.dragBaseRect = nil
                resizingHandle = nil
                syncCursor()
            }
    }
}
