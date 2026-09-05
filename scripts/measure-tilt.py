#!/usr/bin/env python3
"""Discriminate rigid camera tilt from film non-flatness, from the stitches'
own correspondences.

The hypothesis this measures (docs/STITCH_QUALITY_PLAN.md's similarity model
vs reality): the camera sits slightly off fronto-parallel to the film plane,
so the true frame-to-frame map is a homography, not the similarity
`layout.py` solves. A similarity fitted to homography-shaped data leaves a
small systematic residual per pair — invisible against MAX_PAIR_RMS_PX —
that accumulates along a strip and bows physically straight film edges.

The discriminator is per-pair model comparison on the same inliers
`register_pair` already returns:

- **rigid** — production's fit, the number the gates measure (context).
- **similarity** — production's fit, what `solve_layout` composes.
- **homography** — 8 free parameters per pair. If its RMS collapses to the
  keypoint-noise floor while the similarity's does not, the data carries a
  projective component.
- **restricted 2-param tilt** — `H = W^-1 . S . W` with one *globally
  shared* rectifying homography `W = [[1,0,0],[0,1,0],[l1,l2,1]]` (centred,
  full-resolution px) and a per-pair similarity. Two rig parameters total.
  If this matches the per-pair homography, the projective component is one
  rigid tilt; if the per-pair homography wins but the shared-l model does
  not, the film is not planar and no global rectification can fix it.

The per-pair restricted fits also estimate `l` independently per pair, so
consistency of `l` across pairs and across negatives can be checked
directly.

Tilt angles are reported in degrees using the EXIF focal length and the
decoded frame width (assumes a 35.9 mm-wide sensor, constant pixel pitch
across crop modes) — an approximation for reporting only; the RMS columns
carry the decision and need no focal length.

**This script only measures. It changes nothing** — production modules are
imported and called, never re-implemented, and no constant is read or
written.

Usage, from the repository root:

    uv run --project cli scripts/measure-tilt.py --nef-dir ~/Downloads/"scanny boy inputs"
    uv run --project cli scripts/measure-tilt.py --nef-dir DIR --group chunks --shots-per-negative 3
    uv run --project cli scripts/measure-tilt.py --nef-dir DIR --negative _DSC4638.NEF,_DSC4639.NEF,_DSC4640.NEF
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import math
import statistics
import sys
from pathlib import Path

import numpy as np
from scipy.optimize import least_squares

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cli" / "src"))

from scanny_boy import layout as layout_module
from scanny_boy import registration as registration_module
from scanny_boy.detection import (
    DETECTION_LONG_EDGE,
    USE_CLAHE,
    build_detection_image,
)
from scanny_boy.metadata import read_digitization_fields, read_exif_settings
from scanny_boy.raw_decode import decode_raw

# A pair needs at least this many inliers before any of this script's own
# model fits mean anything; production's MIN_PAIR_INLIERS gate is stricter.
MIN_FIT_INLIERS = 10

# Pairs whose similarity RMS is below this are already at the keypoint
# noise floor: every model ties there, l is unidentifiable, and they are
# excluded from the verdict aggregates.
STRUCTURED_RMS_FLOOR_PX = 0.5

DEFAULT_SENSOR_WIDTH_MM = 35.9


# --------------------------------------------------------------------------
# Grouping
# --------------------------------------------------------------------------


def capture_times(paths: list[Path]) -> dict[str, datetime.datetime]:
    times = {}
    for path in paths:
        text = read_digitization_fields(path).date_time_original
        times[path.name] = datetime.datetime.strptime(  # noqa: DTZ007
            text, "%Y:%m:%d %H:%M:%S"
        )
    return times


def group_by_gap(
    names: list[str], times: dict[str, datetime.datetime], gap_seconds: float
) -> list[list[str]]:
    """Split the canonical order wherever the capture-time gap exceeds
    `gap_seconds`. Frames shot inside one second (a burst) sort by name."""
    ordered = sorted(names, key=lambda n: (times[n], n))
    groups: list[list[str]] = []
    current = [ordered[0]]
    for previous, name in zip(ordered, ordered[1:]):
        if (times[name] - times[previous]).total_seconds() > gap_seconds:
            groups.append(current)
            current = []
        current.append(name)
    groups.append(current)
    return [g for g in groups if len(g) >= 2]


def group_by_chunks(names: list[str], per_negative: int) -> list[list[str]]:
    groups = [
        names[i : i + per_negative] for i in range(0, len(names), per_negative)
    ]
    return [g for g in groups if len(g) >= 2]


# --------------------------------------------------------------------------
# Geometry: the rectifying model
# --------------------------------------------------------------------------


def rectify(points: np.ndarray, l: np.ndarray, centre: np.ndarray) -> np.ndarray:
    """Image px -> rectified (centred) px through W(l) = [[1,0,0],[0,1,0],
    [l1,l2,1]] applied to centred coordinates: divide by the homogeneous
    weight. Points must be (N, 2), l a 2-vector."""
    q = points - centre
    w = 1.0 + q @ l
    return q / w[:, None]


def unrectify(q_rect: np.ndarray, l: np.ndarray) -> np.ndarray:
    """The inverse map: q = q' / (1 - l . q')."""
    w = 1.0 - q_rect @ l
    return q_rect / w[:, None]


def fit_homography(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Hartley-normalized DLT, 8 degrees of freedom, mapping src -> dst.
    Returns 3x3 with H[2,2] = 1."""
    def normalizer(points: np.ndarray) -> np.ndarray:
        c = points.mean(axis=0)
        scale = math.sqrt(2.0) / np.mean(
            np.linalg.norm(points - c, axis=1)
        )
        return np.array(
            [[scale, 0, -scale * c[0]], [0, scale, -scale * c[1]], [0, 0, 1]]
        )

    ts, td = normalizer(src), normalizer(dst)
    src_h = np.c_[src, np.ones(len(src))] @ ts.T
    dst_h = np.c_[dst, np.ones(len(dst))] @ td.T

    rows = []
    for (x, y, _), (u, v, _) in zip(src_h, dst_h):
        rows.append([0, 0, 0, -x, -y, -1, v * x, v * y, v])
        rows.append([x, y, 1, 0, 0, 0, -u * x, -u * y, -u])
    _, _, vt = np.linalg.svd(np.asarray(rows))
    return np.linalg.inv(td) @ vt[-1].reshape(3, 3) @ ts


