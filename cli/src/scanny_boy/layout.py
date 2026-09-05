"""Global layout solve: places every accepted-pair frame in one canvas
coordinate system via three linear least-squares problems.

Frame *i* maps its own pixel `p` into canvas space as
`x = s_i * R(theta_i)*p + t_i` (docs/STITCH_QUALITY_PLAN.md section 2: film
does not sit at a constant height above the stage, so a strip is not one
magnification). A `PairResult` for (a, b) contributes a **similarity**:
`p_a = sigma_ab * R(phi_ab)*p_b + u_ab`. Requiring both routes into canvas
space to agree gives three relations, each linear in the right variable:
`log s_b - log s_a = log sigma_ab`, `theta_b = theta_a + phi_ab`, and
`t_b = t_a + s_a * R(theta_a) . u_ab`. Scales are solved first (log-space,
the same idiom as `solve_gains`'s geometric-mean-1 anchor), then rotations
(one linear least-squares problem in the scalar `theta`s), then translations
(linear in `t` once `s` and `theta` are known). This three-step formulation
is why section 4.1 forbids SciPy — do not replace it with a nonlinear
bundle adjustment. The model is a similarity — rigid plus one isotropic
scale — never an affine, never a homography. When the stitch stage has
fitted a rig-tilt rectification (`registration.Rectification`,
docs/RECTIFICATION_PLAN.md), the pairs and points these solves consume are
already rectified and the canvas is rectified space: the placement model
itself is unchanged, and only the frame's canvas footprint is the
rectified keystone quad rather than the affine image of the raw rectangle
(`frame_corners`).

`solve_layout` places frames geometrically; `solve_gains` is its photometric
counterpart: per-channel gains reconciling lamp drift between frames, from
one linear least-squares problem per channel in log space (one row per
usable pair: `-1` in column a, `+1` in column b, rhs `log(mean_a/mean_b)`
over the pair's shared area, weighted by `sqrt(shared_count)` — a mean over
N pixels has variance ∝ 1/N). The anchor row is all-ones with rhs 0, so the
solved gains have geometric mean 1: no frame's lamp level is privileged, and
the worst-case gain excursion — the clipping exposure, since gains above 1.0
push linear values into `encode_from_linear`'s [0, 1] clamp — is
minimized. Names are sorted internally so the solved system does not depend
on placement order.

`MAX_GLOBAL_RMS_PX` and `STRIP_SPREAD_RATIO` are Chunk P2-1's measured
constants, approved at user gate C (section 3.12). Production code reads
them from here and from nowhere else. `REBATE_DEVIATION_WARN` is
deliberately not defined: section 3.12.2 found that a generic straight-edge
detector cannot reliably find the same physical rebate edge across frames,
so this chunk implements no rebate detection.
"""

from __future__ import annotations

import dataclasses
import math

import cv2
import numpy as np

from scanny_boy.events import Code
from scanny_boy.registration import (
    PairResult,
    Rectification,
    StitchError,
    rectify,
)

MAX_GLOBAL_RMS_PX = 12.0
STRIP_SPREAD_RATIO = 0.15

# Row weight for the layout solves: a pairwise transform from N inliers at
# residual `rms` has parameter variance proportional to rms^2 / N, so the
# natural row weight is sqrt(N) / rms — mirroring solve_gains's
# sqrt(shared_count), where a mean over N pixels has variance ∝ 1/N. The
# floor is a numerical guard, not a measured threshold — a synthetic
# fixture can fit to essentially zero residual, which without it would give
# one pair unbounded authority over the solve.
RMS_WEIGHT_FLOOR_PX = 0.1

_MAX_PAIR_ROTATION_DEG = 45.0


def _row_weight(pair: PairResult) -> float:
    return math.sqrt(pair.inliers) / max(pair.rms_residual_px, RMS_WEIGHT_FLOOR_PX)


def _rotation_matrix(angle_deg: float) -> np.ndarray:
    angle_rad = np.deg2rad(angle_deg)
    cos_a, sin_a = np.cos(angle_rad), np.sin(angle_rad)
    return np.array([[cos_a, -sin_a], [sin_a, cos_a]])


def _angle_deg(rotation: np.ndarray) -> float:
    return float(np.degrees(np.arctan2(rotation[1, 0], rotation[0, 0])))


@dataclasses.dataclass(frozen=True)
class FramePlacement:
    name: str
    rotation_deg: float
    translation: tuple[float, float]
    scale: float = 1.0

    def matrix(self) -> np.ndarray:
        rotation = self.scale * _rotation_matrix(self.rotation_deg)
        translation = np.array(self.translation).reshape(2, 1)
        return np.hstack([rotation, translation])


