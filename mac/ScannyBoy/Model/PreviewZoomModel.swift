import AppKit
import Foundation
import Observation

/// Zoom and pan state for the Edit tab's large preview (protocol version
/// 9's 1:1 zoom).
///
/// The preview scales to fit by default — that stays. Holding space turns
/// the cursor into a magnifier, space+click toggles between fit view and
/// 100% view, and space+drag pans. "100%" means one image pixel per
/// physical screen pixel against the underlying TIFF, so the pane shows
/// exactly `paneSize x displayScale` pixels of the stitched image,
/// letterboxed when the image is smaller than the pane.
///
/// The pixels on screen are always the CLI's: `edit render-region` renders
/// the display-space crop — net rotation folded in, the same inverted
/// display encode as the cached preview — into a caller-named PNG, and this
/// model only decides which region to ask for and how to draw what came
/// back. Panning translates the previous crop live and refetches on
/// release, so a drag never blocks on a helper round trip.
@MainActor
@Observable
final class PreviewZoomModel {
    enum Mode: Equatable {
        case fit
        case pixels100
    }

    /// One on-screen crop: the pixels the CLI rendered for `rect`, sized
    /// against the `displayScale` it was fetched for.
    struct Crop {
        let image: NSImage
        /// Display-space rect the image covers, in TIFF pixels.
        let rect: CGRect
        let displayScale: CGFloat
    }

    private(set) var mode: Mode = .fit
    /// Spacebar state, published so the host view can pick the cursor.
    private(set) var spaceHeld = false

    /// Top-left corner of the displayed crop, in display-space pixels.
    private(set) var origin: CGPoint = .zero
    /// The crop currently on screen, if any.
    private(set) var crop: Crop?
    /// Live pan translation while space+dragging, in points.
    private(set) var panOffset: CGSize = .zero

    /// A gesture with less total movement than this, in points, is a click.
    @ObservationIgnored static let clickTolerance: CGFloat = 3

    @ObservationIgnored private var requestGeneration = 0
    @ObservationIgnored private var inFlightKey: String?
    @ObservationIgnored private var cropTask: Task<Void, Never>?
    @ObservationIgnored private var drag: Drag?
    @ObservationIgnored private var context = Context()
    @ObservationIgnored private var loader: (@MainActor @Sendable (CGRect) async -> Thumbnail?)?

    /// The pane's inputs, refreshed by the view whenever they change.
    struct Context {
        var paneSize: CGSize = .zero
        var displayScale: CGFloat = 1
        /// The display-space size of the current image, or `.zero` when
        /// unknown (no stitched output yet).
        var displaySize: CGSize = .zero
    }

    private struct Drag {
        /// The pane point the gesture started at.
        let start: CGPoint
        /// The crop origin the view had when the gesture started.
        let originAtStart: CGPoint
        var total: CGSize = .zero
    }

    // MARK: - Geometry (pure, unit-tested)

    /// The stitched image's display-space size: the published TIFF's
    /// dimensions with the net rotation folded in — odd net turns swap the
    /// axes, exactly as `generate_preview` does.
    static func displaySize(tiffSize: CGSize, quarterTurns: Int) -> CGSize {
        quarterTurns % 2 == 0
            ? CGSize(width: tiffSize.width, height: tiffSize.height)
            : CGSize(width: tiffSize.height, height: tiffSize.width)
    }

    /// The aspect-fit rect of `displaySize` inside `container`, top-left
    /// coordinates, in points.
    static func fitRect(displaySize: CGSize, container: CGSize) -> CGRect {
        guard displaySize.width > 0, displaySize.height > 0,
            container.width > 0, container.height > 0
        else { return .zero }
        let scale = min(container.width / displaySize.width, container.height / displaySize.height)
        let size = CGSize(width: displaySize.width * scale, height: displaySize.height * scale)
        return CGRect(
            x: (container.width - size.width) / 2,
            y: (container.height - size.height) / 2,
            width: size.width,
            height: size.height
        )
    }

    /// The display-space pixel a point in the fit view hits, clamped into
    /// the image bounds.
    static func displayPoint(
        for panePoint: CGPoint, fitRect: CGRect, displaySize: CGSize
    ) -> CGPoint {
        guard fitRect.width > 0, fitRect.height > 0,
            displaySize.width > 0, displaySize.height > 0
        else { return .zero }
        let point = CGPoint(
            x: (panePoint.x - fitRect.minX) / fitRect.width * displaySize.width,
            y: (panePoint.y - fitRect.minY) / fitRect.height * displaySize.height
        )
        return CGPoint(
            x: min(max(point.x, 0), displaySize.width),
            y: min(max(point.y, 0), displaySize.height)
        )
    }

