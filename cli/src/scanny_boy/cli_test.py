import json
import signal
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from scanny_boy import concurrency
from scanny_boy.cli import MAX_SELECTION_FILES, main
from scanny_boy.events import PROTOCOL_VERSION
from scanny_boy.fake_nef_support import write_fake_nef
from scanny_boy.manifest import load_manifest
from scanny_boy.output_folder import STAGING_SUFFIX
from scanny_boy.pipeline import ConvertOutcome
from scanny_boy.roll_manifest import load_roll_manifest, write_roll_manifest
from scanny_boy.sample_nef_support import (
    FIXTURES_DIR,
    REAL_SAMPLE_FILES,
    requires_real_samples,
    stage_samples,
)
from scanny_boy.schema_test_support import assert_matches_schema, load_schema
from scanny_boy.stitch_pipeline_test import _make_work_dir, _roll_dir, _stitch

SCHEMA = load_schema()


def _stdout_events(capsys: pytest.CaptureFixture[str]) -> list[dict]:
    captured = capsys.readouterr()
    events = [json.loads(line) for line in captured.out.splitlines() if line.strip()]
    for event in events:
        assert_matches_schema(event, SCHEMA)
    return events, captured.err


def test_main_with_no_arguments_returns_status_2():
    assert main([]) == 2


def test_version_prints_one_plain_text_line_and_exits_0(capsys):
    """`--version` is a diagnostic outside the event stream (CONTRACT.md);
    the packaged checks of section 5.2 use it as their smoke test."""
    import importlib.metadata

    assert main(["--version"]) == 0
    captured = capsys.readouterr()
    version = importlib.metadata.version("scanny-boy")
    assert captured.out == f"scanny-boy {version}\n"
    assert captured.err == ""


def test_probe_with_input_alone_is_accepted(capsys, tmp_path):
    write_fake_nef(tmp_path / "a.NEF", date_time_original="2026:08:02 12:00:00")
    write_fake_nef(tmp_path / "b.NEF", date_time_original="2026:08:02 12:00:05")

    status = main(["probe", "--input", str(tmp_path)])

    assert status == 0
    events, err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "probe_result", "finished"]
    assert events[0]["command"] == "probe"
    assert events[1]["catalogue"] == ["a.NEF", "b.NEF"]
    assert events[1]["groups"] == []
    assert err == ""


def test_probe_with_input_alone_warns_and_falls_back_on_missing_timestamp(
    capsys, tmp_path
):
    write_fake_nef(tmp_path / "DSC_2.NEF", date_time_original="2026:08:02 12:00:00")
    write_fake_nef(tmp_path / "DSC_10.NEF", date_time_original=None)

    status = main(["probe", "--input", str(tmp_path)])

    assert status == 0
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == [
        "started",
        "warning",
        "probe_result",
        "finished",
    ]
    assert events[1]["code"] == "FILENAME_SORT_USED"
    assert events[2]["catalogue"] == ["DSC_2.NEF", "DSC_10.NEF"]
    assert events[2]["warnings"] == ["FILENAME_SORT_USED"]


def test_probe_with_empty_input_folder_is_no_files(capsys, tmp_path):
    status = main(["probe", "--input", str(tmp_path)])

    assert status == 1
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "error", "finished"]
    assert events[1]["code"] == "NO_FILES"
    assert events[2]["status"] == "failed"
    assert events[2]["exit_status"] == 1


@requires_real_samples
def test_probe_with_files_real_samples_emits_groups(capsys, tmp_path):
    # Staged: the shared fixtures directory also holds the gate-B stitching
    # scans and later sessions, which would make this six-file selection
    # non-contiguous in its catalogue.
    input_dir = stage_samples(tmp_path, list(REAL_SAMPLE_FILES))
    status = main(
        [
            "probe",
            "--input",
            str(input_dir),
            "--files",
            *REAL_SAMPLE_FILES,
            "--per-negative",
            "3",
        ]
    )

    assert status == 0
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "probe_result", "finished"]
    assert events[1]["groups"] == [
        ["_DSC4638.NEF", "_DSC4639.NEF", "_DSC4640.NEF"],
        ["_DSC4644.NEF", "_DSC4645.NEF", "_DSC4646.NEF"],
    ]


