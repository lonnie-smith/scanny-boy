import Foundation

/// The parts of `scanny-boy-manifest.json` the app reads back once a run has
/// ended.
///
/// `shared/contract/manifest.schema.json` is the authoritative definition;
/// this type deliberately decodes only the fields the completion UI shows and
/// ignores the rest, so a manifest that grows a field does not stop the app
/// reading the ones it needs.
///
/// `status` and the group statuses are kept as the raw contract strings rather
/// than Swift enumerations. A newer CLI adding a status must not turn the
/// whole manifest into an unreadable one, and the only distinction this app
/// actually has to draw — `running` versus anything final (section 3.7) — is
/// exact either way.
struct RunManifest: Decodable, Sendable, Hashable {
    static let filename = "scanny-boy-manifest.json"
    static let runningStatus = "running"

    struct Output: Decodable, Sendable, Hashable {
        let name: String
        let size: Int
        let sha256: String
    }

    struct Group: Decodable, Sendable, Hashable {
        let groupID: String
        let members: [String]
        let expectedOutputs: [String]
        /// `pending`, `completed`, or `failed`.
        let status: String
        let outputs: [Output]
        let errorCode: String?
        let errorMessage: String?

        var isCompleted: Bool { status == "completed" }
        var isFailed: Bool { status == "failed" }

        enum CodingKeys: String, CodingKey {
            case groupID = "group_id"
            case members
            case expectedOutputs = "expected_outputs"
            case status
            case outputs
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
    let shotsPerNegative: Int
    let groups: [Group]
    let finishedAt: String?

    /// Section 3.8: a forced stop cannot update the manifest, so one left as
    /// `running` is the signature of a run whose cleanup never happened.
    var isRunning: Bool { status == Self.runningStatus }

    /// Every output the manifest records as published, in group order.
    var publishedOutputs: [String] { groups.flatMap { $0.outputs.map(\.name) } }

    enum CodingKeys: String, CodingKey {
        case manifestFormatVersion = "manifest_format_version"
        case scannyBoyVersion = "scanny_boy_version"
        case runID = "run_id"
        case status
        case filmDate = "film_date"
        case shotsPerNegative = "shots_per_negative"
        case groups
        case finishedAt = "finished_at"
    }

    /// Reads the manifest from an output folder. `nonisolated` and `Sendable`
    /// so the run model can do this file work off the main actor.
    static func read(inOutputFolder folder: URL) throws -> RunManifest {
        let url = folder.appending(path: filename, directoryHint: .notDirectory)
        return try JSONDecoder().decode(RunManifest.self, from: try Data(contentsOf: url))
    }
}

/// What reading the manifest after a run told the app.
///
/// Chunk 10: "Read a final manifest for normal completion and cooperative
/// cancellation. After a forced stop, accept a stale `running` manifest,
/// report that cleanup was incomplete, and explain that the next run will
/// recover it."
enum ManifestReport: Sendable, Hashable {
    /// A manifest in a final state: `complete`, `partial`, or `cancelled`.
    case final(RunManifest)
    /// A manifest still marked `running`. Accepted, not treated as corrupt:
    /// it means the CLI never got to finish, so staging directories from this
    /// run may still be on disk.
    case cleanupIncomplete(RunManifest)
    /// No manifest could be read. The string says why.
    case unavailable(String)

    var manifest: RunManifest? {
        switch self {
        case .final(let manifest), .cleanupIncomplete(let manifest): manifest
        case .unavailable: nil
        }
    }

    init(manifest: RunManifest) {
        self = manifest.isRunning ? .cleanupIncomplete(manifest) : .final(manifest)
    }

    /// One sentence for the completion UI.
    var summary: String {
        switch self {
        case .final(let manifest):
            "The run's manifest is recorded as \(manifest.status)."
        case .cleanupIncomplete:
            """
            Cleanup did not finish: the manifest is still marked \
            \(RunManifest.runningStatus), so this run's staging directory may \
            remain in the output folder. The next run removes it and \
            reconverts the negative that was interrupted. Published files are \
            left alone.
            """
        case .unavailable(let reason):
            "The run's manifest could not be read: \(reason)"
        }
    }
}
