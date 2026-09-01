import Foundation

/// One line of the CLI's stdout event stream.
///
/// `shared/contract/CONTRACT.md` and `shared/contract/schema.json` define the
/// format; this type must stay consistent with them. Every known field is
/// exposed through a typed accessor, and the complete decoded line is kept in
/// `fields` so an event type or a field this version has never seen still
/// reaches the app intact rather than failing the stream.
public struct CLIEvent: Sendable, Hashable {
    /// The only protocol version this app understands. A stream announcing
    /// anything else is rejected rather than guessed at.
    public static let supportedProtocolVersion = 5

    public let protocolVersion: Int
    public let kind: Kind
    public let runID: String?
    /// The whole decoded line, including fields with no typed accessor.
    public let fields: [String: JSONValue]

    public enum Kind: Sendable, Hashable {
        case started
        case probeResult
        case progress
        case itemDone
        case groupDone
        case groupFailed
        case warning
        case error
        case finished
        case negativeDone
        case negativeFailed
        case rollCreated
        case rollList
        case rollInfo
        case rollRenamed
        case metadataApplied
        case metadataSkipped
        case editRecorded
        case exportDone
        /// An event type this version of the app does not know. Its fields are
        /// still preserved.
        case unknown(String)

        public init(name: String) {
            switch name {
            case "started": self = .started
            case "probe_result": self = .probeResult
            case "progress": self = .progress
            case "item_done": self = .itemDone
            case "group_done": self = .groupDone
            case "group_failed": self = .groupFailed
            case "warning": self = .warning
            case "error": self = .error
            case "finished": self = .finished
            case "negative_done": self = .negativeDone
            case "negative_failed": self = .negativeFailed
            case "roll_created": self = .rollCreated
            case "roll_list": self = .rollList
            case "roll_info": self = .rollInfo
            case "roll_renamed": self = .rollRenamed
            case "metadata_applied": self = .metadataApplied
            case "metadata_skipped": self = .metadataSkipped
            case "edit_recorded": self = .editRecorded
            case "export_done": self = .exportDone
            default: self = .unknown(name)
            }
        }

        public var name: String {
            switch self {
            case .started: "started"
            case .probeResult: "probe_result"
            case .progress: "progress"
            case .itemDone: "item_done"
            case .groupDone: "group_done"
            case .groupFailed: "group_failed"
            case .warning: "warning"
            case .error: "error"
            case .finished: "finished"
            case .negativeDone: "negative_done"
            case .negativeFailed: "negative_failed"
            case .rollCreated: "roll_created"
            case .rollList: "roll_list"
            case .rollInfo: "roll_info"
            case .rollRenamed: "roll_renamed"
            case .metadataApplied: "metadata_applied"
            case .metadataSkipped: "metadata_skipped"
            case .editRecorded: "edit_recorded"
            case .exportDone: "export_done"
            case .unknown(let name): name
            }
        }

        public var isKnown: Bool {
            if case .unknown = self { return false }
            return true
        }
    }

    public init(line: String) throws {
        guard let data = line.data(using: .utf8) else {
            throw CLIEventDecodingError.notUTF8
        }
        let decoded: JSONValue
        do {
            decoded = try JSONDecoder().decode(JSONValue.self, from: data)
        } catch {
            throw CLIEventDecodingError.malformedJSON(String(describing: error))
        }
        guard let object = decoded.objectValue else {
            throw CLIEventDecodingError.notAnObject
        }
        guard let version = object["protocol_version"]?.intValue else {
            throw CLIEventDecodingError.missingProtocolVersion
        }
        guard version == Self.supportedProtocolVersion else {
            throw CLIEventDecodingError.unsupportedProtocolVersion(version)
        }
        guard let name = object["event"]?.stringValue else {
            throw CLIEventDecodingError.missingEventType
        }

        self.protocolVersion = version
        self.kind = Kind(name: name)
        self.runID = object["run_id"]?.stringValue
        self.fields = object
    }
}

extension CLIEvent {
    // `started`
    public var command: String? { fields["command"]?.stringValue }

    // `probe_result`
    public var catalogue: [String]? { fields["catalogue"]?.stringArrayValue }
    public var warnings: [String]? { fields["warnings"]?.stringArrayValue }
    public var groups: [[String]]? { fields["groups"]?.nestedStringArrayValue }
    // `probe_result`, present only when `--out` was given alongside `--files`
    // (CONTRACT.md: output-folder validation, disk estimate, and
    // overwrite-conflict preview).
    public var outputConflicts: [String]? { fields["output_conflicts"]?.stringArrayValue }
    public var estimatedRequiredBytes: Int? { fields["estimated_required_bytes"]?.intValue }
    public var availableBytes: Int? { fields["available_bytes"]?.intValue }

