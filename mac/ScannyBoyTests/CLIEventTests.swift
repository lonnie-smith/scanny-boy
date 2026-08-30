import Foundation
import Testing

@testable import ScannyBoy

/// Every event type in `shared/contract/schema.json` decodes, and anything
/// the schema has grown since this app was built survives intact.
@Suite("CLI event decoding")
struct CLIEventTests {
    @Test("started")
    func startedDecodes() throws {
        let event = try CLIEvent(
            line: #"{"protocol_version":2,"event":"started","command":"probe"}"#
        )
        #expect(event.kind == .started)
        #expect(event.command == "probe")
        #expect(event.runID == nil)
    }

    @Test("probe_result")
    func probeResultDecodes() throws {
        let event = try CLIEvent(
            line: """
                {"protocol_version":2,"event":"probe_result",\
                "catalogue":["_DSC4638.NEF","_DSC4639.NEF"],\
                "warnings":["FILENAME_SORT_USED"],\
                "groups":[["_DSC4638.NEF","_DSC4639.NEF"]]}
                """
        )
        #expect(event.kind == .probeResult)
        #expect(event.catalogue == ["_DSC4638.NEF", "_DSC4639.NEF"])
        #expect(event.warnings == ["FILENAME_SORT_USED"])
        #expect(event.groups == [["_DSC4638.NEF", "_DSC4639.NEF"]])
    }

    @Test("probe_result with --out carries the disk estimate and conflict preview")
    func probeResultWithOutDecodes() throws {
        let event = try CLIEvent(
            line: """
                {"protocol_version":2,"event":"probe_result",\
                "catalogue":["_DSC4638.NEF"],"warnings":[],"groups":[["_DSC4638.NEF"]],\
                "output_conflicts":["_DSC4638.tif"],\
                "estimated_required_bytes":2223767655,"available_bytes":50000000000}
                """
        )
        #expect(event.outputConflicts == ["_DSC4638.tif"])
        #expect(event.estimatedRequiredBytes == 2_223_767_655)
        #expect(event.availableBytes == 50_000_000_000)
    }

    @Test("probe_result without --out leaves the disk fields null")
    func probeResultWithoutOutDecodes() throws {
        let event = try CLIEvent(
            line: """
                {"protocol_version":2,"event":"probe_result",\
                "catalogue":["_DSC4638.NEF"],"warnings":[],"groups":[],\
                "output_conflicts":[],"estimated_required_bytes":null,"available_bytes":null}
                """
        )
        #expect(event.outputConflicts == [])
        #expect(event.estimatedRequiredBytes == nil)
        #expect(event.availableBytes == nil)
    }

    @Test("progress")
    func progressDecodes() throws {
        let event = try CLIEvent(
            line: """
                {"protocol_version":2,"event":"progress","run_id":"r1",\
                "source_index":3,"step":"write_tiff","completed":4,"total":6}
                """
        )
        #expect(event.kind == .progress)
        #expect(event.runID == "r1")
        #expect(event.sourceIndex == 3)
        #expect(event.step == .writeTIFF)
        #expect(event.completed == 4)
        #expect(event.total == 6)
    }

    @Test("item_done")
    func itemDoneDecodes() throws {
        let event = try CLIEvent(
            line: """
                {"protocol_version":2,"event":"item_done","run_id":"r1",\
                "source_index":0,"output":"_DSC4638.tif"}
                """
        )
        #expect(event.kind == .itemDone)
        #expect(event.sourceIndex == 0)
        #expect(event.output == "_DSC4638.tif")
    }

    @Test("group_done")
    func groupDoneDecodes() throws {
        let event = try CLIEvent(
            line: #"{"protocol_version":2,"event":"group_done","run_id":"r1","group_id":"g1"}"#
        )
        #expect(event.kind == .groupDone)
        #expect(event.groupID == "g1")
    }

    @Test("group_failed")
    func groupFailedDecodes() throws {
        let event = try CLIEvent(
            line: """
                {"protocol_version":2,"event":"group_failed","run_id":"r1",\
                "group_id":"g2","code":"TIFF_WRITE_FAILED","message":"no space"}
                """
        )
        #expect(event.kind == .groupFailed)
        #expect(event.groupID == "g2")
        #expect(event.code == .tiffWriteFailed)
        #expect(event.message == "no space")
    }

