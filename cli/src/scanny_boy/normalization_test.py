"""Tests for the normalization transfer, the encode, the meters, and the
rebate detector (docs/DECISIONS.md, "Normalization decisions").

The golden-value tests against NegPy's own implementation — the
highest-value tests in the plan, since the port is subtle and NegPy is the
reference — need the `negpy` package importable; they skip when it is not
installed, and run with the rebate detector disabled (NegPy has no
equivalent).
"""

from __future__ import annotations

import numpy as np
import pytest

from scanny_boy import normalization as nz
from scanny_boy.normalization import (
    ANALYSIS_GRID,
    NORMALIZED_HEADROOM_HIGH,
    NORMALIZED_HEADROOM_LOW,
    Bounds,
    NormalizationError,
    analyze_bounds,
    block_median_grid,
    build_params,
    clamp_bounds,
    decode_normalized,
    detect_rebate,
    encode_normalized,
    headroom_clip_fractions,
    measure_anchor,
    measure_clip_fractions,
    measure_shadow_refs,
    measure_textural_range,
    normalize_log_image,
    observed_extrema,
    resolve_analysis_region,
    to_log_density,
    withhold_dense_border,
)

FILL_LOG = -6.0  # what an uncovered canvas pixel becomes: log10(1e-6)


# --- N-1: the transfer ------------------------------------------------------


def test_to_log_density_clamps_nan_and_infinities_to_the_bounds():
    values = np.array(
        [
            [np.nan, np.inf, -np.inf],
            [0.5, 0.0, 2.0],
        ],
        dtype=np.float32,
    )
    result = to_log_density(values)
    # NaN and -inf land on the floor, +inf on the ceiling — clamped to the
    # bounds, never through them.
    assert result[0, 0] == pytest.approx(np.log10(1e-6), abs=1e-6)
    assert result[0, 1] == pytest.approx(0.0, abs=1e-6)
    assert result[0, 2] == pytest.approx(np.log10(1e-6), abs=1e-6)
    assert result[1, 0] == pytest.approx(np.log10(0.5), abs=1e-6)
    assert result[1, 1] == pytest.approx(np.log10(1e-6), abs=1e-6)
    assert result[1, 2] == pytest.approx(0.0, abs=1e-6)


def test_normalize_log_image_maps_floor_and_ceil_exactly_and_is_unclamped():
    bounds = Bounds(floors=(-2.0, -2.1, -2.2), ceils=(-0.1, -0.2, -0.3))
    img = np.stack(
        np.meshgrid(
            np.linspace(-2.0, -0.1, 16, dtype=np.float32),
            np.linspace(-2.1, -0.2, 16, dtype=np.float32),
            np.linspace(-2.2, -0.3, 16, dtype=np.float32),
            indexing="ij",
        ),
        axis=-1,
    )
    normalized = normalize_log_image(img, bounds)
    for channel in range(3):
        assert float(normalized[..., channel].min()) == pytest.approx(0.0, abs=1e-6)
        assert float(normalized[..., channel].max()) == pytest.approx(1.0, abs=1e-6)

    # Unclamped outside: a value denser than the floor goes negative, a
    # value thinner than the ceiling goes above one.
    beyond = np.array([[[[-2.5, -0.05, -0.2]]]], dtype=np.float32)[0]
    normalized_beyond = normalize_log_image(beyond, bounds)
    assert normalized_beyond[0, 0, 0] < 0.0
    assert normalized_beyond[0, 0, 1] > 1.0


def test_degenerate_ceil_equals_floor_channel_does_not_divide_by_zero():
    bounds = Bounds(floors=(-1.0, -1.0, -1.0), ceils=(-1.0, -0.5, -1.0))
    img = np.full((4, 4, 3), -1.0, dtype=np.float32)
    normalized = normalize_log_image(img, bounds)
    assert np.all(np.isfinite(normalized))
    # The sign-preserving epsilon guard: a zero span maps to 0, not NaN.
    assert np.all(normalized[..., 0] == 0.0)


# --- N-1: the encode --------------------------------------------------------


def test_decode_then_encode_round_trips_every_representable_code():
    codes = np.arange(65536, dtype=np.uint16)
    decoded = decode_normalized(codes)
    reencoded = encode_normalized(decoded)
    # uint16 comparison: exact for every code, the way linear_test.py
    # proves it for the linear transfer.
    assert np.array_equal(reencoded, codes)


