import CoreGraphics
import Foundation
import Testing

@testable import ScannyBoy

@Suite("Crop session")
@MainActor
struct CropSessionTests {
    private static let displaySize = CGSize(width: 1000, height: 800)

    @Test("begin seeds a max-size default rect")
    func beginSeedsDefaultRect() {
        let session = CropSession()
        session.begin(displaySize: Self.displaySize)
        #expect(session.isActive)
        #expect(session.rect.width == Self.displaySize.width)
        #expect(session.rect.height == Self.displaySize.height)
        #expect(session.rect.origin == .zero)
    }

    @Test("applyPreset reshapes to the chosen ratio inside the previous rect")
    func applyPresetReshapesAndClamps() {
        let session = CropSession()
        session.begin(displaySize: Self.displaySize)
        let previous = session.rect
        session.preset = .film35
        session.applyPreset()

        let ratio = 36.0 / 24.0
        #expect(abs(session.rect.width / session.rect.height - ratio) < 0.001)
        #expect(session.rect.minX >= previous.minX)
        #expect(session.rect.minY >= previous.minY)
        #expect(session.rect.maxX <= previous.maxX)
        #expect(session.rect.maxY <= previous.maxY)
    }

    @Test("applyPreset is a no-op for Free")
    func applyPresetIgnoresFree() {
        let session = CropSession()
        session.begin(displaySize: Self.displaySize)
        let before = session.rect
        session.preset = .free
        session.applyPreset()
        #expect(session.rect == before)
    }

    @Test("applyPreset maximizes within the previous crop box")
    func applyPresetMaximizesWithinPrevious() {
        let session = CropSession()
        session.begin(displaySize: Self.displaySize)
        session.rect = CGRect(x: 100, y: 80, width: 400, height: 300)
        let previous = session.rect
        session.preset = .film35
        session.applyPreset()

        let ratio = 36.0 / 24.0
        let expected = CropGeometry.maxInscribedRect(
            in: previous.size, ratio: ratio, origin: previous.origin
        )
        #expect(session.rect == expected)
    }

    @Test("orientedRatio follows a portrait display image")
    func orientedRatioFollowsPortraitDisplay() throws {
        let session = CropSession()
        session.begin(displaySize: CGSize(width: 800, height: 1000))
        session.preset = .film35
        session.applyPreset()
        let ratio = try #require(session.orientedRatio)
        #expect(abs(ratio - 24.0 / 36.0) < 0.001)
    }

    @Test("applyPreset orients 6x7 to portrait on a portrait display")
    func applyPreset6x7Portrait() {
        let displaySize = CGSize(width: 800, height: 1000)
        let session = CropSession()
        session.begin(displaySize: displaySize)
        session.preset = .film6x7
        session.applyPreset()
        #expect(session.rect.width < session.rect.height)
        #expect(abs(session.rect.width / session.rect.height - 56.0 / 69.5) < 0.001)
    }

    @Test("begin can seed the saved crop window")
    func beginSeedsSavedCropWindow() {
        let session = CropSession()
        let saved = CGRect(x: 40, y: 30, width: 120, height: 80)
        session.begin(
            displaySize: Self.displaySize,
            rect: saved,
            tiltDegrees: 4.5,
            preset: .film35
        )
        #expect(session.isActive)
        #expect(session.rect == saved)
        #expect(session.tiltDegrees == 4.5)
        #expect(session.preset == .film35)
    }

    @Test("applyPreset orients 6x7 to landscape on a landscape display")
    func applyPreset6x7Landscape() {
        let displaySize = CGSize(width: 1000, height: 800)
        let session = CropSession()
        session.begin(displaySize: displaySize)
        session.preset = .film6x7
        session.applyPreset()
        #expect(session.rect.width > session.rect.height)
        #expect(abs(session.rect.width / session.rect.height - 69.5 / 56.0) < 0.001)
    }

