"""Tests for `export`: the ops log replayed over real pixels, rendered as a
positive in Adobe RGB (or grey, for a mono roll), written as a lossless
JPEG XL into a folder of the user's choosing — with the roll's own TIFF
untouched."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import imagecodecs
import numpy as np
import pytest
import tifffile
import tifftools

from scanny_boy import jxl_writer, render
from scanny_boy.edits import run_edit_flip, run_edit_rotate, run_edit_tone
from scanny_boy.edits_test import _tone_params
from scanny_boy.events import Code, ExportDone, WarningEvent
from scanny_boy.exporter import (
    EXPORT_IMAGE_DESCRIPTION_SUFFIX,
    ExportFailure,
    applied_downsample,
    apply_edits,
    parse_downsample,
    run_export,
)
from scanny_boy.icc_profile import (
    EXPORT_GREY_PROFILE_SHA256,
    EXPORT_RGB_PROFILE_SHA256,
    ProfileKind,
)
from scanny_boy.jxl_writer_test import _box, _boxes, read_icc_profile
from scanny_boy.roll_manifest import (
    CameraColor,
    CaptureTime,
    append_run,
    load_roll_manifest,
    write_roll_manifest,
)
from scanny_boy.roll_manifest_test import _negative, _run
from scanny_boy.work_dir_support import make_roll_dir

_NEGATIVE_ID = "stitch-negative-01"
_OTHER_ID = "stitch-negative-02"
# Small enough to encode quickly, large enough that a rotation's geometry
# is unambiguous.
# Full-range codes so the render — and the tone curve on top of it — has
# something to act on (a narrow block of dense codes would render all-white
# and hide a dropped tone op).
_ORIGINAL = np.linspace(0, 65535, 12).astype(np.uint16).reshape(3, 4)
_ORIGINAL_RGB = np.stack(
    [
        np.linspace(0, 65535, 12).astype(np.uint16).reshape(3, 4),
        np.linspace(65535, 0, 12).astype(np.uint16).reshape(3, 4),
        np.linspace(10000, 55000, 12).astype(np.uint16).reshape(3, 4),
    ],
    axis=-1,
)


def _profile_sha(destination: Path) -> str:
    return hashlib.sha256(read_icc_profile(destination.read_bytes())).hexdigest()


def _decode(path: Path) -> np.ndarray:
    return imagecodecs.jpegxl_decode(path.read_bytes())


_MATRIX = CameraColor(
    rgb_xyz_matrix=(
        (0.7, 0.2, 0.1),
        (0.1, 0.75, 0.15),
        (0.05, 0.1, 0.85),
    ),
    source="libraw",
    camera_model="NIKON Z 7",
)


@pytest.fixture()
def two_negative_roll_with_metadata(tmp_path: Path) -> Path:
    """A roll whose metadata is populated at both levels, ready to export:
    the roll carries the fallbacks and a capture date; the second negative
    overrides its capture date."""
    from scanny_boy.metadata_edit import run_metadata_set

    roll_dir = make_roll_dir(tmp_path)
    manifest = load_roll_manifest(roll_dir)

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
    """A mono-shaped roll (2-D published TIFFs, no camera_color): the
    state every real roll was in before the colour-managed export, and
    the state a mono roll is in by design — exports must succeed."""
    roll_dir = make_roll_dir(tmp_path)
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


@pytest.fixture()
def colour_roll(tmp_path: Path) -> Path:
    """A colour roll: 3-channel published TIFFs and a recorded
    `camera_color` block — the state a first-class export needs."""
    from scanny_boy.manifest import SourceRecord
    from scanny_boy.roll_manifest import append_run, merge_sources

    roll_dir = make_roll_dir(tmp_path)
    manifest = load_roll_manifest(roll_dir)
    append_run(manifest, _run(run_id="stitch-run", short_id="stitch"))
    merge_sources(
        manifest,
        [SourceRecord(filename="a.NEF", absolute_path="/x", size=1, mtime=1.0, sha256="a" * 64)],
        "stitch-run",
    )
    for index, negative_id in enumerate((_NEGATIVE_ID, _OTHER_ID), start=1):
        manifest.negatives.append(
            _negative(
                negative_id=negative_id,
                run_id="stitch-run",
                status="completed",
                sequence=index,
                normalization={"normalize": {"format_version": 2}},
                output={
                    "name": f"_DSC000{index}.tif",
                    "size": 0,
                    "sha256": "0" * 64,
                    "width": 4,
                    "height": 3,
                },
            )
        )
    manifest.camera_color = _MATRIX
    write_roll_manifest(roll_dir, manifest)
    tifffile.imwrite(roll_dir / "_DSC0001.tif", _ORIGINAL_RGB)
    tifffile.imwrite(roll_dir / "_DSC0002.tif", _ORIGINAL_RGB)
    return roll_dir


def _export(roll_dir: Path, tmp_path: Path, **kwargs) -> Path:
    output_dir = tmp_path / "export"
    outcome = run_export(roll_dir, output_dir, [_NEGATIVE_ID], emit=lambda event: None, **kwargs)
    assert outcome.failed == []
    assert outcome.exported == ["_DSC0001.jxl"]
    return output_dir / "_DSC0001.jxl"


# --- the geometric replay (unchanged, now composed with the render) --------


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


def test_apply_edits_applies_the_fine_rotation_and_its_fill():
    """The fine auto-rotation keeps the canvas dimensions and fills what it
    uncovers with the stitching fill sentinel — the same empty-pixel
    semantics the stitched canvas already has."""
    from scanny_boy.normalization import NORMALIZED_FILL, encode_normalized
    from scanny_boy.previews import NORMALIZED_DISPLAY_LUT

    fill_code = encode_normalized(
        np.full((1, 1, 3), NORMALIZED_FILL, dtype=np.float32)
    )[0, 0]

    image = np.zeros((40, 60), dtype=np.uint16)
    rotated = apply_edits(image, 0, False, 45.0)

    assert rotated.shape == (40, 60)
    assert rotated[0, 0] == fill_code[0]
    assert rotated[20, 30] == 0

    image_rgb = np.zeros((40, 60, 3), dtype=np.uint16)
    rotated_rgb = apply_edits(image_rgb, 0, False, 45.0)

    np.testing.assert_array_equal(rotated_rgb[0, 0], fill_code)
    np.testing.assert_array_equal(NORMALIZED_DISPLAY_LUT[rotated_rgb[0, 0]], 0)


def test_apply_edits_zero_fine_angle_changes_nothing():
    np.testing.assert_array_equal(apply_edits(_ORIGINAL, 0, False, 0.0), _ORIGINAL)


# --- the export is a rendered positive, as a JPEG XL -----------------------


def test_the_export_is_a_jxl_named_after_the_negative(stitched_roll, tmp_path):
    output_dir = tmp_path / "export"
    events: list = []

    outcome = run_export(stitched_roll, output_dir, [], emit=events.append)

    assert outcome.exported == ["_DSC0001.jxl", "_DSC0003.jxl"]
    assert not list(output_dir.glob("*.tif"))
    done = [e for e in events if isinstance(e, ExportDone)]
    assert [e.output for e in done] == ["_DSC0001.jxl", "_DSC0003.jxl"]
    assert all(e.width == 4 and e.height == 3 for e in done)


def test_the_export_renders_the_flat_positive(stitched_roll, tmp_path):
    """The export is no longer the published TIFF's codes: it is the
    render's flat positive — the preview's `1 - val` look at 16 bits."""
    destination = _export(stitched_roll, tmp_path)
    rendered = _decode(destination)

    flat, _ = render.render_export(_ORIGINAL, None, None)
    np.testing.assert_array_equal(rendered, flat)
    # And the flat look: dense codes go bright, and more dense (larger
    # code) is strictly darker.
    assert rendered[0, 0] > 65000
    assert rendered[-1, -1] < rendered[0, 0]


def test_the_export_embeds_the_grey_profile_for_a_mono_roll(stitched_roll, tmp_path):
    destination = _export(stitched_roll, tmp_path)
    assert _profile_sha(destination) == EXPORT_GREY_PROFILE_SHA256


def test_the_colour_export_is_3_channel_and_embeds_the_export_rgb_profile(
    colour_roll, tmp_path
):
    destination = _export(colour_roll, tmp_path)
    rendered = _decode(destination)
    assert rendered.ndim == 3 and rendered.shape[2] == 3
    assert _profile_sha(destination) == EXPORT_RGB_PROFILE_SHA256


def test_a_colour_roll_without_camera_color_fails_before_writing_anything(
    tmp_path,
):
    """A colour roll predating the colour-managed export fails the
    export outright — once, with `CAMERA_MATRIX_MISSING`, before any file
    is written. Never an identity-matrix fallback."""
    from scanny_boy.manifest import SourceRecord
    from scanny_boy.roll_manifest import append_run, merge_sources

    roll_dir = make_roll_dir(tmp_path)
    manifest = load_roll_manifest(roll_dir)
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
    write_roll_manifest(roll_dir, manifest)
    tifffile.imwrite(roll_dir / "_DSC0001.tif", _ORIGINAL_RGB)
    output_dir = tmp_path / "export"
    output_dir.mkdir()

    with pytest.raises(ExportFailure) as exc_info:
        run_export(roll_dir, output_dir, [], emit=lambda event: None)

    assert exc_info.value.code is Code.CAMERA_MATRIX_MISSING
    assert list(output_dir.iterdir()) == []


def test_a_mono_roll_without_camera_color_exports_successfully(stitched_roll, tmp_path):
    """The matrix check is conditional on the published TIFF's
    channel count — a mono roll needs no matrix and is never failed for
    its absence."""
    outcome = run_export(stitched_roll, tmp_path / "export", [], emit=lambda event: None)
    assert outcome.failed == []
    assert outcome.exported == ["_DSC0001.jxl", "_DSC0003.jxl"]


# --- the tone op is baked in --------------------------------------------


def test_the_tone_op_changes_the_exported_pixels_and_matches_the_curve(
    stitched_roll, tmp_path
):
    run_edit_tone(
        stitched_roll, _NEGATIVE_ID, _tone_params(70.0, 0.4), emit=lambda event: None
    )
    destination = _export(stitched_roll, tmp_path)
    rendered = _decode(destination)

    flat, _ = render.render_export(_ORIGINAL, None, None)
    toned, _ = render.render_export(_ORIGINAL, None, {"grade_r": 70.0, "snap_gamma": 0.4})
    assert not np.array_equal(rendered, flat)
    np.testing.assert_array_equal(rendered, toned)


def test_a_negative_without_a_tone_op_exports_the_flat_look(colour_roll, tmp_path):
    """`tone_params is None` means the identity ramp — the flat look the
    preview shows today, the correct default and not a placeholder."""
    destination = _export(colour_roll, tmp_path)
    rendered = _decode(destination)
    flat, _ = render.render_export(_ORIGINAL_RGB, render.export_matrix(
        _MATRIX.rgb_xyz_matrix
    ), None)
    np.testing.assert_array_equal(rendered, flat)


# --- the geometric replay, end to end ---------------------------------------


def test_export_applies_the_recorded_flip(stitched_roll, tmp_path):
    run_edit_flip(stitched_roll, _NEGATIVE_ID, emit=lambda event: None)

    destination = _export(stitched_roll, tmp_path)

    expected, _ = render.render_export(_ORIGINAL[:, ::-1], None, None)
    np.testing.assert_array_equal(_decode(destination), expected)


def test_export_applies_the_seeded_fine_rotation(stitched_roll, tmp_path):
    """The stitch stage's auto-seeded `rotate_fine` op reaches the exported
    pixels through the same net-state replay as the user ops."""
    from scanny_boy.library import repo

    repo.append_edit(
        stitched_roll,
        _NEGATIVE_ID,
        repo.ROTATE_FINE_OP,
        {"angle_deg": 45.0, "source": "auto"},
    )

    destination = _export(stitched_roll, tmp_path)

    expected, _ = render.render_export(apply_edits(_ORIGINAL, 0, False, 45.0), None, None)
    np.testing.assert_array_equal(_decode(destination), expected)


def test_export_applies_a_flip_and_rotation_in_log_order(stitched_roll, tmp_path):
    """Flip then one cw turn is the mirrored image rotated clockwise —
    not the plain rotation of the original."""
    run_edit_flip(stitched_roll, _NEGATIVE_ID, emit=lambda event: None)
    run_edit_rotate(stitched_roll, _NEGATIVE_ID, "cw", emit=lambda event: None)

    destination = _export(stitched_roll, tmp_path)

    expected = np.rot90(_ORIGINAL[:, ::-1], k=-1)
    assert _decode(destination).shape == expected.shape
    expected_rendered, _ = render.render_export(expected, None, None)
    np.testing.assert_array_equal(_decode(destination), expected_rendered)


def test_export_applies_the_recorded_rotation(stitched_roll, tmp_path):
    run_edit_rotate(stitched_roll, _NEGATIVE_ID, "cw", emit=lambda event: None)
    output_dir = tmp_path / "export"

    outcome = run_export(stitched_roll, output_dir, [], emit=lambda event: None)

    assert outcome.failed == []
    assert outcome.exported == ["_DSC0001.jxl", "_DSC0003.jxl"]
    # One cw turn of the 3x4 arange: 90 degrees clockwise.
    expected, _ = render.render_export(np.rot90(_ORIGINAL, k=-1), None, None)
    np.testing.assert_array_equal(_decode(output_dir / "_DSC0001.jxl"), expected)
    # The negative without edits exports the flat render of the same dims.
    unchanged, _ = render.render_export(_ORIGINAL, None, None)
    np.testing.assert_array_equal(_decode(output_dir / "_DSC0003.jxl"), unchanged)


def test_export_leaves_the_rolls_own_tiff_untouched(stitched_roll, tmp_path):
    before = (stitched_roll / "_DSC0001.tif").read_bytes()
    run_edit_rotate(stitched_roll, _NEGATIVE_ID, "cw", emit=lambda event: None)

    run_export(stitched_roll, tmp_path / "export", [], emit=lambda event: None)

    assert (stitched_roll / "_DSC0001.tif").read_bytes() == before


# --- mechanics ---------------------------------------------------------------


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

    assert outcome.exported == ["_DSC0001.jxl"]
    assert not (tmp_path / "export" / "_DSC0003.jxl").exists()


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
    assert outcome.exported == ["_DSC0001.jxl", "_DSC0003.jxl"]
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
    """A failed encode (disk full, permissions) is a per-negative warning,
    but the partial `.jxl.tmp` must not survive it in the user's folder."""
    def failing_encode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(jxl_writer, "encode_jxl", failing_encode)
    output_dir = tmp_path / "export"
    warnings: list = []

    outcome = run_export(stitched_roll, output_dir, [], emit=warnings.append)

    assert outcome.failed == [_NEGATIVE_ID, _OTHER_ID]
    assert [w.code for w in warnings if isinstance(w, WarningEvent)] == [
        Code.EXPORT_FAILED,
        Code.EXPORT_FAILED,
    ]
    assert list(output_dir.glob("*.tmp")) == []


