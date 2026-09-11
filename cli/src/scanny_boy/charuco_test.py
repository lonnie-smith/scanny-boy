"""Tests for the ChArUco boards.

The board constants are transcribed from
`calibration/lens_calibration_targets.pdf` (drawn by
`cli/tools/generate_charuco_board.py`); that board's marker count and
corner grid are what these tests pin against. Rendered-board
fixtures come from `cv2.aruco.CharucoBoard.generateImage`, the same OpenCV
that will detect them in production.
"""

import cv2
import numpy as np
import pytest

from scanny_boy.charuco import (
    BOARD,
    CHARUCO_PERSPECTIVE_MARGIN,
    MIN_CORNERS_PER_FRAME,
    BoardDetectionError,
    _make_detector,
    build_full_resolution_gray,
    collinear_sets,
    corner_grid,
    detect_board,
    detect_corners,
    make_board,
    marker_count,
    median_corner_pitch,
    percentile_stretch,
    seed_and_refine_ca_corners,
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


def test_board_constants_match_the_pdf():
    assert (BOARD.squares_x, BOARD.squares_y) == (50, 38)
    assert (BOARD.square_length_mm, BOARD.marker_length_mm) == (2.0, 1.5)
    assert corner_grid(BOARD) == (37, 49)
    assert marker_count(BOARD) == 950
    assert BOARD.dictionary == "DICT_4X4_1000"


def test_the_board_fits_its_dictionary():
    """One ArUco marker per white square, and the dictionary holds 1000.
    A board edited past that ceiling cannot be built at all, so this is the
    constraint that caps the board's physical size at a given pitch."""
    assert marker_count(BOARD) <= 1000


def test_detection_finds_every_interior_corner_on_a_rendered_board():
    spec = BOARD
    gray = _render(spec)
    _, ids = detect_corners(gray, spec)
    rows, cols = corner_grid(spec)
    assert len(ids) == rows * cols
    assert np.array_equal(np.sort(ids.ravel()), np.arange(rows * cols))


def test_detector_uses_the_measured_perspective_margin():
    params = _make_detector(BOARD).getDetectorParameters()
    assert params.perspectiveRemoveIgnoredMarginPerCell == pytest.approx(
        CHARUCO_PERSPECTIVE_MARGIN
    )
    # The constant exists because cv2's default is wrong for this board.
    default = cv2.aruco.DetectorParameters().perspectiveRemoveIgnoredMarginPerCell
    assert CHARUCO_PERSPECTIVE_MARGIN != pytest.approx(default)


def test_a_soft_edged_board_detects_every_corner_with_the_measured_margin():
    """A photographed print has soft cell edges, not the binary render's.
    Blurred by a quarter of a module (2.5 px at 40 px/mm, a 10 px module),
    cv2's default margin reads the blurred cell borders as bits and loses
    markers; the measured margin samples inside them and finds the whole
    grid. The second half proves this fixture exercises the regression
    rather than passing under either margin."""
    gray = cv2.GaussianBlur(_render(BOARD, pixels_per_mm=40), (0, 0), 2.5)
    rows, cols = corner_grid(BOARD)

    _, ids = detect_corners(gray, BOARD)
    assert np.array_equal(np.sort(ids.ravel()), np.arange(rows * cols))

    default_detector = cv2.aruco.CharucoDetector(make_board(BOARD))
    _, default_ids, _, _ = default_detector.detectBoard(gray)
    assert default_ids is None or len(default_ids) < rows * cols


def test_collinear_sets_group_rows_cols_and_diagonals():
    spec = BOARD
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
    spec = BOARD
    # Two far-apart corners in one row: fewer than MIN_LINE_SET_MEMBERS,
    # so no set comes out of it.
    ids = np.array([0, 11], dtype=np.int32).reshape(-1, 1)  # row 0, cols 0 and 11
    corners = np.array([[10.0, 10.0], [120.0, 10.0]], dtype=np.float32)
    assert collinear_sets(corners, ids, spec) == []


def test_detect_board_confirms_a_rendered_board():
    assert detect_board(_render(BOARD)).key == BOARD.key


def test_detect_board_raises_on_a_blank_frame():
    blank = np.full((1200, 1600), 128, dtype=np.uint8)
    with pytest.raises(BoardDetectionError):
        detect_board(blank)


def test_detect_board_raises_below_the_corner_floor():
    """A frame holding only a scrap of board is not a detection. A crop a
    few squares across must fail rather than hand the fit a handful of
    corners."""
    gray = _render(BOARD)[:200, :200]
    corners, _ = detect_corners(gray, BOARD, subpix=False)
    assert len(corners) < MIN_CORNERS_PER_FRAME
    with pytest.raises(BoardDetectionError):
        detect_board(gray)


def test_median_corner_pitch_tracks_the_square_size():
    spec = BOARD
    pixels_per_mm = 24
    gray = _render(spec, pixels_per_mm)
    corners, _ = detect_corners(gray, spec)
    pitch = median_corner_pitch(corners)
    expected = spec.square_length_mm * pixels_per_mm
    assert pitch == pytest.approx(expected, rel=0.15)


def test_detection_on_a_low_contrast_frame_still_finds_corners():
    """The percentile-stretched full-resolution grey is what production
    decodes; a washed-out board image must survive it."""
    spec = BOARD
    image = _render(spec).astype(np.float64)
    washed = image * 0.25 + 160.0  # low contrast, bright overall
    frame = np.repeat(washed[:, :, np.newaxis], 3, axis=-1).astype(np.uint16)
    frame = (frame / 255.0 * 65535.0).astype(np.uint16)

    gray = build_full_resolution_gray(frame)
    _, ids = detect_corners(gray, spec)
    assert len(ids) >= MIN_CORNERS_PER_FRAME


def test_detect_corners_returns_empty_for_a_blank_frame():
    spec = BOARD
    blank = np.full((1200, 1600), 128, dtype=np.uint8)
    corners, ids = detect_corners(blank, spec)
    assert len(ids) == 0
    assert corners.shape == (0, 2)


def test_half_size_ca_detection_seeds_from_full_res_at_15_px_per_module():
    """At the scanning-range floor (15 px/module), half-size luminance
    ArUco can miss corners that full-res detection still finds; the CA
    path must seed from the scaled full-res set and refine per channel."""
    # 1.5 mm marker / 6 modules = 0.25 mm per module; 15 px/module => 60 px/mm.
    pixels_per_mm = 60.0
    full_gray = _render(BOARD, pixels_per_mm)
    full_corners, full_ids = detect_corners(full_gray, BOARD)

    half_gray = cv2.resize(
        full_gray, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA
    )
    stretched = percentile_stretch(half_gray.astype(np.float64))
    channels = {
        "red": stretched,
        "green": stretched,
        "blue": stretched,
        "luminance": stretched,
    }

    detected = seed_and_refine_ca_corners(
        channels,
        BOARD,
        full_res_corners=full_corners,
        full_res_ids=full_ids,
    )
    for name in ("red", "green", "blue", "luminance"):
        _, ids = detected[name]
        assert len(ids) >= MIN_CORNERS_PER_FRAME, name

    blank_lum = np.full_like(channels["luminance"], 128, dtype=np.uint8)
    fallback = seed_and_refine_ca_corners(
        {**channels, "luminance": blank_lum},
        BOARD,
        full_res_corners=full_corners,
        full_res_ids=full_ids,
    )
    for name in ("red", "green", "blue", "luminance"):
        _, ids = fallback[name]
        assert len(ids) >= MIN_CORNERS_PER_FRAME, name


def test_generateimage_and_detector_agree_with_the_locked_api():
    """Pins the OpenCV surface the module leans on, so an upgrade that
    moves `detectBoard`'s return shape fails here rather than in the fit."""
    board = make_board(BOARD)
    detector = cv2.aruco.CharucoDetector(board)
    image = board.generateImage((800, 600))
    result = detector.detectBoard(image)
    assert isinstance(result, tuple) and len(result) == 4
