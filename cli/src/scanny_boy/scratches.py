"""Scratch detection and healing for colour negative rolls.

Long, thin, film-length scratches running along either canvas axis are
detected at stitch time and corrected by a level-dependent additive
log-density offset stored as an op.  The module is a leaf: it imports only
``numpy``, ``scipy.ndimage``, ``cv2``, and ``normalization``.

**Polarity convention.**  A scratch through the emulsion passes more light,
so ``val`` *rises* in the published negative — the same ``thin`` polarity
spots.py uses.  The correction subtracts from ``val``, restoring the
attenuated signal.

**All arithmetic runs in ``val``** (decoded normalised density).  Per-channel
spans from the negative's normalisation record convert ``val`` to log10
units, which is what makes the chroma signal physically comparable across
channels.

**Gate constants (provisional).**  Seeded from four negatives on the
``Recalibrate-Geometry`` roll; pending confirmation by
``cli/tools/measure_scratches.py`` (§6.3).  Observed true scratches:
mean path z −4.6 … −7.9, agreement 0.89 … 1.00, drift 14–30 px / 12 k.
Observed false positives (frame edges before step gate): mean z −4.3 … −4.8,
agreement 0.74 … 0.77, drift 50–100 px / 12 k.
"""

from __future__ import annotations

import base64
import dataclasses
import math
from typing import Any

import cv2
import numpy as np
from scipy.ndimage import gaussian_filter1d

from scanny_boy import normalization
from scanny_boy.normalization import ANALYSIS_BLOCK_PX

# Stamped into every ``scratches`` op; the ops-log parser gates on it.
DETECTOR_VERSION = 1

# --- detect ----------------------------------------------------------------

BAND_PX = 64
HALF_WIDTH_PX = 24
BG_START = 6
BG_END = 20

DP_MOVE_COST = 1.5
PATH_SUPPRESS_PX = 40
FILM_EXTENT_MARGIN_PX = 64

# --- gates (provisional — see module docstring) ----------------------------

GATE_MEAN_PATH_Z = -3.0
GATE_AGREEMENT = 0.85
GATE_MAX_DRIFT = 0.004

# --- fit -------------------------------------------------------------------

N_BINS = 12
MIN_ROWS_PER_BIN = 30
LEVEL_SIGMA = 12.0
STRIP_HALF_WIDTH = 32
BG_MARGIN = 24
TAPER_START = 20
TABLE_SCALE = 1e-5

# Region apply margins (4σ of level smoothing, plus strip half-width).
REGION_ROW_MARGIN = 48
REGION_COL_MARGIN = 32

FILL_CODE = int(
    normalization.encode_normalized(
        np.full((1, 1, 1), normalization.NORMALIZED_FILL, dtype=np.float32)
    ).ravel()[0]
)


@dataclasses.dataclass(frozen=True)
class Candidate:
    axis: str
    centres: np.ndarray
    score: float
    agreement: float
    drift: float


@dataclasses.dataclass(frozen=True)
class ScratchFit:
    axis: str
    band_px: int
    centres: list[float]
    half_width_px: int
    levels: list[list[float]]  # (n_bins, 3) quantile bin centres
    table: np.ndarray  # (n_bins, 2*half_width+1, 3) float32
    score: float
    agreement: float


def _decode_val(codes: np.ndarray) -> np.ndarray:
    return normalization.decode_normalized(codes).astype(np.float32)


def _span_array(spans: tuple[float, ...]) -> np.ndarray:
    return np.asarray(spans, dtype=np.float32)


def _encode_table(table: np.ndarray) -> str:
    scaled = np.clip(np.round(table / TABLE_SCALE), -32768, 32767).astype(np.int16)
    return base64.b64encode(scaled.tobytes()).decode("ascii")


def _decode_table(table_b64: str, n_bins: int, half_w: int) -> np.ndarray:
    strip_w = 2 * half_w + 1
    raw = base64.b64decode(table_b64)
    table = np.frombuffer(raw, dtype=np.int16).reshape(n_bins, strip_w, 3)
    return table.astype(np.float32) * TABLE_SCALE


