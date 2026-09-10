"""The staged plumb-line distortion fit.

The objective is straightness: every ChArUco corner carries the row, column,
or diagonal family its id names, and after undistortion each family's points
must be collinear. The residual is each point's perpendicular distance to
its own family's best-fit line — `cv2.undistortPoints` inverts the forward
model iteratively, which is slower per evaluation and exactly the point:
the fitted coefficients are already in the OpenCV forward convention every
consumer (`initUndistortRectifyMap`, the closed-form band map of composite)
wants, with no conversion.

Gauge convention: `K_new = K` and the undistorted frame has exactly the
source frame's pixel dimensions. `K` itself is fixed — straightness is
scale-invariant, so `K` only sets the normalisation of `r`, and holding it
fixed keeps coefficients comparable across sessions.

Every constant in this module is defined here and nowhere else.
"""

from __future__ import annotations

import dataclasses

import cv2
import numpy as np
from scipy.optimize import least_squares

from scanny_boy.events import Code

# Take an earlier stage unless the next stage beats its held-out RMS by at
# least this relative fraction.
STAGE_IMPROVEMENT_FRACTION = 0.05
# The improvement numbers' constants, kept as the reported diagnostic's
# own thresholds. They are no longer
# the acceptance criterion: on this rig the held-out straightness floor is
# printed-target error, not lens error, so a fit can be recovering the
# right coefficient while the improvement metric sits at zero.
GEOMETRY_MIN_IMPROVEMENT_FRACTION = 0.30
GEOMETRY_MIN_IMPROVEMENT_PX = 0.3
# Magnitude sanity band, as a percentage of the half-diagonal.
MAGNITUDE_HARD_MIN_PERCENT = 0.01
MAGNITUDE_HARD_MAX_PERCENT = 1.0
MAGNITUDE_EXPECTED_MIN_PERCENT = 0.03
# Raised from 0.2: the rig's lens measures 0.398% by ChArUco and 0.46% by
# stitch correspondences (two independent instruments, no shared data),
# so the old band would have flagged the known truth suspect on every
# calibration.
MAGNITUDE_EXPECTED_MAX_PERCENT = 0.6
# The jackknife stability gate (see jackknife_relative_se below): the
# leave-one-out corner-displacement spread, as a relative standard error,
# must not exceed this. From the synthetic sweep in
# scripts/measure-stability-gate.py: at this rig's ~2.5 px corner noise
# and 16 frames, the statistic reads ~17% at a true 15 px distortion and
# ~174% at true zero, so 25% separates a real distortion from none.
# Unmeasured against real (non-synthetic) scans.
GEOMETRY_MAX_RELATIVE_SE = 0.25


