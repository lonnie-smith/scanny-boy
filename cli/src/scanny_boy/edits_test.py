"""Tests for `edit rotate`: the ops log gets the op, the preview refreshes,
and the published TIFF is never touched."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import tifffile

from scanny_boy.edits import EditFailure, run_edit_rotate
from scanny_boy.events import Code
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
