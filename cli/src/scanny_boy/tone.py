"""The preview's tone adjustment: a paper-grade contrast curve plus a
midtone-snap trim, applied where the display encode happens.

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
- **Snap** — NegPy's anchor-preserving variable midtone gamma:
  `v += snap * SNAP_WIDTH * tanh((v - pivot) / SNAP_WIDTH)`, zero at the
  pivot, easing to nothing toward the endpoints; positive values steepen
  the midtones.
- **Knees** — softplus toe and shoulder (the H&D paper shape), so the
  steepened midtone rolls off smoothly into display black/white instead
  of hard-clipping; knee sharpness grows with the grade slope. The
  composed curve is then rescaled to pin 0 → 0 and 1 → 1, so soft grades
  still reach full black and white.

Everything composes into one uint16 → uint8 LUT per parameter pair (the
display encode's own shape), so applying the adjustment costs the same
table lookup as the unadjusted preview.
"""

from __future__ import annotations

import numpy as np

# Grade (ISO-R paper range) — matches `repo.TONE_GRADE_*` validation.
GRADE_MIN = 50.0
GRADE_MAX = 180.0
# The grade the slope reference is calibrated at.
GRADE_REFERENCE = 115.0
# Midtone slope at `GRADE_REFERENCE`. The flat display mapping has slope 1;
# this lands the default grade at a print-like contrast while the softest
# end of the grade range comes back to roughly the flat look.
GRADE_SLOPE_REF = 1.55
SLOPE_MIN = 0.5
SLOPE_MAX = 4.0

# Snap (midtone gamma trim), matching `repo.TONE_SNAP_*` validation.
SNAP_MIN = -0.5
SNAP_MAX = 0.5
# Tanh width of the snap bump, NegPy's `paper_gamma_width`.
SNAP_WIDTH = 0.6

# Softplus knee sharpness at slope 1; scales with the grade slope so harder
# grades roll off proportionally sooner.
KNEE_SHARPNESS = 9.0

MAX_CODE = 65535


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


def _curve_raw(values: np.ndarray, grade_r: float, snap_gamma: float) -> np.ndarray:
    """The grade + snap curve with softplus knees, endpoints free."""
    slope = grade_slope(grade_r)
    pivot = 0.5
    v = pivot + slope * (values - pivot)
    if snap_gamma != 0.0:
        # NegPy's anchor-preserving variable midtone gamma: a tanh bump
        # that vanishes at the pivot and eases out toward the endpoints.
        v = v + snap_gamma * SNAP_WIDTH * np.tanh((v - pivot) / SNAP_WIDTH)
    a = KNEE_SHARPNESS * max(slope, 1.0)
    toe = _softplus(a * v) / a
    return 1.0 - _softplus(a * (1.0 - toe)) / a


def curve_values(values: np.ndarray, grade_r: float, snap_gamma: float) -> np.ndarray:
    """Maps positive display values (floats in [0, 1]) through the grade +
    snap tone curve. Monotone; endpoints pinned to 0 and 1."""
    raw = _curve_raw(values, grade_r, snap_gamma)
    # Pin the endpoints so even the softest grade reaches full black and
    # white; the midtone slope is unchanged to first order. (The display
    # domain is inverted — code 0 is the scene highlight — so the anchors
    # are taken at the curve's own 0 and 1, not the array's ends.)
    low = float(_curve_raw(np.array([0.0]), grade_r, snap_gamma)[0])
    high = float(_curve_raw(np.array([1.0]), grade_r, snap_gamma)[0])
    if high > low:
        raw = (raw - low) / (high - low)
    return np.clip(raw, 0.0, 1.0)


def build_display_lut(grade_r: float, snap_gamma: float) -> np.ndarray:
    """The full uint16 normalized-density code → uint8 positive display
    table with the tone curve composed in: the unadjusted display value
    (`1 - val`, bare 8-bit scaling) run through `curve_values`."""
    from scanny_boy import normalization

    codes = np.arange(MAX_CODE + 1, dtype=np.float64)
    base = np.clip(1.0 - normalization.decode_normalized(codes), 0.0, 1.0)
    toned = curve_values(base, grade_r, snap_gamma)
    return np.rint(toned * 255).astype(np.uint8)
