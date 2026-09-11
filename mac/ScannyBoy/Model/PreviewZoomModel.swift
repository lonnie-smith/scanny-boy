import AppKit
import Foundation
import Observation

/// Zoom and pan state for the Edit tab's large preview (protocol version
/// 9's 1:1 zoom).
///
/// The preview scales to fit by default — that stays. Double-click toggles
/// fit ↔ 100%; zooming in anchors on the double-click location. At 100%,
/// plain click-and-drag pans. ⌘Z and the toolbar button also toggle zoom.
/// "100%" means one image pixel per physical screen pixel
/// against the underlying TIFF, so the pane shows exactly
/// `paneSize x displayScale` pixels of the stitched image, letterboxed when
/// the image is smaller than the pane.
///
/// At 100% the pane is a tile grid: each tile is one `edit render-region`
/// call for a fixed display-space rect. Already-drawn tiles stay on screen
/// while panning; only missing tiles are fetched.
@MainActor
@Observable
final class PreviewZoomModel {
    enum Mode: Equatable {
        case fit
        case pixels100
    }

    /// Grid address of one 1:1 tile in display-space pixels.
    struct TileKey: Hashable, Sendable {
        let x: Int
        let y: Int
    }

    /// One loaded tile: the CLI's pixels for `rect`.
    struct Tile: Equatable {
        let image: NSImage
        /// Display-space rect the image covers, in TIFF pixels.
        let rect: CGRect
        let displayScale: CGFloat
    }

    /// A tile ready to draw, with its grid key.
    struct RenderedTile: Identifiable {
        var id: TileKey { key }
        let key: TileKey
        let tile: Tile
    }

    /// Back-compat alias used by spot-marker tests.
    typealias Crop = Tile

    private(set) var mode: Mode = .fit

    /// Top-left corner of the displayed viewport, in display-space pixels.
    private(set) var origin: CGPoint = .zero
    /// Loaded 1:1 tiles keyed by grid origin.
    private(set) var tiles: [TileKey: Tile] = [:]
    /// Live pan translation while dragging at 100%, in points.
    private(set) var panOffset: CGSize = .zero

    /// A gesture with less total movement than this, in points, is a click.
    @ObservationIgnored static let clickTolerance: CGFloat = 3

    /// Fixed display-space extent of each 1:1 tile, in image pixels.
    @ObservationIgnored static let tileExtent: Int = 1024

    @ObservationIgnored private var requestGeneration = 0
    @ObservationIgnored private var inFlightTiles: Set<TileKey> = []
    @ObservationIgnored private var pendingRequired: Set<TileKey> = []
    @ObservationIgnored private var tileFetchTask: Task<Void, Never>?
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
        /// The viewport origin when the gesture started.
        let cropOriginAtStart: CGPoint
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

    /// Clamps a viewport origin so the viewport stays inside the display-space
    /// bounds. A viewport larger than its axis is centered instead of pinned.
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

    /// The screen rect (pane points, top-left origin) a display-space spot
    /// marker occupies in the fit view: the same mapping the image itself
    /// goes through.
    static func spotScreenRect(
        _ spotRect: CGRect, fitRect: CGRect, displaySize: CGSize
    ) -> CGRect {
        guard fitRect.width > 0, fitRect.height > 0,
            displaySize.width > 0, displaySize.height > 0
        else { return .zero }
        let scale = fitRect.width / displaySize.width
        return CGRect(
            x: fitRect.minX + spotRect.minX * scale,
            y: fitRect.minY + spotRect.minY * scale,
            width: spotRect.width * scale,
            height: spotRect.height * scale
        )
    }

    /// The screen rect a display-space spot marker occupies at 100% through
    /// the viewport origin, display scale, and live pan offset.
    static func spotScreenRect(
        _ spotRect: CGRect,
        viewportOrigin: CGPoint,
        panOffset: CGSize,
        displayScale: CGFloat,
        paneSize: CGSize,
        viewportSize: CGSize
    ) -> CGRect {
        let topLeft = displayPointToScreen(
            spotRect.origin,
            viewportOrigin: viewportOrigin,
            panOffset: panOffset,
            displayScale: displayScale,
            paneSize: paneSize,
            viewportSize: viewportSize
        )
        return CGRect(
            x: topLeft.width,
            y: topLeft.height,
            width: spotRect.width / displayScale,
            height: spotRect.height / displayScale
        )
    }

    /// Back-compat spot mapping through a single buffer tile.
    static func spotScreenRect(
        _ spotRect: CGRect, crop: Tile, cropScreenOffset: CGSize
    ) -> CGRect {
        CGRect(
            x: cropScreenOffset.width + (spotRect.minX - crop.rect.minX) / crop.displayScale,
            y: cropScreenOffset.height + (spotRect.minY - crop.rect.minY) / crop.displayScale,
            width: spotRect.width / crop.displayScale,
            height: spotRect.height / crop.displayScale
        )
    }

