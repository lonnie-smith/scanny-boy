"""Tests for the preview tone curve (`tone.py`): the grade-to-slope
mapping, density/zone/knee shaping, monotonicity, and the composed
display LUT."""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from scanny_boy import normalization, tone

# Key codes from the pre-density LUT at NEUTRAL — the acceptance anchor for
# bit-identical neutral behaviour.
_NEUTRAL_LUT_SAMPLES = {
    0: 255,
    1: 255,
    16384: 245,
    32768: 137,
    49152: 21,
    65534: 0,
    65535: 0,
}


@pytest.mark.parametrize(
    "grade_r,expected",
    [
        (115.0, 1.55),
        (50.0, 1.55 * 115.0 / 50.0),
        (180.0, 1.55 * 115.0 / 180.0),
    ],
)
def test_grade_slope_scales_with_the_paper_range(grade_r, expected):
    assert tone.grade_slope(grade_r) == pytest.approx(expected)


def test_grade_slope_is_clamped():
    assert tone.grade_slope(1.0) == tone.SLOPE_MAX
    assert tone.grade_slope(1e6) == tone.SLOPE_MIN


def test_neutral_lut_is_unchanged():
    lut = tone.build_display_lut(tone.NEUTRAL)
    for code, expected in _NEUTRAL_LUT_SAMPLES.items():
        assert int(lut[code]) == expected


def test_density_darkens_the_midtones():
    values = np.array([0.5])
    neutral = tone.curve_values(values, tone.NEUTRAL)[0]
    darker = tone.curve_values(
        values, dataclasses.replace(tone.NEUTRAL, density=1.5)
    )[0]
    brighter = tone.curve_values(
        values, dataclasses.replace(tone.NEUTRAL, density=0.5)
    )[0]
    assert darker < neutral < brighter


def test_density_does_not_rotate_the_midtone_slope():
    probe = np.array([0.45, 0.55])
    slopes = []
    for density in (0.5, 1.0, 1.5):
        params = dataclasses.replace(tone.NEUTRAL, density=density)
        out = tone.curve_values(probe, params)
        slopes.append(out[1] - out[0])
    assert slopes[0] == pytest.approx(slopes[1], abs=2e-3)
    assert slopes[1] == pytest.approx(slopes[2], abs=2e-3)


def test_shadow_density_is_mid_sparing():
    params = dataclasses.replace(tone.NEUTRAL, shadow_density=0.9)
    neutral = tone.curve_values(np.linspace(0.0, 1.0, 101), tone.NEUTRAL)
    shadowed = tone.curve_values(np.linspace(0.0, 1.0, 101), params)
    delta = shadowed - neutral
    assert abs(delta[25]) > 0.08  # quarter tone
    assert abs(delta[50]) < 0.03  # midtone
    assert abs(delta[85]) < 0.01  # highlight


def test_highlight_density_is_mid_sparing():
    params = dataclasses.replace(tone.NEUTRAL, highlight_density=0.5)
    neutral = tone.curve_values(np.linspace(0.0, 1.0, 101), tone.NEUTRAL)
    highlighted = tone.curve_values(np.linspace(0.0, 1.0, 101), params)
    delta = highlighted - neutral
    assert abs(delta[75]) > 0.07  # three-quarter tone
    assert abs(delta[50]) < 0.03
    assert abs(delta[15]) < 0.01


def test_zone_density_signs():
    values = np.array([0.25, 0.75])
    neutral = tone.curve_values(values, tone.NEUTRAL)
    for sign in (-1.0, 1.0):
        shadow = tone.curve_values(
            values,
            dataclasses.replace(tone.NEUTRAL, shadow_density=0.5 * sign),
        )
        assert (shadow[0] - neutral[0]) * sign < 0
        highlight = tone.curve_values(
            values,
            dataclasses.replace(tone.NEUTRAL, highlight_density=0.5 * sign),
        )
        assert (highlight[1] - neutral[1]) * sign < 0


def test_toe_lifts_the_black_and_shoulder_holds_the_white():
    toe_params = dataclasses.replace(tone.NEUTRAL, toe=1.0)
    shoulder_params = dataclasses.replace(tone.NEUTRAL, shoulder=1.0)
    toe_out = tone.curve_values(np.array([0.0, 0.5, 1.0]), toe_params)
    shoulder_out = tone.curve_values(np.array([0.0, 0.5, 1.0]), shoulder_params)
    assert toe_out[0] == pytest.approx(tone.TOE_HEIGHT, abs=0.02)
    assert shoulder_out[2] == pytest.approx(1.0 - tone.SHOULDER_HEIGHT, abs=0.02)
    assert toe_out[1] == pytest.approx(0.5, abs=0.02)
    assert shoulder_out[1] == pytest.approx(0.5, abs=0.02)


def test_negative_toe_and_shoulder_sharpen_without_moving_bounds():
    values = np.linspace(0.0, 1.0, 101)
    neutral = tone.curve_values(values, tone.NEUTRAL)
    sharp_toe = tone.curve_values(
        values, dataclasses.replace(tone.NEUTRAL, toe=-1.0)
    )
    sharp_shoulder = tone.curve_values(
        values, dataclasses.replace(tone.NEUTRAL, shoulder=-1.0)
    )
    assert sharp_toe[0] == pytest.approx(0.0, abs=1e-6)
    assert sharp_shoulder[-1] == pytest.approx(1.0, abs=1e-6)
    assert sharp_toe[10] < neutral[10]
    assert sharp_shoulder[90] > neutral[90]


