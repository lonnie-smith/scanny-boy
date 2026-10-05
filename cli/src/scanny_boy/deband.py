"""Development-band removal for colour negative rolls (DEBAND_PLAN.md).

User-guided, region-based removal of broad, low-contrast colour bands
running along the film's length (uneven development), as distinct from
`scratches.py`'s automatic detection of thin scratches. The user draws one
or more rectangular (optionally tilted) regions over flat, banded film
areas; each region fits a per-line chroma correction from its own pixels
and the correction replays wherever the ops log is read. The module is a
leaf: it imports only ``numpy``, ``scipy.ndimage``, ``cv2``, and
``normalization``.

**Arithmetic.** The fit runs in log10 density: ``decode_normalized(codes)``
times the per-channel span from the negative's normalization record (as in
`scratches.py`). The correction is computed chroma-only (zero-sum across
channels) in those log10 units, then divided by the per-channel span to
convert it to ``val`` units for storage — so, like `scratches.py`'s table,
replay is a plain subtraction from ``val`` with no per-pixel span lookup
needed.

**Geometry.** A region's ``window`` is ``(x, y, w, h, tilt_deg)`` in
TIFF-space pixels, the same shape the `crop` op stores: an axis-aligned
``w`` x ``h`` rectangle tilted ``tilt_deg`` about its own centre. Tilt is
handled two different ways on the two sides of the algorithm, deliberately:

- **Fit** warps a padded extraction of the window straight (`cv2.warpAffine`
  with the window's own rotation) so the block/column statistics can be
  taken on a plain axis-aligned grid. This resamples the *signal being
  measured*, which is fine — the fitted correction is a smooth, low
  frequency field.
- **Apply** never resamples the image. Every touched pixel's position is
  rotated *algebraically* into the window's local frame and used to
  bilinearly look up the (small) stored correction table; the image's own
  pixels are read and written at their original coordinates. This is what
  makes a region render with ``REGION_MARGIN`` byte-identical to the
  corresponding slice of a full apply.

**Axis.** ``"vertical"`` bands vary along the window's height; the block
split runs down `window`'s ``h``. ``"horizontal"`` is the same computation
with the window's local width/height roles swapped (mirroring
`scratches.py`'s transpose-view treatment of its own horizontal axis),
never an actual transpose of the source image.

**Fit model (differs from the plan's §3.1; all measured on real scans).**

1. *Per-block baseline.* The degree-2 robust baseline is fitted to each
   256-line block's own profile, not once to the block mean. A shared
   baseline turned every block's own vertical sky gradient into a
   "correction".
2. *Common profile, not per-block profile.* A band runs the whole length, so
   the correction is what the measured blocks agree on (`_correction_from_
   deviation`): per column, the densest cluster of the blocks' low-passed
   deviations (ties go to the cluster nearer zero), its median being the
   common profile. A cloud, a tree's shadow or any broad object that only
   some blocks see falls outside the cluster and is never "corrected" into
   the band. Per-block wander (a tilted or drifting edge) is admitted only
   for blocks within ``WANDER_TOL_LOG10`` of the common profile.
3. *Confidence.* The whole correction is scaled by the cluster's share of
   the measured blocks (smoothstep between ``AGREEMENT_LOW`` and
   ``AGREEMENT_HIGH``, blurred 48 px), so columns where the blocks disagree
   get no correction rather than a guess, and nothing ends in a hard edge.
   Columns within ``EDGE_GUARD_PX`` of an unmeasured run (a pole, a canopy)
   are re-derived from clean columns beyond.
4. *Gaps are filled along the band* (a cell unmeasured in one block takes the
   same column's nearest measured blocks), then across x only for a column
   unmeasured in every block. A block with < 25 % measurable columns is
   unmeasured.
5. *Hard cap* ``CORR_LIMIT_LOG10`` = 0.05 on the stored correction (measured
   bands are <= 0.03), so a misused region cannot do large damage.

**Measured vs provisional constants** (`Sep-20-2026-at-4-46-PM` roll,
`cli/tools/measure_deband.py`; scores are the p99 |deviation from a
quadratic| of the background B-G column profile in x1e-3 log10, noise floor
~3, fit on even 32-line bands and scored on the odd ones, side feather
excluded). Held-out before -> after: 165541 sky 21.0 -> 3.0 (prototype
3.8), 165541 pavement 11.6 -> 2.6, 165142 sky (tree in region) 17.9 -> 3.8,
165001 sky (cloud, pole, canopy) 11.3 -> 5.4, 165111 control 6.6 -> 2.8 with
max |corr| 0.007 log10. 164837 (cloud + strong 2-D sky gradient) is *not*
flat input: the held-out score stays 26 -> 25.7 because most of its
structure is scene, and the correction stays <= 0.02 log10 with no
streaks or patches in the contact sheet. ``BLOCK_PX`` (256),
``BLOCK_SMOOTH_SIGMA_BLOCKS`` (1), ``DEV_SMOOTH_SIGMA_PX`` (2),
``BASELINE_DEGREE`` (2), ``BG_BLUR_SIGMA`` / ``BG_LUMA_TOL`` reproduce the
plan's prototype. ``AGREE_TOL_LOG10`` (0.012) is measured: at 0.008 the real
bands lose (165142 17.9 -> 6.4); ``WANDER_TOL_LOG10`` (0.004) is measured:
0.012 admits cloud contamination into the per-block wander at no gain.
Measured changes from the plan: ``PITCH_PX`` 4 -> 8 (identical held-out
scores, table halves: 165541 pavement 1876 lines is 46 KB vs 93 KB; a
full-frame region of ~6000 lines will be ~90-130 KB, so the plan's <= 64 KB
target holds for regions up to ~3000 lines); ``COL_BG_FRACTION_BLOCK`` 0.2
-> 0.5 (thin sky strips under a canopy gave visible streaks);
``REGION_MARGIN`` 144 -> 192 (see its comment). Provisional, unchanged or
new: ``PROTECT_TOL``, the feather, ``COL_BG_FRACTION_REGION``,
``MIN_BG_FRACTION``, ``MIN_BLOCK_GOOD_COLUMNS``, ``AGREE_SMOOTH_PX``,
``AGREEMENT_LOW`` / ``AGREEMENT_HIGH``, ``MIN_AGREE_BLOCKS``,
``CONFIDENCE_SMOOTH_PX``, ``EDGE_GUARD_PX``, ``TRACK_LIMIT_LOG10`` (a guard
on narrow per-block spikes), the reject constants. ``PROTECT_TOL`` is in
``val`` units (the op carries no spans), roughly 0.06 log10 for this roll's
spans, not the plan's 0.04 log10.
"""

