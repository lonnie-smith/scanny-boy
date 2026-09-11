"""Automatic tone-split neutral balance for colour negatives.

After stitch and highlight-lock correction, measures a per-band grey-
surfaces residual on the published TIFF (not at stitch time on uncorrected
bounds), stores it on the negative's `normalization` record, and applies a
two-point per-channel cast correction at render time before manual colour
ops.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING, Any

import cv2
import numpy as np
import tifffile

from scanny_boy import color, normalization

if TYPE_CHECKING:
    from scanny_boy.roll_manifest import NegativeRecord, RollManifest

# Display-luma percentile bands for the shadow and highlight neutral
# estimates. PROVISIONAL — chosen to match the verification replay's shadow
# window (2–12) and a symmetric highlight tail; tuned on the Sep-11-2026
# Portra-II roll replay in step 5 of the auto-neutral plan.
AUTO_NEUTRAL_SHADOW_LUMA_PERCENTILE_LOW = 2.0  # PROVISIONAL
AUTO_NEUTRAL_SHADOW_LUMA_PERCENTILE_HIGH = 12.0  # PROVISIONAL
AUTO_NEUTRAL_HIGHLIGHT_LUMA_PERCENTILE_LOW = 88.0  # PROVISIONAL
AUTO_NEUTRAL_HIGHLIGHT_LUMA_PERCENTILE_HIGH = 98.0  # PROVISIONAL

# Decode scale for the measurement pass — the published TIFF is ~350 MB;
# 1/8 long edge keeps the grey-surfaces grid small without changing the
# band statistics materially on the Portra-II replay negatives.
AUTO_NEUTRAL_MEASURE_SCALE = 1.0 / 8.0

AUTO_NEUTRAL_MEASURE_VERSION = 1


@dataclasses.dataclass(frozen=True)
class AutoNeutralBands:
    shadow: tuple[float, float] | None
    highlight: tuple[float, float] | None


def _bounds_from_metering(metering: color.Metering, record: dict) -> normalization.Bounds:
    floors = record.get("floors")
    ceils = record.get("ceils")
    if not isinstance(floors, list) or not isinstance(ceils, list):
        return normalization.Bounds(floors=(0.0, 0.0, 0.0), ceils=(1.0, 1.0, 1.0))
    delta = metering.highlight_floor_delta
    if delta is None:
        return normalization.Bounds(
            floors=tuple(float(v) for v in floors),
            ceils=tuple(float(v) for v in ceils),
        )
    corrected_floors = [
        float(floors[ch]) - float(delta[ch]) for ch in range(len(floors))
    ]
    return normalization.Bounds(
        floors=tuple(corrected_floors),
        ceils=tuple(float(v) for v in ceils),
    )


def _display_luma_grid(
    grid_log: np.ndarray, bounds: normalization.Bounds
) -> np.ndarray:
    norm = normalization.normalize_log_image(grid_log, bounds)
    display = 1.0 - norm
    if display.shape[-1] == 3:
        return (
            normalization.LUMA_R * display[..., 0]
            + normalization.LUMA_G * display[..., 1]
            + normalization.LUMA_B * display[..., 2]
        ).astype(np.float32)
    return display[..., 0].astype(np.float32)


def _luma_band(
    luma: np.ndarray, keep: np.ndarray, low_pct: float, high_pct: float
) -> np.ndarray:
    if not keep.any():
        return np.zeros_like(keep)
    samples = luma[keep]
    lo = float(np.percentile(samples, low_pct))
    hi = float(np.percentile(samples, high_pct))
    if hi <= lo:
        return np.zeros_like(keep)
    return keep & (luma >= lo) & (luma <= hi)


def measure_auto_neutral_bands(
    grid_log: np.ndarray,
    keep: np.ndarray,
    bounds: normalization.Bounds,
) -> AutoNeutralBands:
    """Grey-surfaces `(R-G, B-G)` residuals per tonal band, or `None`."""
    if grid_log.shape[-1] != 3:
        return AutoNeutralBands(shadow=None, highlight=None)
    luma = _display_luma_grid(grid_log, bounds)
    shadow_keep = _luma_band(
        luma,
        keep,
        AUTO_NEUTRAL_SHADOW_LUMA_PERCENTILE_LOW,
        AUTO_NEUTRAL_SHADOW_LUMA_PERCENTILE_HIGH,
    )
    highlight_keep = _luma_band(
        luma,
        keep,
        AUTO_NEUTRAL_HIGHLIGHT_LUMA_PERCENTILE_LOW,
        AUTO_NEUTRAL_HIGHLIGHT_LUMA_PERCENTILE_HIGH,
    )
    return AutoNeutralBands(
        shadow=normalization.measure_neutral_residual(
            grid_log, keep, bounds, band=shadow_keep
        ),
        highlight=normalization.measure_neutral_residual(
            grid_log, keep, bounds, band=highlight_keep
        ),
    )


def _valid_rect_grid(
    valid_rect: tuple[int, int, int, int] | None, image_shape: tuple[int, int]
) -> np.ndarray:
    height, width = image_shape
    block_rows, block_cols = normalization.analysis_grid_block_sizes(
        (height, width, 3)
    )
    grid_rows = -(-height // block_rows)
    grid_cols = -(-width // block_cols)
    if valid_rect is None:
        keep = normalization.resolve_analysis_region((grid_rows, grid_cols), None)
        return keep
    x, y, w, h = (int(v) for v in valid_rect)
    gx0 = int(np.ceil(x / block_cols))
    gy0 = int(np.ceil(y / block_rows))
    gx1 = int(np.floor((x + w) / block_cols))
    gy1 = int(np.floor((y + h) / block_rows))
    if gx1 <= gx0 or gy1 <= gy0:
        gx1 = int(np.ceil((x + w) / block_cols))
        gy1 = int(np.ceil((y + h) / block_rows))
    return normalization.resolve_analysis_region(
        (grid_rows, grid_cols), (gx0, gy0, max(gx1 - gx0, 1), max(gy1 - gy0, 1))
    )


def _downscale_uint16(image: np.ndarray, scale: float) -> np.ndarray:
    if scale >= 1.0:
        return image
    height, width = image.shape[0], image.shape[1]
    new_h = max(1, int(round(height * scale)))
    new_w = max(1, int(round(width * scale)))
    if image.ndim == 2:
        return cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_AREA)
    channels = [
        cv2.resize(image[..., ch], (new_w, new_h), interpolation=cv2.INTER_AREA)
        for ch in range(image.shape[2])
    ]
    return np.stack(channels, axis=-1)


def _norm_to_log_grid(
    norm: np.ndarray, metering: color.Metering, record: dict
) -> np.ndarray:
    floors = record.get("floors")
    ceils = record.get("ceils")
    if not isinstance(floors, list) or not isinstance(ceils, list):
        return norm.astype(np.float32)
    delta = metering.highlight_floor_delta
    out = np.empty_like(norm, dtype=np.float32)
    for ch in range(norm.shape[-1]):
        floor = float(floors[ch])
        if delta is not None and ch < len(delta):
            floor -= float(delta[ch])
        ceil = float(ceils[ch])
        out[..., ch] = floor + norm[..., ch] * metering.ranges[ch]
    return out


def measure_auto_neutral_from_image(
    image: np.ndarray,
    record: dict | None,
    *,
    highlight_lock=None,
    valid_rect: tuple[int, int, int, int] | None = None,
) -> dict[str, Any] | None:
    """Measure tone-split residuals on a published-TIFF uint16 array.

    Returns the `normalization.auto_neutral` block to store, or `None` on a
    mono negative or when the record is unusable."""
    if image.dtype != np.uint16:
        raise ValueError("measure_auto_neutral_from_image expects uint16 codes")
    if image.ndim == 2 or (image.ndim == 3 and image.shape[2] == 1):
        return None
    if image.ndim != 3 or image.shape[2] != 3:
        raise ValueError(f"expected (H, W, 3) colour image; got {image.shape}")
    if not record:
        return None

    metering = color.read_metering(record, highlight_lock=highlight_lock)
    bounds = _bounds_from_metering(metering, record)

    norm = normalization.decode_normalized(image.astype(np.float64))
    for ch in range(3):
        norm[..., ch] = color.remap_dense_end(norm[..., ch], ch, metering)
    grid_log = normalization.block_median_grid(_norm_to_log_grid(norm, metering, record))

    keep = _valid_rect_grid(valid_rect, (image.shape[0], image.shape[1]))
    keep, _opaque = normalization.withhold_opaque(grid_log, keep)
    keep, _film_extent = normalization.withhold_non_film(grid_log, keep)
    keep, _rebate = normalization.detect_rebate(grid_log, keep)
    keep, _dense = normalization.withhold_dense_border(grid_log, keep)

    bands = measure_auto_neutral_bands(grid_log, keep, bounds)
    if bands.shadow is None and bands.highlight is None:
        block: dict[str, Any] = {
            "shadow": None,
            "highlight": None,
            "highlight_lock": _lock_snapshot(highlight_lock),
            "measure_version": AUTO_NEUTRAL_MEASURE_VERSION,
        }
        return block

    return {
        "shadow": None if bands.shadow is None else list(bands.shadow),
        "highlight": None if bands.highlight is None else list(bands.highlight),
        "highlight_lock": _lock_snapshot(highlight_lock),
        "measure_version": AUTO_NEUTRAL_MEASURE_VERSION,
    }


def measure_auto_neutral_from_tiff(
    tiff_path: Path,
    record: dict | None,
    *,
    highlight_lock=None,
    valid_rect: tuple[int, int, int, int] | None = None,
    scale: float = AUTO_NEUTRAL_MEASURE_SCALE,
) -> dict[str, Any] | None:
    image = tifffile.imread(tiff_path)
    if scale < 1.0:
        image = _downscale_uint16(image, scale)
        if valid_rect is not None:
            x, y, w, h = valid_rect
            valid_rect = (
                int(round(x * scale)),
                int(round(y * scale)),
                max(1, int(round(w * scale))),
                max(1, int(round(h * scale))),
            )
    return measure_auto_neutral_from_image(
        image,
        record,
        highlight_lock=highlight_lock,
        valid_rect=valid_rect,
    )


def _lock_snapshot(highlight_lock) -> dict[str, Any] | None:
    if highlight_lock is None:
        return None
    from scanny_boy.highlight_lock import HighlightLock

    lock = (
        highlight_lock
        if isinstance(highlight_lock, HighlightLock)
        else HighlightLock.from_dict(highlight_lock)
    )
    return None if lock is None else lock.to_dict()


def read_auto_neutral(record: dict | None) -> AutoNeutralBands | None:
    if not record:
        return None
    block = record.get("auto_neutral")
    if not isinstance(block, dict):
        return None

    def _pair(key: str) -> tuple[float, float] | None:
        value = block.get(key)
        if not isinstance(value, list) or len(value) != 2:
            return None
        if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value):
            return None
        try:
            a, b = (float(value[0]), float(value[1]))
        except (TypeError, ValueError):
            return None
        if not (np.isfinite(a) and np.isfinite(b)):
            return None
        return a, b

    shadow = _pair("shadow")
    highlight = _pair("highlight")
    if shadow is None and highlight is None:
        return None
    return AutoNeutralBands(shadow=shadow, highlight=highlight)


def auto_neutral_lock_matches(
    record: dict | None, highlight_lock
) -> bool:
    if not record:
        return False
    block = record.get("auto_neutral")
    if not isinstance(block, dict):
        return False
    stored = block.get("highlight_lock")
    current = _lock_snapshot(highlight_lock)
    return stored == current


def recompute_negative_auto_neutral(
    negative: NegativeRecord,
    roll_dir: Path,
    *,
    highlight_lock,
) -> bool:
    """Measure and write `auto_neutral` on `negative`. Returns whether the
    block changed."""
    if negative.output is None or negative.normalization is None:
        return False
    if len(negative.normalization.get("floors") or []) != 3:
        return False
    tiff_path = roll_dir / negative.output["name"]
    if not tiff_path.exists():
        return False
    measured = measure_auto_neutral_from_tiff(
        tiff_path,
        negative.normalization,
        highlight_lock=highlight_lock,
        valid_rect=negative.valid_rect,
    )
    previous = negative.normalization.get("auto_neutral")
    negative.normalization["auto_neutral"] = measured
    return measured != previous


def recompute_roll_auto_neutral(
    roll: RollManifest,
    roll_dir: Path,
    *,
    negative_ids: set[str] | None = None,
) -> bool:
    """Recompute `auto_neutral` for completed colour negatives. Returns
    whether any block changed."""
    changed = False
    for negative in roll.negatives:
        if negative.status != "completed" or negative.output is None:
            continue
        if negative_ids is not None and negative.negative_id not in negative_ids:
            continue
        if recompute_negative_auto_neutral(
            negative, roll_dir, highlight_lock=roll.highlight_lock
        ):
            changed = True
    return changed
