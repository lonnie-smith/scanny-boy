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
from scanny_boy.library import repo
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


def _tone_params(grade_r: float = 90.0, snap_gamma: float = 0.2, **overrides: float):
    from scanny_boy import tone

    params = {
        "grade_r": grade_r,
        "snap_gamma": snap_gamma,
        "density": tone.DENSITY_REFERENCE,
        "shadow_density": 0.0,
        "highlight_density": 0.0,
        "toe": 0.0,
        "toe_width": tone.WIDTH_REFERENCE,
        "shoulder": 0.0,
        "shoulder_width": tone.WIDTH_REFERENCE,
    }
    params.update(overrides)
    return params


def _reset_tone_params():
    from scanny_boy import tone

    return {key: None for key in tone.TONE_PARAM_KEYS}


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
            "--film-kind",
            "colour",
        ]
    )

    assert status == 0
    events, err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "roll_created", "finished"]
    assert events[0]["command"] == "roll init"
    assert events[1]["roll_name"] == "Roll A"
    assert events[1]["path"] == str(tmp_path / "Roll-A")
    manifest = load_roll_manifest(tmp_path / "Roll-A")
    assert manifest.film == {"kind": "colour"}
    assert err == ""


def test_roll_init_requires_film_kind(capsys, tmp_path):
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

    assert status == 2
    events, _err = _stdout_events(capsys)
    assert events == []


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
            "--film-kind",
            "colour",
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
            "--film-kind",
            "colour",
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
            "--film-kind",
            "colour",
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
            "--film-kind",
            "colour",
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
            "--film-kind",
            "colour",
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
            "--film-kind",
            "colour",
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
            "--film-kind",
            "colour",
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
            "--film-kind",
            "colour",
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


# --- roll set-base-frame (docs/REBATE_ANCHORING.md section 7.1) -----------


def _base_measurement(
    density: tuple[float, float, float] = (-0.42, -0.12, -0.99),
) -> "object":
    from scanny_boy import film_base

    population = film_base.Population(
        density=density, luma=-0.25, area_fraction=0.44, cells=34100, spread=0.012
    )
    return film_base.BaseMeasurement(
        density=density,
        chosen_index=0,
        populations=(population,),
        clipped_fractions=(0.0, 0.0, 0.0),
        grid_cells=786432,
    )


def _init_roll(capsys, tmp_path, name: str = "Roll A") -> Path:
    main(
        [
            "roll",
            "init",
            "--library",
            str(tmp_path),
            "--name",
            name,
            "--film-kind",
            "colour",
        ]
    )
    capsys.readouterr()
    return tmp_path / name.replace(" ", "-")


def _set_base_frame(capsys, roll_dir: Path, frame: Path, *extra: str) -> int:
    return main(
        [
            "roll",
            "set-base-frame",
            "--roll",
            str(roll_dir),
            "--frame",
            str(frame),
            *extra,
        ]
    )


def test_roll_set_base_frame_attaches_on_an_absent_roll(capsys, tmp_path, monkeypatch):
    roll_dir = _init_roll(capsys, tmp_path)
    frame = write_fake_nef(tmp_path / "_DSC5012.NEF")
    monkeypatch.setattr(
        "scanny_boy.cli.film_base.load", lambda _frame, _gain: _base_measurement()
    )

    status = _set_base_frame(capsys, roll_dir, frame)

    assert status == 0
    events, err = _stdout_events(capsys)
    assert [e["event"] for e in events] == [
        "started",
        "base_frame_set",
        "finished",
    ]
    assert events[0]["command"] == "roll set-base-frame"
    assert events[1]["source_name"] == "_DSC5012.NEF"
    assert events[1]["density"] == [-0.42, -0.12, -0.99]
    assert events[1]["area_fraction"] == 0.44
    assert events[1]["population_count"] == 1
    assert events[1]["locked"] is False
    assert err == ""

    manifest = load_roll_manifest(roll_dir)
    block = manifest.film_base
    assert block is not None
    assert block["density"] == [-0.42, -0.12, -0.99]
    assert block["locked_at"] is None
    assert block["source_name"] == "_DSC5012.NEF"
    assert block["source_sha256"] == "f" * 64 or len(block["source_sha256"]) == 64
    assert block["chosen_index"] == 0
    assert block["populations"][0]["area_fraction"] == 0.44
    assert block["grid_cells"] == 786432
    assert block["measure_version"] == 1


def test_roll_set_base_frame_replaces_on_an_attached_roll(
    capsys, tmp_path, monkeypatch
):
    roll_dir = _init_roll(capsys, tmp_path)
    first = write_fake_nef(tmp_path / "_DSC5012.NEF")
    second = write_fake_nef(tmp_path / "_DSC5013.NEF")
    monkeypatch.setattr(
        "scanny_boy.cli.film_base.load", lambda _frame, _gain: _base_measurement()
    )

    assert _set_base_frame(capsys, roll_dir, first) == 0
    capsys.readouterr()
    assert _set_base_frame(capsys, roll_dir, second) == 0

    manifest = load_roll_manifest(roll_dir)
    assert manifest.film_base["source_name"] == "_DSC5013.NEF"
    assert manifest.film_base["locked_at"] is None


def test_roll_set_base_frame_refuses_a_locked_roll(capsys, tmp_path, monkeypatch):
    from scanny_boy.roll_manifest import write_roll_manifest as write_manifest

    roll_dir = _init_roll(capsys, tmp_path)
    frame = write_fake_nef(tmp_path / "_DSC5012.NEF")
    monkeypatch.setattr(
        "scanny_boy.cli.film_base.load", lambda _frame, _gain: _base_measurement()
    )
    assert _set_base_frame(capsys, roll_dir, frame) == 0
    capsys.readouterr()

    manifest = load_roll_manifest(roll_dir)
    manifest.film_base["locked_at"] = "2026-09-06T19:00:00Z"
    write_manifest(roll_dir, manifest)
    capsys.readouterr()

    status = _set_base_frame(capsys, roll_dir, frame)

    assert status == 1
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "error", "finished"]
    assert events[1]["code"] == "FILM_BASE_LOCKED"
    assert "2026-09-06" in events[1]["message"]
    # The block is unchanged.
    assert load_roll_manifest(roll_dir).film_base["source_name"] == "_DSC5012.NEF"
    assert load_roll_manifest(roll_dir).film_base["locked_at"] == "2026-09-06T19:00:00Z"


def test_roll_set_base_frame_gate_failure_changes_nothing_on_disk(
    capsys, tmp_path, monkeypatch
):
    from scanny_boy import film_base
    from scanny_boy.events import Code

    roll_dir = _init_roll(capsys, tmp_path)
    frame = write_fake_nef(tmp_path / "_DSC5012.NEF")
    monkeypatch.setattr(
        "scanny_boy.cli.film_base.load", lambda _frame, _gain: _base_measurement()
    )

    def _too_small(_measurement):
        raise film_base.FilmBaseError(
            Code.FILM_BASE_TOO_SMALL,
            "the largest flat rebate region covers only 9% of the base frame",
        )

    monkeypatch.setattr("scanny_boy.cli.film_base.gate", _too_small)

    status = _set_base_frame(capsys, roll_dir, frame)

    assert status == 1
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "error", "finished"]
    assert events[1]["code"] == "FILM_BASE_TOO_SMALL"
    assert load_roll_manifest(roll_dir).film_base is None


def test_roll_set_base_frame_refuses_a_version_7_roll(capsys, tmp_path, monkeypatch):
    """§9: a roll stitched before film-base anchoring cannot be given one.
    The library database does not persist `manifest_format_version` — every
    roll it can produce reads back at the current version — so the v7
    manifest is staged in memory ahead of the load."""
    roll_dir = _init_roll(capsys, tmp_path)
    v7_manifest = load_roll_manifest(roll_dir)
    v7_manifest.manifest_format_version = 7
    monkeypatch.setattr("scanny_boy.cli.load_roll_manifest", lambda _dir: v7_manifest)
    frame = write_fake_nef(tmp_path / "_DSC5012.NEF")

    status = _set_base_frame(capsys, roll_dir, frame)

    assert status == 1
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "error", "finished"]
    assert events[1]["code"] == "ROLL_PREDATES_FILM_BASE"
    assert load_roll_manifest(roll_dir).film_base is None


