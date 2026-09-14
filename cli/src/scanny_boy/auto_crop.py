"""Auto-crop: fit the largest picture-only window to the film format ratio.

The detector runs on a display image (the published pixels with some
geometric transform already applied and no crop). It outputs an axis-aligned
rect in that image's pixels, or a refusal when it cannot find a trustworthy
picture boundary.

It is called in two places:
- At stitch time, seeding a ``crop`` op when the roll's Auto-crop setting
  is ticked (``stitch_pipeline.py``).
- From the ``edit suggest-crop`` CLI command, backing the Auto button in
  the app's crop mode.

The module follows the ``auto_rotate.py`` precedent: it never touches the
published TIFF, refuses rather than guesses, and keeps every constant in
one place.
"""

from __future__ import annotations

import dataclasses

import cv2
import numpy as np

from scanny_boy.normalization import NORMALIZED_FILL, decode_normalized

# The estimation runs on a bounded analysis copy, like auto_rotate.
ANALYSIS_MAX_EDGE = 1024
# Of the short side of the picture rect, applied inward after the fit:
# absorbs analysis-scale rounding, the feathered blend edge, and the soft
# rebate/picture transition.
AUTO_CROP_SAFETY_FRACTION = 0.005
# Non-picture pixels tolerated inside a candidate window, as a fraction of
# its area: dust and specks the morphological clean missed must not shrink
# the crop by a whole speck-width.
AUTO_CROP_MAX_INTRUSION = 0.0005
# Refuse when the picture mask covers less than this fraction of the
# covered canvas: detection failed, or this is not a normal frame.
AUTO_CROP_MIN_PICTURE_FRACTION = 0.25
# Refuse when the fitted window is smaller than this fraction of the picture
# rect's area: a ragged mask, a light leak read as rebate, or the wrong
# format for this film.
AUTO_CROP_MIN_FILL_FRACTION = 0.60
# The seeded angle is rounded to this many degrees.
ANGLE_PRECISION_DEG = 0.01

# The format ratio table: key is the ``FilmFormat`` raw value (and the
# ``CropPreset`` raw value), value is the landscape width : height ratio.
FORMAT_RATIOS: dict[str, float] = {
    "half-frame": 18.0 / 24.0,
    "35mm": 36.0 / 24.0,
    "6x3": 56.0 / 28.0,
    "645": 56.0 / 41.5,
    "6x6": 1.0,
    "6x7": 56.0 / 69.5,
    "xpan": 65.0 / 24.0,
    "6x9": 56.0 / 84.0,
    "6x12": 112.0 / 56.0,
    "6x17": 168.0 / 56.0,
}

AUTO_CROP_VERSION = 1


@dataclasses.dataclass(frozen=True)
class AutoCrop:
    """An axis-aligned window (x, y, w, h) in the full-resolution display
    image the caller described, and the numbers that justified it."""

    rect: tuple[int, int, int, int]
    ratio: float | None
    picture_fraction: float
    fill_fraction: float


@dataclasses.dataclass(frozen=True)
class Refusal:
    """Why no crop was produced. ``reason`` is a stable token for the app
    and the evidence block: ``"unknown_format"``, ``"no_coverage"``,
    ``"little_picture"``, ``"no_fit"``, ``"ragged"``, ``"too_small"``."""

    reason: str
    picture_fraction: float | None = None
    fill_fraction: float | None = None


def analysis_display(
    image: np.ndarray,
    *,
    quarter_turns: int = 0,
    flipped: bool = False,
    fine_angle_deg: float = 0.0,
) -> tuple[np.ndarray, float]:
    """Build a downscaled display copy of ``image`` (the encoded uint16
    normalized-density composite).  Downscale first, then flip, apply
    ``rotate_with_fill``, and apply quarter turns — the same order as
    ``previews._display_image``.

    Returns ``(analysis, scale)`` where ``analysis`` is the encoded uint16
    display image at analysis resolution and ``scale`` is the linear
    scale factor from the full-size display to the analysis copy.
    """
    from scanny_boy.auto_rotate import rotate_with_fill

    image = np.asarray(image)
    if image.ndim == 2:
        image = np.stack([image] * 3, axis=-1)
    height, width = image.shape[0], image.shape[1]

    scale = min(1.0, ANALYSIS_MAX_EDGE / max(height, width))
    if scale < 1.0:
        small = cv2.resize(
            image,
            (round(width * scale), round(height * scale)),
            interpolation=cv2.INTER_AREA,
        )
    else:
        small = image
        scale = 1.0

    if flipped:
        small = small[:, ::-1]

    if abs(fine_angle_deg) > 1e-9:
        small = rotate_with_fill(small, fine_angle_deg)

    for _ in range(quarter_turns % 4):
        small = np.rot90(small)

    return small, scale