    @Test("resetToOriginal fills the canvas and clears the preset")
    func resetToOriginalFillsCanvasAndClearsPreset() {
        let session = CropSession()
        session.begin(displaySize: Self.displaySize)
        session.rect = CGRect(x: 100, y: 80, width: 400, height: 300)
        session.preset = .film35
        session.tiltDegrees = 3.0

        session.resetToOriginal()

        let expected = CropGeometry.maxInscribedRect(
            in: Self.displaySize, ratio: nil
        )
        #expect(session.preset == .free)
        #expect(session.rect == expected)
        #expect(session.tiltDegrees == 3.0)
    }
}

@Suite("Crop geometry")
struct CropGeometryTests {
    private static let bounds = CGSize(width: 1000, height: 800)
    private static let rect = CGRect(x: 100, y: 100, width: 200, height: 150)
    private static let ratio35 = 36.0 / 24.0

    @Test("right handle keeps the left edge fixed")
    func rightHandleKeepsLeftEdgeFixed() {
        let resized = CropGeometry.resized(
            Self.rect,
            handle: .right,
            to: CGPoint(x: 350, y: 175),
            ratio: nil,
            in: Self.bounds
        )
        #expect(resized.minX == Self.rect.minX)
        #expect(resized.minY == Self.rect.minY)
        #expect(resized.maxX == 350)
        #expect(resized.height == Self.rect.height)
    }

    @Test("bottom handle keeps the top edge fixed")
    func bottomHandleKeepsTopEdgeFixed() {
        let resized = CropGeometry.resized(
            Self.rect,
            handle: .bottom,
            to: CGPoint(x: 200, y: 300),
            ratio: nil,
            in: Self.bounds
        )
        #expect(resized.minX == Self.rect.minX)
        #expect(resized.minY == Self.rect.minY)
        #expect(resized.maxY == 300)
        #expect(resized.width == Self.rect.width)
    }

    @Test("left handle keeps the right edge fixed")
    func leftHandleKeepsRightEdgeFixed() {
        let resized = CropGeometry.resized(
            Self.rect,
            handle: .left,
            to: CGPoint(x: 80, y: 175),
            ratio: nil,
            in: Self.bounds
        )
        #expect(resized.maxX == Self.rect.maxX)
        #expect(resized.minY == Self.rect.minY)
        #expect(resized.minX == 80)
        #expect(resized.height == Self.rect.height)
    }

    @Test("top handle keeps the bottom edge fixed")
    func topHandleKeepsBottomEdgeFixed() {
        let resized = CropGeometry.resized(
            Self.rect,
            handle: .top,
            to: CGPoint(x: 200, y: 80),
            ratio: nil,
            in: Self.bounds
        )
        #expect(resized.maxY == Self.rect.maxY)
        #expect(resized.minX == Self.rect.minX)
        #expect(resized.minY == 80)
        #expect(resized.width == Self.rect.width)
    }

    @Test("top-right handle keeps the bottom-left corner fixed")
    func topRightHandleKeepsBottomLeftFixed() {
        let resized = CropGeometry.resized(
            Self.rect,
            handle: .topRight,
            to: CGPoint(x: 350, y: 80),
            ratio: nil,
            in: Self.bounds
        )
        #expect(resized.minX == Self.rect.minX)
        #expect(resized.maxY == Self.rect.maxY)
        #expect(resized.maxX == 350)
        #expect(resized.minY == 80)
    }

    @Test("top-right handle with ratio keeps the bottom-left corner fixed")
    func topRightHandleWithRatioKeepsBottomLeftFixed() {
        let resized = CropGeometry.resized(
            Self.rect,
            handle: .topRight,
            to: CGPoint(x: 350, y: 80),
            ratio: Self.ratio35,
            in: Self.bounds
        )
        #expect(resized.minX == Self.rect.minX)
        #expect(resized.maxY == Self.rect.maxY)
        #expect(resized.maxX == 350)
        #expect(resized.minY < Self.rect.minY)
        #expect(abs(resized.width / resized.height - Self.ratio35) < 0.001)
    }