from __future__ import annotations

import base64
import dataclasses
import math
import warnings
import zlib
from typing import Any

import cv2
import numpy as np
from scipy.ndimage import binary_erosion, gaussian_filter1d

from scanny_boy import normalization

# Stamped into every ``deband`` op; a mismatch means "not live" (§4).
FIT_VERSION = 1

# --- geometry ---------------------------------------------------------------

# 4 sigma of BG_BLUR_SIGMA. The plan called for 3 sigma (144 px); measured
# (region-render-equals-full-apply test) that 3 sigma is not quite enough
# and moved it to 4 sigma, matching `scratches.py`'s own margin convention
# (`REGION_ROW_MARGIN` = 4 * `LEVEL_SIGMA`). The reason is `cv2.GaussianBlur`
# itself: with `ksize=0` on a float array it picks a kernel radius of
# `4 * sigma`, not `3 * sigma`, so a 3-sigma margin lets the kernel's
# outermost taps reach past the read window's edge into the border
# extrapolation OpenCV substitutes there — a real, if tiny, difference from
# what the same blur sees with the true pixels a full-image apply has
# beyond that edge. At 4 sigma the kernel never reaches the edge, and the
# two paths agree exactly.
REGION_MARGIN = 192
SAMPLE_MARGIN_PX = REGION_MARGIN

MIN_BAND_PX = 256  # minimum region extent along the band axis
MIN_CROSS_PX = 512  # minimum region extent across the band

# --- fit ----------------------------------------------------------------

BG_BLUR_SIGMA = 48.0  # measured (prototype, §1/§3.1)
BG_LUMA_TOL = 0.03  # measured (prototype, §3.1 step 2)

BLOCK_PX = 256  # measured, see module docstring
BLOCK_SMOOTH_SIGMA_BLOCKS = 1.0  # measured
DEV_SMOOTH_SIGMA_PX = 2.0  # measured

COL_BG_FRACTION_REGION = 0.4  # provisional (plan §3.1 step 3)
COL_BG_FRACTION_BLOCK = 0.5  # measured (plan said 0.2); see module docstring
MIN_BG_FRACTION = 0.20  # provisional (plan §5.1 failure gate)

WANDER_SMOOTH_PX = (
    32.0  # provisional: scale below which block differences are edge wander
)
AGREE_SMOOTH_PX = 96.0  # provisional: low-pass scale of the block disagreement
AGREE_TOL_LOG10 = 0.012  # provisional: blocks agree within this
WANDER_TOL_LOG10 = 0.004  # provisional: a block's own wander fades out at this offset
AGREEMENT_LOW = 0.5  # provisional: below this agreement, no correction
AGREEMENT_HIGH = 0.8  # provisional: above this, full correction
MIN_AGREE_BLOCKS = 2  # provisional: measured blocks for full confidence
EDGE_GUARD_PX = (
    24  # provisional: columns this close to an unmeasured run are re-derived
)
CONFIDENCE_SMOOTH_PX = 48.0  # provisional
MIN_BLOCK_GOOD_COLUMNS = 0.25  # provisional: a block with fewer is unmeasured
# How far one block's profile may stray from the all-block median profile.
TRACK_LIMIT_LOG10 = 0.02  # provisional guard on narrow per-block spikes
CORR_LIMIT_LOG10 = 0.05  # provisional safety cap; measured bands are <= 0.03

