"""Tests for `auto_rotate`: the rebate-tilt estimator and the
fill-preserving rotation the ops log's fine angle is applied with."""

from __future__ import annotations

import numpy as np
import pytest

from scanny_boy.auto_rotate import (
    AUTO_ROTATE_MAX_DEG,
    estimate_rotation,
    rotate_with_fill,
)
from scanny_boy.normalization import NORMALIZED_FILL, encode_normalized

# Beyond the clamps the estimator refuses on principle.
_OVER_TILT_DEG = AUTO_ROTATE_MAX_DEG + 10.0

_FILL_CODE = int(
    encode_normalized(
        np.full((1, 1, 3), NORMALIZED_FILL, dtype=np.float32)
    )[0, 0, 0]
)


def _encoded_scene(
    height: int = 400,
    width: int = 600,
    rebate_px: int = 40,
    scene_value: float = 0.5,
    rebate_value: float = 1.02,
) -> np.ndarray:
    """An encoded uint16 scene rectangle surrounded by a rebate band —
    a stitched negative with its film rebate visible on all four sides."""
    normalized = np.full((height, width, 3), scene_value, dtype=np.float32)
    normalized[:rebate_px, :] = rebate_value
    normalized[-rebate_px:, :] = rebate_value
    normalized[:, :rebate_px] = rebate_value
    normalized[:, -rebate_px:] = rebate_value
    return encode_normalized(normalized)


def test_rotate_with_fill_keeps_the_canvas_dimensions():
    image = np.zeros((40, 60, 3), dtype=np.uint16)

    rotated = rotate_with_fill(image, 5.0)

    assert rotated.shape == image.shape


def test_rotate_with_fill_fills_the_uncovered_pixels_with_the_sentinel():
    """Pixels whose source falls outside the image are exactly the
    stitching fill sentinel — the thin rail, section 3.14."""
    image = np.zeros((40, 60, 3), dtype=np.uint16)

    rotated = rotate_with_fill(image, 45.0)

    assert rotated[0, 0, 0] == _FILL_CODE
    assert rotated[-1, -1, 0] == _FILL_CODE
    # The centre stays where it was: nothing was invented there.
    np.testing.assert_array_equal(rotated[20, 30], image[20, 30])


def test_rotate_with_fill_at_zero_angle_returns_the_image_unchanged():
    image = np.arange(48, dtype=np.uint16).reshape(4, 12)

    assert rotate_with_fill(image, 0.0) is image


def test_rotate_with_fill_positive_angles_turn_clockwise():
    """The fine angle counts clockwise, matching the quarter-turn ops: a
    marker at the top of the image moves right under a positive angle."""
    image = np.zeros((101, 201, 3), dtype=np.uint16)
    image[2:7, 98:103] = 60000

    right = rotate_with_fill(image, 5.0)
    left = rotate_with_fill(image, -5.0)

    def marker_col(rotated):
        return int(np.argmax(rotated[4].sum(axis=-1)))

    assert marker_col(right) > 100
    assert marker_col(left) < 100


@pytest.mark.parametrize(
    "tilt_deg", [-2.5, -1.0, 1.0, 3.0], ids=lambda v: f"tilt-{v}"
)
def test_estimate_rotation_recovers_a_known_tilt(tilt_deg):
    """A rebate tilted clockwise by `d` squares up under a clockwise
    rotation of `-d`."""
    tilted = rotate_with_fill(_encoded_scene(), tilt_deg)

    rotation = estimate_rotation(tilted)

    assert rotation == pytest.approx(-tilt_deg, abs=0.3)


def test_estimate_rotation_is_self_consistent():
    """Applying the estimated rotation to the tilted image leaves an image
    whose own estimate is inside the deadband — the sign convention of the
    estimator and of `rotate_with_fill` cancel, which is the property every
    consumer relies on."""
    tilted = rotate_with_fill(_encoded_scene(), 2.0)

    rotation = estimate_rotation(tilted)
    assert rotation is not None
    corrected = rotate_with_fill(tilted, rotation)

    assert estimate_rotation(corrected) is None


def test_a_level_rebate_is_inside_the_deadband():
    assert estimate_rotation(_encoded_scene()) is None


def test_no_rebate_returns_none():
    """A frame with no thin population (the rebate cropped away, or a
    full-frame stitched canvas) gives the estimator nothing to square."""
    assert estimate_rotation(_encoded_scene(rebate_px=0)) is None


def test_an_over_large_tilt_is_refused():
    """A tilt beyond the clamp is not a slightly crooked scan — refuse
    rather than rotate on a misread."""
    tilted = rotate_with_fill(_encoded_scene(), _OVER_TILT_DEG)

    assert estimate_rotation(tilted) is None


def test_the_fill_is_never_taken_for_rebate_or_scene():
    """A tilted canvas' fill wedges sit outside the film entirely; the
    estimate on a rebate-less scene with heavy fill wedges is still None."""
    image = rotate_with_fill(_encoded_scene(rebate_px=0), 6.0)

    assert estimate_rotation(image) is None