def test_roll_set_base_frame_warns_on_camera_conflict(capsys, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from scanny_boy.roll_manifest import CameraColor
    from scanny_boy.roll_manifest import write_roll_manifest as write_manifest

    roll_dir = _init_roll(capsys, tmp_path)
    frame = write_fake_nef(tmp_path / "_DSC5012.NEF")
    monkeypatch.setattr(
        "scanny_boy.cli.film_base.load", lambda _frame, _gain: _base_measurement()
    )
    monkeypatch.setattr(
        "scanny_boy.cli.read_source_settings",
        lambda _frame: SimpleNamespace(make="NIKON CORPORATION", model="NIKON Z f"),
    )
    manifest = load_roll_manifest(roll_dir)
    manifest.camera_color = CameraColor(
        rgb_xyz_matrix=((0.7, 0.2, 0.1), (0.1, 0.75, 0.15), (0.05, 0.1, 0.85)),
        source="libraw",
        camera_model="NIKON Z 7",
    )
    write_manifest(roll_dir, manifest)
    capsys.readouterr()

    status = _set_base_frame(capsys, roll_dir, frame)

    assert status == 0
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == [
        "started",
        "warning",
        "base_frame_set",
        "finished",
    ]
    assert events[1]["code"] == "FILM_BASE_CAMERA_CONFLICT"
    assert load_roll_manifest(roll_dir).film_base["camera_model"] == (
        "NIKON CORPORATION NIKON Z f"
    )


def test_roll_set_base_frame_records_the_flatfield_profile_id(
    capsys, tmp_path, monkeypatch
):
    roll_dir = _init_roll(capsys, tmp_path)
    frame = write_fake_nef(tmp_path / "_DSC5012.NEF")
    captured: dict = {}

    def _fake_load(reference, gain_map):
        captured["gain_map_given"] = gain_map is not None
        return _base_measurement()

    monkeypatch.setattr("scanny_boy.cli.film_base.load", _fake_load)

    # An unknown profile id fails before anything is written.
    status = _set_base_frame(capsys, roll_dir, frame, "--flatfield", "nope")
    assert status == 1
    events, _err = _stdout_events(capsys)
    assert events[1]["code"] == "FLATFIELD_PROFILE_NOT_FOUND"
    assert load_roll_manifest(roll_dir).film_base is None


def test_roll_info_reports_the_film_base_block(capsys, tmp_path, monkeypatch):
    roll_dir = _init_roll(capsys, tmp_path)
    frame = write_fake_nef(tmp_path / "_DSC5012.NEF")
    monkeypatch.setattr(
        "scanny_boy.cli.film_base.load", lambda _frame, _gain: _base_measurement()
    )
    assert _set_base_frame(capsys, roll_dir, frame) == 0
    capsys.readouterr()

    status = main(["roll", "info", "--roll", str(roll_dir)])

    assert status == 0
    events, _err = _stdout_events(capsys)
    info = events[1]["manifest"]
    assert info["film_base"]["density"] == [-0.42, -0.12, -0.99]
    assert info["film_base"]["locked_at"] is None


def test_roll_info_reports_a_null_film_base_block(capsys, tmp_path):
    roll_dir = _init_roll(capsys, tmp_path)

    status = main(["roll", "info", "--roll", str(roll_dir)])

    assert status == 0
    events, _err = _stdout_events(capsys)
    assert events[1]["manifest"]["film_base"] is None


def test_roll_delete_unregisters_the_roll_and_leaves_the_folder(capsys, tmp_path):
    main(
        [
            "roll",
            "init",
            "--library",
            str(tmp_path),
            "--name",
            "Roll A",
            "--film-kind",
            "colour",
        ]
    )
    created = _stdout_events(capsys)[0][1]
    roll_dir = tmp_path / "Roll-A"
    capsys.readouterr()

    status = main(["roll", "delete", "--roll", str(roll_dir)])

    assert status == 0
    events, err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "roll_deleted", "finished"]
    assert events[0]["command"] == "roll delete"
    assert events[1]["roll_id"] == created["roll_id"]
    assert events[1]["path"] == str(roll_dir)
    assert events[2]["status"] == "success"
    # The folder is the app's to trash; the CLI only unregisters.
    assert roll_dir.exists()
    assert err == ""

    # The deleted roll no longer comes back on the next scan — the bug the
    # app's delete used to hit, when only its folder went to the Trash.
    capsys.readouterr()
    status = main(["roll", "list", "--library", str(tmp_path)])
    assert status == 0
    events, _err = _stdout_events(capsys)
    assert events[1]["rolls"] == []


def test_roll_delete_missing_roll_reports_roll_not_found(capsys, tmp_path):
    status = main(["roll", "delete", "--roll", str(tmp_path / "nope")])

    assert status == 1
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "error", "finished"]
    assert events[0]["command"] == "roll delete"
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
            "--film-kind",
            "colour",
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


def test_edit_render_region_renders_the_requested_region(capsys, tmp_path):
    import cv2
    import tifffile

    from scanny_boy.previews import NORMALIZED_DISPLAY_LUT

    work_dir = _make_work_dir(tmp_path, negatives=1)
    roll_dir = _roll_dir(tmp_path)
    outcome = _stitch(work_dir, roll_dir)
    assert outcome.status == "complete"
    negative = load_roll_manifest(roll_dir).negatives[0]
    tiff = tifffile.imread(roll_dir / negative.output["name"])
    capsys.readouterr()

    destination = tmp_path / "region.png"
    status = main(
        [
            "edit",
            "render-region",
            "--roll",
            str(roll_dir),
            "--negative",
            negative.negative_id,
            "--x",
            "4",
            "--y",
            "2",
            "--width",
            "10",
            "--height",
            "6",
            "--output",
            str(destination),
        ]
    )

    assert status == 0
    events, err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "region_rendered", "finished"]
    assert events[0]["command"] == "edit render-region"
    assert events[1]["negative_id"] == negative.negative_id
    assert events[1]["path"] == str(destination)
    assert (
        events[1]["x"],
        events[1]["y"],
        events[1]["width"],
        events[1]["height"],
    ) == (4, 2, 10, 6)
    assert events[2]["status"] == "success"
    assert err == ""

    stored = cv2.imread(str(destination), cv2.IMREAD_UNCHANGED)
    display = NORMALIZED_DISPLAY_LUT[tiff[2:8, 4:14]]
    np.testing.assert_array_equal(stored, cv2.cvtColor(display, cv2.COLOR_RGB2BGR))


def test_edit_render_region_folds_in_the_net_transform(capsys, tmp_path):
    """The region comes back in display space — the net transform applied —
    so a region asked for in preview coordinates matches the preview."""
    import cv2
    import tifffile

    from scanny_boy.library import repo
    from scanny_boy.previews import NORMALIZED_DISPLAY_LUT

    work_dir = _make_work_dir(tmp_path, negatives=1)
    roll_dir = _roll_dir(tmp_path)
    outcome = _stitch(work_dir, roll_dir)
    assert outcome.status == "complete"
    roll = load_roll_manifest(roll_dir)
    negative = roll.negatives[0]
    repo.append_edit(
        roll_dir, negative.negative_id, repo.ROTATE_OP, {"direction": "cw"}
    )
    capsys.readouterr()

    destination = tmp_path / "region.png"
    status = main(
        [
            "edit",
            "render-region",
            "--roll",
            str(roll_dir),
            "--negative",
            negative.negative_id,
            "--x",
            "2",
            "--y",
            "3",
            "--width",
            "8",
            "--height",
            "5",
            "--output",
            str(destination),
        ]
    )

    assert status == 0
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "region_rendered", "finished"]

    # One cw turn: display space is the TIFF rotated clockwise. The pixels
    # are what the cached preview shows at those display coordinates.
    tiff = tifffile.imread(roll_dir / negative.output["name"])
    display = np.ascontiguousarray(np.rot90(tiff, k=3))
    expected = cv2.cvtColor(
        NORMALIZED_DISPLAY_LUT[
            display[
                events[1]["y"] : events[1]["y"] + events[1]["height"],
                events[1]["x"] : events[1]["x"] + events[1]["width"],
            ]
        ],
        cv2.COLOR_RGB2BGR,
    )
    stored = cv2.imread(str(destination), cv2.IMREAD_UNCHANGED)
    np.testing.assert_array_equal(stored, expected)