def homography_rms(h: np.ndarray, src: np.ndarray, dst: np.ndarray) -> float:
    projected = (h @ np.c_[src, np.ones(len(src))].T).T
    projected = projected[:, :2] / projected[:, 2:3]
    residuals = projected - dst
    return float(np.sqrt(np.mean(np.sum(residuals**2, axis=1))))


def restricted_pair_fit(
    src: np.ndarray, dst: np.ndarray, centre: np.ndarray
) -> tuple[np.ndarray, float]:
    """One pair on its own: parameters [l1, l2, s, phi, tx, ty]. The
    similarity acts in rectified (centred) space. Returns (l, rms)."""
    rect_src = rectify(src, np.zeros(2), centre)
    rect_dst = rectify(dst, np.zeros(2), centre)
    init_sim, _ = registration_module.similarity_from_correspondences(
        rect_src, rect_dst
    )
    init = np.array(
        [
            0.0,
            0.0,
            np.hypot(*init_sim[:, 0]),
            math.atan2(init_sim[1, 0], init_sim[0, 0]),
            *init_sim[:, 2],
        ]
    )

    def residual(params: np.ndarray) -> np.ndarray:
        l = params[:2]
        s, phi = params[2], params[3]
        rotation = s * np.array(
            [[math.cos(phi), -math.sin(phi)], [math.sin(phi), math.cos(phi)]]
        )
        predicted = rectify(src, l, centre) @ rotation.T + params[4:6]
        return (predicted - rectify(dst, l, centre)).ravel()

    result = least_squares(residual, init, method="lm")
    residuals = result.fun.reshape(-1, 2)
    rms = math.sqrt(float(np.mean(np.sum(residuals**2, axis=1))))
    return result.x[:2], rms


