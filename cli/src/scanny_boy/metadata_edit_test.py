"""Tests for `metadata set` and `metadata values`: the extended-metadata
payloads land in the library database, capture-date changes recompute the
intended timestamps by the rank formula, the catalog remembers canonical
values (caption never), and the whole payload is validated before anything
is written."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scanny_boy.cli import main
from scanny_boy.events import Code
from scanny_boy.library import repo
from scanny_boy.metadata_edit import (
    MetadataEditFailure,
    run_metadata_set,
    run_metadata_values,
)
from scanny_boy.roll_manifest import (
    CaptureTime,
    effective_metadata,
    load_roll_manifest,
)
from scanny_boy.roll_manifest_test import _negative, _run
from scanny_boy.roll_manifest_schema_test_support import (
    assert_matches_roll_manifest_schema,
    load_roll_manifest_schema,
)
from scanny_boy.schema_test_support import assert_matches_schema, load_schema
from scanny_boy.work_dir_support import make_roll_dir

# The stocks migration 0020 seeds into the `film` catalog, in listed order.
FILM_STOCK_SEEDS = [
    ("Kodak Ultramax 400", "colour"),
    ("Kodak Gold 200", "colour"),
    ("Kodak Portra 160", "colour"),
    ("Kodak Portra 400", "colour"),
    ("Kodak Tri-X 400", "monochrome"),
    ("Kodak TMax 400", "monochrome"),
    ("Kodak TMax P3200", "monochrome"),
    ("Ilford HP5+", "monochrome"),
    ("Ilford FP4+", "monochrome"),
    ("Ilford Delta 3200", "monochrome"),
]

_NEGATIVE_A = "stitch-negative-01"
_NEGATIVE_B = "stitch-negative-02"


@pytest.fixture()
def two_negative_roll(tmp_path: Path) -> Path:
    roll_dir = make_roll_dir(tmp_path)
    manifest = load_roll_manifest(roll_dir)
    manifest.metadata.roll_capture_date = "2026-08-01"
    from scanny_boy.roll_manifest import append_run

    append_run(manifest, _run(run_id="stitch-run", short_id="stitch"))
    for index, negative_id in enumerate((_NEGATIVE_A, _NEGATIVE_B), start=1):
        negative = _negative(
            negative_id=negative_id,
            run_id="stitch-run",
            status="completed",
            sequence=index,
            capture_time=CaptureTime(
                source_datetime_original=f"2026-08-01T10:00:0{index}"
            ),
        )
        manifest.negatives.append(negative)
    from scanny_boy.roll_manifest import write_roll_manifest

    write_roll_manifest(roll_dir, manifest)
    return roll_dir


# --- roll-level fallbacks and per-negative overrides ------------------------


def test_roll_fields_are_the_fallback(two_negative_roll: Path):
    run_metadata_set(
        two_negative_roll,
        {"roll": {"city": "Porto", "camera": "Nikon F3"}},
    )
    manifest = load_roll_manifest(two_negative_roll)
    negative = manifest.negative(_NEGATIVE_A)
    effective = effective_metadata(manifest.metadata, negative.metadata)
    assert effective["city"] == "Porto"
    assert effective["camera"] == "Nikon F3"
    assert effective["caption"] is None


def test_negative_value_wins_over_roll(two_negative_roll: Path):
    run_metadata_set(
        two_negative_roll,
        {
            "roll": {"state": "Oregon"},
            "negatives": {_NEGATIVE_B: {"state": "Maine"}},
        },
    )
    manifest = load_roll_manifest(two_negative_roll)
    effective_a = effective_metadata(
        manifest.metadata, manifest.negative(_NEGATIVE_A).metadata
    )
    effective_b = effective_metadata(
        manifest.metadata, manifest.negative(_NEGATIVE_B).metadata
    )
    assert effective_a["state"] == "Oregon"
    assert effective_b["state"] == "Maine"


@pytest.mark.parametrize("field", ["camera", "film", "iso"])
def test_roll_only_fields_are_rejected_on_a_negative(
    two_negative_roll: Path, field: str
):
    with pytest.raises(MetadataEditFailure) as excinfo:
        run_metadata_set(
            two_negative_roll,
            {
                "roll": {"city": "Porto"},
                "negatives": {_NEGATIVE_A: {field: "x"}},
            },
        )
    assert excinfo.value.code == Code.INVALID_METADATA
    assert field in excinfo.value.message
    # Nothing was written: the roll-level edit in the same payload did not land.
    assert load_roll_manifest(two_negative_roll).metadata.city is None


def test_the_rolls_camera_is_every_negatives_effective_camera(
    two_negative_roll: Path,
):
    run_metadata_set(two_negative_roll, {"roll": {"camera": "Nikon F3"}})
    manifest = load_roll_manifest(two_negative_roll)
    for negative in manifest.negatives:
        effective = effective_metadata(manifest.metadata, negative.metadata)
        assert effective["camera"] == "Nikon F3"


def test_clearing_a_negative_value_restores_the_fallback(two_negative_roll: Path):
    run_metadata_set(
        two_negative_roll,
        {
            "roll": {"lens": "50mm f/1.4"},
            "negatives": {_NEGATIVE_A: {"lens": "35mm f/2"}},
        },
    )
    run_metadata_set(
        two_negative_roll,
        {"negatives": {_NEGATIVE_A: {"lens": None}}},
    )
    manifest = load_roll_manifest(two_negative_roll)
    effective = effective_metadata(
        manifest.metadata, manifest.negative(_NEGATIVE_A).metadata
    )
    assert effective["lens"] == "50mm f/1.4"

    # An empty string clears too: the app commits whatever a blurred,
    # emptied text field holds.
    run_metadata_set(
        two_negative_roll,
        {"negatives": {_NEGATIVE_B: {"lens": "  "}}},
    )
    run_metadata_set(
        two_negative_roll,
        {"negatives": {_NEGATIVE_B: {"lens": ""}}},
    )
    manifest = load_roll_manifest(two_negative_roll)
    effective = effective_metadata(
        manifest.metadata, manifest.negative(_NEGATIVE_B).metadata
    )
    assert effective["lens"] == "50mm f/1.4"


def test_values_are_stripped(two_negative_roll: Path):
    run_metadata_set(two_negative_roll, {"roll": {"city": "  Porto  "}})
    manifest = load_roll_manifest(two_negative_roll)
    assert manifest.metadata.city == "Porto"


# --- capture dates and the rank formula -------------------------------------


def test_roll_capture_date_recomputes_intended_times(two_negative_roll: Path):
    manifest = load_roll_manifest(two_negative_roll)
    assert manifest.metadata.roll_capture_date == "2026-08-01"
    # The fixture never stamped intent; the set does, even with no field
    # change, because intent is pure derived state.
    run_metadata_set(two_negative_roll, {"roll": {"caption": "a note"}})
    manifest = load_roll_manifest(two_negative_roll)
    intended_a = manifest.negative(_NEGATIVE_A).capture_time.intended_datetime_original
    intended_b = manifest.negative(_NEGATIVE_B).capture_time.intended_datetime_original
    assert intended_a == "2026-08-01T12:00:00"
    assert intended_b == "2026-08-01T12:00:01"


def test_date_override_ranks_within_its_own_date(two_negative_roll: Path):
    run_metadata_set(
        two_negative_roll,
        {"negatives": {_NEGATIVE_B: {"capture_date": "2026-08-03"}}},
    )
    manifest = load_roll_manifest(two_negative_roll)
    assert manifest.negative(_NEGATIVE_B).capture_time.date_override == "2026-08-03"
    assert (
        manifest.negative(_NEGATIVE_A).capture_time.intended_datetime_original
        == "2026-08-01T12:00:00"
    )
    assert (
        manifest.negative(_NEGATIVE_B).capture_time.intended_datetime_original
        == "2026-08-03T12:00:00"
    )


def test_clearing_the_override_falls_back_to_the_roll_date(two_negative_roll: Path):
    run_metadata_set(
        two_negative_roll,
        {"negatives": {_NEGATIVE_A: {"capture_date": "2026-08-05"}}},
    )
    run_metadata_set(
        two_negative_roll,
        {"negatives": {_NEGATIVE_A: {"capture_date": None}}},
    )
    manifest = load_roll_manifest(two_negative_roll)
    assert manifest.negative(_NEGATIVE_A).capture_time.date_override is None
    assert (
        manifest.negative(_NEGATIVE_A).capture_time.intended_datetime_original
        == "2026-08-01T12:00:00"
    )


def test_clearing_the_roll_date_clears_intent(two_negative_roll: Path):
    run_metadata_set(two_negative_roll, {"roll": {"capture_date": None}})
    manifest = load_roll_manifest(two_negative_roll)
    assert manifest.metadata.roll_capture_date is None
    for negative in manifest.negatives:
        assert negative.capture_time.intended_datetime_original is None


# --- the catalog ------------------------------------------------------------


def test_roll_film_and_iso_persist_and_catalog(two_negative_roll: Path):
    run_metadata_set(
        two_negative_roll,
        {"roll": {"film": "Tri-X 400", "iso": "400"}},
    )
    manifest = load_roll_manifest(two_negative_roll)
    assert manifest.metadata.film == "Tri-X 400"
    assert manifest.metadata.iso == "400"
    effective = effective_metadata(
        manifest.metadata, manifest.negative(_NEGATIVE_A).metadata
    )
    assert effective["film"] == "Tri-X 400"
    assert effective["iso"] == "400"
    # The typed stock outranks the seeded ones and carries no kind.
    assert repo.list_metadata_values("film")[0] == "Tri-X 400"
    assert "Tri-X 400" not in repo.list_metadata_value_kinds("film")
    assert repo.list_metadata_values("iso") == ["400"]


def test_negative_payload_rejects_roll_only_fields(two_negative_roll: Path):
    before = load_roll_manifest(two_negative_roll).to_dict()
    with pytest.raises(MetadataEditFailure):
        run_metadata_set(
            two_negative_roll,
            {"negatives": {_NEGATIVE_A: {"film": "Tri-X 400"}}},
        )
    assert load_roll_manifest(two_negative_roll).to_dict() == before


def test_values_command_accepts_film(two_negative_roll: Path):
    run_metadata_set(two_negative_roll, {"roll": {"film": "HP5+"}})
    values, film_kinds = run_metadata_values("film")
    assert values[0] == "HP5+"
    assert film_kinds is not None
    assert "HP5+" not in film_kinds


def test_values_command_film_carries_seeded_kinds(two_negative_roll: Path):
    values, film_kinds = run_metadata_values("film")
    assert values == [stock for stock, _ in FILM_STOCK_SEEDS]
    assert film_kinds == dict(FILM_STOCK_SEEDS)
    assert film_kinds["Kodak Portra 400"] == "colour"
    assert film_kinds["Ilford HP5+"] == "monochrome"


def test_values_command_film_kind_survives_reuse(two_negative_roll: Path):
    run_metadata_set(two_negative_roll, {"roll": {"film": "Kodak Portra 400"}})
    values, film_kinds = run_metadata_values("film")
    assert values[0] == "Kodak Portra 400"
    assert film_kinds is not None
    assert film_kinds["Kodak Portra 400"] == "colour"
    assert len(values) == len(FILM_STOCK_SEEDS)


def test_values_command_other_fields_have_no_film_kinds(two_negative_roll: Path):
    run_metadata_set(two_negative_roll, {"roll": {"city": "Porto"}})
    values, film_kinds = run_metadata_values("city")
    assert values == ["Porto"]
    assert film_kinds is None


def test_catalog_remembers_canonical_values_not_caption(two_negative_roll: Path):
    run_metadata_set(
        two_negative_roll,
        {
            "roll": {
                "city": "Porto",
                "camera": "Nikon F3",
                "caption": "once, then never offered",
            },
        },
    )
    run_metadata_set(
        two_negative_roll, {"negatives": {_NEGATIVE_B: {"city": "Lisbon"}}}
    )
    run_metadata_set(two_negative_roll, {"negatives": {_NEGATIVE_A: {"city": "Porto"}}})
    assert repo.list_metadata_values("city") == ["Porto", "Lisbon"]
    assert repo.list_metadata_values("camera") == ["Nikon F3"]
    assert repo.list_metadata_values("caption") == []


def test_values_command_rejects_unknown_fields(two_negative_roll: Path):
    with pytest.raises(MetadataEditFailure) as excinfo:
        run_metadata_values("caption")
    assert excinfo.value.code == Code.INVALID_METADATA


# --- validation: nothing is written on a bad payload ------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"roll": {"exposure": "1/125"}},
        {"negatives": {_NEGATIVE_A: {"film": "Tri-X 400"}}},
        {"negatives": {_NEGATIVE_A: {"nope": "x"}}},
        {"negatives": {"unknown-negative": {"city": "x"}}},
        {"roll": {"capture_date": "August"}},
        {"negatives": {_NEGATIVE_A: {"capture_date": "2026-13-01"}}},
        {"roll": "not a map"},
    ],
)
def test_bad_payloads_fail_without_writing(two_negative_roll: Path, payload):
    before = load_roll_manifest(two_negative_roll).to_dict()
    with pytest.raises(MetadataEditFailure):
        run_metadata_set(two_negative_roll, payload)
    assert load_roll_manifest(two_negative_roll).to_dict() == before


def test_set_requires_a_registered_roll(tmp_path: Path):
    with pytest.raises(MetadataEditFailure) as excinfo:
        run_metadata_set(tmp_path / "nope", {"roll": {"city": "x"}})
    assert excinfo.value.code == Code.ROLL_NOT_FOUND


# --- concurrent writers -------------------------------------------------------


def _after_snapshot_read(monkeypatch, change):
    """Run `change()` right after `run_metadata_set`'s early read of the roll."""
    from scanny_boy import metadata_edit

    real_load = metadata_edit.load_roll_manifest

    def load_then_change(roll_dir):
        manifest = real_load(roll_dir)
        change()
        return manifest

    monkeypatch.setattr(metadata_edit, "load_roll_manifest", load_then_change)