    // `roll_created`
    public var rollID: String? { fields["roll_id"]?.stringValue }
    public var rollName: String? { fields["roll_name"]?.stringValue }
    public var rollPath: String? { fields["path"]?.stringValue }

    // `roll_list`
    public var rolls: [[String: JSONValue]]? {
        fields["rolls"]?.arrayValue?.compactMap { entry in
            entry.objectValue
        }
    }

    // `roll_info`
    public var manifest: [String: JSONValue]? { fields["manifest"]?.objectValue }

    // `progress` and `item_done`
    public var sourceIndex: Int? { fields["source_index"]?.intValue }
    public var step: CLIPipelineStep? {
        fields["step"]?.stringValue.map(CLIPipelineStep.init(name:))
    }
    public var completed: Int? { fields["completed"]?.intValue }
    public var total: Int? { fields["total"]?.intValue }
    public var output: String? { fields["output"]?.stringValue }
    // `progress`
    public var stage: String? { fields["stage"]?.stringValue }

    // `group_done` and `group_failed`
    public var groupID: String? { fields["group_id"]?.stringValue }

    // `warning`, `error`, `group_failed`, and `negative_failed`
    public var code: CLICode? {
        fields["code"]?.stringValue.map(CLICode.init(name:))
    }
    public var message: String? { fields["message"]?.stringValue }

    // `finished`
    public var status: String? { fields["status"]?.stringValue }
    public var exitStatus: Int? { fields["exit_status"]?.intValue }

    // `negative_done` and `negative_failed`
    public var negativeID: String? { fields["negative_id"]?.stringValue }
    // `negative_done`
    public var width: Int? { fields["width"]?.intValue }
    public var height: Int? { fields["height"]?.intValue }
    public var globalRMS: Double? { fields["global_rms_px"]?.doubleValue }
    public var maxOverlapMAD: Double? { fields["max_overlap_mad"]?.doubleValue }

    // `edit_recorded`: the appended ops-log row and the negative's net
    // rotation after it.
    public var edit: [String: JSONValue]? { fields["edit"]?.objectValue }
    public var rotationQuarterTurns: Int? { fields["rotation_quarter_turns"]?.intValue }
    public var previewPath: String? { fields["preview_path"]?.stringValue }
}

/// One pipeline step, from the plan's Vocabulary section. The last seven
/// cases are the stitch stage's steps, added by Phase 2 section 3.9.
public enum CLIPipelineStep: Sendable, Hashable {
    case decode
    case writeTIFF
    case addMetadata
    case load
    case detect
    case match
    case solve
    case warp
    case blend
    case writeStitched
    case unknown(String)

    public init(name: String) {
        switch name {
        case "decode": self = .decode
        case "write_tiff": self = .writeTIFF
        case "add_metadata": self = .addMetadata
        case "load": self = .load
        case "detect": self = .detect
        case "match": self = .match
        case "solve": self = .solve
        case "warp": self = .warp
        case "blend": self = .blend
        case "write_stitched": self = .writeStitched
        default: self = .unknown(name)
        }
    }

    public var name: String {
        switch self {
        case .decode: "decode"
        case .writeTIFF: "write_tiff"
        case .addMetadata: "add_metadata"
        case .load: "load"
        case .detect: "detect"
        case .match: "match"
        case .solve: "solve"
        case .warp: "warp"
        case .blend: "blend"
        case .writeStitched: "write_stitched"
        case .unknown(let name): name
        }
    }
}

/// A stable error or warning code from CONTRACT.md.
///
/// `unknown` exists for the same reason `CLIEvent.Kind.unknown` does: a newer
/// CLI may report a code this app predates, and dropping the event would be
/// worse than showing an unfamiliar code.
public enum CLICode: Sendable, Hashable {
    case noFiles
    case nonContiguousSelection
    case notDivisible
    case invalidPerNegative
    case missingCaptureTime
    case filenameSortUsed
    case unsupportedRAW
    case captureMetadataMissing
    case captureSettingsDiffer
    case unreadableRAW
    case outputSameAsInput
    case outputNotWritable
    case outputNotEmpty
    case outputConflict
    case insufficientDisk
    case insufficientMemory
    case badManifest
    case manifestMismatch
    case iccProfileInvalid
    case tiffWriteFailed
    case cancelled
    // Phase 2 section 3.10.
    case workSameAsOutput
    case workManifestUnusable
    case intermediateMissing
    case intermediateChanged
    case stitchInsufficientMatches
    case stitchUnderconstrained
    case stitchResidualTooHigh
    case stitchOutputTooLarge
    case stitchFailed
    case stitchScaleDrift
    case stitchLayoutUnexpected
    case stitchRebateCheckFailed
    case outputDimensionsLarge
    case rollNotFound
    case rollManifestUnsupported
    case rollExists
    case rollRenameFailed
    case rollInvariantMismatch
    case perNegativeLocked
    case outputModifiedExternally
    case metadataWriteFailed
    case orphanFileNotRemoved
    case negativeNotFound
    case invalidEdit
    case exportFailed
    case previewFailed
    case libraryDBUnsupported
    case internalError
    case unknown(String)

