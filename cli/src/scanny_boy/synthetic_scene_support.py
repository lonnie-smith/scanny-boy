"""Shared film-like synthetic scene generator for registration tests and
`scripts/measure-registration.py` (section 6: synthetic fixtures must be
film-like — gradients, blobs, and light grain — never pure noise).
"""

from __future__ import annotations

import cv2
import numpy as np

_CIRCLE_COUNT = 220
_BLUR_SIGMA = 1.4
_GRAIN_SIGMA = 0.012


def synthetic_scene(height: int, width: int, *, seed: int) -> np.ndarray:
    """float32 in [0, 1]: smooth sinusoidal gradients, ~220 filled circles
    of random radius and value, Gaussian blur sigma 1.4, then Gaussian grain
    at sigma 0.012."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float64)

    scene = (
        0.5
        + 0.25 * np.sin(xx / width * 4 * np.pi + 0.3)
        + 0.2 * np.cos(yy / height * 3 * np.pi + 0.7)
    )
    scene = np.clip(scene, 0.0, 1.0)

    min_dim = min(height, width)
    for _ in range(_CIRCLE_COUNT):
        cx = rng.uniform(0, width)
        cy = rng.uniform(0, height)
        radius = rng.uniform(min_dim * 0.01, min_dim * 0.06)
        value = rng.uniform(0.0, 1.0)
        mask = (xx - cx) ** 2 + (yy - cy) ** 2 < radius * radius
        scene[mask] = value

    scene = cv2.GaussianBlur(scene.astype(np.float32), (0, 0), _BLUR_SIGMA)

    grain = rng.normal(0.0, _GRAIN_SIGMA, size=scene.shape).astype(np.float32)
    scene = np.clip(scene + grain, 0.0, 1.0).astype(np.float32)

    return scene


def _rotation_matrix(angle_deg: float) -> np.ndarray:
    angle_rad = np.deg2rad(angle_deg)
    cos_a, sin_a = np.cos(angle_rad), np.sin(angle_rad)
    return np.array([[cos_a, -sin_a], [sin_a, cos_a]])


def cut_frames(
    scene: np.ndarray,
    *,
    frame_size: tuple[int, int],
    count: int,
    overlap: float,
    rotations_deg: list[float],
    seed: int,
) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """Returns the frames and the 2x3 ground-truth placement of each.

    Each returned placement `M_i` is `[R(rotations_deg[i]) | t_i]`, mapping a
    frame-local pixel `p` into `scene` coordinates as `R @ p + t_i`. Frames
    are laid out in a horizontal strip, evenly spaced so that consecutive
    frames overlap by `overlap` (a fraction of `frame_size`'s width) when
    unrotated; each frame is then rotated about its own centre by its entry
    in `rotations_deg`.
    """
    frame_height, frame_width = frame_size
    scene_height, scene_width = scene.shape

    spacing = frame_width * (1.0 - overlap)
    total_width = frame_width + (count - 1) * spacing
    start_x = (scene_width - total_width) / 2.0 + frame_width / 2.0
    center_y = scene_height / 2.0

    local_center = np.array([frame_width / 2.0, frame_height / 2.0])

    frames = []
    placements = []
    for i in range(count):
        angle_deg = rotations_deg[i]
        rotation = _rotation_matrix(angle_deg)
        scene_center = np.array([start_x + i * spacing, center_y])
        translation = scene_center - rotation @ local_center

        placement = np.hstack([rotation, translation.reshape(2, 1)])
        placements.append(placement)

        frame = cv2.warpAffine(
            scene,
            placement,
            (frame_width, frame_height),
            flags=cv2.INTER_LANCZOS4 | cv2.WARP_INVERSE_MAP,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
        frames.append(frame)

    return frames, placements
