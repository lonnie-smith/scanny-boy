"""Tests for preview colour adjustment (`color.py` and colour terms in `tone.py`)."""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from scanny_boy import color, normalization, tone


def _metering(
    *,
    ranges: tuple[float, float, float] = (1.0, 1.0, 1.0),
    shadow_refs_norm: tuple[float, float, float] | None = None,
) -> color.Metering:
    return color.Metering(ranges=ranges, shadow_refs_norm=shadow_refs_norm)


def test_neutral_tables_match_density_plan_lut():
    neutral_lut = tone.build_display_lut(tone.NEUTRAL)
    tables = tone.build_channel_tables(tone.NEUTRAL, color.NEUTRAL_COLOR, _metering())
    np.testing.assert_array_equal(neutral_lut, np.rint(tables[0] * 255).astype(np.uint8))
    np.testing.assert_array_equal(tables[0], tables[1])
    np.testing.assert_array_equal(tables[1], tables[2])


def test_magenta_slider_is_attenuated():
    """Magenta travel is weaker than before the gain; cyan and yellow are
    unchanged because their gains are 1.0."""
    metering = _metering(ranges=(1.0, 1.0, 1.0))
    cyan = color.cmy_offsets(
        dataclasses.replace(color.NEUTRAL_COLOR, wb_cyan=1.0), metering
    )
    yellow = color.cmy_offsets(
        dataclasses.replace(color.NEUTRAL_COLOR, wb_yellow=1.0), metering
    )
    magenta = color.cmy_offsets(
        dataclasses.replace(color.NEUTRAL_COLOR, wb_magenta=1.0), metering
    )

    assert cyan == color._luma_removed((color.CMY_MAX_DENSITY, 0.0, 0.0))
    assert yellow == color._luma_removed((0.0, 0.0, color.CMY_MAX_DENSITY))

    unscaled_magenta = color._luma_removed(
        (0.0, color.CMY_MAX_DENSITY, 0.0)
    )
    assert np.linalg.norm(magenta) < np.linalg.norm(unscaled_magenta)
    assert abs(magenta[0]) < abs(unscaled_magenta[0])
    assert abs(magenta[2]) < abs(unscaled_magenta[2])


def test_global_cmy_offsets_red_only():
    """A cyan-only slider still bites hardest where red's range is narrow —
    and the offsets are luma-neutral: all three channels move while Rec.709
    luma stays fixed."""
    params = dataclasses.replace(color.NEUTRAL_COLOR, wb_cyan=1.0)
    narrow = _metering(ranges=(0.5, 1.0, 1.0))
    wide = _metering(ranges=(1.0, 1.0, 1.0))
    offsets_narrow = color.cmy_offsets(params, narrow)
    offsets_wide = color.cmy_offsets(params, wide)

    assert offsets_narrow[0] > offsets_wide[0] > 0.0
    assert _luma_sum(offsets_narrow) == pytest.approx(0.0, abs=1e-12)
    assert offsets_narrow[1] < 0.0
    assert offsets_narrow[2] < 0.0


def test_regional_cmy_is_regional():
    params = dataclasses.replace(color.NEUTRAL_COLOR, shadow_cyan=1.0)
    tables = tone.build_channel_tables(tone.NEUTRAL, params, _metering())
    neutral = tone.build_channel_tables(tone.NEUTRAL, color.NEUTRAL_COLOR, _metering())
    low_delta = neutral[0, 52429] - tables[0, 52429]
    high_delta = neutral[0, 13107] - tables[0, 13107]
    assert low_delta > high_delta


def test_cast_removal_ties_shadow_reference():
    metering = _metering(
        ranges=(1.0, 1.0, 1.0),
        shadow_refs_norm=(0.2, 0.15, 0.15),
    )
    params = dataclasses.replace(color.NEUTRAL_COLOR, cast_removal=1.0)
    tables = tone.build_channel_tables(tone.NEUTRAL, params, metering)
    codes = np.arange(tone.MAX_CODE + 1, dtype=np.float64)
    norm = normalization.decode_normalized(codes)
    green_code = int(np.argmin(np.abs(norm - 0.15)))
    red_code = int(np.argmin(np.abs(norm - 0.2)))
    assert tables[0, red_code] == pytest.approx(tables[1, green_code], abs=0.02)
    pivot_code = int(np.argmin(np.abs(norm - 0.5)))
    assert tables[0, pivot_code] == pytest.approx(tables[1, pivot_code], abs=1e-5)


