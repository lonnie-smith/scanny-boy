"""Tests for the calibration orchestrator (docs/GEOMETRIC_PLAN.md section 8).

`decode_raw` is replaced — not rawpy itself, but the calibration module's
decode seam — with a renderer that produces synthetic ChArUco frames with
known distortion and CA baked in. The project rule "do not mock rawpy's
decoding" stands: the pixel-level decode behaviour is covered against real
NEFs elsewhere (sample_nef_support-based tests); what this module tests is
the orchestrator's ordering, splits, gating, and provenance recording,
which need controlled inputs the six real sample NEFs cannot provide
(there are not 12 of them, and none carries a board).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import cv2
import numpy as np
import pytest

from scanny_boy import calibration, charuco, flatfield, geometry_fit
from scanny_boy.raw_decode import DecodedFrame

BOARD = charuco.BOARDS["35mm"]
# Mid-format dimensions: the plumb-line sag scales with fx while the
# magnitude percentage does not, so a small synthetic frame cannot clear
# the absolute 0.3 px gate at any distortion inside the hard magnitude
# band. 4536 px with k1 = -0.008 lands in the suspect band with enough sag.
FULL_W, FULL_H = 4536, 3024
# Strong enough that the plumb-line sag clears the absolute 0.3 px gate on
# top of the synthetic detection floor (cornerSubPix on bilinearly
# resampled corners zigzags by ~0.2 px); still inside the hard magnitude
# band, so the fit lands in the suspect-accepted path.
TRUE_K1 = -0.02
RED_SCALE_OBSERVED = 1.0004  # red radius = c0 * green radius
BLUE_SCALE_OBSERVED = 0.9996
REFERENCE_NAME = "reference-bare.NEF"
# The board fills the frame: square pitch 315 px, marker 231 px.
PPM = 105  # render pixels per mm


def _render_ideal_board() -> np.ndarray:
    board = charuco.make_board(BOARD)
    margin = 2
    width = int((BOARD.squares_x * BOARD.square_length_mm + 2 * margin) * PPM)
    height = int((BOARD.squares_y * BOARD.square_length_mm + 2 * margin) * PPM)
    image = board.generateImage((width, height))
    # A real board printed and photographed has soft edges; the raw render
    # is binary, and cornerSubPix on aliased binary edges carries a large
    # pseudo-random bias that drowns the distortion signal.
    image = cv2.GaussianBlur(image, (0, 0), 5.0)
    return np.repeat(image[:, :, np.newaxis], 3, axis=2).astype(np.uint16) * 257


def _distortion_maps(size: tuple[int, int], scale: float) -> tuple[np.ndarray, np.ndarray]:
    """The inverse map the synthetic observation is sampled through:
    `observed(q) = ideal(map(q))`. The green channel samples
    `map = d^-1`; a CA channel's scale lives in *undistorted* space — the
    model's own construction (docs/GEOMETRIC_PLAN.md section 4.6) — so its
    map is `c + (d^-1(q) - c) / scale` about the principal point.
    float32 throughout: at this frame size float64 intermediates would
    needlessly double the test's transient memory."""
    width, height = size
    K = geometry_fit.base_camera(width, height)
    D = np.array([TRUE_K1, 0.0, 0.0, 0.0, 0.0])
    xs = np.arange(width, dtype=np.float32)
    ys = np.arange(height, dtype=np.float32)
    grid = np.empty((height, width, 2), dtype=np.float32)
    grid[..., 0] = xs[np.newaxis, :]
    grid[..., 1] = ys[:, np.newaxis]
    undistorted = (
        cv2.undistortPoints(grid.reshape(-1, 1, 2), K, D, P=K)
        .reshape(height, width, 2)
        .astype(np.float32)
    )
    principal = np.array([K[0, 2], K[1, 2]], dtype=np.float32)
    scaled = principal + (undistorted - principal) / scale
    return scaled[:, :, 0], scaled[:, :, 1]


def _synthetic_frame(shift: tuple[int, int]) -> np.ndarray:
    """One calibration frame: the ideal board shifted, then sampled through
    the distortion map, red and blue radially scaled about the principal
    point."""
    ideal = _render_ideal_board()
    M = np.array([[1.0, 0.0, shift[0]], [0.0, 1.0, shift[1]]], dtype=np.float64)
    shifted = cv2.warpAffine(
        ideal, M, (FULL_W, FULL_H), borderMode=cv2.BORDER_CONSTANT, borderValue=0
    ).astype(np.float32)

    frame = np.zeros((FULL_H, FULL_W, 3), dtype=np.uint16)
    for channel, scale in ((0, RED_SCALE_OBSERVED), (1, 1.0), (2, BLUE_SCALE_OBSERVED)):
        map_x, map_y = _distortion_maps((FULL_W, FULL_H), scale)
        frame[:, :, channel] = np.clip(
            cv2.remap(
                shifted[:, :, channel],
                map_x,
                map_y,
                cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_CONSTANT,
                borderValue=0,
            ),
            0,
            65535,
        ).astype(np.uint16)
    return frame


