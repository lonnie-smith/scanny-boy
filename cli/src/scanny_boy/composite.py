"""Compositing: warp every frame into its own bounding box, reconcile
photometric mismatch between frames with per-frame per-channel gains, then
feather-blend in linear light and encode the finished canvas.

`MAX_OVERLAP_MAD` and `INTERPOLATION` are Chunk P2-1's measured constants,
approved at user gate C (section 3.12). Production code reads them from
here and from nowhere else. Note: `MAX_OVERLAP_MAD = 0.20` was measured
against *uncorrected* overlaps. Since gain compensation now runs before the
measurement, it gates the post-gain residual, and 0.20 is far looser than
the residual a healthy capture produces — the value must be re-measured at
the next user gate (see docs/DECISIONS.md, "Quality gates").

`MIN_GAIN_OVERLAP_PX` and `GAIN_DRIFT_WARN` are **provisional, unmeasured**
values pending a user gate: `MIN_GAIN_OVERLAP_PX` borrows the floor NegPy
measured for its own gain estimator; `GAIN_DRIFT_WARN` was chosen as the
smallest bound that never fires on healthy synthetic fixtures. Neither has
been measured from real scanny-boy scans.
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
    Rebate,
    analysis_grid_block_sizes,
    analyze_bounds,
    block_median_grid,
    detect_rebate,
    encode_normalized,
    headroom_clip_fractions,
    measure_anchor,
    measure_shadow_refs,
    measure_textural_range,
    normalize_log_image,
    observed_extrema,
    resolve_analysis_region,
    to_log_density,
)
from scanny_boy.registration import StitchError

FILL_COLOR: tuple[int, int, int] = (0, 0, 0)  # section 3.3: one constant, one place
MASK_ERODE_PX = 5  # Lanczos4 support radius 4, plus one
MAX_CANVAS_DIMENSION = 30_000  # warn above this
MAX_STITCHED_BYTES = int(3.5 * 1024**3)  # fail above this
MEMORY_SAFETY_FACTOR = 3.5  # section 3.8.1; measured, not padding

MAX_OVERLAP_MAD = 0.20
INTERPOLATION = cv2.INTER_LANCZOS4

FEATHER = "strip-axis"  # recorded in the roll manifest's stitch params
# Numerical guard, not a measured threshold: every covered pixel keeps a
# positive weight, the same invariant cv2.distanceTransform gave for free
# (it never returns less than 1.0 inside a mask).
_FEATHER_FLOOR = 1.0  # px

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
    # of pixels the encode's headroom clipped, and the rebate finding.
    bounds: Bounds
    shadow_refs: tuple[float, float, float]
    anchor: float
    textural_range: float
    observed_min: tuple[float, float, float]
    observed_max: tuple[float, float, float]
    headroom_clipped_highlights: tuple[float, float, float]
    headroom_clipped_shadows: tuple[float, float, float]
    rebate: Rebate


def estimate_peak_bytes(
    canvas_size: tuple[int, int],
    frame_size: tuple[int, int],
    frame_bbox_size: tuple[int, int],
    frame_count: int,
    *,
    geometry: bool = False,
    ca_maps: bool = False,
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

    The strip-axis feather (`_feather_weight`) needs two bbox-sized float32
    scratch buffers (the along-axis coordinate `s`, and the
    `minimum`/`maximum` temporary), live for one frame at a time — one
    additive term, not `frame_count` of them.
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
    warp_aux = bbox_pixels * 4 + bbox_pixels * 2  # feather weight + warped/eroded masks
    feather_scratch = bbox_pixels * 4 * 2  # strip-axis ramp's `s` + min/max temp

    geometry_bytes = 0
    if geometry:
        geometry_bytes += 3 * GEOMETRY_BAND_ROWS * bbox_width * 2 * 4
        if ca_maps:
            geometry_bytes += frame_pixels * 4
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


def _frame_bbox(
    matrix: np.ndarray, height: int, width: int, canvas_size: tuple[int, int]
) -> tuple[int, int, int, int]:
    """(x, y, width, height) of the frame's axis-aligned bounding box in
    canvas space, from its four corners transformed by `matrix`.

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
    rotation, translation = matrix[:, :2], matrix[:, 2]
    corners_local = np.array(
        [[0, 0], [width, 0], [width, height], [0, height]], dtype=np.float64
    )
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
    axis: tuple[float, float] | None,
) -> np.ndarray:
    """Blend weight for one warped frame, in its own bounding box.

    With a strip axis, weight ramps only along that axis: the distance from
    the nearer end of this frame's own along-axis extent, floored so a
    covered pixel always contributes. Constant across the strip, so the
    crossfade at the strip's long borders is identical to the crossfade
    down its middle — the isotropic distance transform's border collapse to
    50/50 is what smeared misregistration into a curve. Without an axis
    (a layout that is not a strip), falls back to the distance transform.
    """
    if axis is None:
        return cv2.distanceTransform(mask, cv2.DIST_L2, 5)
    ax, ay = axis
    height, width = mask.shape
    s = ((np.arange(width, dtype=np.float32) + bbox_x) * ax)[np.newaxis, :]
    s = s + ((np.arange(height, dtype=np.float32) + bbox_y) * ay)[:, np.newaxis]
    covered = mask > 0
    if not covered.any():
        return np.zeros(mask.shape, dtype=np.float32)
    s_min = float(s[covered].min())
    s_max = float(s[covered].max())
    weight = np.maximum(np.minimum(s - s_min, s_max - s), _FEATHER_FLOOR)
    weight[~covered] = 0.0
    return weight.astype(np.float32)


