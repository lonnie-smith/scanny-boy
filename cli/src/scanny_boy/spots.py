"""Spot detection and repair: crud proposals, review, then inpainting.

**Polarity convention** (docs/SPOTTING_PLAN.md §0.3). The published TIFF
holds normalized log density, and `val` rises with the light that reached
the sensor. Something on the film that blocks or scatters light — dust, a
hair, a water spot's mineral residue — makes the negative read locally
denser, so `val` *dips*: that polarity is **`dense`** (the print reads
locally white). A scratch through the emulsion passes more light, so `val`
*rises*: that polarity is **`thin`** (the print reads locally black). The
names are the codebase's two ends of the density axis — do not rename them
"dark"/"light", which is ambiguous between the negative and positive views.

**Crud is neutral.** A speck of dust is a broadband attenuator: the same
additive log offset in every channel, which normalization re-scales by each
channel's own span. The neutrality gate therefore compares the per-channel
responses *after multiplying each by its channel's span*
(`color.read_metering(...).ranges`) — equal log offsets stay equal, unequal
val offsets do not. Colour rolls get this gate; a monochrome roll's single
channel has nothing to agree with, so its threshold runs stricter instead
(`MONO_K_BONUS`).

**The constants are seeded, not measured** (§0.7): every threshold below was
chosen from the physics and the prior art, not from this rig's scans.
Chunk S-6's calibration (`cli/tools/measure_spot_thresholds.py`) is what
turns them into measurements — until it runs, the detector is honest but
untuned.

**The repair** (§3) unions the surviving spots' exact RLE masks, dilates
them (`REPAIR_DILATE_PX` — the detector's threshold set misses a defect's
penumbra, and inpainting it alone leaves a halo), and runs Telea inpainting
once per `uint16` channel. The per-channel loop is forced: OpenCV's
`inpaint` refuses 3-channel `uint16` input, and downgrading to 8-bit to get
one call would throw away half the bit depth in exactly the pixels being
repaired. Channels inpaint independently, which can in principle tint a
patch; against an 8-bit round trip that is the better trade. Telea diffuses
smoothly and synthesizes no grain, so a large repair reads as a smooth
patch at 100% — visibly an interpolation, which is the point: it must never
look like invented texture.

This module is a leaf: it imports `cv2`, `numpy`, `normalization`, and
nothing else from this program.
"""

from __future__ import annotations

import dataclasses
import math
from typing import Any

import cv2
import numpy as np

from scanny_boy import normalization

# Stamped into every `spots` op; the ops-log parser gates on it (§5) so a
# future detector's output is never misread by an older one. Seeded.
DETECTOR_VERSION = 1

# Max defect width as a fraction of the canvas **short** edge (≈20 px on a
# 5000 px edge). The width gate — the one that decides what the feature can
# even see. Seeded pending Chunk S-6.
MAX_SPOT_MINOR_FRACTION = 0.004
# Floor, in pixels; below it is grain or a hot pixel. Seeded.
MIN_SPOT_AREA_PX = 6
# `blob` vs `streak`: mean width along the major axis at or below this
# times the major length is compact, above it is elongated. Seeded.
STREAK_ELONGATION = 4.0
# Max streak length as a fraction of the canvas **long** edge: a hair
# spanning a quarter of the frame is rarer than a wire that does. Seeded.
MAX_STREAK_LENGTH_FRACTION = 0.25
# Robust threshold, in sigmas above the response median: `k` at
# sensitivity 1.0 (most aggressive) and 0.0 (most conservative). Seeded.
THRESHOLD_K_MIN = 4.0
THRESHOLD_K_MAX = 12.0
# ⇒ k = 8.0. Seeded.
DEFAULT_SENSITIVITY = 0.5
# Added to `k` on a monochrome roll, which has no neutrality gate: with one
# fewer filter the remaining one must be stricter. Currently a guess
# standing in for a missing gate (§9.2 step 4). Seeded.
MONO_K_BONUS = 2.0
# Max relative channel spread the neutrality gate tolerates (§2.5). Seeded.
NEUTRALITY_TOLERANCE = 0.35
# (SIGMA_FLOOR lives with _CODE_TO_VAL below: it is a code-space value.)
# Subsampling stride, per axis, for the median/MAD statistic — a robust
# statistic does not need every pixel of 40 megapixels.
STAT_STRIDE = 4
# Per negative (§1.4): above this the highest-scoring survivors are kept
# and `SPOT_LIMIT_REACHED` names the remedy.
MAX_SPOTS = 500
# Rejection carry-forward radius (§2.6): a re-detection proposes a spot
# this close to a previously rejected one and it is born rejected.
REJECTION_MATCH_PX = 8
# Mask growth before inpainting (§3.1): the detected mask is what cleared
# the threshold, and a defect's penumbra falls below it.
REPAIR_DILATE_PX = 2
# `cv2.inpaint`'s neighbourhood radius.
INPAINT_RADIUS_PX = 3

