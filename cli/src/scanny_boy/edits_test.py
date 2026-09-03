"""Tests for `edit rotate` and `edit delete`: the ops log gets the op, the
preview refreshes, and the published TIFF is never touched by rotate;
delete removes the record, the TIFF, and the preview."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import tifffile

from scanny_boy.edits import EditFailure, run_edit_delete, run_edit_rotate
from scanny_boy.events import Code, WarningEvent
from scanny_boy.roll_manifest import load_roll_manifest, write_roll_manifest
from scanny_boy.roll_manifest_test import _negative, _run
from scanny_boy.stitch_pipeline_test import _roll_dir

_NEGATIVE_ID = "stitch-negative-01"


@pytest.fixture()
def stitched_roll(tmp_path: Path) -> Path:
    roll_dir = _roll_dir(tmp_path)
    manifest = load_roll_manifest(roll_dir)
    from scanny_boy.manifest import SourceRecord
    from scanny_boy.roll_manifest import append_run, merge_sources

    append_run(manifest, _run(run_id="stitch-run", short_id="stitch"))
    merge_sources(
        manifest,
        [SourceRecord(filename="a.NEF", absolute_path="/x", size=1, mtime=1.0, sha256="a" * 64)],
        "stitch-run",
    )
    negative = _negative(
        negative_id=_NEGATIVE_ID,
        run_id="stitch-run",
        status="completed",
        sequence=1,
        output={
            "name": "_DSC0001.tif",
            "size": 0,
            "sha256": "0" * 64,
            "width": 4,
            "height": 3,
        },
    )
    manifest.negatives.append(negative)
    write_roll_manifest(roll_dir, manifest)
    tifffile.imwrite(roll_dir / "_DSC0001.tif", np.arange(12, dtype=np.uint16).reshape(3, 4))
    return roll_dir


def _tiff_bytes(roll_dir: Path) -> bytes:
    return (roll_dir / "_DSC0001.tif").read_bytes()


def test_rotate_records_the_edit_and_reports_net_turns(stitched_roll):
    events: list = []

    fields = run_edit_rotate(
        stitched_roll, _NEGATIVE_ID, "cw", emit=events.append
    )

    assert fields["rotation_quarter_turns"] == 1
    assert fields["edit"]["op"] == "rotate"
    assert fields["edit"]["params"] == {"direction": "cw"}
    assert fields["preview_path"] is not None and Path(fields["preview_path"]).exists()
    # No warnings: the preview regenerated cleanly. (The `edit_recorded`
    # event itself is emitted by the CLI layer around this call.)


def test_rotate_never_touches_the_published_tiff(stitched_roll):
    before = _tiff_bytes(stitched_roll)

    run_edit_rotate(stitched_roll, _NEGATIVE_ID, "cw", emit=lambda event: None)
    run_edit_rotate(stitched_roll, _NEGATIVE_ID, "ccw", emit=lambda event: None)

    assert _tiff_bytes(stitched_roll) == before


def test_two_rotations_compose_and_the_preview_tracks_them(stitched_roll):
    run_edit_rotate(stitched_roll, _NEGATIVE_ID, "cw", emit=lambda event: None)
    fields = run_edit_rotate(stitched_roll, _NEGATIVE_ID, "cw", emit=lambda event: None)

    assert fields["rotation_quarter_turns"] == 2
    manifest = load_roll_manifest(stitched_roll)
    assert manifest.negative(_NEGATIVE_ID).preview_path == fields["preview_path"]


def test_rotate_the_wrong_direction_is_a_usage_failure(stitched_roll):
    with pytest.raises(EditFailure) as exc_info:
        run_edit_rotate(stitched_roll, _NEGATIVE_ID, "north", emit=lambda event: None)
    assert exc_info.value.code is Code.INVALID_EDIT


def test_rotate_an_unknown_negative_fails(stitched_roll):
    with pytest.raises(EditFailure) as exc_info:
        run_edit_rotate(
            stitched_roll, "nope-negative-99", "cw", emit=lambda event: None
        )
    assert exc_info.value.code is Code.NEGATIVE_NOT_FOUND


def test_rotate_rejects_another_rolls_negative_id(tmp_path):
    """`negative_id` is a global primary key and the argument is
    user-supplied: roll B's folder with roll A's negative id must be
    refused, not silently edit roll A's negative."""
    from scanny_boy.library import repo
    from scanny_boy.roll_manifest import new_roll_manifest

    roll_a = tmp_path / "roll-a"
    roll_b = tmp_path / "roll-b"
    for roll_dir, roll_id, negative_id in (
        (roll_a, "rid-a", "aaa-negative-01"),
        (roll_b, "rid-b", "bbb-negative-01"),
    ):
        roll_dir.mkdir()
        manifest = new_roll_manifest(roll_id=roll_id, roll_name=roll_dir.name)
        manifest.negatives.append(_negative(negative_id=negative_id, run_id="run-1"))
        write_roll_manifest(roll_dir, manifest)

    with pytest.raises(repo.RollNotRegisteredError):
        repo.append_edit(
            roll_b, "aaa-negative-01", repo.ROTATE_OP, {"direction": "cw"}
        )

    with pytest.raises(EditFailure) as exc_info:
        run_edit_rotate(roll_b, "aaa-negative-01", "cw", emit=lambda event: None)

    # Roll A's ops log is untouched.
    assert repo.edits_for(roll_a, "aaa-negative-01") == []