def _film_extent_px(
    film_extent: Any, height: int, width: int
) -> tuple[int, int, int, int] | None:
    """Canvas-pixel insets (top, bottom, left, right), or None."""
    if film_extent is None:
        return None
    if hasattr(film_extent, "detected"):
        detected = film_extent.detected
        insets = film_extent.insets
    elif isinstance(film_extent, dict):
        detected = film_extent.get("detected")
        insets = film_extent.get("insets")
    else:
        return None
    if not detected or not insets or len(insets) != 4:
        return None
    top, bottom, left, right = insets
    return (
        int(top) * ANALYSIS_BLOCK_PX,
        int(bottom) * ANALYSIS_BLOCK_PX,
        int(left) * ANALYSIS_BLOCK_PX,
        int(right) * ANALYSIS_BLOCK_PX,
    )


def _near_film_extent(
    centres: np.ndarray,
    axis: str,
    margins: tuple[int, int, int, int],
    height: int,
    width: int,
) -> bool:
    top, bottom, left, right = margins
    for cx in centres:
        pos = float(cx)
        if axis == "vertical":
            if pos < left + FILM_EXTENT_MARGIN_PX:
                return True
            if pos >= width - right - FILM_EXTENT_MARGIN_PX:
                return True
        else:
            if pos < top + FILM_EXTENT_MARGIN_PX:
                return True
            if pos >= height - bottom - FILM_EXTENT_MARGIN_PX:
                return True
    return False


def _chroma_signal(val: np.ndarray, spans: np.ndarray) -> np.ndarray:
    return (
        spans[2] * val[..., 2]
        - (spans[0] * val[..., 0] + spans[1] * val[..., 1]) / 2.0
    )


def _band_response(s: np.ndarray, n_bands: int) -> np.ndarray:
    height, width = s.shape
    band_h = BAND_PX
    response = np.zeros((n_bands, width), dtype=np.float32)
    kernel = np.zeros(2 * BG_END + 1, dtype=np.float32)
    kernel[BG_END - 2 : BG_END + 3] = 1.0 / 5.0
    for u in range(BG_START, BG_END + 1):
        kernel[BG_END + u] -= 1.0 / (BG_END - BG_START + 1)
        kernel[BG_END - u] -= 1.0 / (BG_END - BG_START + 1)

    for b in range(n_bands):
        row_start = b * band_h
        row_end = min(row_start + band_h, height)
        rows = np.arange(row_start, row_end, 2)
        if len(rows) == 0:
            continue
        avg = s[rows].mean(axis=0)
        padded = np.pad(avg, BG_END, mode="reflect")
        response[b] = np.correlate(padded, kernel, mode="valid")
    return response


def _normalize_response(response: np.ndarray) -> np.ndarray:
    z = np.zeros_like(response)
    for b in range(response.shape[0]):
        row = response[b]
        med = float(np.median(row))
        mad = float(np.median(np.abs(row - med)))
        sigma = max(1.4826 * mad, 1e-6)
        z[b] = np.clip((row - med) / sigma, -8.0, 8.0)
    return z


