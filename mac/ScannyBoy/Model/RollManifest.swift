import Foundation

/// The roll's film-base reference block, decoded
/// from `roll info`'s `film_base` field.
struct FilmBase: Sendable, Hashable {
    struct Population: Sendable, Hashable {
        let density: [Double]
        let luma: Double
        let areaFraction: Double
        let cells: Int
        let spread: Double
    }

    let density: [Double]
    let lockedAt: String?
    let sourceName: String
    let populations: [Population]

    /// The chosen population's rebate area as a percentage for display.
    var areaFractionPercent: Int? {
        guard let chosen = populations.first(where: { $0.density == density })
            ?? populations.first
        else { return nil }
        return Int((chosen.areaFraction * 100).rounded())
    }

    init?(fields: [String: JSONValue]) {
        guard
            let density = fields["density"]?.arrayValue?.compactMap(\.doubleValue),
            density.count == 3,
            let sourceName = fields["source_name"]?.stringValue
        else { return nil }

        let populationFields = fields["populations"]?.arrayValue ?? []
        let populations = populationFields.compactMap { entry -> Population? in
            guard
                let object = entry.objectValue,
                let popDensity = object["density"]?.arrayValue?.compactMap(\.doubleValue),
                popDensity.count == 3,
                let luma = object["luma"]?.doubleValue,
                let areaFraction = object["area_fraction"]?.doubleValue,
                let cells = object["cells"]?.intValue,
                let spread = object["spread"]?.doubleValue
            else { return nil }
            return Population(
                density: popDensity,
                luma: luma,
                areaFraction: areaFraction,
                cells: cells,
                spread: spread
            )
        }
        guard populations.count == populationFields.count else { return nil }

        self.density = density
        self.lockedAt = fields["locked_at"]?.stringValue
        self.sourceName = sourceName
        self.populations = populations
    }
}

struct RollManifest: Sendable, Hashable {
    /// The roll manifest's optional `camera_color` block:
    /// frozen after the first stitch; preview encode and export both read it.
    struct CameraColor: Sendable, Hashable {
        let rgbXYZMatrix: [[Double]]
        let source: String
        let cameraModel: String?
        let matrixVersion: Int

        /// A stable cache-generation token for preview invalidation.
        var cacheTerm: String {
            let flat = rgbXYZMatrix.flatMap { $0.map { String($0) } }.joined(separator: ",")
            return "\(cameraModel ?? "unknown")#\(flat)"
        }

        init?(fields: [String: JSONValue]) {
            guard
                let matrixRows = fields["rgb_xyz_matrix"]?.arrayValue,
                matrixRows.count == 3,
                let source = fields["source"]?.stringValue
            else { return nil }
            var matrix: [[Double]] = []
            for row in matrixRows {
                guard
                    let values = row.arrayValue,
                    values.count == 3,
                    let r = values[0].doubleValue,
                    let g = values[1].doubleValue,
                    let b = values[2].doubleValue
                else { return nil }
                matrix.append([r, g, b])
            }
            self.rgbXYZMatrix = matrix
            self.source = source
            self.cameraModel = fields["camera_model"]?.stringValue
            self.matrixVersion = fields["matrix_version"]?.intValue ?? 1
        }
    }

    struct Run: Sendable, Hashable {
        let runID: String
        /// `running`, `partial`, `cancelled`, or `complete`. Read by
        /// `RollManifestReport` to decide cleanup-incomplete vs. final.
        let status: String
    }

    struct Output: Sendable, Hashable {
        let name: String
        let size: Int
        let sha256: String
        let width: Int
        let height: Int
    }

    /// Roll-only extended metadata fields (no per-image override).
    static let rollOnlyMetadataFields = ["film", "iso"]

    /// The extended metadata fields, in display order. Each lives on both
    /// the roll (the fallback) and each negative (the explicit value); the
    /// effective value is the negative's own, else the roll's.
    static let metadataFields = ["city", "state", "camera", "lens", "caption"]

    struct ImageMetadata: Sendable, Hashable {
        var city: String?
        var state: String?
        var camera: String?
        var lens: String?
        var caption: String?

        static let empty = ImageMetadata()

