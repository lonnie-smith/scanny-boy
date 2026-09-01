"""Tests for `export`: the ops log replayed over real pixels, into a folder
of the user's choosing, with the roll's own TIFF untouched."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import tifffile

from scanny_boy.edits import run_edit_rotate
from scanny_boy.events import Code, ExportDone
from scanny_boy.exporter import ExportFailure, apply_edits, run_export
from scanny_boy.roll_manifest import load_roll_manifest, write_roll_manifest
from scanny_boy.roll_manifest_test import _negative, _run
from scanny_boy.stitch_pipeline_test import _roll_dir

_NEGATIVE_ID = "stitch-negative-01"
_OTHER_ID = "stitch-negative-02"
_ORIGINAL = np.arange(12, dtype=np.uint16).reshape(3, 4)


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
    manifest.negatives.append(
        _negative(
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
    )
    manifest.negatives.append(
        _negative(
            negative_id=_OTHER_ID,
            run_id="stitch-run",
            status="completed",
            sequence=2,
            output={
                "name": "_DSC0003.tif",
                "size": 0,
                "sha256": "0" * 64,
                "width": 4,
                "height": 3,
            },
        )
    )
    write_roll_manifest(roll_dir, manifest)
    tifffile.imwrite(roll_dir / "_DSC0001.tif", _ORIGINAL)
    tifffile.imwrite(roll_dir / "_DSC0003.tif", _ORIGINAL)
    return roll_dir


def test_apply_edits_matches_clockwise_quarter_turns():
    # Quarter turns count clockwise; np.rot90 is counter-clockwise.
    for k in range(4):
        np.testing.assert_array_equal(apply_edits(_ORIGINAL, k), np.rot90(_ORIGINAL, k=-k))


def test_export_applies_the_recorded_rotation(stitched_roll, tmp_path):
    run_edit_rotate(stitched_roll, _NEGATIVE_ID, "cw", emit=lambda event: None)
    output_dir = tmp_path / "export"

    outcome = run_export(stitched_roll, output_dir, [], emit=lambda event: None)

    assert outcome.failed == []
    assert outcome.exported == ["_DSC0001.tif", "_DSC0003.tif"]
    # One cw turn of the 3x4 arange: 90 degrees clockwise.
    expected = np.rot90(_ORIGINAL, k=-1)
    np.testing.assert_array_equal(
        tifffile.imread(output_dir / "_DSC0001.tif"), expected
    )
    # The negative without edits exports unchanged pixels, same dimensions.
    np.testing.assert_array_equal(
        tifffile.imread(output_dir / "_DSC0003.tif"), _ORIGINAL
    )


def test_export_leaves_the_rolls_own_tiff_untouched(stitched_roll, tmp_path):
    before = (stitched_roll / "_DSC0001.tif").read_bytes()
    run_edit_rotate(stitched_roll, _NEGATIVE_ID, "cw", emit=lambda event: None)

    run_export(stitched_roll, tmp_path / "export", [], emit=lambda event: None)

    assert (stitched_roll / "_DSC0001.tif").read_bytes() == before


def test_export_emits_export_done_per_negative(stitched_roll, tmp_path):
    events: list = []

    run_export(stitched_roll, tmp_path / "export", [], emit=events.append)

    done = [e for e in events if isinstance(e, ExportDone)]
    assert {e.negative_id for e in done} == {_NEGATIVE_ID, _OTHER_ID}
    # Neither negative has edits in this test: dimensions pass through.
    assert all(e.width == 4 and e.height == 3 for e in done)


def test_export_selection_exports_only_the_named_negatives(stitched_roll, tmp_path):
    outcome = run_export(
        stitched_roll,
        tmp_path / "export",
        [_NEGATIVE_ID],
        emit=lambda event: None,
    )

    assert outcome.exported == ["_DSC0001.tif"]
    assert not (tmp_path / "export" / "_DSC0003.tif").exists()


def test_export_of_an_unknown_negative_fails(stitched_roll, tmp_path):
    with pytest.raises(ExportFailure) as exc_info:
        run_export(stitched_roll, tmp_path / "export", ["nope"], emit=lambda e: None)
    assert exc_info.value.code is Code.NEGATIVE_NOT_FOUND


def test_export_skips_an_unstitched_negative_without_stopping(stitched_roll, tmp_path):
    manifest = load_roll_manifest(stitched_roll)
    manifest.negatives.append(
        _negative(negative_id="stitch-negative-03", run_id="stitch-run")
    )
    write_roll_manifest(stitched_roll, manifest)
    events: list = []

    outcome = run_export(stitched_roll, tmp_path / "export", [], emit=events.append)

    assert outcome.failed == ["stitch-negative-03"]
    assert outcome.exported == ["_DSC0001.tif", "_DSC0003.tif"]
    assert any(
        e.code is Code.NEGATIVE_NOT_FOUND
        for e in events
        if hasattr(e, "code")
    )


def test_export_of_an_unregistered_roll_fails(tmp_path):
    with pytest.raises(ExportFailure) as exc_info:
        run_export(tmp_path / "not-a-roll", tmp_path / "export", [], emit=lambda e: None)
    assert exc_info.value.code is Code.ROLL_NOT_FOUND