    /// The 1:1 viewport size for a pane of `paneSize` points: the pane's
    /// size in physical pixels, clamped against the image.
    static func viewportSize(
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

    /// Back-compat name for the viewport size helpers and tests.
    static func cropSize(
        paneSize: CGSize, displayScale: CGFloat, displaySize: CGSize
    ) -> CGSize {
        viewportSize(paneSize: paneSize, displayScale: displayScale, displaySize: displaySize)
    }

    /// Snaps a display coordinate to its tile grid origin.
    static func tileGridOrigin(for coordinate: CGFloat) -> Int {
        let clamped = max(0, Int(floor(coordinate)))
        return (clamped / tileExtent) * tileExtent
    }

    /// The display-space rect one tile covers, clamped to the image.
    static func tileRect(for key: TileKey, displaySize: CGSize) -> CGRect {
        let x = CGFloat(key.x)
        let y = CGFloat(key.y)
        let width = min(CGFloat(tileExtent), max(0, displaySize.width - x))
        let height = min(CGFloat(tileExtent), max(0, displaySize.height - y))
        return CGRect(x: x, y: y, width: width, height: height)
    }

    /// Tile keys whose rects intersect `rect`.
    static func tileKeys(intersecting rect: CGRect, displaySize: CGSize) -> [TileKey] {
        guard rect.width > 0, rect.height > 0, displaySize.width > 0, displaySize.height > 0 else {
            return []
        }
        let minX = tileGridOrigin(for: rect.minX)
        let minY = tileGridOrigin(for: rect.minY)
        let maxX = tileGridOrigin(for: max(0, rect.maxX - 1))
        let maxY = tileGridOrigin(for: max(0, rect.maxY - 1))
        var keys: [TileKey] = []
        for y in stride(from: minY, through: maxY, by: tileExtent) {
            for x in stride(from: minX, through: maxX, by: tileExtent) {
                let key = TileKey(x: x, y: y)
                let tile = tileRect(for: key, displaySize: displaySize)
                if tile.width > 0, tile.height > 0, tile.intersects(rect) {
                    keys.append(key)
                }
            }
        }
        return keys
    }

    /// Tile keys covering the viewport, plus one tile of lead margin when asked.
    static func requiredTileKeys(
        viewportOrigin: CGPoint,
        viewportSize: CGSize,
        displaySize: CGSize,
        includeLeadRing: Bool
    ) -> [TileKey] {
        let viewport = CGRect(origin: viewportOrigin, size: viewportSize)
        var keys = Set(tileKeys(intersecting: viewport, displaySize: displaySize))
        if includeLeadRing {
            let lead = viewport.insetBy(
                dx: -CGFloat(tileExtent), dy: -CGFloat(tileExtent)
            )
            keys.formUnion(tileKeys(intersecting: lead, displaySize: displaySize))
        }
        return keys.sorted {
            if $0.y != $1.y { return $0.y < $1.y }
            return $0.x < $1.x
        }
    }

    /// Whether a viewport origin and size fit entirely inside a buffer rect.
    static func viewportContained(
        in buffer: CGRect, viewportOrigin: CGPoint, viewportSize: CGSize
    ) -> Bool {
        buffer.contains(CGRect(origin: viewportOrigin, size: viewportSize))
    }

    /// Maps a display-space point to pane coordinates at 100%.
    static func displayPointToScreen(
        _ point: CGPoint,
        viewportOrigin: CGPoint,
        panOffset: CGSize,
        displayScale: CGFloat,
        paneSize: CGSize,
        viewportSize: CGSize
    ) -> CGSize {
        guard displayScale > 0 else { return .zero }
        let viewportWidth = CGFloat(viewportSize.width) / displayScale
        let viewportHeight = CGFloat(viewportSize.height) / displayScale
        let letterboxX = (paneSize.width - viewportWidth) / 2
        let letterboxY = (paneSize.height - viewportHeight) / 2
        let liveOrigin = CGPoint(
            x: viewportOrigin.x - panOffset.width * displayScale,
            y: viewportOrigin.y - panOffset.height * displayScale
        )
        return CGSize(
            width: letterboxX + (point.x - liveOrigin.x) / displayScale,
            height: letterboxY + (point.y - liveOrigin.y) / displayScale
        )
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

    /// The 1:1 viewport size the pane is showing now.
    private var liveViewportSize: CGSize {
        Self.viewportSize(
            paneSize: paneSize, displayScale: displayScale, displaySize: displaySize
        )
    }

    /// Tiles sorted for stable drawing order.
    var renderedTiles: [RenderedTile] {
        tiles.keys.sorted {
            if $0.y != $1.y { return $0.y < $1.y }
            return $0.x < $1.x
        }.compactMap { key in
            guard let tile = tiles[key] else { return nil }
            return RenderedTile(key: key, tile: tile)
        }
    }

    /// True while any viewport tile is still missing.
    var viewportIsLoading: Bool {
        guard mode == .pixels100 else { return false }
        let keys = Self.requiredTileKeys(
            viewportOrigin: liveViewportOrigin(),
            viewportSize: liveViewportSize,
            displaySize: displaySize,
            includeLeadRing: false
        )
        return !keys.allSatisfy { tiles[$0] != nil }
    }

    // MARK: - Input (called by the preview's AppKit event host)

    /// Remember where a pan gesture started.
    func mouseDown(at panePoint: CGPoint) {
        drag = Drag(
            start: panePoint,
            cropOriginAtStart: origin
        )
    }

    /// Translates the viewport live and prefetches missing tiles.
    func mouseDragged(to panePoint: CGPoint) {
        guard mode == .pixels100, let drag else { return }
        applyPan(from: drag, moved: moved(from: drag.start, to: panePoint))
        fetchTilesIfNeeded(for: liveViewportOrigin())
    }

    /// A near-motionless gesture is a click; a drag commits the origin and
    /// fetches any tiles the viewport still needs.
    func mouseUp(at panePoint: CGPoint) {
        let gesture = drag
        drag = nil
        guard let gesture else { return }
        let moved = moved(from: gesture.start, to: panePoint)
        let isClick = abs(moved.width) < Self.clickTolerance
            && abs(moved.height) < Self.clickTolerance
        if isClick {
            panOffset = .zero
            return
        }
        guard mode == .pixels100 else {
            panOffset = .zero
            return
        }
        let target = applyPan(from: gesture, moved: moved)
        origin = target
        panOffset = .zero
        fetchTilesIfNeeded(for: target)
    }

    private func moved(from start: CGPoint, to end: CGPoint) -> CGSize {
        CGSize(width: end.x - start.x, height: end.y - start.y)
    }

    /// Updates `panOffset` for a drag and returns the clamped target origin.
    @discardableResult
    private func applyPan(from gesture: Drag, moved: CGSize) -> CGPoint {
        let viewportSize = liveViewportSize
        let target = Self.clampOrigin(
            targetOrigin(from: gesture, moved: moved),
            cropSize: viewportSize,
            displaySize: displaySize
        )
        panOffset = CGSize(
            width: (gesture.cropOriginAtStart.x - target.x) / displayScale,
            height: (gesture.cropOriginAtStart.y - target.y) / displayScale
        )
        return target
    }

    /// The viewport origin currently on screen: the committed origin while
    /// at rest, or the live drag target while `panOffset` is non-zero.
    private func liveViewportOrigin() -> CGPoint {
        CGPoint(
            x: origin.x - panOffset.width * displayScale,
            y: origin.y - panOffset.height * displayScale
        )
    }

    /// The viewport origin a drag asks for, before clamping.
    private func targetOrigin(from gesture: Drag, moved: CGSize) -> CGPoint {
        // Grab-and-drag: dragging right moves the image right, so the origin
        // moves opposite the cursor in display pixels.
        CGPoint(
            x: gesture.cropOriginAtStart.x - moved.width * displayScale,
            y: gesture.cropOriginAtStart.y - moved.height * displayScale
        )
    }

    /// Fit → 100% at `panePoint`. Zooming in anchors the viewport on the
    /// pixel the user double-clicked, Lightroom-style.
    func zoomIn(at panePoint: CGPoint) {
        guard mode == .fit, displaySize.width > 0 else { return }
        mode = .pixels100
        let viewportSize = Self.viewportSize(
            paneSize: paneSize, displayScale: displayScale, displaySize: displaySize
        )
        let hit = Self.displayPoint(
            for: panePoint,
            fitRect: Self.fitRect(displaySize: displaySize, container: paneSize),
            displaySize: displaySize
        )
        origin = Self.clampOrigin(
            CGPoint(x: hit.x - viewportSize.width / 2, y: hit.y - viewportSize.height / 2),
            cropSize: viewportSize,
            displaySize: displaySize
        )
        fetchCrop()
    }

    /// 100% → fit for the Z shortcut and toolbar button.
    func zoomOut() {
        guard mode == .pixels100 else { return }
        discardPendingTiles()
        mode = .fit
        tiles = [:]
        panOffset = .zero
        origin = .zero
        drag = nil
    }

    /// Fit ↔ 100% for tests and callers that still use a single toggle.
    func toggle(at panePoint: CGPoint) {
        switch mode {
        case .fit:
            zoomIn(at: panePoint)
        case .pixels100:
            zoomOut()
        }
    }

    /// Forgets everything: the negative or its rendering changed, so the
    /// view restarts from the fit view.
    func reset() {
        discardPendingTiles()
        mode = .fit
        tiles = [:]
        panOffset = .zero
        origin = .zero
        drag = nil
    }

    // MARK: - Fetching

    /// Fetches tiles covering the current viewport plus a one-tile lead ring.
    func fetchCrop() {
        fetchTilesIfNeeded(for: origin)
    }

    private func fetchTilesIfNeeded(for viewportOrigin: CGPoint) {
        guard mode == .pixels100, displaySize.width > 0, displayScale > 0, loader != nil else {
            return
        }
        let viewportSize = liveViewportSize
        let requested = Self.clampOrigin(
            viewportOrigin, cropSize: viewportSize, displaySize: displaySize
        )
        let required = Set(
            Self.requiredTileKeys(
                viewportOrigin: requested,
                viewportSize: viewportSize,
                displaySize: displaySize,
                includeLeadRing: true
            )
        )
        pendingRequired = required
        let missing = required.filter { tiles[$0] == nil && !inFlightTiles.contains($0) }
        guard !missing.isEmpty else { return }
        ensureTileFetchLoop()
    }

    private func ensureTileFetchLoop() {
        guard tileFetchTask == nil, loader != nil else { return }
        requestGeneration += 1
        let generation = requestGeneration
        tileFetchTask = Task { [weak self] in
            await self?.runTileFetchLoop(generation: generation)
        }
    }

    private func runTileFetchLoop(generation: Int) async {
        guard let loader else { return }
        defer { tileFetchTask = nil }
        while !Task.isCancelled, self.requestGeneration == generation, mode == .pixels100 {
            let viewportSize = liveViewportSize
            let liveOrigin = Self.clampOrigin(
                liveViewportOrigin(),
                cropSize: viewportSize,
                displaySize: displaySize
            )
            pendingRequired = Set(
                Self.requiredTileKeys(
                    viewportOrigin: liveOrigin,
                    viewportSize: viewportSize,
                    displaySize: displaySize,
                    includeLeadRing: true
                )
            )
            let missing = pendingRequired
                .filter { tiles[$0] == nil && !inFlightTiles.contains($0) }
                .sorted(by: tileFetchPriority(viewportOrigin: liveOrigin, viewportSize: viewportSize))
            guard let key = missing.first else { return }
            inFlightTiles.insert(key)
            let rect = Self.tileRect(for: key, displaySize: displaySize)
            let thumbnail = await loader(rect)
            inFlightTiles.remove(key)
            guard !Task.isCancelled, self.requestGeneration == generation, mode == .pixels100,
                pendingRequired.contains(key), let thumbnail
            else { continue }
            tiles[key] = Tile(
                image: thumbnail.image, rect: rect, displayScale: displayScale
            )
        }
    }

    private func tileFetchPriority(
        viewportOrigin: CGPoint, viewportSize: CGSize
    ) -> (TileKey, TileKey) -> Bool {
        let viewport = CGRect(origin: viewportOrigin, size: viewportSize)
        func priority(_ key: TileKey) -> Int {
            let rect = Self.tileRect(for: key, displaySize: displaySize)
            return viewport.intersects(rect) ? 0 : 1
        }
        return { lhs, rhs in
            let left = priority(lhs)
            let right = priority(rhs)
            if left != right { return left < right }
            if lhs.y != rhs.y { return lhs.y < rhs.y }
            return lhs.x < rhs.x
        }
    }

    /// Waits for in-flight tile fetches. Test-only.
    func waitForCropForTesting() async {
        while inFlightTiles.isEmpty == false || tileFetchTask != nil {
            if let task = tileFetchTask {
                await task.value
            } else {
                await Task.yield()
            }
        }
    }

    /// Forgets stale tiles without dropping out of 100% view.
    func invalidate() {
        discardPendingTiles()
        tiles = [:]
        panOffset = .zero
    }

    private func discardPendingTiles() {
        tileFetchTask?.cancel()
        tileFetchTask = nil
        inFlightTiles = []
        pendingRequired = []
        requestGeneration += 1
    }

    // MARK: - Drawing

    /// Screen offset for one tile's top-left corner in pane points.
    func tileScreenOffset(for tileRect: CGRect) -> CGSize {
        Self.displayPointToScreen(
            tileRect.origin,
            viewportOrigin: origin,
            panOffset: panOffset,
            displayScale: displayScale,
            paneSize: paneSize,
            viewportSize: liveViewportSize
        )
    }
}