def test_no_metering_no_cast():
    params = dataclasses.replace(color.NEUTRAL_COLOR, cast_removal=1.0)
    meter = color.Metering(ranges=(1.0, 1.0, 1.0), shadow_refs_norm=None)
    tables = tone.build_channel_tables(tone.NEUTRAL, params, meter)
    neutral = tone.build_channel_tables(tone.NEUTRAL, color.NEUTRAL_COLOR, meter)
    np.testing.assert_array_equal(tables, neutral)


def test_apply_separation_spreads_and_collapses():
    pixel = np.array([[[0.2, 0.5, 0.8]]], dtype=np.float32)
    spread = color.apply_separation(
        pixel, dataclasses.replace(color.NEUTRAL_COLOR, dye_separation=1.5)
    )
    shrink = color.apply_separation(
        pixel, dataclasses.replace(color.NEUTRAL_COLOR, dye_separation=0.5)
    )
    identity = color.apply_separation(pixel, color.NEUTRAL_COLOR)
    assert spread.std() > pixel.std()
    assert shrink.std() < pixel.std()
    np.testing.assert_array_equal(identity, pixel)


def test_damping_reverses_by_chroma():
    k = 1.4
    grey_chroma = 0.0
    vivid_chroma = float(
        np.sqrt(((0.1 - 0.5) ** 2 + (0.5 - 0.9) ** 2 + (0.1 - 0.9) ** 2) / 3)
    )
    assert color.damping_gain(k, 1.0, grey_chroma) == pytest.approx(k, abs=1e-6)
    ref_gain = color.damping_gain(k, 1.0, color.SEPARATION_REF_SPREAD)
    vivid_gain = color.damping_gain(k, 1.0, vivid_chroma)
    assert grey_chroma < color.SEPARATION_REF_SPREAD < vivid_chroma
    assert color.damping_gain(k, 1.0, grey_chroma) > ref_gain > vivid_gain
    assert ref_gain == pytest.approx(1.0, abs=1e-6)


def test_kelvin_round_trips():
    for kelvin in (3000.0, 5500.0, 12000.0):
        m, y = color.kelvin_to_wb(kelvin, 0.1, -0.05)
        assert color.wb_to_kelvin(m, y) == pytest.approx(kelvin, rel=0.01)
    assert color.wb_to_kelvin(0.0, 0.0) == pytest.approx(5500.0)
    m_warm, y_warm = color.kelvin_to_wb(6500.0, 0.0, 0.0)
    assert m_warm > 0.0
    assert y_warm > 0.0
    m_cool, y_cool = color.kelvin_to_wb(4500.0, 0.0, 0.0)
    assert m_cool < 0.0
    assert y_cool < 0.0


def _luma_sum(triple: tuple[float, ...]) -> float:
    return color._luma_weighted_sum(triple)


# --- highlight metering ------------------------------------


def _metering_full(
    shadow_refs_norm, highlight_refs_norm=None
) -> color.Metering:
    return color.Metering(
        ranges=(1.0, 1.0, 1.0),
        shadow_refs_norm=shadow_refs_norm,
        highlight_refs_norm=highlight_refs_norm,
    )


def _code_for_norm(norm_value: float) -> int:
    codes = np.arange(tone.MAX_CODE + 1, dtype=np.float64)
    norm = normalization.decode_normalized(codes)
    return int(np.argmin(np.abs(norm - norm_value)))


def _display_at(
    channel: int, params: color.ColorParams, metering: color.Metering, x: float
) -> float:
    """The composed display value at one continuous display input — no code
    quantization, so tie assertions can hold to 1e-9 (a table lookup can
    only land within half a code of each reference, ~1.5e-5 apart)."""
    return float(
        tone.curve_values(
            np.array([x]),
            tone.NEUTRAL,
            params,
            channel=channel,
            metering=metering,
        )[0]
    )


