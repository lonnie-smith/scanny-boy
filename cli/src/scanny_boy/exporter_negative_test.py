"""Tests for `export --mode negative` (the stitched negative: normalization
undone, not inverted, only geometry and crop applied) and `--format tiff`
(both modes as 16-bit TIFF)."""

from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path

import imagecodecs
import numpy as np
import pytest
import tifffile
import tifftools
from tifftools.constants import Tag

from scanny_boy import normalization, render
from scanny_boy.exporter import (
    FORMAT_TIFF,
    MODE_NEGATIVE,
    export_filename,
    run_export,
)
from scanny_boy.icc_profile import EXPORT_GREY_PROFILE_SHA256, EXPORT_RGB_PROFILE_SHA256
from scanny_boy.library import repo
from scanny_boy.manifest import SourceRecord
from scanny_boy.previews import apply_crop
from scanny_boy.roll_manifest import (
    CameraColor,
    NegativeRecord,
    RunRecord,
    append_run,
    load_roll_manifest,
    merge_sources,
    write_roll_manifest,
)
from scanny_boy.work_dir_support import make_roll_dir

_NEGATIVE_ID = "stitch-negative-01"
_HEIGHT, _WIDTH = 40, 64
_FLOORS_RGB = [-1.9, -1.6, -1.2]
_CEILS_RGB = [-0.35, -0.3, -0.25]
_FLOORS_MONO = [-1.8]
_CEILS_MONO = [-0.3]

_MATRIX = CameraColor(
    rgb_xyz_matrix=(
        (0.7, 0.2, 0.1),
        (0.1, 0.75, 0.15),
        (0.05, 0.1, 0.85),
    ),
    source="libraw",
    camera_model="NIKON Z 7",
)


def _codes(channels: int) -> np.ndarray:
    base = (
        np.arange(_HEIGHT * _WIDTH, dtype=np.uint32).reshape(_HEIGHT, _WIDTH) * 700
    ) % 60000
    if channels == 1:
        return base.astype(np.uint16)
    return np.stack(
        [(base + 5000 * ch) % 65000 for ch in range(channels)], axis=-1
    ).astype(np.uint16)


def _make_roll(tmp_path: Path, channels: int) -> Path:
    roll_dir = make_roll_dir(tmp_path)
    manifest = load_roll_manifest(roll_dir)
    append_run(
        manifest,
        RunRecord(
            run_id="stitch-run",
            short_id="stitch",
            kind="stitch",
            status="complete",
            started_at="2026-08-02T00:00:00Z",
            convert_run_id="convert-1",
            input_folder=None,
            source_order=["a.NEF"],
            work_dir="/tmp/work",
            finished_at="2026-08-02T00:10:00Z",
        ),
    )
    merge_sources(
        manifest,
        [
            SourceRecord(
                filename="a.NEF", absolute_path="/x", size=1, mtime=1.0, sha256="a" * 64
            )
        ],
        "stitch-run",
    )
    floors, ceils = (
        (_FLOORS_MONO, _CEILS_MONO) if channels == 1 else (_FLOORS_RGB, _CEILS_RGB)
    )
    manifest.negatives.append(
        NegativeRecord(
            negative_id=_NEGATIVE_ID,
            run_id="stitch-run",
            members=["a.NEF"],
            expected_output="_DSC0001.tif",
            fill_color=(0, 0, 0),
            status="completed",
            sequence=1,
            normalization={"floors": floors, "ceils": ceils},
            output={
                "name": "_DSC0001.tif",
                "size": 0,
                "sha256": "0" * 64,
                "width": _WIDTH,
                "height": _HEIGHT,
            },
        )
    )
    if channels == 3:
        manifest.camera_color = _MATRIX
    write_roll_manifest(roll_dir, manifest)
    tifffile.imwrite(roll_dir / "_DSC0001.tif", _codes(channels))
    return roll_dir


@pytest.fixture()
def colour_roll(tmp_path: Path) -> Path:
    return _make_roll(tmp_path, 3)


@pytest.fixture()
def mono_roll(tmp_path: Path) -> Path:
    return _make_roll(tmp_path, 1)


def _export(roll_dir: Path, tmp_path: Path, expected: str, **kwargs) -> Path:
    output_dir = tmp_path / "export"
    outcome = run_export(
        roll_dir, output_dir, [_NEGATIVE_ID], emit=lambda event: None, **kwargs
    )
    assert outcome.failed == []
    assert outcome.exported == [expected]
    return output_dir / expected


