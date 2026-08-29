#!/usr/bin/env python3
"""Measure everything Chunk P2-1 has to decide, on the real gate-B scans.

`docs/PHASE2_IMPLEMENTATION_PLAN.md` Chunk P2-1 requires seven tables of
measurements plus a ROMM gate, from which the user approves the constants in
section 3.12 at gate C. This script produces them as GitHub-flavoured Markdown
on stdout, so appendix B can be regenerated rather than remembered.

**This script deliberately duplicates logic that Chunks P2-2 through P2-5 will
put under `cli/src/scanny_boy/`.** P2-1 is forbidden from writing production
code, and the modules it would import do not exist yet. Every algorithm here is
written to match the plan's specification of those modules — the colour
transfer of section 2.3.1, the detection image of P2-2, the rigid re-fit of
P2-3, the two-step layout solve of P2-4, the feather blend of P2-5 — so the
measurements predict what the real pipeline will do. Once those modules exist
this script is something to check them against, not a second implementation to
keep in step.

`MEASURE_*` below are the settings these measurements were taken *at*. They are
not proposals for section 3.12; the proposals come out of the tables, in the
pull request, for the user to approve at gate C.

Usage, from the repository root:

    uv run --project cli scripts/measure-registration.py
    uv run --project cli scripts/measure-registration.py --out /tmp/p2-1
    uv run --project cli scripts/measure-registration.py --tables 1,5
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import os
import resource
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import cv2
import numpy as np
import rawpy
import tifffile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cli" / "src"))

# Imported after the path insert above, so this runs from a checkout without
# the package installed, exactly as `measure-concurrency.py` does it.
from scanny_boy.pipeline import run_convert
from scanny_boy.raw_decode import RAW_PARAMS

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_NEF_DIR = ROOT / "tests" / "fixtures" / "nef"
FILM_DATE = datetime.date(2026, 8, 29)
MIB = 1024 * 1024

# The gate-B negatives (plan section 5, user gate B). The last one is the
# negative that is *supposed* to fail: without it nothing here proves the gates
# ever refuse anything.
GOOD_NEGATIVES = ["normal", "wonky", "order", "tight"]
BAD_NEGATIVES = ["mismatch"]
NEGATIVES = GOOD_NEGATIVES + BAD_NEGATIVES
FRAMES_PER_NEGATIVE = 3

# Which pairs of each negative genuinely share film, recorded in appendix C.
#
# Gate B labels whole negatives, but a three-frame strip has three pairs and
# only two of them are adjacent unless the strip was shot out of spatial order.
# So `normal` and `tight` each contain one end-to-end pair that overlaps
# nothing — and those pairs are the bad-side evidence `MAX_OVERLAP_MAD` needs,
# which labelling by negative alone would have thrown away.
#
# Established by consensus of all three detectors rather than assumed: an
# overlapping pair here yields 198–7381 AKAZE inliers at 1.4–1.8 px RMS, and a
# non-overlapping one yields 0–3 inliers at 64–1245 px, with no case in
# between. Appendix C records the per-pair numbers.
OVERLAPPING_PAIRS = {
    "normal": {(1, 2), (2, 3)},
    "wonky": {(1, 2), (2, 3), (1, 3)},
    "order": {(1, 2), (2, 3), (1, 3)},
    "tight": {(1, 3), (2, 3)},
    "mismatch": set(),
}

# Settings these measurements were taken at — not proposals for section 3.12.
MEASURE_LONG_EDGE = 2000
MEASURE_CLAHE = False
MEASURE_RATIO = 0.75  # Lowe's original value, so table 1 starts somewhere neutral
MEASURE_RANSAC_PX = 3.0  # full-resolution pixels, per section 3.12's note
MEASURE_INTERPOLATION = cv2.INTER_LANCZOS4

# Provisional rejection used only so a layout can be solved at all, since a
# garbage pair would otherwise corrupt the global fit. Not proposals either:
# they sit in the two-orders-of-magnitude gap table 1 measures between an
# overlapping pair (>=198 inliers, <=1.8 px) and a non-overlapping one
# (<=3 inliers, >=64 px), so anything in that gap gives the same layouts.
MEASURE_MIN_INLIERS = 20
MEASURE_MAX_RMS_PX = 10.0

# Section 2.3.1: LibRaw's curve, which is what rawpy's gamma=(1.8, 16) writes.
# Not ROMM's — see that amendment for the measurement which settled it.
ROMM_GAMMA = 1.8
ROMM_SLOPE = 16.0
ENCODED_BREAKPOINT = 0.008454220179
LINEAR_BREAKPOINT = 0.000528388761
CURVE_OFFSET = 0.006763376143
MAX_CODE = 65535

MASK_ERODE_PX = 5  # section 3.3: Lanczos4 support radius 4, plus one

# `ru_maxrss` is bytes on macOS and kilobytes on Linux.
_RSS_SCALE = 1 if sys.platform == "darwin" else 1024


def _build_decode_lut() -> np.ndarray:
    e = np.arange(MAX_CODE + 1, dtype=np.float64) / MAX_CODE
    return np.where(
        e < ENCODED_BREAKPOINT,
        e / ROMM_SLOPE,
        np.power((e + CURVE_OFFSET) / (1.0 + CURVE_OFFSET), ROMM_GAMMA),
    ).astype(np.float32)


DECODE_LUT = _build_decode_lut()


def decode_to_linear(image: np.ndarray) -> np.ndarray:
    return DECODE_LUT[image]


def encode_from_linear(image: np.ndarray) -> np.ndarray:
    """float32 linear -> uint16, in row bands so a canvas-sized array does not
    need three canvas-sized temporaries (section 3.8's `result` term).
    """
    out = np.empty(image.shape, dtype=np.uint16)
    per_row = max(1, int(np.prod(image.shape[1:])))
    band = max(1, 8_000_000 // per_row)
    for y in range(0, image.shape[0], band):
        block = np.clip(image[y : y + band], 0.0, 1.0).astype(np.float32)
        high = np.power(block, np.float32(1.0 / ROMM_GAMMA))
        high *= np.float32(1.0 + CURVE_OFFSET)
        high -= np.float32(CURVE_OFFSET)
        low = block * np.float32(ROMM_SLOPE)
        encoded = np.where(block < np.float32(LINEAR_BREAKPOINT), low, high)
        np.clip(encoded, 0.0, 1.0, out=encoded)
        out[y : y + band] = np.rint(encoded * np.float32(MAX_CODE)).astype(np.uint16)
    return out


# --------------------------------------------------------------------------
# Detection images (Chunk P2-2's detection.py, per its specification)
# --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class DetectionImage:
    image: np.ndarray
    scale: float  # detection px * scale = full-res px
    source_size: tuple[int, int]  # (height, width)


def build_detection_image(frame: np.ndarray, *, long_edge: int, clahe: bool) -> DetectionImage:
    linear = decode_to_linear(frame)
    lum = linear[:, :, 0] * 0.2126 + linear[:, :, 1] * 0.7152 + linear[:, :, 2] * 0.0722
    height, width = lum.shape
    factor = min(1.0, long_edge / max(height, width))
    if factor < 1.0:
        target = (round(width * factor), round(height * factor))
        small = cv2.resize(lum, target, interpolation=cv2.INTER_AREA)
    else:
        small = lum
    scale = width / small.shape[1]
    lo, hi = np.percentile(small, [0.5, 99.5])
    stretched = np.clip((small - lo) / max(float(hi - lo), 1e-12), 0.0, 1.0)
    image = (stretched * 255.0).round().astype(np.uint8)
    if clahe:
        image = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(image)
    return DetectionImage(image=image, scale=scale, source_size=(height, width))


def to_full_resolution(points, scale: float) -> np.ndarray:
    return np.asarray(points, dtype=np.float64) * scale


# --------------------------------------------------------------------------
# Pairwise registration (Chunk P2-3's registration.py, per its specification)
# --------------------------------------------------------------------------

DETECTOR_FACTORIES = {
    "SIFT": cv2.SIFT_create,
    # Default nfeatures (500). The plan names `ORB_create` with no parameters,
    # and inventing one is what section 5.1 forbids; that cap is why ORB's
    # keypoint counts in table 1 are so much lower than the others'.
    "ORB": cv2.ORB_create,
    "AKAZE": cv2.AKAZE_create,
}


@dataclasses.dataclass
class PairMeasurement:
    negative: str
    a: str
    b: str
    detector: str
    keypoints_a: int = 0
    keypoints_b: int = 0
    good_matches: int = 0
    inliers: int = 0
    inlier_ratio: float = 0.0
    rigid_rms_px: float = float("nan")
    scale: float = float("nan")
    scale_drift: float = float("nan")
    detect_seconds: float = 0.0
    match_seconds: float = 0.0
    transform: np.ndarray | None = None
    src_inliers: np.ndarray | None = None
    dst_inliers: np.ndarray | None = None
    overlap_fraction: float | None = None
    overlap_mad: float | None = None
    note: str = ""

    @property
    def seconds(self) -> float:
        return self.detect_seconds + self.match_seconds


def rigid_from_correspondences(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Closed-form Umeyama with scale forced to exactly 1 (plan P2-3)."""
    mu_s, mu_d = src.mean(0), dst.mean(0)
    cov = ((dst - mu_d).T @ (src - mu_s)) / len(src)
    u, _, vt = np.linalg.svd(cov)
    d = np.diag([1.0, np.sign(np.linalg.det(u @ vt))])
    rotation = u @ d @ vt
    translation = mu_d - rotation @ mu_s
    return np.hstack([rotation, translation.reshape(2, 1)])


def _rms(transform: np.ndarray, src: np.ndarray, dst: np.ndarray) -> float:
    predicted = (transform[:, :2] @ src.T).T + transform[:, 2]
    return float(np.sqrt(np.mean(np.sum((predicted - dst) ** 2, axis=1))))


def detect_features(detection: DetectionImage, detector: str):
    return DETECTOR_FACTORIES[detector]().detectAndCompute(detection.image, None)


def register_pair(
    negative: str,
    name_a: str,
    features_a,
    name_b: str,
    features_b,
    *,
    detector: str,
    scale: float,
    ratio: float = MEASURE_RATIO,
    ransac_px: float = MEASURE_RANSAC_PX,
    detect_seconds: float = 0.0,
) -> PairMeasurement:
    (kp_a, desc_a), (kp_b, desc_b) = features_a, features_b
    result = PairMeasurement(
        negative=negative, a=name_a, b=name_b, detector=detector,
        keypoints_a=len(kp_a), keypoints_b=len(kp_b), detect_seconds=detect_seconds,
    )
    if desc_a is None or desc_b is None or len(kp_a) < 2 or len(kp_b) < 2:
        result.note = "no descriptors"
        return result

    started = time.monotonic()
    norm = cv2.NORM_HAMMING if desc_a.dtype == np.uint8 else cv2.NORM_L2
    knn = cv2.BFMatcher(norm).knnMatch(desc_a, desc_b, k=2)
    good = [m for m, second in (p for p in knn if len(p) == 2) if m.distance < ratio * second.distance]
    result.good_matches = len(good)
    if len(good) < 3:
        result.note = "too few good matches"
        result.match_seconds = time.monotonic() - started
        return result

    # Convert to full resolution BEFORE RANSAC, so every number downstream —
    # including the RANSAC threshold — is in full-resolution pixels (P2-3 step 3).
    src = to_full_resolution([kp_b[m.trainIdx].pt for m in good], scale)
    dst = to_full_resolution([kp_a[m.queryIdx].pt for m in good], scale)

    similarity, mask = cv2.estimateAffinePartial2D(
        src, dst, method=cv2.RANSAC, ransacReprojThreshold=ransac_px, maxIters=5000
    )
    if similarity is None or mask is None or int(mask.sum()) < 3:
        result.note = "RANSAC found no model"
        result.match_seconds = time.monotonic() - started
        return result

    keep = mask.ravel().astype(bool)
    src_in, dst_in = src[keep], dst[keep]
    fitted_scale = float(np.hypot(similarity[0, 0], similarity[1, 0]))

    result.inliers = int(keep.sum())
    result.inlier_ratio = result.inliers / len(good)
    result.scale = fitted_scale
    result.scale_drift = abs(fitted_scale - 1.0)
    # The rigid re-fit, not the similarity, is the transform that gets used.
    result.transform = rigid_from_correspondences(src_in, dst_in)
    result.rigid_rms_px = _rms(result.transform, src_in, dst_in)
    result.src_inliers = src_in
    result.dst_inliers = dst_in
    result.match_seconds = time.monotonic() - started
    return result


# --------------------------------------------------------------------------
# Global layout (Chunk P2-4's layout.py, per its specification)
# --------------------------------------------------------------------------


@dataclasses.dataclass
class Layout:
    names: list[str]
    rotations_deg: list[float]
    translations: list[tuple[float, float]]
    canvas_size: tuple[int, int]
    global_rms_px: float
    strip_spread_ratio: float

    def matrix(self, index: int) -> np.ndarray:
        theta = np.deg2rad(self.rotations_deg[index])
        rotation = np.array(
            [[np.cos(theta), -np.sin(theta)], [np.sin(theta), np.cos(theta)]]
        )
        translation = np.array(self.translations[index], dtype=np.float64).reshape(2, 1)
        return np.hstack([rotation, translation])


def _rotation(theta: float) -> np.ndarray:
    return np.array([[np.cos(theta), -np.sin(theta)], [np.sin(theta), np.cos(theta)]])


def connected_component(names: list[str], pairs: list[PairMeasurement]) -> set[str]:
    parent = {n: n for n in names}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for pair in pairs:
        ra, rb = find(pair.a), find(pair.b)
        if ra != rb:
            parent[ra] = rb
    root = find(names[0])
    return {n for n in names if find(n) == root}


def solve_layout(
    names: list[str], frame_size: tuple[int, int], pairs: list[PairMeasurement]
) -> Layout:
    """Plan P2-4: two linear least-squares problems — no SciPy, no bundle
    adjustment. Frame i maps p to R(theta_i) p + t_i; a pair (a, b) asserts
    p_a = R(phi_ab) p_b + u_ab, which gives theta_b = theta_a + phi_ab and
    t_b = t_a + R(theta_a) u_ab.
    """
    index = {name: i for i, name in enumerate(names)}
    count = len(names)

    phis = []
    for pair in pairs:
        phi = float(np.arctan2(pair.transform[1, 0], pair.transform[0, 0]))
        if abs(np.rad2deg(phi)) >= 45.0:
            raise ValueError(
                f"pair rotation {np.rad2deg(phi):.2f} deg exceeds 45 deg "
                f"({pair.a} -> {pair.b}): an upstream bug, not a case to handle"
            )
        phis.append(phi)

    # Step 1 — rotations, one scalar least-squares problem.
    rot_rows = [[0.0] * count for _ in pairs] + [[0.0] * count]
    rot_rhs = list(phis) + [0.0]
    for row, pair in zip(rot_rows, pairs):
        row[index[pair.a]] = -1.0
        row[index[pair.b]] = 1.0
    rot_rows[-1][0] = 1.0
    thetas = np.linalg.lstsq(np.array(rot_rows), np.array(rot_rhs), rcond=None)[0]

    # Step 2 — translations, linear once theta is known.
    rows: list[list[float]] = []
    rhs: list[float] = []
    for pair in pairs:
        ia, ib = index[pair.a], index[pair.b]
        offset = _rotation(thetas[ia]) @ pair.transform[:, 2]
        for axis in (0, 1):
            row = [0.0] * (2 * count)
            row[2 * ia + axis] = -1.0
            row[2 * ib + axis] = 1.0
            rows.append(row)
            rhs.append(float(offset[axis]))
    for axis in (0, 1):
        row = [0.0] * (2 * count)
        row[axis] = 1.0
        rows.append(row)
        rhs.append(0.0)
    flat = np.linalg.lstsq(np.array(rows), np.array(rhs), rcond=None)[0]
    translations = [(float(flat[2 * i]), float(flat[2 * i + 1])) for i in range(count)]

    # Canvas: union bounding box of every frame's four transformed corners.
    height, width = frame_size
    corners = np.array(
        [[0.0, 0.0], [width, 0.0], [width, height], [0.0, height]], dtype=np.float64
    )
    placed = np.vstack([
        (_rotation(thetas[i]) @ corners.T).T + np.array(translations[i])
        for i in range(count)
    ])
    min_xy, max_xy = placed.min(0), placed.max(0)
    translations = [(t[0] - min_xy[0], t[1] - min_xy[1]) for t in translations]
    canvas = (int(np.ceil(max_xy[0] - min_xy[0])), int(np.ceil(max_xy[1] - min_xy[1])))

    layout = Layout(
        names=list(names),
        rotations_deg=[float(np.rad2deg(t)) for t in thetas],
        translations=translations,
        canvas_size=canvas,
        global_rms_px=float("nan"),
        strip_spread_ratio=float("nan"),
    )
    layout.global_rms_px = global_rms(layout, pairs)
    layout.strip_spread_ratio = strip_spread_ratio(layout, frame_size)
    return layout


def global_rms(layout: Layout, pairs: list[PairMeasurement]) -> float:
    index = {name: i for i, name in enumerate(layout.names)}
    squared: list[float] = []
    for pair in pairs:
        ma, mb = layout.matrix(index[pair.a]), layout.matrix(index[pair.b])
        # The same physical point, routed into canvas space through each frame.
        via_a = (ma[:, :2] @ pair.dst_inliers.T).T + ma[:, 2]
        via_b = (mb[:, :2] @ pair.src_inliers.T).T + mb[:, 2]
        squared.extend(np.sum((via_a - via_b) ** 2, axis=1).tolist())
    return float(np.sqrt(np.mean(squared))) if squared else float("nan")


def strip_spread_ratio(layout: Layout, frame_size: tuple[int, int]) -> float:
    height, width = frame_size
    centre = np.array([width / 2.0, height / 2.0])
    centres = np.array([
        layout.matrix(i)[:, :2] @ centre + layout.matrix(i)[:, 2]
        for i in range(len(layout.names))
    ])
    singular = np.linalg.svd(centres - centres.mean(0), compute_uv=False)
    return float(singular[1] / singular[0]) if singular[0] > 0 else 0.0


# --------------------------------------------------------------------------
# Compositing (Chunk P2-5's composite.py, per its specification)
# --------------------------------------------------------------------------


def _feather_weight(mask: np.ndarray) -> np.ndarray:
    kernel = np.ones((MASK_ERODE_PX * 2 + 1, MASK_ERODE_PX * 2 + 1), np.uint8)
    return cv2.distanceTransform(cv2.erode(mask, kernel), cv2.DIST_L2, 5)


def warp_into_own_box(
    frame: np.ndarray, matrix: np.ndarray, interpolation: int, canvas_size: tuple[int, int]
) -> tuple[np.ndarray, np.ndarray, tuple[int, int], float]:
    """Warp into the frame's OWN bounding box (section 3.3), returning
    (linear rgb, feather weight, (offset_x, offset_y), min value before clamp).

    The box is clamped into the canvas. A frame whose placed corner lands on
    the canvas origin can compute a minimum of -1e-13, and `floor` turns that
    into -1: an offset one pixel outside the canvas, which silently produces an
    empty destination slice rather than an error. The clamp costs a sub-pixel
    sliver that the 5-pixel erosion discards anyway.
    """
    canvas_w, canvas_h = canvas_size
    height, width = frame.shape[:2]
    corners = np.array(
        [[0.0, 0.0], [width, 0.0], [width, height], [0.0, height]], dtype=np.float64
    )
    placed = (matrix[:, :2] @ corners.T).T + matrix[:, 2]
    min_xy = np.floor(placed.min(0)).astype(int)
    max_xy = np.ceil(placed.max(0)).astype(int)
    min_xy = np.maximum(min_xy, [0, 0])
    max_xy = np.minimum(max_xy, [canvas_w, canvas_h])
    box = (int(max_xy[0] - min_xy[0]), int(max_xy[1] - min_xy[1]))

    shifted = matrix.copy()
    shifted[0, 2] -= min_xy[0]
    shifted[1, 2] -= min_xy[1]

    warped = cv2.warpAffine(
        decode_to_linear(frame), shifted, box,
        flags=interpolation, borderMode=cv2.BORDER_CONSTANT, borderValue=0,
    )
    minimum = float(warped.min())
    # Section 2.3: INTER_LANCZOS4 undershoots below zero (measured -0.088).
    np.clip(warped, 0.0, None, out=warped)

    mask = cv2.warpAffine(
        np.ones((height, width), dtype=np.uint8), shifted, box,
        flags=cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT, borderValue=0,
    )
    return warped, _feather_weight(mask), (int(min_xy[0]), int(min_xy[1])), minimum


def pairwise_overlap(
    frame_a: np.ndarray, frame_b: np.ndarray, transform: np.ndarray, interpolation: int
) -> tuple[float, float | None, float]:
    """`overlap_fraction`, `overlap_mad`, and the minimum value before clamping.

    Frame b is warped into frame a's own coordinate system, which is defined
    for any pair whether or not a global layout exists — so the deliberately
    bad negative still yields a number. Section 3.4's definition otherwise:
    mean absolute difference in linear light over the shared valid area,
    normalised by the mean level there.
    """
    height, width = frame_a.shape[:2]
    linear_a = decode_to_linear(frame_a)
    warped_b = cv2.warpAffine(
        decode_to_linear(frame_b), transform, (width, height),
        flags=interpolation, borderMode=cv2.BORDER_CONSTANT, borderValue=0,
    )
    minimum = float(warped_b.min())
    np.clip(warped_b, 0.0, None, out=warped_b)

    mask_b = cv2.warpAffine(
        np.ones(frame_b.shape[:2], dtype=np.uint8), transform, (width, height),
        flags=cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT, borderValue=0,
    )
    kernel = np.ones((MASK_ERODE_PX * 2 + 1, MASK_ERODE_PX * 2 + 1), np.uint8)
    valid = cv2.erode(mask_b, kernel) > 0
    valid[:MASK_ERODE_PX, :] = False
    valid[-MASK_ERODE_PX:, :] = False
    valid[:, :MASK_ERODE_PX] = False
    valid[:, -MASK_ERODE_PX:] = False

    fraction = float(valid.sum()) / float(height * width)
    if not valid.any():
        return fraction, None, minimum
    va, vb = linear_a[valid], warped_b[valid]
    level = float((va.mean() + vb.mean()) / 2.0)
    if level <= 0:
        return fraction, None, minimum
    return fraction, float(np.abs(va - vb).mean() / level), minimum


# --------------------------------------------------------------------------
# Rebate edges (plan section 3.4's independent check, table 6)
# --------------------------------------------------------------------------

REBATE_CANNY_LOW = 50
REBATE_CANNY_HIGH = 150
REBATE_MIN_LENGTH_FRACTION = 0.5


def find_rebate_line(detection: DetectionImage) -> tuple[float, np.ndarray] | None:
    """The longest straight edge, as (angle_deg, [x1, y1, x2, y2]) in detection
    pixels, or None.

    The Canny and Hough settings above are this script's, not the plan's:
    section 3.4 leaves the rebate check recorded-and-warned rather than a gate
    precisely because its detectability is what P2-1 has to assess.
    """
    edges = cv2.Canny(detection.image, REBATE_CANNY_LOW, REBATE_CANNY_HIGH)
    min_length = int(min(detection.image.shape) * REBATE_MIN_LENGTH_FRACTION)
    lines = cv2.HoughLinesP(
        edges, 1, np.pi / 720, threshold=100, minLineLength=min_length, maxLineGap=10
    )
    if lines is None or len(lines) == 0:
        return None
    best = max(lines[:, 0, :], key=lambda ln: (ln[0] - ln[2]) ** 2 + (ln[1] - ln[3]) ** 2)
    x1, y1, x2, y2 = (float(v) for v in best)
    return float(np.rad2deg(np.arctan2(y2 - y1, x2 - x1))) % 180.0, np.array([x1, y1, x2, y2])


def rebate_deviation(layout: Layout, lines: dict[str, np.ndarray], scale: float) -> float | None:
    """Maximum perpendicular deviation, in canvas pixels, between the frames'
    rebate lines once mapped into canvas space. The longest line is the
    reference; the others' endpoints are measured against it.
    """
    index = {name: i for i, name in enumerate(layout.names)}
    mapped = []
    for name, line in lines.items():
        if name not in index:
            continue
        matrix = layout.matrix(index[name])
        points = to_full_resolution([[line[0], line[1]], [line[2], line[3]]], scale)
        mapped.append((matrix[:, :2] @ points.T).T + matrix[:, 2])
    if len(mapped) < 2:
        return None
    reference = max(mapped, key=lambda p: float(np.linalg.norm(p[1] - p[0])))
    direction = reference[1] - reference[0]
    length = float(np.linalg.norm(direction))
    if length == 0:
        return None
    normal = np.array([-direction[1], direction[0]]) / length
    worst = 0.0
    for points in mapped:
        if points is reference:
            continue
        for point in points:
            worst = max(worst, abs(float(normal @ (point - reference[0]))))
    return worst


# --------------------------------------------------------------------------
# Conversion and frame access
# --------------------------------------------------------------------------


def frame_names(negative: str) -> list[str]:
    return [f"{negative}_{i}" for i in range(1, FRAMES_PER_NEGATIVE + 1)]


def source_files(negatives: list[str]) -> list[str]:
    return [f"{name}.NEF" for negative in negatives for name in frame_names(negative)]


def ensure_intermediates(nef_dir: Path, work: Path, negatives: list[str]) -> None:
    work.mkdir(parents=True, exist_ok=True)
    wanted = [f"{name}.tif" for negative in negatives for name in frame_names(negative)]
    if all((work / name).exists() for name in wanted):
        print(f"Reusing {len(wanted)} intermediates in `{work}`.\n")
        return
    files = source_files(negatives)
    print(f"Converting {len(files)} frames into `{work}` (once) ...\n")
    started = time.monotonic()
    outcome = run_convert(
        nef_dir, files, work, FILM_DATE, FRAMES_PER_NEGATIVE,
        run_id=str(uuid.uuid4()), jobs=4,
    )
    print(f"`run_convert` status **{outcome.status}** in {time.monotonic() - started:.1f} s.\n")


def load_frame(work: Path, name: str) -> np.ndarray:
    return tifffile.imread(work / f"{name}.tif")


def read_frame_size(work: Path, name: str) -> tuple[int, int]:
    with tifffile.TiffFile(work / f"{name}.tif") as handle:
        shape = handle.pages[0].shape
    return int(shape[0]), int(shape[1])


def measure_negative(
    work: Path, negative: str, detector: str, *, long_edge: int, clahe: bool,
    ratio: float = MEASURE_RATIO, ransac_px: float = MEASURE_RANSAC_PX,
) -> tuple[list[PairMeasurement], float, tuple[int, int], float]:
    """Every pair of one negative, plus the detection scale, the full-resolution
    frame size, and the total per-frame detection time.
    """
    features: dict = {}
    detect_seconds: dict[str, float] = {}
    scale, size = 0.0, (0, 0)
    for name in frame_names(negative):
        started = time.monotonic()
        detection = build_detection_image(
            load_frame(work, name), long_edge=long_edge, clahe=clahe
        )
        features[name] = detect_features(detection, detector)
        detect_seconds[name] = time.monotonic() - started
        scale, size = detection.scale, detection.source_size

    names = frame_names(negative)
    results = [
        register_pair(
            negative, a, features[a], b, features[b],
            detector=detector, scale=scale, ratio=ratio, ransac_px=ransac_px,
            detect_seconds=detect_seconds[a] + detect_seconds[b],
        )
        for i, a in enumerate(names)
        for b in names[i + 1 :]
    ]
    return results, scale, size, sum(detect_seconds.values())


def pair_index(measurement: PairMeasurement) -> tuple[int, int]:
    return (int(measurement.a.rsplit("_", 1)[1]), int(measurement.b.rsplit("_", 1)[1]))


def truly_overlaps(measurement: PairMeasurement) -> bool:
    """Ground truth from appendix C, not from the metrics being calibrated."""
    return pair_index(measurement) in OVERLAPPING_PAIRS[measurement.negative]


def passes_provisional_gates(measurement: PairMeasurement) -> bool:
    return (
        measurement.transform is not None
        and measurement.inliers >= MEASURE_MIN_INLIERS
        and measurement.rigid_rms_px <= MEASURE_MAX_RMS_PX
    )


def accepted_pairs(results: list[PairMeasurement]) -> list[PairMeasurement]:
    """The pairs a layout may be built from — the provisional gates, so that a
    garbage fit on a non-overlapping pair cannot corrupt the global solve.
    """
    return [r for r in results if passes_provisional_gates(r)]


def try_layout(negative: str, size: tuple[int, int], results: list[PairMeasurement]):
    names = frame_names(negative)
    usable = accepted_pairs(results)
    if not usable or len(connected_component(names, usable)) != len(names):
        return None
    try:
        return solve_layout(names, size, usable)
    except (ValueError, np.linalg.LinAlgError):
        return None


# --------------------------------------------------------------------------
# Markdown helpers
# --------------------------------------------------------------------------


def table(headers: list[str], rows: list[list[str]]) -> None:
    print("| " + " | ".join(headers) + " |")
    print("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        print("| " + " | ".join(row) + " |")
    print()


def num(value, places: int = 3) -> str:
    if value is None:
        return "—"
    if isinstance(value, float) and (np.isnan(value) or np.isinf(value)):
        return "—"
    return f"{value:.{places}f}"


def pair_label(a: str, b: str) -> str:
    return f"{a.rsplit('_', 1)[1]}–{b.rsplit('_', 1)[1]}"


def median_or_nan(values: list[float]) -> float:
    return statistics.median(values) if values else float("nan")


# --------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------


def table_1(work: Path) -> str:
    print("## Table 1 — detector comparison\n")
    print(
        f"Detection images at `long_edge={MEASURE_LONG_EDGE}`, `clahe={MEASURE_CLAHE}`; "
        f"ratio test {MEASURE_RATIO}; RANSAC {MEASURE_RANSAC_PX} full-resolution px. "
        f"`seconds` covers detection of both frames plus matching and fitting. "
        f"ORB runs at its default 500-keypoint cap, which is what the plan's bare "
        f"`ORB_create` gives.\n"
    )
    rows = []
    per_detector: dict[str, list[PairMeasurement]] = {}
    for detector in DETECTOR_FACTORIES:
        collected: list[PairMeasurement] = []
        for negative in NEGATIVES:
            results, _, _, _ = measure_negative(
                work, negative, detector, long_edge=MEASURE_LONG_EDGE, clahe=MEASURE_CLAHE
            )
            collected.extend(results)
        per_detector[detector] = collected
        for m in collected:
            rows.append([
                m.negative, pair_label(m.a, m.b), m.detector,
                str(m.keypoints_a), str(m.keypoints_b), str(m.good_matches),
                str(m.inliers), num(m.inlier_ratio), num(m.rigid_rms_px),
                num(m.seconds, 2),
            ])
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    table(
        ["negative", "pair", "detector", "keypoints_a", "keypoints_b", "good_matches",
         "inliers", "inlier_ratio", "rigid_rms_px", "seconds"],
        rows,
    )

    print("Selection rule, stated because every later table depends on it. A "
          "detector is eligible if it recovers **every genuinely overlapping "
          "pair** (appendix C) and **falsely accepts none** of the "
          "non-overlapping ones; among the eligible, lowest median "
          "`rigid_rms_px` wins. Recall matters and so does refusal: a detector "
          "that confidently fits frames sharing no film is worse than one that "
          "misses a pair, because the global solve can route around a missing "
          "pair but not around a wrong one.\n")
    summary = []
    eligible = []
    for detector, collected in per_detector.items():
        overlapping = [m for m in collected if truly_overlaps(m)]
        disjoint = [m for m in collected if not truly_overlaps(m)]
        recovered = [m for m in overlapping if passes_provisional_gates(m)]
        false_accepts = [m for m in disjoint if passes_provisional_gates(m)]
        complete = len(recovered) == len(overlapping) and not false_accepts
        summary.append([
            detector,
            f"{len(recovered)}/{len(overlapping)}",
            str(len(false_accepts)),
            num(median_or_nan([float(m.inliers) for m in recovered]), 0),
            num(median_or_nan([m.inlier_ratio for m in recovered])),
            num(median_or_nan([m.rigid_rms_px for m in recovered])),
            num(median_or_nan([m.seconds for m in collected]), 2),
            "yes" if complete else "no",
        ])
        if complete:
            eligible.append((median_or_nan([m.rigid_rms_px for m in recovered]), detector))
    table(
        ["detector", "overlapping pairs recovered", "false accepts", "median_inliers",
         "median_inlier_ratio", "median_rms_px", "median_seconds", "eligible"],
        summary,
    )

    print("Worst false accept per detector — a non-overlapping pair the "
          "provisional gates would have let through:\n")
    worst = []
    for detector, collected in per_detector.items():
        offenders = [m for m in collected if not truly_overlaps(m) and passes_provisional_gates(m)]
        if not offenders:
            worst.append([detector, "none", "—", "—", "—", "—"])
            continue
        for m in offenders:
            worst.append([
                detector, f"{m.negative} {pair_label(m.a, m.b)}", str(m.good_matches),
                str(m.inliers), num(m.inlier_ratio), num(m.rigid_rms_px),
            ])
    table(
        ["detector", "pair", "good_matches", "inliers", "inlier_ratio", "rigid_rms_px"],
        worst,
    )

    best = min(eligible)[1] if eligible else "AKAZE"
    print(f"**Best detector: {best}.**\n")
    return best


def table_2(work: Path, detector: str) -> None:
    print("## Table 2 — detection-image preparation\n")
    print(f"Detector {detector}. Medians are over the genuinely overlapping pairs "
          f"only, so a non-overlapping pair's garbage fit cannot flatter or "
          f"punish a setting. `recovered` counts how many of those "
          f"{sum(len(v) for v in OVERLAPPING_PAIRS.values())} pairs the setting "
          f"found at all, and `false accepts` how many non-overlapping pairs it "
          f"wrongly fitted — a setting that loses a real pair or invents a fake "
          f"one is disqualified whatever its medians look like. "
          f"`seconds_per_frame` is detection-image construction plus feature "
          f"detection, the part that scales with `long_edge`.\n")
    rows = []
    for long_edge in (1200, 2000, 3000):
        for clahe in (False, True):
            recovered: list[PairMeasurement] = []
            overlapping = false_accepts = 0
            detect_total = 0.0
            frames = 0
            for negative in NEGATIVES:
                results, _, _, detect_seconds = measure_negative(
                    work, negative, detector, long_edge=long_edge, clahe=clahe
                )
                detect_total += detect_seconds
                frames += FRAMES_PER_NEGATIVE
                for m in results:
                    if truly_overlaps(m):
                        overlapping += 1
                        if passes_provisional_gates(m):
                            recovered.append(m)
                    elif passes_provisional_gates(m):
                        false_accepts += 1
            rows.append([
                str(long_edge), "on" if clahe else "off",
                f"{len(recovered)}/{overlapping}", str(false_accepts),
                num(median_or_nan([float(m.inliers) for m in recovered]), 0),
                num(median_or_nan([m.inlier_ratio for m in recovered])),
                num(median_or_nan([m.rigid_rms_px for m in recovered])),
                num(detect_total / frames, 2),
            ])
    table(
        ["long_edge", "clahe", "recovered", "false accepts", "median_inliers",
         "median_inlier_ratio", "median_rms_px", "seconds_per_frame"],
        rows,
    )


def table_3(measurements: list[PairMeasurement]) -> None:
    print("## Table 3 — scale drift\n")
    print("From the similarity fit, before the rigid re-fit. A real scale change "
          "would mean the copy stand moved, and then no rigid model can be right.\n")
    rows = []
    drifts = []
    for m in measurements:
        rows.append([m.negative, pair_label(m.a, m.b), num(m.scale, 6), num(m.scale_drift, 6)])
        if not np.isnan(m.scale_drift):
            drifts.append(m.scale_drift)
    table(["negative", "pair", "scale", "abs(scale - 1)"], rows)
    if drifts:
        drifts.sort()
        table(
            ["statistic", "abs(scale - 1)"],
            [
                ["min", num(drifts[0], 6)],
                ["median", num(statistics.median(drifts), 6)],
                ["99th percentile", num(float(np.percentile(drifts, 99)), 6)],
                ["max", num(drifts[-1], 6)],
            ],
        )


def table_4(work: Path, measurements: list[PairMeasurement]) -> None:
    print("## Table 4 — interpolation\n")
    negative = GOOD_NEGATIVES[0]
    pairs = [
        m for m in measurements
        if m.negative == negative and truly_overlaps(m) and passes_provisional_gates(m)
    ]
    print(f"Negative `{negative}`, full-resolution warps of its "
          f"{len(pairs)} accepted pairs. `overlap_mad` is the mean over them; "
          f"`min_value_after_warp` is the lowest value seen **before** the "
          f"mandatory clamp — section 2.3's undershoot, on film rather than on "
          f"synthetic data.\n")
    rows = []
    for label, interpolation in (
        ("INTER_LANCZOS4", cv2.INTER_LANCZOS4),
        ("INTER_CUBIC", cv2.INTER_CUBIC),
    ):
        started = time.monotonic()
        mads, minimum = [], float("inf")
        for m in pairs:
            frame_a, frame_b = load_frame(work, m.a), load_frame(work, m.b)
            _, mad, low = pairwise_overlap(frame_a, frame_b, m.transform, interpolation)
            del frame_a, frame_b
            minimum = min(minimum, low)
            if mad is not None:
                mads.append(mad)
        rows.append([
            label,
            num(float(np.mean(mads)), 5) if mads else "—",
            num(minimum, 4),
            num(time.monotonic() - started, 1),
        ])
    table(["interpolation", "overlap_mad", "min_value_after_warp", "seconds"], rows)


def table_5(work: Path, measurements: list[PairMeasurement]) -> None:
    print("## Table 5 — overlap MAD separation\n")
    print("The honest gate: whether the pixels actually line up, in linear light, "
          "over the shared valid area. Computed **pairwise** — frame b warped into "
          "frame a's own coordinates — so a pair still yields a number when the "
          "negative has no solvable global layout, which is exactly the case for "
          "the negative that is supposed to fail.\n")
    print("Labelled per pair from appendix C's ground truth, not per negative: "
          "`normal` and `tight` each contain an end-to-end pair sharing no film, "
          "and those are most of the bad-side evidence.\n")
    rows = []
    by_label: dict[str, list[float]] = {"good": [], "should_fail": []}
    for m in measurements:
        label = "good" if truly_overlaps(m) else "should_fail"
        if m.transform is not None:
            frame_a, frame_b = load_frame(work, m.a), load_frame(work, m.b)
            fraction, mad, _ = pairwise_overlap(
                frame_a, frame_b, m.transform, MEASURE_INTERPOLATION
            )
            del frame_a, frame_b
            m.overlap_fraction, m.overlap_mad = fraction, mad
            if mad is not None:
                by_label[label].append(mad)
        rows.append([
            m.negative, pair_label(m.a, m.b), label, str(m.good_matches), str(m.inliers),
            num(m.inlier_ratio), num(m.rigid_rms_px),
            num(m.overlap_fraction), num(m.overlap_mad, 5), m.note or "",
        ])
    table(
        ["negative", "pair", "label", "good_matches", "inliers", "inlier_ratio",
         "rigid_rms_px", "overlap_fraction", "overlap_mad", "note"],
        rows,
    )

    good, bad = sorted(by_label["good"]), sorted(by_label["should_fail"])
    table(
        ["label", "n", "min", "median", "max"],
        [
            [label, str(len(values)), num(values[0], 5) if values else "—",
             num(median_or_nan(values), 5), num(values[-1], 5) if values else "—"]
            for label, values in (("good", good), ("should_fail", bad))
        ],
    )
    if good and bad:
        gap = bad[0] - good[-1]
        print(f"**Worst good pair {good[-1]:.5f}. Best bad pair {bad[0]:.5f}. "
              f"Gap {gap:+.5f}.**\n")
        if gap <= 0:
            print("The distributions **overlap**, so `MAX_OVERLAP_MAD` cannot be set "
                  "from this data. That is a finding, not a missing number.\n")
    elif not bad:
        print("**No `should_fail` pair produced an `overlap_mad` at all**: every bad "
              "pair was refused before any transform existed to warp with. "
              "`MAX_OVERLAP_MAD` therefore has no bad-side evidence here, and the "
              "earlier gates are what did the refusing.\n")


def table_6(work: Path, measurements: list[PairMeasurement]) -> None:
    print("## Table 6 — rebate edges\n")
    print(f"Canny {REBATE_CANNY_LOW}/{REBATE_CANNY_HIGH}; `HoughLinesP` at "
          f"0.25-degree resolution, threshold 100, minimum line length "
          f"{REBATE_MIN_LENGTH_FRACTION:.0%} of the detection image's short edge, "
          f"maximum gap 10. **These settings are this script's, not the plan's** — "
          f"section 3.4 leaves this check recorded-and-warned rather than gated "
          f"because whether it is detectable at all is what P2-1 must assess.\n")
    rows, deviations = [], []
    for negative in NEGATIVES:
        lines: dict[str, np.ndarray] = {}
        scale, size = 0.0, (0, 0)
        for name in frame_names(negative):
            detection = build_detection_image(
                load_frame(work, name), long_edge=MEASURE_LONG_EDGE, clahe=MEASURE_CLAHE
            )
            scale, size = detection.scale, detection.source_size
            found = find_rebate_line(detection)
            if found is None:
                rows.append([negative, name.rsplit("_", 1)[1], "no", "—", "—"])
            else:
                angle, line = found
                lines[name] = line
                length = float(np.hypot(line[2] - line[0], line[3] - line[1]) * scale)
                rows.append([negative, name.rsplit("_", 1)[1], "yes", num(angle, 2), num(length, 0)])
        layout = try_layout(negative, size, [m for m in measurements if m.negative == negative])
        deviations.append((
            negative,
            rebate_deviation(layout, lines, scale) if layout is not None else None,
            len(lines),
        ))
    table(["negative", "frame", "long_edge_found", "angle_deg", "length_px"], rows)
    table(
        ["negative", "frames with an edge", "max_rebate_deviation_px"],
        [[n, f"{c}/{FRAMES_PER_NEGATIVE}", num(d, 1)] for n, d, c in deviations],
    )


def table_7(work: Path, detector: str, out: Path) -> None:
    print("## Table 7 — cost\n")
    print("Each negative is stitched in a **child process**, so `peak_rss_mib` is "
          "that negative's own `ru_maxrss` through `os.wait4`, exactly the way "
          "`measure-concurrency.py` measures it rather than as a running maximum "
          "over this process.\n")
    rows = []
    for negative in NEGATIVES:
        argv = [
            sys.executable, str(Path(__file__).resolve()),
            "--stitch-one", negative, "--work", str(work),
            "--out", str(out), "--detector", detector,
        ]
        with tempfile.TemporaryFile(mode="w+") as sink:
            process = subprocess.Popen(argv, stdout=sink, stderr=subprocess.STDOUT)
            _, status, usage = os.wait4(process.pid, 0)
            process.returncode = os.waitstatus_to_exitcode(status)
            sink.seek(0)
            text = sink.read()
        peak_mib = usage.ru_maxrss * _RSS_SCALE / MIB
        payload = {}
        for line in text.splitlines():
            if line.startswith("{"):
                payload = json.loads(line)
        if "error" in payload:
            rows.append([negative, str(FRAMES_PER_NEGATIVE)] + ["—"] * 8
                        + [num(peak_mib, 0), payload["error"]])
            continue
        if not payload:
            last = (text.strip().splitlines() or ["no output"])[-1][:70]
            rows.append([negative, "—"] + ["—"] * 8 + [num(peak_mib, 0), last])
            continue
        rows.append([
            negative, str(payload["frames"]),
            f'{payload["canvas_width"]}x{payload["canvas_height"]}',
            num(payload["detect_seconds"], 1), num(payload["match_seconds"], 1),
            num(payload["solve_seconds"], 2), num(payload["warp_seconds"], 1),
            num(payload["blend_seconds"], 1), num(payload["write_seconds"], 1),
            num(payload["total_seconds"], 1), num(peak_mib, 0),
            (
                f'rms {payload["global_rms_px"]:.2f} px, '
                f'spread {payload["strip_spread_ratio"]:.4f}, '
                f'covered {payload["coverage_fraction"]:.3f}, '
                f'{payload["output_bytes"] / MIB:.0f} MiB'
            ),
        ])
    table(
        ["negative", "frames", "canvas", "detect_s", "match_s", "solve_s", "warp_s",
         "blend_s", "write_s", "total_s", "peak_rss_mib", "outcome"],
        rows,
    )


def table_sweep(work: Path, detector: str) -> None:
    """Supplementary to the plan's seven tables.

    Tables 1 to 7 are all taken at one ratio test and one RANSAC threshold, so
    on their own they justify no value for either — they only show that the
    values used worked. `RATIO_TEST` and `RANSAC_REPROJ_PX` would then be the
    two proposals resting on nothing, so each is swept here, one at a time,
    around the settings the other tables used.
    """
    print("## Supplementary — ratio test and RANSAC threshold sweeps\n")
    print(f"Detector {detector} at `long_edge={MEASURE_LONG_EDGE}`, "
          f"`clahe={MEASURE_CLAHE}`. `margin` is the ratio of the fewest inliers "
          f"on a genuinely overlapping pair to the most on a non-overlapping one, "
          f"which is what a threshold actually has to separate; higher is safer.\n")

    def run(ratio: float, ransac_px: float) -> list[str]:
        recovered: list[PairMeasurement] = []
        overlapping = false_accepts = 0
        worst_good = float("inf")
        best_bad = 0.0
        for negative in NEGATIVES:
            results, _, _, _ = measure_negative(
                work, negative, detector, long_edge=MEASURE_LONG_EDGE,
                clahe=MEASURE_CLAHE, ratio=ratio, ransac_px=ransac_px,
            )
            for m in results:
                if truly_overlaps(m):
                    overlapping += 1
                    if passes_provisional_gates(m):
                        recovered.append(m)
                        worst_good = min(worst_good, m.inliers)
                else:
                    best_bad = max(best_bad, m.inliers)
                    if passes_provisional_gates(m):
                        false_accepts += 1
        margin = worst_good / best_bad if best_bad > 0 else float("inf")
        return [
            f"{len(recovered)}/{overlapping}", str(false_accepts),
            num(median_or_nan([float(m.inliers) for m in recovered]), 0),
            num(median_or_nan([m.inlier_ratio for m in recovered])),
            num(median_or_nan([m.rigid_rms_px for m in recovered])),
            str(int(worst_good)) if recovered else "—", str(int(best_bad)),
            num(margin, 1) if np.isfinite(margin) else "inf",
        ]

    columns = ["recovered", "false accepts", "median_inliers", "median_inlier_ratio",
               "median_rms_px", "fewest inliers, good pair", "most inliers, bad pair",
               "margin"]
    table(
        ["ratio_test", *columns],
        [[num(r, 2), *run(r, MEASURE_RANSAC_PX)] for r in (0.6, 0.7, 0.75, 0.8, 0.9)],
    )
    table(
        ["ransac_reproj_px", *columns],
        [[num(p, 1), *run(MEASURE_RATIO, p)] for p in (1.0, 1.5, 2.0, 3.0, 5.0, 8.0)],
    )


def romm_gate(work: Path, nef_dir: Path) -> None:
    print("## The ROMM gate\n")
    name = frame_names(GOOD_NEGATIVES[0])[0]
    codes = load_frame(work, name)
    again = encode_from_linear(decode_to_linear(codes))
    diff = np.abs(again.astype(np.int32) - codes.astype(np.int32))
    print(f"Round trip on `{name}.tif` with the section 2.3.1 curve: **max "
          f"{int(diff.max())} LSB**, {int(np.count_nonzero(diff))} of {codes.size} "
          f"pixels changed.\n")

    with rawpy.imread(str(nef_dir / f"{name}.NEF")) as raw:
        linear_codes = raw.postprocess(**dict(RAW_PARAMS, gamma=(1, 1)))
    index = np.linspace(0, codes.size - 1, 2_000_000).astype(np.int64)
    truth = linear_codes.reshape(-1)[index].astype(np.float64) / MAX_CODE
    got = decode_to_linear(codes.reshape(-1)[index]).astype(np.float64)
    nonzero = truth > 0
    relative = np.abs(got[nonzero] - truth[nonzero]) / truth[nonzero]
    print(f"Against a linear decode of the same NEF: **max "
          f"{relative.max() * 100:.4f}%**, mean {relative.mean() * 100:.4f}% "
          f"relative error.\n")
    if diff.max() != 0:
        print("**GATE FAILED** — the round trip is not exact. Stop and report.\n")
    else:
        print("Gate passes. The round trip alone could not detect a wrong curve "
              "(section 2.3.1); the linear comparison above is what does.\n")


# --------------------------------------------------------------------------
# Child mode: stitch one negative, report timings as JSON on stdout
# --------------------------------------------------------------------------


def _stitch_child(negative: str, work: Path, out: Path, detector: str) -> int:
    overall = time.monotonic()
    results, _, size, detect_seconds = measure_negative(
        work, negative, detector, long_edge=MEASURE_LONG_EDGE, clahe=MEASURE_CLAHE
    )
    match_seconds = sum(m.match_seconds for m in results)
    usable = accepted_pairs(results)

    names = frame_names(negative)
    component = connected_component(names, usable)
    if not usable or len(component) != len(names):
        print(json.dumps({
            "negative": negative,
            "error": "STITCH_UNDERCONSTRAINED",
            "unplaceable": sorted(set(names) - component),
        }))
        return 2

    started = time.monotonic()
    layout = solve_layout(names, size, usable)
    solve_seconds = time.monotonic() - started

    canvas_w, canvas_h = layout.canvas_size
    accumulator = np.zeros((canvas_h, canvas_w, 3), dtype=np.float32)
    weights = np.zeros((canvas_h, canvas_w), dtype=np.float32)
    warp_seconds = blend_seconds = 0.0

    for i, name in enumerate(layout.names):
        frame = load_frame(work, name)
        started = time.monotonic()
        warped, weight, (ox, oy), _ = warp_into_own_box(
            frame, layout.matrix(i), MEASURE_INTERPOLATION, layout.canvas_size
        )
        warp_seconds += time.monotonic() - started
        del frame
        started = time.monotonic()
        height, width = weight.shape
        accumulator[oy : oy + height, ox : ox + width] += warped * weight[:, :, None]
        weights[oy : oy + height, ox : ox + width] += weight
        blend_seconds += time.monotonic() - started
        del warped, weight

    started = time.monotonic()
    covered = weights > 0
    np.divide(accumulator, weights[:, :, None], out=accumulator, where=covered[:, :, None])
    accumulator[~covered] = 0.0  # FILL_COLOR, section 3.3
    coverage = float(covered.mean())
    del weights, covered
    image = encode_from_linear(accumulator)
    blend_seconds += time.monotonic() - started
    del accumulator

    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{negative}-stitched.tif"
    started = time.monotonic()
    tifffile.imwrite(
        path, image, photometric="rgb", compression="deflate",
        predictor=True, metadata=None,
    )
    write_seconds = time.monotonic() - started

    # A JPEG the user can actually open next to the TIFF, which is 300+ MiB.
    preview_w = min(2400, canvas_w)
    preview = cv2.resize(
        (image[:, :, ::-1] >> 8).astype(np.uint8),
        (preview_w, max(1, round(canvas_h * preview_w / canvas_w))),
        interpolation=cv2.INTER_AREA,
    )
    cv2.imwrite(str(out / f"{negative}-preview.jpg"), preview, [cv2.IMWRITE_JPEG_QUALITY, 92])

    print(json.dumps({
        "negative": negative,
        "frames": len(names),
        "canvas_width": canvas_w,
        "canvas_height": canvas_h,
        "coverage_fraction": coverage,
        "global_rms_px": layout.global_rms_px,
        "strip_spread_ratio": layout.strip_spread_ratio,
        "rotations_deg": layout.rotations_deg,
        "translations": layout.translations,
        "output_bytes": path.stat().st_size,
        "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * _RSS_SCALE / MIB,
        "detect_seconds": detect_seconds,
        "match_seconds": match_seconds,
        "solve_seconds": solve_seconds,
        "warp_seconds": warp_seconds,
        "blend_seconds": blend_seconds,
        "write_seconds": write_seconds,
        "total_seconds": time.monotonic() - overall,
    }))
    return 0


# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nef-dir", type=Path, default=DEFAULT_NEF_DIR, dest="nef_dir")
    parser.add_argument("--out", type=Path, default=Path("/tmp/scanny-p2-1"))
    parser.add_argument("--work", type=Path, default=None)
    parser.add_argument("--tables", default="all")
    parser.add_argument("--detector", default=None)
    parser.add_argument("--stitch-one", default=None, dest="stitch_one")
    args = parser.parse_args()

    work = args.work or (args.out / "work")

    if args.stitch_one:
        return _stitch_child(args.stitch_one, work, args.out, args.detector or "AKAZE")

    files = source_files(NEGATIVES)
    missing = [f for f in files if not (args.nef_dir / f).exists()]
    if missing:
        print(
            "Nothing was measured: the user gate B sample scans are not present at "
            f"{args.nef_dir}.\n\n"
            f"Missing {len(missing)} of {len(files)}: {', '.join(missing)}\n\n"
            "Gate B (plan section 5) requires a routine, a deliberately rotated, a "
            "reverse-order, a tight-overlap, and a should-fail negative. Without "
            "them no detector can be compared, no threshold can be calibrated, and "
            "appendix B cannot be written. Substitutes may not be synthesised.",
            file=sys.stderr,
        )
        return 1

    wanted = {"1", "2", "3", "4", "5", "6", "7", "romm", "sweep"}
    if args.tables != "all":
        wanted = set(args.tables.split(","))

    print("# Chunk P2-1 registration measurements\n")
    stamp = datetime.datetime.now(datetime.UTC).astimezone().isoformat(timespec="seconds")
    print(f"Generated {stamp} by "
          f"`scripts/measure-registration.py` from `{args.nef_dir}`, OpenCV "
          f"{cv2.__version__}.\n")
    print(f"Negatives: {', '.join(GOOD_NEGATIVES)} (expected to stitch); "
          f"{', '.join(BAD_NEGATIVES)} (expected to fail).\n")

    ensure_intermediates(args.nef_dir, work, NEGATIVES)

    if "romm" in wanted:
        romm_gate(work, args.nef_dir)

    detector = args.detector
    if "1" in wanted:
        detector = table_1(work)
    detector = detector or "AKAZE"
    if "2" in wanted:
        table_2(work, detector)

    measurements: list[PairMeasurement] = []
    if wanted & {"3", "4", "5", "6"}:
        for negative in NEGATIVES:
            results, _, _, _ = measure_negative(
                work, negative, detector, long_edge=MEASURE_LONG_EDGE, clahe=MEASURE_CLAHE
            )
            measurements.extend(results)
    if "3" in wanted:
        table_3(measurements)
    if "4" in wanted:
        table_4(work, measurements)
    if "5" in wanted:
        table_5(work, measurements)
    if "6" in wanted:
        table_6(work, measurements)
    if "sweep" in wanted:
        table_sweep(work, detector)
    if "7" in wanted:
        table_7(work, detector, args.out)

    print(f"Stitched TIFFs and previews are in `{args.out}`.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