def test_edit_render_region_rejects_a_bad_region(capsys, tmp_path):
    work_dir = _make_work_dir(tmp_path, negatives=1)
    roll_dir = _roll_dir(tmp_path)
    outcome = _stitch(work_dir, roll_dir)
    assert outcome.status == "complete"
    negative = load_roll_manifest(roll_dir).negatives[0]
    capsys.readouterr()

    destination = tmp_path / "region.png"
    status = main(
        [
            "edit",
            "render-region",
            "--roll",
            str(roll_dir),
            "--negative",
            negative.negative_id,
            "--x",
            "0",
            "--y",
            "0",
            "--width",
            "0",
            "--height",
            "6",
            "--output",
            str(destination),
        ]
    )

    assert status == 1
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "error", "finished"]
    assert events[1]["code"] == "INVALID_EDIT"
    assert not destination.exists()


def test_edit_render_region_negative_mode_encodes_without_inversion(capsys, tmp_path):
    """`--mode negative` renders the same display-space rect through the
    un-inverted density LUT — the published TIFF's own appearance, which
    the region route also honours."""
    import cv2
    import tifffile

    from scanny_boy.previews import NEGATIVE_DISPLAY_LUT, NORMALIZED_DISPLAY_LUT

    work_dir = _make_work_dir(tmp_path, negatives=1)
    roll_dir = _roll_dir(tmp_path)
    outcome = _stitch(work_dir, roll_dir)
    assert outcome.status == "complete"
    negative = load_roll_manifest(roll_dir).negatives[0]
    tiff = tifffile.imread(roll_dir / negative.output["name"])
    capsys.readouterr()

    destination = tmp_path / "region.png"
    status = main(
        [
            "edit",
            "render-region",
            "--roll",
            str(roll_dir),
            "--negative",
            negative.negative_id,
            "--x",
            "4",
            "--y",
            "2",
            "--width",
            "10",
            "--height",
            "6",
            "--output",
            str(destination),
            "--mode",
            "negative",
        ]
    )

    assert status == 0
    events, err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "region_rendered", "finished"]
    assert events[1]["path"] == str(destination)
    assert err == ""

    stored = cv2.imread(str(destination), cv2.IMREAD_UNCHANGED)
    negative_view = cv2.cvtColor(
        NEGATIVE_DISPLAY_LUT[tiff[2:8, 4:14]], cv2.COLOR_RGB2BGR
    )
    np.testing.assert_array_equal(stored, negative_view)
    # And it is genuinely the other view, not the default encode.
    positive_view = cv2.cvtColor(
        NORMALIZED_DISPLAY_LUT[tiff[2:8, 4:14]], cv2.COLOR_RGB2BGR
    )
    assert not np.array_equal(stored, positive_view)


def test_edit_render_region_unknown_mode_is_a_usage_error(capsys, tmp_path):
    work_dir = _make_work_dir(tmp_path, negatives=1)
    roll_dir = _roll_dir(tmp_path)
    outcome = _stitch(work_dir, roll_dir)
    assert outcome.status == "complete"
    negative = load_roll_manifest(roll_dir).negatives[0]
    capsys.readouterr()

    status = main(
        [
            "edit",
            "render-region",
            "--roll",
            str(roll_dir),
            "--negative",
            negative.negative_id,
            "--x",
            "0",
            "--y",
            "0",
            "--width",
            "4",
            "--height",
            "4",
            "--output",
            str(tmp_path / "region.png"),
            "--mode",
            "grayscale",
        ]
    )

    assert status == 2
    events, err = _stdout_events(capsys)
    assert events == []
    assert err != ""


def test_edit_render_preview_renders_the_underlying_negative(capsys, tmp_path):
    """`edit render-preview --mode negative` writes the whole display image
    — net transform folded in — through the un-inverted LUT, and
    `preview_rendered` carries the written PNG's pixel dimensions."""
    import cv2
    import tifffile

    from scanny_boy.library import repo
    from scanny_boy.previews import NEGATIVE_DISPLAY_LUT

    work_dir = _make_work_dir(tmp_path, negatives=1)
    roll_dir = _roll_dir(tmp_path)
    outcome = _stitch(work_dir, roll_dir)
    assert outcome.status == "complete"
    negative = load_roll_manifest(roll_dir).negatives[0]
    repo.append_edit(
        roll_dir, negative.negative_id, repo.ROTATE_OP, {"direction": "cw"}
    )
    capsys.readouterr()

    destination = tmp_path / "preview.png"
    status = main(
        [
            "edit",
            "render-preview",
            "--roll",
            str(roll_dir),
            "--negative",
            negative.negative_id,
            "--mode",
            "negative",
            "--output",
            str(destination),
        ]
    )

    assert status == 0
    events, err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "preview_rendered", "finished"]
    assert events[0]["command"] == "edit render-preview"
    assert events[1]["negative_id"] == negative.negative_id
    assert events[1]["path"] == str(destination)
    assert events[2]["status"] == "success"
    assert err == ""

    # One cw turn: the display is the TIFF rotated clockwise. The preview
    # is capped at PREVIEW_MAX_EDGE on its longest edge, in density space,
    # exactly as the managed preview's downscale — so replay that here.
    from scanny_boy.previews import PREVIEW_MAX_EDGE

    tiff = tifffile.imread(roll_dir / negative.output["name"])
    display = np.ascontiguousarray(np.rot90(tiff, k=3))
    edge = max(display.shape[0], display.shape[1])
    if edge > PREVIEW_MAX_EDGE:
        scale = PREVIEW_MAX_EDGE / edge
        display = cv2.resize(
            display,
            (round(display.shape[1] * scale), round(display.shape[0] * scale)),
            interpolation=cv2.INTER_AREA,
        )
    assert (events[1]["width"], events[1]["height"]) == (
        display.shape[1],
        display.shape[0],
    )
    stored = cv2.imread(str(destination), cv2.IMREAD_UNCHANGED)
    np.testing.assert_array_equal(
        stored, cv2.cvtColor(NEGATIVE_DISPLAY_LUT[display], cv2.COLOR_RGB2BGR)
    )


