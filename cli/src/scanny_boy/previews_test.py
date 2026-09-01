"""Tests for the preview cache: lossless quarter turns go the way labelled,
and the 16→8-bit preview encode is the sRGB display transfer function."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from scanny_boy.previews import MAX_CODE, SRGB_ENCODE_LUT, rotate_preview


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


def test_srgb_lut_is_monotonic_and_spans_the_range():
    assert SRGB_ENCODE_LUT.shape == (MAX_CODE + 1,)
    assert int(SRGB_ENCODE_LUT[0]) == 0
    assert int(SRGB_ENCODE_LUT[MAX_CODE]) == 255
    assert np.all(np.diff(SRGB_ENCODE_LUT.astype(np.int32)) >= 0)


def test_srgb_lut_matches_the_reference_curve():
    """Spot-check against the IEC sRGB transfer function at a few codes,
    including inside and outside the linear toe."""
    for code in (0, 1, 50, 2000, 8192, 32768, 65535):
        linear = code / MAX_CODE
        if linear <= 0.0031308:
            expected = linear * 12.92
        else:
            expected = 1.055 * linear ** (1 / 2.4) - 0.055
        assert int(SRGB_ENCODE_LUT[code]) == round(expected * 255)


def test_srgb_lut_midtone_is_near_half():
    """Linear mid-grey (~18% scene grey ≈ 0.184 linear) encodes near the
    middle of the 8-bit range, so previews are not near-black."""
    grey = SRGB_ENCODE_LUT[int(0.184 * MAX_CODE)]
    assert 100 <= int(grey) <= 130
