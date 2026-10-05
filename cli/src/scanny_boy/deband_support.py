"""Synthetic development-band injection for `deband_test.py`.

Bands are injected as blue-only chroma deviations in log10 units (§1: up to
~0.03 log10 in raw transmission), on a copy of `synthetic_scene` (read-only;
copied here before any modification) encoded through
`normalization.encode_normalized`. Two shapes are provided, matching the
measured profiles in DEBAND_PLAN.md §1:

- a **sharp step** with exponential decay (`sharp_amplitude`,
  `sharp_decay_px` — measured ~100-200 px step, 300-500 px decay);
- a **broad hump** with soft (Gaussian) edges (`broad_amplitude`,
  `broad_width` — measured ~1500-2000 px wide on the real roll; scaled down
  proportionally here for a fast synthetic canvas).

A smooth, neutral, whole-width brightness gradient (`gradient_amplitude`) is
layered in as a legitimate scene feature the fit must not treat as a band.
An optional full-height "pole" (a locally dense object, well outside the
background luminance tolerance) and a thin, full-width neutral "wire"
exercise the protection weight — both are excluded from the background
sample at fit time and additionally suppressed by the apply-time protection
weight, so their own colour should survive close to untouched.
"""

from __future__ import annotations

import cv2
import numpy as np

from scanny_boy import normalization
from scanny_boy.synthetic_scene_support import synthetic_scene

# Representative per-channel log10 spans for a colour negative (arbitrary
# but fixed so injected log10 amplitudes translate to a realistic fraction
# of `val`'s [0, 1] range).
DEFAULT_SPANS = (1.35, 1.25, 1.45)

BG_BLUR_SIGMA = 48.0
BG_LUMA_TOL = 0.03


