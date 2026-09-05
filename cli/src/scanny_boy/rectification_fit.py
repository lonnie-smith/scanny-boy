"""The rig-tilt rectification fit: two shared parameters per negative.

`fit_rectification` implements docs/RECTIFICATION_PLAN.md sections 2 and 3.
The film plane is not fronto-parallel to the camera, so the true
frame-to-frame map is a homography, `H = W⁻¹ · S · W`, with one globally
shared rectifying homography `W(l) = [[1,0,0],[0,1,0],[l1,l2,1]]` and a
per-pair similarity. Fitting a similarity alone to homography-shaped data
leaves a small systematic residual per pair that accumulates along a strip
into visibly curved film edges (`scripts/measure-tilt.py` measured the
effect and the tilt magnitudes on real scans).

The fit minimises, over `(l1, l2)` only, the RMS residual where each pair's
similarity is re-fit **in closed form** (Umeyama) inside the residual on
the rectified points — no per-pair parameter is ever handed to the
optimiser, which is why the two-parameter model holds where per-pair
homographies degrade as overlap narrows. The optimiser runs unweighted
(the layout solves' `sqrt(inliers)/rms` row weighting is deliberately not
replicated; the zero-tilt validation ran unweighted), over the accepted
pairs' inlier correspondences in canonical pair order, so the result is
deterministic and independent of placement order.

The acceptance gates follow `docs/GEOMETRIC_PLAN.md` section 4.5's
discipline — the fit that does not measurably help is dropped, not an
error: a rejected fit returns `None` and the negative stitches exactly as
it would have before this module existed.
"""

from __future__ import annotations

import math

import numpy as np
from scipy.optimize import least_squares

from scanny_boy.registration import (
    PairResult,
    Rectification,
    similarity_from_correspondences,
)

# A negative with fewer accepted pairs than this has no chain for a tilt to
# bow, and a single pair's `l` is statistically weak (fitted from one thin
# overlap band). A floor, not a measured threshold.
MIN_ACCEPTED_PAIRS = 2

# The fit is applied only when the shared-`l` model beats the per-pair
# similarity fits by at least this relative RMS improvement over the same
# inliers. Chosen from the scripts/measure-tilt.py run on real scans: real
# tilts improve 40-90%+, true zero recovers as zero with no improvement,
# and this sits between with margin. Provisional — to be confirmed against
# the user's R1 roll at a user gate, like MAX_OVERLAP_MAD's semantics were.
MIN_RELATIVE_IMPROVEMENT = 0.15

# The homogeneous weight `1 + l . q` must stay within this band across the
# frame's corners. Keeps the rectification moderate — at this rig's
# geometry it bounds the equivalent tilt near a few degrees — and guards
# the division in `unrectify` when compositing. A sanity bound, not a
# measured threshold, the same role `_FEATHER_FLOOR` plays.
MAX_WEIGHT_EXCURSION = 0.02

# Below this pass-1 similarity RMS the correspondences are already
# similarity-consistent to within float noise; there is nothing for a
# tilt to explain and `relative_improvement` would be meaningless.
_MIN_RMS_BEFORE_PX = 1e-9


def _rectify_centred(
    points: np.ndarray, l: np.ndarray, centre: np.ndarray
) -> np.ndarray:
    """The centred-coordinate rectification the residual evaluates.

    Absolute coordinates come from `registration.rectify` once the fit is
    accepted; inside the fit `l` acts on points centred at the frame
    centre, which is what keeps the two parameters well-conditioned."""
    q = points - centre
    return q / (1.0 + q @ l)[:, np.newaxis]


def _model_residuals(
    fit_pairs: list[tuple[np.ndarray, np.ndarray]],
    l: np.ndarray,
    centre: np.ndarray,
) -> np.ndarray:
    """Each pair's similarity re-fit in closed form on the rectified
    points, stacked as (N, 2) blocks in canonical pair order — the same
    shape `_rms_residual`'s per-point convention works on. `l = 0` is the
    per-pair similarity baseline the improvement gate measures against:
    with no rectification the model reduces to exactly that."""
    blocks = []
    for src, dst in fit_pairs:
        src_rect = _rectify_centred(src, l, centre)
        dst_rect = _rectify_centred(dst, l, centre)
        sim, _ = similarity_from_correspondences(src_rect, dst_rect)
        blocks.append(src_rect @ sim[:, :2].T + sim[:, 2] - dst_rect)
    return np.concatenate(blocks, axis=0)


def _rms(residuals: np.ndarray) -> float:
    """RMS of per-point Euclidean residuals — `registration._rms_residual`'s
    convention, so the fit's numbers are comparable with the gates'."""
    return math.sqrt(float(np.mean(np.sum(residuals**2, axis=1))))


def fit_rectification(
    pairs: list[PairResult], frame_size: tuple[int, int]
) -> Rectification | None:
    """Fit one shared `W` to a negative's accepted pairs, or return `None`.

    `pairs` are the pass-1 `PairResult`s — `pair.accepted` decides what
    feeds the fit, and `inlier_points_a/b` are undistorted full-resolution
    px when a calibration profile is active, so the rectification lives in
    undistorted coordinates. `frame_size` is (height, width), identical for
    every frame of the negative.

    Gates, in order, each returning `None`: support (enough accepted
    pairs), excursion (`l`'s weight stays in its band across the frame),
    and improvement (the shared model must beat the per-pair similarity
    fits by `MIN_RELATIVE_IMPROVEMENT`)."""
    usable = [pair for pair in pairs if pair.accepted]
    if len(usable) < MIN_ACCEPTED_PAIRS:
        return None

    centre = np.array([frame_size[1] / 2.0, frame_size[0] / 2.0])
    fit_pairs = [(pair.inlier_points_b, pair.inlier_points_a) for pair in usable]

    rms_before = _rms(_model_residuals(fit_pairs, np.zeros(2), centre))
    if rms_before <= _MIN_RMS_BEFORE_PX:
        return None

    result = least_squares(
        lambda params: _model_residuals(fit_pairs, params, centre).ravel(),
        np.zeros(2),
        method="lm",
    )
    l = result.x
    rms_after = _rms(result.fun.reshape(-1, 2))

    height, width = frame_size
    corners = np.array(
        [[0, 0], [width, 0], [width, height], [0, height]], dtype=np.float64
    )
    excursion = float(np.max(np.abs((corners - centre) @ l)))
    if excursion > MAX_WEIGHT_EXCURSION:
        return None

    relative_improvement = 1.0 - rms_after / rms_before
    if relative_improvement < MIN_RELATIVE_IMPROVEMENT:
        return None

    return Rectification(
        l=l,
        centre=centre,
        frame_size=frame_size,
        rms_before_px=rms_before,
        rms_after_px=rms_after,
        relative_improvement=relative_improvement,
        pair_count=len(usable),
    )
