"""The staged plumb-line distortion fit (docs/GEOMETRIC_PLAN.md section 4.4).

The objective is straightness: every ChArUco corner carries the row, column,
or diagonal family its id names, and after undistortion each family's points
must be collinear. The residual is each point's perpendicular distance to
its own family's best-fit line — `cv2.undistortPoints` inverts the forward
model iteratively, which is slower per evaluation and exactly the point:
the fitted coefficients are already in the OpenCV forward convention every
consumer (`initUndistortRectifyMap`, the closed-form band map of composite)
wants, with no conversion.

Gauge convention (section 1.1): `K_new = K` and the undistorted frame has
exactly the source frame's pixel dimensions. `K` itself is fixed per
section 1.2 — straightness is scale-invariant, so `K` only sets the
normalisation of `r`, and holding it fixed keeps coefficients comparable
across sessions.

Every constant in this module is defined here and nowhere else.
"""

from __future__ import annotations

import dataclasses

import cv2
import numpy as np
from scipy.optimize import least_squares

from scanny_boy.events import Code

# Take an earlier stage unless the next stage beats its held-out RMS by at
# least this relative fraction (section 4.4).
STAGE_IMPROVEMENT_FRACTION = 0.05
# Acceptance gates (section 4.5): both must hold.
GEOMETRY_MIN_IMPROVEMENT_FRACTION = 0.30
GEOMETRY_MIN_IMPROVEMENT_PX = 0.3
# Magnitude sanity band (section 4.5), as a percentage of the half-diagonal.
MAGNITUDE_HARD_MIN_PERCENT = 0.01
MAGNITUDE_HARD_MAX_PERCENT = 1.0
MAGNITUDE_EXPECTED_MIN_PERCENT = 0.03
MAGNITUDE_EXPECTED_MAX_PERCENT = 0.2


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
    result, not an exception (section 4.5: the profile is still created)."""

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


def base_camera(frame_width: int, frame_height: int) -> np.ndarray:
    """The fixed camera matrix of section 1.2: `fx = fy = max(w, h)`,
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
        u = cv2.undistortPoints(pts, K, D, P=K).reshape(-1, 2)
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
    and as a percentage of the half-diagonal (section 4.5)."""
    corner = np.array([[0.0, 0.0]])
    distorted = forward_distort(corner, k1, k2, cx, cy, K_base)
    displacement = float(np.hypot(*(distorted[0] - corner[0])))
    half_diagonal = float(np.hypot(frame_width, frame_height) / 2)
    return displacement, displacement / half_diagonal * 100.0


def fit_geometry(
    train_sets: list[np.ndarray],
    heldout_sets: list[np.ndarray],
    frame_width: int,
    frame_height: int,
) -> GeometryFitResult:
    """The staged fit, held-out evaluation, and acceptance gates of sections
    4.4 and 4.5, in one call. Never raises for a rejected fit — the result
    carries `accepted=False` and the reason; only degenerate inputs (no
    training sets) raise."""
    if not train_sets:
        raise GeometryFitError(
            Code.GEOMETRY_INSUFFICIENT_FRAMES, "no collinear sets survived detection"
        )

    K_base = base_camera(frame_width, frame_height)
    x0 = np.array([0.0, 0.0, K_base[0, 2], K_base[1, 2]])

    stage_rms: dict[str, float] = {}
    stage_params: dict[str, np.ndarray] = {}
    for stage in ("k1", "k1k2", "k1k2c"):
        params = _fit_stage(stage, train_sets, K_base, x0)
        stage_params[stage] = params
        stage_rms[stage] = _rms(residuals(params, heldout_sets, K_base))

    # The earliest stage the next does not beat by
    # STAGE_IMPROVEMENT_FRACTION relative. On a lens this clean, expect
    # stage 1 to win.
    chosen = "k1"
    for stage, nxt in (("k1", "k1k2"), ("k1k2", "k1k2c")):
        if stage_rms[nxt] < stage_rms[stage] * (1.0 - STAGE_IMPROVEMENT_FRACTION):
            chosen = nxt
        else:
            break

    params = stage_params[chosen]
    rms_before = _rms(residuals(np.array([0.0, 0.0, K_base[0, 2], K_base[1, 2]]), heldout_sets, K_base))
    rms_after = stage_rms[chosen]

    relative = 1.0 - rms_after / rms_before if rms_before > 0 else 0.0
    absolute = rms_before - rms_after

    displacement, percent = _corner_displacement(
        params[0], params[1], params[2], params[3], K_base, frame_width, frame_height
    )

    accepted = (
        relative >= GEOMETRY_MIN_IMPROVEMENT_FRACTION
        and absolute >= GEOMETRY_MIN_IMPROVEMENT_PX
    )
    rejection_reason: str | None = None
    if not accepted:
        rejection_reason = (
            f"held-out RMS improved {rms_before:.3f}px -> {rms_after:.3f}px "
            f"({relative * 100:.1f}% relative, {absolute:.3f}px absolute); "
            f"the gates need >= {GEOMETRY_MIN_IMPROVEMENT_FRACTION * 100:.0f}% "
            f"relative and >= {GEOMETRY_MIN_IMPROVEMENT_PX}px absolute"
        )

    suspect = False
    if accepted and not (
        MAGNITUDE_EXPECTED_MIN_PERCENT <= percent <= MAGNITUDE_EXPECTED_MAX_PERCENT
    ):
        if MAGNITUDE_HARD_MIN_PERCENT <= percent <= MAGNITUDE_HARD_MAX_PERCENT:
            suspect = True
        else:
            accepted = False
            rejection_reason = (
                f"corner displacement {percent:.3f}% of the half-diagonal is "
                f"outside the plausible {MAGNITUDE_HARD_MIN_PERCENT}-"
                f"{MAGNITUDE_HARD_MAX_PERCENT}% band"
            )

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
    )
