import dataclasses
import math

import cv2
import numpy as np
import pytest

from scanny_boy.cancellation import CancellationToken
from scanny_boy.composite import (
    MAX_CANVAS_DIMENSION,
    MAX_STITCHED_BYTES,
    MEMORY_SAFETY_FACTOR,
    _region_keep,
    check_memory_budget,
    check_output_size,
    composite,
    estimate_peak_bytes,
)
from scanny_boy.events import Code
from scanny_boy.layout import solve_layout
from scanny_boy.linear import decode_to_linear, encode_from_linear
from scanny_boy.normalization import (
    NORMALIZED_FILL,
    Bounds,
    analyze_bounds,
    block_median_grid,
    decode_normalized,
    detect_rebate,
    encode_normalized,
    normalize_log_image,
    to_log_density,
)
from scanny_boy.registration import PairResult, StitchError
from scanny_boy.selection import GridSpec
from scanny_boy.synthetic_scene_support import cut_frames, synthetic_scene

_SCENE_SIZE = (700, 1300)
_FRAME_SIZE = (500, 700)  # (height, width)


def _unnormalize(image: np.ndarray, bounds: Bounds) -> np.ndarray:
    """The arithmetic inverse of the published encoding (section 3.11):
    `10 ** (floor + val * (ceil - floor))` recovers the linear composite to
    within quantization."""
    normalized = decode_normalized(image)
    floors = np.asarray(bounds.floors, dtype=np.float32)
    ceils = np.asarray(bounds.ceils, dtype=np.float32)
    return np.power(10.0, floors + normalized * (ceils - floors))


def _rotation_matrix(angle_deg):
    angle_rad = np.radians(angle_deg)
    cos_a, sin_a = np.cos(angle_rad), np.sin(angle_rad)
    return np.array([[cos_a, -sin_a], [sin_a, cos_a]])


def _build_two_frame_scene(*, rotations_deg=(0.0, 5.0), overlap=0.3, seed=7):
    """A known scene cut into two overlapping frames, plus the ground-truth
    pair and solved layout needed to composite them. Returns (scene, names,
    uint16_frames, layout, cut_placements)."""
    scene = synthetic_scene(*_SCENE_SIZE, seed=seed)
    frames, cut_placements = cut_frames(
        scene,
        frame_size=_FRAME_SIZE,
        count=2,
        overlap=overlap,
        rotations_deg=list(rotations_deg),
        seed=seed,
    )
    names = ["f0", "f1"]
    uint16_frames = {
        name: encode_from_linear(np.stack([frame, frame, frame], axis=-1))
        for name, frame in zip(names, frames, strict=True)
    }

    matrix_a, matrix_b = cut_placements
    rotation_a, translation_a = matrix_a[:, :2], matrix_a[:, 2]
    rotation_b, translation_b = matrix_b[:, :2], matrix_b[:, 2]
    phi_ab_deg = np.degrees(
        np.arctan2(rotation_b[1, 0], rotation_b[0, 0])
    ) - np.degrees(np.arctan2(rotation_a[1, 0], rotation_a[0, 0]))
    u_ab = rotation_a.T @ (translation_b - translation_a)
    rotation_ab = _rotation_matrix(phi_ab_deg)

    rng = np.random.default_rng(seed)
    height, width = _FRAME_SIZE
    pts_b = rng.uniform([0, 0], [width, height], size=(100, 2))
    pts_a = pts_b @ rotation_ab.T + u_ab
    transform = np.hstack([rotation_ab, u_ab.reshape(2, 1)])

    pair = PairResult(
        a="f0",
        b="f1",
        transform=transform,
        good_matches=100,
        inliers=100,
        inlier_ratio=1.0,
        rms_residual_px=0.0,
        scale_drift=0.0,
        accepted=True,
        reject_code=None,
        reject_message=None,
        inlier_points_a=pts_a,
        inlier_points_b=pts_b,
        overlap_fraction=None,
        overlap_mad=None,
        overlap_mad_pregain=None,
        similarity_transform=transform,
        similarity_scale=1.0,
    )

    layout = solve_layout(names, _FRAME_SIZE, [pair])
    return scene, names, uint16_frames, layout, cut_placements


def _composite(layout, uint16_frames, *, progress_calls=None):
    def load_frame(name):
        return uint16_frames[name]

    def on_progress():
        if progress_calls is not None:
            progress_calls.append(1)

    return composite(
        layout, load_frame, cancel=CancellationToken(), on_progress=on_progress
    )


def test_reconstructs_a_known_scene():
    scene, _names, uint16_frames, layout, cut_placements = _build_two_frame_scene()
    result = _composite(layout, uint16_frames)

    # Map canvas coordinates back to the original scene's coordinates via
    # frame f0 (unrotated in this fixture): canvas -> f0-local (invert
    # f0's solved placement) -> scene (f0's own ground-truth cut_frames
    # placement).
    matrix_solved = layout.placements[0].matrix()
    rotation_solved, translation_solved = matrix_solved[:, :2], matrix_solved[:, 2]
    rotation_solved_inv = rotation_solved.T
    translation_solved_inv = -rotation_solved_inv @ translation_solved

    matrix_cut = cut_placements[0]
    rotation_cut, translation_cut = matrix_cut[:, :2], matrix_cut[:, 2]
    rotation_compose = rotation_cut @ rotation_solved_inv
    translation_compose = rotation_cut @ translation_solved_inv + translation_cut
    canvas_to_scene = np.hstack([rotation_compose, translation_compose.reshape(2, 1)])

    linear_result = _unnormalize(result.image, result.bounds)
    scene_height, scene_width = scene.shape
    reconstructed = cv2.warpAffine(
        linear_result,
        canvas_to_scene,
        (scene_width, scene_height),
        flags=cv2.INTER_LANCZOS4,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )[:, :, 0]

    # Compare over the interior of f0's own footprint (in scene space, an
    # axis-aligned rectangle since f0 has zero rotation), shrunk by a
    # margin comfortably past MASK_ERODE_PX and Lanczos4's support radius.
    # Part of this region is the overlap zone, so this exercises both the
    # warp and the blend, not just a trivial single-frame copy.
    frame_height, frame_width = _FRAME_SIZE
    x0, y0 = round(translation_cut[0]), round(translation_cut[1])
    margin = 25
    x0m, y0m = x0 + margin, y0 + margin
    x1m, y1m = x0 + frame_width - margin, y0 + frame_height - margin

    original_crop = scene[y0m:y1m, x0m:x1m]
    reconstructed_crop = reconstructed[y0m:y1m, x0m:x1m]

    # Measured ~0.0033 mean / ~0.020 max absolute error on this fixture.
    # Resampling twice (compositing's warp, then this test's own reverse
    # warp for comparison) is not lossless, so the tolerance below is
    # roughly 6x the measured mean — enough margin for a different fixture
    # or platform, tight enough to catch a wrong transform or a wrong
    # blend rather than just "it produced a picture".
    mean_absolute_error = float(np.mean(np.abs(original_crop - reconstructed_crop)))
    assert mean_absolute_error < 0.02


def test_reconstruction_is_order_independent():
    _scene, _names, uint16_frames, layout, _cut = _build_two_frame_scene()
    result_forward = _composite(layout, uint16_frames)

    reversed_layout = dataclasses.replace(
        layout, placements=list(reversed(layout.placements))
    )
    result_reversed = _composite(reversed_layout, uint16_frames)

    assert np.array_equal(result_forward.image, result_reversed.image)


def test_feather_weights_sum_to_one_inside_coverage():
    _scene, names, _uint16_frames, layout, _cut = _build_two_frame_scene()

    # Both frames carry the same known near-constant linear value with a
    # hair of gradient (a strictly constant frame would be degenerate for
    # the bounds meters: floor == ceil). If the weights inside the overlap
    # zone summed to anything other than 1 (e.g. a forgotten division,
    # summing instead of averaging), the blended region would come out
    # brighter or darker than this constant.
    height, width = _FRAME_SIZE
    rng = np.random.default_rng(3)
    gradient = 0.4 + 0.005 * rng.uniform(-1.0, 1.0, size=(height, width))
    gradient_frame = encode_from_linear(
        np.stack([gradient, gradient, gradient], axis=-1).astype(np.float32)
    )
    uniform_frames = {name: gradient_frame for name in names}

    result = _composite(layout, uniform_frames)
    linear = _unnormalize(result.image, result.bounds)
    covered = (
        result.image != encode_normalized(np.full((1, 1, 3), NORMALIZED_FILL))[0, 0]
    )

    assert np.count_nonzero(covered) > 0
    # The tolerance covers the double resampling (the warp, then this
    # test's un-normalization) of a noisy gradient.
    assert np.max(np.abs(linear[covered] - 0.4)) < 0.025