def test_an_unavailable_encoder_stops_the_export_with_the_dedicated_code(
    stitched_roll, tmp_path, monkeypatch
):
    """libjxl missing is a packaging failure, not a user error —
    one `JXL_ENCODER_UNAVAILABLE` error, not a per-negative warning."""

    def unavailable(*args, **kwargs):
        raise jxl_writer.JxlEncoderUnavailable("the bundled libjxl is missing")

    monkeypatch.setattr(jxl_writer, "encode_jxl", unavailable)
    output_dir = tmp_path / "export"

    with pytest.raises(ExportFailure) as exc_info:
        run_export(stitched_roll, output_dir, [], emit=lambda event: None)

    assert exc_info.value.code is Code.JXL_ENCODER_UNAVAILABLE
    assert "libjxl" in exc_info.value.message


# --- metadata written on export ----------------------------------------------

# The box payloads: the Exif box is a TIFF stream (parse it with
# tifftools over a BytesIO), the XMP box is the packet bytes.


def _export_one(roll_dir: Path, tmp_path: Path) -> Path:
    return _export(roll_dir, tmp_path)


def _read_box_tags(destination: Path) -> dict:
    """The Exif box's IFD0 tag dict, parsed back out of the written file."""
    exif_box = _box(_boxes(destination.read_bytes()), b"Exif")
    # libjxl prefixes the payload with the 4-byte big-endian offset to the
    # TIFF header; 0 puts it at the start.
    payload = exif_box[4:]
    info = tifftools.read_tiff(io.BytesIO(payload))
    return info["ifds"][0]["tags"]


