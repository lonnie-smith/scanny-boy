"""Derived colour-adjustment quantities for the preview curve.

Pure functions, no I/O, no image — the colour-relevant slice of a
negative's `normalization` record is read here; curve composition lives in
`tone.py`; manifest knowledge stays out of both.
"""

from __future__ import annotations

import dataclasses

import numpy as np

# Global CMY filtration — NegPy's cmy_max_density, unchanged (log10 density).
CMY_MAX_DENSITY = 0.2
CMY_MIN = -1.0
CMY_MAX = 1.0

# Cast removal — NegPy's cast_removal_max_offset, same normalized units.
# CAST_MAX_OFFSET bounds BOTH ends' ties (docs/CAST_REMOVAL_PLAN.md §2.2).
CAST_REMOVAL_MIN = 0.0
CAST_REMOVAL_MAX = 1.0
CAST_REMOVAL_HIGHLIGHTS_MIN = 0.0
CAST_REMOVAL_HIGHLIGHTS_MAX = 1.0
CAST_MAX_OFFSET = 0.1

# Regional CMY — calibrated for our 0..1 display axis (see COLOR_PLAN §1.3).
REGION_CENTRE = 0.5
REGION_SHARPNESS = 7.0
REGION_CMY_SCALE = 0.09

# Dye separation and damping — NegPy's clamp; ref spread mapped to display.
DYE_SEPARATION_MIN = 0.5
DYE_SEPARATION_MAX = 1.5
SEPARATION_DAMPING_MIN = 0.0
SEPARATION_DAMPING_MAX = 1.0
SEPARATION_REF_SPREAD = 0.15
SEPARATION_K_MAX = 3.0

# Temperature lever — nominal readout, not colorimetric (NegPy logic.py).
TEMP_REF_KELVIN = 5500.0
TEMP_MIN_KELVIN = 3000.0
TEMP_MAX_KELVIN = 12000.0
TEMP_K_MAGENTA = 0.0029
TEMP_K_YELLOW = 0.0057

COLOR_PARAM_KEYS = (
    "wb_cyan",
    "wb_magenta",
    "wb_yellow",
    "shadow_cyan",
    "shadow_magenta",
    "shadow_yellow",
    "highlight_cyan",
    "highlight_magenta",
    "highlight_yellow",
    "cast_removal",
    "cast_removal_highlights",
    "dye_separation",
    "separation_damping",
)

# The original twelve, frozen, in their original order. This exists only so
# `repo._parse_color_op` can recognise an op written before
# docs/CAST_REMOVAL_PLAN.md (that plan's R-2 §6.1): a twelve-key op is a
# complete colour state, and a missing newer key keeps its neutral default.
# Nothing else may read it.
COLOR_PARAM_KEYS_V1 = (
    "wb_cyan",
    "wb_magenta",
    "wb_yellow",
    "shadow_cyan",
    "shadow_magenta",
    "shadow_yellow",
    "highlight_cyan",
    "highlight_magenta",
    "highlight_yellow",
    "cast_removal",
    "dye_separation",
    "separation_damping",
)


@dataclasses.dataclass(frozen=True)
class ColorParams:
    wb_cyan: float = 0.0
    wb_magenta: float = 0.0
    wb_yellow: float = 0.0
    shadow_cyan: float = 0.0
    shadow_magenta: float = 0.0
    shadow_yellow: float = 0.0
    highlight_cyan: float = 0.0
    highlight_magenta: float = 0.0
    highlight_yellow: float = 0.0
    cast_removal: float = 0.0
    cast_removal_highlights: float = 0.0
    dye_separation: float = 1.0
    separation_damping: float = 0.0


NEUTRAL_COLOR = ColorParams()