def test_neutral_colour_is_byte_identical():
    """§4 test 1: at every slider zero, both offset functions return all
    zeros and the tables are what the achromatic path returns — the
    neutral-is-identical invariant."""
    metering = _metering_full((0.2, 0.15, 0.15), (0.9, 0.85, 0.85))
    assert color.cmy_offsets(color.NEUTRAL_COLOR, metering) == (0.0, 0.0, 0.0)
    shadow, highlight = color.region_cmy(color.NEUTRAL_COLOR)
    assert shadow == (0.0, 0.0, 0.0)
    assert highlight == (0.0, 0.0, 0.0)
    tables = tone.build_channel_tables(tone.NEUTRAL, color.NEUTRAL_COLOR, metering)
    achromatic = tone.build_channel_tables(tone.NEUTRAL, color.NEUTRAL_COLOR, None)
    np.testing.assert_array_equal(tables, achromatic)


def test_global_cmy_is_lightness_neutral_over_random_triples():
    """For random slider triples and unequal ranges, the offsets are luma-
    neutral to 1e-12 — filtration changes hue, never Rec.709 luma."""
    rng = np.random.default_rng(11)
    for _ in range(64):
        sliders = rng.uniform(-1.0, 1.0, 3)
        params = color.ColorParams(
            wb_cyan=float(sliders[0]),
            wb_magenta=float(sliders[1]),
            wb_yellow=float(sliders[2]),
        )
        metering = _metering(ranges=(0.6, 1.0, 0.8))
        offsets = color.cmy_offsets(params, metering)
        assert _luma_sum(offsets) == pytest.approx(0.0, abs=1e-12)


def test_global_cmy_equal_move_is_a_hue_move_with_unequal_ranges():
    """§4 test 4 — §1.1's stated consequence, asserted so it cannot be
    'fixed' by accident: an equal three-slider move with unequal ranges
    sums to zero but is NOT three equal offsets."""
    params = color.ColorParams(wb_cyan=0.5, wb_magenta=0.5, wb_yellow=0.5)
    offsets = color.cmy_offsets(params, _metering(ranges=(0.5, 1.0, 1.0)))
    assert _luma_sum(offsets) == pytest.approx(0.0, abs=1e-12)
    assert offsets[0] != pytest.approx(offsets[1], abs=1e-9)
    assert offsets[0] != pytest.approx(offsets[2], abs=1e-9)


def test_regional_cmy_is_lightness_neutral_and_exactly_cancels():
    """Each returned triple is luma-neutral; equal gained sliders cancel;
    shadow and highlight trims differ at midtone."""
    params = dataclasses.replace(
        color.NEUTRAL_COLOR,
        shadow_cyan=0.8,
        shadow_magenta=-0.3,
        shadow_yellow=0.1,
        highlight_cyan=-0.6,
        highlight_magenta=0.4,
        highlight_yellow=0.2,
    )
    shadow, highlight = color.region_cmy(params)
    assert _luma_sum(shadow) == pytest.approx(0.0, abs=1e-12)
    assert _luma_sum(highlight) == pytest.approx(0.0, abs=1e-12)

    equal_gained = color.ColorParams(
        shadow_cyan=0.5, shadow_magenta=1.0, shadow_yellow=0.5
    )
    assert color.region_cmy(equal_gained)[0] == (0.0, 0.0, 0.0)

    shadow_only = dataclasses.replace(
        color.NEUTRAL_COLOR, shadow_yellow=1.0
    )
    highlight_only = dataclasses.replace(
        color.NEUTRAL_COLOR, highlight_yellow=1.0
    )
    quarter = 0.25
    shadow_out = _display_at(2, shadow_only, _metering(), quarter)
    highlight_out = _display_at(2, highlight_only, _metering(), quarter)
    assert shadow_out != pytest.approx(highlight_out, abs=1e-6)


def test_regional_cmy_matches_global_strength_at_zone_centres():
    """Full-travel shadow yellow at the quarter tone is comparable to full-
    travel global yellow at the midtone on the default grade."""
    metering = _metering()
    neutral = tone.build_channel_tables(
        tone.NEUTRAL, color.NEUTRAL_COLOR, metering
    )
    global_tables = tone.build_channel_tables(
        tone.NEUTRAL, color.ColorParams(wb_yellow=1.0), metering
    )
    shadow_tables = tone.build_channel_tables(
        tone.NEUTRAL, color.ColorParams(shadow_yellow=1.0), metering
    )
    code_mid = int(np.argmin(np.abs(neutral[0] - 0.5)))
    code_sh = int(np.argmin(np.abs(neutral[0] - 0.25)))
    global_delta = abs(global_tables[2, code_mid] - neutral[2, code_mid])
    shadow_delta = abs(shadow_tables[2, code_sh] - neutral[2, code_sh])
    assert global_delta > 0.02
    assert shadow_delta > 0.02
    assert shadow_delta == pytest.approx(global_delta, rel=0.30)


