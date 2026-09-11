import AppKit
import Foundation
import Testing

@testable import ScannyBoy

/// The 1:1 zoom's geometry and gesture state machine: which tiles the pane
/// asks the CLI for, how fit-view clicks anchor the viewport, and how a drag
/// pans. Pure math and main-actor state — no helper runs here.
@Suite("Preview zoom model")
@MainActor
struct PreviewZoomModelTests {
    /// The published TIFF's pixel size (2000x1000), the pane's point size,
    /// and a 2x display scale: the constants every test reasons about.
    private static let tiffSize = CGSize(width: 2000, height: 1000)
    private static let paneSize = CGSize(width: 500, height: 400)
    private static let scale: CGFloat = 2
    /// The pane-sized 1:1 viewport: 500x400 points at 2x.
    private static let viewportPixelSize = CGSize(width: 1000, height: 800)

    @Test("display size swaps axes for odd net turns")
    func displaySize() {
        #expect(
            PreviewZoomModel.displaySize(tiffSize: Self.tiffSize, quarterTurns: 0)
                == Self.tiffSize
        )
        #expect(
            PreviewZoomModel.displaySize(tiffSize: Self.tiffSize, quarterTurns: 2)
                == Self.tiffSize
        )
        #expect(
            PreviewZoomModel.displaySize(tiffSize: Self.tiffSize, quarterTurns: 1)
                == CGSize(width: 1000, height: 2000)
        )
        #expect(
            PreviewZoomModel.displaySize(tiffSize: Self.tiffSize, quarterTurns: 3)
                == CGSize(width: 1000, height: 2000)
        )
    }

    @Test("fit rect centers the image in the container")
    func fitRectCenters() {
        let rect = PreviewZoomModel.fitRect(
            displaySize: Self.tiffSize, container: Self.paneSize
        )
        // 2000x1000 fits into 500x400 at 1:4.
        #expect(rect.width == 500)
        #expect(rect.height == 250)
        #expect(rect.minX == 0)
        #expect(rect.minY == 75)
    }

    @Test("display point maps the fit view into image pixels, clamped")
    func displayPointMapsIntoTheImage() {
        let fit = PreviewZoomModel.fitRect(
            displaySize: Self.tiffSize, container: Self.paneSize
        )
        let topLeft = PreviewZoomModel.displayPoint(
            for: CGPoint(x: 0, y: 0), fitRect: fit, displaySize: Self.tiffSize
        )
        #expect(topLeft == .zero)

        let centre = PreviewZoomModel.displayPoint(
            for: CGPoint(x: 250, y: 200), fitRect: fit, displaySize: Self.tiffSize
        )
        #expect(centre == CGPoint(x: 1000, y: 500))

        let clamped = PreviewZoomModel.displayPoint(
            for: CGPoint(x: 250, y: 5), fitRect: fit, displaySize: Self.tiffSize
        )
        #expect(clamped.y == 0)
    }

    @Test("viewport size is the pane's physical pixel size, clamped to the image")
    func viewportSizeFollowsPhysicalPixels() {
        let size = PreviewZoomModel.viewportSize(
            paneSize: Self.paneSize, displayScale: 2, displaySize: Self.tiffSize
        )
        #expect(size == CGSize(width: 1000, height: 800))

        let small = PreviewZoomModel.viewportSize(
            paneSize: Self.paneSize,
            displayScale: Self.scale,
            displaySize: CGSize(width: 400, height: 200)
        )
        #expect(small == CGSize(width: 400, height: 200))
    }

    @Test("required tiles cover the viewport plus a lead ring")
    func requiredTilesCoverViewportAndLead() {
        let origin = CGPoint(x: 500, y: 100)
        let viewportOnly = PreviewZoomModel.requiredTileKeys(
            viewportOrigin: origin,
            viewportSize: Self.viewportPixelSize,
            displaySize: Self.tiffSize,
            includeLeadRing: false
        )
        let withLead = PreviewZoomModel.requiredTileKeys(
            viewportOrigin: origin,
            viewportSize: Self.viewportPixelSize,
            displaySize: Self.tiffSize,
            includeLeadRing: true
        )
        #expect(withLead.count >= viewportOnly.count)
        #expect(withLead.contains(TileKey(x: 0, y: 0)))
        #expect(withLead.contains(TileKey(x: 1024, y: 0)))
    }

    @Test("tile rects are 1024 pixels except at image edges")
    func tileRectsClampAtEdges() {
        let full = PreviewZoomModel.tileRect(
            for: TileKey(x: 0, y: 0), displaySize: Self.tiffSize
        )
        #expect(full == CGRect(x: 0, y: 0, width: 1024, height: 1000))

        let edge = PreviewZoomModel.tileRect(
            for: TileKey(x: 1024, y: 0), displaySize: Self.tiffSize
        )
        #expect(edge == CGRect(x: 1024, y: 0, width: 976, height: 1000))
    }

    @Test("origins clamp against the display bounds, centered when larger")
    func clampOriginKeepsTheCropInside() {
        let centered = PreviewZoomModel.clampOrigin(
            CGPoint(x: -50, y: 0),
            cropSize: CGSize(width: 2000, height: 800),
            displaySize: Self.tiffSize
        )
        #expect(centered.x == 0)

        let pinned = PreviewZoomModel.clampOrigin(
            CGPoint(x: 1_500, y: -50),
            cropSize: CGSize(width: 1000, height: 800),
            displaySize: Self.tiffSize
        )
        #expect(pinned.x == 1000)
        #expect(pinned.y == 0)
    }

    @Test("zoomIn in fit view anchors on the clicked pixel")
    func zoomInAnchorsAtTheClick() async {
        let zoom = PreviewZoomModel()
        zoom.update(
            paneSize: Self.paneSize,
            displayScale: Self.scale,
            displaySize: Self.tiffSize,
            loader: { _ in Self.thumbnail() }
        )
        zoom.zoomIn(at: CGPoint(x: 250, y: 125))
        await zoom.waitForCropForTesting()

        #expect(zoom.mode == .pixels100)
        #expect(zoom.origin == CGPoint(x: 500, y: 0))
        let viewportKeys = PreviewZoomModel.requiredTileKeys(
            viewportOrigin: zoom.origin,
            viewportSize: Self.viewportPixelSize,
            displaySize: Self.tiffSize,
            includeLeadRing: false
        )
        #expect(viewportKeys.allSatisfy { zoom.tiles[$0] != nil })
    }

    @Test("Z toggles back out from 100%")
    func toggleZoomsBackOut() async {
        let zoom = await zoomedIn()
        zoom.toggle(at: CGPoint(x: 10, y: 10))

        #expect(zoom.mode == .fit)
        #expect(zoom.tiles.isEmpty)
    }

    @Test("zooming out ignores a tile fetch that finishes afterward")
    func zoomOutIgnoresLateCrop() async {
        let zoom = PreviewZoomModel()
        var releaseFetch: CheckedContinuation<Void, Never>?
        zoom.update(
            paneSize: Self.paneSize,
            displayScale: Self.scale,
            displaySize: Self.tiffSize,
            loader: { _ in
                await withCheckedContinuation { releaseFetch = $0 }
                return Self.thumbnail()
            }
        )
        zoom.toggle(at: CGPoint(x: 250, y: 125))
        zoom.toggle(at: CGPoint(x: 10, y: 10))
        #expect(zoom.mode == .fit)
        #expect(zoom.tiles.isEmpty)

        releaseFetch?.resume()
        await Task.yield()
        await Task.yield()

        #expect(zoom.mode == .fit)
        #expect(zoom.tiles.isEmpty)
    }

    @Test("a drag pans and refetches when the viewport leaves loaded tiles")
    func dragPans() async {
        let zoom = await zoomedIn()
        let before = zoom.origin
        zoom.mouseDown(at: CGPoint(x: 250, y: 200))
        zoom.mouseDragged(to: CGPoint(x: 100, y: 200))
        #expect(zoom.panOffset.width == -150)
        zoom.mouseUp(at: CGPoint(x: 100, y: 200))
        #expect(zoom.panOffset == .zero)

        #expect(zoom.mode == .pixels100)
        #expect(zoom.origin.x == 800)
        #expect(zoom.origin.y == before.y)
        await zoom.waitForCropForTesting()
        let viewportKeys = PreviewZoomModel.requiredTileKeys(
            viewportOrigin: zoom.origin,
            viewportSize: Self.viewportPixelSize,
            displaySize: Self.tiffSize,
            includeLeadRing: false
        )
        #expect(viewportKeys.allSatisfy { zoom.tiles[$0] != nil })
    }

    @Test("a small pan inside loaded tiles skips a refetch")
    func panInsideLoadedTilesSkipsFetch() async {
        var renders = 0
        let zoom = PreviewZoomModel()
        zoom.update(
            paneSize: Self.paneSize,
            displayScale: Self.scale,
            displaySize: Self.tiffSize,
            loader: { _ in
                renders += 1
                return Self.thumbnail()
            }
        )
        zoom.toggle(at: CGPoint(x: 250, y: 125))
        await zoom.waitForCropForTesting()
        let afterZoomIn = renders

        zoom.mouseDown(at: CGPoint(x: 250, y: 200))
        zoom.mouseUp(at: CGPoint(x: 200, y: 200))

        #expect(zoom.origin.x == 600)
        #expect(zoom.panOffset == .zero)
        #expect(renders == afterZoomIn)
    }

    @Test("mouseUp commits the release point, not the last drag event alone")
    func mouseUpUsesReleasePoint() async {
        let zoom = await zoomedIn()
        zoom.mouseDown(at: CGPoint(x: 250, y: 200))
        zoom.mouseDragged(to: CGPoint(x: 150, y: 200))
        zoom.mouseUp(at: CGPoint(x: 110, y: 200))

        #expect(zoom.origin.x == 780)
        #expect(zoom.panOffset == .zero)
    }

    @Test("dragging outside loaded tiles fetches only new tile keys")
    func dragOutsideTilesFetchesNewKeys() async {
        var fetchedRects: [CGRect] = []
        let largeDisplay = CGSize(width: 6000, height: 3000)
        let zoom = PreviewZoomModel()
        zoom.update(
            paneSize: Self.paneSize,
            displayScale: Self.scale,
            displaySize: largeDisplay,
            loader: { rect in
                fetchedRects.append(rect)
                return Self.thumbnail(size: rect.size)
            }
        )
        zoom.toggle(at: CGPoint(x: 250, y: 125))
        await zoom.waitForCropForTesting()
        let initialCount = fetchedRects.count
        #expect(initialCount > 0)

        zoom.mouseDown(at: CGPoint(x: 250, y: 200))
        zoom.mouseDragged(to: CGPoint(x: -200, y: 200))
        zoom.mouseUp(at: CGPoint(x: -200, y: 200))
        await zoom.waitForCropForTesting()

        #expect(fetchedRects.count > initialCount)
        for rect in fetchedRects[initialCount...] {
            #expect(rect.width <= CGFloat(PreviewZoomModel.tileExtent))
            #expect(rect.height <= CGFloat(PreviewZoomModel.tileExtent))
        }
        let viewportKeys = PreviewZoomModel.requiredTileKeys(
            viewportOrigin: zoom.origin,
            viewportSize: Self.viewportPixelSize,
            displaySize: largeDisplay,
            includeLeadRing: false
        )
        #expect(viewportKeys.allSatisfy { zoom.tiles[$0] != nil })
    }

    @Test("mouseUp does not shift the on-screen viewport")
    func mouseUpPreservesScreenMapping() async {
        let zoom = await zoomedIn()
        zoom.mouseDown(at: CGPoint(x: 250, y: 200))
        zoom.mouseDragged(to: CGPoint(x: 200, y: 200))
        let duringDrag = screenPoint(for: CGPoint(x: 600, y: 100), zoom: zoom)
        zoom.mouseUp(at: CGPoint(x: 200, y: 200))
        let afterRelease = screenPoint(for: CGPoint(x: 600, y: 100), zoom: zoom)
        #expect(duringDrag == afterRelease)
    }

    @Test("a drag wider than the image clamps and does not wrap")
    func dragClampsAtTheEdges() async {
        let zoom = await zoomedIn()
        zoom.mouseDown(at: CGPoint(x: 250, y: 200))
        zoom.mouseDragged(to: CGPoint(x: -5_000, y: 200))
        #expect(zoom.panOffset.width == -250)
        zoom.mouseUp(at: CGPoint(x: -5_000, y: 200))
        #expect(zoom.panOffset == .zero)

        #expect(zoom.mode == .pixels100)
        await zoom.waitForCropForTesting()
        #expect(zoom.origin.x == 1000)
    }

    @Test("a click at 100% does not zoom back out")
    func clickAt100DoesNotZoomOut() async {
        let zoom = await zoomedIn()
        zoom.mouseDown(at: CGPoint(x: 10, y: 10))
        zoom.mouseUp(at: CGPoint(x: 10, y: 10))

        #expect(zoom.mode == .pixels100)
        #expect(!zoom.tiles.isEmpty)
    }

    @Test("fetchCrop skips tiles already on screen")
    func fetchCropDeduplicates() async {
        var renders = 0
        let zoom = PreviewZoomModel()
        zoom.update(
            paneSize: Self.paneSize,
            displayScale: Self.scale,
            displaySize: Self.tiffSize,
            loader: { _ in
                renders += 1
                return Self.thumbnail()
            }
        )
        zoom.toggle(at: CGPoint(x: 250, y: 125))
        await zoom.waitForCropForTesting()
        let firstCount = renders
        #expect(firstCount > 0)

        zoom.fetchCrop()
        await zoom.waitForCropForTesting()
        #expect(renders == firstCount)
    }

    @Test("invalidate keeps 100% mode and drops tiles")
    func invalidateKeepsZoomLevel() async {
        let zoom = await zoomedIn()
        let origin = zoom.origin
        zoom.invalidate()
        #expect(zoom.mode == .pixels100)
        #expect(zoom.tiles.isEmpty)
        #expect(zoom.origin == origin)
    }

    // MARK: - Helpers

    private typealias TileKey = PreviewZoomModel.TileKey

    private func screenPoint(for displayPoint: CGPoint, zoom: PreviewZoomModel) -> CGSize {
        PreviewZoomModel.displayPointToScreen(
            displayPoint,
            viewportOrigin: zoom.origin,
            panOffset: zoom.panOffset,
            displayScale: Self.scale,
            paneSize: Self.paneSize,
            viewportSize: Self.viewportPixelSize
        )
    }

    private func zoomedIn() async -> PreviewZoomModel {
        let zoom = PreviewZoomModel()
        zoom.update(
            paneSize: Self.paneSize,
            displayScale: Self.scale,
            displaySize: Self.tiffSize,
            loader: { _ in Self.thumbnail() }
        )
        zoom.toggle(at: CGPoint(x: 250, y: 125))
        await zoom.waitForCropForTesting()
        return zoom
    }

    private static func thumbnail(size: CGSize = CGSize(width: 1024, height: 1024)) -> Thumbnail {
        let image = NSImage(size: NSSize(width: size.width, height: size.height))
        image.lockFocus()
        NSColor.gray.setFill()
        NSBezierPath(rect: NSRect(origin: .zero, size: image.size)).fill()
        image.unlockFocus()
        return Thumbnail(image: image)
    }

    // MARK: - Spot marker mapping (protocol 13)

    @Test("a spot rect maps into the fit rect, hand-computed")
    func spotScreenRectInFitMode() {
        let fit = PreviewZoomModel.fitRect(
            displaySize: Self.tiffSize, container: Self.paneSize
        )
        let screen = PreviewZoomModel.spotScreenRect(
            CGRect(x: 400, y: 400, width: 40, height: 20),
            fitRect: fit,
            displaySize: Self.tiffSize
        )
        #expect(screen == CGRect(x: 100, y: 175, width: 10, height: 5))
    }

    @Test("a spot rect maps through the viewport and pan offset at 100%, hand-computed")
    func spotScreenRectAt100() {
        let screen = PreviewZoomModel.spotScreenRect(
            CGRect(x: 400, y: 400, width: 40, height: 20),
            viewportOrigin: CGPoint(x: 200, y: 300),
            panOffset: CGSize(width: -30, height: 10),
            displayScale: Self.scale,
            paneSize: Self.paneSize,
            viewportSize: Self.viewportPixelSize
        )
        #expect(screen == CGRect(x: 70, y: 60, width: 20, height: 10))
    }
}