    public init(name: String) {
        switch name {
        case "NO_FILES": self = .noFiles
        case "NON_CONTIGUOUS_SELECTION": self = .nonContiguousSelection
        case "NOT_DIVISIBLE": self = .notDivisible
        case "INVALID_PER_NEGATIVE": self = .invalidPerNegative
        case "MISSING_CAPTURE_TIME": self = .missingCaptureTime
        case "FILENAME_SORT_USED": self = .filenameSortUsed
        case "UNSUPPORTED_RAW": self = .unsupportedRAW
        case "CAPTURE_METADATA_MISSING": self = .captureMetadataMissing
        case "CAPTURE_SETTINGS_DIFFER": self = .captureSettingsDiffer
        case "UNREADABLE_RAW": self = .unreadableRAW
        case "OUTPUT_SAME_AS_INPUT": self = .outputSameAsInput
        case "OUTPUT_NOT_WRITABLE": self = .outputNotWritable
        case "OUTPUT_NOT_EMPTY": self = .outputNotEmpty
        case "OUTPUT_CONFLICT": self = .outputConflict
        case "INSUFFICIENT_DISK": self = .insufficientDisk
        case "INSUFFICIENT_MEMORY": self = .insufficientMemory
        case "BAD_MANIFEST": self = .badManifest
        case "MANIFEST_MISMATCH": self = .manifestMismatch
        case "ICC_PROFILE_INVALID": self = .iccProfileInvalid
        case "TIFF_WRITE_FAILED": self = .tiffWriteFailed
        case "CANCELLED": self = .cancelled
        case "WORK_SAME_AS_OUTPUT": self = .workSameAsOutput
        case "WORK_MANIFEST_UNUSABLE": self = .workManifestUnusable
        case "INTERMEDIATE_MISSING": self = .intermediateMissing
        case "INTERMEDIATE_CHANGED": self = .intermediateChanged
        case "STITCH_INSUFFICIENT_MATCHES": self = .stitchInsufficientMatches
        case "STITCH_UNDERCONSTRAINED": self = .stitchUnderconstrained
        case "STITCH_RESIDUAL_TOO_HIGH": self = .stitchResidualTooHigh
        case "STITCH_OUTPUT_TOO_LARGE": self = .stitchOutputTooLarge
        case "STITCH_FAILED": self = .stitchFailed
        case "STITCH_SCALE_DRIFT": self = .stitchScaleDrift
        case "STITCH_LAYOUT_UNEXPECTED": self = .stitchLayoutUnexpected
        case "STITCH_REBATE_CHECK_FAILED": self = .stitchRebateCheckFailed
        case "OUTPUT_DIMENSIONS_LARGE": self = .outputDimensionsLarge
        case "ROLL_NOT_FOUND": self = .rollNotFound
        case "ROLL_MANIFEST_UNSUPPORTED": self = .rollManifestUnsupported
        case "ROLL_EXISTS": self = .rollExists
        case "ROLL_RENAME_FAILED": self = .rollRenameFailed
        case "ROLL_INVARIANT_MISMATCH": self = .rollInvariantMismatch
        case "PER_NEGATIVE_LOCKED": self = .perNegativeLocked
        case "OUTPUT_MODIFIED_EXTERNALLY": self = .outputModifiedExternally
        case "METADATA_WRITE_FAILED": self = .metadataWriteFailed
        case "ORPHAN_FILE_NOT_REMOVED": self = .orphanFileNotRemoved
        case "NEGATIVE_NOT_FOUND": self = .negativeNotFound
        case "INVALID_EDIT": self = .invalidEdit
        case "EXPORT_FAILED": self = .exportFailed
        case "PREVIEW_FAILED": self = .previewFailed
        case "LIBRARY_DB_UNSUPPORTED": self = .libraryDBUnsupported
        case "INTERNAL_ERROR": self = .internalError
        default: self = .unknown(name)
        }
    }