def test_toe_width_widens_the_knee():
    narrow = tone.curve_values(
        np.array([0.3]),
        dataclasses.replace(tone.NEUTRAL, toe=0.5, toe_width=0.5),
    )[0]
    wide = tone.curve_values(
        np.array([0.3]),
        dataclasses.replace(tone.NEUTRAL, toe=0.5, toe_width=5.0),
    )[0]
    straight = 0.3
    assert abs(narrow - straight) > abs(wide - straight)


def test_curve_is_monotone_and_in_range_over_the_parameter_box():
    values = np.linspace(0.0, 1.0, 4097)
    grades = (50.0, 85.0, 115.0, 150.0, 180.0)
    snaps = (-0.5, -0.2, 0.0, 0.2, 0.5)
    densities = (0.0, 1.0, 2.0)
    shadow_densities = (-0.9, 0.0, 0.9)
    highlight_densities = (-0.5, 0.0, 0.5)
    toes = (-1.0, 0.0, 1.0)
    toe_widths = (0.1, 2.5, 5.0)
    shoulders = (-1.0, 0.0, 1.0)
    shoulder_widths = (0.1, 2.5, 5.0)
    for grade_r in grades:
        for snap in snaps:
            for density in densities:
                for shadow_density in shadow_densities:
                    for highlight_density in highlight_densities:
                        for toe in toes:
                            for toe_width in toe_widths:
                                for shoulder in shoulders:
                                    for shoulder_width in shoulder_widths:
                                        params = tone.ToneParams(
                                            grade_r=grade_r,
                                            snap_gamma=snap,
                                            density=density,
                                            shadow_density=shadow_density,
                                            highlight_density=highlight_density,
                                            toe=toe,
                                            toe_width=toe_width,
                                            shoulder=shoulder,
                                            shoulder_width=shoulder_width,
                                        )
                                        out = tone.curve_values(values, params)
                                        assert np.all(np.diff(out) >= -1e-9)
                                        assert out.min() >= 0.0 and out.max() <= 1.0


def test_zone_constants_satisfy_monotonicity_bound():
    bound = (
        (abs(tone.SHADOW_DENSITY_MAX) + abs(tone.HIGHLIGHT_DENSITY_MAX))
        * tone.ZONE_DENSITY_SCALE
        * tone.ZONE_SHARPNESS
        / 4
    )
    assert bound < 1.0


def test_curve_pins_endpoints_at_neutral_shaping():
    for grade_r in (50.0, 115.0, 180.0):
        for snap in (-0.5, 0.5):
            params = dataclasses.replace(
                tone.NEUTRAL, grade_r=grade_r, snap_gamma=snap
            )
            out = tone.curve_values(np.array([0.0, 1.0]), params)
            assert out[0] == pytest.approx(0.0, abs=1e-6)
            assert out[1] == pytest.approx(1.0, abs=1e-6)


def test_curve_keeps_the_pivot_fixed_at_neutral_shaping():
    for grade_r in (50.0, 115.0, 180.0):
        for snap in (-0.5, 0.0, 0.5):
            params = dataclasses.replace(
                tone.NEUTRAL, grade_r=grade_r, snap_gamma=snap
            )
            out = tone.curve_values(np.array([0.5]), params)
            assert out[0] == pytest.approx(0.5, abs=1e-4)


def test_harder_grade_steepens_the_midtones():
    values = np.array([0.35, 0.65])
    soft = tone.curve_values(values, dataclasses.replace(tone.NEUTRAL, grade_r=180.0))
    hard = tone.curve_values(values, dataclasses.replace(tone.NEUTRAL, grade_r=50.0))
    assert hard[1] - hard[0] > soft[1] - soft[0]


def test_snap_steepens_without_moving_the_pivot():
    values = np.array([0.35, 0.5, 0.65])
    flat = tone.curve_values(values, tone.NEUTRAL)
    snapped = tone.curve_values(
        values, dataclasses.replace(tone.NEUTRAL, snap_gamma=0.4)
    )
    assert snapped[2] - snapped[0] > flat[2] - flat[0]
    assert snapped[1] == pytest.approx(0.5, abs=1e-6)


def test_display_lut_composes_the_curve_over_the_flat_encode():
    params = dataclasses.replace(tone.NEUTRAL, snap_gamma=0.1)
    lut = tone.build_display_lut(params)
    flat = tone.curve_values(
        np.clip(1.0 - normalization.decode_normalized(np.arange(65536.0)), 0.0, 1.0),
        params,
    )
    np.testing.assert_allclose(lut, np.rint(flat * 255).astype(np.uint8))


def test_display_lut_endpoints_and_monotonicity():
    lut = tone.build_display_lut(dataclasses.replace(tone.NEUTRAL, grade_r=90.0))
    assert int(lut[0]) == 255
    assert int(lut[tone.MAX_CODE]) == 0
    assert np.all(np.diff(lut.astype(np.int32)) <= 0)
