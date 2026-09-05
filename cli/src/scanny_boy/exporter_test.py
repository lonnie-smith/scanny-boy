"""Tests for `export`: the ops log replayed over real pixels, into a folder
of the user's choosing, with the roll's own TIFF untouched."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import tifffile

from scanny_boy.edits import run_edit_flip, run_edit_rotate
from scanny_boy.events import Code, ExportDone, WarningEvent
from scanny_boy.exporter import ExportFailure, apply_edits, run_export
from scanny_boy.roll_manifest import load_roll_manifest, write_roll_manifest
from scanny_boy.roll_manifest_test import _negative, _run
from scanny_boy.stitch_pipeline_test import _roll_dir

_NEGATIVE_ID = "stitch-negative-01"
_OTHER_ID = "stitch-negative-02"
_ORIGINAL = np.arange(12, dtype=np.uint16).reshape(3, 4)


@pytest.fixture()
def two_negative_roll_with_metadata(tmp_path: Path) -> Path:
    """A roll whose metadata is populated at both levels, ready to export:
    the roll carries the fallbacks and a capture date; the second negative
    overrides its capture date."""
    from scanny_boy.metadata_edit import run_metadata_set

    roll_dir = _roll_dir(tmp_path)
    manifest = load_roll_manifest(roll_dir)
    from scanny_boy.roll_manifest import CaptureTime, append_run

    append_run(manifest, _run(run_id="stitch-run", short_id="stitch"))
    for index, negative_id in enumerate((_NEGATIVE_ID, _OTHER_ID), start=1):
        manifest.negatives.append(
            _negative(
                negative_id=negative_id,
                run_id="stitch-run",
                status="completed",
                sequence=index,
                capture_time=CaptureTime(
                    source_datetime_original=f"2026-08-01T10:00:0{index}"
                ),
                output={
                    "name": f"_DSC000{index}.tif",
                    "size": 0,
                    "sha256": "0" * 64,
                    "width": 4,
                    "height": 3,
                },
            )
        )
    write_roll_manifest(roll_dir, manifest)
    tifffile.imwrite(roll_dir / "_DSC0001.tif", _ORIGINAL)
    tifffile.imwrite(roll_dir / "_DSC0002.tif", _ORIGINAL)
    run_metadata_set(
        roll_dir,
        {
            "roll": {
                "city": "Porto",
                "state": "Oregon",
                "camera": "Nikon F3",
                "lens": "50mm f/1.4",
                "caption": "harbor morning",
                "capture_date": "2026-08-01",
            },
            "negatives": {_OTHER_ID: {"capture_date": "2026-08-02"}},
        },
    )
    return roll_dir


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


def test_apply_edits_mirrors_before_rotating_when_flipped():
    """The canonical net transform is a horizontal mirror of the original
    followed by the quarter turns — the flip composes under the rotation."""
    mirrored = _ORIGINAL[:, ::-1]
    for k in range(4):
        np.testing.assert_array_equal(
            apply_edits(_ORIGINAL, k, True), np.rot90(mirrored, k=-k)
        )


def test_export_applies_the_recorded_flip(stitched_roll, tmp_path):
    run_edit_flip(stitched_roll, _NEGATIVE_ID, emit=lambda event: None)
    output_dir = tmp_path / "export"

    outcome = run_export(stitched_roll, output_dir, [], emit=lambda event: None)

    assert outcome.failed == []
    expected = _ORIGINAL[:, ::-1]
    np.testing.assert_array_equal(
        tifffile.imread(output_dir / "_DSC0001.tif"), expected
    )


def test_export_applies_a_flip_and_rotation_in_log_order(stitched_roll, tmp_path):
    """Flip then one cw turn is the mirrored image rotated clockwise —
    not the plain rotation of the original."""
    run_edit_flip(stitched_roll, _NEGATIVE_ID, emit=lambda event: None)
    run_edit_rotate(stitched_roll, _NEGATIVE_ID, "cw", emit=lambda event: None)
    output_dir = tmp_path / "export"

    outcome = run_export(stitched_roll, output_dir, [], emit=lambda event: None)

    assert outcome.failed == []
    expected = np.rot90(_ORIGINAL[:, ::-1], k=-1)
    assert tifffile.imread(output_dir / "_DSC0001.tif").shape == expected.shape
    np.testing.assert_array_equal(
        tifffile.imread(output_dir / "_DSC0001.tif"), expected
    )


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


def test_a_failed_write_leaves_no_tmp_file_in_the_output_folder(
    stitched_roll, tmp_path, monkeypatch
):
    """A failed imwrite (disk full, permissions) is a per-negative warning,
    but the partial `.tif.tmp` must not survive it in the user's folder."""
    import scanny_boy.exporter as exporter_module

    def failing_imwrite(path, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(exporter_module.tifffile, "imwrite", failing_imwrite)
    output_dir = tmp_path / "export"
    warnings: list = []

    outcome = run_export(stitched_roll, output_dir, [], emit=warnings.append)

    assert outcome.failed == [_NEGATIVE_ID, _OTHER_ID]
    assert [w.code for w in warnings if isinstance(w, WarningEvent)] == [
        Code.EXPORT_FAILED,
        Code.EXPORT_FAILED,
    ]
    assert list(output_dir.glob("*.tmp")) == []


# --- metadata written on export ---------------------------------------------


def _export_one(roll_dir: Path, tmp_path: Path) -> Path:
    output_dir = tmp_path / "out"
    outcome = run_export(roll_dir, output_dir, [], emit=lambda event: None)
    assert outcome.exported and not outcome.failed
    return output_dir / "_DSC0001.tif"


def _read_tags(path: Path) -> dict:
    import tifftools

    info = tifftools.read_tiff(str(path))
    return info["ifds"][0]["tags"]


def _exif_tag(tags: dict, code: int) -> str | None:
    from tifftools.constants import Tag

    exif = tags.get(Tag.ExifIFD.value)
    if exif is None:
        return None
    entry = exif["ifds"][0][0]["tags"].get(code)
    return entry["data"] if entry else None


def _xmp_text(tags: dict) -> str:
    data = tags[700]["data"]
    if isinstance(data, list):
        data = bytes(data).decode("utf-8")
    return data


def test_export_writes_roll_metadata(two_negative_roll_with_metadata, tmp_path):
    destination = _export_one(two_negative_roll_with_metadata, tmp_path)
    tags = _read_tags(destination)
    assert tags[272]["data"] == "Nikon F3"
    xmp = _xmp_text(tags)
    assert "photoshop:City>Porto<" in xmp
    assert "photoshop:State>Oregon<" in xmp
    assert "dc:description" in xmp and "harbor morning" in xmp
    assert _exif_tag(tags, 36867) == "2026:08:01 12:00:00"
    assert _exif_tag(tags, 42036) == "50mm f/1.4"


def test_export_negative_value_overrides_roll(tmp_path, two_negative_roll_with_metadata):
    from scanny_boy.metadata_edit import run_metadata_set

    run_metadata_set(
        two_negative_roll_with_metadata,
        {"negatives": {"stitch-negative-01": {"city": "Lisbon"}}},
    )
    output_dir = tmp_path / "out"
    outcome = run_export(
        two_negative_roll_with_metadata, output_dir, [], emit=lambda event: None
    )
    assert not outcome.failed
    first = _xmp_text(_read_tags(output_dir / "_DSC0001.tif"))
    second = _xmp_text(_read_tags(output_dir / "_DSC0002.tif"))
    assert "photoshop:City>Lisbon<" in first
    assert "photoshop:City>Porto<" in second


def test_export_without_metadata_writes_no_xmp(tmp_path, two_negative_roll_with_metadata):
    from scanny_boy.roll_manifest import (
        CaptureTime,
        load_roll_manifest,
        write_roll_manifest,
    )

    roll_dir = two_negative_roll_with_metadata
    manifest = load_roll_manifest(roll_dir)
    manifest.metadata = type(manifest.metadata)(roll_capture_date=None)
    for negative in manifest.negatives:
        negative.metadata = type(negative.metadata)()
        negative.capture_time = CaptureTime(
            source_datetime_original=negative.capture_time.source_datetime_original
        )
    write_roll_manifest(roll_dir, manifest)

    destination = _export_one(roll_dir, tmp_path)
    tags = _read_tags(destination)
    assert 700 not in tags
    from tifftools.constants import Tag

    assert Tag.ExifIFD.value not in tags