@dataclasses.dataclass(frozen=True)
class Metering:
    """The colour-relevant slice of a negative's `normalization` record.

    `highlight_refs_norm` is the dense end's same-pixel neutral reference,
    normalized exactly as the shadow one; `None` when the negative's record
    predates it (CAST_REMOVAL_PLAN R-1) or when the dense-end neutral band
    held no trustworthy set — which is load-bearing information, not an
    error (that plan's §0.4)."""

    ranges: tuple[float, ...]
    shadow_refs_norm: tuple[float, ...] | None
    highlight_refs_norm: tuple[float, ...] | None = None


def read_metering(record: dict | None) -> Metering:
    """Never raises. Missing or incomplete records yield uncalibrated CMY
    and inert cast removal."""
    default_ranges = (1.0, 1.0, 1.0)
    if not record:
        return Metering(ranges=default_ranges, shadow_refs_norm=None)
    floors = record.get("floors")
    ceils = record.get("ceils")
    if not isinstance(floors, list) or not isinstance(ceils, list):
        return Metering(ranges=default_ranges, shadow_refs_norm=None)
    if len(floors) != len(ceils) or not floors:
        return Metering(ranges=default_ranges, shadow_refs_norm=None)
    channels = len(floors)
    ranges: list[float] = []
    for ch in range(channels):
        floor = floors[ch]
        ceil = ceils[ch]
        if isinstance(floor, bool) or not isinstance(floor, (int, float)):
            return Metering(ranges=default_ranges, shadow_refs_norm=None)
        if isinstance(ceil, bool) or not isinstance(ceil, (int, float)):
            return Metering(ranges=default_ranges, shadow_refs_norm=None)
        ranges.append(max(abs(float(ceil) - float(floor)), 1e-6))
    shadow_refs_norm: tuple[float, ...] | None = None
    shadow_refs = record.get("shadow_refs")
    if isinstance(shadow_refs, list) and len(shadow_refs) == channels:
        normed: list[float] = []
        for ch in range(channels):
            floor = float(floors[ch])
            ceil = float(ceils[ch])
            span = ceil - floor
            if abs(span) < 1e-6:
                normed = []
                break
            ref = shadow_refs[ch]
            if isinstance(ref, bool) or not isinstance(ref, (int, float)):
                normed = []
                break
            normed.append((float(ref) - floor) / span)
        if len(normed) == channels:
            shadow_refs_norm = tuple(normed)
    highlight_refs_norm: tuple[float, ...] | None = None
    highlight_refs = record.get("highlight_refs")
    # Normalized exactly as the shadow refs: same guards, same
    # (ref - floor)/span, same anything-wrong -> None rule
    # (docs/CAST_REMOVAL_PLAN.md R-0).
    if isinstance(highlight_refs, list) and len(highlight_refs) == channels:
        normed = []
        for ch in range(channels):
            floor = float(floors[ch])
            ceil = float(ceils[ch])
            span = ceil - floor
            if abs(span) < 1e-6:
                normed = []
                break
            ref = highlight_refs[ch]
            if isinstance(ref, bool) or not isinstance(ref, (int, float)):
                normed = []
                break
            normed.append((float(ref) - floor) / span)
        if len(normed) == channels:
            highlight_refs_norm = tuple(normed)
    return Metering(
        ranges=tuple(ranges),
        shadow_refs_norm=shadow_refs_norm,
        highlight_refs_norm=highlight_refs_norm,
    )