def _hand_built_strip_masks():
    """Two axis-aligned frame masks, each eroded on all four true edges by
    MASK_ERODE_PX as `composite` erodes a real frame's warped mask, placed
    side by side along x with frame A's right edge (canvas col 295) much
    closer to the sampled overlap column than frame B's left edge (canvas
    col 205) — the geometry needed to make the isotropic distance
    transform's border collapse actually visible: at a canvas row right at
    MASK_ERODE_PX (i.e. at the frames' own top edge, which for a full-height
    frame is also the strip's long border), the nearest zero pixel for
    *both* frames is that top edge, not the seam, so both weights collapse
    to the same tiny value regardless of how far the column sits from the
    seam. Returns (mask_a, mask_b, b_offset, height, x_pick)."""
    from scanny_boy.composite import MASK_ERODE_PX

    height, width_a, width_b = 200, 300, 300
    b_offset = 200
    mask_a = np.zeros((height, width_a), dtype=np.uint8)
    mask_a[MASK_ERODE_PX : height - MASK_ERODE_PX, MASK_ERODE_PX : width_a - MASK_ERODE_PX] = 1
    mask_b = np.zeros((height, width_b), dtype=np.uint8)
    mask_b[MASK_ERODE_PX : height - MASK_ERODE_PX, MASK_ERODE_PX : width_b - MASK_ERODE_PX] = 1
    return mask_a, mask_b, b_offset, height, 280


def _place_on_canvas(weight_a, weight_b, b_offset, height, width_a, width_b):
    canvas_width = b_offset + width_b
    canvas_a = np.zeros((height, canvas_width), dtype=np.float32)
    canvas_a[:, :width_a] = weight_a
    canvas_b = np.zeros((height, canvas_width), dtype=np.float32)
    canvas_b[:, b_offset:] = weight_b
    return canvas_a, canvas_b


def test_feather_contribution_is_constant_across_the_strip():
    from scanny_boy.composite import MASK_ERODE_PX, _feather_weight

    mask_a, mask_b, b_offset, height, x_pick = _hand_built_strip_masks()
    axes = ((1.0, 0.0),)
    canvas_a, canvas_b = _place_on_canvas(
        _feather_weight(mask_a, 0, 0, axes),
        _feather_weight(mask_b, b_offset, 0, axes),
        b_offset, height, mask_a.shape[1], mask_b.shape[1],
    )

    row_border = MASK_ERODE_PX  # the frames' own top edge == the strip's long border
    row_mid = height // 2

    def contribution(canvas_a, canvas_b, y):
        a, b = canvas_a[y, x_pick], canvas_b[y, x_pick]
        return a / (a + b)

    # The strip-axis ramp must give the same crossfade at the border as at
    # the middle.
    assert contribution(canvas_a, canvas_b, row_border) == pytest.approx(
        contribution(canvas_a, canvas_b, row_mid), abs=1e-6
    )

    # And this is the regression it fixes: the same geometry, fed through
    # the old isotropic distance transform, collapses to a near-50/50 blend
    # at the border while giving a position-dependent ratio in the middle —
    # exactly the curved smear this plan describes.
    old_canvas_a, old_canvas_b = _place_on_canvas(
        _feather_weight(mask_a, 0, 0, ()),
        _feather_weight(mask_b, b_offset, 0, ()),
        b_offset, height, mask_a.shape[1], mask_b.shape[1],
    )
    old_border = contribution(old_canvas_a, old_canvas_b, row_border)
    old_mid = contribution(old_canvas_a, old_canvas_b, row_mid)
    assert old_border == pytest.approx(0.5, abs=0.02)
    assert abs(old_border - old_mid) > 0.1


def test_transition_band_width_does_not_grow_toward_the_border():
    from scanny_boy.composite import MASK_ERODE_PX, _feather_weight

    mask_a, mask_b, b_offset, height, _x_pick = _hand_built_strip_masks()
    axes = ((1.0, 0.0),)
    canvas_a, canvas_b = _place_on_canvas(
        _feather_weight(mask_a, 0, 0, axes),
        _feather_weight(mask_b, b_offset, 0, axes),
        b_offset, height, mask_a.shape[1], mask_b.shape[1],
    )

    total = canvas_a + canvas_b
    frac = np.divide(canvas_a, total, out=np.zeros_like(total), where=total > 0)
    band = (total > 0) & (frac > 0.1) & (frac < 0.9)

    def band_width(y):
        xs = np.where(band[y])[0]
        return int(xs[-1] - xs[0] + 1) if xs.size else 0

    row_border = MASK_ERODE_PX
    row_mid = height // 2
    row_bottom = height - 1 - MASK_ERODE_PX

    widths = [band_width(row_border), band_width(row_mid), band_width(row_bottom)]
    assert min(widths) > 0
    # Bounded: the transition band's width at the strip's borders must not
    # exceed its width at the middle — under the old isotropic feather this
    # widened toward the border without limit.
    assert max(widths) - min(widths) == 0