    public var name: String {
        switch self {
        case .noFiles: "NO_FILES"
        case .nonContiguousSelection: "NON_CONTIGUOUS_SELECTION"
        case .notDivisible: "NOT_DIVISIBLE"
        case .invalidPerNegative: "INVALID_PER_NEGATIVE"
        case .missingCaptureTime: "MISSING_CAPTURE_TIME"
        case .filenameSortUsed: "FILENAME_SORT_USED"
        case .unsupportedRAW: "UNSUPPORTED_RAW"
        case .captureMetadataMissing: "CAPTURE_METADATA_MISSING"
        case .captureSettingsDiffer: "CAPTURE_SETTINGS_DIFFER"
        case .unreadableRAW: "UNREADABLE_RAW"
        case .outputSameAsInput: "OUTPUT_SAME_AS_INPUT"
        case .outputNotWritable: "OUTPUT_NOT_WRITABLE"
        case .outputNotEmpty: "OUTPUT_NOT_EMPTY"
        case .outputConflict: "OUTPUT_CONFLICT"
        case .insufficientDisk: "INSUFFICIENT_DISK"
        case .insufficientMemory: "INSUFFICIENT_MEMORY"
        case .badManifest: "BAD_MANIFEST"
        case .manifestMismatch: "MANIFEST_MISMATCH"
        case .iccProfileInvalid: "ICC_PROFILE_INVALID"
        case .tiffWriteFailed: "TIFF_WRITE_FAILED"
        case .cancelled: "CANCELLED"
        case .workSameAsOutput: "WORK_SAME_AS_OUTPUT"
        case .workManifestUnusable: "WORK_MANIFEST_UNUSABLE"
        case .intermediateMissing: "INTERMEDIATE_MISSING"
        case .intermediateChanged: "INTERMEDIATE_CHANGED"
        case .stitchInsufficientMatches: "STITCH_INSUFFICIENT_MATCHES"
        case .stitchUnderconstrained: "STITCH_UNDERCONSTRAINED"
        case .stitchResidualTooHigh: "STITCH_RESIDUAL_TOO_HIGH"
        case .stitchOutputTooLarge: "STITCH_OUTPUT_TOO_LARGE"
        case .stitchFailed: "STITCH_FAILED"
        case .stitchScaleDrift: "STITCH_SCALE_DRIFT"
        case .stitchLayoutUnexpected: "STITCH_LAYOUT_UNEXPECTED"
        case .stitchRebateCheckFailed: "STITCH_REBATE_CHECK_FAILED"
        case .outputDimensionsLarge: "OUTPUT_DIMENSIONS_LARGE"
        case .rollNotFound: "ROLL_NOT_FOUND"
        case .rollManifestUnsupported: "ROLL_MANIFEST_UNSUPPORTED"
        case .rollExists: "ROLL_EXISTS"
        case .rollRenameFailed: "ROLL_RENAME_FAILED"
        case .rollInvariantMismatch: "ROLL_INVARIANT_MISMATCH"
        case .perNegativeLocked: "PER_NEGATIVE_LOCKED"
        case .outputModifiedExternally: "OUTPUT_MODIFIED_EXTERNALLY"
        case .metadataWriteFailed: "METADATA_WRITE_FAILED"
        case .orphanFileNotRemoved: "ORPHAN_FILE_NOT_REMOVED"
        case .negativeNotFound: "NEGATIVE_NOT_FOUND"
        case .invalidEdit: "INVALID_EDIT"
        case .exportFailed: "EXPORT_FAILED"
        case .previewFailed: "PREVIEW_FAILED"
        case .libraryDBUnsupported: "LIBRARY_DB_UNSUPPORTED"
        case .internalError: "INTERNAL_ERROR"
        case .unknown(let name): name
        }
    }
}

/// Why one stdout line could not be read as an event. An unknown *event type*
/// is not one of these: that decodes successfully as `Kind.unknown`.
public enum CLIEventDecodingError: Error, Sendable, Hashable {
    case notUTF8
    case malformedJSON(String)
    case notAnObject
    case missingProtocolVersion
    case unsupportedProtocolVersion(Int)
    case missingEventType
}

extension CLIEventDecodingError: CustomStringConvertible {
    public var description: String {
        switch self {
        case .notUTF8:
            "the line was not valid UTF-8"
        case .malformedJSON(let detail):
            "the line was not valid JSON: \(detail)"
        case .notAnObject:
            "the line was valid JSON but not an object"
        case .missingProtocolVersion:
            "the line has no integer `protocol_version`"
        case .unsupportedProtocolVersion(let version):
            """
            the line announces protocol version \(version); this app \
            understands version \(CLIEvent.supportedProtocolVersion)
            """
        case .missingEventType:
            "the line has no string `event`"
        }
    }
}
