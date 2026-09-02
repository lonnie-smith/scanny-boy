"""The two ChArUco calibration boards and everything corner-shaped around
them (docs/GEOMETRIC_PLAN.md section 2).

`calibration/lens_calibration_targets.pdf` is the authoritative artefact:
the `BOARDS` constants here are transcribed from its embedded OpenCV
recreation lines and must match it exactly. The two boards deliberately use
different ArUco dictionaries, which makes board-format detection free — run
both detectors on the first calibration frame and take the one with more
detected corners.

The whole reason ChArUco was chosen is that every detected corner carries an
exact, known collinear-set membership through its `charucoId`: the interior
corner grid is `(squares_x - 1) x (squares_y - 1)` and `charucoIds` index it
row-major, so `row = id // (squares_x - 1)` and `col = id % (squares_x - 1)`
with no inference.

Every constant in this module is defined here and nowhere else.
"""

from __future__ import annotations

import dataclasses

import cv2
import numpy as np

from scanny_boy.detection import HIGH_PERCENTILE, LOW_PERCENTILE, LUMINANCE_WEIGHTS
from scanny_boy.events import Code
from scanny_boy.linear import decode_to_linear

# A frame with fewer corners than this is dropped from the fit with a
# warning, not a failure (section 4.2).
MIN_CORNERS_PER_FRAME = 20
# A collinear set with fewer members than this is not worth a row of the
# residual vector (section 4.3).
MIN_LINE_SET_MEMBERS = 4
# Format auto-detection: the loser must have essentially no corners. A
# winner whose runner-up exceeds this fraction of its own count is an
# ambiguous read of the frame, not a detection.
AMBIGUOUS_LOSER_FRACTION = 0.1
# cornerSubPix's search window is a quarter of the measured square pitch,
# capped: a window spanning several squares stops refining the junction
# and starts biasing it toward the local gradient centroid, which grows
# with the window rather than shrinking (measured on rendered boards).
CORNER_SUBPIX_MAX_WINDOW = 21


class BoardDetectionError(Exception):
    """Board detection failed with a stable CONTRACT.md code. The
    calibration orchestrator maps this onto the flat-field family's own
    error type without reinterpreting the code."""

    def __init__(self, code: Code, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclasses.dataclass(frozen=True)
class BoardSpec:
    key: str  # "35mm" | "6x9"
    squares_x: int  # columns, along the strip's long axis
    squares_y: int  # rows, across the strip's width
    square_length_mm: float
    marker_length_mm: float
    dictionary: str  # cv2.aruco predefined dictionary name


BOARDS: dict[str, BoardSpec] = {
    "35mm": BoardSpec("35mm", 13, 9, 3.0, 2.2, "DICT_5X5_100"),
    "6x9": BoardSpec("6x9", 21, 14, 4.0, 3.0, "DICT_5X5_250"),
}


def corner_grid(spec: BoardSpec) -> tuple[int, int]:
    """`(rows, cols)` of the interior ChArUco corner grid — the grid the
    `charucoId`s index row-major."""
    return spec.squares_y - 1, spec.squares_x - 1


def marker_count(spec: BoardSpec) -> int:
    """`floor(squares_x * squares_y / 2)` — the board's marker count, which
    the PDF states as its id range."""
    return spec.squares_x * spec.squares_y // 2


def make_board(spec: BoardSpec) -> cv2.aruco.CharucoBoard:
    dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, spec.dictionary))
    return cv2.aruco.CharucoBoard(
        (spec.squares_x, spec.squares_y),
        spec.square_length_mm,
        spec.marker_length_mm,
        dictionary,
    )


def _make_detector(spec: BoardSpec) -> cv2.aruco.CharucoDetector:
    return cv2.aruco.CharucoDetector(make_board(spec))


def percentile_stretch(luminance: np.ndarray) -> np.ndarray:
    """8-bit contrast stretch by the 0.5/99.5 percentiles — the same
    stretch `detection.build_detection_image` applies, shared by the
    full-resolution greyscale builder and the per-channel CA detection
    images."""
    low, high = np.percentile(luminance, [LOW_PERCENTILE, HIGH_PERCENTILE])
    clipped = np.clip(luminance, low, high)
    if high > low:
        normalised = (clipped - low) / (high - low) * 255.0
    else:
        normalised = np.zeros_like(clipped)
    return np.rint(normalised).astype(np.uint8)


def build_full_resolution_gray(frame: np.ndarray) -> np.ndarray:
    """The calibration detection image at native size: Rec.709 luminance and
    the 0.5/99.5 percentile stretch that `detection.build_detection_image`
    applies, but never downscaled — the sub-pixel corner positions the fit
    measures are exactly what a `DETECTION_LONG_EDGE` resize would throw
    away (section 4.2)."""
    linear = decode_to_linear(frame).astype(np.float64)
    luminance = linear @ LUMINANCE_WEIGHTS
    return percentile_stretch(luminance)


