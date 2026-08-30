from pathlib import Path

import numpy as np
import pytest
import tifffile
import tifftools
from tifftools.constants import Tag

from scanny_boy import hashing
from scanny_boy.apply_metadata import ApplyMetadataFailure, run_apply_metadata
from scanny_boy.events import Code, MetadataApplied, MetadataSkipped
from scanny_boy.roll_manifest import load_roll_manifest, write_roll_manifest
from scanny_boy.stitch_pipeline_test import _make_work_dir, _roll_dir, _stitch
from scanny_boy.tiff_exif import DATE_TIME_ORIGINAL, SUBSEC_TIME_ORIGINAL

_INTENDED = "2026-01-15T09:30:00.250000"

_IFD0_TAGS_TO_CHECK = [
    "Make",
    "Model",
    "Software",
    "ImageDescription",
    "Orientation",
    "Compression",
    "Predictor",
]


def _stitched_roll(tmp_path: Path, *, negatives: int = 1) -> Path:
    """A real roll holding `negatives` genuinely stitched TIFFs -- built
    through `stitch_pipeline_test.py`'s own real-Phase-1-intermediates
    fixtures, so `apply-metadata` operates on a file it could actually see
    in production, not a hand-crafted stand-in."""
    work_dir = _make_work_dir(tmp_path, negatives=negatives)
    out_dir = _roll_dir(tmp_path)
    outcome = _stitch(work_dir, out_dir)
    assert outcome.status == "complete"
    return out_dir


def _mark_dirty(roll_dir: Path, negative_id: str, intended: str = _INTENDED) -> None:
    roll = load_roll_manifest(roll_dir)
    roll.negative(negative_id).capture_time.intended_datetime_original = intended
    write_roll_manifest(roll_dir, roll)


def _ifd0_snapshot(path: Path) -> tuple[dict, bytes]:
    with tifffile.TiffFile(path) as handle:
        page = handle.pages[0]
        snapshot = {
            name: page.tags[name].value for name in _IFD0_TAGS_TO_CHECK if name in page.tags
        }
        icc = page.tags["InterColorProfile"].value
    return snapshot, icc


def _exif_tags(path: Path) -> dict:
    info = tifftools.read_tiff(str(path))
    return info["ifds"][0]["tags"][Tag.ExifIFD.value]["ifds"][0][0]["tags"]


def test_applies_intended_time_and_rehashes(tmp_path):
    roll_dir = _stitched_roll(tmp_path)
    negative_id = load_roll_manifest(roll_dir).negatives[0].negative_id
    _mark_dirty(roll_dir, negative_id)

    before = load_roll_manifest(roll_dir).negative(negative_id)
    tiff_path = roll_dir / before.output["name"]
    original_sha256 = before.output["sha256"]

    events: list = []
    outcome = run_apply_metadata(roll_dir, emit=events.append)

    assert outcome.applied == [negative_id]
    assert outcome.skipped == []

    after = load_roll_manifest(roll_dir).negative(negative_id)
    assert after.capture_time.applied_datetime_original == _INTENDED
    assert after.output["sha256"] != original_sha256
    assert after.output["sha256"] == hashing.sha256_file(tiff_path)
    assert after.output["size"] == tiff_path.stat().st_size

    applied_events = [e for e in events if isinstance(e, MetadataApplied)]
    assert [e.negative_id for e in applied_events] == [negative_id]

    exif = _exif_tags(tiff_path)
    assert exif[DATE_TIME_ORIGINAL]["data"] == "2026:01:15 09:30:00"
    assert exif[SUBSEC_TIME_ORIGINAL]["data"] == "25"


