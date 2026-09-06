"""The film-base reference: one measured per-roll anchor for the thin end
(docs/REBATE_ANCHORING.md).

A roll's `film_base` block records the per-channel median log density
inside the film rebate of one dedicated reference frame — the **base
frame** — shot once per roll, showing as much clear rebate as the film
offers. Every negative on the roll then takes its thin-end *colour*
deviation from that locked measurement instead of from its own scene
percentiles, which is what fixes the high-key failure of
`normalization.analyze_bounds`' colour axis: a frame with no shadows
estimates the orange mask from scene content, and the scene's colour is
not the mask.

The measurement is **exposure-invariant** (§0.2): a shutter, aperture or
ISO change scales linear light by one factor, which in log density is a
pure common-mode shift — only deviations from the median are consumed,
so the base frame's exposure never has to match the roll's. §11's
measurement gate exists to verify that claim on real film.

The module owns the decode of one reference frame, the detector, the
gates, and the params record. It knows nothing about manifests or rolls.
Every constant of the feature is defined here and nowhere else.

All of §2.1's thresholds are **provisional and unmeasured**, in the same
status as `normalization.REBATE_*`: §11 pins them, and chunk B-5 (the
only consumer) must not land before the user has approved the numbers.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import cv2
import numpy as np

from scanny_boy import flatfield, normalization, raw_decode
from scanny_boy import linear as linear_module
from scanny_boy.events import Code

# The measurement runs on normalization's block-median grid, at
# normalization.ANALYSIS_GRID. This module does not define its own.

# --- finding the populations ---

# Log10 D. Width of the candidate band taken below each pass's thin anchor.
# Wider than normalization.REBATE_DENSITY_TOLERANCE because a base frame's
# rebate is a large region that may carry a gentle residual gradient.
# Provisional, unmeasured (§2.1).
FILM_BASE_BAND_WIDTH = 0.15
# Thin-end anchor percentile within each pass's remaining cells.
# Provisional, unmeasured (§2.1).
FILM_BASE_ANCHOR_PERCENTILE = 99.5
# Log10 D, P90 - P10 within one component: base is featureless.
# Provisional, unmeasured (§2.1).
FILM_BASE_MAX_COMPONENT_SPREAD = 0.05
# Log10 D. Two populations closer than this are the same population — this
# is what merges two rebate bands on opposite sides of the frame into one
# measurement instead of throwing half the data away (§2.2 step 5).
# Provisional, unmeasured (§2.1).
FILM_BASE_MERGE_SEPARATION = 0.06
# How many peel passes enumerate populations from the thin end down.
# Provisional, unmeasured (§2.1).
FILM_BASE_MAX_PASSES = 4

# --- gating the result ---

# Of the whole grid. "A lot of rebate": the chosen population must be at
# least this much of the frame. Provisional, unmeasured (§2.1).
FILM_BASE_MIN_AREA_FRACTION = 0.20
# Ambiguity gate. If some OTHER separated flat population is at least this
# fraction of the chosen one's area, the frame is refused rather than
# guessed at (§2.3 gate 6). Provisional, unmeasured (§2.1).
FILM_BASE_AMBIGUOUS_RATIO = 0.60
# Per-channel fraction of the chosen population's cells at or above
# normalization.SCAN_CLIP_LEVEL past which the frame is refused. Clipped
# base is worthless base — the same line detect_rebate already takes.
# Provisional, unmeasured (§2.1).
FILM_BASE_MAX_CLIPPED = 0.001
# Log10 D. Per-channel median floor inside the chosen population. Blue
# through an orange mask is the channel that runs out first (§1.2), so this
# is deliberately per-channel and not a luma test. Provisional, unmeasured
# (§2.1).
FILM_BASE_MIN_CHANNEL = -2.0
# Grid cells in the chosen population. Fewer is too few samples for a
# stable per-channel median. Provisional, unmeasured (§2.1).
FILM_BASE_MIN_CELLS = 1024

# Bumped whenever the measurement's arithmetic changes in a way that makes
# an old recorded value non-comparable with a fresh one.
FILM_BASE_MEASURE_VERSION = 1

_CHANNEL_NAMES = ("red", "green", "blue")


@dataclasses.dataclass(frozen=True)
class Population:
    """One flat, thin population found in the base frame. `area_fraction` is
    of the whole grid; `density` is the per-channel median log10 density
    inside it."""

    density: tuple[float, float, float]
    luma: float
    area_fraction: float
    cells: int
    spread: float


@dataclasses.dataclass(frozen=True)
class BaseMeasurement:
    """One base frame's finding. `density` is the per-channel median log10
    density of the chosen population — the same quantity, measured the same
    way, as normalization.Rebate.base_density, so the two are directly
    comparable (§6). Always three channels: the base frame is decoded as RGB
    whatever the roll's film kind turns out to be (§5).

    `populations` is every population the detector found, thinnest first,
    recorded whether or not the frame passed. §11 reads these off real
    frames to pin the thresholds, and a rejected frame's list is what tells
    the user what went wrong."""

    density: tuple[float, float, float]
    chosen_index: int
    populations: tuple[Population, ...]
    clipped_fractions: tuple[float, float, float]
    grid_cells: int
    measure_version: int = FILM_BASE_MEASURE_VERSION


class FilmBaseError(Exception):
    """A base frame that cannot be used. Carries a stable CONTRACT.md code,
    exactly like flatfield.FlatFieldError. A bad NEF is NOT one of these —
    decode_raw's UnsupportedRawError / UnreadableRawError propagate
    unchanged, because a bad NEF already has stable codes."""

    def __init__(self, code: Code, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _population_from_mask(
    mask: np.ndarray, grid: np.ndarray, lum: np.ndarray, total_cells: int
) -> Population:
    """One `Population` over the union of a group's cells: per-channel
    medians (never means — a dust shadow that survived the block median
    must not move the answer), the median luma, and the P90-P10 luma
    spread."""
    channels = grid.shape[-1]
    density = tuple(
        float(np.median(grid[..., channel][mask])) for channel in range(channels)
    )
    luma = float(np.median(lum[mask]))
    cells = int(np.count_nonzero(mask))
    return Population(
        density=density,  # type: ignore[arg-type]
        luma=luma,
        area_fraction=cells / total_cells,
        cells=cells,
        spread=_percentile(lum[mask], 90.0) - _percentile(lum[mask], 10.0),
    )


def _percentile(values: np.ndarray, q: float) -> float:
    return float(np.percentile(values, q))


def measure(linear: np.ndarray) -> BaseMeasurement:
    """Find the film-base population in one decoded, flat-fielded base
    frame.

    `linear` is float32 linear light in [0, 1] — what
    linear.decode_to_linear returns — with the run's flat-field gain
    already applied. Flat-fielding is not optional: the gain map is what
    removes the light panel's falloff, and without it the falloff alone can
    split one rebate region into several components or fail the flatness
    test.

    Measures and chooses; does not gate. `gate()` gates, so a caller can
    record a failing frame's populations before raising (§11).
    """
    grid = normalization.block_median_grid(normalization.to_log_density(linear))
    lum = normalization.luma_of_log(grid)
    total_cells = int(lum.size)

    # The multi-pass peel (§2.2 steps 2-3), mirroring
    # normalization.withhold_dense_border's structure: enumerate the thin
    # flat populations from the thin end down. Deliberately NO
    # border-connectivity gate and NO separation-from-outside gate — both
    # exist in detect_rebate because there the rebate is a minority
    # intruding on a picture; here it is the subject, and including them is
    # what makes detect_rebate return nothing on an all-rebate frame (§0.3).
    remaining = np.ones(lum.shape, dtype=bool)
    found: list[np.ndarray] = []
    for _pass in range(FILM_BASE_MAX_PASSES):
        if not remaining.any():
            break
        anchor = _percentile(lum[remaining], FILM_BASE_ANCHOR_PERCENTILE)
        candidates = remaining & (lum >= anchor - FILM_BASE_BAND_WIDTH)
        if not candidates.any():
            break
        count, labels, _stats, _centroids = cv2.connectedComponentsWithStats(
            candidates.astype(np.uint8), connectivity=8
        )
        kept_any = False
        for label in range(1, count):
            component = labels == label
            # Flatness: base is featureless, so one component's own P90-P10
            # must be inside the spread gate.
            spread = _percentile(lum[component], 90.0) - _percentile(
                lum[component], 10.0
            )
            if spread <= FILM_BASE_MAX_COMPONENT_SPREAD:
                found.append(component)
                kept_any = True
        remaining &= ~candidates
        if not kept_any:
            break

    if not found:
        return BaseMeasurement(
            density=(0.0, 0.0, 0.0),
            chosen_index=-1,
            populations=(),
            clipped_fractions=(0.0, 0.0, 0.0),
            grid_cells=total_cells,
        )

    # Merge by density, not by space (§2.2 step 5): two components whose
    # luma medians differ by less than FILM_BASE_MERGE_SEPARATION are one
    # population — two rebate bands on opposite sides of a frame become one
    # measurement of ~40% of the frame rather than two of ~20% each. The
    # components arrive thinnest first, so a greedy nearest-median chain
    # over the running unions is the grouping.
    ordered = sorted(
        found,
        key=lambda mask: float(np.median(lum[mask])),
        reverse=True,
    )
    groups: list[tuple[np.ndarray, float]] = []
    for component in ordered:
        median = float(np.median(lum[component]))
        if groups:
            best_index, best_gap = -1, FILM_BASE_MERGE_SEPARATION
            for index, (_mask, group_median) in enumerate(groups):
                gap = abs(group_median - median)
                if gap < best_gap:
                    best_index, best_gap = index, gap
            if best_index >= 0:
                mask, _ = groups[best_index]
                mask |= component
                groups[best_index] = (mask, float(np.median(lum[mask])))
                continue
        groups.append((component, median))

    by_luma = sorted(
        (
            (_population_from_mask(mask, grid, lum, total_cells), mask)
            for mask, _median in groups
        ),
        key=lambda pair: pair[0].luma,
        reverse=True,
    )
    populations = tuple(population for population, _mask in by_luma)
    masks = [mask for _population, mask in by_luma]

    # The choice rule (§2.2 step 7): the largest area wins — rebate is the
    # largest flat thing in the frame, and the capture instruction is
    # written to satisfy exactly that. Bare light and sprocket holes are
    # thinner but small; image content is not flat.
    chosen_index = max(
        range(len(populations)),
        key=lambda index: populations[index].area_fraction,
    )
    chosen = populations[chosen_index]

    # Clipping is measured inside the chosen population only (§2.2 step 8),
    # on the grid's linear estimate, against normalization.SCAN_CLIP_LEVEL —
    # the number that matters, so load() ignores apply_in_place's own count.
    grid_linear = np.power(10.0, grid.astype(np.float64))
    channels = grid.shape[-1]
    chosen_mask = masks[chosen_index]
    clipped_fractions = tuple(
        float(
            np.mean(
                grid_linear[..., channel][chosen_mask] >= normalization.SCAN_CLIP_LEVEL
            )
        )
        for channel in range(channels)
    )
    return BaseMeasurement(
        density=chosen.density,
        chosen_index=chosen_index,
        populations=populations,
        clipped_fractions=clipped_fractions,  # type: ignore[arg-type]
        grid_cells=total_cells,
    )


def gate(measurement: BaseMeasurement) -> None:
    """Raise FilmBaseError unless `measurement` came from a usable base
    frame. Checked in the order below (§2.3); the first failure is the one
    the user sees, so the order is chosen for diagnostic value."""
    if not measurement.populations:
        raise FilmBaseError(
            Code.FILM_BASE_NOT_FOUND,
            "no flat film-base region was found in the base frame; it must "
            "show a large area of clear rebate",
        )
    chosen = measurement.populations[measurement.chosen_index]
    if chosen.area_fraction < FILM_BASE_MIN_AREA_FRACTION:
        raise FilmBaseError(
            Code.FILM_BASE_TOO_SMALL,
            f"the largest flat rebate region covers only "
            f"{chosen.area_fraction * 100:.0f}% of the base frame (at least "
            f"{FILM_BASE_MIN_AREA_FRACTION * 100:.0f}% is needed); reframe "
            "to include more clear film base",
        )
    if chosen.cells < FILM_BASE_MIN_CELLS:
        raise FilmBaseError(
            Code.FILM_BASE_TOO_SMALL,
            f"the largest flat rebate region covers only {chosen.cells} "
            f"analysis cells (at least {FILM_BASE_MIN_CELLS} is needed); "
            "reframe to include more clear film base",
        )
    worst_clipped = max(
        range(len(measurement.clipped_fractions)),
        key=lambda ch: measurement.clipped_fractions[ch],
    )
    if measurement.clipped_fractions[worst_clipped] > FILM_BASE_MAX_CLIPPED:
        raise FilmBaseError(
            Code.FILM_BASE_CLIPPED,
            f"the base frame's rebate is sensor-clipped in the "
            f"{_CHANNEL_NAMES[worst_clipped]} channel; re-shoot it about two "
            "stops darker — the exposure does not need to match the roll",
        )
    thinnest_channel = min(
        range(len(chosen.density)), key=lambda ch: chosen.density[ch]
    )
    if chosen.density[thinnest_channel] < FILM_BASE_MIN_CHANNEL:
        raise FilmBaseError(
            Code.FILM_BASE_TOO_DARK,
            f"the base frame's {_CHANNEL_NAMES[thinnest_channel]} channel "
            f"through the rebate is too dark to measure "
            f"({chosen.density[thinnest_channel]:.1f}); re-shoot it brighter "
            "— about two stops below your scanning exposure, not more than "
            "three",
        )
    for population in measurement.populations:
        if population is chosen:
            continue
        if population.area_fraction >= FILM_BASE_AMBIGUOUS_RATIO * chosen.area_fraction:
            raise FilmBaseError(
                Code.FILM_BASE_AMBIGUOUS,
                f"the base frame contains two large flat regions of "
                f"different density ({chosen.area_fraction * 100:.0f}% and "
                f"{population.area_fraction * 100:.0f}% of the frame); one "
                "of them may be bare light or a second strip — reframe so "
                "the film rebate clearly dominates",
            )


def load(reference: Path, gain_map: np.ndarray | None) -> BaseMeasurement:
    """Decode `reference` with the locked RAW_PARAMS, apply `gain_map` if
    given, and measure. Does NOT gate — the caller gates, so it can record a
    failing frame's populations before raising (§11).

    Geometric correction (distortion, CA) is deliberately not applied: the
    measurement is a per-channel median over a flat region, which no
    geometric warp moves. Flat-field IS applied, because falloff is what
    the flatness test would otherwise trip on."""
    decoded = raw_decode.decode_raw(reference)
    if gain_map is not None:
        full_res_gain = flatfield.resize_gain_map(
            gain_map, decoded.width, decoded.height
        )
        # The returned clipped count is ignored: gate 4 measures clipping on
        # the measurement's own result, which is the number that matters.
        flatfield.apply_in_place(decoded.pixels, full_res_gain)
    linear = linear_module.decode_to_linear(decoded.pixels)
    return measure(linear)


def build_params() -> dict:
    """The feature's constants, recorded under stitch_params["film_base"]
    (§2.5). These shape published pixels once B-5 lands — a frame that
    fails a gate produces no roll at all, and the chosen population decides
    the anchor — so they are roll invariants."""
    return {
        "band_width": FILM_BASE_BAND_WIDTH,
        "anchor_percentile": FILM_BASE_ANCHOR_PERCENTILE,
        "max_component_spread": FILM_BASE_MAX_COMPONENT_SPREAD,
        "merge_separation": FILM_BASE_MERGE_SEPARATION,
        "max_passes": FILM_BASE_MAX_PASSES,
        "min_area_fraction": FILM_BASE_MIN_AREA_FRACTION,
        "ambiguous_ratio": FILM_BASE_AMBIGUOUS_RATIO,
        "max_clipped": FILM_BASE_MAX_CLIPPED,
        "min_channel": FILM_BASE_MIN_CHANNEL,
        "min_cells": FILM_BASE_MIN_CELLS,
        "measure_version": FILM_BASE_MEASURE_VERSION,
    }
