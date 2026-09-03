import Foundation

/// One flat-field profile, as `flatfield list` and `flatfield_created`
/// report it — section 3.1's rule again: the library's storage is the CLI's,
/// and Swift only reads back what an event handed it.
///
/// The gain map's path and SHA-256 are deliberately absent: the path is
/// app-private storage the UI has no use for, and the hash is roll-invariant
/// bookkeeping the CLI owns.
struct FlatFieldProfile: Identifiable, Sendable, Hashable {
    let profileID: String
    let name: String
    /// The reference's full-resolution dimensions, for display only; `nil`
    /// when the event did not carry them.
    let referenceWidth: Int?
    let referenceHeight: Int?
    /// Provenance only — the CLI never reads the reference again.
    let sourcePath: String?
    let createdAt: String?
    /// The ChArUco board the calibration was fitted with ("35mm" | "6x9"),
    /// or nil for a flat-field-only profile (protocol version 7).
    let boardKey: String?
    /// Whether the profile carries a distortion fit.
    let hasGeometry: Bool
    /// `"scale"` or `"maps"` when a CA fit is carried; nil otherwise.
    let chromaticAberrationMode: String?
    /// The CLI's calibration report, decoded whole for the UI to summarise
    /// or disclose. Computation happens in the CLI, never here.
    let calibrationReport: JSONValue?

    var id: String { profileID }

    /// Decodes one entry of `flatfield_list`'s `profiles` array or
    /// `flatfield_created`'s `profile` object (CONTRACT.md). `nil` for a
    /// malformed entry — a CLI this version understands never sends one,
    /// but a stream is still read line by line rather than trusted blindly.
    init?(fields: [String: JSONValue]) {
        guard
            let profileID = fields["profile_id"]?.stringValue,
            let name = fields["name"]?.stringValue
        else { return nil }

        self.profileID = profileID
        self.name = name
        referenceWidth = fields["reference_width"]?.intValue
        referenceHeight = fields["reference_height"]?.intValue
        sourcePath = fields["source_path"]?.stringValue
        createdAt = fields["created_at"]?.stringValue
        boardKey = fields["board_key"]?.stringValue
        hasGeometry = fields["has_geometry"]?.boolValue ?? false
        chromaticAberrationMode = fields["chromatic_aberration_mode"]?.stringValue
        calibrationReport = fields["calibration_report"]
    }

    /// One caption line summarising the calibration, per the plan's UI
    /// requirement: the user has to be able to see that a correction was
    /// dropped and why, or the automatic gates become invisible.
    var calibrationSummary: String {
        switch (hasGeometry, chromaticAberrationMode) {
        case (false, nil):
            "Flat-field only"
        case (true, nil):
            "Distortion \(summaryOfDistortion)"
        case (true, .some):
            "Distortion \(summaryOfDistortion) · CA corrected (\(chromaticAberrationMode!))"
        case (false, .some):
            "CA corrected (\(chromaticAberrationMode!))"
        }
    }

    private var summaryOfDistortion: String {
        guard let report = calibrationReport?.objectValue,
            let distortion = report["distortion"]?.objectValue,
            let displacement = distortion["corner_displacement_px"]?.doubleValue,
            let percent = distortion["corner_displacement_percent"]?.doubleValue
        else { return "applied" }
        if distortion["accepted"]?.boolValue == false {
            return "not applied"
        }
        return String(format: "%.1f px (%.2f%%)", displacement, percent)
    }
}