def _expected_negative(codes: np.ndarray, floors, ceils, matrix) -> np.ndarray:
    linear = 10.0 ** (
        np.asarray(floors)
        + normalization.decode_normalized(codes).astype(np.float64)[
            ..., None if codes.ndim == 2 else slice(None)
        ]
        * (np.asarray(ceils) - np.asarray(floors))
    )
    if codes.ndim == 2:
        linear = linear[..., 0]
    if matrix is not None:
        linear = linear @ np.asarray(matrix).T
    linear = np.clip(linear, 0.0, 1.0)
    return np.rint(linear ** (1.0 / render.GAMMA_ADOBE) * 65535).astype(np.uint16)


def _tiff_xmp_provenance(path: Path) -> dict:
    with tifffile.TiffFile(path) as tif:
        xmp = bytes(tif.pages[0].tags[700].value).decode("utf-8")
    start = xmp.index("<scannyboy:provenance>") + len("<scannyboy:provenance>")
    end = xmp.index("</scannyboy:provenance>")
    return json.loads(html.unescape(xmp[start:end]))


# --- naming ------------------------------------------------------------------


def test_export_filename_marks_negatives_and_picks_the_extension():
    assert export_filename("_DSC0001.tif", "positive", "jxl") == "_DSC0001.jxl"
    assert export_filename("_DSC0001.tif", "positive", "tiff") == "_DSC0001.tif"
    assert export_filename("_DSC0001.tif", "negative", "jxl") == "_DSC0001-negative.jxl"
    assert (
        export_filename("_DSC0001.tif", "negative", "tiff") == "_DSC0001-negative.tif"
    )
    with pytest.raises(ValueError):
        export_filename("_DSC0001.tif", "sideways", "jxl")


# --- denormalization ---------------------------------------------------------


def test_denormalize_inverts_the_stitch_encode():
    rng = np.random.default_rng(3)
    linear = rng.uniform(0.02, 0.6, size=(16, 16, 3)).astype(np.float32)
    bounds = normalization.Bounds(
        floors=tuple(float(v) for v in np.log10(linear.reshape(-1, 3).min(axis=0))),
        ceils=tuple(float(v) for v in np.log10(linear.reshape(-1, 3).max(axis=0))),
    )
    codes = normalization.encode_normalized(
        normalization.normalize_log_image(normalization.to_log_density(linear), bounds)
    )
    recovered = normalization.denormalize_to_linear(codes, bounds.floors, bounds.ceils)
    np.testing.assert_allclose(recovered, linear, rtol=2e-4)


def test_denormalize_rejects_a_bound_count_mismatch():
    with pytest.raises(ValueError):
        normalization.denormalize_to_linear(
            np.zeros((2, 2, 3), dtype=np.uint16), [0.0], [1.0]
        )


# --- the stitched negative ---------------------------------------------------


def test_negative_export_is_the_denormalized_crop_in_adobe_rgb(colour_roll, tmp_path):
    crop = {
        "canvas": [_WIDTH, _HEIGHT],
        "x": 8,
        "y": 6,
        "w": 32,
        "h": 20,
        "tilt_deg": 0.0,
    }
    repo.append_edit(colour_roll, _NEGATIVE_ID, repo.CROP_OP, crop)
    destination = _export(
        colour_roll, tmp_path, "_DSC0001-negative.jxl", mode=MODE_NEGATIVE
    )
    pixels = imagecodecs.jpegxl_decode(destination.read_bytes())
    assert pixels.shape == (20, 32, 3)
    matrix = render.export_matrix(_MATRIX.rgb_xyz_matrix)
    expected = _expected_negative(
        apply_crop(_codes(3), crop), _FLOORS_RGB, _CEILS_RGB, matrix
    )
    assert np.abs(pixels.astype(np.int32) - expected.astype(np.int32)).max() <= 1


def test_negative_export_is_not_inverted(colour_roll, tmp_path):
    """Thin codes (the film base end) stay bright and dense codes stay
    dark: the file is a negative."""
    destination = _export(
        colour_roll, tmp_path, "_DSC0001-negative.jxl", mode=MODE_NEGATIVE
    )
    pixels = imagecodecs.jpegxl_decode(destination.read_bytes()).astype(np.int64)
    codes = _codes(3).astype(np.int64)
    luma_pixels = pixels.sum(axis=-1).ravel()
    luma_codes = codes.sum(axis=-1).ravel()
    assert np.corrcoef(luma_pixels, luma_codes)[0, 1] > 0.9


def test_negative_export_ignores_tone_colour_and_heal_ops(colour_roll, tmp_path):
    plain = imagecodecs.jpegxl_decode(
        _export(
            colour_roll, tmp_path / "a", "_DSC0001-negative.jxl", mode=MODE_NEGATIVE
        ).read_bytes()
    )
    repo.append_edit(colour_roll, _NEGATIVE_ID, repo.TONE_OP, {"grade_r": 40.0})
    repo.append_edit(colour_roll, _NEGATIVE_ID, repo.COLOR_OP, {"warmth": 0.5})
    edited = imagecodecs.jpegxl_decode(
        _export(
            colour_roll, tmp_path / "b", "_DSC0001-negative.jxl", mode=MODE_NEGATIVE
        ).read_bytes()
    )
    np.testing.assert_array_equal(plain, edited)