    @Test("negative_done")
    func negativeDoneDecodes() throws {
        let event = try CLIEvent(
            line: """
                {"protocol_version":2,"event":"negative_done","run_id":"r1",\
                "negative_id":"negative-0","output":"_DSC4638.tif",\
                "width":13972,"height":4553,\
                "global_rms_px":1.12,"max_overlap_mad":0.004}
                """
        )
        #expect(event.kind == .negativeDone)
        #expect(event.negativeID == "negative-0")
        #expect(event.output == "_DSC4638.tif")
        #expect(event.width == 13972)
        #expect(event.height == 4553)
        #expect(event.globalRMS == 1.12)
        #expect(event.maxOverlapMAD == 0.004)
    }

    @Test("negative_failed")
    func negativeFailedDecodes() throws {
        let event = try CLIEvent(
            line: """
                {"protocol_version":2,"event":"negative_failed","run_id":"r1",\
                "negative_id":"negative-0","code":"STITCH_UNDERCONSTRAINED",\
                "message":"frame not reachable"}
                """
        )
        #expect(event.kind == .negativeFailed)
        #expect(event.negativeID == "negative-0")
        #expect(event.code == .stitchUnderconstrained)
        #expect(event.message == "frame not reachable")
    }

    @Test("progress carries the stitch stage")
    func progressStitchStageDecodes() throws {
        let event = try CLIEvent(
            line: """
                {"protocol_version":2,"event":"progress","run_id":"r1",\
                "source_index":0,"step":"warp","completed":1,"total":6,"stage":"stitch"}
                """
        )
        #expect(event.stage == "stitch")
    }

    @Test("warning")
    func warningDecodes() throws {
        let event = try CLIEvent(
            line: """
                {"protocol_version":2,"event":"warning",\
                "code":"FILENAME_SORT_USED","message":"fell back"}
                """
        )
        #expect(event.kind == .warning)
        #expect(event.code == .filenameSortUsed)
        #expect(event.message == "fell back")
    }

    @Test("error")
    func errorDecodes() throws {
        let event = try CLIEvent(
            line: """
                {"protocol_version":2,"event":"error",\
                "code":"NON_CONTIGUOUS_SELECTION","message":"gap"}
                """
        )
        #expect(event.kind == .error)
        #expect(event.code == .nonContiguousSelection)
    }

    @Test("finished")
    func finishedDecodes() throws {
        let event = try CLIEvent(
            line: """
                {"protocol_version":2,"event":"finished","run_id":"r1",\
                "status":"cancelled","exit_status":143}
                """
        )
        #expect(event.kind == .finished)
        #expect(event.status == "cancelled")
        #expect(event.exitStatus == 143)
    }

    /// Every code in CONTRACT.md maps to a case, and none of them lands in
    /// `unknown`.
    @Test("every stable code maps to a known case", arguments: [
        "NO_FILES", "NON_CONTIGUOUS_SELECTION", "NOT_DIVISIBLE",
        "INVALID_PER_NEGATIVE", "MISSING_CAPTURE_TIME", "FILENAME_SORT_USED",
        "UNSUPPORTED_RAW", "CAPTURE_METADATA_MISSING", "CAPTURE_SETTINGS_DIFFER",
        "CAPTURE_SPAN_TOO_LONG", "UNREADABLE_RAW", "OUTPUT_SAME_AS_INPUT",
        "OUTPUT_NOT_WRITABLE", "OUTPUT_NOT_EMPTY", "OUTPUT_CONFLICT",
        "INSUFFICIENT_DISK", "INSUFFICIENT_MEMORY", "BAD_MANIFEST",
        "MANIFEST_MISMATCH", "ICC_PROFILE_INVALID", "TIFF_WRITE_FAILED",
        "CANCELLED",
        // Phase 2 section 3.10.
        "WORK_SAME_AS_OUTPUT", "WORK_MANIFEST_UNUSABLE", "INTERMEDIATE_MISSING",
        "INTERMEDIATE_CHANGED", "STITCH_INSUFFICIENT_MATCHES",
        "STITCH_UNDERCONSTRAINED", "STITCH_RESIDUAL_TOO_HIGH",
        "STITCH_OUTPUT_TOO_LARGE", "STITCH_FAILED", "STITCH_SCALE_DRIFT",
        "STITCH_LAYOUT_UNEXPECTED", "STITCH_REBATE_CHECK_FAILED",
        "OUTPUT_DIMENSIONS_LARGE", "INTERMEDIATES_KEPT",
    ])
    func everyStableCodeIsKnown(name: String) {
        let code = CLICode(name: name)
        #expect(code != .unknown(name))
        #expect(code.name == name)
    }