@requires_real_samples
def test_probe_with_out_emits_disk_estimate_and_empty_conflicts(capsys, tmp_path):
    negative_1 = ["_DSC4638.NEF", "_DSC4639.NEF", "_DSC4640.NEF"]
    status = main(
        [
            "probe",
            "--input",
            str(FIXTURES_DIR),
            "--files",
            *negative_1,
            "--per-negative",
            "3",
            "--out",
            str(tmp_path),
        ]
    )

    assert status == 0
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "probe_result", "finished"]
    result = events[1]
    assert result["output_conflicts"] == []
    assert isinstance(result["estimated_required_bytes"], int)
    assert result["estimated_required_bytes"] > 0
    assert isinstance(result["available_bytes"], int)
    assert result["available_bytes"] > 0


def test_probe_without_out_leaves_disk_fields_null(capsys, tmp_path):
    write_fake_nef(tmp_path / "a.NEF", date_time_original="2026:08:02 12:00:00")

    status = main(["probe", "--input", str(tmp_path)])

    assert status == 0
    events, _err = _stdout_events(capsys)
    result = events[1]
    assert result["output_conflicts"] == []
    assert result["estimated_required_bytes"] is None
    assert result["available_bytes"] is None


@requires_real_samples
def test_probe_with_out_same_as_input_reports_structured_error(capsys):
    negative_1 = ["_DSC4638.NEF", "_DSC4639.NEF", "_DSC4640.NEF"]
    status = main(
        [
            "probe",
            "--input",
            str(FIXTURES_DIR),
            "--files",
            *negative_1,
            "--per-negative",
            "3",
            "--out",
            str(FIXTURES_DIR),
        ]
    )

    assert status == 1
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "error", "finished"]
    assert events[1]["code"] == "OUTPUT_SAME_AS_INPUT"


@requires_real_samples
def test_probe_with_files_non_contiguous_selection_reports_structured_error(capsys):
    files = [
        "_DSC4638.NEF",
        "_DSC4639.NEF",
        "_DSC4644.NEF",
        "_DSC4645.NEF",
        "_DSC4646.NEF",
    ]

    status = main(
        [
            "probe",
            "--input",
            str(FIXTURES_DIR),
            "--files",
            *files,
            "--per-negative",
            "3",
        ]
    )

    assert status == 1
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "error", "finished"]
    assert events[1]["code"] == "NON_CONTIGUOUS_SELECTION"


def test_prepare_without_files_is_rejected(capsys):
    status = main(
        [
            "prepare",
            "--input",
            "/tmp/in",
            "--out",
            "/tmp/out",
        ]
    )
    assert status == 2
    events, _err = _stdout_events(capsys)
    assert events == []


def test_roll_init_creates_roll_and_emits_roll_created(capsys, tmp_path):
    status = main(
        [
            "roll",
            "init",
            "--library",
            str(tmp_path),
            "--name",
            "Roll A",
        ]
    )

    assert status == 0
    events, err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "roll_created", "finished"]
    assert events[0]["command"] == "roll init"
    assert events[1]["roll_name"] == "Roll A"
    assert events[1]["path"] == str(tmp_path / "Roll-A")
    assert err == ""


def test_roll_init_per_negative_is_no_longer_a_flag(capsys, tmp_path):
    status = main(
        [
            "roll",
            "init",
            "--library",
            str(tmp_path),
            "--name",
            "Roll A",
            "--per-negative",
            "3",
        ]
    )

    # A grouping is each batch's choice, not the roll's — the flag is gone.
    assert status == 2
    events, _err = _stdout_events(capsys)
    assert events == []


def test_roll_init_collision_reports_roll_exists(capsys, tmp_path):
    (tmp_path / "roll-a").mkdir()
    status = main(
        [
            "roll",
            "init",
            "--library",
            str(tmp_path),
            "--name",
            "roll-a",
        ]
    )
    assert status == 0

    events, _err = _stdout_events(capsys)
    assert events[1]["event"] == "roll_created"
    assert events[1]["path"] == str(tmp_path / "roll-a-2")