    /// Clamps a crop origin so the crop stays inside the display-space
    /// bounds. A crop larger than its axis is centered instead of pinned.
    static func clampOrigin(_ origin: CGPoint, cropSize: CGSize, displaySize: CGSize) -> CGPoint {
        let x: CGFloat
        if cropSize.width >= displaySize.width {
            x = (displaySize.width - cropSize.width) / 2
        } else {
            x = min(max(origin.x, 0), displaySize.width - cropSize.width)
        }
        let y: CGFloat
        if cropSize.height >= displaySize.height {
            y = (displaySize.height - cropSize.height) / 2
        } else {
            y = min(max(origin.y, 0), displaySize.height - cropSize.height)
        }
        return CGPoint(x: x, y: y)
    }

    /// The 1:1 crop size to fetch for a pane of `paneSize` points: the
    /// pane's size in physical pixels, clamped against the image.
    static func cropSize(
        paneSize: CGSize, displayScale: CGFloat, displaySize: CGSize
    ) -> CGSize {
        guard displayScale > 0 else { return CGSize(width: 1, height: 1) }
        let width = max(1, min(
            Int((paneSize.width * displayScale).rounded(.up)),
            Int(displaySize.width)
        ))
        let height = max(1, min(
            Int((paneSize.height * displayScale).rounded(.up)),
            Int(displaySize.height)
        ))
        return CGSize(width: width, height: height)
    }

    // MARK: - Context

    /// Refreshes the pane's inputs. Call whenever the pane resizes, the
    /// display scale changes, or the selected negative changes.
    func update(
        paneSize: CGSize, displayScale: CGFloat, displaySize: CGSize,
        loader: (@MainActor @Sendable (CGRect) async -> Thumbnail?)?
    ) {
        self.context.paneSize = paneSize
        self.context.displayScale = displayScale
        self.context.displaySize = displaySize
        self.loader = loader
    }

    private var paneSize: CGSize { context.paneSize }
    private var displayScale: CGFloat { context.displayScale }
    private var displaySize: CGSize { context.displaySize }

    // MARK: - Input (called by the preview's AppKit event host)

    func spaceDown() {
        spaceHeld = true
    }

    func spaceUp() {
        spaceHeld = false
    }

    /// Space+mouseDown: remember where the gesture started. The behaviour —
    /// toggle or pan — is decided at `mouseUp`, from how far it moved.
    func mouseDown(at panePoint: CGPoint) {
        drag = Drag(start: panePoint, originAtStart: origin)
    }

    /// Translates the on-screen crop live, following the drag 1:1 until the
    /// clamped target origin hits the image's edge. No-op when the image
    /// fits the pane at 1:1.
    func mouseDragged(to panePoint: CGPoint) {
        guard mode == .pixels100, let drag, let crop else { return }
        let moved = CGSize(
            width: panePoint.x - drag.start.x,
            height: panePoint.y - drag.start.y
        )
        self.drag?.total = moved
        let target = Self.clampOrigin(
            Self.targetOrigin(from: drag, moved: moved, crop: crop),
            cropSize: crop.rect.size,
            displaySize: displaySize
        )
        // The image's top-left rides the mouse, in points.
        panOffset = CGSize(
            width: (drag.originAtStart.x - target.x) / crop.displayScale,
            height: (drag.originAtStart.y - target.y) / crop.displayScale
        )
    }

    /// Space+mouseUp. A near-motionless gesture toggles the zoom, anchored
    /// where the user clicked; a drag commits the clamped target origin and
    /// refetches, snapping onto it when the pixels arrive.
    func mouseUp(at panePoint: CGPoint) {
        let gesture = drag
        drag = nil
        guard let gesture else { return }
        let isClick = abs(gesture.total.width) < Self.clickTolerance
            && abs(gesture.total.height) < Self.clickTolerance
        if isClick {
            panOffset = .zero
            toggle(at: panePoint)
            return
        }
        guard mode == .pixels100, let crop else {
            panOffset = .zero
            return
        }
        let target = Self.clampOrigin(
            Self.targetOrigin(from: gesture, moved: gesture.total, crop: crop),
            cropSize: crop.rect.size,
            displaySize: displaySize
        )
        origin = target
        // The crop stays where the user left it, translated, until the new
        // pixels arrive and snap the view onto the new origin.
        panOffset = CGSize(
            width: (gesture.originAtStart.x - target.x) / crop.displayScale,
            height: (gesture.originAtStart.y - target.y) / crop.displayScale
        )
        fetchCrop()
    }

