"""Scratch detection and healing for colour negative rolls.

Long, thin, film-length scratches running along either canvas axis are
detected at stitch time and corrected by a level-dependent additive
log-density offset stored as an op.  The module is a leaf: it imports only
``numpy``, ``scipy.ndimage``, and ``normalization``.

**Polarity convention.**  A scratch through the emulsion passes more light,
so ``val`` *rises* in the published negative — the same ``thin`` polarity
spots.py uses.  The correction subtracts from ``val``, restoring the
attenuated signal.

**All arithmetic runs in ``val``** (decoded normalised density).  Per-channel
spans from the negative's normalisation record convert ``val`` to log10
units, which is what makes the chroma signal physically comparable across
channels.
"""

from __future__ import annotations

import base64
import dataclasses
import math
from typing import Any

import cv2
import numpy as np

from scanny_boy import normalization

# Stamped into every ``scratches`` op; the ops-log parser gates on it.
DETECTOR_VERSION = 1

# --- detect ----------------------------------------------------------------

# Band height in rows.
BAND_PX = 64
# Half-width of the correction window in columns.
HALF_WIDTH_PX = 24
# Background margin for the kernel and the strip.
BG_START = 6
BG_END = 20

# DP move cost in z-units.
DP_MOVE_COST = 1.5
# Suppression radius around each accepted path (columns).
PATH_SUPPRESS_PX = 40

# --- gates (seeded, pending calibration tool §6.3) -------------------------

# Mean path z must be at most this.
GATE_MEAN_PATH_Z = -3.0
# Fraction of bands with z < -1 must be at least this.
GATE_AGREEMENT = 0.85
# Max drift: (max_x - min_x) / length.
GATE_MAX_DRIFT = 0.004
# Chroma-sign gate constant.
GATE_CHROMA_MIN_RATIO = 0.0  # blue loss must be > green loss

# --- fit -------------------------------------------------------------------

# Number of quantile bins for the level table.
N_BINS = 12
# Minimum rows per bin before merging.
MIN_ROWS_PER_BIN = 30
# Level smoothing sigma in rows.
LEVEL_SIGMA = 12.0
# Strip half-width in columns (must be > HALF_WIDTH_PX).
STRIP_HALF_WIDTH = 32
# Background margin (|u| >= this).
BG_MARGIN = 24
# Taper start (|u| >= this, linear taper to zero at BG_MARGIN).
TAPER_START = 20

# The stitching fill sentinel in code space, derived once.
FILL_CODE = int(
    normalization.encode_normalized(
        np.full((1, 1, 1), normalization.NORMALIZED_FILL, dtype=np.float32)
    ).ravel()[0]
)

# One uint16 code in val units.
_CODE_TO_VAL = (
    1.0 + normalization.NORMALIZED_HEADROOM_LOW + normalization.NORMALIZED_HEADROOM_HIGH
) / 65535.0


# --- data structures --------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Candidate:
    """One detected scratch path before fitting."""

    axis: str  # ``"vertical"`` or ``"horizontal"``
    centres: np.ndarray  # (n_bands,) subpixel x positions
    score: float  # mean path z
    agreement: float  # fraction of bands with z < -1
    drift: float  # (max_x - min_x) / length


@dataclasses.dataclass(frozen=True)
class ScratchFit:
    """The fitted correction table for one scratch."""

    axis: str
    band_px: int
    centres: list[float]  # one per band
    half_width_px: int
    levels: list[list[float]]  # (n_bands, 3) — per-band background level per channel
    table: np.ndarray  # (N_BINS, 2*HALF_WIDTH_PX+1, 3) float32
    score: float
    agreement: float


# --- helpers ----------------------------------------------------------------


def _decode_val(codes: np.ndarray) -> np.ndarray:
    """uint16 codes -> float32 val."""
    return normalization.decode_normalized(codes).astype(np.float32)


def _span_array(spans: tuple[float, ...]) -> np.ndarray:
    """Channel spans as a (3,) float32 array."""
    return np.asarray(spans, dtype=np.float32)


# --- detect -----------------------------------------------------------------