def test_misregistration_produces_a_bounded_step_not_a_growing_smear():
    """A deliberate 3px translational error (perpendicular to the strip
    axis) between the solved and the "true" placement, on a real-content
    scene. With the strip-axis feather the mismatched band's width is fixed
    by the feather geometry alone, not by the misregistration, so it must
    not grow toward the canvas's long borders — the defect this plan
    describes was exactly that growth."""
    _scene, names, uint16_frames, layout, _cut = _build_two_frame_scene(
        rotations_deg=(0.0, 0.0), overlap=0.3
    )
    assert layout.strip_axis is not None
    ax, ay = layout.strip_axis
    perp = (-ay, ax)

    misplaced = [
        dataclasses.replace(
            p,
            translation=(
                p.translation[0] + perp[0] * 3.0,
                p.translation[1] + perp[1] * 3.0,
            ),
        )
        if p.name == names[1]
        else p
        for p in layout.placements
    ]
    misregistered_layout = dataclasses.replace(layout, placements=misplaced)

    correct_result = _composite(layout, uint16_frames)
    misregistered_result = _composite(misregistered_layout, uint16_frames)

    correct_linear = _unnormalize(correct_result.image, correct_result.bounds).astype(
        np.float32
    )
    mis_linear = _unnormalize(
        misregistered_result.image, misregistered_result.bounds
    ).astype(np.float32)
    diff = np.mean(np.abs(correct_linear - mis_linear), axis=-1)

    fill_code = encode_normalized(np.full((1, 1, 3), NORMALIZED_FILL))[0, 0]
    covered = np.any(correct_result.image != fill_code, axis=-1) & np.any(
        misregistered_result.image != fill_code, axis=-1
    )
    rows = np.where(covered.any(axis=1))[0]
    # A small margin from the very first/last covered row, which can carry
    # partial coverage from the erosion/warp boundary itself.
    top_y, mid_y, bottom_y = rows[5], rows[len(rows) // 2], rows[-6]

    def step_width(y):
        return int(np.count_nonzero(covered[y] & (diff[y] > 0.02)))

    widths = [step_width(top_y), step_width(mid_y), step_width(bottom_y)]
    assert min(widths) > 0
    assert max(widths) - min(widths) < 0.3 * max(widths)


def test_strip_axis_none_reproduces_the_distance_transform_exactly():
    from scanny_boy.composite import _feather_weight

    mask = np.zeros((80, 120), dtype=np.uint8)
    mask[10:70, 15:105] = 1
    expected = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
    actual = _feather_weight(mask, bbox_x=3, bbox_y=7, axes=())
    assert np.array_equal(actual, expected.astype(np.float32))


# --- the separable (grid) feather (docs/GRID_STITCH_PLAN.md section 5) -----


def _hand_built_grid_masks(overlap_px=100):
    """Four axis-aligned frame masks in a 2x2 at the plan's 1/3 overlap
    geometry (step = 2/3 of the frame in both directions), each eroded on
    all four true edges as `composite` erodes a real warped mask. Frame
    (row, col) sits at (col*step_x, row*step_y) on the canvas. Returns
    (masks, offsets, height, width, step_x, step_y)."""
    from scanny_boy.composite import MASK_ERODE_PX

    height = width = 300
    step = 200  # 2/3 of 300: 100 px overlap on both axes
    masks = []
    offsets = {}
    for row in range(2):
        for col in range(2):
            mask = np.zeros((height, width), dtype=np.uint8)
            mask[
                MASK_ERODE_PX : height - MASK_ERODE_PX,
                MASK_ERODE_PX : width - MASK_ERODE_PX,
            ] = 1
            masks.append(mask)
            offsets[(row, col)] = (col * step, row * step)
    return masks, offsets, height, width, step, step


def _grid_weight_canvas(masks, offsets, height, width, axes):
    """Each frame's feather weight placed at its canvas offset, in the
    (A=(0,0), B=(0,1), C=(1,0), D=(1,1)) order of `masks`."""
    from scanny_boy.composite import _feather_weight

    canvas = np.zeros((height, width), dtype=np.float32)
    placed = []
    for (row, col), offset in sorted(offsets.items()):
        weight = _feather_weight(masks[row * 2 + col], offsets[(row, col)][0], offsets[(row, col)][1], axes)
        placed = np.zeros((height, width), dtype=np.float32)
        placed[weight.shape[0] // 2 :]  # no-op; keep shape obvious
        canvas[offsets[(row, col)][1] : offsets[(row, col)][1] + weight.shape[0],
               offsets[(row, col)][0] : offsets[(row, col)][0] + weight.shape[1]] += weight
        placed.append((row, col, weight))
    return canvas, placed


def test_two_axis_feather_ratio_is_y_invariant_across_a_vertical_seam():
    """The 2D analogue of the strip regression (§5.3): for two frames in
    the same row, w_A / (w_A + w_B) at a fixed position along the
    across-axis is equal at the top, middle, and bottom of their vertical
    overlap band — same-row frames share a down-extent, so their
    along-down ramp factors cancel in the ratio."""
    from scanny_boy.composite import _feather_weight

    height, width = 200, 300
    b_offset = 200  # 1/3 overlap: 100 px
    mask_a = np.zeros((height, width), dtype=np.uint8)
    mask_a[5 : height - 5, 5 : width - 5] = 1
    mask_b = np.zeros((height, width), dtype=np.uint8)
    mask_b[5 : height - 5, 5 : width - 5] = 1
    axes = ((1.0, 0.0), (0.0, 1.0))  # across along x, down along y

    wa = _feather_weight(mask_a, 0, 0, axes)
    wb = _feather_weight(mask_b, b_offset, 0, axes)

    # The band where both frames cover (x fixed inside the overlap).
    x_pick = 250
    top, mid, bottom = 20, height // 2, height - 21

    def ratio(y):
        a, b = wa[y, x_pick], wb[y, x_pick]
        return a / (a + b)

    assert ratio(top) == pytest.approx(ratio(mid), abs=1e-6)
    assert ratio(mid) == pytest.approx(ratio(bottom), abs=1e-6)


def test_grid_feather_ratio_is_x_invariant_across_a_horizontal_seam():
    """Transposed: frames sharing an x-extent (same column) blend across a
    horizontal seam, and the ratio is x-invariant."""
    from scanny_boy.composite import _feather_weight

    height, width = 300, 200
    d_offset = 200  # vertical step, 100 px overlap
    mask_c = np.zeros((height, width), dtype=np.uint8)
    mask_c[5 : height - 5, 5 : width - 5] = 1
    mask_d = np.zeros((height, width), dtype=np.uint8)
    mask_d[5 : height - 5, 5 : width - 5] = 1
    axes = ((1.0, 0.0), (0.0, 1.0))

    wc = _feather_weight(mask_c, 0, 0, axes)
    wd = _feather_weight(mask_d, 0, d_offset, axes)

    y_pick = 250
    left, mid, right = 20, width // 2, width - 21

    def ratio(x):
        c, d = wc[y_pick, x], wd[y_pick, x]
        return c / (c + d)

    assert ratio(left) == pytest.approx(ratio(mid), abs=1e-6)
    assert ratio(mid) == pytest.approx(ratio(right), abs=1e-6)


def test_one_axis_tuple_reproduces_the_strip_weights_byte_for_byte():
    """The no-regression guarantee: the degenerate one-axis path produces
    exactly today's strip weights."""
    from scanny_boy.composite import MASK_ERODE_PX, _axis_ramp, _feather_weight

    mask = np.zeros((200, 300), dtype=np.uint8)
    mask[MASK_ERODE_PX : 200 - MASK_ERODE_PX, MASK_ERODE_PX : 300 - MASK_ERODE_PX] = 1
    axis = (0.6, 0.8)
    assert np.array_equal(
        _feather_weight(mask, 17, 29, (axis,)), _axis_ramp(mask, 17, 29, axis)
    )


def test_empty_axes_reproduce_the_distance_transform_byte_for_byte():
    from scanny_boy.composite import _feather_weight

    mask = np.zeros((90, 140), dtype=np.uint8)
    mask[12:78, 20:120] = 1
    expected = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
    assert np.array_equal(
        _feather_weight(mask, 5, 9, ()), expected.astype(np.float32)
    )


def test_four_way_corner_weights_are_positive_smooth_and_normalized():
    """The §7.3 case the isotropic transform gets wrong: at 1/3 overlap,
    over the region all four frames of a 2x2 cover, every weight is
    strictly positive (the floor is applied to the product, not per-axis),
    the four normalized weights sum to 1, and the blend is smooth — no
    interior pixel's weight vector jumps by more than a small bound
    between neighbouring pixels."""
    from scanny_boy.composite import MASK_ERODE_PX, _feather_weight

    height = width = 300
    step = 200
    mask = np.zeros((height, width), dtype=np.uint8)
    mask[MASK_ERODE_PX : height - MASK_ERODE_PX, MASK_ERODE_PX : width - MASK_ERODE_PX] = 1
    axes = ((1.0, 0.0), (0.0, 1.0))

    offsets = [(0, 0), (step, 0), (0, step), (step, step)]
    weights = [
        _feather_weight(mask, x, y, axes) for x, y in offsets
    ]

    # The four-way region: where every frame's mask overlaps in canvas
    # space. Frame (x, y) covers canvas [x, x+300) x [y, y+300), eroded by
    # MASK_ERODE_PX; the intersection starts at (step, step) minus erosion.
    canvas_h = canvas_w = height + step
    placed = np.zeros((4, canvas_h, canvas_w), dtype=np.float32)
    for i, (x, y) in enumerate(offsets):
        placed[i, y : y + height, x : x + width] = weights[i]

    region = np.ones((canvas_h, canvas_w), dtype=bool)
    for i, (x, y) in enumerate(offsets):
        covered = placed[i] > 0
        region &= covered
    assert region.sum() > 1000

    # 1. strictly positive everywhere in the four-way region.
    assert np.all(placed[:, region] > 0)

    # 2. the four normalized weights sum to 1 — the convex-combination
    # property any positive weights give, checked here over the region.
    sums = np.where(region, placed.sum(axis=0), 1.0)
    norm = placed / sums[np.newaxis]
    assert np.allclose(norm[:, region].sum(axis=0), 1.0)

    # 3. smooth: no interior pixel's normalized weight vector jumps between
    # neighbouring pixels by more than a small bound. Interior = pixels of
    # the region whose horizontal neighbours are also in the region.
    interior_x = region & np.roll(region, 1, axis=1) & np.roll(region, -1, axis=1)
    interior_cols = interior_x.any(axis=0)[:-1]
    jumps = np.abs(np.diff(norm, axis=2))[:, :, interior_cols]
    assert np.all(jumps <= 0.05)


def test_gain_correction_reconciles_a_known_brightness_offset():
    _scene, _names, uint16_frames, layout, _cut = _build_two_frame_scene()
    # Scale frame f1's linear values down per channel (never up: no clipping
    # to confound the measured means), so mean_b = k * mean_a exactly.
    k = (0.8, 0.9, 1.0)
    offset_frames = {}
    for name, frame in uint16_frames.items():
        linear = decode_to_linear(frame).astype(np.float32)
        if name == "f1":
            linear = linear * np.asarray(k, dtype=np.float32)
        offset_frames[name] = encode_from_linear(linear)

    result = _composite(layout, offset_frames)
    pair = ("f0", "f1")

    assert result.overlap_mad_pregain[pair] > 5 * result.overlap_mad[pair]

    # Geometric-mean anchor: g1/g0 = 1/k and g0*g1 = 1, so the gains bracket
    # 1 symmetrically per channel.
    assert np.allclose(result.gains["f0"], np.sqrt(k), rtol=0.02)
    assert np.allclose(result.gains["f1"], 1.0 / np.sqrt(k), rtol=0.02)


def test_frames_without_a_usable_gain_row_stay_at_unity(monkeypatch):
    import scanny_boy.composite as composite_module

    _scene, _names, uint16_frames, layout, _cut = _build_two_frame_scene()
    k = (0.8, 0.9, 1.0)
    offset_frames = {}
    for name, frame in uint16_frames.items():
        linear = decode_to_linear(frame).astype(np.float32)
        if name == "f1":
            linear = linear * np.asarray(k, dtype=np.float32)
        offset_frames[name] = encode_from_linear(linear)

    # A floor above any real overlap drops every gain row: the solve has no
    # evidence, so the frames must keep gain 1 and the two MAD measurements
    # must agree exactly.
    monkeypatch.setattr(composite_module, "MIN_GAIN_OVERLAP_PX", 10**9)
    result = _composite(layout, offset_frames)
    pair = ("f0", "f1")

    assert result.gains["f0"] == (1.0, 1.0, 1.0)
    assert result.gains["f1"] == (1.0, 1.0, 1.0)
    assert result.overlap_mad[pair] == result.overlap_mad_pregain[pair]


def test_no_output_value_is_negative_or_clipped_high():
    _scene, names, _uint16_frames, layout, _cut = _build_two_frame_scene(
        rotations_deg=(0.0, 5.0)
    )
    height, width = _FRAME_SIZE

    # A near-uniform frame close to each end of the linear range, with a
    # hair of gradient (a strictly constant frame is degenerate for the
    # bounds meters). Lanczos4 undershoots below 0 and can overshoot above
    # 1 near a warped frame's own border (section 2.3); without
    # composite.py's clamp, an undershoot could drag the weighted average
    # below the true value, or an unclamped negative could otherwise
    # corrupt the blend.
    rng = np.random.default_rng(5)
    bright_linear = 0.98 + 0.005 * rng.uniform(-1.0, 1.0, size=(height, width))
    dark_linear = 0.02 + 0.005 * rng.uniform(-1.0, 1.0, size=(height, width))

    def _frame(linear):
        return encode_from_linear(
            np.stack([linear, linear, linear], axis=-1).astype(np.float32)
        )

    bright_result = _composite(
        layout, {names[0]: _frame(bright_linear), names[1]: _frame(bright_linear)}
    )
    dark_result = _composite(
        layout, {names[0]: _frame(dark_linear), names[1]: _frame(dark_linear)}
    )

    fill_code = encode_normalized(np.full((1, 1, 3), NORMALIZED_FILL))[0, 0]
    bright_covered = np.any(bright_result.image != fill_code, axis=-1)
    dark_covered = np.any(dark_result.image != fill_code, axis=-1)

    # Normalized values: dense film (scene highlight, i.e. the *bright*
    # frames) maps toward 0 and thin film (scene shadow, the *dark* frames)
    # toward 1 — the published image stays a negative in appearance
    # (section 3.2), so the two fixtures land at opposite ends of the
    # stretch. The meters' own bounds make both spans full-range by
    # construction, so the assertions that still mean something are: the
    # un-normalized reconstruction stays near each frame's known constant
    # (a wraparound or unclamped blend would produce wild values), and the
    # observed extrema stay inside the encode's headroom.
    bright_linear = _unnormalize(bright_result.image, bright_result.bounds)
    dark_linear = _unnormalize(dark_result.image, dark_result.bounds)
    assert np.min(bright_linear[bright_covered]) > 0.9
    assert np.max(dark_linear[dark_covered]) < 0.1
    # The observed extrema are picture statistics: the scene's densest and
    # thinnest single pixels sit beyond the *grid's* floor/ceil percentiles
    # (the block median never sees them), which is exactly why the encode
    # reserves asymmetric headroom (section 3.6) and why excursions past it
    # warn rather than fail. They must be finite scene values, not the
    # fill: no excursion may reach the log10(1e-6) regime.
    for result in (bright_result, dark_result):
        assert min(result.observed_min) > -1.0
        assert max(result.observed_max) < 2.0


def test_uncovered_pixels_take_the_normalized_fill():
    """Section 3.14: uncovered canvas pixels take `encode_normalized(
    NORMALIZED_FILL)` — code 65535, the top of the encodable range. Expect
    the published file's border to flip from black to white; it is not a
    regression."""
    _scene, _names, uint16_frames, layout, _cut = _build_two_frame_scene()
    result = _composite(layout, uint16_frames)

    canvas_width, canvas_height = layout.canvas_size
    corners = [
        (0, 0),
        (canvas_width - 1, 0),
        (0, canvas_height - 1),
        (canvas_width - 1, canvas_height - 1),
    ]
    fill_code = encode_normalized(np.full((1, 1, 3), NORMALIZED_FILL))[0, 0]
    for x, y in corners:
        assert tuple(int(v) for v in result.image[y, x]) == tuple(
            int(v) for v in fill_code
        )


def test_region_keep_withholds_uncovered_interior_blocks():
    """Section 1.5's hole: the blend's `covered` mask can hold interior
    holes the layout's valid rect never saw. `_region_keep` intersects
    every candidate region with the blocks the blend actually covered, so
    a hole's cells — linear 0, log -6.0 — can never reach the meters and
    latch the floor percentile (the R1 negative-08 failure)."""
    canvas_shape = (1200, 1200)  # block = 2: blocks, not cells, get gated
    covered = np.ones(canvas_shape, dtype=bool)
    covered[100:140, 100:140] = False  # an interior hole
    keep = _region_keep((600, 600), canvas_shape, (0, 0, 1200, 1200), covered)

    block = 2
    # The hole's blocks — including the partially covered edge blocks — are
    # withheld; everything else survives.
    assert not keep[
        100 // block : -(-140 // block), 100 // block : -(-140 // block)
    ].any()
    assert keep.any()
    assert keep[0, 0]
    assert keep[599, 599]


def test_region_keep_with_a_fully_uncovered_region_falls_back_to_coverage():
    """A rect the blend barely covered must not empty the meters — and must
    not meter the fill again either: the last-resort fallback is the
    covered blocks alone, never the unfiltered grid."""
    canvas_shape = (1200, 1200)
    covered = np.ones(canvas_shape, dtype=bool)
    covered[400:800, 400:800] = False  # the middle of the rect is a hole
    rect = (400, 400, 400, 400)
    keep = _region_keep((600, 600), canvas_shape, rect, covered)
    # Every block of the rect is at least partially uncovered, so both rect
    # fallbacks are empty; the meters fall back to the covered blocks.
    assert keep.any()
    assert not keep[200:400, 200:400].any()
    assert keep[0, 0] and keep[599, 599]
    assert keep.sum() == 600 * 600 - 200 * 200


def test_memory_estimate_rejects_an_impossible_canvas():
    with pytest.raises(StitchError) as exc_info:
        check_memory_budget(10**18)
    assert exc_info.value.code is Code.INSUFFICIENT_MEMORY


# --- frame_bbox (docs/GRID_STITCH_PLAN.md section 1a) ----------------------


def test_frame_bbox_is_frame_sized_for_an_unrotated_placement():
    from scanny_boy.composite import frame_bbox

    matrix = np.array([[1.0, 0.0, 100.0], [0.0, 1.0, 50.0]])
    x, y, width, height = frame_bbox(matrix, 200, 300, (1000, 1000))
    assert (x, y, width, height) == (100, 50, 300, 200)


def test_frame_bbox_is_larger_for_a_rotated_placement():
    from scanny_boy.composite import frame_bbox

    angle = np.radians(30.0)
    cos_a, sin_a = np.cos(angle), np.sin(angle)
    matrix = np.array([[cos_a, -sin_a, 500.0], [sin_a, cos_a, 500.0]])
    _x, _y, width, height = frame_bbox(matrix, 200, 300, (2000, 2000))
    # A 300x200 frame at 30 degrees: the box must exceed 300x200 on the
    # long axis but stay well under the diagonal (which a canvas-sized
    # bbox would effectively be).
    assert width > 300
    assert height > 200
    assert width < 400
    assert height < 350
    # And the (width, height) pair is in (x, y, W, H) pixel ordering, not
    # the (height, width) `estimate_peak_bytes` ordering — getting that
    # backwards is silent, so pin it: W is the transformed long dimension.
    assert width == max(width, height)


def test_frame_bbox_clamps_to_the_canvas():
    from scanny_boy.composite import frame_bbox

    # Frame extending past the canvas's right and bottom edges.
    matrix = np.array([[1.0, 0.0, 900.0], [0.0, 1.0, 900.0]])
    x, y, width, height = frame_bbox(matrix, 200, 300, (1000, 1000))
    assert x == 900 and y == 900
    assert x + width == 1000 and y + height == 1000


def test_peak_estimate_counts_the_source_frame_and_the_safety_factor():
    canvas_size = (4000, 3000)
    bbox_size = (1200, 1600)  # (height, width)

    small_frame = estimate_peak_bytes(canvas_size, (2000, 3000), bbox_size, 1)
    large_frame = estimate_peak_bytes(canvas_size, (4000, 6000), bbox_size, 2)
    # The original (superseded) formula omitted the source frame entirely,
    # so it would not move at all when frame_size grows at a fixed canvas
    # and bounding box (section 3.8.1); the current formula also charges
    # every warped frame, which must be resident simultaneously for the
    # pairwise stats and gain solve.
    assert large_frame > small_frame

    canvas_width, canvas_height = canvas_size
    frame_height, frame_width = (4000, 6000)
    bbox_height, bbox_width = bbox_size

    canvas_pixels = canvas_width * canvas_height
    frame_pixels = frame_width * frame_height
    bbox_pixels = bbox_width * bbox_height

    accum = canvas_pixels * 3 * 4
    weight = canvas_pixels * 4
    result = canvas_pixels * 3 * 2
    log_density = canvas_pixels * 3 * 4
    normalized = canvas_pixels * 3 * 4
    source = frame_pixels * 3 * 2 + frame_pixels * 3 * 4
    warped = bbox_pixels * 3 * 4
    warp_aux = bbox_pixels * 2
    feather_scratch = bbox_pixels * 4 * 3

    all_warped = 2 * (warped + warp_aux)
    live_bytes = max(
        accum + weight + source + all_warped + feather_scratch,
        accum + weight + log_density + normalized + result,
    )

    expected = math.ceil(live_bytes * MEMORY_SAFETY_FACTOR)
    assert large_frame == expected


def test_oversized_canvas_warns():
    warnings = []

    def on_warning(code, message):
        warnings.append((code, message))

    # Stubbed canvas size: never allocate gigabytes in a test.
    check_output_size((MAX_CANVAS_DIMENSION + 1, 100), on_warning=on_warning)

    assert any(code is Code.OUTPUT_DIMENSIONS_LARGE for code, _ in warnings)


def test_oversized_file_fails():
    warnings = []

    def on_warning(code, message):
        warnings.append((code, message))

    # Both dimensions stay under MAX_CANVAS_DIMENSION so only the byte-size
    # gate is exercised; still a stubbed canvas size, never allocated.
    width, height = 29_000, 25_000
    assert width * height * 3 * 2 > MAX_STITCHED_BYTES

    with pytest.raises(StitchError) as exc_info:
        check_output_size((width, height), on_warning=on_warning)

    assert exc_info.value.code is Code.STITCH_OUTPUT_TOO_LARGE
    assert warnings == []


# --- geometric calibration (docs/GEOMETRIC_PLAN.md sections 5.3 and 8) -----

from scanny_boy.composite import (
    GEOMETRY_BAND_ROWS,
    _geometry_camera,
    _warp_bands,
)


def _geometry_dict(k1: float, frame_width: int, frame_height: int) -> dict:
    """A section 3.2 geometry object with the identity gauge."""
    return {
        "format_version": 1,
        "frame_width": frame_width,
        "frame_height": frame_height,
        "fx": float(max(frame_width, frame_height)),
        "fy": float(max(frame_width, frame_height)),
        "k1": k1,
        "k2": 0.0,
        "cx": (frame_width - 1) / 2,
        "cy": (frame_height - 1) / 2,
        "stage": "k1",
        "gauge": "identity",
        "board_key": "35mm",
    }


def test_no_geometry_produces_pixels_identical_to_the_warp_affine_path():
    """The section 5.1 regression guard: a profile without geometry must
    keep `composite`'s `cv2.warpAffine` implementation byte-for-byte.

    The reference reimplements the pre-geometry warp pass and blend. The
    solved gains are taken from the result itself — the warp path is what
    the geometry switch changes, and pinning it is the point."""
    _scene, _names, uint16_frames, layout, _cut = _build_two_frame_scene()
    result = _composite(layout, uint16_frames)

    canvas_width, canvas_height = layout.canvas_size
    accum = np.zeros((canvas_height, canvas_width, 3), dtype=np.float32)
    weight_canvas = np.zeros((canvas_height, canvas_width), dtype=np.float32)
    warped_frames = []
    for placement in layout.placements:
        frame = uint16_frames[placement.name]
        src_h, src_w = frame.shape[:2]
        linear = decode_to_linear(frame).astype(np.float32)
        matrix = placement.matrix()
        from scanny_boy.composite import _EROSION_KERNEL, _feather_weight, frame_bbox

        x, y, w, h = frame_bbox(matrix, src_h, src_w, layout.canvas_size)
        M = matrix.copy()
        M[:, 2] -= (x, y)
        warped = cv2.warpAffine(
            linear, M, (w, h), flags=cv2.INTER_LANCZOS4,
            borderMode=cv2.BORDER_CONSTANT, borderValue=0,
        )
        warped = np.clip(warped, 0.0, None)
        mask = cv2.warpAffine(
            np.ones((src_h, src_w), np.uint8), M, (w, h),
            flags=cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT, borderValue=0,
        )
        eroded = cv2.erode(
            mask, _EROSION_KERNEL, borderType=cv2.BORDER_CONSTANT, borderValue=0
        )
        weight = _feather_weight(
            eroded, x, y, layout.feather_axes()
        )
        gain = np.asarray(result.gains[placement.name], dtype=np.float32)
        warped_frames.append((x, y, w, h, warped * gain, weight))

    for x, y, w, h, warped, weight in warped_frames:
        accum[y : y + h, x : x + w] += warped * weight[:, :, np.newaxis]
        weight_canvas[y : y + h, x : x + w] += weight

    covered = weight_canvas > 0
    out = np.zeros_like(accum)
    out[covered] = accum[covered] / weight_canvas[covered, np.newaxis]

    # The same fused normalization pass composite() runs on its own
    # accumulator (no region here: the meters fall back to the blocks the
    # blend actually covered).
    img_log = to_log_density(out)
    grid = block_median_grid(img_log)
    keep = block_median_grid(np.where(covered, np.float32(1.0), np.float32(0.0))) >= 1.0
    keep, _rebate = detect_rebate(grid, keep)
    bounds = analyze_bounds(grid, keep)
    normalized_img = normalize_log_image(img_log, bounds)
    encoded = encode_normalized(normalized_img)
    encoded[~covered] = encode_normalized(np.full((1, 1, 3), NORMALIZED_FILL))[0, 0]
    assert np.array_equal(result.image, encoded)


def test_band_map_round_trips_a_distorted_frame():
    """Distort the frames with known coefficients, composite through the
    band-map path, and recover the undistorted composite to within
    interpolation error."""
    _scene, _names, uint16_frames, layout, _cut = _build_two_frame_scene()
    height, width = _FRAME_SIZE
    geometry = _geometry_dict(-0.02, width, height)

    distorted = {}
    K = np.array(
        [[geometry["fx"], 0, geometry["cx"]],
         [0, geometry["fy"], geometry["cy"]],
         [0, 0, 1.0]]
    )
    for name, frame in uint16_frames.items():
        ys, xs = np.mgrid[0:height, 0:width]
        grid = np.stack([xs, ys], axis=-1).reshape(-1, 1, 2).astype(np.float32)
        # observed(q) = ideal(d^-1(q)): sample the source through the
        # undistortion map to synthesise the distorted frame.
        undistorted = cv2.undistortPoints(
            grid, K, np.array([geometry["k1"], 0.0, 0, 0, 0]), P=K
        ).reshape(height, width, 2)
        distorted[name] = np.clip(
            cv2.remap(
                frame,
                undistorted[:, :, 0].astype(np.float32),
                undistorted[:, :, 1].astype(np.float32),
                cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_CONSTANT,
                borderValue=0,
            ),
            0,
            65535,
        ).astype(np.uint16)

    distorted_result = composite(
        layout,
        lambda name: distorted[name],
        cancel=CancellationToken(),
        on_progress=lambda: None,
        geometry=geometry,
    )
    plain_result = _composite(layout, uint16_frames)

    # Compare after inverting each result's own bounds: the two composites
    # meter slightly different canvases, so their normalized encodings are
    # only comparable once unnormalized back to linear. `covered` uses the
    # fill code, since the published image no longer has a zero fill.
    fill_code = encode_normalized(np.full((1, 1, 3), NORMALIZED_FILL))[0, 0]
    covered = np.any(plain_result.image != fill_code, axis=-1)
    err = np.abs(
        _unnormalize(distorted_result.image, distorted_result.bounds)
        - _unnormalize(plain_result.image, plain_result.bounds)
    )[covered]
    # Double resampling (synthetic distortion, then the band map's inverse)
    # plus the border pixels the distortion pulls in at the frame edge.
    assert float(np.mean(err)) < 0.02


def test_maps_mode_leaves_green_untouched_and_moves_red_and_blue():
    """In "maps" mode the green channel's map is the plain forward
    distortion, and red and blue move by their own fitted maps."""
    height, width = 200, 260
    geometry = _geometry_dict(0.0, width, height)  # no distortion
    ca = {
        "format_version": 1,
        "mode": "maps",
        "red": {"c0": 1.01, "c1": 0.0, "c2": 0.0, "center_x": 0.0, "center_y": 0.0},
        "blue": {"c0": 0.99, "c1": 0.0, "c2": 0.0, "center_x": 0.0, "center_y": 0.0},
    }

    frame = encode_from_linear(
        np.random.default_rng(0).uniform(0.1, 0.9, (height, width, 3)).astype(np.float32)
    )
    linear = decode_to_linear(frame).astype(np.float32)
    ones = np.ones((height, width), dtype=np.uint8)
    bbox_matrix = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])

    warped, mask = _warp_bands(
        linear, ones, bbox_matrix, width, height, geometry, ca
    )

    # Green: the map is the identity, and Lanczos at exact integer
    # coordinates is the delta function.
    assert np.array_equal(mask, ones)
    assert np.allclose(warped[:, :, 1], linear[:, :, 1], atol=1e-5)

    # Red: sampled at radius * 1.01 about the centre.
    ys, xs = np.mgrid[0:height, 0:width]
    fx = geometry["fx"]
    cx, cy = geometry["cx"], geometry["cy"]
    dx = (xs - cx) / fx
    dy = (ys - cy) / fx
    r = np.hypot(dx, dy)
    expected_x = (dx * 1.01 * fx + cx).astype(np.float32)
    expected_y = (dy * 1.01 * fx + cy).astype(np.float32)
    expected_red = cv2.remap(
        linear[:, :, 0], expected_x, expected_y, cv2.INTER_LANCZOS4,
        borderMode=cv2.BORDER_CONSTANT, borderValue=0,
    )
    interior = (r < 0.4) & (r > 0.05)
    assert np.allclose(warped[:, :, 0][interior], expected_red[interior], atol=1e-4)