def test_roll_list_emits_roll_list_with_every_roll(capsys, tmp_path):
    main(
        [
            "roll",
            "init",
            "--library",
            str(tmp_path),
            "--name",
            "Roll A",
        ]
    )
    main(
        [
            "roll",
            "init",
            "--library",
            str(tmp_path),
            "--name",
            "Roll B",
        ]
    )
    capsys.readouterr()

    status = main(["roll", "list", "--library", str(tmp_path)])

    assert status == 0
    events, err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "roll_list", "finished"]
    assert events[0]["command"] == "roll list"
    names = {r["roll_name"] for r in events[1]["rolls"]}
    assert names == {"Roll A", "Roll B"}
    assert all(r["status"] == "ok" for r in events[1]["rolls"])
    assert err == ""


def test_roll_list_on_empty_library_reports_no_rolls(capsys, tmp_path):
    status = main(["roll", "list", "--library", str(tmp_path)])

    assert status == 0
    events, _err = _stdout_events(capsys)
    assert events[1]["rolls"] == []


def test_roll_info_emits_the_manifest(capsys, tmp_path):
    main(
        [
            "roll",
            "init",
            "--library",
            str(tmp_path),
            "--name",
            "Roll A",
        ]
    )
    capsys.readouterr()

    status = main(["roll", "info", "--roll", str(tmp_path / "Roll-A")])

    assert status == 0
    events, err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "roll_info", "finished"]
    assert events[0]["command"] == "roll info"
    assert events[1]["manifest"]["roll_name"] == "Roll A"
    assert err == ""


def test_roll_info_missing_roll_reports_roll_not_found(capsys, tmp_path):
    status = main(["roll", "info", "--roll", str(tmp_path / "nope")])

    assert status == 1
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "error", "finished"]
    assert events[1]["code"] == "ROLL_NOT_FOUND"


def _set_db_revision(revision: str) -> None:
    import sqlite3

    from scanny_boy.library.db import library_db_path

    with sqlite3.connect(library_db_path()) as connection:
        connection.execute("UPDATE alembic_version SET version_num = ?", (revision,))


def test_roll_info_on_a_newer_database_reports_library_db_unsupported(capsys, tmp_path):
    """A database migrated by a newer helper must surface as an ordinary
    `error` event, not a stream that stops after `started` — the app's
    "produced no result" hid an Alembic `ResolutionError`."""
    main(
        [
            "roll",
            "init",
            "--library",
            str(tmp_path),
            "--name",
            "Roll A",
        ]
    )
    capsys.readouterr()
    _set_db_revision("9999")

    status = main(["roll", "info", "--roll", str(tmp_path / "Roll-A")])

    assert status == 1
    events, err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "error", "finished"]
    assert events[1]["code"] == "LIBRARY_DB_UNSUPPORTED"
    assert "9999" in events[1]["message"]
    assert err == ""


def test_an_internal_crash_reaches_the_stream_as_an_error_event(
    capsys, tmp_path, monkeypatch
):
    """Whatever escapes a command must still produce a decodable failure:
    `INTERNAL_ERROR` plus the exception, rather than a bare `started`."""
    main(
        [
            "roll",
            "init",
            "--library",
            str(tmp_path),
            "--name",
            "Roll A",
        ]
    )
    capsys.readouterr()

    def _raise(_roll_dir):
        raise RuntimeError("boom")

    monkeypatch.setattr("scanny_boy.cli.load_roll_manifest", _raise)

    status = main(["roll", "info", "--roll", str(tmp_path / "Roll-A")])

    assert status == 1
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "error", "finished"]
    assert events[1]["code"] == "INTERNAL_ERROR"
    assert "RuntimeError" in events[1]["message"]
    assert "boom" in events[1]["message"]


def test_roll_rename_moves_the_folder_and_updates_the_name(capsys, tmp_path):
    main(
        [
            "roll",
            "init",
            "--library",
            str(tmp_path),
            "--name",
            "Roll A",
        ]
    )
    capsys.readouterr()

    status = main(
        ["roll", "rename", "--roll", str(tmp_path / "Roll-A"), "--name", "Roll B"]
    )

    assert status == 0
    events, err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "roll_renamed", "finished"]
    assert events[0]["command"] == "roll rename"
    assert events[1]["roll_name"] == "Roll B"
    assert events[1]["path"] == str(tmp_path / "Roll-B")
    assert not (tmp_path / "Roll-A").exists()
    assert (tmp_path / "Roll-B").exists()
    assert err == ""


