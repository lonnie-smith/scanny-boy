#!/usr/bin/env python3
"""Chunk E-0's measurement tool: replay the film-extent pass's meters
against a real roll, off the published TIFFs.

The normalization is invertible from the recorded `floors`/`ceils`, so a
negative's `grid_log` is reconstructed exactly — no re-stitching — and the
detector is replayed over it. **One lossy step, by design of the encode:**
the encode clips at `-NORMALIZED_HEADROOM_LOW`, so truly opaque cells come
back at the rail (-3.0 below the floor) rather than at -6.0. That affects
`withhold_opaque` replay only — the carrier lobe this plan targets sits
well above the rail — but this tool does not pretend the reconstruction is
exact, and says so here and in its output.

For each negative it writes, into `--out-dir`:

- a **histogram PNG** of the region's log luma at
  `FILM_EXTENT_HISTOGRAM_BIN`, with the detected valley, the contaminant's
  lobe and the film mode marked — the evidence that the valley is where
  the detector put it (§1's protocol);
- an **inset sweep** — the floor against a uniform inset in 10-cell steps
  to 150 cells — the evidence for §3's convergence rule;
- a **mask overlay PNG** — film in grey, the carrier mask in red — so the
  finding is judged by eye and not only asserted; and
- one CSV row recording the valley threshold, lobe fraction, mask
  fraction, the four per-edge insets, the region fraction kept, the
  floors and spans before and after, and the whole sweep.

The protocol this tool serves (§1): run it over at least three rolls
spanning a roll with carrier on one edge only, a roll with carrier on all
four, and a roll with no carrier in frame at all — the no-op case, which
matters most. For each, confirm from the histogram that the valley is
where the detector put it, and from the sweep that the floor reaches a
plateau at least 50 cells wide; record the results as measured constants.

It is a tool, not production code: nothing bundles it and nothing is
tested against it.

Run from the repository root:

    uv run --project cli python cli/tools/measure_film_extent.py \
        --roll /path/to/roll --negative ID [--negative ID ...] \
        --out-dir measure-film-extent-out
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import cv2
import numpy as np
import tifffile

from scanny_boy import normalization as nz
from scanny_boy.roll_manifest import load_roll_manifest

# The sweep's uniform insets, in grid cells (§1: 10-cell steps to 150).
SWEEP_INSETS = list(range(0, 151, 10))


def rebuild_grid_log(
    codes: np.ndarray, floors: list[float], ceils: list[float]
) -> np.ndarray:
    """The published TIFF's codes back to log density, per channel: the
    arithmetic inverse of `normalize_log_image`. A mono TIFF's 2-D codes
    get the trailing channel axis back."""
    decoded = nz.decode_normalized(codes)
    if decoded.ndim == 2:
        decoded = decoded[..., np.newaxis]
    channels = decoded.shape[-1]
    if len(floors) != channels or len(ceils) != channels:
        raise SystemExit(
            f"published image has {channels} channels but the record's "
            f"bounds have {len(floors)}/{len(ceils)}"
        )
    floors_a = np.asarray(floors, dtype=np.float32)
    ceils_a = np.asarray(ceils, dtype=np.float32)
    return decoded * (ceils_a - floors_a) + floors_a


def rebuild_keep(
    grid_shape: tuple[int, int],
    analysis_rect: list[int],
    block_px: int,
) -> np.ndarray:
    """`keep` from the recorded `analysis_rect`, divided by the block size,
    rounding inward exactly as `composite._region_keep` does — no
    uncovered-canvas cell may leak into the meters."""
    grid_rows, grid_cols = grid_shape
    x, y, width, height = (float(v) for v in analysis_rect)
    gx0 = int(np.ceil(x / block_px))
    gy0 = int(np.ceil(y / block_px))
    gx1 = int(np.floor((x + width) / block_px))
    gy1 = int(np.floor((y + height) / block_px))
    keep = np.zeros((grid_rows, grid_cols), dtype=bool)
    gy1 = min(gy1, grid_rows)
    gx1 = min(gx1, grid_cols)
    if gx1 <= gx0 or gy1 <= gy0:
        raise SystemExit(
            f"analysis rect {analysis_rect} rounds away entirely at block "
            f"{block_px}"
        )
    keep[gy0:gy1, gx0:gx1] = True
    return keep


def carrier_mask(
    lum: np.ndarray, keep: np.ndarray, valley: float
) -> np.ndarray:
    """The hysteresis mask the pass acts on, rebuilt from the pass's own
    building blocks so the overlay shows what the detector found rather
    than only the rect it applied."""
    seed = keep & (lum <= valley - nz.FILM_EXTENT_SEED_OFFSET)
    loose = keep & (lum <= valley)
    count, labels = cv2.connectedComponents(loose.astype(np.uint8), connectivity=8)
    border = nz._region_border(keep)
    mask = np.zeros(keep.shape, dtype=bool)
    for label in range(1, count):
        component = labels == label
        if (component & seed).any() and (component & border).any():
            mask |= component
    return mask


def histogram_png(
    values: np.ndarray,
    valley: float | None,
    path: Path,
    label: str,
) -> None:
    """The region's log-luma histogram at `FILM_EXTENT_HISTOGRAM_BIN`, on a
    log-count axis, with the film mode, the contaminant's lobe and the
    valley marked."""
    edges = np.arange(
        values.min(),
        values.max() + nz.FILM_EXTENT_HISTOGRAM_BIN,
        nz.FILM_EXTENT_HISTOGRAM_BIN,
    )
    counts, edges = np.histogram(values, bins=edges)
    mode = int(np.argmax(counts))
    lobe = None
    if valley is not None:
        # The contaminant's own mode, located the way `_find_valley` does:
        # the tallest bin denser than the first collapse below the film mode.
        drop = nz.FILM_EXTENT_VALLEY_DROP * counts[mode]
        first = next(
            (i for i in range(mode - 1, 0, -1) if counts[i] <= drop), None
        )
        if first is not None:
            lobe = int(np.argmax(counts[:first]))

    width, height = 1600, 900
    margin_l, margin_b, margin_t = 70, 60, 70
    plot_w, plot_h = width - margin_l - 20, height - margin_b - margin_t
    canvas = np.full((height, width, 3), 255, np.uint8)

    peak = max(int(counts.max()), 1)
    bar_w = plot_w / max(counts.size, 1)
    for i, count in enumerate(counts):
        if count <= 0:
            continue
        h = int(np.log10(max(count, 1)) / np.log10(peak) * (plot_h - 10)) + 1
        x0 = int(margin_l + i * bar_w) + 1
        x1 = int(margin_l + (i + 1) * bar_w) - 1
        colour = (120, 120, 120)
        if i == mode:
            colour = (40, 120, 40)  # the film mode, green
        elif lobe is not None and i == lobe:
            colour = (200, 120, 0)  # the contaminant's lobe, amber
        canvas[height - margin_b - h : height - margin_b, x0:x1] = colour
    if valley is not None:
        x = int(
            margin_l
            + (valley - edges[0]) / nz.FILM_EXTENT_HISTOGRAM_BIN * bar_w
        )
        cv2.line(canvas, (x, margin_t - 20), (x, height - margin_b), (0, 0, 220), 2)
        cv2.putText(
            canvas, "valley", (x - 30, margin_t - 28),
            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 220), 2,
        )
    cv2.putText(
        canvas, label, (margin_l, margin_t - 40),
        cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 0), 2,
    )
    cv2.putText(
        canvas, f"log10 luma  (mode {edges[mode]:.2f}, bin "
        f"{nz.FILM_EXTENT_HISTOGRAM_BIN}, log counts)",
        (margin_l, height - 20),
        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 1,
    )
    cv2.imwrite(str(path), canvas)


def mask_overlay_png(
    lum: np.ndarray, keep: np.ndarray, mask: np.ndarray, path: Path, label: str
) -> None:
    """Film in grey, the carrier mask in red, at grid-cell resolution."""
    region = lum[keep]
    lo, hi = float(region.min()), float(region.max())
    grey = np.full(lum.shape, 200, np.float32)
    scaled = (lum - lo) / max(hi - lo, 1e-6) * 180.0 + 40.0
    grey[keep] = scaled[keep]
    image = np.clip(grey, 0, 255).astype(np.uint8)
    image = np.stack([image] * 3, axis=-1)
    image[mask] = (0, 0, 255)
    image[~keep] = (60, 60, 60)
    cv2.putText(
        image, label, (8, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2
    )
    cv2.imwrite(str(path), image)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--roll", required=True, metavar="DIR")
    parser.add_argument("--negative", action="append", required=True, metavar="ID")
    parser.add_argument("--out-dir", required=True, metavar="DIR")
    args = parser.parse_args()

    roll_dir = Path(args.roll)
    manifest = load_roll_manifest(roll_dir)
    params = manifest.processing_params.get("normalize", {})
    block_px = int(params.get("analysis_block_px", nz.ANALYSIS_BLOCK_PX))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(
        "NOTE: the reconstruction is invertible except at the encode's low "
        f"rail: opaque cells come back at {nz.decode_normalized(np.array([0], dtype=np.uint16))[0]:.3f} "
        "normalized (the -NORMALIZED_HEADROOM_LOW clip), not at -6.0. This "
        "affects withhold_opaque replay only; the carrier lobe sits well "
        "above the rail."
    )

    rows: list[dict[str, object]] = []
    for negative_id in args.negative:
        negative = manifest.negative(negative_id)
        if negative.output is None or negative.normalization is None:
            raise SystemExit(f"{negative_id} has no published normalization record")
        record = negative.normalization
        codes = tifffile.imread(roll_dir / negative.output["name"])
        grid_log = rebuild_grid_log(
            codes, record["floors"], record["ceils"]
        )
        grid = nz.block_median_grid(grid_log)
        lum = nz.luma_of_log(grid)
        keep = rebuild_keep(
            grid.shape[:2], record["analysis_rect"], block_px
        )
        label = f"{negative_id}  valley=?"

        before = nz.analyze_bounds(grid, keep)
        row: dict[str, object] = {
            "negative_id": negative_id,
            "block_px": block_px,
            "region_cells": int(keep.sum()),
            "floor_before": min(before.floors),
            "span_before": max(b - f for f, b in zip(before.floors, before.ceils)),
        }

        try:
            new_keep, extent = nz.withhold_non_film(grid, keep)
        except nz.NormalizationError as exc:
            print(f"{negative_id}: the pass failed loud: {exc}")
            row["error"] = str(exc)
            rows.append(row)
            continue

        row.update(
            {
                "detected": extent.detected,
                "valley": extent.valley,
                "lobe_fraction": extent.lobe_fraction,
                "mask_fraction": extent.mask_fraction,
                "inset_top": extent.insets[0],
                "inset_bottom": extent.insets[1],
                "inset_left": extent.insets[2],
                "inset_right": extent.insets[3],
                "region_fraction": extent.region_fraction,
                "convergence_steps": extent.convergence_steps,
            }
        )

        if extent.detected:
            mask = carrier_mask(lum, keep, float(extent.valley))
            after = nz.analyze_bounds(grid, new_keep)
            row["floor_after"] = min(after.floors)
            row["span_after"] = max(
                b - f for f, b in zip(after.floors, after.ceils)
            )
            label = f"{negative_id}  valley={extent.valley:.3f}"
            histogram_png(lum[keep], float(extent.valley), out_dir / f"{negative_id}-histogram.png", label)
            mask_overlay_png(lum, keep, mask, out_dir / f"{negative_id}-mask.png", label)
        else:
            histogram_png(lum[keep], None, out_dir / f"{negative_id}-histogram.png", f"{negative_id}  no valley")
            mask_overlay_png(lum, keep, np.zeros_like(keep), out_dir / f"{negative_id}-mask.png", f"{negative_id}  no-op")

        # The inset sweep (§1 step 4): the floor against a uniform inset.
        for inset in SWEEP_INSETS:
            rect = nz._inset_rect(keep, (inset, inset, inset, inset))
            floor = (
                float(np.percentile(lum[rect], nz.BASE_LUMA_CLIP))
                if rect.any()
                else None
            )
            row[f"sweep_{inset:03d}"] = floor

        rows.append(row)
        print(
            f"{negative_id}: detected={extent.detected} "
            f"valley={extent.valley} insets={extent.insets} "
            f"region_fraction={extent.region_fraction:.3f} "
            f"steps={extent.convergence_steps}"
        )

    csv_path = out_dir / "measurements.csv"
    fieldnames = sorted({key for row_ in rows for key in row_})
    with open(csv_path, "w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} rows to {csv_path}")
    print(
        "Next: confirm from the histograms that the valley is where the "
        "detector put it, and from the sweep that the floor plateaus at "
        "least 50 cells wide (BLACK_POINT_REFINEMENT §1)."
    )


if __name__ == "__main__":
    main()
