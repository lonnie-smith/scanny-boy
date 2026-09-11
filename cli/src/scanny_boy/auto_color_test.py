"""Tests for the Auto Cast Removal solve (`auto_color.py`)."""

from __future__ import annotations

import dataclasses

import pytest

from scanny_boy import auto_color, color


def _record(residual, *, floors=None, ceils=None, highlight_refs=None) -> dict:
    return {
        "floors": floors if floors is not None else [0.0, 0.0, 0.0],
        "ceils": ceils if ceils is not None else [1.0, 1.0, 1.0],
        "shadow_refs": [0.2, 0.15, 0.15],
        "highlight_refs": highlight_refs,
        "auto_neutral": {
            "shadow": list(residual),
            "highlight": None,
            "highlight_lock": None,
            "measure_version": 1,
        },
    }


def test_solve_nulls_the_recorded_residual():
    """The returned sliders, fed back through `cmy_offsets`, produce
    offsets whose R-G and B-G deviations negate the recorded residual."""
    metering = color.read_metering(_record([0.06, -0.03]))
    params = color.ColorParams()
    sliders = auto_color.solve_cmy(_record([0.06, -0.03]), params, 1.55, 0.5)
    assert sliders is not None
    solved = color.ColorParams(
        wb_cyan=sliders[0], wb_magenta=sliders[1], wb_yellow=sliders[2]
    )
    offsets = color.cmy_offsets(solved, metering)
    assert offsets[0] - offsets[1] == pytest.approx(-0.06, abs=1e-9)
    assert offsets[2] - offsets[1] == pytest.approx(0.03, abs=1e-9)
    assert color._luma_weighted_sum(offsets) == pytest.approx(0.0, abs=1e-12)


def test_the_neutral_target_is_luma_neutral_by_construction():
    for a, b in ([0.06, -0.03], [0.0, 0.0], [-0.5, 0.25]):
        o = auto_color._neutral_defaults_target(a, b)
        assert color._luma_weighted_sum(o) == pytest.approx(0.0, abs=1e-12)
        assert o[0] - o[1] == pytest.approx(-a, abs=1e-12)
        assert o[2] - o[1] == pytest.approx(-b, abs=1e-12)


def test_the_tie_compensation_is_inert_in_the_one_point_branch():
    """§7.3's test: with `cast_removal_highlights = 0`, the result is
    independent of `cast_removal` (every one-point branch pins the pivot,
    so the compensation is exactly zero)."""
    record = _record([0.06, -0.03])
    baseline = auto_color.solve_cmy(record, color.ColorParams(), 1.55, 0.5)
    for cast in (0.3, 0.7, 1.0):
        params = dataclasses.replace(color.NEUTRAL_COLOR, cast_removal=cast)
        assert auto_color.solve_cmy(record, params, 1.55, 0.5) == baseline


def test_the_tie_compensation_moves_once_a_highlight_tie_is_dialled():
    record = _record([0.06, -0.03], highlight_refs=[0.9, 0.85, 0.85])
    baseline = auto_color.solve_cmy(record, color.ColorParams(), 1.55, 0.5)
    params = dataclasses.replace(color.NEUTRAL_COLOR, cast_removal_highlights=1.0)
    moved = auto_color.solve_cmy(record, params, 1.55, 0.5)
    assert moved is not None and baseline is not None
    assert moved != baseline


def test_solve_returns_none_for_malformed_input():
    assert auto_color.solve_cmy(None, color.ColorParams(), 1.55, 0.5) is None
    assert auto_color.solve_cmy({}, color.ColorParams(), 1.55, 0.5) is None
    record = _record([0.06, -0.03])
    assert auto_color.solve_cmy(
        {key: value for key, value in record.items() if key != "auto_neutral"},
        color.ColorParams(),
        1.55,
        0.5,
    ) is None
    for bad in ([0.06], [0.06, -0.03, 0.0], ["0.06", -0.03], [True, -0.03]):
        assert auto_color.solve_cmy(_record(bad), color.ColorParams(), 1.55, 0.5) is None


def test_solve_returns_none_for_a_non_finite_residual():
    import math

    record = _record([0.06, math.inf])
    assert auto_color.solve_cmy(record, color.ColorParams(), 1.55, 0.5) is None
    record = _record([float("nan"), -0.03])
    assert auto_color.solve_cmy(record, color.ColorParams(), 1.55, 0.5) is None
