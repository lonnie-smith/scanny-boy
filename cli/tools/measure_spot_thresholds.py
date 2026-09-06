#!/usr/bin/env python3
"""Chunk S-6's calibration tool (docs/SPOTTING_PLAN.md §9): measure the
spot detector's constants against a real roll.

Given a roll folder and a list of negative ids, for each negative and for
`sensitivity` in 0.0 … 1.0 step 0.1, this:

- runs `spots.detect` on the negative's published TIFF (its own
  `valid_rect`, its own metering ranges), and records `found`, the kept
  count, `sigma`, and the score/`minor`/`area` distributions of the
  survivors;
- writes a **contact sheet**: one PNG per (negative, sensitivity), tiling
  1:1 crops of the 36 highest-scoring spots with a 64-px margin, in the
  positive display encode — so precision can be judged by eye rather than
  asserted; and
- writes a CSV of every measurement to `--out-dir/measurements.csv`.

The protocol this tool serves (§9.2): pick six published negatives
spanning the roll's density range (at least one with sky, one with fine
foliage or fabric, one known dirty); read the contact sheets from the most
aggressive sensitivity downward; choose the highest sensitivity at which at
least 90% of the sampled crops are genuine crud; set
`THRESHOLD_K_MIN`/`THRESHOLD_K_MAX` so that sensitivity lands at
`DEFAULT_SENSITIVITY`, and record the measured `k` and the grain `sigma`
range; repeat on a monochrome roll for `MONO_K_BONUS`; and measure
`MAX_SPOT_MINOR_FRACTION` from the widest genuine defect found. The chosen
values, the negatives they were measured on, and the observed precision go
into a "Measured constants" section appended to SPOTTING_PLAN.md (§9.3).

Run from the repository root:

    uv run --project cli python cli/tools/measure_spot_thresholds.py \
        --roll /path/to/roll --negative ID [--negative ID ...] \
        --out-dir measure-spots-out
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import cv2
import numpy as np
import tifffile

from scanny_boy import color, previews, spots
from scanny_boy.roll_manifest import load_roll_manifest

# Crops per contact sheet (a 6x6 tile) and the margin each crop grows by,
# so a spot is judged with its surroundings in frame.
SHEET_COLUMNS = 6
SHEET_ROWS = 6
CROP_MARGIN_PX = 64

# Contact-sheet crops are stored 8-bit for viewing; the underlying tile is
# 1:1 pixels, then labelled downscale is not applied — a sheet is large.
SENSITIVITIES = [round(0.1 * step, 1) for step in range(11)]


def crop_tile(image: np.ndarray, bbox: list[int], margin: int) -> np.ndarray:
    """A 1:1 crop around a spot's bounding box, clamped to the image."""
    height, width = image.shape[:2]
    x, y, w, h = bbox
    x0 = max(x - margin, 0)
    y0 = max(y - margin, 0)
    x1 = min(x + w + margin, width)
    y1 = min(y + h + margin, height)
    return image[y0:y1, x0:x1]


def display_tile(tile: np.ndarray) -> np.ndarray:
    """The positive display encode of a 1:1 tile, as an 8-bit BGR array
    for `cv2` — the same encode the cached preview uses (decode → invert →
    8-bit, no gamma), so the sheets read the way the Edit tab does."""
    if tile.ndim == 2:
        tile = np.stack([tile] * 3, axis=-1)
    display = previews.NORMALIZED_DISPLAY_LUT[tile[..., :3]]
    ok, encoded = cv2.imencode(".png", cv2.cvtColor(display, cv2.COLOR_RGB2BGR))
    if not ok:
        raise SystemExit("could not encode a contact-sheet tile")
    decoded = cv2.imdecode(encoded, cv2.IMREAD_UNCHANGED)
    return decoded