def test_metadata_set_keeps_a_change_committed_after_its_read(
    two_negative_roll: Path, monkeypatch
):
    from scanny_boy.roll_manifest import mutate_roll_manifest

    def other_writer_edits_b():
        mutate_roll_manifest(
            two_negative_roll,
            lambda fresh: setattr(
                fresh.negative(_NEGATIVE_B).metadata, "city", "Lisbon"
            ),
        )

    _after_snapshot_read(monkeypatch, other_writer_edits_b)

    run_metadata_set(
        two_negative_roll,
        {"roll": {"camera": "Nikon F3"}, "negatives": {_NEGATIVE_A: {"state": "ME"}}},
    )

    manifest = load_roll_manifest(two_negative_roll)
    assert manifest.metadata.camera == "Nikon F3"
    assert manifest.negative(_NEGATIVE_A).metadata.state == "ME"
    assert manifest.negative(_NEGATIVE_B).metadata.city == "Lisbon"


def test_metadata_set_for_a_negative_deleted_after_its_read_fails_cleanly(
    two_negative_roll: Path, monkeypatch
):
    from scanny_boy.roll_manifest import mutate_roll_manifest

    def other_writer_deletes_a():
        def delete(fresh):
            fresh.negatives = [
                n for n in fresh.negatives if n.negative_id != _NEGATIVE_A
            ]

        mutate_roll_manifest(two_negative_roll, delete)

    _after_snapshot_read(monkeypatch, other_writer_deletes_a)

    with pytest.raises(MetadataEditFailure) as exc_info:
        run_metadata_set(
            two_negative_roll,
            {
                "roll": {"camera": "Nikon F3"},
                "negatives": {_NEGATIVE_A: {"state": "ME"}},
            },
        )

    assert exc_info.value.code == Code.NEGATIVE_NOT_FOUND
    manifest = load_roll_manifest(two_negative_roll)
    assert manifest.metadata.camera is None
    assert [n.negative_id for n in manifest.negatives] == [_NEGATIVE_B]


