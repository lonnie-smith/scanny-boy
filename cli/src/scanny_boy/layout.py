"""Global layout solve: places every accepted-pair frame in one canvas
coordinate system via two linear least-squares problems.

Frame *i* maps its own pixel `p` into canvas space as `x = R(theta_i)*p +
t_i`. A `PairResult` for (a, b) gives `p_a = R(phi_ab)*p_b + u_ab`.
Requiring both routes into canvas space to agree gives exactly two
relations: `theta_b = theta_a + phi_ab` and
`t_b = t_a + R(theta_a) . u_ab`. Rotations are solved first (one linear
least-squares problem in the scalar `theta`s), then translations (linear in
`t` once `theta` is known). This two-step formulation is why section 4.1
forbids SciPy — do not replace it with a nonlinear bundle adjustment.

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
from scanny_boy.registration import PairResult, StitchError

MAX_GLOBAL_RMS_PX = 12.0
STRIP_SPREAD_RATIO = 0.15

_MAX_PAIR_ROTATION_DEG = 45.0


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

    def matrix(self) -> np.ndarray:
        rotation = _rotation_matrix(self.rotation_deg)
        translation = np.array(self.translation).reshape(2, 1)
        return np.hstack([rotation, translation])


@dataclasses.dataclass(frozen=True)
class Layout:
    placements: list[FramePlacement]
    canvas_size: tuple[int, int]  # (width, height)
    global_rms_px: float
    used_pairs: list[PairResult]
    strip_spread_ratio: float


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


def solve_layout(
    names: list[str],
    frame_size: tuple[int, int],
    pairs: list[PairResult],
) -> Layout:
    """frame_size is (height, width), identical for every frame."""
    check_connectivity(names, pairs)

    accepted_pairs = [pair for pair in pairs if pair.accepted]
    index = {name: i for i, name in enumerate(names)}
    n = len(names)

    # Step 1: rotations. One row per accepted pair (-1 in column a, +1 in
    # column b, rhs phi_ab), plus one anchor row fixing theta_0 = 0.
    rotation_rows = []
    rotation_rhs = []
    for pair in accepted_pairs:
        phi_ab_deg = _angle_deg(pair.transform[:, :2])
        assert abs(phi_ab_deg) < _MAX_PAIR_ROTATION_DEG, (
            f"pair {pair.a}-{pair.b} rotation {phi_ab_deg} degrees exceeds "
            f"{_MAX_PAIR_ROTATION_DEG} degrees; this is a bug upstream, "
            "not a case this solver handles"
        )
        row = np.zeros(n)
        row[index[pair.a]] = -1.0
        row[index[pair.b]] = 1.0
        rotation_rows.append(row)
        rotation_rhs.append(np.deg2rad(phi_ab_deg))

    anchor_row = np.zeros(n)
    anchor_row[0] = 1.0
    rotation_rows.append(anchor_row)
    rotation_rhs.append(0.0)

    theta, *_ = np.linalg.lstsq(
        np.array(rotation_rows), np.array(rotation_rhs), rcond=None
    )

    # Step 2: translations. With theta known, t_b - t_a = R(theta_a) . u_ab
    # is linear in t. Two rows per accepted pair (x and y), plus two anchor
    # rows fixing t_0 = (0, 0).
    translation_rows = []
    translation_rhs = []
    for pair in accepted_pairs:
        rotation_a = _rotation_matrix(np.degrees(theta[index[pair.a]]))
        u_ab = pair.transform[:, 2]
        predicted = rotation_a @ u_ab

        row_x = np.zeros(2 * n)
        row_x[2 * index[pair.a]] = -1.0
        row_x[2 * index[pair.b]] = 1.0
        translation_rows.append(row_x)
        translation_rhs.append(predicted[0])

        row_y = np.zeros(2 * n)
        row_y[2 * index[pair.a] + 1] = -1.0
        row_y[2 * index[pair.b] + 1] = 1.0
        translation_rows.append(row_y)
        translation_rhs.append(predicted[1])

    anchor_x = np.zeros(2 * n)
    anchor_x[0] = 1.0
    translation_rows.append(anchor_x)
    translation_rhs.append(0.0)

    anchor_y = np.zeros(2 * n)
    anchor_y[1] = 1.0
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
        )
        for i in range(n)
    ]

    # Canvas bounds: transform each frame's four corners, union bounding box.
    height, width = frame_size
    corners_local = np.array(
        [[0, 0], [width, 0], [width, height], [0, height]], dtype=np.float64
    )

    all_corners = []
    for placement in placements:
        matrix = placement.matrix()
        rotation, translation = matrix[:, :2], matrix[:, 2]
        all_corners.append(corners_local @ rotation.T + translation)
    all_corners = np.vstack(all_corners)

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
        )
        for p in placements
    ]

    canvas_size = (
        math.ceil(max_xy[0] - min_xy[0]),
        math.ceil(max_xy[1] - min_xy[1]),
    )

    return Layout(
        placements=shifted_placements,
        canvas_size=canvas_size,
        global_rms_px=global_rms(shifted_placements, accepted_pairs),
        used_pairs=accepted_pairs,
        strip_spread_ratio=strip_spread_ratio(shifted_placements, frame_size),
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


def strip_spread_ratio(
    placements: list[FramePlacement], frame_size: tuple[int, int]
) -> float:
    """Placed frame centres, mean-subtracted; the ratio of the second
    singular value to the first. A strip is near 0."""
    height, width = frame_size
    local_center = np.array([width / 2.0, height / 2.0])

    centers = []
    for placement in placements:
        matrix = placement.matrix()
        rotation, translation = matrix[:, :2], matrix[:, 2]
        centers.append(rotation @ local_center + translation)
    centers = np.array(centers)
    centers = centers - centers.mean(axis=0)

    singular_values = np.linalg.svd(centers, compute_uv=False)
    if singular_values[0] == 0:
        return 0.0
    return float(singular_values[1] / singular_values[0])


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
    layout: Layout, frame_size: tuple[int, int], *, probe_long_edge: int = 2000
) -> tuple[int, int, int, int]:
    """Returns (x, y, width, height) in canvas pixels.

    Build a uint8 coverage mask on a canvas downscaled so its long edge is
    at most probe_long_edge, filling each frame's transformed quadrilateral
    with cv2.fillConvexPoly. Find the largest all-covered axis-aligned
    rectangle with the standard histogram-and-stack sweep. Scale the result
    back up, then shrink it by one probe cell on every side, so the
    recorded rectangle is always inside the true valid area and never
    larger than it.
    """
    canvas_width, canvas_height = layout.canvas_size
    probe_scale = min(1.0, probe_long_edge / max(canvas_width, canvas_height))

    probe_width = max(1, round(canvas_width * probe_scale))
    probe_height = max(1, round(canvas_height * probe_scale))

    mask = np.zeros((probe_height, probe_width), dtype=np.uint8)

    height, width = frame_size
    corners_local = np.array(
        [[0, 0], [width, 0], [width, height], [0, height]], dtype=np.float64
    )
    for placement in layout.placements:
        matrix = placement.matrix()
        rotation, translation = matrix[:, :2], matrix[:, 2]
        corners_canvas = corners_local @ rotation.T + translation
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