# The stitching fill sentinel in code space, derived once — never written
# as a literal (§2.1).
FILL_CODE = int(
    normalization.encode_normalized(
        np.full((1, 1, 1), normalization.NORMALIZED_FILL, dtype=np.float32)
    ).ravel()[0]
)

# One uint16 code in `val` units: the encode's span over its code range
# (§2.2). Converts a code-space response to normalized-density units.
_CODE_TO_VAL = (
    1.0 + normalization.NORMALIZED_HEADROOM_LOW + normalization.NORMALIZED_HEADROOM_HIGH
) / 65535.0

# Numerical guard, not a measurement: keeps a synthetic zero-noise fixture
# from dividing the world by zero (val 1e-4, in code space). Not a tuned
# threshold.
SIGMA_FLOOR = 1e-4 / _CODE_TO_VAL


@dataclasses.dataclass(frozen=True)
class DetectionResult:
    """What `detect` proposes: the op's spot entries (ids assigned in
    raster order, `rejected` absent-false until the user says otherwise),
    the count found before the `MAX_SPOTS` cap, and the robust scale the
    threshold was measured against — the larger of the two polarities',
    recorded for Chunk S-6's calibration."""

    spots: list[dict]
    found: int
    sigma: float


# --- the RLE mask (§1.3) -----------------------------------------------------


def encode_rle(mask: np.ndarray) -> list[int]:
    """A boolean mask -> run lengths, row-major over its own bounding box,
    alternating and **starting with a run of zeros** (which may be 0). The
    runs always sum to `width * height`."""
    flat = np.asarray(mask, dtype=bool).ravel()
    if flat.size == 0:
        return [0]
    changes = np.flatnonzero(np.diff(flat)) + 1
    boundaries = np.concatenate(([0], changes, [flat.size]))
    runs = np.diff(boundaries).tolist()
    if flat[0]:
        runs.insert(0, 0)
    return [int(run) for run in runs]


def decode_rle(rle: list[int], width: int, height: int) -> np.ndarray:
    """The inverse of `encode_rle`: one repeat and one reshape."""
    values = np.arange(len(rle)) % 2
    return np.repeat(values, rle).reshape(height, width).astype(bool)


def _inside(
    valid_rect: tuple[int, int, int, int], shape: tuple[int, int]
) -> np.ndarray:
    """A boolean mask of the canvas pixels inside a `(x, y, w, h)` rect,
    clipped to the canvas."""
    height, width = shape
    x, y, w, h = valid_rect
    inside = np.zeros((height, width), dtype=bool)
    x0, y0 = max(int(x), 0), max(int(y), 0)
    x1, y1 = min(int(x) + int(w), width), min(int(y) + int(h), height)
    if x1 > x0 and y1 > y0:
        inside[y0:y1, x0:x1] = True
    return inside


# --- the detector (§2) --------------------------------------------------------


