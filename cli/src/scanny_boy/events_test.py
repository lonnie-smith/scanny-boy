import json

from scanny_boy.events import (
    PROTOCOL_VERSION,
    Code,
    EditRecorded,
    ErrorEvent,
    Event,
    EventWriter,
    ExportDone,
    Finished,
    FlatFieldCreated,
    FlatFieldDeleted,
    FlatFieldList,
    FlatFieldProfileSummary,
    FlatFieldProgress,
    GroupDone,
    GroupFailed,
    ItemDone,
    MetadataApplied,
    MetadataSkipped,
    MetadataUpdated,
    MetadataValues,
    NegativeDeleted,
    NegativeDone,
    NegativeFailed,
    PipelineStep,
    ProbeResult,
    Progress,
    RegionRendered,
    RollCreated,
    RollInfo,
    RollList,
    RollListingEntry,
    RollListingReason,
    RollOverlapEntry,
    RollRenamed,
    Stage,
    Started,
    WarningEvent,
)
from scanny_boy.schema_test_support import assert_matches_schema, load_schema

SCHEMA = load_schema()

ALL_EVENTS: list[Event] = [
    Started(command="probe"),
    Started(command="prepare", run_id="run-1"),
    Started(command="edit delete"),
    ProbeResult(catalogue=["DSC_0001.NEF", "DSC_0002.NEF"], run_id="run-1"),
    ProbeResult(
        catalogue=["DSC_0001.NEF", "DSC_0002.NEF"],
        warnings=["FILENAME_SORT_USED"],
        groups=[["DSC_0001.NEF"], ["DSC_0002.NEF"]],
    ),
    Progress(
        source_index=0,
        step=PipelineStep.DECODE,
        completed=1,
        total=6,
        run_id="run-1",
    ),
    ItemDone(source_index=0, output="DSC_0001.tif", run_id="run-1"),
    GroupDone(group_id="group-0", run_id="run-1"),
    GroupFailed(
        group_id="group-0",
        code=Code.TIFF_WRITE_FAILED,
        message="disk full",
        run_id="run-1",
    ),
    NegativeDone(
        negative_id="negative-0",
        output="_DSC4638.tif",
        width=13972,
        height=4553,
        global_rms_px=1.12,
        max_overlap_mad=0.004,
        run_id="run-1",
    ),
    NegativeFailed(
        negative_id="negative-0",
        code=Code.STITCH_UNDERCONSTRAINED,
        message="frame not reachable from any other frame",
        run_id="run-1",
    ),
    WarningEvent(code=Code.FILENAME_SORT_USED, message="fell back to filenames"),
    ErrorEvent(code=Code.INVALID_PER_NEGATIVE, message="out of range"),
    Finished(status="success", exit_status=0, run_id="run-1"),
    RollCreated(
        roll_id="00000000-0000-4000-8000-000000000001",
        roll_name="Tri-X, Portland 1998",
        path="/Users/me/Pictures/Scanny Boy/Tri-X-Portland-1998",
    ),
    RollList(
        rolls=[
            RollListingEntry(
                path="/Users/me/Pictures/Scanny Boy/Tri-X-Portland-1998",
                status="ok",
                roll_id="00000000-0000-4000-8000-000000000001",
                roll_name="Tri-X, Portland 1998",
                negative_count=12,
            ),
            RollListingEntry(
                path="/Users/me/Pictures/Scanny Boy/broken-roll",
                status="unreadable",
                reason=RollListingReason(
                    code="ROLL_MANIFEST_UNSUPPORTED",
                    message="manifest_format_version must be 4",
                ),
            ),
        ]
    ),
    RollInfo(manifest={"manifest_format_version": 3, "manifest_kind": "roll"}),
    MetadataApplied(negative_id="a1b2c3-negative-01"),
    MetadataSkipped(
        negative_id="a1b2c3-negative-02",
        code=Code.OUTPUT_MODIFIED_EXTERNALLY,
        message="published TIFF hash differs from manifest",
    ),
    EditRecorded(
        negative_id="a1b2c3-negative-03",
        edit={
            "id": 1,
            "negative_id": "a1b2c3-negative-03",
            "position": 1,
            "op": "rotate",
            "params": {"direction": "cw"},
            "created_at": "2026-08-31T12:00:00Z",
        },
        rotation_quarter_turns=1,
        flipped_horizontally=False,
        preview_path="/Users/me/Library/Application Support/ScannyBoy/previews/roll/a1b2c3-negative-03.png",
    ),
    ExportDone(
        negative_id="a1b2c3-negative-03",
        output="_DSC4638.tif",
        width=4553,
        height=13972,
    ),
    NegativeDeleted(negative_id="a1b2c3-negative-04", output="_DSC4641.tif"),
    NegativeDeleted(negative_id="a1b2c3-negative-05", output=None),
    ProbeResult(
        catalogue=["DSC_0001.NEF"],
        groups=[["DSC_0001.NEF"]],
        roll_overlap=[
            RollOverlapEntry(
                negative_id="a1b2c3-negative-01",
                expected_output="_DSC4638.tif",
                run_id="run-1",
                overlapping_sources=["DSC_0001.NEF"],
                group_index=0,
            )
        ],
    ),
]


