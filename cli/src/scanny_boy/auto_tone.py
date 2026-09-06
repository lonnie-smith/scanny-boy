"""Auto Density and Auto Grade: closed-form tone solves from a negative's
recorded normalization block. No image I/O, no metering pass."""

from __future__ import annotations

from scanny_boy import tone
from scanny_boy.normalization import LUMA_B, LUMA_G, LUMA_R

ANCHOR_ASSUMED = 0.5
ANCHOR_METER_STRENGTH = 0.2
ANCHOR_METER_BAND = 0.12

AUTO_GRADE_TARGET = 0.6
AUTO_GRADE_STRENGTH = 0.5
NOMINAL_RATIO = 2.0
NOMINAL_RANGE = AUTO_GRADE_TARGET * NOMINAL_RATIO
DEGENERATE_GRADE_RANGE = 3.5


def _luma_bounds(normalization: dict) -> tuple[float, float, float] | None:
    floors = normalization.get("floors")
    ceils = normalization.get("ceils")
    if not isinstance(floors, list) or not isinstance(ceils, list):
        return None
    if len(floors) != len(ceils) or not floors:
        return None
    channel_count = len(floors)
    if channel_count == 1:
        weights = (1.0,)
    elif channel_count == 3:
        weights = (LUMA_R, LUMA_G, LUMA_B)
    else:
        return None
    for value in (*floors, *ceils):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
    luma_floor = sum(w * float(f) for w, f in zip(weights, floors, strict=True))
    luma_ceil = sum(w * float(c) for w, c in zip(weights, ceils, strict=True))
    span = luma_ceil - luma_floor
    if span < 1e-6:
        return None
    return luma_floor, luma_ceil, span


def solve_density(normalization: dict | None) -> float | None:
    """Solve print density from the recorded anchor meter."""
    if not normalization:
        return None
    anchor = normalization.get("anchor")
    if anchor is None or isinstance(anchor, bool) or not isinstance(anchor, (int, float)):
        return None
    bounds = _luma_bounds(normalization)
    if bounds is None:
        return None
    luma_floor, _, span = bounds
    measured = max(0.0, min(1.0, (float(anchor) - luma_floor) / span))
    # Strength 0.2 and pivot shift 0.2 both 0.2 — coefficient is exactly 1.
    density = tone.DENSITY_REFERENCE + ANCHOR_METER_STRENGTH * (
        ANCHOR_ASSUMED - measured
    ) / tone.DENSITY_PIVOT_SHIFT
    band = ANCHOR_METER_BAND / tone.DENSITY_PIVOT_SHIFT
    density = max(tone.DENSITY_REFERENCE - band, min(tone.DENSITY_REFERENCE + band, density))
    return max(tone.DENSITY_MIN, min(tone.DENSITY_MAX, density))


def solve_grade(normalization: dict | None) -> float | None:
    """Solve paper grade from the recorded textural range."""
    if not normalization:
        return None
    textural = normalization.get("textural_range")
    if textural is None or isinstance(textural, bool) or not isinstance(
        textural, (int, float)
    ):
        return None
    bounds = _luma_bounds(normalization)
    if bounds is None:
        return None
    _, _, span = bounds
    textural_abs = abs(float(textural))
    if textural_abs < 1e-6:
        effective = DEGENERATE_GRADE_RANGE
    else:
        ratio = span / textural_abs
        effective = AUTO_GRADE_TARGET * (
            NOMINAL_RATIO + AUTO_GRADE_STRENGTH * (ratio - NOMINAL_RATIO)
        )
    grade_r = tone.GRADE_REFERENCE * NOMINAL_RANGE / effective
    return max(tone.GRADE_MIN, min(tone.GRADE_MAX, grade_r))