def _xmp_text(destination: Path) -> str:
    return _box(_boxes(destination.read_bytes()), b"xml ").decode("utf-8")


def _exif_tag(tags: dict, code: int) -> str | None:
    from tifftools.constants import Tag

    exif = tags.get(Tag.ExifIFD.value)
    if exif is None:
        return None
    entry = exif["ifds"][0][0]["tags"].get(code)
    return entry["data"] if entry else None


def _provenance(destination: Path) -> dict:
    """The `scannyboy:provenance` JSON out of the XMP packet."""
    xmp = _xmp_text(destination)
    start = xmp.index("<scannyboy:provenance>")
    end = xmp.index("</scannyboy:provenance>")
    return json.loads(xmp[start + len("<scannyboy:provenance>") : end])


def test_export_writes_roll_metadata(two_negative_roll_with_metadata, tmp_path):
    destination = _export_one(two_negative_roll_with_metadata, tmp_path)
    tags = _read_box_tags(destination)
    assert tags[272]["data"] == "Nikon F3"
    xmp = _xmp_text(destination)
    assert "photoshop:City>Porto<" in xmp
    assert "photoshop:State>Oregon<" in xmp
    assert "dc:description" in xmp and "harbor morning" in xmp
    assert _exif_tag(tags, 36867) == "2026:08:01 12:00:00"
    assert _exif_tag(tags, 42036) == "50mm f/1.4"
    # The ImageDescription is the short human string.
    assert tags[270]["data"] == f"{_NEGATIVE_ID}{EXPORT_IMAGE_DESCRIPTION_SUFFIX}"


