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

    @Test("tilted top-right handle keeps the bottom-left corner fixed")
    func tiltedTopRightHandleKeepsBottomLeftFixed() {
        let tilt = 10.0
        let centre = CGPoint(x: Self.rect.midX, y: Self.rect.midY)
        let handlePoint = CropGeometry.handlePoint(
            .topRight, on: Self.rect, tiltDegrees: tilt
        )
        let dragged = CGPoint(x: handlePoint.x + 30, y: handlePoint.y - 20)
        let unrotated = CropGeometry.unrotated(
            dragged, about: centre, tiltDegrees: tilt
        )
        let resized = CropGeometry.resized(
            Self.rect,
            handle: .topRight,
            to: unrotated,
            ratio: nil,
            in: Self.bounds
        )
        #expect(resized.minX == Self.rect.minX)
        #expect(resized.maxY == Self.rect.maxY)
        #expect(resized.maxX > Self.rect.maxX)
    }

    @Test("tilted bottom-left handle keeps the top-right corner fixed")
    func tiltedBottomLeftHandleKeepsTopRightFixed() {
        let tilt = -8.0
        let centre = CGPoint(x: Self.rect.midX, y: Self.rect.midY)
        let handlePoint = CropGeometry.handlePoint(
            .bottomLeft, on: Self.rect, tiltDegrees: tilt
        )
        let dragged = CGPoint(x: handlePoint.x - 25, y: handlePoint.y + 15)
        let unrotated = CropGeometry.unrotated(
            dragged, about: centre, tiltDegrees: tilt
        )
        let resized = CropGeometry.resized(
            Self.rect,
            handle: .bottomLeft,
            to: unrotated,
            ratio: nil,
            in: Self.bounds
        )
        #expect(resized.maxX == Self.rect.maxX)
        #expect(resized.minY == Self.rect.minY)
        #expect(resized.minX < Self.rect.minX)
    }
}
