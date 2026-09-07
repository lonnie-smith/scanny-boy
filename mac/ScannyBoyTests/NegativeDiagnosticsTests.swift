import Foundation
import Testing
@testable import ScannyBoy

@Suite("Negative diagnostics")
struct NegativeDiagnosticsTests {
    @Test("Alignment quality follows the CLI gates")
    func alignmentQuality() {
        #expect(NegativeDiagnostics.quality(rms: 1.0, mad: 0.04) == .good)
        #expect(NegativeDiagnostics.quality(rms: 6.0, mad: 0.15) == .fair)
        #expect(NegativeDiagnostics.quality(rms: 20.0, mad: 0.30) == .poor)
    }

    @Test("A completed negative shows alignment and dimensions")
    func completedNegativeInfoLine() {
        let negative = Self.sampleNegative(
            globalRMSPixels: 1.57,
            maxOverlapMAD: 0.04,
            output: RollManifest.Output(
                name: "a.tif", size: 1, sha256: String(repeating: "a", count: 64),
                width: 6140, height: 7917
            )
        )
        let parts = NegativeDiagnostics.infoParts(for: negative)
        #expect(parts.first == "a.tif")
        #expect(parts.contains(where: { $0.hasPrefix("Alignment: good") }))
        #expect(parts.contains(where: { $0.contains("6140 × 7917") }))
    }

    @Test("A failed negative shows the friendly failure title")
    func failedNegativeInfoLine() {
        let negative = Self.sampleNegative(
            status: "failed",
            errorCode: "STITCH_UNDERCONSTRAINED"
        )
        let parts = NegativeDiagnostics.infoParts(for: negative)
        #expect(parts.contains("Not enough usable frames to stitch"))
    }

    @Test("Normalization headroom clipping becomes a warning")
    func normalizationWarning() {
        let negative = Self.sampleNegative(
            normalization: RollManifest.Negative.NormalizationSummary(
                maxHeadroomClippedHighlight: 0.002,
                maxHeadroomClippedShadow: 0
            )
        )
        let parts = NegativeDiagnostics.infoParts(for: negative)
        #expect(
            parts.contains("Normalization clipped some highlights or shadows")
        )
    }

    @Test("Run overlay warnings merge with manifest-backed ones")
    func runOverlayWarnings() {
        let negative = Self.sampleNegative()
        let overlay = RunModel.NegativeResult(
            id: "negative-01",
            status: .succeeded,
            output: "a.tif",
            dimensions: "100×100",
            quality: .good,
            qualityDetail: "RMS 1.00 px, overlap MAD 0.040",
            failure: nil,
            warnings: [
                RunModel.Issue(
                    code: .unknown("STITCH_GAIN_DRIFT"),
                    message: "negative-01: frame drift"
                )
            ]
        )
        let parts = NegativeDiagnostics.infoParts(for: negative, runOverlay: overlay)
        #expect(parts.contains("STITCH_GAIN_DRIFT"))
        #expect(parts.contains(where: { $0.hasPrefix("Alignment: good") }))
    }

    @Test("Roll-level warnings join distinct messages")
    func rollLevelWarningCaption() {
        let warnings = [
            RunModel.Issue(code: .captureMetadataMissing, message: "a.NEF: no lens"),
            RunModel.Issue(code: .captureMetadataMissing, message: "b.NEF: no lens"),
        ]
        let caption = NegativeDiagnostics.rollLevelWarningCaption(warnings)
        #expect(caption.contains("a.NEF: no lens"))
        #expect(caption.contains("b.NEF: no lens"))
    }

    private static func sampleNegative(
        status: String = "completed",
        globalRMSPixels: Double? = nil,
        maxOverlapMAD: Double? = nil,
        output: RollManifest.Output? = nil,
        errorCode: String? = nil,
        normalization: RollManifest.Negative.NormalizationSummary? = nil
    ) -> RollManifest.Negative {
        RollManifest.Negative(
            negativeID: "negative-01",
            runID: "run-1",
            sequence: 1,
            members: ["a.NEF"],
            expectedOutput: "a.tif",
            status: status,
            output: output,
            captureTime: RollManifest.CaptureTime(
                sourceDatetimeOriginal: nil,
                intendedDatetimeOriginal: nil,
                appliedDatetimeOriginal: nil,
                dateOverride: nil
            ),
            metadata: .empty,
            globalRMSPixels: globalRMSPixels,
            rebateDeviationPixels: nil,
            previewPath: nil,
            rotationQuarterTurns: 0,
            flippedHorizontally: false,
            rectification: nil,
            toneGradeR: nil,
            toneSnapGamma: nil,
            toneDensity: nil,
            toneShadowDensity: nil,
            toneHighlightDensity: nil,
            toneToe: nil,
            toneToeWidth: nil,
            toneShoulder: nil,
            toneShoulderWidth: nil,
            colorWbCyan: nil,
            colorWbMagenta: nil,
            colorWbYellow: nil,
            colorShadowCyan: nil,
            colorShadowMagenta: nil,
            colorShadowYellow: nil,
            colorHighlightCyan: nil,
            colorHighlightMagenta: nil,
            colorHighlightYellow: nil,
            colorCastRemoval: nil,
            colorCastRemovalHighlights: nil,
            colorDyeSeparation: nil,
            colorSeparationDamping: nil,
            colorTemperature: nil,
            errorCode: errorCode,
            errorMessage: nil,
            maxOverlapMAD: maxOverlapMAD,
            normalization: normalization,
            usedClaheFallback: false,
            gridPitchRatio: nil,
            gridAlignmentRatio: nil
        )
    }
}
