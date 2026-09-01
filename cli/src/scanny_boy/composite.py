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

from scanny_boy.linear import decode_to_linear, encode_from_linear
from scanny_boy.cancellation import CancellationToken
from scanny_boy.concurrency import physical_memory_bytes
from scanny_boy.events import Code
from scanny_boy.layout import GainStat, Layout, solve_gains
from scanny_boy.registration import StitchError

FILL_COLOR: tuple[int, int, int] = (0, 0, 0)  # section 3.3: one constant, one place
MASK_ERODE_PX = 5  # Lanczos4 support radius 4, plus one
MAX_CANVAS_DIMENSION = 30_000  # warn above this
MAX_STITCHED_BYTES = int(3.5 * 1024**3)  # fail above this
MEMORY_SAFETY_FACTOR = 3.5  # section 3.8.1; measured, not padding

MAX_OVERLAP_MAD = 0.20
INTERPOLATION = cv2.INTER_LANCZOS4

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
    image: np.ndarray  # uint16 (H, W, 3)
    gains: dict[str, tuple[float, float, float]]
    overlap_mad: dict[tuple[str, str], float]  # post-gain residual
    overlap_mad_pregain: dict[tuple[str, str], float]
    overlap_fraction: dict[tuple[str, str], float]
    coverage_fraction: float


def estimate_peak_bytes(
    canvas_size: tuple[int, int],
    frame_size: tuple[int, int],
    frame_bbox_size: tuple[int, int],
    frame_count: int,
) -> int:
    """Section 3.8's revised formula, exactly, including MEMORY_SAFETY_FACTOR.

    `frame_size` is (height, width) at full resolution — the revised formula
    needs it because the decoded source frame has to be resident for
    cv2.warpAffine and the original formula omitted it (section 3.8.1).
    `frame_count` is the negative's frame count: every warped frame stays
    resident until all frames are warped, because the pairwise photometric
    stats, the gain solve, and both overlap-MAD passes need any pair's two
    frames side by side.
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
    source = frame_pixels * 3 * 2 + frame_pixels * 3 * 4  # uint16 + linear decode
    warped = bbox_pixels * 3 * 4  # one warped frame
    warp_aux = bbox_pixels * 4 + bbox_pixels * 2  # feather weight + warped/eroded masks

    all_warped = frame_count * (warped + warp_aux)
    live_bytes = max(accum + weight + source + all_warped, accum + result)
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


def composite(layout: Layout, load_frame, *, cancel: CancellationToken, on_progress) -> CompositeResult:
    """load_frame(name) -> uint16 (H, W, 3). Called once per frame and the
    result released immediately, so the caller controls residency.

    Warp pass, per frame:
      1. decode_to_linear -> float32.
      2. cv2.warpAffine into the frame's OWN bounding box (not the canvas)
         with INTERPOLATION, BORDER_CONSTANT, borderValue 0.
      3. np.clip(warped, 0.0, None) — section 2.3's measured -0.088
         undershoot.
      4. Warp a ones-mask with INTER_NEAREST; cv2.erode by MASK_ERODE_PX.
      5. weight = cv2.distanceTransform(mask, cv2.DIST_L2, 5).
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

    Finally: divide where weight > 0; write FILL_COLOR elsewhere; encode
    with encode_from_linear.

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

        warped = cv2.warpAffine(
            linear,
            bbox_matrix,
            (bbox_width, bbox_height),
            flags=INTERPOLATION,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
        del linear
        warped = np.clip(warped, 0.0, None)

        ones_mask = np.ones((source_height, source_width), dtype=np.uint8)
        warped_mask = cv2.warpAffine(
            ones_mask,
            bbox_matrix,
            (bbox_width, bbox_height),
            flags=cv2.INTER_NEAREST,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
        # cv2.erode's default border treats "outside the array" as fully
        # covered, so it would not erode a frame's own corners — exactly
        # where the bounding box array's edge coincides with real content.
        # BORDER_CONSTANT/0 makes it treat the far side of every edge as
        # uncovered, which is what it actually is.
        eroded_mask = cv2.erode(
            warped_mask, _EROSION_KERNEL, borderType=cv2.BORDER_CONSTANT, borderValue=0
        )

        pair_weight = cv2.distanceTransform(eroded_mask, cv2.DIST_L2, 5)

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
        weight_canvas[entry.y : entry.y + entry.height, entry.x : entry.x + entry.width] += (
            entry.weight
        )

    covered = weight_canvas > 0
    result_linear = np.zeros_like(accum)
    result_linear[covered] = accum[covered] / weight_canvas[covered, np.newaxis]

    encoded = encode_from_linear(result_linear)
    encoded[~covered] = FILL_COLOR

    coverage_fraction = float(np.count_nonzero(covered)) / covered.size

    return CompositeResult(
        image=encoded,
        gains=gains,
        overlap_mad=overlap_mad,
        overlap_mad_pregain=overlap_mad_pregain,
        overlap_fraction=overlap_fraction,
        coverage_fraction=coverage_fraction,
    )
