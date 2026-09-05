import dataclasses
import math

import numpy as np
import pytest

from scanny_boy.events import Code
from scanny_boy.layout import (
    STRIP_SPREAD_RATIO,
    FramePlacement,
    GainStat,
    StitchError,
    _largest_all_covered_rectangle,
    global_rms,
    largest_valid_rect,
    solve_gains,
    solve_layout,
)
from scanny_boy.registration import PairResult

_FRAME_SIZE = (400, 600)  # (height, width)


def _stat(a, b, mean_a, mean_b, shared_count=10_000):
    return GainStat(
        a=a,
        b=b,
        mean_a=tuple(mean_a),
        mean_b=tuple(mean_b),
        shared_count=shared_count,
    )


def test_solve_gains_recovers_injected_offsets_under_the_geometric_mean_anchor():
    stats = [_stat("f0", "f1", (0.5, 0.4, 0.6), (0.4, 0.4, 0.42))]
    gains = solve_gains(["f0", "f1"], stats)

    # g1/g0 = mean_a/mean_b per channel; the anchor pins g0*g1 = 1, so the
    # two gains bracket 1 symmetrically in log space.
    ratio = np.array([0.5 / 0.4, 0.4 / 0.4, 0.6 / 0.42])
    assert np.allclose(gains["f0"], 1.0 / np.sqrt(ratio), rtol=1e-9)
    assert np.allclose(gains["f1"], np.sqrt(ratio), rtol=1e-9)
    assert np.allclose(np.multiply(gains["f0"], gains["f1"]), 1.0)


def test_solve_gains_distributes_inconsistent_ratios_instead_of_chaining():
    # f0-f1 and f1-f2 each report a doubling; f0-f2 directly reports 3x.
    # A chained estimator (NegPy's) accumulates down the chain and puts the
    # whole conflict into the f0-f2 relation; the global least-squares solve
    # spreads it equally across the three equal-weight rows.
    log2 = math.log(2.0)
    log3 = math.log(3.0)
    stats = [
        _stat("f0", "f1", (0.4, 0.4, 0.4), (0.2, 0.2, 0.2)),  # ratio 2
        _stat("f1", "f2", (0.4, 0.4, 0.4), (0.2, 0.2, 0.2)),  # ratio 2
        _stat("f0", "f2", (0.4, 0.4, 0.4), (0.4 / 3.0,) * 3),  # ratio 3
    ]
    gains = solve_gains(["f0", "f1", "f2"], stats)

    r01 = math.log(gains["f1"][0] / gains["f0"][0]) - log2
    r12 = math.log(gains["f2"][0] / gains["f1"][0]) - log2
    r02 = math.log(gains["f2"][0] / gains["f0"][0]) - log3
    assert r01 == pytest.approx(r12, abs=1e-9)
    assert r01 == pytest.approx(-r02, abs=1e-9)
    # The worst per-row residual is far below the chained estimator's, which
    # would leave the direct pair with the full 4x-vs-3x conflict.
    assert max(abs(r01), abs(r02)) < abs(2 * log2 + 2 * log2 - log3)

    # The anchor still holds exactly per channel.
    assert gains["f0"][0] * gains["f1"][0] * gains["f2"][0] == pytest.approx(1.0)


def test_solve_gains_drops_rows_with_degenerate_channel_means():
    stats = [
        _stat("f0", "f1", (0.5, 0.0, 0.6), (0.4, 0.5, 0.5)),
        _stat("f1", "f2", (0.5, 0.0, 0.6), (0.4, 0.5, 0.5)),
    ]
    gains = solve_gains(["f0", "f1", "f2"], stats)

    # Every channel-1 row is degenerate, so that channel is unsolved and
    # every frame keeps gain 1 there; the other channels still solve.
    assert gains["f0"][1] == 1.0
    assert gains["f1"][1] == 1.0
    assert gains["f2"][1] == 1.0
    assert gains["f0"][0] != 1.0 and gains["f2"][0] != 1.0


def test_solve_gains_leaves_uncovered_frames_at_unity():
    stats = [_stat("f0", "f1", (0.5, 0.5, 0.5), (0.4, 0.4, 0.4))]
    gains = solve_gains(["f0", "f1", "f2"], stats)

    assert gains["f2"] == (1.0, 1.0, 1.0)
    assert np.allclose(np.multiply(gains["f0"], gains["f1"]), 1.0)