    @Test("every pipeline step maps to a known case", arguments: [
        "decode", "write_tiff", "add_metadata",
        // Phase 2 section 3.9's stitch-stage steps.
        "load", "detect", "match", "solve", "warp", "blend", "write_stitched",
    ])
    func everyPipelineStepIsKnown(name: String) {
        let step = CLIPipelineStep(name: name)
        #expect(step != .unknown(name))
        #expect(step.name == name)
    }

    // MARK: - Unknown things are preserved, not rejected

    @Test("an unknown event type decodes and keeps its fields")
    func unknownEventTypeIsPreserved() throws {
        let event = try CLIEvent(
            line: """
                {"protocol_version":2,"event":"negative_previewed","run_id":"r1",\
                "group_id":"g1","preview":{"width":800,"height":600}}
                """
        )
        #expect(event.kind == .unknown("negative_previewed"))
        #expect(event.kind.isKnown == false)
        #expect(event.kind.name == "negative_previewed")
        #expect(event.runID == "r1")
        #expect(event.groupID == "g1")
        #expect(
            event.fields["preview"]?.objectValue?["width"]?.intValue == 800
        )
    }

    @Test("an unknown code on a known event is kept verbatim")
    func unknownCodeIsPreserved() throws {
        let event = try CLIEvent(
            line: """
                {"protocol_version":2,"event":"error",\
                "code":"SENSOR_DUST_DETECTED","message":"speck"}
                """
        )
        #expect(event.kind == .error)
        #expect(event.code == .unknown("SENSOR_DUST_DETECTED"))
        #expect(event.code?.name == "SENSOR_DUST_DETECTED")
    }

    @Test("unknown extra fields on a known event survive")
    func unknownFieldsSurvive() throws {
        let event = try CLIEvent(
            line: """
                {"protocol_version":2,"event":"item_done","source_index":1,\
                "output":"a.tif","bytes":154340928,"verified":true}
                """
        )
        #expect(event.kind == .itemDone)
        #expect(event.fields["bytes"]?.intValue == 154_340_928)
        #expect(event.fields["verified"] == .bool(true))
    }

    // MARK: - Lines that are not events

    @Test("a line that is not JSON is a decode failure")
    func nonJSONLineFails() {
        #expect(throws: CLIEventDecodingError.self) {
            try CLIEvent(line: "Traceback (most recent call last):")
        }
    }

    @Test("a JSON array is not an event")
    func jsonArrayIsNotAnEvent() {
        #expect(throws: CLIEventDecodingError.notAnObject) {
            try CLIEvent(line: "[1, 2, 3]")
        }
    }

    @Test("a missing protocol_version is rejected")
    func missingProtocolVersionIsRejected() {
        #expect(throws: CLIEventDecodingError.missingProtocolVersion) {
            try CLIEvent(line: #"{"event":"started","command":"probe"}"#)
        }
    }

    @Test("a different protocol version is rejected rather than guessed at")
    func unsupportedProtocolVersionIsRejected() {
        #expect(throws: CLIEventDecodingError.unsupportedProtocolVersion(1)) {
            try CLIEvent(line: #"{"protocol_version":1,"event":"started","command":"probe"}"#)
        }
    }

    @Test("a missing event type is rejected")
    func missingEventTypeIsRejected() {
        #expect(throws: CLIEventDecodingError.missingEventType) {
            try CLIEvent(line: #"{"protocol_version":2,"command":"probe"}"#)
        }
    }
}
