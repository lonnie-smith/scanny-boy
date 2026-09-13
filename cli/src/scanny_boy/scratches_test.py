"""Tests for scratch detection and correction (`scratches.py`)."""

from __future__ import annotations

import numpy as np
import pytest

from scanny_boy import normalization, scratches
from scanny_boy.library import repo
from scanny_boy.synthetic_scene_support import synthetic_scene

H, W = 640, 800
SPANS = (1.2, 1.2, 1.2)
SEEDS = (1, 7, 13)


def _encoded_scene(height: int, width: int, *, seed: int) -> np.ndarray:
    scene = synthetic_scene(height, width, seed=seed).copy()
    # Keep a gentle scene gradient for clean-scene tests; scratch injection
    # uses a locally flat patch so the step-edge gate is not tripped by the
    # background alone.
    val = (0.15 + 0.35 * scene).astype(np.float32)
    val = np.stack([val, val, val], axis=-1)
    return normalization.encode_normalized(val).astype(np.uint16)


def _flat_encoded(height: int, width: int, level: float = 0.45) -> np.ndarray:
    val = np.full((height, width, 3), level, dtype=np.float32)
    return normalization.encode_normalized(val).astype(np.uint16)


def _decode(codes: np.ndarray) -> np.ndarray:
    return normalization.decode_normalized(codes).astype(np.float32)


def inject_vertical_scratch(
    codes: np.ndarray,
    x: float,
    *,
    seed: int = 0,
    depth_r: float = 0.10,
    depth_g: float = 0.085,
    depth_b: float = 0.020,
    flat: bool = True,
    tilt_px_per_row: float = 0.0,
    walk_sigma: float = 0.03,
) -> np.ndarray:
    """Inject a vertical scratch with blue-selective chroma (§1 profile)."""
    if flat:
        val = np.full(codes.shape, 0.45, dtype=np.float32)
    else:
        val = _decode(codes).copy()
    rng = np.random.default_rng(seed)
    walk = np.cumsum(rng.normal(0, walk_sigma, val.shape[0])).astype(np.float32)
    walk = np.clip(walk - walk.mean(), -15.0, 15.0)
    y_coords = np.arange(val.shape[0], dtype=np.float32)
    y0 = (val.shape[0] - 1) / 2.0
    for y in range(val.shape[0]):
        cx = round(x + tilt_px_per_row * (y - y0) + walk[y])
        for dx in range(-20, 21):
            xi = cx + dx
            if not 0 <= xi < val.shape[1]:
                continue
            au = abs(dx)
            if au <= 2:
                # Blue attenuated most: val_B below bg, R/G slightly above.
                val[y, xi, 0] += depth_r
                val[y, xi, 1] += depth_g
                val[y, xi, 2] -= depth_b
    val = np.clip(
        val,
        -normalization.NORMALIZED_HEADROOM_LOW,
        1.0 + normalization.NORMALIZED_HEADROOM_HIGH,
    )
    return normalization.encode_normalized(val).astype(np.uint16)


BG_START = scratches.BG_START
BG_END = scratches.BG_END


def test_matched_kernel_is_zero_sum():
    kernel = scratches._matched_kernel()
    assert abs(float(kernel.sum())) < 1e-6
    assert kernel[scratches.BG_END - 2 : scratches.BG_END + 3].sum() == pytest.approx(
        1.0
    )


def _broken_matched_kernel() -> np.ndarray:
    """The pre-fix kernel (DC = −1) for regression tests."""
    kernel = np.zeros(2 * BG_END + 1, dtype=np.float32)
    kernel[BG_END - 2 : BG_END + 3] = 1.0 / 5.0
    for u in range(BG_START, BG_END + 1):
        kernel[BG_END + u] -= 1.0 / (BG_END - BG_START + 1)
        kernel[BG_END - u] -= 1.0 / (BG_END - BG_START + 1)
    return kernel