def test_one_point_parity_is_byte_for_byte():
    """§4 test 6: with the highlight reference absent, or present with the
    highlight strength at rest, `cast_slopes` returns exactly the
    hand-computed pre-change values.

    shadow_refs_norm (0.2, 0.15, 0.15) -> display red 0.8, green 0.85,
    blue 0.85; slope 1.55, pivot 0.5. Red: offset -0.05, target 0.8,
    slope_ch = 1.55*(0.5-0.85)/(0.5-0.8) = 1.8083...; pivot stays 0.5.
    Blue ties green's own reference, so it is the unchanged line."""
    expected = (
        (1.55 * (0.5 - 0.85) / (0.5 - 0.8), 0.5),
        (1.55, 0.5),
        (1.55, 0.5),
    )
    params = dataclasses.replace(color.NEUTRAL_COLOR, cast_removal=1.0)
    without_refs = color.cast_slopes(
        params, _metering_full((0.2, 0.15, 0.15)), 1.55, 0.5
    )
    for (slope_ch, pivot_ch), (slope_want, pivot_want) in zip(
        without_refs, expected, strict=True
    ):
        assert slope_ch == pytest.approx(slope_want)
        assert pivot_ch == pytest.approx(pivot_want)
    with_refs_strength_zero = color.cast_slopes(
        params,
        _metering_full((0.2, 0.15, 0.15), (0.9, 0.85, 0.85)),
        1.55,
        0.5,
    )
    assert with_refs_strength_zero == without_refs


def _two_point_metering():
    """Both ends off green's references: display shadow red 0.8 / green
    0.85, display highlight red 0.1 / green 0.15."""
    return _metering_full((0.2, 0.15, 0.15), (0.9, 0.85, 0.85))


def test_two_point_ties_both_ends():
    """§4 test 7: with both strengths at 1, the red table prints at the
    shadow target what green prints at green's shadow reference, and at the
    highlight target what green prints at green's highlight reference."""
    params = dataclasses.replace(
        color.NEUTRAL_COLOR, cast_removal=1.0, cast_removal_highlights=1.0
    )
    metering = _two_point_metering()
    assert _display_at(0, params, metering, 0.8) == pytest.approx(
        _display_at(1, params, metering, 0.85), abs=1e-9
    )
    assert _display_at(0, params, metering, 0.1) == pytest.approx(
        _display_at(1, params, metering, 0.15), abs=1e-9
    )


def test_two_point_corrects_a_pure_offset():
    """§4 test 8: a constant per-channel offset (r_s - g_s == r_h - g_h in
    display units) leaves the two-point slope at the base slope and moves
    the pivot, while the one-point solve can only change the slope."""
    metering = _metering_full((0.10, 0.15, 0.15), (0.80, 0.85, 0.85))
    two_point = color.cast_slopes(
        dataclasses.replace(
            color.NEUTRAL_COLOR, cast_removal=1.0, cast_removal_highlights=1.0
        ),
        metering,
        1.55,
        0.5,
    )
    slope_ch, pivot_ch = two_point[0]
    assert slope_ch == pytest.approx(1.55)
    assert pivot_ch == pytest.approx(0.55)
    one_point = color.cast_slopes(
        dataclasses.replace(color.NEUTRAL_COLOR, cast_removal=1.0),
        _metering_full((0.10, 0.15, 0.15)),
        1.55,
        0.5,
    )
    assert one_point[0][0] != pytest.approx(1.55)


def test_both_ends_bounded_at_cast_max_offset():
    """§4 test 9: references 10x past CAST_MAX_OFFSET give exactly the
    tables of references at the limit, on each end independently — the
    clamp, not the reference, decides the target."""
    params = dataclasses.replace(
        color.NEUTRAL_COLOR, cast_removal=1.0, cast_removal_highlights=1.0
    )
    # Highlight end: display offset +1.0 clips to +0.1, matching +0.1.
    far = _metering_full((0.2, 0.15, 0.15), (1.0 - 1.15, 0.85, 0.85))
    at_limit = _metering_full((0.2, 0.15, 0.15), (0.75, 0.85, 0.85))
    np.testing.assert_array_equal(
        tone.build_channel_tables(tone.NEUTRAL, params, far),
        tone.build_channel_tables(tone.NEUTRAL, params, at_limit),
    )
    # Shadow end: display offset +1.0 clips to +0.1, matching +0.1.
    far = _metering_full((1.0 - 1.85, 0.15, 0.15), (0.9, 0.85, 0.85))
    at_limit = _metering_full((0.05, 0.15, 0.15), (0.9, 0.85, 0.85))
    np.testing.assert_array_equal(
        tone.build_channel_tables(tone.NEUTRAL, params, far),
        tone.build_channel_tables(tone.NEUTRAL, params, at_limit),
    )


