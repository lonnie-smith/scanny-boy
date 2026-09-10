#!/usr/bin/env python3
"""Measure the distortion stability gate.

Sweeps true corner displacement (including zero) against board pitch and
corner noise, and reports the jackknife relative standard error for each
cell — the curve `GEOMETRY_MAX_RELATIVE_SE` was chosen from and must be
re-derived from if it is ever moved. The statistic is the production one:
`geometry_fit.fit_geometry`'s own jackknife, computed inside the fit.

**This script only measures. It changes nothing** — it does not read or
write any of `geometry_fit.py`'s or `charuco.py`'s constants, and it
imports the production modules rather than reimplementing them, exactly
as `scripts/measure-stitch-quality.py` does.

The synthesis instrument: ideal ChArUco
detections at the rig's magnification, pushed through a known radial
field whose corner displacement is the swept truth, plus per-frame corner
noise (a fresh draw per frame — printed-target error is random across
frames, which is the whole reason the statistic separates).

Usage, from the repository root:

    uv run --project cli scripts/measure-stability-gate.py
    uv run --project cli scripts/measure-stability-gate.py --pitches 4.0,2.0,1.2
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cli" / "src"))

# The actual production modules, imported rather than reimplemented, so
# this script cannot silently drift from what production actually does.
from scanny_boy import charuco, geometry_fit

# The scanning rig's measured magnification: ~168 px/mm, a 6048x4024
# frame covering ~36x24 mm.
DEFAULT_PX_PER_MM = 168.0
DEFAULT_WIDTH, DEFAULT_HEIGHT = 6048, 4024
# Sweep axes: true displacement including zero, the
# pitches that bound the rig's boards, and the noise levels that bracket
# the rig's measured 2.5 px floor.
SWEEP_TRUE_PX = (0, 2, 5, 15, 30)
SWEEP_NOISE_PX = (0.5, 1.0, 2.5)
DEFAULT_FRAMES = 16


@dataclasses.dataclass(frozen=True)
class Cell:
    square_mm: float
    true_px: float
    noise_px: float
    fitted_px: float
    relative_se: float
    accepted: bool


def board_spec(
    square_mm: float, width: int, height: int, px_per_mm: float
) -> tuple[charuco.BoardSpec, int, int]:
    """A BoardSpec whose corner grid is whatever fits in the frame at this
    pitch: `cols = floor(width / pitch)` and `rows = floor(height /
    pitch)` corners, one square larger each way for the spec's own
    accounting. At 168 px/mm this reproduces the plan's counts (4.0 mm ->
    40 corners, 2.0 mm -> 187)."""
    pitch_px = square_mm * px_per_mm
    cols = int(width / pitch_px)
    rows = int(height / pitch_px)
    spec = charuco.BoardSpec(
        key=f"{square_mm:g}mm-sweep",
        squares_x=cols + 1,
        squares_y=rows + 1,
        square_length_mm=square_mm,
        marker_length_mm=square_mm * 0.75,
        dictionary="DICT_4X4_1000",
    )
    return spec, rows, cols


def k1_for_displacement(true_px: float, width: int, height: int) -> float:
    """The k1 whose forward model displaces the frame corner by exactly
    `true_px`: closed form, since with k2 = 0 and the centre at the frame
    centre the corner displacement is `cx * |k1| * r2`."""
    K = geometry_fit.base_camera(width, height)
    cx, cy = K[0, 2], K[1, 2]
    fx = K[0, 0]
    r2 = (cx * cx + cy * cy) / (fx * fx)
    return -true_px / (cx * r2)


def synth_frame_sets(
    spec: charuco.BoardSpec,
    rows: int,
    cols: int,
    k1: float,
    noise_px: float,
    rng: np.random.Generator,
    width: int,
    height: int,
) -> list[np.ndarray]:
    """One frame's collinear sets: the ideal corner grid, shifted a little
    so each frame samples the (frame-fixed) distortion field at different
    points, distorted with the known k1, then perturbed by a fresh noise
    draw. Ids are the row-major interior-grid indices `charuco.collinear_sets`
    expects."""
    pitch_x = width / (cols + 1)
    pitch_y = height / (rows + 1)
    xs = np.arange(1, cols + 1) * pitch_x
    ys = np.arange(1, rows + 1) * pitch_y
    ideal = np.stack(np.meshgrid(xs, ys), axis=-1).reshape(-1, 2)
    ideal = ideal + rng.uniform(-pitch_x / 4, pitch_x / 4, size=(1, 2))
    observed = geometry_fit.forward_distort(
        ideal, k1, 0.0, width / 2, height / 2, geometry_fit.base_camera(width, height)
    )
    if noise_px:
        observed = observed + rng.normal(0.0, noise_px, observed.shape)
    ids = np.arange(len(observed), dtype=np.int32).reshape(-1, 1)
    return charuco.collinear_sets(observed.astype(np.float32), ids, spec)


def sweep_cell(
    square_mm: float,
    true_px: float,
    noise_px: float,
    *,
    frames: int,
    width: int,
    height: int,
    px_per_mm: float,
    seed: int,
) -> Cell:
    """One cell: synthesise `frames` frames at this pitch, displacement
    and noise, split every 4th out as held-out exactly as the calibration
    orchestrator does, and run the production fit. Reports its jackknife
    relative SE and the fitted displacement (the noise-induced upward
    bias the plan says to record, not debias)."""
    spec, rows, cols = board_spec(square_mm, width, height, px_per_mm)
    k1 = k1_for_displacement(true_px, width, height)
    rng = np.random.default_rng(seed)
    groups = [
        synth_frame_sets(spec, rows, cols, k1, noise_px, rng, width, height)
        for _ in range(frames)
    ]
    train = [g for i, g in enumerate(groups) if i % 4 != 3]
    heldout = [g for i, g in enumerate(groups) if i % 4 == 3]
    result = geometry_fit.fit_geometry(train, heldout, width, height)
    return Cell(
        square_mm=square_mm,
        true_px=true_px,
        noise_px=noise_px,
        fitted_px=result.corner_displacement_px,
        relative_se=result.jackknife_relative_se,
        accepted=result.accepted,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pitches", default="4.0,2.0")
    parser.add_argument("--noises", default=",".join(str(v) for v in SWEEP_NOISE_PX))
    parser.add_argument("--true-px", default=",".join(str(v) for v in SWEEP_TRUE_PX))
    parser.add_argument("--frames", type=int, default=DEFAULT_FRAMES)
    parser.add_argument("--px-per-mm", type=float, default=DEFAULT_PX_PER_MM)
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    pitches = [float(v) for v in args.pitches.split(",")]
    noises = [float(v) for v in args.noises.split(",")]
    trues = [float(v) for v in args.true_px.split(",")]

    print("# Stability gate sweep\n")
    stamp = datetime.datetime.now(datetime.UTC).astimezone().isoformat(timespec="seconds")
    print(
        f"Generated {stamp} by `scripts/measure-stability-gate.py`: "
        f"{args.frames} frames at {args.px_per_mm:g} px/mm, frame "
        f"{args.width}x{args.height}, every 4th frame held out, "
        f"GEOMETRY_MAX_RELATIVE_SE = {geometry_fit.GEOMETRY_MAX_RELATIVE_SE}.\n"
    )
    print(
        "Each cell is the production fit's jackknife relative standard "
        "error of the corner displacement over leave-one-frame-out "
        "refits. `fitted` shows the noise-induced inflation of the point "
        "estimate (plan section 6.1: the estimator is biased upward by "
        "noise; recorded, not debiased).\n"
    )

    cells = []
    for square_mm in pitches:
        for true_px in trues:
            for noise_px in noises:
                cell = sweep_cell(
                    square_mm,
                    true_px,
                    noise_px,
                    frames=args.frames,
                    width=args.width,
                    height=args.height,
                    px_per_mm=args.px_per_mm,
                    seed=args.seed,
                )
                cells.append(cell)
                print(
                    f"  {square_mm:g} mm | true {true_px:>4.0f} px | noise "
                    f"{noise_px:.1f} px | fitted {cell.fitted_px:6.2f} px | "
                    f"rel SE {cell.relative_se * 100:8.1f}% | "
                    f"{'accepted' if cell.accepted else 'rejected'}",
                    flush=True,
                )

    def column_headers() -> list[str]:
        return [
            f"{square_mm:g} mm @ {noise_px:.1f} px"
            for square_mm in pitches
            for noise_px in noises
        ]

    def cell_at(true_px: float, column: int) -> Cell:
        square_mm = pitches[column // len(noises)]
        noise_px = noises[column % len(noises)]
        return next(
            c
            for c in cells
            if c.square_mm == square_mm
            and c.true_px == true_px
            and c.noise_px == noise_px
        )

    for title, render in (
        (
            "Jackknife relative SE per cell",
            lambda cell: f"{cell.relative_se * 100:.1f}%",
        ),
        (
            "Fitted corner displacement per cell (px)",
            lambda cell: f"{cell.fitted_px:.2f} px",
        ),
    ):
        print(f"\n## {title}\n")
        print("| true corner (px) | " + " | ".join(column_headers()) + " |")
        print("|" + "---|" * (len(column_headers()) + 1))
        for true_px in trues:
            row = [render(cell_at(true_px, column)) for column in range(len(column_headers()))]
            print(f"| {true_px:.0f} | " + " | ".join(row) + " |")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
