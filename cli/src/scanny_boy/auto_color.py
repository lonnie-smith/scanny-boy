"""The Auto Balance solve: closed-form warmth/tint from a negative's
recorded neutral estimate.

With automatic tone-split neutral balance (`auto_neutral.py`) applied at
render time, the `--auto-balance` button is a fine global trim on top: it
reads the stored `auto_neutral` band residuals (averaged when both exist)
rather than the stale stitch-time `neutral_residual`.
"""

from __future__ import annotations

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


def _residual_to_display_offsets(
    residual_a: float, residual_b: float
) -> tuple[float, float, float]:
    """The luma-neutral display-direction offsets that null the residual."""
    w_r, _w_g, w_b = color.LUMA_WEIGHTS
    o_g = w_r * residual_a + w_b * residual_b
    return (o_g - residual_a, o_g, o_g - residual_b)


def _project_display_offsets(
    display_offsets: tuple[float, float, float],
) -> tuple[float, float]:
    """Project a display-direction offset pair onto the balance axes,
    returning (warmth, tint) in [-1, 1]."""
    import numpy as np

    d = np.array(display_offsets)
    warmth = float(np.clip(
        np.dot(d, color.WARM_AXIS) / color.BALANCE_SCALE, -1.0, 1.0
    ))
    tint = float(np.clip(
        np.dot(d, color.MAGENTA_AXIS) / color.BALANCE_SCALE, -1.0, 1.0
    ))
    return warmth, tint


def solve_balance(
    record: dict | None,
    params: color.ColorParams,
    slope: float,
    pivot_in: float,
    highlight_lock=None,
) -> tuple[float, float] | None:
    """Warmth/tint values that null the stored auto-neutral residual.

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

    display_offsets = _residual_to_display_offsets(a, b)
    return _project_display_offsets(display_offsets)
