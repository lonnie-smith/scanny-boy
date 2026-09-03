#!/usr/bin/env python3
"""Measure the corrected pipeline for docs/STITCH_QUALITY_PLAN.md section 4's
re-tuning gate, on the real gate-B scans.

Every constant `registration.py`, `layout.py`, and `composite.py` gate on was
measured at user gate C against captures with **no** distortion correction,
an isotropic feather, and a scale-1 layout model. All three of those are now
gone (the strip-axis feather, the per-frame scale solve, and the weighted
layout rows all landed on this branch already), so those constants gate a
pipeline that no longer exists.

**This script only measures and proposes. It changes nothing.** Read the
report it prints, then land the new constants in a follow-up commit only
after the user approves the proposed table — the same discipline every
constant in this codebase was set by.

Unlike `scripts/measure-registration.py` (P2-1, written before `detection.py`,
`registration.py`, `layout.py`, and `composite.py` existed, so it had to
reimplement their algorithms to predict them), this script **imports the
production modules** and calls them directly. There is no second
implementation to keep in step here.

Usage, from the repository root:

    uv run --project cli scripts/measure-stitch-quality.py
    uv run --project cli scripts/measure-stitch-quality.py --out /tmp/quality
    uv run --project cli scripts/measure-stitch-quality.py --long-edges 2000,3000
    uv run --project cli scripts/measure-stitch-quality.py --profile /path/to/profile.json
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import statistics
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cli" / "src"))

# Imported after the path insert above, so this runs from a checkout without
# the package installed, exactly as measure-registration.py does it. These
# are the actual production modules docs/STITCH_QUALITY_PLAN.md section 4.1
# requires this script to import rather than reimplement.
from scanny_boy import composite as composite_module
from scanny_boy import layout as layout_module
from scanny_boy import registration as registration_module
from scanny_boy.cancellation import CancellationToken
from scanny_boy.detection import DETECTION_LONG_EDGE, build_detection_image
from scanny_boy.raw_decode import decode_raw

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_NEF_DIR = ROOT / "tests" / "fixtures" / "nef"

# The gate-B negatives (docs/PHASE2_IMPLEMENTATION_PLAN.md section 5, user
# gate B), the same ones registration_test.py's test_real_sample_pairs_meet_
# their_gates checks: a routine strip, a deliberately rotated one, one shot
# out of spatial order, one at minimum overlap, and one whose three frames
# share no film with each other at all (the negative that is *supposed* to
# fail). Ground truth for which pairs of each genuinely overlap is
# registration_test.py's own appendix-C table, reproduced here so this
# script has no import-time dependency on a test module.
NEGATIVES: dict[str, dict] = {
    "normal": {
        "frames": ["normal_1.NEF", "normal_2.NEF", "normal_3.NEF"],
        "overlapping": [(1, 2), (2, 3)],
    },
    "wonky": {
        "frames": ["wonky_1.NEF", "wonky_2.NEF", "wonky_3.NEF"],
        "overlapping": [(1, 2), (2, 3), (1, 3)],
    },
    "order": {
        "frames": ["order_1.NEF", "order_2.NEF", "order_3.NEF"],
        "overlapping": [(1, 2), (2, 3), (1, 3)],
    },
    "tight": {
        "frames": ["tight_1.NEF", "tight_2.NEF", "tight_3.NEF"],
        "overlapping": [(1, 3), (2, 3)],
    },
    "mismatch": {
        "frames": ["mismatch_1.NEF", "mismatch_2.NEF", "mismatch_3.NEF"],
        "overlapping": [],
    },
}
GOOD_NEGATIVES = ["normal", "wonky", "order", "tight"]
BAD_NEGATIVES = ["mismatch"]

SWEEP_LONG_EDGES = (2000, 3000, 4000)

# Today's gate-C constants, for the "today" column of the proposal table
# (section 4.2). Read from the modules themselves, not retyped, so this
# script cannot silently drift from what production actually gates on.
_TODAY = {
    "DETECTION_LONG_EDGE": DETECTION_LONG_EDGE,
    "MAX_PAIR_RMS_PX": registration_module.MAX_PAIR_RMS_PX,
    "MAX_GLOBAL_RMS_PX": layout_module.MAX_GLOBAL_RMS_PX,
    "SCALE_DRIFT_WARN": registration_module.SCALE_DRIFT_WARN,
    "SCALE_DRIFT_FAIL": registration_module.SCALE_DRIFT_FAIL,
    "MAX_OVERLAP_MAD": composite_module.MAX_OVERLAP_MAD,
    "MIN_GAIN_OVERLAP_PX": composite_module.MIN_GAIN_OVERLAP_PX,
    "GAIN_DRIFT_WARN": composite_module.GAIN_DRIFT_WARN,
}


@dataclasses.dataclass
class PairMeasurement:
    negative: str
    a: str
    b: str
    good_matches: int
    inliers: int
    inlier_ratio: float
    rms_residual_px: float
    similarity_scale: float
    accepted: bool
    truly_overlaps: bool
    overlap_fraction: float | None = None
    overlap_mad: float | None = None


@dataclasses.dataclass
class NegativeMeasurement:
    negative: str
    long_edge: int
    detect_seconds: float
    match_seconds: float
    pairs: list[PairMeasurement]
    solved: bool
    global_rms_px: float | None = None
    strip_spread_ratio: float | None = None
    scales: dict[str, float] | None = None
    peak_rss_bytes: int | None = None


def frame_names(negative: str) -> list[str]:
    return [Path(f).stem for f in NEGATIVES[negative]["frames"]]


def truly_overlapping_pairs(negative: str) -> set[tuple[int, int]]:
    return set(NEGATIVES[negative]["overlapping"])


def _pair_indices(name_a: str, name_b: str) -> tuple[int, int]:
    return (int(name_a.rsplit("_", 1)[1]), int(name_b.rsplit("_", 1)[1]))


def load_negative_frames(nef_dir: Path, negative: str) -> dict[str, np.ndarray]:
    """Decodes every frame of one negative once, uint16 (H, W, 3) — the same
    shape `composite.composite`'s `load_frame` callback returns."""
    frames = {}
    for filename in NEGATIVES[negative]["frames"]:
        name = Path(filename).stem
        frames[name] = decode_raw(nef_dir / filename).pixels
    return frames