@dataclasses.dataclass(frozen=True)
class Layout:
    placements: list[FramePlacement]
    canvas_size: tuple[int, int]  # (width, height)
    global_rms_px: float
    used_pairs: list[PairResult]
    strip_spread_ratio: float
    # Unit vector, canvas space: the strip's long axis, for composite.py's
    # strip-axis feather. None when fewer than two placements, the centres
    # are coincident, or strip_spread_ratio says the layout is not
    # strip-shaped — an axis fitted to a blob would feather along an
    # arbitrary direction. The weight formula it feeds is symmetric under a
    # sign flip of the axis, so no sign canonicalisation is needed here.
    strip_axis: tuple[float, float] | None


def check_connectivity(names: list[str], pairs: list[PairResult]) -> None:
    """Union-find over accepted pairs. Raises
    StitchError(STITCH_UNDERCONSTRAINED, ...) naming every frame not in the
    component containing names[0]."""
    parent = {name: name for name in names}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str) -> None:
        root_x, root_y = find(x), find(y)
        if root_x != root_y:
            parent[root_x] = root_y

    for pair in pairs:
        if pair.accepted:
            union(pair.a, pair.b)

    root = find(names[0])
    unreachable = [name for name in names if find(name) != root]
    if unreachable:
        raise StitchError(
            Code.STITCH_UNDERCONSTRAINED,
            f"frames not reachable from {names[0]!r}: {unreachable}",
        )


def frame_corners(
    placement: FramePlacement,
    frame_size: tuple[int, int],
    rectification: Rectification | None = None,
) -> np.ndarray:
    """The frame's four corners in canvas space, (4, 2).

    With a rectification, the corners first map through `W`: the frame's
    canvas footprint is the rectified keystone quad, not the affine image
    of the raw rectangle. Without one this is exactly the pre-rectification
    computation, bit for bit — the regression the rectification=None path
    pins."""
    height, width = frame_size
    corners_local = np.array(
        [[0, 0], [width, 0], [width, height], [0, height]], dtype=np.float64
    )
    if rectification is not None:
        corners_local = rectify(corners_local, rectification)

    matrix = placement.matrix()
    rotation, translation = matrix[:, :2], matrix[:, 2]
    return corners_local @ rotation.T + translation


