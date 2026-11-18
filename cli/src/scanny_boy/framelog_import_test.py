"""The `framelog` runners and CLI: `rolls`, `roll` and `apply` end to end
over the committed fixtures with a scripted opener (no network)."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import pytest

from scanny_boy import framelog_client
from scanny_boy.cli import main
from scanny_boy.events import Code
from scanny_boy.framelog_client import FrameLogError
from scanny_boy.framelog_import import (
    run_framelog_apply,
    run_framelog_roll,
    run_framelog_rolls,
)
from scanny_boy.framelog_support import (
    TOKEN,
    ScriptedOpener,
    http_error,
    json_opener,
    load_fixture,
    make_sequenced_roll,
    negative_id,
)
from scanny_boy.metadata_edit import MetadataEditFailure, run_metadata_set
from scanny_boy.roll_manifest import load_roll_manifest
from scanny_boy.schema_test_support import assert_matches_schema, load_schema

BASE = "https://example.test"
ENV = {"SCANNY_BOY_FRAMELOG_TOKEN": TOKEN}


@pytest.fixture()
def roll_dir(tmp_path: Path) -> Path:
    return make_sequenced_roll(tmp_path, 7)


def _apply(roll_dir, assignments, opener=None, ref="k3j9x0a1bc"):
    opener = opener or json_opener(load_fixture("roll_42"))
    return run_framelog_apply(
        roll_dir, ref, json.dumps(assignments), BASE, environ=ENV, opener=opener
    )


def _identity():
    return {negative_id(i): i for i in range(1, 8)}


def _snapshot(roll_dir):
    return load_roll_manifest(roll_dir).to_dict()


# --- rolls ------------------------------------------------------------------


def test_rolls_are_snake_cased():
    opener = json_opener(load_fixture("rolls"))
    rolls = run_framelog_rolls("all", BASE, environ=ENV, opener=opener)
    assert len(rolls) == 3
    assert set(rolls[0]) == {
        "uid", "roll_number", "film", "camera", "iso", "created_at",
        "finished_at", "frame_count", "last_frame", "first_captured_at",
        "last_captured_at",
    }  # fmt: skip
    assert rolls[0]["finished_at"] is None
    assert rolls[1]["roll_number"] == 42
    assert rolls[2]["camera"] is None and rolls[2]["last_frame"] is None


def test_no_token_means_no_request(roll_dir):
    opener = json_opener({})
    for call in (
        lambda: run_framelog_rolls("all", BASE, environ={}, opener=opener),
        lambda: run_framelog_roll(roll_dir, "42", BASE, environ={}, opener=opener),
        lambda: run_framelog_apply(
            roll_dir, "k3j9x0a1bc", "{}", BASE, environ={}, opener=opener
        ),
    ):
        with pytest.raises(FrameLogError) as excinfo:
            call()
        assert excinfo.value.code == Code.FRAMELOG_NO_TOKEN
    assert opener.requests == []


# --- roll -------------------------------------------------------------------


def test_roll_reports_roll_frames_and_default_alignment(roll_dir):
    result = run_framelog_roll(
        roll_dir, "42", BASE, environ=ENV, opener=json_opener(load_fixture("roll_42"))
    )
    assert result["roll"] == {
        "uid": "k3j9x0a1bc",
        "roll_number": 42,
        "film": "Kodak Portra 400",
        "camera": "Nikon FM2",
        "iso": 400,
        "created_at": "2026-09-30T14:02:11.000Z",
        "finished_at": "2026-10-05T20:41:00.000Z",
        "frame_count": 6,
        "last_frame": 7,
        "missing_frame_numbers": [4],
    }
    assert [f["frame_number"] for f in result["frames"]] == [1, 2, 3, 5, 6, 7]
    assert result["alignment"] == [
        {"negative_id": negative_id(i), "frame_number": i} for i in range(1, 8)
    ]
    assert result["linked"] is False


def test_roll_after_apply_is_linked_and_reuses_stored_pairs(roll_dir):
    _apply(roll_dir, {negative_id(1): 1, negative_id(2): 5, negative_id(3): 7})
    result = run_framelog_roll(
        roll_dir, "42", BASE, environ=ENV, opener=json_opener(load_fixture("roll_42"))
    )
    assert result["linked"] is True
    numbers = [a["frame_number"] for a in result["alignment"]]
    assert numbers == [1, 5, 7, None, None, None, None]


def test_roll_of_an_empty_frame_log_roll(roll_dir):
    result = run_framelog_roll(
        roll_dir, "9", BASE, environ=ENV, opener=json_opener(load_fixture("roll_empty"))
    )
    assert result["roll"]["last_frame"] == 0
    assert result["frames"] == []
    assert all(a["frame_number"] is None for a in result["alignment"])


def test_roll_with_unregistered_roll_dir_fails_before_the_request(tmp_path):
    opener = json_opener(load_fixture("roll_42"))
    with pytest.raises(MetadataEditFailure) as excinfo:
        run_framelog_roll(tmp_path / "nope", "42", BASE, environ=ENV, opener=opener)
    assert excinfo.value.code == Code.ROLL_NOT_FOUND


# --- apply: what lands in the manifest ---------------------------------------


def test_apply_writes_roll_and_negative_metadata(roll_dir):
    result = _apply(roll_dir, _identity())
    manifest = load_roll_manifest(roll_dir)
    assert result["metadata"]["film"] == "Kodak Portra 400"

    roll = manifest.metadata
    assert (roll.film, roll.camera, roll.iso) == (
        "Kodak Portra 400",
        "Nikon FM2",
        "400",
    )
    assert roll.roll_capture_date == "2026-10-01"
    assert (roll.framelog_roll_uid, roll.framelog_roll_number) == ("k3j9x0a1bc", 42)

    n1 = manifest.negative(negative_id(1))
    assert n1.framelog_frame_uid == "a8f2m4q9zz"
    assert n1.capture_time.logged_datetime_original == "2026-10-01T10:10:00"
    assert n1.capture_time.logged_offset == "-05:00"
    assert n1.capture_time.date_override is None
    assert n1.metadata.lens == "Nikkor 50mm f/1.8"
    assert n1.metadata.caption == "f/8 1/125"
    assert (n1.metadata.city, n1.metadata.state) == ("Chicago", "IL")
    assert (n1.place.latitude, n1.place.longitude) == (41.8781, -87.6298)
    assert n1.place.gps_accuracy_m == 12
    assert n1.place.location_name == "Holy Name Cathedral"
    assert n1.place.address == "735 N State St, Chicago, IL 60654"

    # Date precision: an override, no logged time.
    n2 = manifest.negative(negative_id(2))
    assert n2.capture_time.date_override == "2026-10-02"
    assert n2.capture_time.logged_datetime_original is None
    assert n2.framelog_frame_uid == "b7k2n5r8tw"

    # All-null frame: only the uid is written.
    n3 = manifest.negative(negative_id(3))
    assert n3.framelog_frame_uid == "c4d9e1f6gh"
    assert n3.metadata.caption is None and n3.place.latitude is None

    # Frame number 4 is not logged: nothing written, negative 4 stays blank.
    n4 = manifest.negative(negative_id(4))
    assert n4.framelog_frame_uid is None and n4.metadata.lens is None

    # Paris: no city/state; offset +02:00.
    n5 = manifest.negative(negative_id(5))
    assert n5.framelog_frame_uid == "d2m7p0s3vx"
    assert n5.capture_time.logged_offset == "+02:00"
    assert n5.metadata.city is None and n5.metadata.state is None
    assert n5.place.address == "12 Rue de Rivoli, 75004 Paris, France"
    assert n5.place.location_name == "Rue de Rivoli"

    n6 = manifest.negative(negative_id(6))
    assert (n6.metadata.city, n6.metadata.state) == ("Chicago", "IL")
    assert n6.place.gps_accuracy_m == 150

    # Blank notes are not written; ZIP+4 address still parses.
    n7 = manifest.negative(negative_id(7))
    assert n7.metadata.caption is None
    assert (n7.metadata.city, n7.metadata.state) == ("Chicago", "IL")

    # The logged time feeds the intended capture time.
    assert manifest.negative(
        negative_id(1)
    ).capture_time.intended_datetime_original == ("2026-10-01T10:10:00")


def test_apply_overwrites_existing_values_but_never_clears_with_nulls(roll_dir):
    run_metadata_set(
        roll_dir,
        {
            "roll": {"film": "Old Film", "camera": "Old Camera"},
            "negatives": {
                negative_id(1): {"lens": "Old lens", "caption": "old"},
                negative_id(3): {"caption": "keep me", "address": "keep"},
            },
        },
    )
    _apply(roll_dir, {negative_id(1): 1, negative_id(3): 3})
    manifest = load_roll_manifest(roll_dir)
    assert manifest.metadata.film == "Kodak Portra 400"
    assert manifest.negative(negative_id(1)).metadata.lens == "Nikkor 50mm f/1.8"
    assert manifest.negative(negative_id(1)).metadata.caption == "f/8 1/125"
    # Frame 3 is all-null: the existing values survive.
    kept = manifest.negative(negative_id(3))
    assert kept.metadata.caption == "keep me" and kept.place.address == "keep"


def test_apply_date_precision_replaces_a_stored_logged_time(roll_dir):
    _apply(roll_dir, {negative_id(1): 1})
    assert (
        load_roll_manifest(roll_dir)
        .negative(negative_id(1))
        .capture_time.logged_datetime_original
    )
    _apply(roll_dir, {negative_id(1): 2})
    capture_time = load_roll_manifest(roll_dir).negative(negative_id(1)).capture_time
    assert capture_time.logged_datetime_original is None
    assert capture_time.logged_offset is None
    assert capture_time.date_override == "2026-10-02"


def test_apply_clears_frame_uid_only_where_set(roll_dir):
    _apply(roll_dir, {negative_id(1): 1, negative_id(2): 2, negative_id(4): 5})
    before = _snapshot(roll_dir)
    assert before["negatives"][1]["framelog_frame_uid"] == "b7k2n5r8tw"

    # Negative 2 -> no match; 3 (never linked) -> no match; 4 -> missing frame 4.
    _apply(
        roll_dir,
        {
            negative_id(1): 1,
            negative_id(2): None,
            negative_id(3): None,
            negative_id(4): 4,
        },
    )
    manifest = load_roll_manifest(roll_dir)
    assert manifest.negative(negative_id(2)).framelog_frame_uid is None
    assert manifest.negative(negative_id(3)).framelog_frame_uid is None
    assert manifest.negative(negative_id(4)).framelog_frame_uid is None
    assert manifest.negative(negative_id(1)).framelog_frame_uid == "a8f2m4q9zz"
    # Clearing the link leaves the previously imported values in place.
    assert manifest.negative(negative_id(2)).capture_time.date_override == "2026-10-02"


def test_apply_leaves_negatives_absent_from_assignments_untouched(roll_dir):
    _apply(roll_dir, {negative_id(1): 1})
    manifest = load_roll_manifest(roll_dir)
    assert manifest.negative(negative_id(2)).framelog_frame_uid is None
    assert manifest.negative(negative_id(7)).place.address is None


def test_apply_with_no_assignments_still_links_the_roll(roll_dir):
    _apply(roll_dir, {})
    manifest = load_roll_manifest(roll_dir)
    assert manifest.metadata.framelog_roll_uid == "k3j9x0a1bc"


def test_apply_makes_exactly_one_request(roll_dir):
    opener = json_opener(load_fixture("roll_42"))
    _apply(roll_dir, _identity(), opener=opener, ref="k3j9x0a1bc")
    assert len(opener.requests) == 1
    assert opener.requests[0].full_url.endswith("/rolls/k3j9x0a1bc")


# --- apply: validation, nothing written --------------------------------------


@pytest.mark.parametrize(
    ("assignments", "code", "fetches"),
    [
        ({"no-such-negative": 1}, Code.NEGATIVE_NOT_FOUND, 0),
        ({negative_id(1): 1, "no-such-negative": None}, Code.NEGATIVE_NOT_FOUND, 0),
        ({negative_id(1): 3, negative_id(2): 2}, Code.INVALID_METADATA, 0),
        ({negative_id(1): 2, negative_id(2): 2}, Code.INVALID_METADATA, 0),
        ({negative_id(2): 1, negative_id(1): 5}, Code.INVALID_METADATA, 0),
        ({negative_id(1): 1.5}, Code.INVALID_METADATA, 0),
        ({negative_id(1): True}, Code.INVALID_METADATA, 0),
        ({negative_id(1): "1"}, Code.INVALID_METADATA, 0),
        ({negative_id(1): 0}, Code.INVALID_METADATA, 0),
        ({negative_id(1): 8}, Code.INVALID_METADATA, 1),
        ({negative_id(1): -1}, Code.INVALID_METADATA, 0),
    ],
)
def test_invalid_assignments_write_nothing(roll_dir, assignments, code, fetches):
    before = _snapshot(roll_dir)
    opener = json_opener(load_fixture("roll_42"))
    with pytest.raises(MetadataEditFailure) as excinfo:
        _apply(roll_dir, assignments, opener=opener)
    assert excinfo.value.code == code
    assert len(opener.requests) == fetches
    assert _snapshot(roll_dir) == before


def test_assignments_must_be_a_json_object(roll_dir):
    for raw in ("not json", "[1]", "3"):
        with pytest.raises(MetadataEditFailure) as excinfo:
            run_framelog_apply(
                roll_dir, "k3j9x0a1bc", raw, BASE, environ=ENV, opener=json_opener({})
            )
        assert excinfo.value.code == Code.INVALID_METADATA


def test_payload_rejection_from_metadata_set_leaves_the_roll_untouched(roll_dir):
    data = load_fixture("roll_42")
    data["frames"][0]["uid"] = "BAD UID"  # fails the 10-char uid rule
    before = _snapshot(roll_dir)
    with pytest.raises(MetadataEditFailure) as excinfo:
        _apply(roll_dir, {negative_id(1): 1}, opener=json_opener(data))
    assert excinfo.value.code == Code.INVALID_METADATA
    assert _snapshot(roll_dir) == before


def test_api_failure_during_apply_writes_nothing(roll_dir):
    before = _snapshot(roll_dir)
    opener = ScriptedOpener(lambda request: http_error(404))
    with pytest.raises(FrameLogError) as excinfo:
        _apply(roll_dir, _identity(), opener=opener)
    assert excinfo.value.code == Code.FRAMELOG_NOT_FOUND
    assert _snapshot(roll_dir) == before


# --- the CLI surface ---------------------------------------------------------


def _run_cli(monkeypatch, argv, payload=None, opener=None, token=TOKEN):
    if token is None:
        monkeypatch.delenv("SCANNY_BOY_FRAMELOG_TOKEN", raising=False)
    else:
        monkeypatch.setenv("SCANNY_BOY_FRAMELOG_TOKEN", token)
    opener = opener or json_opener(payload)
    monkeypatch.setattr(framelog_client, "_default_open", opener)
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        status = main(argv)
    events = [json.loads(line) for line in stdout.getvalue().splitlines()]
    schema = load_schema()
    for event in events:
        assert_matches_schema(event, schema)
    assert TOKEN not in stdout.getvalue()
    return status, events, opener


def test_cli_rolls(monkeypatch):
    status, events, opener = _run_cli(
        monkeypatch,
        ["framelog", "rolls", "--status", "active", "--base-url", BASE],
        load_fixture("rolls"),
    )
    assert status == 0
    assert [e["event"] for e in events] == ["started", "framelog_rolls", "finished"]
    assert events[0]["command"] == "framelog rolls"
    assert len(events[1]["rolls"]) == 3
    assert "run_id" not in events[1]
    assert opener.requests[0].full_url.endswith("/rolls?status=active")


def test_cli_roll(monkeypatch, roll_dir):
    status, events, _ = _run_cli(
        monkeypatch,
        [
            "framelog",
            "roll",
            "--roll",
            str(roll_dir),
            "--ref",
            "42",
            "--base-url",
            BASE,
        ],
        load_fixture("roll_42"),
    )
    assert status == 0
    assert [e["event"] for e in events] == ["started", "framelog_roll", "finished"]
    assert len(events[1]["alignment"]) == 7
    assert events[1]["linked"] is False


def test_cli_apply_emits_metadata_updated(monkeypatch, roll_dir):
    status, events, _ = _run_cli(
        monkeypatch,
        [
            "framelog", "apply", "--roll", str(roll_dir), "--ref", "k3j9x0a1bc",
            "--assignments", json.dumps(_identity()), "--base-url", BASE,
        ],
        load_fixture("roll_42"),
    )  # fmt: skip
    assert status == 0
    assert [e["event"] for e in events] == ["started", "metadata_updated", "finished"]
    assert events[1]["manifest"]["metadata"]["framelog_roll_uid"] == "k3j9x0a1bc"


def test_cli_no_token_is_a_fatal_error_and_makes_no_request(monkeypatch, roll_dir):
    opener = json_opener({})
    status, events, _ = _run_cli(
        monkeypatch,
        ["framelog", "rolls", "--base-url", BASE],
        opener=opener,
        token=None,
    )
    assert status == 1
    assert [e["event"] for e in events] == ["started", "error", "finished"]
    assert events[1]["code"] == "FRAMELOG_NO_TOKEN"
    assert opener.requests == []


def test_cli_maps_api_and_validation_failures_to_error_events(monkeypatch, roll_dir):
    status, events, _ = _run_cli(
        monkeypatch,
        ["framelog", "rolls", "--base-url", BASE],
        opener=ScriptedOpener(lambda request: http_error(401)),
    )
    assert status == 1 and events[1]["code"] == "FRAMELOG_UNAUTHORIZED"

    status, events, _ = _run_cli(
        monkeypatch,
        [
            "framelog", "apply", "--roll", str(roll_dir), "--ref", "k3j9x0a1bc",
            "--assignments", json.dumps({"ghost": 1}), "--base-url", BASE,
        ],
        load_fixture("roll_42"),
    )  # fmt: skip
    assert status == 1 and events[1]["code"] == "NEGATIVE_NOT_FOUND"
    assert events[-1]["status"] == "failed"
