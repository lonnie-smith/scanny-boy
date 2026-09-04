"""Scan normalization ("Convert"): the log transfer, the meters, and the
encode.

Modelled on NegPy's `docs/PIPELINE.md` section 2 (*Scan Normalization*) and
`negpy/features/exposure/normalization.py`, adapted to this program's
architecture — colour negative only, on a Bayer sensor under white light
(see docs/DECISIONS.md, "Normalization decisions" for what is deliberately *not*
ported: no E-6 branch, no channel unmix, no user-facing controls).

The transfer (section 3.2), ported unchanged:

    D_log  = log10(clamp(I_linear, 1e-6, 1.0))          # to_log_density
    val    = (D_log - floor_ch) / (ceil_ch - floor_ch)  # normalize_log_image

Polarity, fixed for negative film: `floor` is the *low* log percentile
(dense film = scene highlight) and maps to `0.0`; `ceil` is the *high* log
percentile (thin film / base = scene shadow) and maps to `1.0`. The
published file remains, in appearance, a negative — inversion belongs to
the print stage.

Every constant of the feature is defined here and nowhere else
(section 3.3). The two headroom constants and the fill value make the
`uint16` encode reversible to within quantization; `decode_normalized` is
the single inverse, and everything downstream — previews, the edit stage,
export — goes through it, never through the file's ICC profile (section
3.12's rule).
"""

from __future__ import annotations

import dataclasses

import cv2
import numpy as np

from scanny_boy.events import Code

# --- section 3.3: the constants, ported verbatim from NegPy's
# EXPOSURE_CONSTANTS. Production code reads them from here and nowhere else.

# Side of the block-median prefilter grid.
ANALYSIS_GRID = 1024
# Per-tail percentile clip for the luma axis / the colour axis.
BASE_LUMA_CLIP = 0.01
BASE_COLOR_CLIP = 1.0
# Width, in percentile points, of the luma-extreme band the same-pixel
# colour refs read.
COLOR_BOUNDS_BAND_WIDTH = 4.0
# Lowest-chroma fraction of a band kept as the near-neutral set.
NEUTRAL_CHROMA_QUANTILE = 0.30
# Pass-2 median-chroma ceiling; above it, fall back.
NEUTRAL_CHROMA_CAP = 0.29
# Pass-1 ceiling: admits strong correctable casts, rejects saturated
# content.
NEUTRAL_FIRST_PASS_CAP = 0.55
# Minimum usable near-neutral set.
NEUTRAL_MIN_PIXELS = 64
# Per-channel shadow reference (metering).
SHADOW_NEUTRAL_PERCENTILE = 98.0
# Exposure anchor (metering).
ANCHOR_METER_PERCENTILE = 50.0
# P10-P90 textural range (metering).
TEXTURAL_RANGE_CLIP = 10.0
# Linear level treated as sensor-white clipping.
SCAN_CLIP_LEVEL = 0.99
# Per-channel clipped fraction that warns.
SCAN_CLIP_WARN = 0.01
# Rec.709 weights (NegPy's `domain/types`), applied to the log grid.
LUMA_R = 0.2126
LUMA_G = 0.7152
LUMA_B = 0.0722

# --- section 3.6: encoding with headroom ---

NORMALIZED_HEADROOM_LOW = 0.15  # dense end (scene highlights)
NORMALIZED_HEADROOM_HIGH = 0.10  # thin end (film base / scene shadows)
NORMALIZED_FILL = 1.0 + NORMALIZED_HEADROOM_HIGH  # section 3.14
NORMALIZE_FORMAT_VERSION = 1

# The fraction of pixels the headroom clips past which
# NORMALIZE_HEADROOM_CLIPPED warns (section 3.6's "the signal that the
# constants are too tight"). Provisional, unmeasured — recorded per
# negative either way (section 3.6's observed_min/observed_max).
HEADROOM_CLIP_WARN_FRACTION = 0.001

