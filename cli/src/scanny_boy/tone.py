"""The preview's tone adjustment: a paper-grade contrast curve plus
midtone-snap and density/zone/toe-shoulder trims, applied where the
display encode happens.

The published TIFF holds normalized log density; its preview display is a
deliberately flat, contrast-free inversion (`previews.py`). That is honest
but hard to judge, so the Edit screen offers a nondestructive tone
adjustment — recorded in the ops log as a `tone` op (`repo.TONE_OP`), a
state the display LUT consumes, never baked into any TIFF.

The math is a simplified port of NegPy's print curve
(`NegPy/negpy/features/exposure/logic.py`, `CharacteristicCurve` /
`_apply_print_curve_kernel`), operating on the *positive display value*
v ∈ [0, 1] after `1 - val`:

- **Grade** — an ISO-R paper "range" value (`grade_r`, 50–180; lower is
  harder) turned into a straight-line slope about the midtone pivot:
  `k = GRADE_SLOPE_REF * 115 / grade_r`. Unlike NegPy's print engine —
  where R115 is a real grade-2-ish paper — our baseline is the flat linear
  mapping, so the reference is chosen to land R115 at a print-like
  midtone slope (~1.55×) and the softest end of the range near the flat
  look. Slope is clamped to `[SLOPE_MIN, SLOPE_MAX]`.
- **Density** — an input-pivot offset before the grade rotation: higher
  values translate the pivot so the print is denser (darker) without
  changing the midtone slope.
- **Snap** — NegPy's anchor-preserving variable midtone gamma:
  `v += snap * SNAP_WIDTH * tanh((v - pivot) / SNAP_WIDTH)`, zero at the
  pivot, easing to nothing toward the endpoints; positive values steepen
  the midtones.
- **Zone density** — mid-sparing sigmoid offsets on the quarter and
  three-quarter tones, read on the post-Snap value so the controls track
  the print you are looking at as grade moves.
- **Knees** — parameterised softplus toe and shoulder bounds (the H&D
  paper shape), with width and height exposed; negative values sharpen
  rather than move the bound. The composed curve is rescaled to pin 0 → 0
  and 1 → 1 using anchors read with grade and snap only — the shaping
  controls move the endpoints deliberately.

Everything composes into one uint16 → uint8 LUT per parameter set (the
display encode's own shape), so applying the adjustment costs the same
table lookup as the unadjusted preview.
"""

from __future__ import annotations

import dataclasses

import numpy as np

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
DENSITY_PIVOT_SHIFT = 0.2  # NegPy's density_multiplier.

# Zone density — NegPy's asymmetric ranges kept for vocabulary; the shadow
# control needs more travel than the highlight one to reach a fully blocked
# shadow from the quarter tone (not because log10 density reads smaller
# near paper black on our linear display scale).
SHADOW_DENSITY_MIN = -0.9
SHADOW_DENSITY_MAX = 0.9
HIGHLIGHT_DENSITY_MIN = -0.5
HIGHLIGHT_DENSITY_MAX = 0.5
ZONE_SHADOW_CENTRE = 0.25
ZONE_HIGHLIGHT_CENTRE = 0.75
ZONE_SHARPNESS = 9.0  # 4.0 × 2.3, NegPy's fractional sigmoid width.
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
WIDTH_REFERENCE = 2.5  # NegPy's toeshoulder_width_ref; neutral reproduces today.
TOE_HEIGHT = 0.18
SHOULDER_HEIGHT = 0.25
KNEE_SHARPNESS = 9.0
KNEE_SHARPEN = 3.4  # NegPy's 4.0 with its 0.85 strength folded in.

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


def _curve_raw(values: np.ndarray, params: ToneParams) -> np.ndarray:
    """The full tone curve with softplus knees, endpoints free."""
    slope = grade_slope(params.grade_r)
    pivot_out = 0.5
    pivot_in = 0.5 + (params.density - DENSITY_REFERENCE) * DENSITY_PIVOT_SHIFT
    v = pivot_out + slope * (values - pivot_in)
    if params.snap_gamma != 0.0:
        v = v + params.snap_gamma * SNAP_WIDTH * np.tanh((v - pivot_out) / SNAP_WIDTH)
    w_sh = _expit(ZONE_SHARPNESS * (ZONE_SHADOW_CENTRE - v))
    w_hi = _expit(ZONE_SHARPNESS * (v - ZONE_HIGHLIGHT_CENTRE))
    v = v - ZONE_DENSITY_SCALE * (
        params.shadow_density * w_sh + params.highlight_density * w_hi
    )
    a_base = KNEE_SHARPNESS * max(slope, 1.0)
    a_toe = a_base * WIDTH_REFERENCE / params.toe_width
    a_shoulder = a_base * WIDTH_REFERENCE / params.shoulder_width
    toe_floor = params.toe * TOE_HEIGHT if params.toe >= 0 else 0.0
    if params.toe < 0:
        a_toe = a_toe * (1.0 - params.toe * KNEE_SHARPEN)
    shoulder_ceil = (
        1.0 - params.shoulder * SHOULDER_HEIGHT if params.shoulder >= 0 else 1.0
    )
    if params.shoulder < 0:
        a_shoulder = a_shoulder * (1.0 - params.shoulder * KNEE_SHARPEN)
    if shoulder_ceil < toe_floor + 0.1:
        shoulder_ceil = toe_floor + 0.1
    v = toe_floor + _softplus(a_toe * (v - toe_floor)) / a_toe
    v = shoulder_ceil - _softplus(a_shoulder * (shoulder_ceil - v)) / a_shoulder
    return v


def curve_values(values: np.ndarray, params: ToneParams) -> np.ndarray:
    """Maps positive display values (floats in [0, 1]) through the tone
    curve. Monotone; endpoints pinned using grade/snap-only anchors."""
    raw = _curve_raw(values, params)
    neutral = dataclasses.replace(
        params,
        density=DENSITY_REFERENCE,
        shadow_density=0.0,
        highlight_density=0.0,
        toe=0.0,
        shoulder=0.0,
        toe_width=WIDTH_REFERENCE,
        shoulder_width=WIDTH_REFERENCE,
    )
    low = float(_curve_raw(np.array([0.0]), neutral)[0])
    high = float(_curve_raw(np.array([1.0]), neutral)[0])
    if high > low:
        raw = (raw - low) / (high - low)
    return np.clip(raw, 0.0, 1.0)


def build_display_lut(params: ToneParams) -> np.ndarray:
    """The full uint16 normalized-density code → uint8 positive display
    table with the tone curve composed in."""
    from scanny_boy import normalization

    codes = np.arange(MAX_CODE + 1, dtype=np.float64)
    base = np.clip(1.0 - normalization.decode_normalized(codes), 0.0, 1.0)
    toned = curve_values(base, params)
    return np.rint(toned * 255).astype(np.uint8)