def global_tilt_fit(
    pairs: list[tuple[np.ndarray, np.ndarray]], centre: np.ndarray
) -> tuple[np.ndarray, float]:
    """All of a negative's pairs at once: [l1, l2] shared, each pair's
    similarity re-fit in closed form (Umeyama) inside the residual. Returns
    (l, rms over every correspondence)."""
    def residual(params: np.ndarray) -> np.ndarray:
        l = params
        blocks = []
        for src, dst in pairs:
            src_rect = rectify(src, l, centre)
            dst_rect = rectify(dst, l, centre)
            sim, _ = registration_module.similarity_from_correspondences(
                src_rect, dst_rect
            )
            rotation, translation = sim[:, :2], sim[:, 2]
            blocks.append(
                (src_rect @ rotation.T + translation - dst_rect).ravel()
            )
        return np.concatenate(blocks)

    result = least_squares(residual, np.zeros(2), method="lm")
    residuals = result.fun.reshape(-1, 2)
    rms = math.sqrt(float(np.mean(np.sum(residuals**2, axis=1))))
    return result.x, rms


def restricted_homography(
    l: np.ndarray, sim: np.ndarray, centre: np.ndarray
) -> np.ndarray:
    """The restricted model H = W^-1 . S . W as one 3x3 mapping raw image
    px to raw image px (b -> a), for corner-divergence measurements."""
    inv_w = np.array([[1, 0, 0], [0, 1, 0], [-l[0], -l[1], 1.0]])
    w = np.array([[1, 0, 0], [0, 1, 0], [l[0], l[1], 1.0]])
    c = np.array([*centre, 1.0])
    to_centred = np.eye(3)
    to_centred[:2, 2] = -centre
    from_centred = np.eye(3)
    from_centred[:2, 2] = centre
    s = np.eye(3)
    s[:2, :2] = sim[:, :2]
    s[:2, 2] = sim[:, 2] + sim[:, :2] @ centre - centre
    return from_centred @ np.linalg.inv(inv_w) @ s @ w @ to_centred


def tilt_degrees(l: np.ndarray, focal_px: float | None) -> tuple[str, str]:
    if focal_px is None:
        return "—", "—"
    alpha_x = math.degrees(math.atan(l[0] * focal_px))
    alpha_y = math.degrees(math.atan(l[1] * focal_px))
    return f"{alpha_x:+.3f}", f"{alpha_y:+.3f}"


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------


@dataclasses.dataclass
class PairFit:
    negative: str
    a: str
    b: str
    accepted: bool
    inliers: int
    rigid_rms: float
    similarity_rms: float
    homography_rms: float | None
    restricted_rms: float | None
    l: np.ndarray | None
    scale_drift: float
    corner_divergence_px: float | None


@dataclasses.dataclass
class NegativeFit:
    negative: str
    n_frames: int
    solved: bool
    global_rms_px: float | None
    shared_l: np.ndarray | None
    shared_rms: float | None
    pairs: list[PairFit]


