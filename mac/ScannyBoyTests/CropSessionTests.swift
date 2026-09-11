import CoreGraphics
import Foundation
import Testing

@testable import ScannyBoy

@Suite("Crop session")
@MainActor
struct CropSessionTests {
    private static let displaySize = CGSize(width: 1000, height: 800)

    @Test("begin seeds a centred default rect")
    func beginSeedsDefaultRect() {
        let session = CropSession()
        session.begin(displaySize: Self.displaySize)
        #expect(session.isActive)
        #expect(session.rect.width > CropGeometry.minSize)
        #expect(session.rect.height > CropGeometry.minSize)
        #expect(session.rect.maxX <= Self.displaySize.width)
        #expect(session.rect.maxY <= Self.displaySize.height)
    }

    @Test("applyPreset reshapes to the chosen ratio and stays in bounds")
    func applyPresetReshapesAndClamps() {
        let session = CropSession()
        session.begin(displaySize: Self.displaySize)
        session.preset = .film35
        session.applyPreset(in: Self.displaySize)

        let ratio = 36.0 / 24.0
        #expect(abs(session.rect.width / session.rect.height - ratio) < 0.001)
        #expect(session.rect.minX >= 0)
        #expect(session.rect.minY >= 0)
        #expect(session.rect.maxX <= Self.displaySize.width)
        #expect(session.rect.maxY <= Self.displaySize.height)
    }

    @Test("applyPreset is a no-op for Free")
    func applyPresetIgnoresFree() {
        let session = CropSession()
        session.begin(displaySize: Self.displaySize)
        let before = session.rect
        session.preset = .free
        session.applyPreset(in: Self.displaySize)
        #expect(session.rect == before)
    }

    @Test("orientedRatio swaps for a portrait display rect")
    func orientedRatioSwapsForPortraitRect() throws {
        let session = CropSession()
        session.begin(displaySize: CGSize(width: 800, height: 1000))
        session.preset = .film35
        session.applyPreset(in: CGSize(width: 800, height: 1000))
        let ratio = try #require(session.orientedRatio)
        #expect(abs(ratio - 24.0 / 36.0) < 0.001)
    }
}
