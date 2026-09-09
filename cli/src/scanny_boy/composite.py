"""Compositing: warp every frame into its own bounding box, reconcile
photometric mismatch between frames with per-frame per-channel gains, then
feather-blend in linear light and encode the finished canvas.

`MAX_OVERLAP_MAD` and `INTERPOLATION` are Chunk P2-1's measured constants.
Production code reads them from here and from nowhere else. Note:
`MAX_OVERLAP_MAD = 0.20` was measured against *uncorrected* overlaps. Since
gain compensation now runs before the measurement, it gates the post-gain
residual, and 0.20 is looser than the residual a healthy capture produces
(see docs/DECISIONS.md, "Quality gates").

`MIN_GAIN_OVERLAP_PX` and `GAIN_DRIFT_WARN` are **provisional, unmeasured**
values: `MIN_GAIN_OVERLAP_PX` borrows the floor NegPy measured for its own
gain estimator; `GAIN_DRIFT_WARN` was chosen as the smallest bound that
never fires on healthy synthetic fixtures. Neither has been measured from
real scanny-boy scans.
"""

from __future__ import annotations

import dataclasses
import math

import cv2
import numpy as np

from scanny_boy.cancellation import CancellationToken
from scanny_boy.concurrency import physical_memory_bytes
from scanny_boy.events import Code
from scanny_boy.layout import GainStat, Layout, solve_gains
from scanny_boy.linear import decode_to_linear
from scanny_boy.normalization import (
    NORMALIZED_FILL,
    Bounds,
    DenseBorder,
    FilmExtent,
    FilmKind,
    Opaque,
    Rebate,
    analysis_grid_block_sizes,
    analyze_bounds,
    block_median_grid,
    clamp_bounds,
    collapse_to_mono,
    detect_rebate,
    encode_normalized,
    headroom_clip_fractions,
    measure_anchor,
    measure_highlight_refs,
    measure_neutral_residual,
    measure_shadow_refs,
    measure_textural_range,
    normalize_log_image,
    observed_extrema,
    rebate_insets_agreement,
    resolve_analysis_region,
    to_log_density,
    withhold_dense_border,
    withhold_non_film,
    withhold_opaque,
)
from scanny_boy.registration import Rectification, StitchError, rectify

FILL_COLOR: tuple[int, int, int] = (0, 0, 0)  # section 3.3: one constant, one place
MASK_ERODE_PX = 5  # Lanczos4 support radius 4, plus one
MAX_CANVAS_DIMENSION = 30_000  # warn above this
MAX_STITCHED_BYTES = int(3.5 * 1024**3)  # fail above this
MEMORY_SAFETY_FACTOR = 3.5  # section 3.8.1; measured, not padding

MAX_OVERLAP_MAD = 0.20
INTERPOLATION = cv2.INTER_LANCZOS4

FEATHER = "axis-separable"  # recorded in the roll manifest's stitch params
# The two-axis (grid) feather's floor, as a *fraction* of full weight: the
# separable product of the two axis ramps is dimensionless in [0, 1], so a
# px-valued floor cannot govern it. **Unmeasured starting value**
# (docs/GRID_STITCH_PLAN.md sections 2.4 and 5.1): chosen as the same order
# as the pre-grid strip floor's relative magnitude (1.0 px against a
# ~3000 px ramp is ~3e-4), recorded in `_stitch_params` as
# `feather_floor_fraction`, and revisited at the same user gate as the
# grid-pitch/alignment constants.
_FEATHER_FLOOR_FRACTION = 1e-3

# The exponent applied to the normalised ramp product, narrowing the
# crossfade to a band around the overlap midline
# (docs/NARROW_FEATHER.md section 1). **Unmeasured starting value**
# (section 6): 1 reproduces the pre-existing full-extent ramp exactly.
# Recorded in the roll manifest's stitch params as `feather_exponent`.
#
# Bounded to [1, 8]: the floor runs *before* the power (section 1.4), so the
# floored region's weight becomes `_FEATHER_FLOOR_FRACTION ** FEATHER_EXPONENT`.
# In float32, with `_FEATHER_FLOOR_FRACTION = 1e-3`, p=8 gives 1e-24 (a
# normal float32, comfortable); p=12 gives 1e-36 (normal, marginal); p=13
# gives 1e-39 (subnormal — the covered-implies-positive-weight invariant
# starts to erode). A larger exponent needs the floor redesigned first, not
# just a higher bound here.
FEATHER_EXPONENT = 4
assert isinstance(FEATHER_EXPONENT, int) and 1 <= FEATHER_EXPONENT <= 8

# Rows of output corrected per cv2.remap call when a profile's geometry is
# applied (docs/GEOMETRIC_PLAN.md section 5.3): the band map is generated
# closed-form a band at a time, so no frame-sized base map ever exists.
GEOMETRY_BAND_ROWS = 256

# Provisional, unmeasured — see the module docstring.
MIN_GAIN_OVERLAP_PX = 1000
GAIN_DRIFT_WARN = 0.05

_USABLE_MEMORY_FRACTION = 0.5  # section 3.8: "must not exceed half of physical RAM"
# A disk-shaped structuring element erodes a uniform margin regardless of
# the mask boundary's orientation; a repeated small square kernel erodes by
# Chebyshev (not Euclidean) distance and under-erodes a diagonal edge,
# which is exactly where a rotated frame's boundary sits.
_EROSION_KERNEL = cv2.getStructuringElement(
    cv2.MORPH_ELLIPSE, (2 * MASK_ERODE_PX + 1, 2 * MASK_ERODE_PX + 1)
)


