"""Per-frame capture-time analysis for tethered shooting.

All constants live here. Thresholds marked provisional are shipped unmeasured
and tuned by use (docs/TETHER_PLAN.md §6.5).
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from scanny_boy.events import Code
from scanny_boy.metadata import (
    UnreadableRawError,
    UnsupportedRawError,
    read_source_settings,
)
from scanny_boy.normalization import SCAN_CLIP_WARN, measure_clip_fractions
from scanny_boy.raw_decode import RAW_PARAMS, decode_raw

# --- provisional, unmeasured (§6.5) ---------------------------------------

# Dense end: warn when a channel's 0.5th percentile sits fewer stops above
# black than this. Starting value: half a stop — conservative enough to
# surface genuinely underexposed frames without nagging on normal negatives.
DENSE_END_WARN_STOPS = 0.5

# Focus drift: warn when the frame's median region ratio falls further below
# the session baseline than this fraction (0.25 = 25% softer).
FOCUS_DRIFT_WARN = 0.25

# Focus tilt: warn when the spread of region ratios exceeds this fraction of
# the baseline median.
FOCUS_TILT_WARN = 0.20

# --- measured layout -------------------------------------------------------

FOCUS_GRID = (3, 3)
FOCUS_MIN_TEXTURE = 1e-4


def _half_size_params() -> dict:
    return {**RAW_PARAMS, "half_size": True}


def _linear_float(pixels: np.ndarray) -> np.ndarray:
    return pixels.astype(np.float64) / 65535.0


def _dense_end_stops(linear: np.ndarray) -> tuple[float, float, float]:
    """Each channel's 0.5th percentile, in stops above the black level."""
    black = 1e-6
    stops: list[float] = []
    for channel in range(3):
        values = linear[..., channel].ravel()
        floor = max(float(np.percentile(values, 0.5)), black)
        stops.append(math.log2(floor / black))
    return tuple(stops)


def _region_focus_ratios(green: np.ndarray) -> list[float | None]:
    """High/mid band energy ratio on a regional grid over the green channel."""
    rows, cols = FOCUS_GRID
    height, width = green.shape
    ratios: list[float | None] = []
    for row in range(rows):
        y0 = row * height // rows
        y1 = (row + 1) * height // rows
        for col in range(cols):
            x0 = col * width // cols
            x1 = (col + 1) * width // cols
            patch = green[y0:y1, x0:x1]
            if patch.size == 0:
                ratios.append(None)
                continue
            variance = float(np.var(patch))
            if variance < FOCUS_MIN_TEXTURE:
                ratios.append(None)
                continue
            # Laplacian energy as a high-frequency proxy; mean absolute
            # gradient as the mid band.
            lap = np.abs(
                -4 * patch[1:-1, 1:-1]
                + patch[:-2, 1:-1]
                + patch[2:, 1:-1]
                + patch[1:-1, :-2]
                + patch[1:-1, 2:]
            )
            high = float(np.mean(lap * lap))
            mid = float(
                np.mean(np.abs(np.diff(patch, axis=0)))
                + np.mean(np.abs(np.diff(patch, axis=1)))
            )
            mid = max(mid, 1e-8)
            ratios.append(high / mid)
    return ratios


def analyze_frame(
    frame: Path,
    *,
    baseline_ratios: list[float | None] | None = None,
) -> dict[str, Any]:
    """Analyze one capture frame and return the ``frame_analyzed`` payload."""
    try:
        decoded = decode_raw(frame, params=_half_size_params())
    except (UnsupportedRawError, UnreadableRawError) as exc:
        code = (
            Code.UNSUPPORTED_RAW
            if isinstance(exc, UnsupportedRawError)
            else Code.UNREADABLE_RAW
        )
        raise CaptureAnalysisError(code, str(exc)) from exc

    linear = _linear_float(decoded.pixels)
    clip_fractions = measure_clip_fractions(linear)
    dense_end_stops = _dense_end_stops(linear)
    green = linear[..., 1]
    focus_regions = _region_focus_ratios(green)

    baseline_values = [value for value in (baseline_ratios or []) if value is not None]
    region_values = [value for value in focus_regions if value is not None]
    focus_relative: float | None = None
    focus_spread: float | None = None
    if baseline_values and region_values:
        baseline_median = float(np.median(baseline_values))
        frame_median = float(np.median(region_values))
        if baseline_median > 0:
            focus_relative = frame_median / baseline_median
            focus_spread = (
                float(np.max(region_values) - np.min(region_values)) / baseline_median
            )

    warnings: list[str] = []
    if any(fraction > SCAN_CLIP_WARN for fraction in clip_fractions):
        warnings.append(Code.CAPTURE_CLIPPED.value)
    if any(stops < DENSE_END_WARN_STOPS for stops in dense_end_stops):
        warnings.append(Code.CAPTURE_DENSE_END_LOW.value)
    if focus_relative is not None and focus_relative < (1.0 - FOCUS_DRIFT_WARN):
        warnings.append(Code.CAPTURE_FOCUS_DRIFT.value)
    if focus_spread is not None and focus_spread > FOCUS_TILT_WARN:
        warnings.append(Code.CAPTURE_FOCUS_TILT.value)

    return {
        "frame": str(frame),
        "clip_fractions": list(clip_fractions),
        "dense_end_stops": list(dense_end_stops),
        "focus_regions": focus_regions,
        "focus_relative": focus_relative,
        "focus_spread": focus_spread,
        "warnings": warnings,
    }


class CaptureAnalysisError(Exception):
    def __init__(self, code: Code, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def append_log_entry(log_path: Path, entry: dict[str, Any]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


def load_log_entries(log_path: Path) -> list[dict[str, Any]]:
    if not log_path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entries.append(json.loads(line))
    return entries


def summarize_log(log_path: Path, *, base_frame: Path | None = None) -> dict[str, Any]:
    entries = load_log_entries(log_path)
    warning_counts: dict[str, int] = {}
    focus_trend: list[float | None] = []
    negatives: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        for code in entry.get("warnings", []):
            warning_counts[code] = warning_counts.get(code, 0) + 1
        stem = Path(entry["frame"]).stem
        prefix = stem.rsplit("_", 1)[0] if "_" in stem else stem
        negatives.setdefault(prefix, []).append(entry)

    for group in negatives.values():
        medians = [
            value
            for item in group
            for value in [item.get("focus_relative")]
            if value is not None
        ]
        focus_trend.append(float(np.median(medians)) if medians else None)

    exposure_mismatches: list[str] = []
    if base_frame is not None:
        try:
            base_settings = read_source_settings(base_frame)
        except (UnsupportedRawError, UnreadableRawError):
            base_settings = None
        if base_settings is not None:
            for entry in entries:
                frame = Path(entry["frame"])
                try:
                    settings = read_source_settings(frame)
                except (UnsupportedRawError, UnreadableRawError):
                    continue
                if (
                    settings.exposure_time != base_settings.exposure_time
                    or settings.f_number != base_settings.f_number
                    or settings.iso != base_settings.iso
                ):
                    exposure_mismatches.append(frame.name)

    return {
        "frames": len(entries),
        "warning_counts": warning_counts,
        "focus_trend": focus_trend,
        "exposure_mismatches": exposure_mismatches,
    }