# --- the CLI surface --------------------------------------------------------


def test_cli_set_emits_metadata_updated_matching_schema(two_negative_roll: Path):
    import contextlib
    import io

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_status = main(
            [
                "metadata",
                "set",
                "--roll",
                str(two_negative_roll),
                "--payload",
                json.dumps({"roll": {"city": "Porto"}}),
            ]
        )
    assert exit_status == 0
    events = [json.loads(line) for line in stdout.getvalue().splitlines()]
    kinds = [event["event"] for event in events]
    assert kinds == ["started", "metadata_updated", "finished"]
    schema = load_schema()
    for event in events:
        assert_matches_schema(event, schema)
    updated = events[1]["manifest"]
    assert updated["metadata"]["city"] == "Porto"
    assert updated["negatives"][0]["metadata"]["city"] is None


def test_cli_values_emits_metadata_values_matching_schema(two_negative_roll: Path):
    run_metadata_set(two_negative_roll, {"roll": {"state": "Oregon"}})
    import contextlib
    import io

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_status = main(["metadata", "values", "--field", "state"])
    assert exit_status == 0
    events = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [event["event"] for event in events] == [
        "started",
        "metadata_values",
        "finished",
    ]
    schema = load_schema()
    for event in events:
        assert_matches_schema(event, schema)
    assert events[1]["values"] == ["Oregon"]
    assert "film_kinds" not in events[1]


