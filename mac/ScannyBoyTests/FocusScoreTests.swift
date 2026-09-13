import CoreGraphics
import Foundation
import Testing

@testable import ScannyBoy

@Suite("FocusScore")
struct FocusScoreTests {
    @Test("textured frames outscore flat grey and weak texture")
    func texturedVsFlat() {
        let sharp = LiveViewFixtures.texturedJPEG(seed: 90)
        let soft = LiveViewFixtures.texturedJPEG(seed: 10)
        let flat = LiveViewFixtures.flatGreyJPEG()
        let sharpScore = FocusScore.score(jpegData: sharp) ?? 0
        let softScore = FocusScore.score(jpegData: soft) ?? 0
        let flatScore = FocusScore.score(jpegData: flat) ?? 0
        #expect(sharpScore > softScore)
        #expect(flatScore < FocusAssistTuning.scoreMinTexture)
    }
}