def test_highlight_only_strength_runs_the_two_point_branch():
    """§4 test 10: strength_s = 0 with strength_h = 1 is legal and ties the
    highlight end (the shadow target collapses onto green's own
    reference); the two-point branch is what runs."""
    metering = _two_point_metering()
    params = dataclasses.replace(
        color.NEUTRAL_COLOR, cast_removal=0.0, cast_removal_highlights=1.0
    )
    # The tie is exact on the raw line (equal raws in, and here the slopes
    # differ, so the shaped curve only approximates it — the knee
    # sharpness follows the per-channel slope).
    pairs = color.cast_slopes(params, metering, 1.55, 0.5)
    raw_red = 0.5 + pairs[0][0] * (0.1 - pairs[0][1])
    raw_green = 0.5 + 1.55 * (0.15 - 0.5)
    assert raw_red == pytest.approx(raw_green, abs=1e-12)
    assert _display_at(0, params, metering, 0.1) == pytest.approx(
        _display_at(1, params, metering, 0.15), abs=0.01
    )
    # And the two-point branch moved the red line off the one-point answer.
    assert _display_at(0, params, metering, 0.8) != pytest.approx(
        _display_at(1, params, metering, 0.85), abs=1e-6
    )


def test_shadow_only_strength_runs_the_one_point_branch():
    """§4 test 10: strength_h = 0 is guard 3 — today's one-point solve,
    which ties the shadow end and pins the pivot."""
    metering = _two_point_metering()
    params = dataclasses.replace(
        color.NEUTRAL_COLOR, cast_removal=1.0, cast_removal_highlights=0.0
    )
    # The tie is exact on the raw line; the shaped curve approximates it
    # (the one-point slope_ch differs from green's, and the knee
    # sharpness follows the per-channel slope).
    pairs = color.cast_slopes(params, metering, 1.55, 0.5)
    raw_red = 0.5 + pairs[0][0] * (0.8 - pairs[0][1])
    raw_green = 0.5 + 1.55 * (0.85 - 0.5)
    assert raw_red == pytest.approx(raw_green, abs=1e-12)
    assert _display_at(0, params, metering, 0.8) == pytest.approx(
        _display_at(1, params, metering, 0.85), abs=0.02
    )
    assert _display_at(0, params, metering, 0.1) != pytest.approx(
        _display_at(1, params, metering, 0.15), abs=1e-6
    )
    # The pivot never moved: the one-point solve pins it.
    assert pairs[0][1] == pytest.approx(0.5)


def test_slope_clamp_keeps_the_shadow_tie():
    """§4 test 11: refs chosen so slope_ch saturates at SLOPE_MAX — after
    the clamp the pivot is re-solved through the shadow constraint, so the
    shadow tie still holds exactly and only the highlight tie degrades.

    Display: green shadow 0.85, green highlight 0.64; red targets 0.75 and
    0.74, so slope_ch = 1.55*0.21/0.01 and clamps to 4.0."""
    metering = _metering_full((0.25, 0.15, 0.15), (0.26, 0.36, 0.36))
    params = dataclasses.replace(
        color.NEUTRAL_COLOR, cast_removal=1.0, cast_removal_highlights=1.0
    )
    pairs = color.cast_slopes(params, metering, 1.55, 0.5)
    assert pairs[0][0] == pytest.approx(tone.SLOPE_MAX)
    assert pairs[0][1] == pytest.approx(0.75 - (1.55 / tone.SLOPE_MAX) * 0.35)

    # The shadow tie holds exactly on the raw line — pivot_out +
    # slope_ch*(t_s - pivot_ch) equals green's raw at g_s. The shaped
    # curve only approximates it (the knee sharpness follows the
    # per-channel slope), and the highlight tie degrades by design.
    raw_red = 0.5 + pairs[0][0] * (0.75 - pairs[0][1])
    raw_green = 0.5 + 1.55 * (0.85 - 0.5)
    assert raw_red == pytest.approx(raw_green, abs=1e-12)
    raw_red_highlight = 0.5 + pairs[0][0] * (0.74 - pairs[0][1])
    raw_green_highlight = 0.5 + 1.55 * (0.64 - 0.5)
    assert raw_red_highlight != pytest.approx(raw_green_highlight, abs=1e-4)