def measure_negative(
    negative: str, frames: dict[str, np.ndarray], *, long_edge: int, clahe: bool
) -> NegativeMeasurement:
    """Detect, match, and lay out one negative's frames at one
    `DETECTION_LONG_EDGE`, calling `registration.py` and `layout.py`
    directly. Compositing (and its overlap-MAD measurement) only runs when
    a layout actually solves."""
    names = frame_names(negative)

    detect_started = time.monotonic()
    features = {}
    for name in names:
        detection = build_detection_image(frames[name], long_edge=long_edge, clahe=clahe)
        features[name] = registration_module.detect_features(detection, name=name)
    detect_seconds = time.monotonic() - detect_started

    match_started = time.monotonic()
    overlapping = truly_overlapping_pairs(negative)
    pair_results = []
    measurements = []
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            result = registration_module.register_pair(features[a], features[b])
            pair_results.append(result)
            measurements.append(
                PairMeasurement(
                    negative=negative,
                    a=a,
                    b=b,
                    good_matches=result.good_matches,
                    inliers=result.inliers,
                    inlier_ratio=result.inlier_ratio,
                    rms_residual_px=result.rms_residual_px,
                    similarity_scale=result.similarity_scale,
                    accepted=result.accepted,
                    truly_overlaps=_pair_indices(a, b) in overlapping,
                )
            )
    match_seconds = time.monotonic() - match_started

    negative_measurement = NegativeMeasurement(
        negative=negative,
        long_edge=long_edge,
        detect_seconds=detect_seconds,
        match_seconds=match_seconds,
        pairs=measurements,
        solved=False,
    )

    try:
        layout_module.check_connectivity(names, pair_results)
    except registration_module.StitchError:
        return negative_measurement

    frame_size = (frames[names[0]].shape[0], frames[names[0]].shape[1])
    solved_layout = layout_module.solve_layout(names, frame_size, pair_results)
    negative_measurement.solved = True
    negative_measurement.global_rms_px = solved_layout.global_rms_px
    negative_measurement.strip_spread_ratio = solved_layout.strip_spread_ratio
    negative_measurement.scales = {
        p.name: p.scale for p in solved_layout.placements
    }

    result = composite_module.composite(
        solved_layout,
        lambda name: frames[name],
        cancel=CancellationToken(),
        on_progress=lambda: None,
    )
    for m in measurements:
        key = (m.a, m.b)
        m.overlap_fraction = result.overlap_fraction.get(key)
        m.overlap_mad = result.overlap_mad.get(key)

    return negative_measurement


# --------------------------------------------------------------------------
# Markdown helpers
# --------------------------------------------------------------------------


