import Foundation

/// Photographer-facing titles for the CLI's stable codes. The codes remain
/// the interface (CONTRACT.md) — this mapping only decides what the results
/// view leads with; the raw `code.name` stays available in each row's
/// expanded detail and in the copied report. A code with no title here
/// falls back to its raw name.
extension CLICode {
    var friendlyTitle: String? {
        switch self {
        case .noFiles: "No files found"
        case .filenameSortUsed: "Files were sorted by filename"
        case .missingCaptureTime: "A file has no capture time"
        case .captureSettingsDiffer: "Capture settings differ across the selection"
        case .unsupportedRAW: "A file is not a supported RAW"
        case .captureMetadataMissing: "A file has no capture metadata"
        case .unreadableRAW: "A file could not be read"
        case .outputConflict: "Output files already exist"
        case .insufficientDisk: "Not enough disk space"
        case .insufficientMemory: "Not enough memory"
        case .manifestMismatch: "Files changed since the last run"
        case .tiffWriteFailed: "Could not write a TIFF"
        case .intermediateMissing: "Intermediate files are missing"
        case .intermediateChanged: "Intermediate files changed since conversion"
        case .stitchInsufficientMatches: "Not enough matching detail between frames"
        case .stitchUnderconstrained: "Not enough usable frames to stitch"
        case .stitchResidualTooHigh: "Frames did not align closely enough"
        case .stitchOutputTooLarge: "The stitched image would be too large to save"
        case .stitchFailed: "Stitching failed"
        case .stitchScaleDrift: "A frame changed size between shots"
        case .stitchLayoutUnexpected: "The frames' arrangement was not recognized"
        case .stitchRebateCheckFailed: "The stitched image failed its safety check"
        case .scanClipped: "Some highlights are clipped in the scan"
        case .normalizeHeadroomClipped: "Normalization clipped some highlights or shadows"
        case .normalizeDegenerateBounds: "The scan has no usable brightness range"
        case .flatFieldHighlightClipped: "Flat-field correction clipped some highlights"
        case .flatFieldProfileNotFound: "Flat-field profile not found"
        case .flatFieldAspectMismatch: "Flat-field profile does not match the frame size"
        case .outputModifiedExternally: "The file was changed outside the app"
        case .outputDimensionsLarge: "The output is very large"
        case .cancelled: "Cancelled"
        case .internalError: "An unexpected internal error occurred"
        default: nil
        }
    }
}