def test_solve_gains_weights_rows_by_overlap_area():
    # f0-f1 (huge overlap) says ratio 2; f1-f2 and f0-f2 (tiny overlaps)
    # say ratio 1. The large row must dominate the small ones.
    stats = [
        _stat("f0", "f1", (0.4, 0.4, 0.4), (0.2, 0.2, 0.2), shared_count=1_000_000),
        _stat("f1", "f2", (0.4, 0.4, 0.4), (0.4, 0.4, 0.4), shared_count=100),
        _stat("f0", "f2", (0.4, 0.4, 0.4), (0.4, 0.4, 0.4), shared_count=100),
    ]
    gains = solve_gains(["f0", "f1", "f2"], stats)

    assert gains["f1"][0] / gains["f0"][0] > 1.8


def _rotation_matrix(angle_deg):
    angle_rad = np.radians(angle_deg)
    cos_a, sin_a = np.cos(angle_rad), np.sin(angle_rad)
    return np.array([[cos_a, -sin_a], [sin_a, cos_a]])


def _ground_truth_pair(
    name_a,
    name_b,
    placement_a: FramePlacement,
    placement_b: FramePlacement,
    frame_size,
    *,
    n_points=60,
    noise_px=0.0,
    seed=0,
):
    """Builds a PairResult whose similarity fit and inlier correspondences
    are exactly what register_pair would have produced for two frames
    placed at the given ground-truth global similarity poses (rotation,
    translation, and per-frame scale), optionally with small measurement
    noise on the correspondences. `transform`/`rms_residual_px` carry the
    same rigid (scale-forced-to-1) relation as before: layout.py's scale,
    rotation, and translation solves all read the similarity fields, not
    these, so an exact rigid re-fit is not needed for these tests."""
    rng = np.random.default_rng(seed)
    height, width = frame_size

    rotation_a = _rotation_matrix(placement_a.rotation_deg)
    translation_a = np.array(placement_a.translation)
    translation_b = np.array(placement_b.translation)

    phi_ab_deg = placement_b.rotation_deg - placement_a.rotation_deg
    sigma_ab = placement_b.scale / placement_a.scale
    u_ab = rotation_a.T @ (translation_b - translation_a) / placement_a.scale
    rotation_ab = _rotation_matrix(phi_ab_deg)

    pts_b = rng.uniform([0, 0], [width, height], size=(n_points, 2))
    pts_a = sigma_ab * (pts_b @ rotation_ab.T) + u_ab
    if noise_px:
        pts_a = pts_a + rng.normal(0, noise_px, size=pts_a.shape)

    similarity_transform = np.hstack([rotation_ab, u_ab.reshape(2, 1)])

    return PairResult(
        a=name_a,
        b=name_b,
        transform=similarity_transform,
        good_matches=n_points,
        inliers=n_points,
        inlier_ratio=1.0,
        rms_residual_px=noise_px if noise_px else 0.0,
        scale_drift=abs(sigma_ab - 1.0),
        accepted=True,
        reject_code=None,
        reject_message=None,
        inlier_points_a=pts_a,
        inlier_points_b=pts_b,
        overlap_fraction=None,
        overlap_mad=None,
        overlap_mad_pregain=None,
        similarity_transform=similarity_transform,
        similarity_scale=sigma_ab,
    )


def _reversed_pair(pair: PairResult) -> PairResult:
    rotation = pair.similarity_transform[:, :2]
    translation = pair.similarity_transform[:, 2]
    scale = pair.similarity_scale
    rotation_inv = rotation.T
    scale_inv = 1.0 / scale
    translation_inv = -scale_inv * (rotation_inv @ translation)
    transform_inv = np.hstack([rotation_inv, translation_inv.reshape(2, 1)])
    return dataclasses.replace(
        pair,
        a=pair.b,
        b=pair.a,
        transform=transform_inv,
        inlier_points_a=pair.inlier_points_b,
        inlier_points_b=pair.inlier_points_a,
        similarity_transform=transform_inv,
        similarity_scale=scale_inv,
    )


def test_recovers_a_known_layout():
    ground_truth = [
        FramePlacement("f0", 0.0, (0.0, 0.0)),
        FramePlacement("f1", 3.0, (500.0, 50.0)),
        FramePlacement("f2", -2.0, (1000.0, 50.0)),
    ]
    # f0's own (0, 0) corner must stay the global minimum in both axes, so
    # solve_layout's canvas-origin shift is exactly zero and the recovered
    # placements can be compared directly against this ground truth.
    by_name = {p.name: p for p in ground_truth}
    names = ["f0", "f1", "f2"]
    pairs = [
        _ground_truth_pair(
            "f0", "f1", by_name["f0"], by_name["f1"], _FRAME_SIZE, noise_px=0.2, seed=1
        ),
        _ground_truth_pair(
            "f1", "f2", by_name["f1"], by_name["f2"], _FRAME_SIZE, noise_px=0.2, seed=2
        ),
    ]

    layout = solve_layout(names, _FRAME_SIZE, pairs)

    for placement in layout.placements:
        expected = by_name[placement.name]
        assert abs(placement.rotation_deg - expected.rotation_deg) < 0.1
        assert (
            np.linalg.norm(
                np.array(placement.translation) - np.array(expected.translation)
            )
            < 1.0
        )
        # No scale variation in this fixture: the scale solve must recover
        # ~1 for every frame and leave rotation/translation exactly as the
        # scale-1-only solver did — this is the regression that proves the
        # scale solve is additive.
        assert placement.scale == pytest.approx(1.0, abs=0.01)


