"""The Auto Cast Removal solve: closed-form global CMY from a negative's
recorded neutral estimate.

With automatic tone-split neutral balance (`auto_neutral.py`) applied at
render time, the `--auto-cast` button is a fine global trim on top: it
reads the stored `auto_neutral` band residuals (averaged when both exist)
rather than the stale stitch-time `neutral_residual`.
"""

from __future__ import annotations

from scanny_boy import auto_neutral, color


def _neutral_defaults_target(
    residual_a: float, residual_b: float
) -> tuple[float, float, float]:
    """Step 2: the luma-neutral global CMY offsets that null the residual."""
    w_r, _w_g, w_b = color.LUMA_WEIGHTS
    o_g = w_r * residual_a + w_b * residual_b
    return (o_g - residual_a, o_g, o_g - residual_b)


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


def solve_cmy(
    record: dict | None,
    params: color.ColorParams,
    slope: float,
    pivot_in: float,
    highlight_lock=None,
) -> tuple[float, float, float] | None:
    """Global CMY slider values that null the stored auto-neutral residual.

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
    offsets = [value - luma_mean for value in offsets]

    sliders = tuple(
        min(
            max(
                offsets[ch]
                * metering.ranges[ch]
                / (color.CMY_MAX_DENSITY * color.CMY_SLIDER_GAIN[ch]),
                color.CMY_MIN,
            ),
            color.CMY_MAX,
        )
        for ch in range(3)
    )
    return sliders
