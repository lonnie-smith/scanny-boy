"""Auto-rotation: squaring the rebate after stitching.

A stitched canvas comes out in whatever orientation the strip was scanned,
and a camera that was not quite square to the film leaves the rebate's
edges visibly tilted. This module estimates one corrective rotation angle
from the stitched image and applies it — but never to the published TIFF:
like every edit, the rotation is a nondestructive ops-log entry
(`repo.ROTATE_FINE_OP`, params `{"angle_deg": ..., "source": "auto"}`)
seeded automatically at stitch time, and its pixels are transformed only
where the ops log meets pixels — the preview generator and the exporter.

**Which edges.** The rotation squares the rebate's *frame boundary* — the
line between the film rebate and the picture. The discriminator is
density, not geometry (section 3.13's insight, reused here): on a negative
the rebate/unexposed base is strictly the thinnest thing on the film, so
the thin-end population of the normalized image is the rebate, and the
scene — everything else the blend actually covered — is the picture. The
empty-canvas fill (`NORMALIZED_FILL`, section 3.14) sits even thinner than
the rebate and is excluded first, so the fill wedges a tilted stitch
leaves at the canvas edges never contaminate the estimate.

**One angle that splits the difference.** The rebate's four edges are not
perfectly straight and not perfectly parallel to one another — film curls,
the film gate was never perfect. Rather than fit four lines and average
four opinions, the scene mask (morphologically cleaned, dust discarded)
gets one `cv2.minAreaRect`: the minimum-area enclosing rectangle is by
construction the single orientation that best compromises across all four
sides. Its deviation from the canvas axes, normalized to (-45, 45] degrees,
is the tilt; the corrective rotation is its negation (a rebate tilted
clockwise by `d` squares up under a clockwise rotation of `-d`).

**Guards.** The detector refuses to invent a rotation: no thin-end
population worth the name (no rebate visible), no scene large enough to
frame, or a tilt beyond `AUTO_ROTATE_MAX_DEG` — all return `None`, and the
caller seeds no op. Below `AUTO_ROTATE_DEADBAND_DEG` the rotation is real
but invisible, and also returns `None`: warping a whole scan by two
hundredths of a degree costs more than it buys.

**Filling what the rotation uncovers.** `rotate_with_fill` keeps the
canvas dimensions exactly: pixels whose source falls outside the image are
the empty pixels the stitching fill already defines, and they get the same
sentinel code — the `NORMALIZED_FILL` encode, the thin rail — so the
preview's `1 - val` renders them black and the export's density semantics
stay intact, with no new concept anywhere downstream.
"""

from __future__ import annotations

import cv2
import numpy as np

from scanny_boy.normalization import (
    NORMALIZED_FILL,
    decode_normalized,
    encode_normalized,
)

# Refuse to rotate more than this: a tilt this large is not a scan that was
# slightly crooked, it is a scan the rebate detector misread.
AUTO_ROTATE_MAX_DEG = 10.0
# Below this the rotation is real but invisible; not worth a warp.
AUTO_ROTATE_DEADBAND_DEG = 0.05
# The estimation runs on a bounded analysis copy of the stitched image.
ANALYSIS_MAX_EDGE = 1024
# Slack, in normalized units, below the thin-end anchor a pixel may sit and
# still count as rebate (the normalized-space analogue of `detect_rebate`'s
# `REBATE_DENSITY_TOLERANCE`).
REBATE_SLACK = 0.02
# Of the covered canvas; a smaller thin population is not rebate, and there
# is no geometric signal worth rotating from.
REBATE_MIN_AREA_FRACTION = 0.01
# Of the covered canvas; smaller scene masks frame nothing.
SCENE_MIN_AREA_FRACTION = 0.05
# The scene rectangle must span at least this fraction of the analysis
# image's smaller side, in both dimensions, or the fit is not trusted.
SCENE_MIN_RECT_FRACTION = 0.3
# Morphological cleanup runs at this fraction of the analysis image's
# smaller side (kernel side, kept odd) — dust and rebate holes vanish
# without rounding the corners that carry the angle.
SCENE_CLEAN_FRACTION = 0.01
# The seeded angle is rounded to this many degrees.
ANGLE_PRECISION_DEG = 0.01

_FILL_CODE = int(
    encode_normalized(
        np.full((1, 1, 3), NORMALIZED_FILL, dtype=np.float32)
    )[0, 0, 0]
)


