"""Compositing: warp every frame into its own bounding box, feather-blend
in linear light, and encode the finished canvas.

`MAX_OVERLAP_MAD` and `INTERPOLATION` are Chunk P2-1's measured constants,
approved at user gate C (section 3.12). Production code reads them from
here and from nowhere else.
"""

from __future__ import annotations

import dataclasses
import math

import cv2
import numpy as np

from scanny_boy import romm
from scanny_boy.cancellation import CancellationToken
from scanny_boy.concurrency import physical_memory_bytes
from scanny_boy.events import Code
from scanny_boy.layout import Layout
from scanny_boy.registration import StitchError

FILL_COLOR: tuple[int, int, int] = (0, 0, 0)  # section 3.3: one constant, one place
MASK_ERODE_PX = 5  # Lanczos4 support radius 4, plus one
MAX_CANVAS_DIMENSION = 30_000  # warn above this
MAX_STITCHED_BYTES = int(3.5 * 1024**3)  # fail above this
MEMORY_SAFETY_FACTOR = 3.5  # section 3.8.1; measured, not padding

MAX_OVERLAP_MAD = 0.20
INTERPOLATION = cv2.INTER_LANCZOS4

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
    overlap_mad: dict[tuple[str, str], float]
    overlap_fraction: dict[tuple[str, str], float]
    coverage_fraction: float


def estimate_peak_bytes(
    canvas_size: tuple[int, int],
    frame_size: tuple[int, int],
    frame_bbox_size: tuple[int, int],
) -> int:
    """Section 3.8's revised formula, exactly, including MEMORY_SAFETY_FACTOR.

    `frame_size` is (height, width) at full resolution — the revised formula
    needs it because the decoded source frame has to be resident for
    cv2.warpAffine and the original formula omitted it (section 3.8.1).
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

    per_frame = max(source, warped + warp_aux)
    live_bytes = max(accum + weight + per_frame, accum + result)
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


def composite(layout: Layout, load_frame, *, cancel: CancellationToken, on_progress) -> CompositeResult:
    """load_frame(name) -> uint16 (H, W, 3). Called once per frame and the
    result released immediately, so the caller controls residency.

    Per frame:
      1. romm.decode_to_linear -> float32.
      2. cv2.warpAffine into the frame's OWN bounding box (not the canvas)
         with INTERPOLATION, BORDER_CONSTANT, borderValue 0.
      3. np.clip(warped, 0.0, None) — section 2.3's measured -0.088
         undershoot.
      4. Warp a ones-mask with INTER_NEAREST; cv2.erode by MASK_ERODE_PX.
      5. weight = cv2.distanceTransform(mask, cv2.DIST_L2, 5).
      6. Accumulate weight * rgb into the canvas accumulator and weight into
         the weight canvas, at the bounding box's offset.
      7. Check `cancel` between frames.

    Then: divide where weight > 0; write FILL_COLOR elsewhere; encode with
    romm.encode_from_linear.

    overlap_mad for a pair is the mean absolute difference between the two
    frames' linear values over their shared valid area, divided by the mean
    level over that area. Compute it while both warped frames are in hand.
    """
    canvas_width, canvas_height = layout.canvas_size
    accum = np.zeros((canvas_height, canvas_width, 3), dtype=np.float32)
    weight_canvas = np.zeros((canvas_height, canvas_width), dtype=np.float32)

    # Kept for the pairwise overlap_mad/overlap_fraction pass below, once
    # every frame has been warped: (x, y, width, height, warped linear rgb,
    # eroded mask, full-resolution (height, width)). These are bounding-box
    # sized, not canvas sized, so keeping all of them resident is cheap next
    # to the two canvas-sized accumulators above.
    warped_by_name: dict[str, tuple] = {}

    for placement in layout.placements:
        cancel.raise_if_cancelled()

        frame = load_frame(placement.name)
        source_height, source_width = frame.shape[0], frame.shape[1]
        linear = romm.decode_to_linear(frame).astype(np.float32)
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

        accum[bbox_y : bbox_y + bbox_height, bbox_x : bbox_x + bbox_width] += (
            warped * pair_weight[:, :, np.newaxis]
        )
        weight_canvas[bbox_y : bbox_y + bbox_height, bbox_x : bbox_x + bbox_width] += (
            pair_weight
        )

        warped_by_name[placement.name] = (
            bbox_x,
            bbox_y,
            bbox_width,
            bbox_height,
            warped,
            eroded_mask,
            (source_height, source_width),
        )

        on_progress()

    covered = weight_canvas > 0
    result_linear = np.zeros_like(accum)
    result_linear[covered] = accum[covered] / weight_canvas[covered, np.newaxis]

    encoded = romm.encode_from_linear(result_linear)
    encoded[~covered] = FILL_COLOR

    coverage_fraction = float(np.count_nonzero(covered)) / covered.size

    overlap_mad: dict[tuple[str, str], float] = {}
    overlap_fraction: dict[tuple[str, str], float] = {}
    for pair in layout.used_pairs:
        a_data = warped_by_name.get(pair.a)
        b_data = warped_by_name.get(pair.b)
        if a_data is None or b_data is None:
            continue
        ax, ay, aw, ah, a_linear, a_mask, a_source_size = a_data
        bx, by, bw, bh, b_linear, b_mask, _ = b_data

        ix0, iy0 = max(ax, bx), max(ay, by)
        ix1, iy1 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
        if ix1 <= ix0 or iy1 <= iy0:
            continue

        a_sub_linear = a_linear[iy0 - ay : iy1 - ay, ix0 - ax : ix1 - ax]
        a_sub_mask = a_mask[iy0 - ay : iy1 - ay, ix0 - ax : ix1 - ax]
        b_sub_linear = b_linear[iy0 - by : iy1 - by, ix0 - bx : ix1 - bx]
        b_sub_mask = b_mask[iy0 - by : iy1 - by, ix0 - bx : ix1 - bx]

        shared = (a_sub_mask > 0) & (b_sub_mask > 0)
        shared_count = int(np.count_nonzero(shared))

        a_frame_height, a_frame_width = a_source_size
        overlap_fraction[(pair.a, pair.b)] = shared_count / (a_frame_height * a_frame_width)

        if shared_count == 0:
            continue

        a_values = a_sub_linear[shared]
        b_values = b_sub_linear[shared]
        mean_level = float(np.mean(np.concatenate([a_values, b_values])))
        mad = float(np.mean(np.abs(a_values - b_values)))
        overlap_mad[(pair.a, pair.b)] = mad / mean_level if mean_level > 0 else 0.0

    return CompositeResult(
        image=encoded,
        overlap_mad=overlap_mad,
        overlap_fraction=overlap_fraction,
        coverage_fraction=coverage_fraction,
    )