        subscript(field: String) -> String? {
            get {
                switch field {
                case "city": city
                case "state": state
                case "camera": camera
                case "lens": lens
                case "caption": caption
                default: nil
                }
            }
            set {
                switch field {
                case "city": city = newValue
                case "state": state = newValue
                case "camera": camera = newValue
                case "lens": lens = newValue
                case "caption": caption = newValue
                default: break
                }
            }
        }

        /// The live-fallback resolution: the negative's own explicit value
        /// when it has one, else the roll-level value. Nothing is copied —
        /// a roll-level change covers every negative without an explicit
        /// one, instantly.
        static func effective(
            _ negative: ImageMetadata, roll fallback: RollManifest.Metadata, field: String
        ) -> String? {
            negative[field] ?? fallback[field]
        }
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
        /// 1-based position in the roll; `nil` when unranked (pending/failed).
        let sequence: Int?
        /// The source NEFs this negative was built from, in canonical order
        /// — the Edit tab's "source frames".
        let members: [String]
        let expectedOutput: String
        /// `pending`, `completed`, or `failed`.
        let status: String
        let output: Output?
        let captureTime: CaptureTime
        /// The negative's explicit extended-metadata values (`nil` = inherit
        /// the roll's fallback).
        let metadata: ImageMetadata
        /// The registration quality numbers already reported on
        /// `negative_done`, read back here for the Edit tab's
        /// display: RMS pixel error across every accepted pair, and the
        /// deviation `nil` unless a rebate check ran.
        let globalRMSPixels: Double?
        let rebateDeviationPixels: Double?
        /// The CLI-rendered preview of this negative as its edits render so
        /// far; `nil` until first generated or for unstitched negatives.
        /// Swift displays the file; it never renders one.
        let previewPath: String?
        /// The ops log's net clockwise quarter turns, derived by the CLI.
        /// Swift rotates nothing itself; the preview file already shows the
        /// edited orientation.
        let rotationQuarterTurns: Int
        /// The ops log's net transform's horizontal-mirror half, derived by
        /// the CLI. A flip and a rotation do not commute, so the pair — not
        /// the turn count alone — is what identifies the rendered state.
        let flippedHorizontally: Bool
        /// The negative's fitted rig-tilt rectification. `nil` when the fit
        /// was rejected, the negative failed before it ran, or the roll's record
        /// predates the field. Swift displays it; it never re-fits or
        /// recomputes anything.
        let rectification: Rectification?

        /// The measured rig tilt: `l` in 1/px acting on coordinates centred
        /// at the frame centre, and the RMS improvement the shared model
        /// measured over the per-pair similarity fits.
        struct Rectification: Sendable, Hashable {
            let lX: Double
            let lY: Double
            let relativeImprovement: Double
        }

