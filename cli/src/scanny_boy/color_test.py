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
    np.testing.assert_array_equal(
        neutral_lut, np.rint(tables[0] * 255).astype(np.uint8)
    )
    np.testing.assert_array_equal(tables[0], tables[1])
    np.testing.assert_array_equal(tables[1], tables[2])


def test_warmth_slider_shifts_red_up_blue_down():
    """Full warmth: red channel density offset is negative (brighter display),
    blue is positive, green is zero."""
    params = dataclasses.replace(color.NEUTRAL_COLOR, warmth=1.0)
    offsets = color.balance_offsets(params)
    assert offsets[0] < 0.0  # red density down = display up
    assert offsets[2] > 0.0  # blue density up = display down
    assert offsets[1] == pytest.approx(0.0, abs=1e-12)


def test_tint_slider_shifts_green_down():
    """Full tint: green channel density offset is positive (darker display),
    red and blue are negative."""
    params = dataclasses.replace(color.NEUTRAL_COLOR, tint=1.0)
    offsets = color.balance_offsets(params)
    assert offsets[1] > 0.0  # green density up = display down
    assert offsets[0] < 0.0  # red density down = display up
    assert offsets[2] < 0.0  # blue density down = display up


def test_warmth_shifts_red_up_blue_down_and_is_luma_neutral():
    """A warmth slider moves red and blue in opposite directions while
    Rec.709 luma stays fixed."""
    params = dataclasses.replace(color.NEUTRAL_COLOR, warmth=1.0)
    offsets = color.balance_offsets(params)
    assert offsets[0] < 0.0  # red density down = display up
    assert offsets[2] > 0.0  # blue density up = display down
    assert _luma_sum(offsets) == pytest.approx(0.0, abs=1e-12)


def test_warmth_shifts_tables():
    """A warmth slider moves the red and blue tables away from neutral."""
    params = dataclasses.replace(color.NEUTRAL_COLOR, warmth=1.0)
    tables = tone.build_channel_tables(tone.NEUTRAL, params, _metering())
    neutral = tone.build_channel_tables(tone.NEUTRAL, color.NEUTRAL_COLOR, _metering())
    mid = 32768
    assert tables[0, mid] != pytest.approx(neutral[0, mid], abs=1e-6)
    assert tables[2, mid] != pytest.approx(neutral[2, mid], abs=1e-6)


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
    gain = color.SEPARATION_DAMPING_GAIN
    grey_chroma = 0.0
    muted_chroma = 0.05
    vivid_chroma = float(
        np.sqrt(((0.1 - 0.5) ** 2 + (0.5 - 0.9) ** 2 + (0.1 - 0.9) ** 2) / 3)
    )
    grey_gain = color.damping_gain(k, 1.0, grey_chroma)
    muted_gain = color.damping_gain(k, 1.0, muted_chroma)
    ref_gain = color.damping_gain(k, 1.0, color.SEPARATION_REF_SPREAD)
    vivid_gain = color.damping_gain(k, 1.0, vivid_chroma)
    assert grey_chroma < muted_chroma < color.SEPARATION_REF_SPREAD < vivid_chroma
    assert grey_gain == pytest.approx(k * gain, abs=1e-6)
    assert muted_gain > k
    assert grey_gain > muted_gain > ref_gain > vivid_gain
    assert ref_gain == pytest.approx(1.0, abs=1e-6)


def test_damping_zero_is_the_flat_k():
    for k in (0.5, 1.0, 1.4):
        for chroma in (0.0, 0.3, 0.5):
            assert color.damping_gain(k, 0.0, chroma) == pytest.approx(k, abs=1e-6)


def test_identity_k_is_inert_at_any_chroma():
    for damping in (0.0, 0.5, 1.0):
        for chroma in (0.0, 0.2, color.SEPARATION_REF_SPREAD, 0.5):
            assert color.damping_gain(1.0, damping, chroma) == pytest.approx(
                1.0, abs=1e-6
            )


def test_damping_chroma_transfer_is_monotone():
    """Non-monotone chroma means two pixels swap which reads as more saturated."""
    cs = np.linspace(0.0, 0.5, 2000)
    for k in (0.5, 1.0, 1.3, 1.5):
        for damping in (0.5, 1.0):
            out = np.array([c * color.damping_gain(k, damping, c) for c in cs])
            assert np.diff(out).min() >= -1e-12, (
                f"non-monotone at k={k}, damping={damping}"
            )


