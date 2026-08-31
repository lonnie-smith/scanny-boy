import Foundation

/// The parts of `scanny-boy-roll.json` the app needs, decoded from
/// `roll info`'s `manifest` field — never read from disk. Phase 3 section
/// 3.1: "Swift never parses `scanny-boy-roll.json` itself... `roll list`
/// and `roll info` are the only two ways in."
///
/// `shared/contract/roll-manifest.schema.json` is the authoritative
/// definition; this type deliberately decodes only the fields the app's
/// planned UI (section 3.10) actually shows — the roll's own identity, its
/// runs, and each negative's identity, sequence, output, and capture-time
/// state — and ignores the rest (`sources`, `processing_params`,
/// `icc_profile`, `stitch_params`, and every per-negative registration
/// detail: `frames`, `pairs`, `global_rms_px`, `canvas`, `valid_rect`,
/// `fill_color`, `rebate_deviation_px`). A manifest that grows a field must
/// not stop the app reading the ones it needs.
struct RollManifest: Sendable, Hashable {
    struct Run: Sendable, Hashable {
        let runID: String
        let shortID: String
        /// `"run"` or `"stitch"`.
        let kind: String
        /// `running`, `partial`, `cancelled`, or `complete`.
        let status: String
        let startedAt: String
        let finishedAt: String?
    }

    struct Output: Sendable, Hashable {
        let name: String
        let size: Int
        let sha256: String
        let width: Int
        let height: Int
    }

    struct CaptureTime: Sendable, Hashable {
        let sourceDatetimeOriginal: String?
        let intendedDatetimeOriginal: String?
        let appliedDatetimeOriginal: String?
        let dateOverride: String?

        /// Section 3.8: dirty when the intent differs from what was last
        /// written into the TIFF.
        var isDirty: Bool { intendedDatetimeOriginal != appliedDatetimeOriginal }
    }

    struct Negative: Sendable, Hashable {
        let negativeID: String
        let runID: String
        /// 1-based position in the roll; `nil` when unranked or superseded.
        let sequence: Int?
        /// The `negative_id` that replaced this one, if any.
        let supersededBy: String?
        /// The source NEFs this negative was built from, in canonical order
        /// — the Edit tab's "source frames" (section 3.10).
        let members: [String]
        let expectedOutput: String
        /// `pending`, `completed`, or `failed`.
        let status: String
        let output: Output?
        let captureTime: CaptureTime
        /// The registration quality numbers Chunk P2-9 already reports on
        /// `negative_done` (section 3.4), read back here for the Edit tab's
        /// display: RMS pixel error across every accepted pair, and the
        /// deviation `nil` unless a rebate check ran.
        let globalRMSPixels: Double?
        let rebateDeviationPixels: Double?

        var isCompleted: Bool { status == "completed" }
        var isFailed: Bool { status == "failed" }
        var isSuperseded: Bool { supersededBy != nil }
    }

    struct Metadata: Sendable, Hashable {
        /// `YYYY-MM-DD`.
        let rollCaptureDate: String?
        let lastAppliedAt: String?
    }

    let rollID: String
    let rollName: String
    let shotsPerNegative: Int
    let createdAt: String
    let updatedAt: String
    let runs: [Run]
    let negatives: [Negative]
    let metadata: Metadata

    /// Every negative still standing, per section 3.4 — the same notion
    /// `RollManifest.live_negatives()` names on the Python side.
    var liveNegatives: [Negative] { negatives.filter { !$0.isSuperseded } }

    /// Every stitched TIFF the manifest records as published, in negative
    /// order — the `RunManifest.publishedOutputs` counterpart.
    var publishedOutputs: [String] { negatives.compactMap { $0.output?.name } }

    /// Decodes the `manifest` field of a `roll_info` event.
    /// `CLIEvent.manifest` is already `[String: JSONValue]`; this performs
    /// the same manual field-by-field extraction `CLIEvent` itself uses,
    /// rather than routing through `Decodable` (`JSONValue` has no
    /// `Encodable` counterpart to re-serialize through). `nil` for a
    /// malformed manifest — a CLI this version understands never sends
    /// one.
    init?(fields: [String: JSONValue]) {
        guard
            let rollID = fields["roll_id"]?.stringValue,
            let rollName = fields["roll_name"]?.stringValue,
            let shotsPerNegative = fields["shots_per_negative"]?.intValue,
            let createdAt = fields["created_at"]?.stringValue,
            let updatedAt = fields["updated_at"]?.stringValue,
            let runFields = fields["runs"]?.arrayValue,
            let negativeFields = fields["negatives"]?.arrayValue,
            let metadataFields = fields["metadata"]?.objectValue
        else { return nil }

        let runs = runFields.compactMap { $0.objectValue.flatMap(Self.decodeRun) }
        guard runs.count == runFields.count else { return nil }

        let negatives = negativeFields.compactMap { $0.objectValue.flatMap(Self.decodeNegative) }
        guard negatives.count == negativeFields.count else { return nil }

        guard let metadata = Self.decodeMetadata(metadataFields) else { return nil }

        self.rollID = rollID
        self.rollName = rollName
        self.shotsPerNegative = shotsPerNegative
        self.createdAt = createdAt
        self.updatedAt = updatedAt
        self.runs = runs
        self.negatives = negatives
        self.metadata = metadata
    }

