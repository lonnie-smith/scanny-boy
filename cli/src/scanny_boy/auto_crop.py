"""Auto-crop: the largest picture-only window at the film format's ratio.

A stitched canvas carries more than the picture: the wedges of empty fill a
tilted stitch or rotation leaves, the film's rebate and sprocket holes, bare
light past the film's edge, and the negative holder. The crop a person draws
by hand drags a format-shaped rect in until none of that remains. This
module finds that rect — and like `auto_rotate`, never touches the published
TIFF: the result is a nondestructive `crop` ops-log entry (`repo.CROP_OP`,
params tagged `"source": "auto"`), seeded at stitch time and re-runnable from
crop mode's **Auto** button (`edit suggest-crop`).

**The detector works on a display image** — the published pixels with some
geometric transform already applied and no crop — and returns a rect in that
image's pixels. It knows nothing about ops logs. Each caller builds its own
display image with `analysis_display` and maps the result back to the op's
published-TIFF space with `previews.display_crop_window_to_tiff`, the same
function the app uses to re-enter crop mode, so the rect lands exactly where
the detector put it.

**What counts as picture.** Everything the blend covered that is neither
thin material (`auto_rotate.picture_mask`: the rebate, sprocket holes and
bare light, all at or above the thin anchor) nor dense carrier (a
border-connected, featureless band denser than the film can be). The
recorded `film_extent` insets are the carrier fallback only: they are
meter-deep, so they apply on an edge only where the dense pass found nothing.
Edge printing (frame numbers, DX marks) sits inside the rebate band and is
removed by the rectangle fit, not the mask.

**The fit.** The largest rect of the format's ratio lying entirely inside
the cleaned picture mask, found by binary search over the window height with
a summed-area table — every origin tested at once — then shrunk by a small
safety margin. It refuses rather than guesses (`Refusal`): no coverage, too
little picture, no window that fits, a window too small for the picture it
was cut from (a ragged mask, or the wrong format for this film), or a window
below the crop size floor.

Every constant below is provisional until the measurement gate
(docs/AUTO_CROP_PLAN.md §9) pins it.
"""

from __future__ import annotations

import dataclasses
import math

import cv2
import numpy as np

from scanny_boy import previews
from scanny_boy.auto_rotate import (
    SCENE_CLEAN_FRACTION,
    SCENE_MIN_AREA_FRACTION,
    picture_mask,
    rotate_with_fill,
)
from scanny_boy.library import repo
from scanny_boy.normalization import analysis_grid_block_sizes, decode_normalized

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
# Dense-border pass: a border-connected component at or below this normalized
# level — at or past the dense-end anchor, where the meters never let scene
# content sit — and flatter than AUTO_CROP_MAX_DENSE_SPREAD is carrier.
AUTO_CROP_DENSE_LEVEL = 0.0
# Standard deviation, in normalized units, a carrier band may vary by.
AUTO_CROP_MAX_DENSE_SPREAD = 0.05
# Of the covered canvas; a smaller dense component is not a band.
AUTO_CROP_DENSE_MIN_AREA_FRACTION = 0.002
# A carrier band spans at least this fraction of the analysis image along
# the edge it borders; a compact dense blob touching the border is not one.
AUTO_CROP_DENSE_MIN_SPAN_FRACTION = 0.5

# The crop preset label stored on the op is the film-format string itself
# (`FilmFormat` / `CropPreset` raw values); the value is the gate's real
# width : height, not the nominal one. Orientation is never taken from the
# table — the detector orients to the picture's long axis. Swift's
# `CropPreset` carries the same table; each side hard-codes the literal
# table in CONTRACT.md in its tests (see `cli.py`'s `--format` choices).
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

# `Refusal.reason` tokens: stable, read by the app and the evidence block.
REASONS = (
    "no_coverage",
    "little_picture",
    "no_fit",
    "ragged",
    "too_small",
)


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
    """Why no crop was produced. `reason` is one of `REASONS`."""

    reason: str
    picture_fraction: float | None = None
    fill_fraction: float | None = None