def test_apply_separation_damping_matches_gain():
    muted = np.array([[[0.48, 0.50, 0.52]]], dtype=np.float32)
    vivid = np.array([[[0.1, 0.5, 0.9]]], dtype=np.float32)
    params = dataclasses.replace(
        color.NEUTRAL_COLOR, dye_separation=1.3, separation_damping=1.0
    )
    for pixel in (muted, vivid):
        out = color.apply_separation(pixel, params)
        luma = (
            color.LUMA_WEIGHTS[0] * pixel[..., 0]
            + color.LUMA_WEIGHTS[1] * pixel[..., 1]
            + color.LUMA_WEIGHTS[2] * pixel[..., 2]
        )
        diff = pixel - luma[..., np.newaxis]
        chroma = float(np.sqrt(np.sum(diff**2) / 3.0))
        k_eff = color.damping_gain(
            params.dye_separation, params.separation_damping, chroma
        )
        expected = luma[..., np.newaxis] + k_eff * diff
        np.testing.assert_allclose(out, expected, rtol=1e-6)


def _luma_sum(triple: tuple[float, ...]) -> float:
    return color._luma_weighted_sum(triple)


# --- highlight metering ------------------------------------


def _metering_full(shadow_refs_norm, highlight_refs_norm=None) -> color.Metering:
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
    """§4 test 1: at every slider zero, the balance offsets are all zeros
    and the tables are what the achromatic path returns — the
    neutral-is-identical invariant."""
    metering = _metering_full((0.2, 0.15, 0.15), (0.9, 0.85, 0.85))
    assert color.balance_offsets(color.NEUTRAL_COLOR) == pytest.approx(
        (0.0, 0.0, 0.0)
    )
    tables = tone.build_channel_tables(tone.NEUTRAL, color.NEUTRAL_COLOR, metering)
    achromatic = tone.build_channel_tables(tone.NEUTRAL, color.NEUTRAL_COLOR, None)
    np.testing.assert_array_equal(tables, achromatic)


def test_balance_offsets_are_luma_neutral_over_random_triples():
    """For random warmth/tint pairs, the offsets are luma-neutral to 1e-12 —
    filtration changes hue, never Rec.709 luma."""
    rng = np.random.default_rng(11)
    for _ in range(64):
        w = float(rng.uniform(-1.0, 1.0))
        t = float(rng.uniform(-1.0, 1.0))
        params = dataclasses.replace(color.NEUTRAL_COLOR, warmth=w, tint=t)
        offsets = color.balance_offsets(params)
        assert _luma_sum(offsets) == pytest.approx(0.0, abs=1e-12)


def test_balance_equal_warmth_and_tint_move_is_luma_neutral():
    """An equal warmth and tint move sums to luma-neutral offsets."""
    params = dataclasses.replace(color.NEUTRAL_COLOR, warmth=0.5, tint=0.5)
    offsets = color.balance_offsets(params)
    assert _luma_sum(offsets) == pytest.approx(0.0, abs=1e-12)
    assert offsets[0] != pytest.approx(offsets[1], abs=1e-9)
    assert offsets[0] != pytest.approx(offsets[2], abs=1e-9)


def test_balance_offsets_are_luma_neutral_and_compose():
    """Warmth and tint offsets are individually luma-neutral and compose
    additively."""
    params = dataclasses.replace(color.NEUTRAL_COLOR, warmth=0.5, tint=-0.3)
    offsets = color.balance_offsets(params)
    assert _luma_sum(offsets) == pytest.approx(0.0, abs=1e-12)

    warmth_only = color.balance_offsets(
        dataclasses.replace(color.NEUTRAL_COLOR, warmth=0.5)
    )
    tint_only = color.balance_offsets(
        dataclasses.replace(color.NEUTRAL_COLOR, tint=-0.3)
    )
    composed = color.balance_offsets(params)
    assert composed[0] == pytest.approx(warmth_only[0] + tint_only[0], abs=1e-12)
    assert composed[1] == pytest.approx(warmth_only[1] + tint_only[1], abs=1e-12)
    assert composed[2] == pytest.approx(warmth_only[2] + tint_only[2], abs=1e-12)


