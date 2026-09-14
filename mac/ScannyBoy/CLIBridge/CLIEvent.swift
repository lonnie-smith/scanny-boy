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
    /// anything else is rejected rather than guessed at. Protocol 9 added
    /// two features: the extended-metadata editing feature (the `metadata`
    /// command family — `metadata_updated`, `metadata_values`, the
    /// `INVALID_METADATA` code — and the roll/negative extended-metadata
    /// fields in the roll manifest), and `edit render-region` with its
    /// `region_rendered` event: a 1:1 PNG of one display-space region of
    /// a published TIFF, for the 100% zoom. Protocol 10 adds 2D grid
    /// stitching (the `--grid AxD` flag on `probe`, `prepare`, and `run`,
    /// the `INVALID_GRID` error code) and the preview's
    /// nondestructive tone adjustment (the `edit tone` command and the
    /// `tone_snap_gamma` fields in the roll manifest).
    /// Protocol 17 retires `STITCH_GRID_ORDER_UNEXPECTED`.
    /// Protocol 15 retires the film-kind auto-detector: `--film-kind` is
    /// required on `roll init` only; `run`/`stitch` read `film.kind` from
    /// the manifest. Protocol 11 added monochrome film support (single-
    /// channel published TIFFs and the `ScannyBoy-Density-Grey-v1.icc`
    /// profile), the preview tone extension (seven curve controls and two
    /// auto flags on `edit tone`, the matching `tone_*` fields in the roll
    /// manifest, and the `TONE_METERING_UNAVAILABLE` warning code), and
    /// the positive/negative display toggle for the Edit tab (`--mode
    /// positive|negative` on `edit render-region`, the `edit render-preview`
    /// command with its `preview_rendered` event — the negative mode being
    /// the un-inverted density view the tone adjustment never reaches).
    /// Protocol 11 is also the colour-managed JPEG XL export (see
    /// `events.py`): the `JXL_ENCODER_UNAVAILABLE`/`CAMERA_MATRIX_MISSING`
    /// error codes, the `CAMERA_MATRIX_CONFLICT` warning, the roll
    /// manifest's optional `camera_color` block, and the work manifest
    /// curated block's `rgb_xyz_matrix`/`camera_model`.
    /// Protocol 12 adds the preview colour adjustment (`edit color`, the
    /// `color_*` fields in the roll manifest, and the `color` op in the
    /// ops log). Protocol 13 adds spotting: the three
    /// `edit detect-spots` / `edit spots` / `edit list-spots` commands,
    /// the `spots_reported` event — whose spot rects are **display
    /// space**, already transformed, so Swift converts no coordinates and
    /// rejects by `id` only — the `SPOT_LIMIT_REACHED` and `SPOTS_STALE`
    /// warning codes, and the per-negative `spots` summary block on
    /// `roll info`. The same protocol 13 also carries the film-base
    /// reference: the `roll set-base-frame` command with its
    /// `base_frame_set` event, the `film_base` block on the roll manifest
    /// (reported verbatim by `roll info` and by `probe --roll`), the ten
    /// `FILM_BASE_*` / `ROLL_PREDATES_FILM_BASE` codes, and the per-negative
    /// `base_check` meters. Protocol 14 extends `edit color` with
    /// `--cast-removal-highlights` and
    /// `--auto-balance`, adds the derived `color_cast_removal_highlights`
    /// field to `roll info`, and records the `highlight_refs` /
    /// `neutral_residual` meters in the per-negative `normalization`
    /// block; global and regional CMY are now mean-removed. No new
    /// event kinds the app must decode — the new work is CLI-side.
    /// Protocol 18 adds the resident helper:
    /// events answered by `scanny-boy serve` carry an optional
    /// `request_id`, and each served request ends with a `finished`
    /// carrying it and the one-shot exit status. Optional, because a
    /// one-shot invocation has no daemon to scope an event to. The same
    /// bump adds the film-extent pass:
    /// the `NORMALIZE_FILM_EXTENT_WITHHELD` and
    /// `NORMALIZE_FILM_EXTENT_EXCESSIVE` warning codes. Protocol 24 replaces
    /// the regional CMY colour balance with warmth/tint plus per-channel
    /// curves: `edit color`'s `--warmth`/`--tint`/`--red-*`/`--green-*`/
    /// `--blue-*` flags and `--auto-balance`, and the matching
    /// `color_warmth`/`color_tint`/`color_curve_*` roll manifest fields.
    public static let supportedProtocolVersion = 24

    public let protocolVersion: Int
    public let kind: Kind
    public let runID: String?
    /// The served request this event belongs to, when the event came from
    /// the resident helper; `nil` on a one-shot invocation's stream.
    public let requestID: String?
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
        case rollDeleted
        case metadataApplied
        case metadataSkipped
        case metadataUpdated
        case metadataValues
        case editRecorded
        case negativeDeleted
        case regionRendered
        case previewRendered
        case exportDone
        case rigCreated
        case rigList
        case rigDeleted
        case rigProgress
        case flatFieldReferenceSet
        case gridCreated
        case gridList
        case gridDeleted
        case spotsReported
        case scratchesReported
        case cropSuggested
        case baseFrameSet
        case frameAnalyzed
        case captureChecked
        case captureSummary
        case rollRefreshed
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
            case "roll_deleted": self = .rollDeleted
            case "metadata_applied": self = .metadataApplied
            case "metadata_skipped": self = .metadataSkipped
            case "metadata_updated": self = .metadataUpdated
            case "metadata_values": self = .metadataValues
            case "edit_recorded": self = .editRecorded
            case "negative_deleted": self = .negativeDeleted
            case "region_rendered": self = .regionRendered
            case "preview_rendered": self = .previewRendered
            case "export_done": self = .exportDone
            case "rig_created": self = .rigCreated
            case "rig_list": self = .rigList
            case "rig_deleted": self = .rigDeleted
            case "rig_progress": self = .rigProgress
            case "flat_field_reference_set": self = .flatFieldReferenceSet
            case "grid_created": self = .gridCreated
            case "grid_list": self = .gridList
            case "grid_deleted": self = .gridDeleted
            case "spots_reported": self = .spotsReported
            case "scratches_reported": self = .scratchesReported
            case "crop_suggested": self = .cropSuggested
            case "base_frame_set": self = .baseFrameSet
            case "frame_analyzed": self = .frameAnalyzed
            case "capture_checked": self = .captureChecked
            case "capture_summary": self = .captureSummary
            case "roll_refreshed": self = .rollRefreshed
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
            case .rollDeleted: "roll_deleted"
            case .metadataApplied: "metadata_applied"
            case .metadataSkipped: "metadata_skipped"
            case .metadataUpdated: "metadata_updated"
            case .metadataValues: "metadata_values"
            case .editRecorded: "edit_recorded"
            case .negativeDeleted: "negative_deleted"
            case .regionRendered: "region_rendered"
            case .previewRendered: "preview_rendered"
            case .exportDone: "export_done"
            case .rigCreated: "rig_created"
            case .rigList: "rig_list"
            case .rigDeleted: "rig_deleted"
            case .rigProgress: "rig_progress"
            case .flatFieldReferenceSet: "flat_field_reference_set"
            case .gridCreated: "grid_created"
            case .gridList: "grid_list"
            case .gridDeleted: "grid_deleted"
            case .spotsReported: "spots_reported"
            case .scratchesReported: "scratches_reported"
            case .cropSuggested: "crop_suggested"
            case .baseFrameSet: "base_frame_set"
            case .frameAnalyzed: "frame_analyzed"
            case .captureChecked: "capture_checked"
            case .captureSummary: "capture_summary"
            case .rollRefreshed: "roll_refreshed"
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
        self.requestID = object["request_id"]?.stringValue
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
    /// Present when `--roll` was given.
    public var filmBase: [String: JSONValue]? { fields["film_base"]?.objectValue }
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
    public var refreshPending: Bool? { fields["refresh_pending"]?.boolValue }

    // `frame_analyzed`
    public var analyzedFrame: String? { fields["frame"]?.stringValue }
    public var focusRegions: [Double?]? {
        guard let elements = fields["focus_regions"]?.arrayValue else { return nil }
        return elements.map { element in
            if case .null = element { return nil }
            return element.doubleValue
        }
    }

    // `capture_checked`
    public var captureCheckPassed: Bool? { fields["passed"]?.boolValue }
    public var usedCLAHEFallback: Bool? { fields["used_clahe_fallback"]?.boolValue }

    // `capture_summary`
    public var captureSummaryFrames: Int? { fields["frames"]?.intValue }

    // `roll_refreshed`
    public var lockChanged: Bool? { fields["lock_changed"]?.boolValue }
    public var previewsRegenerated: Bool? { fields["previews_regenerated"]?.boolValue }

    // `metadata_values`
    public var metadataField: String? { fields["field"]?.stringValue }
    public var metadataFieldValues: [String]? {
        fields["values"]?.arrayValue?.compactMap { $0.stringValue }
    }

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

    // `base_frame_set`
    public var baseFrameSourceName: String? { fields["source_name"]?.stringValue }
    public var baseFrameDensity: [Double]? {
        fields["density"]?.arrayValue?.compactMap(\.doubleValue)
    }
    public var baseFrameAreaFraction: Double? { fields["area_fraction"]?.doubleValue }
    public var baseFramePopulationCount: Int? { fields["population_count"]?.intValue }
    public var baseFrameLocked: Bool? { fields["locked"]?.boolValue }

    // `edit_recorded`: the appended ops-log row and the negative's net
    // transform after it (quarter turns plus the horizontal-mirror flag —
    // a flip and a rotation do not commute, so one number cannot carry
    // both).
    public var edit: [String: JSONValue]? { fields["edit"]?.objectValue }
    public var rotationQuarterTurns: Int? { fields["rotation_quarter_turns"]?.intValue }
    public var flippedHorizontally: Bool? { fields["flipped_horizontally"]?.boolValue }
    public var previewPath: String? { fields["preview_path"]?.stringValue }

    /// The net crop state, carried by *every* `edit_recorded` (protocol
    /// version 19) as a full report — `null` for no live crop — so the
    /// app can overwrite without caring which op was recorded. The outer
    /// optional is "the field is absent" (a pre-19 CLI); the inner is the
    /// report's own null.
    public var crop: CropState?? {
        guard case .some = fields["crop"] else { return nil }
        guard let object = fields["crop"]?.objectValue else { return .some(nil) }
        return .some(CropState(fields: object))
    }

    /// The recorded op's tone params, when it is a `tone` op: its `params`
    /// always name all four user keys (explicit nulls for the reset to the
    /// default scan-start curve). The geometric ops carry no tone keys, so
    /// `nil` here means "the negative's tone state is untouched".
    public var recordedTone: ToneAdjustment?? {
        guard let params = edit?["params"]?.objectValue,
            case .some = params["snap_gamma"]
        else { return nil }
        guard let snapGamma = params["snap_gamma"]?.doubleValue else { return .some(nil) }
        return .some(
            ToneAdjustment(
                snapGamma: snapGamma,
                density: params["density"]?.doubleValue ?? ToneAdjustment.neutral.density,
                shadowDensity: params["shadow_density"]?.doubleValue ?? 0,
                highlightDensity: params["highlight_density"]?.doubleValue ?? 0
            )
        )
    }

    /// The recorded op's colour params when it is a `color` op.
    public var recordedColor: ColorAdjustment?? {
        guard let params = edit?["params"]?.objectValue,
            case .some = params["warmth"]
        else { return nil }
        guard let warmth = params["warmth"]?.doubleValue,
            let tint = params["tint"]?.doubleValue
        else { return .some(nil) }
        return .some(
            ColorAdjustment(
                warmth: warmth,
                tint: tint,
                curveRed25: params["curve_red_25"]?.doubleValue ?? 0,
                curveRed50: params["curve_red_50"]?.doubleValue ?? 0,
                curveRed75: params["curve_red_75"]?.doubleValue ?? 0,
                curveGreen25: params["curve_green_25"]?.doubleValue ?? 0,
                curveGreen50: params["curve_green_50"]?.doubleValue ?? 0,
                curveGreen75: params["curve_green_75"]?.doubleValue ?? 0,
                curveBlue25: params["curve_blue_25"]?.doubleValue ?? 0,
                curveBlue50: params["curve_blue_50"]?.doubleValue ?? 0,
                curveBlue75: params["curve_blue_75"]?.doubleValue ?? 0,
                castRemoval: params["cast_removal"]?.doubleValue ?? 0,
                castRemovalHighlights: params["cast_removal_highlights"]?.doubleValue ?? 0,
                dyeSeparation: params["dye_separation"]?.doubleValue
                    ?? ColorAdjustment.neutral.dyeSeparation,
                separationDamping: params["separation_damping"]?.doubleValue ?? 0
            )
        )
    }

    // `region_rendered`: the 1:1 PNG's path and the rect actually rendered,
    // post-clamp, in display space.
    public var regionPath: String? { fields["path"]?.stringValue }
    public var regionX: Int? { fields["x"]?.intValue }
    public var regionY: Int? { fields["y"]?.intValue }

    // `preview_rendered`: the whole-display PNG's path (protocol version
    // 11's `edit render-preview`); its `width`/`height` ride the shared
    // `width`/`height` accessors above.
    public var previewRenderedPath: String? { fields["path"]?.stringValue }

    // `rig_created` and `rig_list`
    public var rigProfile: [String: JSONValue]? { fields["profile"]?.objectValue }
    public var rigProfiles: [[String: JSONValue]]? {
        fields["profiles"]?.arrayValue?.compactMap { entry in
            entry.objectValue
        }
    }
    // `rig_deleted`
    public var rigProfileID: String? { fields["profile_id"]?.stringValue }

    // `rig_progress`
    public var rigPhase: String? { fields["phase"]?.stringValue }

    // `flat_field_reference_set`
    public var flatFieldReferenceSourceName: String? { fields["source_name"]?.stringValue }
    public var flatFieldReferenceWidth: Int? { fields["reference_width"]?.intValue }
    public var flatFieldReferenceHeight: Int? { fields["reference_height"]?.intValue }
    public var flatFieldReferenceRigProfileID: String? { fields["rig_profile_id"]?.stringValue }
    public var flatFieldReferenceLocked: Bool? { fields["locked"]?.boolValue }

    // `grid_created` and `grid_list`
    public var gridProfile: [String: JSONValue]? { fields["profile"]?.objectValue }
    public var gridProfiles: [[String: JSONValue]]? {
        fields["profiles"]?.arrayValue?.compactMap { entry in
            entry.objectValue
        }
    }
    // `grid_deleted`
    public var gridProfileID: String? { fields["profile_id"]?.stringValue }

    // `spots_reported` (protocol version 13): a negative's spot set as the
    // app draws it. Every rect is display space, already transformed — the
    // app never converts coordinates, rejects by `id`, and never sees an
    // RLE mask. Decoding into typed values lives on `NegativeSpots`.
    public var spotsNegativeID: String? { fields["negative_id"]?.stringValue }
    public var spotsDetectorVersion: Int? { fields["detector_version"]?.intValue }
    public var spotsSensitivity: Double? { fields["sensitivity"]?.doubleValue }
    public var spotsRepair: Bool? { fields["repair"]?.boolValue }
    public var spotsFound: Int? { fields["found"]?.intValue }

    public var scratchesNegativeID: String? { fields["negative_id"]?.stringValue }
    public var scratchesDetectorVersion: Int? { fields["detector_version"]?.intValue }
    public var scratchesEnabled: Bool? { fields["enabled"]?.boolValue }
    public var scratchesCount: Int? { fields["count"]?.intValue }
    public var scratchesStale: Bool? { fields["stale"]?.boolValue }
    var scratchesSummary: NegativeScratches.Summary? {
        guard
            let detectorVersion = scratchesDetectorVersion,
            let enabled = scratchesEnabled,
            let stale = scratchesStale,
            let count = scratchesCount
        else { return nil }
        return NegativeScratches.Summary(
            detectorVersion: detectorVersion,
            enabled: enabled,
            stale: stale,
            count: count
        )
    }
    // `preview_path` rides the shared `previewPath` accessor above.
}