    /// The crop origin a drag asks for, before clamping: dragging right
    /// moves the image right, so the origin moves left — in display pixels,
    /// which are `crop.displayScale` per point.
    private static func targetOrigin(
        from gesture: Drag, moved: CGSize, crop: Crop
    ) -> CGPoint {
        CGPoint(
            x: gesture.originAtStart.x - moved.width * crop.displayScale,
            y: gesture.originAtStart.y - moved.height * crop.displayScale
        )
    }

    /// Fit ↔ 100% for a space+click at `panePoint`. Zooming in anchors the
    /// 1:1 crop on the pixel the user clicked, Lightroom-style.
    func toggle(at panePoint: CGPoint) {
        switch mode {
        case .fit:
            guard displaySize.width > 0 else { return }
            mode = .pixels100
            let size = Self.cropSize(
                paneSize: paneSize, displayScale: displayScale, displaySize: displaySize
            )
            let hit = Self.displayPoint(
                for: panePoint,
                fitRect: Self.fitRect(displaySize: displaySize, container: paneSize),
                displaySize: displaySize
            )
            origin = Self.clampOrigin(
                CGPoint(x: hit.x - size.width / 2, y: hit.y - size.height / 2),
                cropSize: size,
                displaySize: displaySize
            )
            fetchCrop()
        case .pixels100:
            mode = .fit
            crop = nil
            panOffset = .zero
            origin = .zero
        }
    }

    /// Forgets everything: the negative or its rendering changed, so the
    /// view restarts from the fit view.
    func reset() {
        mode = .fit
        crop = nil
        panOffset = .zero
        origin = .zero
        drag = nil
        requestGeneration += 1
        inFlightKey = nil
    }

    // MARK: - Fetching

    /// Fetches the crop the current `origin` calls for, if it is not
    /// already on screen or in flight. The previous crop stays on screen,
    /// translated, until the new pixels arrive.
    func fetchCrop() {
        guard mode == .pixels100, displaySize.width > 0, let loader else { return }
        let size = Self.cropSize(
            paneSize: paneSize, displayScale: displayScale, displaySize: displaySize
        )
        let requested = Self.clampOrigin(origin, cropSize: size, displaySize: displaySize)
        origin = requested
        let rect = CGRect(
            x: requested.x, y: requested.y,
            width: size.width, height: size.height
        )
        let key = "\(Int(rect.minX)),\(Int(rect.minY)),\(Int(rect.width)),\(Int(rect.height))"
        if crop?.rect == rect, crop?.displayScale == displayScale {
            return
        }
        if inFlightKey == key {
            return
        }
        requestGeneration += 1
        let generation = requestGeneration
        inFlightKey = key
        cropTask = Task { [weak self] in
            let thumbnail = await loader(rect)
            guard let self, self.requestGeneration == generation else { return }
            self.inFlightKey = nil
            guard let thumbnail else { return }
            self.crop = Crop(
                image: thumbnail.image,
                rect: rect,
                displayScale: displayScale
            )
            self.origin = rect.origin
            self.panOffset = .zero
        }
    }

    /// Waits for any crop fetch currently in flight. Test-only: production
    /// code drives everything from `@Observable`'s change notifications.
    func waitForCropForTesting() async {
        await cropTask?.value
        cropTask = nil
    }

    /// Forgets a stale crop without dropping out of 100% view — the crop
    /// rendering changed (rotation, re-stitch) but the pane did not.
    func invalidate() {
        crop = nil
        panOffset = .zero
        requestGeneration += 1
        inFlightKey = nil
    }

    // MARK: - Drawing

    /// The displayed crop's top-left relative to the pane's top-left, in
    /// points: the centering offset when the image is smaller than the
    /// pane, plus the live pan translation.
    var cropScreenOffset: CGSize {
        guard let crop else { return .zero }
        let contentWidth = CGFloat(crop.rect.width) / crop.displayScale
        let contentHeight = CGFloat(crop.rect.height) / crop.displayScale
        return CGSize(
            width: (paneSize.width - contentWidth) / 2 + panOffset.width,
            height: (paneSize.height - contentHeight) / 2 + panOffset.height
        )
    }
}