BASELINE_DEGREE = 2  # measured, see module docstring
BASELINE_REJECT_PASSES = 3  # provisional (prototype default)
BASELINE_REJECT_SIGMA = 2.5  # provisional (prototype default, x1.4826*MAD)

PITCH_PX = 8  # measured (plan said 4); see module docstring

TABLE_SCALE = 1e-5  # matches scratches.py's table quantization

# --- apply ----------------------------------------------------------------

PROTECT_BLUR_SIGMA = BG_BLUR_SIGMA  # "recomputed from the image being corrected"
PROTECT_TOL = 0.04  # provisional (plan §3.2)

FEATHER_PX = 128  # provisional (plan §3.2)
FEATHER_FRACTION = 0.10  # provisional (plan §3.2, "10% of the side")

FILL_CODE = int(
    normalization.encode_normalized(
        np.full((1, 1, 1), normalization.NORMALIZED_FILL, dtype=np.float32)
    ).ravel()[0]
)


@dataclasses.dataclass(frozen=True)
class RegionFit:
    """One region's fitted correction. ``window`` is the TIFF-space
    ``(x, y, w, h, tilt_deg)`` the region was fitted on. ``corr`` is
    ``(blocks, n_cols, 3)`` float32, **val units**, at an x pitch of
    ``pitch_px`` measured from the window's own near edge along the cross
    axis (left edge for a vertical band, top edge for a horizontal one);
    ``n_cols = ceil(cross_extent / pitch_px)``."""

    window: tuple[float, float, float, float, float]
    axis: str
    block_px: int
    pitch_px: int
    blocks: int
    corr: np.ndarray


def _rotation_matrix(angle_deg: float) -> np.ndarray:
    """CCW rotation matrix mapping local (u, v) offsets onto global (x, y)
    offsets: ``global = R @ local``. Purely a bookkeeping convention shared
    by fit (image warp) and apply (algebraic point rotation) — nothing
    downstream depends on which rotation sense "CCW" means in pixel space,
    only that both sides agree."""
    angle_rad = math.radians(angle_deg)
    cos_a, sin_a = math.cos(angle_rad), math.sin(angle_rad)
    return np.array([[cos_a, -sin_a], [sin_a, cos_a]], dtype=np.float64)


def _decode_val(codes: np.ndarray) -> np.ndarray:
    return normalization.decode_normalized(codes).astype(np.float32)


def _encode_table(table: np.ndarray) -> str:
    scaled = np.clip(np.round(table / TABLE_SCALE), -32768, 32767).astype(np.int16)
    return base64.b64encode(zlib.compress(scaled.tobytes())).decode("ascii")


def _decode_table(table_b64: str, n_blocks: int, n_cols: int) -> np.ndarray:
    raw = zlib.decompress(base64.b64decode(table_b64))
    table = np.frombuffer(raw, dtype=np.int16).reshape(n_blocks, n_cols, 3)
    return table.astype(np.float32) * TABLE_SCALE


def _band_cross_extent(
    window: tuple[float, float, float, float, float], axis: str
) -> tuple[float, float]:
    _x, _y, w, h, _tilt = window
    return (h, w) if axis == "vertical" else (w, h)


def _robust_baseline(t: np.ndarray, values: np.ndarray) -> np.ndarray:
    """Robust polynomial of ``BASELINE_DEGREE`` through ``values`` (plan
    §3.1 step 5): refit ``BASELINE_REJECT_PASSES`` times, each pass dropping
    residuals beyond ``BASELINE_REJECT_SIGMA`` x 1.4826 x MAD."""
    keep = np.ones(len(t), dtype=bool)
    coeffs = np.polyfit(t, values, BASELINE_DEGREE)
    for _pass in range(BASELINE_REJECT_PASSES):
        coeffs = np.polyfit(t[keep], values[keep], BASELINE_DEGREE)
        residual = values - np.polyval(coeffs, t)
        mad = float(np.median(np.abs(residual)))
        keep = np.abs(residual) < BASELINE_REJECT_SIGMA * 1.4826 * mad + 1e-9
    return np.polyval(coeffs, t)


