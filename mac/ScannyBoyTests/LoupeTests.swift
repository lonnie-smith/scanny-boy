import CoreGraphics
import Foundation
import Testing

@testable import ScannyBoy

/// docs/FOCUS_LOCK_PLAN.md §6, "Loupe" and "Grain view".
@Suite("Loupe sizes and grain view")
@MainActor
struct LoupeTests {
    private func makeDefaults() -> UserDefaults {
        let name = "com.lonniesmith.scanny-boy.tests.loupe.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: name)!
        defaults.removePersistentDomain(forName: name)
        return defaults
    }

    private func makeModel(defaults: UserDefaults) -> FocusAssistModel {
        FocusAssistModel(
            camera: FakeTetherCamera(),
            runner: CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/false")),
            defaults: defaults
        )
    }

    private func frame(seed: UInt8, areaWidth: UInt16 = 512) -> LiveViewFrame {
        LiveViewFixtures.makeFrame(areaWidth: areaWidth, jpeg: LiveViewFixtures.texturedJPEG(seed: seed))
    }

    // MARK: Sizes

    @Test("sizes are whole screen pixels per frame pixel at display scales 1, 2 and 3")
    func wholePixelMapping() {
        let frameWidth = FocusAssistTuning.liveViewFrameWidth
        for scale: CGFloat in [1, 2, 3] {
            for size in LoupeSize.allCases {
                let points = size.points(framePixels: frameWidth, displayScale: scale)
                let screenPixels = points * scale
                #expect(screenPixels == CGFloat(frameWidth * size.pixelsPerFramePixel))
            }
        }
        #expect(LoupeSize.small.points(framePixels: 640, displayScale: 2) == 320)
        #expect(LoupeSize.medium.points(framePixels: 640, displayScale: 2) == 640)
        #expect(LoupeSize.large.points(framePixels: 640, displayScale: 2) == 960)
        #expect(LoupeSize.small.points(framePixels: 640, displayScale: 1) == 640)
    }

    @Test("a width that does not fit steps down a whole size, never a fractional scale")
    func narrowWidthStepsDown() {
        // Retina: L is 960 pt, M 640 pt, S 320 pt.
        #expect(LoupeSize.large.fitting(framePixels: 640, displayScale: 2, availableWidth: 960) == .large)
        #expect(LoupeSize.large.fitting(framePixels: 640, displayScale: 2, availableWidth: 959) == .medium)
        #expect(LoupeSize.large.fitting(framePixels: 640, displayScale: 2, availableWidth: 639) == .small)
        #expect(LoupeSize.medium.fitting(framePixels: 640, displayScale: 2, availableWidth: 2000) == .medium)
        // Non-Retina: M is already 1280 pt.
        #expect(LoupeSize.large.fitting(framePixels: 640, displayScale: 1, availableWidth: 1300) == .medium)
        // Nothing fits: S, the minimum.
        #expect(LoupeSize.large.fitting(framePixels: 640, displayScale: 2, availableWidth: 100) == .small)
    }

    @Test("L cycles S, M, L, S")
    func cycle() {
        #expect(LoupeSize.small.next == .medium)
        #expect(LoupeSize.medium.next == .large)
        #expect(LoupeSize.large.next == .small)
    }

    @Test("loupe size and grain view are remembered through the injected defaults")
    func persistence() {
        let defaults = makeDefaults()
        let first = makeModel(defaults: defaults)
        #expect(first.loupeSize == .medium)
        #expect(first.grainView == false)
        first.cycleLoupeSize()
        first.toggleGrainView()
        let second = makeModel(defaults: defaults)
        #expect(second.loupeSize == .large)
        #expect(second.grainView == true)
        second.cycleLoupeSize()
        second.toggleGrainView()
        let third = makeModel(defaults: defaults)
        #expect(third.loupeSize == .small)
        #expect(third.grainView == false)
        // Another suite is untouched.
        #expect(makeModel(defaults: makeDefaults()).loupeSize == .medium)
    }

