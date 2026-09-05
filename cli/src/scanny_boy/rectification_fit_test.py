"""Synthetic-ground-truth tests for the rig-tilt rectification fit and the
closed-form rectification core (docs/RECTIFICATION_PLAN.md sections 2-4).

Scenes are built from point correspondences, not rendered frames: a known
`W` rectifies frame b's points, a known similarity maps them onto frame a,
and `W⁻¹` maps them back — plus pixel noise. The fit must recover the tilt,
must refuse a true zero, and must refuse anything its gates reject.
"""

import dataclasses

import numpy as np
import pytest

from scanny_boy.rectification_fit import fit_rectification
from scanny_boy.registration import (
    PairResult,
    Rectification,
    rectified_pairs,
    rectify,
    rigid_from_correspondences,
    similarity_from_correspondences,
    unrectify,
)

_FRAME_SIZE = (1400, 2100)  # (height, width)
_FOCAL_PX = 9000.0
_NOISE_PX = 0.3
# The overlaps are a band on one side, like the capture workflow's
# minimum-overlap case — the regime where the per-pair homography degrades
# and the two-parameter model must hold.
_BAND_PX = 700.0


def _make_rectification(tilt_x_deg: float, tilt_y_deg: float) -> Rectification:
    height, width = _FRAME_SIZE
    l = np.array(
        [
            np.tan(np.deg2rad(tilt_x_deg)) / _FOCAL_PX,
            np.tan(np.deg2rad(tilt_y_deg)) / _FOCAL_PX,
        ]
    )
    return Rectification(
        l=l,
        centre=np.array([width / 2.0, height / 2.0]),
        frame_size=_FRAME_SIZE,
        rms_before_px=0.0,
        rms_after_px=0.0,
        relative_improvement=0.0,
        pair_count=0,
    )


def _scene_pairs(
    truth: Rectification,
    shift: tuple[float, float],
    *,
    n_pairs: int = 3,
    n_points: int = 500,
    seed: int = 3,
    accept: bool = True,
) -> list[PairResult]:
    """`n_pairs` accepted pairs whose ground-truth inter-frame map is
    `W⁻¹ · S · W` with `S` a pure translation of `shift * k`. Correspondence
    noise at `_NOISE_PX` on both point sets is the keypoint noise floor."""
    rng = np.random.default_rng(seed)
    width = _FRAME_SIZE[1]
    centre = truth.centre
    pairs = []
    for k in range(1, n_pairs + 1):
        q_b = rng.uniform(-centre, centre, size=(n_points, 2))
        q_b = q_b[q_b[:, 0] > width / 2.0 - _BAND_PX]
        p_b = q_b + centre
        # The true inter-frame map: a translation in *rectified* space,
        # composed as p_a = W⁻¹ · S_k · W · p_b — the model the fit
        # assumes, so its global minimum is exactly `truth` (plus noise).
        p_a = unrectify(rectify(p_b, truth) + np.asarray(shift) * k, truth)
        noisy_b = p_b + rng.normal(0.0, _NOISE_PX, p_b.shape)
        noisy_a = p_a + rng.normal(0.0, _NOISE_PX, p_a.shape)
        pairs.append(
            PairResult(
                a=f"frame{k - 1}",
                b=f"frame{k}",
                transform=rigid_from_correspondences(noisy_b, noisy_a),
                good_matches=len(noisy_b),
                inliers=len(noisy_b),
                inlier_ratio=0.6,
                rms_residual_px=1.0,
                scale_drift=0.0,
                accepted=accept,
                reject_code=None,
                reject_message=None,
                inlier_points_a=noisy_a,
                inlier_points_b=noisy_b,
                overlap_fraction=None,
                overlap_mad=None,
                overlap_mad_pregain=None,
                similarity_transform=rigid_from_correspondences(noisy_b, noisy_a),
                similarity_scale=1.0,
            )
        )
    return pairs


def test_recovers_a_known_two_axis_tilt():
    truth = _make_rectification(1.0, 0.6)
    pairs = _scene_pairs(truth, shift=(1500.0, 0.0))

    fit = fit_rectification(pairs, _FRAME_SIZE)

    assert fit is not None
    assert np.allclose(fit.l, truth.l, rtol=0.1)
    assert np.allclose(fit.centre, truth.centre)
    # The floor is the per-point norm of 2D noise: sqrt(2) * _NOISE_PX.
    assert fit.rms_after_px < 2.0 * _NOISE_PX
    assert fit.relative_improvement > 0.5
    assert fit.pair_count == 3


def test_true_zero_tilt_is_rejected_not_invented():
    """The regression that keeps this honest: similarity-consistent
    correspondences must not produce a tilt (the synthetic zero case in
    docs/RECTIFICATION_PLAN.md section 0 recovered ±0.02° and was dropped
    by the improvement gate)."""
    truth = _make_rectification(0.0, 0.0)
    pairs = _scene_pairs(truth, shift=(1500.0, 0.0))

    assert fit_rectification(pairs, _FRAME_SIZE) is None