def _reference_frame() -> np.ndarray:
    """A bare light source: mild vignette, no structure."""
    ys, xs = np.mgrid[0:FULL_H, 0:FULL_W]
    r = np.hypot((xs - FULL_W / 2) / FULL_W, (ys - FULL_H / 2) / FULL_H)
    level = (1.0 - 0.1 * r) * 40000.0
    return np.repeat(level[:, :, np.newaxis], 3, axis=2).astype(np.uint16)


class FakeDecoder:
    """Stands in for `decode_raw` at the calibration module's seam and
    records every call, so the tests can assert on the decode that actually
    happened rather than on pixels."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[float, float] | None, bool]] = []
        frames = [_synthetic_frame((2 * i, 3 * i)) for i in range(4)]
        self.calibration_full = [
            frames[i % len(frames)] for i in range(calibration.MIN_CALIBRATION_FRAMES)
        ]
        self.reference_full = _reference_frame()

    def __call__(
        self,
        path,
        *,
        chromatic_aberration: tuple[float, float] | None = None,
        params: dict | None = None,
    ) -> DecodedFrame:
        name = Path(path).name
        half = params is not None and params.get("half_size") is True
        self.calls.append((name, chromatic_aberration, half))
        if name == REFERENCE_NAME:
            source = self.reference_full
        else:
            index = int(name.replace("cal-", "").replace(".NEF", ""))
            source = self.calibration_full[index]
        if half:
            small = cv2.resize(
                source, (FULL_W // 2, FULL_H // 2), interpolation=cv2.INTER_AREA
            )
            return DecodedFrame(pixels=small, width=FULL_W // 2, height=FULL_H // 2)
        return DecodedFrame(pixels=source, width=FULL_W, height=FULL_H)


@pytest.fixture()
def calibrated_profile(tmp_path, monkeypatch):
    """A profile created from 12 synthetic calibration frames, with the
    events it emitted alongside."""
    monkeypatch.setenv("SCANNY_BOY_LIBRARY_DB", str(tmp_path / "library.db"))
    from scanny_boy.library import db as library_db

    library_db.reset_engine_cache()
    decoder = FakeDecoder()
    monkeypatch.setattr(calibration, "decode_raw", decoder)

    reference = tmp_path / REFERENCE_NAME
    paths = [tmp_path / f"cal-{i}.NEF" for i in range(calibration.MIN_CALIBRATION_FRAMES)]

    events: list = []
    try:
        profile = calibration.create_profile(
            reference,
            "Calibrated",
            paths,
            emit=events.append,
        )
        yield decoder, profile, events
    finally:
        library_db.reset_engine_cache()


def test_no_calibration_frames_produces_todays_profile(tmp_path, monkeypatch):
    """A profile with a reference and no calibration frames is exactly
    today's flat-field profile: four Nones, today's build_params, and a
    reference decode with no CA scales (section 6)."""
    monkeypatch.setenv("SCANNY_BOY_LIBRARY_DB", str(tmp_path / "library.db"))
    from scanny_boy.library import db as library_db

    library_db.reset_engine_cache()
    decoder = FakeDecoder()
    monkeypatch.setattr(calibration, "decode_raw", decoder)
    monkeypatch.setattr(flatfield, "decode_raw", decoder)
    reference = tmp_path / REFERENCE_NAME
    try:
        profile = calibration.create_profile(reference, "Plain")
        plain = calibration._create_gain_only_profile(reference, "Plain 2")

        assert profile.geometry is None
        assert profile.chromatic_aberration is None
        assert profile.calibration_report is None
        assert profile.board_key is None
        assert profile.params == flatfield.build_params()
        # Structurally identical to the historical code path.
        def strip(profile):
            return dataclasses.replace(
                profile,
                profile_id="",
                name="",
                created_at="",
                gain_map_path="",
                gain_map_sha256="",
            )

        assert strip(profile) == strip(plain)
        assert all(call[1] is None for call in decoder.calls)
    finally:
        library_db.reset_engine_cache()


def test_scale_mode_decodes_the_reference_with_the_ca_scales(calibrated_profile):
    """The load-bearing ordering constraint (section 4.7): in "scale" mode
    the flat-field reference is decoded with the same CA scales production
    will use — asserted on the recorded provenance, not on pixels."""
    decoder, profile, _ = calibrated_profile

    assert profile.chromatic_aberration is not None
    assert profile.chromatic_aberration["mode"] == "scale"
    red_scale = profile.chromatic_aberration["red_scale"]
    blue_scale = profile.chromatic_aberration["blue_scale"]
    assert profile.params["chromatic_aberration_scales"] == [red_scale, blue_scale]

    reference_calls = [
        call for call in decoder.calls if call[0] == REFERENCE_NAME and not call[2]
    ]
    assert len(reference_calls) == 1
    assert reference_calls[0][1] == (red_scale, blue_scale)


def test_scales_are_reciprocals_of_c0(calibrated_profile):
    """Section 3.3: the decoder is told what to *multiply* red by to put it
    back — the reciprocal of the fitted c0."""
    _, profile, _ = calibrated_profile
    ca = profile.chromatic_aberration
    assert ca["red_scale"] == pytest.approx(1 / ca["red"]["c0"], rel=1e-6)
    assert ca["blue_scale"] == pytest.approx(1 / ca["blue"]["c0"], rel=1e-6)


def test_geometry_and_report_are_recorded(calibrated_profile):
    _, profile, _ = calibrated_profile
    geometry = profile.geometry
    assert geometry is not None
    assert geometry["format_version"] == 1
    assert geometry["gauge"] == "identity"
    assert geometry["frame_width"] == FULL_W
    assert geometry["frame_height"] == FULL_H
    assert geometry["fx"] == float(max(FULL_W, FULL_H))
    assert geometry["stage"] in ("k1", "k1k2", "k1k2c")
    assert geometry["board_key"] == "35mm"

    report = profile.calibration_report
    assert report["frames_total"] == calibration.MIN_CALIBRATION_FRAMES
    assert report["frames_fit"] + report["frames_heldout"] == report["frames_total"]
    assert report["corners_detected_median"] >= charuco.MIN_CORNERS_PER_FRAME
    assert len(report["heldout_frame_names"]) == report["frames_heldout"]
    assert report["distortion"]["accepted"] is True
    assert report["chromatic_aberration"]["accepted"] is True
    assert "detection_channel_ca_px" in report


def test_heldout_split_is_deterministic_across_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("SCANNY_BOY_LIBRARY_DB", str(tmp_path / "library.db"))
    from scanny_boy.library import db as library_db

    library_db.reset_engine_cache()
    decoder = FakeDecoder()
    monkeypatch.setattr(calibration, "decode_raw", decoder)
    reference = tmp_path / REFERENCE_NAME
    paths = [tmp_path / f"cal-{i}.NEF" for i in range(calibration.MIN_CALIBRATION_FRAMES)]
    try:
        first = calibration.create_profile(reference, "One", paths)
        second = calibration.create_profile(reference, "Two", paths)
        assert (
            first.calibration_report["heldout_frame_names"]
            == second.calibration_report["heldout_frame_names"]
        )
        assert first.geometry["k1"] == pytest.approx(second.geometry["k1"], abs=1e-9)
        assert first.chromatic_aberration == second.chromatic_aberration
    finally:
        library_db.reset_engine_cache()


def test_progress_events_carry_every_phase(calibrated_profile):
    _, _, events = calibrated_profile
    from scanny_boy.events import FlatFieldProgress

    phases = [e.phase for e in events if isinstance(e, FlatFieldProgress)]
    assert set(phases) == {"detect", "fit", "chromatic", "reference"}
    assert all(e.run_id is None for e in events if isinstance(e, FlatFieldProgress))


def test_too_few_calibration_frames_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("SCANNY_BOY_LIBRARY_DB", str(tmp_path / "library.db"))
    from scanny_boy.events import Code
    from scanny_boy.flatfield import FlatFieldError
    from scanny_boy.library import db as library_db

    library_db.reset_engine_cache()
    decoder = FakeDecoder()
    monkeypatch.setattr(calibration, "decode_raw", decoder)
    reference = tmp_path / REFERENCE_NAME
    paths = [tmp_path / f"cal-{i}.NEF" for i in range(calibration.MIN_CALIBRATION_FRAMES - 1)]
    try:
        with pytest.raises(FlatFieldError) as excinfo:
            calibration.create_profile(reference, "Few", paths)
        assert excinfo.value.code == Code.GEOMETRY_INSUFFICIENT_FRAMES
    finally:
        library_db.reset_engine_cache()
