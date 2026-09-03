"""Pairwise registration: feature detection, matching, and the rigid fit.

`DETECTOR`, `RATIO_TEST`, `RANSAC_REPROJ_PX`, `MIN_PAIR_INLIERS`,
`MIN_PAIR_INLIER_RATIO`, `MAX_PAIR_RMS_PX`, `SCALE_DRIFT_WARN`, and
`SCALE_DRIFT_FAIL` are Chunk P2-1's measured constants, approved at user
gate C (section 3.12). Production code reads them from here and from
nowhere else.

`RANSAC_REPROJ_PX` and `MAX_PAIR_RMS_PX` are full-resolution pixels — every
point is converted to full resolution with `detection.to_full_resolution`
before RANSAC, per section 3.12.
"""

from __future__ import annotations

import dataclasses

import cv2
import numpy as np

from scanny_boy.detection import DetectionImage, to_full_resolution
from scanny_boy.events import Code

DETECTOR = "AKAZE"
RATIO_TEST = 0.75
RANSAC_REPROJ_PX = 3.0
MIN_PAIR_INLIERS = 40
MIN_PAIR_INLIER_RATIO = 0.25
MAX_PAIR_RMS_PX = 6.0
SCALE_DRIFT_WARN = 0.005
SCALE_DRIFT_FAIL = 0.01

_RANSAC_MAX_ITERS = 5000
_MIN_INLIERS_FOR_RIGID_FIT = 2


