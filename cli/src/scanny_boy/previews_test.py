"""Tests for the preview cache: lossless quarter turns and mirrors go the
way labelled, and the 16→8-bit preview encode is decode-normalize-invert —
no gamma, a positive-looking display of the normalized-density negative."""

from __future__ import annotations

import struct
from pathlib import Path

import cv2
import numpy as np
import pytest

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
    sRGB OETF is applied."""
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


# --- the negative display mode ----------------------------------------------


def test_negative_display_lut_is_the_uninverted_density():
    """The negative view is decode_normalized straight to 8-bit — no
    inversion, no gamma: the published TIFF's own appearance, what a
    densitometer sees. A denser code (closer to the scene's shadow) reads
    brighter, opposite to the positive LUT."""
    from scanny_boy.previews import NEGATIVE_DISPLAY_LUT

    assert NEGATIVE_DISPLAY_LUT.shape == (MAX_CODE + 1,)
    assert int(NEGATIVE_DISPLAY_LUT[0]) == 0
    assert int(NEGATIVE_DISPLAY_LUT[MAX_CODE]) == 255
    for code in (0, 1, 50, 2000, 8192, 32768, 65535):
        val = float(normalization.decode_normalized(np.array([code]))[0])
        assert int(NEGATIVE_DISPLAY_LUT[code]) == round(np.clip(val, 0.0, 1.0) * 255)
    # Monotonically *increasing* in the code, and the un-inverted mirror of
    # the positive view.
    assert np.all(np.diff(NEGATIVE_DISPLAY_LUT.astype(np.int32)) >= 0)
    for code in (0, 2000, 32768, 65535):
        assert int(NEGATIVE_DISPLAY_LUT[code]) + int(NORMALIZED_DISPLAY_LUT[code]) in (
            255,
            254,
        )


def test_render_region_negative_mode_encodes_without_inversion(tmp_path):
    """`render_region(mode="negative")` is the same rect through the
    un-inverted LUT — the positive view inverted back, byte for byte, and
    the tone adjustment never reaches it even when one is named."""
    import cv2

    from scanny_boy.previews import NEGATIVE_DISPLAY_LUT, render_region

    image = (np.arange(40 * 64 * 3, dtype=np.uint16).reshape(40, 64, 3) * 137) % 60000
    tiff_path = _write_published_tiff(tmp_path, image)

    destination = tmp_path / "region.png"
    rect = render_region(
        tiff_path,
        10, 5, 20, 12,
        tone_params={"grade_r": 160.0, "snap_gamma": 0.3},
        destination=destination,
        mode="negative",
    )
    assert rect == (10, 5, 20, 12)
    stored = cv2.imread(str(destination), cv2.IMREAD_UNCHANGED)
    expected = cv2.cvtColor(
        NEGATIVE_DISPLAY_LUT[image[5:17, 10:30]],
        cv2.COLOR_RGB2BGR,
    )
    np.testing.assert_array_equal(stored, expected)


def test_render_region_rejects_an_unknown_mode(tmp_path):
    import pytest

    from scanny_boy.previews import render_region

    image = (np.arange(40 * 64 * 3, dtype=np.uint16).reshape(40, 64, 3) * 137) % 60000
    tiff_path = _write_published_tiff(tmp_path, image)

    with pytest.raises(ValueError):
        render_region(tiff_path, 0, 0, 10, 10, mode="grayscale")


def test_render_preview_matches_the_display_encode_and_folds_the_transform(tmp_path):
    """`render_preview` is `generate_preview`'s pixel content written to a
    caller-named path: the net transform replayed (mirror, then rotation),
    then the 16→8-bit encode of the chosen mode — no inversion in the
    negative mode — with no downscale when the image is small enough."""
    import cv2

    from scanny_boy.previews import NEGATIVE_DISPLAY_LUT, render_preview

    image = (np.arange(40 * 64 * 3, dtype=np.uint16).reshape(40, 64, 3) * 137) % 60000
    tiff_path = _write_published_tiff(tmp_path, image)

    for mode, lut in (("positive", NORMALIZED_DISPLAY_LUT), ("negative", NEGATIVE_DISPLAY_LUT)):
        for quarter_turns in range(4):
            destination = tmp_path / f"preview-{mode}-{quarter_turns}.png"
            width, height = render_preview(
                tiff_path,
                destination,
                quarter_turns=quarter_turns,
                mode=mode,
            )
            display = np.ascontiguousarray(np.rot90(image, k=(-quarter_turns) % 4))
            assert (width, height) == (display.shape[1], display.shape[0])
            stored = cv2.imread(str(destination), cv2.IMREAD_UNCHANGED)
            expected = cv2.cvtColor(lut[display], cv2.COLOR_RGB2BGR)
            np.testing.assert_array_equal(stored, expected)


def test_render_preview_downscales_to_the_max_edge(tmp_path):
    """Past `PREVIEW_MAX_EDGE` the downscale is density-space `INTER_AREA`,
    exactly as the managed preview's: the written PNG's longest edge is
    capped, and the returned dimensions are the written file's."""
    import cv2

    from scanny_boy.previews import (
        NEGATIVE_DISPLAY_LUT,
        PREVIEW_MAX_EDGE,
        render_preview,
    )

    image = (np.arange(1400 * 1000 * 3, dtype=np.uint16).reshape(1400, 1000, 3) * 137) % 60000
    tiff_path = _write_published_tiff(tmp_path, image)

    destination = tmp_path / "preview.png"
    width, height = render_preview(tiff_path, destination, mode="negative")

    scale = PREVIEW_MAX_EDGE / 1400
    assert (width, height) == (round(1000 * scale), PREVIEW_MAX_EDGE)
    stored = cv2.imread(str(destination), cv2.IMREAD_UNCHANGED)
    assert stored.shape[:2] == (height, width)

    # The same downscale the managed preview does, then the negative LUT.
    scale = PREVIEW_MAX_EDGE / max(image.shape[0], image.shape[1])
    downscaled = cv2.resize(
        image,
        (round(image.shape[1] * scale), round(image.shape[0] * scale)),
        interpolation=cv2.INTER_AREA,
    )
    expected = cv2.cvtColor(NEGATIVE_DISPLAY_LUT[downscaled], cv2.COLOR_RGB2BGR)
    np.testing.assert_array_equal(stored, expected)


def test_render_preview_rejects_an_unknown_mode(tmp_path):
    import pytest

    from scanny_boy.previews import render_preview

    image = (np.arange(40 * 64 * 3, dtype=np.uint16).reshape(40, 64, 3) * 137) % 60000
    tiff_path = _write_published_tiff(tmp_path, image)

    with pytest.raises(ValueError):
        render_preview(tiff_path, tmp_path / "preview.png", mode="grayscale")


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
    manifest = new_roll_manifest(roll_id="rid-1", roll_name="Roll", film_kind="colour")
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


def test_ensure_preview_regenerates_with_the_fine_rotation(tmp_path):
    """The stitch stage's auto-seeded `rotate_fine` op reaches the preview
    pixels through the same canonical replay: mirror, then the fine warp
    (fill sentinel in what it uncovers), then quarter turns."""
    from scanny_boy import previews
    from scanny_boy.auto_rotate import rotate_with_fill
    from scanny_boy.library import repo

    image = np.repeat(np.arange(1200, dtype=np.uint16).reshape(30, 40, 1), 3, axis=-1)
    image = (image * 50).astype(np.uint16)
    roll_dir, _manifest, negative = _roll_with_published_negative(tmp_path, image)

    repo.append_edit(
        roll_dir,
        negative.negative_id,
        repo.ROTATE_FINE_OP,
        {"angle_deg": 30.0, "source": "auto"},
    )
    preview = previews.ensure_preview(roll_dir, "rid-1", negative)

    display = NORMALIZED_DISPLAY_LUT[rotate_with_fill(image, 30.0)]
    expected = cv2.cvtColor(np.ascontiguousarray(display), cv2.COLOR_RGB2BGR)
    stored = cv2.imread(str(preview), cv2.IMREAD_UNCHANGED)
    assert stored.shape == expected.shape
    np.testing.assert_array_equal(stored, expected)


def test_ensure_preview_regenerates_on_a_tone_op(tmp_path):
    """A `tone` op cannot ride the lossless incremental path — an 8-bit PNG
    cannot be re-curved — so it regenerates from the published TIFF with
    the net tone state composed into the display LUT."""
    from scanny_boy import previews, tone
    from scanny_boy.library import repo

    image = np.repeat(np.arange(12, dtype=np.uint16).reshape(3, 4, 1), 3, axis=-1)
    image = (image * 3000).astype(np.uint16)
    roll_dir, _manifest, negative = _roll_with_published_negative(tmp_path, image)


    flat = previews.ensure_preview(roll_dir, "rid-1", negative)
    flat_pixels = cv2.imread(str(flat), cv2.IMREAD_UNCHANGED)
    params = {
        "grade_r": 70.0,
        "snap_gamma": 0.3,
        "density": tone.DENSITY_REFERENCE,
        "shadow_density": 0.0,
        "highlight_density": 0.0,
        "toe": 0.0,
        "toe_width": tone.WIDTH_REFERENCE,
        "shoulder": 0.0,
        "shoulder_width": tone.WIDTH_REFERENCE,
    }
    repo.append_tone_edit(roll_dir, negative.negative_id, params)
    # The same canonical path is rewritten in place.
    toned = previews.ensure_preview(roll_dir, "rid-1", negative, repo.TONE_OP)

    toned_pixels = cv2.imread(str(toned), cv2.IMREAD_UNCHANGED)
    assert not np.array_equal(toned_pixels, flat_pixels)
    lut = tone.build_display_lut(tone.ToneParams(**params))
    np.testing.assert_array_equal(
        toned_pixels, cv2.cvtColor(lut[image], cv2.COLOR_RGB2BGR)
    )


def test_ensure_preview_regenerates_on_a_color_op(tmp_path):
    """A `color` op cannot ride the lossless incremental path — the display
    encode's per-channel LUTs change — so it regenerates from the TIFF."""
    import dataclasses

    from scanny_boy import color, previews
    from scanny_boy.library import repo

    image = np.repeat(np.arange(12, dtype=np.uint16).reshape(3, 4, 1), 3, axis=-1)
    image = (image * 3000).astype(np.uint16)
    roll_dir, _manifest, negative = _roll_with_published_negative(tmp_path, image)

    flat = previews.ensure_preview(roll_dir, "rid-1", negative)
    flat_pixels = cv2.imread(str(flat), cv2.IMREAD_UNCHANGED)
    params = dataclasses.asdict(color.NEUTRAL_COLOR) | {"wb_cyan": 0.2, "wb_magenta": 0.1}
    repo.append_color_edit(roll_dir, negative.negative_id, params)
    coloured = previews.ensure_preview(roll_dir, "rid-1", negative, repo.COLOR_OP)

    coloured_pixels = cv2.imread(str(coloured), cv2.IMREAD_UNCHANGED)
    assert not np.array_equal(coloured_pixels, flat_pixels)
    assert coloured == flat


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


def _write_published_tiff(
    tmp_path: Path, image: np.ndarray, name: str = "out.tif"
) -> Path:
    """A compressed single-page TIFF written the way `write_base_tiff` does
    (Adobe Deflate + horizontal predictor), so the strip-level reader is
    exercised against the real storage layout."""
    import tifffile

    path = tmp_path / name
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


def test_render_region_folds_in_the_fine_rotation(tmp_path):
    """A nonzero fine angle takes the exact path — full decode, the whole
    transform replayed on it, then the rect sliced out — so the region PNG
    is pixel-identical to the same slice of the full display image (the
    crop-then-transform shortcut is not exact under the warp)."""
    import cv2

    from scanny_boy.auto_rotate import rotate_with_fill
    from scanny_boy.previews import render_region

    image = np.repeat(np.arange(1200, dtype=np.uint16).reshape(30, 40, 1), 3, axis=-1)
    image = (image * 50).astype(np.uint16)
    tiff_path = _write_published_tiff(tmp_path, image)

    display = rotate_with_fill(image, 30.0)
    cases = [(4, 6, 20, 12), (0, 0, 40, 30), (33, 21, 7, 9)]
    for x, y, w, h in cases:
        destination = tmp_path / "region.png"
        rect = render_region(
            tiff_path,
            x, y, w, h,
            fine_angle_deg=30.0,
            destination=destination,
        )
        assert rect == (x, y, w, h)
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


# --- spots: the coordinate map and the repair in the render -------------------


def test_tiff_rect_to_display_round_trips_through_the_point_map():
    """The forward map composed with `_display_point_to_tiff` is the
    identity on corner points, for all four quarter turns x flipped/not, at
    zero fine angle."""
    from scanny_boy.previews import _display_point_to_tiff, tiff_rect_to_display

    tiff_h, tiff_w = 30, 40
    for quarter_turns in range(4):
        r = (-quarter_turns) % 4
        for flipped in (False, True):
            _dx, _dy, dw, dh = tiff_rect_to_display(
                (9, 7, 12, 8),
                (tiff_h, tiff_w),
                quarter_turns=quarter_turns,
                flipped_horizontally=flipped,
                fine_angle_deg=0.0,
            )
            # Odd net turns swap the rect's own dimensions.
            assert (dw, dh) == ((12, 8) if quarter_turns % 2 == 0 else (8, 12))
            # A 1x1 rect maps 1:1 at zero fine angle; the display point
            # maps back to the TIFF point.
            for tx, ty in ((9, 7), (20, 14)):
                px, py, pw, ph = tiff_rect_to_display(
                    (tx, ty, 1, 1),
                    (tiff_h, tiff_w),
                    quarter_turns=quarter_turns,
                    flipped_horizontally=flipped,
                    fine_angle_deg=0.0,
                )
                assert (pw, ph) == (1, 1)
                # Undo the quarter turns, then the mirror — the inverse of
                # the canonical order the forward map replays.
                ti, tj = _display_point_to_tiff(py, px, tiff_h, tiff_w, r)
                tx_back = tiff_w - 1 - tj if flipped else tj
                assert (ti, tx_back) == (ty, tx)


def test_tiff_rect_to_display_moves_a_rect_with_the_rotation():
    """Pinned against a hand-computed expectation, not against the
    function's own output: one cw turn of a rect at (2, 3, 5, 4) on a
    10x20 TIFF lands at (3, 2, 4, 5)."""
    from scanny_boy.previews import tiff_rect_to_display

    assert tiff_rect_to_display(
        (2, 3, 5, 4),
        (10, 20),
        quarter_turns=1,
        flipped_horizontally=False,
        fine_angle_deg=0.0,
    ) == (3, 2, 4, 5)
    # A flip with no turns mirrors the column bounds in place: tiff cols
    # 2..6 land at display cols 13..17 of a 20-wide image.
    assert tiff_rect_to_display(
        (2, 3, 5, 4),
        (10, 20),
        quarter_turns=0,
        flipped_horizontally=True,
        fine_angle_deg=0.0,
    ) == (13, 3, 5, 4)


def test_tiff_rect_to_display_grows_under_a_fine_angle():
    import cv2

    from scanny_boy.previews import tiff_rect_to_display

    tiff_h, tiff_w = 30, 40
    rect = (5, 6, 10, 8)
    angle = 6.0
    _dx, _dy, dw, dh = tiff_rect_to_display(
        rect,
        (tiff_h, tiff_w),
        quarter_turns=0,
        flipped_horizontally=False,
        fine_angle_deg=angle,
    )
    assert (dw, dh) >= (10, 8)
    # Every mapped corner lies inside the returned rect.
    matrix = cv2.getRotationMatrix2D((tiff_w / 2.0, tiff_h / 2.0), -angle, 1.0)
    x, y, w, _h = rect
    for px, py in ((x, y), (x + w - 1, y), (x + w - 1, y + _h - 1), (x, y + _h - 1)):
        mx = matrix[0, 0] * px + matrix[0, 1] * py + matrix[0, 2]
        my = matrix[1, 0] * px + matrix[1, 1] * py + matrix[1, 2]
        assert _dx <= mx <= _dx + dw
        assert _dy <= my <= _dy + dh


def _spots_params_for(image: np.ndarray, bbox: tuple[int, int, int, int], repair=True):
    from scanny_boy import spots

    _x, _y, w, h = bbox
    local = np.zeros((h, w), dtype=bool)
    local[:, :] = True
    return spots.spots_params(
        canvas=(image.shape[1], image.shape[0]),
        spots=[
            {
                "id": 1,
                "kind": "blob",
                "polarity": "dense",
                "bbox": list(bbox),
                "rle": spots.encode_rle(local),
                "area": w * h,
                "score": 10.0,
                "rejected": False,
            }
        ],
        sensitivity=0.5,
        repair=repair,
    )


def test_generate_preview_with_a_repair_off_is_byte_identical(tmp_path):
    from scanny_boy.previews import generate_preview

    image = (np.arange(60 * 80 * 3, dtype=np.uint16).reshape(60, 80, 3) * 97) % 60000
    image[12:15, 10:14] = image[12:15, 10:14] // 2  # a planted dark blob
    roll_dir, _manifest, negative = _roll_with_published_negative(tmp_path, image)

    plain = generate_preview(roll_dir, "rid-1", negative)
    off = generate_preview(
        roll_dir,
        "rid-1",
        negative,
        spots_params=_spots_params_for(image, (10, 12, 4, 3), repair=False),
    )
    assert plain.read_bytes() == off.read_bytes()


def test_generate_preview_with_a_repair_differs_only_near_the_spot(tmp_path):
    from scanny_boy import previews
    from scanny_boy import spots as spots_module
    from scanny_boy.previews import generate_preview

    image = (np.arange(60 * 80 * 3, dtype=np.uint16).reshape(60, 80, 3) * 97) % 60000
    image[12:15, 10:14] = image[12:15, 10:14] // 2  # a planted dark blob
    roll_dir, _manifest, negative = _roll_with_published_negative(tmp_path, image)

    plain = cv2.imread(str(generate_preview(roll_dir, "rid-1", negative)),
                       cv2.IMREAD_UNCHANGED)
    repaired_path = generate_preview(
        roll_dir,
        "rid-1",
        negative,
        spots_params=_spots_params_for(image, (10, 12, 4, 3), repair=True),
    )
    repaired = cv2.imread(str(repaired_path), cv2.IMREAD_UNCHANGED)
    assert not np.array_equal(plain, repaired)

    changed = np.any(repaired != plain, axis=2)
    assert changed.any()
    # The only pixels that moved are the spot's, grown by the repair's
    # dilation (this preview fits under PREVIEW_MAX_EDGE, so there is no
    # downscale between the repair and the PNG).
    assert 80 <= previews.PREVIEW_MAX_EDGE
    pad = spots_module.REPAIR_DILATE_PX + 1
    ys, xs = np.nonzero(changed)
    assert ys.min() >= 12 - pad
    assert xs.min() >= 10 - pad
    assert ys.max() <= 15 + pad
    assert xs.max() <= 14 + pad


def test_render_region_takes_the_exact_path_when_a_repair_is_live(tmp_path, monkeypatch):
    """Inpainting a crop uses different surroundings than inpainting the
    whole image, so a live repair must leave the strip-level fast path for
    the exact one — and the pixels must match a full decode + replay +
    slice."""
    from scanny_boy import previews

    image = (np.arange(40 * 64 * 3, dtype=np.uint16).reshape(40, 64, 3) * 137) % 60000
    image[12:15, 10:14] = image[12:15, 10:14] // 2  # a planted dark blob
    tiff_path = _write_published_tiff(tmp_path, image)
    params = _spots_params_for(image, (10, 12, 4, 3), repair=True)

    def _boom(*args, **kwargs):
        raise AssertionError("a live repair must not take the strip path")

    monkeypatch.setattr(previews, "_read_tiff_region", _boom)

    destination = tmp_path / "region.png"
    rect = previews.render_region(
        tiff_path, 5, 8, 20, 12, destination=destination, spots_params=params
    )
    assert rect == (5, 8, 20, 12)
    stored = cv2.imread(str(destination), cv2.IMREAD_UNCHANGED)
    display = previews._display_image(tiff_path, spots_params=params)
    expected = cv2.cvtColor(
        NORMALIZED_DISPLAY_LUT[display[8:20, 5:25]], cv2.COLOR_RGB2BGR
    )
    np.testing.assert_array_equal(stored, expected)


def test_ensure_preview_regenerates_on_a_spots_op(tmp_path):
    """A repair changes pixels, and the incremental transform path is
    lossless-geometry only — so the `spots` op regenerates the cached
    preview     rather than transforming it."""
    from scanny_boy import previews
    from scanny_boy.library import repo

    image = (np.arange(60 * 80 * 3, dtype=np.uint16).reshape(60, 80, 3) * 97) % 60000
    image[12:15, 10:14] = image[12:15, 10:14] // 2  # a planted dark blob
    roll_dir, _manifest, negative = _roll_with_published_negative(tmp_path, image)

    flat = previews.ensure_preview(roll_dir, "rid-1", negative)
    flat_pixels = cv2.imread(str(flat), cv2.IMREAD_UNCHANGED)

    params = _spots_params_for(image, (10, 12, 4, 3), repair=True)
    repo.append_spots_edit(roll_dir, negative.negative_id, params)
    preview = previews.ensure_preview(
        roll_dir, "rid-1", negative, repo.SPOTS_OP
    )

    repaired_pixels = cv2.imread(str(preview), cv2.IMREAD_UNCHANGED)
    assert not np.array_equal(flat_pixels, repaired_pixels)
    expected_path = previews.generate_preview(
        roll_dir, "rid-1", negative, spots_params=params
    )
    assert repaired_pixels.tobytes() == cv2.imread(
        str(expected_path), cv2.IMREAD_UNCHANGED
    ).tobytes()

    # Rejection is a decision, not a disappearance: with every spot
    # rejected the preview returns to its pre-repair bytes.
    rejected = _spots_params_for(image, (10, 12, 4, 3), repair=True)
    rejected["spots"][0]["rejected"] = True
    repo.append_spots_edit(roll_dir, negative.negative_id, rejected)
    preview = previews.ensure_preview(roll_dir, "rid-1", negative, repo.SPOTS_OP)
    np.testing.assert_array_equal(
        cv2.imread(str(preview), cv2.IMREAD_UNCHANGED), flat_pixels
    )


# --- the daemon's decoded-pixel cache ----------------------------------------


@pytest.fixture(autouse=True)
def _empty_preview_cache():
    """The cache is process state; every test here starts from empty and
    leaves nothing behind for the next one."""
    from scanny_boy import previews

    previews._DISPLAY_PREVIEW_CACHE.clear()
    previews._DISPLAY_PREVIEW_CACHE_BYTES = 0
    previews._DISPLAY_IMAGE_CACHE.clear()
    previews._DISPLAY_IMAGE_CACHE_BYTES = 0
    yield
    previews._DISPLAY_PREVIEW_CACHE.clear()
    previews._DISPLAY_PREVIEW_CACHE_BYTES = 0
    previews._DISPLAY_IMAGE_CACHE.clear()
    previews._DISPLAY_IMAGE_CACHE_BYTES = 0


def test_preview_cache_hits_across_tone_changes(tmp_path):
    """§3.1's whole point: the key is the geometry, never the tone, so a
    slider drag — two renders of the same negative with different tone
    params — decodes once."""
    from scanny_boy import previews

    image = (np.arange(40 * 64 * 3, dtype=np.uint16).reshape(40, 64, 3) * 137) % 60000
    tiff_path = _write_published_tiff(tmp_path, image)

    decode_calls = []
    real_decode = previews._display_image

    def counting(*args, **kwargs):
        decode_calls.append(args)
        return real_decode(*args, **kwargs)

    previews._display_image = counting
    try:
        previews.render_preview(
            tiff_path,
            tmp_path / "flat.png",
            mode="positive",
        )
        previews.render_preview(
            tiff_path,
            tmp_path / "graded.png",
            tone_params={"grade_r": 160.0, "snap_gamma": 0.3},
            mode="positive",
        )
    finally:
        previews._display_image = real_decode

    assert len(decode_calls) == 1
    flat = cv2.imread(str(tmp_path / "flat.png"), cv2.IMREAD_UNCHANGED)
    graded = cv2.imread(str(tmp_path / "graded.png"), cv2.IMREAD_UNCHANGED)
    assert not np.array_equal(flat, graded)


def test_preview_cache_invalidates_on_geometry_and_pixels(tmp_path):
    """§3.3: the key must include everything that changes decoded pixels
    before the LUT — the transform, and the TIFF itself (a re-stitch
    rewrites it, which is what the mtime term catches) — and the spot set,
    whose repair is the first step of the replay."""
    from scanny_boy import previews

    image = (np.arange(40 * 64 * 3, dtype=np.uint16).reshape(40, 64, 3) * 137) % 60000
    tiff_path = _write_published_tiff(tmp_path, image)

    decode_calls = []
    real_decode = previews._display_image

    def counting(*args, **kwargs):
        decode_calls.append(args)
        return real_decode(*args, **kwargs)

    previews._display_image = counting
    try:
        previews.render_preview(tiff_path, tmp_path / "a.png", mode="negative")
        previews.render_preview(tiff_path, tmp_path / "b.png", mode="negative")
        assert len(decode_calls) == 1

        # A different net transform decodes afresh...
        previews.render_preview(
            tiff_path, tmp_path / "c.png", quarter_turns=1, mode="negative"
        )
        assert len(decode_calls) == 2
        # ...as does a rewritten TIFF at the same path — a re-stitch's
        # shape, and what the mtime term exists to catch (§3.3).
        rewritten = (
            np.arange(40 * 64 * 3, dtype=np.uint16).reshape(40, 64, 3) * 251
        ) % 60000
        _write_published_tiff(tmp_path, rewritten, name=tiff_path.name)
        previews.render_preview(tiff_path, tmp_path / "d.png", mode="negative")
        assert len(decode_calls) == 3
    finally:
        previews._display_image = real_decode


def test_preview_cache_folds_the_spot_set_into_its_key(tmp_path):
    """A live spot set's repair changes pixels, so the whole set is in the
    key: an identical set hits, a changed one misses."""
    from scanny_boy import previews

    image = (np.arange(40 * 64 * 3, dtype=np.uint16).reshape(40, 64, 3) * 137) % 60000
    tiff_path = _write_published_tiff(tmp_path, image)
    spots_params = {
        "repair": True,
        "spots": [{"rect": [10, 10, 4, 4], "rejected": False, "rle": "1x4"}],
    }

    decode_calls = []
    real_decode = previews._display_image

    def counting(*args, **kwargs):
        decode_calls.append(args)
        return real_decode(*args, **kwargs)

    previews._display_image = counting
    try:
        previews.render_preview(
            tiff_path, tmp_path / "a.png", spots_params=spots_params, mode="negative"
        )
        previews.render_preview(
            tiff_path,
            tmp_path / "b.png",
            spots_params=dict(spots_params),
            mode="negative",
        )
        assert len(decode_calls) == 1
        changed = {
            "repair": True,
            "spots": [{"rect": [12, 12, 4, 4], "rejected": False, "rle": "1x4"}],
        }
        previews.render_preview(
            tiff_path, tmp_path / "c.png", spots_params=changed, mode="negative"
        )
        assert len(decode_calls) == 2
    finally:
        previews._display_image = real_decode


def test_preview_cache_is_bounded_by_total_bytes(tmp_path, monkeypatch):
    """The bound is total bytes, not entry count, and it is one constant
    in one module."""
    from scanny_boy import previews

    monkeypatch.setattr(previews, "PREVIEW_CACHE_MAX_BYTES", 1)
    for index in range(4):
        image = (
            np.arange(40 * 64 * 3, dtype=np.uint16).reshape(40, 64, 3) * (index + 1) * 137
        ) % 60000
        roll = tmp_path / f"roll-{index}"
        roll.mkdir()
        tiff_path = _write_published_tiff(roll, image)
        previews.render_preview(tiff_path, tmp_path / f"out-{index}.png", mode="negative")

    # Total bytes bound the cache; the newest entry always survives (the
    # eviction never empties it), so the steady state is exactly that one
    # entry and nothing else.
    assert len(previews._DISPLAY_PREVIEW_CACHE) == 1
    (_, kept,) = previews._DISPLAY_PREVIEW_CACHE.popitem()
    assert kept.nbytes == previews._DISPLAY_PREVIEW_CACHE_BYTES


def test_display_image_cache_hits_on_second_render_region(tmp_path, monkeypatch):
    """The slow path decodes once; a second region on the same negative
    reuses the full-resolution display array."""
    from scanny_boy import previews

    image = (np.arange(40 * 64 * 3, dtype=np.uint16).reshape(40, 64, 3) * 137) % 60000
    tiff_path = _write_published_tiff(tmp_path, image)

    decode_calls = []
    real_decode = previews._display_image

    def counting(*args, **kwargs):
        decode_calls.append(args)
        return real_decode(*args, **kwargs)

    previews._display_image = counting
    try:
        previews.render_region(
            tiff_path, 0, 0, 10, 10, fine_angle_deg=1.0, destination=tmp_path / "a.rgba"
        )
        previews.render_region(
            tiff_path, 12, 0, 10, 10, fine_angle_deg=1.0, destination=tmp_path / "b.rgba"
        )
    finally:
        previews._display_image = real_decode

    assert len(decode_calls) == 1


def test_render_region_raw_cache_matches_png(tmp_path):
    """Raw region caches carry the same pixels as the PNG encode."""
    from scanny_boy import previews

    image = (np.arange(40 * 64 * 3, dtype=np.uint16).reshape(40, 64, 3) * 137) % 60000
    tiff_path = _write_published_tiff(tmp_path, image)
    png_path = tmp_path / "region.png"
    raw_path = tmp_path / "region.rgba"
    rect = previews.render_region(
        tiff_path, 10, 5, 20, 12, destination=png_path
    )
    assert rect == (10, 5, 20, 12)
    previews.render_region(tiff_path, 10, 5, 20, 12, destination=raw_path)

    png = cv2.imread(str(png_path), cv2.IMREAD_UNCHANGED)
    raw_bytes = raw_path.read_bytes()
    assert raw_bytes[:4] == previews.REGION_RAW_MAGIC
    width, height = struct.unpack("<II", raw_bytes[4:12])
    assert (width, height) == (20, 12)
    rgba = np.frombuffer(raw_bytes[12:], dtype=np.uint8).reshape(height, width, 4)
    raw = cv2.cvtColor(rgba, cv2.COLOR_RGBA2RGB)
    np.testing.assert_array_equal(raw, cv2.cvtColor(png, cv2.COLOR_BGR2RGB))


# --- the crop op -------------------------------------------------------------


def _gradient_tiff(
    tmp_path: Path, height: int = 200, width: int = 300
) -> tuple[Path, np.ndarray]:
    """A smooth coordinate gradient, uint16 RGB — gentle enough that a
    one-pixel interpolation misalignment reads as a small code delta, and
    asymmetric enough that a displaced crop is visible in the codes."""
    rows = np.arange(height, dtype=np.float64)[:, None]
    cols = np.arange(width, dtype=np.float64)[None, :]
    values = (cols * 90.0 + rows * 45.0)[..., None]
    image = np.repeat(values, 3, axis=-1).astype(np.uint16)
    return _write_published_tiff(tmp_path, image), image


def _window_params(
    window: tuple[int, int, int, int, float], tiff_shape: tuple[int, int]
) -> dict:
    """A `crop` op's params for a mapped window, the way `run_edit_crop`
    stores them."""
    x, y, w, h, tilt = window
    return {
        "canvas": [tiff_shape[1], tiff_shape[0]],
        "x": x,
        "y": y,
        "w": w,
        "h": h,
        "tilt_deg": tilt,
    }


@pytest.mark.parametrize("quarter_turns", range(4))
@pytest.mark.parametrize("flipped", [False, True])
def test_the_recorded_crop_slices_the_display_exactly(tmp_path, quarter_turns, flipped):
    """With no tilt, a rect drawn over the transformed display records as a
    TIFF-space window whose replay reproduces exactly the drawn rect's
    pixels — the record mapping and the crop step agree with the canonical
    replay, so the drawn tilt is the only thing that ever interpolates."""
    from scanny_boy import previews
    from scanny_boy.previews import _display_image

    tiff_path, image = _gradient_tiff(tmp_path)
    tiff_size = (image.shape[0], image.shape[1])
    rect = (10, 8, 50, 24)

    window = previews.display_crop_window_to_tiff(
        rect,
        tiff_size,
        tilt_deg=0.0,
        quarter_turns=quarter_turns,
        flipped_horizontally=flipped,
        fine_angle_deg=0.0,
        crop_params=None,
    )
    _tx, _ty, tw, th, tilt = window
    assert tilt == 0.0
    # Odd net turns swap the sides: the TIFF-space window is the drawn
    # rect mapped back through the rotation.
    assert (tw, th) == (
        (rect[3], rect[2]) if quarter_turns % 2 else (rect[2], rect[3])
    )

    cropped = _display_image(
        tiff_path,
        quarter_turns,
        flipped,
        0.0,
        None,
        _window_params(window, tiff_size),
    )
    assert cropped.shape[:2] == (rect[3], rect[2])
    uncropped = _display_image(tiff_path, quarter_turns, flipped)
    expected = uncropped[
        rect[1] : rect[1] + rect[3], rect[0] : rect[0] + rect[2]
    ]
    np.testing.assert_array_equal(cropped, expected)


def test_full_frame_crop_round_trips_through_rotation():
    """With `--full-frame`, a rect on the uncropped display round-trips
    through the stored TIFF window and back — even when quarter_turns is
    odd (portrait display)."""
    from scanny_boy import previews

    tiff_h, tiff_w = 800, 1000
    tiff_size = (tiff_h, tiff_w)
    rect = (100, 50, 400, 300)

    window = previews.display_crop_window_to_tiff(
        rect,
        tiff_size,
        tilt_deg=0.0,
        quarter_turns=1,
        flipped_horizontally=False,
        fine_angle_deg=0.0,
        crop_params=None,
        full_frame=True,
    )
    x, y, w, h, tilt = window
    assert tilt == 0.0

    assert previews.tiff_rect_to_display(
        (x, y, w, h),
        tiff_size,
        quarter_turns=1,
        flipped_horizontally=False,
        fine_angle_deg=0.0,
        crop_params=None,
    ) == rect


def test_the_tilted_crop_removes_the_drawn_tilt(tmp_path):
    """The crop semantics, end to end: a rect drawn tilted over the current
    display records as the composed TIFF-space window, and the replayed
    display equals the drawn rect's content upright — the uncropped
    display image warped by the drawn tilt about the rect's centre and
    cropped to the rect. The warp interpolates, so the comparison
    tolerates the two paths' resampling differences."""
    import cv2

    from scanny_boy import previews
    from scanny_boy.previews import _display_image

    tiff_path, image = _gradient_tiff(tmp_path)
    tiff_size = (image.shape[0], image.shape[1])
    rect = (40, 30, 120, 80)
    tilt = 7.0

    window = previews.display_crop_window_to_tiff(
        rect,
        tiff_size,
        tilt_deg=tilt,
        quarter_turns=0,
        flipped_horizontally=False,
        fine_angle_deg=0.0,
        crop_params=None,
    )
    assert window[2] == rect[2] and window[3] == rect[3]
    assert abs(window[4] - tilt) < 0.51

    cropped = _display_image(
        tiff_path, 0, False, 0.0, None, _window_params(window, tiff_size)
    )
    assert cropped.shape[:2] == (rect[3], rect[2])
    uncropped = _display_image(tiff_path, 0, False)

    centre = (rect[0] + rect[2] / 2.0, rect[1] + rect[3] / 2.0)
    matrix = cv2.getRotationMatrix2D(centre, -tilt, 1.0)
    warped = cv2.warpAffine(
        uncropped,
        matrix,
        (uncropped.shape[1], uncropped.shape[0]),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    expected = warped[rect[1] : rect[1] + rect[3], rect[0] : rect[0] + rect[2]]
    # The two warps run in different spaces (TIFF vs display) about
    # centres half a pixel apart, so the same content lands within a
    # couple of pixels; the gradient's 90-codes-per-column slope bounds
    # the delta.
    assert np.max(np.abs(cropped.astype(int) - expected.astype(int))) < 600


def test_positive_tilt_samples_the_ccw_diagonal(tmp_path):
    """A +tilt crop must sample the overlay's CCW window — more of the
    image's top-right than its top-left — not the opposite diagonal."""
    from scanny_boy import previews
    from scanny_boy.previews import _display_image

    tiff_path, image = _gradient_tiff(tmp_path, height=200, width=300)
    tiff_size = (image.shape[0], image.shape[1])
    # A centred window large enough that tilt skew is visible in the slice.
    rect = (60, 40, 180, 120)
    tilt = 10.0

    window = previews.display_crop_window_to_tiff(
        rect,
        tiff_size,
        tilt_deg=tilt,
        quarter_turns=0,
        flipped_horizontally=False,
        fine_angle_deg=0.0,
        crop_params=None,
    )
    cropped = _display_image(
        tiff_path, 0, False, 0.0, None, _window_params(window, tiff_size)
    )
    width = cropped.shape[1]
    left_mean = float(cropped[:, : width // 3].mean())
    right_mean = float(cropped[:, -width // 3 :].mean())
    assert right_mean > left_mean

    # The opposite tilt maps a different window and samples the other
    # diagonal — higher left and lower right than +tilt, even though both
    # stay right-heavy on this gradient.
    opposite_window = previews.display_crop_window_to_tiff(
        rect,
        tiff_size,
        tilt_deg=-tilt,
        quarter_turns=0,
        flipped_horizontally=False,
        fine_angle_deg=0.0,
        crop_params=None,
    )
    opposite = _display_image(
        tiff_path, 0, False, 0.0, None, _window_params(opposite_window, tiff_size)
    )
    opposite_left = float(opposite[:, : width // 3].mean())
    opposite_right = float(opposite[:, -width // 3 :].mean())
    assert left_mean > opposite_left
    assert right_mean < opposite_right


def test_the_crop_composes_with_the_display_transforms(tmp_path):
    """A crop drawn under net quarter turns and a flip records as one
    TIFF-space window, and the replayed display equals the drawn rect
    cropped from the transformed display — the transforms land on the
    cropped frame wholesale."""
    from scanny_boy import previews
    from scanny_boy.previews import _display_image

    tiff_path, image = _gradient_tiff(tmp_path)
    tiff_size = (image.shape[0], image.shape[1])
    quarter_turns, flipped = 1, True
    rect = (10, 8, 50, 24)

    window = previews.display_crop_window_to_tiff(
        rect,
        tiff_size,
        tilt_deg=0.0,
        quarter_turns=quarter_turns,
        flipped_horizontally=flipped,
        fine_angle_deg=0.0,
        crop_params=None,
    )
    cropped = _display_image(
        tiff_path, quarter_turns, flipped, 0.0, None, _window_params(window, tiff_size)
    )
    uncropped = _display_image(tiff_path, quarter_turns, flipped)
    expected = uncropped[rect[1] : rect[1] + rect[3], rect[0] : rect[0] + rect[2]]
    np.testing.assert_array_equal(cropped, expected)


def test_a_recrop_composes_into_one_window(tmp_path):
    """A second crop drawn over an already-cropped display stores the
    fully-composed window: recording crop B over crop A's output and
    replaying must equal mapping B's rect back through A and applying it
    to the original TIFF. The ops log's net crop is a single window."""
    import cv2

    from scanny_boy import previews
    from scanny_boy.previews import _display_image

    tiff_path, image = _gradient_tiff(tmp_path)
    tiff_size = (image.shape[0], image.shape[1])
    first_rect = (20, 15, 180, 120)
    first_window = previews.display_crop_window_to_tiff(
        first_rect,
        tiff_size,
        tilt_deg=0.0,
        quarter_turns=0,
        flipped_horizontally=False,
        fine_angle_deg=0.0,
        crop_params=None,
    )
    first = _window_params(first_window, tiff_size)

    # The display now shows the first crop; the user draws a tilted rect
    # over it.
    second_rect = (30, 25, 100, 60)
    second_tilt = 5.0
    second_window = previews.display_crop_window_to_tiff(
        second_rect,
        tiff_size,
        tilt_deg=second_tilt,
        quarter_turns=0,
        flipped_horizontally=False,
        fine_angle_deg=0.0,
        crop_params=first,
    )
    second = _window_params(second_window, tiff_size)

    replayed = _display_image(tiff_path, 0, False, 0.0, None, second)
    assert replayed.shape[:2] == (second_rect[3], second_rect[2])

    # The expectation: the first crop's display, warped by the drawn tilt
    # about the drawn rect's centre, sliced at the drawn rect.
    first_display = _display_image(tiff_path, 0, False, 0.0, None, first)
    centre = (
        second_rect[0] + second_rect[2] / 2.0,
        second_rect[1] + second_rect[3] / 2.0,
    )
    matrix = cv2.getRotationMatrix2D(centre, -second_tilt, 1.0)
    warped = cv2.warpAffine(
        first_display,
        matrix,
        (first_display.shape[1], first_display.shape[0]),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    expected = warped[
        second_rect[1] : second_rect[1] + second_rect[3],
        second_rect[0] : second_rect[0] + second_rect[2],
    ]
    assert np.max(np.abs(replayed.astype(int) - expected.astype(int))) < 600


def test_apply_crop_ignores_a_stale_crop(tmp_path):
    """A crop recorded against a canvas a re-stitch has replaced applies as
    nothing — the same canvas rule the spots op has, degrading to the full
    frame rather than describing pixels the display no longer shows."""
    from scanny_boy import previews
    from scanny_boy.previews import _display_image

    tiff_path, image = _gradient_tiff(tmp_path)
    stale = {
        "canvas": [999, 888],
        "x": 10,
        "y": 20,
        "w": 100,
        "h": 60,
        "tilt_deg": 0.0,
    }
    assert not previews.crop_is_live(stale, image.shape[:2])
    np.testing.assert_array_equal(
        previews.apply_crop(image, stale), image
    )
    np.testing.assert_array_equal(
        _display_image(tiff_path, 0, False, 0.0, None, stale),
        _display_image(tiff_path, 0, False),
    )


def test_display_shape_and_crop_report_follow_the_crop_window():
    from scanny_boy import previews

    tiff_size = (200, 300)  # (height, width)
    crop = {"canvas": [300, 200], "x": 10, "y": 20, "w": 100, "h": 60, "tilt_deg": 3.0}
    assert previews.display_shape(
        tiff_size, quarter_turns=0, crop_params=crop
    ) == (60, 100)
    # Odd net turns swap the cropped display's dimensions.
    assert previews.display_shape(
        tiff_size, quarter_turns=1, crop_params=crop
    ) == (100, 60)
    assert previews.crop_report(
        crop, tiff_size, quarter_turns=1
    ) == {
        "width": 60,
        "height": 100,
        "tilt_deg": 3.0,
        "preset": None,
        "x": 120,
        "y": 10,
        "canvas_width": 200,
        "canvas_height": 300,
    }
    assert (
        previews.crop_report(None, tiff_size, quarter_turns=0) is None
    )


def test_render_region_works_in_cropped_display_space(tmp_path):
    """With a live crop, `render_region`'s coordinates are the cropped
    display's — the same space `roll info`'s crop report names — and the
    exact path (full decode, transform replayed, rect sliced) returns the
    same pixels the cropped preview shows."""
    import cv2

    from scanny_boy import previews
    from scanny_boy.previews import _display_image, render_region

    tiff_path, image = _gradient_tiff(tmp_path)
    tiff_size = (image.shape[0], image.shape[1])
    window = previews.display_crop_window_to_tiff(
        (40, 30, 120, 80),
        tiff_size,
        tilt_deg=0.0,
        quarter_turns=0,
        flipped_horizontally=False,
        fine_angle_deg=0.0,
        crop_params=None,
    )
    crop = _window_params(window, tiff_size)
    destination = tmp_path / "region.png"
    rect = render_region(
        tiff_path,
        20,
        10,
        50,
        30,
        destination=destination,
        crop_params=crop,
    )
    assert rect == (20, 10, 50, 30)
    stored = cv2.imread(str(destination), cv2.IMREAD_UNCHANGED)
    display = _display_image(tiff_path, 0, False, 0.0, None, crop)
    expected = cv2.cvtColor(
        NORMALIZED_DISPLAY_LUT[display[10:40, 20:70]],
        cv2.COLOR_RGB2BGR,
    )
    np.testing.assert_array_equal(stored, expected)


def test_tiff_rect_to_display_folds_the_crop_in():
    """The spot-marker mapping lands in cropped display coordinates: with
    an axis-aligned crop the mapping is the translation by the window's
    origin."""
    from scanny_boy import previews

    tiff_size = (200, 300)
    crop = {"canvas": [300, 200], "x": 40, "y": 30, "w": 120, "h": 80, "tilt_deg": 0.0}
    rect = previews.tiff_rect_to_display(
        (50, 40, 20, 10),
        tiff_size,
        quarter_turns=0,
        flipped_horizontally=False,
        fine_angle_deg=0.0,
        crop_params=crop,
    )
    assert rect == (10, 10, 20, 10)