def _fill_gaps(deviation: np.ndarray, usable: np.ndarray, xs: np.ndarray) -> np.ndarray:
    """Fill the NaN (unmeasured) cells of ``deviation`` (blocks, columns, 3).

    A band runs *along* the film, so a cell unmeasured in one block is best
    taken from the same column in its nearest measured blocks (linear between
    two, nearest at an end). A column unmeasured in every block (a pole or
    tower) takes its neighbours' values across the band instead."""
    n_blocks = deviation.shape[0]
    block_axis = np.arange(n_blocks)
    filled = deviation.copy()
    partial = np.where(usable.any(axis=0) & ~usable.all(axis=0))[0]
    for col in partial:
        rows = np.where(usable[:, col])[0]
        for channel in range(3):
            filled[:, col, channel] = np.interp(
                block_axis, rows, deviation[rows, col, channel]
            )
    measured_cols = usable.any(axis=0)
    if not measured_cols.all():
        for block in range(n_blocks):
            for channel in range(3):
                filled[block, :, channel] = np.interp(
                    xs, xs[measured_cols], filled[block, measured_cols, channel]
                )
    return filled


# --- fit ---------------------------------------------------------------


def _correction_from_deviation(
    deviation: np.ndarray, usable: np.ndarray, *, debug: dict | None = None
) -> np.ndarray:
    """Per-block measured deviations (blocks, columns, 3; NaN where a block
    could not measure a column) -> the chroma-only correction in log10,
    shape (blocks, columns, 3).

    A band runs the whole length, so the blocks that measured a column agree
    on a **common** profile there, and the correction is that profile plus a
    small per-block ``wander`` (a slightly tilted or wandering edge). What
    only some blocks see (a cloud, a tree's shadow) is scene, not band:

    - per column, the common profile is the densest cluster of the measured
      blocks' (low-passed) deviations; when clusters tie the one nearer zero
      wins, so ambiguity resolves to the smaller correction;
    - a block outside the cluster contributes no wander;
    - the whole correction fades to zero through ``confidence`` (the cluster's
      share of the measured blocks), smoothly in x, so ambiguous columns get
      no correction rather than a guess, and nothing ends in a hard edge."""
    n_blocks, n_cross = deviation.shape[:2]
    xs = np.arange(n_cross)
    if not usable.any():
        raise ValueError("deband region has no usable background; pick a flatter area")
    filled = _fill_gaps(deviation, usable, xs)
    chroma = filled - filled.mean(axis=2, keepdims=True)
    # Clusters are judged on a low-passed copy so a narrow edge that shifts
    # between blocks does not split them.
    smooth = gaussian_filter1d(chroma, AGREE_SMOOTH_PX, axis=1, mode="nearest")

    # distance[b, c, x]: how far block c's deviation is from block b's.
    distance = np.abs(smooth[:, np.newaxis] - smooth[np.newaxis]).max(axis=3)
    neighbours = (distance < AGREE_TOL_LOG10) & usable[np.newaxis]
    counts = neighbours.sum(axis=1)  # (blocks, columns)
    magnitude = np.minimum(np.abs(smooth).max(axis=2) / 0.05, 1.0)
    score = np.where(usable, counts + 0.5 * (1.0 - magnitude), -1.0)
    seed = np.argmax(score, axis=0)  # (columns,)
    members = neighbours[seed, :, xs].T & usable  # (blocks, columns)
    member_count = members.sum(axis=0)
    measured = usable.sum(axis=0)
    # Columns beside an object (within EDGE_GUARD_PX of an unmeasured run)
    # carry its halo; they are re-derived from the clean columns beyond.
    seen = measured > 0
    reliable = binary_erosion(seen, iterations=EDGE_GUARD_PX, border_value=1)
    if not reliable.any():
        reliable = seen

    # The cluster's median (not mean): a few cloud blocks that fall just
    # inside the tolerance must not drag the common profile.
    member_mask = members[..., np.newaxis]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        common = np.nanmedian(np.where(member_mask, chroma, np.nan), axis=0)
        smooth_common = np.nanmedian(np.where(member_mask, smooth, np.nan), axis=0)
    for channel in range(3):
        common[:, channel] = np.interp(xs, xs[reliable], common[reliable, channel])
        smooth_common[:, channel] = np.interp(
            xs, xs[reliable], smooth_common[reliable, channel]
        )

    with np.errstate(invalid="ignore", divide="ignore"):
        agreement = member_count / measured
    agreement = np.interp(xs, xs[reliable], agreement[reliable])
    coverage = np.interp(
        xs,
        xs[reliable],
        np.minimum(measured[reliable] / min(MIN_AGREE_BLOCKS, n_blocks), 1.0),
    )
    agreement = gaussian_filter1d(
        agreement * coverage, CONFIDENCE_SMOOTH_PX, mode="nearest"
    )
    confidence = np.clip(
        (agreement - AGREEMENT_LOW) / (AGREEMENT_HIGH - AGREEMENT_LOW), 0.0, 1.0
    )
    confidence = confidence * confidence * (3.0 - 2.0 * confidence)  # smoothstep

    # Per-block wander: only for measured blocks close to the common
    # profile, fading to nothing as they approach the agreement tolerance.
    off = np.abs(smooth - smooth_common[np.newaxis]).max(axis=2)
    admit = np.clip(2.0 - 2.0 * off / WANDER_TOL_LOG10, 0.0, 1.0) * usable
    wander = np.clip(chroma - common[np.newaxis], -TRACK_LIMIT_LOG10, TRACK_LIMIT_LOG10)
    corr = common[np.newaxis] + wander * admit[..., np.newaxis]

    if n_blocks > 1:
        corr = gaussian_filter1d(
            corr, BLOCK_SMOOTH_SIGMA_BLOCKS, axis=0, mode="nearest"
        )
    if DEV_SMOOTH_SIGMA_PX > 0:
        corr = gaussian_filter1d(corr, DEV_SMOOTH_SIGMA_PX, axis=1, mode="nearest")
    corr = corr * confidence[np.newaxis, :, np.newaxis]
    corr = corr - corr.mean(axis=2, keepdims=True)
    if debug is not None:
        debug.update(common=common, confidence=confidence, agreement=agreement)
    return np.clip(corr, -CORR_LIMIT_LOG10, CORR_LIMIT_LOG10)


