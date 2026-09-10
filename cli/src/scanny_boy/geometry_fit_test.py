"""Tests for the staged plumb-line fit (docs/GEOMETRIC_PLAN.md section 8).

The load-bearing test is the round trip: distort a synthetic collinear set
with known parameters, fit, and recover them. Everything else — staging,
gates, held-out evaluation — is plumbing around that objective. The
acceptance gates are the stability gate of docs/STABILITY_GATE.md: the
jackknife spread of leave-one-frame-out refits, not held-out improvement.
"""

import numpy as np
import pytest

from scanny_boy.geometry_fit import (
    GEOMETRY_MAX_RELATIVE_SE,
    GEOMETRY_MIN_IMPROVEMENT_FRACTION,
    GeometryFitError,
    base_camera,
    fit_geometry,
    forward_distort,
    jackknife_relative_se,
    residuals,
)

FRAME_WIDTH, FRAME_HEIGHT = 6048, 4024


def _grid(rows: int, cols: int) -> np.ndarray:
    """A regular grid of ideal points spanning the frame interior."""
    xs = np.linspace(300.0, FRAME_WIDTH - 300.0, cols)
    ys = np.linspace(300.0, FRAME_HEIGHT - 300.0, rows)
    return np.stack(np.meshgrid(xs, ys), axis=-1).reshape(-1, 2)


def _line_sets(points: np.ndarray, rows: int, cols: int) -> list[np.ndarray]:
    """Group grid points into row, column, and both diagonal families —
    the same grouping `charuco.collinear_sets` produces from ids."""
    row_idx = np.arange(len(points)) // cols
    col_idx = np.arange(len(points)) % cols
    sets = []
    for keys in (row_idx, col_idx, row_idx - col_idx, row_idx + col_idx):
        for key in np.unique(keys):
            members = points[keys == key]
            if len(members) >= 4:
                sets.append(members.reshape(-1, 1, 2).astype(np.float32))
    return sets


def _frame_sets(
    k1: float,
    k2: float,
    cx: float,
    cy: float,
    *,
    offset: tuple[float, float],
    noise_px: float,
    rng: np.random.Generator,
) -> list[np.ndarray]:
    """One synthetic calibration frame: the ideal grid translated by
    `offset`, distorted, then perturbed by `noise_px` of corner noise —
    a fresh draw per frame, the way the board position and printed-target
    error differ frame to frame."""
    ideal = _grid(9, 13) + np.asarray(offset)
    observed = forward_distort(ideal, k1, k2, cx, cy, base_camera(FRAME_WIDTH, FRAME_HEIGHT))
    if noise_px:
        observed = observed + rng.normal(0.0, noise_px, observed.shape)
    return _line_sets(observed, 9, 13)


def _distorted_sets(
    k1: float, k2: float, cx: float, cy: float, *, frames: int = 4, noise_px: float = 0.0, seed: int = 0
):
    """Synthetic calibration frames, grouped one inner list per frame and
    split deterministically like the orchestrator's every-4th holdout:
    sorted by name, every 4th frame held out."""
    rng = np.random.default_rng(seed)
    groups = [
        _frame_sets(
            k1, k2, cx, cy, offset=(0.35 * i, 0.65 * i), noise_px=noise_px, rng=rng
        )
        for i in range(frames)
    ]
    train = [g for i, g in enumerate(groups) if i % 4 != 3]
    heldout = [g for i, g in enumerate(groups) if i % 4 == 3]
    return train, heldout


# On a synthetic board the plumb-line sag before correction is roughly
# half the corner-displacement percentage in pixels-of-percent (measured,
# not assumed): distortion just outside the expected 0.03-0.6% band
# produces under 0.7 px of sag, so the absolute improvement diagnostic
# only reads large for distortions well into the suspect band. Real
# captures add CA, demosaic, and board-flatness error the synthetic grid
# does not have — the tests below use magnitudes chosen to reach each
# code path, not to mirror the plan's example numbers.
TRUE_K1 = -0.007
TRUE_K2 = 0.0018


def test_round_trip_recovers_known_parameters():
    """The load-bearing test: synthetic distortion in, fitted parameters
    out."""
    true = {"k1": TRUE_K1, "k2": TRUE_K2, "cx": 3023.4, "cy": 2011.8}
    train, heldout = _distorted_sets(**true)
    result = fit_geometry(train, heldout, FRAME_WIDTH, FRAME_HEIGHT)

    assert result.accepted
    assert result.k1 == pytest.approx(true["k1"], abs=1e-5)
    # k2 trades a little against k1 at this residual scale; 1e-4 is still
    # a tight recovery (about 0.3 px of corner displacement).
    assert result.k2 == pytest.approx(true["k2"], abs=1e-4)
    assert result.cx == pytest.approx(true["cx"], abs=5.0)
    assert result.cy == pytest.approx(true["cy"], abs=5.0)