def measure_negative(
    negative: str, paths: list[Path], focal_px: float | None
) -> NegativeFit:
    frame_size = None
    features = {}
    for path in paths:
        pixels = decode_raw(path).pixels
        frame_size = (pixels.shape[0], pixels.shape[1])
        detection = build_detection_image(
            pixels, long_edge=DETECTION_LONG_EDGE, clahe=USE_CLAHE
        )
        del pixels
        features[path.name] = registration_module.detect_features(
            detection, name=path.name
        )

    names = [path.name for path in paths]
    pair_results = []
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            pair_results.append(
                registration_module.register_pair(features[a], features[b])
            )

    solved_layout = None
    try:
        solved_layout = layout_module.solve_layout(
            names, frame_size, pair_results
        )
    except registration_module.StitchError:
        pass

    centre = np.array([frame_size[1] / 2.0, frame_size[0] / 2.0])
    fit_pairs: list[tuple[np.ndarray, np.ndarray]] = []
    pair_fits = []
    for pair in pair_results:
        if not pair.accepted or pair.inliers < MIN_FIT_INLIERS:
            pair_fits.append(
                PairFit(
                    negative=negative,
                    a=pair.a,
                    b=pair.b,
                    accepted=pair.accepted,
                    inliers=pair.inliers,
                    rigid_rms=pair.rms_residual_px
                    if math.isfinite(pair.rms_residual_px)
                    else float("nan"),
                    similarity_rms=float("nan"),
                    homography_rms=None,
                    restricted_rms=None,
                    l=None,
                    scale_drift=pair.scale_drift
                    if math.isfinite(pair.scale_drift)
                    else float("nan"),
                    corner_divergence_px=None,
                )
            )
            continue

        src, dst = pair.inlier_points_b, pair.inlier_points_a
        similarity_rms = registration_module._rms_residual(
            pair.similarity_transform, src, dst
        )
        homography = fit_homography(src, dst)
        homography_rms_value = homography_rms(homography, src, dst)
        l_pair, restricted_rms = restricted_pair_fit(src, dst, centre)

        # The restricted model's own mapping b -> a, evaluated at the frame
        # corners against the production similarity fit: the systematic
        # error per pair the current pipeline absorbs as noise.
        rect_sim, _ = registration_module.similarity_from_correspondences(
            rectify(src, l_pair, centre), rectify(dst, l_pair, centre)
        )
        h_restricted = restricted_homography(l_pair, rect_sim, centre)
        corners = np.array(
            [
                [0, 0],
                [frame_size[1], 0],
                [frame_size[1], frame_size[0]],
                [0, frame_size[0]],
            ],
            dtype=np.float64,
        )
        corners_rect = (h_restricted @ np.c_[corners, np.ones(4)].T).T
        corners_rect = corners_rect[:, :2] / corners_rect[:, 2:3]
        corners_sim = corners @ pair.similarity_transform[:, :2].T + pair.similarity_transform[:, 2]
        divergence = float(
            np.max(np.linalg.norm(corners_rect - corners_sim, axis=1))
        )

        fit_pairs.append((src, dst))
        pair_fits.append(
            PairFit(
                negative=negative,
                a=pair.a,
                b=pair.b,
                accepted=True,
                inliers=pair.inliers,
                rigid_rms=pair.rms_residual_px,
                similarity_rms=similarity_rms,
                homography_rms=homography_rms_value,
                restricted_rms=restricted_rms,
                l=l_pair,
                scale_drift=pair.scale_drift,
                corner_divergence_px=divergence,
            )
        )

    shared_l, shared_rms = None, None
    if fit_pairs:
        shared_l, shared_rms = global_tilt_fit(fit_pairs, centre)

    return NegativeFit(
        negative=negative,
        n_frames=len(paths),
        solved=solved_layout is not None,
        global_rms_px=(
            solved_layout.global_rms_px if solved_layout is not None else None
        ),
        shared_l=shared_l,
        shared_rms=shared_rms,
        pairs=pair_fits,
    )


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