def test_a_scaled_layout_is_recovered_with_geometric_mean_one():
    ground_truth = [
        FramePlacement("f0", 0.0, (0.0, 0.0), scale=0.98),
        FramePlacement("f1", 3.0, (500.0, 50.0), scale=1.03),
        FramePlacement("f2", -2.0, (1000.0, 50.0), scale=1.01),
    ]
    by_name = {p.name: p for p in ground_truth}
    names = ["f0", "f1", "f2"]
    pairs = [
        _ground_truth_pair(
            "f0", "f1", by_name["f0"], by_name["f1"], _FRAME_SIZE, seed=1
        ),
        _ground_truth_pair(
            "f1", "f2", by_name["f1"], by_name["f2"], _FRAME_SIZE, seed=2
        ),
    ]

    layout = solve_layout(names, _FRAME_SIZE, pairs)
    solved_by_name = {p.name: p for p in layout.placements}

    # Every pairwise scale ratio is exactly reproducible (noise_px=0), so
    # the solve should recover each frame's scale relative to the others to
    # a tight tolerance, up to the geometric-mean-1 gauge freedom.
    log_offset = math.log(by_name["f0"].scale) - math.log(solved_by_name["f0"].scale)
    for name in names:
        expected_log_scale = math.log(by_name[name].scale) - log_offset
        assert math.log(solved_by_name[name].scale) == pytest.approx(
            expected_log_scale, abs=1e-3
        )

    geometric_mean_log = sum(
        math.log(p.scale) for p in layout.placements
    ) / len(layout.placements)
    assert geometric_mean_log == pytest.approx(0.0, abs=1e-6)


def test_canvas_size_matches_transformed_corners_under_a_scaled_layout():
    ground_truth = [
        FramePlacement("f0", 0.0, (0.0, 0.0), scale=0.95),
        FramePlacement("f1", 5.0, (500.0, 20.0), scale=1.05),
    ]
    by_name = {p.name: p for p in ground_truth}
    names = ["f0", "f1"]
    pairs = [
        _ground_truth_pair("f0", "f1", by_name["f0"], by_name["f1"], _FRAME_SIZE, seed=1)
    ]

    layout = solve_layout(names, _FRAME_SIZE, pairs)

    height, width = _FRAME_SIZE
    corners_local = np.array(
        [[0, 0], [width, 0], [width, height], [0, height]], dtype=np.float64
    )
    all_corners = []
    for placement in layout.placements:
        matrix = placement.matrix()
        rotation, translation = matrix[:, :2], matrix[:, 2]
        all_corners.append(corners_local @ rotation.T + translation)
    all_corners = np.vstack(all_corners)

    min_xy = all_corners.min(axis=0)
    max_xy = all_corners.max(axis=0)
    expected_canvas = (
        math.ceil(max_xy[0] - min_xy[0]),
        math.ceil(max_xy[1] - min_xy[1]),
    )
    assert layout.canvas_size == expected_canvas
    assert min_xy[0] == pytest.approx(0.0, abs=1e-6)
    assert min_xy[1] == pytest.approx(0.0, abs=1e-6)


def test_shuffled_frame_order_gives_the_same_layout():
    ground_truth = [
        FramePlacement("f0", 0.0, (0.0, 0.0)),
        FramePlacement("f1", 4.0, (500.0, 20.0)),
        FramePlacement("f2", -3.0, (1000.0, -25.0)),
    ]
    by_name = {p.name: p for p in ground_truth}
    names = ["f0", "f1", "f2"]
    pair_01 = _ground_truth_pair(
        "f0", "f1", by_name["f0"], by_name["f1"], _FRAME_SIZE, noise_px=0.0, seed=1
    )
    pair_12 = _ground_truth_pair(
        "f1", "f2", by_name["f1"], by_name["f2"], _FRAME_SIZE, noise_px=0.0, seed=2
    )

    baseline = solve_layout(names, _FRAME_SIZE, [pair_01, pair_12])

    # Same anchor frame (names[0] unchanged), but the rest of the names are
    # scrambled and the pairs are reordered and given in reversed (b, a)
    # form. Section 3.2: capture order is not spatial order, and nothing
    # may depend on which of a pair's two frames comes first.
    scrambled_names = ["f0", "f2", "f1"]
    scrambled_pairs = [_reversed_pair(pair_12), pair_01]

    scrambled = solve_layout(scrambled_names, _FRAME_SIZE, scrambled_pairs)

    assert scrambled.canvas_size == baseline.canvas_size

    baseline_by_name = {p.name: p for p in baseline.placements}
    scrambled_by_name = {p.name: p for p in scrambled.placements}
    for name in names:
        base = baseline_by_name[name]
        scr = scrambled_by_name[name]
        assert base.rotation_deg == pytest.approx(scr.rotation_deg, abs=1e-6)
        assert base.translation[0] == pytest.approx(scr.translation[0], abs=1e-6)
        assert base.translation[1] == pytest.approx(scr.translation[1], abs=1e-6)
        assert base.scale == pytest.approx(scr.scale, abs=1e-6)