        /// The ops log's net preview tone adjustment (protocol 10's `tone`
        /// op): an ISO-R paper grade and a midtone snap composed into the
        /// CLI's preview display encode. `nil` = no adjustment recorded —
        /// the flat linear look. The published TIFF never carries it.
        let toneGradeR: Double?
        let toneSnapGamma: Double?
        /// Absent before protocol 11's density control existed.
        let toneDensity: Double?
        let toneShadowDensity: Double?
        let toneHighlightDensity: Double?
        let toneToe: Double?
        let toneToeWidth: Double?
        let toneShoulder: Double?
        let toneShoulderWidth: Double?
        /// Protocol 12's net preview colour adjustment. `nil` = no op recorded.
        let colorWbCyan: Double?
        let colorWbMagenta: Double?
        let colorWbYellow: Double?
        let colorShadowCyan: Double?
        let colorShadowMagenta: Double?
        let colorShadowYellow: Double?
        let colorHighlightCyan: Double?
        let colorHighlightMagenta: Double?
        let colorHighlightYellow: Double?
        let colorCastRemoval: Double?
        let colorCastRemovalHighlights: Double?
        let colorDyeSeparation: Double?
        let colorSeparationDamping: Double?
        let colorTemperature: Double?
        /// The stitch-stage failure code, when `status` is `failed`.
        let errorCode: String?
        let errorMessage: String?
        /// Worst post-gain overlap MAD across accepted pairs — scanned from
        /// the manifest's `pairs` array at decode time rather than stored
        /// as its own field.
        let maxOverlapMAD: Double?
        /// Headroom clipping recorded during normalization — enough to derive
        /// the Edit tab's normalization warning without reading the full
        /// normalization object.
        let normalization: NormalizationSummary?
        /// Whether registration needed the CLAHE retry to solve this negative.
        let usedClaheFallback: Bool
        /// Grid regularity measures.
        let gridPitchRatio: Double?
        let gridAlignmentRatio: Double?
        /// Protocol 13's per-negative spots summary from `roll info` — the
        /// counts, without the list (the full list is `edit list-spots`'s
        /// job, and `EditModel` holds it for the displayed negative only).
        /// `nil` for a negative with no spot set or a manifest predating
        /// the field. Defaults to `nil` so every construction site that
        /// predates the field keeps compiling.
        var spotsSummary: NegativeSpots.Summary? = nil
        /// Protocol 21's per-negative scratches summary from `roll info`.
        var scratchesSummary: NegativeScratches.Summary? = nil
        /// Protocol 19's net crop state — the cropped display image's
        /// dimensions, the window's tilt, and the ratio-preset label. The
        /// published TIFF is never cropped; the preview already shows the
        /// cropped frame and the export bakes the window in. `nil` for a
        /// negative with no live crop (or a manifest predating the field).
        /// Defaults to `nil` so every construction site that predates the
        /// field keeps compiling.
        var crop: CropState? = nil

        /// The normalization meters the Edit tab needs from the stored record.
        struct NormalizationSummary: Sendable, Hashable {
            let maxHeadroomClippedHighlight: Double
            let maxHeadroomClippedShadow: Double
        }

        var isCompleted: Bool { status == "completed" }
        var isFailed: Bool { status == "failed" }
    }

    struct Metadata: Sendable, Hashable {
        /// `YYYY-MM-DD`.
        let rollCaptureDate: String?
        let lastAppliedAt: String?
        // Roll-only film stock and ISO rating.
        var film: String?
        var iso: String?
        // The roll-level extended-metadata fallbacks: what every negative
        // without its own explicit value displays and exports.
        var city: String?
        var state: String?
        var camera: String?
        var lens: String?
        var caption: String?

        subscript(field: String) -> String? {
            get {
                switch field {
                case "film": film
                case "iso": iso
                case "city": city
                case "state": state
                case "camera": camera
                case "lens": lens
                case "caption": caption
                default: nil
                }
            }
            set {
                switch field {
                case "film": film = newValue
                case "iso": iso = newValue
                case "city": city = newValue
                case "state": state = newValue
                case "camera": camera = newValue
                case "lens": lens = newValue
                case "caption": caption = newValue
                default: break
                }
            }
        }
    }

    let rollID: String
    let rollName: String
    let createdAt: String
    let updatedAt: String
    let runs: [Run]
    let negatives: [Negative]
    let metadata: Metadata
    /// The roll's frozen film kind (`colour` or `monochrome`), protocol 12.
    let filmKind: String?
    /// The roll's film-base reference. `nil` when
    /// the roll has none attached yet.
    let filmBase: FilmBase?
    /// The roll's frozen camera colour matrix.
    let cameraColor: CameraColor?

    /// Every stitched TIFF the manifest records as published, in negative
    /// order — the `RunManifest.publishedOutputs` counterpart.
    var publishedOutputs: [String] { negatives.compactMap { $0.output?.name } }

    /// A copy of this manifest with one negative replaced — how an
    /// `edit_recorded` event's fresh preview path and net rotation land in
    /// the model without a `roll info` round trip.
    func replacingNegative(_ updated: Negative) -> RollManifest {
        var negatives = negatives
        if let index = negatives.firstIndex(where: { $0.negativeID == updated.negativeID }) {
            negatives[index] = updated
        }
        return RollManifest(
            rollID: rollID,
            rollName: rollName,
            createdAt: createdAt,
            updatedAt: updatedAt,
            runs: runs,
            negatives: negatives,
            metadata: metadata,
            filmKind: filmKind,
            filmBase: filmBase,
            cameraColor: cameraColor
        )
    }