def contact_sheet(tiles: list[np.ndarray], label: str) -> np.ndarray | None:
    """Tile the crops into one labelled sheet; fewer spots means fewer
    tiles, and none means no sheet."""
    if not tiles:
        return None
    height = max(tile.shape[0] for tile in tiles)
    width = max(tile.shape[1] for tile in tiles)
    canvas = np.zeros(
        (SHEET_ROWS * (height + 4), SHEET_COLUMNS * (width + 4), 3), dtype=np.uint8
    )
    for index, tile in enumerate(tiles[: SHEET_ROWS * SHEET_COLUMNS]):
        row, col = divmod(index, SHEET_COLUMNS)
        y = row * (height + 4) + 2
        x = col * (width + 4) + 2
        canvas[y : y + tile.shape[0], x : x + tile.shape[1]] = tile
    cv2.putText(
        canvas,
        label,
        (8, 28),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.9,
        (255, 255, 255),
        2,
    )
    return canvas


def percentile_rows(values: list[float]) -> dict[str, float | None]:
    """The distribution summary one CSV row carries per measured quantity:
    the p50/p90/max of the survivors' score, width, and area."""
    keys = ("p50", "p90", "max")
    if not values:
        return {key: None for key in keys}
    quantiles = np.percentile(np.asarray(values, dtype=np.float64), [50, 90, 100])
    return {key: float(quantiles[index]) for index, key in enumerate(keys)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--roll", required=True, metavar="DIR")
    parser.add_argument("--negative", action="append", required=True, metavar="ID")
    parser.add_argument("--out-dir", required=True, metavar="DIR")
    args = parser.parse_args()

    roll_dir = Path(args.roll)
    manifest = load_roll_manifest(roll_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, object]] = []
    for negative_id in args.negative:
        negative = manifest.negative(negative_id)
        if negative.output is None:
            raise SystemExit(f"{negative_id} has no published output")
        image = tifffile.imread(roll_dir / negative.output["name"])
        meter = color.read_metering(negative.normalization)
        print(
            f"{negative_id}: {image.shape[1]}x{image.shape[0]}x"
            f"{image.shape[2] if image.ndim == 3 else 1}, "
            f"channel ranges {tuple(round(r, 4) for r in meter.ranges)}"
        )
        for sensitivity in SENSITIVITIES:
            result = spots.detect(
                image,
                valid_rect=negative.valid_rect,
                channel_ranges=meter.ranges,
                sensitivity=sensitivity,
            )
            ordered = sorted(result.spots, key=lambda s: -s["score"])
            tiles = [
                display_tile(crop_tile(image, spot["bbox"], CROP_MARGIN_PX))
                for spot in ordered[: SHEET_ROWS * SHEET_COLUMNS]
            ]
            label = f"{negative_id} s={sensitivity} found={result.found}"
            sheet = contact_sheet(tiles, label)
            if sheet is not None:
                sheet_path = out_dir / f"{negative_id}-s{sensitivity:.1f}.png"
                cv2.imwrite(str(sheet_path), sheet)

            scores = [spot["score"] for spot in ordered]
            minors = [
                spot["area"] / max(spot["bbox"][2], spot["bbox"][3]) for spot in ordered
            ]
            areas = [float(spot["area"]) for spot in ordered]
            row: dict[str, object] = {
                "negative_id": negative_id,
                "sensitivity": sensitivity,
                "found": result.found,
                "kept": len(result.spots),
                "sigma": round(result.sigma, 4),
            }
            for key, values in (
                ("score", scores),
                ("minor", minors),
                ("area", areas),
            ):
                summary = percentile_rows(values)
                for name, value in summary.items():
                    row[f"{key}_{name}"] = None if value is None else round(value, 2)
            rows.append(row)

    csv_path = out_dir / "measurements.csv"
    fieldnames = sorted({key for row in rows for key in row})
    with open(csv_path, "w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} rows to {csv_path}")
    print(
        "Next: read the contact sheets from the most aggressive sensitivity "
        "downward (SPOTTING_PLAN §9.2)."
    )


if __name__ == "__main__":
    main()
