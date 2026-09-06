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
CAST_REMOVAL_MIN = 0.0
CAST_REMOVAL_MAX = 1.0
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
    dye_separation: float = 1.0
    separation_damping: float = 0.0


NEUTRAL_COLOR = ColorParams()


@dataclasses.dataclass(frozen=True)
class Metering:
    """The colour-relevant slice of a negative's `normalization` record."""

    ranges: tuple[float, ...]
    shadow_refs_norm: tuple[float, ...] | None


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
    return Metering(ranges=tuple(ranges), shadow_refs_norm=shadow_refs_norm)


def cmy_offsets(params: ColorParams, metering: Metering) -> tuple[float, ...]:
    """Global CMY as normalized log-density input offsets (§1.2)."""
    sliders = (params.wb_cyan, params.wb_magenta, params.wb_yellow)
    return tuple(
        slider * CMY_MAX_DENSITY / metering.ranges[ch]
        for ch, slider in enumerate(sliders)
    )


def region_cmy(params: ColorParams) -> tuple[tuple[float, ...], ...]:
    """Regional shadow/highlight CMY slider tuples (§1.3)."""
    shadow = (params.shadow_cyan, params.shadow_magenta, params.shadow_yellow)
    highlight = (
        params.highlight_cyan,
        params.highlight_magenta,
        params.highlight_yellow,
    )
    return shadow, highlight


def cast_slopes(
    params: ColorParams,
    metering: Metering,
    slope: float,
    pivot_in: float,
) -> tuple[tuple[float, float], ...]:
    """Per-channel (slope, pivot_in) for cast removal's shadow-tie branch.

    Ports NegPy's fallback one-point tie (`logic.py:1067-1088`), not the
    neutral-axis branch — we do not measure the refs that branch needs.
    Coordinates are display space (`x_display = 1 - x_norm`).
    """
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
        ("dye_separation", DYE_SEPARATION_MIN, DYE_SEPARATION_MAX),
        ("separation_damping", SEPARATION_DAMPING_MIN, SEPARATION_DAMPING_MAX),
    )