def test_disconnected_graph_is_rejected():
    ground_truth = [
        FramePlacement("f0", 0.0, (0.0, 0.0)),
        FramePlacement("f1", 2.0, (500.0, 0.0)),
        FramePlacement("f2", 1.0, (1000.0, 0.0)),
    ]
    by_name = {p.name: p for p in ground_truth}
    names = ["f0", "f1", "f2"]
    connected_pair = _ground_truth_pair(
        "f0", "f1", by_name["f0"], by_name["f1"], _FRAME_SIZE, seed=1
    )
    rejected_pair = dataclasses.replace(
        _ground_truth_pair("f1", "f2", by_name["f1"], by_name["f2"], _FRAME_SIZE, seed=2),
        accepted=False,
        reject_code=Code.STITCH_INSUFFICIENT_MATCHES,
        reject_message="rejected for this test",
    )

    with pytest.raises(StitchError) as exc_info:
        solve_layout(names, _FRAME_SIZE, [connected_pair, rejected_pair])

    assert exc_info.value.code is Code.STITCH_UNDERCONSTRAINED
    assert "f2" in exc_info.value.message


def test_canvas_bounds_match_hand_computed_corners():
    ground_truth = [
        FramePlacement("f0", 0.0, (0.0, 0.0)),
        FramePlacement("f1", 10.0, (500.0, 0.0)),
    ]
    by_name = {p.name: p for p in ground_truth}
    names = ["f0", "f1"]
    pair = _ground_truth_pair("f0", "f1", by_name["f0"], by_name["f1"], _FRAME_SIZE, seed=1)

    layout = solve_layout(names, _FRAME_SIZE, [pair])

    height, width = _FRAME_SIZE
    corners_local = np.array(
        [[0, 0], [width, 0], [width, height], [0, height]], dtype=np.float64
    )

    rotation_f0 = _rotation_matrix(0.0)
    corners_f0 = corners_local @ rotation_f0.T + np.array([0.0, 0.0])

    rotation_f1 = _rotation_matrix(10.0)
    corners_f1 = corners_local @ rotation_f1.T + np.array([500.0, 0.0])

    all_corners = np.vstack([corners_f0, corners_f1])
    min_xy = all_corners.min(axis=0)
    max_xy = all_corners.max(axis=0)

    expected_width = int(np.ceil(max_xy[0] - min_xy[0]))
    expected_height = int(np.ceil(max_xy[1] - min_xy[1]))

    assert layout.canvas_size == (expected_width, expected_height)


def test_valid_rect_contains_no_uncovered_pixel():
    import cv2

    ground_truth = [
        FramePlacement("f0", 0.0, (0.0, 0.0)),
        FramePlacement("f1", 4.0, (450.0, 30.0)),
    ]
    by_name = {p.name: p for p in ground_truth}
    names = ["f0", "f1"]
    pair = _ground_truth_pair("f0", "f1", by_name["f0"], by_name["f1"], _FRAME_SIZE, seed=1)

    layout = solve_layout(names, _FRAME_SIZE, [pair])
    x, y, width, height = largest_valid_rect(layout, _FRAME_SIZE, probe_long_edge=300)
    assert width > 0
    assert height > 0

    canvas_width, canvas_height = layout.canvas_size
    full_mask = np.zeros((canvas_height, canvas_width), dtype=np.uint8)
    frame_height, frame_width = _FRAME_SIZE
    corners_local = np.array(
        [[0, 0], [frame_width, 0], [frame_width, frame_height], [0, frame_height]],
        dtype=np.float64,
    )
    for placement in layout.placements:
        matrix = placement.matrix()
        rotation, translation = matrix[:, :2], matrix[:, 2]
        corners = corners_local @ rotation.T + translation
        cv2.fillConvexPoly(full_mask, np.round(corners).astype(np.int32), 1)

    region = full_mask[y : y + height, x : x + width]
    assert region.size > 0
    assert np.all(region == 1)