def build_params() -> dict:
    """The detector's constants, for `stitch_params` — a non-invariant entry
    with the same posture as the params auto-rotate records: what a
    measurement (docs/AUTO_CROP_PLAN.md §9) would revise, read by nothing."""
    return {
        "version": AUTO_CROP_VERSION,
        "analysis_max_edge": ANALYSIS_MAX_EDGE,
        "safety_fraction": AUTO_CROP_SAFETY_FRACTION,
        "max_intrusion": AUTO_CROP_MAX_INTRUSION,
        "min_picture_fraction": AUTO_CROP_MIN_PICTURE_FRACTION,
        "min_fill_fraction": AUTO_CROP_MIN_FILL_FRACTION,
        "dense_level": AUTO_CROP_DENSE_LEVEL,
        "max_dense_spread": AUTO_CROP_MAX_DENSE_SPREAD,
        "dense_min_area_fraction": AUTO_CROP_DENSE_MIN_AREA_FRACTION,
        "dense_min_span_fraction": AUTO_CROP_DENSE_MIN_SPAN_FRACTION,
        "format_ratios": dict(FORMAT_RATIOS),
    }


def analysis_display(
    image: np.ndarray,
    *,
    quarter_turns: int = 0,
    flipped: bool = False,
    fine_angle_deg: float = 0.0,
) -> tuple[np.ndarray, float]:
    """The downscaled display copy of `image` (encoded uint16 normalized
    density) for `estimate_crop`, and the linear scale from the full-size
    image to it.

    Downscales **first**, then mirrors, applies `rotate_with_fill`, and
    applies the quarter turns — `previews._display_image`'s order, so the
    warp is cheap and the two agree on geometry."""
    image = np.asarray(image)
    if image.ndim == 2:
        image = np.stack([image] * 3, axis=-1)
    height, width = image.shape[0], image.shape[1]
    scale = min(1.0, ANALYSIS_MAX_EDGE / max(height, width))
    if scale < 1.0:
        image = cv2.resize(
            image,
            (round(width * scale), round(height * scale)),
            interpolation=cv2.INTER_AREA,
        )
    if flipped:
        image = np.ascontiguousarray(image[:, ::-1])
    if abs(fine_angle_deg) >= 1e-9:
        image = rotate_with_fill(image, fine_angle_deg)
    if quarter_turns % 4:
        # np.rot90 turns counter-clockwise; the count is net clockwise
        # quarter turns.
        image = np.ascontiguousarray(np.rot90(image, k=(-quarter_turns) % 4))
    return image, scale