def solve_layout(
    names: list[str],
    frame_size: tuple[int, int],
    pairs: list[PairResult],
    rectification: Rectification | None = None,
) -> Layout:
    """frame_size is (height, width), identical for every frame.

    `rectification`, when given, is the stitch stage's fitted rig-tilt
    rectification: `pairs` are already rectified (the caller re-registered
    them — docs/RECTIFICATION_PLAN.md section 4), and it is used only for
    the canvas-bounds corner mapping here."""
    check_connectivity(names, pairs)

    accepted_pairs = [pair for pair in pairs if pair.accepted]
    index = {name: i for i, name in enumerate(names)}
    n = len(names)

    # Step 0: per-frame scale. One row per accepted pair whose similarity
    # scale is usable (-1 in column a, +1 in column b, rhs log sigma_ab), in
    # log space so the system is linear, plus an all-ones anchor row at rhs
    # 0 — solve_gains's idiom exactly — so the solved scales' geometric mean
    # is 1 and no frame's magnification is privileged. A non-positive
    # similarity scale means a reflected fit, which the reflection guard in
    # similarity_from_correspondences should already have excluded; such a
    # row is dropped, not raised on, the same way solve_gains drops a
    # degenerate channel mean.
    scale_rows = []
    scale_rhs = []
    scale_row_weights = []
    scale_covered: set[str] = set()
    for pair in accepted_pairs:
        if pair.similarity_scale <= 0:
            continue
        weight = _row_weight(pair)
        row = np.zeros(n)
        row[index[pair.a]] = -weight
        row[index[pair.b]] = weight
        scale_rows.append(row)
        scale_rhs.append(weight * math.log(pair.similarity_scale))
        scale_row_weights.append(weight)
        scale_covered.add(pair.a)
        scale_covered.add(pair.b)

    if scale_rows:
        scale_anchor_weight = math.sqrt(sum(w * w for w in scale_row_weights))
        scale_anchor = np.zeros(n)
        for name in scale_covered:
            scale_anchor[index[name]] = scale_anchor_weight
        scale_rows.append(scale_anchor)
        scale_rhs.append(0.0)

        log_scales, *_ = np.linalg.lstsq(
            np.array(scale_rows), np.array(scale_rhs), rcond=None
        )
        scales = np.exp(log_scales)
    else:
        scales = np.ones(n)

    # Step 1: rotations. One row per accepted pair (-1 in column a, +1 in
    # column b, rhs phi_ab, taken from the pair's similarity fit now that
    # scale is no longer forced to 1), plus one anchor row fixing theta_0
    # = 0.
    rotation_rows = []
    rotation_rhs = []
    rotation_row_weights = []
    for pair in accepted_pairs:
        phi_ab_deg = _angle_deg(pair.similarity_transform[:, :2])
        if not abs(phi_ab_deg) < _MAX_PAIR_ROTATION_DEG:
            # Data from `register_pair` (RANSAC output), not an internal
            # invariant: a wildly wrong pair fit is a residual problem, and
            # raising the stable code keeps it CLAHE-retry eligible.
            raise StitchError(
                Code.STITCH_RESIDUAL_TOO_HIGH,
                f"pair {pair.a}-{pair.b} rotation {phi_ab_deg:.1f} degrees "
                f"exceeds {_MAX_PAIR_ROTATION_DEG} degrees",
            )
        weight = _row_weight(pair)
        row = np.zeros(n)
        row[index[pair.a]] = -weight
        row[index[pair.b]] = weight
        rotation_rows.append(row)
        rotation_rhs.append(weight * np.deg2rad(phi_ab_deg))
        rotation_row_weights.append(weight)

    rotation_anchor_weight = math.sqrt(sum(w * w for w in rotation_row_weights))
    anchor_row = np.zeros(n)
    anchor_row[0] = rotation_anchor_weight
    rotation_rows.append(anchor_row)
    rotation_rhs.append(0.0)

    theta, *_ = np.linalg.lstsq(
        np.array(rotation_rows), np.array(rotation_rhs), rcond=None
    )

    # Step 2: translations. With s and theta known,
    # t_b - t_a = s_a * R(theta_a) . u_ab is linear in t (u_ab likewise
    # taken from the pair's similarity fit). Two rows per accepted pair (x
    # and y), plus two anchor rows fixing t_0 = (0, 0).
    translation_rows = []
    translation_rhs = []
    translation_row_weights = []
    for pair in accepted_pairs:
        rotation_a = _rotation_matrix(np.degrees(theta[index[pair.a]]))
        u_ab = pair.similarity_transform[:, 2]
        predicted = scales[index[pair.a]] * (rotation_a @ u_ab)
        weight = _row_weight(pair)
        translation_row_weights.append(weight)

        row_x = np.zeros(2 * n)
        row_x[2 * index[pair.a]] = -weight
        row_x[2 * index[pair.b]] = weight
        translation_rows.append(row_x)
        translation_rhs.append(weight * predicted[0])

        row_y = np.zeros(2 * n)
        row_y[2 * index[pair.a] + 1] = -weight
        row_y[2 * index[pair.b] + 1] = weight
        translation_rows.append(row_y)
        translation_rhs.append(weight * predicted[1])

    translation_anchor_weight = math.sqrt(sum(w * w for w in translation_row_weights))
    anchor_x = np.zeros(2 * n)
    anchor_x[0] = translation_anchor_weight
    translation_rows.append(anchor_x)
    translation_rhs.append(0.0)

    anchor_y = np.zeros(2 * n)
    anchor_y[1] = translation_anchor_weight
    translation_rows.append(anchor_y)
    translation_rhs.append(0.0)

    t_flat, *_ = np.linalg.lstsq(
        np.array(translation_rows), np.array(translation_rhs), rcond=None
    )
    translations = t_flat.reshape(n, 2)

    placements = [
        FramePlacement(
            name=names[i],
            rotation_deg=float(np.degrees(theta[i])),
            translation=(float(translations[i, 0]), float(translations[i, 1])),
            scale=float(scales[i]),
        )
        for i in range(n)
    ]

    # Canvas bounds: transform each frame's four corners, union bounding box.
    all_corners = np.vstack(
        [
            frame_corners(placement, frame_size, rectification)
            for placement in placements
        ]
    )

    min_xy = all_corners.min(axis=0)
    max_xy = all_corners.max(axis=0)

    # Subtract (min_x, min_y) so the canvas origin is (0, 0).
    shifted_placements = [
        FramePlacement(
            name=p.name,
            rotation_deg=p.rotation_deg,
            translation=(
                p.translation[0] - min_xy[0],
                p.translation[1] - min_xy[1],
            ),
            scale=p.scale,
        )
        for p in placements
    ]

    canvas_size = (
        math.ceil(max_xy[0] - min_xy[0]),
        math.ceil(max_xy[1] - min_xy[1]),
    )

    ratio, axis = _strip_geometry(shifted_placements, frame_size)
    if ratio > STRIP_SPREAD_RATIO:
        axis = None

    return Layout(
        placements=shifted_placements,
        canvas_size=canvas_size,
        global_rms_px=global_rms(shifted_placements, accepted_pairs),
        used_pairs=accepted_pairs,
        strip_spread_ratio=ratio,
        strip_axis=axis,
    )