def fit_region(
    image_codes: np.ndarray,
    spans: tuple[float, ...],
    window: tuple[float, float, float, float, float],
    axis: str,
    *,
    prior_regions: list[RegionFit] | None = None,
    row_mask: np.ndarray | None = None,
) -> RegionFit:
    """Fit one region's chroma correction (plan §3.1).

    ``image_codes`` is the published TIFF's uint16 codes. ``spans`` are the
    per-channel ``ceil - floor`` log10 spans from the negative's
    normalization record. ``window`` is TIFF-space ``(x, y, w, h,
    tilt_deg)``. ``axis`` is ``"vertical"`` or ``"horizontal"``. ``prior_regions``
    is the list of already-fitted earlier regions (in op order); they are
    applied to ``image_codes`` before this region is fitted, so a
    sequential replay of the finished op sees exactly what each fit saw
    (§3.3).

    ``row_mask`` is a calibration/validation hook (``cli/tools/
    measure_deband.py``): a boolean array indexed by the window's own line
    along the band axis (0 = the near edge); only lines marked True feed the
    statistics, so a fit/test split can be scored without the fit seeing the
    test lines. Production callers leave it ``None``.

    Raises ``ValueError`` for a non-3-channel image, a region smaller than
    ``MIN_BAND_PX`` along the band axis or ``MIN_CROSS_PX`` across it, or a
    region with less than ``MIN_BG_FRACTION`` background pixels.
    """
    image_codes = np.asarray(image_codes)
    if image_codes.ndim != 3 or image_codes.shape[2] != 3:
        raise ValueError(
            f"deband requires a 3-channel image; got shape {image_codes.shape}"
        )
    if axis not in ("vertical", "horizontal"):
        raise ValueError(f"unknown deband axis {axis!r}")
    if len(spans) != 3:
        raise ValueError("deband requires 3 per-channel spans")

    x, y, w, h, tilt_deg = (float(v) for v in window)
    band_len, cross_len = _band_cross_extent((x, y, w, h, tilt_deg), axis)
    if band_len < MIN_BAND_PX:
        raise ValueError(
            f"deband region is {band_len:.0f}px along the band axis; "
            f"need at least {MIN_BAND_PX}px"
        )
    if cross_len < MIN_CROSS_PX:
        raise ValueError(
            f"deband region is {cross_len:.0f}px across the band; "
            f"need at least {MIN_CROSS_PX}px"
        )

    if prior_regions:
        prior_params = deband_params(
            (image_codes.shape[1], image_codes.shape[0]),
            axis,
            prior_regions,
            True,
            1.0,
        )
        image_codes = apply(image_codes, prior_params)

    spans_arr = np.asarray(spans, dtype=np.float32)
    cx, cy = x + (w - 1) / 2.0, y + (h - 1) / 2.0
    rotation = _rotation_matrix(tilt_deg)

    sample_w = math.ceil(w) + 2 * SAMPLE_MARGIN_PX
    sample_h = math.ceil(h) + 2 * SAMPLE_MARGIN_PX
    offset = np.array([(sample_w - 1) / 2.0, (sample_h - 1) / 2.0])
    centre = np.array([cx, cy])
    translation = centre - rotation @ offset
    affine = np.hstack([rotation, translation.reshape(2, 1)])

    fill_value = (float(FILL_CODE),) * 3
    sample_codes = cv2.warpAffine(
        image_codes,
        affine,
        (sample_w, sample_h),
        flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=fill_value,
    )
    sample_fill = np.all(sample_codes == FILL_CODE, axis=-1)
    sample_log = _decode_val(sample_codes) * spans_arr[np.newaxis, np.newaxis, :]

    # Orient so array rows always run along the band axis, mirroring
    # scratches.py's transpose-view treatment of its horizontal axis — the
    # source image itself is never transposed, only this local sample.
    if axis == "horizontal":
        sample_log = np.swapaxes(sample_log, 0, 1)
        sample_fill = np.swapaxes(sample_fill, 0, 1)

    height_local, width_local = sample_log.shape[:2]
    va = np.arange(height_local, dtype=np.float64) - (height_local - 1) / 2.0
    ua = np.arange(width_local, dtype=np.float64) - (width_local - 1) / 2.0

    luminance = sample_log.mean(axis=2)
    blurred = cv2.GaussianBlur(
        luminance.astype(np.float32), (0, 0), sigmaX=BG_BLUR_SIGMA
    )
    background = (np.abs(luminance - blurred) < BG_LUMA_TOL) & ~sample_fill

    row_in_window = np.abs(va) <= band_len / 2.0
    col_in_window = np.abs(ua) <= cross_len / 2.0
    rows_idx = np.where(row_in_window)[0]
    cols_idx = np.where(col_in_window)[0]

    interior_bg = background[np.ix_(rows_idx, cols_idx)]
    keep_rows = np.ones(len(rows_idx), dtype=bool)
    if row_mask is not None:
        keep_rows[:] = False
        given = np.asarray(row_mask, dtype=bool)
        keep_rows[: min(len(keep_rows), len(given))] = given[: len(keep_rows)]
        if not keep_rows.any():
            raise ValueError("deband row_mask selects no lines")
        rows_idx = rows_idx[keep_rows]
        interior_bg = interior_bg[keep_rows]
    bg_fraction = float(interior_bg.mean()) if interior_bg.size else 0.0
    if bg_fraction < MIN_BG_FRACTION:
        raise ValueError(
            f"deband region has only {bg_fraction:.0%} background pixels "
            f"(< {MIN_BG_FRACTION:.0%}); pick a flatter area"
        )

    sub_log = sample_log[np.ix_(rows_idx, cols_idx)]
    sub_bg = interior_bg
    va_sub = va[rows_idx]

    n_blocks = max(1, math.ceil(band_len / BLOCK_PX))
    block_idx = np.clip(
        np.floor((va_sub + band_len / 2.0) / BLOCK_PX).astype(int), 0, n_blocks - 1
    )
    col_bg_fraction_total = sub_bg.mean(axis=0)
    good_cols_overall = col_bg_fraction_total >= COL_BG_FRACTION_REGION

    n_cross = len(cols_idx)
    xs = np.arange(n_cross)
    t = (xs.astype(np.float64) - (n_cross - 1) / 2.0) / max((n_cross - 1) / 2.0, 1.0)

    # Per block: the median background profile across the band, minus that
    # block's own robust baseline. A block that cannot be measured (an object
    # spans it, or its lines were withheld) stays NaN and is filled below.
    deviation = np.full((n_blocks, n_cross, 3), np.nan, dtype=np.float32)
    for block in range(n_blocks):
        row_sel = block_idx == block
        if not row_sel.any():
            continue
        block_log = sub_log[row_sel]
        block_bg = sub_bg[row_sel]
        block_fraction = block_bg.mean(axis=0)
        good = good_cols_overall & (block_fraction >= COL_BG_FRACTION_BLOCK)
        if good.mean() < MIN_BLOCK_GOOD_COLUMNS:
            continue
        for channel in range(3):
            masked = np.where(block_bg[:, good], block_log[:, good, channel], np.nan)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", category=RuntimeWarning)
                median = np.nanmedian(masked, axis=0)
            deviation[block, good, channel] = median - _robust_baseline(t[good], median)

    usable = ~np.isnan(deviation[..., 0])  # (blocks, columns)
    corr_log = _correction_from_deviation(deviation, usable)

    n_cols = max(1, math.ceil(cross_len / PITCH_PX))
    pitch_positions = np.clip(np.arange(n_cols) * PITCH_PX, 0, n_cross - 1)
    corr_log_pitch = corr_log[:, pitch_positions, :]
    corr_val = corr_log_pitch / spans_arr[np.newaxis, np.newaxis, :]

    return RegionFit(
        window=(x, y, w, h, tilt_deg),
        axis=axis,
        block_px=BLOCK_PX,
        pitch_px=PITCH_PX,
        blocks=n_blocks,
        corr=corr_val.astype(np.float32),
    )


