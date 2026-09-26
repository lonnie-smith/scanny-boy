"""The Auto Balance solve: closed-form warmth/tint from a negative's
recorded neutral estimate.

With automatic tone-split neutral balance (`auto_neutral.py`) applied at
render time, the `--auto-balance` button is a fine global trim on top: it
reads the stored `auto_neutral` band residuals (averaged when both exist)
rather than the stale stitch-time `neutral_residual`.
"""

from __future__ import annotations

import numpy as np

from scanny_boy import auto_neutral, color


def _residual_from_auto_neutral(record: dict) -> tuple[float, float] | None:
    bands = auto_neutral.read_auto_neutral(record)
    if bands is None:
        return None
    parts = [value for value in (bands.shadow, bands.highlight) if value is not None]
    if not parts:
        return None
    stacked = [parts[0][0], parts[0][1]]
    if len(parts) == 2:
        stacked[0] = (parts[0][0] + parts[1][0]) / 2.0
        stacked[1] = (parts[0][1] + parts[1][1]) / 2.0
    return float(stacked[0]), float(stacked[1])


def _neutral_defaults_target(
    residual_a: float, residual_b: float
) -> tuple[float, float, float]:
    """Step 1: the luma-neutral density offsets that null the residual."""
    w_r, _w_g, w_b = color.LUMA_WEIGHTS
    o_g = w_r * residual_a + w_b * residual_b
    return (o_g - residual_a, o_g, o_g - residual_b)


def _warmth_tint_from_offsets(
    offsets: tuple[float, float, float],
) -> tuple[float, float]:
    """§3: project a luma-zero density offset vector onto the balance axes.

    ``d = -o / BALANCE_SCALE`` converts the density offset back to the
    display-direction vector `balance_offsets` would need to reproduce it,
    and the two dot products (under the W inner product, since WARM_AXIS
    and MAGENTA_AXIS are W-orthonormal, not Euclidean-orthonormal) read the
    warmth/tint coordinates straight off — because ``o`` is luma-zero it
    lies exactly in the plane the two axes span, so this round-trips
    exactly through `balance_offsets` before clamping."""
    d = tuple(-value / color.BALANCE_SCALE for value in offsets)
    warmth = float(np.clip(color._w_inner(d, color.WARM_AXIS), -1.0, 1.0))
    tint = float(np.clip(color._w_inner(d, color.MAGENTA_AXIS), -1.0, 1.0))
    return warmth, tint


def solve_balance(
    record: dict | None,
    params: color.ColorParams,
    slope: float,
    pivot_in: float,
    highlight_lock=None,
) -> tuple[float, float] | None:
    """Warmth/tint values that null the stored auto-neutral residual.

    Steps, unchanged from the pre-balance `solve_cmy` (§3): the target
    density offsets from the residual, the cast-slope compensation loop
    (so a two-point cast-removal tie already in effect is accounted for
    rather than fought), and luma removal — leaving a luma-zero offset
    vector projected onto the two balance axes.

    Returns `None` when `record` is missing or has no usable `auto_neutral`
    estimate. Never raises."""
    import math

    if not record:
        return None
    residual_pair = _residual_from_auto_neutral(record)
    if residual_pair is None:
        return None
    a, b = residual_pair
    if not (math.isfinite(a) and math.isfinite(b)):
        return None

    metering = color.read_metering(record, highlight_lock=highlight_lock)
    offsets = list(_neutral_defaults_target(a, b))

    per_channel = color.cast_slopes(params, metering, slope, pivot_in)
    for ch, (slope_ch, pivot_ch) in enumerate(per_channel):
        if ch == 1:
            continue
        d_ch = slope_ch * (pivot_in - pivot_ch)
        offsets[ch] -= -d_ch / slope
    luma_mean = color._luma_weighted_sum(tuple(offsets))
    o = tuple(value - luma_mean for value in offsets)

    return _warmth_tint_from_offsets(o)