def detect(
    image: np.ndarray,
    *,
    valid_rect: tuple[int, int, int, int] | None,
    channel_ranges: tuple[float, ...],
    sensitivity: float = DEFAULT_SENSITIVITY,
) -> DetectionResult:
    """Propose spots on a published TIFF's array — uint16, (H, W) for a
    monochrome roll or (H, W, 3) for a colour one. Pure: nothing here
    writes, and nothing here knows about the ops log. Detection never reads
    outside `valid_rect` and never reads a fill pixel (§0.4), and each
    polarity is measured and thresholded end to end independently (§2.3),
    the two sets merging only at the capping step (§2.6).

    Raises `ValueError` when `sensitivity` is outside [0, 1] — a leaf
    module with a strict contract; the CLI range-checks first so the user
    sees `INVALID_EDIT`, never a traceback.
    """
    if not 0.0 <= sensitivity <= 1.0:
        raise ValueError(f"sensitivity must be within [0, 1], got {sensitivity}")
    image = np.asarray(image)
    if image.dtype != np.uint16:
        raise ValueError(f"detect expects uint16 codes, got {image.dtype}")
    height, width = image.shape[:2]

    # §2.7: the width gate and the SE radius are derived per image, not
    # constants. A disk of radius r removes features narrower than 2r, so
    # an SE radius equal to the full width gate leaves comfortable margin.
    max_minor_px = MAX_SPOT_MINOR_FRACTION * min(height, width)
    se_radius_px = max(3, math.ceil(max_minor_px))
    se = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (2 * se_radius_px + 1, 2 * se_radius_px + 1)
    )

    # §2.1: one boolean analysis mask — inside `valid_rect`, never a fill
    # pixel, then eroded by the same SE the filter uses, so the analysis
    # mask and the filter always agree about what "one SE radius from the
    # edge" means (and no component straddles the fill boundary to take a
    # step edge for signal).
    analysis = np.ones((height, width), dtype=bool)
    if valid_rect is not None:
        analysis &= _inside(valid_rect, (height, width))
    fill = image == FILL_CODE
    analysis &= ~(fill if image.ndim == 2 else fill.all(axis=2))
    analysis = cv2.erode(analysis.view(np.uint8), se).astype(bool)

    if not analysis.any():
        return DetectionResult(spots=[], found=0, sigma=0.0)

    candidates: list[dict] = []
    sigmas: list[float] = []
    for polarity in ("dense", "thin"):
        if polarity == "dense":
            # BLACKHAT (closing - image) is large where the image dips
            # below its surroundings.
            response_ch = cv2.morphologyEx(image, cv2.MORPH_BLACKHAT, se)
        else:
            # TOPHAT (image - opening) is large where it rises above them.
            response_ch = cv2.morphologyEx(image, cv2.MORPH_TOPHAT, se)
        if image.ndim == 2:
            response = response_ch
        else:
            # The channel-mean: better signal-to-noise than any single
            # channel, and unbiased, unlike a min or a max. Kept in code
            # space (uint16 rounding is far below the threshold scale).
            response = np.clip(np.rint(response_ch.mean(axis=2)), 0, 65535).astype(
                np.uint16
            )

        # §2.3: the robust threshold, measured from the negative's own
        # grain over the analysis mask, subsampled.
        stride = slice(None, None, STAT_STRIDE)
        samples = response[stride, stride][analysis[stride, stride]].astype(np.float32)
        med = float(np.median(samples))
        sigma = max(
            1.4826 * float(np.median(np.abs(samples - med))),
            SIGMA_FLOOR,
        )
        sigmas.append(sigma)
        k = THRESHOLD_K_MAX - sensitivity * (THRESHOLD_K_MAX - THRESHOLD_K_MIN)
        if image.ndim == 2:
            # §2.5: no neutrality gate on a mono roll, so the remaining
            # gate must be stricter.
            k += MONO_K_BONUS
        mask = (response > med + k * sigma) & analysis

        candidates.extend(
            _components_for_polarity(
                mask,
                response,
                response_ch,
                polarity=polarity,
                med=med,
                sigma=sigma,
                max_minor_px=max_minor_px,
                channel_ranges=channel_ranges,
                long_edge=max(height, width),
                colour=image.ndim == 3,
            )
        )
        del response_ch, response

    found = len(candidates)
    candidates.sort(key=lambda spot: -spot["score"])
    kept = candidates[:MAX_SPOTS]
    # Raster order, top-left to bottom-right, so the numbering the user
    # sees is stable for a given detection (§2.6).
    kept.sort(key=lambda spot: (spot["bbox"][1], spot["bbox"][0]))
    for index, spot in enumerate(kept, start=1):
        spot["id"] = index
    return DetectionResult(spots=kept, found=found, sigma=max(sigmas, default=0.0))


