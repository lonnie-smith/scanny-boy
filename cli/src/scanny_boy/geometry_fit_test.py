"""Tests for the staged plumb-line fit (docs/GEOMETRIC_PLAN.md section 8).

The load-bearing test is the round trip: distort a synthetic collinear set
with known parameters, fit, and recover them. Everything else — staging,
gates, held-out evaluation — is plumbing around that objective.
"""

import numpy as np
import pytest

from scanny_boy.geometry_fit import (
    GEOMETRY_MIN_IMPROVEMENT_FRACTION,
    GeometryFitError,
    base_camera,
    fit_geometry,
    forward_distort,
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


def _distorted_sets(k1: float, k2: float, cx: float, cy: float):
    K = base_camera(FRAME_WIDTH, FRAME_HEIGHT)
    ideal = _grid(9, 13)
    observed = forward_distort(ideal, k1, k2, cx, cy, K)
    sets = _line_sets(observed, 9, 13)
    # Deterministic split, mirroring the orchestrator's every-4th holdout.
    train = sets[0::2]
    heldout = sets[1::2]
    return train, heldout


# On a synthetic board the plumb-line sag before correction is roughly
# half the corner-displacement percentage in pixels-of-percent (measured,
# not assumed): distortion inside the expected 0.03-0.2% band produces
# under 0.3 px of sag, so the absolute gate only clears for distortions
# in the suspect band. Real captures add CA, demosaic, and board-flatness
# error the synthetic grid does not have — the tests below use magnitudes
# chosen to reach each code path, not to mirror the plan's example
# numbers.
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


def test_null_fit_is_rejected_by_the_improvement_gates():
    train, heldout = _distorted_sets(k1=-0.00001, k2=0.0, cx=3024.0, cy=2012.0)
    result = fit_geometry(train, heldout, FRAME_WIDTH, FRAME_HEIGHT)
    assert not result.accepted
    assert result.rejection_reason is not None
    assert "held-out RMS" in result.rejection_reason


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
    """Between the expected 0.03-0.2% band and the hard 0.01-1.0% band the
    fit is accepted with a warning, not dropped (section 4.5)."""
    train, heldout = _distorted_sets(k1=TRUE_K1, k2=0.0, cx=3024.0, cy=2012.0)
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
    residual = residuals(np.array([0.0, 0.0, K[0, 2], K[1, 2]]), train, K)
    assert residual.dtype == np.float64


def test_improvement_gates_are_both_required():
    """A fit must clear the relative and the absolute gate together — pin
    the constants this module owns and nowhere else."""
    assert GEOMETRY_MIN_IMPROVEMENT_FRACTION == 0.30