    @Test("top-left handle keeps the bottom-right corner fixed")
    func topLeftHandleKeepsBottomRightFixed() {
        let resized = CropGeometry.resized(
            Self.rect,
            handle: .topLeft,
            to: CGPoint(x: 80, y: 80),
            ratio: nil,
            in: Self.bounds
        )
        #expect(resized.maxX == Self.rect.maxX)
        #expect(resized.maxY == Self.rect.maxY)
        #expect(resized.minX == 80)
        #expect(resized.minY == 80)
    }

    @Test("bottom-right handle keeps the top-left corner fixed")
    func bottomRightHandleKeepsTopLeftFixed() {
        let resized = CropGeometry.resized(
            Self.rect,
            handle: .bottomRight,
            to: CGPoint(x: 350, y: 300),
            ratio: nil,
            in: Self.bounds
        )
        #expect(resized.minX == Self.rect.minX)
        #expect(resized.minY == Self.rect.minY)
        #expect(resized.maxX == 350)
        #expect(resized.maxY == 300)
    }

    @Test("bottom-left handle keeps the top-right corner fixed")
    func bottomLeftHandleKeepsTopRightFixed() {
        let resized = CropGeometry.resized(
            Self.rect,
            handle: .bottomLeft,
            to: CGPoint(x: 80, y: 300),
            ratio: nil,
            in: Self.bounds
        )
        #expect(resized.maxX == Self.rect.maxX)
        #expect(resized.minY == Self.rect.minY)
        #expect(resized.minX == 80)
        #expect(resized.maxY == 300)
    }

    @Test("bottom-left handle with ratio keeps the top-right corner fixed")
    func bottomLeftHandleWithRatioKeepsTopRightFixed() {
        let resized = CropGeometry.resized(
            Self.rect,
            handle: .bottomLeft,
            to: CGPoint(x: 80, y: 300),
            ratio: Self.ratio35,
            in: Self.bounds
        )
        #expect(resized.maxX == Self.rect.maxX)
        #expect(resized.minY == Self.rect.minY)
        #expect(resized.minX == 80)
        #expect(abs(resized.width / resized.height - Self.ratio35) < 0.001)
    }

    @Test("bottom handle with ratio keeps the top edge fixed and centres width")
    func bottomHandleWithRatioCentresWidth() {
        let resized = CropGeometry.resized(
            Self.rect,
            handle: .bottom,
            to: CGPoint(x: 200, y: 300),
            ratio: Self.ratio35,
            in: Self.bounds
        )
        #expect(resized.minY == Self.rect.minY)
        #expect(resized.maxY == 300)
        #expect(resized.midX == Self.rect.midX)
        #expect(resized.minX < Self.rect.minX)
        #expect(resized.maxX > Self.rect.maxX)
        #expect(abs(resized.width / resized.height - Self.ratio35) < 0.001)
    }

    @Test("top handle with ratio keeps the bottom edge fixed and centres width")
    func topHandleWithRatioCentresWidth() {
        let resized = CropGeometry.resized(
            Self.rect,
            handle: .top,
            to: CGPoint(x: 200, y: 80),
            ratio: Self.ratio35,
            in: Self.bounds
        )
        #expect(resized.maxY == Self.rect.maxY)
        #expect(resized.minY == 80)
        #expect(resized.midX == Self.rect.midX)
        #expect(abs(resized.width / resized.height - Self.ratio35) < 0.001)
    }

    @Test("right handle with ratio keeps the left edge fixed and centres height")
    func rightHandleWithRatioCentresHeight() {
        let resized = CropGeometry.resized(
            Self.rect,
            handle: .right,
            to: CGPoint(x: 350, y: 175),
            ratio: Self.ratio35,
            in: Self.bounds
        )
        #expect(resized.minX == Self.rect.minX)
        #expect(resized.maxX == 350)
        #expect(resized.midY == Self.rect.midY)
        #expect(resized.minY < Self.rect.minY)
        #expect(resized.maxY > Self.rect.maxY)
        #expect(abs(resized.width / resized.height - Self.ratio35) < 0.001)
    }