class StitchError(Exception):
    def __init__(self, code: Code, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclasses.dataclass(frozen=True)
class FrameFeatures:
    name: str
    keypoints: tuple  # cv2.KeyPoint
    descriptors: np.ndarray
    scale: float  # from DetectionImage


@dataclasses.dataclass(frozen=True)
class PairResult:
    a: str
    b: str
    transform: np.ndarray  # 2x3 float64, rigid, maps b -> a, FULL-RES px
    good_matches: int
    inliers: int
    inlier_ratio: float
    rms_residual_px: float  # full-resolution pixels
    scale_drift: float  # abs(similarity scale - 1)
    accepted: bool
    reject_code: Code | None
    reject_message: str | None
    # RANSAC inlier correspondences, full-resolution px, (inliers, 2) each,
    # row-aligned: inlier_points_a[i] <-> inlier_points_b[i]. Chunk P2-4's
    # global_rms needs the actual point-level correspondences, not just the
    # summary statistics above.
    inlier_points_a: np.ndarray
    inlier_points_b: np.ndarray
    # Filled in later by composite.py; None here. `overlap_mad` is the
    # post-gain residual (what the MAX_OVERLAP_MAD gate checks);
    # `overlap_mad_pregain` is the same measurement taken before per-frame
    # gain compensation, kept as the diagnostic that explains why a gain
    # was applied.
    overlap_fraction: float | None
    overlap_mad: float | None
    overlap_mad_pregain: float | None
    # docs/STITCH_QUALITY_PLAN.md section 2: the similarity fit (rotation,
    # translation, and one isotropic scale) on the same inliers, from
    # similarity_from_correspondences. layout.py's per-frame scale solve
    # uses these; `transform` and `rms_residual_px` above are unaffected and
    # stay what the acceptance gates measure.
    similarity_transform: np.ndarray  # 2x3, rotation+translation, maps b -> a
    similarity_scale: float


_IDENTITY_TRANSFORM = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
_EMPTY_POINTS = np.zeros((0, 2))


def _make_detector() -> cv2.Feature2D:
    if DETECTOR == "SIFT":
        return cv2.SIFT_create()
    if DETECTOR == "AKAZE":
        return cv2.AKAZE_create(descriptor_type=cv2.AKAZE_DESCRIPTOR_KAZE)
    if DETECTOR == "ORB":
        return cv2.ORB_create()
    raise StitchError(Code.STITCH_FAILED, f"unknown DETECTOR {DETECTOR!r}")


def detect_features(detection: DetectionImage, name: str) -> FrameFeatures:
    """Uses the gate-C DETECTOR. SIFT/AKAZE -> float descriptors,
    ORB -> uint8. A featureless frame normalises to empty (zero-length)
    keypoints and an empty array of the detector's dtype — `register_pair`
    refuses the pair; it must never see `None` descriptors."""
    detector = _make_detector()
    keypoints, descriptors = detector.detectAndCompute(detection.image, None)
    if keypoints is None or len(keypoints) == 0 or descriptors is None:
        return FrameFeatures(
            name=name,
            keypoints=(),
            descriptors=np.empty((0, 0), dtype=_descriptor_dtype(detector)),
            scale=detection.scale,
        )
    return FrameFeatures(
        name=name,
        keypoints=tuple(keypoints),
        descriptors=descriptors,
        scale=detection.scale,
    )


def _descriptor_dtype(detector: cv2.Feature2D) -> type:
    """The descriptor dtype the configured DETECTOR emits: uint8 for ORB,
    float32 for SIFT/AKAZE. Only used for the empty-array fallback, where
    `register_pair`'s norm-type choice must not crash on `None`."""
    return np.uint8 if DETECTOR == "ORB" else np.float32


def undistorter_from_geometry(geometry: dict):
    """Build the point undistorter `register_pair` consumes from a profile's
    section 3.2 geometry object. The coefficients are already in the OpenCV
    forward convention, so this is a direct `cv2.undistortPoints` closure —
    full-resolution pixel points in, undistorted full-resolution pixel
    points out."""
    K = np.array(
        [
            [geometry["fx"], 0.0, geometry["cx"]],
            [0.0, geometry["fy"], geometry["cy"]],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    D = np.array([geometry["k1"], geometry["k2"], 0.0, 0.0, 0.0])

    def undistort(points: np.ndarray) -> np.ndarray:
        undistorted = cv2.undistortPoints(
            points.reshape(-1, 1, 2).astype(np.float32), K, D, P=K
        )
        return undistorted.reshape(-1, 2).astype(np.float64)

    return undistort


def rigid_from_correspondences(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Closed-form Umeyama with scale forced to exactly 1. Returns 2x3."""
    mu_s, mu_d = src.mean(0), dst.mean(0)
    covariance = ((dst - mu_d).T @ (src - mu_s)) / len(src)
    u, _, vt = np.linalg.svd(covariance)
    d = np.diag([1.0, np.sign(np.linalg.det(u @ vt))])
    rotation = u @ d @ vt
    translation = mu_d - rotation @ mu_s
    return np.hstack([rotation, translation.reshape(2, 1)])


def similarity_from_correspondences(
    src: np.ndarray, dst: np.ndarray
) -> tuple[np.ndarray, float]:
    """Closed-form Umeyama *with* scale, from the same SVD as the rigid
    fit. Returns (2x3 [R|t], scale). The rigid fit stays the one the
    acceptance gates measure against, so no gate constant changes meaning
    when this is added."""
    mu_s, mu_d = src.mean(0), dst.mean(0)
    covariance = ((dst - mu_d).T @ (src - mu_s)) / len(src)
    u, singular_values, vt = np.linalg.svd(covariance)
    d = np.diag([1.0, np.sign(np.linalg.det(u @ vt))])
    rotation = u @ d @ vt
    variance_src = float(np.mean(np.sum((src - mu_s) ** 2, axis=1)))
    scale = float(np.trace(np.diag(singular_values) @ d) / variance_src)
    translation = mu_d - scale * rotation @ mu_s
    return np.hstack([rotation, translation.reshape(2, 1)]), scale


def _rms_residual(
    transform: np.ndarray, src: np.ndarray, dst: np.ndarray
) -> float:
    rotation = transform[:, :2]
    translation = transform[:, 2]
    projected = src @ rotation.T + translation
    residuals = projected - dst
    return float(np.sqrt(np.mean(np.sum(residuals**2, axis=1))))


def register_pair(
    a: FrameFeatures, b: FrameFeatures, undistorter=None
) -> PairResult:
    """1. BFMatcher — NORM_HAMMING for uint8 descriptors, NORM_L2 otherwise.
    2. knnMatch(k=2) plus Lowe ratio test at RATIO_TEST.
    3. Convert both point sets to full resolution with
       detection.to_full_resolution BEFORE anything else, so every number
       from here on is in full-resolution pixels — including
       RANSAC_REPROJ_PX.
    4. cv2.estimateAffinePartial2D(..., method=cv2.RANSAC,
       ransacReprojThreshold=RANSAC_REPROJ_PX, maxIters=5000) for the
       inlier mask and the similarity scale.
    5. rigid_from_correspondences on the inliers only. This, not the
       similarity, is the returned transform.
    6. Apply the section 3.4 gates and set accepted/reject_code.

    `undistorter`, when given (docs/GEOMETRIC_PLAN.md section 5.3), pushes
    both point sets through `cv2.undistortPoints` with the profile's fitted
    coefficients after step 3 — everything downstream (RANSAC, the rigid
    fit, rms_residual_px, scale_drift) then works in undistorted
    full-resolution pixels, and no threshold's units change. The transform
    used is always re-fitted rigidly; the undistorter never changes that.
    """
    norm_type = (
        cv2.NORM_HAMMING if a.descriptors.dtype == np.uint8 else cv2.NORM_L2
    )
    matcher = cv2.BFMatcher(norm_type)

    if len(a.descriptors) == 0 or len(b.descriptors) == 0:
        # A blank or near-black intermediate finds no keypoints at all: an
        # ordinary scanning outcome, refused as insufficient matches rather
        # than crashing (and eligible for the CLAHE retry).
        return PairResult(
            a=a.name,
            b=b.name,
            transform=_IDENTITY_TRANSFORM,
            good_matches=0,
            inliers=0,
            inlier_ratio=0.0,
            rms_residual_px=float("inf"),
            scale_drift=float("inf"),
            accepted=False,
            reject_code=Code.STITCH_INSUFFICIENT_MATCHES,
            reject_message=(
                f"no features detected in {a.name} or {b.name}"
            ),
            inlier_points_a=_EMPTY_POINTS,
            inlier_points_b=_EMPTY_POINTS,
            overlap_fraction=None,
            overlap_mad=None,
            overlap_mad_pregain=None,
        )

    raw_matches = matcher.knnMatch(a.descriptors, b.descriptors, k=2)

    good = [
        m
        for pair in raw_matches
        if len(pair) == 2
        for m, n in [pair]
        if m.distance < RATIO_TEST * n.distance
    ]
    good_matches = len(good)

    pts_a_detect = np.array(
        [a.keypoints[m.queryIdx].pt for m in good], dtype=np.float64
    ).reshape(-1, 2)
    pts_b_detect = np.array(
        [b.keypoints[m.trainIdx].pt for m in good], dtype=np.float64
    ).reshape(-1, 2)

    pts_a_full = to_full_resolution(pts_a_detect, a.scale)
    pts_b_full = to_full_resolution(pts_b_detect, b.scale)
    if undistorter is not None:
        pts_a_full = undistorter(pts_a_full)
        pts_b_full = undistorter(pts_b_full)

    matrix = None
    inlier_mask = None
    if good_matches > 0:
        matrix, inlier_mask = cv2.estimateAffinePartial2D(
            pts_b_full,
            pts_a_full,
            method=cv2.RANSAC,
            ransacReprojThreshold=RANSAC_REPROJ_PX,
            maxIters=_RANSAC_MAX_ITERS,
        )

    if matrix is None:
        return PairResult(
            a=a.name,
            b=b.name,
            transform=_IDENTITY_TRANSFORM,
            good_matches=good_matches,
            inliers=0,
            inlier_ratio=0.0,
            rms_residual_px=float("inf"),
            scale_drift=float("inf"),
            accepted=False,
            reject_code=Code.STITCH_INSUFFICIENT_MATCHES,
            reject_message=(
                f"too few good matches to fit a transform ({good_matches})"
            ),
            inlier_points_a=_EMPTY_POINTS,
            inlier_points_b=_EMPTY_POINTS,
            overlap_fraction=None,
            overlap_mad=None,
            overlap_mad_pregain=None,
            similarity_transform=_IDENTITY_TRANSFORM,
            similarity_scale=1.0,
        )

    inlier_bool = inlier_mask.ravel().astype(bool)
    inliers = int(inlier_bool.sum())
    inlier_ratio = inliers / good_matches

    scale_from_similarity = float(
        np.hypot(matrix[0, 0], matrix[1, 0])
    )
    scale_drift = abs(scale_from_similarity - 1.0)

    src_inliers = pts_b_full[inlier_bool]
    dst_inliers = pts_a_full[inlier_bool]

    if inliers >= _MIN_INLIERS_FOR_RIGID_FIT:
        transform = rigid_from_correspondences(src_inliers, dst_inliers)
        rms_residual_px = _rms_residual(transform, src_inliers, dst_inliers)
        similarity_transform, similarity_scale = similarity_from_correspondences(
            src_inliers, dst_inliers
        )
    else:
        transform = _IDENTITY_TRANSFORM
        rms_residual_px = float("inf")
        similarity_transform = _IDENTITY_TRANSFORM
        similarity_scale = 1.0

    if inliers < MIN_PAIR_INLIERS or inlier_ratio < MIN_PAIR_INLIER_RATIO:
        return PairResult(
            a=a.name,
            b=b.name,
            transform=transform,
            good_matches=good_matches,
            inliers=inliers,
            inlier_ratio=inlier_ratio,
            rms_residual_px=rms_residual_px,
            scale_drift=scale_drift,
            accepted=False,
            reject_code=Code.STITCH_INSUFFICIENT_MATCHES,
            reject_message=(
                f"only {inliers} inliers at ratio {inlier_ratio:.3f} "
                f"(need >= {MIN_PAIR_INLIERS} at ratio "
                f">= {MIN_PAIR_INLIER_RATIO})"
            ),
            inlier_points_a=dst_inliers,
            inlier_points_b=src_inliers,
            overlap_fraction=None,
            overlap_mad=None,
            overlap_mad_pregain=None,
            similarity_transform=similarity_transform,
            similarity_scale=similarity_scale,
        )

    if scale_drift > SCALE_DRIFT_FAIL:
        return PairResult(
            a=a.name,
            b=b.name,
            transform=transform,
            good_matches=good_matches,
            inliers=inliers,
            inlier_ratio=inlier_ratio,
            rms_residual_px=rms_residual_px,
            scale_drift=scale_drift,
            accepted=False,
            reject_code=Code.STITCH_RESIDUAL_TOO_HIGH,
            reject_message=(
                f"scale drift {scale_drift:.4f} exceeds {SCALE_DRIFT_FAIL}"
            ),
            inlier_points_a=dst_inliers,
            inlier_points_b=src_inliers,
            overlap_fraction=None,
            overlap_mad=None,
            overlap_mad_pregain=None,
            similarity_transform=similarity_transform,
            similarity_scale=similarity_scale,
        )

    if rms_residual_px > MAX_PAIR_RMS_PX:
        return PairResult(
            a=a.name,
            b=b.name,
            transform=transform,
            good_matches=good_matches,
            inliers=inliers,
            inlier_ratio=inlier_ratio,
            rms_residual_px=rms_residual_px,
            scale_drift=scale_drift,
            accepted=False,
            reject_code=Code.STITCH_RESIDUAL_TOO_HIGH,
            reject_message=(
                f"rms residual {rms_residual_px:.2f}px exceeds "
                f"{MAX_PAIR_RMS_PX}px"
            ),
            inlier_points_a=dst_inliers,
            inlier_points_b=src_inliers,
            overlap_fraction=None,
            overlap_mad=None,
            overlap_mad_pregain=None,
            similarity_transform=similarity_transform,
            similarity_scale=similarity_scale,
        )

    return PairResult(
        a=a.name,
        b=b.name,
        transform=transform,
        good_matches=good_matches,
        inliers=inliers,
        inlier_ratio=inlier_ratio,
        rms_residual_px=rms_residual_px,
        scale_drift=scale_drift,
        accepted=True,
        reject_code=None,
        reject_message=None,
        inlier_points_a=dst_inliers,
        inlier_points_b=src_inliers,
        overlap_fraction=None,
        overlap_mad=None,
        overlap_mad_pregain=None,
        similarity_transform=similarity_transform,
        similarity_scale=similarity_scale,
    )