def cmy_offsets(params: ColorParams, metering: Metering) -> tuple[float, ...]:
    """Global CMY as normalized log-density input offsets (§1.2), made
    **lightness-neutral** (docs/CAST_REMOVAL_PLAN.md §1.1): the raw
    range-divided offsets are mean-removed, so moving the sliders changes
    hue and never the display's channel mean — Print Density and the zone
    controls keep sole ownership of lightness.

    The mean is removed *after* the range division because the curve
    applies the same slope to every channel near the pivot, so the display
    shift's mean is proportional to the mean of the post-division values;
    zeroing that is what holds lightness. Two stated consequences, both
    deliberate: an equal three-slider move is not a no-op when the ranges
    differ — it is a pure hue move at constant lightness (a neutral-density
    filter on separately stretched channels genuinely has a chromatic
    effect); and the arithmetic mean (not luma-weighted) keeps the three
    sliders symmetric with each other."""
    sliders = (params.wb_cyan, params.wb_magenta, params.wb_yellow)
    if len(sliders) != len(metering.ranges):
        # Unreachable in production (mono never applies colour), but a
        # malformed record must not index out of range.
        return (0.0,) * len(sliders)
    raw = [
        slider * CMY_MAX_DENSITY / max(metering.ranges[ch], 1e-6)
        for ch, slider in enumerate(sliders)
    ]
    mean = sum(raw) / len(raw)
    return tuple(value - mean for value in raw)


def region_cmy(params: ColorParams) -> tuple[tuple[float, ...], ...]:
    """Regional shadow/highlight CMY slider tuples (§1.3), each
    **mean-removed** (docs/CAST_REMOVAL_PLAN.md §1.2): they are added to the
    display value directly, and the blend's complementary weights sum to 1,
    so a mean-zero triple contributes a mean-zero display shift at every
    tone — the region controls are purely chromatic and stop competing with
    the shadow/highlight density trims. An equal three-slider move is an
    exact no-op here, because no per-channel range is in the path."""
    shadow = (params.shadow_cyan, params.shadow_magenta, params.shadow_yellow)
    highlight = (
        params.highlight_cyan,
        params.highlight_magenta,
        params.highlight_yellow,
    )
    return _mean_removed(shadow), _mean_removed(highlight)


def _mean_removed(triple: tuple[float, ...]) -> tuple[float, ...]:
    mean = sum(triple) / 3.0
    return tuple(value - mean for value in triple)


def _one_point_cast_slopes(
    params: ColorParams,
    metering: Metering,
    slope: float,
    pivot_in: float,
) -> tuple[tuple[float, float], ...]:
    """Today's one-point tie, kept verbatim as the fallback branch
    (docs/CAST_REMOVAL_PLAN.md §2.3 guard 3) — do not rewrite it, and do
    not let the two-point formula degenerate into it, because it does not."""
    achromatic = ((slope, pivot_in),) * 3
    if params.cast_removal <= 0.0 or metering.shadow_refs_norm is None:
        return achromatic
    if len(metering.shadow_refs_norm) != 3:
        return achromatic
    anchor = pivot_in
    green_ref = 1.0 - metering.shadow_refs_norm[1]
    from scanny_boy import tone

    result: list[tuple[float, float]] = []
    for ch in range(3):
        if ch == 1:
            result.append((slope, pivot_in))
            continue
        channel_ref = 1.0 - metering.shadow_refs_norm[ch]
        offset = float(
            np.clip(
                params.cast_removal * (channel_ref - green_ref),
                -CAST_MAX_OFFSET,
                CAST_MAX_OFFSET,
            )
        )
        target = green_ref + offset
        denom = anchor - target
        if abs(denom) < 1e-6:
            slope_ch = slope
        else:
            slope_ch = float(
                np.clip(
                    slope * (anchor - green_ref) / denom,
                    tone.SLOPE_MIN,
                    tone.SLOPE_MAX,
                )
            )
        if abs(slope_ch) < 1e-6:
            pivot_ch = pivot_in
        else:
            pivot_ch = anchor - (slope / slope_ch) * (anchor - pivot_in)
        result.append((slope_ch, pivot_ch))
    return tuple(result)