def test_edit_render_preview_negative_mode_ignores_the_tone(capsys, tmp_path):
    """The negative view is a density view: a recorded tone adjustment is
    composed into the positive render's display LUT and never reaches the
    negative render."""
    import cv2
    import tifffile

    from scanny_boy import tone

    work_dir = _make_work_dir(tmp_path, negatives=1)
    roll_dir = _roll_dir(tmp_path)
    outcome = _stitch(work_dir, roll_dir)
    assert outcome.status == "complete"
    negative = load_roll_manifest(roll_dir).negatives[0]
    capsys.readouterr()

    def _render(mode: str, name: str) -> Path:
        destination = tmp_path / name
        status = main(
            [
                "edit",
                "render-preview",
                "--roll",
                str(roll_dir),
                "--negative",
                negative.negative_id,
                "--mode",
                mode,
                "--output",
                str(destination),
            ]
        )
        assert status == 0
        capsys.readouterr()
        return destination

    negative_before = _render("negative", "negative-before.png").read_bytes()

    status = main(
        [
            "edit",
            "tone",
            "--roll",
            str(roll_dir),
            "--negative",
            negative.negative_id,
            "--grade",
            "160",
            "--snap",
            "0.3",
        ]
    )
    assert status == 0
    capsys.readouterr()

    # The negative view is byte-for-byte unaffected...
    assert _render("negative", "negative-after.png").read_bytes() == negative_before
    # ...while the positive render carries the tone curve: the graded LUT,
    # not the flat one, encodes the published TIFF — after the same
    # PREVIEW_MAX_EDGE density-space downscale the command applies.
    _render("positive", "positive-after.png")
    import numpy as np

    from scanny_boy.previews import PREVIEW_MAX_EDGE

    tiff = tifffile.imread(roll_dir / negative.output["name"])
    edge = max(tiff.shape[0], tiff.shape[1])
    if edge > PREVIEW_MAX_EDGE:
        scale = PREVIEW_MAX_EDGE / edge
        tiff = cv2.resize(
            tiff,
            (round(tiff.shape[1] * scale), round(tiff.shape[0] * scale)),
            interpolation=cv2.INTER_AREA,
        )
    graded = cv2.cvtColor(
        tone.build_display_lut(tone.ToneParams(grade_r=160.0, snap_gamma=0.3))[tiff],
        cv2.COLOR_RGB2BGR,
    )
    stored = cv2.imread(str(tmp_path / "positive-after.png"), cv2.IMREAD_UNCHANGED)
    np.testing.assert_array_equal(stored, graded)


def test_edit_render_preview_missing_roll_reports_roll_not_found(capsys, tmp_path):
    status = main(
        [
            "edit",
            "render-preview",
            "--roll",
            str(tmp_path / "nope"),
            "--negative",
            "x",
            "--output",
            str(tmp_path / "preview.png"),
        ]
    )

    assert status == 1
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "error", "finished"]
    assert events[0]["command"] == "edit render-preview"
    assert events[1]["code"] == "ROLL_NOT_FOUND"


def test_edit_flip_records_the_flip_and_refreshes_the_preview(capsys, tmp_path):
    work_dir = _make_work_dir(tmp_path, negatives=1)
    roll_dir = _roll_dir(tmp_path)
    outcome = _stitch(work_dir, roll_dir)
    assert outcome.status == "complete"
    negative_id = load_roll_manifest(roll_dir).negatives[0].negative_id
    capsys.readouterr()

    status = main(["edit", "flip", "--roll", str(roll_dir), "--negative", negative_id])

    assert status == 0
    events, err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "edit_recorded", "finished"]
    assert events[0]["command"] == "edit flip"
    assert events[1]["negative_id"] == negative_id
    assert events[1]["edit"]["op"] == "flip"
    assert events[1]["rotation_quarter_turns"] == 0
    assert events[1]["flipped_horizontally"] is True
    assert Path(events[1]["preview_path"]).exists()
    assert events[2]["status"] == "success"
    assert err == ""


def test_edit_rotate_accepts_a_selection(capsys, tmp_path):
    work_dir = _make_work_dir(tmp_path, negatives=2)
    roll_dir = _roll_dir(tmp_path)
    outcome = _stitch(work_dir, roll_dir)
    assert outcome.status == "complete"
    negative_ids = [n.negative_id for n in load_roll_manifest(roll_dir).negatives]
    capsys.readouterr()

    status = main(
        [
            "edit",
            "rotate",
            "--roll",
            str(roll_dir),
            "--negative",
            negative_ids[0],
            "--negative",
            negative_ids[1],
            "--direction",
            "cw",
        ]
    )

    assert status == 0
    events, err = _stdout_events(capsys)
    edit_recorded = [e for e in events if e["event"] == "edit_recorded"]
    assert [e["negative_id"] for e in edit_recorded] == negative_ids
    assert all(
        e["rotation_quarter_turns"] == 1 and e["flipped_horizontally"] is False
        for e in edit_recorded
    )
    assert events[-1]["status"] == "success"
    assert err == ""


def test_edit_flip_without_negative_id_returns_status_2(capsys):
    status = main(["edit", "flip", "--roll", "/tmp/roll"])

    assert status == 2
    events, err = _stdout_events(capsys)
    assert events == []
    assert err != ""


def test_edit_tone_records_the_adjustment_and_refreshes_the_preview(capsys, tmp_path):
    work_dir = _make_work_dir(tmp_path, negatives=1)
    roll_dir = _roll_dir(tmp_path)
    outcome = _stitch(work_dir, roll_dir)
    assert outcome.status == "complete"
    negative_id = load_roll_manifest(roll_dir).negatives[0].negative_id
    capsys.readouterr()

    status = main(
        [
            "edit",
            "tone",
            "--roll",
            str(roll_dir),
            "--negative",
            negative_id,
            "--grade",
            "90",
            "--snap",
            "0.2",
        ]
    )

    assert status == 0
    events, err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "edit_recorded", "finished"]
    assert events[0]["command"] == "edit tone"
    assert events[1]["edit"]["op"] == "tone"
    assert events[1]["edit"]["params"] == _tone_params(90.0, 0.2)
    assert Path(events[1]["preview_path"]).exists()
    assert events[2]["status"] == "success"
    assert err == ""


def test_edit_tone_reset_records_null_params(capsys, tmp_path):
    work_dir = _make_work_dir(tmp_path, negatives=1)
    roll_dir = _roll_dir(tmp_path)
    outcome = _stitch(work_dir, roll_dir)
    assert outcome.status == "complete"
    negative_id = load_roll_manifest(roll_dir).negatives[0].negative_id
    assert (
        main(
            [
                "edit",
                "tone",
                "--roll",
                str(roll_dir),
                "--negative",
                negative_id,
                "--grade",
                "90",
                "--snap",
                "0.2",
            ]
        )
        == 0
    )
    capsys.readouterr()

    status = main(
        ["edit", "tone", "--roll", str(roll_dir), "--negative", negative_id, "--reset"]
    )

    assert status == 0
    events, _err = _stdout_events(capsys)
    edit_recorded = events[1]
    assert edit_recorded["edit"]["params"] == _reset_tone_params()
    # The trailing tone op was updated in place: the log holds the single
    # coalesced op, not one per commit.
    edits = repo.edits_for(roll_dir, negative_id)
    assert len(edits) == 1
    assert edits[0]["op"] == "tone"
    assert edits[0]["params"] == _reset_tone_params()


def test_edit_tone_needs_grade_and_snap_together(capsys, tmp_path):
    work_dir = _make_work_dir(tmp_path, negatives=1)
    roll_dir = _roll_dir(tmp_path)
    outcome = _stitch(work_dir, roll_dir)
    assert outcome.status == "complete"
    negative_id = load_roll_manifest(roll_dir).negatives[0].negative_id
    capsys.readouterr()

    status = main(
        [
            "edit",
            "tone",
            "--roll",
            str(roll_dir),
            "--negative",
            negative_id,
            "--grade",
            "90",
        ]
    )

    assert status == 1
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "error", "finished"]
    assert events[1]["code"] == "INVALID_EDIT"


def test_edit_tone_round_trips_all_nine_flags_through_roll_info(capsys, tmp_path):
    work_dir = _make_work_dir(tmp_path, negatives=1)
    roll_dir = _roll_dir(tmp_path)
    outcome = _stitch(work_dir, roll_dir)
    assert outcome.status == "complete"
    negative_id = load_roll_manifest(roll_dir).negatives[0].negative_id
    capsys.readouterr()

    status = main(
        [
            "edit",
            "tone",
            "--roll",
            str(roll_dir),
            "--negative",
            negative_id,
            "--grade",
            "90",
            "--snap",
            "0.2",
            "--density",
            "1.2",
            "--shadow-density",
            "0.1",
            "--highlight-density",
            "-0.1",
            "--toe",
            "0.3",
            "--toe-width",
            "3.0",
            "--shoulder",
            "-0.2",
            "--shoulder-width",
            "4.0",
        ]
    )
    assert status == 0
    capsys.readouterr()

    status = main(["roll", "info", "--roll", str(roll_dir)])
    assert status == 0
    events, _err = _stdout_events(capsys)
    negative = events[1]["manifest"]["negatives"][0]
    assert negative["tone_grade_r"] == 90.0
    assert negative["tone_snap_gamma"] == 0.2
    assert negative["tone_density"] == 1.2
    assert negative["tone_shadow_density"] == 0.1
    assert negative["tone_highlight_density"] == -0.1
    assert negative["tone_toe"] == 0.3
    assert negative["tone_toe_width"] == 3.0
    assert negative["tone_shoulder"] == -0.2
    assert negative["tone_shoulder_width"] == 4.0