def test_cli_values_film_emits_film_kinds_matching_schema(two_negative_roll: Path):
    run_metadata_set(two_negative_roll, {"roll": {"film": "Homebrew 100"}})
    import contextlib
    import io

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_status = main(["metadata", "values", "--field", "film"])
    assert exit_status == 0
    events = [json.loads(line) for line in stdout.getvalue().splitlines()]
    schema = load_schema()
    for event in events:
        assert_matches_schema(event, schema)
    values_event = events[1]
    assert values_event["event"] == "metadata_values"
    assert values_event["values"][0] == "Homebrew 100"
    assert values_event["film_kinds"] == dict(FILM_STOCK_SEEDS)


def test_cli_set_bad_payload_fails_with_invalid_metadata(two_negative_roll: Path):
    import contextlib
    import io

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_status = main(
            [
                "metadata",
                "set",
                "--roll",
                str(two_negative_roll),
                "--payload",
                json.dumps({"roll": {"not_a_field": "x"}}),
            ]
        )
    assert exit_status == 1
    events = [json.loads(line) for line in stdout.getvalue().splitlines()]
    error = next(event for event in events if event["event"] == "error")
    assert error["code"] == "INVALID_METADATA"


# --- Frame log keys (docs/METADATA_IMPORT_PLAN.md section 4.4) -----------------