def test_roll_rename_missing_roll_reports_roll_not_found(capsys, tmp_path):
    status = main(
        ["roll", "rename", "--roll", str(tmp_path / "nope"), "--name", "New Name"]
    )

    assert status == 1
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "error", "finished"]
    assert events[1]["code"] == "ROLL_NOT_FOUND"


def test_roll_without_subcommand_returns_status_2(capsys):
    status = main(["roll"])

    assert status == 2
    events, err = _stdout_events(capsys)
    assert events == []
    assert err != ""


def test_apply_metadata_missing_roll_reports_roll_not_found(capsys, tmp_path):
    status = main(["apply-metadata", "--roll", str(tmp_path / "nope")])

    assert status == 1
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "error", "finished"]
    assert events[0]["command"] == "apply-metadata"
    assert events[1]["code"] == "ROLL_NOT_FOUND"


def test_apply_metadata_with_nothing_dirty_exits_0(capsys, tmp_path):
    main(
        [
            "roll",
            "init",
            "--library",
            str(tmp_path),
            "--name",
            "Roll A",
        ]
    )
    capsys.readouterr()

    status = main(["apply-metadata", "--roll", str(tmp_path / "Roll-A")])

    assert status == 0
    events, err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "finished"]
    assert events[1]["status"] == "success"
    assert err == ""


def test_edit_delete_removes_the_negative_and_its_tiff(capsys, tmp_path):
    work_dir = _make_work_dir(tmp_path, negatives=1)
    roll_dir = _roll_dir(tmp_path)
    outcome = _stitch(work_dir, roll_dir)
    assert outcome.status == "complete"
    negative_id = load_roll_manifest(roll_dir).negatives[0].negative_id
    output_name = load_roll_manifest(roll_dir).negatives[0].output["name"]
    assert (roll_dir / output_name).exists()
    capsys.readouterr()

    status = main(
        ["edit", "delete", "--roll", str(roll_dir), "--negative", negative_id]
    )

    assert status == 0
    events, err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "negative_deleted", "finished"]
    assert events[0]["command"] == "edit delete"
    assert events[1]["negative_id"] == negative_id
    assert events[1]["output"] == output_name
    assert events[2]["status"] == "success"
    assert not (roll_dir / output_name).exists()
    assert load_roll_manifest(roll_dir).negatives == []
    assert err == ""


def test_edit_delete_missing_roll_reports_roll_not_found(capsys, tmp_path):
    status = main(
        ["edit", "delete", "--roll", str(tmp_path / "nope"), "--negative", "x"]
    )

    assert status == 1
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "error", "finished"]
    assert events[0]["command"] == "edit delete"
    assert events[1]["code"] == "ROLL_NOT_FOUND"


def test_edit_delete_without_negative_id_returns_status_2(capsys):
    status = main(["edit", "delete", "--roll", "/tmp/roll"])

    assert status == 2
    events, err = _stdout_events(capsys)
    assert events == []
    assert err != ""


def test_exit_status_one_when_anything_was_skipped(capsys, tmp_path):
    work_dir = _make_work_dir(tmp_path, negatives=1)
    roll_dir = _roll_dir(tmp_path)
    outcome = _stitch(work_dir, roll_dir)
    assert outcome.status == "complete"

    roll = load_roll_manifest(roll_dir)
    negative = roll.negatives[0]
    negative.capture_time.intended_datetime_original = "2026-01-15T09:30:00"
    write_roll_manifest(roll_dir, roll)

    # An externally-modified TIFF is skipped rather than rewritten.
    tiff_path = roll_dir / negative.output["name"]
    tiff_path.write_bytes(tiff_path.read_bytes() + b"\x00")

    capsys.readouterr()
    status = main(["apply-metadata", "--roll", str(roll_dir)])

    assert status == 1
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "metadata_skipped", "finished"]
    assert events[1]["code"] == "OUTPUT_MODIFIED_EXTERNALLY"
    assert events[2]["status"] == "failed"
    assert events[2]["exit_status"] == 1


def test_invalid_command_returns_status_2_with_no_stdout_events(capsys):
    status = main(["frobnicate"])
    assert status == 2
    events, err = _stdout_events(capsys)
    assert events == []
    assert err != ""