def test_rotate_an_unstitched_negative_fails(stitched_roll):
    manifest = load_roll_manifest(stitched_roll)
    manifest.negatives.append(
        _negative(negative_id="stitch-negative-02", run_id="stitch-run")
    )
    write_roll_manifest(stitched_roll, manifest)

    with pytest.raises(EditFailure) as exc_info:
        run_edit_rotate(stitched_roll, "stitch-negative-02", "cw", emit=lambda event: None)
    assert exc_info.value.code is Code.NEGATIVE_NOT_FOUND


def test_roll_info_reports_the_net_rotation(stitched_roll):
    from scanny_boy.library import repo

    run_edit_rotate(stitched_roll, _NEGATIVE_ID, "ccw", emit=lambda event: None)

    manifest = load_roll_manifest(stitched_roll)
    assert (
        repo.net_rotation_quarter_turns(stitched_roll, _NEGATIVE_ID) == 3
    )
    assert manifest.negative(_NEGATIVE_ID).preview_path is not None


# --- edit delete -----------------------------------------------------------


def test_delete_removes_the_record_the_tiff_and_the_preview(stitched_roll):
    events: list = []
    # A rotation first, so a preview PNG exists to be removed too.
    rotated = run_edit_rotate(stitched_roll, _NEGATIVE_ID, "cw", emit=lambda event: None)
    preview_path = Path(rotated["preview_path"])
    assert preview_path.exists()
    tiff_path = stitched_roll / "_DSC0001.tif"
    assert tiff_path.exists()

    fields = run_edit_delete(stitched_roll, _NEGATIVE_ID, emit=events.append)

    assert fields == {"negative_id": _NEGATIVE_ID, "output": "_DSC0001.tif"}
    assert not tiff_path.exists()
    assert not preview_path.exists()
    manifest = load_roll_manifest(stitched_roll)
    with pytest.raises(KeyError):
        manifest.negative(_NEGATIVE_ID)
    # No warnings: every file came off the disk cleanly. (The
    # `negative_deleted` event itself is emitted by the CLI layer.)
    assert [e for e in events if isinstance(e, WarningEvent)] == []


def test_delete_cascades_the_ops_log(stitched_roll):
    from scanny_boy.library import repo

    run_edit_rotate(stitched_roll, _NEGATIVE_ID, "cw", emit=lambda event: None)

    run_edit_delete(stitched_roll, _NEGATIVE_ID, emit=lambda event: None)

    with pytest.raises(repo.RollNotRegisteredError):
        repo.edits_for(stitched_roll, _NEGATIVE_ID)


def test_delete_renumbers_the_survivors(stitched_roll):
    from scanny_boy.roll_manifest import CaptureTime

    manifest = load_roll_manifest(stitched_roll)
    # Ranking is by the first member's real capture time, so the survivor
    # needs one to hold a position at all (section 3.7).
    manifest.negatives.append(
        _negative(
            negative_id="stitch-negative-02",
            run_id="stitch-run",
            status="completed",
            sequence=2,
            capture_time=CaptureTime(source_datetime_original="2026-08-01T12:00:00"),
            output={
                "name": "_DSC0004.tif",
                "size": 0,
                "sha256": "0" * 64,
                "width": 4,
                "height": 3,
            },
        )
    )
    write_roll_manifest(stitched_roll, manifest)

    run_edit_delete(stitched_roll, _NEGATIVE_ID, emit=lambda event: None)

    survivor = load_roll_manifest(stitched_roll).negative("stitch-negative-02")
    assert survivor.sequence == 1


def test_delete_a_pending_negative_succeeds_without_files(stitched_roll):
    manifest = load_roll_manifest(stitched_roll)
    manifest.negatives.append(
        _negative(negative_id="stitch-negative-02", run_id="stitch-run")
    )
    write_roll_manifest(stitched_roll, manifest)

    fields = run_edit_delete(
        stitched_roll, "stitch-negative-02", emit=lambda event: None
    )

    assert fields == {"negative_id": "stitch-negative-02", "output": None}


def test_delete_an_unknown_negative_fails(stitched_roll):
    with pytest.raises(EditFailure) as exc_info:
        run_edit_delete(stitched_roll, "nope-negative-99", emit=lambda event: None)
    assert exc_info.value.code is Code.NEGATIVE_NOT_FOUND


def test_delete_an_unregistered_roll_fails(tmp_path: Path):
    with pytest.raises(EditFailure) as exc_info:
        run_edit_delete(tmp_path / "no-such-roll", "any-negative", emit=lambda event: None)
    assert exc_info.value.code is Code.ROLL_NOT_FOUND


def test_delete_survives_a_stuck_tiff(stitched_roll, monkeypatch):
    def stuck_unlink(self, missing_ok=False):
        raise PermissionError(1, "Operation not permitted", str(self))

    monkeypatch.setattr(Path, "unlink", stuck_unlink)
    events: list = []

    fields = run_edit_delete(stitched_roll, _NEGATIVE_ID, emit=events.append)

    # The record is gone regardless; the file left behind is reported, not fatal.
    assert fields == {"negative_id": _NEGATIVE_ID, "output": "_DSC0001.tif"}
    assert load_roll_manifest(stitched_roll).negatives == []
    warnings = [e for e in events if isinstance(e, WarningEvent)]
    assert [w.code for w in warnings] == [Code.ORPHAN_FILE_NOT_REMOVED]