@dataclasses.dataclass(frozen=True)
class CompositeResult:
    image: np.ndarray  # uint16 (H, W, 3), normalized log density (section 3.11)
    gains: dict[str, tuple[float, float, float]]
    overlap_mad: dict[tuple[str, str], float]  # post-gain residual
    overlap_mad_pregain: dict[tuple[str, str], float]
    overlap_fraction: dict[tuple[str, str], float]
    coverage_fraction: float
    # The normalization meters (docs/DECISIONS.md, "Normalization decisions"
    # 3.7, 3.13): per-negative bounds, the recorded-not-acted-on print-stage
    # statistics, the observed pre-clip extrema (section 3.6), the fraction
    # of pixels the encode's headroom clipped, and the rebate,
    # dense-border and opaque-holder findings.
    bounds: Bounds
    shadow_refs: tuple[float, float, float]
    # CAST_REMOVAL_PLAN R-1: the dense end's same-pixel neutral reference
    # (None when the band held no trustworthy neutrals) and the frame's
    # residual neutral offset — both recorded, read by nothing in the
    # stitch stage.
    highlight_refs: tuple[float, ...] | None
    neutral_residual: tuple[float, float] | None
    anchor: float
    textural_range: float
    observed_min: tuple[float, float, float]
    observed_max: tuple[float, float, float]
    headroom_clipped_highlights: tuple[float, float, float]
    headroom_clipped_shadows: tuple[float, float, float]
    rebate: Rebate
    dense_border: DenseBorder
    opaque: Opaque
    # BLACK_POINT_REFINEMENT: the film-extent pass's finding — where the
    # film's own extent was judged to sit and how far the meters' region
    # was inset inside it. Report-only until chunk E-3 applies the rect.
    film_extent: FilmExtent
    # Section 3.4's clamp: whether the roll-population safety net pulled the
    # bounds toward the run's reference population, and the bounds the
    # frame's own meters measured before it did.
    clamped: bool
    unclamped_bounds: Bounds | None


def estimate_peak_bytes(
    canvas_size: tuple[int, int],
    frame_size: tuple[int, int],
    frame_bbox_size: tuple[int, int],
    frame_count: int,
    *,
    geometry: bool = False,
    ca_maps: bool = False,
    rectification: bool = False,
) -> int:
    """Section 3.8's revised formula, exactly, including MEMORY_SAFETY_FACTOR.

    `frame_size` is (height, width) at full resolution — the revised formula
    needs it because the decoded source frame has to be resident for
    cv2.warpAffine and the original formula omitted it (section 3.8.1).
    `frame_count` is the negative's frame count: every warped frame stays
    resident until all frames are warped, because the pairwise photometric
    stats, the gain solve, and both overlap-MAD passes need any pair's two
    frames side by side.

    With a profile's geometry applied (docs/GEOMETRIC_PLAN.md section 5.3)
    the warp is a banded cv2.remap, which adds the band maps
    (`3 * GEOMETRY_BAND_ROWS * bbox_width * 2 * 4` — the worst case, three
    channels' maps in "maps" mode) and, in "maps" mode, one contiguous
    single-channel source view held during each remap
    (`frame_pixels * 4`). MEMORY_SAFETY_FACTOR is unchanged.

    A rig-tilt rectification (docs/RECTIFICATION_PLAN.md section 6) routes
    the warp through the same banded remap even without geometry, so with
    `rectification=True` and no geometry the band-map term applies too.
    With geometry already active there is no additional term: the maps are
    counted once either way. The per-worker budget is not re-measured, per
    the docs/STITCH_QUALITY_PLAN.md section 1.4 precedent.

    The feather (`_feather_weight`) needs bbox-sized float32 scratch — in
    the two-axis case three buffers (one per axis ramp plus the product),
    in the one-axis case two — live for one frame at a time in the
    accumulate pass, since the weight is computed lazily there rather than
    retained per frame (docs/GRID_STITCH_PLAN.md section 5.2): one additive
    term, not `frame_count` of them.
    """
    canvas_width, canvas_height = canvas_size
    frame_height, frame_width = frame_size
    bbox_height, bbox_width = frame_bbox_size

    canvas_pixels = canvas_width * canvas_height
    frame_pixels = frame_width * frame_height
    bbox_pixels = bbox_width * bbox_height

    accum = canvas_pixels * 3 * 4  # float32 RGB weighted sum
    weight = canvas_pixels * 4  # float32 weight sum
    result = canvas_pixels * 3 * 2  # uint16 encoded output
    # The normalization pass (docs/DECISIONS.md, "Normalization decisions"):
    # log density and the normalized image are both canvas-sized float32,
    # alive alongside the accumulators before the encode.
    log_density = canvas_pixels * 3 * 4
    normalized = canvas_pixels * 3 * 4
    source = frame_pixels * 3 * 2 + frame_pixels * 3 * 4  # uint16 + linear decode
    warped = bbox_pixels * 3 * 4  # one warped frame
    warp_aux = bbox_pixels * 2  # warped/eroded masks
    feather_scratch = bbox_pixels * 4 * 3  # two ramps + the product

    geometry_bytes = 0
    if geometry:
        geometry_bytes += 3 * GEOMETRY_BAND_ROWS * bbox_width * 2 * 4
        if ca_maps:
            geometry_bytes += frame_pixels * 4
    elif rectification:
        # Rectification without geometry still warps through the banded
        # remap; the band maps are the only geometry-shaped cost it adds.
        geometry_bytes += 3 * GEOMETRY_BAND_ROWS * bbox_width * 2 * 4
    elif ca_maps:
        # "maps" mode never occurs without geometry; kept for completeness.
        geometry_bytes += frame_pixels * 4

    all_warped = frame_count * (warped + warp_aux)
    live_bytes = max(
        accum + weight + source + all_warped + geometry_bytes + feather_scratch,
        accum + weight + log_density + normalized + result,
    )
    return math.ceil(live_bytes * MEMORY_SAFETY_FACTOR)


def check_memory_budget(peak_bytes: int) -> None:
    """Raises StitchError(INSUFFICIENT_MEMORY, ...) reporting both numbers
    when peak_bytes exceeds half of physical RAM."""
    total_memory = physical_memory_bytes()
    usable = int(total_memory * _USABLE_MEMORY_FRACTION)
    if peak_bytes > usable:
        raise StitchError(
            Code.INSUFFICIENT_MEMORY,
            f"compositing this negative needs an estimated {peak_bytes} "
            f"bytes at peak, which is more than half of this machine's "
            f"{total_memory} bytes of physical memory",
        )


