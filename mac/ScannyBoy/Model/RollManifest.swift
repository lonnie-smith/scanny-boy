import Foundation

/// The parts of `scanny-boy-roll.json` the app reads back once a `run` or
/// `stitch` has ended — the Phase 2 counterpart to `RunManifest`, which reads
/// `scanny-boy-manifest.json`.
///
/// `shared/contract/roll-manifest.schema.json` is the authoritative
/// definition; this type deliberately decodes only the fields the completion
/// UI shows and ignores the rest, for the same reason `RunManifest` does: a
/// roll manifest that grows a field must not stop the app reading the ones it
/// needs.
///
/// `status` and the per-negative statuses are kept as the raw contract
/// strings rather than Swift enumerations, again mirroring `RunManifest`: the
/// only distinction the app actually draws — `running` versus anything final
/// (section 3.7) — is exact either way.
struct RollManifest: Decodable, Sendable, Hashable {
    static let filename = "scanny-boy-roll.json"
    static let runningStatus = "running"

    struct Output: Decodable, Sendable, Hashable {
        let name: String
        let size: Int
        let sha256: String
        let width: Int
        let height: Int
    }

    struct Negative: Decodable, Sendable, Hashable {
        let negativeID: String
        let members: [String]
        let expectedOutput: String
        /// `pending`, `completed`, or `failed`.
        let status: String
        let output: Output?
        let errorCode: String?
        let errorMessage: String?

        var isCompleted: Bool { status == "completed" }
        var isFailed: Bool { status == "failed" }

        enum CodingKeys: String, CodingKey {
            case negativeID = "negative_id"
            case members
            case expectedOutput = "expected_output"
            case status
            case output
            case errorCode = "error_code"
            case errorMessage = "error_message"
        }
    }

    let manifestFormatVersion: Int
    let scannyBoyVersion: String
    let runID: String
    /// `running`, `partial`, `cancelled`, or `complete`.
    let status: String
    let filmDate: String
    let negatives: [Negative]
    let finishedAt: String?

    /// Section 3.7's roll-manifest counterpart to `RunManifest.isRunning`: a
    /// forced stop cannot update this manifest either, so one left `running`
    /// is the signature of a run whose cleanup never happened.
    var isRunning: Bool { status == Self.runningStatus }

    /// Every stitched TIFF the manifest records as published, in negative
    /// order.
    var publishedOutputs: [String] { negatives.compactMap { $0.output?.name } }

    enum CodingKeys: String, CodingKey {
        case manifestFormatVersion = "manifest_format_version"
        case scannyBoyVersion = "scanny_boy_version"
        case runID = "run_id"
        case status
        case filmDate = "film_date"
        case negatives
        case finishedAt = "finished_at"
    }

    /// Reads the roll manifest from an output folder. `nonisolated` and
    /// `Sendable` so callers can do this file work off the main actor.
    static func read(inOutputFolder folder: URL) throws -> RollManifest {
        let url = folder.appending(path: filename, directoryHint: .notDirectory)
        return try JSONDecoder().decode(RollManifest.self, from: try Data(contentsOf: url))
    }
}

/// What reading the roll manifest after a `run` or `stitch` told the app.
/// The `RunManifest`/`ManifestReport` counterpart, over `RollManifest`.
enum RollManifestReport: Sendable, Hashable {
    /// A manifest in a final state: `complete`, `partial`, or `cancelled`.
    case final(RollManifest)
    /// A manifest still marked `running`. Accepted, not treated as corrupt:
    /// it means the CLI never got to finish, so a staging directory from this
    /// run may still be on disk in the output folder.
    case cleanupIncomplete(RollManifest)
    /// No roll manifest could be read. The string says why.
    case unavailable(String)

    var manifest: RollManifest? {
        switch self {
        case .final(let manifest), .cleanupIncomplete(let manifest): manifest
        case .unavailable: nil
        }
    }

    init(manifest: RollManifest) {
        self = manifest.isRunning ? .cleanupIncomplete(manifest) : .final(manifest)
    }

    /// One sentence for the completion UI.
    var summary: String {
        switch self {
        case .final(let manifest):
            "The roll's manifest is recorded as \(manifest.status)."
        case .cleanupIncomplete:
            """
            Cleanup did not finish: the roll manifest is still marked \
            \(RollManifest.runningStatus), so a staging file for this run may \
            remain in the output folder. The next run or re-stitch removes it \
            and recomposites the negative that was interrupted. Published \
            negatives are left alone.
            """
        case .unavailable(let reason):
            "The roll's manifest could not be read: \(reason)"
        }
    }
}
