import AppKit
import Foundation
import Testing

@testable import ScannyBoy

/// The 1:1 zoom's geometry and gesture state machine: which region the pane
/// asks the CLI for, how fit-view clicks anchor the crop, and how a drag
/// pans. Pure math and main-actor state — no helper runs here.
@Suite("Preview zoom model")
@MainActor
struct PreviewZoomModelTests {
    /// The published TIFF's pixel size (2000x1000), the pane's point size,
    /// and a 2x display scale: the constants every test reasons about.
    private static let tiffSize = CGSize(width: 2000, height: 1000)
    private static let paneSize = CGSize(width: 500, height: 400)
    private static let scale: CGFloat = 2
    /// The pane-sized 1:1 crop: 500x400 points at 2x.
    private static let cropSize = CGSize(width: 1000, height: 800)

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

        // The fit rect's vertical centre sits at pane y=200 (75 + 250/2),
        // which hits the image's centre.
        let centre = PreviewZoomModel.displayPoint(
            for: CGPoint(x: 250, y: 200), fitRect: fit, displaySize: Self.tiffSize
        )
        #expect(centre == CGPoint(x: 1000, y: 500))

        // A point above the letterboxed image clamps to the top edge.
        let clamped = PreviewZoomModel.displayPoint(
            for: CGPoint(x: 250, y: 5), fitRect: fit, displaySize: Self.tiffSize
        )
        #expect(clamped.y == 0)
    }

    @Test("crop size is the pane's physical pixel size, clamped to the image")
    func cropSizeFollowsPhysicalPixels() {
        let size = PreviewZoomModel.cropSize(
            paneSize: Self.paneSize, displayScale: 2, displaySize: Self.tiffSize
        )
        #expect(size == CGSize(width: 1000, height: 800))

        // An image smaller than the pane clamps to the image.
        let small = PreviewZoomModel.cropSize(
            paneSize: Self.paneSize,
            displayScale: Self.scale,
            displaySize: CGSize(width: 400, height: 200)
        )
        #expect(small == CGSize(width: 400, height: 200))
    }

    @Test("origins clamp against the display bounds, centered when larger")
    func clampOriginKeepsTheCropInside() {
        // Crop wider than the image: centered, not pinned.
        let centered = PreviewZoomModel.clampOrigin(
            CGPoint(x: -50, y: 0),
            cropSize: CGSize(width: 2000, height: 800),
            displaySize: Self.tiffSize
        )
        #expect(centered.x == 0)

        // Otherwise pinned into [0, display - crop].
        let pinned = PreviewZoomModel.clampOrigin(
            CGPoint(x: 1_500, y: -50),
            cropSize: CGSize(width: 1000, height: 800),
            displaySize: Self.tiffSize
        )
        #expect(pinned.x == 1000)
        #expect(pinned.y == 0)
    }

    @Test("⌘Space+click in fit view zooms in anchored on the clicked pixel")
    func clickZoomsInAnchoredAtTheClick() async {
        let zoom = PreviewZoomModel()
        zoom.update(
            paneSize: Self.paneSize,
            displayScale: Self.scale,
            displaySize: Self.tiffSize,
            loader: { _ in Self.thumbnail() }
        )
        zoom.mouseDown(at: CGPoint(x: 250, y: 125), kind: .zoomInAtClick)
        zoom.mouseUp(at: CGPoint(x: 250, y: 125))
        await zoom.waitForCropForTesting()

        #expect(zoom.mode == .pixels100)
        // The clicked pixel (1000, 200) is centred; the vertical clamp pins
        // the 800px crop to the top of the 1000px image.
        #expect(zoom.origin == CGPoint(x: 500, y: 0))
        #expect(zoom.crop?.rect.minX == 500)
        #expect(zoom.crop?.rect.minY == 0)
    }

    @Test("Z toggles back out from 100%")
    func toggleZoomsBackOut() async {
        let zoom = await zoomedIn()
        zoom.toggle(at: CGPoint(x: 10, y: 10))

        #expect(zoom.mode == .fit)
        #expect(zoom.crop == nil)
    }

    @Test("a drag pans and refetches, clamped to the image bounds")
    func dragPans() async {
        let zoom = await zoomedIn()
        let before = zoom.origin
        zoom.mouseDown(at: CGPoint(x: 250, y: 200), kind: .pan)
        zoom.mouseDragged(to: CGPoint(x: 100, y: 200))
        // Grab-and-drag: dragging left moves the image left.
        #expect(zoom.panOffset.width == -150)
        zoom.mouseUp(at: CGPoint(x: 100, y: 200))

        #expect(zoom.mode == .pixels100)
        // Dragging left moved the view right by 300 display pixels.
        #expect(zoom.origin.x == 800)
        #expect(zoom.origin.y == before.y)
        await zoom.waitForCropForTesting()
        #expect(zoom.crop?.rect.minX == 800)
    }

    @Test("a drag wider than the image clamps and does not wrap")
    func dragClampsAtTheEdges() async {
        let zoom = await zoomedIn()
        zoom.mouseDown(at: CGPoint(x: 250, y: 200), kind: .pan)
        zoom.mouseDragged(to: CGPoint(x: -5_000, y: 200))
        // Live: the view stops at the image's right edge.
        #expect(zoom.panOffset.width == -250)
        zoom.mouseUp(at: CGPoint(x: -5_000, y: 200))

        #expect(zoom.mode == .pixels100)
        await zoom.waitForCropForTesting()
        // The 1000px crop against a 2000px image clamps at the right edge.
        #expect(zoom.origin.x == 1000)
        #expect(zoom.crop?.rect.minX == 1000)
    }

    @Test("a click at 100% does not zoom back out")
    func clickAt100DoesNotZoomOut() async {
        let zoom = await zoomedIn()
        zoom.mouseDown(at: CGPoint(x: 10, y: 10), kind: .pan)
        zoom.mouseUp(at: CGPoint(x: 10, y: 10))

        #expect(zoom.mode == .pixels100)
        #expect(zoom.crop != nil)
    }

    @Test("fetchCrop skips a crop already on screen")
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
        let firstRect = zoom.crop?.rect
        #expect(firstRect != nil)

        zoom.fetchCrop()
        await zoom.waitForCropForTesting()
        #expect(zoom.crop?.rect == firstRect)
    }

    // MARK: - Helpers

    /// A model zoomed into 100% with its crop already on screen.
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

    private static func thumbnail() -> Thumbnail {
        let image = NSImage(size: NSSize(width: 1000, height: 800))
        image.lockFocus()
        NSColor.gray.setFill()
        NSBezierPath(rect: NSRect(origin: .zero, size: image.size)).fill()
        image.unlockFocus()
        return Thumbnail(image: image)
    }
    // MARK: - Spot marker mapping (protocol 13)

    @Test("a spot rect maps into the fit rect, hand-computed")
    func spotScreenRectInFitMode() {
        // The fit rect of 2000x1000 in 500x400 is 1:4 at (0, 75).
        let fit = PreviewZoomModel.fitRect(
            displaySize: Self.tiffSize, container: Self.paneSize
        )
        let screen = PreviewZoomModel.spotScreenRect(
            CGRect(x: 400, y: 400, width: 40, height: 20),
            fitRect: fit,
            displaySize: Self.tiffSize
        )
        // 1:4 scale, offset by the letterbox.
        #expect(screen == CGRect(x: 100, y: 175, width: 10, height: 5))
    }

    @Test("a spot rect maps through the crop and the pan offset at 100%, hand-computed")
    func spotScreenRectAt100() {
        let crop = PreviewZoomModel.Crop(
            image: NSImage(),
            rect: CGRect(x: 200, y: 300, width: 1000, height: 800),
            displayScale: Self.scale
        )
        let pan = CGSize(width: -30, height: 10)
        let screen = PreviewZoomModel.spotScreenRect(
            CGRect(x: 400, y: 400, width: 40, height: 20),
            crop: crop,
            cropScreenOffset: pan
        )
        // (400 - 200) / 2 - 30 = 70; (400 - 300) / 2 + 10 = 60.
        #expect(screen == CGRect(x: 70, y: 60, width: 20, height: 10))
    }
}