def test_export_negative_value_overrides_roll(tmp_path, two_negative_roll_with_metadata):
    from scanny_boy.metadata_edit import run_metadata_set

    run_metadata_set(
        two_negative_roll_with_metadata,
        {"negatives": {"stitch-negative-01": {"city": "Lisbon"}}},
    )
    output_dir = tmp_path / "export"
    outcome = run_export(
        two_negative_roll_with_metadata, output_dir, [], emit=lambda event: None
    )
    assert not outcome.failed
    first = _xmp_text(output_dir / "_DSC0001.jxl")
    second = _xmp_text(output_dir / "_DSC0002.jxl")
    assert "photoshop:City>Lisbon<" in first
    assert "photoshop:City>Porto<" in second


def test_export_without_metadata_writes_no_exif_box_but_still_the_provenance(
    tmp_path, two_negative_roll_with_metadata
):
    """A field nobody set writes nothing — no Exif box at all when
    no metadata field is set. The XMP still goes, because the provenance
    record is not user-set metadata but the file's interpretability
    record."""
    from scanny_boy.roll_manifest import load_roll_manifest, write_roll_manifest

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

    types = [name for name, _ in _boxes(destination.read_bytes())]
    assert b"Exif" not in types
    assert b"xml " in types
    assert "scannyboy:provenance" in _xmp_text(destination)