def _color_params(**overrides: float):
    import dataclasses

    from scanny_boy import color

    params = dataclasses.asdict(color.NEUTRAL_COLOR)
    params.update(overrides)
    return params


def test_edit_color_records_the_adjustment_and_refreshes_the_preview(capsys, tmp_path):
    work_dir = _make_work_dir(tmp_path, negatives=1)
    roll_dir = _roll_dir(tmp_path)
    outcome = _stitch(work_dir, roll_dir)
    assert outcome.status == "complete"
    negative_id = load_roll_manifest(roll_dir).negatives[0].negative_id
    capsys.readouterr()

    status = main(
        [
            "edit",
            "color",
            "--roll",
            str(roll_dir),
            "--negative",
            negative_id,
            "--cyan",
            "0.1",
            "--magenta",
            "0.2",
            "--yellow",
            "0.05",
            "--cast-removal",
            "0.1",
            "--dye-separation",
            "1.1",
        ]
    )

    assert status == 0
    events, err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "edit_recorded", "finished"]
    assert events[0]["command"] == "edit color"
    assert events[0]["protocol_version"] == PROTOCOL_VERSION
    assert events[1]["edit"]["op"] == "color"
    assert events[1]["edit"]["params"]["wb_cyan"] == pytest.approx(0.1)
    assert Path(events[1]["preview_path"]).exists()
    assert events[2]["status"] == "success"
    assert err == ""


def test_edit_color_partial_update_preserves_recorded_values(capsys, tmp_path):
    work_dir = _make_work_dir(tmp_path, negatives=1)
    roll_dir = _roll_dir(tmp_path)
    outcome = _stitch(work_dir, roll_dir)
    assert outcome.status == "complete"
    negative_id = load_roll_manifest(roll_dir).negatives[0].negative_id
    base = _color_params(wb_cyan=0.1, wb_magenta=0.2, cast_removal=0.3)
    assert (
        main(
            [
                "edit",
                "color",
                "--roll",
                str(roll_dir),
                "--negative",
                negative_id,
                "--cyan",
                str(base["wb_cyan"]),
                "--magenta",
                str(base["wb_magenta"]),
                "--yellow",
                str(base["wb_yellow"]),
                "--cast-removal",
                str(base["cast_removal"]),
            ]
        )
        == 0
    )
    capsys.readouterr()

    status = main(
        [
            "edit",
            "color",
            "--roll",
            str(roll_dir),
            "--negative",
            negative_id,
            "--cyan",
            "0.5",
        ]
    )
    assert status == 0
    events, _err = _stdout_events(capsys)
    params = events[1]["edit"]["params"]
    assert params["wb_cyan"] == pytest.approx(0.5)
    assert params["wb_magenta"] == pytest.approx(0.2)
    assert params["cast_removal"] == pytest.approx(0.3)


def test_edit_color_temperature_is_exclusive_with_region_magenta(capsys, tmp_path):
    work_dir = _make_work_dir(tmp_path, negatives=1)
    roll_dir = _roll_dir(tmp_path)
    outcome = _stitch(work_dir, roll_dir)
    assert outcome.status == "complete"
    negative_id = load_roll_manifest(roll_dir).negatives[0].negative_id
    capsys.readouterr()

    status = main(
        [
            "edit",
            "color",
            "--roll",
            str(roll_dir),
            "--negative",
            negative_id,
            "--temperature",
            "3200",
            "--magenta",
            "0.1",
        ]
    )

    assert status == 1
    events, _err = _stdout_events(capsys)
    assert events[1]["code"] == "INVALID_EDIT"


def test_edit_color_round_trips_through_roll_info(capsys, tmp_path):
    from scanny_boy import color

    work_dir = _make_work_dir(tmp_path, negatives=1)
    roll_dir = _roll_dir(tmp_path)
    outcome = _stitch(work_dir, roll_dir)
    assert outcome.status == "complete"
    negative_id = load_roll_manifest(roll_dir).negatives[0].negative_id
    params = _color_params(
        wb_cyan=0.1,
        wb_magenta=0.2,
        wb_yellow=0.05,
        shadow_cyan=0.01,
        cast_removal=0.15,
        dye_separation=1.1,
        separation_damping=0.2,
    )
    flag_for_key = {
        "wb_cyan": "--cyan",
        "wb_magenta": "--magenta",
        "wb_yellow": "--yellow",
        "shadow_cyan": "--shadow-cyan",
        "shadow_magenta": "--shadow-magenta",
        "shadow_yellow": "--shadow-yellow",
        "highlight_cyan": "--highlight-cyan",
        "highlight_magenta": "--highlight-magenta",
        "highlight_yellow": "--highlight-yellow",
        "cast_removal": "--cast-removal",
        "cast_removal_highlights": "--cast-removal-highlights",
        "dye_separation": "--dye-separation",
        "separation_damping": "--separation-damping",
    }
    argv = [
        "edit",
        "color",
        "--roll",
        str(roll_dir),
        "--negative",
        negative_id,
    ]
    for key, value in params.items():
        argv.extend([flag_for_key[key], str(value)])
    assert main(argv) == 0
    capsys.readouterr()

    status = main(["roll", "info", "--roll", str(roll_dir)])
    assert status == 0
    events, _err = _stdout_events(capsys)
    negative = events[1]["manifest"]["negatives"][0]
    for key in color.COLOR_PARAM_KEYS:
        assert negative[f"color_{key}"] == pytest.approx(params[key])
    assert negative["color_temperature"] == pytest.approx(
        color.wb_to_kelvin(params["wb_magenta"], params["wb_yellow"]), rel=0.02
    )


def test_edit_tone_auto_density_records_a_solved_value(capsys, tmp_path):
    work_dir = _make_work_dir(tmp_path, negatives=1)
    roll_dir = _roll_dir(tmp_path)
    outcome = _stitch(work_dir, roll_dir)
    assert outcome.status == "complete"
    negative_id = load_roll_manifest(roll_dir).negatives[0].negative_id
    capsys.readouterr()

    status = main(
        [
            "edit",
            "tone",
            "--roll",
            str(roll_dir),
            "--negative",
            negative_id,
            "--auto-grade",
            "--auto-density",
            "--snap",
            "0",
        ]
    )
    assert status == 0
    events, _err = _stdout_events(capsys)
    density = events[1]["edit"]["params"]["density"]
    grade = events[1]["edit"]["params"]["grade_r"]
    assert density != 1.0
    assert grade != 115.0


def test_edit_tone_auto_on_missing_normalization_warns(capsys, tmp_path):
    work_dir = _make_work_dir(tmp_path, negatives=1)
    roll_dir = _roll_dir(tmp_path)
    outcome = _stitch(work_dir, roll_dir)
    assert outcome.status == "complete"
    roll = load_roll_manifest(roll_dir)
    negative_id = roll.negatives[0].negative_id
    roll.negatives[0].normalization = None
    write_roll_manifest(roll_dir, roll)
    capsys.readouterr()

    status = main(
        [
            "edit",
            "tone",
            "--roll",
            str(roll_dir),
            "--negative",
            negative_id,
            "--auto-density",
            "--auto-grade",
            "--snap",
            "0",
        ]
    )
    assert status == 0
    events, _err = _stdout_events(capsys)
    warnings = [event for event in events if event["event"] == "warning"]
    assert len(warnings) == 2
    assert all(event["code"] == "TONE_METERING_UNAVAILABLE" for event in warnings)


