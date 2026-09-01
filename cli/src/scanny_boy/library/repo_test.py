"""Tests for the library repository: registration, edits ops log, the
rotation bookkeeping the edit/export commands build on, and the flat-field
profile records."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import inspect

from scanny_boy import flatfield
from scanny_boy.events import Code
from scanny_boy.flatfield import FlatFieldError, FlatFieldProfile
from scanny_boy.library import db, repo
from scanny_boy.roll_manifest import new_roll_manifest, write_roll_manifest


@pytest.fixture()
def roll_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "Roll"
    directory.mkdir()
    write_roll_manifest(
        directory,
        new_roll_manifest(roll_id="rid-1", roll_name="Roll", shots_per_negative=2),
    )
    return directory


def _negative_in(roll_dir: Path, negative_id: str) -> None:
    from scanny_boy.roll_manifest import load_roll_manifest

    manifest = load_roll_manifest(roll_dir)
    from scanny_boy.roll_manifest_test import _negative

    manifest.negatives.append(_negative(negative_id=negative_id))
    write_roll_manifest(roll_dir, manifest)


# --- the database itself ---------------------------------------------------


def test_open_engine_migrates_to_head():
    engine = db.open_engine()
    table_names = set(inspect(engine).get_table_names())
    assert {"rolls", "runs", "sources", "negatives", "edits"} <= table_names


def test_migrations_are_idempotent():
    db.open_engine()
    db.open_engine()


# --- the edits ops log -----------------------------------------------------


def test_append_edit_assigns_ascending_positions(roll_dir):
    _negative_in(roll_dir, "rid-1-negative-01")

    first = repo.append_edit(roll_dir, "rid-1-negative-01", repo.ROTATE_OP, {"direction": "cw"})
    second = repo.append_edit(
        roll_dir, "rid-1-negative-01", repo.ROTATE_OP, {"direction": "ccw"}
    )

    assert (first["position"], second["position"]) == (1, 2)
    assert [e["params"]["direction"] for e in repo.edits_for(roll_dir, "rid-1-negative-01")] == [
        "cw",
        "ccw",
    ]


def test_append_edit_rejects_an_unknown_negative(roll_dir):
    with pytest.raises(repo.RollNotRegisteredError):
        repo.append_edit(roll_dir, "nope", repo.ROTATE_OP, {"direction": "cw"})


def test_net_rotation_composes_quarter_turns(roll_dir):
    _negative_in(roll_dir, "rid-1-negative-01")
    append = repo.append_edit

    append(roll_dir, "rid-1-negative-01", repo.ROTATE_OP, {"direction": "cw"})
    assert repo.net_rotation_quarter_turns(roll_dir, "rid-1-negative-01") == 1

    append(roll_dir, "rid-1-negative-01", repo.ROTATE_OP, {"direction": "cw"})
    append(roll_dir, "rid-1-negative-01", repo.ROTATE_OP, {"direction": "cw"})
    assert repo.net_rotation_quarter_turns(roll_dir, "rid-1-negative-01") == 3

    append(roll_dir, "rid-1-negative-01", repo.ROTATE_OP, {"direction": "ccw"})
    assert repo.net_rotation_quarter_turns(roll_dir, "rid-1-negative-01") == 2

    # A full turn nets out to zero.
    append(roll_dir, "rid-1-negative-01", repo.ROTATE_OP, {"direction": "cw"})
    append(roll_dir, "rid-1-negative-01", repo.ROTATE_OP, {"direction": "cw"})
    assert repo.net_rotation_quarter_turns(roll_dir, "rid-1-negative-01") == 0


def test_net_rotation_ignores_unknown_ops_and_directions(roll_dir):
    _negative_in(roll_dir, "rid-1-negative-01")
    repo.append_edit(roll_dir, "rid-1-negative-01", "future_op", {"whatever": 1})
    repo.append_edit(roll_dir, "rid-1-negative-01", repo.ROTATE_OP, {"direction": "sideways"})

    assert repo.net_rotation_quarter_turns(roll_dir, "rid-1-negative-01") == 0


def test_edits_survive_re_saving_the_manifest(roll_dir):
    """Re-stitching keeps a negative's `negative_id`, so its edit history
    must survive the diff-and-merge save."""
    _negative_in(roll_dir, "rid-1-negative-01")
    repo.append_edit(roll_dir, "rid-1-negative-01", repo.ROTATE_OP, {"direction": "cw"})

    from scanny_boy.roll_manifest import load_roll_manifest

    manifest = load_roll_manifest(roll_dir)
    write_roll_manifest(roll_dir, manifest)

    assert repo.net_rotation_quarter_turns(roll_dir, "rid-1-negative-01") == 1


def test_removing_a_negative_removes_its_edits(roll_dir):
    _negative_in(roll_dir, "rid-1-negative-01")
    _negative_in(roll_dir, "rid-1-negative-02")
    repo.append_edit(roll_dir, "rid-1-negative-01", repo.ROTATE_OP, {"direction": "cw"})
    repo.append_edit(roll_dir, "rid-1-negative-02", repo.ROTATE_OP, {"direction": "cw"})

    from scanny_boy.roll_manifest import load_roll_manifest

    manifest = load_roll_manifest(roll_dir)
    manifest.negatives = [
        n for n in manifest.negatives if n.negative_id != "rid-1-negative-01"
    ]
    write_roll_manifest(roll_dir, manifest)

    assert repo.net_rotation_quarter_turns(roll_dir, "rid-1-negative-02") == 1
    # The removed negative is gone outright, edits and all.
    with pytest.raises(repo.RollNotRegisteredError):
        repo.net_rotation_quarter_turns(roll_dir, "rid-1-negative-01")


# --- roll rename and folder moves -------------------------------------------


def test_save_updates_folder_path_after_a_move(roll_dir, tmp_path):
    moved = tmp_path / "Moved"
    roll_dir.rename(moved)

    # Registration follows the row's `folder_path`, not the filesystem: the
    # row still names the old folder until a save tells it otherwise.
    assert repo.roll_registered(roll_dir) is True
    assert repo.roll_registered(moved) is False

    from scanny_boy.roll_manifest import load_roll_manifest

    with pytest.raises(repo.RollNotRegisteredError):
        load_roll_manifest(moved)

    manifest = load_roll_manifest(roll_dir)
    write_roll_manifest(moved, manifest)

    assert repo.roll_registered(moved) is True
    assert repo.roll_registered(roll_dir) is False
    assert load_roll_manifest(moved).roll_id == "rid-1"


# --- flat-field profiles ---------------------------------------------------


def _flatfield_profile(name: str = "Copy stand") -> FlatFieldProfile:
    gain_map = np.full((8, 8, 3), 1.25, dtype=np.float32)
    path, sha256 = flatfield.save_gain_map(f"pid-{name}", gain_map)
    return FlatFieldProfile(
        profile_id=f"pid-{name}",
        name=name,
        gain_map_path=str(path),
        gain_map_sha256=sha256,
        source_path="/refs/bare.NEF",
        reference_width=6064,
        reference_height=4040,
        params=flatfield.build_params(),
        scanny_boy_version="0.3.0",
        created_at="2026-09-01T00:00:00Z",
    )


def test_flatfield_profiles_round_trip_all_fields():
    profile = _flatfield_profile()

    repo.save_flatfield_profile(profile)

    loaded = repo.load_flatfield_profile(profile.profile_id)
    assert loaded == profile
    assert [p.profile_id for p in repo.list_flatfield_profiles()] == [
        profile.profile_id
    ]


def test_load_flatfield_profile_unknown_id_is_typed_not_found():
    with pytest.raises(FlatFieldError) as excinfo:
        repo.load_flatfield_profile("nope")

    assert excinfo.value.code == Code.FLATFIELD_PROFILE_NOT_FOUND


def test_delete_flatfield_profile_removes_the_row():
    profile = _flatfield_profile()
    repo.save_flatfield_profile(profile)

    repo.delete_flatfield_profile(profile.profile_id)

    assert repo.list_flatfield_profiles() == []
    with pytest.raises(FlatFieldError):
        repo.load_flatfield_profile(profile.profile_id)


def test_rolls_using_flatfield_matches_the_token_inside_processing_params():
    profile = _flatfield_profile()
    repo.save_flatfield_profile(profile)

    locked = new_roll_manifest(roll_id="rid-locked", roll_name="Locked", shots_per_negative=2)
    locked.processing_params = {
        "output_bps": 16,
        "flat_field": flatfield.profile_token(profile),
    }
    write_roll_manifest(tmp_roll_dir("locked"), locked)

    other = new_roll_manifest(roll_id="rid-other", roll_name="Other", shots_per_negative=2)
    other.processing_params = {"output_bps": 16}
    write_roll_manifest(tmp_roll_dir("other"), other)

    assert repo.rolls_using_flatfield(profile.profile_id) == ["rid-locked"]
    assert repo.rolls_using_flatfield("someone-else") == []


def tmp_roll_dir(name: str) -> Path:
    directory = Path(db.library_db_path()).parent / name
    directory.mkdir(parents=True, exist_ok=True)
    return directory
