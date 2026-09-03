"""The calibration orchestrator: `flatfield create` with
`--calibration FILE [FILE ...]` (docs/GEOMETRIC_PLAN.md section 4).

One profile record carries the whole optical description of one rig
configuration — gain map, radial distortion, lateral chromatic
aberration, and the human-readable calibration report. The ordering here
is load-bearing (section 4.7): in `"scale"` mode the flat-field reference
must be decoded with the *same* CA scales production will use, or the gain
map and the frames disagree about geometry.

`flatfield.create_profile` moved here; `flatfield.py` went back to owning
only the gain map. Every calibration constant of the orchestrator lives
here and nowhere else; the fits' constants live in `geometry_fit.py` and
`ca_fit.py`, and the boards' in `charuco.py`.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from scanny_boy import ca_fit as ca_fit_module
from scanny_boy import charuco, concurrency, flatfield, geometry_fit
from scanny_boy.charuco import BoardDetectionError, BoardSpec
from scanny_boy.events import (
    Code,
    Event,
    FlatFieldProgress,
    WarningEvent,
)
from scanny_boy.flatfield import FlatFieldError, FlatFieldProfile
from scanny_boy.linear import decode_to_linear
from scanny_boy.raw_decode import decode_raw

# Section 4.1's frame-count floors: fewer than the minimum fails, fewer
# than the recommended warns and proceeds.
MIN_CALIBRATION_FRAMES = 12
RECOMMENDED_CALIBRATION_FRAMES = 16
# The deterministic held-out split: sorted by filename, hold out every 4th
# (indices 3, 7, 11, ...). No randomness, no seed, no UI control.
HELDOUT_EVERY = 4


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.UTC).replace(tzinfo=None).isoformat() + "Z"
    )


def _current_scanny_boy_version() -> str:
    from scanny_boy.manifest import current_scanny_boy_version

    return current_scanny_boy_version()


EmitFn = Callable[[Event], None]


def _map_board_error(exc: BoardDetectionError) -> FlatFieldError:
    return FlatFieldError(exc.code, exc.message)


def _map_geometry_error(exc: geometry_fit.GeometryFitError) -> FlatFieldError:
    return FlatFieldError(exc.code, exc.message)


def _map_ca_error(exc: ca_fit_module.CAFitError) -> FlatFieldError:
    return FlatFieldError(exc.code, exc.message)


def _split_heldout(paths: list[Path]) -> tuple[list[Path], list[Path]]:
    """The section 4.1 split: sorted by filename, every 4th path held out.
    A rerun on the same files must produce the same profile."""
    ordered = sorted(paths, key=lambda path: path.name)
    train = [path for i, path in enumerate(ordered) if i % HELDOUT_EVERY != 3]
    heldout = [path for i, path in enumerate(ordered) if i % HELDOUT_EVERY == 3]
    return train, heldout


def _decode_workers() -> int:
    """Decoding is the bottleneck and is embarrassingly parallel; reuse
    `concurrency.resolve_worker_count`'s budget rather than inventing a
    second worker-count policy (section 4.8)."""
    return concurrency.resolve_worker_count(concurrency.MAX_DEFAULT_WORKERS, None)


def _detect_paths(
    paths: list[Path],
    board: BoardSpec,
    workers: int,
    cancel_check: Callable[[], None] | None = None,
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Decode every path with the locked `RAW_PARAMS` and detect ChArUco
    corners on the full-resolution greyscale, in parallel (section 4.2)."""

    def one(path: Path) -> tuple[np.ndarray, np.ndarray]:
        if cancel_check is not None:
            cancel_check()
        frame = decode_raw(path)
        gray = charuco.build_full_resolution_gray(frame.pixels)
        return charuco.detect_corners(gray, board)

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
    workers: int,
    emit: EmitFn,
) -> list[dict[str, Any]]:
    """Decode every path at half size, per channel (section 4.6), and detect
    ChArUco corners independently on R, G, B plus the Rec.709 luminance
    image. Returns per frame: the four corner/id pairs and the half-size
    dimensions."""
    total = len(paths)

    def one(index_path: tuple[int, Path]) -> dict[str, Any]:
        index, path = index_path
        frame = decode_raw(path, params=ca_fit_module.RAW_PARAMS_HALF_SIZE)
        height, width = frame.pixels.shape[:2]
        result: dict[str, Any] = {"width": width, "height": height}
        linear = decode_to_linear(frame.pixels).astype(np.float64)
        for name, image in (
            ("red", frame.pixels[:, :, 0]),
            ("green", frame.pixels[:, :, 1]),
            ("blue", frame.pixels[:, :, 2]),
            # The detection-channel measurement: Rec.709 luminance, weighted
            # exactly as `detection.build_detection_image` weights it.
            ("luminance", linear @ detection_weights()),
        ):
            gray = charuco.percentile_stretch(image.astype(np.float64))
            result[name] = charuco.detect_corners(gray, board)
        emit(FlatFieldProgress(phase="chromatic", completed=index + 1, total=total))
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
    measurable, section 4.6 step 3) and convert to normalised coordinates
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
    if geometry is None:
        return (points_px - np.array([cx, cy])) / fx
    D = np.array([k1, k2, 0.0, 0.0, 0.0])
    undistorted = cv2.undistortPoints(
        points_px.reshape(-1, 1, 2).astype(np.float32), K, D, P=K
    ).reshape(-1, 2)
    return (undistorted - np.array([cx, cy])) / fx