    // MARK: Grain view

    @Test("levels are taken from the first frames and held while later frames differ")
    func levelsHeld() async {
        let model = makeModel(defaults: makeDefaults())
        // Distinct JPEGs: a repeat of the last one is not sampled again.
        for index in 0..<FocusAssistTuning.grainLevelFrames {
            await model.process(frame(seed: 10 + UInt8(index)))
        }
        let held = model.grainLevels
        #expect(held != nil)
        await model.process(frame(seed: 90))
        await model.process(frame(seed: 200))
        #expect(model.grainLevels == held)
    }

    @Test("R retakes the levels from the next frames")
    func levelsRetakenAfterReset() async {
        let model = makeModel(defaults: makeDefaults())
        for index in 0..<FocusAssistTuning.grainLevelFrames {
            await model.process(frame(seed: UInt8(index)))
        }
        let before = model.grainLevels
        model.resetPeak()
        #expect(model.grainLevels == nil)
        // A flat frame has no spread, so its levels differ from the textured ones.
        let flat = LiveViewFixtures.makeFrame(areaWidth: 512, jpeg: LiveViewFixtures.flatGreyJPEG(value: 40))
        await model.process(flat)
        #expect(model.grainLevels == GrainLevels(low: 40, high: 40))
        #expect(model.grainLevels != before)
    }

    @Test("an area change retakes the levels")
    func levelsRetakenAfterAreaChange() async {
        let model = makeModel(defaults: makeDefaults())
        for index in 0..<FocusAssistTuning.grainLevelFrames {
            await model.process(frame(seed: UInt8(index), areaWidth: 512))
        }
        let before = model.grainLevels
        let flat = LiveViewFixtures.makeFrame(areaWidth: 256, jpeg: LiveViewFixtures.flatGreyJPEG(value: 40))
        await model.process(flat)
        #expect(model.grainLevels != before)
        #expect(model.grainLevels == GrainLevels(low: 40, high: 40))
    }

    @Test("the score is identical with grain view on and off")
    func scoreUnaffected() async {
        let off = makeModel(defaults: makeDefaults())
        let on = makeModel(defaults: makeDefaults())
        on.toggleGrainView()
        for seed: UInt8 in [5, 70, 130] {
            await off.process(frame(seed: seed))
            await on.process(frame(seed: seed))
            #expect(off.latestScore != nil)
            #expect(on.latestScore == off.latestScore)
        }
    }

    @Test("grain view draws a grey, inverted, stretched picture and the plain view does not")
    func grainPicture() async throws {
        let model = makeModel(defaults: makeDefaults())
        await model.process(frame(seed: 20))
        let plain = try #require(model.loupeImage)
        model.toggleGrainView()
        // Same JPEG again: the toggle alone must redraw it.
        await model.process(frame(seed: 20))
        let grain = try #require(model.loupeImage)
        #expect(grain.width == plain.width && grain.height == plain.height)
        #expect(grain.bitsPerPixel == 8)
        #expect(FocusScore.greyFrame(image: grain)?.pixels != FocusScore.greyFrame(image: plain)?.pixels)
    }

    @Test("the grain lookup inverts and stretches between the held levels")
    func lookup() throws {
        let pixels: [UInt8] = (0..<36).map { UInt8($0 * 7) }
        let grey = FocusScore.GreyFrame(width: 6, height: 6, pixels: pixels)
        let image = try #require(GrainView.render(grey, levels: GrainLevels(low: 50, high: 150)))
        let drawn = try #require(FocusScore.greyFrame(image: image)).pixels
        #expect(drawn[0] == 255)  // 0 is below the black point: inverted to white
        #expect(drawn[35] == 0)  // 245 is above the white point: inverted to black
        #expect(drawn[10] == 255 - UInt8(min(255, max(0, (70 - 50) * 255 / 100))))
    }
}
