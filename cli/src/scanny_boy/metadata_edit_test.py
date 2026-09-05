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
from scanny_boy.schema_test_support import assert_matches_schema, load_schema
from scanny_boy.stitch_pipeline_test import _roll_dir

_NEGATIVE_A = "stitch-negative-01"
_NEGATIVE_B = "stitch-negative-02"


@pytest.fixture()
def two_negative_roll(tmp_path: Path) -> Path:
    roll_dir = _roll_dir(tmp_path)
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
    assert (
        manifest.negative(_NEGATIVE_B).capture_time.date_override == "2026-08-03"
    )
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


def test_catalog_remembers_canonical_values_not_caption(two_negative_roll: Path):
    run_metadata_set(
        two_negative_roll,
        {
            "roll": {"city": "Porto", "caption": "once, then never offered"},
            "negatives": {_NEGATIVE_A: {"camera": "Nikon F3"}},
        },
    )
    run_metadata_set(two_negative_roll, {"negatives": {_NEGATIVE_B: {"city": "Lisbon"}}})
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
        exit_status = main(
            ["metadata", "values", "--field", "state"]
        )
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