def test_edit_tone_rejects_density_with_auto_density(capsys, tmp_path):
    work_dir = _make_work_dir(tmp_path, negatives=1)
    roll_dir = _roll_dir(tmp_path)
    outcome = _stitch(work_dir, roll_dir)
    assert outcome.status == "complete"
    negative_id = load_roll_manifest(roll_dir).negatives[0].negative_id
    capsys.readouterr()

    status = main(
        [
            "edit",
            "tone",
            "--roll",
            str(roll_dir),
            "--negative",
            negative_id,
            "--density",
            "1.2",
            "--auto-density",
            "--grade",
            "115",
            "--snap",
            "0",
        ]
    )
    assert status == 2


def test_edit_tone_toe_only_change_regenerates_the_preview(capsys, tmp_path):
    work_dir = _make_work_dir(tmp_path, negatives=1)
    roll_dir = _roll_dir(tmp_path)
    outcome = _stitch(work_dir, roll_dir)
    assert outcome.status == "complete"
    negative_id = load_roll_manifest(roll_dir).negatives[0].negative_id
    capsys.readouterr()

    assert (
        main(
            [
                "edit",
                "tone",
                "--roll",
                str(roll_dir),
                "--negative",
                negative_id,
                "--grade",
                "115",
                "--snap",
                "0",
            ]
        )
        == 0
    )
    events, _ = _stdout_events(capsys)
    base_preview = Path(events[1]["preview_path"]).read_bytes()

    assert (
        main(
            [
                "edit",
                "tone",
                "--roll",
                str(roll_dir),
                "--negative",
                negative_id,
                "--grade",
                "115",
                "--snap",
                "0",
                "--toe",
                "0.5",
            ]
        )
        == 0
    )
    events, _ = _stdout_events(capsys)
    toe_preview = Path(events[1]["preview_path"]).read_bytes()
    assert toe_preview != base_preview


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


def _spawn_convert(
    input_dir: Path, out_dir: Path, files: list[str], **extra
) -> subprocess.Popen:
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

    # The forced stop left exactly the wreckage section 3.8 predicts. With
    # jobs=2 the run-wide pool stages every group up front, so one staging
    # directory per negative of the abandoned run remains.
    abandoned = load_manifest(out_dir)
    assert abandoned.status == "running"
    assert abandoned.finished_at is None
    staging = [p for p in out_dir.iterdir() if p.name.endswith(STAGING_SUFFIX)]
    assert len(staging) == len(abandoned.groups)

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
    manifest = new_roll_manifest(roll_id="rid-1", roll_name="Roll", film_kind="colour")
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


def _save_grid_profile(name: str, *, across: int = 4, down: int = 2) -> str:
    from scanny_boy.grid_profile import new_grid_profile
    from scanny_boy.library import repo

    profile = new_grid_profile(name=name, across=across, down=down)
    repo.save_grid_profile(profile)
    return profile.profile_id


def test_grid_list_reports_an_empty_library(capsys):
    status = main(["grid", "list"])

    assert status == 0
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "grid_list", "finished"]
    assert events[0]["command"] == "grid list"
    assert events[1]["profiles"] == []


def test_grid_create_persists_a_preset(capsys):
    status = main(["grid", "create", "--name", "Hasselblad", "--across", "4", "--down", "2"])

    assert status == 0
    events, _err = _stdout_events(capsys)
    assert events[1]["event"] == "grid_created"
    profile = events[1]["profile"]
    assert profile["name"] == "Hasselblad"
    assert profile["across"] == 4
    assert profile["down"] == 2

    from scanny_boy.library import repo

    loaded = repo.list_grid_profiles()
    assert len(loaded) == 1
    assert loaded[0].name == "Hasselblad"
    assert loaded[0].across == 4
    assert loaded[0].down == 2


def test_grid_create_rejects_a_taken_name(capsys):
    _save_grid_profile("Hasselblad")

    status = main(["grid", "create", "--name", "Hasselblad", "--across", "3", "--down", "1"])

    assert status == 1
    events, _err = _stdout_events(capsys)
    assert events[1]["code"] == "GRID_PROFILE_EXISTS"


def test_grid_create_rejects_an_invalid_shape(capsys):
    status = main(["grid", "create", "--name", "Too big", "--across", "4", "--down", "4"])

    assert status == 1
    events, _err = _stdout_events(capsys)
    assert events[1]["code"] == "INVALID_GRID"


def test_grid_delete_unknown_profile_is_not_found(capsys):
    status = main(["grid", "delete", "--profile", "nope"])

    assert status == 1
    events, _err = _stdout_events(capsys)
    assert events[1]["code"] == "GRID_PROFILE_NOT_FOUND"


def test_grid_delete_removes_the_row(capsys):
    _save_grid_profile("Hasselblad")
    from scanny_boy.library import repo

    profile = repo.load_grid_profile_by_name("Hasselblad")

    status = main(["grid", "delete", "--profile", profile.profile_id])

    assert status == 0
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "grid_deleted", "finished"]
    assert events[1]["profile_id"] == profile.profile_id
    assert repo.list_grid_profiles() == []


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


# --- --grid (docs/GRID_STITCH_PLAN.md sections 2.2 and 2.7) ----------------


@requires_real_samples
def test_probe_with_grid_emits_the_implied_count_groups(capsys, tmp_path):
    """`--grid 3x2` groups by the implied count `across * down`, exactly as
    `--per-negative 6` would."""
    input_dir = stage_samples(tmp_path, list(REAL_SAMPLE_FILES))
    status = main(
        [
            "probe",
            "--input",
            str(input_dir),
            "--files",
            *REAL_SAMPLE_FILES,
            "--grid",
            "3x2",
        ]
    )

    assert status == 0
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "probe_result", "finished"]
    assert events[1]["groups"] == [
        [
            "_DSC4638.NEF",
            "_DSC4639.NEF",
            "_DSC4640.NEF",
            "_DSC4644.NEF",
            "_DSC4645.NEF",
            "_DSC4646.NEF",
        ],
    ]


def test_grid_and_per_negative_are_mutually_exclusive(capsys):
    status = main(
        [
            "prepare",
            "--input",
            "/tmp/in",
            "--files",
            "a.NEF",
            "--out",
            "/tmp/out",
            "--grid",
            "3x2",
            "--per-negative",
            "6",
        ]
    )
    assert status == 2
    events, err = _stdout_events(capsys)
    assert events == []
    assert "mutually exclusive" in err


@pytest.mark.parametrize("command", ["prepare", "run"])
def test_omitting_both_grid_and_per_negative_is_a_usage_error(capsys, command):
    """The changed failure mode: argparse used to demand --per-negative by
    name; now the exactly-one-of check names both flags."""
    argv = [command, "--input", "/tmp/in", "--files", "a.NEF"]
    if command == "prepare":
        argv += ["--out", "/tmp/out"]
    else:
        argv += ["--roll", "/tmp/roll"]
    status = main(argv)
    assert status == 2
    events, err = _stdout_events(capsys)
    assert events == []
    assert "--grid" in err and "--per-negative" in err


def test_malformed_grid_is_a_usage_error(capsys):
    status = main(
        [
            "prepare",
            "--input",
            "/tmp/in",
            "--files",
            "a.NEF",
            "--out",
            "/tmp/out",
            "--grid",
            "3z2",
        ]
    )
    assert status == 2
    events, err = _stdout_events(capsys)
    assert events == []
    assert "AxD" in err


def test_grid_3x3_is_invalid_grid_with_the_rebate_rule(capsys):
    status = main(
        [
            "prepare",
            "--input",
            "/tmp/in",
            "--files",
            "a.NEF",
            "--out",
            "/tmp/out",
            "--grid",
            "3x3",
        ]
    )
    assert status == 2
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["error", "finished"]
    assert events[0]["code"] == "INVALID_GRID"
    assert "rebate" in events[0]["message"]


