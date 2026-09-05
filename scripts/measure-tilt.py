#!/usr/bin/env python3
"""Measure whether the camera and the film are parallel, from the scans.

The stitch places every frame with a **similarity** (rotation, translation,
one isotropic scale — `layout.py`). That model is exact only when the film
plane is parallel to the sensor. Tilt the camera by a fraction of a degree
and the true frame-to-frame mapping becomes a homography; fitting a
similarity to it leaves a residual too small to trip `MAX_PAIR_RMS_PX` but
which accumulates along the strip as a bow in the film's straight edges.

A tilt is only **two** numbers for the whole rig, whatever the frame count:
the film plane's vanishing line `l = (l1, l2)`. Rectifying every frame by

    W = [[1, 0, 0], [0, 1, 0], [l1, l2, 1]]      (centred, normalised px)

makes a stage translation an exact similarity again — the model `layout.py`
already solves. So this script fits **one global `l` shared by every pair**,
by variable projection: for a candidate `l` each pair's similarity is solved
in closed form with `registration.similarity_from_correspondences`, and only
the two global numbers are searched.

It fits three more models alongside it, because "the residual got smaller"
is not evidence for tilt on its own:

| model | global params | what it would mean |
| --- | --- | --- |
| `baseline` | — | what production does today |
| `radial` | k1, k2 | residual *lens distortion*, not tilt |
| `tilt` | l1, l2 | camera/film not parallel |
| `radial+tilt` | k1, k2, l1, l2 | both, fitted jointly so neither steals the other's signal |
| `aniso` | a, b | a *constant* aspect/shear, the same for every pair |

`aniso` is there to separate two things that both look like "the similarity
is too rigid". A tilt makes each pair anisotropic **in proportion to that
pair's step along the strip** (to first order in the tilt angle, the
inter-frame map is an anisotropic scale of that size); a constant global
affine makes every pair anisotropic by the *same* amount. Only the first is
a tilt.

It reports two per-pair references, both unconstrained and fitted to one
pair alone, which no 2-parameter global model can beat:

- **affine** (6 dof) — a tilt's first-order signature is exactly the shear
  and aspect a similarity cannot represent, so this is the ceiling a tilt
  correction could reach;
- **homography** (8 dof) — anything affine misses. Homography much below
  affine means a genuinely projective residual; the two roughly equal means
  the residual is first-order, which is what a tilt looks like.

**The discriminator is consistency, not fit.** A rigid tilt is one number
for the whole session, so the `l` fitted to each negative separately must
agree with the pooled `l`. A scatter of per-negative values means the film
is not flat, and no homography can fix that. The jackknife column is what
says whether an `l` is distinguishable from zero at all.

**This script only measures. It changes nothing** — it writes nothing, and
reads no constant it does not print.

Usage, from the repository root:

    uv run --project cli scripts/measure-tilt.py
    uv run --project cli scripts/measure-tilt.py --nef-dir ~/Pictures/scans/R1
    uv run --project cli scripts/measure-tilt.py --profile <profile-id>
    uv run --project cli scripts/measure-tilt.py --magnification 2.0
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path

import cv2
import numpy as np
from scipy.optimize import least_squares

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cli" / "src"))

# Imported after the path insert, exactly as measure-stitch-quality.py does
# it. These are the production modules — the similarity fit, the undistorter,
# the acceptance gates and the layout solve are all theirs, not copies.
from scanny_boy import layout as layout_module
from scanny_boy import registration as registration_module
from scanny_boy.detection import (
    DETECTION_LONG_EDGE,
    USE_CLAHE,
    build_detection_image,
)
from scanny_boy.geometry_fit import base_camera
from scanny_boy.library import repo
from scanny_boy.raw_decode import decode_raw
from scanny_boy.registration import (
    PairResult,
    register_pair,
    similarity_from_correspondences,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_NEF_DIR = ROOT / "tests" / "fixtures" / "nef"

# Sensor and lens of the rig these scans come from, used *only* to turn a
# fitted vanishing line into degrees for the report. Nothing in the fit
# needs them: `l` lives in image coordinates, which is why this measurement
# works without ever knowing the true focal length (`base_camera`'s `fx` is
# a gauge — max(w, h) — not a focal length).
SENSOR_WIDTH_MM = 35.9
LENS_FOCAL_MM = 55.0


# --------------------------------------------------------------------------
# The global model
# --------------------------------------------------------------------------


def _normalise(points: np.ndarray, K: np.ndarray) -> np.ndarray:
    return np.column_stack([
        (points[:, 0] - K[0, 2]) / K[0, 0],
        (points[:, 1] - K[1, 2]) / K[1, 1],
    ])


def _denormalise(points: np.ndarray, K: np.ndarray) -> np.ndarray:
    return np.column_stack([
        points[:, 0] * K[0, 0] + K[0, 2],
        points[:, 1] * K[1, 1] + K[1, 2],
    ])


def apply_model(points: np.ndarray, theta: np.ndarray, K: np.ndarray) -> np.ndarray:
    """Map observed full-resolution pixels to the model's rectified pixels.

    `theta` is `(k1, k2, l1, l2, a, b)`, where `(a, b)` is a constant
    aspect/shear correction — `[[1 + a, b], [0, 1 - a]]`, trace-free so it
    cannot drift into the isotropic scale each pair's similarity already
    owns. Radial first, then the rectification —
    the same order section 5.3 composes distortion and everything after it,
    and the only order that is self-consistent: the vanishing line is a
    property of the *undistorted* image.

    Returns pixels, not normalised coordinates, so every residual this
    script prints is directly comparable to `MAX_PAIR_RMS_PX`.
    """
    k1, k2, l1, l2, a, b = theta
    if k1 or k2:
        # cv2.undistortPoints inverts the OpenCV forward model iteratively —
        # the same call, in the same convention, that
        # registration.undistorter_from_geometry makes in production.
        D = np.array([k1, k2, 0.0, 0.0, 0.0])
        points = cv2.undistortPoints(
            points.reshape(-1, 1, 2).astype(np.float32), K, D, P=K
        ).reshape(-1, 2).astype(np.float64)
    if not (l1 or l2 or a or b):
        return points
    normalised = _normalise(points, K)
    if l1 or l2:
        w = 1.0 + l1 * normalised[:, 0] + l2 * normalised[:, 1]
        normalised = normalised / w[:, None]
    if a or b:
        normalised = normalised @ np.array([[1.0 + a, 0.0], [b, 1.0 - a]])
    return _denormalise(normalised, K)


def pair_residual(
    pts_a: np.ndarray, pts_b: np.ndarray, theta: np.ndarray, K: np.ndarray
) -> np.ndarray:
    """One pair's residual under the global model, with its similarity
    profiled out in closed form (variable projection): whatever `theta` is,
    the best similarity for it is not searched for, it is solved."""
    a = apply_model(pts_a, theta, K)
    b = apply_model(pts_b, theta, K)
    transform, _ = similarity_from_correspondences(b, a)
    projected = b @ transform[:, :2].T + transform[:, 2]
    return (projected - a).ravel()


def rms_of(residual: np.ndarray) -> float:
    """RMS point distance, in pixels: the residual is interleaved (dx, dy)."""
    return float(np.sqrt(np.mean(np.sum(residual.reshape(-1, 2) ** 2, axis=1))))


def fit_model(
    pair_points: list[tuple[np.ndarray, np.ndarray]],
    K: np.ndarray,
    *,
    radial: bool,
    tilt: bool,
    aniso: bool = False,
) -> tuple[np.ndarray, float]:
    """Least-squares over the enabled global parameters only. Returns
    `(theta, pooled_rms_px)`."""
    free = np.array([radial, radial, tilt, tilt, aniso, aniso])
    if not free.any():
        theta = np.zeros(6)
        return theta, rms_of(_pooled(pair_points, theta, K))

    def unpack(x: np.ndarray) -> np.ndarray:
        theta = np.zeros(6)
        theta[free] = x
        return theta

    result = least_squares(
        lambda x: _pooled(pair_points, unpack(x), K),
        np.zeros(int(free.sum())),
        method="lm",
        xtol=1e-12,
    )
    theta = unpack(result.x)
    return theta, rms_of(result.fun)


def _pooled(
    pair_points: list[tuple[np.ndarray, np.ndarray]], theta: np.ndarray, K: np.ndarray
) -> np.ndarray:
    return np.concatenate([pair_residual(a, b, theta, K) for a, b in pair_points])


def affine_rms(pts_a: np.ndarray, pts_b: np.ndarray) -> float:
    """The 6-dof per-pair reference. A tilt's first-order signature is the
    shear and aspect that a similarity has no parameters for, so this is the
    ceiling any tilt correction could reach on this pair."""
    design = np.column_stack([pts_b, np.ones(len(pts_b))])
    solution, *_ = np.linalg.lstsq(design, pts_a, rcond=None)
    return float(np.sqrt(np.mean(np.sum((design @ solution - pts_a) ** 2, axis=1))))


def homography_rms(pts_a: np.ndarray, pts_b: np.ndarray) -> float:
    """The unconstrained per-pair reference: 8 free parameters fitted to
    this pair alone. Nothing a 2-parameter global model does can beat it."""
    matrix, _ = cv2.findHomography(pts_b, pts_a, method=0)
    if matrix is None:
        return float("nan")
    projected = cv2.perspectiveTransform(
        pts_b.reshape(-1, 1, 2), matrix
    ).reshape(-1, 2)
    return float(np.sqrt(np.mean(np.sum((projected - pts_a) ** 2, axis=1))))


def tilt_degrees(l: float, magnification: float, frame_width: int, norm: float) -> float:
    """A vanishing-line component in degrees, for the report only.

    `l` is measured in units of the `base_camera` gauge, so converting needs
    the real camera constant — the rear node to sensor distance `f(1+m)`, in
    pixels: `tan(theta) = l * camera_constant / norm`. Wrong
    `--magnification` scales this number and nothing else; the fit and the
    correction never see it, which is the whole reason this measurement
    works on a rig whose true focal length nobody has calibrated.
    """
    pitch_mm = SENSOR_WIDTH_MM / frame_width
    camera_constant_px = LENS_FOCAL_MM * (1.0 + magnification) / pitch_mm
    return float(np.degrees(np.arctan(l * camera_constant_px / norm)))


# --------------------------------------------------------------------------
# Gathering pairs from real scans
# --------------------------------------------------------------------------


@dataclasses.dataclass
class Negative:
    """A run of consecutive frames that actually register to each other —
    derived from the data, not from a filename convention: the chain is cut
    wherever `register_pair` refuses the consecutive link, which is exactly
    a negative boundary."""

    frames: list[str]
    pairs: list[PairResult]


def detect_all(paths: list[Path]) -> tuple[dict, tuple[int, int]]:
    """Returns the features and the frame size (height, width). Only one
    frame's pixels are resident at a time — the fit needs keypoints, not
    images, so there is no reason to hold 21 decoded NEFs in memory."""
    features = {}
    frame_size = None
    for i, path in enumerate(paths, start=1):
        print(f"  [{i}/{len(paths)}] {path.name}", file=sys.stderr)
        decoded = decode_raw(path)
        if frame_size is None:
            frame_size = (decoded.pixels.shape[0], decoded.pixels.shape[1])
        detection = build_detection_image(
            decoded.pixels, long_edge=DETECTION_LONG_EDGE, clahe=USE_CLAHE
        )
        features[path.stem] = registration_module.detect_features(
            detection, name=path.stem
        )
        del decoded
    return features, frame_size


def group_into_negatives(names: list[str], features: dict, undistorter) -> list[Negative]:
    links = [
        register_pair(features[names[i]], features[names[i + 1]], undistorter)
        for i in range(len(names) - 1)
    ]
    negatives: list[Negative] = []
    current = [names[0]]
    current_pairs: list[PairResult] = []
    for i, link in enumerate(links):
        if link.accepted:
            current.append(names[i + 1])
            current_pairs.append(link)
        else:
            negatives.append(Negative(current, current_pairs))
            current, current_pairs = [names[i + 1]], []
    negatives.append(Negative(current, current_pairs))

    # Non-consecutive pairs inside a negative carry the accumulated error of
    # two hops in one measurement, so they are the most informative rows in
    # the fit — and the loop closure the report checks.
    for negative in negatives:
        for gap in range(2, len(negative.frames)):
            for i in range(len(negative.frames) - gap):
                extra = register_pair(
                    features[negative.frames[i]],
                    features[negative.frames[i + gap]],
                    undistorter,
                )
                if extra.accepted:
                    negative.pairs.append(extra)
    return [n for n in negatives if n.pairs]


def refit_pair(pair: PairResult, theta: np.ndarray, K: np.ndarray) -> PairResult:
    """The same pair with its correspondences rectified and every fitted
    field re-derived, so `layout.solve_layout` and `layout.global_rms` can
    be run on the corrected model without either of them changing."""
    a = apply_model(pair.inlier_points_a, theta, K)
    b = apply_model(pair.inlier_points_b, theta, K)
    rigid = registration_module.rigid_from_correspondences(b, a)
    similarity, scale = similarity_from_correspondences(b, a)
    projected = b @ rigid[:, :2].T + rigid[:, 2]
    rms = float(np.sqrt(np.mean(np.sum((projected - a) ** 2, axis=1))))
    return dataclasses.replace(
        pair,
        transform=rigid,
        rms_residual_px=rms,
        similarity_transform=similarity,
        similarity_scale=scale,
        scale_drift=abs(scale - 1.0),
        inlier_points_a=a,
        inlier_points_b=b,
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


def num(value, places: int = 3) -> str:
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return "—"
    return f"{value:.{places}f}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nef-dir", type=Path, default=DEFAULT_NEF_DIR)
    parser.add_argument("--profile", default=None, help="flat-field profile id")
    parser.add_argument("--magnification", type=float, default=2.0)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    paths = sorted(args.nef_dir.glob("*.NEF")) or sorted(args.nef_dir.glob("*.nef"))
    if args.limit:
        paths = paths[: args.limit]
    if len(paths) < 2:
        print(f"need at least two NEFs in {args.nef_dir}", file=sys.stderr)
        return 1

    undistorter = None
    geometry = None
    if args.profile:
        profile = repo.load_flatfield_profile(args.profile)
        geometry = profile.geometry
        if geometry is not None:
            undistorter = registration_module.undistorter_from_geometry(geometry)

    print(f"# Tilt measurement\n")
    print(f"- frames: {len(paths)} from `{args.nef_dir}`")
    print(f"- detection: long edge {DETECTION_LONG_EDGE}, CLAHE {USE_CLAHE}")
    print(
        "- profile geometry: "
        + (
            f"`{args.profile}` (k1={geometry['k1']:.5f}, k2={geometry['k2']:.5f}) — "
            "correspondences are undistorted before this fit, so a fitted "
            "`radial` here is *residual* distortion"
            if geometry
            else "none — radial distortion is uncorrected in these scans, so "
            "it is fitted here alongside the tilt rather than assumed absent"
        )
    )
    print()

    print("Decoding and detecting…", file=sys.stderr)
    features, frame_size = detect_all(paths)
    names = [p.stem for p in paths]
    frame_height, frame_width = frame_size

    print("Registering…", file=sys.stderr)
    negatives = group_into_negatives(names, features, undistorter)
    all_pairs = [p for n in negatives for p in n.pairs]
    if len(all_pairs) < 3:
        print(f"only {len(all_pairs)} accepted pairs — too few to fit", file=sys.stderr)
        return 1

    # The same gauge camera the plumb-line fit uses (fx = fy = max(w, h),
    # principal point centred), so a fitted k1/k2 here is directly
    # comparable to a profile's and `l` is in the same normalisation.
    K = base_camera(frame_width, frame_height)
    norm = float(K[0, 0])

    pair_points = [(p.inlier_points_a, p.inlier_points_b) for p in all_pairs]

    print("Fitting…", file=sys.stderr)
    models = {}
    for label, (radial, tilt) in {
        "baseline": (False, False),
        "radial": (True, False),
        "tilt": (False, True),
        "radial+tilt": (True, True),
    }.items():
        models[label] = fit_model(pair_points, K, radial=radial, tilt=tilt)

    hom = float(np.sqrt(np.mean(np.concatenate([
        np.full(len(a), homography_rms(a, b) ** 2) for a, b in pair_points
    ]))))

    print("## Which model explains the residual\n")
    rows = []
    for label, (theta, rms) in models.items():
        base = models["baseline"][1]
        rows.append([
            f"`{label}`",
            num(rms, 3),
            f"{100 * (1 - rms / base):.0f}%" if label != "baseline" else "—",
            num(theta[0], 6) if theta[0] else "—",
            num(theta[1], 6) if theta[1] else "—",
            num(theta[2], 6) if theta[2] else "—",
            num(theta[3], 6) if theta[3] else "—",
        ])
    rows.append([
        "per-pair homography *(reference)*", num(hom, 3),
        f"{100 * (1 - hom / models['baseline'][1]):.0f}%", "—", "—", "—", "—",
    ])
    table(
        ["model", "pooled RMS px", "vs baseline", "k1", "k2", "l1", "l2"], rows
    )

    best_label = "radial+tilt" if geometry is None else "tilt"
    theta_best = models[best_label][0]

    # Per-negative consistency: a rigid tilt is one number for the session.
    print("## Is the tilt the same everywhere?\n")
    print(
        "A rigid camera/film tilt is a property of the rig, so every "
        "negative must agree. Scatter here means the film is not flat, and "
        "no global homography can fix that.\n"
    )
    rows = []
    per_negative_l = []
    for negative in negatives:
        points = [(p.inlier_points_a, p.inlier_points_b) for p in negative.pairs]
        if len(points) < 2:
            rows.append([
                f"{negative.frames[0]}–{negative.frames[-1]}",
                str(len(negative.frames)), str(len(points)),
                "—", "—", "(too few pairs)",
            ])
            continue
        theta_n, rms_n = fit_model(
            points, K, radial=geometry is None, tilt=True
        )
        per_negative_l.append(theta_n[2:])
        rows.append([
            f"{negative.frames[0]}–{negative.frames[-1]}",
            str(len(negative.frames)), str(len(points)),
            num(theta_n[2], 6), num(theta_n[3], 6), num(rms_n, 3),
        ])
    table(
        ["negative", "frames", "pairs", "l1", "l2", "RMS px"], rows
    )

    if per_negative_l:
        l_array = np.array(per_negative_l)
        spread = l_array.std(axis=0)
        deg_x = tilt_degrees(theta_best[2], args.magnification, frame_width, norm)
        deg_y = tilt_degrees(theta_best[3], args.magnification, frame_width, norm)
        print(
            f"- pooled `l` = ({theta_best[2]:+.6f}, {theta_best[3]:+.6f})\n"
            f"- per-negative scatter (sd) = ({spread[0]:.6f}, {spread[1]:.6f}), "
            f"over {len(per_negative_l)} negatives\n"
            f"- implied tilt at m={args.magnification}: **{deg_x:+.2f}°** about "
            f"the image-y axis (depth varying along image x), **{deg_y:+.2f}°** "
            f"about the image-x axis (depth varying along image y)\n"
        )
        print(
            "Which of those two bows the strip depends on how the strip lies "
            "in the frame: the component whose depth gradient runs **across** "
            "the strip is the one that curves it, and it produces almost no "
            "scale drift, so nothing in the current gates can see it. The "
            "component running **along** the strip is largely absorbed by the "
            "per-frame scale and shows up as `STITCH_SCALE_DRIFT` instead. "
            "The strip axis of each negative is in the table below.\n"
        )

    # The production metric, before and after.
    print("## `global_rms_px`, before and after\n")
    print(
        f"The layout solve and `layout.global_rms` are the production ones, "
        f"run unchanged on correspondences rectified by the pooled "
        f"`{best_label}` model.\n"
    )
    rows = []
    for negative in negatives:
        if len(negative.frames) < 2:
            continue
        try:
            before = layout_module.solve_layout(
                negative.frames, frame_size, negative.pairs
            )
            corrected = [refit_pair(p, theta_best, K) for p in negative.pairs]
            after = layout_module.solve_layout(
                negative.frames, frame_size, corrected
            )
        except registration_module.StitchError as error:
            rows.append([
                f"{negative.frames[0]}–{negative.frames[-1]}",
                "—", "—", f"({error.code.value})", "—",
            ])
            continue
        rows.append([
            f"{negative.frames[0]}–{negative.frames[-1]}",
            num(before.global_rms_px, 3), num(after.global_rms_px, 3),
            f"{100 * (1 - after.global_rms_px / before.global_rms_px):.0f}%"
            if before.global_rms_px else "—",
            f"({before.strip_axis[0]:+.2f}, {before.strip_axis[1]:+.2f})"
            if before.strip_axis else "—",
        ])
    table(
        ["negative", "global_rms_px now", "corrected", "reduction", "strip axis"],
        rows,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