def test_film_date_argument_is_rejected(capsys):
    """Phase 3 section 3.5: `--film-date` is removed from every command,
    so `convert` (and `run`) no longer recognize it at all."""
    status = main(
        [
            "prepare",
            "--input",
            "/tmp/in",
            "--files",
            "a.NEF",
            "--out",
            "/tmp/out",
            "--film-date",
            "2026-08-02",
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


def test_probe_catalogue_only_needs_no_per_negative(capsys, tmp_path):
    """Without `--files` there is no selection to group, so `--per-negative`
    is not required."""
    write_fake_nef(tmp_path / "a.NEF")
    status = main(["probe", "--input", str(tmp_path)])
    assert status == 0


def test_probe_with_files_requires_per_negative(capsys):
    status = main(["probe", "--input", "/tmp/in", "--files", "a.NEF"])
    assert status == 2
    events, err = _stdout_events(capsys)
    assert events == []
    assert "--per-negative" in err


@pytest.mark.parametrize("jobs", ["0", "13", "-1"])
def test_job_count_out_of_range_returns_status_2(capsys, jobs):
    status = main(
        [
            "prepare",
            "--input",
            "/tmp/in",
            "--files",
            "a.NEF",
            "--out",
            "/tmp/out",
            "--jobs",
            jobs,
        ]
    )
    assert status == 2
    events, _err = _stdout_events(capsys)
    assert events == []


def test_selection_above_5000_files_is_a_usage_error(capsys, tmp_path):
    files = [f"DSC_{i:05d}.NEF" for i in range(MAX_SELECTION_FILES + 1)]
    status = main(["probe", "--input", str(tmp_path), "--files", *files])
    assert status == 2
    events, err = _stdout_events(capsys)
    assert events == []
    assert err != ""


def test_selection_at_5000_files_is_not_rejected_by_the_usage_cap(capsys, tmp_path):
    # The 5000-file usage cap is checked purely from argv, before any
    # catalogue work. At exactly the cap it must not be rejected there; it
    # proceeds to real catalogue validation and fails differently instead
    # (the input folder is empty).
    files = [f"DSC_{i:05d}.NEF" for i in range(MAX_SELECTION_FILES)]
    status = main(
        ["probe", "--input", str(tmp_path), "--files", *files, "--per-negative", "3"]
    )
    assert status == 1
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "error", "finished"]
    assert events[1]["code"] == "NO_FILES"


def test_stderr_never_contains_machine_readable_events(capsys):
    scenarios = [
        [],
        ["frobnicate"],
        ["probe", "--input", "/tmp/in", "--per-negative", "99"],
        [
            "prepare",
            "--input",
            "/tmp/in",
            "--files",
            "a.NEF",
            "--out",
            "/tmp/out",
            "--per-negative",
            "not-a-number",
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


def test_prepare_started_carries_a_run_id_even_when_validation_fails_immediately(
    capsys,
):
    # `--input`/`--files` don't need to exist yet for `started` itself to
    # carry a run_id — the run "exists" as soon as convert begins, even if
    # it fails validation a moment later (here: the input folder is
    # missing, so this fails with NO_FILES before any real work starts).
    status = main(
        [
            "prepare",
            "--input",
            "/tmp/in",
            "--files",
            "a.NEF",
            "b.NEF",
            "--out",
            "/tmp/out",
            "--per-negative",
            "3",
            "--jobs",
            "2",
            "--overwrite",
        ]
    )
    assert status == 1
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "error", "finished"]
    assert events[0]["command"] == "prepare"
    run_id = events[0]["run_id"]
    assert run_id
    assert all(e["run_id"] == run_id for e in events)
    assert events[1]["code"] == "NO_FILES"
    assert events[2]["exit_status"] == 1


@pytest.mark.slow
@requires_real_samples
def test_convert_with_real_samples_writes_six_tiffs_and_completes(capsys, tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    input_dir = stage_samples(tmp_path, list(REAL_SAMPLE_FILES))

    status = main(
        [
            "prepare",
            "--input",
            str(input_dir),
            "--files",
            *REAL_SAMPLE_FILES,
            "--per-negative",
            "3",
            "--out",
            str(out_dir),
        ]
    )

    assert status == 0
    events, _err = _stdout_events(capsys)
    assert events[0]["event"] == "started"
    assert events[-1] == {
        "protocol_version": PROTOCOL_VERSION,
        "event": "finished",
        "run_id": events[0]["run_id"],
        "status": "success",
        "exit_status": 0,
    }
    assert {e["event"] for e in events} >= {
        "started",
        "progress",
        "item_done",
        "group_done",
        "finished",
    }
    for name in REAL_SAMPLE_FILES:
        assert (out_dir / f"{Path(name).stem}.tif").exists()
    assert (out_dir / "scanny-boy-manifest.json").exists()


# =========================================================================
# Chunk 6: --jobs, cancellation, and exit status 143 (section 3.8)
# =========================================================================


def _convert_argv(input_dir, out_dir, files, **extra) -> list[str]:
    argv = [
        "prepare",
        "--input",
        str(input_dir),
        "--files",
        *files,
        "--per-negative",
        "3",
        "--out",
        str(out_dir),
    ]
    for key, value in extra.items():
        argv += [f"--{key.replace('_', '-')}", str(value)]
    return argv


@pytest.mark.parametrize("jobs", ["0", "13", "-1"])
def test_jobs_outside_1_to_12_is_a_usage_error(capsys, jobs, tmp_path):
    status = main(_convert_argv("/tmp/in", tmp_path, ["a.NEF"], jobs=jobs))
    assert status == 2
    events, err = _stdout_events(capsys)
    assert events == []
    assert "--jobs" in err


def test_an_explicit_jobs_over_the_memory_budget_reports_insufficient_memory(
    capsys, monkeypatch, tmp_path
):
    monkeypatch.setattr(
        concurrency,
        "physical_memory_bytes",
        lambda: 2 * concurrency.WORKER_MEMORY_BUDGET_BYTES,
    )

    status = main(_convert_argv("/tmp/in", tmp_path, ["a.NEF", "b.NEF"], jobs=12))

    # Not a usage error (exit 2): the command is well formed, this
    # machine just cannot honour it. Same shape as INSUFFICIENT_DISK.
    assert status == 1
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "error", "finished"]
    assert events[1]["code"] == "INSUFFICIENT_MEMORY"
    assert events[2]["exit_status"] == 1


def test_a_cancelled_run_emits_cancelled_and_exits_143(capsys, monkeypatch, tmp_path):
    """The exit status and event tail of a cooperative cancellation,
    without paying for a real conversion. The real signal path is covered
    by `test_sigterm_during_a_real_conversion_exits_143` below."""

    def _cancelled_run(*args, **kwargs):
        return ConvertOutcome(
            run_id=kwargs["run_id"],
            status="cancelled",
            manifest=None,
            workers=1,
        )

    monkeypatch.setattr("scanny_boy.cli.run_convert", _cancelled_run)

    status = main(_convert_argv("/tmp/in", tmp_path, ["a.NEF", "b.NEF"]))

    assert status == 143  # 128 + SIGTERM
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "error", "finished"]
    assert events[1]["code"] == "CANCELLED"
    assert events[2]["status"] == "cancelled"
    assert events[2]["exit_status"] == 143


# --- real subprocesses, driven by their own event stream -----------------


def _spawn_convert(input_dir: Path, out_dir: Path, files: list[str], **extra) -> subprocess.Popen:
    argv = [
        sys.executable,
        "-m",
        "scanny_boy.cli",
        *_convert_argv(input_dir, out_dir, files, **extra),
    ]
    return subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
    )