def test_balance_composition_shifts_tables_differently():
    """Warmth-only and tint-only produce different table shifts at the
    midtone, confirming they are independent axes."""
    metering = _metering()
    warm_tables = tone.build_channel_tables(
        tone.NEUTRAL, dataclasses.replace(color.NEUTRAL_COLOR, warmth=1.0), metering
    )
    tint_tables = tone.build_channel_tables(
        tone.NEUTRAL, dataclasses.replace(color.NEUTRAL_COLOR, tint=1.0), metering
    )
    code_mid = 32768
    assert warm_tables[0, code_mid] != pytest.approx(tint_tables[0, code_mid], abs=1e-4)
    assert warm_tables[2, code_mid] != pytest.approx(tint_tables[2, code_mid], abs=1e-4)


def test_warmth_and_tint_affect_different_channels():
    """Warmth primarily shifts red/blue; tint primarily shifts green.
    Both produce measurable table changes."""
    metering = _metering()
    neutral = tone.build_channel_tables(tone.NEUTRAL, color.NEUTRAL_COLOR, metering)
    warm_tables = tone.build_channel_tables(
        tone.NEUTRAL, dataclasses.replace(color.NEUTRAL_COLOR, warmth=1.0), metering
    )
    tint_tables = tone.build_channel_tables(
        tone.NEUTRAL, dataclasses.replace(color.NEUTRAL_COLOR, tint=1.0), metering
    )
    code_mid = int(np.argmin(np.abs(neutral[0] - 0.5)))
    warm_delta_r = abs(warm_tables[0, code_mid] - neutral[0, code_mid])
    tint_delta_g = abs(tint_tables[1, code_mid] - neutral[1, code_mid])
    tint_delta_r = abs(tint_tables[0, code_mid] - neutral[0, code_mid])
    # Thresholds scale with BALANCE_SCALE (chunk 1's rescale for the
    # W-orthonormal axes shrank a full-strength move somewhat), not a
    # magic 0.01 — both must still be clearly nonzero.
    assert warm_delta_r > 0.005
    assert tint_delta_g > 0.005
    # Warmth should affect red more than tint affects red, and vice versa
    # for green — but both can be nonzero; the key is that they're different.
    assert warm_delta_r != pytest.approx(tint_delta_r, abs=0.005)


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
    assert color.cast_slopes(
        params, _metering_full(None, (0.9, 0.85, 0.85)), 1.55, 0.5
    ) == (achromatic)


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
        warmth=0.5,
        tint=-0.4,
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
            params = dataclasses.replace(tone.NEUTRAL, grade_r=grade_r, density=density)
            slope, pivot_in = tone.base_slope_and_pivot(params)
            assert slope == tone.grade_slope(grade_r)
            assert pivot_in == pytest.approx(0.5 + (density - 1.0) * 0.2)
            out = tone.curve_values(np.array([pivot_in]), params)
            assert out[0] == pytest.approx(0.5, abs=2e-3)


# --- balance axes (chunk 1) -----------------------------------------------
#
# WARM_AXIS and MAGENTA_AXIS are orthonormal under the luma-weighted inner
# product <u, v> = sum(w_i * u_i * v_i), w = LUMA_WEIGHTS — *not* under the
# Euclidean dot product, which these vectors' Euclidean norms of ~3.396 and
# ~2.328 make obvious. The derivation below reconstructs both axes to full
# precision from LUMA_WEIGHTS alone, independent of the stored constants:
# for WARM_AXIS (G = 0), luma-zero forces the R:B ratio, so any vector
# proportional to (w_b, 0, -w_r) is luma-zero; for MAGENTA_AXIS (R = B),
# luma-zero forces the R:G ratio, so any vector proportional to
# (w_g, -(w_r + w_b), w_g) is luma-zero. Both directions are then
# normalized to unit W-norm.


def _w_inner(u, v):
    return color._w_inner(tuple(u), tuple(v))


def _derive_warm_axis_unit():
    w_r, _w_g, w_b = color.LUMA_WEIGHTS
    direction = (w_b, 0.0, -w_r)
    norm = np.sqrt(_w_inner(direction, direction))
    return tuple(x / norm for x in direction)


def _derive_magenta_axis_unit():
    w_r, w_g, w_b = color.LUMA_WEIGHTS
    direction = (w_g, -(w_r + w_b), w_g)
    norm = np.sqrt(_w_inner(direction, direction))
    return tuple(x / norm for x in direction)


