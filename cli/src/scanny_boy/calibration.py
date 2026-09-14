"""The calibration orchestrator: `rig create` with
`--calibration FILE [FILE ...]`.

One rig profile record carries the optical description of one rig
configuration — radial distortion, lateral chromatic aberration, and the
human-readable calibration report. The bare-light gain map is attached
per roll via `roll set-flatfield-reference`.

Every calibration constant of the orchestrator lives here and nowhere
else; the fits' constants live in `geometry_fit.py` and `ca_fit.py`, and
the boards' in `charuco.py`.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from scanny_boy import ca_fit as ca_fit_module
from scanny_boy import charuco, concurrency, geometry_fit
from scanny_boy.charuco import BoardDetectionError, BoardSpec
from scanny_boy.events import (
    Code,
    Event,
    RigProfileSummary,
    RigProgress,
    WarningEvent,
)
from scanny_boy.linear import decode_to_linear
from scanny_boy.raw_decode import decode_raw

# Frame-count floors: fewer than the minimum fails, fewer
# than the recommended warns and proceeds.
MIN_CALIBRATION_FRAMES = 12
RECOMMENDED_CALIBRATION_FRAMES = 16
# The deterministic held-out split: sorted by filename, hold out every 4th
# (indices 3, 7, 11, ...). No randomness, no seed, no UI control.
HELDOUT_EVERY = 4


class RigError(Exception):
    """A rig profile operation failed with a stable CONTRACT.md code."""

    def __init__(self, code: Code, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclasses.dataclass(frozen=True)
class RigProfile:
    profile_id: str
    name: str
    scanny_boy_version: str
    created_at: str
    board_key: str | None = None
    geometry: dict | None = None
    chromatic_aberration: dict | None = None
    calibration_report: dict | None = None


def chromatic_aberration_scales(
    profile: RigProfile,
) -> tuple[float, float] | None:
    """The rawpy decode scales a profile carries, or None: present only in
    `"scale"` mode — a decode parameter, so it belongs to the convert
    stage's processing params."""
    if profile.chromatic_aberration is None:
        return None
    if profile.chromatic_aberration.get("mode") != "scale":
        return None
    return (
        profile.chromatic_aberration["red_scale"],
        profile.chromatic_aberration["blue_scale"],
    )


def check_geometry_frame_size(profile: RigProfile, width: int, height: int) -> None:
    """A profile's geometry is only valid for the frame dimensions it was
    fitted at."""
    geometry = profile.geometry
    if geometry is None:
        return
    if geometry["frame_width"] != width or geometry["frame_height"] != height:
        raise RigError(
            Code.GEOMETRY_FRAME_SIZE_MISMATCH,
            f"profile {profile.name!r} was fitted at "
            f"{geometry['frame_width']}x{geometry['frame_height']} but these "
            f"frames decode at {width}x{height}",
        )


def rig_profile_summary(profile: RigProfile) -> RigProfileSummary:
    """The fields a `rig` event carries."""
    return RigProfileSummary(
        profile_id=profile.profile_id,
        name=profile.name,
        created_at=profile.created_at,
        board_key=profile.board_key,
        has_geometry=profile.geometry is not None,
        chromatic_aberration_mode=(
            profile.chromatic_aberration.get("mode")
            if profile.chromatic_aberration is not None
            else None
        ),
        calibration_report=profile.calibration_report,
    )


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None).isoformat() + "Z"


def _current_scanny_boy_version() -> str:
    from scanny_boy.manifest import current_scanny_boy_version

    return current_scanny_boy_version()


EmitFn = Callable[[Event], None]


def _map_board_error(exc: BoardDetectionError) -> RigError:
    return RigError(exc.code, exc.message)


def _map_geometry_error(exc: geometry_fit.GeometryFitError) -> RigError:
    return RigError(exc.code, exc.message)


def _map_ca_error(exc: ca_fit_module.CAFitError) -> RigError:
    return RigError(exc.code, exc.message)


def _split_heldout(paths: list[Path]) -> tuple[list[Path], list[Path]]:
    """Sorted by filename, every 4th path held out.
    A rerun on the same files must produce the same profile."""
    ordered = sorted(paths, key=lambda path: path.name)
    train = [path for i, path in enumerate(ordered) if i % HELDOUT_EVERY != 3]
    heldout = [path for i, path in enumerate(ordered) if i % HELDOUT_EVERY == 3]
    return train, heldout


