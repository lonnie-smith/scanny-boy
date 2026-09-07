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
import enum

import cv2
import numpy as np

from scanny_boy.events import Code

# --- section 3.3: the constants, ported verbatim from NegPy's
# EXPOSURE_CONSTANTS. Production code reads them from here and nowhere else.

# Side of the block-median prefilter's cell, in *source-frame* pixels.
#
# Pinned, deliberately, rather than derived from the canvas. The rule this
# replaced was `b = ceil(max(h, w) / 1024)`, which bounded the grid's long
# side and so tied the cell to the canvas's *aspect ratio*: at the target
# grid workload (docs/GRID_STITCH_PLAN.md section 7.1) one 6000x4000 frame
# gives b = 6 while a 5x2's 22000x6667 canvas gives b = 22, a 13x larger
# cell over a grid holding 2.2x *fewer* samples. Measured on one frame
# tiled to each canvas size, so the film content per unit area is
# identical, the meters drifted monotonically with grid shape: the floor
# lifted 0.049 log10 D and the span contracted 0.057 (3.8%) from 1x1 to
# 5x2 — about 0.19 stop of black point, on the same negative. Nothing
# caught it: `CLAMP_MIN_WINDOW` is 0.5 log10 D, nine times too coarse.
#
# A pinned cell removes the variable instead of making it a function of
# the grid, which is what every consumer actually wants — the meters, both
# border detectors and the neutral residual's 3x3 neighbourhood are all
# statements about a physical scale on film. It also makes
# `film_base`'s measurement, which always runs on a single frame, use
# literally the same reduction as the per-negative path, which is what
# docs/REBATE_ANCHORING.md section 2.2 already claims.
#
# 6 source pixels is the value the single-frame case had all along: at the
# reference rig (24MP over a 36x24mm patch, 166.7 px/mm) it is 36 um on
# film. It is a per-rig constant, not a physical one — a different capture
# magnification rescales it — but it is uniform across every negative and
# every grid shape on a roll, which is the invariant the meters need.
#
# It costs nothing. On a 22000x6667 canvas the reduction takes 6.9 s at
# b = 6 against 7.5 s at b = 22 (the cost is the whole-canvas copy either
# way), and the grid it produces grows from 3.5 MiB to 47 MiB against a
# 23.8 GB estimated peak.
ANALYSIS_BLOCK_PX = 6
# An image whose long side is at or below this is already at analysis
# resolution and passes through unreduced — reducing it further would
# throw away the samples the meters need. Production never approaches it
# (the smallest canvas is one 6000 px frame), so the step from block 1 to
# block `ANALYSIS_BLOCK_PX` at the boundary is a property of synthetic
# inputs alone. `analysis_grid_block_sizes` reports block 1 below the
# threshold so a caller mapping canvas coordinates onto cells stays
# consistent with what `block_median_grid` actually did.
ANALYSIS_PASSTHROUGH_PX = 1024
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
# MONOCHROME_PLAN section 5.1: v1 predates the mono feature. §1's detector
# constants stay out of `build_params()` (they shape recorded evidence,
# never published output); §2's thresholds and §3's merge weights join in
# their own steps, and `upgrade_normalize_params` absorbs the invariant
# break each addition would otherwise cause.
# CAST_REMOVAL_PLAN R-1: v2 predates the two meters; the constants join
# `build_params()` because the residual the auto solve reads is recorded
# per negative against them.
# v3 predates the pinned analysis cell (`ANALYSIS_BLOCK_PX`) and the two
# region gates it let become absolute. This is the first bump where the
# meters' *arithmetic* moved, not just the recorded constant set: a v3
# roll re-stitched under v4 gets different bounds on any canvas that is
# not one frame — that is the point of the change, and the version is what
# says an old recorded value is not comparable with a fresh one.
NORMALIZE_FORMAT_VERSION = 4

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
    """Per-channel log-density bounds of one negative's composite:
    `floors` are the dense ends (scene highlights), `ceils` the thin ends
    (film base / scene shadows). One entry per published channel — three
    (R, G, B) for a colour roll, one merged channel on a mono roll
    (MONOCHROME_PLAN §4)."""

    floors: tuple[float, ...]
    ceils: tuple[float, ...]


