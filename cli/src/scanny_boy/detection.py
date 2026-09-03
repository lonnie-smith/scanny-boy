"""Detection images: small, contrast-normalised 8-bit greyscale derivatives
of an intermediate, used only for finding and matching features. Never
composited, never written out. See Phase 2 plan section 1.1.

`DETECTION_LONG_EDGE` and `USE_CLAHE` are Chunk P2-1's measured constants,
approved at user gate C (section 3.12). Production code reads them from here
and from nowhere else.
"""

from __future__ import annotations

import dataclasses

import cv2
import numpy as np

from scanny_boy.linear import decode_to_linear

DETECTION_LONG_EDGE = 2000
USE_CLAHE = False

# Rec.709 luminance weights and the percentile stretch, shared with
# charuco.py's full-resolution calibration detection image
# (docs/GEOMETRIC_PLAN.md section 4.2).
LUMINANCE_WEIGHTS = np.array([0.2126, 0.7152, 0.0722], dtype=np.float64)
LOW_PERCENTILE = 0.5
HIGH_PERCENTILE = 99.5


@dataclasses.dataclass(frozen=True)
class DetectionImage:
    image: np.ndarray  # uint8, (h, w)
    scale: float  # detection px * scale = full-res px
    source_size: tuple[int, int]  # (height, width) at full resolution


def build_detection_image(
    frame: np.ndarray, *, long_edge: int, clahe: bool
) -> DetectionImage:
    """frame is uint16 (H, W, 3) as read from an intermediate TIFF.

    1. Decode to linear with `decode_to_linear`.
    2. Luminance: 0.2126 R + 0.7152 G + 0.0722 B.
    3. Downscale with cv2.INTER_AREA so the long edge is `long_edge`,
       never upscaling; scale is the exact ratio used.
    4. Scale to 0..255 uint8 by the 0.5th and 99.5th percentiles, clipped.
    5. If `clahe`, apply cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).
    """
    height, width = frame.shape[0], frame.shape[1]
    source_size = (height, width)

    linear = decode_to_linear(frame).astype(np.float64)
    luminance = linear @ LUMINANCE_WEIGHTS

    source_long_edge = max(height, width)
    target_long_edge = min(long_edge, source_long_edge)
    resize_factor = target_long_edge / source_long_edge
    scale = source_long_edge / target_long_edge

    new_width = max(1, round(width * resize_factor))
    new_height = max(1, round(height * resize_factor))
    resized = cv2.resize(
        luminance.astype(np.float32),
        (new_width, new_height),
        interpolation=cv2.INTER_AREA,
    )

    low, high = np.percentile(resized, [LOW_PERCENTILE, HIGH_PERCENTILE])
    clipped = np.clip(resized, low, high)
    if high > low:
        normalised = (clipped - low) / (high - low) * 255.0
    else:
        normalised = np.zeros_like(clipped)
    image = np.rint(normalised).astype(np.uint8)

    if clahe:
        clahe_filter = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        image = clahe_filter.apply(image)

    return DetectionImage(image=image, scale=scale, source_size=source_size)


def to_full_resolution(points: np.ndarray, scale: float) -> np.ndarray:
    """(N, 2) float detection-space points -> full-resolution points."""
    return points * scale
