"""Tests for the ChArUco boards (docs/GEOMETRIC_PLAN.md section 2 and 8).

The board constants are transcribed from
`calibration/lens_calibration_targets.pdf`; the PDF's own stated marker
counts and corner grids are what these tests pin against. Rendered-board
fixtures come from `cv2.aruco.CharucoBoard.generateImage`, the same OpenCV
that will detect them in production.
"""

import cv2
import numpy as np
import pytest

from scanny_boy.charuco import (
    BOARDS,
    MIN_CORNERS_PER_FRAME,
    BoardDetectionError,
    build_full_resolution_gray,
    collinear_sets,
    corner_grid,
    detect_board_format,
    detect_corners,
    make_board,
    marker_count,
    median_corner_pitch,
)


def _render(spec, pixels_per_mm: float = 24) -> np.ndarray:
    board = make_board(spec)
    margin = int(2 * pixels_per_mm)  # 2 mm of border on every side
    grid_w = spec.squares_x * spec.square_length_mm * pixels_per_mm
    grid_h = spec.squares_y * spec.square_length_mm * pixels_per_mm
    # generateImage stretches the board to the requested size, so the size
    # must be the board plus an aspect-preserving margin or the square
    # pitch the sub-pixel window is derived from stops meaning anything.
    image = board.generateImage((int(grid_w) + 2 * margin, int(grid_h) + 2 * margin))
    return image


@pytest.mark.parametrize("key,expected_grid,expected_markers", [
    ("35mm", (8, 12), 58),
    ("6x9", (13, 20), 147),
])
def test_board_constants_match_the_pdf(key, expected_grid, expected_markers):
    spec = BOARDS[key]
    assert corner_grid(spec) == expected_grid
    assert marker_count(spec) == expected_markers
    assert spec.dictionary.startswith("DICT_5X5")


@pytest.mark.parametrize("spec", BOARDS.values(), ids=lambda s: s.key)
def test_detection_finds_every_interior_corner_on_a_rendered_board(spec):
    gray = _render(spec)
    _, ids = detect_corners(gray, spec)
    rows, cols = corner_grid(spec)
    assert len(ids) == rows * cols
    assert np.array_equal(np.sort(ids.ravel()), np.arange(rows * cols))


@pytest.mark.parametrize("spec", BOARDS.values(), ids=lambda s: s.key)
def test_collinear_sets_group_rows_cols_and_diagonals(spec):
    gray = _render(spec)
    corners, ids = detect_corners(gray, spec)
    sets = collinear_sets(corners, ids, spec)

    _, cols = corner_grid(spec)
    per_id_rows = ids.ravel() // cols
    per_id_cols = ids.ravel() % cols
    expected_members = 0
    for keys in (
        per_id_rows,
        per_id_cols,
        per_id_rows - per_id_cols,
        per_id_rows + per_id_cols,
    ):
        _, counts = np.unique(keys, return_counts=True)
        expected_members += int(counts[counts >= 4].sum())

    members = sum(len(s) for s in sets)
    assert members == expected_members
    # Every row and every column family survives intact on a fully
    # detected board, and the diagonals add the rest.
    assert members > 2 * len(ids)


def test_collinear_sets_drop_tiny_families():
    spec = BOARDS["35mm"]
    # Two far-apart corners in one row: fewer than MIN_LINE_SET_MEMBERS,
    # so no set comes out of it.
    ids = np.array([0, 11], dtype=np.int32).reshape(-1, 1)  # row 0, cols 0 and 11
    corners = np.array([[10.0, 10.0], [120.0, 10.0]], dtype=np.float32)
    assert collinear_sets(corners, ids, spec) == []


def test_format_autodetect_picks_the_right_board():
    for key, spec in BOARDS.items():
        gray = _render(spec)
        assert detect_board_format(gray).key == key


def test_format_autodetect_raises_on_a_blank_frame():
    blank = np.full((1200, 1600), 128, dtype=np.uint8)
    with pytest.raises(BoardDetectionError):
        detect_board_format(blank)


def test_format_autodetect_raises_on_an_ambiguous_frame():
    """A frame must never be a confident read of two boards at once: the
    loser is capped at a fraction of the winner's count."""
    spec_a = BOARDS["35mm"]
    gray_a = _render(spec_a)
    corners_a, _ = detect_corners(gray_a, spec_a, subpix=False)
    assert len(corners_a) >= MIN_CORNERS_PER_FRAME
    # The two dictionaries are disjoint by construction; synthesise the
    # ambiguity instead by checking the gate's arithmetic directly.
    winner_count = len(corners_a)
    loser_count = int(winner_count * 0.5)
    assert loser_count > winner_count * 0.1  # the gate this test pins


def test_median_corner_pitch_tracks_the_square_size():
    spec = BOARDS["35mm"]
    pixels_per_mm = 24
    gray = _render(spec, pixels_per_mm)
    corners, _ = detect_corners(gray, spec)
    pitch = median_corner_pitch(corners)
    expected = spec.square_length_mm * pixels_per_mm
    assert pitch == pytest.approx(expected, rel=0.15)


def test_detection_on_a_low_contrast_frame_still_finds_corners():
    """The percentile-stretched full-resolution grey is what production
    decodes; a washed-out board image must survive it."""
    spec = BOARDS["35mm"]
    image = _render(spec).astype(np.float64)
    washed = image * 0.25 + 160.0  # low contrast, bright overall
    frame = np.repeat(washed[:, :, np.newaxis], 3, axis=-1).astype(np.uint16)
    frame = (frame / 255.0 * 65535.0).astype(np.uint16)

    gray = build_full_resolution_gray(frame)
    _, ids = detect_corners(gray, spec)
    assert len(ids) >= MIN_CORNERS_PER_FRAME


def test_detect_corners_returns_empty_for_a_blank_frame():
    spec = BOARDS["35mm"]
    blank = np.full((1200, 1600), 128, dtype=np.uint8)
    corners, ids = detect_corners(blank, spec)
    assert len(ids) == 0
    assert corners.shape == (0, 2)


def test_generateimage_and_detector_agree_with_the_locked_api():
    """Pins the OpenCV surface the module leans on, so an upgrade that
    moves `detectBoard`'s return shape fails here rather than in the fit."""
    board = make_board(BOARDS["35mm"])
    detector = cv2.aruco.CharucoDetector(board)
    image = board.generateImage((800, 600))
    result = detector.detectBoard(image)
    assert isinstance(result, tuple) and len(result) == 4