def cast_slopes(
    params: ColorParams,
    metering: Metering,
    slope: float,
    pivot_in: float,
) -> tuple[tuple[float, float], ...]:
    """Per-channel (slope, pivot_in) for cast removal.

    With a highlight reference and a non-zero `cast_removal_highlights`,
    the tie has **two points** (docs/CAST_REMOVAL_PLAN.md §2.2): each
    channel's line is required to print at the shadow target what green
    prints at green's shadow reference, and at the highlight target what
    green prints at green's highlight reference. Two constraints determine
    the line exactly — a genuine affine, gain *and* offset, which is
    darktable `negadoctor`'s `wb_high`/`wb_low` pair in our coordinates.
    One point is not enough: a rotation can tie one reference but cannot
    null a constant per-channel offset, because nulling it requires moving
    the line without rotating it.

    All coordinates are display space (`x_display = 1 - x_norm`). Green is
    the reference channel and is never modified, so exposure stays anchored
    — green carries 0.7152 of the Rec.709 luma (§2.4). In the two-point
    branch R and B no longer print unchanged at the anchor; that is the
    second degree of freedom being used, not a regression.

    The one-point branch (no highlight reference, or the strength at rest)
    is today's behaviour byte for byte: at `strength_h = 0` the two-point
    formula would give a *different* number, so the branches are separate
    by design, not by limit (§2.3 guard 3).

    COLOR_PLAN §1.6 must keep holding: the endpoint rescale anchors are
    read once on the achromatic curve — grade and snap only, every density,
    colour and shaping control at rest — and the same `(low, high)` pair
    rescales all three channels. A per-channel rescale would undo exactly
    the colour difference this solve just created. `pivot_out` stays 0.5
    for every channel; it is not per-channel."""
    achromatic = ((slope, pivot_in),) * 3
    # Guard 1, widened: no strength at either end.
    if params.cast_removal <= 0.0 and params.cast_removal_highlights <= 0.0:
        return achromatic
    # Guard 2: the shadow reference is the anchor equation; without it
    # there is nothing to solve.
    if metering.shadow_refs_norm is None or len(metering.shadow_refs_norm) != 3:
        return achromatic
    # Guard 3: the one-point branch, unchanged.
    if (
        metering.highlight_refs_norm is None
        or len(metering.highlight_refs_norm) != 3
        or params.cast_removal_highlights == 0.0
    ):
        return _one_point_cast_slopes(params, metering, slope, pivot_in)

    from scanny_boy import tone

    g_s = 1.0 - metering.shadow_refs_norm[1]
    g_h = 1.0 - metering.highlight_refs_norm[1]
    result: list[tuple[float, float]] = []
    for ch in range(3):
        if ch == 1:
            result.append((slope, pivot_in))
            continue
        r_s = 1.0 - metering.shadow_refs_norm[ch]
        r_h = 1.0 - metering.highlight_refs_norm[ch]
        t_s = g_s + float(
            np.clip(
                params.cast_removal * (r_s - g_s), -CAST_MAX_OFFSET, CAST_MAX_OFFSET
            )
        )
        t_h = g_h + float(
            np.clip(
                params.cast_removal_highlights * (r_h - g_h),
                -CAST_MAX_OFFSET,
                CAST_MAX_OFFSET,
            )
        )
        # Guard 4: the two references have collapsed onto each other and
        # the line is undetermined — that channel falls back to the
        # one-point solve.
        if abs(t_h - t_s) < 1e-6:
            fallback = _one_point_cast_slopes(params, metering, slope, pivot_in)
            result.append(fallback[ch])
            continue
        slope_ch = float(
            np.clip(
                slope * (g_h - g_s) / (t_h - t_s), tone.SLOPE_MIN, tone.SLOPE_MAX
            )
        )
        # Guard 5: after clamping, re-solve the pivot from the clamped
        # slope through the *shadow* constraint, so the shadow tie still
        # holds exactly and only the highlight tie degrades.
        # Guard 6: a vanishing slope anchors the pivot.
        if abs(slope_ch) < 1e-6:
            pivot_ch = pivot_in
        else:
            pivot_ch = t_s - (slope / slope_ch) * (g_s - pivot_in)
        result.append((slope_ch, pivot_ch))
    return tuple(result)