def test_warp_bands_matches_a_whole_frame_map():
    """Banding must not change the result: the band-map output equals a
    single whole-frame remap with the same formula (identity placement)."""
    height, width = 300, 240
    geometry = _geometry_dict(-0.015, width, height)
    linear = decode_to_linear(
        encode_from_linear(
            np.random.default_rng(1).uniform(0.1, 0.9, (height, width, 3)).astype(np.float32)
        )
    ).astype(np.float32)
    ones = np.ones((height, width), dtype=np.uint8)
    identity = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])

    warped, _mask = _warp_bands(linear, ones, identity, width, height, geometry, None)

    K, _ = _geometry_camera(geometry)
    fx, fy, cx, cy = K[0, 0], K[1, 1], K[0, 2], K[1, 2]
    ys, xs = np.mgrid[0:height, 0:width]
    x = (xs - cx) / fx
    y = (ys - cy) / fy
    r2 = x * x + y * y
    k = 1.0 + geometry["k1"] * r2
    map_x = (x * k * fx + cx).astype(np.float32)
    map_y = (y * k * fy + cy).astype(np.float32)
    expected = cv2.remap(
        linear, map_x, map_y, cv2.INTER_LANCZOS4,
        borderMode=cv2.BORDER_CONSTANT, borderValue=0,
    )
    assert np.allclose(warped, expected, atol=1e-4)


