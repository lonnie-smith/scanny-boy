"""Tests for the preview cache: lossless quarter turns go the way labelled,
and the 16→8-bit preview encode is decode-normalize-invert — no gamma, a
positive-looking display of the normalized-density negative."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from scanny_boy import normalization
from scanny_boy.previews import MAX_CODE, NORMALIZED_DISPLAY_LUT, rotate_preview


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

    rotate_preview(path, "cw")

    rotated = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    np.testing.assert_array_equal(rotated, np.rot90(image, k=3).squeeze())


def test_rotate_preview_ccw_is_counter_clockwise(tmp_path):
    image = np.arange(12, dtype=np.uint8).reshape(3, 4, 1)
    path = _write_preview(tmp_path, image)

    rotate_preview(path, "ccw")

    rotated = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    np.testing.assert_array_equal(rotated, np.rot90(image, k=1).squeeze())


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