# --- params --------------------------------------------------------------


def deband_params(
    canvas: tuple[int, int],
    axis: str,
    fits: list[RegionFit],
    enabled: bool,
    strength: float,
) -> dict[str, Any]:
    """Build the ``deband`` op's JSON shape (plan §4). ``canvas`` is
    ``(width, height)``. Region ids are assigned sequentially starting at 1
    in list order — id stability across edits is a `library/repo.py`
    concern (step 2), out of scope for this module."""
    regions = []
    for region_id, fit in enumerate(fits, start=1):
        regions.append(
            {
                "id": region_id,
                "window": [round(float(v), 3) for v in fit.window],
                "block_px": int(fit.block_px),
                "pitch_px": int(fit.pitch_px),
                "blocks": int(fit.blocks),
                "corr": _encode_table(fit.corr),
            }
        )
    return {
        "fit_version": FIT_VERSION,
        "enabled": bool(enabled),
        "strength": float(strength),
        "axis": axis,
        "canvas": [int(canvas[0]), int(canvas[1])],
        "regions": regions,
    }


def is_live(params: dict | None, shape: tuple[int, int]) -> bool:
    """Whether the op should apply to an image of ``shape`` (H, W, ...).
    Never raises — a structurally malformed top-level dict is simply not
    live (plan §4: "anything else is a no-op, never an error")."""
    if not isinstance(params, dict):
        return False
    if params.get("fit_version") != FIT_VERSION:
        return False
    if not params.get("enabled"):
        return False
    canvas = params.get("canvas")
    try:
        if (
            not isinstance(canvas, (list, tuple))
            or len(canvas) != 2
            or (int(canvas[0]), int(canvas[1])) != (shape[1], shape[0])
        ):
            return False
    except (TypeError, ValueError):
        return False
    regions = params.get("regions")
    return isinstance(regions, list) and len(regions) > 0


