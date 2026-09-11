"""Tests for automatic tone-split neutral balance."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import numpy as np
import pytest

from scanny_boy import auto_neutral, color, normalization, render, tone
from scanny_boy.auto_neutral import (
    AUTO_NEUTRAL_HIGHLIGHT_LUMA_PERCENTILE_HIGH,
    AUTO_NEUTRAL_HIGHLIGHT_LUMA_PERCENTILE_LOW,
    AUTO_NEUTRAL_SHADOW_LUMA_PERCENTILE_HIGH,
    AUTO_NEUTRAL_SHADOW_LUMA_PERCENTILE_LOW,
    measure_auto_neutral_bands,
    measure_auto_neutral_from_image,
)


def _bounds() -> normalization.Bounds:
    return normalization.Bounds(floors=(0.0, 0.0, 0.0), ceils=(1.0, 1.0, 1.0))


def _metering(
    *,
    shadow: tuple[float, float] | None = None,
    highlight: tuple[float, float] | None = None,
) -> color.Metering:
    return color.Metering(
        ranges=(1.0, 1.0, 1.0),
        shadow_refs_norm=(0.2, 0.15, 0.15),
        highlight_refs_norm=(0.9, 0.85, 0.85),
        auto_neutral_shadow=shadow,
        auto_neutral_highlight=highlight,
    )


def _grey_ramp_grid(height: int = 64, width: int = 64) -> tuple[np.ndarray, np.ndarray]:
    ys = np.linspace(0.05, 0.95, height, dtype=np.float32)
    xs = np.linspace(0.05, 0.95, width, dtype=np.float32)
    yy, xx = np.meshgrid(ys, xs, indexing="ij")
    luma = (0.25 * xx + 0.75 * yy).astype(np.float32)
    grid = np.stack([luma, luma, luma], axis=-1)
    keep = np.ones((height, width), dtype=bool)
    return grid, keep


def test_shadow_only_cast_is_corrected_at_shadows_not_highlights():
    params = color.ColorParams()
    meter = _metering(shadow=(0.08, 0.0), highlight=None)
    tables = tone.build_channel_tables(tone.NEUTRAL, params, meter)
    neutral = tone.build_channel_tables(tone.NEUTRAL, color.NEUTRAL_COLOR, meter)
    codes = np.arange(tone.MAX_CODE + 1, dtype=np.float64)
    norm = normalization.decode_normalized(codes)
    shadow_code = int(np.argmin(np.abs(norm - 0.08)))
    highlight_code = int(np.argmin(np.abs(norm - 0.88)))
    assert tables[0, shadow_code] == pytest.approx(tables[1, shadow_code], abs=0.03)
    assert tables[0, highlight_code] == pytest.approx(
        neutral[0, highlight_code], abs=0.01
    )


def test_highlight_only_cast_is_corrected_at_highlights_not_shadows():
    params = color.ColorParams()
    meter = _metering(shadow=None, highlight=(0.08, 0.0))
    tables = tone.build_channel_tables(tone.NEUTRAL, params, meter)
    neutral = tone.build_channel_tables(tone.NEUTRAL, color.NEUTRAL_COLOR, meter)
    codes = np.arange(tone.MAX_CODE + 1, dtype=np.float64)
    norm = normalization.decode_normalized(codes)
    shadow_code = int(np.argmin(np.abs(norm - 0.08)))
    highlight_code = int(np.argmin(np.abs(norm - 0.88)))
    assert tables[0, highlight_code] == pytest.approx(tables[1, highlight_code], abs=0.03)
    assert tables[0, shadow_code] == pytest.approx(neutral[0, shadow_code], abs=0.01)


def test_one_band_falls_back_to_one_point_tie():
    meter = _metering(shadow=(0.06, -0.02), highlight=None)
    slopes = color.cast_slopes_from_residuals(
        meter, 1.55, 0.5, meter.auto_neutral_shadow, meter.auto_neutral_highlight
    )
    assert slopes != ((1.55, 0.5),) * 3
    identity = color.cast_slopes_from_residuals(meter, 1.55, 0.5, None, None)
    assert identity == ((1.55, 0.5),) * 3


def test_no_neutrals_anywhere_is_identity():
    params = color.ColorParams()
    meter = _metering(shadow=None, highlight=None)
    tables = tone.build_channel_tables(tone.NEUTRAL, params, meter)
    neutral = tone.build_channel_tables(tone.NEUTRAL, color.NEUTRAL_COLOR, meter)
    np.testing.assert_array_equal(tables, neutral)


def test_auto_neutral_false_is_identity():
    grid, keep = _grey_ramp_grid()
    shadow = auto_neutral._luma_band(
        auto_neutral._display_luma_grid(grid, _bounds()),
        keep,
        AUTO_NEUTRAL_SHADOW_LUMA_PERCENTILE_LOW,
        AUTO_NEUTRAL_SHADOW_LUMA_PERCENTILE_HIGH,
    )
    cast = grid.copy()
    cast[..., 0] += np.where(shadow, 0.08, 0.0)
    bands = measure_auto_neutral_bands(cast, keep, _bounds())
    meter = _metering(shadow=bands.shadow, highlight=bands.highlight)
    off = dataclasses.replace(color.NEUTRAL_COLOR, auto_neutral=0.0)
    tables = tone.build_channel_tables(tone.NEUTRAL, off, meter)
    neutral = tone.build_channel_tables(tone.NEUTRAL, color.NEUTRAL_COLOR, meter)
    np.testing.assert_array_equal(tables, neutral)


def test_preview_matches_export_with_auto_neutral_on():
    height, width = 128, 192
    codes = np.linspace(0, 60000, width, dtype=np.uint16)
    image = np.tile(codes, (height, 1)).astype(np.uint16)
    image = np.stack([image, image, image], axis=-1)
    record = {
        "floors": [0.0, 0.0, 0.0],
        "ceils": [1.0, 1.0, 1.0],
        "shadow_refs": [0.2, 0.15, 0.15],
        "highlight_refs": [0.9, 0.85, 0.85],
        "auto_neutral": {
            "shadow": [0.05, -0.02],
            "highlight": [0.01, 0.0],
            "highlight_lock": None,
            "measure_version": 1,
        },
    }
    meter = color.read_metering(record)
    matrix = np.eye(3, dtype=np.float64)
    preview, _ = render.render_positive_float(
        image, matrix, None, None, meter, long_edge=64
    )
    export, _ = render.render_export(
        image, matrix, None, None, meter, long_edge=64
    )
    np.testing.assert_allclose(preview, export.astype(np.float32) / 65535.0, atol=1e-5)


def test_measurement_from_image_on_neutral_ramp():
    height, width = 512, 512
    ramp = np.linspace(0.02, 0.98, width, dtype=np.float32)
    plane = np.tile(ramp, (height, 1))
    norm = np.stack([plane, plane, plane], axis=-1)
    codes = np.clip(norm * 60000.0, 0, 65535).astype(np.uint16)
    record = {
        "floors": [0.0, 0.0, 0.0],
        "ceils": [1.0, 1.0, 1.0],
        "shadow_refs": [0.2, 0.15, 0.15],
        "highlight_refs": [0.9, 0.85, 0.85],
    }
    block = measure_auto_neutral_from_image(codes, record)
    assert block is not None
    assert block["shadow"] is None or abs(block["shadow"][0]) < 0.05


def test_recompute_changes_when_lock_changes(monkeypatch):
    calls: list = []

    def _fake_measure(*args, **kwargs):
        calls.append(kwargs.get("highlight_lock"))
        return {
            "shadow": [0.01, 0.0],
            "highlight": None,
            "highlight_lock": kwargs.get("highlight_lock"),
            "measure_version": 1,
        }

    monkeypatch.setattr(auto_neutral, "measure_auto_neutral_from_tiff", _fake_measure)

    class _Negative:
        output = {"name": "a.tif"}
        valid_rect = None
        normalization = {
            "floors": [0.0, 0.0, 0.0],
            "ceils": [1.0, 1.0, 1.0],
            "auto_neutral": {
                "shadow": [0.02, 0.0],
                "highlight": None,
                "highlight_lock": {"k": [1.0, 1.0, 1.0]},
                "measure_version": 1,
            },
        }

    lock = {"k": [1.1, 1.0, 0.9], "base": [0.0, 0.0, 0.0], "qualifying_count": 1}
    monkeypatch.setattr(Path, "exists", lambda self: True)
    changed = auto_neutral.recompute_negative_auto_neutral(
        _Negative(), Path("/tmp"), highlight_lock=lock
    )
    assert changed
    assert calls[-1] == lock
