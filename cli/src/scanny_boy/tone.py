"""The preview's tone adjustment: a paper-grade contrast curve plus
midtone-snap and density/zone/toe-shoulder trims, applied where the
display encode happens.

The published TIFF holds normalized log density; its preview display is a
deliberately flat, contrast-free inversion (`previews.py`). That is honest
but hard to judge, so the Edit screen offers a nondestructive tone
adjustment — recorded in the ops log as a `tone` op (`repo.TONE_OP`). The
display LUT composes it into the preview's 8-bit encode, and the export's
render (`render.py`) bakes the same curve into the exported pixels at
full resolution (docs/EXPORT_PLAN.md §4.6) — the same `curve_values`, so
preview and export cannot drift apart. The published TIFF itself is never
touched; the curve owns pixels only where a *rendering* is made.

Colour shaping (global/regional CMY, cast removal) composes in the same
per-channel tables; dye separation is the one control that is not a LUT
(see `color.py` and `previews.py`).

The math is a simplified port of NegPy's print curve
(`NegPy/negpy/features/exposure/logic.py`, `CharacteristicCurve` /
`_apply_print_curve_kernel`), operating on the *positive display value*
v ∈ [0, 1] after `1 - val`:

- **Grade** — an ISO-R paper "range" value (`grade_r`, 50–180; lower is
  harder) turned into a straight-line slope about the midtone pivot.
- **Density** — an input-pivot offset before the grade rotation.
- **Snap** — NegPy's anchor-preserving variable midtone gamma.
- **Zone density** — mid-sparing sigmoid offsets on the quarter and
  three-quarter tones, read on the post-Snap value.
- **Knees** — parameterised softplus toe and shoulder bounds.

Three uint16 → float tables (one per channel when colour is active)
compose steps 1–7; the endpoint rescale is shared across channels so
cast removal and CMY are not self-cancelling (COLOR_PLAN §1.6).
"""

from __future__ import annotations

import dataclasses

import numpy as np

from scanny_boy import color

# Grade (ISO-R paper range).
GRADE_MIN = 50.0
GRADE_MAX = 180.0
GRADE_REFERENCE = 115.0
GRADE_SLOPE_REF = 1.55
SLOPE_MIN = 0.5
SLOPE_MAX = 4.0

# Snap (midtone gamma trim).
SNAP_MIN = -0.5
SNAP_MAX = 0.5
SNAP_WIDTH = 0.6

# Print density — NegPy's range, higher is denser (darker).
DENSITY_MIN = 0.0
DENSITY_MAX = 2.0
DENSITY_REFERENCE = 1.0
DENSITY_PIVOT_SHIFT = 0.2

# Zone density.
SHADOW_DENSITY_MIN = -0.9
SHADOW_DENSITY_MAX = 0.9
HIGHLIGHT_DENSITY_MIN = -0.5
HIGHLIGHT_DENSITY_MAX = 0.5
ZONE_SHADOW_CENTRE = 0.25
ZONE_HIGHLIGHT_CENTRE = 0.75
ZONE_SHARPNESS = 9.0
ZONE_DENSITY_SCALE = 0.28

# Toe and shoulder knees.
TOE_MIN = -1.0
TOE_MAX = 1.0
TOE_WIDTH_MIN = 0.1
TOE_WIDTH_MAX = 5.0
SHOULDER_MIN = -1.0
SHOULDER_MAX = 1.0
SHOULDER_WIDTH_MIN = 0.1
SHOULDER_WIDTH_MAX = 5.0
WIDTH_REFERENCE = 2.5
TOE_HEIGHT = 0.18
SHOULDER_HEIGHT = 0.25
KNEE_SHARPNESS = 9.0
KNEE_SHARPEN = 3.4

MAX_CODE = 65535

TONE_PARAM_KEYS = (
    "grade_r",
    "snap_gamma",
    "density",
    "shadow_density",
    "highlight_density",
    "toe",
    "toe_width",
    "shoulder",
    "shoulder_width",
)


