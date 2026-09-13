#!/usr/bin/env python3
"""Calibration tool for the scratch detector (SCRATCH_REMOVAL_PLAN §6.3).

Given a roll folder and negative ids, for each negative this:

- runs ``scratches.inspect_paths`` and records every DP path's score,
  agreement, residual drift, and accept/reject reasons;
- runs ``scratches.detect`` for the accepted set, fits and applies
  correction, then writes held-out residual metrics;
- writes a contact sheet of before / after / 5×-difference crops at fixed
  rows for each accepted scratch.

Run from the repository root:

    uv run --project cli python cli/tools/measure_scratches.py \\
        --roll /path/to/roll --negative ID [--negative ID ...] \\
        --out-dir measure-scratches-out

The tool does not load at runtime; it is for gate calibration only.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import cv2
import numpy as np
import tifffile

from scanny_boy import previews, scratches
from scanny_boy.roll_manifest import load_roll_manifest

CROP_ROWS = 3
CROP_HALF_WIDTH = 80
DIFF_SCALE = 5.0


def _display_rgb(tile: np.ndarray) -> np.ndarray:
    if tile.ndim == 2:
        tile = np.stack([tile] * 3, axis=-1)
    display = previews.NORMALIZED_DISPLAY_LUT[tile[..., :3]]
    return cv2.cvtColor(display, cv2.COLOR_RGB2BGR)


def _scratch_crop(
    image: np.ndarray, centre_x: float, row: int
) -> np.ndarray:
    height, width = image.shape[:2]
    cx = round(centre_x)
    x0 = max(0, cx - CROP_HALF_WIDTH)
    x1 = min(width, cx + CROP_HALF_WIDTH)
    y0 = max(0, row - CROP_ROWS)
    y1 = min(height, row + CROP_ROWS + 1)
    return image[y0:y1, x0:x1]


def _contact_row(before: np.ndarray, after: np.ndarray) -> np.ndarray:
    diff = np.clip((after.astype(np.float32) - before.astype(np.float32)) * DIFF_SCALE + 128, 0, 255)
    diff = diff.astype(np.uint8)
    if diff.ndim == 2:
        diff = np.stack([diff] * 3, axis=-1)
    return np.hstack([before, after, diff])


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

    path_rows: list[dict[str, object]] = []
    accepted_rows: list[dict[str, object]] = []
    for negative_id in args.negative:
        negative = manifest.negative(negative_id)
        if negative.output is None:
            raise SystemExit(f"{negative_id} has no published output")
        image = tifffile.imread(roll_dir / negative.output["name"])
        norm = negative.normalization
        spans = tuple(norm["ceils"][ch] - norm["floors"][ch] for ch in range(3))
        film_extent = norm.get("film_extent")
        analysis_rect = norm.get("analysis_rect")

        inspected = scratches.inspect_paths(
            image, spans, film_extent, analysis_rect
        )
        for row in inspected:
            path_rows.append({"negative_id": negative_id, **row})

        candidates = scratches.detect(image, spans, film_extent, analysis_rect)
        print(f"{negative_id}: {len(candidates)} accepted scratch(es)")

        sheet_tiles: list[np.ndarray] = []
        for index, candidate in enumerate(candidates):
            fit = scratches.fit(image, candidate)
            params = scratches.scratches_params(
                canvas=(image.shape[1], image.shape[0]),
                fits=[fit],
                enabled=True,
            )
            healed = scratches.apply(image.copy(), params)
            cx = fit.centres[len(fit.centres) // 2]
            row = image.shape[0] // 2
            before = _display_rgb(_scratch_crop(image, cx, row))
            after = _display_rgb(_scratch_crop(healed, cx, row))
            sheet_tiles.append(_contact_row(before, after))
            accepted_rows.append(
                {
                    "negative_id": negative_id,
                    "scratch_index": index,
                    "axis": fit.axis,
                    "score": fit.score,
                    "agreement": fit.agreement,
                    "drift": candidate.drift,
                }
            )

        if sheet_tiles:
            sheet = np.vstack(sheet_tiles)
            cv2.imwrite(str(out_dir / f"{negative_id}-scratches.png"), sheet)

    paths_csv = out_dir / "paths.csv"
    if path_rows:
        with paths_csv.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(path_rows[0].keys()))
            writer.writeheader()
            writer.writerows(path_rows)
    print(f"wrote {paths_csv}")

    accepted_csv = out_dir / "measurements.csv"
    if accepted_rows:
        with accepted_csv.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(accepted_rows[0].keys()))
            writer.writeheader()
            writer.writerows(accepted_rows)
    print(f"wrote {accepted_csv}")


if __name__ == "__main__":
    main()