def _band_response_with_kernel(s: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    height, width = s.shape
    n_bands = height // scratches.BAND_PX
    response = np.zeros((n_bands, width), dtype=np.float32)
    pad = kernel.shape[0] // 2
    for b in range(n_bands):
        row_start = b * scratches.BAND_PX
        row_end = min(row_start + scratches.BAND_PX, height)
        rows = np.arange(row_start, row_end, 2)
        if len(rows) == 0:
            continue
        avg = s[rows].mean(axis=0)
        response[b] = np.correlate(np.pad(avg, pad, mode="reflect"), kernel, mode="valid")
    return response


def test_zero_sum_kernel_finds_scratch_under_chroma_gradient():
    """The old DC=−1 kernel drowned scratch z-scores on real film gradients."""
    height, width = 1280, 1600
    spans = np.asarray(SPANS, dtype=np.float32)
    val = np.full((height, width, 3), 0.45, dtype=np.float32)
    grad = np.linspace(-0.4, 0.4, width, dtype=np.float32)
    val[..., 2] += grad[np.newaxis, :]
    val[..., 0] -= grad[np.newaxis, :] * 0.5
    target_x = int(width * 0.52)
    for y in range(height):
        for dx in range(-2, 3):
            xi = target_x + dx
            if 0 <= xi < width:
                val[y, xi, 0] += 0.10
                val[y, xi, 1] += 0.085
                val[y, xi, 2] -= 0.020
    s = (
        spans[2] * val[..., 2]
        - (spans[0] * val[..., 0] + spans[1] * val[..., 1]) / 2.0
    )
    z_fixed = scratches._normalize_response(
        _band_response_with_kernel(s, scratches._matched_kernel())
    )
    z_broken = scratches._normalize_response(
        _band_response_with_kernel(s, _broken_matched_kernel())
    )
    assert float(z_fixed[:, target_x].mean()) <= -3.0
    assert float(z_broken[:, target_x].mean()) > -1.5


def test_detect_accepts_shallow_tilted_scratch():
    import math

    height, width = 1280, 1600
    clean = _flat_encoded(height, width)
    # ~0.7° tilt over the canvas height plus ±15 px residual wander.
    tilt = math.tan(math.radians(0.7))
    target_x = width * 0.5
    scratched = inject_vertical_scratch(
        clean,
        x=target_x,
        seed=21,
        tilt_px_per_row=tilt,
        walk_sigma=0.03,
    )
    candidates = scratches.detect(scratched, SPANS)
    vertical = [c for c in candidates if c.axis == "vertical"]
    assert vertical
    cx = vertical[0].centres[len(vertical[0].centres) // 2]
    assert abs(cx - target_x) <= 25.0


def test_detect_rejects_snaking_path():
    clean = _flat_encoded(H, W)
    val = np.full((H, W, 3), 0.45, dtype=np.float32)
    rng = np.random.default_rng(22)
    x_base = W // 2
    walk = np.cumsum(rng.normal(0, 4.0, H)).astype(np.float32)
    walk = np.clip(walk, -80.0, 80.0)
    for y in range(H):
        cx = round(x_base + walk[y])
        for dx in range(-2, 3):
            xi = cx + dx
            if 0 <= xi < W:
                val[y, xi, 0] += 0.10
                val[y, xi, 1] += 0.085
                val[y, xi, 2] -= 0.020
    scratched = normalization.encode_normalized(
        np.clip(
            val,
            -normalization.NORMALIZED_HEADROOM_LOW,
            1.0 + normalization.NORMALIZED_HEADROOM_HIGH,
        )
    ).astype(np.uint16)
    candidates = scratches.detect(scratched, SPANS)
    vertical = [c for c in candidates if c.axis == "vertical"]
    assert not vertical


def test_detect_finds_injected_vertical_scratch():
    clean = _flat_encoded(H, W)
    scratched = inject_vertical_scratch(clean, x=W * 0.52, seed=2)
    candidates = scratches.detect(scratched, SPANS)
    assert len(candidates) >= 1
    vertical = [c for c in candidates if c.axis == "vertical"]
    assert vertical
    cx = vertical[0].centres[len(vertical[0].centres) // 2]
    assert abs(cx - W * 0.52) <= 5.0


def test_detect_finds_injected_horizontal_scratch():
    clean = _flat_encoded(H, W)
    scratched = np.swapaxes(
        inject_vertical_scratch(np.swapaxes(clean, 0, 1), x=H * 0.45, seed=4),
        0,
        1,
    )
    candidates = scratches.detect(scratched, SPANS)
    horizontal = [c for c in candidates if c.axis == "horizontal"]
    assert horizontal


@pytest.mark.parametrize("seed", SEEDS)
def test_detect_clean_scene_finds_nothing(seed: int):
    clean = _encoded_scene(H, W, seed=seed)
    assert scratches.detect(clean, SPANS) == []


def test_detect_rejects_step_edge():
    codes = _encoded_scene(H, W, seed=5)
    val = _decode(codes)
    val[:, : W // 2] += 0.15
    val[:, W // 2 :] -= 0.05
    edge = normalization.encode_normalized(
        np.clip(
            val,
            -normalization.NORMALIZED_HEADROOM_LOW,
            1.0 + normalization.NORMALIZED_HEADROOM_HIGH,
        )
    ).astype(np.uint16)
    assert scratches.detect(edge, SPANS) == []


def test_detect_rejects_neutral_dark_line():
    codes = _encoded_scene(H, W, seed=6)
    val = _decode(codes)
    cx = W // 2
    val[:, cx - 1 : cx + 2] -= 0.06
    line = normalization.encode_normalized(
        np.clip(
            val,
            -normalization.NORMALIZED_HEADROOM_LOW,
            1.0 + normalization.NORMALIZED_HEADROOM_HIGH,
        )
    ).astype(np.uint16)
    assert scratches.detect(line, SPANS) == []


def test_heal_reduces_error_inside_window():
    clean = _flat_encoded(H, W)
    scratched = inject_vertical_scratch(clean, x=W * 0.5, seed=9, flat=False)
    candidates = scratches.detect(scratched, SPANS)
    assert candidates
    fits = [scratches.fit(scratched, c) for c in candidates]
    params = scratches.scratches_params((W, H), fits, enabled=True)
    healed = scratches.apply(scratched, params)
    clean_val = _decode(clean)
    healed_val = _decode(healed)
    cx = round(candidates[0].centres[len(candidates[0].centres) // 2])
    mask = np.zeros((H, W), dtype=bool)
    for y in range(H):
        for dx in range(-scratches.HALF_WIDTH_PX, scratches.HALF_WIDTH_PX + 1):
            x = cx + dx
            if 0 <= x < W:
                mask[y, x] = True
    rms = float(np.sqrt(np.mean((healed_val[mask] - clean_val[mask]) ** 2)))
    assert rms < 0.025
    # The core itself must move: a zeroed / row-count-divided table used
    # to pass the window RMS (most of ±24 px is untouched background).
    core = np.zeros((H, W), dtype=bool)
    for y in range(H):
        for dx in range(-2, 3):
            x = cx + dx
            if 0 <= x < W:
                core[y, x] = True
    scratched_val = _decode(scratched)
    before = float(np.sqrt(np.mean((scratched_val[core] - clean_val[core]) ** 2)))
    after = float(np.sqrt(np.mean((healed_val[core] - clean_val[core]) ** 2)))
    assert before > 0.02
    assert after < before * 0.4
    assert float(np.abs(fits[0].table).max()) > 0.01


def test_heal_leaves_outside_window_unchanged():
    clean = _flat_encoded(H, W)
    scratched = inject_vertical_scratch(clean, x=W * 0.5, seed=11, flat=False)
    candidates = scratches.detect(scratched, SPANS)
    fits = [scratches.fit(scratched, c) for c in candidates]
    params = scratches.scratches_params((W, H), fits, enabled=True)
    healed = scratches.apply(scratched, params)
    cx = round(candidates[0].centres[len(candidates[0].centres) // 2])
    outside = np.ones((H, W), dtype=bool)
    for y in range(H):
        for dx in range(-scratches.HALF_WIDTH_PX, scratches.HALF_WIDTH_PX + 1):
            x = cx + dx
            if 0 <= x < W:
                outside[y, x] = False
    np.testing.assert_array_equal(healed[outside], scratched[outside])


def test_apply_is_deterministic():
    clean = _encoded_scene(H, W, seed=12)
    scratched = inject_vertical_scratch(clean, x=W * 0.48, seed=13)
    candidates = scratches.detect(scratched, SPANS)
    params = scratches.scratches_params(
        (W, H), [scratches.fit(scratched, c) for c in candidates], enabled=True
    )
    once = scratches.apply(scratched.copy(), params)
    twice = scratches.apply(scratched.copy(), params)
    np.testing.assert_array_equal(once, twice)


def test_region_apply_matches_full_apply_slice():
    clean = _encoded_scene(H, W, seed=14)
    scratched = inject_vertical_scratch(clean, x=W * 0.55, seed=15)
    candidates = scratches.detect(scratched, SPANS)
    params = scratches.scratches_params(
        (W, H), [scratches.fit(scratched, c) for c in candidates], enabled=True
    )
    full = scratches.apply(scratched.copy(), params)
    y, h = 100, 200
    x, w = 200, 300
    row_m = scratches.REGION_ROW_MARGIN
    col_m = scratches.REGION_COL_MARGIN
    padded = scratched[
        max(0, y - row_m) : min(H, y + h + row_m),
        max(0, x - col_m) : min(W, x + w + col_m),
    ]
    inner_x = x - max(0, x - col_m)
    inner_y = y - max(0, y - row_m)
    region = scratches.apply(
        padded.copy(),
        params,
        region=(inner_x, inner_y, w, h),
        origin=(max(0, x - col_m), max(0, y - row_m)),
    )
    np.testing.assert_array_equal(region, full[y : y + h, x : x + w])


def test_scratches_params_round_trip():
    clean = _encoded_scene(H, W, seed=16)
    scratched = inject_vertical_scratch(clean, x=W * 0.5, seed=17)
    candidates = scratches.detect(scratched, SPANS)
    params = scratches.scratches_params(
        (W, H), [scratches.fit(scratched, c) for c in candidates], enabled=True
    )
    validated = repo.validated_scratches_params(params)
    assert validated["detector_version"] == scratches.DETECTOR_VERSION
    assert validated["enabled"] is True
    assert validated["canvas"] == [W, H]


def test_is_live_false_on_canvas_mismatch():
    params = {
        "enabled": True,
        "canvas": [100, 100],
        "scratches": [{"axis": "vertical"}],
    }
    assert scratches.is_live(params, (H, W)) is False


def test_apply_no_op_on_2d_image():
    clean = _encoded_scene(H, W, seed=18)
    scratched = inject_vertical_scratch(clean, x=W * 0.5, seed=19)
    candidates = scratches.detect(scratched, SPANS)
    params = scratches.scratches_params(
        (W, H), [scratches.fit(scratched, c) for c in candidates], enabled=True
    )
    gray = np.full((H, W), 30000, dtype=np.uint16)
    np.testing.assert_array_equal(scratches.apply(gray, params), gray)