_DENSITY_FLOOR = 1e-6  # log10 clamp floor: a hair above -6.0 decades
_NORMALIZE_EPSILON = 1e-6  # NegPy's sign-preserving degenerate-solve guard


class NormalizationError(Exception):
    """A degenerate normalization solve (section 3.4's degenerate-bounds
    guard). Maps to `NORMALIZE_DEGENERATE_BOUNDS`."""

    def __init__(
        self, message: str, code: Code = Code.NORMALIZE_DEGENERATE_BOUNDS
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclasses.dataclass(frozen=True)
class Bounds:
    """Per-channel (R, G, B) log-density bounds of one negative's
    composite: `floors` are the dense ends (scene highlights), `ceils` the
    thin ends (film base / scene shadows)."""

    floors: tuple[float, float, float]
    ceils: tuple[float, float, float]


@dataclasses.dataclass(frozen=True)
class Rebate:
    """The rebate detector's finding for one negative (section 3.13).
    `base_density` is the RAW per-channel median log density inside the
    rebate mask — no exposure-time correction applied; that belongs to the
    consumer. `None` when the base is sensor-clipped (clipped base is
    worthless base) or nothing was detected."""

    detected: bool
    mask_fraction: float
    base_density: tuple[float, float, float] | None
    clipped: bool


# --- section 3.2: the transfer ---------------------------------------------


def to_log_density(linear: np.ndarray) -> np.ndarray:
    """Linear light (float, [0, 1]) -> log10 density, clamped.

    `np.fmin` / `np.fmax` rather than `np.clip`, exactly as NegPy has it:
    they drop NaN in favour of the bound, so one clamp covers the NaN and
    infinity fixup too — NaN and -inf land on `_DENSITY_FLOOR`, +inf on
    1.0. Returns float32.
    """
    clamped = np.fmin(
        np.fmax(np.asarray(linear, dtype=np.float32), _DENSITY_FLOOR), 1.0
    )
    return np.log10(clamped)


def luma_of_log(img_log: np.ndarray) -> np.ndarray:
    """Rec.709-weighted luma of a log-density image. Log values are
    negative; **thinner is larger**."""
    return (
        LUMA_R * img_log[..., 0] + LUMA_G * img_log[..., 1] + LUMA_B * img_log[..., 2]
    ).astype(np.float32)


# --- section 3.5: the block-median prefilter --------------------------------


def analysis_grid_block_sizes(image_shape: tuple[int, ...]) -> tuple[int, int]:
    """The (row, column) block sizes `block_median_grid` uses for an image
    of `image_shape` — the single place the downscale rule lives, so a
    caller can map canvas coordinates onto grid cells without guessing.

    The blocks are square, `b = ceil(max(h, w) / ANALYSIS_GRID)` on a side:
    a b x b median is what makes a single hot pixel vanish for any b >= 2
    (a 2x1 median is just the mean of two values and would only halve it),
    and the resulting grid never exceeds `ANALYSIS_GRID` on a side.
    """
    height, width = int(image_shape[0]), int(image_shape[1])
    block = max(1, -(-max(height, width) // ANALYSIS_GRID))
    return block, block


def block_median_grid(img_log: np.ndarray) -> np.ndarray:
    """Reduce the analysis image to an `ANALYSIS_GRID`-bounded side by
    taking the median of each b x b block (section 3.5).

    Isolated extremes — speculars, dust pinholes, a scratch — vanish
    inside their block's median, so the extreme percentiles are robust
    without clipping the histogram hard; and the statistics become nearly
    resolution-invariant, which matters because a canvas size varies with
    how much the frames overlap.

    Images at or below the grid side pass through unchanged. Edge blocks
    are padded by replicating the image edge, so the median of a partial
    block stays representative. Single-threaded, per section 3.5: the
    composite accumulator is deliberately single-threaded and a 1024-grid
    median on a stitched canvas is milliseconds.
    """
    height, width = img_log.shape[0], img_log.shape[1]
    if max(height, width) <= ANALYSIS_GRID:
        return np.asarray(img_log, dtype=np.float32)

    block_rows, block_cols = analysis_grid_block_sizes(img_log.shape)
    grid_rows = -(-height // block_rows)
    grid_cols = -(-width // block_cols)
    pad_rows = grid_rows * block_rows - height
    pad_cols = grid_cols * block_cols - width

    padded = np.pad(
        img_log,
        ((0, pad_rows), (0, pad_cols), *(((0, 0),) * (img_log.ndim - 2))),
        mode="edge",
    )
    blocks = padded.reshape(
        grid_rows, block_rows, grid_cols, block_cols, *img_log.shape[2:]
    )
    return np.median(blocks, axis=(1, 3)).astype(np.float32)


# --- section 3.13: the analysis region ---------------------------------------

ANALYSIS_INSET = 0.0  # section 3.13's fallback; the detector does the work


def resolve_analysis_region(
    grid_shape: tuple[int, int],
    valid_rect: tuple[int, int, int, int] | None = None,
    crop_roi: tuple[int, int, int, int] | None = None,
) -> np.ndarray:
    """The analysis region as a flat boolean over the prefiltered grid
    (section 3.13). Resolution order, first hit wins:

        explicit crop ROI      (does not exist yet -- the crop tool
                                attaches here; grid-cell coordinates)
          ?? valid rect
          ?? the whole grid    (when no rect is known)

    `valid_rect`/`crop_roi` are `(x, y, width, height)` in *grid-cell*
    coordinates; `composite()` maps its canvas-space rect through
    `analysis_grid_block_sizes`. The region restricts the meters only —
    it never crops the output. `ANALYSIS_INSET` is pinned at 0.0: the
    section's fallback inset is shut off, because the rebate detector does
    the work.
    """
    grid_rows, grid_cols = grid_shape
    keep = np.zeros((grid_rows, grid_cols), dtype=bool)
    rect = crop_roi if crop_roi is not None else valid_rect
    if rect is None:
        keep[:] = True
        return keep
    inset_cells = round(ANALYSIS_INSET)  # no-op while pinned at 0.0
    x, y, width, height = (int(v) for v in rect)
    x0 = max(0, x + inset_cells)
    y0 = max(0, y + inset_cells)
    x1 = min(grid_cols, x + width - inset_cells)
    y1 = min(grid_rows, y + height - inset_cells)
    if x1 <= x0 or y1 <= y0:
        # A degenerate rect leaves nothing to meter on; the whole grid is
        # the safer read than raising on a stitched canvas that merely
        # rounds small.
        keep[:] = True
        return keep
    keep[y0:y1, x0:x1] = True
    return keep


# --- section 3.4: the meters, two axes recombined ----------------------------


def _percentile(values: np.ndarray, q: float) -> float:
    return float(np.percentile(values, q))


def _same_pixel_color_floor_refs(
    g_flat: np.ndarray, keep_flat: np.ndarray, lum_flat: np.ndarray, base: np.ndarray
) -> list[float] | None:
    """The dense-end colour references: one shared, chroma-gated pixel set
    drawn from the luma-extreme band, chroma measured base-anchored, with a
    two-pass provisional refinement (section 3.4).

    Independent per-channel percentiles at the dense end read a *different
    scene object per channel*, so coloured highlight content masquerades
    as film cast; a shared set cannot. Returns `None` — the caller falls
    back to plain percentiles — when the band holds no trustworthy
    neutrals.

    Pass 1 selects the band's near-neutral cells with the loose ceiling
    (`NEUTRAL_FIRST_PASS_CAP`, which admits strong correctable casts) and
    takes their median as a provisional anchor; pass 2 re-measures chroma
    against that anchor with the tight ceiling (`NEUTRAL_CHROMA_CAP`),
    rejecting saturated content pass 1 admitted. `NEUTRAL_CHROMA_CAP`'s
    median-chroma ceiling gates the whole two-pass result: if the
    surviving set's own median chroma exceeds it, the band held no
    trustworthy neutrals and the fallback wins.
    """
    band_limit = _percentile(
        lum_flat[keep_flat], BASE_COLOR_CLIP + COLOR_BOUNDS_BAND_WIDTH
    )
    band = keep_flat & (lum_flat <= band_limit)
    if int(np.count_nonzero(band)) < NEUTRAL_MIN_PIXELS:
        return None

    anchored = g_flat - base[np.newaxis, :]
    chroma = anchored.max(axis=1) - anchored.min(axis=1)
    # Pass 1: the loose ceiling admits strong correctable casts, and the
    # lowest-chroma `NEUTRAL_CHROMA_QUANTILE` fraction of the band is the
    # near-neutral set it aims at.
    quantile_limit = float(np.quantile(chroma[band], NEUTRAL_CHROMA_QUANTILE))
    first = band & (chroma <= NEUTRAL_FIRST_PASS_CAP) & (chroma <= quantile_limit)
    if int(np.count_nonzero(first)) < NEUTRAL_MIN_PIXELS:
        first = band & (chroma <= NEUTRAL_FIRST_PASS_CAP)
    if int(np.count_nonzero(first)) < NEUTRAL_MIN_PIXELS:
        return None

    provisional = np.median(g_flat[first], axis=0)
    refined_anchored = g_flat - provisional[np.newaxis, :]
    refined_chroma = refined_anchored.max(axis=1) - refined_anchored.min(axis=1)
    second = first & (refined_chroma <= NEUTRAL_CHROMA_CAP)
    if int(np.count_nonzero(second)) >= NEUTRAL_MIN_PIXELS:
        final = second
    else:
        final = first

    if _percentile(refined_chroma[final], 50.0) > NEUTRAL_CHROMA_CAP:
        return None

    return [
        _percentile(g_flat[final, channel], BASE_COLOR_CLIP) for channel in range(3)
    ]


def analyze_bounds(grid_log: np.ndarray, keep: np.ndarray) -> Bounds:
    """The bounds meters, ported from NegPy's
    `analyze_log_exposure_bounds_from_log` (section 3.4).

    Bounds are sampled on **two independent axes** and recombined:

    - the **luma axis** at `BASE_LUMA_CLIP`, fixing the floor/ceil *mean* —
      black point, white point, dynamic range;
    - the **colour axis** at `BASE_COLOR_CLIP`, fixing each channel's
      *deviation from that mean* — white balance and the orange mask.

    Recombination keeps NegPy's asymmetry — **mean** on the luma axis,
    **median** on the colour axis — because the median makes the colour
    recentre robust to one channel being pulled by a strong single-channel
    cast. The two ends are sampled differently: plain per-channel
    percentiles at the thin end (physically anchored: density on real film
    is bounded below by base), the shared chroma-gated pixel set at the
    dense end (independent percentiles read a different scene object per
    channel and mistake coloured highlights for film cast).

    Identical channels (a mono negative) give zero deviation at any clip.
    """
    g_flat = grid_log.reshape(-1, 3)
    keep_flat = keep.reshape(-1)
    if not keep_flat.any():
        raise NormalizationError("the analysis region is empty; nothing to meter")

    lum_full = luma_of_log(grid_log).reshape(-1)
    values = g_flat[keep_flat]
    lum = lum_full[keep_flat]

    # Luma pass: one percentile pair on the weighted luma; the per-channel
    # floors/ceils it contributes are that pair, so their mean is the pair.
    mean_lf = _percentile(lum, BASE_LUMA_CLIP)
    mean_lc = _percentile(lum, 100.0 - BASE_LUMA_CLIP)

    # Colour pass. Thin end: plain per-channel percentiles — physically
    # anchored at film base.
    c_ceils = [
        _percentile(values[:, channel], 100.0 - BASE_COLOR_CLIP) for channel in range(3)
    ]
    # Dense end: the shared, chroma-gated pixel set, falling back to plain
    # per-channel percentiles when the band holds no trustworthy neutrals.
    base = np.asarray(c_ceils, dtype=np.float64)
    c_floors = _same_pixel_color_floor_refs(g_flat, keep_flat, lum_full, base)
    if c_floors is None:
        c_floors = [
            _percentile(values[:, channel], BASE_COLOR_CLIP) for channel in range(3)
        ]

    mean_cf = sorted(c_floors)[1]
    mean_cc = sorted(c_ceils)[1]
    floors = tuple(mean_lf + (c_floors[channel] - mean_cf) for channel in range(3))
    ceils = tuple(mean_lc + (c_ceils[channel] - mean_cc) for channel in range(3))

    for channel in range(3):
        if not np.isfinite(floors[channel]) or not np.isfinite(ceils[channel]):
            raise NormalizationError(
                "bounds analysis produced a non-finite channel bound"
            )
        if ceils[channel] <= floors[channel]:
            raise NormalizationError(
                f"bounds analysis produced a degenerate channel {channel}: "
                f"ceil {ceils[channel]:.6f} <= floor {floors[channel]:.6f}"
            )
    return Bounds(floors=floors, ceils=ceils)


# --- section 3.7: metering, recorded, never acted on -------------------------


def measure_shadow_refs(
    grid_log: np.ndarray, keep: np.ndarray
) -> tuple[float, float, float]:
    """Per-channel shadow references: the `SHADOW_NEUTRAL_PERCENTILE`
    percentile of each channel over the analysis region. Recorded for the
    print stage; nothing in this plan reads them back (section 3.7)."""
    values = grid_log.reshape(-1, 3)[keep.reshape(-1)]
    return tuple(
        _percentile(values[:, ch], SHADOW_NEUTRAL_PERCENTILE) for ch in range(3)
    )


def measure_anchor(grid_log: np.ndarray, keep: np.ndarray) -> float:
    """The exposure anchor: the `ANCHOR_METER_PERCENTILE` percentile of the
    log luma over the analysis region."""
    lum = luma_of_log(grid_log).reshape(-1)[keep.reshape(-1)]
    return _percentile(lum, ANCHOR_METER_PERCENTILE)


def measure_textural_range(grid_log: np.ndarray, keep: np.ndarray) -> float:
    """The textural range: the P90-P10 spread of the log luma over the
    analysis region — `TEXTURAL_RANGE_CLIP` percentile points clipped from
    each tail."""
    lum = luma_of_log(grid_log).reshape(-1)[keep.reshape(-1)]
    p90 = _percentile(lum, 100.0 - TEXTURAL_RANGE_CLIP)
    p10 = _percentile(lum, TEXTURAL_RANGE_CLIP)
    return p90 - p10


def measure_clip_fractions(linear: np.ndarray) -> tuple[float, float, float]:
    """Per-channel fraction of pixels at or above `SCAN_CLIP_LEVEL` —
    sensor-white clipping. `linear` is uint16 codes or float linear light;
    clipping is a property of the capture, so this runs in the prepare
    stage, per frame, before flat-field touches the pixels (section N-4)."""
    values = np.asarray(linear)
    if values.dtype == np.uint16:
        values = values.astype(np.float32) / 65535.0
    else:
        values = values.astype(np.float32)
    return tuple(float(np.mean(values[..., ch] >= SCAN_CLIP_LEVEL)) for ch in range(3))


# --- section 3.13: the rebate detector ---------------------------------------

# All five provisional and unmeasured — the same status
# `MIN_GAIN_OVERLAP_PX` and `GAIN_DRIFT_WARN` carry; they go on the
# punchlist together (docs/DECISIONS.md, "Normalization decisions").

# Robust thin-end anchor for the candidate band.
REBATE_ANCHOR_PERCENTILE = 99.9
# Log10 D below that anchor a cell may sit.
REBATE_DENSITY_TOLERANCE = 0.10
# Of the region; smaller is not rebate.
REBATE_MIN_AREA_FRACTION = 0.02
# Log10 D, P90-P10 within a component: base is featureless.
REBATE_MAX_SPREAD = 0.05
# Log10 D between the component and the scene.
REBATE_MIN_SEPARATION = 0.08


def _region_border(keep: np.ndarray) -> np.ndarray:
    """The cells of `keep` that touch the analysis region's edge: a keep
    cell with any non-keep (or out-of-grid) neighbour."""
    padded = np.pad(keep.astype(np.uint8), 1, mode="constant", constant_values=0)
    eroded = cv2.erode(padded, np.ones((3, 3), np.uint8))
    interior = eroded[1:-1, 1:-1].astype(bool)
    return keep & ~interior


def detect_rebate(grid_log: np.ndarray, keep: np.ndarray) -> tuple[np.ndarray, Rebate]:
    """Detect the film rebate / clear base among the region's thinnest
    cells and exclude it from `keep` (section 3.13).

    On a **negative**, base is strictly the thinnest thing on the film —
    no scene content can be thinner than unexposed film — which gives a
    density discriminator that does not depend on geometry. The detector
    operates on the block-median grid, which is already computed,
    already dust-free and already resolution-invariant:

    1. candidates are cells within `REBATE_DENSITY_TOLERANCE` of the
       region's `REBATE_ANCHOR_PERCENTILE` thin-end luma anchor;
    2. connected components of the candidate mask survive only when they
       touch the region border;
    3. each survivor is gated on area, flatness (base is featureless),
       and separation — separation is the gate that makes "no rebate at
       all" return cleanly, since with no distinct base population the
       thinnest cells are not separated from the scene distribution;
    4. survivors' union is the rebate mask, removed from `keep`;
    5. `base_density` is the per-channel median log inside the mask, or
       `None` with `clipped=True` when the grid's linear estimate inside
       the mask clips past `SCAN_CLIP_WARN` — clipped base is worthless
       base (section 3.13's first gotcha), but the cells are excluded
       either way.

    The known false positive: a genuinely deep, featureless shadow
    touching the region border can pass flatness and connectivity. When
    it fires wrongly the failure is mild — some real shadow is withheld
    from the meters and the ceiling compresses slightly. It never invents
    data, and a blown highlight cannot trigger it at all: on a negative a
    highlight is dense, at the opposite end.
    """
    empty = Rebate(detected=False, mask_fraction=0.0, base_density=None, clipped=False)
    if not keep.any():
        return keep, empty

    lum = luma_of_log(grid_log)
    region_cells = int(np.count_nonzero(keep))
    anchor = _percentile(lum[keep], REBATE_ANCHOR_PERCENTILE)
    candidates = keep & (lum >= anchor - REBATE_DENSITY_TOLERANCE)
    if not candidates.any():
        return keep, empty

    count, labels, _stats, _centroids = cv2.connectedComponentsWithStats(
        candidates.astype(np.uint8), connectivity=8
    )
    border = _region_border(keep)

    # Gate 1+2, per component: border connectivity, area, and flatness.
    # Flatness is measured on the component's cells in the lower half of
    # the candidate band — base is featureless, and a *thinner* excursion
    # inside the component (sprocket holes are film-free, therefore thinner
    # than base and able to pull the anchor up) is a population to exclude
    # with the base, not evidence against it. When the component's own
    # density sits above that half-band (the usual no-hole case), the full
    # component is measured instead. The separation gate is evaluated
    # afterwards against the union of the flat components — a second rebate
    # edge on the opposite side of the frame is exactly as flat and as thin
    # as the first, and evaluating separation per component would let each
    # strip hide behind the other.
    flat_components: list[np.ndarray] = []
    for label in range(1, count):
        component = labels == label
        if not (component & border).any():
            continue
        if int(np.count_nonzero(component)) < REBATE_MIN_AREA_FRACTION * region_cells:
            continue
        half_band = (
            component
            & (lum >= anchor - REBATE_DENSITY_TOLERANCE)
            & (lum <= anchor - REBATE_DENSITY_TOLERANCE / 2.0)
        )
        vals = lum[half_band] if half_band.any() else lum[component]
        spread = _percentile(vals, 90.0) - _percentile(vals, 10.0)
        if spread > REBATE_MAX_SPREAD:
            continue
        flat_components.append(component)

    if not flat_components:
        return keep, empty

    flat_union = np.zeros(keep.shape, dtype=bool)
    for component in flat_components:
        flat_union |= component

    # Gate 3, per component against the union: separation — the component's
    # median must be at least REBATE_MIN_SEPARATION thinner than the P99 of
    # everything outside the flat union. This is the gate that makes "no
    # rebate at all" return cleanly: with no distinct base population the
    # thinnest cells are not separated from the scene distribution and
    # nothing fires.
    mask = np.zeros(keep.shape, dtype=bool)
    outside = keep & ~flat_union
    for component in flat_components:
        # No outside left to separate from (the flat union is the whole
        # region): there is no scene population to be thin *relative to*,
        # so this cannot be established as rebate.
        if not outside.any():
            continue
        if (
            _percentile(lum[component], 50.0)
            < _percentile(lum[outside], 99.0) + REBATE_MIN_SEPARATION
        ):
            continue
        mask |= component

    if not mask.any():
        return keep, empty

    new_keep = keep & ~mask
    base_density = tuple(
        float(np.median(grid_log[..., channel][mask])) for channel in range(3)
    )
    grid_linear = np.power(10.0, grid_log.astype(np.float64))
    clipped = any(
        float(np.mean(grid_linear[..., channel][mask] >= SCAN_CLIP_LEVEL))
        > SCAN_CLIP_WARN
        for channel in range(3)
    )
    rebate = Rebate(
        detected=True,
        mask_fraction=float(np.count_nonzero(mask)) / region_cells,
        base_density=None if clipped else base_density,
        clipped=clipped,
    )
    return new_keep, rebate


# --- section 3.2 / 3.6: normalize, encode, decode -----------------------------


def normalize_log_image(img_log: np.ndarray, bounds: Bounds) -> np.ndarray:
    """Per-channel affine stretch of log density into normalized values
    (section 3.2): `floor -> 0.0`, `ceil -> 1.0`, **unclamped outside** —
    NegPy deliberately does not clamp; tones outside the detected bounds
    are kept for the encode's headroom and the print curve's soft toe and
    shoulder. A degenerate `ceil == floor` channel divides by NegPy's
    sign-preserving `epsilon = 1e-6` instead of zero."""
    floors = np.asarray(bounds.floors, dtype=np.float32)
    ceils = np.asarray(bounds.ceils, dtype=np.float32)
    span = ceils - floors
    safe_span = np.where(
        np.abs(span) < _NORMALIZE_EPSILON,
        np.copysign(np.float32(_NORMALIZE_EPSILON), span + (span == 0)),
        span,
    )
    return (img_log - floors) / safe_span


def observed_extrema(
    normalized: np.ndarray,
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """The per-channel minimum and maximum normalized value of the input
    (section 3.6): recorded per negative so the two headroom constants can
    be tuned from real scans instead of estimated — `observed_min` pinned
    at `-NORMALIZED_HEADROOM_LOW` means the headroom is clipping and
    tones have been lost."""
    flat = normalized.reshape(-1, 3)
    mins = tuple(float(flat[:, ch].min()) for ch in range(3))
    maxs = tuple(float(flat[:, ch].max()) for ch in range(3))
    return mins, maxs


def headroom_clip_fractions(
    normalized: np.ndarray,
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Per-channel fraction of normalized values the encode's headroom
    clips (section 3.6), split by which rail they clip against: below
    `-NORMALIZED_HEADROOM_LOW` is the dense end (scene highlights), above
    `1.0 + NORMALIZED_HEADROOM_HIGH` is the thin end (scene shadows) — see
    the constants' own comments. Returns `(highlights, shadows)`."""
    low = -NORMALIZED_HEADROOM_LOW
    high = 1.0 + NORMALIZED_HEADROOM_HIGH
    flat = normalized.reshape(-1, 3)
    highlights = tuple(float(np.mean(flat[:, ch] < low)) for ch in range(3))
    shadows = tuple(float(np.mean(flat[:, ch] > high)) for ch in range(3))
    return highlights, shadows


def encode_normalized(normalized: np.ndarray) -> np.ndarray:
    """Normalized values -> uint16 codes, reserving the asymmetric headroom
    (section 3.6):

        code = rint(clip((val + LOW) / span, 0, 1) * 65535)
        span = 1.0 + NORMALIZED_HEADROOM_LOW + NORMALIZED_HEADROOM_HIGH

    Values at `-NORMALIZED_HEADROOM_LOW` and `1 + NORMALIZED_HEADROOM_HIGH`
    survive; beyond them they clip — documented, not accidental: those are
    exactly the speculars and deepest shadows the creative-edit stage's
    tone curve will want, and the headroom keeps them representable. NaN
    lands on the low rail (the dense end), never mid-scale.
    """
    span = 1.0 + NORMALIZED_HEADROOM_LOW + NORMALIZED_HEADROOM_HIGH
    values = np.asarray(normalized, dtype=np.float32)
    values = np.fmin(
        np.fmax(values, np.float32(-NORMALIZED_HEADROOM_LOW)),
        np.float32(1.0 + NORMALIZED_HEADROOM_HIGH),
    )
    codes = np.rint((values + NORMALIZED_HEADROOM_LOW) / span * 65535.0)
    return codes.astype(np.uint16)


def decode_normalized(codes: np.ndarray) -> np.ndarray:
    """The single inverse of `encode_normalized` (section 3.6): uint16
    codes -> normalized float32. Everything downstream — previews, the
    edit stage, export — decodes through this, never through the file's
    ICC profile (section 3.12's rule)."""
    span = 1.0 + NORMALIZED_HEADROOM_LOW + NORMALIZED_HEADROOM_HIGH
    return (
        np.asarray(codes, dtype=np.float32) / 65535.0 * span - NORMALIZED_HEADROOM_LOW
    )


def build_params() -> dict:
    """Every constant of the feature plus a `format_version`, folded into
    `processing_params` under the key `normalize` — a roll invariant
    (section 3.8). A file written by any build is interpretable through
    this record and `decode_normalized`."""
    return {
        "format_version": NORMALIZE_FORMAT_VERSION,
        "analysis_grid": ANALYSIS_GRID,
        "base_luma_clip": BASE_LUMA_CLIP,
        "base_color_clip": BASE_COLOR_CLIP,
        "color_bounds_band_width": COLOR_BOUNDS_BAND_WIDTH,
        "neutral_chroma_quantile": NEUTRAL_CHROMA_QUANTILE,
        "neutral_chroma_cap": NEUTRAL_CHROMA_CAP,
        "neutral_first_pass_cap": NEUTRAL_FIRST_PASS_CAP,
        "neutral_min_pixels": NEUTRAL_MIN_PIXELS,
        "shadow_neutral_percentile": SHADOW_NEUTRAL_PERCENTILE,
        "anchor_meter_percentile": ANCHOR_METER_PERCENTILE,
        "textural_range_clip": TEXTURAL_RANGE_CLIP,
        "scan_clip_level": SCAN_CLIP_LEVEL,
        "scan_clip_warn": SCAN_CLIP_WARN,
        "normalized_headroom_low": NORMALIZED_HEADROOM_LOW,
        "normalized_headroom_high": NORMALIZED_HEADROOM_HIGH,
        "normalized_fill": NORMALIZED_FILL,
    }