    private static func decodeRun(_ fields: [String: JSONValue]) -> Run? {
        guard
            let runID = fields["run_id"]?.stringValue,
            let shortID = fields["short_id"]?.stringValue,
            let kind = fields["kind"]?.stringValue,
            let status = fields["status"]?.stringValue,
            let startedAt = fields["started_at"]?.stringValue
        else { return nil }
        return Run(
            runID: runID,
            shortID: shortID,
            kind: kind,
            status: status,
            startedAt: startedAt,
            finishedAt: fields["finished_at"]?.stringValue
        )
    }

    private static func decodeOutput(_ fields: [String: JSONValue]) -> Output? {
        guard
            let name = fields["name"]?.stringValue,
            let size = fields["size"]?.intValue,
            let sha256 = fields["sha256"]?.stringValue,
            let width = fields["width"]?.intValue,
            let height = fields["height"]?.intValue
        else { return nil }
        return Output(name: name, size: size, sha256: sha256, width: width, height: height)
    }

    private static func decodeCaptureTime(_ fields: [String: JSONValue]) -> CaptureTime {
        CaptureTime(
            sourceDatetimeOriginal: fields["source_datetime_original"]?.stringValue,
            intendedDatetimeOriginal: fields["intended_datetime_original"]?.stringValue,
            appliedDatetimeOriginal: fields["applied_datetime_original"]?.stringValue,
            dateOverride: fields["date_override"]?.stringValue
        )
    }

    private static func decodeNegative(_ fields: [String: JSONValue]) -> Negative? {
        guard
            let negativeID = fields["negative_id"]?.stringValue,
            let runID = fields["run_id"]?.stringValue,
            let members = fields["members"]?.stringArrayValue,
            let expectedOutput = fields["expected_output"]?.stringValue,
            let status = fields["status"]?.stringValue,
            let captureTimeFields = fields["capture_time"]?.objectValue
        else { return nil }

        let output = fields["output"]?.objectValue.flatMap(Self.decodeOutput)

        return Negative(
            negativeID: negativeID,
            runID: runID,
            sequence: fields["sequence"]?.intValue,
            supersededBy: fields["superseded_by"]?.stringValue,
            members: members,
            expectedOutput: expectedOutput,
            status: status,
            output: output,
            captureTime: Self.decodeCaptureTime(captureTimeFields),
            globalRMSPixels: fields["global_rms_px"]?.doubleValue,
            rebateDeviationPixels: fields["rebate_deviation_px"]?.doubleValue
        )
    }

    private static func decodeMetadata(_ fields: [String: JSONValue]) -> Metadata? {
        Metadata(
            rollCaptureDate: fields["roll_capture_date"]?.stringValue,
            lastAppliedAt: fields["last_applied_at"]?.stringValue
        )
    }
}

/// What reading the roll manifest after a `run` or `stitch` told the app —
/// the `ManifestReport` counterpart, over the new `RollManifest`.
///
/// Unlike Phase 2's version, a roll has no single top-level status (section
/// 3.3: it is additive, and can hold other runs at any status
/// simultaneously) — so "did cleanup finish" is judged from *this
/// invocation's own* run record, not the roll as a whole.
enum RollManifestReport: Sendable, Hashable {
    /// This invocation's own run is in a final state: `complete`, `partial`,
    /// or `cancelled`.
    case final(RollManifest)
    /// This invocation's own run is still marked `running`. Accepted, not
    /// treated as corrupt: a forced stop cannot update it, so this means the
    /// CLI never got to finish, and a staging directory for this run may
    /// still be on disk in the roll folder.
    case cleanupIncomplete(RollManifest)
    /// No roll manifest could be read. The string says why.
    case unavailable(String)

    var manifest: RollManifest? {
        switch self {
        case .final(let manifest), .cleanupIncomplete(let manifest): manifest
        case .unavailable: nil
        }
    }

    /// `runID` is the invocation's own run id — the roll may hold other
    /// runs at any status, so only this one's status decides `.final` vs
    /// `.cleanupIncomplete`. `nil` (the run id was never learned) is
    /// treated as `.final`, matching "nothing more to wait for."
    init(manifest: RollManifest, runID: String?) {
        let runStatus = runID.flatMap { id in manifest.runs.first { $0.runID == id }?.status }
        self = runStatus == "running" ? .cleanupIncomplete(manifest) : .final(manifest)
    }

    /// One sentence for the completion UI.
    var summary: String {
        switch self {
        case .final(let manifest):
            "The roll now holds \(manifest.liveNegatives.count) negative(s)."
        case .cleanupIncomplete:
            """
            Cleanup did not finish: this run is still marked running in the \
            roll manifest, so a staging file for it may remain in the roll \
            folder. The next run or re-stitch removes it and recomposites \
            the negative that was interrupted. Published negatives are left \
            alone.
            """
        case .unavailable(let reason):
            "The roll's manifest could not be read: \(reason)"
        }
    }
}
