import cv2
import numpy as np

from scanny_boy.detection import build_detection_image, to_full_resolution
from scanny_boy.linear import encode_from_linear


def _film_like_frame(height: int, width: int, *, seed: int) -> np.ndarray:
    """A film-like uint16 (H, W, 3) frame: smooth gradients, a handful of
    blurred blobs, and light grain. Never pure noise (section 6) — this
    chunk does not depend on Chunk P2-3's shared
    `synthetic_scene_support.py` generator, so it builds its own minimal
    equivalent locally."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float64)

    base = 0.5 + 0.3 * np.sin(xx / width * 3 * np.pi) + 0.15 * np.cos(
        yy / height * 2 * np.pi
    )
    image = np.stack([base, base, base], axis=-1)

    for _ in range(25):
        cx = rng.uniform(0, width)
        cy = rng.uniform(0, height)
        radius = rng.uniform(10, min(height, width) / 8)
        value = rng.uniform(0.1, 0.9)
        mask = (xx - cx) ** 2 + (yy - cy) ** 2 < radius * radius
        image[mask] = value

    image = cv2.GaussianBlur(image.astype(np.float32), (0, 0), 1.4)
    grain = rng.normal(0.0, 0.01, size=image.shape).astype(np.float32)
    image = np.clip(image + grain, 0.0, 1.0)

    return encode_from_linear(image)


def test_long_edge_and_dtype():
    frame = _film_like_frame(800, 1200, seed=1)
    detection = build_detection_image(frame, long_edge=600, clahe=False)

    assert detection.image.dtype == np.uint8
    assert detection.image.ndim == 2
    assert max(detection.image.shape) == 600
    assert detection.source_size == (800, 1200)
    assert detection.scale == 1200 / 600


def test_scale_maps_points_back_within_half_a_pixel():
    height, width = 810, 1230
    frame = _film_like_frame(height, width, seed=2)
    detection = build_detection_image(frame, long_edge=500, clahe=False)

    detect_height, detect_width = detection.image.shape
    actual_scale_x = detect_width / width
    actual_scale_y = detect_height / height

    full_point = np.array([width * 0.37, height * 0.62])
    detect_point = np.array(
        [[full_point[0] * actual_scale_x, full_point[1] * actual_scale_y]]
    )

    recovered = to_full_resolution(detect_point, detection.scale)[0]

    assert abs(recovered[0] - full_point[0]) < 0.5
    assert abs(recovered[1] - full_point[1]) < 0.5


def test_never_upscales_a_small_frame():
    height, width = 300, 500
    frame = _film_like_frame(height, width, seed=3)
    detection = build_detection_image(frame, long_edge=2000, clahe=False)

    assert detection.image.shape == (height, width)
    assert detection.scale == 1.0


def test_clahe_flag_changes_the_result():
    frame = _film_like_frame(800, 1200, seed=4)

    without_clahe = build_detection_image(frame, long_edge=600, clahe=False)
    with_clahe = build_detection_image(frame, long_edge=600, clahe=True)

    assert not np.array_equal(without_clahe.image, with_clahe.image)