    @Test("left handle with ratio keeps the right edge fixed and centres height")
    func leftHandleWithRatioCentresHeight() {
        let resized = CropGeometry.resized(
            Self.rect,
            handle: .left,
            to: CGPoint(x: 80, y: 175),
            ratio: Self.ratio35,
            in: Self.bounds
        )
        #expect(resized.maxX == Self.rect.maxX)
        #expect(resized.minX == 80)
        #expect(resized.midY == Self.rect.midY)
        #expect(abs(resized.width / resized.height - Self.ratio35) < 0.001)
    }

    @Test("bottom handle with ratio flush left grows only to the right")
    func bottomHandleWithRatioFlushLeftGrowsRight() {
        let flushLeft = CGRect(x: 0, y: 100, width: 200, height: 150)
        let resized = CropGeometry.resized(
            flushLeft,
            handle: .bottom,
            to: CGPoint(x: 100, y: 300),
            ratio: Self.ratio35,
            in: Self.bounds
        )
        #expect(resized.minX == 0)
        #expect(resized.minY == flushLeft.minY)
        #expect(resized.maxY == 300)
        #expect(resized.maxX > flushLeft.maxX)
        #expect(abs(resized.width / resized.height - Self.ratio35) < 0.001)
    }

    @Test("bottom handle with ratio flush right grows only to the left")
    func bottomHandleWithRatioFlushRightGrowsLeft() {
        let flushRight = CGRect(x: 800, y: 100, width: 200, height: 150)
        let resized = CropGeometry.resized(
            flushRight,
            handle: .bottom,
            to: CGPoint(x: 900, y: 300),
            ratio: Self.ratio35,
            in: Self.bounds
        )
        #expect(resized.maxX == Self.bounds.width)
        #expect(resized.minY == flushRight.minY)
        #expect(resized.maxY == 300)
        #expect(resized.minX < flushRight.minX)
        #expect(abs(resized.width / resized.height - Self.ratio35) < 0.001)
    }

    @Test("bottom handle with ratio at full width does not grow")
    func bottomHandleWithRatioAtFullWidthDoesNotGrow() {
        let fullWidth = CGRect(x: 0, y: 100, width: Self.bounds.width, height: 150)
        let resized = CropGeometry.resized(
            fullWidth,
            handle: .bottom,
            to: CGPoint(x: 500, y: 400),
            ratio: Self.ratio35,
            in: Self.bounds
        )
        #expect(resized.width == Self.bounds.width)
        #expect(resized.height == fullWidth.height)
        #expect(resized.minY == fullWidth.minY)
        #expect(resized.maxY == fullWidth.maxY)
    }

    @Test("right handle with ratio at full height does not grow")
    func rightHandleWithRatioAtFullHeightDoesNotGrow() {
        let fullHeight = CGRect(x: 100, y: 0, width: 200, height: Self.bounds.height)
        let resized = CropGeometry.resized(
            fullHeight,
            handle: .right,
            to: CGPoint(x: 400, y: 400),
            ratio: Self.ratio35,
            in: Self.bounds
        )
        #expect(resized.height == Self.bounds.height)
        #expect(resized.width == fullHeight.width)
        #expect(resized.minX == fullHeight.minX)
        #expect(resized.maxX == fullHeight.maxX)
    }

    @Test("bottom handle with ratio flush left shrinks from the left edge")
    func bottomHandleWithRatioFlushLeftShrinksFromLeft() {
        let flushLeft = CGRect(x: 0, y: 100, width: 400, height: 200)
        let resized = CropGeometry.resized(
            flushLeft,
            handle: .bottom,
            to: CGPoint(x: 200, y: 250),
            ratio: Self.ratio35,
            in: Self.bounds
        )
        #expect(resized.minX > 0)
        #expect(resized.midX == flushLeft.midX)
        #expect(resized.minY == flushLeft.minY)
        #expect(resized.maxY == 250)
        #expect(abs(resized.width / resized.height - Self.ratio35) < 0.001)
    }

}