def test_grid_above_the_count_cap_is_invalid_grid(capsys):
    status = main(
        [
            "prepare",
            "--input",
            "/tmp/in",
            "--files",
            "a.NEF",
            "--out",
            "/tmp/out",
            "--grid",
            "13x1",
        ]
    )
    assert status == 2
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["error", "finished"]
    assert events[0]["code"] == "INVALID_GRID"
    assert "12" in events[0]["message"]


def test_grid_2x5_is_a_legal_shape(capsys, tmp_path):
    """`2x5` is legal and CLI-only (the UI caps Down at 2): it must pass
    `validate_grid` and fail later, on the catalogue, rather than with
    INVALID_GRID."""
    status = main(["probe", "--input", str(tmp_path), "--grid", "2x5"])
    assert status == 1
    events, _err = _stdout_events(capsys)
    assert events[1]["code"] == "NO_FILES"


# --- the spotting commands (docs/SPOTTING_PLAN.md §7) --------------------------


def _spots_roll(capsys, tmp_path):
    """A registered roll with one completed negative (a small published
    TIFF), built the way `roll init` plus a stitch leaves one — without
    paying for the stitch."""
    import tifffile

    from scanny_boy.roll_manifest import CaptureTime
    from scanny_boy.roll_manifest_test import _negative, _run

    main(
        [
            "roll",
            "init",
            "--library",
            str(tmp_path),
            "--name",
            "Spots",
            "--film-kind",
            "colour",
        ]
    )
    capsys.readouterr()
    roll_dir = tmp_path / "Spots"
    manifest = load_roll_manifest(roll_dir)
    from scanny_boy.manifest import SourceRecord
    from scanny_boy.roll_manifest import append_run, merge_sources

    append_run(manifest, _run(run_id="stitch-run", short_id="stitch"))
    merge_sources(
        manifest,
        [SourceRecord(filename="a.NEF", absolute_path="/x", size=1, mtime=1.0, sha256="a" * 64)],
        "stitch-run",
    )
    manifest.negatives.append(
        _negative(
            negative_id="spots-negative-01",
            run_id="stitch-run",
            status="completed",
            sequence=1,
            capture_time=CaptureTime(source_datetime_original="2026-08-01T12:00:00"),
            output={
                "name": "_DSC0001.tif",
                "size": 0,
                "sha256": "0" * 64,
                "width": 64,
                "height": 48,
            },
        )
    )
    write_roll_manifest(roll_dir, manifest)
    tifffile.imwrite(roll_dir / "_DSC0001.tif", np.full((48, 64, 3), 30000, dtype=np.uint16))
    return roll_dir, "spots-negative-01"


def _hand_spots_op(repair=False):
    from scanny_boy import spots

    return spots.spots_params(
        canvas=(64, 48),
        spots=[
            {
                "id": 1,
                "kind": "blob",
                "polarity": "dense",
                "bbox": [10, 12, 4, 3],
                "rle": [0, 4, 0, 4, 0, 4],
                "area": 12,
                "score": 9.0,
                "rejected": False,
            },
            {
                "id": 2,
                "kind": "streak",
                "polarity": "thin",
                "bbox": [40, 30, 12, 3],
                "rle": [0, 3, 9, 0, 3, 9, 0, 3, 9],
                "area": 24,
                "score": 8.0,
                "rejected": False,
            },
        ],
        sensitivity=0.5,
        repair=repair,
    )


def test_edit_detect_spots_records_and_reports(capsys, tmp_path):
    roll_dir, negative_id = _spots_roll(capsys, tmp_path)

    status = main(
        [
            "edit", "detect-spots",
            "--roll", str(roll_dir),
            "--negative", negative_id,
            "--sensitivity", "0.5",
        ]
    )

    assert status == 0
    events, err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "spots_reported", "finished"]
    assert events[0]["command"] == "edit detect-spots"
    reported = events[1]
    assert reported["negative_id"] == negative_id
    assert reported["detector_version"] == 1
    assert reported["sensitivity"] == 0.5
    assert reported["repair"] is False
    assert reported["preview_path"] is not None
    for spot in reported["spots"]:
        assert "rle" not in spot
        assert set(spot) == {"id", "kind", "polarity", "rect", "score", "rejected"}
    assert events[2]["status"] == "success"
    assert err == ""


def test_edit_spots_reject_accept_and_repair_flags(capsys, tmp_path):
    from scanny_boy.library import repo

    roll_dir, negative_id = _spots_roll(capsys, tmp_path)
    repo.append_spots_edit(roll_dir, negative_id, _hand_spots_op())
    capsys.readouterr()

    status = main(
        [
            "edit", "spots",
            "--roll", str(roll_dir),
            "--negative", negative_id,
            "--reject", "1",
            "--reject", "2",
        ]
    )

    assert status == 0
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "spots_reported", "finished"]
    reported = events[1]
    assert [spot["rejected"] for spot in reported["spots"]] == [True, True]
    assert reported["repair"] is False

    capsys.readouterr()
    status = main(
        [
            "edit", "spots",
            "--roll", str(roll_dir),
            "--negative", negative_id,
            "--accept", "2",
            "--repair",
        ]
    )
    assert status == 0
    events, _err = _stdout_events(capsys)
    reported = events[1]
    assert [spot["rejected"] for spot in reported["spots"]] == [True, False]
    assert reported["repair"] is True


def test_edit_list_spots_is_a_pure_query(capsys, tmp_path):
    from scanny_boy.library import repo

    roll_dir, negative_id = _spots_roll(capsys, tmp_path)
    repo.append_spots_edit(roll_dir, negative_id, _hand_spots_op(repair=True))
    capsys.readouterr()
    before = repo.edits_for(roll_dir, negative_id)

    status = main(
        ["edit", "list-spots", "--roll", str(roll_dir), "--negative", negative_id]
    )

    assert status == 0
    events, err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "spots_reported", "finished"]
    reported = events[1]
    assert reported["repair"] is True
    assert reported["preview_path"] is None
    assert len(reported["spots"]) == 2
    assert repo.edits_for(roll_dir, negative_id) == before
    assert err == ""


def test_edit_list_spots_on_a_stale_set_warns(capsys, tmp_path):
    from scanny_boy import spots
    from scanny_boy.library import repo

    roll_dir, negative_id = _spots_roll(capsys, tmp_path)
    stale = spots.spots_params(
        canvas=(100, 100),
        spots=[
            {
                "id": 1,
                "kind": "blob",
                "polarity": "dense",
                "bbox": [10, 12, 4, 3],
                "rle": [0, 4, 0, 4, 0, 4],
                "area": 12,
                "score": 9.0,
                "rejected": False,
            }
        ],
        sensitivity=0.5,
        repair=True,
    )
    repo.append_spots_edit(roll_dir, negative_id, stale)
    capsys.readouterr()

    status = main(
        ["edit", "list-spots", "--roll", str(roll_dir), "--negative", negative_id]
    )

    assert status == 0
    events, err = _stdout_events(capsys)
    assert [e["event"] for e in events] == [
        "started", "warning", "spots_reported", "finished"
    ]
    assert events[1]["code"] == "SPOTS_STALE"
    assert events[2]["spots"] == []
    assert err == ""


def test_roll_info_carries_the_spots_summary(capsys, tmp_path):
    from scanny_boy.library import repo

    roll_dir, negative_id = _spots_roll(capsys, tmp_path)

    main(["roll", "info", "--roll", str(roll_dir)])
    events, _err = _stdout_events(capsys)
    negative = events[1]["manifest"]["negatives"][0]
    assert negative["spots"] is None

    repo.append_spots_edit(roll_dir, negative_id, _hand_spots_op(repair=True))
    capsys.readouterr()
    main(["roll", "info", "--roll", str(roll_dir)])
    events, _err = _stdout_events(capsys)
    negative = events[1]["manifest"]["negatives"][0]
    assert negative["spots"] == {
        "detector_version": 1,
        "sensitivity": 0.5,
        "repair": True,
        "stale": False,
        "count": 2,
        "rejected": 0,
    }

    stale = _hand_spots_op()
    stale["canvas"] = [100, 100]
    repo.append_spots_edit(roll_dir, negative_id, stale)
    capsys.readouterr()
    main(["roll", "info", "--roll", str(roll_dir)])
    events, _err = _stdout_events(capsys)
    negative = events[1]["manifest"]["negatives"][0]
    assert negative["spots"]["stale"] is True
    assert negative["spots"]["count"] == 0
    assert negative["spots"]["rejected"] == 0