@dataclasses.dataclass(frozen=True)
class GainStat:
    """Per-pair photometric evidence for `solve_gains`, measured by
    composite.py over the pair's shared valid area: the per-channel mean
    linear level of each frame, and the number of shared pixels."""

    a: str
    b: str
    mean_a: tuple[float, float, float]
    mean_b: tuple[float, float, float]
    shared_count: int


# Numerical guard, not a measured threshold: a channel mean at or below this
# carries no usable log-ratio (NegPy's gain_compensate uses the same floor).
_MIN_CHANNEL_MEAN = 1e-6


def solve_gains(
    names: list[str],
    stats: list[GainStat],
) -> dict[str, tuple[float, float, float]]:
    """Per-frame, per-channel gains reconciling photometric mismatch
    (lamp drift, exposure variation) between a negative's frames.

    For each channel independently, the log-gains solve one least-squares
    system: one row per stat whose channel means are both usable
    (`g_b - g_a = log(mean_a / mean_b)`, weighted by `sqrt(shared_count)`),
    plus one all-ones anchor row with rhs 0 fixing the gains' geometric mean
    to 1. Rows are dropped, not errored, when a channel mean is degenerate;
    frames that survive in no row for a channel keep gain 1.0. Connectivity
    of `stats` is guaranteed upstream by `check_connectivity`, so the only
    degeneracy this handles is dropped rows.

    Names are sorted before the system is built so the matrix does not
    depend on the caller's placement order — compositing a layout forward or
    reversed must produce bitwise-identical gains.
    """
    gains = {name: [1.0, 1.0, 1.0] for name in names}
    index = {name: i for i, name in enumerate(sorted(names))}

    for channel in range(3):
        rows = []
        rhs = []
        covered: set[str] = set()
        for stat in stats:
            mean_a = stat.mean_a[channel]
            mean_b = stat.mean_b[channel]
            if mean_a <= _MIN_CHANNEL_MEAN or mean_b <= _MIN_CHANNEL_MEAN:
                continue
            weight = math.sqrt(stat.shared_count)
            row = np.zeros(len(index))
            row[index[stat.a]] = -weight
            row[index[stat.b]] = weight
            rows.append(row)
            rhs.append(weight * math.log(mean_a / mean_b))
            covered.add(stat.a)
            covered.add(stat.b)

        if not rows:
            continue

        # The anchor row is weighted to the same total as the data rows so
        # the geometric-mean constraint holds effectively exactly without
        # being a hard constraint lstsq cannot trade against.
        anchor_weight = math.sqrt(sum(float(row @ row) for row in rows))
        anchor = np.zeros(len(index))
        for name in sorted(covered):
            anchor[index[name]] = anchor_weight
        rows.append(anchor)
        rhs.append(0.0)

        solution, *_ = np.linalg.lstsq(
            np.array(rows), np.array(rhs), rcond=None
        )
        for name in covered:
            gains[name][channel] = float(np.exp(solution[index[name]]))

    return {
        name: (channel_gains[0], channel_gains[1], channel_gains[2])
        for name, channel_gains in gains.items()
    }


def global_rms(placements: list[FramePlacement], pairs: list[PairResult]) -> float:
    """For every accepted pair and every one of its inlier correspondences,
    the distance between the two frames' canvas-space predictions of the
    same point. Returns the RMS over all of them."""
    placement_by_name = {placement.name: placement for placement in placements}

    squared_errors = []
    for pair in pairs:
        if not pair.accepted:
            continue
        matrix_a = placement_by_name[pair.a].matrix()
        matrix_b = placement_by_name[pair.b].matrix()
        rotation_a, translation_a = matrix_a[:, :2], matrix_a[:, 2]
        rotation_b, translation_b = matrix_b[:, :2], matrix_b[:, 2]

        canvas_from_a = pair.inlier_points_a @ rotation_a.T + translation_a
        canvas_from_b = pair.inlier_points_b @ rotation_b.T + translation_b
        diff = canvas_from_a - canvas_from_b
        if diff.size:
            squared_errors.append(np.sum(diff**2, axis=1))

    if not squared_errors:
        return 0.0
    return float(np.sqrt(np.mean(np.concatenate(squared_errors))))


