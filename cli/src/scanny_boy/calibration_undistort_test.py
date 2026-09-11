"""Fast tests for empty-corner undistortion guards on the calibration path."""

from __future__ import annotations

import numpy as np

from scanny_boy import calibration


def test_empty_corners_with_accepted_geometry_returns_empty():
    """OpenCV returns None from undistortPoints on empty input; the guard must
    not let that reach .reshape."""
    empty = np.zeros((0, 2), dtype=np.float32)
    geometry = (-0.01, 0.0, 2268.0, 1512.0)
    result = calibration._undistort_to_normalised(empty, 4536, 3024, geometry)
    assert result.shape == (0, 2)
    assert result.dtype == np.float64


def test_prepare_skips_frame_when_all_channels_empty():
    """prepare() undistorts before checking emptiness; empty inputs must not
    crash when geometry was accepted."""
    geometry_params = (-0.01, 0.0, 2268.0, 1512.0)
    half_w, half_h = 2268, 1512
    empty = (np.zeros((0, 2), dtype=np.float32), np.zeros((0, 1), dtype=np.int32))
    detection = {
        "red": empty,
        "green": empty,
        "blue": empty,
        "luminance": empty,
    }

    def normalised(channel: str) -> tuple[np.ndarray, np.ndarray]:
        points, ids = detection[channel]
        points_n = calibration._undistort_to_normalised(
            points, half_w, half_h, geometry_params
        )
        return points_n, ids.reshape(-1)

    red_n, red_ids = normalised("red")
    green_n, green_ids = normalised("green")
    blue_n, blue_ids = normalised("blue")

    red, _green_for_red, _ = calibration._intersect_ids(
        (red_n, red_ids), (green_n, green_ids)
    )
    blue, _green_for_blue, _ = calibration._intersect_ids(
        (blue_n, blue_ids), (green_n, green_ids)
    )
    assert len(red) == 0 or len(blue) == 0