def _params_enabled(params: dict | None) -> bool:
    if not isinstance(params, dict) or not params.get("enabled"):
        return False
    regions = params.get("regions")
    return isinstance(regions, list) and len(regions) > 0


def _parse_region_dict(
    region: dict, axis: str
) -> tuple[tuple[float, float, float, float, float], int, int, int, np.ndarray]:
    """Parse and decode one region dict. Raises ``ValueError`` with a clear
    message for anything malformed — a missing key, a wrong-shaped window,
    or a corr table that doesn't decode."""
    try:
        window_raw = region["window"]
        if len(window_raw) != 5:
            raise ValueError("window must have 5 elements")
        window = tuple(float(v) for v in window_raw)
        block_px = int(region["block_px"])
        pitch_px = int(region["pitch_px"])
        blocks = int(region["blocks"])
        if pitch_px <= 0 or blocks <= 0:
            raise ValueError("pitch_px and blocks must be positive")
        _band_len, cross_len = _band_cross_extent(window, axis)
        n_cols = max(1, math.ceil(cross_len / pitch_px))
        corr = _decode_table(region["corr"], blocks, n_cols)
    except ValueError:
        raise
    except Exception as exc:  # KeyError, binascii.Error, zlib.error, ...
        raise ValueError(f"malformed deband region: {exc}") from exc
    return window, block_px, pitch_px, blocks, corr