def test_estimate_peak_bytes_grows_by_exactly_the_geometry_terms():
    args = ((1000, 800), (500, 700), (1000, 800), 3)
    base = estimate_peak_bytes(*args)
    with_geometry = estimate_peak_bytes(*args, geometry=True)
    with_maps = estimate_peak_bytes(*args, geometry=True, ca_maps=True)

    frame_pixels = 500 * 700
    band_maps = 3 * GEOMETRY_BAND_ROWS * 800 * 2 * 4

    def live(extra: int) -> int:
        canvas = 1000 * 800
        bbox = 1000 * 800
        count = 3
        feather_scratch = bbox * 4 * 3
        return max(
            canvas * 3 * 4  # accum
            + canvas * 4  # weight
            + 500 * 700 * 3 * 2
            + 500 * 700 * 3 * 4  # source
            + count * (bbox * 3 * 4 + bbox * 2)  # all_warped
            + feather_scratch
            + extra,
            canvas * 3 * 4  # accum
            + canvas * 4  # weight
            + canvas * 3 * 4  # log density
            + canvas * 3 * 4  # normalized
            + canvas * 3 * 2,  # result
        )

    assert with_geometry == math.ceil((live(0) + band_maps) * MEMORY_SAFETY_FACTOR)
    assert base == math.ceil(live(0) * MEMORY_SAFETY_FACTOR)
    assert with_maps == math.ceil(
        (live(0) + band_maps + frame_pixels * 4) * MEMORY_SAFETY_FACTOR
    )