def picture_mask(normalized: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Identify fill, rebate, and picture in a normalized-density image.

    Returns ``(covered, rebate)`` where ``covered`` is True for every
    non-fill pixel and ``rebate`` is True for rebate/base pixels within
    the covered area.

    This is factored out of ``auto_rotate.estimate_rotation`` so rotation
    and crop can never disagree about what rebate is.
    """
    # The fill is exactly the sentinel code, all channels; it is canvas,
    # not film.
    fill = np.all(normalized >= NORMALIZED_FILL - 1e-6, axis=-1)
    covered = ~fill
    if not covered.any():
        return covered, np.zeros_like(covered, dtype=bool)

    # Thin in *every* channel is the rebate: base is the thinnest thing
    # on the film, and a scene shadow dense in one channel is not base.
    from scanny_boy.auto_rotate import REBATE_SLACK

    thinness = normalized.min(axis=-1)
    thin_values = thinness[covered]
    anchor = float(np.percentile(thin_values, 99.5))
    rebate = covered & (thinness >= anchor - REBATE_SLACK)

    return covered, rebate


def exclusion_hint(
    normalization: dict | None,
    tiff_size: tuple[int, int],
    transform: dict,
    scale: float,
    analysis_size: tuple[int, int],
) -> tuple[int, int, int, int] | None:
    """Convert the recorded ``film_extent`` insets into an analysis-pixel
    exclusion rect, or ``None`` when there is nothing to exclude.

    The hint is meter-deep (it includes ``FILM_EXTENT_MARGIN_CELLS``
    and the convergence travel), so it is a fallback, never the primary
    signal.
    """
    if normalization is None:
        return None
    film_extent = normalization.get("film_extent")
    if film_extent is None or not film_extent.get("detected"):
        return None
    insets = film_extent.get("insets")
    if insets is None:
        return None

    # film_extent insets are (top, bottom, left, right) in grid cells.
    # analysis_rect is (y, x) origin of the analysis region in TIFF pixels.
    # The full TIFF size is tiff_size (height, width).
    # ANALYSIS_BLOCK_PX converts cells to pixels.
    from scanny_boy.normalization import ANALYSIS_BLOCK_PX

    analysis_rect = normalization.get("analysis_rect")
    if analysis_rect is None:
        return None
    ar_y, ar_x = int(analysis_rect[0]), int(analysis_rect[1])
    _ar_h, _ar_w = analysis_size
    tiff_h, tiff_w = tiff_size

    top = int(insets[0]) * ANALYSIS_BLOCK_PX
    bottom = int(insets[1]) * ANALYSIS_BLOCK_PX
    left = int(insets[2]) * ANALYSIS_BLOCK_PX
    right = int(insets[3]) * ANALYSIS_BLOCK_PX

    # The exclusion rect in TIFF pixels (inner rect).
    tiff_x1 = max(0, left)
    tiff_y1 = max(0, top)
    tiff_x2 = min(tiff_w, tiff_w - right)
    tiff_y2 = min(tiff_h, tiff_h - bottom)

    # Map to analysis pixels: subtract the analysis rect origin, then
    # scale to the analysis copy's coordinate space.
    inv_scale = 1.0 / scale if scale > 0 else 1.0
    analysis_w = analysis_size[1]
    analysis_h = analysis_size[0]

    # Convert TIFF rect to full-display space (same as TIFF at scale=1,
    # before any quarter turns — the exclusion hint is measured without
    # display transforms since the carrier is in TIFF space).
    # Then scale to analysis pixels.
    ax1 = max(0, min(analysis_w, int((tiff_x1 - ar_x * ANALYSIS_BLOCK_PX) * inv_scale)))
    ay1 = max(0, min(analysis_h, int((tiff_y1 - ar_y * ANALYSIS_BLOCK_PX) * inv_scale)))
    ax2 = max(0, min(analysis_w, int((tiff_x2 - ar_x * ANALYSIS_BLOCK_PX) * inv_scale)))
    ay2 = max(0, min(analysis_h, int((tiff_y2 - ar_y * ANALYSIS_BLOCK_PX) * inv_scale)))

    if ax1 >= ax2 or ay1 >= ay2:
        return None

    return (ax1, ay1, ax2 - ax1, ay2 - ay1)


def estimate_crop(
    analysis: np.ndarray,
    *,
    full_size: tuple[int, int],
    ratio: float | None,
    exclude: tuple[int, int, int, int] | None = None,
) -> AutoCrop | Refusal:
    """Estimate the largest picture-only crop window in the display image.

    Parameters
    ----------
    analysis : np.ndarray
        Encoded uint16 display image at analysis resolution (downscaled).
    full_size : tuple[int, int]
        (height, width) of the full-resolution display image.
    ratio : float | None
        Landscape width : height ratio, or ``None`` for unconstrained.
    exclude : tuple[int, int, int, int] | None
        Carrier hint (x, y, w, h) in analysis pixels.

    Returns
    -------
    AutoCrop | Refusal
        The crop rect in full-resolution display pixels, or a refusal.
    """
    from scanny_boy.auto_rotate import (
        SCENE_CLEAN_FRACTION,
        SCENE_MIN_AREA_FRACTION,
    )
    from scanny_boy.library.repo import CROP_MIN_SIZE_PX

    analysis = np.asarray(analysis)
    if analysis.ndim == 2:
        analysis = np.stack([analysis] * 3, axis=-1)
    analysis_height, analysis_width = analysis.shape[:2]

    # Step 1: Decode to normalized density.
    normalized = decode_normalized(analysis).astype(np.float32)

    # Step 2: Mask out thin material: fill, bare light and rebate.
    covered, rebate = picture_mask(normalized)

    if not covered.any():
        return Refusal(reason="no_coverage")

    picture = covered & ~rebate

    # Step 3: Mask out the dense carrier.
    # The exclude hint is a fallback, never the primary signal.
    if exclude is not None:
        ex, ey, ew, eh = exclude
        x1 = max(0, min(ex, analysis_width))
        y1 = max(0, min(ey, analysis_height))
        x2 = max(0, min(ex + ew, analysis_width))
        y2 = max(0, min(ey + eh, analysis_height))
        picture[y1:y2, x1:x2] = False

    # Step 4: Clean the mask and find the picture rect.
    scene = picture.astype(np.uint8)
    kernel_side = max(
        3, round(SCENE_CLEAN_FRACTION * min(analysis_height, analysis_width))
    )
    if kernel_side % 2 == 0:
        kernel_side += 1
    kernel = np.ones((kernel_side, kernel_side), np.uint8)
    scene = cv2.morphologyEx(scene, cv2.MORPH_OPEN, kernel)
    scene = cv2.morphologyEx(scene, cv2.MORPH_CLOSE, kernel)

    # Keep only the components big enough to be picture.
    count, labels, stats, _ = cv2.connectedComponentsWithStats(scene, 8)
    big = np.zeros_like(scene)
    for label_idx in range(1, count):
        if stats[label_idx, cv2.CC_STAT_AREA] >= SCENE_MIN_AREA_FRACTION * covered.size:
            big[labels == label_idx] = 1
    if not big.any():
        return Refusal(
            reason="little_picture",
            picture_fraction=0.0,
            fill_fraction=0.0,
        )

    picture_fraction = float(np.count_nonzero(big)) / float(covered.size)

    if picture_fraction < AUTO_CROP_MIN_PICTURE_FRACTION:
        return Refusal(
            reason="little_picture",
            picture_fraction=picture_fraction,
            fill_fraction=0.0,
        )

    # Step 5: Fit the largest window inside the mask.
    # Build the axis-aligned bounding box of the picture.
    points = cv2.findNonZero(big)
    if points is None:
        return Refusal(reason="no_fit")
    bbox = cv2.boundingRect(points)
    bbox_x, bbox_y, bbox_w, bbox_h = bbox

    if ratio is not None:
        # Orient the ratio to the picture rect's long axis.
        picture_ratio = bbox_w / max(bbox_h, 1)
        oriented = ratio if picture_ratio >= 1 else 1.0 / ratio

        # Binary search for the largest feasible height.
        # Build a summed-area table of non-picture pixels.
        non_picture = (1 - big).astype(np.float32)
        sat = cv2.integral(non_picture)

        best = None
        lo, hi = 1, bbox_h
        while lo <= hi:
            mid = (lo + hi) // 2
            w = round(mid * oriented)
            if w < 1 or w > bbox_w:
                if mid * oriented > bbox_w:
                    hi = mid - 1
                else:
                    lo = mid + 1
                continue

            # Check all origins where a w x mid window fits inside bbox.
            # Use the SAT for O(1) per-origin intrusion count.
            max_intrusion = AUTO_CROP_MAX_INTRUSION * w * mid
            feasible: list[tuple[int, int, int, int]] = []
            for oy in range(bbox_y, bbox_y + bbox_h - mid + 1):
                for ox in range(bbox_x, bbox_x + bbox_w - w + 1):
                    # Sum of non-picture pixels in (ox, oy) to (ox+w, oy+mid).
                    intrusion = (
                        sat[oy + mid, ox + w]
                        - sat[oy, ox + w]
                        - sat[oy + mid, ox]
                        + sat[oy, ox]
                    )
                    if intrusion <= max_intrusion:
                        feasible.append((ox, oy, w, mid))

            if feasible:
                # Choose the origin closest to the bbox center.
                cx = bbox_x + bbox_w / 2.0
                cy = bbox_y + bbox_h / 2.0
                best = min(
                    feasible,
                    key=lambda r: (
                        (r[0] + r[2] / 2.0 - cx) ** 2 + (r[1] + r[3] / 2.0 - cy) ** 2
                    ),
                )
                lo = mid + 1
            else:
                hi = mid - 1

        if best is None:
            return Refusal(
                reason="no_fit",
                picture_fraction=picture_fraction,
                fill_fraction=0.0,
            )

        rect = best
    else:
        # Unconstrained: maximal-area axis-aligned rectangle.
        # Use the histogram-stack algorithm on the picture mask after
        # the intrusion tolerance is folded in by a 1-px erosion of
        # isolated specks.
        eroded = cv2.erode(big, np.ones((3, 3), np.uint8))
        # The eroded mask may shrink edges; use the original big for
        # the area calculation but the eroded for the maximal rect search.
        rect = _maximal_rect(eroded)

    rx, ry, rw, rh = rect
    fill_fraction = float(np.count_nonzero(big[ry : ry + rh, rx : rx + rw])) / float(
        rw * rh
    )

    if fill_fraction < AUTO_CROP_MIN_FILL_FRACTION:
        return Refusal(
            reason="ragged",
            picture_fraction=picture_fraction,
            fill_fraction=fill_fraction,
        )

    # Step 6: Shrink and scale to full size.
    # Inset by safety fraction of the short side.
    inset = max(1, round(AUTO_CROP_SAFETY_FRACTION * min(rw, rh)))
    rx2 = rx + inset
    ry2 = ry + inset
    rw2 = max(rw - 2 * inset, 1)
    rh2 = max(rh - 2 * inset, 1)

    # Scale to full-resolution display pixels.
    inv_scale = 1.0 / max(1e-9, analysis.shape[1] / full_size[1])
    fx = int(rx2 * inv_scale)
    fy = int(ry2 * inv_scale)
    fw = int((rx2 + rw2) * inv_scale) - fx
    fh = int((ry2 + rh2) * inv_scale) - fy

    # Clamp to full image.
    fx = max(0, min(fx, full_size[1] - 1))
    fy = max(0, min(fy, full_size[0] - 1))
    fw = min(fw, full_size[1] - fx)
    fh = min(fh, full_size[0] - fy)

    if fw < CROP_MIN_SIZE_PX or fh < CROP_MIN_SIZE_PX:
        return Refusal(
            reason="too_small",
            picture_fraction=picture_fraction,
            fill_fraction=fill_fraction,
        )

    return AutoCrop(
        rect=(fx, fy, fw, fh),
        ratio=ratio,
        picture_fraction=picture_fraction,
        fill_fraction=fill_fraction,
    )


def _maximal_rect(mask: np.ndarray) -> tuple[int, int, int, int]:
    """Find the largest axis-aligned rectangle containing only 1-bits
    in a binary mask, using the histogram-stack algorithm. O(N)."""
    height, width = mask.shape
    if height == 0 or width == 0:
        return (0, 0, 0, 0)

    # heights[j] = number of consecutive 1-bits ending at row i, column j.
    heights = np.zeros(width, dtype=int)
    best_area = 0
    best_rect = (0, 0, 0, 0)

    for i in range(height):
        heights = np.where(mask[i] == 1, heights + 1, 0)
        # Monotonic stack for maximal rectangle in histogram.
        stack: list[int] = []
        for j in range(width + 1):
            h = int(heights[j]) if j < width else 0
            while stack and heights[stack[-1]] > h:
                height_val = int(heights[stack.pop()])
                left = stack[-1] + 1 if stack else 0
                w = j - left
                area = height_val * w
                if area > best_area:
                    best_area = area
                    best_rect = (left, i - height_val + 1, w, height_val)
            stack.append(j)

    return best_rect


def build_params() -> dict:
    """Record the detector's version in the roll manifest's stitch params."""
    return {"version": AUTO_CROP_VERSION}