def test_every_event_type_validates_against_schema():
    for event in ALL_EVENTS:
        assert_matches_schema(event.to_dict(), SCHEMA)


def test_every_event_serialises_to_one_json_line():
    for event in ALL_EVENTS:
        writer_output = json.dumps(event.to_dict())
        assert "\n" not in writer_output
        # round-trips through json.loads without error
        assert json.loads(writer_output) == event.to_dict()


def test_run_id_omitted_when_absent():
    event = Started(command="probe")
    assert "run_id" not in event.to_dict()


def test_run_id_included_when_present():
    event = Started(command="prepare", run_id="run-7")
    assert event.to_dict()["run_id"] == "run-7"


def test_event_writer_writes_one_flushed_line_per_event():
    class RecordingStream:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def write(self, text: str) -> None:
            self.calls.append(("write", text))

        def flush(self) -> None:
            self.calls.append(("flush", ""))

    stream = RecordingStream()
    writer = EventWriter(stream)

    writer.write(Started(command="probe"))
    writer.write(Finished(status="success", exit_status=0))

    # Every write is immediately followed by a flush, for both events.
    assert stream.calls == [
        ("write", stream.calls[0][1]),
        ("flush", ""),
        ("write", stream.calls[2][1]),
        ("flush", ""),
    ]
    assert stream.calls[0][1].endswith("\n")
    assert json.loads(stream.calls[0][1])["event"] == "started"
    assert json.loads(stream.calls[2][1])["event"] == "finished"


def test_event_writer_line_is_valid_json_per_write():
    import io

    stream = io.StringIO()
    writer = EventWriter(stream)
    writer.write(
        Progress(source_index=2, step=PipelineStep.WRITE_TIFF, completed=3, total=6)
    )

    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["step"] == "write_tiff"


def test_protocol_version_is_ten():
    """Protocol 9→10: the preview's nondestructive tone adjustment — the
    `edit tone` command (an ISO-R paper grade plus a midtone snap,
    recorded as a `tone` op in the ops log and coalesced there) and the
    net `tone_grade_r`/`tone_snap_gamma` fields in the roll manifest's
    negatives. The published TIFF never carries the adjustment
    (docs/DECISIONS.md, "The preview's tone adjustment")."""
    assert PROTOCOL_VERSION == 10