def _dp_track(z: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    n_bands, width = z.shape
    inf = 1e18
    cost = np.full((n_bands, width), inf, dtype=np.float64)
    parent = np.full((n_bands, width), -1, dtype=np.int32)
    cost[0] = z[0].astype(np.float64)

    for i in range(1, n_bands):
        band_cost = z[i].astype(np.float64)
        for dx in (-1, 0, 1):
            src_cols = np.arange(width)
            dst_cols = src_cols + dx
            valid = (dst_cols >= 0) & (dst_cols < width)
            src = src_cols[valid]
            dst = dst_cols[valid]
            move = DP_MOVE_COST if dx != 0 else 0.0
            new_cost = cost[i - 1, src] + move + band_cost[dst]
            improved = new_cost < cost[i, dst]
            cost[i, dst] = np.where(improved, new_cost, cost[i, dst])
            parent[i, dst] = np.where(improved, src, parent[i, dst])

    end_scores = cost[-1] / max(n_bands, 1)
    return end_scores.astype(np.float32), parent


def _backtrack(
    parent: np.ndarray, end_scores: np.ndarray, z: np.ndarray
) -> list[tuple[float, np.ndarray]]:
    n_bands, width = z.shape
    peaks: list[tuple[float, int]] = []
    for x in range(1, width - 1):
        if end_scores[x] < end_scores[x - 1] and end_scores[x] < end_scores[x + 1]:
            peaks.append((float(end_scores[x]), x))
    if not peaks:
        best_x = int(np.argmin(end_scores))
        peaks = [(float(end_scores[best_x]), best_x)]
    peaks.sort()

    taken_mask = np.zeros(width, dtype=bool)
    results: list[tuple[float, np.ndarray]] = []
    for _score, x in peaks:
        if taken_mask[x]:
            continue
        centres = np.zeros(n_bands, dtype=np.float32)
        c = x
        for i in range(n_bands - 1, -1, -1):
            centres[i] = float(c)
            c = parent[i, c] if parent[i, c] >= 0 else c
        path_z = float(np.mean([z[i, int(centres[i])] for i in range(n_bands)]))
        results.append((path_z, centres))
        for i in range(n_bands):
            cx = round(centres[i])
            lo = max(0, cx - PATH_SUPPRESS_PX)
            hi = min(width, cx + PATH_SUPPRESS_PX + 1)
            taken_mask[lo:hi] = True
    return results


def _is_step_edge(val: np.ndarray, centres: np.ndarray, n_bands: int) -> bool:
    height, width = val.shape[:2]
    step_count = 0
    total = 0
    for b in range(n_bands):
        row_start = b * BAND_PX
        row_end = min(row_start + BAND_PX, height)
        cx = round(centres[b])
        if cx < BG_END or cx >= width - BG_END:
            continue
        left_bg = val[row_start:row_end, cx - BG_END : cx - BG_START].mean()
        right_bg = val[row_start:row_end, cx + BG_START : cx + BG_END].mean()
        core_val = val[row_start:row_end, max(0, cx - 2) : min(width, cx + 3)].mean()
        step = abs(left_bg - right_bg)
        depth = abs(core_val - (left_bg + right_bg) / 2.0)
        total += 1
        if depth > 0 and step > depth:
            step_count += 1
    return total > 0 and step_count > total / 2


def _refine_centres(s: np.ndarray, centres: np.ndarray, n_bands: int) -> np.ndarray:
    height, width = s.shape
    refined = np.copy(centres)
    for b in range(n_bands):
        row_start = b * BAND_PX
        row_end = min(row_start + BAND_PX, height)
        cx = round(centres[b])
        if cx < 3 or cx >= width - 3:
            continue
        profile = s[row_start:row_end, cx - 1 : cx + 2].mean(axis=0)
        denom = profile[0] - 2 * profile[1] + profile[2]
        if abs(denom) > 1e-10:
            offset = (profile[0] - profile[2]) / (2 * denom)
            offset = max(-1.0, min(1.0, offset))
            refined[b] = centres[b] + offset

    kernel_size = 5
    padded = np.pad(refined, kernel_size // 2, mode="edge")
    smoothed = np.array(
        [np.median(padded[i : i + kernel_size]) for i in range(n_bands)],
        dtype=np.float32,
    )
    return gaussian_filter1d(smoothed, sigma=2.0, mode="nearest")


def detect(
    image_codes: np.ndarray,
    spans: tuple[float, ...],
    film_extent: Any = None,
) -> list[Candidate]:
    """Detect long, thin scratches on a published TIFF's uint16 codes."""
    image_codes = np.asarray(image_codes)
    if image_codes.ndim != 3 or image_codes.shape[2] != 3:
        return []
    if image_codes.dtype != np.uint16:
        return []

    val = _decode_val(image_codes)
    span_arr = _span_array(spans)
    extent_margins = _film_extent_px(film_extent, val.shape[0], val.shape[1])

    candidates: list[Candidate] = []
    for axis in ("vertical", "horizontal"):
        work_val = np.swapaxes(val, 0, 1) if axis == "horizontal" else val
        height, width = work_val.shape[:2]
        n_bands = height // BAND_PX
        if n_bands < 3:
            continue

        s = _chroma_signal(work_val, span_arr)
        z = _normalize_response(_band_response(s, n_bands))
        end_scores, parent = _dp_track(z)
        paths = _backtrack(parent, end_scores, z)

        for path_z, raw_centres in paths:
            if path_z > GATE_MEAN_PATH_Z:
                continue
            band_z = np.array([z[b, int(raw_centres[b])] for b in range(n_bands)])
            agree = float(np.mean(band_z < -1.0))
            if agree < GATE_AGREEMENT:
                continue
            drift = (raw_centres.max() - raw_centres.min()) / max(height, 1)
            if drift > GATE_MAX_DRIFT:
                continue
            if _is_step_edge(work_val, raw_centres, n_bands):
                continue
            if extent_margins is not None and _near_film_extent(
                raw_centres, axis, extent_margins, height, width
            ):
                continue

            core_depths = np.zeros(3, dtype=np.float64)
            for b in range(n_bands):
                row_start = b * BAND_PX
                row_end = min(row_start + BAND_PX, height)
                cx = round(raw_centres[b])
                if cx < BG_END or cx >= width - BG_END:
                    continue
                for ch in range(3):
                    core = work_val[row_start:row_end, cx - 2 : cx + 3, ch].mean()
                    bg = (
                        work_val[row_start:row_end, cx - BG_END : cx - BG_START, ch].mean()
                        + work_val[row_start:row_end, cx + BG_START : cx + BG_END, ch].mean()
                    ) / 2.0
                    core_depths[ch] += bg - core
            if core_depths[2] <= 0:
                continue
            if core_depths[1] > core_depths[2]:
                continue
            if core_depths[0] > core_depths[1]:
                continue

            refined = _refine_centres(s, raw_centres, n_bands)
            candidates.append(
                Candidate(
                    axis=axis,
                    centres=refined,
                    score=round(path_z, 4),
                    agreement=round(agree, 4),
                    drift=round(drift, 6),
                )
            )

    candidates.sort(key=lambda c: c.score)
    kept: list[Candidate] = []
    for c in candidates:
        overlap = False
        for k in kept:
            if c.axis != k.axis:
                continue
            min_len = min(len(c.centres), len(k.centres))
            if min_len == 0:
                continue
            if np.any(np.abs(c.centres[:min_len] - k.centres[:min_len]) < PATH_SUPPRESS_PX):
                overlap = True
                break
        if not overlap:
            kept.append(c)
    return kept


def _background_line(strip: np.ndarray) -> np.ndarray:
    _h, w, _ch = strip.shape
    left = strip[:, : STRIP_HALF_WIDTH - BG_MARGIN + 1].mean(axis=1)
    right = strip[:, STRIP_HALF_WIDTH + BG_MARGIN - 1 :].mean(axis=1)
    u = np.arange(w, dtype=np.float32) - STRIP_HALF_WIDTH
    t = np.clip((u + BG_MARGIN) / (2 * BG_MARGIN), 0.0, 1.0)
    return (
        left[:, np.newaxis, :] * (1 - t[np.newaxis, :, np.newaxis])
        + right[:, np.newaxis, :] * t[np.newaxis, :, np.newaxis]
    ).astype(np.float32)


def _fit_scratch(val: np.ndarray, candidate: Candidate) -> ScratchFit:
    height, width = val.shape[:2]
    n_bands = height // BAND_PX
    centres = candidate.centres
    strip_w = 2 * STRIP_HALF_WIDTH + 1

    row_levels = np.zeros((n_bands * BAND_PX, 3), dtype=np.float32)
    strips: list[np.ndarray] = []

    for b in range(n_bands):
        row_start = b * BAND_PX
        row_end = min(row_start + BAND_PX, height)
        cx = centres[b]
        x0 = round(cx) - STRIP_HALF_WIDTH
        x1 = x0 + strip_w
        pad_left = max(0, -x0)
        pad_right = max(0, x1 - width)
        x0_c = max(0, x0)
        x1_c = min(width, x1)
        strip = val[row_start:row_end, x0_c:x1_c]
        if pad_left > 0 or pad_right > 0:
            strip = np.pad(
                strip, ((0, 0), (pad_left, pad_right), (0, 0)), mode="edge"
            )
        strips.append(strip)
        left_bg = strip[:, : STRIP_HALF_WIDTH - BG_MARGIN + 1].mean(axis=1)
        right_bg = strip[:, STRIP_HALF_WIDTH + BG_MARGIN - 1 :].mean(axis=1)
        band_levels = (left_bg + right_bg) / 2.0
        row_levels[row_start:row_end] = band_levels

    all_strips = np.concatenate(strips, axis=0)
    bgline = _background_line(all_strips)
    dev = all_strips - bgline
    row_levels = row_levels[: all_strips.shape[0]]

    table = np.zeros((N_BINS, strip_w, 3), dtype=np.float32)
    bin_centres = np.zeros((N_BINS, 3), dtype=np.float32)
    bin_counts = np.zeros(N_BINS, dtype=np.int32)

    channel_levels = row_levels[:, 1]
    valid = np.isfinite(channel_levels)
    if valid.sum() >= N_BINS:
        edges = np.percentile(channel_levels[valid], np.linspace(0, 100, N_BINS + 1))
        for bi in range(N_BINS):
            mask = (channel_levels >= edges[bi]) & (channel_levels < edges[bi + 1])
            if bi == N_BINS - 1:
                mask = mask | (channel_levels == edges[bi + 1])
            count = int(mask.sum())
            bin_counts[bi] = count
            if count >= MIN_ROWS_PER_BIN:
                table[bi] = np.median(dev[mask], axis=0)
                bin_centres[bi] = row_levels[mask].mean(axis=0)
            else:
                bin_centres[bi] = np.full(3, edges[bi], dtype=np.float32)

    merged_table: list[np.ndarray] = []
    merged_centres: list[np.ndarray] = []
    i = 0
    while i < N_BINS:
        accum = table[i].copy()
        accum_count = int(bin_counts[i])
        accum_centre = bin_centres[i].copy()
        while accum_count < MIN_ROWS_PER_BIN and i + 1 < N_BINS:
            i += 1
            accum += table[i]
            accum_count += int(bin_counts[i])
            accum_centre = (accum_centre + bin_centres[i]) / 2.0
        if accum_count > 0:
            merged_table.append(accum / max(accum_count, 1))
            merged_centres.append(accum_centre)
        i += 1

    if not merged_table:
        merged_table = [np.zeros((strip_w, 3), dtype=np.float32)]
        merged_centres = [np.zeros(3, dtype=np.float32)]

    merged_table_arr = np.array(merged_table, dtype=np.float32)
    merged_centres_arr = np.array(merged_centres, dtype=np.float32)

    u_axis = np.arange(strip_w, dtype=np.float32) - STRIP_HALF_WIDTH
    taper = np.where(
        np.abs(u_axis) >= BG_MARGIN,
        0.0,
        np.where(
            np.abs(u_axis) >= TAPER_START,
            1.0 - (np.abs(u_axis) - TAPER_START) / (BG_MARGIN - TAPER_START),
            1.0,
        ),
    )
    merged_table_arr *= taper[np.newaxis, :, np.newaxis]

    # Store only the correction window (±half_width_px), not the full strip.
    u0 = STRIP_HALF_WIDTH - HALF_WIDTH_PX
    u1 = STRIP_HALF_WIDTH + HALF_WIDTH_PX + 1
    merged_table_arr = merged_table_arr[:, u0:u1, :]
    store_w = merged_table_arr.shape[1]

    if merged_table_arr.shape[0] < N_BINS:
        pad = N_BINS - merged_table_arr.shape[0]
        merged_table_arr = np.concatenate(
            [merged_table_arr, np.zeros((pad, store_w, 3), dtype=np.float32)], axis=0
        )
        merged_centres_arr = np.concatenate(
            [merged_centres_arr, np.zeros((pad, 3), dtype=np.float32)], axis=0
        )
    elif merged_table_arr.shape[0] > N_BINS:
        merged_table_arr = merged_table_arr[:N_BINS]
        merged_centres_arr = merged_centres_arr[:N_BINS]

    return ScratchFit(
        axis=candidate.axis,
        band_px=BAND_PX,
        centres=centres.tolist(),
        half_width_px=HALF_WIDTH_PX,
        levels=merged_centres_arr.tolist(),
        table=merged_table_arr,
        score=candidate.score,
        agreement=candidate.agreement,
    )


def fit(image_codes: np.ndarray, candidate: Candidate) -> ScratchFit:
    val = _decode_val(np.asarray(image_codes))
    if candidate.axis == "horizontal":
        val = np.swapaxes(val, 0, 1)
    result = _fit_scratch(val, candidate)
    return result


def scratches_params(
    canvas: tuple[int, int],
    fits: list[ScratchFit],
    enabled: bool,
) -> dict[str, Any]:
    scratches_list = []
    for f in fits:
        n_bins = f.table.shape[0]
        scratches_list.append(
            {
                "axis": f.axis,
                "band_px": f.band_px,
                "centres": [round(c, 2) for c in f.centres],
                "half_width_px": f.half_width_px,
                "levels": [[round(v, 6) for v in row] for row in f.levels[:n_bins]],
                "table": _encode_table(f.table),
                "score": f.score,
                "agreement": f.agreement,
            }
        )
    return {
        "detector_version": DETECTOR_VERSION,
        "source": "auto",
        "enabled": enabled,
        "canvas": [canvas[0], canvas[1]],
        "scratches": scratches_list,
    }


def is_live(params: dict | None, shape: tuple[int, int]) -> bool:
    if not params or not params.get("enabled"):
        return False
    canvas = params.get("canvas")
    if (
        not isinstance(canvas, (list, tuple))
        or len(canvas) != 2
        or (int(canvas[0]), int(canvas[1])) != (shape[1], shape[0])
    ):
        return False
    scratch_list = params.get("scratches")
    return isinstance(scratch_list, list) and len(scratch_list) > 0


def _centre_path(centres: np.ndarray, band_px: int, height: int) -> np.ndarray:
    n_bands = len(centres)
    ys = np.arange(height, dtype=np.float32)
    band_idx = ys / band_px
    b0 = np.clip(np.floor(band_idx).astype(int), 0, n_bands - 1)
    b1 = np.clip(b0 + 1, 0, n_bands - 1)
    t = np.clip(band_idx - b0, 0.0, 1.0)
    return centres[b0] * (1.0 - t) + centres[b1] * t


def _recompute_row_levels(
    val: np.ndarray, centre_path: np.ndarray, half_w: int
) -> np.ndarray:
    """Per-row background level at u=0, Gaussian-smoothed along the scratch."""
    height, width = val.shape[:2]
    levels = np.zeros((height, 3), dtype=np.float32)
    for y in range(height):
        cx = centre_path[y]
        x0 = int(round(cx)) - STRIP_HALF_WIDTH
        x1 = x0 + 2 * STRIP_HALF_WIDTH + 1
        pad_left = max(0, -x0)
        pad_right = max(0, x1 - width)
        x0_c = max(0, x0)
        x1_c = min(width, x1)
        row = val[y : y + 1, x0_c:x1_c]
        if row.shape[1] == 0:
            levels[y] = val[y, min(max(int(round(cx)), 0), width - 1)]
            continue
        if pad_left > 0 or pad_right > 0:
            row = np.pad(row, ((0, 0), (pad_left, pad_right), (0, 0)), mode="edge")
        left = row[:, : STRIP_HALF_WIDTH - BG_MARGIN + 1].mean(axis=1)
        right = row[:, STRIP_HALF_WIDTH + BG_MARGIN - 1 :].mean(axis=1)
        levels[y] = (left + right) / 2.0
    for ch in range(3):
        levels[:, ch] = cv2.GaussianBlur(
            levels[:, ch : ch + 1], (0, 0), sigmaX=LEVEL_SIGMA
        ).ravel()
    return levels


def _lookup_correction(
    table: np.ndarray,
    bin_levels: np.ndarray,
    u: np.ndarray,
    level: np.ndarray,
    half_w: int,
) -> np.ndarray:
    """Bilinear lookup: u and level are (N,) and (N,3). Returns (N,3)."""
    n_bins = table.shape[0]
    strip_w = table.shape[1]
    u_idx = u + half_w
    u0 = np.floor(u_idx).astype(int)
    u1 = np.clip(u0 + 1, 0, strip_w - 1)
    u0 = np.clip(u0, 0, strip_w - 1)
    t_u = u_idx - u0

    corrections = np.zeros((len(u), 3), dtype=np.float32)
    for ch in range(3):
        lev = level[:, ch]
        centres = bin_levels[:, ch]
        valid = centres != 0
        if not np.any(valid):
            continue
        idx = np.searchsorted(centres, lev) - 1
        b0 = np.clip(idx, 0, n_bins - 1)
        b1 = np.clip(b0 + 1, 0, n_bins - 1)
        lev0 = centres[b0]
        lev1 = centres[b1]
        same = b0 == b1
        t_l = np.where(
            same | (np.abs(lev1 - lev0) < 1e-10),
            0.0,
            np.clip((lev - lev0) / np.maximum(lev1 - lev0, 1e-10), 0.0, 1.0),
        )
        c0 = table[b0, u0, ch] * (1 - t_u) + table[b0, u1, ch] * t_u
        c1 = table[b1, u0, ch] * (1 - t_u) + table[b1, u1, ch] * t_u
        corrections[:, ch] = np.where(same, c0, c0 * (1 - t_l) + c1 * t_l)
    return corrections


def _apply_one_scratch(val: np.ndarray, image: np.ndarray, scratch: dict) -> None:
    """Apply one scratch in place on val and image (uint16)."""
    centres = np.asarray(scratch["centres"], dtype=np.float32)
    n_bins = len(scratch["levels"])
    bin_levels = np.asarray(scratch["levels"], dtype=np.float32)
    half_w = int(scratch.get("half_width_px", HALF_WIDTH_PX))
    band_px = int(scratch.get("band_px", BAND_PX))
    table = _decode_table(scratch["table"], n_bins, half_w)

    height, width = val.shape[:2]
    centre_path = _centre_path(centres, band_px, height)
    row_levels = _recompute_row_levels(val, centre_path, half_w)

    for y in range(height):
        cx = centre_path[y]
        x_lo = max(0, int(math.floor(cx - half_w)))
        x_hi = min(width, int(math.ceil(cx + half_w)) + 1)
        if x_lo >= x_hi:
            continue
        xs = np.arange(x_lo, x_hi, dtype=np.float32)
        fill_mask = np.all(image[y, x_lo:x_hi] == FILL_CODE, axis=1)
        if fill_mask.all():
            continue
        u = xs - cx
        level = np.broadcast_to(row_levels[y], (len(xs), 3))
        corr = _lookup_correction(table, bin_levels, u, level, half_w)
        corr[fill_mask] = 0.0
        val[y, x_lo:x_hi] -= corr

    clipped = np.clip(
        val,
        -normalization.NORMALIZED_HEADROOM_LOW,
        1.0 + normalization.NORMALIZED_HEADROOM_HIGH,
    )
    image[:] = normalization.encode_normalized(clipped).astype(np.uint16)


def _params_enabled(params: dict | None) -> bool:
    if not params or not params.get("enabled"):
        return False
    scratches_list = params.get("scratches")
    return isinstance(scratches_list, list) and len(scratches_list) > 0


def apply(
    image_codes: np.ndarray,
    params: dict | None,
    *,
    region: tuple[int, int, int, int] | None = None,
) -> np.ndarray:
    """Apply scratch corrections. With ``region=(x,y,w,h)`` the caller has
    read the margin; this returns the healed unpadded rect."""
    image_codes = np.asarray(image_codes)
    if image_codes.ndim != 3 or image_codes.shape[2] != 3:
        return image_codes
    if region is None and not is_live(params, image_codes.shape[:2]):
        return image_codes
    if region is not None and not _params_enabled(params):
        return image_codes

    image = image_codes.copy()
    val = _decode_val(image)

    for scratch in params.get("scratches", []):
        axis = scratch.get("axis", "vertical")
        if axis == "horizontal":
            val_t = np.swapaxes(val, 0, 1)
            image_t = np.swapaxes(image, 0, 1)
            _apply_one_scratch(val_t, image_t, scratch)
            val = np.swapaxes(val_t, 0, 1)
            image = np.swapaxes(image_t, 0, 1)
        else:
            _apply_one_scratch(val, image, scratch)

    if region is not None:
        x, y, w, h = region
        return image[y : y + h, x : x + w]
    return image