# --- --cast-removal-highlights and --auto-cast (docs/CAST_REMOVAL_PLAN.md R-3)


def test_cast_removal_highlights_round_trips_through_roll_info(capsys, tmp_path):
    work_dir = _make_work_dir(tmp_path, negatives=1)
    roll_dir = _roll_dir(tmp_path)
    outcome = _stitch(work_dir, roll_dir)
    assert outcome.status == "complete"
    negative_id = load_roll_manifest(roll_dir).negatives[0].negative_id
    capsys.readouterr()

    status = main(
        [
            "edit",
            "color",
            "--roll",
            str(roll_dir),
            "--negative",
            negative_id,
            "--cast-removal-highlights",
            "0.4",
        ]
    )
    assert status == 0
    capsys.readouterr()

    status = main(["roll", "info", "--roll", str(roll_dir)])
    assert status == 0
    events, _err = _stdout_events(capsys)
    negative = events[1]["manifest"]["negatives"][0]
    assert negative["color_cast_removal_highlights"] == pytest.approx(0.4)
    # A single flag leaves the other twelve at their recorded values.
    assert negative["color_wb_cyan"] == pytest.approx(0.0)
    assert negative["color_cast_removal"] == pytest.approx(0.0)
    assert negative["color_dye_separation"] == pytest.approx(1.0)


def test_auto_cast_writes_nulling_filtration(capsys, tmp_path, monkeypatch):
    work_dir = _make_work_dir(tmp_path, negatives=1)
    roll_dir = _roll_dir(tmp_path)
    outcome = _stitch(work_dir, roll_dir)
    assert outcome.status == "complete"
    negative_id = load_roll_manifest(roll_dir).negatives[0].negative_id
    # Record a neutral residual for the negative to solve from.
    roll = load_roll_manifest(roll_dir)
    roll.negatives[0].normalization["neutral_residual"] = [0.06, -0.03]
    write_roll_manifest(roll_dir, roll)
    capsys.readouterr()

    status = main(
        [
            "edit",
            "color",
            "--roll",
            str(roll_dir),
            "--negative",
            negative_id,
            "--auto-cast",
        ]
    )

    assert status == 0
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == ["started", "edit_recorded", "finished"]
    params = events[1]["edit"]["params"]
    # The three CMY values null the residual: with unit ranges,
    # o_R - o_G = -a and o_B - o_G = -b.
    from scanny_boy import color as color_mod

    metering = color_mod.read_metering(
        load_roll_manifest(roll_dir).negatives[0].normalization
    )
    offsets = color_mod.cmy_offsets(
        color_mod.ColorParams(
            wb_cyan=params["wb_cyan"],
            wb_magenta=params["wb_magenta"],
            wb_yellow=params["wb_yellow"],
        ),
        metering,
    )
    assert offsets[0] - offsets[1] == pytest.approx(-0.06, abs=1e-6)
    assert offsets[2] - offsets[1] == pytest.approx(0.03, abs=1e-6)


def test_auto_cast_without_a_residual_warns_and_records_unchanged(
    capsys, tmp_path
):
    work_dir = _make_work_dir(tmp_path, negatives=1)
    roll_dir = _roll_dir(tmp_path)
    outcome = _stitch(work_dir, roll_dir)
    assert outcome.status == "complete"
    negative_id = load_roll_manifest(roll_dir).negatives[0].negative_id
    capsys.readouterr()

    status = main(
        [
            "edit",
            "color",
            "--roll",
            str(roll_dir),
            "--negative",
            negative_id,
            "--auto-cast",
        ]
    )

    assert status == 0
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == [
        "started",
        "warning",
        "edit_recorded",
        "finished",
    ]
    assert events[1]["code"] == "TONE_METERING_UNAVAILABLE"
    assert "no neutral estimate" in events[1]["message"]
    params = events[2]["edit"]["params"]
    assert params["wb_cyan"] == pytest.approx(0.0)
    assert params["cast_removal"] == pytest.approx(0.0)


def test_auto_cast_is_exclusive_with_reset_and_global_sliders(capsys, tmp_path):
    work_dir = _make_work_dir(tmp_path, negatives=1)
    roll_dir = _roll_dir(tmp_path)
    outcome = _stitch(work_dir, roll_dir)
    assert outcome.status == "complete"
    negative_id = load_roll_manifest(roll_dir).negatives[0].negative_id
    capsys.readouterr()

    for extra in (["--reset"], ["--cyan", "0.1"], ["--magenta", "0.1"], ["--yellow", "0.1"]):
        status = main(
            [
                "edit",
                "color",
                "--roll",
                str(roll_dir),
                "--negative",
                negative_id,
                "--auto-cast",
                *extra,
            ]
        )
        assert status == 1
        events, _err = _stdout_events(capsys)
        assert events[1]["code"] == "INVALID_EDIT"
        capsys.readouterr()


def test_auto_cast_result_is_independent_of_cast_removal_in_the_one_point_branch(
    capsys, tmp_path
):
    """§7.3's tie-compensation test at the CLI level: with the highlight
    strength at rest, the solved CMY does not move when a shadow tie is
    already recorded."""
    work_dir = _make_work_dir(tmp_path, negatives=1)
    roll_dir = _roll_dir(tmp_path)
    outcome = _stitch(work_dir, roll_dir)
    assert outcome.status == "complete"
    negative_id = load_roll_manifest(roll_dir).negatives[0].negative_id
    roll = load_roll_manifest(roll_dir)
    roll.negatives[0].normalization["neutral_residual"] = [0.06, -0.03]
    write_roll_manifest(roll_dir, roll)
    capsys.readouterr()

    main(
        ["edit", "color", "--roll", str(roll_dir), "--negative", negative_id,
         "--auto-cast"]
    )
    first = _stdout_events(capsys)[0][1]["edit"]["params"]
    capsys.readouterr()
    main(
        ["edit", "color", "--roll", str(roll_dir), "--negative", negative_id,
         "--cast-removal", "0.8", "--auto-cast"]
    )
    second = _stdout_events(capsys)[0][1]["edit"]["params"]

    assert second["wb_cyan"] == pytest.approx(first["wb_cyan"], abs=1e-9)
    assert second["wb_magenta"] == pytest.approx(first["wb_magenta"], abs=1e-9)
    assert second["wb_yellow"] == pytest.approx(first["wb_yellow"], abs=1e-9)


def test_cast_removal_highlights_warns_without_a_highlight_reference(
    capsys, tmp_path
):
    work_dir = _make_work_dir(tmp_path, negatives=1)
    roll_dir = _roll_dir(tmp_path)
    outcome = _stitch(work_dir, roll_dir)
    assert outcome.status == "complete"
    negative_id = load_roll_manifest(roll_dir).negatives[0].negative_id
    # R-1's stitch may well have measured a usable highlight reference;
    # this test needs the fallback signal.
    roll = load_roll_manifest(roll_dir)
    roll.negatives[0].normalization["highlight_refs"] = None
    write_roll_manifest(roll_dir, roll)
    capsys.readouterr()

    status = main(
        [
            "edit",
            "color",
            "--roll",
            str(roll_dir),
            "--negative",
            negative_id,
            "--cast-removal-highlights",
            "0.5",
        ]
    )

    assert status == 0
    events, _err = _stdout_events(capsys)
    assert [e["event"] for e in events] == [
        "started",
        "warning",
        "edit_recorded",
        "finished",
    ]
    assert events[1]["code"] == "TONE_METERING_UNAVAILABLE"
    params = events[2]["edit"]["params"]
    assert params["cast_removal_highlights"] == pytest.approx(0.5)