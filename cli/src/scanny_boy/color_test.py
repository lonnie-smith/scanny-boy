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


def test_global_cmy_offsets_red_only():
    params = dataclasses.replace(color.NEUTRAL_COLOR, wb_cyan=1.0)
    narrow = _metering(ranges=(0.5, 1.0, 1.0))
    wide = _metering(ranges=(1.0, 1.0, 1.0))
    narrow_tables = tone.build_channel_tables(tone.NEUTRAL, params, narrow)
    wide_tables = tone.build_channel_tables(tone.NEUTRAL, params, wide)
    neutral = tone.build_channel_tables(tone.NEUTRAL, color.NEUTRAL_COLOR, wide)
    red_shift_narrow = neutral[0, 32768] - narrow_tables[0, 32768]
    red_shift_wide = neutral[0, 32768] - wide_tables[0, 32768]
    assert red_shift_narrow > red_shift_wide
    assert narrow_tables[1, 32768] == pytest.approx(neutral[1, 32768], abs=1e-9)
    assert narrow_tables[2, 32768] == pytest.approx(neutral[2, 32768], abs=1e-9)


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
        m2, _y2 = color.kelvin_to_wb(kelvin + 500, m, y)
        assert m2 - m == pytest.approx(0.0, abs=0.15)
    assert color.wb_to_kelvin(0.0, 0.0) == pytest.approx(5500.0)