def _components_for_polarity(
    mask: np.ndarray,
    response: np.ndarray,
    response_ch: np.ndarray,
    *,
    polarity: str,
    med: float,
    sigma: float,
    max_minor_px: float,
    channel_ranges: tuple[float, ...],
    long_edge: int,
    colour: bool,
) -> list[dict]:
    """One polarity's threshold mask -> gated spot candidates (§2.4-2.6).
    Shape gates from the connected-component stats alone; the neutrality
    gate from the per-channel responses inside the component; the score
    from the peak response in this polarity's own robust sigmas."""
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask.view(np.uint8), 8)
    spots: list[dict] = []
    for label in range(1, count):
        area = int(stats[label, cv2.CC_STAT_AREA])
        if area < MIN_SPOT_AREA_PX:
            continue
        bx = int(stats[label, cv2.CC_STAT_LEFT])
        by = int(stats[label, cv2.CC_STAT_TOP])
        bw = int(stats[label, cv2.CC_STAT_WIDTH])
        bh = int(stats[label, cv2.CC_STAT_HEIGHT])
        major = max(bw, bh)
        # Mean width along the major axis: exact for an axis-aligned
        # component, and up to √2 pessimistic for a diagonal one — an
        # error direction toward rejecting, which costs a manual spot and
        # never costs a pixel (§2.4).
        minor = area / major
        elongation = major / max(minor, 1.0)
        kind = "streak" if elongation >= STREAK_ELONGATION else "blob"
        if minor > max_minor_px:
            continue
        if kind == "blob" and major > 2 * max_minor_px:
            # A blob wider than twice the width gate is image content
            # that happened to clear the threshold.
            continue
        if kind == "streak" and major > MAX_STREAK_LENGTH_FRACTION * long_edge:
            # A power line, a horizon, a branch against sky and a hair
            # are all thin and dark; length is the only cheap separator.
            continue

        local = labels[by : by + bh, bx : bx + bw] == label
        if colour:
            # §2.5: the neutrality gate. Each channel's mean response, in
            # the component, converted to log-density units by its own
            # channel span (§0.3) — an equal log offset stays equal, an
            # equal val offset does not.
            d = [
                float(response_ch[..., ch][by : by + bh, bx : bx + bw][local].mean())
                * _CODE_TO_VAL
                * channel_ranges[ch]
                for ch in range(response_ch.shape[2])
            ]
            spread = max(d) - min(d)
            if spread > NEUTRALITY_TOLERANCE * (sum(d) / len(d)):
                continue

        # §2.6: the peak height in this polarity's own robust sigmas.
        score = (float(response[by : by + bh, bx : bx + bw][local].max()) - med) / sigma
        spots.append(
            {
                "kind": kind,
                "polarity": polarity,
                "bbox": [bx, by, bw, bh],
                "rle": encode_rle(local),
                "area": area,
                "score": round(score, 2),
                "rejected": False,
            }
        )
    return spots


# --- the repair (§3) ----------------------------------------------------------


