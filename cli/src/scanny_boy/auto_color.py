"""The Auto Cast Removal solve: closed-form global CMY from a negative's
recorded neutral estimate.

Mirrors `auto_tone.py`'s shape (docs/CAST_REMOVAL_PLAN.md §3.3): pure
functions over a negative's recorded `normalization` block, no image I/O,
no metering pass — the meter (`normalization.measure_neutral_residual`)
ran at stitch time and is read back here.

The estimator is a port of darktable's grey-surfaces illuminant detector;
the solve turns its `(R-G, B-G)` residual into the three CMY slider values
a printer would dial. Auto writes `wb_cyan / wb_magenta / wb_yellow` and
touches nothing else (§0.5): the two ties are anchored on this negative's
own percentile references and are fully determined by them, while the
grey-surfaces estimator measures a *global* neutral residual over the
whole tonal range — a filtration correction.
"""

from __future__ import annotations

from scanny_boy import color


def _neutral_defaults_target(
    residual_a: float, residual_b: float
) -> tuple[float, float, float]:
    """Step 2: the mean-removed global CMY offsets that null the residual.

    A global CMY offset adds `o_ch` to the normalized `u_ch`. Nulling the
    residual means `o_R - o_G = -a` and `o_B - o_G = -b`; combined with the
    mean removal `o_R + o_G + o_B = 0` that solves in closed form. The sum
    is zero by construction."""
    return (
        (residual_b - 2.0 * residual_a) / 3.0,
        (residual_a + residual_b) / 3.0,
        (residual_a - 2.0 * residual_b) / 3.0,
    )


def solve_cmy(
    record: dict | None,
    params: color.ColorParams,
    slope: float,
    pivot_in: float,
) -> tuple[float, float, float] | None:
    """Global CMY slider values that null the recorded neutral residual.

    Returns `None` when `record` is missing, has no `neutral_residual`, or
    the value is not a two-element list of finite numbers. Never raises.

    Steps 1-3 read the residual, solve the mean-removed offsets that null
    it, and subtract what the cast-removal ties already do at the anchor.
    That compensation is **exactly zero in every one-point branch** (there
    `pivot_ch == pivot_in`), so Auto's behaviour on today's state is
    unchanged; it appears only once a highlight tie is dialled in. It is a
    first-order proxy — the tie's effect is evaluated at the anchor while
    the residual is a whole-image average — not an exact cancellation.

    Step 4 converts to sliders through `cmy_offsets`' inverse. Because the
    target is already mean-zero, `cmy_offsets` reproduces it exactly (its
    own mean removal is then a no-op); clamping can break that, and a
    clamped solve is a saturated one."""
    import math

    if not record:
        return None
    residual = record.get("neutral_residual")
    if (
        not isinstance(residual, list)
        or len(residual) != 2
        or any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            for value in residual
        )
    ):
        return None
    a, b = (float(value) for value in residual)
    if not (math.isfinite(a) and math.isfinite(b)):
        return None

    metering = color.read_metering(record)
    offsets = list(_neutral_defaults_target(a, b))

    # Step 3: subtract what the ties already do at the anchor. For each
    # channel the tie's display value at the anchor x = pivot_in is
    # pivot_out + slope_ch*(pivot_in - pivot_ch), and green's is pivot_out;
    # the difference is d_ch = slope_ch*(pivot_in - pivot_ch), and since
    # dv = -slope*o the equivalent input offset is tie_ch = -d_ch/slope.
    per_channel = color.cast_slopes(params, metering, slope, pivot_in)
    for ch, (slope_ch, pivot_ch) in enumerate(per_channel):
        if ch == 1:
            continue
        d_ch = slope_ch * (pivot_in - pivot_ch)
        offsets[ch] -= -d_ch / slope
    # Re-remove the mean of the three after the compensation.
    mean = sum(offsets) / 3.0
    offsets = [value - mean for value in offsets]

    # Step 4: the inverse of `cmy_offsets` (slider -> offset * range /
    # CMY_MAX_DENSITY), clamped into the sliders' range.
    sliders = tuple(
        min(
            max(offsets[ch] * metering.ranges[ch] / color.CMY_MAX_DENSITY, color.CMY_MIN),
            color.CMY_MAX,
        )
        for ch in range(3)
    )
    return sliders