def test_new_event_kinds_round_trip():
    events = [
        RollCreated(roll_id="id", roll_name="name", path="/tmp/roll"),
        RollList(rolls=[]),
        RollInfo(manifest={"manifest_kind": "roll"}),
        RollRenamed(roll_id="id", roll_name="new name", path="/tmp/roll-2"),
        MetadataApplied(negative_id="neg-1"),
        MetadataUpdated(manifest={"manifest_kind": "roll"}),
        MetadataValues(field="city", values=["Porto", "Lisbon"]),
        MetadataSkipped(
            negative_id="neg-2",
            code=Code.METADATA_WRITE_FAILED,
            message="verify failed",
        ),
        EditRecorded(
            negative_id="neg-3",
            edit={"position": 1, "op": "rotate", "params": {"direction": "cw"}},
            rotation_quarter_turns=1,
            flipped_horizontally=True,
            preview_path=None,
        ),
        ExportDone(negative_id="neg-4", output="out.tif", width=4, height=3),
        FlatFieldCreated(
            profile=FlatFieldProfileSummary(
                profile_id="pid-1",
                name="Copy stand",
                reference_width=6064,
                reference_height=4040,
                source_path="/refs/bare.NEF",
                created_at="2026-09-01T00:00:00Z",
            )
        ),
        FlatFieldList(profiles=[]),
        FlatFieldDeleted(profile_id="pid-1"),
        FlatFieldProgress(phase="detect", completed=3, total=12),
        FlatFieldProgress(phase="chromatic", completed=12, total=12),
        NegativeDeleted(negative_id="neg-5", output="out.tif"),
        NegativeDeleted(negative_id="neg-6", output=None),
        RegionRendered(
            negative_id="neg-7",
            path="/tmp/region.png",
            x=4,
            y=2,
            width=10,
            height=6,
        ),
    ]
    for event in events:
        data = event.to_dict()
        assert data["protocol_version"] == PROTOCOL_VERSION
        assert json.loads(json.dumps(data)) == data


def test_retired_code_absent():
    assert not hasattr(Code, "CAPTURE_SPAN_TOO_LONG")
    assert "CAPTURE_SPAN_TOO_LONG" not in {member.value for member in Code}
    assert not hasattr(Code, "SUPERSEDED_FILE_NOT_REMOVED")
    assert "SUPERSEDED_FILE_NOT_REMOVED" not in {member.value for member in Code}
    assert Code.ORPHAN_FILE_NOT_REMOVED.value == "ORPHAN_FILE_NOT_REMOVED"


def test_negative_done_round_trips():
    event = NegativeDone(
        negative_id="negative-0",
        output="_DSC4638.tif",
        width=13972,
        height=4553,
        global_rms_px=1.12,
        max_overlap_mad=0.004,
        run_id="run-1",
    )
    data = event.to_dict()
    assert data["event"] == "negative_done"
    assert data["negative_id"] == "negative-0"
    assert data["output"] == "_DSC4638.tif"
    assert data["width"] == 13972
    assert data["height"] == 4553
    assert data["global_rms_px"] == 1.12
    assert data["max_overlap_mad"] == 0.004
    assert json.loads(json.dumps(data)) == data


def test_negative_failed_round_trips():
    event = NegativeFailed(
        negative_id="negative-0",
        code=Code.STITCH_UNDERCONSTRAINED,
        message="frame not reachable from any other frame",
        run_id="run-1",
    )
    data = event.to_dict()
    assert data["event"] == "negative_failed"
    assert data["negative_id"] == "negative-0"
    assert data["code"] == "STITCH_UNDERCONSTRAINED"
    assert data["message"] == "frame not reachable from any other frame"
    assert json.loads(json.dumps(data)) == data


def test_progress_defaults_to_prepare_stage():
    event = Progress(source_index=0, step=PipelineStep.DECODE, completed=1, total=6)
    assert event.stage == Stage.PREPARE
    assert event.to_dict()["stage"] == "prepare"


def test_progress_carries_stitch_stage():
    event = Progress(
        source_index=0,
        step=PipelineStep.WARP,
        completed=1,
        total=6,
        stage=Stage.STITCH,
    )
    assert event.to_dict()["stage"] == "stitch"