def _strip_geometry(
    placements: list[FramePlacement], frame_size: tuple[int, int]
) -> tuple[float, tuple[float, float] | None]:
    """One SVD of the placed, mean-subtracted frame centres, shared by
    `strip_spread_ratio` (the ratio of the second singular value to the
    first — a strip is near 0) and the strip axis (the first right-singular
    vector) that `solve_layout` publishes on `Layout` for composite.py's
    feather. The axis is None whenever the ratio is: fewer than two
    placements, or the largest singular value is 0 (coincident centres)."""
    height, width = frame_size
    local_center = np.array([width / 2.0, height / 2.0])

    centers = []
    for placement in placements:
        matrix = placement.matrix()
        rotation, translation = matrix[:, :2], matrix[:, 2]
        centers.append(rotation @ local_center + translation)
    centers = np.array(centers)

    if len(centers) < 2:
        return 0.0, None

    centers = centers - centers.mean(axis=0)

    _u, singular_values, vt = np.linalg.svd(centers)
    if singular_values[0] == 0:
        return 0.0, None

    axis = (float(vt[0, 0]), float(vt[0, 1]))
    ratio = float(singular_values[1] / singular_values[0])
    return ratio, axis


def strip_spread_ratio(
    placements: list[FramePlacement], frame_size: tuple[int, int]
) -> float:
    """Placed frame centres, mean-subtracted; the ratio of the second
    singular value to the first. A strip is near 0."""
    ratio, _axis = _strip_geometry(placements, frame_size)
    return ratio


def _largest_all_covered_rectangle(mask: np.ndarray) -> tuple[int, int, int, int]:
    """Largest all-1 axis-aligned rectangle in a 2D 0/1 array, via the
    standard per-row histogram-and-stack sweep. Returns (x, y, width,
    height) in mask coordinates."""
    n_rows, n_cols = mask.shape
    heights = np.zeros(n_cols, dtype=np.int64)

    best_area = 0
    best_rect = (0, 0, 0, 0)

    for row in range(n_rows):
        row_covered = mask[row] != 0
        heights = np.where(row_covered, heights + 1, 0)

        stack: list[tuple[int, int]] = []  # (start_col, height)
        for col in range(n_cols + 1):
            h = int(heights[col]) if col < n_cols else 0
            start = col
            while stack and stack[-1][1] >= h:
                stack_start, stack_height = stack.pop()
                width = col - stack_start
                area = stack_height * width
                if area > best_area:
                    best_area = area
                    top = row - stack_height + 1
                    best_rect = (stack_start, top, width, stack_height)
                start = stack_start
            stack.append((start, h))

    return best_rect


def largest_valid_rect(
    layout: Layout,
    frame_size: tuple[int, int],
    *,
    probe_long_edge: int = 2000,
    rectification: Rectification | None = None,
) -> tuple[int, int, int, int]:
    """Returns (x, y, width, height) in canvas pixels.

    Build a uint8 coverage mask on a canvas downscaled so its long edge is
    at most probe_long_edge, filling each frame's transformed quadrilateral
    with cv2.fillConvexPoly. Find the largest all-covered axis-aligned
    rectangle with the standard histogram-and-stack sweep. Scale the result
    back up, then shrink it by one probe cell on every side, so the
    recorded rectangle is always inside the true valid area and never
    larger than it.

    `rectification` reaches the corner mapping exactly as it does
    `solve_layout` (docs/RECTIFICATION_PLAN.md section 5)."""
    canvas_width, canvas_height = layout.canvas_size
    probe_scale = min(1.0, probe_long_edge / max(canvas_width, canvas_height))

    probe_width = max(1, round(canvas_width * probe_scale))
    probe_height = max(1, round(canvas_height * probe_scale))

    mask = np.zeros((probe_height, probe_width), dtype=np.uint8)

    for placement in layout.placements:
        corners_canvas = frame_corners(placement, frame_size, rectification)
        corners_probe = np.round(corners_canvas * probe_scale).astype(np.int32)
        cv2.fillConvexPoly(mask, corners_probe, 1)

    probe_x, probe_y, probe_w, probe_h = _largest_all_covered_rectangle(mask)

    shrink = 1.0 / probe_scale
    full_x = probe_x / probe_scale
    full_y = probe_y / probe_scale
    full_right = (probe_x + probe_w) / probe_scale
    full_bottom = (probe_y + probe_h) / probe_scale

    x = math.ceil(full_x + shrink)
    y = math.ceil(full_y + shrink)
    right = math.floor(full_right - shrink)
    bottom = math.floor(full_bottom - shrink)

    result_width = max(0, right - x)
    result_height = max(0, bottom - y)

    return (x, y, result_width, result_height)