class GeometryFitError(Exception):
    """The staged fit failed for a reason the acceptance gates cannot
    express — degenerate input, no convergence. The orchestrator maps this
    onto the flat-field family's error type."""

    def __init__(self, code: Code, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclasses.dataclass(frozen=True)
class GeometryFitResult:
    """The staged fit's outcome, gates included — a rejected fit is a
    result, not an exception — the profile is still created either way."""

    k1: float
    k2: float
    cx: float
    cy: float
    stage: str  # "k1" | "k1k2" | "k1k2c"
    heldout_rms_before: float
    heldout_rms_after: float
    stage_heldout_rms: dict[str, float]
    corner_displacement_px: float
    corner_displacement_percent: float
    accepted: bool
    rejection_reason: str | None
    suspect: bool  # outside the expected band but inside the hard one
    # The stability gate's statistic: jackknife mean/SE of the corner
    # displacement over leave-one-frame-out refits, and the frame count
    # they came from.
    jackknife_corner_px_mean: float | None = None
    jackknife_corner_px_se: float | None = None
    jackknife_relative_se: float | None = None
    jackknife_frames: int | None = None


def base_camera(frame_width: int, frame_height: int) -> np.ndarray:
    """The fixed camera matrix: `fx = fy = max(w, h)`,
    principal point at the frame centre. Held fixed so coefficients stay
    comparable across sessions; `cx, cy` are the fit's starting point, not
    necessarily its result."""
    fx = float(max(frame_width, frame_height))
    cx = (frame_width - 1) / 2
    cy = (frame_height - 1) / 2
    return np.array([[fx, 0.0, cx], [0.0, fx, cy], [0.0, 0.0, 1.0]], dtype=np.float64)


def forward_distort(points: np.ndarray, k1: float, k2: float, cx: float, cy: float, K: np.ndarray) -> np.ndarray:
    """The OpenCV forward model applied to `(N, 2)` pixel coordinates — the
    inverse of what `cv2.undistortPoints` computes, in closed form. Used to
    synthesise test data and to measure corner displacement."""
    fx, fy = K[0, 0], K[1, 1]
    x = (points[:, 0] - cx) / fx
    y = (points[:, 1] - cy) / fy
    r2 = x * x + y * y
    k = 1.0 + k1 * r2 + k2 * (r2 * r2)
    return np.stack([x * k * fx + cx, y * k * fy + cy], axis=-1)


def residuals(p: np.ndarray, line_sets: list[np.ndarray], K_base: np.ndarray) -> np.ndarray:
    """Perpendicular straightness residuals for `p = (k1, k2, cx, cy)`,
    concatenated over every collinear set. `K_base` supplies fx/fy; the
    principal point comes from `p` — it is the fit's unknown, never a
    constant."""
    k1, k2, cx, cy = p
    K = K_base.copy()
    K[0, 2], K[1, 2] = cx, cy
    D = np.array([k1, k2, 0.0, 0.0, 0.0])
    out = []
    for pts in line_sets:  # (N, 1, 2) float32
        # Undistort in float64: in float32 the pixel values quantise at
        # ~2e-4 px, which swallows the ~1e-5 px residual shift of the
        # optimiser's finite-difference step on k1 (which starts at 0) and
        # collapses the Jacobian column to exact zero — the stage solve
        # then terminates without ever moving. Platform rounding decides
        # whether the collapse happens, so this must not be left to
        # float32.
        u = cv2.undistortPoints(pts.astype(np.float64), K, D, P=K).reshape(-1, 2)
        centred = u - u.mean(0)
        normal = np.linalg.svd(centred, full_matrices=False)[2][1]
        out.append(centred @ normal)
    return np.concatenate(out)


def _rms(residual: np.ndarray) -> float:
    return float(np.sqrt(np.mean(residual**2)))


def _fit_stage(
    stage: str,
    train_sets: list[np.ndarray],
    K_base: np.ndarray,
    x0: np.ndarray,
) -> np.ndarray:
    """One stage's least-squares solve. Stages `k1`/`k1k2` fix the centre at
    the image centre by slicing the parameter vector; only `k1k2c` frees
    it."""
    if stage == "k1":
        index = [0]
    elif stage == "k1k2":
        index = [0, 1]
    elif stage == "k1k2c":
        index = [0, 1, 2, 3]
    else:  # pragma: no cover - internal
        raise ValueError(stage)

    def unpack(sub: np.ndarray) -> np.ndarray:
        p = x0.copy()
        p[index] = sub
        return p

    result = least_squares(
        lambda sub: residuals(unpack(sub), train_sets, K_base),
        x0[index],
        loss="huber",
        f_scale=1.0,
    )
    return unpack(result.x)


def _corner_displacement(
    k1: float, k2: float, cx: float, cy: float, K_base: np.ndarray, frame_width: int, frame_height: int
) -> tuple[float, float]:
    """Displacement of the image corner under the forward model, in pixels
    and as a percentage of the half-diagonal."""
    corner = np.array([[0.0, 0.0]])
    distorted = forward_distort(corner, k1, k2, cx, cy, K_base)
    displacement = float(np.hypot(*(distorted[0] - corner[0])))
    half_diagonal = float(np.hypot(frame_width, frame_height) / 2)
    return displacement, displacement / half_diagonal * 100.0


def _staged_fit(
    train_sets_flat: list[np.ndarray],
    heldout_sets_flat: list[np.ndarray],
    K_base: np.ndarray,
    x0: np.ndarray,
) -> tuple[str, np.ndarray, dict[str, float]]:
    """The three stage solves and the held-out stage selection of section
    4.4. Shared by the main fit and by every jackknife refit, so a
    leave-one-out estimate is exactly the fit the gate is judging."""
    stage_rms: dict[str, float] = {}
    stage_params: dict[str, np.ndarray] = {}
    for stage in ("k1", "k1k2", "k1k2c"):
        params = _fit_stage(stage, train_sets_flat, K_base, x0)
        stage_params[stage] = params
        stage_rms[stage] = _rms(residuals(params, heldout_sets_flat, K_base))

    # The earliest stage the next does not beat by
    # STAGE_IMPROVEMENT_FRACTION relative. On a lens this clean, expect
    # stage 1 to win.
    chosen = "k1"
    for stage, nxt in (("k1", "k1k2"), ("k1k2", "k1k2c")):
        if stage_rms[nxt] < stage_rms[stage] * (1.0 - STAGE_IMPROVEMENT_FRACTION):
            chosen = nxt
        else:
            break

    return chosen, stage_params[chosen], stage_rms


def _as_frame_groups(
    sets: list[list[np.ndarray]] | list[np.ndarray],
) -> list[list[np.ndarray]]:
    """The grouped per-frame form is the signature's contract; a flat list
    of sets — the historical call shape — counts as one frame's worth.
    Distinguishable by element type, since a set is an array and a group
    is a list."""
    if not sets:
        return []
    if isinstance(sets[0], np.ndarray):
        return [list(sets)]
    return [list(group) for group in sets]


def jackknife_relative_se(estimates: list[float]) -> tuple[float, float, float]:
    """The jackknife standard error of leave-one-out estimates, its mean,
    and the mean-relative form the gate uses:

        SE = sqrt( (n - 1) / n * sum_i (theta_i - theta_bar)^2 )

    The `(n - 1)/n` factor is not decoration: leave-one-out estimates are
    strongly correlated, so the raw standard deviation of the `theta_i`
    understates the true spread by roughly `sqrt(n - 1)`; without the
    factor the threshold would silently depend on how many frames were
    shot. Deterministic — no seed, no resampling draw. With fewer than
    two estimates (or a zero mean) the spread is meaningless and the
    relative form is infinite."""
    thetas = np.asarray(estimates, dtype=np.float64)
    n = len(thetas)
    if n == 0:
        return float("nan"), float("inf"), float("inf")
    mean = float(thetas.mean())
    if n < 2 or mean <= 0.0:
        return mean, float("inf"), float("inf")
    se = float(np.sqrt((n - 1) / n * np.sum((thetas - mean) ** 2)))
    return mean, se, se / mean


def fit_geometry(
    train_sets: list[list[np.ndarray]],
    heldout_sets: list[list[np.ndarray]],
    frame_width: int,
    frame_height: int,
) -> GeometryFitResult:
    """The staged fit, held-out evaluation, and acceptance gates of sections
    4.4 and 4.5, in one call. Never raises for a rejected fit — the result
    carries `accepted=False` and the reason; only degenerate inputs (no
    training sets) raise.

    `train_sets` and `heldout_sets` are grouped: one inner list per
    calibration frame. They are
    flattened for the staged fit, so the fitted coefficients and both
    held-out RMS numbers are bit-identical to a flat-list fit — the
    grouping is used only by the jackknife stability statistic. Passing
    everything as one inner list is legitimate and equivalent to the
    historical flat signature, but it makes the jackknife meaningless
    (one group, nothing to leave out)."""
    train_groups = _as_frame_groups(train_sets)
    heldout_groups = _as_frame_groups(heldout_sets)
    flat_train = [s for group in train_groups for s in group]
    if not flat_train:
        raise GeometryFitError(
            Code.GEOMETRY_INSUFFICIENT_FRAMES, "no collinear sets survived detection"
        )
    flat_heldout = [s for group in heldout_groups for s in group]

    K_base = base_camera(frame_width, frame_height)
    x0 = np.array([0.0, 0.0, K_base[0, 2], K_base[1, 2]])

    chosen, params, stage_rms = _staged_fit(flat_train, flat_heldout, K_base, x0)
    rms_before = _rms(
        residuals(np.array([0.0, 0.0, K_base[0, 2], K_base[1, 2]]), flat_heldout, K_base)
    )
    rms_after = stage_rms[chosen]

    relative = 1.0 - rms_after / rms_before if rms_before > 0 else 0.0
    absolute = rms_before - rms_after

    displacement, percent = _corner_displacement(
        params[0], params[1], params[2], params[3], K_base, frame_width, frame_height
    )

    # The stability statistic: leave
    # one calibration frame's sets out, refit the staged fit, repeat over
    # every frame, and take the spread of the corner displacements. Target
    # error is random across frames and averages out; lens distortion is
    # fixed across frames and stays — agreement between subsets separates
    # the two without the lens signal having to dominate one measurement.
    # Fewer than two frames leaves nothing to agree or disagree, so the
    # refits are skipped and the statistic reads infinite.
    thetas: list[float] = []
    if len(train_groups) >= 2:
        for index in range(len(train_groups)):
            remaining = [group for i, group in enumerate(train_groups) if i != index]
            remaining_sets = [s for group in remaining for s in group]
            _, leave_out_params, _ = _staged_fit(
                remaining_sets, flat_heldout, K_base, x0
            )
            thetas.append(
                _corner_displacement(
                    leave_out_params[0],
                    leave_out_params[1],
                    leave_out_params[2],
                    leave_out_params[3],
                    K_base,
                    frame_width,
                    frame_height,
                )[0]
            )
    jk_mean, jk_se, relative_se = jackknife_relative_se(thetas)

    accepted = (
        MAGNITUDE_HARD_MIN_PERCENT <= percent <= MAGNITUDE_HARD_MAX_PERCENT
        and relative_se <= GEOMETRY_MAX_RELATIVE_SE
    )
    rejection_reason: str | None = None
    if not accepted:
        if relative_se > GEOMETRY_MAX_RELATIVE_SE:
            rejection_reason = (
                f"leave-one-out corner-displacement spread is "
                f"{relative_se * 100:.1f}% relative standard error "
                f"({jk_se:.3f} px about a {jk_mean:.3f} px mean over "
                f"{len(train_groups)} frames); the stability gate allows "
                f"<= {GEOMETRY_MAX_RELATIVE_SE * 100:.0f}%. The improvement "
                f"diagnostic read {rms_before:.3f} px -> {rms_after:.3f} px "
                f"held-out ({relative * 100:.1f}% relative, "
                f"{absolute:.3f} px absolute)"
            )
        else:
            rejection_reason = (
                f"corner displacement {percent:.3f}% of the half-diagonal is "
                f"outside the plausible {MAGNITUDE_HARD_MIN_PERCENT}-"
                f"{MAGNITUDE_HARD_MAX_PERCENT}% band"
            )

    suspect = False
    if accepted and not (
        MAGNITUDE_EXPECTED_MIN_PERCENT <= percent <= MAGNITUDE_EXPECTED_MAX_PERCENT
    ):
        # Inside the hard band (which acceptance required) but outside the
        # expected one: applied with a warning, not dropped.
        suspect = True

    return GeometryFitResult(
        k1=float(params[0]),
        k2=float(params[1]),
        cx=float(params[2]),
        cy=float(params[3]),
        stage=chosen,
        heldout_rms_before=rms_before,
        heldout_rms_after=rms_after,
        stage_heldout_rms=dict(stage_rms),
        corner_displacement_px=displacement,
        corner_displacement_percent=percent,
        accepted=accepted,
        rejection_reason=rejection_reason,
        suspect=suspect,
        jackknife_corner_px_mean=jk_mean,
        jackknife_corner_px_se=jk_se,
        jackknife_relative_se=relative_se,
        jackknife_frames=len(train_groups),
    )