def median_corner_pitch(corners: np.ndarray) -> float:
    """Median distance from each detected corner to its nearest detected
    neighbour, in pixels — the observed square pitch, measured from this
    frame rather than assumed. The cornerSubPix window is a quarter of it."""
    points = corners.reshape(-1, 2)
    if len(points) < 2:
        return 0.0
    diff = points[:, np.newaxis, :] - points[np.newaxis, :, :]
    distances = np.sqrt((diff**2).sum(-1))
    np.fill_diagonal(distances, np.inf)
    nearest = distances.min(axis=1)
    return float(np.median(nearest))


def detect_corners(
    gray: np.ndarray, spec: BoardSpec, *, subpix: bool = True
) -> tuple[np.ndarray, np.ndarray]:
    """Detect ChArUco corners on one 8-bit greyscale frame and refine them.

    Returns `(corners, ids)`: `(N, 2)` float32 pixel coordinates and the
    matching `(N,)` charuco ids. With `subpix`, `cv2.cornerSubPix` runs with
    a search window of roughly a quarter of the median detected square
    pitch — measured from this frame's own detections, not assumed
    (section 4.2)."""
    detector = _make_detector(spec)
    charuco_corners, charuco_ids, _, _ = detector.detectBoard(gray)
    if charuco_corners is None or charuco_ids is None or len(charuco_ids) == 0:
        return np.zeros((0, 2), dtype=np.float32), np.zeros((0, 1), dtype=np.int32)

    points = charuco_corners.reshape(-1, 2).astype(np.float32)
    if subpix:
        window = min(
            round(median_corner_pitch(points) / 4), CORNER_SUBPIX_MAX_WINDOW
        )
        if window >= 2:
            # cornerSubPix wants an odd window and (N, 1, 2) float32.
            window += 1 - (window % 2)
            criteria = (
                cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER,
                30,
                0.001,
            )
            refined = cv2.cornerSubPix(
                gray, points.reshape(-1, 1, 2), (window, window), (-1, -1), criteria
            )
            points = refined.reshape(-1, 2)
    return points, charuco_ids.reshape(-1, 1).astype(np.int32)


def detect_board_format(gray: np.ndarray) -> BoardSpec:
    """Which board is in the frame — the free format detection the two
    dictionaries buy (section 2). Runs both boards' detectors on this one
    frame and takes the one with more detected corners; the winner must
    reach `MIN_CORNERS_PER_FRAME` and the loser must be essentially
    absent, or the read is ambiguous and fails
    `GEOMETRY_BOARD_NOT_DETECTED`."""
    counts: list[tuple[BoardSpec, int]] = []
    for spec in BOARDS.values():
        corners, _ = detect_corners(gray, spec, subpix=False)
        counts.append((spec, len(corners)))

    counts.sort(key=lambda pair: pair[1], reverse=True)
    winner, winner_count = counts[0]
    loser, loser_count = counts[1]
    if winner_count < MIN_CORNERS_PER_FRAME:
        raise BoardDetectionError(
            Code.GEOMETRY_BOARD_NOT_DETECTED,
            "neither calibration board was detected "
            f"({winner.key}: {winner_count} corners, {loser.key}: {loser_count})",
        )
    if loser_count > winner_count * AMBIGUOUS_LOSER_FRACTION:
        raise BoardDetectionError(
            Code.GEOMETRY_BOARD_NOT_DETECTED,
            "the board format is ambiguous: both dictionaries detected "
            f"corners ({winner.key}: {winner_count}, {loser.key}: {loser_count})",
        )
    return winner


def collinear_sets(corners: np.ndarray, ids: np.ndarray, spec: BoardSpec) -> list[np.ndarray]:
    """Group detected corners into the straight families their ids name
    (section 4.3): one set per row and per column of the corner grid, plus
    the two diagonal families (`row - col` and `row + col` constant) that
    are what constrain the principal point. Any set with at least
    `MIN_LINE_SET_MEMBERS` members is kept. Each set is returned as an
    `(N, 1, 2)` float32 array, the shape `cv2.undistortPoints` consumes."""
    _, cols = corner_grid(spec)
    rows = ids.reshape(-1) // cols
    cols_idx = ids.reshape(-1) % cols
    points = corners.reshape(-1, 2).astype(np.float32)

    sets: list[np.ndarray] = []
    for keys in (
        rows,
        cols_idx,
        rows - cols_idx,
        rows + cols_idx,
    ):
        for key in np.unique(keys):
            mask = keys == key
            if int(mask.sum()) >= MIN_LINE_SET_MEMBERS:
                sets.append(points[mask].reshape(-1, 1, 2))
    return sets