@dataclasses.dataclass(frozen=True)
class Rebate:
    """The rebate detector's finding for one negative (section 3.13).
    `base_density` is the RAW per-channel median log density inside the
    rebate mask — no exposure-time correction applied; that belongs to the
    consumer. `None` when the base is sensor-clipped (clipped base is
    worthless base) or nothing was detected."""

    detected: bool
    mask_fraction: float
    base_density: tuple[float, ...] | None
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
    negative; **thinner is larger**. On a single channel (a mono roll's
    collapsed image, MONOCHROME_PLAN §4) there is no weighting to do and
    the channel is its own luma."""
    if img_log.shape[-1] == 1:
        return np.asarray(img_log[..., 0], dtype=np.float32)
    return (
        LUMA_R * img_log[..., 0] + LUMA_G * img_log[..., 1] + LUMA_B * img_log[..., 2]
    ).astype(np.float32)


# --- section 3.5: the block-median prefilter --------------------------------


def analysis_grid_block_sizes(image_shape: tuple[int, ...]) -> tuple[int, int]:
    """The (row, column) block sizes `block_median_grid` uses for an image
    of `image_shape` — the single place the downscale rule lives, so a
    caller can map canvas coordinates onto grid cells without guessing.

    The blocks are square and `ANALYSIS_BLOCK_PX` on a side, *whatever the
    image's size or shape* — a b x b median is what makes a single hot
    pixel vanish for any b >= 2 (a 2x1 median is just the mean of two
    values and would only halve it), and pinning b is what keeps a cell
    the same piece of film on a single frame and on a 5x2 grid's canvas
    alike. The grid's dimensions, not its cell, are what grow with the
    canvas.

    Below `ANALYSIS_PASSTHROUGH_PX` the reduction does not run, and the
    reported block is 1 to match.
    """
    height, width = int(image_shape[0]), int(image_shape[1])
    if max(height, width) <= ANALYSIS_PASSTHROUGH_PX:
        return 1, 1
    return ANALYSIS_BLOCK_PX, ANALYSIS_BLOCK_PX


def block_median_grid(img_log: np.ndarray) -> np.ndarray:
    """Reduce the analysis image by taking the median of each b x b block,
    `b = ANALYSIS_BLOCK_PX` (section 3.5).

    Isolated extremes — speculars, dust pinholes, a scratch — vanish
    inside their block's median, so the extreme percentiles are robust
    without clipping the histogram hard; and the statistics become
    shape-invariant, which matters because a negative's canvas is one
    frame, a strip, or an R x C grid, and the older long-side-bounded rule
    made the *cell* vary with that choice (see `ANALYSIS_BLOCK_PX`).

    Images at or below `ANALYSIS_PASSTHROUGH_PX` pass through unchanged.
    Edge blocks are padded by replicating the image edge, so the median of
    a partial block stays representative. Single-threaded, per section
    3.5: the composite accumulator is deliberately single-threaded, and
    the reduction's cost is the whole-canvas copy rather than the block
    size — 6.9 s on the largest canvas in scope, against 7.5 s for the
    coarser rule it replaced.
    """
    height, width = img_log.shape[0], img_log.shape[1]
    if max(height, width) <= ANALYSIS_PASSTHROUGH_PX:
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
        _percentile(g_flat[final, channel], BASE_COLOR_CLIP)
        for channel in range(g_flat.shape[-1])
    ]


def _thin_end_refs(
    values: np.ndarray,
    channels: int,
    base_refs: tuple[float, ...] | None = None,
) -> list[float]:
    """The thin-end per-channel colour references: the roll's measured film
    base when it has one (REBATE_ANCHORING §4), else plain per-channel
    percentiles of scene content. Lifted out of `analyze_bounds` (whose
    fallback branch and `len(base_refs) == channels` guard are that plan's)
    so `measure_highlight_refs` can measure the dense end against the same
    physically anchored thin end the published pixels use
    (docs/CAST_REMOVAL_PLAN.md §0.6 point 3).

    With `base_refs=None` — and on a mono roll, where a 3-array cannot
    match a 1-channel image — this is exactly what `analyze_bounds` computed
    before the extraction."""
    if base_refs is not None and len(base_refs) == channels:
        return [float(v) for v in base_refs]
    return [
        _percentile(values[:, channel], 100.0 - BASE_COLOR_CLIP)
        for channel in range(channels)
    ]


def analyze_bounds(
    grid_log: np.ndarray,
    keep: np.ndarray,
    base_refs: tuple[float, ...] | None = None,
) -> Bounds:
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

    On a **single channel** — the collapsed image of a mono roll — the two
    axes degenerate correctly and by construction: the luma percentile pair
    *is* the channel's percentile pair, `c_floors[0] == mean_cf`, so the
    colour deviation vanishes and `floors`/`ceils` are the luma percentile
    pair unchanged. That is the collapse's point (MONOCHROME_PLAN §0.2),
    not a special case: with no colour there is no colour deviation to add
    back, and the generalised arithmetic below reaches it on its own.
    """
    channels = grid_log.shape[-1]
    g_flat = grid_log.reshape(-1, channels)
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

    # Colour pass. Thin end: the roll's measured film base when it has one
    # (docs/REBATE_ANCHORING.md §4), falling back to plain per-channel
    # percentiles of scene content. Only the deviation from the median
    # survives the recombination below, so the base frame's own exposure
    # cancels and never has to match the roll's (§0.2 of that plan).
    c_ceils = _thin_end_refs(values, channels, base_refs)
    # Dense end: the shared, chroma-gated pixel set, falling back to plain
    # per-channel percentiles when the band holds no trustworthy neutrals.
    base = np.asarray(c_ceils, dtype=np.float64)
    c_floors = _same_pixel_color_floor_refs(g_flat, keep_flat, lum_full, base)
    if c_floors is None:
        c_floors = [
            _percentile(values[:, channel], BASE_COLOR_CLIP)
            for channel in range(channels)
        ]

    # The median per-channel reference, generalising NegPy's
    # `sorted(...)[1]` (the middle of three) to any channel count.
    mean_cf = float(np.median(c_floors))
    mean_cc = float(np.median(c_ceils))
    floors = tuple(mean_lf + (c_floors[channel] - mean_cf) for channel in range(channels))
    ceils = tuple(mean_lc + (c_ceils[channel] - mean_cc) for channel in range(channels))

    for channel in range(channels):
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
) -> tuple[float, ...]:
    """Per-channel shadow references: the `SHADOW_NEUTRAL_PERCENTILE`
    percentile of each channel over the analysis region. Recorded for the
    print stage; nothing in this plan reads them back (section 3.7)."""
    channels = grid_log.shape[-1]
    values = grid_log.reshape(-1, channels)[keep.reshape(-1)]
    return tuple(
        _percentile(values[:, ch], SHADOW_NEUTRAL_PERCENTILE) for ch in range(channels)
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


# --- MONOCHROME_PLAN: film kind and channel collapse -------------------------


class FilmKind(enum.StrEnum):
    """A roll's film kind, set at `roll init` and never per-negative
    (§0.3). A plain `str` subclass: it serializes into the roll manifest's
    `film.kind` and `icc_profile.published_profile_kind`'s comparison
    unchanged."""

    COLOUR = "colour"
    MONOCHROME = "monochrome"


# --- MONOCHROME_PLAN section 3: the collapse ----------------------------------

# Three noisy measurements of one physical quantity — silver density — not
# a colorimetry problem. The minimum-variance estimator weights by
# 1/sigma^2; a Bayer CFA has twice as many green sites, so green carries
# about twice the photons and needs no interpolation at half its
# positions. Two honest caveats: demosaicing correlates the channels (R
# and B at a green site are partly interpolated *from* green), so the real
# gain over green-only is closer to sqrt(1.5) than sqrt(2); and R/B have
# worse post-demosaic MTF, so weighting them in costs a little sharpness.
# Green-only (0, 1, 0) is a defensible fallback if a measurement ever says
# so — measuring the real per-channel sigma from the flat-field
# calibration frames is out of scope here (§8/docs/punchlist.md).
#
# Deliberately not Rec.709 luma: those coefficients model the eye's
# response to display primaries, a photometric weighting for scene
# brightness — the right job for `luma_of_log`'s bounds axis, the wrong
# job here, where the "signal" is silver density, not perceived
# brightness.
MONO_MERGE_WEIGHTS = (0.25, 0.50, 0.25)


def collapse_to_mono(img_log: np.ndarray, covered: np.ndarray) -> np.ndarray:
    """§3.1: merge a colour composite's three log-density channels into
    one, for a roll whose frozen `film.kind` is monochrome.

    An offset-aligned, inverse-variance-weighted mean in log density:

    1. per channel, subtract its median *over the covered pixels*
       (`covered` is the composite's blend-coverage mask, computed before
       the analysis region or the rebate mask exist — "covered" is the
       region that exists). Not optional: without it the weights conflate
       "undo the CFA gain" with "combine the estimates".
    2. the `MONO_MERGE_WEIGHTS` weighted sum of the offset channels (the
       weights already sum to 1.0, so this is the weighted mean).
    3. add back the weighted mean of the three channel medians. This does
       not make the output bracket its inputs — a weighted mean is
       narrower than its inputs' envelope by construction — it guarantees
       only that the merged channel sits at the weighted mean of the
       inputs' density level, close enough that `REBATE_DENSITY_TOLERANCE`
       and `DENSE_BORDER_TOLERANCE`'s absolute thresholds keep meaning
       what they were measured to mean.

    A weighted sum in log is a weighted **geometric mean** of the linear
    transmittances — a defensible physical quantity, and what
    `10 ** (floor + val * (ceil - floor))` recovers downstream. Returns an
    `(H, W, 1)` float32 array."""
    weights = np.asarray(MONO_MERGE_WEIGHTS, dtype=np.float64)
    medians = np.array(
        [float(np.median(img_log[..., ch][covered])) for ch in range(3)]
    )
    aligned = img_log.astype(np.float64) - medians
    merged = aligned @ weights
    merged += float(np.dot(weights, medians))
    return merged[..., np.newaxis].astype(np.float32)


# --- section 3.13: the rebate detector ---------------------------------------

# All five provisional and unmeasured — the same status
# `MIN_GAIN_OVERLAP_PX` and `GAIN_DRIFT_WARN` carry; they go on the
# punchlist together (docs/DECISIONS.md, "Normalization decisions").

# Robust thin-end anchor for the candidate band.
REBATE_ANCHOR_PERCENTILE = 99.9
# Log10 D below that anchor a cell may sit.
REBATE_DENSITY_TOLERANCE = 0.10
# Smaller is not rebate. Absolute, in cells: with `ANALYSIS_BLOCK_PX`
# pinned a cell is a fixed piece of film, so a cell count *is* an area on
# film and this gate means the same millimetres on a single frame and on a
# grid's canvas. 13_340 cells is 17 mm^2 at the reference rig — the value
# the retired 2%-of-the-region rule produced on a single 6000x4000 frame,
# so single-frame behaviour is unchanged by construction.
#
# The fraction it replaced scaled with the *negative's area* while a
# rebate band scales with the edge it runs along, so it tightened as the
# negative grew: a 1.5 mm band across the short ends of a 5x2's 132x40 mm
# canvas is 60 mm^2, which cleared 2% of a 2x2 region and missed it on a
# 5x2. `REBATE_MIN_AREA_FRACTION` survives only as the small-region guard
# — the gate takes whichever of the two is *smaller*, so a region below a
# frame's worth of film is never asked for more than its own 2%.
REBATE_MIN_AREA_CELLS = 13_340
REBATE_MIN_AREA_FRACTION = 0.02
# Log10 D, P90-P10 within a component: base is featureless.
REBATE_MAX_SPREAD = 0.05
# Log10 D between the component and the scene.
REBATE_MIN_SEPARATION = 0.08


def _region_limit(absolute: int, fraction: float, extent: float) -> float:
    """A gate expressed in cells: the absolute physical value, or the
    retired canvas-scaled fraction, whichever is *smaller*.

    Used the same way by all three region gates the pinned cell made
    absolute. The absolute value is what a gate means on real film and is
    the one that binds at any production canvas; the fraction binds only
    on a region smaller than the frame it was calibrated against, where an
    absolute cell count is not a meaningful piece of film — a degenerate
    stitch, or a synthetic grid. Taking the smaller keeps the gate no
    stricter than the fraction rule ever was on a small region (for a
    floor) and no looser than it was (for a ceiling), while stopping both
    from scaling with a large canvas."""
    return min(float(absolute), fraction * extent)


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
    min_area = _region_limit(
        REBATE_MIN_AREA_CELLS, REBATE_MIN_AREA_FRACTION, region_cells
    )
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
        if int(np.count_nonzero(component)) < min_area:
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
    channels = grid_log.shape[-1]
    base_density = tuple(
        float(np.median(grid_log[..., channel][mask])) for channel in range(channels)
    )
    grid_linear = np.power(10.0, grid_log.astype(np.float64))
    clipped = any(
        float(np.mean(grid_linear[..., channel][mask] >= SCAN_CLIP_LEVEL))
        > SCAN_CLIP_WARN
        for channel in range(channels)
    )
    rebate = Rebate(
        detected=True,
        mask_fraction=float(np.count_nonzero(mask)) / region_cells,
        base_density=None if clipped else base_density,
        clipped=clipped,
    )
    return new_keep, rebate


# --- the opaque-holder gate: the detector that needs no geometry -------------

# The rebate detector's discriminator is "no scene content is thinner than
# unexposed film"; `withhold_dense_border` is its mirror, "no scene content
# is denser than the film's characteristic maximum". But the mirror reads
# that maximum off the frame's *own* dense tail, so once it has its
# candidate band every remaining gate is about how the contaminant is
# shaped -- border-touching, thin, featureless, bounded in area. A section
# of the negative holder defeats all four: it is not shaped like a stripe,
# it is arbitrarily large, and it can sit anywhere the film does not.
#
# It is also the one contaminant that does not need those gates, because it
# has an *absolute* discriminator. The holder passes no light, so
# `to_log_density`'s clamp lands it at log10(_DENSITY_FLOOR) = -6.0, while a
# colour negative's Dmax runs about 2.0-2.5 above base and a black-and-white
# negative's about 2.5-3.0. More than `OPAQUE_MAX_DENSITY_BELOW_BASE`
# decades below the thin end is not film at any shape or size, so this gate
# is density and nothing else.
#
# Why it must run before both other detectors: the holder owns every
# dense-end percentile it touches. `DENSE_BORDER_ANCHOR_PERCENTILE` (P0.1)
# lands *inside* the holder, and `DENSE_BORDER_TOLERANCE`'s 0.2-wide band
# then covers holder only -- so the edge fog the mirror exists to catch
# sits three decades outside it, undetected. The same holder pins
# `analyze_bounds`' floor: `BASE_LUMA_CLIP` is 0.01 percent, which on one
# frame's grid is ~70 cells, and a one-cell-wide sliver along that grid's
# 1000-cell edge is fourteen times that. The ratio only widens on a
# stitched canvas, where the sliver runs a longer edge — with
# `ANALYSIS_BLOCK_PX` pinned the grid grows with the canvas, so the tail
# the floor reads grows with it too rather than shrinking.

# Decades below the thin-end anchor past which a cell cannot be film. Sits
# clear of the densest film above base and well clear of the -6.0 clamp, so
# the gate separates "holder" from "film" rather than "clamped" from
# "nearly clamped". Provisional and unmeasured, like the REBATE_* and
# DENSE_BORDER_* constants; recorded per negative either way.
OPAQUE_MAX_DENSITY_BELOW_BASE = 3.2
# The block median softens the holder's edge: a block straddling the
# boundary is a median over holder and film cells together, so it lands
# between the two populations -- above the gate, and contaminated. One cell
# of dilation withholds the straddlers along with the holder.
OPAQUE_DILATE_CELLS = 1
# The density an opaque cell clamps to: log10(_DENSITY_FLOOR), the one
# absolute landmark in the transfer.
OPAQUE_CLAMP_DENSITY = -6.0
# Decades above that clamp the region's thin end must reach for the region
# to contain any film at all. A relative gate cannot see a *wholly* opaque
# region -- its own thin end is the holder, so nothing is decades below
# anything -- and this is the absolute check that can. Real film base sits
# within a few tenths of zero on an exposed scan, so a whole decade of
# margin never fires on film.
OPAQUE_MIN_ANCHOR_ABOVE_CLAMP = 1.0


@dataclasses.dataclass(frozen=True)
class Opaque:
    """The opaque-holder detector's finding for one negative. `threshold`
    is the absolute log density the gate fired at -- the frame's own
    thin-end anchor less `OPAQUE_MAX_DENSITY_BELOW_BASE` -- recorded
    because the anchor and the constant together are what a later
    measurement would revise, and neither is recoverable from
    `mask_fraction` alone. `None` when nothing fired."""

    detected: bool
    mask_fraction: float
    threshold: float | None


def withhold_opaque(
    grid_log: np.ndarray, keep: np.ndarray
) -> tuple[np.ndarray, Opaque]:
    """Withhold cells too dense to be film -- opaque negative holder in the
    analysis region -- from `keep`.

    The anchor is the region's `REBATE_ANCHOR_PERCENTILE` thin-end luma,
    and the choice of end is the point: the holder contaminates the dense
    tail only, so a dense-end anchor would move with the very thing it is
    trying to measure, while the thin end is untouchable by it. Every cell
    at or below `anchor - OPAQUE_MAX_DENSITY_BELOW_BASE` is withheld, with
    no connectivity, area, thinness or flatness gate: those exist to keep
    the dense-border detector off real scene content, and film cannot reach
    this density to begin with.

    Runs *before* `detect_rebate`, which is also why the anchor reads the
    film base rather than the thinnest scene content -- the rebate cells are
    still in `keep` here, and "decades below base" is the physical statement
    the constant is written against.

    The one caveat, and the reason the constant carries margin: clear
    sprocket holes inside the analysis region are film-free and therefore
    thinner than base, and when they exceed 0.1 percent of the region they
    take the anchor with them. That tightens the gate by the base density
    (a few tenths on a masked colour negative) and never loosens it, so the
    failure direction is toward withholding slightly more, and the margin
    keeps even that clear of real film.

    Raises `NormalizationError` on a *wholly* opaque region, which the
    relative gate is structurally unable to see: with nothing but holder
    there is no thin end for the holder to be decades below, and the
    anchor is the holder itself. `OPAQUE_MIN_ANCHOR_ABOVE_CLAMP` is the
    absolute check that catches it. Left to fall through, the case does
    still fail -- `analyze_bounds` finds floors and ceils both at the clamp
    and reports a degenerate channel -- but that message sends the reader
    after the meters when the fault is the layout's rect sitting on the
    holder.
    """
    empty = Opaque(detected=False, mask_fraction=0.0, threshold=None)
    if not keep.any():
        return keep, empty

    lum = luma_of_log(grid_log)
    region_cells = int(np.count_nonzero(keep))
    anchor = _percentile(lum[keep], REBATE_ANCHOR_PERCENTILE)
    if anchor <= OPAQUE_CLAMP_DENSITY + OPAQUE_MIN_ANCHOR_ABOVE_CLAMP:
        raise NormalizationError(
            f"the analysis region holds no film: its thin end ({anchor:.4f}) "
            f"is within {OPAQUE_MIN_ANCHOR_ABOVE_CLAMP} decade of the opaque "
            f"clamp at {OPAQUE_CLAMP_DENSITY}; the analysis rect is on the "
            "negative holder"
        )
    threshold = anchor - OPAQUE_MAX_DENSITY_BELOW_BASE
    mask = keep & (lum <= threshold)
    if not mask.any():
        return keep, empty

    if OPAQUE_DILATE_CELLS > 0:
        side = 2 * OPAQUE_DILATE_CELLS + 1
        dilated = cv2.dilate(
            mask.astype(np.uint8), np.ones((side, side), np.uint8)
        ).astype(bool)
        mask = dilated & keep

    new_keep = keep & ~mask
    if not new_keep.any():
        # Only reachable through the dilation: the threshold alone cannot
        # withhold the anchor cell that defined it.
        raise NormalizationError(
            "the analysis region is entirely opaque: nothing survives the "
            f"gate at {threshold:.4f}; the analysis rect is on the negative "
            "holder, not the film"
        )

    opaque = Opaque(
        detected=True,
        mask_fraction=float(np.count_nonzero(mask)) / region_cells,
        threshold=threshold,
    )
    return new_keep, opaque


# --- section 3.13's dense mirror: the dense-border detector -------------------

# All provisional and unmeasured, like the REBATE_* five. The failure that
# shaped them: a dark, partially-lit sliver beyond the film edge (light-panel
# falloff) that stitching reports as covered on some frames — a featureless
# dense stripe along one border. Raw dense-end percentiles (P0.01 luma, P1.0
# colour) have no defense against it: the block median only removes extremes
# smaller than one block, and the rebate detector withholds *thin* border
# junk only. The mirrored gates: candidates near the dense anchor, border
# connectivity, area and thinness bounds, and separation from the scene's
# own dense tail.

# Robust dense-end anchor for the candidate band.
DENSE_BORDER_ANCHOR_PERCENTILE = 0.1
# Log10 D above that anchor a cell may sit. Wider than the rebate
# detector's tolerance: a gradient stripe's core must be reachable in one
# band, and the area cap bounds what a wide band can withhold.
DENSE_BORDER_TOLERANCE = 0.2
# A smaller candidate is not a stripe. Absolute, in cells, for the reason
# `REBATE_MIN_AREA_CELLS` is: a pinned `ANALYSIS_BLOCK_PX` makes a cell
# count an area on film. 3_335 cells is 4.3 mm^2 at the reference rig, the
# value 0.5% of a single 6000x4000 frame's grid produced.
# `DENSE_BORDER_MIN_AREA_FRACTION` survives as the small-region guard, the
# smaller of the two winning (`_min_area_cells`).
DENSE_BORDER_MIN_AREA_CELLS = 3_335
DENSE_BORDER_MIN_AREA_FRACTION = 0.005
# How thick contamination may be, in cells — 33 is 1.2 mm at the reference
# rig. Two gates, one physical statement, replacing two fractions that
# both scaled with the canvas:
#
# - the stripe's *mean* width, `area / bbox long side`, replacing
#   `DENSE_BORDER_MAX_AREA_FRACTION` (0.05 of the region). A stripe's area
#   is its width times the border it runs along, so capping the area as a
#   fraction of the region let a 5x2's canvas admit a 265 mm^2 component
#   where a single frame admitted 43. Capping the width says what the
#   constant always meant, and is *tighter* than the fraction on a short
#   component, which is the direction that keeps scene content out.
# - the bounding box's thin axis, replacing `DENSE_BORDER_MAX_BBOX_FRACTION`
#   (0.05 of each grid axis, which on a 22000x6667 canvas called anything
#   up to 6.6 mm wide a sliver). Scene content dense enough to matter for
#   the meters spans the frame.
#
# `DENSE_BORDER_MAX_WIDTH_FRACTION` is the surviving fraction, applied to
# the grid's *short* side as the small-region guard (`_region_limit`): 33
# cells is 5% of one frame's short side and binds on anything at or above
# a frame, while on a region below that — a degenerate stitch, a synthetic
# grid — an absolute 1.2 mm is not a meaningful thickness and the fraction
# takes over.
DENSE_BORDER_MAX_WIDTH_CELLS = 33
DENSE_BORDER_MAX_WIDTH_FRACTION = 0.05
# Log10 D along the stripe's length: contamination is featureless.
DENSE_BORDER_MAX_SPREAD = 0.05
# Log10 D between the stripe and the scene's own dense tail.
DENSE_BORDER_MIN_SEPARATION = 0.08
# Outside reference percentile for the separation gate: the scene's
# densest half-percent, outside every gated component.
DENSE_BORDER_OUTSIDE_PERCENTILE = 0.5
# Edge fog fades with distance from the edge, so one pass withholds only
# its densest band; the next pass re-anchors on what is left. A gradient
# ending at the scene's own dense end converges with nothing left to
# withhold.
DENSE_BORDER_MAX_PASSES = 4


@dataclasses.dataclass(frozen=True)
class DenseBorder:
    """The dense-border detector's finding for one negative. Dense, flat
    contamination along a region border (edge fog, a dark sliver beyond the
    film edge) is the max-density mirror of the rebate: `mask_fraction` is
    the fraction of region cells withheld."""

    detected: bool
    mask_fraction: float


def withhold_dense_border(
    grid_log: np.ndarray, keep: np.ndarray
) -> tuple[np.ndarray, DenseBorder]:
    """Withhold dense, featureless, border-touching contamination from
    `keep` (the dense-end mirror of the rebate detector).

    On a **negative**, scene content denser than the film's characteristic
    maximum does not exist — a dense stripe along a border is contamination
    (edge fog, a dark gap between film and light panel), which gives a
    discriminator that does not depend on geometry:

    1. candidates are cells within `DENSE_BORDER_TOLERANCE` of the
       region's `DENSE_BORDER_ANCHOR_PERCENTILE` dense-end luma anchor;
    2. connected components of the candidate mask survive only when they
       touch the region border;
    3. each survivor is gated on area (too small is not a stripe),
       thickness — a border stripe is thin perpendicular to its border, by
       bounding box and by mean width, while scene content dense enough to
       matter spans the frame — and flatness along its length: a stripe
       is featureless *along* the border, while a photograph's dark edge
       (vignette, a wall's shading) varies along it. Edge fog fades across
       its thickness, so the flatness test runs on the medians along the
       length, not on the cells;
    4. separation is the gate that makes "no stripe at all" return
       cleanly: a survivor fires only when its median is at least
       `DENSE_BORDER_MIN_SEPARATION` denser than the P
       `DENSE_BORDER_OUTSIDE_PERCENTILE` of everything outside the gated
       union. Scene content sits inside the distribution's continuous
       dense tail and never clears it; a separated stripe always does;
    5. survivors' union is withheld from `keep`.

    The detector runs up to `DENSE_BORDER_MAX_PASSES`, re-anchoring after
    each pass: an edge-fog gradient is flat *along* the border but falls
    off across it, so one pass catches only the separated core and the
    next pass catches the band behind it. A frame with no separated
    population exits after one pass with nothing withheld.

    The known false positive: a genuinely dense, featureless, thin
    border-touching scene object (a roofline along the edge) denser than
    the scene's dense tail by the separation margin. When it fires
    wrongly the failure is mild — scene highlights map slightly brighter.
    It never invents data.
    """
    empty = DenseBorder(detected=False, mask_fraction=0.0)
    if not keep.any():
        return keep, empty

    lum = luma_of_log(grid_log)
    region_cells = int(np.count_nonzero(keep))
    min_area = _region_limit(
        DENSE_BORDER_MIN_AREA_CELLS, DENSE_BORDER_MIN_AREA_FRACTION, region_cells
    )
    max_width = _region_limit(
        DENSE_BORDER_MAX_WIDTH_CELLS,
        DENSE_BORDER_MAX_WIDTH_FRACTION,
        min(grid_log.shape[0], grid_log.shape[1]),
    )
    total_mask = np.zeros(keep.shape, dtype=bool)
    passes = 0
    while passes < DENSE_BORDER_MAX_PASSES:
        passes += 1
        current = keep & ~total_mask
        if not current.any():
            break
        # The border of what is left to meter: after a pass withholds the
        # stripe's core, the band behind it borders the withheld mask, not
        # the grid edge, and must still count as border-touching.
        border = _region_border(current)
        anchor = _percentile(lum[current], DENSE_BORDER_ANCHOR_PERCENTILE)
        candidates = current & (lum <= anchor + DENSE_BORDER_TOLERANCE)
        if not candidates.any():
            break

        count, labels, _stats, _centroids = cv2.connectedComponentsWithStats(
            candidates.astype(np.uint8), connectivity=8
        )

        gated: list[np.ndarray] = []
        for label in range(1, count):
            component = labels == label
            if not (component & border).any():
                continue
            area = int(np.count_nonzero(component))
            if area < min_area:
                continue
            if not _is_thin(component, max_width):
                continue
            if not _is_featureless(component, lum):
                continue
            gated.append(component)

        if not gated:
            break

        gated_union = np.zeros(keep.shape, dtype=bool)
        for component in gated:
            gated_union |= component

        mask = np.zeros(keep.shape, dtype=bool)
        outside = keep & ~total_mask & ~gated_union
        for component in gated:
            # No outside left to separate from (the gated union is the
            # whole region): there is no scene population to be dense
            # *relative to*, so this cannot be established as
            # contamination.
            if not outside.any():
                continue
            if _percentile(lum[component], 50.0) >= _percentile(
                lum[outside], DENSE_BORDER_OUTSIDE_PERCENTILE
            ) - DENSE_BORDER_MIN_SEPARATION:
                continue
            mask |= component

        if not mask.any():
            break
        total_mask |= mask

    if not total_mask.any():
        return keep, empty

    new_keep = keep & ~total_mask
    dense_border = DenseBorder(
        detected=True,
        mask_fraction=float(np.count_nonzero(total_mask)) / region_cells,
    )
    return new_keep, dense_border


def _is_thin(component: np.ndarray, max_width: float) -> bool:
    """Contamination is no thicker than `max_width` cells, measured two
    ways: its bounding box is thin along at least one axis, and its mean
    width along its own long axis (area over that axis' extent) is within
    the same bound. Scene content dense enough to matter for the meters
    spans the frame; a component that is thin only because its bounding
    box is small, but solid within it, is not a stripe.

    Both tests read the *component's* own extent rather than the grid's,
    so what a caller passes as `max_width` is the only thing tying the
    gate to the region — see `DENSE_BORDER_MAX_WIDTH_CELLS`."""
    rows, cols = np.nonzero(component)
    height = int(rows.max() - rows.min() + 1)
    width = int(cols.max() - cols.min() + 1)
    if min(height, width) > max_width:
        return False
    mean_width = int(np.count_nonzero(component)) / max(height, width)
    return mean_width <= max_width


def _is_featureless(component: np.ndarray, lum: np.ndarray) -> bool:
    """Contamination is featureless *along* its length: the medians of the
    component's cells along the long axis do not vary much. Edge fog fades
    across its thickness, so the test runs on the medians — the stripe's
    interior gradient never reaches them."""
    rows, cols = np.nonzero(component)
    height = rows.max() - rows.min() + 1
    width = cols.max() - cols.min() + 1
    if height >= width:
        vals = [
            float(np.median(lum[component][rows == row_index]))
            for row_index in range(rows.min(), rows.max() + 1)
        ]
    else:
        vals = [
            float(np.median(lum[component][cols == col_index]))
            for col_index in range(cols.min(), cols.max() + 1)
        ]
    return _percentile(np.asarray(vals), 90.0) - _percentile(
        np.asarray(vals), 10.0
    ) <= DENSE_BORDER_MAX_SPREAD


# --- CAST_REMOVAL_PLAN R-1: the highlight reference and the neutral
# --- residual meter ---------------------------------------------------------


def measure_highlight_refs(
    grid_log: np.ndarray,
    keep: np.ndarray,
    base_refs: tuple[float, ...] | None = None,
) -> tuple[float, ...] | None:
    """The dense end's colour references: the same shared, chroma-gated,
    same-pixel neutral set `_same_pixel_color_floor_refs` returns for
    `analyze_bounds` (docs/CAST_REMOVAL_PLAN.md §0.4/§3.2). Independent
    per-channel percentiles at the dense end read a *different scene object
    per channel*, so the highlight reference reuses the gated set rather
    than mirroring the shadow percentile.

    The thin-end anchor it measures chroma against is `_thin_end_refs` —
    the roll's measured film base when one is threaded in, else the
    percentile fallback, exactly what `analyze_bounds` uses — so the
    highlight reference is measured against the same thin end the
    published pixels are stretched by.

    Returns `None` for a single-channel grid, and — load-bearing, not an
    error — when the band held no trustworthy neutrals: that `None` is
    recorded as `null` and is precisely when a user-driven highlight tie
    has the most to do. Recorded, never acted on by the stitch stage."""
    if grid_log.shape[-1] != 3:
        return None
    g_flat = grid_log.reshape(-1, 3)
    keep_flat = keep.reshape(-1)
    lum_full = luma_of_log(grid_log).reshape(-1)
    base = np.asarray(_thin_end_refs(g_flat[keep_flat], 3, base_refs))
    return _same_pixel_color_floor_refs(g_flat, keep_flat, lum_full, base)


# Ported from darktable's DT_ILLUMINANT_DETECT_SURFACES weighting
# (src/iop/channelmixerrgb.c:_auto_detect_WB) into our coordinates
# (docs/CAST_REMOVAL_PLAN.md §3.1).
# darktable's Minkowski p, unchanged: downweights strongly-coloured
# patches.
NEUTRAL_RESIDUAL_P_NORM = 8.0
# Below this many contributing cells there is no estimate.
NEUTRAL_RESIDUAL_MIN_CELLS = 64
# darktable's NORM_MIN, same role.
NEUTRAL_RESIDUAL_EPS = 1e-6

_BSPLINE_KERNEL = np.array([[1, 2, 1], [2, 4, 2], [1, 2, 1]], np.float32) / 16.0


def measure_neutral_residual(
    grid_log: np.ndarray, keep: np.ndarray, bounds: Bounds
) -> tuple[float, float] | None:
    """The frame's residual neutral offset, `(R-G, B-G)` in normalized
    units, over structured low-chroma regions — the meter `auto_color`'s
    solve reads back (docs/CAST_REMOVAL_PLAN.md §3.1/§3.3). Runs on the
    block-median grid, normalized by the same `bounds` the published image
    is stretched by, so the per-channel stretch is already removed and
    what is left is exactly the residual the auto solve wants. Recorded,
    never acted on by the stitch stage — same status as `shadow_refs` and
    `anchor`.

    Returns `None` for a single-channel grid, when fewer than
    `NEUTRAL_RESIDUAL_MIN_CELLS` cells contribute weight, or when the
    weight sum is non-positive."""
    if grid_log.shape[-1] != 3:
        return None
    norm = normalize_log_image(grid_log, bounds)
    a = norm[..., 0] - norm[..., 1]
    b = norm[..., 2] - norm[..., 1]

    # Local statistics over a 3x3 neighbourhood: the B-spline blur
    # darktable uses for the means, and box means for the variances and
    # covariance. Every filter replicates its border.
    border = cv2.BORDER_REPLICATE
    a_bar = cv2.filter2D(a, -1, _BSPLINE_KERNEL, borderType=border)
    b_bar = cv2.filter2D(b, -1, _BSPLINE_KERNEL, borderType=border)

    def box3(values: np.ndarray) -> np.ndarray:
        return cv2.boxFilter(values, -1, (3, 3), borderType=border)

    var_a = box3(a * a) - box3(a) ** 2
    var_b = box3(b * b) - box3(b) ** 2
    cov_ab = box3(a * b) - box3(a) * box3(b)

    # Deliberate deviation from the port: darktable lets a negative
    # covariance subtract from the accumulation, but on our grid a negative
    # weight can flip the sign of the estimate on a noisy frame, and a
    # patch whose two chroma coordinates are anti-correlated is not
    # evidence about the illuminant either way. Clamp, don't subtract.
    w = np.maximum(var_a * var_b * cov_ab, 0.0)

    # Cells whose whole 3x3 neighbourhood is inside the analysis region, so
    # a withheld rebate or dense border never leaks into a neighbourhood —
    # the same idiom `_region_border` uses, eroding `keep` itself. The
    # constant-zero border makes grid-edge cells ineligible, as their
    # neighbourhood is not fully inside the grid.
    keep_eroded = keep & (
        cv2.erode(
            keep.astype(np.uint8),
            np.ones((3, 3), np.uint8),
            borderType=border,
            borderValue=0,
        ).astype(bool)
    )

    p_norm = (
        np.power(np.abs(a_bar), NEUTRAL_RESIDUAL_P_NORM)
        + np.power(np.abs(b_bar), NEUTRAL_RESIDUAL_P_NORM)
    ) ** (1.0 / NEUTRAL_RESIDUAL_P_NORM) + NEUTRAL_RESIDUAL_EPS

    weight = np.where(keep_eroded, w / p_norm, np.float32(0.0))
    total = float(weight.sum())
    contributing = int(np.count_nonzero(weight > 0.0))
    if total <= 0.0 or contributing < NEUTRAL_RESIDUAL_MIN_CELLS:
        return None
    return (
        float((a_bar * weight).sum() / total),
        float((b_bar * weight).sum() / total),
    )


# --- section 3.4's clamp: the roll-population safety net ----------------------

# The per-negative bounds a clamp needs before it will act.
CLAMP_MIN_SAMPLES = 3
# Clamp window half-width, in multiples of the population's MAD.
CLAMP_K_MAD = 4.0
# Clamp window floor, in log10 D — one and a half stops of legitimate
# per-frame exposure shift must never be clamped; the population's MAD is
# sometimes far tighter than real rolls are.
CLAMP_MIN_WINDOW = 0.5


def clamp_bounds(bounds: Bounds, references: list[Bounds]) -> tuple[Bounds, bool]:
    """D-4's safety net, from data the pipeline already records: pull
    per-channel bounds back toward the roll's population when a negative's
    own meters latched contamination the per-frame detectors missed.

    Per channel, floors and ceils are clamped independently into
    `median(references) +/- max(CLAMP_K_MAD * MAD, CLAMP_MIN_WINDOW)` —
    wide enough for a stop or two of legitimate per-frame exposure shift,
    tight enough that a latched outlier (the fill's -6.0, an edge fog
    band) cannot survive. Returns `(bounds, clamped)`; with fewer than
    `CLAMP_MIN_SAMPLES` references nothing is clamped, and a clamp that
    would ever degenerate a channel (`ceil <= floor`) is discarded whole —
    a safety net must never make things worse."""
    if len(references) < CLAMP_MIN_SAMPLES:
        return bounds, False

    channels = len(bounds.floors)

    def clamp_axis(
        values: tuple[float, ...], references_by_channel: list[list[float]]
    ) -> tuple[float, ...]:
        clamped = []
        for channel in range(channels):
            column = references_by_channel[channel]
            median = float(np.median(column))
            mad = float(np.median(np.abs(np.asarray(column) - median)))
            window = max(CLAMP_K_MAD * mad, CLAMP_MIN_WINDOW)
            clamped.append(min(max(values[channel], median - window), median + window))
        return tuple(clamped)

    floors = clamp_axis(
        bounds.floors,
        [[b.floors[ch] for b in references] for ch in range(channels)],
    )
    ceils = clamp_axis(
        bounds.ceils,
        [[b.ceils[ch] for b in references] for ch in range(channels)],
    )
    for channel in range(channels):
        if ceils[channel] <= floors[channel]:
            return bounds, False
    return Bounds(floors=floors, ceils=ceils), floors != bounds.floors or ceils != bounds.ceils


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
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """The per-channel minimum and maximum normalized value of the input
    (section 3.6): recorded per negative so the two headroom constants can
    be tuned from real scans instead of estimated — `observed_min` pinned
    at `-NORMALIZED_HEADROOM_LOW` means the headroom is clipping and
    tones have been lost."""
    channels = normalized.shape[-1]
    flat = normalized.reshape(-1, channels)
    mins = tuple(float(flat[:, ch].min()) for ch in range(channels))
    maxs = tuple(float(flat[:, ch].max()) for ch in range(channels))
    return mins, maxs


def headroom_clip_fractions(
    normalized: np.ndarray,
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Per-channel fraction of normalized values the encode's headroom
    clips (section 3.6), split by which rail they clip against: below
    `-NORMALIZED_HEADROOM_LOW` is the dense end (scene highlights), above
    `1.0 + NORMALIZED_HEADROOM_HIGH` is the thin end (scene shadows) — see
    the constants' own comments. Returns `(highlights, shadows)`."""
    channels = normalized.shape[-1]
    low = -NORMALIZED_HEADROOM_LOW
    high = 1.0 + NORMALIZED_HEADROOM_HIGH
    flat = normalized.reshape(-1, channels)
    highlights = tuple(float(np.mean(flat[:, ch] < low)) for ch in range(channels))
    shadows = tuple(float(np.mean(flat[:, ch] > high)) for ch in range(channels))
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
        "analysis_block_px": ANALYSIS_BLOCK_PX,
        "analysis_passthrough_px": ANALYSIS_PASSTHROUGH_PX,
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
        "dense_border_anchor_percentile": DENSE_BORDER_ANCHOR_PERCENTILE,
        "dense_border_tolerance": DENSE_BORDER_TOLERANCE,
        "dense_border_min_area_cells": DENSE_BORDER_MIN_AREA_CELLS,
        "dense_border_min_area_fraction": DENSE_BORDER_MIN_AREA_FRACTION,
        "dense_border_max_width_cells": DENSE_BORDER_MAX_WIDTH_CELLS,
        "dense_border_max_width_fraction": DENSE_BORDER_MAX_WIDTH_FRACTION,
        "dense_border_min_separation": DENSE_BORDER_MIN_SEPARATION,
        "dense_border_outside_percentile": DENSE_BORDER_OUTSIDE_PERCENTILE,
        "dense_border_max_passes": DENSE_BORDER_MAX_PASSES,
        "clamp_min_samples": CLAMP_MIN_SAMPLES,
        "clamp_k_mad": CLAMP_K_MAD,
        "clamp_min_window": CLAMP_MIN_WINDOW,
        "normalized_headroom_low": NORMALIZED_HEADROOM_LOW,
        "normalized_headroom_high": NORMALIZED_HEADROOM_HIGH,
        "normalized_fill": NORMALIZED_FILL,
        # MONOCHROME_PLAN §3: the merge weights shape published output on
        # mono rolls, so they are roll invariants from the step that
        # introduces them.
        "mono_merge_weights": list(MONO_MERGE_WEIGHTS),
        # CAST_REMOVAL_PLAN R-1: the neutral-residual meter's constants and
        # the highlight reference's provenance.
        "neutral_residual_p_norm": NEUTRAL_RESIDUAL_P_NORM,
        "neutral_residual_min_cells": NEUTRAL_RESIDUAL_MIN_CELLS,
        "highlight_neutral_source": "same_pixel_color_refs",
    }


def upgrade_normalize_params(params: dict) -> dict:
    """MONOCHROME_PLAN section 5.1's forward shim, for the exact-dict
    comparison `manifest.py` and `roll_manifest.py` run over
    `processing_params`: a stored `normalize` block is upgraded in memory
    by injecting the current defaults for every key it lacks, then
    compared. Because the injected defaults are read from the live
    `build_params()`, this covers not just a v1 block (predating the mono
    feature entirely) but also a v2 block written between §1 shipping and
    a later step (§2's thresholds, §3's weights) adding a new key —
    exactly what a roll stitched during the plan's own measurement gate
    produces. Either one compares equal to a fresh build as long as the
    new constants sit at their defaults, which for an existing colour roll
    they do.

    Not gated on the stored `format_version` at all: `setdefault` is a
    no-op for a key already present, so a fully current block passes
    through unchanged and the function is idempotent (applying it to an
    upgraded block is a no-op) — it is simply always safe to apply before
    comparing. It is the only place that knows an older format existed."""
    upgraded = dict(params)
    for key, value in build_params().items():
        if key == "format_version":
            continue
        upgraded.setdefault(key, value)
    # Retired with the auto-detector (protocol 15), and — v4 — with the
    # pinned analysis cell and the region gates it let become absolute:
    # strip if an older roll still carries them so invariant comparison
    # stays equal. A key whose *value* changed cannot be absorbed by
    # `setdefault`, so a retired key must be removed by name here and
    # reintroduced under a new one above.
    for deprecated in (
        "mono_chroma_max",
        "colour_chroma_min",
        "analysis_grid",
        "dense_border_max_area_fraction",
        "dense_border_max_bbox_fraction",
    ):
        upgraded.pop(deprecated, None)
    upgraded["format_version"] = NORMALIZE_FORMAT_VERSION
    return upgraded