def test_a_single_accepted_pair_is_rejected():
    truth = _make_rectification(1.0, 0.6)
    pairs = _scene_pairs(truth, shift=(1500.0, 0.0), n_pairs=1)

    assert fit_rectification(pairs, _FRAME_SIZE) is None


def test_unaccepted_pairs_do_not_feed_the_fit():
    """Only accepted pairs count toward the support gate: three pairs that
    all failed their gates behave like no pairs at all."""
    truth = _make_rectification(1.0, 0.6)
    pairs = _scene_pairs(truth, shift=(1500.0, 0.0), accept=False)

    assert fit_rectification(pairs, _FRAME_SIZE) is None


def test_a_tilt_below_the_improvement_gate_is_rejected():
    """A small tilt improves the fit, just not enough. Self-validating: the
    same scene at ten times the tilt must be accepted, so the rejection is
    the gate's magnitude and not a broken fit."""
    truth = _make_rectification(0.05, 0.05)
    pairs = _scene_pairs(truth, shift=(1500.0, 0.0))

    assert fit_rectification(pairs, _FRAME_SIZE) is None

    loud = _make_rectification(0.5, 0.5)
    assert (
        fit_rectification(_scene_pairs(loud, shift=(1500.0, 0.0)), _FRAME_SIZE)
        is not None
    )


def test_an_implausibly_large_tilt_is_rejected_by_the_excursion_gate():
    """The excursion bound is the numerical guard for compositing's
    division; a wild `l` must never reach the canvas even when it would
    improve the RMS."""
    truth = _make_rectification(10.0, 10.0)
    pairs = _scene_pairs(truth, shift=(1500.0, 0.0))

    height, width = _FRAME_SIZE
    centre = np.array([width / 2.0, height / 2.0])
    corners = np.array(
        [[0, 0], [width, 0], [width, height], [0, height]], dtype=np.float64
    )
    assert np.max(np.abs((corners - centre) @ truth.l)) > 0.02

    assert fit_rectification(pairs, _FRAME_SIZE) is None


def test_the_fit_is_deterministic_and_order_independent():
    truth = _make_rectification(1.0, 0.6)
    pairs = _scene_pairs(truth, shift=(1500.0, 0.0))

    first = fit_rectification(pairs, _FRAME_SIZE)
    again = fit_rectification(pairs, _FRAME_SIZE)
    reversed_order = fit_rectification(list(reversed(pairs)), _FRAME_SIZE)

    assert first is not None
    assert np.array_equal(first.l, again.l)
    # Reordered residuals take a different floating-point path through the
    # same optimum; bitwise equality is only promised for identical order.
    assert np.allclose(first.l, reversed_order.l, rtol=1e-9)


def test_rectify_unrectify_round_trips():
    truth = _make_rectification(1.0, 0.6)
    rng = np.random.default_rng(11)
    points = rng.uniform(-2000, 2000, size=(200, 2))

    round_tripped = unrectify(rectify(points, truth), truth)

    assert np.allclose(round_tripped, points, atol=1e-9)


def test_rectified_pairs_land_on_the_noise_floor():
    """The physical evidence: under the true `W`, every pair's re-fitted
    transform is a similarity to within the noise, so the residual lands at
    the keypoint floor and the scale the tilt was absorbing collapses to
    1 — while acceptance and the match statistics carry over unchanged."""
    truth = _make_rectification(1.0, 0.6)
    pairs = _scene_pairs(truth, shift=(1500.0, 0.0))

    re_fitted = rectified_pairs(pairs, truth)

    assert [pair.accepted for pair in re_fitted] == [pair.accepted for pair in pairs]
    assert [pair.good_matches for pair in re_fitted] == [
        pair.good_matches for pair in pairs
    ]
    for pair in re_fitted:
        # The floor is 2*noise in expectation; finite samples sit above it.
        assert pair.rms_residual_px < 2.5 * _NOISE_PX
        assert pair.scale_drift < 0.001
    for original, re_fitted_pair in zip(pairs, re_fitted):
        src = rectify(original.inlier_points_b, truth)
        dst = rectify(original.inlier_points_a, truth)
        similarity, scale = similarity_from_correspondences(src, dst)
        assert scale == pytest.approx(re_fitted_pair.similarity_scale)
        assert np.allclose(similarity, re_fitted_pair.similarity_transform)
        assert np.allclose(
            re_fitted_pair.inlier_points_b,
            rectify(original.inlier_points_b, truth),
        )


def test_rectified_pairs_passthrough_without_enough_inliers():
    """A pair with no usable inliers (an unaccepted reject) keeps its
    original fields — there is nothing to re-fit and nothing consumes it."""
    truth = _make_rectification(1.0, 0.6)
    (pair,) = _scene_pairs(truth, shift=(1500.0, 0.0), n_pairs=1)
    empty = dataclasses.replace(
        pair,
        a="x",
        b="y",
        accepted=False,
        inlier_points_a=np.zeros((0, 2)),
        inlier_points_b=np.zeros((0, 2)),
    )

    re_fitted = rectified_pairs([empty], truth)

    assert re_fitted[0] is empty