@dataclasses.dataclass
class _WarpedFrame:
    """One warped frame's residency between the warp pass and the
    accumulate pass: bounding-box sized, not canvas sized, so keeping all
    of them resident is cheap next to the two canvas-sized accumulators.
    Mutable: the solved per-frame gain is applied in place to `linear`."""

    x: int
    y: int
    width: int
    height: int
    linear: np.ndarray  # float32 (H, W, 3)
    mask: np.ndarray  # uint8 eroded validity mask
    weight: np.ndarray  # float32 feather weight
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
    geometry: dict,
    ca: dict | None,
) -> tuple[np.ndarray, np.ndarray]:
    """The composed band map of section 5.3: warp through distortion (and,
    in "maps" mode, the per-channel CA maps) with `cv2.remap`, a band of
    GEOMETRY_BAND_ROWS output rows at a time.

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

        # 1. bbox output px -> undistorted frame px (inverted bbox_matrix).
        du = uu - translation[0]
        dv = vv - translation[1]
        px = scaled_rotation_inv[0, 0] * du + scaled_rotation_inv[0, 1] * dv
        py = scaled_rotation_inv[1, 0] * du + scaled_rotation_inv[1, 1] * dv

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
    region: tuple[int, int, int, int] | None = None,
) -> CompositeResult:
    """load_frame(name) -> uint16 (H, W, 3). Called once per frame and the
    result released immediately, so the caller controls residency.

    `region` is the analysis region `(x, y, width, height)` in canvas
    pixels — the caller's `largest_valid_rect`, moved above the composite
    call so the meters can be told where the fill is not
    (docs/DECISIONS.md, "Normalization decisions"). It restricts the meters
    only; the canvas stays the full union bounding box and nothing
    captured is discarded.

    Warp pass, per frame:
      1. decode_to_linear -> float32.
      2. cv2.warpAffine into the frame's OWN bounding box (not the canvas)
         with INTERPOLATION, BORDER_CONSTANT, borderValue 0.
      3. np.clip(warped, 0.0, None) — section 2.3's measured -0.088
         undershoot.
      4. Warp a ones-mask with INTER_NEAREST; cv2.erode by MASK_ERODE_PX.
      5. weight = _feather_weight(mask, bbox_x, bbox_y, layout.strip_axis):
         a ramp along the strip axis when the layout has one, else the
         isotropic cv2.distanceTransform.
      6. Check `cancel` between frames.

    Nothing is accumulated during the warp pass: the photometric gain solve
    needs every used pair's overlap statistics, so every warped frame stays
    resident until all of them are warped (see estimate_peak_bytes). Then,
    with every warped frame in hand:

      * gather per-pair photometric stats (per-channel means over each used
        pair's shared valid area, rows below MIN_GAIN_OVERLAP_PX dropped)
        and the pre-gain overlap MAD;
      * solve_gains (geometric-mean-1 anchor, one solve per channel) and
        apply the gains in place to the warped linear float32 — never to
        encoded uint16, never to the composite canvas;
      * measure the post-gain overlap MAD — the residual the MAX_OVERLAP_MAD
        gate checks;
      * accumulate weight * rgb into the canvas accumulator and weight into
        the weight canvas, at each bounding box's offset, freeing each
        warped frame as it is consumed.

    Finally: divide where weight > 0, and — blending, warping and the gain
    solve having stayed in linear light, which is where they are
    physically correct (section 1.3) — fuse the normalization into the
    encode on the float32 accumulator that already exists:

      img_log    = to_log_density(result_linear)
      keep       = resolve_analysis_region(...); rebate detector excludes
                   the film rebate from it (section 3.13)
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

    for placement in layout.placements:
        cancel.raise_if_cancelled()

        frame = load_frame(placement.name)
        source_height, source_width = frame.shape[0], frame.shape[1]
        linear = decode_to_linear(frame).astype(np.float32)
        del frame

        matrix = placement.matrix()
        bbox_x, bbox_y, bbox_width, bbox_height = _frame_bbox(
            matrix, source_height, source_width, layout.canvas_size
        )
        bbox_matrix = matrix.copy()
        bbox_matrix[:, 2] -= (bbox_x, bbox_y)

        ones_mask = np.ones((source_height, source_width), dtype=np.uint8)
        if geometry is not None:
            # The composed band map (docs/GEOMETRIC_PLAN.md section 5.3):
            # distortion — and, in "maps" mode, the per-channel CA maps —
            # folded into the warp, one interpolation pass per pixel.
            warped, warped_mask = _warp_bands(
                linear,
                ones_mask,
                bbox_matrix,
                bbox_width,
                bbox_height,
                geometry,
                ca,
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

        pair_weight = _feather_weight(eroded_mask, bbox_x, bbox_y, layout.strip_axis)

        warped_by_name[placement.name] = _WarpedFrame(
            x=bbox_x,
            y=bbox_y,
            width=bbox_width,
            height=bbox_height,
            linear=warped,
            mask=eroded_mask,
            weight=pair_weight,
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
        warped_by_name[name].linear *= np.asarray(gains[name], dtype=np.float32)

    # The post-gain residual, over the same shared areas as above.
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
        overlap_mad[(pair.a, pair.b)] = _mean_level_mad(a_sub[shared], b_sub[shared])

    # Accumulate, freeing each warped frame as it is consumed.
    for placement in layout.placements:
        entry = warped_by_name.pop(placement.name)
        accum[entry.y : entry.y + entry.height, entry.x : entry.x + entry.width] += (
            entry.linear * entry.weight[:, :, np.newaxis]
        )
        weight_canvas[
            entry.y : entry.y + entry.height, entry.x : entry.x + entry.width
        ] += entry.weight

    covered = weight_canvas > 0
    result_linear = np.zeros_like(accum)
    result_linear[covered] = accum[covered] / weight_canvas[covered, np.newaxis]

    # The normalization pass, fused into the encode (section 1.3). One
    # uint16 code at a linear value of 0.008 is ~8.3e-4 in log10 density —
    # about 11.3 effective bits at the densest end, against a uniform 16
    # once the data is log-encoded.
    img_log = to_log_density(result_linear)
    del result_linear

    grid = block_median_grid(img_log)
    keep = _region_keep(grid.shape[:2], img_log.shape, region, covered)
    keep, rebate = detect_rebate(grid, keep)
    bounds = analyze_bounds(grid, keep)
    shadow_refs = measure_shadow_refs(grid, keep)
    anchor = measure_anchor(grid, keep)
    textural_range = measure_textural_range(grid, keep)
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

    fill_code = encode_normalized(
        np.full((1, 1, 3), NORMALIZED_FILL, dtype=np.float32)
    )[0, 0]
    encoded[~covered] = fill_code

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
        anchor=anchor,
        textural_range=textural_range,
        observed_min=observed_min,
        observed_max=observed_max,
        headroom_clipped_highlights=headroom_clipped_highlights,
        headroom_clipped_shadows=headroom_clipped_shadows,
        rebate=rebate,
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

    With no region known, the fallback restricts the meters to the blocks
    the blend actually covered — the same protection, from the one
    per-pixel fact the accumulator already knows. (Production always
    passes the caller's `largest_valid_rect`.)"""
    if region is None:
        covered_grid = block_median_grid(
            np.where(covered, np.float32(1.0), np.float32(0.0))
        )
        keep = covered_grid >= 1.0
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
    if keep.any():
        return keep
    # A rect that rounds away entirely: fall back outward, then to all.
    gx1 = int(np.ceil((x + width) / block_cols))
    gy1 = int(np.ceil((y + height) / block_rows))
    keep = resolve_analysis_region(grid_shape, (gx0, gy0, gx1 - gx0, gy1 - gy0))
    if keep.any():
        return keep
    return resolve_analysis_region(grid_shape, None)