def test_grouped_signature_does_not_move_the_fit():
    """The regression that protects the grouped signature (docs/
    STABILITY_GATE.md section 1.3): the same frames fed as per-frame
    groups and flattened into one group produce bit-identical k1, k2, cx,
    cy, and both held-out RMS numbers — the grouping feeds only the
    jackknife statistic."""
    train, heldout = _distorted_sets(k1=TRUE_K1, k2=TRUE_K2, cx=3023.4, cy=2011.8)
    grouped = fit_geometry(train, heldout, FRAME_WIDTH, FRAME_HEIGHT)
    one_group = fit_geometry(
        [[s for g in train for s in g]],
        [[s for g in heldout for s in g]],
        FRAME_WIDTH,
        FRAME_HEIGHT,
    )
    assert grouped.k1 == one_group.k1
    assert grouped.k2 == one_group.k2
    assert grouped.cx == one_group.cx
    assert grouped.cy == one_group.cy
    assert grouped.heldout_rms_before == one_group.heldout_rms_before
    assert grouped.heldout_rms_after == one_group.heldout_rms_after
    assert grouped.stage == one_group.stage


def test_jackknife_is_deterministic_and_frame_order_insensitive():
    """Two calls on the same input return the same relative SE exactly;
    reordering the frames changes it only by float tolerance."""
    train, heldout = _distorted_sets(
        k1=TRUE_K1, k2=TRUE_K2, cx=3023.4, cy=2011.8, frames=8, noise_px=2.5, seed=3
    )
    first = fit_geometry(train, heldout, FRAME_WIDTH, FRAME_HEIGHT)
    second = fit_geometry(train, heldout, FRAME_WIDTH, FRAME_HEIGHT)
    assert first.jackknife_relative_se == second.jackknife_relative_se

    reversed_train = list(reversed(train))
    reversed_heldout = list(reversed(heldout))
    reordered = fit_geometry(
        reversed_train, reversed_heldout, FRAME_WIDTH, FRAME_HEIGHT
    )
    assert reordered.jackknife_relative_se == pytest.approx(
        first.jackknife_relative_se, rel=1e-6
    )


def test_real_distortion_at_rig_noise_is_accepted():
    """The section 0.3 conditions the old improvement gate rejected: a
    true ~15 px corner displacement at 2.5 px corner noise. The stability
    gate accepts the fit and the recovered displacement is near truth."""
    true_k1 = -0.0138  # ~15 px of corner displacement at this frame size
    train, heldout = _distorted_sets(
        k1=true_k1, k2=0.0, cx=3024.0, cy=2012.0, frames=16, noise_px=2.5, seed=1
    )
    result = fit_geometry(train, heldout, FRAME_WIDTH, FRAME_HEIGHT)
    assert result.accepted, result.rejection_reason
    assert result.corner_displacement_px == pytest.approx(15.0, abs=4.0)


def test_no_distortion_at_rig_noise_is_rejected_for_instability():
    """Same conditions, zero true distortion: rejected because the
    leave-one-out estimates disagree, not incidentally by the magnitude
    floor — the fitted displacement sits above the hard minimum, so the
    spread is what fails. (Fewer frames than the 16-frame sweep: the null
    is far from the gate line at any frame count, and this keeps the fast
    tier cheap.)"""
    train, heldout = _distorted_sets(
        k1=0.0, k2=0.0, cx=3024.0, cy=2012.0, frames=8, noise_px=2.5, seed=2
    )
    result = fit_geometry(train, heldout, FRAME_WIDTH, FRAME_HEIGHT)
    assert not result.accepted
    assert result.rejection_reason is not None
    assert "relative standard error" in result.rejection_reason
    # Not the magnitude floor: the noise-inflated estimate sits inside the
    # hard band, so the stability gate is the binding rejection.
    assert result.corner_displacement_percent > 0.01


def test_low_noise_old_regime_still_passes():
    """Section 1.4's widening claim: at low corner noise, where the old
    improvement gate accepted, the stability gate accepts too."""
    train, heldout = _distorted_sets(
        k1=TRUE_K1, k2=0.0, cx=3024.0, cy=2012.0, frames=8, noise_px=0.2, seed=4
    )
    result = fit_geometry(train, heldout, FRAME_WIDTH, FRAME_HEIGHT)
    assert result.accepted, result.rejection_reason