@dataclasses.dataclass(frozen=True)
class ToneParams:
    grade_r: float = GRADE_REFERENCE
    snap_gamma: float = 0.0
    density: float = DENSITY_REFERENCE
    shadow_density: float = 0.0
    highlight_density: float = 0.0
    toe: float = 0.0
    toe_width: float = WIDTH_REFERENCE
    shoulder: float = 0.0
    shoulder_width: float = WIDTH_REFERENCE


NEUTRAL = ToneParams()


def grade_slope(grade_r: float) -> float:
    """The straight-line midtone slope for one ISO-R grade value."""
    return min(SLOPE_MAX, max(SLOPE_MIN, GRADE_SLOPE_REF * GRADE_REFERENCE / grade_r))


def _softplus(x: np.ndarray | float) -> np.ndarray | float:
    """Numerically stable softplus: log(1 + exp(x))."""
    if isinstance(x, np.ndarray):
        out = np.log1p(np.exp(-np.abs(x))) + np.maximum(x, 0.0)
        return out
    if x > 0:
        return x + float(np.log1p(np.exp(-x)))
    return float(np.log1p(np.exp(x)))


def _expit(x: np.ndarray | float) -> np.ndarray | float:
    return 0.5 * (1.0 + np.tanh(0.5 * np.asarray(x, dtype=np.float64)))


def _neutral_shaping(tone_params: ToneParams) -> ToneParams:
    return dataclasses.replace(
        tone_params,
        density=DENSITY_REFERENCE,
        shadow_density=0.0,
        highlight_density=0.0,
        toe=0.0,
        shoulder=0.0,
        toe_width=WIDTH_REFERENCE,
        shoulder_width=WIDTH_REFERENCE,
    )


def _curve_raw(
    values: np.ndarray,
    tone_params: ToneParams,
    color_params: color.ColorParams,
    *,
    channel: int | None,
    metering: color.Metering,
    apply_color: bool,
) -> np.ndarray:
    """Steps 2–6 on display values; global CMY is applied before the flip
    in `build_channel_tables`. `channel=None` is the achromatic path."""
    base_slope = grade_slope(tone_params.grade_r)
    pivot_out = 0.5
    pivot_in = 0.5 + (tone_params.density - DENSITY_REFERENCE) * DENSITY_PIVOT_SHIFT
    if apply_color and channel is not None:
        per_channel = color.cast_slopes(
            color_params, metering, base_slope, pivot_in
        )
        slope, pivot_in = per_channel[channel]
    else:
        slope = base_slope

    v = pivot_out + slope * (values - pivot_in)
    if tone_params.snap_gamma != 0.0:
        v = v + tone_params.snap_gamma * SNAP_WIDTH * np.tanh((v - pivot_out) / SNAP_WIDTH)

    if apply_color and channel is not None:
        shadow_cmy, highlight_cmy = color.region_cmy(color_params)
        w_sh = _expit(color.REGION_SHARPNESS * (color.REGION_CENTRE - v))
        w_hi = 1.0 - w_sh
        regional = shadow_cmy[channel] * w_sh + highlight_cmy[channel] * w_hi
        v = v - color.REGION_CMY_SCALE * regional

    w_sh = _expit(ZONE_SHARPNESS * (ZONE_SHADOW_CENTRE - v))
    w_hi = _expit(ZONE_SHARPNESS * (v - ZONE_HIGHLIGHT_CENTRE))
    v = v - ZONE_DENSITY_SCALE * (
        tone_params.shadow_density * w_sh + tone_params.highlight_density * w_hi
    )
    a_base = KNEE_SHARPNESS * max(slope, 1.0)
    a_toe = a_base * WIDTH_REFERENCE / tone_params.toe_width
    a_shoulder = a_base * WIDTH_REFERENCE / tone_params.shoulder_width
    toe_floor = tone_params.toe * TOE_HEIGHT if tone_params.toe >= 0 else 0.0
    if tone_params.toe < 0:
        a_toe = a_toe * (1.0 - tone_params.toe * KNEE_SHARPEN)
    shoulder_ceil = (
        1.0 - tone_params.shoulder * SHOULDER_HEIGHT
        if tone_params.shoulder >= 0
        else 1.0
    )
    if tone_params.shoulder < 0:
        a_shoulder = a_shoulder * (1.0 - tone_params.shoulder * KNEE_SHARPEN)
    shoulder_ceil = max(shoulder_ceil, toe_floor + 0.1)
    v = toe_floor + _softplus(a_toe * (v - toe_floor)) / a_toe
    v = shoulder_ceil - _softplus(a_shoulder * (shoulder_ceil - v)) / a_shoulder
    return v