def damping_gain(k: float, damping: float, chroma: float) -> float:
    """NegPy's separation_damping_gain, verbatim in display space."""
    if k <= 0.0:
        return 0.0
    ref = SEPARATION_REF_SPREAD
    h = (ref - chroma) / (ref + chroma)
    k_eff = k ** ((1.0 - damping) + damping * h)
    return float(min(k_eff, SEPARATION_K_MAX))


def apply_separation(rgb: np.ndarray, params: ColorParams) -> np.ndarray:
    """Spread each pixel about its achromatic mean (§1.5). float32 in/out."""
    out = np.asarray(rgb, dtype=np.float32)
    if params.dye_separation == 1.0 and params.separation_damping == 0.0:
        return out
    k = params.dye_separation
    damping = params.separation_damping
    mean = out.mean(axis=-1, keepdims=True)
    diff = out - mean
    chroma = np.sqrt(
        (diff[..., 0:1] ** 2 + diff[..., 1:2] ** 2 + diff[..., 2:3] ** 2) / 3.0
    )
    if damping == 0.0:
        return mean + k * diff
    h = (SEPARATION_REF_SPREAD - chroma) / (SEPARATION_REF_SPREAD + chroma)
    k_eff = np.minimum(
        k ** ((1.0 - damping) + damping * h),
        SEPARATION_K_MAX,
    )
    return mean + k_eff * diff


def wb_to_kelvin(magenta: float, yellow: float) -> float:
    """Nominal print temperature: least-squares projection onto the
    Planckian (mired) direction; 5500 K at neutral. Not colorimetric."""
    km, ky = TEMP_K_MAGENTA, TEMP_K_YELLOW
    dmu = (km * magenta + ky * yellow) / (km * km + ky * ky)
    mu = min(
        max(1e6 / TEMP_REF_KELVIN + dmu, 1e6 / TEMP_MAX_KELVIN),
        1e6 / TEMP_MIN_KELVIN,
    )
    return float(1e6 / mu)


def kelvin_to_wb(
    kelvin: float, magenta: float, yellow: float
) -> tuple[float, float]:
    """Move (M, Y) along the Planckian direction to `kelvin`, preserving
    the off-locus tint component."""
    km, ky = TEMP_K_MAGENTA, TEMP_K_YELLOW
    kelvin = min(max(kelvin, TEMP_MIN_KELVIN), TEMP_MAX_KELVIN)
    dmu_cur = (km * magenta + ky * yellow) / (km * km + ky * ky)
    delta = (1e6 / kelvin - 1e6 / TEMP_REF_KELVIN) - dmu_cur
    m2 = min(max(magenta + km * delta, CMY_MIN), CMY_MAX)
    y2 = min(max(yellow + ky * delta, CMY_MIN), CMY_MAX)
    return float(m2), float(y2)


def _color_param_bounds() -> tuple[tuple[str, float, float], ...]:
    cmy = (CMY_MIN, CMY_MAX)
    return (
        ("wb_cyan", *cmy),
        ("wb_magenta", *cmy),
        ("wb_yellow", *cmy),
        ("shadow_cyan", *cmy),
        ("shadow_magenta", *cmy),
        ("shadow_yellow", *cmy),
        ("highlight_cyan", *cmy),
        ("highlight_magenta", *cmy),
        ("highlight_yellow", *cmy),
        ("cast_removal", CAST_REMOVAL_MIN, CAST_REMOVAL_MAX),
        (
            "cast_removal_highlights",
            CAST_REMOVAL_HIGHLIGHTS_MIN,
            CAST_REMOVAL_HIGHLIGHTS_MAX,
        ),
        ("dye_separation", DYE_SEPARATION_MIN, DYE_SEPARATION_MAX),
        ("separation_damping", SEPARATION_DAMPING_MIN, SEPARATION_DAMPING_MAX),
    )