/// One pipeline step. The last seven cases are the stitch stage's steps.
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
    case normalize
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
        case "normalize": self = .normalize
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
        case .normalize: "normalize"
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
    case invalidGrid
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
    case outputModifiedExternally
    case metadataWriteFailed
    case orphanFileNotRemoved
    case negativeNotFound
    case invalidEdit
    // Protocol version 9: the extended-metadata editing feature.
    case invalidMetadata
    case exportFailed
    case previewFailed
    // Rig profiles and roll flat-field references.
    case rigProfileNotFound
    case rigProfileExists
    case rigProfileInUse
    case flatFieldReferenceLocked
    case flatFieldReferenceRigConflict
    case flatFieldGainMapMissing
    case flatFieldAspectMismatch
    case flatFieldHighlightClipped
    case gridProfileNotFound
    case gridProfileExists
    // Protocol version 7: geometric calibration.
    case geometryInsufficientFrames
    case geometryBoardNotDetected
    case geometryFrameSizeMismatch
    case geometryFitRejected
    case geometryMagnitudeSuspect
    case geometryFewFrames
    case chromaticFitRejected
    case scanClipped
    case normalizeDegenerateBounds
    case normalizeHeadroomClipped
    // Protocol version 18: the film-extent pass.
    case normalizeFilmExtentWithheld
    case normalizeFilmExtentExcessive
    case spotLimitReached
    case spotsStale
    case filmKindRequired
    case filmKindLocked
    case filmBaseRequired
    case filmBaseLocked
    case filmBaseNotFound
    case filmBaseTooSmall
    case filmBaseClipped
    case filmBaseTooDark
    case filmBaseAmbiguous
    case rollPredatesFilmBase
    case filmBaseCameraConflict
    case libraryDBUnsupported
    case rollBusy
    case rollRefreshPending
    case captureClipped
    case captureDenseEndLow
    case captureFocusDrift
    case captureFocusTilt
    case autoCropFailed
    case autoCropNoFormat
    case internalError
    case unknown(String)

    public init(name: String) {
        switch name {
        case "NO_FILES": self = .noFiles
        case "NON_CONTIGUOUS_SELECTION": self = .nonContiguousSelection
        case "NOT_DIVISIBLE": self = .notDivisible
        case "INVALID_PER_NEGATIVE": self = .invalidPerNegative
        case "INVALID_GRID": self = .invalidGrid
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
        case "OUTPUT_MODIFIED_EXTERNALLY": self = .outputModifiedExternally
        case "METADATA_WRITE_FAILED": self = .metadataWriteFailed
        case "ORPHAN_FILE_NOT_REMOVED": self = .orphanFileNotRemoved
        case "NEGATIVE_NOT_FOUND": self = .negativeNotFound
        case "INVALID_EDIT": self = .invalidEdit
        case "INVALID_METADATA": self = .invalidMetadata
        case "EXPORT_FAILED": self = .exportFailed
        case "PREVIEW_FAILED": self = .previewFailed
        case "RIG_PROFILE_NOT_FOUND": self = .rigProfileNotFound
        case "RIG_PROFILE_EXISTS": self = .rigProfileExists
        case "RIG_PROFILE_IN_USE": self = .rigProfileInUse
        case "FLATFIELD_REFERENCE_LOCKED": self = .flatFieldReferenceLocked
        case "FLATFIELD_REFERENCE_RIG_CONFLICT": self = .flatFieldReferenceRigConflict
        case "FLATFIELD_GAIN_MAP_MISSING": self = .flatFieldGainMapMissing
        case "FLATFIELD_ASPECT_MISMATCH": self = .flatFieldAspectMismatch
        case "FLATFIELD_HIGHLIGHT_CLIPPED": self = .flatFieldHighlightClipped
        case "GRID_PROFILE_NOT_FOUND": self = .gridProfileNotFound
        case "GRID_PROFILE_EXISTS": self = .gridProfileExists
        case "GEOMETRY_INSUFFICIENT_FRAMES": self = .geometryInsufficientFrames
        case "GEOMETRY_BOARD_NOT_DETECTED": self = .geometryBoardNotDetected
        case "GEOMETRY_FRAME_SIZE_MISMATCH": self = .geometryFrameSizeMismatch
        case "GEOMETRY_FIT_REJECTED": self = .geometryFitRejected
        case "GEOMETRY_MAGNITUDE_SUSPECT": self = .geometryMagnitudeSuspect
        case "GEOMETRY_FEW_FRAMES": self = .geometryFewFrames
        case "CHROMATIC_FIT_REJECTED": self = .chromaticFitRejected
        case "SCAN_CLIPPED": self = .scanClipped
        case "NORMALIZE_DEGENERATE_BOUNDS": self = .normalizeDegenerateBounds
        case "NORMALIZE_HEADROOM_CLIPPED": self = .normalizeHeadroomClipped
        case "NORMALIZE_FILM_EXTENT_WITHHELD": self = .normalizeFilmExtentWithheld
        case "NORMALIZE_FILM_EXTENT_EXCESSIVE": self = .normalizeFilmExtentExcessive
        case "SPOT_LIMIT_REACHED": self = .spotLimitReached
        case "SPOTS_STALE": self = .spotsStale
        case "FILM_KIND_REQUIRED": self = .filmKindRequired
        case "FILM_KIND_LOCKED": self = .filmKindLocked
        case "FILM_BASE_REQUIRED": self = .filmBaseRequired
        case "FILM_BASE_LOCKED": self = .filmBaseLocked
        case "FILM_BASE_NOT_FOUND": self = .filmBaseNotFound
        case "FILM_BASE_TOO_SMALL": self = .filmBaseTooSmall
        case "FILM_BASE_CLIPPED": self = .filmBaseClipped
        case "FILM_BASE_TOO_DARK": self = .filmBaseTooDark
        case "FILM_BASE_AMBIGUOUS": self = .filmBaseAmbiguous
        case "ROLL_PREDATES_FILM_BASE": self = .rollPredatesFilmBase
        case "FILM_BASE_CAMERA_CONFLICT": self = .filmBaseCameraConflict
        case "LIBRARY_DB_UNSUPPORTED": self = .libraryDBUnsupported
        case "ROLL_BUSY": self = .rollBusy
        case "ROLL_REFRESH_PENDING": self = .rollRefreshPending
        case "CAPTURE_CLIPPED": self = .captureClipped
        case "CAPTURE_DENSE_END_LOW": self = .captureDenseEndLow
        case "CAPTURE_FOCUS_DRIFT": self = .captureFocusDrift
        case "CAPTURE_FOCUS_TILT": self = .captureFocusTilt
        case "AUTO_CROP_FAILED": self = .autoCropFailed
        case "AUTO_CROP_NO_FORMAT": self = .autoCropNoFormat
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
        case .invalidGrid: "INVALID_GRID"
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
        case .outputModifiedExternally: "OUTPUT_MODIFIED_EXTERNALLY"
        case .metadataWriteFailed: "METADATA_WRITE_FAILED"
        case .orphanFileNotRemoved: "ORPHAN_FILE_NOT_REMOVED"
        case .negativeNotFound: "NEGATIVE_NOT_FOUND"
        case .invalidEdit: "INVALID_EDIT"
        case .invalidMetadata: "INVALID_METADATA"
        case .exportFailed: "EXPORT_FAILED"
        case .previewFailed: "PREVIEW_FAILED"
        case .rigProfileNotFound: "RIG_PROFILE_NOT_FOUND"
        case .rigProfileExists: "RIG_PROFILE_EXISTS"
        case .rigProfileInUse: "RIG_PROFILE_IN_USE"
        case .flatFieldReferenceLocked: "FLATFIELD_REFERENCE_LOCKED"
        case .flatFieldReferenceRigConflict: "FLATFIELD_REFERENCE_RIG_CONFLICT"
        case .flatFieldGainMapMissing: "FLATFIELD_GAIN_MAP_MISSING"
        case .flatFieldAspectMismatch: "FLATFIELD_ASPECT_MISMATCH"
        case .flatFieldHighlightClipped: "FLATFIELD_HIGHLIGHT_CLIPPED"
        case .gridProfileNotFound: "GRID_PROFILE_NOT_FOUND"
        case .gridProfileExists: "GRID_PROFILE_EXISTS"
        case .geometryInsufficientFrames: "GEOMETRY_INSUFFICIENT_FRAMES"
        case .geometryBoardNotDetected: "GEOMETRY_BOARD_NOT_DETECTED"
        case .geometryFrameSizeMismatch: "GEOMETRY_FRAME_SIZE_MISMATCH"
        case .geometryFitRejected: "GEOMETRY_FIT_REJECTED"
        case .geometryMagnitudeSuspect: "GEOMETRY_MAGNITUDE_SUSPECT"
        case .geometryFewFrames: "GEOMETRY_FEW_FRAMES"
        case .chromaticFitRejected: "CHROMATIC_FIT_REJECTED"
        case .scanClipped: "SCAN_CLIPPED"
        case .normalizeDegenerateBounds: "NORMALIZE_DEGENERATE_BOUNDS"
        case .normalizeHeadroomClipped: "NORMALIZE_HEADROOM_CLIPPED"
        case .normalizeFilmExtentWithheld: "NORMALIZE_FILM_EXTENT_WITHHELD"
        case .normalizeFilmExtentExcessive: "NORMALIZE_FILM_EXTENT_EXCESSIVE"
        case .spotLimitReached: "SPOT_LIMIT_REACHED"
        case .spotsStale: "SPOTS_STALE"
        case .filmKindRequired: "FILM_KIND_REQUIRED"
        case .filmKindLocked: "FILM_KIND_LOCKED"
        case .filmBaseRequired: "FILM_BASE_REQUIRED"
        case .filmBaseLocked: "FILM_BASE_LOCKED"
        case .filmBaseNotFound: "FILM_BASE_NOT_FOUND"
        case .filmBaseTooSmall: "FILM_BASE_TOO_SMALL"
        case .filmBaseClipped: "FILM_BASE_CLIPPED"
        case .filmBaseTooDark: "FILM_BASE_TOO_DARK"
        case .filmBaseAmbiguous: "FILM_BASE_AMBIGUOUS"
        case .rollPredatesFilmBase: "ROLL_PREDATES_FILM_BASE"
        case .filmBaseCameraConflict: "FILM_BASE_CAMERA_CONFLICT"
        case .libraryDBUnsupported: "LIBRARY_DB_UNSUPPORTED"
        case .rollBusy: "ROLL_BUSY"
        case .rollRefreshPending: "ROLL_REFRESH_PENDING"
        case .captureClipped: "CAPTURE_CLIPPED"
        case .captureDenseEndLow: "CAPTURE_DENSE_END_LOW"
        case .captureFocusDrift: "CAPTURE_FOCUS_DRIFT"
        case .captureFocusTilt: "CAPTURE_FOCUS_TILT"
        case .autoCropFailed: "AUTO_CROP_FAILED"
        case .autoCropNoFormat: "AUTO_CROP_NO_FORMAT"
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