def test_valid_rect_is_conservative_not_optimistic():
    ground_truth = [
        FramePlacement("f0", 0.0, (0.0, 0.0)),
        FramePlacement("f1", 6.0, (450.0, 40.0)),
    ]
    by_name = {p.name: p for p in ground_truth}
    names = ["f0", "f1"]
    pair = _ground_truth_pair("f0", "f1", by_name["f0"], by_name["f1"], _FRAME_SIZE, seed=1)

    layout = solve_layout(names, _FRAME_SIZE, [pair])

    _, _, coarse_width, coarse_height = largest_valid_rect(
        layout, _FRAME_SIZE, probe_long_edge=150
    )
    coarse_area = coarse_width * coarse_height

    # The true optimum at full resolution (no probe downscaling).
    canvas_width, canvas_height = layout.canvas_size
    full_probe_edge = max(canvas_width, canvas_height)
    _, _, exact_width, exact_height = largest_valid_rect(
        layout, _FRAME_SIZE, probe_long_edge=full_probe_edge
    )
    exact_area = exact_width * exact_height

    assert coarse_area <= exact_area


def test_l_shaped_layout_reports_a_high_spread_ratio():
    ground_truth = [
        FramePlacement("f0", 0.0, (0.0, 0.0)),
        FramePlacement("f1", 0.0, (600.0, 0.0)),
        FramePlacement("f2", 0.0, (0.0, 400.0)),
    ]
    by_name = {p.name: p for p in ground_truth}
    names = ["f0", "f1", "f2"]
    pairs = [
        _ground_truth_pair("f0", "f1", by_name["f0"], by_name["f1"], _FRAME_SIZE, seed=1),
        _ground_truth_pair("f0", "f2", by_name["f0"], by_name["f2"], _FRAME_SIZE, seed=2),
    ]

    layout = solve_layout(names, _FRAME_SIZE, pairs)

    assert layout.strip_spread_ratio > STRIP_SPREAD_RATIO
    assert layout.strip_axis is None


def test_strip_axis_points_along_the_strip():
    ground_truth = [
        FramePlacement("f0", 0.0, (0.0, 0.0)),
        FramePlacement("f1", 3.0, (500.0, 50.0)),
        FramePlacement("f2", -2.0, (1000.0, 50.0)),
    ]
    by_name = {p.name: p for p in ground_truth}
    names = ["f0", "f1", "f2"]
    pairs = [
        _ground_truth_pair(
            "f0", "f1", by_name["f0"], by_name["f1"], _FRAME_SIZE, noise_px=0.2, seed=1
        ),
        _ground_truth_pair(
            "f1", "f2", by_name["f1"], by_name["f2"], _FRAME_SIZE, noise_px=0.2, seed=2
        ),
    ]

    layout = solve_layout(names, _FRAME_SIZE, pairs)

    assert layout.strip_axis is not None
    ax, ay = layout.strip_axis
    assert math.isclose(math.hypot(ax, ay), 1.0, abs_tol=1e-6)
    # The frames run mostly along x (500px steps vs ~50px of y drift), so
    # the axis must be nearly (+-1, ~0).
    assert abs(ax) > 0.98


def test_strip_axis_is_order_independent_up_to_sign():
    ground_truth = [
        FramePlacement("f0", 0.0, (0.0, 0.0)),
        FramePlacement("f1", 4.0, (500.0, 20.0)),
        FramePlacement("f2", -3.0, (1000.0, -25.0)),
    ]
    by_name = {p.name: p for p in ground_truth}
    names = ["f0", "f1", "f2"]
    pair_01 = _ground_truth_pair(
        "f0", "f1", by_name["f0"], by_name["f1"], _FRAME_SIZE, noise_px=0.0, seed=1
    )
    pair_12 = _ground_truth_pair(
        "f1", "f2", by_name["f1"], by_name["f2"], _FRAME_SIZE, noise_px=0.0, seed=2
    )

    baseline = solve_layout(names, _FRAME_SIZE, [pair_01, pair_12])

    scrambled_names = ["f0", "f2", "f1"]
    scrambled_pairs = [_reversed_pair(pair_12), pair_01]
    scrambled = solve_layout(scrambled_names, _FRAME_SIZE, scrambled_pairs)

    assert baseline.strip_axis is not None
    assert scrambled.strip_axis is not None
    dot = (
        baseline.strip_axis[0] * scrambled.strip_axis[0]
        + baseline.strip_axis[1] * scrambled.strip_axis[1]
    )
    assert abs(dot) == pytest.approx(1.0, abs=1e-6)


