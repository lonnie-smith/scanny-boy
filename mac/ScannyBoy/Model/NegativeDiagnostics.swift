import Foundation

/// Per-negative stitch and conversion diagnostics for the Edit tab's info
/// line — alignment quality, failures, warnings, and normalization notes.
/// The thresholds mirror the CLI gates (`composite.py`/`layout.py`,
/// `normalization.py`, `stitch_pipeline.py`); they are not part of the
/// event protocol.
enum NegativeDiagnostics {
    static let globalRMSGate = 12.0
    static let overlapMADGate = 0.20
    static let headroomClipWarnFraction = 0.001
    static let gridPitchRatioMin = 0.6
    static let gridAlignmentRatioMax = 0.25

    enum AlignmentQuality: Hashable {
        case good
        case fair
        case poor
    }

    static func quality(rms: Double, mad: Double) -> AlignmentQuality {
        if rms <= globalRMSGate / 4 && mad <= overlapMADGate / 4 {
            return .good
        }
        if rms <= globalRMSGate && mad <= overlapMADGate {
            return .fair
        }
        return .poor
    }

    /// Dot-separated info-line segments for one negative. `runOverlay` carries
    /// fresher warning text from the most recent finished run when the
    /// manifest has not yet caught up.
    static func infoParts(
        for negative: RollManifest.Negative,
        runOverlay: RunModel.NegativeResult? = nil,
        selectionCount: Int = 1
    ) -> [String] {
        var parts = [negative.expectedOutput]

        if negative.isFailed {
            parts.append(failureTitle(for: negative, runOverlay: runOverlay))
        }

        var warningTitles = manifestWarningTitles(for: negative)
        if let runOverlay {
            for warning in runOverlay.warnings {
                warningTitles.insert(friendlyTitle(for: warning.code))
            }
        }
        parts.append(contentsOf: warningTitles.sorted())

        if let alignment = alignmentPart(for: negative, runOverlay: runOverlay) {
            parts.append(alignment)
        }

        if let normalization = normalizationPart(for: negative),
           !warningTitles.contains(friendlyTitle(for: .normalizeHeadroomClipped))
        {
            parts.append(normalization)
        }

        if let rectification = negative.rectification {
            parts.append(
                String(
                    format: "Rig tilt corrected (%.0f%% fit improvement)",
                    rectification.relativeImprovement * 100
                )
            )
        }

        if let output = negative.output {
            let megapixels = Double(output.width * output.height) / 1_000_000
            parts.append(
                String(format: "%d × %d (%.1f MP)", output.width, output.height, megapixels)
            )
        }

        if selectionCount > 1 {
            parts.append("\(selectionCount) selected")
        }

        return parts
    }

    /// One compact caption for run-level warnings on the Edit tab.
    static func rollLevelWarningCaption(_ warnings: [RunModel.Issue]) -> String {
        let grouped = Dictionary(grouping: warnings, by: \.self)
        return grouped.values
            .sorted { $0.count > $1.count }
            .compactMap { group -> String? in
                guard let warning = group.first else { return nil }
                if group.count > 1 {
                    return "\(warning.message) (\(group.count)×)"
                }
                return warning.message
            }
            .joined(separator: "  ·  ")
    }

    // MARK: - Private

    private static func failureTitle(
        for negative: RollManifest.Negative,
        runOverlay: RunModel.NegativeResult?
    ) -> String {
        if let failure = runOverlay?.failure {
            return friendlyTitle(for: failure.code)
        }
        if let code = negative.errorCode {
            return friendlyTitle(for: CLICode(name: code))
        }
        return "Stitching failed"
    }

    private static func friendlyTitle(for code: CLICode) -> String {
        code.friendlyTitle ?? code.name
    }

    private static func manifestWarningTitles(
        for negative: RollManifest.Negative
    ) -> Set<String> {
        var titles = Set<String>()
        if negative.usedClaheFallback {
            titles.insert("Registration used CLAHE fallback")
        }
        if let pitch = negative.gridPitchRatio, pitch < gridPitchRatioMin {
            titles.insert(friendlyTitle(for: .stitchLayoutUnexpected))
        }
        if let alignment = negative.gridAlignmentRatio,
           alignment > gridAlignmentRatioMax
        {
            titles.insert(friendlyTitle(for: .stitchLayoutUnexpected))
        }
        if let normalization = negative.normalization,
           normalization.maxHeadroomClippedHighlight > headroomClipWarnFraction
            || normalization.maxHeadroomClippedShadow > headroomClipWarnFraction
        {
            titles.insert(friendlyTitle(for: .normalizeHeadroomClipped))
        }
        return titles
    }

    private static func normalizationPart(for negative: RollManifest.Negative) -> String? {
        guard let normalization = negative.normalization else { return nil }
        guard normalization.maxHeadroomClippedHighlight > headroomClipWarnFraction
            || normalization.maxHeadroomClippedShadow > headroomClipWarnFraction
        else { return nil }
        return friendlyTitle(for: .normalizeHeadroomClipped)
    }

    private static func alignmentPart(
        for negative: RollManifest.Negative,
        runOverlay: RunModel.NegativeResult?
    ) -> String? {
        if let runOverlay, let quality = runOverlay.quality {
            return qualityLine(
                quality: quality,
                dimensions: runOverlay.dimensions ?? dimensions(for: negative),
                rms: negative.globalRMSPixels,
                mad: negative.maxOverlapMAD,
                qualityDetail: runOverlay.qualityDetail
            )
        }

        guard let rms = negative.globalRMSPixels else { return nil }
        let mad = negative.maxOverlapMAD
        if let mad {
            let quality = quality(rms: rms, mad: mad)
            return qualityLine(
                quality: quality,
                dimensions: dimensions(for: negative),
                rms: rms,
                mad: mad,
                qualityDetail: String(format: "RMS %.2f px, overlap MAD %.3f", rms, mad)
            )
        }
        return String(format: "Alignment: RMS %.2f px", rms)
    }

    private static func qualityLine(
        quality: AlignmentQuality,
        dimensions: String?,
        rms: Double?,
        mad: Double?,
        qualityDetail: String?
    ) -> String {
        var line = "Alignment: \(qualityWord(quality))"
        if let dimensions {
            line += " — \(dimensions)"
        }
        if let qualityDetail {
            line += ", \(qualityDetail)"
        } else if let rms, let mad {
            line += String(format: ", RMS %.2f px, overlap MAD %.3f", rms, mad)
        }
        return line
    }

    private static func qualityWord(_ quality: AlignmentQuality) -> String {
        switch quality {
        case .good: "good"
        case .fair: "fair"
        case .poor: "poor"
        }
    }

    private static func dimensions(for negative: RollManifest.Negative) -> String? {
        negative.output.map { "\($0.width)×\($0.height)" }
    }
}