def exclusion_hint(
    normalization: dict | None,
    tiff_size: tuple[int, int],  # (height, width)
    *,
    quarter_turns: int,
    flipped: bool,
    fine_angle_deg: float,
    analysis_size: tuple[int, int],  # (height, width) of the analysis copy
) -> tuple[int, int, int, int] | None:
    """The recorded `film_extent` inset as the inner window `(x, y, w, h)`
    the picture must lie inside, in analysis pixels of the display image —
    or `None` when the stitch found no carrier.

    The insets (grid cells, top/bottom/left/right, relative to the analysis
    region) become an inner TIFF rect; an edge with no inset is pushed out
    of the canvas so it constrains nothing. The rect's corners go forward
    through the same display transform the analysis copy got, and the
    axis-aligned rect inscribed in the result is scaled to analysis pixels
    (conservative under a fine rotation: it can only exclude more).

    The hint is meter-deep — it includes `FILM_EXTENT_MARGIN_CELLS` and the
    convergence loop's travel — so `estimate_crop` applies it only on an edge
    where the dense pass found nothing."""
    if not normalization:
        return None
    extent = normalization.get("film_extent") or {}
    if not extent.get("detected"):
        return None
    insets = extent.get("insets")
    analysis_rect = normalization.get("analysis_rect")
    if not insets or not analysis_rect or not any(int(v) > 0 for v in insets):
        return None

    tiff_h, tiff_w = tiff_size
    block = analysis_grid_block_sizes((tiff_h, tiff_w))[0]
    rect_x, rect_y, rect_w, rect_h = (int(v) for v in analysis_rect)
    top, bottom, left, right = (int(v) * block for v in insets)
    reach = 2 * (tiff_h + tiff_w)  # "no constraint": far outside the canvas
    x0 = rect_x + left if left else -reach
    y0 = rect_y + top if top else -reach
    x1 = rect_x + rect_w - 1 - right if right else tiff_w + reach
    y1 = rect_y + rect_h - 1 - bottom if bottom else tiff_h + reach

    corners = previews.tiff_points_to_display(
        [(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
        (tiff_h, tiff_w),
        quarter_turns=quarter_turns,
        flipped_horizontally=flipped,
        fine_angle_deg=fine_angle_deg,
    )
    display_h, display_w = previews.display_shape(
        (tiff_h, tiff_w), quarter_turns=quarter_turns, crop_params=None
    )
    xs = sorted(px for px, _ in corners)
    ys = sorted(py for _, py in corners)
    # The two smaller coordinates belong to one side, the two larger to the
    # other: the inscribed rect is bounded by the inner pair.
    left_edge, right_edge = xs[1], xs[2]
    top_edge, bottom_edge = ys[1], ys[2]

    analysis_h, analysis_w = analysis_size
    sx, sy = analysis_w / display_w, analysis_h / display_h
    ax0 = min(max(math.ceil(left_edge * sx), 0), analysis_w)
    ay0 = min(max(math.ceil(top_edge * sy), 0), analysis_h)
    ax1 = min(max(math.floor((right_edge + 1) * sx), 0), analysis_w)
    ay1 = min(max(math.floor((bottom_edge + 1) * sy), 0), analysis_h)
    if ax1 <= ax0 or ay1 <= ay0:
        return None
    if (ax0, ay0, ax1, ay1) == (0, 0, analysis_w, analysis_h):
        return None
    return (ax0, ay0, ax1 - ax0, ay1 - ay0)


def _dense_carrier(
    normalized: np.ndarray, covered: np.ndarray
) -> tuple[np.ndarray, set[str]]:
    """The dense carrier: border-connected, featureless, denser than the
    film can be. A small normalized-space version of
    `normalization.withhold_dense_border`.

    On a negative, scene content at or past the dense-end anchor barely
    exists — the meters put the scene's dense tail there — so a component
    at that level that borders the canvas (or the empty fill), spans the
    edge it borders, and is flat is the holder, not picture. Returns the
    carrier mask and the image sides it borders (`"left"`, `"right"`,
    `"top"`, `"bottom"`)."""
    height, width = covered.shape
    carrier = np.zeros_like(covered)
    sides: set[str] = set()
    luma = normalized.mean(axis=-1)
    dense = covered & (luma <= AUTO_CROP_DENSE_LEVEL)
    if not dense.any():
        return carrier, sides

    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        dense.astype(np.uint8), connectivity=8
    )
    # What "border" means: the image edge, or the empty fill beside it (a
    # tilted stitch's wedge separates the canvas edge from the carrier).
    border = ~cv2.erode(
        covered.astype(np.uint8), np.ones((3, 3), np.uint8), borderValue=0
    ).astype(bool)
    touching = set(np.unique(labels[border & dense]).tolist())
    covered_count = int(np.count_nonzero(covered))
    for label in range(1, count):
        if label not in touching:
            continue
        x, y, w, h, area = (int(v) for v in stats[label])
        if area < AUTO_CROP_DENSE_MIN_AREA_FRACTION * covered_count:
            continue
        if (
            w < AUTO_CROP_DENSE_MIN_SPAN_FRACTION * width
            and h < AUTO_CROP_DENSE_MIN_SPAN_FRACTION * height
        ):
            continue
        component = labels == label
        if float(luma[component].std()) > AUTO_CROP_MAX_DENSE_SPREAD:
            continue
        carrier |= component
        if x <= 1:
            sides.add("left")
        if x + w >= width - 1:
            sides.add("right")
        if y <= 1:
            sides.add("top")
        if y + h >= height - 1:
            sides.add("bottom")
    return carrier, sides


def _apply_hint(
    picture: np.ndarray,
    exclude: tuple[int, int, int, int],
    dense_sides: set[str],
) -> None:
    """Clear everything outside the hint's inner window, on each edge where
    the dense pass found nothing (in place)."""
    x, y, w, h = exclude
    if "left" not in dense_sides:
        picture[:, :x] = False
    if "right" not in dense_sides:
        picture[:, x + w :] = False
    if "top" not in dense_sides:
        picture[:y, :] = False
    if "bottom" not in dense_sides:
        picture[y + h :, :] = False


def _oriented_ratio(ratio: float, box_w: int, box_h: int) -> float:
    """The width : height to fit: the format's ratio turned to the picture
    rect's long axis, so a panoramic format lands right even on a
    square-ish canvas."""
    landscape = max(ratio, 1.0 / ratio)
    return landscape if box_w >= box_h else 1.0 / landscape


def _fit_ratio_window(
    non_picture: np.ndarray,
    box: tuple[int, int, int, int],
    ratio: float,
) -> tuple[int, int, int, int] | None:
    """The largest `ratio` window whose non-picture count stays within
    `AUTO_CROP_MAX_INTRUSION` of its area, or `None`.

    Binary search over the window height; each probe tests every origin at
    once with one summed-area-table lookup, O(log H · N). Among the origins
    feasible at the largest height, the one whose centre is nearest the
    picture rect's — a picture wider than the ratio keeps the crop
    symmetric."""
    box_x, box_y, box_w, box_h = box
    sat = cv2.integral(non_picture.astype(np.uint8), sdepth=cv2.CV_64F)
    canvas_h, canvas_w = non_picture.shape

    def probe(h: int) -> tuple[int, np.ndarray] | None:
        """`(w, ok)` — `ok[row, col]` is True where the `w` x `h` window
        with that origin is feasible — or None when it cannot fit at all."""
        w = round(h * ratio)
        if w < 1 or w > canvas_w or h > canvas_h:
            return None
        intrusion = sat[h:, w:] - sat[:-h, w:] - sat[h:, :-w] + sat[:-h, :-w]
        return w, intrusion <= AUTO_CROP_MAX_INTRUSION * w * h

    low, high = 1, min(canvas_h, int(canvas_w / ratio) + 1)
    best: tuple[int, int, np.ndarray] | None = None
    while low <= high:
        mid = (low + high) // 2
        result = probe(mid)
        if result is not None and result[1].any():
            best = (result[0], mid, result[1])
            low = mid + 1
        else:
            high = mid - 1
    if best is None:
        return None
    w, h, ok = best
    origins = np.argwhere(ok)  # (row, col) of every feasible origin
    centre_x = box_x + box_w / 2.0
    centre_y = box_y + box_h / 2.0
    distance = (origins[:, 1] + w / 2.0 - centre_x) ** 2 + (
        origins[:, 0] + h / 2.0 - centre_y
    ) ** 2
    row, col = origins[int(np.argmin(distance))]
    return int(col), int(row), w, h


def _maximal_rect(mask: np.ndarray) -> tuple[int, int, int, int]:
    """The maximal-area axis-aligned rectangle of True cells `(x, y, w, h)`,
    by the histogram-and-stack sweep, O(N)."""
    rows, cols = mask.shape
    heights = np.zeros(cols, dtype=np.int64)
    best_area, best = 0, (0, 0, 0, 0)
    for row in range(rows):
        heights = np.where(mask[row], heights + 1, 0)
        stack: list[int] = []
        line = heights.tolist() + [0]
        for col, current in enumerate(line):
            while stack and line[stack[-1]] >= current:
                top = stack.pop()
                left = stack[-1] + 1 if stack else 0
                area = line[top] * (col - left)
                if area > best_area:
                    best_area = area
                    best = (left, row - line[top] + 1, col - left, line[top])
            stack.append(col)
    return best


def _to_full_resolution(
    window: tuple[int, int, int, int],
    analysis_size: tuple[int, int],
    full_size: tuple[int, int],
    oriented: float | None,
) -> tuple[int, int, int, int]:
    """A window in analysis pixels as the largest one at full resolution
    that lies inside it — every edge rounded inward — with the ratio held
    exactly when there is one."""
    analysis_h, analysis_w = analysis_size
    full_h, full_w = full_size
    sx, sy = full_w / analysis_w, full_h / analysis_h
    x, y, w, h = window
    x0 = math.ceil(x * sx)
    y0 = math.ceil(y * sy)
    x1 = math.floor((x + w) * sx)
    y1 = math.floor((y + h) * sy)
    width, height = x1 - x0, y1 - y0
    if oriented is not None and width > 0 and height > 0:
        fit_h = min(height, int(width / oriented))
        fit_w = min(width, round(fit_h * oriented))
        x0 += (width - fit_w) // 2
        y0 += (height - fit_h) // 2
        width, height = fit_w, fit_h
    return x0, y0, width, height


def estimate_crop(
    analysis: np.ndarray,  # encoded uint16 display image, <= ANALYSIS_MAX_EDGE
    *,
    full_size: tuple[int, int],  # (height, width) of the full-res display image
    ratio: float | None,  # landscape-agnostic w:h, or None for unconstrained
    exclude: tuple[int, int, int, int] | None = None,  # carrier hint, analysis px
) -> AutoCrop | Refusal:
    """The largest picture-only window in the display image, as an
    axis-aligned rect in full-resolution display pixels — or a `Refusal`.

    Steps (docs/AUTO_CROP_PLAN.md §2.2): decode to normalized density; mask
    out thin material (`auto_rotate.picture_mask`); mask out the dense
    carrier, falling back to `exclude` on any edge where that finds nothing;
    clean the mask and take its bounding rect for orientation and the fill
    guard; fit the largest window inside the mask; shrink by the safety
    margin and scale to full resolution, every edge rounded inward."""
    analysis = np.asarray(analysis)
    if analysis.ndim == 2:
        analysis = np.stack([analysis] * 3, axis=-1)
    analysis_h, analysis_w = analysis.shape[:2]

    normalized = decode_normalized(analysis).astype(np.float32)
    covered, rebate = picture_mask(normalized)
    covered_count = int(np.count_nonzero(covered))
    if covered_count == 0:
        return Refusal("no_coverage")

    picture = covered & ~rebate
    carrier, dense_sides = _dense_carrier(normalized, covered)
    picture &= ~carrier
    if exclude is not None:
        _apply_hint(picture, exclude, dense_sides)

    scene = picture.astype(np.uint8)
    kernel_side = max(3, round(SCENE_CLEAN_FRACTION * min(analysis_h, analysis_w)))
    if kernel_side % 2 == 0:
        kernel_side += 1
    kernel = np.ones((kernel_side, kernel_side), np.uint8)
    scene = cv2.morphologyEx(scene, cv2.MORPH_OPEN, kernel)
    scene = cv2.morphologyEx(scene, cv2.MORPH_CLOSE, kernel)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(scene, connectivity=8)
    keep = [
        label
        for label in range(1, count)
        if stats[label, cv2.CC_STAT_AREA] >= SCENE_MIN_AREA_FRACTION * covered_count
    ]
    mask = np.isin(labels, keep) if keep else np.zeros_like(picture)
    picture_fraction = float(np.count_nonzero(mask)) / covered_count
    if not mask.any() or picture_fraction < AUTO_CROP_MIN_PICTURE_FRACTION:
        return Refusal("little_picture", picture_fraction=picture_fraction)

    box_x, box_y, box_w, box_h = cv2.boundingRect(
        cv2.findNonZero(mask.astype(np.uint8))
    )
    oriented = None if ratio is None else _oriented_ratio(ratio, box_w, box_h)

    if oriented is not None:
        window = _fit_ratio_window(~mask, (box_x, box_y, box_w, box_h), oriented)
    else:
        window = _maximal_rect(mask[box_y : box_y + box_h, box_x : box_x + box_w])
        window = (window[0] + box_x, window[1] + box_y, window[2], window[3])
    if window is None or window[2] < 1 or window[3] < 1:
        return Refusal("no_fit", picture_fraction=picture_fraction)

    fill_fraction = window[2] * window[3] / float(box_w * box_h)
    if fill_fraction < AUTO_CROP_MIN_FILL_FRACTION:
        return Refusal(
            "ragged", picture_fraction=picture_fraction, fill_fraction=fill_fraction
        )

    # The safety margin, held to the ratio: shrink the height, then derive
    # the width from it, about the window's own centre.
    x, y, w, h = window
    inset = max(1, round(AUTO_CROP_SAFETY_FRACTION * min(box_w, box_h)))
    if oriented is None:
        x, y, w, h = x + inset, y + inset, w - 2 * inset, h - 2 * inset
    else:
        new_h = h - 2 * inset
        new_w = min(w, round(new_h * oriented))
        x, y = x + (w - new_w) // 2, y + inset
        w, h = new_w, new_h
    if w < 1 or h < 1:
        return Refusal(
            "too_small", picture_fraction=picture_fraction, fill_fraction=fill_fraction
        )

    rect = _to_full_resolution(
        (x, y, w, h), (analysis_h, analysis_w), full_size, oriented
    )
    if min(rect[2], rect[3]) < repo.CROP_MIN_SIZE_PX:
        return Refusal(
            "too_small", picture_fraction=picture_fraction, fill_fraction=fill_fraction
        )
    return AutoCrop(
        rect=rect,
        ratio=ratio,
        picture_fraction=picture_fraction,
        fill_fraction=fill_fraction,
    )