def rotate_with_fill(image: np.ndarray, angle_deg: float) -> np.ndarray:
    """Rotate `image` clockwise by `angle_deg` about its center, keeping
    its dimensions, and fill the pixels the rotation uncovers with the
    stitching fill sentinel (`NORMALIZED_FILL`'s code — the thin rail).

    The fill rides on `warpAffine`'s constant border: a destination pixel
    whose source falls entirely outside the image is exactly the sentinel
    code, and one that straddles the edge blends toward it, the same
    one-pixel courtesy the feathered blend gives a covered edge. Works on
    uint16 normalized-density images — the only domain the ops log's pixels
    ever meet it in."""
    if abs(angle_deg) < 1e-9:
        return image
    height, width = image.shape[0], image.shape[1]
    # cv2's positive angles turn counter-clockwise; this module's angles
    # count clockwise, matching the quarter-turn ops' convention.
    matrix = cv2.getRotationMatrix2D(
        (width / 2.0, height / 2.0), -angle_deg, 1.0
    )
    return cv2.warpAffine(
        image,
        matrix,
        (width, height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=_FILL_CODE,
    )


def estimate_rotation(image: np.ndarray) -> float | None:
    """The clockwise rotation, in degrees, that squares the rebate's frame
    boundary with the canvas — or `None` when there is nothing trustworthy
    to rotate by (no detectable rebate, too little scene, tilt outside the
    clamps). `image` is the encoded uint16 normalized-density composite."""
    image = np.asarray(image)
    if image.ndim == 2:
        image = np.stack([image] * 3, axis=-1)
    height, width = image.shape[0], image.shape[1]

    # The analysis copy is downscaled in normalized density (code space):
    # averaging density is what averaging a photographic image means, and
    # the angle needs nowhere near full resolution.
    scale = min(1.0, ANALYSIS_MAX_EDGE / max(height, width))
    if scale < 1.0:
        small = cv2.resize(
            image,
            (round(width * scale), round(height * scale)),
            interpolation=cv2.INTER_AREA,
        )
    else:
        small = image
    normalized = decode_normalized(small).astype(np.float32)

    # The fill is exactly the sentinel code, all channels; it is canvas,
    # not film, and never participates.
    fill = np.all(normalized >= NORMALIZED_FILL - 1e-6, axis=-1)
    covered = ~fill
    if not covered.any():
        return None

    # Thin in *every* channel is the rebate: base is the thinnest thing on
    # the film, and a scene shadow dense in one channel is not base. The
    # anchor is the covered pixels' thin-end percentile — the normalized
    # space's version of `detect_rebate`'s `REBATE_ANCHOR_PERCENTILE`.
    thinness = normalized.min(axis=-1)
    thin_values = thinness[covered]
    anchor = float(np.percentile(thin_values, 99.5))
    rebate = covered & (thinness >= anchor - REBATE_SLACK)
    if (
        np.count_nonzero(rebate) < REBATE_MIN_AREA_FRACTION * covered.size
    ):
        return None

    scene = (covered & ~rebate).astype(np.uint8)
    kernel_side = max(
        3, round(SCENE_CLEAN_FRACTION * min(normalized.shape[:2]))
    )
    if kernel_side % 2 == 0:
        kernel_side += 1
    kernel = np.ones((kernel_side, kernel_side), np.uint8)
    scene = cv2.morphologyEx(scene, cv2.MORPH_OPEN, kernel)
    scene = cv2.morphologyEx(scene, cv2.MORPH_CLOSE, kernel)

    # Keep only the components big enough to be picture: dust, rebate
    # speckle misclassified as scene, and stray markings go.
    count, labels, stats, _ = cv2.connectedComponentsWithStats(scene, 8)
    big = np.zeros_like(scene)
    for label in range(1, count):
        if stats[label, cv2.CC_STAT_AREA] >= SCENE_MIN_AREA_FRACTION * covered.size:
            big[labels == label] = 1
    if not big.any():
        return None

    points = cv2.findNonZero(big)
    if points is None:
        return None
    rect = cv2.minAreaRect(points)
    (_center_x, _center_y), (rect_width, rect_height), _angle = rect
    if min(rect_width, rect_height) < (
        SCENE_MIN_RECT_FRACTION * min(normalized.shape[:2])
    ):
        return None

    # The enclosing rectangle's edge directions, as deviations from the
    # nearest axis in (-45, 45]: for a rectangle all four edges agree (the
    # perpendicular pair differs by exactly 90, which the mod folds away),
    # and the mean is the least-squares compromise when they do not —
    # the "splits the difference" the feature promises.
    box = cv2.boxPoints(rect)
    deviations = []
    for i in range(4):
        vx = box[(i + 1) % 4][0] - box[i][0]
        vy = box[(i + 1) % 4][1] - box[i][1]
        edge_angle = edge_degrees(vy, vx)
        deviations.append((edge_angle + 45.0) % 90.0 - 45.0)
    deviation = float(np.mean(deviations))

    # A rebate edge tilted clockwise by `d` squares up under a clockwise
    # rotation of `-d`.
    rotation = -deviation
    rotation = round(rotation / ANGLE_PRECISION_DEG) * ANGLE_PRECISION_DEG
    if abs(rotation) < AUTO_ROTATE_DEADBAND_DEG:
        return None
    if abs(rotation) > AUTO_ROTATE_MAX_DEG:
        return None
    return float(rotation)


def edge_degrees(vy: float, vx: float) -> float:
    """The direction of one box edge in degrees, image coordinates (y
    down): positive is a clockwise tilt in viewing space."""
    return float(np.degrees(np.arctan2(vy, vx)))