def test_a_single_frame_has_no_strip_axis():
    layout = solve_layout(["f0"], _FRAME_SIZE, [])
    assert layout.strip_axis is None


def test_weighted_rows_favor_strong_pairs_over_a_weak_one(monkeypatch):
    ground_truth = [
        FramePlacement("f0", 0.0, (0.0, 0.0)),
        FramePlacement("f1", 3.0, (500.0, 50.0)),
        FramePlacement("f2", -2.0, (1000.0, 50.0)),
    ]
    by_name = {p.name: p for p in ground_truth}
    names = ["f0", "f1", "f2"]
    strong_01 = _ground_truth_pair(
        "f0", "f1", by_name["f0"], by_name["f1"], _FRAME_SIZE,
        n_points=200, noise_px=0.05, seed=1,
    )
    strong_12 = _ground_truth_pair(
        "f1", "f2", by_name["f1"], by_name["f2"], _FRAME_SIZE,
        n_points=200, noise_px=0.05, seed=2,
    )
    # An accepted but weak f0-f2 pair: few inliers, high residual, and built
    # from a placement 200px off the true one, so it pulls the solve away
    # from the strong pairs' consensus if given equal say.
    wrong_f2 = FramePlacement("f2", -2.0, (1000.0, 250.0))
    weak_02 = _ground_truth_pair(
        "f0", "f2", by_name["f0"], wrong_f2, _FRAME_SIZE,
        n_points=41, noise_px=5.0, seed=3,
    )

    weighted = solve_layout(names, _FRAME_SIZE, [strong_01, strong_12, weak_02])

    monkeypatch.setattr("scanny_boy.layout._row_weight", lambda pair: 1.0)
    unweighted = solve_layout(names, _FRAME_SIZE, [strong_01, strong_12, weak_02])

    def rms_over_strong_pairs(layout):
        return global_rms(layout.placements, [strong_01, strong_12])

    assert rms_over_strong_pairs(weighted) < rms_over_strong_pairs(unweighted)


def test_rejects_an_implausibly_large_pair_rotation():
    """A 60-degree RANSAC output is data gone wrong, not an internal
    invariant: it must raise the stable residual code (which keeps the
    negative CLAHE-retry eligible), not an AssertionError."""
    ground_truth = [
        FramePlacement("f0", 0.0, (0.0, 0.0)),
        FramePlacement("f1", 60.0, (500.0, 0.0)),
    ]
    by_name = {p.name: p for p in ground_truth}
    names = ["f0", "f1"]
    pair = _ground_truth_pair("f0", "f1", by_name["f0"], by_name["f1"], _FRAME_SIZE, seed=1)

    with pytest.raises(StitchError) as exc_info:
        solve_layout(names, _FRAME_SIZE, [pair])

    assert exc_info.value.code is Code.STITCH_RESIDUAL_TOO_HIGH
    assert "f0" in exc_info.value.message and "f1" in exc_info.value.message


def test_largest_all_covered_rectangle_finds_the_true_maximum():
    mask = np.array(
        [
            [1, 1, 1, 0],
            [1, 1, 1, 0],
            [1, 1, 0, 0],
            [0, 0, 0, 0],
        ],
        dtype=np.uint8,
    )
    x, y, width, height = _largest_all_covered_rectangle(mask)
    assert width * height == 6
    assert np.all(mask[y : y + height, x : x + width] == 1)




# --- 2D grid stitching (docs/GRID_STITCH_PLAN.md sections 3 and 4) ---------

from scanny_boy.layout import GRID_ALIGNMENT_RATIO_MAX, GRID_PITCH_RATIO_MIN
from scanny_boy.selection import GridSpec

_GRID_FRAME_SIZE = (400, 600)  # (height, width)
_STEP_ACROSS = 400  # 2/3 of 600: 1/3 overlap on the long axis
_STEP_DOWN = 267  # 2/3 of 400: 1/3 overlap on the short axis


def _grid_placements(across, down, *, rotation_deg=0.0, displace=None):
    """Placements for an across x down grid: frame (row, col) centred at
    (col * _STEP_ACROSS, row * _STEP_DOWN), all at `rotation_deg`.
    `displace`, when given, is `(name, dx, dy)` shifting one frame's
    ground-truth translation by that many pixels."""
    placements = []
    for row in range(down):
        for col in range(across):
            name = f"f{row * across + col}"
            placements.append(
                FramePlacement(
                    name, rotation_deg, (col * _STEP_ACROSS, row * _STEP_DOWN)
                )
            )
    if displace is not None:
        name, dx, dy = displace
        placements = [
            dataclasses.replace(
                p, translation=(p.translation[0] + dx, p.translation[1] + dy)
            )
            if p.name == name
            else p
            for p in placements
        ]
    return placements