def curve_values(
    values: np.ndarray,
    tone_params: ToneParams,
    color_params: color.ColorParams = color.NEUTRAL_COLOR,
    *,
    channel: int | None = None,
    metering: color.Metering | None = None,
    apply_color: bool = True,
) -> np.ndarray:
    """Maps positive display values through the tone+colour curve. Monotone;
    endpoints pinned using grade/snap-only anchors read on the achromatic
    curve with every colour control at rest."""
    if metering is None:
        metering = color.Metering(ranges=(1.0, 1.0, 1.0), shadow_refs_norm=None)
    use_color = apply_color and channel is not None
    raw = _curve_raw(
        values,
        tone_params,
        color_params,
        channel=channel,
        metering=metering,
        apply_color=use_color,
    )
    neutral_tone = _neutral_shaping(tone_params)
    low = float(
        _curve_raw(
            np.array([0.0]),
            neutral_tone,
            color.NEUTRAL_COLOR,
            channel=None,
            metering=metering,
            apply_color=False,
        )[0]
    )
    high = float(
        _curve_raw(
            np.array([1.0]),
            neutral_tone,
            color.NEUTRAL_COLOR,
            channel=None,
            metering=metering,
            apply_color=False,
        )[0]
    )
    if high > low:
        raw = (raw - low) / (high - low)
    return np.clip(raw, 0.0, 1.0)


def build_channel_tables(
    tone_params: ToneParams,
    color_params: color.ColorParams = color.NEUTRAL_COLOR,
    metering: color.Metering | None = None,
    channels: int = 3,
) -> np.ndarray:
    """uint16 code → float display value, shape `(channels, 65536)`.

    On `channels == 1` every colour term is skipped and the single row is
    the achromatic curve."""
    from scanny_boy import normalization

    if metering is None:
        metering = color.Metering(ranges=(1.0, 1.0, 1.0), shadow_refs_norm=None)
    apply_color = channels > 1
    codes = np.arange(MAX_CODE + 1, dtype=np.float64)
    norm = normalization.decode_normalized(codes)
    offsets = color.cmy_offsets(color_params, metering) if apply_color else (0.0,) * channels
    tables = np.empty((channels, MAX_CODE + 1), dtype=np.float64)
    for ch in range(channels):
        offset = offsets[ch] if ch < len(offsets) else 0.0
        if apply_color:
            display = np.clip(1.0 - (norm + offset), 0.0, 1.0)
        else:
            display = np.clip(1.0 - norm, 0.0, 1.0)
        tables[ch] = curve_values(
            display,
            tone_params,
            color_params,
            channel=ch if apply_color else None,
            metering=metering,
            apply_color=apply_color,
        )
    return tables


def build_display_lut(
    tone_params: ToneParams,
    color_params: color.ColorParams = color.NEUTRAL_COLOR,
    metering: color.Metering | None = None,
    channels: int = 3,
) -> np.ndarray:
    """The uint16 normalized-density code → uint8 positive display table.

    When colour is neutral all channel tables are identical; any row suffices
    for the fast single-table path."""
    tables = build_channel_tables(tone_params, color_params, metering, channels)
    return np.rint(tables[0] * 255).astype(np.uint8)
