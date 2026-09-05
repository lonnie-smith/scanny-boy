"""Tests for the preview tone curve (`tone.py`): the grade-to-slope
mapping, the curve's monotonicity and pinned endpoints, and the composed
display LUT."""

from __future__ import annotations

import numpy as np
import pytest

from scanny_boy import normalization, tone


@pytest.mark.parametrize(
    "grade_r,expected",
    [
        (115.0, 1.55),  # the calibration grade: the reference slope
        (50.0, 1.55 * 115.0 / 50.0),
        (180.0, 1.55 * 115.0 / 180.0),
    ],
)
def test_grade_slope_scales_with_the_paper_range(grade_r, expected):
    assert tone.grade_slope(grade_r) == pytest.approx(expected)


def test_grade_slope_is_clamped():
    assert tone.grade_slope(1.0) == tone.SLOPE_MAX  # absurdly hard grade
    assert tone.grade_slope(1e6) == tone.SLOPE_MIN


def test_curve_is_monotone_and_in_range():
    values = np.linspace(0.0, 1.0, 4097)
    for grade_r in (50.0, 85.0, 115.0, 150.0, 180.0):
        for snap in (-0.5, -0.2, 0.0, 0.2, 0.5):
            out = tone.curve_values(values, grade_r, snap)
            assert np.all(np.diff(out) >= -1e-9)
            assert out.min() >= 0.0 and out.max() <= 1.0


def test_curve_pins_the_endpoints():
    """Even the softest grade reaches full black and white."""
    for grade_r in (50.0, 115.0, 180.0):
        for snap in (-0.5, 0.5):
            out = tone.curve_values(np.array([0.0, 1.0]), grade_r, snap)
            assert out[0] == pytest.approx(0.0, abs=1e-6)
            assert out[1] == pytest.approx(1.0, abs=1e-6)


def test_curve_keeps_the_pivot_fixed():
    """The midtone pivot is the anchor both the grade slope and the snap
    bump vanish at; the endpoint rescale moves it only in the fifth
    decimal."""
    for grade_r in (50.0, 115.0, 180.0):
        for snap in (-0.5, 0.0, 0.5):
            out = tone.curve_values(np.array([0.5]), grade_r, snap)
            assert out[0] == pytest.approx(0.5, abs=1e-4)


def test_harder_grade_steepens_the_midtones():
    values = np.array([0.35, 0.65])
    soft = tone.curve_values(values, 180.0, 0.0)
    hard = tone.curve_values(values, 50.0, 0.0)
    assert hard[1] - hard[0] > soft[1] - soft[0]


def test_snap_steepens_without_moving_the_pivot():
    values = np.array([0.35, 0.5, 0.65])
    flat = tone.curve_values(values, 115.0, 0.0)
    snapped = tone.curve_values(values, 115.0, 0.4)
    assert snapped[2] - snapped[0] > flat[2] - flat[0]
    assert snapped[1] == pytest.approx(0.5, abs=1e-6)


def test_display_lut_composes_the_curve_over_the_flat_encode():
    lut = tone.build_display_lut(115.0, 0.1)
    flat = tone.curve_values(
        np.clip(1.0 - normalization.decode_normalized(np.arange(65536.0)), 0.0, 1.0),
        115.0,
        0.1,
    )
    np.testing.assert_allclose(lut, np.rint(flat * 255).astype(np.uint8))


def test_display_lut_endpoints_and_monotonicity():
    lut = tone.build_display_lut(90.0, 0.0)
    # The display domain is inverted: code 0 is the scene highlight.
    assert int(lut[0]) == 255
    assert int(lut[tone.MAX_CODE]) == 0
    assert np.all(np.diff(lut.astype(np.int32)) <= 0)