_FRAME_UID = "abcde12345"


def test_frame_log_keys_round_trip_into_the_manifest(two_negative_roll: Path):
    result = run_metadata_set(
        two_negative_roll,
        {
            "roll": {"framelog_roll_uid": "zyxwv98765", "framelog_roll_number": 42},
            "negatives": {
                _NEGATIVE_A: {
                    "framelog_frame_uid": _FRAME_UID,
                    "latitude": 41.15,
                    "longitude": -8.61,
                    "gps_accuracy_m": 5,
                    "location_name": "  Park ",
                    "address": "1 Main St",
                    "logged_time": "2026-08-01T10:30:00",
                    "logged_offset": "+01:00",
                }
            },
        },
    )

    manifest = load_roll_manifest(two_negative_roll)
    assert manifest.metadata.framelog_roll_uid == "zyxwv98765"
    assert manifest.metadata.framelog_roll_number == 42
    negative = manifest.negative(_NEGATIVE_A)
    assert negative.framelog_frame_uid == _FRAME_UID
    assert (negative.place.latitude, negative.place.longitude) == (41.15, -8.61)
    assert negative.place.gps_accuracy_m == 5.0
    assert negative.place.location_name == "Park"
    assert negative.place.address == "1 Main St"
    assert negative.capture_time.logged_datetime_original == "2026-08-01T10:30:00"
    assert negative.capture_time.logged_offset == "+01:00"
    # The logged time is the intended time, and takes no rank: B is still
    # rank 1 on the roll date.
    assert negative.capture_time.intended_datetime_original == "2026-08-01T10:30:00"
    assert (
        manifest.negative(_NEGATIVE_B).capture_time.intended_datetime_original
        == "2026-08-01T12:00:00"
    )
    assert result["metadata"]["framelog_roll_uid"] == "zyxwv98765"
    assert result["negatives"][0]["place"]["address"] == "1 Main St"
    assert_matches_roll_manifest_schema(result, load_roll_manifest_schema())


