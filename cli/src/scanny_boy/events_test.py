import json

from scanny_boy.events import (
    Code,
    ErrorEvent,
    Event,
    EventWriter,
    Finished,
    GroupDone,
    GroupFailed,
    ItemDone,
    PipelineStep,
    ProbeResult,
    Progress,
    Started,
    WarningEvent,
)
from scanny_boy.schema_test_support import assert_matches_schema, load_schema

SCHEMA = load_schema()

ALL_EVENTS: list[Event] = [
    Started(command="probe"),
    Started(command="convert", run_id="run-1"),
    ProbeResult(catalogue=["DSC_0001.NEF", "DSC_0002.NEF"], run_id="run-1"),
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