def _chroma_signal(
    val: np.ndarray, spans: np.ndarray
) -> np.ndarray:
    """The scratch-detection chroma signal: ``s = span_B*val_B -
    (span_R*val_R + span_G*val_G)/2``.  Shape (H, W)."""
    return (
        spans[2] * val[..., 2]
        - (spans[0] * val[..., 0] + spans[1] * val[..., 1]) / 2.0
    )


def _band_response(
    s: np.ndarray, n_bands: int
) -> np.ndarray:
    """Per-band, per-column matched-filter response.  Shape (n_bands, W).

    For each 64-row band the signal is averaged to one row (sampling every
    second row), then correlated with a kernel that is ``+1`` over the core
    ``|u| <= 2`` and ``-1`` over the background ``6 <= |u| <= 20``.
    """
    height, width = s.shape
    band_h = BAND_PX
    response = np.zeros((n_bands, width), dtype=np.float32)

    # Build the matched kernel (1-D, centred at 0).
    kernel = np.zeros(2 * BG_END + 1, dtype=np.float32)
    kernel[BG_END - 2 : BG_END + 3] = 1.0 / 5.0  # core: |u| <= 2
    for u in range(BG_START, BG_END + 1):
        kernel[BG_END + u] -= 1.0 / (BG_END - BG_START + 1)
        kernel[BG_END - u] -= 1.0 / (BG_END - BG_START + 1)

    for b in range(n_bands):
        row_start = b * band_h
        row_end = min(row_start + band_h, height)
        # Average every second row in the band.
        rows = np.arange(row_start, row_end, 2)
        if len(rows) == 0:
            continue
        avg = s[rows].mean(axis=0)  # (W,)
        # Correlate with the kernel (valid mode, pad reflected).
        padded = np.pad(avg, BG_END, mode="reflect")
        resp = np.correlate(padded, kernel, mode="valid")
        response[b] = resp

    return response


def _normalize_response(response: np.ndarray) -> np.ndarray:
    """Per-band robust z-score, clipped to +/-8."""
    z = np.zeros_like(response)
    for b in range(response.shape[0]):
        row = response[b]
        med = float(np.median(row))
        mad = float(np.median(np.abs(row - med)))
        sigma = max(1.4826 * mad, 1e-6)
        z[b] = np.clip((row - med) / sigma, -8.0, 8.0)
    return z


