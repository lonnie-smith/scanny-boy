"""Tests for auto tone solves from normalization records."""

from __future__ import annotations

import pytest

from scanny_boy import auto_tone, tone


def _record(*, anchor: float, textural_range: float, floors, ceils) -> dict:
    return {
        "anchor": anchor,
        "textural_range": textural_range,
        "floors": list(floors),
        "ceils": list(ceils),
    }


def test_midpoint_anchor_solves_to_neutral_density():
    record = _record(anchor=-0.5, textural_range=0.3, floors=[-1.0], ceils=[0.0])
    assert auto_tone.solve_density(record) == pytest.approx(tone.DENSITY_REFERENCE)


def test_nominal_ratio_solves_to_reference_grade():
    record = _record(
        anchor=-0.5,
        textural_range=0.5,
        floors=[-1.0],
        ceils=[0.0],
    )
    assert auto_tone.solve_grade(record) == pytest.approx(tone.NEUTRAL_GRADE_R)


def test_dense_metering_solves_darker():
    dark = _record(anchor=-0.8, textural_range=0.3, floors=[-1.0], ceils=[0.0])
    bright = _record(anchor=-0.2, textural_range=0.3, floors=[-1.0], ceils=[0.0])
    assert auto_tone.solve_density(bright) < auto_tone.solve_density(dark)


def test_density_band_clamp_holds():
    band = auto_tone.ANCHOR_METER_BAND / tone.DENSITY_PIVOT_SHIFT
    low = tone.DENSITY_REFERENCE - band
    high = tone.DENSITY_REFERENCE + band
    for anchor in (-5.0, 5.0):
        record = _record(anchor=anchor, textural_range=0.3, floors=[-1.0], ceils=[0.0])
        density = auto_tone.solve_density(record)
        assert low <= density <= high


def test_wide_extremes_solve_harder_grade():
    flat = _record(anchor=-0.5, textural_range=0.1, floors=[-1.0], ceils=[0.0])
    contrasty = _record(anchor=-0.5, textural_range=0.5, floors=[-1.0], ceils=[0.0])
    assert auto_tone.solve_grade(flat) < auto_tone.solve_grade(contrasty)


def test_grade_clamp_holds():
    extreme = _record(anchor=-0.5, textural_range=100.0, floors=[-1.0], ceils=[0.0])
    grade = auto_tone.solve_grade(extreme)
    assert tone.GRADE_MIN <= grade <= tone.GRADE_MAX


@pytest.mark.parametrize(
    "record",
    [
        None,
        {},
        {"anchor": -0.5},
        _record(anchor=-0.5, textural_range=0.3, floors=[0.0], ceils=[0.0]),
    ],
)
def test_invalid_records_return_none(record):
    assert auto_tone.solve_density(record) is None
    assert auto_tone.solve_grade(record) is None


def test_mono_roll_record_solves_without_shape_error():
    record = _record(anchor=-0.5, textural_range=0.25, floors=[-0.8], ceils=[-0.1])
    assert auto_tone.solve_density(record) is not None
    assert auto_tone.solve_grade(record) is not None
