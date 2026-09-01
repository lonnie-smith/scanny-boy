import dataclasses
import math

import cv2
import numpy as np
import pytest

from scanny_boy.cancellation import CancellationToken
from scanny_boy.composite import (
    FILL_COLOR,
    MAX_CANVAS_DIMENSION,
    MAX_STITCHED_BYTES,
    MEMORY_SAFETY_FACTOR,
    check_memory_budget,
    check_output_size,
    composite,
    estimate_peak_bytes,
)
from scanny_boy.events import Code
from scanny_boy.layout import solve_layout
from scanny_boy.registration import PairResult, StitchError
from scanny_boy.linear import decode_to_linear, encode_from_linear
from scanny_boy.synthetic_scene_support import cut_frames, synthetic_scene

_SCENE_SIZE = (700, 1300)
_FRAME_SIZE = (500, 700)  # (height, width)


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
    phi_ab_deg = np.degrees(np.arctan2(rotation_b[1, 0], rotation_b[0, 0])) - np.degrees(
        np.arctan2(rotation_a[1, 0], rotation_a[0, 0])
    )
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

    linear_result = decode_to_linear(result.image).astype(np.float32)
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

    # Both frames carry a single known uniform linear value. If the weights
    # inside the overlap zone summed to anything other than 1 (e.g. a
    # forgotten division, summing instead of averaging), the blended
    # region would come out brighter or darker than this constant.
    constant_value = 0.4
    height, width = _FRAME_SIZE
    constant_frame = encode_from_linear(
        np.full((height, width, 3), constant_value, dtype=np.float32)
    )
    uniform_frames = {name: constant_frame for name in names}

    result = _composite(layout, uniform_frames)
    linear = decode_to_linear(result.image)

    covered = np.any(result.image != 0, axis=-1) | np.all(
        np.abs(linear - constant_value) < 0.05, axis=-1
    )
    assert np.count_nonzero(covered) > 0
    assert np.max(np.abs(linear[covered] - constant_value)) < 0.01


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

    # A uniform frame close to each end of the linear range. Lanczos4
    # undershoots below 0 and can overshoot above 1 near a warped frame's
    # own border (section 2.3); without composite.py's clamp, an
    # undershoot could drag the weighted average below the true value and
    # get silently zeroed by encode_from_linear's own clamp, or an
    # unclamped negative could otherwise corrupt the blend.
    bright_frame = encode_from_linear(
        np.full((height, width, 3), 0.98, dtype=np.float32)
    )
    dark_frame = encode_from_linear(np.full((height, width, 3), 0.02, dtype=np.float32))

    bright_result = _composite(layout, {names[0]: bright_frame, names[1]: bright_frame})
    dark_result = _composite(layout, {names[0]: dark_frame, names[1]: dark_frame})

    bright_linear = decode_to_linear(bright_result.image)
    dark_linear = decode_to_linear(dark_result.image)

    bright_covered = np.any(bright_result.image != 0, axis=-1)
    dark_covered = np.any(dark_result.image != 0, axis=-1)

    # A corrupted (unclamped) undershoot would drag the bright frame's
    # covered pixels well below 0.98; an unclamped overshoot/wraparound
    # would drag the dark frame's covered pixels well above 0.02.
    assert np.min(bright_linear[bright_covered]) > 0.5
    assert np.max(dark_linear[dark_covered]) < 0.5


def test_uncovered_pixels_are_exactly_fill_color():
    _scene, _names, uint16_frames, layout, _cut = _build_two_frame_scene()
    result = _composite(layout, uint16_frames)

    canvas_width, canvas_height = layout.canvas_size
    corners = [
        (0, 0),
        (canvas_width - 1, 0),
        (0, canvas_height - 1),
        (canvas_width - 1, canvas_height - 1),
    ]
    for x, y in corners:
        assert tuple(int(v) for v in result.image[y, x]) == FILL_COLOR


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
    source = frame_pixels * 3 * 2 + frame_pixels * 3 * 4
    warped = bbox_pixels * 3 * 4
    warp_aux = bbox_pixels * 4 + bbox_pixels * 2

    all_warped = 2 * (warped + warp_aux)
    live_bytes = max(accum + weight + source + all_warped, accum + result)

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