def test_warm_axis_matches_its_derivation():
    derived = _derive_warm_axis_unit()
    np.testing.assert_allclose(color.WARM_AXIS, derived, atol=1e-9)


def test_magenta_axis_matches_its_derivation():
    derived = _derive_magenta_axis_unit()
    np.testing.assert_allclose(color.MAGENTA_AXIS, derived, atol=1e-9)


def test_warm_axis_luma_is_zero():
    assert _luma_sum(color.WARM_AXIS) == pytest.approx(0.0, abs=1e-10)


def test_magenta_axis_luma_is_zero():
    assert _luma_sum(color.MAGENTA_AXIS) == pytest.approx(0.0, abs=1e-10)


def test_warm_axis_unit_norm_under_w_inner_product():
    assert _w_inner(color.WARM_AXIS, color.WARM_AXIS) == pytest.approx(1.0, abs=1e-9)


def test_magenta_axis_unit_norm_under_w_inner_product():
    assert _w_inner(color.MAGENTA_AXIS, color.MAGENTA_AXIS) == pytest.approx(
        1.0, abs=1e-9
    )


def test_axes_euclidean_norm_is_not_one():
    """The axes are unit under the W inner product, not Euclidean — a
    regression guard against reintroducing a Euclidean-unit vector that
    happens to look plausible but is not W-orthonormal."""
    assert np.linalg.norm(color.WARM_AXIS) == pytest.approx(3.3958227963, abs=1e-6)
    assert np.linalg.norm(color.MAGENTA_AXIS) == pytest.approx(2.3282358560, abs=1e-6)


def test_axes_are_orthogonal_under_w_inner_product():
    assert _w_inner(color.WARM_AXIS, color.MAGENTA_AXIS) == pytest.approx(
        0.0, abs=1e-10
    )


def test_tint_moves_red_and_blue_equally():
    """MAGENTA_AXIS's R and B components are exactly equal, so a tint move
    shifts red and blue by the same amount (and only warmth tells them
    apart)."""
    params = dataclasses.replace(color.NEUTRAL_COLOR, tint=0.7)
    offsets = color.balance_offsets(params)
    assert offsets[0] == pytest.approx(offsets[2], abs=1e-12)
    assert offsets[0] != pytest.approx(offsets[1], abs=1e-6)


def test_warmth_signs():
    """+warmth raises display R and lowers B (yellow shift)."""
    params = dataclasses.replace(color.NEUTRAL_COLOR, warmth=1.0)
    offsets = color.balance_offsets(params)
    # density offset: + density = darker display
    # balance_offsets returns -display, so -offset = display direction
    display = (-offsets[0], -offsets[1], -offsets[2])
    assert display[0] > 0  # red up
    assert display[2] < 0  # blue down
    assert display[1] == pytest.approx(0.0, abs=1e-12)  # green untouched


def test_tint_signs():
    """+tint lowers display G (magenta shift)."""
    params = dataclasses.replace(color.NEUTRAL_COLOR, tint=1.0)
    offsets = color.balance_offsets(params)
    display = (-offsets[0], -offsets[1], -offsets[2])
    assert display[1] < 0  # green down
    assert display[0] > 0  # red up
    assert display[2] > 0  # blue up


def test_balance_neutral_is_zero():
    params = color.NEUTRAL_COLOR
    offsets = color.balance_offsets(params)
    assert offsets == pytest.approx((0.0, 0.0, 0.0))


def test_balance_offsets_are_luma_neutral():
    """Every warmth/tint combination stays luma-neutral."""
    rng = np.random.default_rng(42)
    for _ in range(64):
        w = float(rng.uniform(-1.0, 1.0))
        t = float(rng.uniform(-1.0, 1.0))
        params = dataclasses.replace(color.NEUTRAL_COLOR, warmth=w, tint=t)
        offsets = color.balance_offsets(params)
        assert _luma_sum(offsets) == pytest.approx(0.0, abs=1e-12)


# --- channel curve (chunk 2) ----------------------------------------------


def test_channel_curve_identity_at_zero_offsets():
    v = np.linspace(0.0, 1.0, 100)
    result = color.channel_curve(v, (0.0, 0.0, 0.0))
    np.testing.assert_allclose(result, v, atol=1e-10)