def make_banded_scene(
    height: int,
    width: int,
    *,
    seed: int = 1,
    spans: tuple[float, float, float] = DEFAULT_SPANS,
    sharp_edge: int | None = None,
    sharp_amplitude: float = 0.02,
    sharp_decay_px: float = 350.0,
    broad_centre: int | None = None,
    broad_width: float = 500.0,
    broad_amplitude: float = 0.015,
    gradient_amplitude: float = 0.04,
    pole_x: int | None = None,
    pole_width: int = 16,
    pole_contrast: float = 1.0,
    wire_y: int | None = None,
    wire_width: int = 2,
    cloud: tuple[int, int, int, int, float] | None = None,
    canopy: tuple[int, int, int] | None = None,
    grain_scale: float = 0.12,
    flat: bool = False,
) -> tuple[np.ndarray, dict]:
    """A `(height, width, 3)` uint16 encoded scene with development bands
    running along the *column* axis (i.e. a vertical-axis band, matching
    `axis="vertical"`): colour varies with column, uniformly down each
    column (aside from grain), as on real film. Returns `(codes, info)`
    with the injected features' positions.

    `grain_scale` shrinks `synthetic_scene`'s own blobs and grain: a real
    banded region is a user-picked *flat* area (sky, pavement — DEBAND_PLAN
    §1), so a full-strength `synthetic_scene` (large random-value circles)
    is not representative and would swamp the protection-weight tests with
    scene texture indistinguishable from a real protected object. 0.12
    keeps the background comfortably inside `BG_LUMA_TOL` almost everywhere
    while leaving real per-pixel texture for the median stats to chew on.
    `flat=True` drops the blobs altogether (fine grain only), for tests of how
    the fit treats one large smooth object (`cloud`, `canopy`) in isolation.
    """
    if flat:
        # Flat sky: fine grain only, no scene blobs.
        rng = np.random.default_rng(seed)
        scene = rng.normal(0.0, 0.1, (height, width)).astype(np.float32)
    else:
        scene = synthetic_scene(height, width, seed=seed).copy()
    mean = float(scene.mean())
    spans_arr = np.asarray(spans, dtype=np.float32)
    # Scene texture and the gradient are neutral in *log10* density (equal
    # density change in every channel), like real flat sky: a val-neutral
    # blob would carry a spurious chroma proportional to the span differences.
    neutral = (float(spans_arr.mean()) / spans_arr)[np.newaxis, np.newaxis, :]
    texture = (grain_scale * (scene - mean))[..., np.newaxis]
    val = (0.5 + texture * neutral).astype(np.float32)

    xs = np.arange(width, dtype=np.float32)
    gradient = gradient_amplitude * (xs / max(width - 1, 1))
    val += gradient[np.newaxis, :, np.newaxis] * neutral

    if sharp_edge is None:
        sharp_edge = width // 4
    if broad_centre is None:
        broad_centre = (3 * width) // 4

    log_band = np.zeros(width, dtype=np.float32)
    if sharp_amplitude:
        past = xs >= sharp_edge
        log_band[past] += sharp_amplitude * np.exp(
            -(xs[past] - sharp_edge) / sharp_decay_px
        )
    if broad_amplitude:
        log_band += broad_amplitude * np.exp(
            -0.5 * ((xs - broad_centre) / (broad_width / 2.355)) ** 2
        )

    # Blue record only, chroma-only by construction: B gets +log_band, R/G
    # share -log_band/2, so the per-channel mean (luminance, in log units)
    # is unaffected — exactly the zero-sum shape §3.1 step 7 fits back out.
    val[..., 2] += log_band / spans_arr[2]
    val[..., 0] -= 0.5 * log_band / spans_arr[0]
    val[..., 1] -= 0.5 * log_band / spans_arr[1]

    if cloud is not None:
        # A large smooth blob with its own chroma and no luminance texture:
        # it passes the background test, so it looks like "flat sky" to the
        # fit. `cloud` is (row0, row1, col0, col1, chroma_log10).
        row0, row1, col0, col1, amount = cloud
        mask = np.zeros((height, width), dtype=np.float32)
        mask[row0:row1, col0:col1] = 1.0
        mask = cv2.GaussianBlur(mask, (0, 0), sigmaX=40)
        shift = amount * mask
        val[..., 2] += shift / spans_arr[2]
        val[..., 0] -= 0.5 * shift / spans_arr[0]
        val[..., 1] -= 0.5 * shift / spans_arr[1]

    if canopy is not None:
        # A dark object hanging from the top edge (a tree canopy): its rows
        # are excluded from the background; the sky below it is not.
        # `canopy` is (row_end, col0, col1).
        row_end, col0, col1 = canopy
        leaves = np.random.default_rng(seed + 1000).normal(
            -0.3, 0.15, (row_end, col1 - col0, 1)
        )  # dark and strongly textured, like leaves: never "flat background"
        val[:row_end, col0:col1, :] += (leaves * neutral).astype(np.float32)

    info: dict = {
        "sharp_edge": sharp_edge,
        "broad_centre": broad_centre,
        "spans": spans,
    }

    if pole_x is not None:
        lo, hi = max(0, pole_x - pole_width // 2), min(width, pole_x + pole_width // 2)
        val[:, lo:hi, 0] -= 0.10 * pole_contrast
        val[:, lo:hi, 1] += 0.05 * pole_contrast
        val[:, lo:hi, 2] += 0.14 * pole_contrast
        info["pole"] = (lo, hi)

    if wire_y is not None:
        lo, hi = max(0, wire_y - wire_width // 2), min(height, wire_y + wire_width // 2)
        val[lo:hi, :, :] += 0.12  # neutral (equal per channel): no chroma of its own
        info["wire"] = (lo, hi)

    val = np.clip(
        val,
        -normalization.NORMALIZED_HEADROOM_LOW,
        1.0 + normalization.NORMALIZED_HEADROOM_HIGH,
    )
    codes = normalization.encode_normalized(val).astype(np.uint16)
    return codes, info


def banding_score(
    codes: np.ndarray,
    spans: tuple[float, float, float],
    *,
    rows: slice | np.ndarray | None = None,
    cols: slice | np.ndarray | None = None,
) -> float:
    """The p99 |deviation from a quadratic| of the B-G column profile,
    background-masked and mildly smoothed — the same metric
    `cli/tools/measure_deband.py` and the prototype use, in x1e-3 log10
    units. Lower is less visible banding. `rows`/`cols` may be a slice or
    an explicit row/column index array (e.g. a held-out parity subset)."""
    spans_arr = np.asarray(spans, dtype=np.float32)
    log_val = normalization.decode_normalized(codes) * spans_arr
    # Background classification runs on the *full, contiguous* array first
    # — the prototype's `bg_mask(lv)` does the same before any row subset is
    # taken — so a non-contiguous `rows` selection (a held-out parity
    # subset) never feeds the sigma=48 blur a false discontinuity stitched
    # together from physically distant rows.
    luminance = log_val.mean(axis=2)
    blurred = cv2.GaussianBlur(
        luminance.astype(np.float32), (0, 0), sigmaX=BG_BLUR_SIGMA
    )
    background = np.abs(luminance - blurred) < BG_LUMA_TOL

    if rows is not None:
        log_val = log_val[rows]
        background = background[rows]
    if cols is not None:
        log_val = log_val[:, cols]
        background = background[:, cols]

    bg_diff = np.where(background, log_val[..., 2] - log_val[..., 1], np.nan)
    with np.errstate(all="ignore"):
        profile = np.nanmedian(bg_diff, axis=0)
    good = background.mean(axis=0) > 0.5
    if good.sum() < 8:
        return 0.0
    t = np.linspace(-1.0, 1.0, len(profile))
    coeffs = np.polyfit(t[good], profile[good], 2)
    residual = profile - np.polyval(coeffs, t)
    residual = np.where(good, residual, np.nan)
    smoothed = cv2.GaussianBlur(
        np.nan_to_num(residual).astype(np.float32).reshape(1, -1), (0, 0), sigmaX=8
    ).ravel()
    smoothed = np.where(good, smoothed, np.nan)
    return float(np.nanpercentile(np.abs(smoothed), 99) * 1000.0)


def make_banded_roll(
    tmp_path,
    *,
    height: int = 700,
    width: int = 1100,
    name: str = "banded",
    film_kind: str = "colour",
    codes: np.ndarray | None = None,
    seed: int = 1,
    negative_id: str = "stitch-negative-01",
):
    """A registered roll with one completed negative whose published TIFF
    is a banded synthetic scene (`make_banded_scene`, or `codes` when
    given), written the way the stitch writes it (deflate + predictor).
    The negative's normalization record carries `DEFAULT_SPANS` as its
    floors/ceils. Returns `(roll_dir, negative_id)`. The stitched
    `work_dir` scene is textured scene content with too little flat
    background to fit a region on, so the `edit deband` tests use this."""
    import tifffile

    from scanny_boy.manifest import SourceRecord
    from scanny_boy.roll_manifest import (
        NegativeRecord,
        RunRecord,
        append_run,
        load_roll_manifest,
        merge_sources,
        write_roll_manifest,
    )
    from scanny_boy.work_dir_support import make_roll_dir

    roll_dir = make_roll_dir(tmp_path, name, film_kind=film_kind)
    if codes is None:
        codes, _info = make_banded_scene(height, width, seed=seed)
    height, width = codes.shape[:2]
    manifest = load_roll_manifest(roll_dir)
    append_run(
        manifest,
        RunRecord(
            run_id="stitch-run",
            short_id="stitch",
            kind="stitch",
            status="complete",
            started_at="2026-08-02T00:00:00Z",
            convert_run_id="convert-1",
            input_folder=None,
            source_order=["a.NEF"],
            work_dir="/tmp/work",
            finished_at="2026-08-02T00:10:00Z",
        ),
    )
    merge_sources(
        manifest,
        [
            SourceRecord(
                filename="a.NEF", absolute_path="/x", size=1, mtime=1.0, sha256="a" * 64
            )
        ],
        "stitch-run",
    )
    manifest.negatives.append(
        NegativeRecord(
            negative_id=negative_id,
            run_id="stitch-run",
            members=["a.NEF"],
            expected_output="_DSC0001.tif",
            fill_color=(0, 0, 0),
            status="completed",
            sequence=1,
            output={
                "name": "_DSC0001.tif",
                "size": 0,
                "sha256": "0" * 64,
                "width": width,
                "height": height,
            },
            normalization={
                "floors": [0.0, 0.0, 0.0],
                "ceils": [float(v) for v in DEFAULT_SPANS],
            },
        )
    )
    write_roll_manifest(roll_dir, manifest)
    tifffile.imwrite(
        roll_dir / "_DSC0001.tif",
        codes,
        photometric="rgb",
        compression="deflate",
        predictor=True,
        maxworkers=1,
        metadata=None,
    )
    return roll_dir, negative_id
