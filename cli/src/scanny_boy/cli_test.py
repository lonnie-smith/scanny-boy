import json

import pytest

from scanny_boy.cli import MAX_SELECTION_FILES, main
from scanny_boy.schema_test_support import assert_matches_schema, load_schema

SCHEMA = load_schema()


def _stdout_events(capsys: pytest.CaptureFixture[str]) -> list[dict]:
    captured = capsys.readouterr()
    events = [json.loads(line) for line in captured.out.splitlines() if line.strip()]
    for event in events:
        assert_matches_schema(event, SCHEMA)
    return events, captured.err


def test_main_with_no_arguments_returns_status_2():
    assert main([]) == 2


def test_probe_with_input_alone_is_accepted(capsys):
    status = main(["probe", "--input", "/tmp/some-input"])
    assert status == 0
    events, err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "finished"]
    assert events[0]["command"] == "probe"
    assert err == ""


def test_convert_without_files_is_rejected(capsys):
    status = main(
        [
            "convert",
            "--input",
            "/tmp/in",
            "--out",
            "/tmp/out",
            "--film-date",
            "2026-08-02",
        ]
    )
    assert status == 2
    events, _err = _stdout_events(capsys)
    assert events == []


def test_invalid_command_returns_status_2_with_no_stdout_events(capsys):
    status = main(["frobnicate"])
    assert status == 2
    events, err = _stdout_events(capsys)
    assert events == []
    assert err != ""


def test_invalid_film_date_returns_status_2(capsys):
    status = main(
        [
            "convert",
            "--input",
            "/tmp/in",
            "--files",
            "a.NEF",
            "--out",
            "/tmp/out",
            "--film-date",
            "not-a-date",
        ]
    )
    assert status == 2
    events, _err = _stdout_events(capsys)
    assert events == []


@pytest.mark.parametrize("per_negative", ["0", "13", "-1"])
def test_per_negative_out_of_range_returns_structured_error(capsys, per_negative):
    status = main(["probe", "--input", "/tmp/in", "--per-negative", per_negative])
    assert status == 2
    events, _err = _stdout_events(capsys)
    assert len(events) == 1
    assert events[0]["event"] == "error"
    assert events[0]["code"] == "INVALID_PER_NEGATIVE"


def test_per_negative_default_is_three(capsys):
    status = main(["probe", "--input", "/tmp/in"])
    assert status == 0


@pytest.mark.parametrize("jobs", ["0", "13", "-1"])
def test_job_count_out_of_range_returns_status_2(capsys, jobs):
    status = main(
        [
            "convert",
            "--input",
            "/tmp/in",
            "--files",
            "a.NEF",
            "--out",
            "/tmp/out",
            "--film-date",
            "2026-08-02",
            "--jobs",
            jobs,
        ]
    )
    assert status == 2
    events, _err = _stdout_events(capsys)
    assert events == []


def test_selection_above_5000_files_is_a_usage_error(capsys):
    files = [f"DSC_{i:05d}.NEF" for i in range(MAX_SELECTION_FILES + 1)]
    status = main(["probe", "--input", "/tmp/in", "--files", *files])
    assert status == 2
    events, err = _stdout_events(capsys)
    assert events == []
    assert err != ""


def test_selection_at_5000_files_is_accepted(capsys):
    files = [f"DSC_{i:05d}.NEF" for i in range(MAX_SELECTION_FILES)]
    status = main(["probe", "--input", "/tmp/in", "--files", *files])
    assert status == 0


def test_stderr_never_contains_machine_readable_events(capsys):
    scenarios = [
        [],
        ["frobnicate"],
        ["probe", "--input", "/tmp/in", "--per-negative", "99"],
        [
            "convert",
            "--input",
            "/tmp/in",
            "--files",
            "a.NEF",
            "--out",
            "/tmp/out",
            "--film-date",
            "bad",
        ],
    ]
    for argv in scenarios:
        main(argv)
        err = capsys.readouterr().err
        for line in err.splitlines():
            if not line.strip():
                continue
            with pytest.raises(json.JSONDecodeError):
                json.loads(line)


def test_convert_with_valid_arguments_emits_started_and_finished(capsys):
    status = main(
        [
            "convert",
            "--input",
            "/tmp/in",
            "--files",
            "a.NEF",
            "b.NEF",
            "--out",
            "/tmp/out",
            "--film-date",
            "2026-08-02",
            "--jobs",
            "2",
            "--overwrite",
        ]
    )
    assert status == 0
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "finished"]
    assert events[0]["command"] == "convert"
    assert events[1]["exit_status"] == 0
