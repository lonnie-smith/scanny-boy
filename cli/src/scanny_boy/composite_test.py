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
    axis = (1.0, 0.0)
    canvas_a, canvas_b = _place_on_canvas(
        _feather_weight(mask_a, 0, 0, axis),
        _feather_weight(mask_b, b_offset, 0, axis),
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
        _feather_weight(mask_a, 0, 0, None),
        _feather_weight(mask_b, b_offset, 0, None),
        b_offset, height, mask_a.shape[1], mask_b.shape[1],
    )
    old_border = contribution(old_canvas_a, old_canvas_b, row_border)
    old_mid = contribution(old_canvas_a, old_canvas_b, row_mid)
    assert old_border == pytest.approx(0.5, abs=0.02)
    assert abs(old_border - old_mid) > 0.1


def test_transition_band_width_does_not_grow_toward_the_border():
    from scanny_boy.composite import MASK_ERODE_PX, _feather_weight

    mask_a, mask_b, b_offset, height, _x_pick = _hand_built_strip_masks()
    axis = (1.0, 0.0)
    canvas_a, canvas_b = _place_on_canvas(
        _feather_weight(mask_a, 0, 0, axis),
        _feather_weight(mask_b, b_offset, 0, axis),
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
    actual = _feather_weight(mask, bbox_x=3, bbox_y=7, axis=None)
    assert np.array_equal(actual, expected.astype(np.float32))


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
    warp_aux = bbox_pixels * 4 + bbox_pixels * 2
    feather_scratch = bbox_pixels * 4 * 2

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
        from scanny_boy.composite import _EROSION_KERNEL, _feather_weight, _frame_bbox

        x, y, w, h = _frame_bbox(matrix, src_h, src_w, layout.canvas_size)
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
        weight = _feather_weight(eroded, x, y, layout.strip_axis)
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
        feather_scratch = bbox * 4 * 2
        return max(
            canvas * 3 * 4  # accum
            + canvas * 4  # weight
            + 500 * 700 * 3 * 2
            + 500 * 700 * 3 * 4  # source
            + count * (bbox * 3 * 4 + bbox * 4 + bbox * 2)  # all_warped
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