def _read_until(proc: subprocess.Popen, predicate, *, timeout: float = 120) -> dict:
    """Read the child's event stream until `predicate` matches an event.

    This is the "controlled" half of the chunk's "cancels only after work
    has definitely started; do not use a race-prone fixed sleep": the
    signal is sent in response to the child telling us where it is, not
    after an interval we guessed.
    """
    deadline = time.monotonic() + timeout
    for line in proc.stdout:
        if time.monotonic() > deadline:
            break
        line = line.strip()
        if not line:
            continue
        event = json.loads(line)
        if predicate(event):
            return event
    raise AssertionError("the child never emitted a matching event")


@pytest.mark.slow
@requires_real_samples
def test_sigterm_during_a_real_conversion_exits_143(tmp_path):
    """End to end: a real child process, a real SIGTERM, exit 143, the
    first negative kept and the second discarded."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    input_dir = stage_samples(tmp_path, list(REAL_SAMPLE_FILES))
    proc = _spawn_convert(input_dir, out_dir, REAL_SAMPLE_FILES, per_negative=3, jobs=1)
    try:
        # Wait until the first negative has been published in full, so
        # "completed groups remain" is actually being tested.
        _read_until(proc, lambda e: e["event"] == "group_done")
        # ...and until the second negative is genuinely under way.
        _read_until(
            proc,
            lambda e: e["event"] == "progress" and e["source_index"] >= 3,
        )
        proc.send_signal(signal.SIGTERM)
        remaining = proc.stdout.read()
        status = proc.wait(timeout=120)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)
        proc.stdout.close()

    assert status == 143

    tail = [json.loads(line) for line in remaining.splitlines() if line.strip()]
    assert tail, "the child emitted no events after the signal"
    assert tail[-1]["event"] == "finished"
    assert tail[-1]["status"] == "cancelled"
    assert tail[-1]["exit_status"] == 143
    assert any(e.get("code") == "CANCELLED" for e in tail)

    # The first negative survived; the second was discarded whole.
    for name in REAL_SAMPLE_FILES[:3]:
        assert (out_dir / f"{Path(name).stem}.tif").exists()
    for name in REAL_SAMPLE_FILES[3:]:
        assert not (out_dir / f"{Path(name).stem}.tif").exists()

    manifest = load_manifest(out_dir)
    assert manifest.status == "cancelled"
    assert [g.status for g in manifest.groups] == ["completed", "pending"]
    assert [p for p in out_dir.iterdir() if p.name.endswith(STAGING_SUFFIX)] == []


@pytest.mark.slow
@requires_real_samples
def test_forced_termination_leaves_running_state_that_the_next_run_recovers(tmp_path):
    """Section 3.8: "A forced stop cannot clean files, update the
    manifest, or emit a final event... The next probe or conversion
    detects a manifest left as `running` and staging directories owned by
    that run. It removes those staging directories before rerunning."

    SIGKILL, not SIGTERM: the point is the state a *forced* stop leaves.
    """
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    input_dir = stage_samples(tmp_path, list(REAL_SAMPLE_FILES))
    proc = _spawn_convert(input_dir, out_dir, REAL_SAMPLE_FILES, per_negative=3, jobs=2)
    try:
        _read_until(proc, lambda e: e["event"] == "progress")
        proc.kill()
        status = proc.wait(timeout=120)
    finally:
        proc.stdout.close()

    assert status == -signal.SIGKILL

    # The forced stop left exactly the wreckage section 3.8 predicts.
    abandoned = load_manifest(out_dir)
    assert abandoned.status == "running"
    assert abandoned.finished_at is None
    staging = [p for p in out_dir.iterdir() if p.name.endswith(STAGING_SUFFIX)]
    assert len(staging) == 1
    assert staging[0].name.startswith(abandoned.run_id)

    # The next run cleans it up and completes the incomplete group.
    status = main(_convert_argv(input_dir, out_dir, REAL_SAMPLE_FILES))

    assert status == 0
    recovered = load_manifest(out_dir)
    assert recovered.status == "complete"
    assert recovered.run_id != abandoned.run_id
    assert all(g.status == "completed" for g in recovered.groups)
    for name in REAL_SAMPLE_FILES:
        assert (out_dir / f"{Path(name).stem}.tif").exists()
    assert [p for p in out_dir.iterdir() if p.name.endswith(STAGING_SUFFIX)] == []


# --- flatfield ------------------------------------------------------------


def _save_flatfield_profile(name: str = "Copy stand") -> None:
    from scanny_boy import flatfield
    from scanny_boy.library import repo

    gain_map = np.full((8, 8, 3), 1.5, dtype=np.float32)
    path, sha256 = flatfield.save_gain_map(f"pid-{name}", gain_map)
    repo.save_flatfield_profile(
        flatfield.FlatFieldProfile(
            profile_id=f"pid-{name}",
            name=name,
            gain_map_path=str(path),
            gain_map_sha256=sha256,
            source_path="/refs/bare.NEF",
            reference_width=12,
            reference_height=8,
            params=flatfield.build_params(),
            scanny_boy_version="0.3.0",
            created_at="2026-09-01T00:00:00Z",
        )
    )


def test_flatfield_list_reports_an_empty_library(capsys):
    status = main(["flatfield", "list"])

    assert status == 0
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "flatfield_list", "finished"]
    assert events[0]["command"] == "flatfield list"
    assert events[1]["profiles"] == []


def test_flatfield_create_rejects_a_taken_name_without_decoding(capsys, tmp_path):
    _save_flatfield_profile("Copy stand")

    status = main(
        [
            "flatfield",
            "create",
            "--reference",
            str(tmp_path / "does-not-matter.NEF"),
            "--name",
            "Copy stand",
        ]
    )

    assert status == 1
    events, _err = _stdout_events(capsys)
    assert events[1]["code"] == "FLATFIELD_PROFILE_EXISTS"


def test_flatfield_create_maps_a_non_raw_reference_to_unsupported_raw(capsys, tmp_path):
    write_fake_nef(tmp_path / "ref.NEF")

    status = main(
        [
            "flatfield",
            "create",
            "--reference",
            str(tmp_path / "ref.NEF"),
            "--name",
            "Nope",
        ]
    )

    assert status == 1
    events, _err = _stdout_events(capsys)
    assert events[1]["code"] == "UNSUPPORTED_RAW"


def test_flatfield_delete_unknown_profile_is_not_found(capsys):
    status = main(["flatfield", "delete", "--profile", "nope"])

    assert status == 1
    events, _err = _stdout_events(capsys)
    assert events[1]["code"] == "FLATFIELD_PROFILE_NOT_FOUND"


def test_flatfield_delete_refuses_a_profile_locked_into_a_roll(capsys, tmp_path):
    _save_flatfield_profile("Copy stand")
    from scanny_boy import flatfield
    from scanny_boy.library import repo
    from scanny_boy.roll_manifest import new_roll_manifest, write_roll_manifest

    roll_dir = tmp_path / "Roll"
    roll_dir.mkdir()
    manifest = new_roll_manifest(roll_id="rid-1", roll_name="Roll")
    manifest.processing_params = {
        "output_bps": 16,
        "flat_field": flatfield.profile_token(
            repo.load_flatfield_profile("pid-Copy stand")
        ),
    }
    write_roll_manifest(roll_dir, manifest)

    status = main(["flatfield", "delete", "--profile", "pid-Copy stand"])

    assert status == 1
    events, _err = _stdout_events(capsys)
    assert events[1]["code"] == "FLATFIELD_PROFILE_IN_USE"
    assert repo.load_flatfield_profile("pid-Copy stand") is not None


def test_flatfield_delete_removes_the_row_and_the_npz(capsys, tmp_path):
    _save_flatfield_profile("Copy stand")
    from scanny_boy.library import repo

    profile = repo.load_flatfield_profile("pid-Copy stand")
    assert Path(profile.gain_map_path).exists()

    status = main(["flatfield", "delete", "--profile", "pid-Copy stand"])

    assert status == 0
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == [
        "started",
        "flatfield_deleted",
        "finished",
    ]
    assert events[1]["profile_id"] == "pid-Copy stand"
    assert repo.list_flatfield_profiles() == []
    assert not Path(profile.gain_map_path).exists()


@requires_real_samples
def test_flatfield_create_list_and_delete_round_trip(capsys):
    status = main(
        [
            "flatfield",
            "create",
            "--reference",
            str(FIXTURES_DIR / "_DSC4638.NEF"),
            "--name",
            "Real reference",
        ]
    )

    assert status == 0
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == [
        "started",
        "flatfield_created",
        "finished",
    ]
    profile = events[1]["profile"]
    assert profile["name"] == "Real reference"
    assert profile["reference_width"] == 6064
    assert profile["reference_height"] == 4040
    assert profile["source_path"].endswith("_DSC4638.NEF")

    status = main(["flatfield", "list"])
    assert status == 0
    events, _err = _stdout_events(capsys)
    assert [p["profile_id"] for p in events[1]["profiles"]] == [profile["profile_id"]]

    status = main(["flatfield", "delete", "--profile", profile["profile_id"]])
    assert status == 0
    events, _err = _stdout_events(capsys)
    assert events[1]["profile_id"] == profile["profile_id"]