def _decode_workers() -> int:
    """Decoding is the bottleneck and is embarrassingly parallel; reuse
    `concurrency.resolve_worker_count`'s budget rather than inventing a
    second worker-count policy."""
    return concurrency.resolve_worker_count(concurrency.MAX_DEFAULT_WORKERS, None)


def _detect_paths(
    paths: list[Path],
    board: BoardSpec,
    workers: int,
    cancel_check: Callable[[], None] | None = None,
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Decode every path with the locked `RAW_PARAMS` and detect ChArUco
    corners on the full-resolution greyscale, in parallel."""

    def one(path: Path) -> tuple[np.ndarray, np.ndarray]:
        if cancel_check is not None:
            cancel_check()
        frame = decode_raw(path)
        gray = charuco.build_full_resolution_gray(frame.pixels)
        try:
            return charuco.detect_corners(gray, board)
        except BoardDetectionError as exc:  # defensive: the same contract path
            raise _map_board_error(exc) from exc

    if workers <= 1:
        return [one(path) for path in paths]

    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="scanny-cal")
    try:
        return list(pool.map(one, paths))
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def _detect_ca_paths(
    paths: list[Path],
    board: BoardSpec,
    full_res_detections: dict[Path, tuple[np.ndarray, np.ndarray]],
    workers: int,
    emit: EmitFn,
) -> list[dict[str, Any]]:
    """Decode every path at half size and measure ChArUco corners per
    channel. Corners are seeded once from half-size luminance (or from the
    matching full-resolution detection scaled by one half), then refined
    independently on R, G, B, and luminance with `cornerSubPix`. Returns
    per frame: the four corner/id pairs and the half-size dimensions."""
    total = len(paths)

    def one(index_path: tuple[int, Path]) -> dict[str, Any]:
        index, path = index_path
        frame = decode_raw(path, params=ca_fit_module.RAW_PARAMS_HALF_SIZE)
        height, width = frame.pixels.shape[:2]
        result: dict[str, Any] = {"width": width, "height": height}
        linear = decode_to_linear(frame.pixels).astype(np.float64)
        luminance = linear @ detection_weights()
        channels = {
            "red": charuco.percentile_stretch(frame.pixels[:, :, 0].astype(np.float64)),
            "green": charuco.percentile_stretch(
                frame.pixels[:, :, 1].astype(np.float64)
            ),
            "blue": charuco.percentile_stretch(
                frame.pixels[:, :, 2].astype(np.float64)
            ),
            "luminance": charuco.percentile_stretch(luminance.astype(np.float64)),
        }
        full_corners, full_ids = full_res_detections[path]
        try:
            detected = charuco.seed_and_refine_ca_corners(
                channels,
                board,
                full_res_corners=full_corners,
                full_res_ids=full_ids,
            )
        except BoardDetectionError as exc:  # defensive: same contract path
            raise _map_board_error(exc) from exc
        result.update(detected)
        emit(RigProgress(phase="chromatic", completed=index + 1, total=total))
        return result

    indexed = list(enumerate(paths))
    if workers <= 1:
        return [one(pair) for pair in indexed]

    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="scanny-ca")
    try:
        return list(pool.map(one, indexed))
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def detection_weights() -> np.ndarray:
    """Rec.709 luminance weights, from `detection.py` — the one place they
    are defined."""
    from scanny_boy.detection import LUMINANCE_WEIGHTS

    return LUMINANCE_WEIGHTS


def _intersect_ids(
    a: tuple[np.ndarray, np.ndarray], b: tuple[np.ndarray, np.ndarray]
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Corners of `a` and `b` kept to their common ids, id-sorted and
    row-aligned."""
    a_points, a_ids = a
    b_points, b_ids = b
    common, a_index, b_index = np.intersect1d(
        a_ids.reshape(-1), b_ids.reshape(-1), return_indices=True
    )
    return (
        a_points[a_index].reshape(-1, 2),
        b_points[b_index].reshape(-1, 2),
        common.reshape(-1, 1),
    )


def _undistort_to_normalised(
    points_px: np.ndarray,
    frame_width: int,
    frame_height: int,
    geometry: tuple[float, float, float, float] | None,
) -> np.ndarray:
    """Undistort half-size pixel corners with the accepted green
    coefficients (or zero ones when geometry was rejected — CA is still
    measurable) and convert to normalised coordinates
    relative to the principal point. Because `K_half = K_full / 2`,
    normalised coordinates are identical at both resolutions."""
    K = geometry_fit.base_camera(frame_width, frame_height)
    if geometry is not None:
        k1, k2, cx, cy = geometry
        # The fit's centre is in full-resolution pixels; this frame is
        # half size.
        K[0, 2], K[1, 2] = cx / 2.0, cy / 2.0
    fx = K[0, 0]
    cx, cy = K[0, 2], K[1, 2]
    if len(points_px) == 0:
        return np.zeros((0, 2), dtype=np.float64)
    if geometry is None:
        return (points_px - np.array([cx, cy])) / fx
    D = np.array([k1, k2, 0.0, 0.0, 0.0])
    undistorted = cv2.undistortPoints(
        points_px.reshape(-1, 1, 2).astype(np.float32), K, D, P=K
    ).reshape(-1, 2)
    return (undistorted - np.array([cx, cy])) / fx


def _geometry_dict(
    result: geometry_fit.GeometryFitResult,
    board_key: str,
    frame_width: int,
    frame_height: int,
) -> dict:
    """The profile object. `k1`/`k2` are in the OpenCV forward
    convention, so they drop straight into every consumer with no
    conversion."""
    return {
        "format_version": 1,
        "frame_width": frame_width,
        "frame_height": frame_height,
        "fx": float(max(frame_width, frame_height)),
        "fy": float(max(frame_width, frame_height)),
        "k1": result.k1,
        "k2": result.k2,
        "cx": result.cx,
        "cy": result.cy,
        "stage": result.stage,
        "gauge": "identity",
        "board_key": board_key,
    }


def _channel_dict(fit: ca_fit_module.ChannelFit) -> dict:
    return {
        "c0": fit.c0,
        "c1": fit.c1,
        "c2": fit.c2,
        "center_x": fit.center_x,
        "center_y": fit.center_y,
    }


def create_profile(
    name: str,
    calibration_paths: list[Path],
    *,
    emit: EmitFn = lambda event: None,
) -> RigProfile:
    """Fit geometry and CA from ChArUco frames and insert a rig profile.
    Raises `RigError` (`RIG_PROFILE_EXISTS`) when the name is already taken."""
    from scanny_boy.library import repo

    existing = repo.list_rig_profiles()
    if any(profile.name == name for profile in existing):
        raise RigError(
            Code.RIG_PROFILE_EXISTS, f"a profile named {name!r} already exists"
        )

    return _create_calibrated_profile(name, calibration_paths, emit)


def _create_calibrated_profile(
    name: str,
    calibration_paths: list[Path],
    emit: EmitFn,
) -> RigProfile:
    paths = sorted(calibration_paths, key=lambda path: path.name)
    if len(paths) < MIN_CALIBRATION_FRAMES:
        raise RigError(
            Code.GEOMETRY_INSUFFICIENT_FRAMES,
            f"{len(paths)} calibration frames is fewer than the minimum of "
            f"{MIN_CALIBRATION_FRAMES}",
        )
    if len(paths) < RECOMMENDED_CALIBRATION_FRAMES:
        emit(
            WarningEvent(
                code=Code.GEOMETRY_FEW_FRAMES,
                message=(
                    f"{len(paths)} calibration frames is under the "
                    f"recommended {RECOMMENDED_CALIBRATION_FRAMES}; the fit "
                    "may be less reliable"
                ),
            )
        )

    workers = _decode_workers()

    # 1. Board presence check on the first calibration frame.
    #    There is one board, so this confirms rather than chooses, and the
    #    spec is reused for every remaining frame — never re-detected.
    first = decode_raw(paths[0])
    try:
        board = charuco.detect_board(charuco.build_full_resolution_gray(first.pixels))
    except BoardDetectionError as exc:
        # The stable contract code (`GEOMETRY_BOARD_NOT_DETECTED`), not the
        # last-resort internal-error handler.
        raise _map_board_error(exc) from exc
    frame_width, frame_height = first.width, first.height
    del first
    emit(RigProgress(phase="detect", completed=1, total=len(paths)))

    # 2. Decode and detect all calibration frames at full resolution.
    detections = _detect_paths(paths, board, workers)
    emit(RigProgress(phase="detect", completed=len(paths), total=len(paths)))

    train_paths, heldout_paths = _split_heldout(paths)
    heldout_names = {path.name for path in heldout_paths}

    surviving: list[tuple[Path, tuple[np.ndarray, np.ndarray]]] = []
    for path, (corners, ids) in zip(paths, detections, strict=True):
        if len(ids) < charuco.MIN_CORNERS_PER_FRAME:
            emit(
                WarningEvent(
                    code=Code.GEOMETRY_INSUFFICIENT_FRAMES,
                    message=(
                        f"{path.name}: only {len(ids)} corners detected; the "
                        "frame is dropped from the fit"
                    ),
                )
            )
            continue
        surviving.append((path, (corners, ids)))

    if len(surviving) < MIN_CALIBRATION_FRAMES:
        raise RigError(
            Code.GEOMETRY_INSUFFICIENT_FRAMES,
            f"only {len(surviving)} calibration frames yielded "
            f"{charuco.MIN_CORNERS_PER_FRAME}+ corners; the minimum is "
            f"{MIN_CALIBRATION_FRAMES}",
        )

    # One inner list per frame: the jackknife needs frame identity back.
    # `fit_geometry` flattens for the staged fit.
    train_sets: list[list[np.ndarray]] = []
    heldout_sets: list[list[np.ndarray]] = []
    for path, (corners, ids) in surviving:
        sets = charuco.collinear_sets(corners, ids, board)
        (heldout_sets if path.name in heldout_names else train_sets).append(sets)

    # 3. Fit and gate the distortion.
    emit(RigProgress(phase="fit", completed=1, total=3))
    try:
        fit = geometry_fit.fit_geometry(
            train_sets, heldout_sets, frame_width, frame_height
        )
    except geometry_fit.GeometryFitError as exc:
        raise _map_geometry_error(exc) from exc
    emit(RigProgress(phase="fit", completed=3, total=3))

    geometry_dict: dict | None = None
    geometry_params: tuple[float, float, float, float] | None = None
    if fit.accepted:
        geometry_dict = _geometry_dict(fit, board.key, frame_width, frame_height)
        geometry_params = (fit.k1, fit.k2, fit.cx, fit.cy)
        if fit.suspect:
            emit(
                WarningEvent(
                    code=Code.GEOMETRY_MAGNITUDE_SUSPECT,
                    message=(
                        f"corner displacement {fit.corner_displacement_percent:.3f}% "
                        "of the half-diagonal is outside the expected "
                        f"{geometry_fit.MAGNITUDE_EXPECTED_MIN_PERCENT}-"
                        f"{geometry_fit.MAGNITUDE_EXPECTED_MAX_PERCENT}% "
                        "band; the fit is applied, but check the board"
                    ),
                )
            )
    else:
        emit(
            WarningEvent(
                code=Code.GEOMETRY_FIT_REJECTED,
                message=(
                    f"distortion fit rejected: {fit.rejection_reason}. "
                    "Existing profiles are unaffected: nothing "
                    "retroactively accepts a stored fit, and geometry "
                    "stays null until recalibration."
                ),
            )
        )

    # 4. Decode and detect all calibration frames at half size, per channel.
    full_res_detections = dict(zip(paths, detections, strict=True))
    ca_frames = _detect_ca_paths(paths, board, full_res_detections, workers, emit)

    half_width = ca_frames[0]["width"]
    half_height = ca_frames[0]["height"]

    def prepare(
        detection: dict,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None:
        """Per frame: intersect the three channels by id, undistort with the
        green coefficients, convert to normalised coordinates.
        Returns `(red, green_for_red, blue, green_for_blue)`
        normalised, row-aligned on the common ids — or None when no corner
        survived in all three."""

        def normalised(channel: str) -> tuple[np.ndarray, np.ndarray]:
            points, ids = detection[channel]
            points_n = _undistort_to_normalised(
                points, half_width, half_height, geometry_params
            )
            return points_n, ids.reshape(-1)

        red_n, red_ids = normalised("red")
        green_n, green_ids = normalised("green")
        blue_n, blue_ids = normalised("blue")

        red, green_for_red, _ = _intersect_ids((red_n, red_ids), (green_n, green_ids))
        blue, green_for_blue, _ = _intersect_ids(
            (blue_n, blue_ids), (green_n, green_ids)
        )
        if len(red) == 0 or len(blue) == 0:
            return None
        # The green points each pair needs, id-aligned to that pair.
        return red, green_for_red, blue, green_for_blue

    prepared: list[
        tuple[Path, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]]
    ] = []
    for path, detection in zip(paths, ca_frames, strict=True):
        frame = prepare(detection)
        if frame is not None:
            prepared.append((path, frame))

    if len(prepared) < MIN_CALIBRATION_FRAMES:
        raise RigError(
            Code.CHROMATIC_FIT_REJECTED,
            f"only {len(prepared)} calibration frames yielded corners in all "
            f"three channels; the minimum is {MIN_CALIBRATION_FRAMES}",
        )

    # 5. Fit and gate CA; decide the mode. `fit_ca` takes
    #    `(red, green_for_red, blue, green_for_blue)` per frame — each
    #    channel pair carries its own id-aligned green corners.
    train_ca = [frame for path, frame in prepared if path.name not in heldout_names]
    heldout_ca = [frame for path, frame in prepared if path.name in heldout_names]

    try:
        ca = ca_fit_module.fit_ca(
            train_ca, heldout_ca, frame_width, frame_height, geometry_params
        )
    except ca_fit_module.CAFitError as exc:
        raise _map_ca_error(exc) from exc

    chromatic_aberration_dict: dict | None = None
    if ca.accepted:
        chromatic_aberration_dict = {
            "format_version": 1,
            "mode": ca.mode,
            "red": _channel_dict(ca.red),
            "blue": _channel_dict(ca.blue),
        }
        if ca.mode == "scale":
            assert ca.red_scale is not None and ca.blue_scale is not None
            chromatic_aberration_dict["red_scale"] = ca.red_scale
            chromatic_aberration_dict["blue_scale"] = ca.blue_scale
    else:
        emit(
            WarningEvent(
                code=Code.CHROMATIC_FIT_REJECTED,
                message=f"chromatic aberration fit rejected: {ca.rejection_reason}",
            )
        )

    # The detection-channel measurement: the Rec.709
    # luminance image's corner displacement from green, pooled over the
    # half-size frames and reported in full-resolution pixels. Gates
    # nothing; settles the detect-on-green question later with a number.
    luminance_norm = [
        _undistort_to_normalised(
            ca_frames[index]["luminance"][0],
            half_width,
            half_height,
            geometry_params,
        )
        for index in range(len(ca_frames))
    ]
    green_norm_by_frame = [
        _undistort_to_normalised(
            ca_frames[index]["green"][0], half_width, half_height, geometry_params
        )
        for index in range(len(ca_frames))
    ]
    detection_channel_ca = ca_fit_module.channel_ca_px(
        green_norm_by_frame, luminance_norm, float(max(frame_width, frame_height))
    )

    # 6. Assemble the report and insert the row. The frame
    #    counts describe the fit that actually happened: the ones that
    #    survived corner detection and reached the train/heldout split.
    surviving_names = {path.name for path, _ in surviving}
    surviving_train = [p for p in train_paths if p.name in surviving_names]
    surviving_heldout = [p for p in heldout_paths if p.name in surviving_names]
    report = {
        "frames_total": len(paths),
        "frames_fit": len(surviving_train),
        "frames_heldout": len(surviving_heldout),
        "heldout_frame_names": sorted(heldout_names),
        "corners_detected_median": int(
            np.median([len(ids) for _, (_, ids) in surviving])
        ),
        "distortion": {
            "heldout_rms_px_before": fit.heldout_rms_before,
            "heldout_rms_px_after": fit.heldout_rms_after,
            "corner_displacement_px": fit.corner_displacement_px,
            "corner_displacement_percent": fit.corner_displacement_percent,
            "accepted": fit.accepted,
            "rejection_reason": fit.rejection_reason,
            "stage_heldout_rms_px": fit.stage_heldout_rms,
            # The stability gate's own statistic and the threshold it was
            # judged against, so a stored profile reads without knowing
            # which build wrote it.
            "jackknife_corner_px_mean": fit.jackknife_corner_px_mean,
            "jackknife_corner_px_se": fit.jackknife_corner_px_se,
            "jackknife_relative_se": fit.jackknife_relative_se,
            "jackknife_frames": fit.jackknife_frames,
            "max_relative_se": geometry_fit.GEOMETRY_MAX_RELATIVE_SE,
        },
        "chromatic_aberration": {
            "heldout_misregistration_px_before": ca.misregistration_before_px,
            "heldout_misregistration_px_after": ca.misregistration_after_px,
            "radial_term_px_at_corner": ca.radial_term_px_at_corner,
            "mode": ca.mode,
            "accepted": ca.accepted,
            "rejection_reason": ca.rejection_reason,
        },
        "detection_channel_ca_px": detection_channel_ca,
    }

    profile = RigProfile(
        profile_id=str(uuid.uuid4()),
        name=name,
        scanny_boy_version=_current_scanny_boy_version(),
        created_at=_now_iso(),
        board_key=board.key,
        geometry=geometry_dict,
        chromatic_aberration=chromatic_aberration_dict,
        calibration_report=report,
    )
    from scanny_boy.library import repo

    repo.save_rig_profile(profile)
    return profile
