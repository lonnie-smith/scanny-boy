"""Tests for the preview cache: lossless quarter turns go the way labelled."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from scanny_boy.previews import rotate_preview


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