# --- the provenance record ----------------------------------------------------


def test_the_provenance_round_trips_with_the_matrix_and_tone(colour_roll, tmp_path):
    run_edit_tone(
        colour_roll, _NEGATIVE_ID, _tone_params(115.0, 0.2), emit=lambda event: None
    )
    destination = _export_one(colour_roll, tmp_path)

    record = _provenance(destination)

    assert record["kind"] == "scanny-boy export"
    assert record["negative_id"] == _NEGATIVE_ID
    assert record["rendered"]["profile"]["name"] == "ScannyBoy-Export-AdobeRGB-v1.icc"
    assert record["rendered"]["gamma"] == pytest.approx(render.GAMMA_ADOBE)
    assert record["rendered"]["matrix"] is not None
    assert record["rendered"]["tone"] == _tone_params(115.0, 0.2)
    # The synthetic RGB's channels are far from neutral, so the gamut clip
    # does real work here; assert the record's shape, and that it is in
    # [0, 1] per channel (render_test pins the in-gamut zero case).
    fractions = record["rendered"]["clip_fractions"]
    assert len(fractions) == 3
    assert all(0.0 <= f <= 1.0 for f in fractions)


def test_the_provenance_carries_the_published_tiffs_normalization(
    colour_roll, tmp_path
):
    """The normalization block still rides along — describing the
    *published* TIFF's encoding, which the export no longer is."""
    destination = _export_one(colour_roll, tmp_path)

    record = _provenance(destination)

    assert record["normalization"] == {"normalize": {"format_version": 2}}


def test_a_mono_provenance_has_no_matrix(stitched_roll, tmp_path):
    destination = _export_one(stitched_roll, tmp_path)

    record = _provenance(destination)

    assert record["rendered"]["matrix"] is None
    assert record["rendered"]["profile"]["name"] == "ScannyBoy-Export-Grey-v1.icc"
    assert record["rendered"]["clip_fractions"] == [0.0]


def test_the_provenance_names_the_profile_kind_constant_mapping(tmp_path):
    """The exporter selects the profile through `export_profile_kind` —
    pin the mapping here too."""
    from scanny_boy.icc_profile import export_profile_kind

    assert export_profile_kind(1) is ProfileKind.EXPORT_GREY
    assert export_profile_kind(3) is ProfileKind.EXPORT_RGB