def test_values_at_the_headroom_rails_survive_the_encode():
    span = 1.0 + NORMALIZED_HEADROOM_LOW + NORMALIZED_HEADROOM_HIGH
    low_rail = -NORMALIZED_HEADROOM_LOW
    high_rail = 1.0 + NORMALIZED_HEADROOM_HIGH
    codes = encode_normalized(np.array([low_rail, high_rail], dtype=np.float32))
    assert int(codes[0]) == 0
    assert int(codes[1]) == 65535
    # And the decode recovers the rails exactly.
    decoded = decode_normalized(codes)
    assert float(decoded[0]) == pytest.approx(low_rail, abs=1e-6)
    assert float(decoded[1]) == pytest.approx(high_rail, abs=1e-6)

    # Beyond them they clip — documented, not accidental.
    beyond = encode_normalized(
        np.array([low_rail - 0.1, high_rail + 0.1], dtype=np.float32)
    )
    assert int(beyond[0]) == 0
    assert int(beyond[1]) == 65535
    assert span == pytest.approx(1.25)


def test_encode_clips_nan_to_the_dense_end_not_mid_scale():
    codes = encode_normalized(np.array([np.nan], dtype=np.float32))
    assert int(codes[0]) == 0


def test_observed_extrema_report_the_input_without_a_second_pass():
    values = np.array([[[-0.3, 0.2, -1.0], [0.5, -2.0, 0.0]]], dtype=np.float32)
    mins, maxs = observed_extrema(values)
    assert mins == pytest.approx((-0.3, -2.0, -1.0))
    assert maxs == pytest.approx((0.5, 0.2, 0.0))


def test_headroom_clip_fractions_count_only_the_excursions():
    values = np.zeros((100, 100, 3), dtype=np.float32)
    values[0, 0] = (-0.5, 0.0, 2.0)  # R below the low rail, B above the high
    values[0, 1] = (-0.15, 1.10, 0.5)  # R and B exactly on the rails: kept
    highlights, shadows = headroom_clip_fractions(values)
    assert highlights == pytest.approx((1 / 10000, 0.0, 0.0))
    assert shadows == pytest.approx((0.0, 0.0, 1 / 10000))


# --- N-1: the block-median prefilter ----------------------------------------


def test_block_median_grid_vanishes_a_single_hot_pixel():
    rng = np.random.default_rng(7)
    img = (rng.uniform(-2.0, -0.5, size=(2100, 2100, 3))).astype(np.float32)
    img[40, 40] = -6.0  # one hot (dense) pixel, e.g. a dust pinhole
    grid = block_median_grid(img)
    assert grid.shape == (700, 700, 3)
    # The hot pixel's own block does not contain it: the block median sits
    # inside the scene's range, not at the outlier's value.
    assert grid[13, 13].min() > -2.0


def test_block_median_grid_passthrough_below_the_grid_side():
    img = np.full((64, 48, 3), -1.0, dtype=np.float32)
    grid = block_median_grid(img)
    assert grid.shape == (64, 48, 3)
    assert np.array_equal(grid, img)