def _grid_pairs(ground_truth, frame_size):
    """Every accepted pair between the placements, exactly as all-pairs
    registration would produce."""
    pairs = []
    for i in range(len(ground_truth)):
        for j in range(i + 1, len(ground_truth)):
            pairs.append(
                _ground_truth_pair(
                    ground_truth[i].name,
                    ground_truth[j].name,
                    ground_truth[i],
                    ground_truth[j],
                    frame_size,
                    seed=i * 100 + j,
                )
            )
    return pairs


@pytest.mark.parametrize(("across", "down"), [(5, 2), (3, 2), (2, 2), (2, 5)])
def test_grid_cells_assign_from_geometry_alone(across, down):
    grid = GridSpec(across=across, down=down)
    ground_truth = _grid_placements(across, down)
    names = [p.name for p in ground_truth]
    layout = solve_layout(
        names,
        _GRID_FRAME_SIZE,
        _grid_pairs(ground_truth, _GRID_FRAME_SIZE),
        grid=grid,
    )

    assert layout.cells is not None
    expected = {
        f"f{i}": (i // across, i % across) for i in range(len(ground_truth))
    }
    assert layout.cells == expected
    assert sorted(layout.cells.values()) == sorted(
        (r, c) for r in range(down) for c in range(across)
    )


def test_grid_cells_assign_in_shuffled_input_order():
    across, down = 5, 2
    grid = GridSpec(across=across, down=down)
    ground_truth = _grid_placements(across, down)
    names = [p.name for p in ground_truth]
    pairs = _grid_pairs(ground_truth, _GRID_FRAME_SIZE)
    baseline = solve_layout(names, _GRID_FRAME_SIZE, pairs, grid=grid)

    rng = np.random.default_rng(9)
    order = list(range(len(names)))
    rng.shuffle(order)
    scrambled_names = [names[i] for i in order]
    scrambled_pairs = [
        _reversed_pair(p) if i % 3 == 0 else p for i, p in enumerate(pairs)
    ]
    scrambled = solve_layout(
        scrambled_names, _GRID_FRAME_SIZE, scrambled_pairs, grid=grid
    )

    assert scrambled.cells is not None
    assert scrambled.cells == baseline.cells


def test_grid_axes_are_orthogonal_and_point_the_expected_way():
    across, down = 5, 2
    grid = GridSpec(across=across, down=down)
    for rotation in (0.0, 3.0):
        ground_truth = _grid_placements(across, down, rotation_deg=rotation)
        names = [p.name for p in ground_truth]
        layout = solve_layout(
            names,
            _GRID_FRAME_SIZE,
            _grid_pairs(ground_truth, _GRID_FRAME_SIZE),
            grid=grid,
        )
        assert layout.grid_axes is not None
        (ax, ay), (dx, dy) = layout.grid_axes
        assert ax * dx + ay * dy == pytest.approx(0.0, abs=1e-9)
        assert math.isclose(math.hypot(ax, ay), 1.0, abs_tol=1e-9)
        assert math.isclose(math.hypot(dx, dy), 1.0, abs_tol=1e-9)
        # The axes are the circular mean of the *solved* rotations — the
        # solve's gauge pins theta_0 = 0, so a uniform true rotation is
        # shifted away and the solved mean sits at 0; assert against the
        # solved placements, not the ground truth.
        angles = np.radians([p.rotation_deg for p in layout.placements])
        mean_angle = math.atan2(
            float(np.sin(angles).sum()), float(np.cos(angles).sum())
        )
        assert ax == pytest.approx(math.cos(mean_angle), abs=1e-9)
        assert ay == pytest.approx(math.sin(mean_angle), abs=1e-9)


def test_grid_pitch_ratio_is_none_for_2x2_and_set_for_5x2():
    two = _grid_placements(2, 2)
    layout2 = solve_layout(
        [p.name for p in two],
        _GRID_FRAME_SIZE,
        _grid_pairs(two, _GRID_FRAME_SIZE),
        grid=GridSpec(across=2, down=2),
    )
    assert layout2.grid_pitch_ratio is None  # no axis has three positions

    five = _grid_placements(5, 2)
    layout5 = solve_layout(
        [p.name for p in five],
        _GRID_FRAME_SIZE,
        _grid_pairs(five, _GRID_FRAME_SIZE),
        grid=GridSpec(across=5, down=2),
    )
    assert layout5.grid_pitch_ratio is not None
    assert layout5.grid_pitch_ratio > GRID_PITCH_RATIO_MIN
    assert layout5.grid_alignment_ratio < GRID_ALIGNMENT_RATIO_MAX


def test_grid_frames_at_45_degrees_to_the_declared_grid_fail_assignment():
    """The steps run 45 degrees away from the frames' own sensor axes: the
    rotation-derived axes and the centre cloud disagree, the SVD
    cross-check catches it, and all four grid fields come back None
    (docs/GRID_STITCH_PLAN.md sections 4.1 and 4.5)."""
    across, down = 5, 2
    grid = GridSpec(across=across, down=down)
    # The solve's gauge pins theta_0 = 0, so the disagreement that matters
    # is the one between the frames' axes and the steps, which is gauge
    # invariant: frames at rotation 0, steps diagonal to their axes —
    # across at +45 degrees, down at -45, so the centres form a true 2D
    # grid rotated away from the frames' own axes.
    angle = np.radians(45.0)
    across_step = _STEP_ACROSS * np.array([np.cos(angle), np.sin(angle)])
    down_step = _STEP_DOWN * np.array([-np.sin(angle), np.cos(angle)])
    ground_truth = []
    for row in range(down):
        for col in range(across):
            t = col * across_step + row * down_step
            ground_truth.append(
                FramePlacement(f"f{row * across + col}", 0.0, tuple(t))
            )
    names = [p.name for p in ground_truth]
    layout = solve_layout(
        names,
        _GRID_FRAME_SIZE,
        _grid_pairs(ground_truth, _GRID_FRAME_SIZE),
        grid=grid,
    )

    assert layout.cells is None
    assert layout.grid_axes is None
    assert layout.grid_pitch_ratio is None
    assert layout.grid_alignment_ratio is None
    # And the blend falls back: with grid_axes None and strip_axis nulled
    # by the spread ratio, feather_axes() is empty — the distance
    # transform's path.
    assert layout.feather_axes() == ()


def test_a_full_cell_displacement_fails_the_bijection():
    """Half a cell or more snaps into a neighbouring cell, the bijection
    fails outright, and all four grid fields are None (§4.1's magnitude
    boundary)."""
    across, down = 5, 2
    grid = GridSpec(across=across, down=down)
    ground_truth = _grid_placements(
        across, down, displace=("f7", float(_STEP_ACROSS), 0.0)
    )
    layout = solve_layout(
        [p.name for p in ground_truth],
        _GRID_FRAME_SIZE,
        _grid_pairs(ground_truth, _GRID_FRAME_SIZE),
        grid=grid,
    )
    assert layout.cells is None
    assert layout.grid_axes is None
    assert layout.grid_pitch_ratio is None
    assert layout.grid_alignment_ratio is None
    assert layout.feather_axes() == ()


def test_a_0_4_cell_displacement_keeps_its_cell_and_trips_alignment_only():
    """Sub-cell drift keeps a clean bijection, so all four grid fields are
    populated — a gap-cutting implementation would return None here — and
    the asymmetry the two checks exist for: the alignment check fires
    (0.4 > 0.25) while the pitch check does not (the displaced column's
    centroid moves by only half the displacement, leaving
    grid_pitch_ratio ~ 0.67, above the 0.6 floor) (§4.5)."""
    across, down = 5, 2
    grid = GridSpec(across=across, down=down)
    drift = 0.4 * _STEP_ACROSS
    ground_truth = _grid_placements(across, down, displace=("f7", drift, 0.0))
    layout = solve_layout(
        [p.name for p in ground_truth],
        _GRID_FRAME_SIZE,
        _grid_pairs(ground_truth, _GRID_FRAME_SIZE),
        grid=grid,
    )
    assert layout.cells is not None
    # The displaced frame kept its true cell: bottom row, same column.
    assert layout.cells["f7"] == (1, 7 % across)

    assert layout.grid_alignment_ratio == pytest.approx(0.4, abs=0.02)
    assert layout.grid_alignment_ratio > GRID_ALIGNMENT_RATIO_MAX
    assert layout.grid_pitch_ratio is not None
    assert layout.grid_pitch_ratio == pytest.approx(320.0 / 480.0, abs=0.02)
    assert layout.grid_pitch_ratio > GRID_PITCH_RATIO_MIN


def test_solve_layout_grid_defaults_to_none():
    """The default preserves every existing call and every existing
    test."""
    ground_truth = [
        FramePlacement("f0", 0.0, (0.0, 0.0)),
        FramePlacement("f1", 0.0, (600.0, 0.0)),
    ]
    pair = _ground_truth_pair(
        "f0", "f1", ground_truth[0], ground_truth[1], _GRID_FRAME_SIZE, seed=1
    )
    layout = solve_layout(["f0", "f1"], _GRID_FRAME_SIZE, [pair])
    assert layout.cells is None
    assert layout.grid_axes is None
    assert layout.grid_pitch_ratio is None
    assert layout.grid_alignment_ratio is None
