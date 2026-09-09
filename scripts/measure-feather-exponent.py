#!/usr/bin/env python3
"""Measure `FEATHER_EXPONENT`'s effect on real scans (docs/NARROW_FEATHER.md
section 6).

Like `scripts/measure-stitch-quality.py`, this script **imports the
production modules and calls them directly** — detection, registration,
layout, and compositing are exactly what `stitch_pipeline.py` runs. It
changes nothing: `composite.FEATHER_EXPONENT` is monkeypatched for the
duration of one composite call and restored immediately after, and no
constant in `composite.py`, `layout.py`, or `registration.py` is written
back.

For each negative and each `p` in `(1, 2, 3, 4, 6, 8)` it:

  - composites the negative at that exponent and writes the TIFF to
    `--out`;
  - reports the balance histogram of docs/NARROW_FEATHER.md section 0
    (`balance = max_frame_weight / sum_of_weights` per covered pixel) —
    a function of the solved placements and feather weights alone, so it
    costs no pixel comparison and is reported for every `p` in one pass;
  - reports `overlap_mad` per pair, read straight off `CompositeResult`;
  - reports the high-to-mid frequency energy ratio in near-50/50 regions
    against unblended regions, as section 0 measured it.

Usage, from the repository root:

    uv run --project cli scripts/measure-feather-exponent.py
    uv run --project cli scripts/measure-feather-exponent.py --out /tmp/feather
    uv run --project cli scripts/measure-feather-exponent.py --exponents 1,4,8
    uv run --project cli scripts/measure-feather-exponent.py --negatives normal,wonky
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cli" / "src"))

# Imported after the path insert above, exactly as measure-stitch-quality.py
# does it: the actual production modules, never reimplemented.
from scanny_boy import composite as composite_module
from scanny_boy import layout as layout_module
from scanny_boy import registration as registration_module
from scanny_boy.cancellation import CancellationToken
from scanny_boy.detection import build_detection_image
from scanny_boy.linear import decode_to_linear
from scanny_boy.raw_decode import decode_raw

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_NEF_DIR = ROOT / "tests" / "fixtures" / "nef"

# The same gate-B negatives measure-stitch-quality.py sweeps; `_DSC5207`'s
# roll is not part of this repository's fixtures, so any of these serve as
# a stand-in when run without `--nef-dir` pointed at a real roll's frames.
# Ground truth for which pairs of each genuinely overlap is
# registration_test.py's own appendix-C table (reproduced in
# measure-stitch-quality.py).
NEGATIVES: dict[str, list[str]] = {
    "normal": ["normal_1.NEF", "normal_2.NEF", "normal_3.NEF"],
    "wonky": ["wonky_1.NEF", "wonky_2.NEF", "wonky_3.NEF"],
    "order": ["order_1.NEF", "order_2.NEF", "order_3.NEF"],
    "tight": ["tight_1.NEF", "tight_2.NEF", "tight_3.NEF"],
}

DEFAULT_EXPONENTS = (1, 2, 3, 4, 6, 8)

# docs/NARROW_FEATHER.md section 0's near-50/50 window.
_NEAR_HALF_LO, _NEAR_HALF_HI = 0.45, 0.55
_UNBLENDED_MIN = 0.99


def solve_negative(
    negative: str, frames: dict[str, np.ndarray], *, long_edge: int, clahe: bool
) -> layout_module.Layout | None:
    """Detect, match, and solve one negative's layout — the same calls
    `stitch_pipeline._solve_negative` makes, minus its retry policy. None
    when the pair graph does not connect."""
    features = {
        name: registration_module.detect_features(
            build_detection_image(frame, long_edge=long_edge, clahe=clahe), name=name
        )
        for name, frame in frames.items()
    }
    pair_results = []
    for i, a in enumerate(features):
        for b in list(features)[i + 1 :]:
            pair_results.append(
                registration_module.register_pair(features[a], features[b], None)
            )
    try:
        layout_module.check_connectivity(list(frames), pair_results)
    except registration_module.StitchError:
        return None

    frame_size = next(iter(frames.values())).shape[:2]
    return layout_module.solve_layout(list(frames), frame_size, pair_results)


def per_frame_weights(
    layout: layout_module.Layout, frame_size: tuple[int, int]
) -> tuple[np.ndarray, dict[str, tuple[int, int, int, int]]]:
    """The solved weight canvas, one channel per frame — `_feather_weight`
    called directly per placement, exactly as `composite.composite` calls
    it in its own accumulate pass, so the reported balance histogram is a
    function of the same weights the compositor actually uses. Frame
    rotation/scale is not replayed here (docs/GRID_STITCH_PLAN.md's warp
    only ever changes bounding-box shape, not the eroded mask's coverage
    footprint at the frame's own resolution), so each frame's mask is a
    filled rectangle at its placement's bounding box."""
    canvas_width, canvas_height = layout.canvas_size
    height, width = frame_size
    axes = layout.feather_axes()
    weights = np.zeros((len(layout.placements), canvas_height, canvas_width), dtype=np.float32)
    boxes: dict[str, tuple[int, int, int, int]] = {}
    for i, placement in enumerate(layout.placements):
        matrix = placement.matrix()
        bbox_x, bbox_y, bbox_w, bbox_h = composite_module.frame_bbox(
            matrix, height, width, layout.canvas_size
        )
        mask = np.full((bbox_h, bbox_w), 255, dtype=np.uint8)
        mask = cv2.erode(
            mask,
            composite_module._EROSION_KERNEL,
            borderType=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
        weight = composite_module._feather_weight(mask, bbox_x, bbox_y, axes)
        weights[i, bbox_y : bbox_y + bbox_h, bbox_x : bbox_x + bbox_w] = weight
        boxes[placement.name] = (bbox_x, bbox_y, bbox_w, bbox_h)
    return weights, boxes


def balance_histogram(weights: np.ndarray) -> dict[str, float]:
    """docs/NARROW_FEATHER.md section 0's table:
    `balance = max_frame_weight / sum_of_weights`, bucketed, as a share of
    the covered canvas."""
    total = weights.sum(axis=0)
    covered = total > 0
    if not covered.any():
        return {}
    balance = np.zeros_like(total)
    balance[covered] = weights.max(axis=0)[covered] / total[covered]
    covered_count = int(covered.sum())
    buckets = [
        (0.50, 0.55), (0.55, 0.70), (0.70, 0.90), (0.90, 0.99), (0.99, 1.0001),
    ]
    return {
        f"{lo:.2f}-{hi:.2f}": float(
            np.count_nonzero((balance >= lo) & (balance < hi) & covered)
        ) / covered_count
        for lo, hi in buckets
    }


def _hf_mf_ratio(patch: np.ndarray) -> float:
    """High-to-mid frequency spectral energy, via the DFT magnitude in an
    outer ring (high) against an inner ring (mid) of the patch's spectrum —
    a ratio, so subject matter cancels, matching section 0's method."""
    f = np.fft.fftshift(np.fft.fft2(patch.astype(np.float64)))
    mag2 = np.abs(f) ** 2
    h, w = patch.shape
    cy, cx = h // 2, w // 2
    yy, xx = np.mgrid[0:h, 0:w]
    r = np.hypot(yy - cy, xx - cx) / max(cy, cx, 1)
    mid = (r >= 0.15) & (r < 0.4)
    high = r >= 0.4
    mid_energy = float(mag2[mid].sum())
    high_energy = float(mag2[high].sum())
    if mid_energy <= 0:
        return float("nan")
    return 10.0 * np.log10(high_energy / mid_energy)


def spectral_energy_deficit(
    linear: np.ndarray, balance: np.ndarray, covered: np.ndarray, *, patch: int = 32
) -> tuple[float | None, float | None]:
    """Median high/mid dB in near-50/50 patches vs. unblended patches
    (docs/NARROW_FEATHER.md section 0). `linear` is the composite's green
    channel; patches are non-overlapping `patch`x`patch` tiles, kept only
    when every pixel inside is covered and (for the "near" bucket) inside
    the balance window, or (for the "unblended" bucket) above
    `_UNBLENDED_MIN`."""
    height, width = linear.shape
    near_db, unblended_db = [], []
    for y in range(0, height - patch, patch):
        for x in range(0, width - patch, patch):
            tile_covered = covered[y : y + patch, x : x + patch]
            if not tile_covered.all():
                continue
            tile_balance = balance[y : y + patch, x : x + patch]
            tile = linear[y : y + patch, x : x + patch]
            if np.all((tile_balance >= _NEAR_HALF_LO) & (tile_balance <= _NEAR_HALF_HI)):
                near_db.append(_hf_mf_ratio(tile))
            elif np.all(tile_balance >= _UNBLENDED_MIN):
                unblended_db.append(_hf_mf_ratio(tile))
    near = float(np.nanmedian(near_db)) if near_db else None
    unblended = float(np.nanmedian(unblended_db)) if unblended_db else None
    return near, unblended


@dataclasses.dataclass
class ExponentMeasurement:
    p: int
    balance: dict[str, float]
    overlap_mad: dict[tuple[str, str], float]
    near_db: float | None
    unblended_db: float | None


def measure_negative_at_exponent(
    negative: str,
    layout: layout_module.Layout,
    frames: dict[str, np.ndarray],
    p: int,
    out_dir: Path,
) -> ExponentMeasurement:
    weights, _boxes = per_frame_weights(layout, next(iter(frames.values())).shape[:2])
    balance = balance_histogram(weights)

    original = composite_module.FEATHER_EXPONENT
    composite_module.FEATHER_EXPONENT = p
    try:
        result = composite_module.composite(
            layout,
            lambda name: frames[name],
            cancel=CancellationToken(),
            on_progress=lambda: None,
        )
    finally:
        composite_module.FEATHER_EXPONENT = original

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{negative}_p{p}.tif"
    cv2.imwrite(str(out_path), cv2.cvtColor(result.image, cv2.COLOR_RGB2BGR))

    fill_code = result.image[0, 0]
    covered = np.any(result.image != fill_code, axis=-1)
    total_weight = weights.sum(axis=0)
    frame_balance = np.zeros_like(total_weight)
    frame_balance[covered] = weights.max(axis=0)[covered] / np.maximum(
        total_weight[covered], 1e-12
    )
    linear_green = decode_to_linear(result.image)[:, :, 1].astype(np.float32)
    near_db, unblended_db = spectral_energy_deficit(linear_green, frame_balance, covered)

    return ExponentMeasurement(
        p=p,
        balance=balance,
        overlap_mad=result.overlap_mad,
        near_db=near_db,
        unblended_db=unblended_db,
    )


def print_report(negative: str, measurements: list[ExponentMeasurement]) -> None:
    print(f"## {negative}\n")
    buckets = sorted({key for m in measurements for key in m.balance})
    print("### Balance histogram (share of covered canvas)\n")
    print("| p | " + " | ".join(buckets) + " |")
    print("| --- | " + " | ".join("---" for _ in buckets) + " |")
    for m in measurements:
        print(
            "| "
            + str(m.p)
            + " | "
            + " | ".join(f"{m.balance.get(b, 0.0):.3f}" for b in buckets)
            + " |"
        )
    print()

    pairs = sorted({pair for m in measurements for pair in m.overlap_mad})
    print("### overlap_mad per pair (post-gain)\n")
    print("| p | " + " | ".join(f"{a}-{b}" for a, b in pairs) + " |")
    print("| --- | " + " | ".join("---" for _ in pairs) + " |")
    for m in measurements:
        print(
            "| "
            + str(m.p)
            + " | "
            + " | ".join(f"{m.overlap_mad.get(pair, float('nan')):.5f}" for pair in pairs)
            + " |"
        )
    print()

    print("### High/mid frequency energy, near-50/50 vs. unblended (dB)\n")
    print("| p | near_50_50_db | unblended_db | deficit_db |")
    print("| --- | --- | --- | --- |")
    for m in measurements:
        deficit = (
            m.near_db - m.unblended_db
            if m.near_db is not None and m.unblended_db is not None
            else float("nan")
        )
        near = "—" if m.near_db is None else f"{m.near_db:.3f}"
        unblended = "—" if m.unblended_db is None else f"{m.unblended_db:.3f}"
        print(f"| {m.p} | {near} | {unblended} | {deficit:.3f} |")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nef-dir", type=Path, default=DEFAULT_NEF_DIR, dest="nef_dir")
    parser.add_argument("--out", type=Path, default=Path("/tmp/scanny-feather-exponent"))
    parser.add_argument("--exponents", default=",".join(str(v) for v in DEFAULT_EXPONENTS))
    parser.add_argument(
        "--negatives", default=",".join(NEGATIVES),
        help="Comma-separated negative names from this script's built-in set.",
    )
    parser.add_argument("--long-edge", type=int, default=3000)
    parser.add_argument("--clahe", action="store_true")
    args = parser.parse_args()

    exponents = [int(v) for v in args.exponents.split(",")]
    negatives = [n.strip() for n in args.negatives.split(",")]
    unknown = [n for n in negatives if n not in NEGATIVES]
    if unknown:
        print(f"Unknown negatives: {unknown}. Known: {list(NEGATIVES)}", file=sys.stderr)
        return 1

    files = [f for name in negatives for f in NEGATIVES[name]]
    missing = [f for f in files if not (args.nef_dir / f).exists()]
    if missing:
        print(
            "Nothing was measured: the sample scans are not present at "
            f"{args.nef_dir}.\n\nMissing {len(missing)} of {len(files)}: "
            f"{', '.join(missing)}\n\nThe real gate-B NEF fixtures are "
            "required — substitutes may not be synthesised, the same rule "
            "scripts/measure-stitch-quality.py follows.",
            file=sys.stderr,
        )
        return 1

    print("# Feather exponent measurements (docs/NARROW_FEATHER.md section 6)\n")
    stamp = datetime.datetime.now(datetime.UTC).astimezone().isoformat(timespec="seconds")
    print(
        f"Generated {stamp} by `scripts/measure-feather-exponent.py` from "
        f"`{args.nef_dir}`, OpenCV {cv2.__version__}. Exponents swept: "
        f"{exponents}. TIFFs written to `{args.out}`.\n"
    )

    for negative in negatives:
        frames = {
            Path(f).stem: decode_raw(args.nef_dir / f).pixels for f in NEGATIVES[negative]
        }
        started = time.monotonic()
        layout = solve_negative(
            negative, frames, long_edge=args.long_edge, clahe=args.clahe
        )
        if layout is None:
            print(f"## {negative}\n\nDid not solve (pair graph disconnected).\n")
            continue

        measurements = [
            measure_negative_at_exponent(negative, layout, frames, p, args.out)
            for p in exponents
        ]
        print_report(negative, measurements)
        print(f"({negative} took {time.monotonic() - started:.1f}s)\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