# --- the spots repair reaches the export ---------------------------------


def _planted_original() -> np.ndarray:
    """The ramp with a dark blob planted at TIFF (1, 1)-(2, 2) — small
    enough that a hand-written spot mask covers it exactly."""
    planted = _ORIGINAL.copy()
    planted[1:3, 1:3] = planted[1:3, 1:3] // 4
    return planted


def _hand_spots_params(repair: bool = True, rejected: bool = False) -> dict:
    from scanny_boy import spots

    return spots.spots_params(
        canvas=(4, 3),  # (width, height) of the 3x4 published TIFF
        spots=[
            {
                "id": 1,
                "kind": "blob",
                "polarity": "dense",
                "bbox": [1, 1, 2, 2],
                "rle": [0, 2, 2],
                "area": 2,
                "score": 10.0,
                "rejected": rejected,
            }
        ],
        sensitivity=0.5,
        repair=repair,
    )


def test_the_export_applies_the_repair_before_the_geometry(stitched_roll, tmp_path):
    """A rotate plus a repair compose in the right order: the repair reads
    TIFF-space coordinates first, the rotation moves the healed pixels."""
    import tifffile

    from scanny_boy import spots
    from scanny_boy.library import repo

    planted = _planted_original()
    tifffile.imwrite(stitched_roll / "_DSC0001.tif", planted)
    repo.append_spots_edit(
        stitched_roll, _NEGATIVE_ID, _hand_spots_params(repair=True)
    )
    repo.append_edit(stitched_roll, _NEGATIVE_ID, repo.ROTATE_OP, {"direction": "cw"})

    destination = _export(stitched_roll, tmp_path)

    repaired = spots.apply_repair(planted, _hand_spots_params(repair=True))
    expected, _ = render.render_export(np.rot90(repaired, k=-1), None, None)
    np.testing.assert_array_equal(_decode(destination), expected)


def test_the_provenance_record_names_the_repair_count(stitched_roll, tmp_path):
    from scanny_boy.library import repo

    repo.append_spots_edit(
        stitched_roll, _NEGATIVE_ID, _hand_spots_params(repair=True)
    )
    destination = _export(stitched_roll, tmp_path)

    record = _provenance(destination)
    assert record["rendered"]["spots"] == {
        "detector_version": 1,
        "sensitivity": 0.5,
        "repaired": 1,
    }


def test_the_provenance_record_is_null_without_a_live_repair(stitched_roll, tmp_path):
    repo_module = __import__("scanny_boy.library.repo", fromlist=["repo"])
    repo_module.append_spots_edit(
        stitched_roll, _NEGATIVE_ID, _hand_spots_params(repair=False)
    )
    destination = _export(stitched_roll, tmp_path)

    record = _provenance(destination)
    assert record["rendered"]["spots"] is None


# --- the crop op reaches the export --------------------------------------


@pytest.fixture()
def croppable_export_roll(tmp_path: Path) -> Path:
    """One completed mono negative with a 40x64 published TIFF — big
    enough for a crop above the 16px floor."""
    roll_dir = make_roll_dir(tmp_path)
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
                "width": 64,
                "height": 40,
            },
        )
    )
    write_roll_manifest(roll_dir, manifest)
    image = (np.arange(40 * 64, dtype=np.uint16).reshape(40, 64) * 700) % 60000
    tifffile.imwrite(roll_dir / "_DSC0001.tif", image)
    return roll_dir


def test_apply_edits_applies_the_crop_window():
    """The crop is the replay's first geometric step — the exported frame
    is the crop window's content, its dimensions the window's."""
    from scanny_boy.previews import apply_crop

    crop = {
        "canvas": [_ORIGINAL.shape[1], _ORIGINAL.shape[0]],
        "x": 1,
        "y": 0,
        "w": _ORIGINAL.shape[1] - 1,
        "h": _ORIGINAL.shape[0],
        "tilt_deg": 0.0,
    }
    np.testing.assert_array_equal(
        apply_edits(_ORIGINAL, 0, False, 0.0, crop), apply_crop(_ORIGINAL, crop)
    )