def _dp_track(z: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Dynamic-programming min-cost path through the band responses.

    Returns ``(end_scores, paths)`` where ``end_scores`` is (W,) and
    ``paths`` is (n_bands, W) storing the parent column at each step.
    """
    n_bands, width = z.shape
    INF = 1e18
    # cost[i, x] = min cost to reach band i at column x.
    cost = np.full((n_bands, width), INF, dtype=np.float64)
    parent = np.full((n_bands, width), -1, dtype=np.int32)

    # Band 0: cost is just -z (we want large negative z).
    cost[0] = -z[0].astype(np.float64)

    for i in range(1, n_bands):
        band_cost = -z[i].astype(np.float64)
        for dx in (-1, 0, 1):
            shift = dx
            src_cols = np.arange(width)
            dst_cols = src_cols + shift
            valid = (dst_cols >= 0) & (dst_cols < width)
            src = src_cols[valid]
            dst = dst_cols[valid]
            new_cost = cost[i - 1, src] + DP_MOVE_COST * (dx != 0) + band_cost[dst]
            improved = new_cost < cost[i, dst]
            cost[i, dst] = np.where(improved, new_cost, cost[i, dst])
            parent[i, dst] = np.where(improved, src, parent[i, dst])

    # End score = total cost / n_bands.
    end_scores = cost[-1] / max(n_bands, 1)
    return end_scores.astype(np.float32), parent


def _backtrack(
    parent: np.ndarray, end_scores: np.ndarray, z: np.ndarray
) -> list[tuple[int, np.ndarray]]:
    """Backtrack from local minima of the end score, suppressing
    ``PATH_SUPPRESS_PX`` around each taken path.  Returns a list of
    ``(path_z, centres)`` tuples."""
    n_bands, width = z.shape
    peaks: list[tuple[float, int]] = []
    # Find local minima in end_scores.
    for x in range(1, width - 1):
        if end_scores[x] < end_scores[x - 1] and end_scores[x] < end_scores[x + 1]:
            peaks.append((float(end_scores[x]), x))
    # Sort by score (most negative first).
    peaks.sort()

    taken_mask = np.zeros(width, dtype=bool)
    results: list[tuple[int, np.ndarray]] = []
    for score, x in peaks:
        if taken_mask[x]:
            continue
        # Trace back.
        centres = np.zeros(n_bands, dtype=np.float32)
        c = x
        for i in range(n_bands - 1, -1, -1):
            centres[i] = float(c)
            c = parent[i, c] if parent[i, c] >= 0 else c
        # Mean z along this path.
        path_z = float(np.mean([z[i, int(centres[i])] for i in range(n_bands)]))
        results.append((path_z, centres))
        # Suppress around this path.
        for i in range(n_bands):
            cx = int(round(centres[i]))
            lo = max(0, cx - PATH_SUPPRESS_PX)
            hi = min(width, cx + PATH_SUPPRESS_PX + 1)
            taken_mask[lo:hi] = True

    return results


def _is_step_edge(
    val: np.ndarray, centres: np.ndarray, n_bands: int
) -> bool:
    """True when the background across the candidate is a step rather than
    continuous — i.e. the candidate is a frame edge, not a scratch.

    Per band, compare the mean of the left background (u in [-BG_END, -BG_START])
    and the right background (u in [BG_START, BG_END]).  If the step exceeds
    the core depth in more than half the bands, reject."""
    height, width = val.shape[:2]
    band_h = BAND_PX
    step_count = 0
    total = 0
    for b in range(n_bands):
        row_start = b * band_h
        row_end = min(row_start + band_h, height)
        cx = int(round(centres[b]))
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


def _refine_centres(
    s: np.ndarray, centres: np.ndarray, n_bands: int
) -> np.ndarray:
    """Per band, refine the scratch centre to subpixel accuracy using a
    three-point parabola, then smooth with a 5-band median and Gaussian
    sigma=2.  Returns (n_bands,) float32 centres."""
    height, width = s.shape
    band_h = BAND_PX
    refined = np.copy(centres)

    for b in range(n_bands):
        row_start = b * band_h
        row_end = min(row_start + band_h, height)
        cx = int(round(centres[b]))
        if cx < 3 or cx >= width - 3:
            continue
        # Sample the chroma profile at cx-1, cx, cx+1 (averaged over the band).
        profile = s[row_start:row_end, cx - 1 : cx + 2].mean(axis=0)  # (3,)
        # Three-point parabola: minimum at offset = (p[0] - p[2]) / (2*(p[0] - 2*p[1] + p[2]))
        denom = profile[0] - 2 * profile[1] + profile[2]
        if abs(denom) > 1e-10:
            offset = (profile[0] - profile[2]) / (2 * denom)
            offset = max(-1.0, min(1.0, offset))
            refined[b] = centres[b] + offset

    # 5-band median.
    kernel_size = 5
    padded = np.pad(refined, kernel_size // 2, mode="edge")
    smoothed = np.array(
        [np.median(padded[i : i + kernel_size]) for i in range(n_bands)],
        dtype=np.float32,
    )
    # Gaussian smoothing, sigma=2 bands.
    from scipy.ndimage import gaussian_filter1d

    smoothed = gaussian_filter1d(smoothed, sigma=2.0, mode="nearest")
    return smoothed


def detect(
    image_codes: np.ndarray,
    spans: tuple[float, ...],
    film_extent: Any = None,
) -> list[Candidate]:
    """Detect long, thin scratches on a published TIFF's uint16 codes.

    Runs once per axis: vertical as-is, horizontal on the transposed view.
    Returns a list of accepted ``Candidate`` objects (may be empty).

    ``image_codes`` is (H, W, 3) uint16 for a colour roll.
    ``spans`` is the per-channel (ceil - floor) from the normalisation record.
    ``film_extent`` is the roll's ``FilmExtent`` record (currently unused but
    reserved for the 64px boundary exclusion).
    """
    image_codes = np.asarray(image_codes)
    if image_codes.ndim != 3 or image_codes.shape[2] != 3:
        return []
    if image_codes.dtype != np.uint16:
        return []

    val = _decode_val(image_codes)
    span_arr = _span_array(spans)

    candidates: list[Candidate] = []
    for axis in ("vertical", "horizontal"):
        if axis == "horizontal":
            work_val = np.swapaxes(val, 0, 1)
        else:
            work_val = val

        height, width = work_val.shape[:2]
        n_bands = height // BAND_PX
        if n_bands < 3:
            continue

        s = _chroma_signal(work_val, span_arr)
        response = _band_response(s, n_bands)
        z = _normalize_response(response)
        end_scores, parent = _dp_track(z)
        paths = _backtrack(parent, end_scores, z)

        for path_z, raw_centres in paths:
            if path_z > GATE_MEAN_PATH_Z:
                continue
            # Agreement: fraction of bands with z < -1.
            band_z = np.array(
                [z[b, int(raw_centres[b])] for b in range(n_bands)]
            )
            agree = float(np.mean(band_z < -1.0))
            if agree < GATE_AGREEMENT:
                continue
            # Drift.
            drift = (raw_centres.max() - raw_centres.min()) / max(n_bands, 1)
            if drift > GATE_MAX_DRIFT:
                continue
            # Step-edge gate.
            if _is_step_edge(work_val, raw_centres, n_bands):
                continue
            # Chroma sign gate: blue loss > green loss >= red loss.
            core_depths = np.zeros(3, dtype=np.float64)
            bg_depths = np.zeros(3, dtype=np.float64)
            for b in range(n_bands):
                row_start = b * BAND_PX
                row_end = min(row_start + BAND_PX, height)
                cx = int(round(raw_centres[b]))
                if cx < BG_END or cx >= width - BG_END:
                    continue
                for ch in range(3):
                    core = work_val[row_start:row_end, cx - 2 : cx + 3, ch].mean()
                    bg = (
                        work_val[row_start:row_end, cx - BG_END : cx - BG_START, ch].mean()
                        + work_val[row_start:row_end, cx + BG_START : cx + BG_END, ch].mean()
                    ) / 2.0
                    core_depths[ch] += bg - core
                    bg_depths[ch] += bg
            if core_depths[2] <= 0:
                continue  # no blue loss
            if core_depths[1] > core_depths[2]:
                continue  # green loss must not exceed blue
            if core_depths[0] > core_depths[1]:
                continue  # red loss must not exceed green

            # Refine centres.
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

    # Suppress overlapping candidates.
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
            c_c = c.centres[:min_len]
            k_c = k.centres[:min_len]
            if np.any(np.abs(c_c - k_c) < PATH_SUPPRESS_PX):
                overlap = True
                break
        if not overlap:
            kept.append(c)

    return kept


# --- fit --------------------------------------------------------------------


def _background_line(strip: np.ndarray) -> np.ndarray:
    """Per-row background line: mean of u <= -BG_MARGIN and u >= BG_MARGIN,
    joined linearly across u.  Shape (H, W_strip, 3)."""
    h, w, ch = strip.shape
    left = strip[:, : STRIP_HALF_WIDTH - BG_MARGIN + 1].mean(axis=1)  # (H, 3)
    right = strip[:, STRIP_HALF_WIDTH + BG_MARGIN - 1 :].mean(axis=1)  # (H, 3)
    # Linear interpolation across u.
    u = np.arange(w, dtype=np.float32) - STRIP_HALF_WIDTH
    left_u = -float(BG_MARGIN)
    right_u = float(BG_MARGIN)
    t = (u - left_u) / (right_u - left_u)  # (W_strip,)
    t = np.clip(t, 0.0, 1.0)
    bgline = left[:, np.newaxis, :] * (1 - t[np.newaxis, :, np.newaxis]) + right[:, np.newaxis, :] * t[np.newaxis, :, np.newaxis]
    return bgline.astype(np.float32)


def _fit_scratch(
    val: np.ndarray, candidate: Candidate
) -> ScratchFit:
    """Fit the level-dependent correction table for one candidate scratch."""
    height, width = val.shape[:2]
    n_bands = height // BAND_PX

    # Build the per-band smoothed background levels.
    centres = candidate.centres
    levels = np.zeros((n_bands, 3), dtype=np.float32)
    for b in range(n_bands):
        row_start = b * BAND_PX
        row_end = min(row_start + BAND_PX, height)
        cx = int(round(centres[b]))
        # Background at u=0: the mean of the background line at the centre.
        left_bg = val[row_start:row_end, max(0, cx - BG_END) : max(0, cx - BG_START)].mean(axis=0)
        right_bg = val[row_start:row_end, min(width, cx + BG_START) : min(width, cx + BG_END)].mean(axis=0)
        levels[b] = (left_bg + right_bg) / 2.0

    # Smooth along the scratch with Gaussian sigma=12 rows.
    for ch in range(3):
        levels[:, ch] = cv2.GaussianBlur(
            levels[:, ch:ch + 1], (0, 0), sigmaX=LEVEL_SIGMA
        ).ravel()

    # Sample strips and build the deviation table.
    strip_w = 2 * STRIP_HALF_WIDTH + 1
    all_strips: list[np.ndarray] = []
    all_u: list[np.ndarray] = []
    for b in range(n_bands):
        row_start = b * BAND_PX
        row_end = min(row_start + BAND_PX, height)
        cx = centres[b]
        x0 = int(round(cx)) - STRIP_HALF_WIDTH
        x1 = x0 + strip_w
        # Clamp to image bounds.
        pad_left = max(0, -x0)
        pad_right = max(0, x1 - width)
        x0_c = max(0, x0)
        x1_c = min(width, x1)
        strip = val[row_start:row_end, x0_c:x1_c]
        if pad_left > 0 or pad_right > 0:
            strip = np.pad(
                strip,
                ((0, 0), (pad_left, pad_right), (0, 0)),
                mode="constant",
                constant_values=0,
            )
        all_strips.append(strip)
        u = np.arange(strip_w, dtype=np.float32) - STRIP_HALF_WIDTH + (cx - int(round(cx)))
        all_u.append(u)

    strips = np.concatenate(all_strips, axis=0)  # (total_rows, strip_w, 3)
    u_all = np.concatenate(all_u)  # (total_rows*strip_w,)

    # Background line per row.
    bgline = _background_line(strips)  # (total_rows, strip_w, 3)
    # Deviation from background.
    dev = strips - bgline  # (total_rows, strip_w, 3)

    # Quantile bins by level.
    row_levels = np.repeat(levels, BAND_PX, axis=0)[: strips.shape[0]]
    # Per-channel binning.
    table = np.zeros((N_BINS, strip_w, 3), dtype=np.float32)
    bin_centres = np.zeros((N_BINS, 3), dtype=np.float32)
    bin_counts = np.zeros(N_BINS, dtype=np.int32)

    for ch in range(3):
        channel_levels = row_levels[:, ch]
        valid = np.isfinite(channel_levels)
        if valid.sum() < N_BINS:
            continue
        quantiles = np.linspace(0, 100, N_BINS + 1)
        edges = np.percentile(channel_levels[valid], quantiles)
        for bi in range(N_BINS):
            mask = (channel_levels >= edges[bi]) & (channel_levels < edges[bi + 1])
            if bi == N_BINS - 1:
                mask = mask | (channel_levels == edges[bi + 1])
            count = mask.sum()
            bin_counts[bi] = count
            if count >= MIN_ROWS_PER_BIN:
                table[bi, :, ch] = dev[mask].mean(axis=0)
                bin_centres[bi, ch] = channel_levels[mask].mean()
            else:
                table[bi, :, ch] = 0.0
                bin_centres[bi, ch] = edges[bi]

    # Merge bins with fewer than MIN_ROWS_PER_BIN rows.
    merged_table = []
    merged_centres = []
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

    # Zero the table for |u| >= BG_MARGIN, taper linearly from TAPER_START.
    u_axis = np.arange(strip_w, dtype=np.float32) - STRIP_HALF_WIDTH
    mask_outer = np.abs(u_axis) >= BG_MARGIN
    mask_taper = (np.abs(u_axis) >= TAPER_START) & (np.abs(u_axis) < BG_MARGIN)
    taper = np.where(
        mask_taper,
        1.0 - (np.abs(u_axis) - TAPER_START) / (BG_MARGIN - TAPER_START),
        np.where(mask_outer, 0.0, 1.0),
    )
    for bi in range(merged_table_arr.shape[0]):
        merged_table_arr[bi] *= taper[np.newaxis, :]

    # Ensure the table has exactly N_BINS rows by padding or truncating.
    if merged_table_arr.shape[0] < N_BINS:
        pad_count = N_BINS - merged_table_arr.shape[0]
        merged_table_arr = np.concatenate(
            [merged_table_arr, np.zeros((pad_count, strip_w, 3), dtype=np.float32)],
            axis=0,
        )
        merged_centres_arr = np.concatenate(
            [merged_centres_arr, np.zeros((pad_count, 3), dtype=np.float32)],
            axis=0,
        )
    elif merged_table_arr.shape[0] > N_BINS:
        merged_table_arr = merged_table_arr[:N_BINS]
        merged_centres_arr = merged_centres_arr[:N_BINS]

    return ScratchFit(
        axis=candidate.axis,
        band_px=BAND_PX,
        centres=centres.tolist(),
        half_width_px=HALF_WIDTH_PX,
        levels=levels.tolist(),
        table=merged_table_arr,
        score=candidate.score,
        agreement=candidate.agreement,
    )


def fit(image_codes: np.ndarray, candidate: Candidate) -> ScratchFit:
    """Fit the correction table for one detected scratch candidate.

    ``image_codes`` is the full (H, W, 3) uint16 published TIFF.
    """
    val = _decode_val(np.asarray(image_codes))
    return _fit_scratch(val, candidate)


# --- params -----------------------------------------------------------------


def scratches_params(
    canvas: tuple[int, int],
    fits: list[ScratchFit],
    enabled: bool,
) -> dict[str, Any]:
    """The net ``scratches`` op's params for one negative."""
    scratches_list = []
    for f in fits:
        table_bytes = f.table.astype(np.float64).tobytes()
        table_b64 = base64.b64encode(table_bytes).decode("ascii")
        scratches_list.append(
            {
                "axis": f.axis,
                "band_px": f.band_px,
                "centres": [round(c, 2) for c in f.centres],
                "half_width_px": f.half_width_px,
                "levels": [[round(v, 6) for v in row] for row in f.levels],
                "table": table_b64,
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


# --- is_live ----------------------------------------------------------------


def is_live(params: dict | None, shape: tuple[int, int]) -> bool:
    """True when a ``scratches`` op would actually change pixels: enabled
    is true, the list is non-empty, the canvas matches this image's
    ``(height, width)``, and the image is 3-channel."""
    if not params:
        return False
    if not params.get("enabled"):
        return False
    canvas = params.get("canvas")
    if (
        not isinstance(canvas, (list, tuple))
        or len(canvas) != 2
        or (int(canvas[0]), int(canvas[1])) != (shape[1], shape[0])
    ):
        return False
    scratches = params.get("scratches")
    if not isinstance(scratches, list) or len(scratches) == 0:
        return False
    return True


# --- apply ------------------------------------------------------------------


def _interp_level(
    centres: list[float], levels: list[list[float]], y: int, n_bands: int
) -> np.ndarray:
    """Interpolate the background level at row y from the per-band
    smoothed levels.  Returns (3,) float32."""
    band_h = BAND_PX
    b_float = y / band_h
    b0 = min(max(int(b_float), 0), n_bands - 1)
    b1 = min(b0 + 1, n_bands - 1)
    t = b_float - b0
    t = max(0.0, min(1.0, t))
    lev0 = np.asarray(levels[b0], dtype=np.float32)
    lev1 = np.asarray(levels[b1], dtype=np.float32)
    return lev0 * (1.0 - t) + lev1 * t


def _interp_table(
    table: np.ndarray, centres_arr: np.ndarray, u: float, level: np.ndarray
) -> np.ndarray:
    """Interpolate the correction from the table at fractional u and
    interpolated level.  Returns (3,) float32."""
    n_bins = table.shape[0]
    strip_w = table.shape[1]
    half_w = strip_w // 2

    # Interpolate along u.
    u_idx = u + half_w
    u0 = int(math.floor(u_idx))
    u1 = u0 + 1
    t_u = u_idx - u0
    u0 = max(0, min(strip_w - 1, u0))
    u1 = max(0, min(strip_w - 1, u1))

    # Find the two nearest bins by level.
    bin_idx = np.searchsorted(centres_arr, level) - 1
    bin0 = max(0, min(n_bins - 1, bin_idx))
    bin1 = min(n_bins - 1, bin0 + 1)
    if n_bins <= 1:
        bin0 = bin1 = 0

    # Bilinear interpolation.
    c0 = table[bin0, u0] * (1 - t_u) + table[bin0, u1] * t_u
    c1 = table[bin1, u0] * (1 - t_u) + table[bin1, u1] * t_u
    if bin0 == bin1:
        return c0
    # Level interpolation weight.
    lev0 = centres_arr[bin0]
    lev1 = centres_arr[bin1]
    if abs(lev1 - lev0) < 1e-10:
        return c0
    t_l = np.clip((level - lev0) / (lev1 - lev0), 0.0, 1.0)
    return c0 * (1.0 - t_l) + c1 * t_l


def _apply_one_scratch(
    image: np.ndarray, scratch: dict
) -> np.ndarray:
    """Apply one scratch's correction to the image (uint16, H x W x 3).
    Modifies in place and returns the image."""
    centres = scratch["centres"]
    n_bands = len(centres)
    levels = scratch["levels"]
    half_w = scratch.get("half_width_px", HALF_WIDTH_PX)
    band_px = scratch.get("band_px", BAND_PX)

    table_b64 = scratch.get("table", "")
    table_bytes = base64.b64decode(table_b64)
    table = np.frombuffer(table_bytes, dtype=np.float64).reshape(
        N_BINS, 2 * half_w + 1, 3
    ).astype(np.float32)

    centres_arr = np.array(centres, dtype=np.float32)
    height, width = image.shape[:2]

    # Decode the image to val for correction.
    val = normalization.decode_normalized(image).astype(np.float32)

    for y in range(height):
        b_float = y / band_px
        b0 = min(max(int(b_float), 0), n_bands - 1)
        cx = centres_arr[b0]
        level = _interp_level(centres, levels, y, n_bands)

        x_center = int(round(cx))
        for dx in range(-half_w, half_w + 1):
            x = x_center + dx
            if x < 0 or x >= width:
                continue
            u = float(dx)
            # Check fill.
            if np.all(image[y, x] == FILL_CODE):
                continue
            correction = _interp_table(table, centres_arr, u, level)
            val[y, x] -= correction

    # Clip and re-encode.
    val = np.clip(val, -normalization.NORMALIZED_HEADROOM_LOW,
                   1.0 + normalization.NORMALIZED_HEADROOM_HIGH)
    result = normalization.encode_normalized(val)
    return result.astype(np.uint16)


def apply(
    image_codes: np.ndarray,
    params: dict | None,
    *,
    region: tuple[int, int, int, int] | None = None,
) -> np.ndarray:
    """Apply scratch corrections to a published TIFF's uint16 codes.

    ``image_codes`` is (H, W) or (H, W, 3) uint16.  When ``region`` is
    given as ``(x, y, w, h)``, the caller has read the margin and this
    function returns the healed rect.

    Pixels equal to the fill code are skipped.  Overlapping scratch windows
    are applied sequentially in a fixed order (by position in the list).
    """
    image_codes = np.asarray(image_codes)
    if not is_live(params, image_codes.shape[:2]):
        return image_codes

    image = image_codes.copy()
    for scratch in params.get("scratches", []):
        image = _apply_one_scratch(image, scratch)

    if region is not None:
        x, y, w, h = region
        return image[y : y + h, x : x + w]
    return image