def test_channel_curve_ends_pinned():
    v = np.concatenate([np.linspace(-0.1, 0.0, 20), [1.0], np.linspace(1.0, 1.1, 20)])
    result = color.channel_curve(v, (0.05, 0.05, 0.05))
    assert result[0] == pytest.approx(0.0, abs=1e-10)
    idx_1 = np.argmin(np.abs(v - 1.0))
    assert result[idx_1] == pytest.approx(1.0, abs=1e-10)


def test_channel_curve_exact_at_knots():
    offsets = (0.05, -0.03, 0.08)
    for x, expected in [
        (0.0, 0.0),
        (0.25, 0.25 + offsets[0]),
        (0.5, 0.5 + offsets[1]),
        (0.75, 0.75 + offsets[2]),
        (1.0, 1.0),
    ]:
        result = color.channel_curve(np.array([x]), offsets)
        assert result[0] == pytest.approx(expected, abs=1e-10)


def test_channel_curve_identity_above_1():
    v = np.array([1.0, 1.05, 1.1, 1.2])
    result = color.channel_curve(v, (0.1, 0.1, 0.1))
    np.testing.assert_allclose(result, v, atol=1e-10)


def test_channel_curve_monotone():
    """A curve with positive offsets must be monotonically increasing."""
    offsets = (0.1, 0.1, 0.1)
    v = np.linspace(0.0, 1.0, 500)
    result = color.channel_curve(v, offsets)
    assert np.all(np.diff(result) >= -1e-10)


def test_channel_curve_monotone_negative_offsets():
    offsets = (-0.1, -0.1, -0.1)
    v = np.linspace(0.0, 1.0, 500)
    result = color.channel_curve(v, offsets)
    assert np.all(np.diff(result) >= -1e-10)


def test_curve_offsets_in_order_accepts_a_valid_curve():
    # Knots: 0, 0.02, 0.5, 0.95, 1 — each at least CURVE_MIN_GAP above the
    # previous.
    assert color.curve_offsets_in_order((-0.23, 0.0, 0.2))


def test_curve_offsets_in_order_rejects_a_reversal():
    """The bug this ordering rule exists to catch: curve_red_25=0.2,
    curve_red_50=-0.2 gives y(0.25)=0.45 then y(0.5)=0.30 — the curve
    reverses. `curve_offsets_in_order` must say no."""
    assert not color.curve_offsets_in_order((0.2, -0.2, 0.0))


def test_curve_offsets_in_order_rejects_a_gap_exactly_at_the_boundary():
    # y(0) = 0, y(0.25) = 0.25 + offset25 — a gap of exactly CURVE_MIN_GAP
    # above y(0) is allowed (modulo float round-off in the knot sum), a
    # clearly smaller gap is not.
    just_enough = color.CURVE_MIN_GAP - 0.25
    assert color.curve_offsets_in_order((just_enough, 0.0, 0.0))
    assert not color.curve_offsets_in_order((just_enough - 1e-3, 0.0, 0.0))


def test_channel_curve_ordering_rule():
    """Offsets that violate CURVE_MIN_GAP produce a non-monotone curve.
    This is the ordering-rule check — validation should reject such inputs
    before they reach channel_curve, but the function itself does not enforce it."""
    # o25 = -0.23, o50 = 0.0 -> y at 0.25 = 0.02, y at 0.5 = 0.5
    # That's still monotone.  A real violation would be caught by validation.
    offsets = (-0.23, 0.0, 0.2)
    v = np.linspace(0.0, 1.0, 500)
    result = color.channel_curve(v, offsets)
    # The curve should still be monotone with valid offsets.
    assert np.all(np.diff(result) >= -1e-10)


def test_channel_curve_small_offsets_near_identity():
    """Tiny offsets produce a curve very close to the identity."""
    offsets = (0.01, -0.01, 0.005)
    v = np.linspace(0.0, 1.0, 100)
    result = color.channel_curve(v, offsets)
    np.testing.assert_allclose(result, v, atol=0.02)


def test_channel_curve_extreme_offsets():
    """Max offsets push the curve but it stays bounded and monotone."""
    offsets = (color.CURVE_OFFSET_MAX, color.CURVE_OFFSET_MAX, color.CURVE_OFFSET_MAX)
    v = np.linspace(0.0, 1.0, 500)
    result = color.channel_curve(v, offsets)
    assert np.all(np.diff(result) >= -1e-10)
    assert result.min() >= -0.01
    assert result.max() <= 1.25
