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

# Per-slider scale applied before luma removal. Magenta starts at 0.5 so
# equal travel matches cyan/yellow feel; cyan and yellow stay 1.0.
CMY_SLIDER_GAIN = (1.0, 0.5, 1.0)

# Cast removal — NegPy's cast_removal_max_offset, same normalized units.
# CAST_MAX_OFFSET bounds BOTH ends' ties.
CAST_REMOVAL_MIN = 0.0
CAST_REMOVAL_MAX = 1.0
CAST_REMOVAL_HIGHLIGHTS_MIN = 0.0
CAST_REMOVAL_HIGHLIGHTS_MAX = 1.0
CAST_MAX_OFFSET = 0.1

# Regional CMY — calibrated for our 0..1 display axis. Zone weights match
# the tone panel's shadow/highlight density centres (0.25 / 0.75).
REGION_CMY_SCALE = 1.0

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

# Rec.709 luma weights — used for lightness-neutral mean removal and dye
# separation so a colour move does not change perceived brightness.
LUMA_WEIGHTS = (0.2126, 0.7152, 0.0722)

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
# `repo._parse_color_op` can recognise an older op: a twelve-key op is a
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
    predates it or when the dense-end neutral band
    held no trustworthy set — which is load-bearing information, not an
    error.

    `highlight_floor_delta` is docs/ROLL_HIGHLIGHT_LOCK.md's render-time
    correction, `floor_old[ch] - floor_new[ch]` in log10 D — `None` when no
    roll highlight lock applies (a mono negative, or a roll with no
    qualifying negative). `ranges` and `highlight_refs_norm` above are
    already computed against the *corrected* floor when one applies —
    `read_metering`'s `highlight_lock` argument threads through to every
    consumer of this object, cast removal and the global CMY sliders
    included, so what those controls tie against is the same colour the
    render actually shows (§2.3 of that plan: "one effective-bounds
    function, not ad hoc patches"). `highlight_floor_delta` itself is what
    `render.py`/`tone.py` apply to the *decoded pixels*, immediately after
    `normalization.decode_normalized` and before anything else — see
    `remap_dense_end`."""

    ranges: tuple[float, ...]
    shadow_refs_norm: tuple[float, ...] | None
    highlight_refs_norm: tuple[float, ...] | None = None
    highlight_floor_delta: tuple[float, ...] | None = None


def _corrected_floors_and_delta(
    floors: list, ceils: list, highlight_lock, record: dict
) -> tuple[list, tuple[float, ...] | None]:
    """docs/ROLL_HIGHLIGHT_LOCK.md §2: the corrected dense-end floor this
    record's `ranges`/`*_refs_norm` should be measured against, plus the
    `(floor_old - floor_new)` delta `render.py`/`tone.py` apply to decoded
    pixels. `floors` unchanged and delta `None` whenever there is nothing
    to correct — no lock passed in, or a non-3-channel record (mono, or a
    malformed block `read_metering`'s caller already gave up on).

    `highlight_lock` accepts either a `highlight_lock.HighlightLock`
    instance or the roll manifest's raw `highlight_lock` dict — every
    caller of `read_metering` already has one or the other lying around
    (the manifest dict when it just loaded the roll, the dataclass when it
    is threading one through from somewhere that already converted), and
    making this boundary accept both means neither call site has to import
    `highlight_lock` just to convert a `None`.

    `highlight_refs` is this record's own (possibly `None`) recorded
    measurement, passed straight through to `corrected_floors` — see that
    function for why a qualifying negative's real amplitude and a
    non-qualifying negative's green-only approximation are not
    interchangeable."""
    if highlight_lock is None or len(floors) != 3 or len(ceils) != 3:
        return floors, None
    from scanny_boy.highlight_lock import HighlightLock, base_offset_for, corrected_floors

    lock = (
        highlight_lock
        if isinstance(highlight_lock, HighlightLock)
        else HighlightLock.from_dict(highlight_lock)
    )
    if lock is None:
        return floors, None

    try:
        floors_f = tuple(float(v) for v in floors)
        ceils_f = tuple(float(v) for v in ceils)
    except (TypeError, ValueError):
        return floors, None
    highlight_refs = record.get("highlight_refs")
    refs_f = None
    if isinstance(highlight_refs, list) and len(highlight_refs) == 3:
        try:
            refs_f = tuple(float(v) for v in highlight_refs)
        except (TypeError, ValueError):
            refs_f = None
    offset = base_offset_for(record)
    new_floors = corrected_floors(floors_f, ceils_f, lock, refs_f, offset)
    delta = tuple(old - new for old, new in zip(floors_f, new_floors, strict=True))
    if all(abs(d) < 1e-12 for d in delta):
        return list(new_floors), None
    return list(new_floors), delta


def read_metering(record: dict | None, highlight_lock=None) -> Metering:
    """Never raises. Missing or incomplete records yield uncalibrated CMY
    and inert cast removal.

    `highlight_lock` is the roll's highlight-colour lock — a
    `highlight_lock.HighlightLock`, the roll manifest's raw dict, or `None`
    (docs/ROLL_HIGHLIGHT_LOCK.md): when one resolves and this record is a
    3-channel colour negative, the dense-end `floors` this function reads
    everything else against are first retargeted to the roll's highlight
    colour, and `Metering.highlight_floor_delta` records what changed so
    the render path can apply the same correction to decoded pixels."""
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
    for value in (*floors, *ceils):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return Metering(ranges=default_ranges, shadow_refs_norm=None)

    floors, highlight_floor_delta = _corrected_floors_and_delta(
        floors, ceils, highlight_lock, record
    )

    ranges: list[float] = [
        max(abs(float(ceils[ch]) - float(floors[ch])), 1e-6) for ch in range(channels)
    ]
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
    # (ref - floor)/span, same anything-wrong -> None rule. `floor` here
    # is already the corrected one when a highlight lock applies, so the
    # highlight reference's normalized position moves with the same
    # correction the render shows — it is still *this negative's own* raw
    # measurement (`highlight_refs` itself is never rewritten), just read
    # against the corrected floor.
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
        highlight_floor_delta=highlight_floor_delta,
    )


def remap_dense_end(norm: np.ndarray, channel: int, metering: Metering) -> np.ndarray:
    """docs/ROLL_HIGHLIGHT_LOCK.md §2.3: remap `normalization.decode_normalized`'s
    per-channel output from the published stretch to the roll-corrected one,
    fixing the thin end (`val = 1`) exactly — applied immediately after the
    decode and before global CMY / `1 - val` in every render path
    (`render.py`, `tone.py`), so everything downstream composes unchanged.

    `val` is already `(D - floor_old) / (ceil - floor_old)` by construction
    (that is what the published encode is), so `floor_old` and `ceil` are
    implicitly `0` and `1` in `val`'s own units — the only two free
    quantities are `delta = floor_old - floor_new` (log10 D) and `range =
    ceil - floor_new` (`Metering.ranges[channel]`, already measured against
    the corrected floor by `read_metering`):

        D          = floor_old + val * (ceil - floor_old)
        val_new    = (D - floor_new) / (ceil - floor_new)
                   = (delta + val * (range - delta)) / range

    Identity (`norm` returned unchanged, not merely equal) when this
    channel has no correction — `metering.highlight_floor_delta is None`,
    or the channel is out of range (mono, or a malformed record) — so
    calling this unconditionally on every channel of every render is safe
    and costs nothing extra on a roll with no lock."""
    delta_tuple = metering.highlight_floor_delta
    if delta_tuple is None or channel >= len(delta_tuple):
        return norm
    delta = delta_tuple[channel]
    if delta == 0.0:
        return norm
    span = metering.ranges[channel]
    if span <= 0.0 or not np.isfinite(span):
        return norm
    return (delta + norm * (span - delta)) / span


def cmy_offsets(params: ColorParams, metering: Metering) -> tuple[float, ...]:
    """Global CMY as normalized log-density input offsets, made
    **lightness-neutral**: the raw range-divided offsets are luma-mean-
    removed, so moving the sliders changes hue without changing Rec.709
    luma — Print Density and the zone controls keep sole ownership of
    lightness.

    The mean is removed *after* the range division because the curve
    applies the same slope to every channel near the pivot, so the display
    shift's luma is proportional to the luma-weighted mean of the post-
    division values; zeroing that is what holds lightness. An equal three-
    slider move is not a no-op when the ranges differ — it is a pure hue
    move at constant lightness."""
    sliders = (params.wb_cyan, params.wb_magenta, params.wb_yellow)
    if len(sliders) != len(metering.ranges):
        # Unreachable in production (mono never applies colour), but a
        # malformed record must not index out of range.
        return (0.0,) * len(sliders)
    raw = [
        slider
        * CMY_MAX_DENSITY
        * CMY_SLIDER_GAIN[ch]
        / max(metering.ranges[ch], 1e-6)
        for ch, slider in enumerate(sliders)
    ]
    return _luma_removed(raw)


def region_cmy(params: ColorParams) -> tuple[tuple[float, ...], ...]:
    """Regional shadow/highlight CMY slider tuples, each **luma-mean-
    removed**: they are added to the display value directly, so a luma-zero
    triple contributes a luma-neutral display shift — the region controls
    are purely chromatic and stop competing with the shadow/highlight
    density trims."""
    shadow = (
        params.shadow_cyan * CMY_SLIDER_GAIN[0],
        params.shadow_magenta * CMY_SLIDER_GAIN[1],
        params.shadow_yellow * CMY_SLIDER_GAIN[2],
    )
    highlight = (
        params.highlight_cyan * CMY_SLIDER_GAIN[0],
        params.highlight_magenta * CMY_SLIDER_GAIN[1],
        params.highlight_yellow * CMY_SLIDER_GAIN[2],
    )
    return _luma_removed(shadow), _luma_removed(highlight)


def _luma_weighted_sum(triple: tuple[float, ...]) -> float:
    return sum(value * weight for value, weight in zip(triple, LUMA_WEIGHTS))


def _luma_removed(triple: tuple[float, ...]) -> tuple[float, ...]:
    """Subtract the scalar that zeroes the Rec.709 luma-weighted sum."""
    mean = _luma_weighted_sum(triple)
    return tuple(value - mean for value in triple)


def _one_point_cast_slopes(
    params: ColorParams,
    metering: Metering,
    slope: float,
    pivot_in: float,
) -> tuple[tuple[float, float], ...]:
    """Today's one-point tie, kept verbatim as the fallback branch —
    do not rewrite it, and do
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
    the tie has **two points**: each
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

    The endpoint rescale anchors are
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
    """Spread each pixel about its Rec.709 luma. float32 in/out."""
    out = np.asarray(rgb, dtype=np.float32)
    if params.dye_separation == 1.0 and params.separation_damping == 0.0:
        return out
    k = params.dye_separation
    damping = params.separation_damping
    luma = (
        LUMA_WEIGHTS[0] * out[..., 0:1]
        + LUMA_WEIGHTS[1] * out[..., 1:2]
        + LUMA_WEIGHTS[2] * out[..., 2:3]
    )
    diff = out - luma
    chroma = np.sqrt(
        (diff[..., 0:1] ** 2 + diff[..., 1:2] ** 2 + diff[..., 2:3] ** 2) / 3.0
    )
    if damping == 0.0:
        return luma + k * diff
    h = (SEPARATION_REF_SPREAD - chroma) / (SEPARATION_REF_SPREAD + chroma)
    k_eff = np.minimum(
        k ** ((1.0 - damping) + damping * h),
        SEPARATION_K_MAX,
    )
    return luma + k_eff * diff


def wb_to_kelvin(magenta: float, yellow: float) -> float:
    """Nominal illuminant temperature: least-squares projection onto the
    Planckian (mired) direction; 5500 K at neutral. Higher K is warmer
    (Lightroom convention). Not colorimetric."""
    km, ky = TEMP_K_MAGENTA, TEMP_K_YELLOW
    dmu = -(km * magenta + ky * yellow) / (km * km + ky * ky)
    mu = min(
        max(1e6 / TEMP_REF_KELVIN + dmu, 1e6 / TEMP_MAX_KELVIN),
        1e6 / TEMP_MIN_KELVIN,
    )
    return float(1e6 / mu)


def kelvin_to_wb(
    kelvin: float, magenta: float, yellow: float
) -> tuple[float, float]:
    """Move (M, Y) along the Planckian direction to `kelvin`, preserving
    the off-locus tint component. Higher K warms the image."""
    km, ky = TEMP_K_MAGENTA, TEMP_K_YELLOW
    kelvin = min(max(kelvin, TEMP_MIN_KELVIN), TEMP_MAX_KELVIN)
    dmu_cur = -(km * magenta + ky * yellow) / (km * km + ky * ky)
    delta = -(1e6 / kelvin - 1e6 / TEMP_REF_KELVIN) - dmu_cur
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