def check_output_size(canvas_size: tuple[int, int], *, on_warning) -> None:
    """OUTPUT_DIMENSIONS_LARGE warning above MAX_CANVAS_DIMENSION;
    StitchError(STITCH_OUTPUT_TOO_LARGE) above MAX_STITCHED_BYTES."""
    canvas_width, canvas_height = canvas_size

    if canvas_width > MAX_CANVAS_DIMENSION:
        on_warning(
            Code.OUTPUT_DIMENSIONS_LARGE,
            f"canvas width {canvas_width}px exceeds {MAX_CANVAS_DIMENSION}px",
        )
    if canvas_height > MAX_CANVAS_DIMENSION:
        on_warning(
            Code.OUTPUT_DIMENSIONS_LARGE,
            f"canvas height {canvas_height}px exceeds {MAX_CANVAS_DIMENSION}px",
        )

    estimated_bytes = canvas_width * canvas_height * 3 * 2
    if estimated_bytes > MAX_STITCHED_BYTES:
        raise StitchError(
            Code.STITCH_OUTPUT_TOO_LARGE,
            f"estimated stitched file size {estimated_bytes} bytes exceeds "
            f"{MAX_STITCHED_BYTES} bytes",
        )


def frame_bbox(
    matrix: np.ndarray,
    height: int,
    width: int,
    canvas_size: tuple[int, int],
    rectification: Rectification | None = None,
) -> tuple[int, int, int, int]:
    """(x, y, width, height) of the frame's axis-aligned bounding box in
    canvas space, from its four corners transformed by `matrix`.

    With a rectification the corners first map through `W`: the frame's
    canvas footprint is the rectified keystone quad, not the affine image
    of the raw rectangle (docs/RECTIFICATION_PLAN.md section 6.2).

    `layout.py` computes the canvas size from the *aggregate* min/max
    corner across every frame (`ceil(global_max - global_min)`), while this
    function floors/ceils *this* frame's own corners independently. The two
    can disagree by a pixel at the canvas edge on floating-point rounding
    alone, so the result is clamped to the canvas bounds — never a true
    loss, since anything in that last pixel is inside MASK_ERODE_PX anyway.

    Under pincushion distortion the frame's true content corners pull
    inward by the corner-displacement amount (1-7 px at the magnitudes this
    plan expects, section 1.1), so the rect computed here is off by that
    much at the corners when a profile's geometry is applied. Accepted:
    `MASK_ERODE_PX` already discards a comparable margin, and complicating
    this function for it is not worth it.
    """
    canvas_width, canvas_height = canvas_size
    corners_local = np.array(
        [[0, 0], [width, 0], [width, height], [0, height]], dtype=np.float64
    )
    if rectification is not None:
        # The frame's canvas footprint is the rectified keystone quad, not
        # the affine image of the raw rectangle
        # (docs/RECTIFICATION_PLAN.md section 6.2) — `layout.frame_corners`
        # does the same for the canvas bounds and the valid rect.
        corners_local = rectify(corners_local, rectification)
    rotation, translation = matrix[:, :2], matrix[:, 2]
    corners_canvas = corners_local @ rotation.T + translation
    min_xy = corners_canvas.min(axis=0)
    max_xy = corners_canvas.max(axis=0)

    x = max(0, int(np.floor(min_xy[0])))
    y = max(0, int(np.floor(min_xy[1])))
    right = min(canvas_width, int(np.ceil(max_xy[0])))
    bottom = min(canvas_height, int(np.ceil(max_xy[1])))
    return x, y, right - x, bottom - y


def _feather_weight(
    mask: np.ndarray,
    bbox_x: int,
    bbox_y: int,
    axes: tuple[tuple[float, float], ...],
) -> np.ndarray:
    """Blend weight for one warped frame, in its own bounding box.

    `axes` is a tuple of one or two unit vectors. Along each, the weight
    ramps from the frame's own extent on that axis — distance from the
    nearer end — normalised by the axis's own `(s_max - s_min) / 2` so each
    per-axis ramp is dimensionless in [0, 1]. The returned weight is the
    *product* of the per-axis ramps, floored once at the end at
    `_FEATHER_FLOOR_FRACTION` and then raised to `FEATHER_EXPONENT`
    (docs/NARROW_FEATHER.md section 1.1), narrowing the crossfade to a band
    around the overlap midline without moving it (section 1.2). One axis is
    the strip case (docs/STITCH_QUALITY_PLAN.md section 1.3); two axes is a
    grid, where the ramp is separable, so a pixel's crossfade profile
    across a vertical seam is the same at the top of the canvas as in the
    middle, and likewise for horizontal seams. Empty `axes` (a layout that
    is neither) falls back to the distance transform, unpowered — it is
    isotropic, not a product of independent ramps, and out of this plan's
    scope.

    The floor **must** run before the power: flooring first keeps the
    floored region byte-identically the same set of pixels for every
    `FEATHER_EXPONENT` — the predicate is `product < _FEATHER_FLOOR_FRACTION`,
    which the power never sees. Powering first would floor wherever
    `product < _FEATHER_FLOOR_FRACTION ** (1 / FEATHER_EXPONENT)` instead, a
    much larger region (docs/NARROW_FEATHER.md section 1.4). The floor and
    the `weight[~covered] = 0.0` are applied to the *product*, not
    per-axis: a covered pixel keeps a positive weight, and a four-way
    corner does not land on `floor**2`.

    Before this plan, the one-axis path was kept pixel-valued and
    byte-identical to the pre-grid build's, and was not powered — a
    pixel-valued ramp cannot be powered safely (a 3000 px ramp at p=8 is
    6.5e27). It is now folded into this same normalised-and-powered
    formulation instead, changing strip output pixels: at `p = 1` the
    normalisation cancels in the ratio between two frames of equal extent,
    but the floor does not — the old floor was `max(ramp_px, 1.0)`, this
    one is effectively `max(ramp_px, 1e-3 * half_span)`, about
    `max(ramp_px, 3.0)` on a ~3000 px geometry. The difference is confined
    to a few-pixel sliver at a frame's along-axis extreme.
    """
    if not axes:
        return cv2.distanceTransform(mask, cv2.DIST_L2, 5)

    height, width = mask.shape
    covered = mask > 0
    if not covered.any():
        return np.zeros(mask.shape, dtype=np.float32)
    product = np.ones(mask.shape, dtype=np.float32)
    for axis in axes:
        ax, ay = axis
        s = ((np.arange(width, dtype=np.float32) + bbox_x) * ax)[
            np.newaxis, :
        ] + ((np.arange(height, dtype=np.float32) + bbox_y) * ay)[:, np.newaxis]
        s_min = float(s[covered].min())
        s_max = float(s[covered].max())
        half_span = (s_max - s_min) / 2.0
        if half_span <= 0:
            return np.zeros(mask.shape, dtype=np.float32)
        ramp = np.minimum(s - s_min, s_max - s) / half_span
        ramp[~covered] = 0.0
        product *= ramp.astype(np.float32)
    weight = np.maximum(product, _FEATHER_FLOOR_FRACTION)
    np.power(weight, FEATHER_EXPONENT, out=weight)
    weight[~covered] = 0.0
    return weight.astype(np.float32)