def test_jackknife_applies_the_n_minus_1_over_n_factor():
    """The reported SE for a known set of leave-one-out estimates matches
    the closed-form jackknife SE, not their raw standard deviation — the
    one piece of arithmetic that is easy to get quietly wrong."""
    estimates = [14.0, 16.0, 15.0, 13.0, 15.5, 14.5, 15.2, 14.8]
    thetas = np.asarray(estimates)
    n = len(thetas)
    mean, se, relative = jackknife_relative_se(estimates)
    expected_se = float(
        np.sqrt((n - 1) / n * np.sum((thetas - thetas.mean()) ** 2))
    )
    assert mean == pytest.approx(float(thetas.mean()))
    assert se == pytest.approx(expected_se)
    assert relative == pytest.approx(expected_se / float(thetas.mean()))
    # Not the raw standard deviation, which understates by sqrt(n-1).
    raw_sd = float(np.std(thetas, ddof=1))
    assert se == pytest.approx(raw_sd * (n - 1) / np.sqrt(n))
    assert se > raw_sd


def test_heldout_rms_falls_for_real_distortion_and_not_for_straight_input():
    train, heldout = _distorted_sets(k1=TRUE_K1, k2=0.0, cx=3024.0, cy=2012.0)
    result = fit_geometry(train, heldout, FRAME_WIDTH, FRAME_HEIGHT)
    assert result.heldout_rms_after < result.heldout_rms_before

    straight_train, straight_heldout = _distorted_sets(
        k1=0.0, k2=0.0, cx=3024.0, cy=2012.0
    )
    straight = fit_geometry(
        straight_train, straight_heldout, FRAME_WIDTH, FRAME_HEIGHT
    )
    assert straight.heldout_rms_before == pytest.approx(0.0, abs=1e-3)
    assert not straight.accepted


def test_k2_free_synthetic_selects_stage_k1():
    """Staged selection: data with no k2 term must not spend the extra
    parameter — stage k1 wins because k1k2 does not beat it by the
    improvement fraction."""
    train, heldout = _distorted_sets(k1=TRUE_K1, k2=0.0, cx=3024.0, cy=2012.0)
    result = fit_geometry(train, heldout, FRAME_WIDTH, FRAME_HEIGHT)
    assert result.stage == "k1"
    assert set(result.stage_heldout_rms) == {"k1", "k1k2", "k1k2c"}


def test_null_fit_is_rejected_by_the_magnitude_floor():
    """A displacement below the hard minimum is not a lens this rig could
    have: rejected by the plausible-magnitude band regardless of how
    stable the estimate is."""
    train, heldout = _distorted_sets(k1=-0.00001, k2=0.0, cx=3024.0, cy=2012.0)
    result = fit_geometry(train, heldout, FRAME_WIDTH, FRAME_HEIGHT)
    assert not result.accepted
    assert result.rejection_reason is not None
    assert "band" in result.rejection_reason


def test_wildly_out_of_band_magnitude_is_rejected():
    """A distortion this large (over 1% of the half-diagonal at the corner)
    is not a lens this rig could have; the magnitude gate rejects it even
    though the straightness fit itself is excellent."""
    train, heldout = _distorted_sets(k1=-0.05, k2=0.0, cx=3024.0, cy=2012.0)
    result = fit_geometry(train, heldout, FRAME_WIDTH, FRAME_HEIGHT)
    assert not result.accepted
    assert result.rejection_reason is not None
    assert "band" in result.rejection_reason


def test_moderately_out_of_band_magnitude_is_suspect_not_rejected():
    """Between the expected 0.03-0.6% band and the hard 0.01-1.0% band the
    fit is accepted with a warning, not dropped (section 4.5)."""
    train, heldout = _distorted_sets(k1=-0.022, k2=0.0, cx=3024.0, cy=2012.0)
    result = fit_geometry(train, heldout, FRAME_WIDTH, FRAME_HEIGHT)
    assert result.accepted
    assert result.suspect


def test_no_training_sets_raises_insufficient_frames():
    with pytest.raises(GeometryFitError):
        fit_geometry([], [], FRAME_WIDTH, FRAME_HEIGHT)


def test_residuals_are_float64():
    """The optimiser's default finite-difference step on k1 (~1.5e-8)
    shifts the residuals by ~1e-5 px; in float32 that is below the
    quantisation of the pixel values, so the Jacobian column for k1
    collapses to exact zero and the stage solve never moves. float32 input
    corners (what detection produces) must not leak into the residual."""
    train, _ = _distorted_sets(k1=TRUE_K1, k2=0.0, cx=3024.0, cy=2012.0)
    K = base_camera(FRAME_WIDTH, FRAME_HEIGHT)
    residual = residuals(
        np.array([0.0, 0.0, K[0, 2], K[1, 2]]),
        [s for group in train for s in group],
        K,
    )
    assert residual.dtype == np.float64


def test_constants_are_pinned():
    """Pin the constants this module owns and nowhere else."""
    assert GEOMETRY_MAX_RELATIVE_SE == 0.25
    assert GEOMETRY_MIN_IMPROVEMENT_FRACTION == 0.30