def test_other_tags_and_icc_profile_are_unchanged(tmp_path):
    roll_dir = _stitched_roll(tmp_path)
    negative_id = load_roll_manifest(roll_dir).negatives[0].negative_id
    tiff_path = roll_dir / load_roll_manifest(roll_dir).negative(negative_id).output["name"]

    before_ifd0, before_icc = _ifd0_snapshot(tiff_path)
    before_exif = _exif_tags(tiff_path)

    _mark_dirty(roll_dir, negative_id)
    run_apply_metadata(roll_dir, emit=lambda e: None)

    after_ifd0, after_icc = _ifd0_snapshot(tiff_path)
    after_exif = _exif_tags(tiff_path)

    assert after_ifd0 == before_ifd0
    assert after_icc == before_icc

    touched = {DATE_TIME_ORIGINAL, SUBSEC_TIME_ORIGINAL}
    assert set(before_exif) - touched == set(after_exif) - touched
    for code in set(before_exif) - touched:
        assert after_exif[code] == before_exif[code]


def test_pixel_data_is_byte_identical_after_apply(tmp_path):
    roll_dir = _stitched_roll(tmp_path)
    negative_id = load_roll_manifest(roll_dir).negatives[0].negative_id
    tiff_path = roll_dir / load_roll_manifest(roll_dir).negative(negative_id).output["name"]

    before = tifffile.imread(tiff_path)

    _mark_dirty(roll_dir, negative_id)
    run_apply_metadata(roll_dir, emit=lambda e: None)

    after = tifffile.imread(tiff_path)
    assert np.array_equal(before, after)


def test_externally_modified_tiff_is_skipped_and_named(tmp_path):
    roll_dir = _stitched_roll(tmp_path)
    negative_id = load_roll_manifest(roll_dir).negatives[0].negative_id
    _mark_dirty(roll_dir, negative_id)

    tiff_path = roll_dir / load_roll_manifest(roll_dir).negative(negative_id).output["name"]
    tiff_path.write_bytes(tiff_path.read_bytes() + b"\x00")

    events: list = []
    outcome = run_apply_metadata(roll_dir, emit=events.append)

    assert outcome.applied == []
    assert outcome.skipped == [negative_id]

    skipped_events = [e for e in events if isinstance(e, MetadataSkipped)]
    assert len(skipped_events) == 1
    assert skipped_events[0].negative_id == negative_id
    assert skipped_events[0].code is Code.OUTPUT_MODIFIED_EXTERNALLY

    # Never rewrite a file the roll no longer recognises.
    unchanged = load_roll_manifest(roll_dir).negative(negative_id)
    assert unchanged.capture_time.applied_datetime_original is None


def test_skip_does_not_block_other_negatives(tmp_path):
    roll_dir = _stitched_roll(tmp_path, negatives=2)
    roll = load_roll_manifest(roll_dir)
    bad, good = roll.negatives[0], roll.negatives[1]
    _mark_dirty(roll_dir, bad.negative_id)
    _mark_dirty(roll_dir, good.negative_id)

    bad_path = roll_dir / bad.output["name"]
    bad_path.write_bytes(bad_path.read_bytes() + b"\x00")

    events: list = []
    outcome = run_apply_metadata(roll_dir, emit=events.append)

    assert outcome.applied == [good.negative_id]
    assert outcome.skipped == [bad.negative_id]
    assert load_roll_manifest(roll_dir).negative(good.negative_id).capture_time.applied_datetime_original == _INTENDED


def test_clean_negatives_are_not_rewritten(tmp_path):
    roll_dir = _stitched_roll(tmp_path, negatives=2)
    roll = load_roll_manifest(roll_dir)
    dirty, clean = roll.negatives[0], roll.negatives[1]
    _mark_dirty(roll_dir, dirty.negative_id)

    clean_path = roll_dir / clean.output["name"]
    original_bytes = clean_path.read_bytes()

    events: list = []
    outcome = run_apply_metadata(roll_dir, emit=events.append)

    assert outcome.applied == [dirty.negative_id]
    assert clean.negative_id not in outcome.applied
    assert clean.negative_id not in outcome.skipped
    assert clean_path.read_bytes() == original_bytes
    assert not any(getattr(e, "negative_id", None) == clean.negative_id for e in events)


def test_roll_not_found_raises(tmp_path):
    with pytest.raises(ApplyMetadataFailure) as exc_info:
        run_apply_metadata(tmp_path / "nope", emit=lambda e: None)
    assert exc_info.value.code is Code.ROLL_NOT_FOUND
