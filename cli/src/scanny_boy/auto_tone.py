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


def _luma_bounds(
    normalization: dict, highlight_lock=None
) -> tuple[float, float, float] | None:
    """The luma floor/ceil/span the two solves below measure against.

    `highlight_lock` (docs/ROLL_HIGHLIGHT_LOCK.md), when it resolves and
    this is a 3-channel colour record, retargets `floors` to the roll's
    corrected dense-end colour before the luma weighting — the same
    correction `color.read_metering` applies, so Auto Density/Auto Grade
    solve against the density level the negative actually *displays*, not
    the one its own (possibly scene-biased) per-negative meter found. The
    shift is normally tiny: the correction is median-zero across channels
    by construction, and Rec.709 luma weights are close to (but not
    exactly) a plain mean, so a real per-channel retarget moves the
    weighted sum only to the extent the weights are non-uniform."""
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
    if channel_count == 3 and highlight_lock is not None:
        from scanny_boy.highlight_lock import HighlightLock, base_offset_for, corrected_floors

        lock = (
            highlight_lock
            if isinstance(highlight_lock, HighlightLock)
            else HighlightLock.from_dict(highlight_lock)
        )
        if lock is not None:
            refs = normalization.get("highlight_refs")
            refs_f = (
                tuple(float(v) for v in refs)
                if isinstance(refs, list) and len(refs) == 3
                else None
            )
            floors = list(
                corrected_floors(
                    tuple(float(v) for v in floors),
                    tuple(float(v) for v in ceils),
                    lock,
                    refs_f,
                    base_offset_for(normalization),
                )
            )
    luma_floor = sum(w * float(f) for w, f in zip(weights, floors, strict=True))
    luma_ceil = sum(w * float(c) for w, c in zip(weights, ceils, strict=True))
    span = luma_ceil - luma_floor
    if span < 1e-6:
        return None
    return luma_floor, luma_ceil, span


def solve_density(normalization: dict | None, highlight_lock=None) -> float | None:
    """Solve print density from the recorded anchor meter."""
    if not normalization:
        return None
    anchor = normalization.get("anchor")
    if anchor is None or isinstance(anchor, bool) or not isinstance(anchor, (int, float)):
        return None
    bounds = _luma_bounds(normalization, highlight_lock)
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


def solve_grade(normalization: dict | None, highlight_lock=None) -> float | None:
    """Solve stored grade from the recorded textural range.

    Targets the scan-start default (``NEUTRAL_GRADE_R``) on a nominal
    negative, not the legacy R115 print reference."""
    if not normalization:
        return None
    textural = normalization.get("textural_range")
    if textural is None or isinstance(textural, bool) or not isinstance(
        textural, (int, float)
    ):
        return None
    bounds = _luma_bounds(normalization, highlight_lock)
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
    grade_r = tone.NEUTRAL_GRADE_R * NOMINAL_RANGE / effective
    return max(tone.GRADE_MIN, min(tone.GRADE_MAX, grade_r))
