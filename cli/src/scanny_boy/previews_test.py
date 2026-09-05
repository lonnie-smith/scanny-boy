"""Tests for the preview cache: lossless quarter turns and mirrors go the
way labelled, and the 16→8-bit preview encode is decode-normalize-invert —
no gamma, a positive-looking display of the normalized-density negative."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from scanny_boy import normalization
from scanny_boy.previews import MAX_CODE, NORMALIZED_DISPLAY_LUT, transform_preview


def _write_preview(tmp_path: Path, image: np.ndarray) -> Path:
    path = tmp_path / "preview.png"
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    path.write_bytes(encoded.tobytes())
    return path


def test_rotate_preview_cw_is_clockwise(tmp_path):
    # np.rot90 is counter-clockwise, so a labelled-cw turn is k=3.
    image = np.arange(12, dtype=np.uint8).reshape(3, 4, 1)
    path = _write_preview(tmp_path, image)

    transform_preview(path, "cw")

    rotated = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    np.testing.assert_array_equal(rotated, np.rot90(image, k=3).squeeze())


def test_rotate_preview_ccw_is_counter_clockwise(tmp_path):
    image = np.arange(12, dtype=np.uint8).reshape(3, 4, 1)
    path = _write_preview(tmp_path, image)

    transform_preview(path, "ccw")

    rotated = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    np.testing.assert_array_equal(rotated, np.rot90(image, k=1).squeeze())


def test_flip_preview_mirrors_horizontally(tmp_path):
    image = np.arange(12, dtype=np.uint8).reshape(3, 4, 1)
    path = _write_preview(tmp_path, image)

    transform_preview(path, "flip")

    flipped = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    np.testing.assert_array_equal(flipped, image[:, ::-1].squeeze())


def test_display_lut_is_monotonic_and_spans_the_range():
    assert NORMALIZED_DISPLAY_LUT.shape == (MAX_CODE + 1,)
    assert int(NORMALIZED_DISPLAY_LUT[0]) == 255
    assert int(NORMALIZED_DISPLAY_LUT[MAX_CODE]) == 0
    # val = 0 is the scene highlight (dense, dark once inverted); val = 1
    # the scene shadow. 1 - val is therefore monotonically *de*creasing in
    # the code.
    assert np.all(np.diff(NORMALIZED_DISPLAY_LUT.astype(np.int32)) <= 0)


def test_display_lut_decodes_through_decode_normalized_with_no_gamma():
    """The LUT is exactly decode_normalized -> 1 - val -> 8-bit, bare
    scaling: log density is already roughly perceptually uniform, so no
    sRGB OETF is applied (docs/DECISIONS.md, "Normalization decisions")."""
    for code in (0, 1, 50, 2000, 8192, 32768, 65535):
        val = float(normalization.decode_normalized(np.array([code]))[0])
        expected = np.clip(1.0 - val, 0.0, 1.0)
        assert int(NORMALIZED_DISPLAY_LUT[code]) == round(expected * 255)


def test_display_lut_uncovered_canvas_renders_black():
    """Section 3.14: the fill sits at the thin end, so 1 - val takes it to
    zero — a preview's uncovered border is black without special-casing."""
    fill_code = int(
        normalization.encode_normalized(
            np.full((1, 1, 3), normalization.NORMALIZED_FILL, dtype=np.float32)
        )[0, 0, 0]
    )
    assert fill_code == 65535
    assert int(NORMALIZED_DISPLAY_LUT[fill_code]) == 0


def test_display_lut_midtone_is_near_half():
    """A mid-density negative (val near 0.5) previews near mid-grey, so the
    filmstrip is legible."""
    mid_code = int(
        normalization.encode_normalized(np.array([0.5], dtype=np.float32))[0]
    )
    grey = NORMALIZED_DISPLAY_LUT[mid_code]
    assert 100 <= int(grey) <= 130


# --- ensure_preview / sync_previews against a real roll ---------------------


def _roll_with_published_negative(tmp_path: Path, image: np.ndarray):
    """A registered roll holding one completed negative whose published TIFF
    is `image`. Returns `(roll_dir, manifest, negative)`."""
    import tifffile

    from scanny_boy.roll_manifest import (
        NegativeRecord,
        new_roll_manifest,
        write_roll_manifest,
    )

    roll_dir = tmp_path / "Roll"
    roll_dir.mkdir()
    manifest = new_roll_manifest(roll_id="rid-1", roll_name="Roll")
    negative = NegativeRecord(
        negative_id="rid-1-negative-01",
        run_id="run-1",
        members=["a.NEF", "b.NEF"],
        expected_output="out.tif",
        fill_color=(0, 0, 0),
        status="completed",
        output={"name": "out.tif", "size": 1, "sha256": "0" * 64},
    )
    manifest.negatives.append(negative)
    write_roll_manifest(roll_dir, manifest)
    tifffile.imwrite(roll_dir / "out.tif", image)
    return roll_dir, manifest, negative