def is_repairing(params: dict | None, shape: tuple[int, int]) -> bool:
    """True when a `spots` op's params would actually change pixels: repair
    switched on, the recorded canvas matching this image's `(height,
    width)`, and at least one spot not rejected (§1.5, §3.4). A stale set —
    detected against a canvas a re-stitch has replaced — repairs nothing,
    ever."""
    if not params:
        return False
    if not params.get("repair"):
        return False
    canvas = params.get("canvas")
    if (
        not isinstance(canvas, (list, tuple))
        or len(canvas) != 2
        or (int(canvas[0]), int(canvas[1])) != (shape[1], shape[0])
    ):
        return False
    spots = params.get("spots")
    if not isinstance(spots, list):
        return False
    return any(not spot.get("rejected") for spot in spots)


def apply_repair(image: np.ndarray, params: dict | None) -> np.ndarray:
    """Inpaint every non-rejected spot's exact mask into the published
    TIFF's array (uint16, (H, W) or (H, W, 3)), or return it untouched.
    The repair never touches a pixel outside the dilated masks, never
    touches a pixel the user rejected, and never touches a canvas it was
    not detected against (§1.5) — `is_repairing` gates the whole thing, so
    the guard and the repair can never disagree (§3.4)."""
    if not is_repairing(params, np.asarray(image).shape[:2]):
        return image
    image = np.asarray(image)
    height, width = image.shape[:2]
    mask = np.zeros((height, width), dtype=np.uint8)
    for spot in params["spots"]:
        if spot.get("rejected"):
            continue
        bbox = spot.get("bbox")
        rle = spot.get("rle")
        if (
            not isinstance(bbox, (list, tuple))
            or len(bbox) != 4
            or not isinstance(rle, list)
        ):
            continue
        x, y, w, h = (int(value) for value in bbox)
        if w <= 0 or h <= 0:
            continue
        x0, y0 = max(x, 0), max(y, 0)
        x1, y1 = min(x + w, width), min(y + h, height)
        if x1 <= x0 or y1 <= y0:
            continue
        try:
            local = decode_rle(rle, w, h)
        except ValueError:
            # Runs that do not sum to the bbox area cannot be placed;
            # degrading to skipping the spot is the only safe direction
            # (§5: malformed state degrades to no repair).
            continue
        mask[y0:y1, x0:x1] |= local[y0 - y : y1 - y, x0 - x : x1 - x].astype(np.uint8)
    if not mask.any():
        return image

    # §3.1: grow the threshold set over the defect's penumbra.
    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (2 * REPAIR_DILATE_PX + 1, 2 * REPAIR_DILATE_PX + 1),
    )
    mask = cv2.dilate(mask, kernel)
    # A spot that somehow reaches uncovered canvas cannot smear fill into
    # the image.
    fill = image == FILL_CODE
    fill_pixels = fill if image.ndim == 2 else fill.all(axis=2)
    mask[fill_pixels] = 0
    if not mask.any():
        return image

    # §3.2: Telea, once per uint16 channel — `inpaint` refuses 3-channel
    # uint16, and an 8-bit round trip is not on the table.
    if image.ndim == 2:
        return cv2.inpaint(image, mask, INPAINT_RADIUS_PX, cv2.INPAINT_TELEA)
    repaired = image.copy()
    for channel in range(image.shape[2]):
        repaired[..., channel] = cv2.inpaint(
            image[..., channel], mask, INPAINT_RADIUS_PX, cv2.INPAINT_TELEA
        )
    return repaired


def spots_params(
    *,
    canvas: tuple[int, int],
    spots: list[dict],
    sensitivity: float,
    repair: bool,
) -> dict[str, Any]:
    """The net `spots` op's params for a detection result: canvas as
    `[width, height]` (§1.1), the spot entries as detected."""
    return {
        "detector_version": DETECTOR_VERSION,
        "sensitivity": sensitivity,
        "repair": repair,
        "canvas": [canvas[0], canvas[1]],
        "spots": [dict(spot) for spot in spots],
    }