    /// The memberwise initializer the decoding path in the extension below
    /// and `replacingNegative` both use.
    init(
        rollID: String,
        rollName: String,
        createdAt: String,
        updatedAt: String,
        runs: [Run],
        negatives: [Negative],
        metadata: Metadata,
        filmKind: String? = nil,
        filmBase: FilmBase? = nil,
        cameraColor: CameraColor? = nil
    ) {
        self.rollID = rollID
        self.rollName = rollName
        self.createdAt = createdAt
        self.updatedAt = updatedAt
        self.runs = runs
        self.negatives = negatives
        self.metadata = metadata
        self.filmKind = filmKind
        self.filmBase = filmBase
        self.cameraColor = cameraColor
    }

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
        self.createdAt = createdAt
        self.updatedAt = updatedAt
        self.runs = runs
        self.negatives = negatives
        self.metadata = metadata
        self.filmKind = fields["film_kind"]?.stringValue
        self.filmBase = fields["film_base"]?.objectValue.flatMap(FilmBase.init(fields:))
        self.cameraColor = fields["camera_color"]?.objectValue.flatMap(CameraColor.init(fields:))
    }

    private static func decodeRun(_ fields: [String: JSONValue]) -> Run? {
        guard
            let runID = fields["run_id"]?.stringValue,
            let status = fields["status"]?.stringValue
        else { return nil }
        return Run(runID: runID, status: status)
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

    private static func decodeImageMetadata(_ fields: [String: JSONValue]) -> ImageMetadata {
        ImageMetadata(
            city: fields["city"]?.stringValue,
            state: fields["state"]?.stringValue,
            camera: fields["camera"]?.stringValue,
            lens: fields["lens"]?.stringValue,
            caption: fields["caption"]?.stringValue
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
        let pairs = fields["pairs"]?.arrayValue ?? []

        return Negative(
            negativeID: negativeID,
            runID: runID,
            sequence: fields["sequence"]?.intValue,
            members: members,
            expectedOutput: expectedOutput,
            status: status,
            output: output,
            captureTime: Self.decodeCaptureTime(captureTimeFields),
            // Pre-protocol-9 manifests carry no nested metadata object; a
            // missing one is all-nil, which is exactly the inherit state.
            metadata: fields["metadata"]?.objectValue.map(Self.decodeImageMetadata)
                ?? .empty,
            globalRMSPixels: fields["global_rms_px"]?.doubleValue,
            rebateDeviationPixels: fields["rebate_deviation_px"]?.doubleValue,
            previewPath: fields["preview_path"]?.stringValue,
            // Protocol version 4's CLI did not augment this field; a roll
            // whose record predates the augmentation reads as unrotated.
            rotationQuarterTurns: fields["rotation_quarter_turns"]?.intValue ?? 0,
            // Likewise absent before the flip op existed: unmirrored.
            flippedHorizontally: fields["flipped_horizontally"]?.boolValue ?? false,
            // Absent when the fit was rejected or the roll's record
            // predates format version 7: no rectification was applied.
            rectification: fields["rectification"]?.objectValue
                .flatMap(Self.decodeRectification),
            // Absent before the tone op existed (or an explicit null from
            // a reset): no adjustment, the flat look.
            toneGradeR: fields["tone_grade_r"]?.doubleValue,
            toneSnapGamma: fields["tone_snap_gamma"]?.doubleValue,
            toneDensity: fields["tone_density"]?.doubleValue,
            toneShadowDensity: fields["tone_shadow_density"]?.doubleValue,
            toneHighlightDensity: fields["tone_highlight_density"]?.doubleValue,
            toneToe: fields["tone_toe"]?.doubleValue,
            toneToeWidth: fields["tone_toe_width"]?.doubleValue,
            toneShoulder: fields["tone_shoulder"]?.doubleValue,
            toneShoulderWidth: fields["tone_shoulder_width"]?.doubleValue,
            colorWbCyan: fields["color_wb_cyan"]?.doubleValue,
            colorWbMagenta: fields["color_wb_magenta"]?.doubleValue,
            colorWbYellow: fields["color_wb_yellow"]?.doubleValue,
            colorShadowCyan: fields["color_shadow_cyan"]?.doubleValue,
            colorShadowMagenta: fields["color_shadow_magenta"]?.doubleValue,
            colorShadowYellow: fields["color_shadow_yellow"]?.doubleValue,
            colorHighlightCyan: fields["color_highlight_cyan"]?.doubleValue,
            colorHighlightMagenta: fields["color_highlight_magenta"]?.doubleValue,
            colorHighlightYellow: fields["color_highlight_yellow"]?.doubleValue,
            colorCastRemoval: fields["color_cast_removal"]?.doubleValue,
            colorCastRemovalHighlights: fields["color_cast_removal_highlights"]?.doubleValue,
            colorDyeSeparation: fields["color_dye_separation"]?.doubleValue,
            colorSeparationDamping: fields["color_separation_damping"]?.doubleValue,
            colorTemperature: fields["color_temperature"]?.doubleValue,
            errorCode: fields["error_code"]?.stringValue,
            errorMessage: fields["error_message"]?.stringValue,
            maxOverlapMAD: Self.maxOverlapMAD(from: pairs),
            normalization: fields["normalization"]?.objectValue
                .flatMap(Self.decodeNormalizationSummary),
            usedClaheFallback: fields["used_clahe_fallback"]?.boolValue ?? false,
            gridPitchRatio: fields["grid_pitch_ratio"]?.doubleValue,
            gridAlignmentRatio: fields["grid_alignment_ratio"]?.doubleValue,
            spotsSummary: fields["spots"]?.objectValue
                .flatMap(NegativeSpots.Summary.init(fields:)),
            scratchesSummary: fields["scratches"]?.objectValue
                .flatMap(NegativeScratches.Summary.init(fields:)),
            crop: fields["crop"]?.objectValue.flatMap(CropState.init(fields:))
        )
    }

    private static func maxOverlapMAD(from pairs: [JSONValue]) -> Double? {
        let values = pairs.compactMap { pair -> Double? in
            guard
                let fields = pair.objectValue,
                fields["accepted"]?.boolValue == true,
                let mad = fields["overlap_mad"]?.doubleValue
            else { return nil }
            return mad
        }
        return values.max()
    }

    private static func decodeNormalizationSummary(
        _ fields: [String: JSONValue]
    ) -> Negative.NormalizationSummary? {
        guard
            let highlight = fields["headroom_clipped_highlights"]?.arrayValue,
            let shadow = fields["headroom_clipped_shadows"]?.arrayValue,
            highlight.count == 3,
            shadow.count == 3
        else { return nil }
        let highlights = highlight.compactMap(\.doubleValue)
        let shadows = shadow.compactMap(\.doubleValue)
        guard highlights.count == 3, shadows.count == 3 else { return nil }
        return Negative.NormalizationSummary(
            maxHeadroomClippedHighlight: highlights.max() ?? 0,
            maxHeadroomClippedShadow: shadows.max() ?? 0
        )
    }

    private static func decodeRectification(
        _ fields: [String: JSONValue]
    ) -> Negative.Rectification? {
        guard
            let l = fields["l"]?.arrayValue,
            l.count == 2,
            let lX = l[0].doubleValue,
            let lY = l[1].doubleValue,
            let relativeImprovement = fields["relative_improvement"]?.doubleValue
        else { return nil }
        return Negative.Rectification(
            lX: lX,
            lY: lY,
            relativeImprovement: relativeImprovement
        )
    }

    private static func decodeMetadata(_ fields: [String: JSONValue]) -> Metadata? {
        Metadata(
            rollCaptureDate: fields["roll_capture_date"]?.stringValue,
            lastAppliedAt: fields["last_applied_at"]?.stringValue,
            film: fields["film"]?.stringValue,
            iso: fields["iso"]?.stringValue,
            city: fields["city"]?.stringValue,
            state: fields["state"]?.stringValue,
            camera: fields["camera"]?.stringValue,
            lens: fields["lens"]?.stringValue,
            caption: fields["caption"]?.stringValue
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
            "The roll now holds \(manifest.negatives.count) negative(s)."
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