def _expected_preview(
    image: np.ndarray, quarter_turns: int, flipped: bool = False
) -> np.ndarray:
    """What `generate_preview` writes: the 16→8-bit display encode, the net
    transform (mirror first, then rotation), then RGB→BGR for storage."""
    display = NORMALIZED_DISPLAY_LUT[image]
    if flipped:
        display = np.ascontiguousarray(display[:, ::-1])
    display = np.ascontiguousarray(np.rot90(display, k=(-quarter_turns) % 4))
    return cv2.cvtColor(display, cv2.COLOR_RGB2BGR)


def test_ensure_preview_regenerates_with_the_net_rotation(tmp_path):
    """A lost cache must not lose the edits: the published TIFF carries no
    rotation, so regenerating from it must apply the ops log's *net* turns,
    not just the turn that triggered the regeneration."""
    from scanny_boy import previews
    from scanny_boy.library import repo

    # An asymmetric image, so every quarter turn is distinguishable.
    image = np.repeat(np.arange(12, dtype=np.uint16).reshape(3, 4, 1), 3, axis=-1)
    image = (image * 3000).astype(np.uint16)
    roll_dir, _manifest, negative = _roll_with_published_negative(tmp_path, image)

    repo.append_edit(
        roll_dir, negative.negative_id, repo.ROTATE_OP, {"direction": "cw"}
    )
    repo.append_edit(
        roll_dir, negative.negative_id, repo.ROTATE_OP, {"direction": "cw"}
    )
    first = previews.ensure_preview(roll_dir, "rid-1", negative)
    first.unlink()  # the cache is lost

    repo.append_edit(
        roll_dir, negative.negative_id, repo.ROTATE_OP, {"direction": "cw"}
    )
    preview = previews.ensure_preview(roll_dir, "rid-1", negative, "cw")

    stored = cv2.imread(str(preview), cv2.IMREAD_UNCHANGED)
    np.testing.assert_array_equal(stored, _expected_preview(image, 3))


def test_ensure_preview_regenerates_with_the_net_flip(tmp_path):
    """Same rule for a flip: a lost cache regenerates from the published
    TIFF with the ops log's whole net transform — mirror included."""
    from scanny_boy import previews
    from scanny_boy.library import repo

    # An asymmetric image, so the mirror is distinguishable from any turn.
    image = np.repeat(np.arange(12, dtype=np.uint16).reshape(3, 4, 1), 3, axis=-1)
    image = (image * 3000).astype(np.uint16)
    roll_dir, _manifest, negative = _roll_with_published_negative(tmp_path, image)

    repo.append_edit(
        roll_dir, negative.negative_id, repo.ROTATE_OP, {"direction": "cw"}
    )
    preview = previews.ensure_preview(roll_dir, "rid-1", negative)
    preview.unlink()  # the cache is lost

    repo.append_edit(roll_dir, negative.negative_id, repo.FLIP_OP, {})
    preview = previews.ensure_preview(roll_dir, "rid-1", negative, "flip")

    stored = cv2.imread(str(preview), cv2.IMREAD_UNCHANGED)
    # flip applied to the already-rotated pixels: mirror of a 1-cw turn,
    # which nets to (mirror first, then 3 cw turns).
    np.testing.assert_array_equal(stored, _expected_preview(image, 3, flipped=True))