def table(headers: list[str], rows: list[list[str]]) -> None:
    print("| " + " | ".join(headers) + " |")
    print("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        print("| " + " | ".join(row) + " |")
    print()


def num(value: float | None, places: int = 3) -> str:
    if value is None or not math.isfinite(value):
        return "—"
    return f"{value:.{places}f}"


def pair_label(a: str, b: str) -> str:
    return f"{a.rsplit('_', 1)[1]}–{b.rsplit('_', 1)[1]}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nef-dir", type=Path, required=True, dest="nef_dir")
    parser.add_argument(
        "--group",
        choices=("gap", "chunks", "explicit"),
        default="gap",
        help=(
            "gap: split the catalogue wherever the capture-time gap exceeds "
            "--gap-seconds. chunks: contiguous runs of "
            "--shots-per-negative. explicit: one --negative per group."
        ),
    )
    parser.add_argument("--gap-seconds", type=float, default=30.0)
    parser.add_argument("--shots-per-negative", type=int, default=3)
    parser.add_argument(
        "--negative",
        action="append",
        default=[],
        help=(
            "explicit negative as a comma-separated list of filenames; "
            "repeat for each negative"
        ),
    )
    parser.add_argument(
        "--focal-px",
        type=float,
        default=None,
        help="Focal length in full-resolution px; derived from EXIF when omitted.",
    )
    parser.add_argument(
        "--sensor-width-mm",
        type=float,
        default=DEFAULT_SENSOR_WIDTH_MM,
        help="Sensor width in mm for the EXIF-derived focal length (default 35.9).",
    )
    args = parser.parse_args()

    paths = sorted(
        p for p in args.nef_dir.iterdir() if p.suffix.lower() == ".nef"
    )
    if not paths:
        print(f"No NEFs in {args.nef_dir}", file=sys.stderr)
        return 1

    if args.group == "explicit":
        groups = []
        for spec in args.negative:
            names = [n.strip() for n in spec.split(",")]
            by_name = {p.name: p for p in paths}
            missing = [n for n in names if n not in by_name]
            if missing:
                print(f"Unknown filenames: {missing}", file=sys.stderr)
                return 1
            groups.append([by_name[n] for n in names])
    else:
        times = capture_times(paths)
        ordered = [p.name for p in paths]
        if args.group == "gap":
            name_groups = group_by_gap(ordered, times, args.gap_seconds)
        else:
            name_groups = group_by_chunks(ordered, args.shots_per_negative)
        by_name = {p.name: p for p in paths}
        groups = [[by_name[n] for n in g] for g in name_groups]

    print("# Tilt vs film-flatness measurements\n")
    stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    print(f"Generated {stamp} by `scripts/measure-tilt.py` from `{args.nef_dir}`.\n")
    print(
        f"Grouping ({args.group}): "
        + ", ".join(
            f"{g[0].stem}…{g[-1].stem} ({len(g)})" for g in groups
        )
        + "\n"
    )

    first = decode_raw(groups[0][0])
    focal_px = args.focal_px
    if focal_px is None:
        settings = read_exif_settings(groups[0][0])
        if settings.focal_length is not None:
            focal_mm = float(settings.focal_length)
            pitch_mm = args.sensor_width_mm / first.pixels.shape[1]
            focal_px = focal_mm * first.pixels.shape[1] / args.sensor_width_mm
            print(
                f"Frame {first.pixels.shape[1]}x{first.pixels.shape[0]} px; "
                f"EXIF focal {focal_mm:g} mm -> f ≈ {focal_px:.0f} px "
                f"(assumes a {args.sensor_width_mm:g} mm-wide sensor). "
                "Degree columns use this; the RMS columns do not need it.\n"
            )
    del first

    negative_fits = [
        measure_negative(
            f"neg-{i + 1:02d}", group_paths, focal_px
        )
        for i, group_paths in enumerate(groups)
    ]

    print("## Per pair\n")
    rows = []
    for nf in negative_fits:
        for pf in nf.pairs:
            rows.append(
                [
                    nf.negative,
                    pair_label(pf.a, pf.b),
                    "yes" if pf.accepted else "no",
                    str(pf.inliers),
                    num(pf.rigid_rms),
                    num(pf.similarity_rms),
                    num(pf.homography_rms),
                    num(pf.restricted_rms),
                    num(pf.corner_divergence_px, 1),
                ]
            )
    table(
        [
            "negative", "pair", "accepted", "inliers", "rigid_rms_px",
            "similarity_rms_px", "homography_rms_px", "restricted_rms_px",
            "sim_vs_restricted_corner_divergence_px",
        ],
        rows,
    )

    print("## Per negative\n")
    rows = []
    for nf in negative_fits:
        tilt_x, tilt_y = ("—", "—")
        if nf.shared_l is not None:
            tilt_x, tilt_y = tilt_degrees(nf.shared_l, focal_px)
        gaps = [
            pf.restricted_rms - pf.homography_rms
            for pf in nf.pairs
            if pf.homography_rms is not None
        ]
        rows.append(
            [
                nf.negative,
                str(nf.n_frames),
                "yes" if nf.solved else "no",
                num(nf.global_rms_px),
                num(nf.shared_rms),
                tilt_x,
                tilt_y,
                num(statistics.mean(gaps), 2) if gaps else "—",
            ]
        )
    table(
        [
            "negative", "frames", "layout_solved", "production_global_rms_px",
            "shared-l_model_rms_px", "tilt_x_deg", "tilt_y_deg",
            "mean_homography_minus_shared-l_px",
        ],
        rows,
    )

    print(
        "tilt_x_deg is the depth ramp along the frame's x axis (rotation "
        "about the vertical axis); tilt_y_deg along y. With the strip "
        "running horizontally, tilt_y is the across-the-strip tilt that "
        "curves a strip while leaving scale untouched, and tilt_x is the "
        "along-strip tilt that shows up as per-frame magnification drift.\n"
    )

    # A tilt is only identifiable where the similarity fit is measurably
    # worse than the noise floor: on a 0.2 px pair every model ties, l is
    # free to wander (its corner extrapolation then diverges wildly), and
    # including such pairs only dilutes the aggregates.
    structured = [
        pf
        for nf in negative_fits
        for pf in nf.pairs
        if pf.l is not None and pf.similarity_rms >= STRUCTURED_RMS_FLOOR_PX
    ]
    shared_ls = [
        nf.shared_l for nf in negative_fits if nf.shared_l is not None
    ]
    if structured and focal_px is not None:
        print("## Verdict inputs\n")
        print(
            f"Aggregated over the {len(structured)} pairs whose similarity "
            f"RMS reaches at least {STRUCTURED_RMS_FLOOR_PX} px — the pairs "
            "where a tilt is actually identifiable. Pairs already at the "
            "keypoint noise floor tie under every model and say nothing "
            "about l.\n"
        )
        degrees = np.array(
            [
                [math.degrees(math.atan(pf.l[0] * focal_px)),
                 math.degrees(math.atan(pf.l[1] * focal_px))]
                for pf in structured
            ]
        )
        table(
            ["quantity", "tilt_x_deg", "tilt_y_deg"],
            [
                [
                    "median of per-pair estimates",
                    f"{np.median(degrees[:, 0]):+.3f}",
                    f"{np.median(degrees[:, 1]):+.3f}",
                ],
                [
                    "per-pair spread (max-min)",
                    f"{np.ptp(degrees[:, 0]):.3f}",
                    f"{np.ptp(degrees[:, 1]):.3f}",
                ],
                [
                    "median of per-negative shared-l estimates",
                    f"{np.median([math.degrees(math.atan(l[0] * focal_px)) for l in shared_ls]):+.3f}"
                    if shared_ls else "—",
                    f"{np.median([math.degrees(math.atan(l[1] * focal_px)) for l in shared_ls]):+.3f}"
                    if shared_ls else "—",
                ],
                [
                    "per-negative spread of shared-l (max-min)",
                    f"{np.ptp([math.degrees(math.atan(l[0] * focal_px)) for l in shared_ls]):.3f}"
                    if shared_ls else "—",
                    f"{np.ptp([math.degrees(math.atan(l[1] * focal_px)) for l in shared_ls]):.3f}"
                    if shared_ls else "—",
                ],
            ],
        )
        improvements = [pf.similarity_rms - pf.homography_rms for pf in structured]
        restricted_vs_homography = [
            pf.restricted_rms - pf.homography_rms for pf in structured
        ]
        if improvements:
            print(
                f"Median similarity→homography RMS improvement: "
                f"{statistics.median(improvements):.3f} px. "
                f"Median homography→shared-restricted gap: "
                f"{statistics.median(restricted_vs_homography):.3f} px "
                "(what one global tilt per negative fails to explain).\n"
            )
        print(
            "Reading: a rigid tilt shows homography_rms ≈ restricted_rms "
            "≪ similarity_rms with a small per-pair *and* per-negative "
            "spread of l (the same two numbers on every negative, stable "
            "across sessions). Film curl shows homography_rms ≪ "
            "similarity_rms but l scattered pair to pair. No projective "
            "component shows homography_rms ≈ similarity_rms.\n"
        )
        print(
            "Caveat on the corner-divergence column: it extrapolates two "
            "models that agree inside the overlap band out to the frame "
            "corners, so it is only meaningful where the similarity fit is "
            "measurably worse than the restricted fit — on noise-floor "
            "pairs it amplifies an arbitrary l into a huge number."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