@pytest.mark.parametrize(
    ("shape", "expected_grid"),
    [
        ((1049, 1049, 3), (525, 525)),
        ((2000, 1500, 3), (1000, 750)),
        ((1024, 1024, 3), (1024, 1024)),
    ],
)
def test_analysis_grid_block_sizes_and_grid_shape(shape, expected_grid):
    block_rows, block_cols = nz.analysis_grid_block_sizes(shape)
    grid_rows = -(-shape[0] // block_rows)
    grid_cols = -(-shape[1] // block_cols)
    assert (grid_rows, grid_cols) == expected_grid
    assert grid_rows <= ANALYSIS_GRID and grid_cols <= ANALYSIS_GRID


def test_analysis_grid_bounded_for_canvas_sizes_from_1mp_to_200mp():
    # The 200MP end is only exercised through the block-size rule —
    # materializing a 200-megapixel canvas is not a fast-tier proposition.
    for width, height in [
        (1024, 1024),  # 1.0 MP
        (4000, 3000),  # 12 MP, a full-size frame
        (12000, 8000),  # 96 MP canvas
        (20000, 10000),  # 200 MP canvas
    ]:
        block_rows, block_cols = nz.analysis_grid_block_sizes((height, width, 3))
        grid_rows = -(-height // block_rows)
        grid_cols = -(-width // block_cols)
        assert grid_rows <= ANALYSIS_GRID
        assert grid_cols <= ANALYSIS_GRID


# --- N-2: the meters ---------------------------------------------------------


def _ramp_scene(
    height: int,
    width: int,
    floor_mean: float,
    ceil_mean: float,
    offsets: tuple[float, float, float],
) -> np.ndarray:
    """A log-density scene whose channels share one neutral ramp, offset per
    channel by the orange-mask cast `offsets`. Per-channel floors are
    floor_mean + offset, ceils ceil_mean + offset."""
    ramp = np.linspace(floor_mean, ceil_mean, height * width, dtype=np.float32)
    rng = np.random.default_rng(11)
    ramp = ramp[rng.permutation(height * width)].reshape(height, width)
    return np.stack([ramp + offset for offset in offsets], axis=-1)


def test_ramp_recovers_known_per_channel_floors_and_ceilings():
    """The scene's channels share one neutral ramp, offset per channel by
    the cast d = (0, +0.1, -0.1). The luma axis reads the scene shifted by
    the luma-weighted mean of the cast (LUMA_G and LUMA_B dominate), the
    colour deviations are relative to that shift, and the median recentre
    cancels median(d) = 0 — so each channel's bound lands on its own ramp
    end plus the luma shift."""
    floor_mean, ceil_mean = -2.0, -0.2
    offsets = (0.0, 0.1, -0.1)
    luma_shift = (
        nz.LUMA_R * offsets[0] + nz.LUMA_G * offsets[1] + nz.LUMA_B * offsets[2]
    )
    img = _ramp_scene(200, 200, floor_mean, ceil_mean, offsets)
    keep = np.ones(img.shape[:2], dtype=bool)
    bounds = analyze_bounds(img, keep)
    assert bounds.floors == pytest.approx(
        tuple(floor_mean + luma_shift + d for d in offsets), abs=0.02
    )
    assert bounds.ceils == pytest.approx(
        tuple(ceil_mean + luma_shift + d for d in offsets), abs=0.02
    )


def test_mono_image_yields_zero_per_channel_deviation_at_any_clip():
    img = _ramp_scene(128, 128, -2.0, -0.2, (0.0, 0.0, 0.0))
    keep = np.ones(img.shape[:2], dtype=bool)
    bounds = analyze_bounds(img, keep)
    assert bounds.floors[0] == bounds.floors[1] == bounds.floors[2]
    assert bounds.ceils[0] == bounds.ceils[1] == bounds.ceils[2]


def test_orange_mask_cast_is_recovered_and_the_normalized_output_is_neutral():
    offsets = (0.10, -0.05, -0.30)  # red thin, blue dense: the orange mask
    img = _ramp_scene(256, 256, -2.0, -0.2, offsets)
    keep = np.ones(img.shape[:2], dtype=bool)
    bounds = analyze_bounds(img, keep)
    normalized = normalize_log_image(img, bounds)
    # The three channels' normalized values coincide: the cast is gone.
    spread = normalized.max(axis=(0, 1)) - normalized.min(axis=(0, 1))
    channel_spread = np.ptp(normalized, axis=2)
    assert float(channel_spread.max()) == pytest.approx(0.0, abs=1e-4)
    del spread


def test_fill_corners_do_not_destroy_the_bounds():
    """The test that matters (section 1.5): a canvas whose uncovered corners
    sit at log10(1e-6) = -6.0 produces the *same* bounds as the same canvas
    cropped to its valid rect — and without the region restriction the
    whole stretch is garbage."""
    interior = _ramp_scene(256, 256, -2.0, -0.2, (0.0, 0.1, -0.1))
    canvas = np.full((320, 320, 3), FILL_LOG, dtype=np.float32)
    canvas[32:288, 32:288] = interior

    keep = resolve_analysis_region(canvas.shape[:2], (32, 32, 256, 256))
    bounds = analyze_bounds(canvas, keep)
    cropped_bounds = analyze_bounds(interior, np.ones(interior.shape[:2], dtype=bool))
    assert bounds.floors == pytest.approx(cropped_bounds.floors, abs=1e-5)
    assert bounds.ceils == pytest.approx(cropped_bounds.ceils, abs=1e-5)

    # And without the region, the fill wins the floor percentile.
    unguarded = analyze_bounds(canvas, np.ones(canvas.shape[:2], dtype=bool))
    assert unguarded.floors[0] < FILL_LOG + 0.5


def test_saturated_red_object_is_not_read_as_film_cast():
    """The whole reason the chroma gate exists: a dense end dominated by a
    saturated red object must not set the red floor."""
    img = _ramp_scene(256, 256, -2.0, -0.2, (0.0, 0.0, 0.0))
    # A saturated red patch at the dense end: channel 0 far denser than the
    # scene's floor, the others near the thin end.
    img[10:40, 10:40, 0] = -2.6
    img[10:40, 10:40, 1] = -0.25
    img[10:40, 10:40, 2] = -0.25

    keep = np.ones(img.shape[:2], dtype=bool)
    bounds = analyze_bounds(img, keep)
    reference = analyze_bounds(
        _ramp_scene(256, 256, -2.0, -0.2, (0.0, 0.0, 0.0)),
        np.ones((256, 256), dtype=bool),
    )
    # The red floor is not dragged to the object's density, and the bounds
    # stay close to the object-free scene's.
    assert bounds.floors[0] > -2.5
    assert bounds.floors == pytest.approx(reference.floors, abs=0.15)
    assert bounds.ceils == pytest.approx(reference.ceils, abs=0.15)


def test_degenerate_bounds_raise_normalization_error():
    img = np.full((64, 64, 3), -1.0, dtype=np.float32)
    keep = np.ones(img.shape[:2], dtype=bool)
    with pytest.raises(NormalizationError) as exc_info:
        analyze_bounds(img, keep)
    assert exc_info.value.code.value == "NORMALIZE_DEGENERATE_BOUNDS"


def test_metering_functions_measure_the_region():
    img = _ramp_scene(128, 128, -2.0, -0.2, (0.0, 0.0, 0.0))
    keep = np.ones(img.shape[:2], dtype=bool)
    shadow_refs = measure_shadow_refs(img, keep)
    # Shadow refs are the per-channel P98: near the thin end, above the
    # anchor and far above the dense end.
    assert all(-0.5 < ref <= 0.0 for ref in shadow_refs)
    anchor = measure_anchor(img, keep)
    assert -1.2 < anchor < -0.8
    textural_range = measure_textural_range(img, keep)
    # P10-P90 of a uniform ramp over [-2.0, -0.2] spans ~80% of the range.
    assert textural_range == pytest.approx(0.8 * 1.8, abs=0.1)


def test_metering_functions_ignore_excluded_cells():
    img = _ramp_scene(128, 128, -2.0, -0.2, (0.0, 0.0, 0.0))
    keep = np.ones(img.shape[:2], dtype=bool)
    # A dense outlier outside the region must not move the meters.
    img[0, 0] = -6.0
    keep[0, 0] = False
    reference = analyze_bounds(_ramp_scene(128, 128, -2.0, -0.2, (0.0, 0.0, 0.0)), keep)
    bounds = analyze_bounds(img, keep)
    assert bounds.floors == pytest.approx(reference.floors, abs=1e-5)


def test_measure_clip_fractions_counts_a_synthetic_blown_region():
    linear = np.full((100, 100, 3), 0.5, dtype=np.float32)
    linear[:10, :10, 0] = 1.0  # 1% of red at sensor white
    linear[:5, :5, 2] = 0.995  # 0.25% of blue, above the clip level
    fractions = measure_clip_fractions(linear)
    assert fractions == pytest.approx((0.01, 0.0, 0.0025), abs=1e-9)

    codes = (linear * 65535).astype(np.uint16)
    assert measure_clip_fractions(codes) == pytest.approx(fractions, abs=1e-6)


# --- N-2: the rebate detector ------------------------------------------------


def _scene_grid(
    height: int,
    width: int,
    base: float = -0.4,
    dense: float = -2.0,
    seed: int = 3,
) -> np.ndarray:
    """A log grid whose thin end sits at `base` and whose dense end at
    `dense`, with a smooth gradient (so it is not featureless)."""
    rng = np.random.default_rng(seed)
    x = np.linspace(dense, base, width, dtype=np.float32)
    rows = np.tile(x, (height, 1))
    rows += rng.uniform(-0.02, 0.02, size=rows.shape).astype(np.float32)
    return np.stack([rows] * 3, axis=-1)


def _add_strip(grid: np.ndarray, cells: dict[str, slice], value: float) -> None:
    for channel in range(3):
        grid[cells["rows"], cells["cols"], channel] = value


def test_rebate_strip_along_one_edge_is_detected_and_excluded():
    grid = _scene_grid(200, 200)
    _add_strip(grid, {"rows": slice(0, 20), "cols": slice(0, 200)}, -0.05)
    keep = np.ones(grid.shape[:2], dtype=bool)
    new_keep, rebate = detect_rebate(grid, keep)
    assert rebate.detected
    assert not rebate.clipped
    assert rebate.base_density is not None
    assert all(d == pytest.approx(-0.05, abs=0.05) for d in rebate.base_density)
    assert not new_keep[:20].any()
    assert new_keep.sum() == 200 * 180

    # The detector's whole contract: bounds after exclusion match the same
    # frame with the strip cropped away.
    bounds = analyze_bounds(grid, new_keep)
    cropped = _scene_grid(180, 200)
    cropped_bounds = analyze_bounds(cropped, np.ones((180, 200), dtype=bool))
    assert bounds.floors == pytest.approx(cropped_bounds.floors, abs=0.05)
    assert bounds.ceils == pytest.approx(cropped_bounds.ceils, abs=0.05)


def test_no_rebate_nothing_fires():
    grid = _scene_grid(200, 200)
    keep = np.ones(grid.shape[:2], dtype=bool)
    new_keep, rebate = detect_rebate(grid, keep)
    assert not rebate.detected
    assert rebate.mask_fraction == 0.0
    assert rebate.base_density is None
    assert np.array_equal(new_keep, keep)


@pytest.mark.parametrize(
    "strips",
    [
        [{"rows": slice(0, 15), "cols": slice(0, 200)}],
        [
            {"rows": slice(0, 15), "cols": slice(0, 200)},
            {"rows": slice(185, 200), "cols": slice(0, 200)},
        ],
        [
            {"rows": slice(0, 15), "cols": slice(0, 200)},
            {"rows": slice(185, 200), "cols": slice(0, 200)},
            {"rows": slice(0, 200), "cols": slice(185, 200)},
        ],
    ],
    ids=["one-edge", "two-opposite-edges", "three-edges"],
)
def test_rebate_on_one_two_and_three_edges(strips):
    grid = _scene_grid(200, 200)
    for strip in strips:
        _add_strip(grid, strip, -0.05)
    keep = np.ones(grid.shape[:2], dtype=bool)
    new_keep, rebate = detect_rebate(grid, keep)
    assert rebate.detected
    for strip in strips:
        assert not new_keep[strip["rows"], strip["cols"]].any()


def test_deep_featureless_shadow_close_to_base_does_not_fire():
    """A thin, featureless scene region touching the border — but separated
    from the base's density by less than the separation gate — does not
    fire: with no distinct base population the thinnest cells are not
    separated from the scene distribution."""
    grid = _scene_grid(200, 200, base=-0.4, dense=-2.0)
    # A "night sky": uniformly thin, 0.03 D thinner than the scene's own
    # thin end — inside the candidate band, featureless, border-touching.
    _add_strip(grid, {"rows": slice(0, 30), "cols": slice(0, 200)}, -0.45)
    keep = np.ones(grid.shape[:2], dtype=bool)
    _new_keep, rebate = detect_rebate(grid, keep)
    assert not rebate.detected


def test_deep_featureless_shadow_far_from_base_fires_mildly():
    """And when the separation is real, it fires — the documented false
    positive: some real shadow is withheld from the meters and the ceiling
    compresses slightly. It never invents data, and it never crashes."""
    grid = _scene_grid(200, 200, base=-0.4, dense=-2.0)
    _add_strip(grid, {"rows": slice(0, 30), "cols": slice(0, 200)}, -0.1)
    keep = np.ones(grid.shape[:2], dtype=bool)
    new_keep, rebate = detect_rebate(grid, keep)
    assert rebate.detected
    assert not new_keep[:30].any()
    # The ceiling compresses onto the scene's own thin end, not the strip's.
    bounds = analyze_bounds(grid, new_keep)
    assert bounds.ceils[0] == pytest.approx(-0.4, abs=0.05)


def test_sprocket_holes_are_excluded_along_with_the_base():
    grid = _scene_grid(200, 200)
    _add_strip(grid, {"rows": slice(0, 20), "cols": slice(0, 200)}, -0.05)
    # Sprocket holes: film-free, therefore *thinner* than base, contiguous
    # with it inside the strip.
    for start in range(10, 200, 30):
        _add_strip(grid, {"rows": slice(4, 12), "cols": slice(start, start + 12)}, 0.05)
    keep = np.ones(grid.shape[:2], dtype=bool)
    new_keep, rebate = detect_rebate(grid, keep)
    assert rebate.detected
    assert not new_keep[:20].any()
    # The ceiling lands on base rather than on the hole: the recorded
    # bounds are computed on the *excluded* keep, so the holes cannot set
    # them.
    bounds = analyze_bounds(grid, new_keep)
    assert bounds.ceils[0] == pytest.approx(-0.4, abs=0.05)


def test_clipped_rebate_is_excluded_but_records_no_base_density():
    grid = _scene_grid(200, 200)
    _add_strip(grid, {"rows": slice(0, 20), "cols": slice(0, 200)}, 0.0)
    keep = np.ones(grid.shape[:2], dtype=bool)
    new_keep, rebate = detect_rebate(grid, keep)
    assert rebate.detected
    assert rebate.clipped
    assert rebate.base_density is None
    assert not new_keep[:20].any()


def test_detector_is_deterministic():
    grid = _scene_grid(150, 150)
    _add_strip(grid, {"rows": slice(0, 15), "cols": slice(0, 150)}, -0.05)
    keep = np.ones(grid.shape[:2], dtype=bool)
    first_keep, first_rebate = detect_rebate(grid, keep)
    second_keep, second_rebate = detect_rebate(grid, keep)
    assert np.array_equal(first_keep, second_keep)
    assert first_rebate == second_rebate


def test_empty_keep_is_handled_cleanly():
    grid = _scene_grid(64, 64)
    keep = np.zeros(grid.shape[:2], dtype=bool)
    new_keep, rebate = detect_rebate(grid, keep)
    assert not rebate.detected
    assert not new_keep.any()


# --- the dense-border detector ------------------------------------------------


def test_dense_stripe_along_one_edge_is_detected_and_excluded():
    """The R1 failure: a featureless dense stripe along a border latched the
    floor percentile (P0.01 luma, P1 colour) and darkened the whole frame.
    The detector withholds it, and the bounds then match the same frame
    with the stripe cropped away."""
    grid = _scene_grid(200, 200)
    _add_strip(grid, {"rows": slice(0, 8), "cols": slice(0, 200)}, -2.6)
    keep = np.ones(grid.shape[:2], dtype=bool)
    new_keep, dense_border = withhold_dense_border(grid, keep)
    assert dense_border.detected
    assert not new_keep[:8].any()
    assert new_keep.sum() == 200 * 192

    bounds = analyze_bounds(grid, new_keep)
    cropped = _scene_grid(192, 200)
    cropped_bounds = analyze_bounds(cropped, np.ones((192, 200), dtype=bool))
    assert bounds.floors == pytest.approx(cropped_bounds.floors, abs=0.05)
    assert bounds.ceils == pytest.approx(cropped_bounds.ceils, abs=0.05)

    # And without the detector, the stripe owns the floor percentile: the
    # unguarded read latches the stripe's density.
    unguarded = analyze_bounds(grid, keep)
    assert unguarded.floors[0] < -2.4


def test_fading_dense_stripe_is_eaten_band_by_band():
    """Edge fog fades with distance from the edge: one pass withholds only
    its densest band, the next pass re-anchors behind it, and the residue —
    a gradient ending at the scene's own dense end, indistinguishable from
    scene content — is what the separation gate correctly spares. The
    latched floor error drops from ~0.6 D (unguarded) to ~0.15 D."""
    grid = _scene_grid(200, 200)
    fading = np.linspace(-2.6, -2.0, 24, dtype=np.float32)
    for row, value in enumerate(fading):
        _add_strip(grid, {"rows": slice(row, row + 1), "cols": slice(0, 200)}, float(value))
    keep = np.ones(grid.shape[:2], dtype=bool)
    new_keep, dense_border = withhold_dense_border(grid, keep)
    assert dense_border.detected
    assert not new_keep[:16].any()

    bounds = analyze_bounds(grid, new_keep)
    cropped = _scene_grid(176, 200)
    cropped_bounds = analyze_bounds(cropped, np.ones((176, 200), dtype=bool))
    assert bounds.floors == pytest.approx(cropped_bounds.floors, abs=0.2)
    assert bounds.ceils == pytest.approx(cropped_bounds.ceils, abs=0.02)

    # The unguarded read latches the gradient's dense core.
    unguarded = analyze_bounds(grid, keep)
    assert unguarded.floors[0] < -2.5
    assert bounds.floors[0] > unguarded.floors[0]


def test_no_dense_stripe_nothing_fires():
    grid = _scene_grid(200, 200)
    keep = np.ones(grid.shape[:2], dtype=bool)
    new_keep, dense_border = withhold_dense_border(grid, keep)
    assert not dense_border.detected
    assert dense_border.mask_fraction == 0.0
    assert np.array_equal(new_keep, keep)


def test_scene_dense_end_touching_a_border_does_not_fire():
    """The false-positive guard: `_scene_grid`'s own dense end sits *at* the
    left border — thin, featureless-ish, denser than the bulk — but it is
    part of the scene's continuous distribution, so the separation gate
    withholds nothing."""
    grid = _scene_grid(200, 200)
    keep = np.ones(grid.shape[:2], dtype=bool)
    new_keep, dense_border = withhold_dense_border(grid, keep)
    assert not dense_border.detected
    assert np.array_equal(new_keep, keep)


def test_large_dense_border_region_does_not_fire():
    """Bounded damage: a candidate larger than the area cap is scene content
    (a night shot's black sky), not contamination, and is left alone."""
    grid = _scene_grid(200, 200)
    _add_strip(grid, {"rows": slice(0, 60), "cols": slice(0, 200)}, -2.6)
    keep = np.ones(grid.shape[:2], dtype=bool)
    new_keep, dense_border = withhold_dense_border(grid, keep)
    assert not dense_border.detected
    assert np.array_equal(new_keep, keep)


def test_dense_detector_is_deterministic_and_handles_empty_keep():
    grid = _scene_grid(150, 150)
    _add_strip(grid, {"rows": slice(0, 8), "cols": slice(0, 150)}, -2.6)
    keep = np.ones(grid.shape[:2], dtype=bool)
    first_keep, first_finding = withhold_dense_border(grid, keep)
    second_keep, second_finding = withhold_dense_border(grid, keep)
    assert np.array_equal(first_keep, second_keep)
    assert first_finding == second_finding

    empty = np.zeros((64, 64), dtype=bool)
    new_keep, finding = withhold_dense_border(_scene_grid(64, 64), empty)
    assert not finding.detected
    assert np.array_equal(new_keep, empty)


# --- section 3.4's clamp ------------------------------------------------------


def _population(
    floor_mean: float = -1.5,
    ceil_mean: float = -0.4,
    count: int = 4,
    spread: float = 0.05,
) -> list[Bounds]:
    return [
        Bounds(
            floors=tuple(floor_mean + (i - count / 2) * spread for _ in range(3)),
            ceils=tuple(ceil_mean + (i % 2) * spread for _ in range(3)),
        )
        for i in range(count)
    ]


def test_clamp_pulls_the_fill_outlier_back_to_the_population():
    """The neg-08 safety net: floors at -6.0 are pulled to the population's
    window — `max(CLAMP_K_MAD * MAD, CLAMP_MIN_WINDOW)` around the median."""
    references = _population()
    outlier = Bounds(
        floors=(-6.0, -6.0, -6.0),
        ceils=(-0.4, -0.4, -0.4),
    )
    clamped, did = clamp_bounds(outlier, references)
    assert did
    median_floor = float(np.median([b.floors[0] for b in references]))
    assert clamped.floors[0] == pytest.approx(median_floor - nz.CLAMP_MIN_WINDOW)
    # The thin end was fine and stays exactly where the frame measured it.
    assert clamped.ceils == pytest.approx(outlier.ceils, abs=1e-9)


def test_clamp_spares_frames_within_the_window():
    references = _population(floor_mean=-1.5, spread=0.1)
    # One stop denser than the median: legitimate per-frame exposure
    # variation, inside the clamp window, must survive untouched.
    legit = Bounds(floors=(-1.9, -1.9, -1.9), ceils=(-0.4, -0.4, -0.4))
    clamped, did = clamp_bounds(legit, references)
    assert not did
    assert clamped == legit


def test_clamp_needs_a_population():
    outlier = Bounds(floors=(-6.0, -6.0, -6.0), ceils=(-0.4, -0.4, -0.4))
    clamped, did = clamp_bounds(outlier, _population(count=nz.CLAMP_MIN_SAMPLES - 1))
    assert not did
    assert clamped == outlier


def test_clamp_that_would_degenerate_a_channel_is_discarded():
    """A safety net must never make things worse: a clamp that would leave a
    channel with ceil <= floor returns the frame's own bounds."""
    references = _population(floor_mean=-1.0, ceil_mean=-0.9, spread=0.0)
    nonsense = Bounds(floors=(-1.2, -1.2, -1.2), ceils=(-2.0, -2.0, -2.0))
    clamped, did = clamp_bounds(nonsense, references)
    assert not did
    assert clamped == nonsense


# --- the resolution region ----------------------------------------------------


def test_resolve_analysis_region_prefers_crop_roi_over_valid_rect():
    keep = resolve_analysis_region((10, 10), (0, 0, 10, 10), crop_roi=(2, 2, 4, 4))
    assert keep.sum() == 16
    assert not keep[:2].any()
    assert keep[2:6, 2:6].all()


def test_resolve_analysis_region_with_no_rect_is_the_whole_grid():
    keep = resolve_analysis_region((6, 8), None)
    assert keep.shape == (6, 8)
    assert keep.all()


def test_resolve_analysis_region_clamps_and_falls_back_on_degenerate_rects():
    clamped = resolve_analysis_region((10, 10), (8, 8, 50, 50))
    assert clamped.sum() == 4
    degenerate = resolve_analysis_region((10, 10), (5, 5, 0, 0))
    assert degenerate.all()


# --- section 3.8: normalize_params ---------------------------------------------


def test_build_params_carries_every_constant_and_the_format_version():
    params = build_params()
    assert params["format_version"] == 1
    assert params["analysis_grid"] == ANALYSIS_GRID
    assert params["base_luma_clip"] == nz.BASE_LUMA_CLIP
    assert params["base_color_clip"] == nz.BASE_COLOR_CLIP
    assert params["normalized_headroom_low"] == NORMALIZED_HEADROOM_LOW
    assert params["normalized_headroom_high"] == NORMALIZED_HEADROOM_HIGH
    assert params["normalized_fill"] == 1.0 + NORMALIZED_HEADROOM_HIGH
    assert params["scan_clip_level"] == nz.SCAN_CLIP_LEVEL
    assert params["scan_clip_warn"] == nz.SCAN_CLIP_WARN
    assert params["dense_border_anchor_percentile"] == nz.DENSE_BORDER_ANCHOR_PERCENTILE
    assert params["dense_border_tolerance"] == nz.DENSE_BORDER_TOLERANCE
    assert (
        params["dense_border_min_area_fraction"] == nz.DENSE_BORDER_MIN_AREA_FRACTION
    )
    assert (
        params["dense_border_max_area_fraction"] == nz.DENSE_BORDER_MAX_AREA_FRACTION
    )
    assert (
        params["dense_border_max_bbox_fraction"] == nz.DENSE_BORDER_MAX_BBOX_FRACTION
    )
    assert params["dense_border_min_separation"] == nz.DENSE_BORDER_MIN_SEPARATION
    assert (
        params["dense_border_outside_percentile"]
        == nz.DENSE_BORDER_OUTSIDE_PERCENTILE
    )
    assert params["dense_border_max_passes"] == nz.DENSE_BORDER_MAX_PASSES
    assert params["clamp_min_samples"] == nz.CLAMP_MIN_SAMPLES
    assert params["clamp_k_mad"] == nz.CLAMP_K_MAD
    assert params["clamp_min_window"] == nz.CLAMP_MIN_WINDOW
    # JSON-serialisable, since it folds into processing_params.
    import json

    assert json.loads(json.dumps(params)) == params


# --- golden values against NegPy (requires the reference implementation) -------


def test_golden_values_match_negpy():
    """Run both implementations on the same fixture array and assert
    agreement to 1e-5. The highest-value test in the plan — the port is
    subtle and NegPy is the reference. Runs with the rebate detector
    disabled, since NegPy has no equivalent."""
    negpy_normalization = pytest.importorskip("negpy.features.exposure.normalization")
    img = _ramp_scene(256, 256, -2.0, -0.2, (0.0, 0.1, -0.1))
    keep = np.ones(img.shape[:2], dtype=bool)
    bounds = analyze_bounds(img, keep)
    reference = negpy_normalization.analyze_log_exposure_bounds_from_log(
        img, keep, luma_clip=nz.BASE_LUMA_CLIP, color_clip=nz.BASE_COLOR_CLIP
    )
    assert bounds.floors == pytest.approx(reference.floors, abs=1e-5)
    assert bounds.ceils == pytest.approx(reference.ceils, abs=1e-5)