def table(headers: list[str], rows: list[list[str]]) -> None:
    print("| " + " | ".join(headers) + " |")
    print("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        print("| " + " | ".join(row) + " |")
    print()


def num(value, places: int = 4) -> str:
    if value is None:
        return "—"
    if isinstance(value, float) and (np.isnan(value) or np.isinf(value)):
        return "—"
    return f"{value:.{places}f}"


def pair_label(a: str, b: str) -> str:
    return f"{a.rsplit('_', 1)[1]}–{b.rsplit('_', 1)[1]}"


def median_or_none(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


def print_pair_table(measurements: list[NegativeMeasurement]) -> None:
    print("### Per pair\n")
    rows = []
    for nm in measurements:
        for pm in nm.pairs:
            rows.append([
                str(nm.long_edge), pm.negative, pair_label(pm.a, pm.b),
                "yes" if pm.truly_overlaps else "no",
                "yes" if pm.accepted else "no",
                str(pm.good_matches), str(pm.inliers), num(pm.inlier_ratio),
                num(pm.rms_residual_px, 3), num(pm.similarity_scale, 6),
                num(pm.overlap_fraction, 4), num(pm.overlap_mad, 5),
            ])
    table(
        ["long_edge", "negative", "pair", "truly_overlaps", "accepted",
         "good_matches", "inliers", "inlier_ratio", "rms_residual_px",
         "similarity_scale", "overlap_fraction", "overlap_mad"],
        rows,
    )


def print_negative_table(measurements: list[NegativeMeasurement]) -> None:
    print("### Per negative\n")
    rows = []
    for nm in measurements:
        scale_spread = None
        if nm.scales:
            values = list(nm.scales.values())
            scale_spread = max(values) - min(values)
        worst_mad = None
        overlapping_mads = [
            p.overlap_mad for p in nm.pairs if p.truly_overlaps and p.overlap_mad is not None
        ]
        if overlapping_mads:
            worst_mad = max(overlapping_mads)
        rows.append([
            str(nm.long_edge), nm.negative, "yes" if nm.solved else "no",
            num(nm.global_rms_px, 3), num(nm.strip_spread_ratio, 4),
            num(scale_spread, 5), num(worst_mad, 5),
            num(nm.detect_seconds, 2), num(nm.match_seconds, 2),
        ])
    table(
        ["long_edge", "negative", "solved", "global_rms_px", "strip_spread_ratio",
         "scale_spread", "worst_overlap_mad_of_true_pairs", "detect_seconds",
         "match_seconds"],
        rows,
    )


def print_sweep_summary(by_long_edge: dict[int, list[NegativeMeasurement]]) -> None:
    print("## Detection resolution sweep\n")
    print(
        "Descriptor matching is O(n_a . n_b), and keypoint count grows roughly "
        "with pixel count, so 2000 -> 4000 can cost on the order of 16x in "
        "matching alone. If 4000 buys no measurable metric improvement over "
        "3000, the recommendation is 3000 and this report says so with the "
        "numbers below.\n"
    )
    rows = []
    for long_edge, measurements in sorted(by_long_edge.items()):
        good_pairs = [
            p for nm in measurements for p in nm.pairs if p.truly_overlaps
        ]
        recovered = [p for p in good_pairs if p.accepted]
        detect_total = sum(nm.detect_seconds for nm in measurements)
        match_total = sum(nm.match_seconds for nm in measurements)
        rows.append([
            str(long_edge),
            f"{len(recovered)}/{len(good_pairs)}",
            num(median_or_none([float(p.inliers) for p in recovered]), 0),
            num(median_or_none([p.rms_residual_px for p in recovered]), 3),
            num(detect_total, 1), num(match_total, 1),
        ])
    table(
        ["long_edge", "overlapping pairs recovered", "median_inliers",
         "median_rms_px", "total_detect_seconds", "total_match_seconds"],
        rows,
    )


def propose_constants(
    by_long_edge: dict[int, list[NegativeMeasurement]], chosen_long_edge: int
) -> None:
    print("## Proposed gate-D constants\n")
    print(
        "Each threshold below is set to the healthy data's worst observation "
        "plus a stated margin, not to a round number. **This table is a "
        "proposal, not a change** — docs/STITCH_QUALITY_PLAN.md section 4 "
        "stops here for the user's approval; the constants change in a "
        "follow-up commit only after that.\n"
    )
    measurements = by_long_edge[chosen_long_edge]
    good_pairs = [
        p for nm in measurements for p in nm.pairs
        if nm.negative in GOOD_NEGATIVES and p.truly_overlaps and p.accepted
    ]
    rms_values = [p.rms_residual_px for p in good_pairs]
    scale_drift_values = [abs(p.similarity_scale - 1.0) for p in good_pairs]
    overlap_mad_values = [
        p.overlap_mad for p in good_pairs if p.overlap_mad is not None
    ]
    global_rms_values = [
        nm.global_rms_px for nm in measurements
        if nm.negative in GOOD_NEGATIVES and nm.global_rms_px is not None
    ]

    def worst_plus_margin(values: list[float], margin_ratio: float) -> float | None:
        if not values:
            return None
        return max(values) * (1.0 + margin_ratio)

    rows = [
        [
            "DETECTION_LONG_EDGE", "detection.py",
            str(_TODAY["DETECTION_LONG_EDGE"]), str(chosen_long_edge),
        ],
        [
            "MAX_PAIR_RMS_PX", "registration.py",
            num(_TODAY["MAX_PAIR_RMS_PX"], 1),
            num(worst_plus_margin(rms_values, 0.25), 2) if rms_values else "no healthy data",
        ],
        [
            "MAX_GLOBAL_RMS_PX", "layout.py",
            num(_TODAY["MAX_GLOBAL_RMS_PX"], 1),
            num(worst_plus_margin(global_rms_values, 0.25), 2)
            if global_rms_values else "no healthy data",
        ],
        [
            "SCALE_DRIFT_WARN / SCALE_DRIFT_FAIL", "registration.py",
            f"{num(_TODAY['SCALE_DRIFT_WARN'], 4)} / {num(_TODAY['SCALE_DRIFT_FAIL'], 4)}",
            (
                f"{num(worst_plus_margin(scale_drift_values, 0.5), 4)} / "
                f"{num(worst_plus_margin(scale_drift_values, 1.0), 4)}"
            ) if scale_drift_values else "no healthy data",
        ],
        [
            "MAX_OVERLAP_MAD", "composite.py",
            num(_TODAY["MAX_OVERLAP_MAD"], 3),
            num(worst_plus_margin(overlap_mad_values, 0.25), 4)
            if overlap_mad_values else "no healthy data",
        ],
    ]
    table(["constant", "module", "today", "proposed"], rows)
    print(
        "`MIN_GAIN_OVERLAP_PX` and `GAIN_DRIFT_WARN` need a gain-drift "
        "distribution across healthy pairs, which this table does not "
        "collect separately from `MAX_OVERLAP_MAD`'s pre/post-gain pairing; "
        "fold them into the same gate rather than leaving a second one "
        "owing, using `composite.CompositeResult.overlap_mad_pregain` "
        "against `overlap_mad` on this same run's data.\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nef-dir", type=Path, default=DEFAULT_NEF_DIR, dest="nef_dir")
    parser.add_argument("--out", type=Path, default=Path("/tmp/scanny-stitch-quality"))
    parser.add_argument("--long-edges", default=",".join(str(v) for v in SWEEP_LONG_EDGES))
    parser.add_argument(
        "--clahe", action="store_true",
        help="Use CLAHE-enhanced detection images (USE_CLAHE fallback path).",
    )
    args = parser.parse_args()

    long_edges = [int(v) for v in args.long_edges.split(",")]

    files = [f for negative in NEGATIVES.values() for f in negative["frames"]]
    missing = [f for f in files if not (args.nef_dir / f).exists()]
    if missing:
        print(
            "Nothing was measured: the gate-B sample scans are not present "
            f"at {args.nef_dir}.\n\n"
            f"Missing {len(missing)} of {len(files)}: {', '.join(missing)}\n\n"
            "docs/STITCH_QUALITY_PLAN.md section 4.1 requires the real gate-B "
            "NEF fixtures (routine, rotated, out-of-order, minimum-overlap, "
            "and non-overlapping negatives) — substitutes may not be "
            "synthesised, the same rule scripts/measure-registration.py "
            "follows.",
            file=sys.stderr,
        )
        return 1

    print("# Stitch quality measurements (docs/STITCH_QUALITY_PLAN.md section 4)\n")
    stamp = datetime.datetime.now(datetime.UTC).astimezone().isoformat(timespec="seconds")
    print(
        f"Generated {stamp} by `scripts/measure-stitch-quality.py` from "
        f"`{args.nef_dir}`, OpenCV {cv2.__version__}.\n"
    )
    print(
        f"Negatives: {', '.join(GOOD_NEGATIVES)} (expected to stitch); "
        f"{', '.join(BAD_NEGATIVES)} (expected to fail). Sweeping "
        f"DETECTION_LONG_EDGE over {long_edges}.\n"
    )

    all_frames = {
        negative: load_negative_frames(args.nef_dir, negative) for negative in NEGATIVES
    }

    by_long_edge: dict[int, list[NegativeMeasurement]] = {}
    for long_edge in long_edges:
        measurements = []
        for negative in NEGATIVES:
            measurements.append(
                measure_negative(
                    negative, all_frames[negative], long_edge=long_edge, clahe=args.clahe
                )
            )
        by_long_edge[long_edge] = measurements

    for long_edge in long_edges:
        print(f"## DETECTION_LONG_EDGE = {long_edge}\n")
        print_negative_table(by_long_edge[long_edge])
        print_pair_table(by_long_edge[long_edge])

    print_sweep_summary(by_long_edge)
    propose_constants(by_long_edge, chosen_long_edge=long_edges[0])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