def _geometry_dict(result: geometry_fit.GeometryFitResult, board_key: str,
                   frame_width: int, frame_height: int) -> dict:
    """The section 3.2 profile object. `k1`/`k2` are in the OpenCV forward
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
    reference: Path,
    name: str,
    calibration_paths: list[Path] | None = None,
    *,
    emit: EmitFn = lambda event: None,
) -> FlatFieldProfile:
    """Decode, build, save, and insert — the one path `flatfield create`
    and the app's New Profile sheet both use. Raises the reference decode's
    own errors for a bad NEF and `FlatFieldError`
    (`FLATFIELD_PROFILE_EXISTS`) when the name is already taken.

    `calibration_paths` is empty or None for today's flat-field-only
    profile: byte-identical to the pre-calibration code path. With frames,
    the full section 4 order of operations runs: board detect, full-res
    detect, distortion fit, half-size per-channel CA fit, mode decision,
    and only then the reference decode — with the CA scales applied to it
    when the mode is `"scale"` (section 4.7)."""
    from scanny_boy.library import repo

    existing = repo.list_flatfield_profiles()
    if any(profile.name == name for profile in existing):
        raise FlatFieldError(
            Code.FLATFIELD_PROFILE_EXISTS, f"a profile named {name!r} already exists"
        )

    if not calibration_paths:
        return _create_gain_only_profile(reference, name)
    return _create_calibrated_profile(reference, name, calibration_paths, emit)


def _create_gain_only_profile(reference: Path, name: str) -> FlatFieldProfile:
    """The pre-calibration path, unchanged: decode with `RAW_PARAMS`, build
    the gain map, insert. `calibration_test.py` pins this as byte-identical
    to the historical code path."""
    gain_map, width, height = flatfield.build_gain_map(reference)
    profile_id = str(uuid.uuid4())
    path, sha256 = flatfield.save_gain_map(profile_id, gain_map)
    profile = flatfield.FlatFieldProfile(
        profile_id=profile_id,
        name=name,
        gain_map_path=str(path),
        gain_map_sha256=sha256,
        source_path=str(reference),
        reference_width=width,
        reference_height=height,
        params=flatfield.build_params(),
        scanny_boy_version=_current_scanny_boy_version(),
        created_at=_now_iso(),
    )
    from scanny_boy.library import repo

    repo.save_flatfield_profile(profile)
    return profile


def _create_calibrated_profile(
    reference: Path,
    name: str,
    calibration_paths: list[Path],
    emit: EmitFn,
) -> FlatFieldProfile:
    paths = sorted(calibration_paths, key=lambda path: path.name)
    if len(paths) < MIN_CALIBRATION_FRAMES:
        raise FlatFieldError(
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

    # 1. Board format detection from the first calibration frame (section
    #    2): both dictionaries race on one frame and the winner is reused
    #    for every remaining frame — never re-detected per frame.
    first = decode_raw(paths[0])
    board = charuco.detect_board_format(
        charuco.build_full_resolution_gray(first.pixels)
    )
    frame_width, frame_height = first.width, first.height
    del first
    emit(FlatFieldProgress(phase="detect", completed=1, total=len(paths)))

    # 2. Decode and detect all calibration frames at full resolution
    #    (section 4.7 step 2).
    detections = _detect_paths(paths, board, workers)
    emit(FlatFieldProgress(phase="detect", completed=len(paths), total=len(paths)))

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
        raise FlatFieldError(
            Code.GEOMETRY_INSUFFICIENT_FRAMES,
            f"only {len(surviving)} calibration frames yielded "
            f"{charuco.MIN_CORNERS_PER_FRAME}+ corners; the minimum is "
            f"{MIN_CALIBRATION_FRAMES}",
        )

    train_sets: list[np.ndarray] = []
    heldout_sets: list[np.ndarray] = []
    for path, (corners, ids) in surviving:
        sets = charuco.collinear_sets(corners, ids, board)
        (heldout_sets if path.name in heldout_names else train_sets).extend(sets)

    # 3. Fit and gate the distortion (section 4.4).
    emit(FlatFieldProgress(phase="fit", completed=1, total=3))
    try:
        fit = geometry_fit.fit_geometry(
            train_sets, heldout_sets, frame_width, frame_height
        )
    except geometry_fit.GeometryFitError as exc:
        raise _map_geometry_error(exc) from exc
    emit(FlatFieldProgress(phase="fit", completed=3, total=3))

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
                        "of the half-diagonal is outside the expected 0.03-0.2% "
                        "band; the fit is applied, but check the board"
                    ),
                )
            )
    else:
        emit(
            WarningEvent(
                code=Code.GEOMETRY_FIT_REJECTED,
                message=f"distortion fit rejected: {fit.rejection_reason}",
            )
        )

    # 4. Decode and detect all calibration frames at half size, per channel
    #    (section 4.6).
    ca_frames = _detect_ca_paths(paths, board, workers, emit)

    half_width = ca_frames[0]["width"]
    half_height = ca_frames[0]["height"]

    def prepare(detection: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
        """Per frame: intersect the three channels by id, undistort with the
        green coefficients, convert to normalised coordinates (section 4.6
        steps 2-4). Returns `(red, green, blue)` normalised, row-aligned on
        the common ids — or None when no corner survived in all three."""
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

    prepared: list[tuple[Path, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]]] = []
    for path, detection in zip(paths, ca_frames, strict=True):
        frame = prepare(detection)
        if frame is not None:
            prepared.append((path, frame))

    if len(prepared) < MIN_CALIBRATION_FRAMES:
        raise FlatFieldError(
            Code.CHROMATIC_FIT_REJECTED,
            f"only {len(prepared)} calibration frames yielded corners in all "
            f"three channels; the minimum is {MIN_CALIBRATION_FRAMES}",
        )

    # 5. Fit and gate CA; decide the mode (section 4.6). `fit_ca` takes
    #    `(red, green_for_red, blue, green_for_blue)` per frame — each
    #    channel pair carries its own id-aligned green corners.
    train_ca = [
        frame for path, frame in prepared if path.name not in heldout_names
    ]
    heldout_ca = [
        frame for path, frame in prepared if path.name in heldout_names
    ]

    try:
        ca = ca_fit_module.fit_ca(
            train_ca, heldout_ca, frame_width, frame_height, geometry_params
        )
    except ca_fit_module.CAFitError as exc:
        raise _map_ca_error(exc) from exc

    chromatic_aberration_dict: dict | None = None
    ca_scales: tuple[float, float] | None = None
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
            ca_scales = (ca.red_scale, ca.blue_scale)
    else:
        emit(
            WarningEvent(
                code=Code.CHROMATIC_FIT_REJECTED,
                message=f"chromatic aberration fit rejected: {ca.rejection_reason}",
            )
        )

    # The detection-channel measurement (section 4.6): the Rec.709
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

    # 6. Decode the flat-field reference with `RAW_PARAMS` plus the CA
    #    scales when the mode is "scale" (section 4.7 step 6) — the gain
    #    map and the frames must agree about geometry.
    emit(FlatFieldProgress(phase="reference", completed=0, total=1))
    reference_frame = decode_raw(reference, chromatic_aberration=ca_scales)
    gain_map = flatfield.compute_gain(decode_to_linear(reference_frame.pixels))
    reference_width, reference_height = reference_frame.width, reference_frame.height
    del reference_frame
    emit(FlatFieldProgress(phase="reference", completed=1, total=1))

    # 7. Assemble the report, save the gain map, insert the row.
    report = {
        "frames_total": len(paths),
        "frames_fit": len(train_paths),
        "frames_heldout": len(heldout_paths),
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

    profile_id = str(uuid.uuid4())
    path, sha256 = flatfield.save_gain_map(profile_id, gain_map)
    profile = flatfield.FlatFieldProfile(
        profile_id=profile_id,
        name=name,
        gain_map_path=str(path),
        gain_map_sha256=sha256,
        source_path=str(reference),
        reference_width=reference_width,
        reference_height=reference_height,
        params=flatfield.build_params(chromatic_aberration_scales=ca_scales),
        scanny_boy_version=_current_scanny_boy_version(),
        created_at=_now_iso(),
        board_key=board.key,
        geometry=geometry_dict,
        chromatic_aberration=chromatic_aberration_dict,
        calibration_report=report,
    )
    from scanny_boy.library import repo

    repo.save_flatfield_profile(profile)
    return profile