def test_frame_log_keys_clear_with_null_or_empty(two_negative_roll: Path):
    run_metadata_set(
        two_negative_roll,
        {
            "roll": {"framelog_roll_uid": "zyxwv98765", "framelog_roll_number": 3},
            "negatives": {
                _NEGATIVE_A: {
                    "framelog_frame_uid": _FRAME_UID,
                    "latitude": 1,
                    "longitude": 2,
                    "address": "x",
                }
            },
        },
    )
    run_metadata_set(
        two_negative_roll,
        {
            "roll": {"framelog_roll_uid": None, "framelog_roll_number": ""},
            "negatives": {
                _NEGATIVE_A: {
                    "framelog_frame_uid": "",
                    "latitude": None,
                    "longitude": None,
                    "address": None,
                }
            },
        },
    )
    manifest = load_roll_manifest(two_negative_roll)
    assert manifest.metadata.framelog_roll_uid is None
    assert manifest.metadata.framelog_roll_number is None
    negative = manifest.negative(_NEGATIVE_A)
    assert negative.framelog_frame_uid is None
    assert negative.place.to_dict() == dict.fromkeys(negative.place.to_dict())


@pytest.mark.parametrize(
    ("roll", "negative"),
    [
        ({"framelog_roll_uid": "ABCDE12345"}, None),  # uppercase
        ({"framelog_roll_uid": "short"}, None),
        ({"framelog_roll_number": True}, None),  # bool is not a roll number
        ({"framelog_roll_number": 0}, None),
        ({"framelog_roll_number": 1.5}, None),
        ({"framelog_roll_number": "7"}, None),
        (None, {"framelog_frame_uid": "bad uid!!!"}),
        (None, {"latitude": 90.5, "longitude": 0}),
        (None, {"latitude": -90.5, "longitude": 0}),
        (None, {"latitude": 0, "longitude": 180.5}),
        (None, {"latitude": True, "longitude": 0}),
        (None, {"latitude": "12", "longitude": 0}),
        (None, {"latitude": 12}),  # lat without lon
        (None, {"longitude": 12}),  # lon without lat
        (None, {"gps_accuracy_m": -1}),
        (None, {"location_name": "x" * 501}),
        (None, {"address": 5}),
        (None, {"logged_time": "2026-08-01 10:30:00"}),
        (None, {"logged_time": "2026-13-01T10:30:00"}),
        (None, {"logged_time": "2026-08-01"}),
        (None, {"logged_time": 20260801}),
        (None, {"logged_time": "2026-08-01T10:30:00", "logged_offset": "0100"}),
        (None, {"logged_offset": "+01:00"}),  # offset without time
    ],
)
def test_invalid_frame_log_values_fail_without_writing(
    two_negative_roll: Path, roll, negative
):
    payload: dict = {"roll": {"city": "Porto", **(roll or {})}}
    if negative is not None:
        payload["negatives"] = {_NEGATIVE_A: negative}
    with pytest.raises(MetadataEditFailure) as excinfo:
        run_metadata_set(two_negative_roll, payload)
    assert excinfo.value.code == Code.INVALID_METADATA
    # All or nothing: the valid city in the same payload did not land.
    assert load_roll_manifest(two_negative_roll).metadata.city is None


def test_lat_lon_rule_is_checked_on_the_merged_result(two_negative_roll: Path):
    run_metadata_set(
        two_negative_roll,
        {"negatives": {_NEGATIVE_A: {"latitude": 10, "longitude": 20}}},
    )
    # Changing only one is fine while the other stays set...
    run_metadata_set(two_negative_roll, {"negatives": {_NEGATIVE_A: {"latitude": 11}}})
    # ...but clearing only one is not.
    with pytest.raises(MetadataEditFailure) as excinfo:
        run_metadata_set(
            two_negative_roll, {"negatives": {_NEGATIVE_A: {"longitude": None}}}
        )
    assert excinfo.value.code == Code.INVALID_METADATA
    place = load_roll_manifest(two_negative_roll).negative(_NEGATIVE_A).place
    assert (place.latitude, place.longitude) == (11, 20)