@dataclasses.dataclass
class _WarpedFrame:
    """One warped frame's residency between the warp pass and the
    accumulate pass: bounding-box sized, not canvas sized, so keeping all
    of them resident is cheap next to the two canvas-sized accumulators.

    Immutable-by-convention: the solved per-frame gain rides along as the
    `gain` scalar array and is folded into the accumulate pass's term, not
    multiplied through `linear` — multiplying a (possibly spilled) buffer
    in place would rewrite every page for nothing."""

    x: int
    y: int
    width: int
    height: int
    linear: np.ndarray  # float32 (H, W, 3)
    mask: np.ndarray  # uint8 eroded validity mask
    gain: np.ndarray  # float32 (3,) per-channel solved gain
    source_size: tuple[int, int]  # full-resolution (height, width)


def _pair_overlap(
    a: _WarpedFrame, b: _WarpedFrame
) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """Views of two warped frames' linear buffers over the intersection of
    their bounding boxes, plus the pair's shared valid mask — or None when
    the boxes do not intersect. Both overlap-MAD passes and the photometric
    stats gatherer measure over exactly this area."""
    ix0, iy0 = max(a.x, b.x), max(a.y, b.y)
    ix1, iy1 = min(a.x + a.width, b.x + b.width), min(a.y + a.height, b.y + b.height)
    if ix1 <= ix0 or iy1 <= iy0:
        return None
    a_sub = a.linear[iy0 - a.y : iy1 - a.y, ix0 - a.x : ix1 - a.x]
    b_sub = b.linear[iy0 - b.y : iy1 - b.y, ix0 - b.x : ix1 - b.x]
    a_mask_sub = a.mask[iy0 - a.y : iy1 - a.y, ix0 - a.x : ix1 - a.x]
    b_mask_sub = b.mask[iy0 - b.y : iy1 - b.y, ix0 - b.x : ix1 - b.x]
    return a_sub, b_sub, (a_mask_sub > 0) & (b_mask_sub > 0)


def _mean_level_mad(a_values: np.ndarray, b_values: np.ndarray) -> float:
    """Mean absolute difference between two frames' linear values over a
    shared area, divided by the mean level over that area."""
    mean_level = float(np.mean(np.concatenate([a_values, b_values])))
    mad = float(np.mean(np.abs(a_values - b_values)))
    return mad / mean_level if mean_level > 0 else 0.0


