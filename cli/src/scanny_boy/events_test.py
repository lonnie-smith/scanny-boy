import json

from scanny_boy.events import (
    PROTOCOL_VERSION,
    Code,
    ErrorEvent,
    Event,
    EventWriter,
    Finished,
    GroupDone,
    GroupFailed,
    ItemDone,
    NegativeDone,
    NegativeFailed,
    PipelineStep,
    ProbeResult,
    Progress,
    Stage,
    Started,
    WarningEvent,
)
from scanny_boy.schema_test_support import assert_matches_schema, load_schema

SCHEMA = load_schema()

ALL_EVENTS: list[Event] = [
    Started(command="probe"),
    Started(command="convert", run_id="run-1"),
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
    event = Started(command="convert", run_id="run-7")
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
    writer.write(Progress(source_index=2, step=PipelineStep.WRITE_TIFF, completed=3, total=6))

    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["step"] == "write_tiff"


def test_protocol_version_is_two():
    assert PROTOCOL_VERSION == 2


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


def test_progress_defaults_to_convert_stage():
    event = Progress(source_index=0, step=PipelineStep.DECODE, completed=1, total=6)
    assert event.stage == Stage.CONVERT
    assert event.to_dict()["stage"] == "convert"


def test_progress_carries_stitch_stage():
    event = Progress(
        source_index=0,
        step=PipelineStep.WARP,
        completed=1,
        total=6,
        stage=Stage.STITCH,
    )
    assert event.to_dict()["stage"] == "stitch"
