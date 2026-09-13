import Foundation

/// One rig profile, as `rig list` and `rig_created` report it — the
/// library's storage is the CLI's, and Swift only reads back what an event
/// handed it.
struct RigProfile: Identifiable, Sendable, Hashable {
    let profileID: String
    let name: String
    let createdAt: String?
    /// The ChArUco board the calibration was fitted with ("2mm"), or nil
    /// when the event did not carry it.
    let boardKey: String?
    /// Whether the profile carries a distortion fit.
    let hasGeometry: Bool
    /// `"scale"` or `"maps"` when a CA fit is carried; nil otherwise.
    let chromaticAberrationMode: String?
    /// The CLI's calibration report, decoded whole for the UI to summarise
    /// or disclose. Computation happens in the CLI, never here.
    let calibrationReport: JSONValue?

    var id: String { profileID }

    /// Decodes one entry of `rig_list`'s `profiles` array or
    /// `rig_created`'s `profile` object (CONTRACT.md). `nil` for a
    /// malformed entry — a CLI this version understands never sends one,
    /// but a stream is still read line by line rather than trusted blindly.
    init?(fields: [String: JSONValue]) {
        guard
            let profileID = fields["profile_id"]?.stringValue,
            let name = fields["name"]?.stringValue
        else { return nil }

        self.profileID = profileID
        self.name = name
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
            "No geometry fit"
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