def _geometry_camera(geometry: dict) -> tuple[np.ndarray, np.ndarray]:
    """K and D for a section 3.2 geometry object: the coefficients are in
    the OpenCV forward convention, so they drop straight in."""
    K = np.array(
        [
            [geometry["fx"], 0.0, geometry["cx"]],
            [0.0, geometry["fy"], geometry["cy"]],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    D = np.array([geometry["k1"], geometry["k2"], 0.0, 0.0, 0.0])
    return K, D


def _warp_bands(
    linear: np.ndarray,
    ones_mask: np.ndarray,
    bbox_matrix: np.ndarray,
    bbox_width: int,
    bbox_height: int,
    geometry: dict | None,
    ca: dict | None,
    rectification: Rectification | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """The composed band map of section 5.3: warp through distortion (and,
    in "maps" mode, the per-channel CA maps) with `cv2.remap`, a band of
    GEOMETRY_BAND_ROWS output rows at a time.

    With a rig-tilt rectification (docs/RECTIFICATION_PLAN.md section 6.1),
    the placement lives in rectified space, so an inverse-rectification
    step sits between the affine inverse and the distortion steps:

        1.   q = R⁻¹ . ([u, v] - t) / s   # bbox output px -> rectified frame px
        1.5  p = centre + (q - centre) / (1 - l . (q - centre))
                                           # -> undistorted frame px
        2-5. unchanged

    Step 1.5 is closed form — one weight and one divide per band pixel, no
    new interpolation pass. With `geometry=None` steps 2-5 reduce to the
    identity and the map is `p` directly: a rectification can be active
    without a profile, and the banded remap serves both.

    The map is *forward* (undistorted -> distorted), which is closed form,
    so it is generated per band for nothing — no `initUndistortRectifyMap`,
    no cached frame-sized base map. In "scale" or no-CA mode the three
    channels share one map, so the 3-channel source is remapped once; in
    "maps" mode three maps are built per band and three single-channel
    sources are remapped. Either way: exactly one interpolation pass per
    output pixel. The validity mask is remapped with the green map at
    INTER_NEAREST; the caller erodes it.

    Returns `(warped_linear, warped_mask)` — clipping and erosion stay with
    the caller."""
    if geometry is not None:
        K, _ = _geometry_camera(geometry)
        fx, fy = K[0, 0], K[1, 1]
        cx, cy = K[0, 2], K[1, 2]
        k1, k2 = geometry["k1"], geometry["k2"]

    rotation = bbox_matrix[:, :2]
    translation = bbox_matrix[:, 2]
    # Inverse of the placement's scaled rotation block (docs/
    # STITCH_QUALITY_PLAN.md section 2: a frame's own matrix() is now
    # scale * R, not R), so this undoes both the rotation and the per-frame
    # scale in one step.
    scaled_rotation_inv = np.linalg.inv(rotation)

    centre = rectification.centre if rectification is not None else None
    l = rectification.l if rectification is not None else None

    ca_by_channel: dict[int, dict] = {}
    if ca is not None and ca.get("mode") == "maps":
        ca_by_channel = {0: ca["red"], 2: ca["blue"]}

    warped = np.zeros((bbox_height, bbox_width, 3), dtype=np.float32)
    warped_mask = np.zeros((bbox_height, bbox_width), dtype=np.uint8)
    for v0 in range(0, bbox_height, GEOMETRY_BAND_ROWS):
        v1 = min(v0 + GEOMETRY_BAND_ROWS, bbox_height)
        rows = np.arange(v0, v1, dtype=np.float64)
        cols = np.arange(bbox_width, dtype=np.float64)
        uu, vv = np.meshgrid(cols, rows)

        # 1. bbox output px -> rectified frame px (inverted bbox_matrix).
        du = uu - translation[0]
        dv = vv - translation[1]
        px = scaled_rotation_inv[0, 0] * du + scaled_rotation_inv[0, 1] * dv
        py = scaled_rotation_inv[1, 0] * du + scaled_rotation_inv[1, 1] * dv

        if rectification is not None:
            # 1.5. inverse rectification, rectified -> undistorted frame px
            # (docs/RECTIFICATION_PLAN.md section 6.1). Closed form; the
            # weight is bounded away from zero by the fit's excursion gate.
            qx = px - centre[0]
            qy = py - centre[1]
            w = 1.0 - l[0] * qx - l[1] * qy
            px = centre[0] + qx / w
            py = centre[1] + qy / w

        if geometry is None:
            # 6. no distortion to undo: the map is the source px directly.
            map_x = px.astype(np.float32)
            map_y = py.astype(np.float32)
            warped[v0:v1] = cv2.remap(
                linear,
                map_x,
                map_y,
                INTERPOLATION,
                borderMode=cv2.BORDER_CONSTANT,
                borderValue=0,
            )
            green_map = (map_x, map_y)
        else:
            # 2. normalise.
            x = (px - cx) / fx
            y = (py - cy) / fy

            if ca_by_channel:
                channel_maps = {}
                for channel in range(3):
                    fit = ca_by_channel.get(channel)
                    if fit is None:
                        xc, yc = x, y
                    else:
                        # 3. CA, "maps" mode only: scale about the channel's own
                        # centre, in normalised coordinates.
                        dx = x - fit["center_x"]
                        dy = y - fit["center_y"]
                        r = np.hypot(dx, dy)
                        s = fit["c0"] + fit["c1"] * r**2 + fit["c2"] * r**4
                        xc = fit["center_x"] + dx * s
                        yc = fit["center_y"] + dy * s
                    # 4-5. forward radial distortion, denormalise.
                    r2 = xc * xc + yc * yc
                    k = 1.0 + k1 * r2 + k2 * (r2 * r2)
                    channel_maps[channel] = (
                        (xc * k * fx + cx).astype(np.float32),
                        (yc * k * fy + cy).astype(np.float32),
                    )
                for channel in range(3):
                    map_x, map_y = channel_maps[channel]
                    # The map coordinates are absolute source-frame pixels, so
                    # remap reads the full source and writes the band. One
                    # contiguous single-channel view held at a time ("maps"
                    # mode's estimate_peak_bytes term).
                    source = np.ascontiguousarray(linear[:, :, channel])
                    warped[v0:v1, :, channel] = cv2.remap(
                        source,
                        map_x,
                        map_y,
                        INTERPOLATION,
                        borderMode=cv2.BORDER_CONSTANT,
                        borderValue=0,
                    )
                    del source
                # The validity mask is remapped with the green map.
                green_map = channel_maps[1]
            else:
                # 4-5. forward radial distortion, denormalise. Green, and every
                # channel in "scale" mode: the CA step is skipped.
                r2 = x * x + y * y
                k = 1.0 + k1 * r2 + k2 * (r2 * r2)
                map_x = (x * k * fx + cx).astype(np.float32)
                map_y = (y * k * fy + cy).astype(np.float32)
                # 6. one interpolation pass for all three channels.
                warped[v0:v1] = cv2.remap(
                    linear,
                    map_x,
                    map_y,
                    INTERPOLATION,
                    borderMode=cv2.BORDER_CONSTANT,
                    borderValue=0,
                )
                green_map = (map_x, map_y)

        warped_mask[v0:v1] = cv2.remap(
            ones_mask,
            green_map[0],
            green_map[1],
            cv2.INTER_NEAREST,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
    return warped, warped_mask


def composite(
    layout: Layout,
    load_frame,
    *,
    cancel: CancellationToken,
    on_progress,
    geometry: dict | None = None,
    ca: dict | None = None,
    rectification: Rectification | None = None,
    region: tuple[int, int, int, int] | None = None,
    reference_bounds: list[Bounds] | None = None,
    base_refs: tuple[float, ...] | None = None,
    film_kind: FilmKind = FilmKind.COLOUR,
) -> CompositeResult:
    """load_frame(name) -> uint16 (H, W, 3). Called once per frame and the
    result released immediately, so the caller controls residency.

    `region` is the analysis region `(x, y, width, height)` in canvas
    pixels — the caller's `largest_valid_rect`, moved above the composite
    call so the meters can be told where the fill is not
    (docs/DECISIONS.md, "Normalization decisions"). It restricts the meters
    only; the canvas stays the full union bounding box and nothing
    captured is discarded.

    `rectification`, when given, is the negative's fitted rig-tilt
    rectification (docs/RECTIFICATION_PLAN.md section 6): the placements
    live in rectified space, so the warp undoes the rectification per
    output pixel through the banded remap — with or without a profile's
    geometry. The plain cv2.warpAffine path runs only when neither is
    present.

    Warp pass, per frame:
      1. decode_to_linear -> float32.
      2. cv2.warpAffine into the frame's OWN bounding box (not the canvas)
         with INTERPOLATION, BORDER_CONSTANT, borderValue 0 — or, when
         geometry or rectification is present, the composed banded remap.
      3. np.clip(warped, 0.0, None) — section 2.3's measured -0.088
         undershoot.
      4. Warp a ones-mask with INTER_NEAREST; cv2.erode by MASK_ERODE_PX.
      5. weight = _feather_weight(mask, bbox_x, bbox_y, axes): the
         separable product of the layout's feather axes'
         `Layout.feather_axes()` ramps when it has any (one axis is the
         strip case), else the isotropic cv2.distanceTransform. The weight
         is computed lazily in the accumulate pass — it is read nowhere
         else, so it is not retained per frame.
      6. Check `cancel` between frames.

    Nothing is accumulated during the warp pass: the photometric gain solve
    needs every used pair's overlap statistics, so every warped frame stays
    resident until all of them are warped (see estimate_peak_bytes). Then,
    with every warped frame in hand:

      * gather per-pair photometric stats (per-channel means over each used
        pair's shared valid area, rows below MIN_GAIN_OVERLAP_PX dropped)
        and the pre-gain overlap MAD;
      * solve_gains (geometric-mean-1 anchor, one solve per channel) and
        carry the gains as a per-frame scalar array — never multiplied
        through into the warped buffers, and never applied to encoded
        uint16, never to the composite canvas;
      * measure the post-gain overlap MAD — the residual the MAX_OVERLAP_MAD
        gate checks;
      * accumulate gain * weight * rgb into the canvas accumulator and
        weight into the weight canvas, at each bounding box's offset,
        freeing each warped frame as it is consumed. The feather weight is
        computed here, once per frame, rather than held since the warp
        pass.

    Finally: divide where weight > 0, and — blending, warping and the gain
    solve having stayed in linear light, which is where they are
    physically correct (section 1.3) — fuse the normalization into the
    encode on the float32 accumulator that already exists:

      img_log    = to_log_density(result_linear)
      img_log    = collapse_to_mono(img_log, covered)  # mono roll only
                   (MONOCHROME_PLAN section 3: after the log, before the
                   bounds — averaging in linear light would weight by
                   intensity, not density, and biases toward the film
                   base; after the bounds, `analyze_bounds`' colour axis
                   would solve for an orange mask that is not there)
      keep       = resolve_analysis_region(...); opaque gate, film-extent
                   pass and rebate detector refine it (section 3.13,
                   docs/BLACK_POINT_REFINEMENT.md)
      bounds     = analyze_bounds(keep)
      normalized = normalize_log_image(img_log, bounds)
      encoded    = encode_normalized(normalized)

    The published image is normalized log density (section 3.11), a
    working intermediate — not the deliverable. Uncovered canvas pixels
    take `encode_normalized(NORMALIZED_FILL)` — code 65535 (section 3.14);
    `FILL_COLOR` survives as the linear-era record only.

    overlap_mad for a pair is the mean absolute difference between the two
    frames' linear values over their shared valid area, divided by the mean
    level over that area, measured *after* gain compensation;
    overlap_mad_pregain is the same measurement taken before it — the
    diagnostic that explains why a gain was applied.
    """
    canvas_width, canvas_height = layout.canvas_size
    accum = np.zeros((canvas_height, canvas_width, 3), dtype=np.float32)
    weight_canvas = np.zeros((canvas_height, canvas_width), dtype=np.float32)

    warped_by_name: dict[str, _WarpedFrame] = {}
    feather_axes = layout.feather_axes()

    for placement in layout.placements:
        cancel.raise_if_cancelled()

        frame = load_frame(placement.name)
        source_height, source_width = frame.shape[0], frame.shape[1]
        linear = decode_to_linear(frame).astype(np.float32)
        del frame

        matrix = placement.matrix()
        bbox_x, bbox_y, bbox_width, bbox_height = frame_bbox(
            matrix, source_height, source_width, layout.canvas_size, rectification
        )
        bbox_matrix = matrix.copy()
        bbox_matrix[:, 2] -= (bbox_x, bbox_y)

        ones_mask = np.ones((source_height, source_width), dtype=np.uint8)
        if geometry is not None or rectification is not None:
            # The composed band map (docs/GEOMETRIC_PLAN.md section 5.3;
            # docs/RECTIFICATION_PLAN.md section 6): distortion, CA, and the
            # inverse rectification folded into the warp, one interpolation
            # pass per pixel.
            warped, warped_mask = _warp_bands(
                linear,
                ones_mask,
                bbox_matrix,
                bbox_width,
                bbox_height,
                geometry,
                ca,
                rectification,
            )
            warped = np.clip(warped, 0.0, None)
        else:
            warped = cv2.warpAffine(
                linear,
                bbox_matrix,
                (bbox_width, bbox_height),
                flags=INTERPOLATION,
                borderMode=cv2.BORDER_CONSTANT,
                borderValue=0,
            )
            warped = np.clip(warped, 0.0, None)

            warped_mask = cv2.warpAffine(
                ones_mask,
                bbox_matrix,
                (bbox_width, bbox_height),
                flags=cv2.INTER_NEAREST,
                borderMode=cv2.BORDER_CONSTANT,
                borderValue=0,
            )
        del linear
        # cv2.erode's default border treats "outside the array" as fully
        # covered, so it would not erode a frame's own corners — exactly
        # where the bounding box array's edge coincides with real content.
        # BORDER_CONSTANT/0 makes it treat the far side of every edge as
        # uncovered, which is what it actually is.
        eroded_mask = cv2.erode(
            warped_mask, _EROSION_KERNEL, borderType=cv2.BORDER_CONSTANT, borderValue=0
        )

        warped_by_name[placement.name] = _WarpedFrame(
            x=bbox_x,
            y=bbox_y,
            width=bbox_width,
            height=bbox_height,
            linear=warped,
            mask=eroded_mask,
            gain=np.ones(3, dtype=np.float32),
            source_size=(source_height, source_width),
        )

        on_progress()

    # Pairwise photometric statistics and the pre-gain overlap MAD, over
    # each used pair's shared valid area.
    overlap_mad_pregain: dict[tuple[str, str], float] = {}
    overlap_fraction: dict[tuple[str, str], float] = {}
    stats: list[GainStat] = []
    for pair in layout.used_pairs:
        a_frame = warped_by_name.get(pair.a)
        b_frame = warped_by_name.get(pair.b)
        if a_frame is None or b_frame is None:
            continue
        overlap = _pair_overlap(a_frame, b_frame)
        if overlap is None:
            continue
        a_sub, b_sub, shared = overlap
        shared_count = int(np.count_nonzero(shared))

        overlap_fraction[(pair.a, pair.b)] = shared_count / (
            a_frame.source_size[0] * a_frame.source_size[1]
        )
        if shared_count == 0:
            continue

        a_values = a_sub[shared]
        b_values = b_sub[shared]
        overlap_mad_pregain[(pair.a, pair.b)] = _mean_level_mad(a_values, b_values)

        if shared_count >= MIN_GAIN_OVERLAP_PX:
            mean_a = a_values.mean(axis=0)
            mean_b = b_values.mean(axis=0)
            stats.append(
                GainStat(
                    a=pair.a,
                    b=pair.b,
                    mean_a=(float(mean_a[0]), float(mean_a[1]), float(mean_a[2])),
                    mean_b=(float(mean_b[0]), float(mean_b[1]), float(mean_b[2])),
                    shared_count=shared_count,
                )
            )

    names = [placement.name for placement in layout.placements]
    gains = solve_gains(names, stats)
    for name in names:
        warped_by_name[name].gain = np.asarray(gains[name], dtype=np.float32)

    # The post-gain residual, over the same shared areas as above. The gain
    # is applied lazily to the overlap slice only — select `[shared]`
    # first, then multiply, so the whole overlap rect is never
    # materialised.
    overlap_mad: dict[tuple[str, str], float] = {}
    for pair in layout.used_pairs:
        a_frame = warped_by_name.get(pair.a)
        b_frame = warped_by_name.get(pair.b)
        if a_frame is None or b_frame is None:
            continue
        overlap = _pair_overlap(a_frame, b_frame)
        if overlap is None:
            continue
        a_sub, b_sub, shared = overlap
        if not shared.any():
            continue
        overlap_mad[(pair.a, pair.b)] = _mean_level_mad(
            a_sub[shared] * a_frame.gain, b_sub[shared] * b_frame.gain
        )

    # Accumulate, freeing each warped frame as it is consumed. The feather
    # weight is computed here — it is read nowhere else, so it is not held
    # since the warp pass — and the per-frame gain folds into the term.
    for placement in layout.placements:
        entry = warped_by_name.pop(placement.name)
        weight = _feather_weight(entry.mask, entry.x, entry.y, feather_axes)
        accum[entry.y : entry.y + entry.height, entry.x : entry.x + entry.width] += (
            entry.linear * entry.gain * weight[:, :, np.newaxis]
        )
        weight_canvas[
            entry.y : entry.y + entry.height, entry.x : entry.x + entry.width
        ] += weight

    covered = weight_canvas > 0
    result_linear = np.zeros_like(accum)
    result_linear[covered] = accum[covered] / weight_canvas[covered, np.newaxis]

    # The normalization pass, fused into the encode (section 1.3). One
    # uint16 code at a linear value of 0.008 is ~8.3e-4 in log10 density —
    # about 11.3 effective bits at the densest end, against a uniform 16
    # once the data is log-encoded.
    img_log = to_log_density(result_linear)
    del result_linear
    if film_kind is FilmKind.MONOCHROME:
        img_log = collapse_to_mono(img_log, covered)

    grid = block_median_grid(img_log)
    keep = _region_keep(grid.shape[:2], img_log.shape, region, covered)
    # The opaque-holder gate runs first: the holder owns every dense-end
    # percentile it touches, so leaving it in blinds `withhold_dense_border`
    # (its P0.1 anchor lands inside the holder) as well as pinning
    # `analyze_bounds`' floor. Before `detect_rebate` too, so its own
    # thin-end anchor still reads the film base.
    keep, opaque = withhold_opaque(grid, keep)
    keep_before_non_film = keep
    # The film-extent pass (docs/BLACK_POINT_REFINEMENT.md): locate the
    # negative carrier's incursion and inset the analysis rect inside it.
    # E-3: the returned keep applies — this is the line that moves
    # published pixels.
    keep, film_extent = withhold_non_film(grid, keep)
    keep_before_rebate = keep
    keep, rebate = detect_rebate(grid, keep)
    # §5.3's cross-check, recorded and read by nothing: the rebate mask is
    # what the detector withheld from the region it saw; the agreement is
    # measured against the region before the film-extent rect.
    film_extent = dataclasses.replace(
        film_extent,
        rebate_agrees=rebate_insets_agreement(
            keep_before_rebate & ~keep, keep_before_non_film, film_extent.insets
        ),
    )
    keep, dense_border = withhold_dense_border(grid, keep)
    bounds = analyze_bounds(grid, keep, base_refs)
    shadow_refs = measure_shadow_refs(grid, keep)
    # CAST_REMOVAL_PLAN R-1: the dense end's neutral reference, beside the
    # other meters. `None` is recorded as null — it is load-bearing
    # information (the plan's §0.4), not an error.
    highlight_refs = measure_highlight_refs(grid, keep, base_refs)
    anchor = measure_anchor(grid, keep)
    textural_range = measure_textural_range(grid, keep)

    # Section 3.4's clamp: a frame whose own meters latched contamination
    # the per-frame detectors missed is pulled back toward the roll's
    # population, from references the already-composited negatives
    # contribute. With a fixed roll anchor (REBATE_ANCHORING §4), every
    # negative's ceils deviations become nearly identical, so the
    # population MAD collapses toward zero. CLAMP_MIN_WINDOW floors the
    # window, so the clamp stays inert on legitimate exposure variation —
    # do not "fix" the now-tiny MAD.
    unclamped_bounds: Bounds | None = None
    clamped = False
    if reference_bounds:
        clamped_bounds, clamped = clamp_bounds(bounds, reference_bounds)
        if clamped:
            unclamped_bounds = bounds
            bounds = clamped_bounds

    # CAST_REMOVAL_PLAN R-1: the residual is measured against the *clamped*
    # bounds — the ones the published pixels are actually stretched by —
    # which is why `del grid, keep` waits until here.
    neutral_residual = measure_neutral_residual(grid, keep, bounds)
    del grid, keep

    normalized = normalize_log_image(img_log, bounds)
    del img_log
    # The observed extrema and headroom clipping are picture statistics:
    # measured over the covered pixels only, never the fill (section 3.6).
    observed_min, observed_max = observed_extrema(normalized[covered])
    headroom_clipped_highlights, headroom_clipped_shadows = headroom_clip_fractions(
        normalized[covered]
    )
    encoded = encode_normalized(normalized)
    del normalized

    # Sized to the published channel count: three on a colour roll, one on
    # a mono roll's collapsed image (MONOCHROME_PLAN section 4).
    fill_code = encode_normalized(
        np.full((1, 1, encoded.shape[-1]), NORMALIZED_FILL, dtype=np.float32)
    )[0, 0]
    encoded[~covered] = fill_code
    if encoded.shape[-1] == 1:
        # MONOCHROME_PLAN §4 (tiff_writer.py:81's `photometric` site, and
        # `write_stitched_tiff`'s own shape check): the published mono TIFF
        # is a true 2-D array, not a (H, W, 1) one — the collapse point
        # keeps a trailing channel axis throughout the meters because every
        # generalised function reads `shape[-1]`, but nothing downstream of
        # this function expects one.
        encoded = encoded[..., 0]

    coverage_fraction = float(np.count_nonzero(covered)) / covered.size

    return CompositeResult(
        image=encoded,
        gains=gains,
        overlap_mad=overlap_mad,
        overlap_mad_pregain=overlap_mad_pregain,
        overlap_fraction=overlap_fraction,
        coverage_fraction=coverage_fraction,
        bounds=bounds,
        shadow_refs=shadow_refs,
        highlight_refs=highlight_refs,
        neutral_residual=neutral_residual,
        anchor=anchor,
        textural_range=textural_range,
        observed_min=observed_min,
        observed_max=observed_max,
        headroom_clipped_highlights=headroom_clipped_highlights,
        headroom_clipped_shadows=headroom_clipped_shadows,
        rebate=rebate,
        dense_border=dense_border,
        opaque=opaque,
        film_extent=film_extent,
        clamped=clamped,
        unclamped_bounds=unclamped_bounds,
    )


def _region_keep(
    grid_shape: tuple[int, int],
    canvas_shape: tuple[int, ...],
    region: tuple[int, int, int, int] | None,
    covered: np.ndarray,
) -> np.ndarray:
    """Map a canvas-space `(x, y, width, height)` analysis region onto the
    prefiltered grid, rounding *inward* so no uncovered-canvas cell ever
    leaks into the meters (section 1.5: the fill would otherwise drag the
    floor percentile to log10(1e-6) = -6.0 and garbage the whole stretch).

    Inward rounding is not enough on its own: the blend's `covered` mask
    can hold interior holes the layout's `largest_valid_rect` never saw,
    so every candidate region is intersected with the blocks the blend
    actually covered.

    With no region known, the fallback restricts the meters to those same
    covered blocks — the same protection, from the one per-pixel fact the
    accumulator already knows. (Production always passes the caller's
    `largest_valid_rect`.)"""
    if region is None:
        keep = _intersect_with_coverage(
            resolve_analysis_region(grid_shape, None), canvas_shape, covered
        )
        if keep.any():
            return keep
        return resolve_analysis_region(grid_shape, None)
    block_rows, block_cols = analysis_grid_block_sizes(canvas_shape)
    x, y, width, height = (float(v) for v in region)
    gx0 = int(np.ceil(x / block_cols))
    gy0 = int(np.ceil(y / block_rows))
    gx1 = int(np.floor((x + width) / block_cols))
    gy1 = int(np.floor((y + height) / block_rows))
    grid_rect = (gx0, gy0, gx1 - gx0, gy1 - gy0)
    keep = resolve_analysis_region(grid_shape, grid_rect)
    keep = _intersect_with_coverage(keep, canvas_shape, covered)
    if keep.any():
        return keep
    # A rect that rounds away entirely: fall back outward, then to the
    # covered blocks alone — never to the unfiltered grid, which would
    # meter the fill again.
    gx1 = int(np.ceil((x + width) / block_cols))
    gy1 = int(np.ceil((y + height) / block_rows))
    keep = resolve_analysis_region(grid_shape, (gx0, gy0, gx1 - gx0, gy1 - gy0))
    keep = _intersect_with_coverage(keep, canvas_shape, covered)
    if keep.any():
        return keep
    keep = _intersect_with_coverage(
        resolve_analysis_region(grid_shape, None), canvas_shape, covered
    )
    if keep.any():
        return keep
    return resolve_analysis_region(grid_shape, None)


def _intersect_with_coverage(
    keep: np.ndarray,
    canvas_shape: tuple[int, ...],
    covered: np.ndarray,
) -> np.ndarray:
    """Withhold every block the blend did not mostly cover, even inside the
    caller's valid rect: the rect comes from the layout's coverage, and the
    blend's `covered` can hold interior holes the layout never saw — a hole
    would meter the fill (linear 0, log -6.0) and garbage the floor
    percentile (docs/DECISIONS.md, "Normalization decisions").

    "Mostly", not "fully", because the test is `block_median_grid` over the
    coverage indicator: a block survives while covered pixels are its
    majority. That is the right threshold rather than a concession — it is
    exactly the condition under which the *image* cell's median is drawn
    from covered pixels too, so the fill never reaches the meters either
    way. It matters more than it used to: with `ANALYSIS_BLOCK_PX` pinned a
    hole's edge no longer lands on a block boundary by construction, so
    straddling blocks are the common case rather than the absent one."""
    covered_grid = block_median_grid(
        np.where(covered, np.float32(1.0), np.float32(0.0))
    )
    return keep & (covered_grid >= 1.0)