def _build_two_by_two_scene(*, overlap=1.0 / 3.0, seed=11):
    """A known scene cut into a 2x2 grid of overlapping frames at the
    plan's target geometry (step = 2/3 of the frame on both axes), plus
    the ground-truth pairs and solved layout needed to composite them —
    the 2D analogue of `_build_two_frame_scene`."""
    frame_height, frame_width = _FRAME_SIZE
    step_x = round(frame_width * (1.0 - overlap))
    step_y = round(frame_height * (1.0 - overlap))
    scene = synthetic_scene(
        frame_height + step_y, frame_width + step_x, seed=seed
    )
    names = ["f0", "f1", "f2", "f3"]
    uint16_frames = {
        name: encode_from_linear(np.stack([frame, frame, frame], axis=-1))
        for name, frame in zip(
            names,
            [
                scene[0:frame_height, 0:frame_width],
                scene[0:frame_height, step_x : step_x + frame_width],
                scene[step_y : step_y + frame_height, 0:frame_width],
                scene[step_y : step_y + frame_height, step_x : step_x + frame_width],
            ],
            strict=True,
        )
    }

    poses = {
        name: np.hstack(
            [np.eye(2), np.array([i % 2 * step_x, i // 2 * step_y], dtype=np.float64).reshape(2, 1)]
        )
        for i, name in enumerate(names)
    }
    pairs = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            pairs.append(
                _ground_truth_similarity_pair_composite(
                    names[i], names[j], poses[names[i]], poses[names[j]], seed=i * 10 + j
                )
            )
    layout = solve_layout(
        names, _FRAME_SIZE, pairs, grid=GridSpec(across=2, down=2)
    )
    return scene, names, uint16_frames, layout, poses


def _ground_truth_similarity_pair_composite(name_a, name_b, pose_a, pose_b, *, seed=0):
    """PairResult for two axis-aligned poses, for the 2x2 fixture."""
    rng = np.random.default_rng(seed)
    height, width = _FRAME_SIZE
    u_ab = pose_b[:, 2] - pose_a[:, 2]
    transform = np.hstack([np.eye(2), u_ab.reshape(2, 1)])
    pts_b = rng.uniform([0, 0], [width, height], size=(100, 2))
    pts_a = pts_b + u_ab
    return PairResult(
        a=name_a,
        b=name_b,
        transform=transform,
        good_matches=100,
        inliers=100,
        inlier_ratio=1.0,
        rms_residual_px=0.0,
        scale_drift=0.0,
        accepted=True,
        reject_code=None,
        reject_message=None,
        inlier_points_a=pts_a,
        inlier_points_b=pts_b,
        overlap_fraction=None,
        overlap_mad=None,
        overlap_mad_pregain=None,
        similarity_transform=transform,
        similarity_scale=1.0,
    )


def test_two_by_two_scene_reconstructs_and_misregistration_is_bounded():
    """§5.3: a synthetic 2x2 scene with a known pattern reconstructs it
    through the separable feather, and a deliberate 3 px misregistration
    in one cell produces a bounded step rather than a widening blur toward
    the canvas corners.

    The misregistration metric isolates the across-seam profile, because
    the defect's amplitude legitimately tapers with the product ramp:
    diff = frac3 * err, where frac3 is f3's normalized weight fraction and
    err the 3 px content-shift error. f2 and f3 share their down-extent,
    so their down-ramp factors cancel in frac3 and the across-seam profile
    is y-invariant below the four-way band — that y-invariance is the
    section 5.1 guarantee, measured row by row. Inside the four-way band
    (the vertical overlap between the rows) frac3 is suppressed by f3's
    down-ramp, so the defect fades out toward the corner; the test asserts
    that taper rather than being confused by it."""
    _scene, _names, uint16_frames, layout, _poses = _build_two_by_two_scene()
    assert layout.grid_axes is not None  # solved into a grid

    result = composite(
        layout,
        lambda name: uint16_frames[name],
        cancel=CancellationToken(),
        on_progress=lambda: None,
    )
    linear_result = _unnormalize(result.image, result.bounds)

    # Map canvas coordinates back to the scene through f0's solved
    # placement (f0 is unrotated in this fixture).
    matrix_solved = layout.placements[0].matrix()
    rotation_solved_inv = matrix_solved[:, :2].T
    canvas_to_scene = np.hstack(
        [rotation_solved_inv, (-rotation_solved_inv @ matrix_solved[:, 2]).reshape(2, 1)]
    )
    scene_height, scene_width = _scene.shape
    reconstructed = cv2.warpAffine(
        linear_result,
        canvas_to_scene,
        (scene_width, scene_height),
        flags=cv2.INTER_LANCZOS4,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )[:, :, 0]

    # Compare over f0's interior (an axis-aligned rect in scene space),
    # shrunk past the erosion margin. The overlap bands are inside it.
    frame_height, frame_width = _FRAME_SIZE
    margin = 25
    mean_absolute_error = float(
        np.mean(
            np.abs(
                _scene[
                    margin : frame_height - margin, margin : frame_width - margin
                ]
                - reconstructed[
                    margin : frame_height - margin, margin : frame_width - margin
                ]
            )
        )
    )
    assert mean_absolute_error < 0.02

    # A deliberate 3 px misregistration of one cell: the solved placement
    # of f3 shifts 3 px along the across axis.
    misplaced = dataclasses.replace(
        layout.placements[3],
        translation=(
            layout.placements[3].translation[0] + 3.0,
            layout.placements[3].translation[1],
        ),
    )
    mis_layout = dataclasses.replace(
        layout, placements=layout.placements[:3] + [misplaced]
    )
    mis_result = composite(
        mis_layout,
        lambda name: uint16_frames[name],
        cancel=CancellationToken(),
        on_progress=lambda: None,
    )
    fill_code = encode_normalized(np.full((1, 1, 3), NORMALIZED_FILL))[0, 0]
    covered = np.any(result.image != fill_code, axis=-1) & np.any(
        mis_result.image != fill_code, axis=-1
    )
    # Compare in linear space against *common* bounds — each composite's
    # own meters shift slightly, which would otherwise double-count as
    # defect.
    diff = np.mean(
        np.abs(
            _unnormalize(mis_result.image, result.bounds)
            - np.asarray(linear_result)
        ),
        axis=-1,
    )

    def defect_width(y: int, threshold: float = 0.005) -> int:
        return int(np.count_nonzero(covered[y] & (diff[y] > threshold)))

    # The four-way band is the vertical overlap between the rows:
    # [step_y, frame_height) = [333, 500). Below it, only the bottom row's
    # frames cover, and their shared down-extent makes the across-seam
    # profile y-invariant.
    step_y = round(frame_height * 2 / 3)
    four_way_rows = list(range(step_y + 7, frame_height - 7, 24))
    below_band_rows = list(range(frame_height + 10, layout.canvas_size[1] - 10, 24))
    assert four_way_rows and below_band_rows

    # The taper: inside the four-way band the defect is suppressed — every
    # row's width there stays at or below the smallest below-band width.
    taper_widths = [defect_width(y) for y in four_way_rows]
    band_widths = [defect_width(y) for y in below_band_rows]
    assert max(taper_widths) <= min(band_widths) + max(
        0.1 * min(band_widths), 3
    )

    # The guarantee: below the four-way band the across-seam defect band's
    # width is constant up to content variation — the same tolerance shape
    # the strip analogue (0.3 * max) grants.
    assert min(band_widths) > 0
    assert max(band_widths) - min(band_widths) < 0.3 * max(band_widths)
# --- rectified-space compositing (docs/RECTIFICATION_PLAN.md section 6) -----

_RECT_SCENE_SIZE = (1100, 1900)
_RECT_FRAME_SIZE = (800, 1200)
_RECT_SHIFT = (400.0, 0.0)
_RECT_TILT_DEG = (12.0, 8.0)
_RECT_FOCAL_PX = 9000.0


def _rectification():
    from scanny_boy.registration import Rectification

    height, width = _RECT_FRAME_SIZE
    return Rectification(
        l=np.array(
            [
                np.tan(np.deg2rad(_RECT_TILT_DEG[0])) / _RECT_FOCAL_PX,
                np.tan(np.deg2rad(_RECT_TILT_DEG[1])) / _RECT_FOCAL_PX,
            ]
        ),
        centre=np.array([width / 2.0, height / 2.0]),
        frame_size=_RECT_FRAME_SIZE,
        rms_before_px=1.0,
        rms_after_px=0.5,
        relative_improvement=0.5,
        pair_count=2,
    )


def _build_rectified_scene():
    """A rectified-space scene; each frame is a *tilted capture* of it —
    frame pixel p samples the scene at W(p) + shift·k — so the ground-truth
    placement in rectified space is a pure translation and the canvas is
    the scene itself. The scene is deliberately smooth: a sharp one would
    put the capture's own resample error where the misregistration signal
    belongs. Returns (scene, rectification, uint16 frames, layout,
    min_xy) where canvas px c corresponds to scene px c + min_xy."""
    from scanny_boy.layout import FramePlacement, Layout
    from scanny_boy.registration import rectified_frame_corners, rectify

    rect = _rectification()
    rng = np.random.default_rng(5)
    scene = cv2.GaussianBlur(
        rng.uniform(0.0, 1.0, size=_RECT_SCENE_SIZE), (0, 0), 12
    )

    height, width = _RECT_FRAME_SIZE
    ys, xs = np.mgrid[0:height, 0:width]
    pts = np.stack([xs, ys], axis=-1).reshape(-1, 2).astype(np.float64)
    uint16_frames = {}
    for k, name in enumerate(["f0", "f1"]):
        scene_xy = rectify(pts, rect) + np.asarray(_RECT_SHIFT) * k
        map_x = scene_xy[:, 0].reshape(height, width).astype(np.float32)
        map_y = scene_xy[:, 1].reshape(height, width).astype(np.float32)
        sampled = cv2.remap(
            scene,
            map_x,
            map_y,
            cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
        uint16_frames[name] = encode_from_linear(
            np.stack([sampled, sampled, sampled], axis=-1)
        )

    quads = np.vstack(
        [
            rectified_frame_corners(rect) + np.asarray(_RECT_SHIFT) * k
            for k in range(2)
        ]
    )
    min_xy = quads.min(axis=0)
    canvas_size = (
        math.ceil(quads[:, 0].max() - min_xy[0]),
        math.ceil(quads[:, 1].max() - min_xy[1]),
    )
    placements = [
        FramePlacement(
            name=f"f{k}",
            rotation_deg=0.0,
            translation=tuple(np.asarray(_RECT_SHIFT) * k - min_xy),
            scale=1.0,
        )
        for k in range(2)
    ]
    layout = Layout(
        placements=placements,
        canvas_size=canvas_size,
        global_rms_px=0.0,
        used_pairs=[],
        strip_spread_ratio=0.0,
        strip_axis=None,
    )
    return scene, rect, uint16_frames, layout, min_xy


def _rectified_composite(layout, uint16_frames, rectification):
    def load_frame(name):
        return uint16_frames[name]

    return composite(
        layout,
        load_frame,
        cancel=CancellationToken(),
        on_progress=lambda: None,
        rectification=rectification,
    )


def _rectified_error(result, scene, min_xy):
    """Mean absolute error over a window measured in *scene* coordinates —
    canvas px c corresponds to scene px c + min_xy — and inside both."""
    linear_result = _unnormalize(result.image, result.bounds)
    margin = 30
    sx0 = margin
    sy0 = margin
    frame_height, frame_width = _RECT_FRAME_SIZE
    window_w = frame_width - 2 * margin
    window_h = frame_height - 2 * margin
    cx0 = round(sx0 - min_xy[0])
    cy0 = round(sy0 - min_xy[1])
    crop = linear_result[cy0 : cy0 + window_h, cx0 : cx0 + window_w, 0]
    scene_crop = scene[sy0 : sy0 + window_h, sx0 : sx0 + window_w]
    return float(np.mean(np.abs(crop - scene_crop)))


def test_rectified_composite_reconstructs_the_scene():
    scene, rect, uint16_frames, layout, min_xy = _build_rectified_scene()

    result = _rectified_composite(layout, uint16_frames, rect)

    error = _rectified_error(result, scene, min_xy)
    assert error < 0.02


def test_the_same_scene_without_the_rectification_misaligns():
    """The control: the same tilted captures composited without the
    rectification sample the source at p instead of W(p), a systematic
    misregistration that grows toward the frame corners — the seam smear
    the fit exists to remove."""
    scene, rect, uint16_frames, layout, min_xy = _build_rectified_scene()

    without = _rectified_composite(layout, uint16_frames, None)
    with_rect = _rectified_composite(layout, uint16_frames, rect)

    error_without = _rectified_error(without, scene, min_xy)
    error_with = _rectified_error(with_rect, scene, min_xy)
    assert error_without > 3.0 * error_with


def test_warp_bands_rectification_samples_the_unrectified_source():
    """Step 1.5 pinned directly: with an identity placement and no
    geometry, the band map must read the source at unrectify(output px)."""
    from scanny_boy.composite import _warp_bands
    from scanny_boy.registration import Rectification, unrectify

    height, width = 60, 80
    # Strong enough that the displacement clears cv2.remap's 1/32-px map
    # quantization: the interior deviation must be well above the
    # quantization noise and far below the signal.
    rect = Rectification(
        l=np.array([3e-4, -2e-4]),
        centre=np.array([width / 2.0, height / 2.0]),
        frame_size=(height, width),
        rms_before_px=1.0,
        rms_after_px=0.5,
        relative_improvement=0.5,
        pair_count=2,
    )
    xs = np.tile(np.arange(width, dtype=np.float64), (height, 1))
    linear = np.repeat((xs / width)[..., None], 3, axis=-1).astype(np.float32)
    ones = np.ones((height, width), dtype=np.uint8)
    identity = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])

    warped, warped_mask = _warp_bands(
        linear, ones, identity, width, height, None, None, rect
    )

    ys, xs = np.mgrid[0:height, 0:width]
    pts = np.stack([xs, ys], axis=-1).reshape(-1, 2).astype(np.float64)
    expected_x = unrectify(pts, rect)[:, 0].reshape(height, width) / width
    deviation = np.abs(
        warped[8 : height - 8, 8 : width - 8, 0]
        - expected_x[8 : height - 8, 8 : width - 8]
    )
    # cv2.remap quantizes its map to 1/32 px: 1/64 px of position error on
    # this 1/80-slope ramp is ~3.1e-4 — the deviation must sit at that
    # level, not grow with the displacement.
    assert deviation.max() < 5e-4
    # ...and the warp must genuinely differ from the identity map the
    # no-rectification path would produce.
    plain, _ = _warp_bands(linear, ones, identity, width, height, None, None, None)
    assert np.abs(plain - warped).max() > 1e-3
    assert np.all(warped_mask[8 : height - 8, 8 : width - 8] == 1)


def test_warp_bands_rectification_matches_zero_coefficient_geometry():
    """Two code paths, one answer: rectification with geometry=None must be
    bit-identical to rectification with a zero-coefficient geometry, since
    the normalise/distort steps reduce to the identity there."""
    from scanny_boy.composite import _warp_bands

    height, width = 60, 80
    rect = _rectification()
    linear = np.random.default_rng(3).uniform(
        0.0, 1.0, size=(height, width, 3)
    ).astype(np.float32)
    ones = np.ones((height, width), dtype=np.uint8)
    identity = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])

    without_geometry, mask_without = _warp_bands(
        linear, ones, identity, width, height, None, None, rect
    )
    zero_geometry, mask_zero = _warp_bands(
        linear,
        ones,
        identity,
        width,
        height,
        {"fx": 1.0, "fy": 1.0, "cx": 0.0, "cy": 0.0, "k1": 0.0, "k2": 0.0},
        None,
        rect,
    )

    assert np.array_equal(without_geometry, zero_geometry)
    assert np.array_equal(mask_without, mask_zero)


def test_peak_estimate_counts_band_maps_for_rectification_without_geometry():
    # Enough frames that the warp-residency branch of the max() dominates
    # the normalization branch — otherwise the band-map term is invisible.
    canvas = (2000, 1500)
    frame = (400, 600)
    bbox = (500, 600)

    base = estimate_peak_bytes(canvas, frame, bbox, 30)
    with_rect = estimate_peak_bytes(
        canvas, frame, bbox, 30, rectification=True
    )
    with_geometry = estimate_peak_bytes(canvas, frame, bbox, 30, geometry=True)
    with_both = estimate_peak_bytes(
        canvas, frame, bbox, 30, geometry=True, rectification=True
    )

    # The band maps are counted once, whichever feature routed the warp.
    assert with_rect == with_geometry
    assert with_rect > base
    # Geometry already accounts for the maps; rectification adds nothing.
    assert with_both == with_geometry