def test_the_export_bakes_the_crop_and_leaves_the_tiff_alone(
    croppable_export_roll, tmp_path
):
    """The exported JXL holds only the crop window's pixels; the published
    TIFF beside it stays byte-identical."""
    from scanny_boy.library import repo
    from scanny_boy.previews import apply_crop
    from scanny_boy.render import render_export

    crop = {
        "canvas": [64, 40],
        "x": 8,
        "y": 6,
        "w": 32,
        "h": 20,
        "tilt_deg": 0.0,
    }
    repo.append_edit(croppable_export_roll, _NEGATIVE_ID, repo.CROP_OP, crop)

    tiff_path = croppable_export_roll / "_DSC0001.tif"
    tiff_before = tiff_path.read_bytes()
    image = tifffile.imread(tiff_path)

    destination = _export(croppable_export_roll, tmp_path)
    assert tiff_path.read_bytes() == tiff_before
    rendered = _decode(destination)
    assert rendered.shape == (20, 32)
    expected, _ = render_export(apply_crop(image, crop), None, None)
    np.testing.assert_array_equal(rendered, expected)


def test_the_export_provenance_records_the_crop(croppable_export_roll, tmp_path):
    """The XMP's `rendered.crop` names the window the exported frame was
    taken from — the published TIFF beside the export still holds the full
    frame, and the record says which part of it this file is."""
    from scanny_boy.exporter import provenance_record
    from scanny_boy.library import repo

    roll_dir = croppable_export_roll
    crop = {
        "canvas": [64, 40],
        "x": 8,
        "y": 6,
        "w": 32,
        "h": 20,
        "tilt_deg": 2.5,
        "preset": "645",
    }
    repo.append_edit(roll_dir, _NEGATIVE_ID, repo.CROP_OP, crop)

    _export(roll_dir, tmp_path)
    record = provenance_record(
        load_roll_manifest(roll_dir).negative(_NEGATIVE_ID),
        None,
        None,
        None,
        ProfileKind.EXPORT_GREY,
        (0.0,),
        None,
        crop,
    )
    assert record["rendered"]["crop"] == {
        "x": 8,
        "y": 6,
        "width": 32,
        "height": 20,
        "tilt_deg": 2.5,
        "preset": "645",
    }
    assert provenance_record(
        load_roll_manifest(roll_dir).negative(_NEGATIVE_ID),
        None,
        None,
        None,
        ProfileKind.EXPORT_GREY,
        (0.0,),
        None,
        None,
    )["rendered"]["crop"] is None


# --- downsampling ------------------------------------------------------------


def test_applied_downsample_reports_the_target_only_when_it_fits():
    assert applied_downsample(_ORIGINAL, 2) == 2  # the 3x4 source exceeds it
    assert applied_downsample(_ORIGINAL, 4) is None  # the long edge, exactly
    assert applied_downsample(_ORIGINAL, 6048) is None  # larger: never upscale
    assert applied_downsample(_ORIGINAL, None) is None


def test_parse_downsample_maps_the_choices():
    assert parse_downsample("none") is None
    assert parse_downsample("6048") == 6048
    assert parse_downsample("9072") == 9072
    assert parse_downsample("12096") == 12096


def test_parse_downsample_rejects_an_unknown_value():
    with pytest.raises(ValueError):
        parse_downsample("12000")


def test_the_export_downsamples_the_rendered_pixels(stitched_roll, tmp_path):
    output_dir = tmp_path / "export"
    events: list = []

    outcome = run_export(
        stitched_roll, output_dir, [], downsample=2, emit=events.append
    )

    assert outcome.failed == []
    done = [e for e in events if isinstance(e, ExportDone)]
    assert all(e.width == 2 and e.height == 2 for e in done)
    expected, _ = render.render_export(_ORIGINAL, None, None, long_edge=2)
    np.testing.assert_array_equal(_decode(output_dir / "_DSC0001.jxl"), expected)


def test_the_export_downsamples_inside_the_render_with_a_tone_op(
    stitched_roll, tmp_path
):
    """The resize is part of the render: the tone op is baked in and the
    downsample runs on the same chain's linear stage, so the exported
    pixels are the toned render's downsample, one computation."""
    run_edit_tone(stitched_roll, _NEGATIVE_ID, _tone_params(70.0, 0.4), emit=lambda event: None)

    output_dir = tmp_path / "export"
    run_export(stitched_roll, output_dir, [_NEGATIVE_ID], downsample=2, emit=lambda event: None)

    expected, _ = render.render_export(
        _ORIGINAL, None, {"grade_r": 70.0, "snap_gamma": 0.4}, long_edge=2
    )
    np.testing.assert_array_equal(_decode(output_dir / "_DSC0001.jxl"), expected)


