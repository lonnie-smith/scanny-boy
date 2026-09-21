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
    """The returned warmth/tint, projected through balance_offsets, produce
    display offsets whose R-G and B-G deviations negate the recorded
    residual's *sign convention*: the display R-G and B-G moves equal the
    residual itself (a positive R-G residual is corrected by a positive
    R-G display move — cosine +1 with the target, not -1, which was the
    inverted-sign bug this test now guards against). With no cast-removal
    tie active there is nothing for the compensation loop to do, and the
    residual (0.02, -0.01) stays within the unclamped range, so the
    round-trip is exact, not approximate."""
    record = _record([0.02, -0.01])
    params = color.ColorParams()
    balance = auto_color.solve_balance(record, params, 1.55, 0.5)
    assert balance is not None
    warmth, tint = balance
    assert abs(warmth) < 1.0 and abs(tint) < 1.0  # unclamped
    solved = color.ColorParams(warmth=warmth, tint=tint)
    offsets = color.balance_offsets(solved)
    display = (-offsets[0], -offsets[1], -offsets[2])
    rg = display[0] - display[1]
    bg = display[2] - display[1]
    assert rg == pytest.approx(0.02, abs=1e-9)
    assert bg == pytest.approx(-0.01, abs=1e-9)
    assert color._luma_weighted_sum(offsets) == pytest.approx(0.0, abs=1e-12)


def test_the_neutral_target_is_luma_neutral_by_construction():
    for a, b in ([0.06, -0.03], [0.0, 0.0], [-0.5, 0.25]):
        o = auto_color._neutral_defaults_target(a, b)
        assert color._luma_weighted_sum(o) == pytest.approx(0.0, abs=1e-12)
        assert o[0] - o[1] == pytest.approx(-a, abs=1e-12)
        assert o[2] - o[1] == pytest.approx(-b, abs=1e-12)


def test_warmth_tint_from_offsets_round_trips_through_balance_offsets():
    """§3: because WARM_AXIS and MAGENTA_AXIS are W-orthonormal and a
    luma-zero offset lies exactly in their span, projecting it to
    warmth/tint and running it back through `balance_offsets` must
    reproduce the original offset exactly (when the projection doesn't
    clamp)."""
    for a, b in ([0.02, -0.01], [-0.03, 0.015], [0.001, 0.004]):
        o = auto_color._neutral_defaults_target(a, b)
        warmth, tint = auto_color._warmth_tint_from_offsets(o)
        assert abs(warmth) < 1.0 and abs(tint) < 1.0  # unclamped
        round_tripped = color.balance_offsets(
            color.ColorParams(warmth=warmth, tint=tint)
        )
        assert round_tripped == pytest.approx(o, abs=1e-9)


def test_the_tie_compensation_is_inert_in_the_one_point_branch():
    """§7.3's test: with `cast_removal_highlights = 0`, the result is
    independent of `cast_removal` (every one-point branch pins the pivot,
    so the compensation is exactly zero)."""
    record = _record([0.06, -0.03])
    baseline = auto_color.solve_balance(record, color.ColorParams(), 1.55, 0.5)
    for cast in (0.3, 0.7, 1.0):
        params = dataclasses.replace(color.NEUTRAL_COLOR, cast_removal=cast)
        assert auto_color.solve_balance(record, params, 1.55, 0.5) == baseline


def test_the_tie_compensation_moves_once_a_highlight_tie_is_dialled():
    record = _record([0.06, -0.03], highlight_refs=[0.9, 0.85, 0.85])
    baseline = auto_color.solve_balance(record, color.ColorParams(), 1.55, 0.5)
    params = dataclasses.replace(color.NEUTRAL_COLOR, cast_removal_highlights=1.0)
    moved = auto_color.solve_balance(record, params, 1.55, 0.5)
    assert moved is not None and baseline is not None
    assert moved != baseline


def test_solve_returns_none_for_malformed_input():
    assert auto_color.solve_balance(None, color.ColorParams(), 1.55, 0.5) is None
    assert auto_color.solve_balance({}, color.ColorParams(), 1.55, 0.5) is None
    record = _record([0.06, -0.03])
    assert (
        auto_color.solve_balance(
            {key: value for key, value in record.items() if key != "auto_neutral"},
            color.ColorParams(),
            1.55,
            0.5,
        )
        is None
    )
    for bad in ([0.06], [0.06, -0.03, 0.0], ["0.06", -0.03], [True, -0.03]):
        assert (
            auto_color.solve_balance(_record(bad), color.ColorParams(), 1.55, 0.5)
            is None
        )


def test_solve_returns_none_for_a_non_finite_residual():
    import math

    record = _record([0.06, math.inf])
    assert auto_color.solve_balance(record, color.ColorParams(), 1.55, 0.5) is None
    record = _record([float("nan"), -0.03])
    assert auto_color.solve_balance(record, color.ColorParams(), 1.55, 0.5) is None
