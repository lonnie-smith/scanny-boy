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
    """Builds a PairResult whose transform and inlier correspondences are
    exactly what register_pair would have produced for two frames placed at
    the given ground-truth global poses, optionally with small measurement
    noise on the correspondences."""
    rng = np.random.default_rng(seed)
    height, width = frame_size

    rotation_a = _rotation_matrix(placement_a.rotation_deg)
    translation_a = np.array(placement_a.translation)
    translation_b = np.array(placement_b.translation)

    phi_ab_deg = placement_b.rotation_deg - placement_a.rotation_deg
    u_ab = rotation_a.T @ (translation_b - translation_a)
    rotation_ab = _rotation_matrix(phi_ab_deg)

    pts_b = rng.uniform([0, 0], [width, height], size=(n_points, 2))
    pts_a = pts_b @ rotation_ab.T + u_ab
    if noise_px:
        pts_a = pts_a + rng.normal(0, noise_px, size=pts_a.shape)

    transform = np.hstack([rotation_ab, u_ab.reshape(2, 1)])

    return PairResult(
        a=name_a,
        b=name_b,
        transform=transform,
        good_matches=n_points,
        inliers=n_points,
        inlier_ratio=1.0,
        rms_residual_px=noise_px if noise_px else 0.0,
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


def _reversed_pair(pair: PairResult) -> PairResult:
    rotation = pair.transform[:, :2]
    translation = pair.transform[:, 2]
    rotation_inv = rotation.T
    translation_inv = -rotation_inv @ translation
    transform_inv = np.hstack([rotation_inv, translation_inv.reshape(2, 1)])
    return dataclasses.replace(
        pair,
        a=pair.b,
        b=pair.a,
        transform=transform_inv,
        inlier_points_a=pair.inlier_points_b,
        inlier_points_b=pair.inlier_points_a,
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


def test_rejects_an_implausibly_large_pair_rotation():
    ground_truth = [
        FramePlacement("f0", 0.0, (0.0, 0.0)),
        FramePlacement("f1", 50.0, (500.0, 0.0)),
    ]
    by_name = {p.name: p for p in ground_truth}
    names = ["f0", "f1"]
    pair = _ground_truth_pair("f0", "f1", by_name["f0"], by_name["f1"], _FRAME_SIZE, seed=1)

    with pytest.raises(AssertionError):
        solve_layout(names, _FRAME_SIZE, [pair])


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