def test_the_colour_export_downsamples_all_three_channels(colour_roll, tmp_path):
    output_dir = tmp_path / "export"

    run_export(colour_roll, output_dir, [_NEGATIVE_ID], downsample=2, emit=lambda event: None)

    rendered = _decode(output_dir / "_DSC0001.jxl")
    assert rendered.shape == (2, 2, 3)
    expected, _ = render.render_export(
        _ORIGINAL_RGB,
        render.export_matrix(_MATRIX.rgb_xyz_matrix),
        None,
        long_edge=2,
    )
    np.testing.assert_array_equal(rendered, expected)


def test_the_downsample_averages_linear_light_not_display_codes(
    stitched_roll, tmp_path
):
    """A black/white step resized to 3/4 scale: the boundary row mixes the
    two halves in *linear light*, so its display code sits near 0.5 linear
    re-encoded (47818 of 65535) — decisively above the 32767 a resampler
    handed gamma-encoded display pixels would produce."""
    import tifffile

    codes = np.zeros((4, 2), dtype=np.uint16)  # white = code 0
    codes[2:4, :] = 65535  # black
    tifffile.imwrite(stitched_roll / "_DSC0001.tif", codes)

    output_dir = tmp_path / "export"
    run_export(stitched_roll, output_dir, [_NEGATIVE_ID], downsample=3, emit=lambda event: None)

    rendered = _decode(output_dir / "_DSC0001.jxl")
    assert rendered.shape == (3, 2)
    boundary = rendered[1, 0]
    linear_midpoint = 0.5 ** (1.0 / render.GAMMA_ADOBE) * 65535
    assert abs(boundary - linear_midpoint) < abs(boundary - 32767.5)


def test_a_constant_field_survives_the_downsample_exactly(stitched_roll, tmp_path):
    """Normalized kernel weights + edge clamping: a flat field downsamples
    to the same flat value, band boundaries and all."""
    import tifffile

    codes = np.full((8, 6), 20000, dtype=np.uint16)
    tifffile.imwrite(stitched_roll / "_DSC0001.tif", codes)

    output_dir = tmp_path / "export"
    run_export(stitched_roll, output_dir, [_NEGATIVE_ID], downsample=4, emit=lambda event: None)

    rendered = _decode(output_dir / "_DSC0001.jxl")
    assert rendered.shape == (4, 3)
    assert np.all(rendered == rendered[0, 0])


def test_an_export_target_above_the_long_edge_is_skipped_silently(
    stitched_roll, tmp_path
):
    """Never an upscale: a 6048 target on a 3x4 source writes the
    full-resolution pixels, and the provenance records no downsample."""
    destination = _export(stitched_roll, tmp_path, downsample=6048)

    rendered = _decode(destination)
    assert rendered.shape == (3, 4)
    full, _ = render.render_export(_ORIGINAL, None, None)
    np.testing.assert_array_equal(rendered, full)
    assert _provenance(destination)["rendered"]["downsample"] is None


def test_the_provenance_records_the_applied_downsample(stitched_roll, tmp_path):
    destination = _export(stitched_roll, tmp_path, downsample=2)

    record = _provenance(destination)
    assert record["rendered"]["downsample"] == {"long_edge": 2}


def test_the_provenance_has_no_downsample_by_default(colour_roll, tmp_path):
    destination = _export_one(colour_roll, tmp_path)

    record = _provenance(destination)
    assert record["rendered"]["downsample"] is None


def test_the_color_op_changes_the_export_and_is_recorded_in_provenance(
    colour_roll, tmp_path
):
    from scanny_boy.edits import run_edit_color
    from scanny_boy.edits_test import _color_params
    from scanny_boy.library import repo

    flat = _decode(_export(colour_roll, tmp_path / "flat"))
    params = _color_params(wb_magenta=0.15, dye_separation=1.3)
    run_edit_color(colour_roll, _NEGATIVE_ID, params, emit=lambda event: None)
    tinted_dest = _export(colour_roll, tmp_path / "tinted")

    assert not np.array_equal(flat, _decode(tinted_dest))
    record = _provenance(tinted_dest)
    assert record["rendered"]["color"] == repo.net_edit_state(
        colour_roll, _NEGATIVE_ID
    ).color