def test_negative_export_applies_rotation(colour_roll, tmp_path):
    repo.append_edit(colour_roll, _NEGATIVE_ID, repo.ROTATE_OP, {"direction": "cw"})
    destination = _export(
        colour_roll, tmp_path, "_DSC0001-negative.jxl", mode=MODE_NEGATIVE
    )
    pixels = imagecodecs.jpegxl_decode(destination.read_bytes())
    assert pixels.shape == (_WIDTH, _HEIGHT, 3)


def test_negative_export_provenance_says_what_was_done(colour_roll, tmp_path):
    destination = _export(
        colour_roll,
        tmp_path,
        "_DSC0001-negative.tif",
        mode=MODE_NEGATIVE,
        fmt=FORMAT_TIFF,
    )
    record = _tiff_xmp_provenance(destination)
    assert record["mode"] == "negative"
    assert record["format"] == "tiff"
    rendered = record["rendered"]
    assert rendered["inverted"] is False
    assert rendered["denormalized"] == {"floors": _FLOORS_RGB, "ceils": _CEILS_RGB}
    for absent in ("tone", "color", "spots", "scratches", "deband", "highlight_lock"):
        assert absent not in rendered


def test_mono_negative_export_is_grey(mono_roll, tmp_path):
    destination = _export(
        mono_roll,
        tmp_path,
        "_DSC0001-negative.tif",
        mode=MODE_NEGATIVE,
        fmt=FORMAT_TIFF,
    )
    with tifffile.TiffFile(destination) as tif:
        page = tif.pages[0]
        pixels = page.asarray()
        icc = bytes(page.tags["InterColorProfile"].value)
    assert pixels.shape == (_HEIGHT, _WIDTH)
    assert hashlib.sha256(icc).hexdigest() == EXPORT_GREY_PROFILE_SHA256
    expected = _expected_negative(_codes(1), _FLOORS_MONO, _CEILS_MONO, None)
    assert np.abs(pixels.astype(np.int32) - expected.astype(np.int32)).max() <= 1


def test_a_negative_without_bounds_fails_that_negative(colour_roll, tmp_path):
    manifest = load_roll_manifest(colour_roll)
    manifest.negatives[0].normalization = {}
    write_roll_manifest(colour_roll, manifest)
    outcome = run_export(
        colour_roll,
        tmp_path / "export",
        [_NEGATIVE_ID],
        mode=MODE_NEGATIVE,
        emit=lambda event: None,
    )
    assert outcome.failed == [_NEGATIVE_ID]


# --- TIFF format -------------------------------------------------------------


def test_positive_tiff_matches_the_positive_jxl(colour_roll, tmp_path):
    jxl = imagecodecs.jpegxl_decode(
        _export(colour_roll, tmp_path / "a", "_DSC0001.jxl").read_bytes()
    )
    tiff_path = _export(colour_roll, tmp_path / "b", "_DSC0001.tif", fmt=FORMAT_TIFF)
    with tifffile.TiffFile(tiff_path) as tif:
        page = tif.pages[0]
        pixels = page.asarray()
        icc = bytes(page.tags["InterColorProfile"].value)
        assert page.dtype == np.uint16
        assert page.compression == 32946
    np.testing.assert_array_equal(pixels, jxl)
    assert hashlib.sha256(icc).hexdigest() == EXPORT_RGB_PROFILE_SHA256
    record = _tiff_xmp_provenance(tiff_path)
    assert record["mode"] == "positive"
    assert record["format"] == "tiff"
    assert not list((tmp_path / "b" / "export").glob("*.tmp"))


def test_tiff_export_carries_the_exif_ifd(colour_roll, tmp_path):
    from scanny_boy.metadata_edit import run_metadata_set

    run_metadata_set(
        colour_roll,
        {
            "roll": {
                "camera": "Nikon F3",
                "lens": "50mm f/1.4",
                "capture_date": "2026-08-01",
            }
        },
    )
    destination = _export(colour_roll, tmp_path, "_DSC0001.tif", fmt=FORMAT_TIFF)
    info = tifftools.read_tiff(str(destination))
    tags = info["ifds"][0]["tags"]
    assert tags[272]["data"] == "Nikon F3"
    exif = tags[Tag.ExifIFD.value]["ifds"][0][0]["tags"]
    assert exif[42036]["data"] == "50mm f/1.4"