def test_sync_previews_regenerates_a_stale_preview_after_a_restitch(tmp_path):
    """A re-stitch adopts the negative — same id, same preview path, new
    TIFF — so the cached preview of the old pixels must be regenerated, with
    the net rotation still applied."""
    import tifffile

    from scanny_boy import previews
    from scanny_boy.library import repo

    image = np.repeat(np.arange(12, dtype=np.uint16).reshape(3, 4, 1), 3, axis=-1)
    image = (image * 3000).astype(np.uint16)
    roll_dir, manifest, negative = _roll_with_published_negative(tmp_path, image)

    previews.sync_previews(roll_dir, manifest)
    original = Path(negative.preview_path).read_bytes()
    repo.append_edit(
        roll_dir, negative.negative_id, repo.ROTATE_OP, {"direction": "cw"}
    )

    # The re-stitch replaces the published pixels under the same name.
    new_image = (image // 2).astype(np.uint16)
    tifffile.imwrite(roll_dir / "out.tif", new_image)
    previews.sync_previews(roll_dir, manifest, published_outputs=["out.tif"])

    stored = cv2.imread(str(negative.preview_path), cv2.IMREAD_UNCHANGED)
    np.testing.assert_array_equal(stored, _expected_preview(new_image, 1))
    assert Path(negative.preview_path).read_bytes() != original


def test_sync_previews_keeps_untouched_cached_previews(tmp_path):
    from scanny_boy import previews

    image = np.repeat(np.arange(12, dtype=np.uint16).reshape(3, 4, 1), 3, axis=-1)
    image = (image * 3000).astype(np.uint16)
    roll_dir, manifest, negative = _roll_with_published_negative(tmp_path, image)

    previews.sync_previews(roll_dir, manifest)
    original = Path(negative.preview_path).read_bytes()

    previews.sync_previews(roll_dir, manifest)

    assert Path(negative.preview_path).read_bytes() == original


# --- 1:1 region rendering ----------------------------------------------------


def _write_published_tiff(tmp_path: Path, image: np.ndarray) -> Path:
    """A compressed single-page TIFF written the way `write_base_tiff` does
    (Adobe Deflate + horizontal predictor), so the strip-level reader is
    exercised against the real storage layout."""
    import tifffile

    path = tmp_path / "out.tif"
    tifffile.imwrite(
        path,
        image,
        photometric="rgb",
        compression="deflate",
        predictor=True,
        maxworkers=1,
        metadata=None,
    )
    return path


def test_render_region_matches_full_decode_for_every_quarter_turn(tmp_path):
    """The region PNG is pixel-identical to slicing a full decode of the
    TIFF with the net rotation folded in — crop-then-rotate equals
    rotate-then-crop for axis-aligned rects."""
    import cv2

    from scanny_boy.previews import render_region

    # An asymmetric image, so every quarter turn is distinguishable.
    image = (np.arange(40 * 64 * 3, dtype=np.uint16).reshape(40, 64, 3) * 137) % 60000
    tiff_path = _write_published_tiff(tmp_path, image)

    for quarter_turns in range(4):
        display = np.ascontiguousarray(np.rot90(image, k=(-quarter_turns) % 4))
        display_h, display_w = display.shape[:2]
        cases = [
            (10, 5, 20, 12),
            (0, 0, display_w, display_h),
            (display_w - 7, display_h - 3, 7, 3),
        ]
        for x, y, w, h in cases:
            destination = tmp_path / "region.png"
            rect = render_region(
                tiff_path,
                x, y, w, h, quarter_turns=quarter_turns, destination=destination,
            )
            rx, ry, rw, rh = rect
            assert (rx, ry, rw, rh) == (x, y, w, h)
            stored = cv2.imread(str(destination), cv2.IMREAD_UNCHANGED)
            expected = cv2.cvtColor(
                NORMALIZED_DISPLAY_LUT[display[y : y + h, x : x + w]],
                cv2.COLOR_RGB2BGR,
            )
            np.testing.assert_array_equal(stored, expected)


def test_render_region_clamps_against_the_display_bounds(tmp_path):
    """A region hanging off the image's edges is clamped, in display space
    (which for odd net turns has swapped dimensions), and the returned rect
    is the intersection."""
    import cv2

    from scanny_boy.previews import render_region

    image = (np.arange(40 * 64 * 3, dtype=np.uint16).reshape(40, 64, 3) * 137) % 60000
    tiff_path = _write_published_tiff(tmp_path, image)

    # One cw turn: the display is 64 rows x 40 columns.
    destination = tmp_path / "region.png"
    rect = render_region(
        tiff_path, -10, -5, 64, 40, quarter_turns=1, destination=destination
    )
    assert rect == (0, 0, 40, 35)
    assert destination.exists()
    stored = cv2.imread(str(destination), cv2.IMREAD_UNCHANGED)
    assert stored.shape == (35, 40, 3)

    # The far corner, hanging off the right and bottom.
    rect = render_region(
        tiff_path, 33, 61, 100, 100, quarter_turns=1, destination=destination
    )
    assert rect == (33, 61, 7, 3)


def test_render_region_rejects_an_empty_region(tmp_path):
    import pytest

    from scanny_boy.previews import render_region

    image = (np.arange(40 * 64 * 3, dtype=np.uint16).reshape(40, 64, 3) * 137) % 60000
    tiff_path = _write_published_tiff(tmp_path, image)

    with pytest.raises(ValueError):
        render_region(tiff_path, 64, 40, 10, 10)
    with pytest.raises(ValueError):
        render_region(tiff_path, 0, 0, 0, 10)


def test_render_region_does_not_fall_back_to_full_decode(tmp_path, monkeypatch):
    """The strip-level reader is the point: if it ever starts falling back
    to a full `imread` on the real files, this test fails rather than the
    optimization silently disappearing."""
    import cv2
    import tifffile

    from scanny_boy.previews import render_region

    image = (np.arange(40 * 64 * 3, dtype=np.uint16).reshape(40, 64, 3) * 137) % 60000
    tiff_path = _write_published_tiff(tmp_path, image)
    tifffile.imread(tiff_path)  # sanity: the file reads

    def _boom(*args, **kwargs):
        raise AssertionError("render_region must not decode the full TIFF")

    monkeypatch.setattr(tifffile, "imread", _boom)
    destination = tmp_path / "region.png"
    rect = render_region(tiff_path, 10, 5, 20, 12, destination=destination)
    assert rect == (10, 5, 20, 12)
    stored = cv2.imread(str(destination), cv2.IMREAD_UNCHANGED)
    expected = cv2.cvtColor(
        NORMALIZED_DISPLAY_LUT[image[5:17, 10:30]],
        cv2.COLOR_RGB2BGR,
    )
    np.testing.assert_array_equal(stored, expected)