def _apply_one_region(
    image: np.ndarray,
    region: dict,
    *,
    axis: str,
    strength: float,
    row_origin: int,
    col_origin: int,
) -> None:
    window, block_px, pitch_px, _blocks, corr = _parse_region_dict(region, axis)
    x, y, w, h, tilt_deg = window
    x -= col_origin
    y -= row_origin
    cx, cy = x + (w - 1) / 2.0, y + (h - 1) / 2.0
    rotation = _rotation_matrix(tilt_deg)
    rotation_inv = rotation.T  # orthonormal

    band_len, cross_len = (h, w) if axis == "vertical" else (w, h)
    half_band, half_cross = band_len / 2.0, cross_len / 2.0

    corners_local = np.array(
        [
            [-w / 2.0, -h / 2.0],
            [w / 2.0, -h / 2.0],
            [w / 2.0, h / 2.0],
            [-w / 2.0, h / 2.0],
        ]
    )
    corners_global = corners_local @ rotation.T + np.array([cx, cy])
    min_x, min_y = corners_global.min(axis=0)
    max_x, max_y = corners_global.max(axis=0)

    height, width = image.shape[:2]
    bx0 = max(0, math.floor(min_x - REGION_MARGIN))
    by0 = max(0, math.floor(min_y - REGION_MARGIN))
    bx1 = min(width, math.ceil(max_x + REGION_MARGIN) + 1)
    by1 = min(height, math.ceil(max_y + REGION_MARGIN) + 1)
    if bx0 >= bx1 or by0 >= by1:
        return

    sub_image = image[by0:by1, bx0:bx1]
    sub_val = _decode_val(sub_image)

    dx = (np.arange(bx0, bx1, dtype=np.float64) - cx)[np.newaxis, :]
    dy = (np.arange(by0, by1, dtype=np.float64) - cy)[:, np.newaxis]
    u_local = rotation_inv[0, 0] * dx + rotation_inv[0, 1] * dy
    v_local = rotation_inv[1, 0] * dx + rotation_inv[1, 1] * dy
    va, ua = (v_local, u_local) if axis == "vertical" else (u_local, v_local)

    inside = (np.abs(va) <= half_band) & (np.abs(ua) <= half_cross)
    if not inside.any():
        return

    feather_band = min(FEATHER_PX, FEATHER_FRACTION * band_len)
    feather_cross = min(FEATHER_PX, FEATHER_FRACTION * cross_len)
    ramp_band = np.clip((half_band - np.abs(va)) / max(feather_band, 1e-6), 0.0, 1.0)
    ramp_cross = np.clip((half_cross - np.abs(ua)) / max(feather_cross, 1e-6), 0.0, 1.0)
    feather = ramp_band * ramp_cross
    feather[~inside] = 0.0
    if not np.any(feather > 0):
        return

    luminance = sub_val.mean(axis=2)
    blurred = cv2.GaussianBlur(
        luminance.astype(np.float32), (0, 0), sigmaX=PROTECT_BLUR_SIGMA
    )
    protect = np.exp(-(((luminance - blurred) / PROTECT_TOL) ** 2))

    fill = np.all(sub_image == FILL_CODE, axis=-1)

    n_blocks = corr.shape[0]
    n_cols = corr.shape[1]
    row_from_top = va + half_band
    # Table column i sits at pixel index i*pitch, whose local offset is
    # i*pitch - (cross_len - 1)/2 (the same convention `fit_region` samples at).
    col_from_left = ua + (cross_len - 1) / 2.0

    block_pos = row_from_top / block_px - 0.5
    block0 = np.clip(np.floor(block_pos), 0, n_blocks - 1).astype(np.int64)
    block1 = np.clip(block0 + 1, 0, n_blocks - 1)
    frac_block = np.clip(block_pos - block0, 0.0, 1.0)

    pitch_pos = col_from_left / pitch_px
    col0 = np.clip(np.floor(pitch_pos), 0, n_cols - 1).astype(np.int64)
    col1 = np.clip(col0 + 1, 0, n_cols - 1)
    frac_col = np.clip(pitch_pos - col0, 0.0, 1.0)

    c00 = corr[block0, col0]
    c01 = corr[block0, col1]
    c10 = corr[block1, col0]
    c11 = corr[block1, col1]
    fb = frac_block[..., np.newaxis]
    fc = frac_col[..., np.newaxis]
    corr_px = (
        c00 * (1 - fb) * (1 - fc)
        + c01 * (1 - fb) * fc
        + c10 * fb * (1 - fc)
        + c11 * fb * fc
    )

    delta = strength * feather[..., np.newaxis] * protect[..., np.newaxis] * corr_px
    touched = inside & ~fill
    delta[~touched] = 0.0
    corrected = np.clip(
        sub_val - delta,
        -normalization.NORMALIZED_HEADROOM_LOW,
        1.0 + normalization.NORMALIZED_HEADROOM_HIGH,
    )
    # Write back only the pixels the region owns; everything else in the
    # read window (the margin, fill) keeps its exact input codes.
    sub_image[touched] = normalization.encode_normalized(corrected[touched]).astype(
        np.uint16
    )


def apply(
    image_codes: np.ndarray,
    params: dict | None,
    *,
    region: tuple[int, int, int, int] | None = None,
    origin: tuple[int, int] | None = None,
) -> np.ndarray:
    """Apply the deband op (plan §3.2/§4). Mirrors `scratches.apply`'s
    signature and region-render contract: with ``region=(x, y, w, h)`` the
    caller has already read the ``REGION_MARGIN`` margin, and this returns
    the healed *unpadded* rect; ``origin`` is the top-left of
    ``image_codes`` in TIFF space, needed so a stored window maps onto a
    crop. A non-3-channel image, or (in whole-image mode) any way the op is
    not live, is a silent no-op — never an error. A malformed individual
    region entry (bad key, undecodable table) raises ``ValueError``."""
    image_codes = np.asarray(image_codes)
    if image_codes.ndim != 3 or image_codes.shape[2] != 3:
        return image_codes
    if region is None and not is_live(params, image_codes.shape[:2]):
        return image_codes
    if region is not None and not _params_enabled(params):
        return image_codes

    image = image_codes.copy()
    axis = params.get("axis", "vertical")
    strength = float(params.get("strength", 1.0))
    ox, oy = (0, 0) if origin is None else (int(origin[0]), int(origin[1]))

    for region_dict in params.get("regions", []):
        _apply_one_region(
            image,
            region_dict,
            axis=axis,
            strength=strength,
            row_origin=oy,
            col_origin=ox,
        )

    if region is not None:
        x, y, w, h = region
        return image[y : y + h, x : x + w]
    return image