def test_logged_time_without_offset_drops_the_stored_offset(
    two_negative_roll: Path,
):
    def logged(fields: dict) -> tuple:
        run_metadata_set(two_negative_roll, {"negatives": {_NEGATIVE_A: fields}})
        ct = load_roll_manifest(two_negative_roll).negative(_NEGATIVE_A).capture_time
        return ct.logged_datetime_original, ct.logged_offset

    assert logged(
        {"logged_time": "2026-08-01T10:30:00", "logged_offset": "+01:00"}
    ) == ("2026-08-01T10:30:00", "+01:00")
    # A re-import whose frame has no offset must not keep the old one.
    assert logged({"logged_time": "2026-08-01T11:00:00"}) == (
        "2026-08-01T11:00:00",
        None,
    )
    logged({"logged_time": "2026-08-01T11:00:00", "logged_offset": "-05:00"})
    # Clearing the time alone clears its offset too.
    assert logged({"logged_time": None}) == (None, None)


def test_setting_capture_date_clears_the_logged_time(two_negative_roll: Path):
    run_metadata_set(
        two_negative_roll,
        {
            "negatives": {
                _NEGATIVE_A: {
                    "logged_time": "2026-08-01T10:30:00",
                    "logged_offset": "+01:00",
                }
            }
        },
    )
    run_metadata_set(
        two_negative_roll, {"negatives": {_NEGATIVE_A: {"capture_date": "2026-08-09"}}}
    )
    capture_time = load_roll_manifest(two_negative_roll).negative(_NEGATIVE_A).capture_time
    assert capture_time.date_override == "2026-08-09"
    assert capture_time.logged_datetime_original is None
    assert capture_time.logged_offset is None
    assert capture_time.intended_datetime_original == "2026-08-09T12:00:00"


def test_setting_logged_time_clears_the_capture_date(two_negative_roll: Path):
    run_metadata_set(
        two_negative_roll, {"negatives": {_NEGATIVE_A: {"capture_date": "2026-08-09"}}}
    )
    run_metadata_set(
        two_negative_roll,
        {"negatives": {_NEGATIVE_A: {"logged_time": "2026-08-01T10:30:00"}}},
    )
    capture_time = load_roll_manifest(two_negative_roll).negative(_NEGATIVE_A).capture_time
    assert capture_time.date_override is None
    assert capture_time.logged_datetime_original == "2026-08-01T10:30:00"


def test_one_payload_may_set_both_capture_date_and_logged_time(
    two_negative_roll: Path,
):
    run_metadata_set(
        two_negative_roll,
        {
            "negatives": {
                _NEGATIVE_A: {
                    "capture_date": "2026-08-09",
                    "logged_time": "2026-08-01T10:30:00",
                }
            }
        },
    )
    capture_time = load_roll_manifest(two_negative_roll).negative(_NEGATIVE_A).capture_time
    assert capture_time.date_override == "2026-08-09"
    assert capture_time.logged_datetime_original == "2026-08-01T10:30:00"


def test_clearing_capture_date_keeps_the_logged_time(two_negative_roll: Path):
    run_metadata_set(
        two_negative_roll,
        {
            "negatives": {
                _NEGATIVE_A: {
                    "capture_date": "2026-08-09",
                    "logged_time": "2026-08-01T10:30:00",
                }
            }
        },
    )
    run_metadata_set(
        two_negative_roll, {"negatives": {_NEGATIVE_A: {"capture_date": None}}}
    )
    capture_time = load_roll_manifest(two_negative_roll).negative(_NEGATIVE_A).capture_time
    assert capture_time.logged_datetime_original == "2026-08-01T10:30:00"


def test_frame_log_keys_do_not_feed_the_catalog(two_negative_roll: Path):
    run_metadata_set(
        two_negative_roll,
        {"negatives": {_NEGATIVE_A: {"location_name": "Park", "address": "1 Main"}}},
    )
    for field in ("film", "iso", "city", "state", "camera", "lens"):
        assert "Park" not in run_metadata_values(field)[0]


def test_roll_keys_are_not_valid_on_a_negative(two_negative_roll: Path):
    with pytest.raises(MetadataEditFailure) as excinfo:
        run_metadata_set(
            two_negative_roll,
            {"negatives": {_NEGATIVE_A: {"framelog_roll_uid": _FRAME_UID}}},
        )
    assert excinfo.value.code == Code.INVALID_METADATA
    with pytest.raises(MetadataEditFailure):
        run_metadata_set(two_negative_roll, {"roll": {"latitude": 1}})