@pytest.mark.parametrize(
    "params",
    [
        dataclasses.replace(color.NEUTRAL_COLOR, cast_removal=1.0),
        dataclasses.replace(
            color.NEUTRAL_COLOR, cast_removal=1.0, cast_removal_highlights=1.0
        ),
        dataclasses.replace(color.NEUTRAL_COLOR, cast_removal_highlights=1.0),
    ],
)
def test_green_is_never_touched(params):
    """§4 test 12: in every branch, green's table is the achromatic one."""
    metering = _two_point_metering()
    tables = tone.build_channel_tables(tone.NEUTRAL, params, metering)
    neutral = tone.build_channel_tables(tone.NEUTRAL, color.NEUTRAL_COLOR, metering)
    np.testing.assert_array_equal(tables[1], neutral[1])


def test_collapsed_references_fall_back_to_the_one_point_solve():
    """§4 test 13: t_h == t_s leaves the line undetermined — that channel
    falls back to the one-point solve. Green's refs sit at the anchor at
    both ends and red carries an equal +0.05 display offset, so red's
    targets collapse."""
    metering = _metering_full((0.45, 0.5, 0.5), (0.45, 0.5, 0.5))
    params = dataclasses.replace(
        color.NEUTRAL_COLOR, cast_removal=1.0, cast_removal_highlights=1.0
    )
    pairs = color.cast_slopes(params, metering, 1.55, 0.5)
    # One-point with green_ref == anchor: slope_ch = 0 clamps to SLOPE_MIN.
    assert pairs[0] == pytest.approx((tone.SLOPE_MIN, 0.5))


def test_missing_shadow_reference_is_inert_for_any_strengths():
    """§4 test 13: guard 2 — no shadow reference, no solve, at any
    strengths."""
    params = dataclasses.replace(
        color.NEUTRAL_COLOR, cast_removal=1.0, cast_removal_highlights=1.0
    )
    achromatic = ((1.55, 0.5),) * 3
    assert color.cast_slopes(params, _metering_full(None, (0.9, 0.85, 0.85)), 1.55, 0.5) == (
        achromatic
    )


@pytest.mark.parametrize(
    "cast_removal,cast_removal_highlights",
    [(0.0, 0.0), (0.0, 1.0), (1.0, 0.0), (1.0, 1.0), (0.3, 0.7)],
)
def test_colour_tables_monotone_and_in_range(cast_removal, cast_removal_highlights):
    """§4 test 14, extended with the new corner: every table is monotone
    non-increasing in the code and within [0, 1]."""
    metering = _two_point_metering()
    params = dataclasses.replace(
        color.NEUTRAL_COLOR,
        cast_removal=cast_removal,
        cast_removal_highlights=cast_removal_highlights,
        wb_cyan=0.5,
        shadow_magenta=-0.4,
        highlight_yellow=0.3,
    )
    tables = tone.build_channel_tables(tone.NEUTRAL, params, metering)
    for ch in range(3):
        assert np.all(np.diff(tables[ch]) <= 1e-9)
        assert tables[ch].min() >= 0.0 and tables[ch].max() <= 1.0


def test_base_slope_and_pivot_reproduce_the_curve_inputs():
    """§4 test 15: the extracted pair reproduces the numbers `_curve_raw`
    used before the extraction, across the grade and density box — and the
    curve still maps the pivot to midtone grey at the neutral shaping."""
    for grade_r in (50.0, 115.0, 180.0):
        for density in (0.0, 0.5, 1.0, 1.5, 2.0):
            params = dataclasses.replace(
                tone.NEUTRAL, grade_r=grade_r, density=density
            )
            slope, pivot_in = tone.base_slope_and_pivot(params)
            assert slope == tone.grade_slope(grade_r)
            assert pivot_in == pytest.approx(0.5 + (density - 1.0) * 0.2)
            out = tone.curve_values(np.array([pivot_in]), params)
            assert out[0] == pytest.approx(0.5, abs=2e-3)
