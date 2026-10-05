#!/usr/bin/env python3
"""Calibration tool for the deband fit (DEBAND_PLAN.md §6.3).

Given a roll folder, a negative (by its TIFF's file name or its
`negative_id`), and one or more region windows, this:

- fits and applies the region on the *whole* window (the correction a real
  edit would ship) and reports its correction range, the corr magnitude at
  the window's left/right edges, the encoded table's size, and runtime;
- separately fits on even 32-row bands and scores the held-out odd bands
  (the prototype's `score`/`masked_bg`, DEBAND_PLAN.md §1) to check the fit
  generalises rather than overfitting grain;
- writes a before/after/10x-chroma contact sheet PNG per region.

Not loaded at runtime — calibration only. Run one negative per invocation;
the published TIFFs are large (~250 MB) and the machine here is loaded.

The roll manifest comes from the library database. Opening that database
runs migrations and WAL pragmas (writes), so for a read-only calibration
copy `library.db*` somewhere and point `SCANNY_BOY_LIBRARY_DB` at the copy.

    uv run --project cli python cli/tools/measure_deband.py \\
        --roll "/path/to/roll" --tiff 20260920-165541_01.tif \\
        --region sky:150,5000,7970,1092 --region pavement:150,184,7970,1876 \\
        --axis vertical --out-dir measure-deband-out
"""

from __future__ import annotations

import argparse
import csv
import math
import time
from pathlib import Path

import cv2
import numpy as np
import tifffile

from scanny_boy import deband, normalization
from scanny_boy import deband_support as ds
from scanny_boy.roll_manifest import load_roll_manifest

HELD_OUT_BLOCK_ROWS = 32
CONTACT_SHEET_WIDTH = 1400
CHROMA_VIS_SCALE = 10.0


def _find_negative(manifest, identifier: str):
    for negative in manifest.negatives:
        if negative.negative_id == identifier:
            return negative
        if negative.output and negative.output.get("name") in (
            identifier,
            f"{identifier}.tif",
        ):
            return negative
    raise SystemExit(f"no negative matching {identifier!r} in this roll's manifest")


def _parse_region_arg(raw: str) -> tuple[str, tuple[float, float, float, float, float]]:
    name, _, spec = raw.partition(":")
    if not spec:
        raise SystemExit(f"--region {raw!r}: expected NAME:X,Y,W,H[,TILT]")
    parts = [float(v) for v in spec.split(",")]
    if len(parts) == 4:
        parts.append(0.0)
    if len(parts) != 5:
        raise SystemExit(f"--region {raw!r}: expected 4 or 5 comma-separated numbers")
    x, y, w, h, tilt = parts
    return name, (x, y, w, h, tilt)


def _held_out_split_fit(
    image: np.ndarray,
    spans: tuple[float, ...],
    window: tuple[float, float, float, float, float],
    axis: str,
    *,
    block_rows: int = HELD_OUT_BLOCK_ROWS,
) -> deband.RegionFit:
    """A region fit computed from the even `block_rows`-line bands only
    (`row_mask`), so the held-out score below is a genuine fit/test split on
    the odd bands rather than the production fit scored on its own training
    data. The background mask is still classified on the full image, as in
    the prototype."""
    _x, _y, w, h, _tilt = window
    band_len = h if axis == "vertical" else w
    local = np.arange(math.ceil(band_len))
    even = ((local // block_rows) % 2) == 0
    return deband.fit_region(image, spans, window, axis, row_mask=even)


def _feather_trim(window: tuple[float, float, float, float, float], axis: str) -> int:
    """Width of the apply-time side feather across the band, in pixels."""
    _x, _y, w, h, _tilt = window
    cross_len = w if axis == "vertical" else h
    return math.ceil(min(deband.FEATHER_PX, deband.FEATHER_FRACTION * cross_len))


def _window_score(
    codes: np.ndarray,
    spans: tuple[float, ...],
    window: tuple[float, float, float, float, float],
    axis: str,
    interior: bool,
) -> float:
    """`banding_score` over the whole window, or (`interior`) with the
    apply-time side feather trimmed off the cross axis."""
    x, y, w, h, _tilt = window
    xi, yi, wi, hi = round(x), round(y), round(w), round(h)
    crop = codes[yi : yi + hi, xi : xi + wi]
    if axis == "horizontal":
        crop = np.transpose(crop, (1, 0, 2))
    cross_len = crop.shape[1]
    trim = _feather_trim(window, axis) if interior else 0
    return ds.banding_score(crop, spans, cols=slice(trim, cross_len - trim))


def _held_out_score(
    image: np.ndarray,
    spans: tuple[float, ...],
    healed: np.ndarray,
    window: tuple[float, float, float, float, float],
    axis: str,
    *,
    block_rows: int = HELD_OUT_BLOCK_ROWS,
) -> tuple[float, float]:
    x, y, w, h, _tilt = window
    xi, yi, wi, hi = round(x), round(y), round(w), round(h)
    band_len = hi if axis == "vertical" else wi
    local = np.arange(band_len)
    odd = ((local // block_rows) % 2) == 1
    # Score only the interior: the outer feather ramp deliberately fades the
    # correction to zero, so those columns are not what the fit corrects.
    trim = _feather_trim(window, axis)
    cross_len = wi if axis == "vertical" else hi
    cross = slice(trim, cross_len - trim)

    margin = 3 * int(ds.BG_BLUR_SIGMA)  # context for the background blur
    if axis == "vertical":
        lo = max(0, yi - margin)
        hi_ = min(image.shape[0], yi + hi + margin)
        rows = np.where(odd)[0] + (yi - lo)
        before = ds.banding_score(
            image[lo:hi_, xi : xi + wi], spans, rows=rows, cols=cross
        )
        after = ds.banding_score(
            healed[lo:hi_, xi : xi + wi], spans, rows=rows, cols=cross
        )
    else:
        # banding_score profiles along axis 1 (columns); transpose so the
        # held-out axis (here the image's own columns) plays that role.
        lo = max(0, xi - margin)
        hi_ = min(image.shape[1], xi + wi + margin)
        cols = np.where(odd)[0] + (xi - lo)
        before = ds.banding_score(
            np.transpose(image[yi : yi + hi, lo:hi_], (1, 0, 2)),
            spans,
            rows=cols,
            cols=cross,
        )
        after = ds.banding_score(
            np.transpose(healed[yi : yi + hi, lo:hi_], (1, 0, 2)),
            spans,
            rows=cols,
            cols=cross,
        )
    return before, after


def _chroma_vis(
    log_val_or_rgb_val: np.ndarray, scale: float = CHROMA_VIS_SCALE
) -> np.ndarray:
    val = log_val_or_rgb_val
    lum = val.mean(axis=2, keepdims=True)
    chroma = val - lum
    chroma = (chroma - np.median(chroma.reshape(-1, 3), axis=0)) * scale
    return np.clip(0.5 + chroma, 0.0, 1.0)


def _bg_profile(
    val: np.ndarray, spans: tuple[float, ...], axis: str = "vertical"
) -> np.ndarray:
    """Background-masked median B-G profile across the bands (log10), NaN
    where a line has no background. Columns for vertical bands, rows for
    horizontal ones."""
    if axis == "horizontal":
        val = np.swapaxes(val, 0, 1)
    log_val = val * np.asarray(spans, dtype=np.float32)
    luminance = log_val.mean(axis=2)
    blurred = cv2.GaussianBlur(
        luminance.astype(np.float32), (0, 0), sigmaX=ds.BG_BLUR_SIGMA
    )
    background = np.abs(luminance - blurred) < ds.BG_LUMA_TOL
    diff = np.where(background, log_val[..., 2] - log_val[..., 1], np.nan)
    with np.errstate(all="ignore"):
        profile = np.nanmedian(diff, axis=0)
    return np.where(background.mean(axis=0) > 0.5, profile, np.nan)


def _profile_plot(
    before: np.ndarray, after: np.ndarray, height: int = 160
) -> np.ndarray:
    """Before (grey) and after (red) B-G column profiles on a shared scale,
    quadratic-detrended per curve so only the band structure shows."""
    width = CONTACT_SHEET_WIDTH
    canvas = np.full((height, width, 3), 255, np.uint8)

    def detrended(profile: np.ndarray) -> np.ndarray:
        good = np.isfinite(profile)
        if good.sum() < 8:
            return profile
        t = np.linspace(-1.0, 1.0, len(profile))
        return profile - np.polyval(np.polyfit(t[good], profile[good], 2), t)

    curves = [detrended(before), detrended(after)]
    finite = np.concatenate([c[np.isfinite(c)] for c in curves])
    limit = (
        max(float(np.percentile(np.abs(finite), 99.5)), 0.002) if finite.size else 0.01
    )
    cv2.line(canvas, (0, height // 2), (width - 1, height // 2), (200, 200, 200), 1)
    for curve, colour in zip(curves, ((120, 120, 120), (0, 0, 220)), strict=True):
        xs = np.linspace(0, width - 1, len(curve))
        ys = height / 2 - np.clip(curve / limit, -1, 1) * (height / 2 - 4)
        ok = np.isfinite(ys)
        pts = np.stack([xs[ok], ys[ok]], axis=1).astype(np.int32).reshape(-1, 1, 2)
        cv2.polylines(canvas, [pts], False, colour, 1, cv2.LINE_AA)
    cv2.putText(
        canvas,
        f"B-G profile, +/-{limit * 1000:.1f} x1e-3 log10 (grey before, red after)",
        (8, 16),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.5,
        (0, 0, 0),
        1,
    )
    return canvas


def _contact_sheet(
    before_val: np.ndarray,
    after_val: np.ndarray,
    label: str,
    spans: tuple[float, ...],
    axis: str = "vertical",
) -> np.ndarray:
    rows = []
    span_arr = np.asarray(spans, dtype=np.float32)
    for img, tag in (
        (before_val, f"{label} before"),
        (after_val, f"{label} after (10x chroma)"),
    ):
        vis = _chroma_vis(img * span_arr)  # log10 units: chroma is physical
        height = int(vis.shape[0] * CONTACT_SHEET_WIDTH / vis.shape[1])
        resized = cv2.resize(
            vis, (CONTACT_SHEET_WIDTH, max(height, 1)), interpolation=cv2.INTER_AREA
        )
        bgr = cv2.cvtColor((resized * 255).astype(np.uint8), cv2.COLOR_RGB2BGR).copy()
        cv2.putText(bgr, tag, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 2)
        rows.append(bgr)
        rows.append(np.full((8, CONTACT_SHEET_WIDTH, 3), 255, np.uint8))
    rows.append(
        _profile_plot(
            _bg_profile(before_val, spans, axis), _bg_profile(after_val, spans, axis)
        )
    )
    return np.vstack(rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--roll", required=True, metavar="DIR")
    parser.add_argument("--tiff", required=True, metavar="NAME_OR_NEGATIVE_ID")
    parser.add_argument(
        "--region",
        action="append",
        required=True,
        metavar="NAME:X,Y,W,H[,TILT]",
        help="TIFF-space window; may be repeated",
    )
    parser.add_argument(
        "--axis", default="vertical", choices=["vertical", "horizontal"]
    )
    parser.add_argument("--strength", type=float, default=1.0)
    parser.add_argument("--out-dir", required=True, metavar="DIR")
    args = parser.parse_args()

    roll_dir = Path(args.roll)
    manifest = load_roll_manifest(roll_dir)
    negative = _find_negative(manifest, args.tiff)
    if negative.output is None:
        raise SystemExit(f"{args.tiff} has no published output")
    tiff_path = roll_dir / negative.output["name"]

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"reading {tiff_path} ...")
    t_read0 = time.time()
    image = tifffile.imread(tiff_path)
    print(
        f"  read in {time.time() - t_read0:.1f}s, shape={image.shape}, dtype={image.dtype}"
    )

    norm = negative.normalization
    if norm is None:
        raise SystemExit(f"{args.tiff} has no normalization record")
    spans = tuple(
        float(norm["ceils"][ch]) - float(norm["floors"][ch]) for ch in range(3)
    )
    canvas = (image.shape[1], image.shape[0])

    rows: list[dict[str, object]] = []
    for raw_region in args.region:
        name, window = _parse_region_arg(raw_region)
        print(f"[{negative.output['name']}:{name}] window={window} axis={args.axis}")
        row: dict[str, object] = {
            "negative": negative.output["name"],
            "region": name,
            "window": window,
            "axis": args.axis,
        }
        try:
            t0 = time.time()
            fit = deband.fit_region(image, spans, window, args.axis)
            t_fit = time.time() - t0

            params = deband.deband_params(
                canvas, args.axis, [fit], True, args.strength, spans
            )
            t1 = time.time()
            healed = deband.apply(image, params)
            t_apply = time.time() - t1

            x, y, w, h, _tilt = window
            xi, yi, wi, hi = int(x), int(y), int(w), int(h)
            before_score = _window_score(image, spans, window, args.axis, True)
            after_score = _window_score(healed, spans, window, args.axis, True)
            before_full = _window_score(image, spans, window, args.axis, False)
            after_full = _window_score(healed, spans, window, args.axis, False)

            t2 = time.time()
            held_out_fit = _held_out_split_fit(image, spans, window, args.axis)
            held_out_params = deband.deband_params(
                canvas, args.axis, [held_out_fit], True, args.strength, spans
            )
            held_out_healed = deband.apply(image, held_out_params)
            held_before, held_after = _held_out_score(
                image, spans, held_out_healed, window, args.axis
            )
            t_held_out = time.time() - t2

            corr = fit.corr  # (blocks, n_cols, 3), val units
            corr_b = corr[..., 2]
            corr_log = corr * np.asarray(spans, dtype=np.float32)
            corr_b_log = corr_log[..., 2]
            # Largest jump between neighbouring table columns (one pitch
            # apart): a sharp band edge shows up here.
            max_step_log = float(np.abs(np.diff(corr_b_log, axis=1)).max())
            max_abs_log = float(np.abs(corr_log).max())
            table_b64 = deband.deband_params(
                canvas, args.axis, [fit], True, 1.0, spans
            )["regions"][0]["corr"]

            row.update(
                {
                    "before_p99_x1e3_log10": round(before_score, 2),
                    "after_p99_x1e3_log10": round(after_score, 2),
                    "before_full_window_x1e3_log10": round(before_full, 2),
                    "after_full_window_x1e3_log10": round(after_full, 2),
                    "held_out_before_x1e3_log10": round(held_before, 2),
                    "held_out_after_x1e3_log10": round(held_after, 2),
                    "corr_b_min_log10": round(float(corr_b_log.min()), 5),
                    "corr_b_max_log10": round(float(corr_b_log.max()), 5),
                    "corr_max_abs_log10": round(max_abs_log, 5),
                    "corr_max_step_per_pitch_log10": round(max_step_log, 5),
                    "corr_b_edge_left_log10": round(
                        float(np.abs(corr_b_log[:, 0]).max()), 5
                    ),
                    "corr_b_edge_right_log10": round(
                        float(np.abs(corr_b_log[:, -1]).max()), 5
                    ),
                    "corr_b_min_val": float(corr_b.min()),
                    "corr_b_max_val": float(corr_b.max()),
                    "corr_b_edge_left_val": float(np.abs(corr_b[:, 0]).max()),
                    "corr_b_edge_right_val": float(np.abs(corr_b[:, -1]).max()),
                    "table_bytes": len(table_b64),
                    "table_kb": round(len(table_b64) / 1024, 1),
                    "blocks": fit.blocks,
                    "fit_seconds": round(t_fit, 3),
                    "apply_seconds": round(t_apply, 3),
                    "held_out_seconds": round(t_held_out, 3),
                    "error": "",
                }
            )
            print(
                f"  before={before_score:.1f} after={after_score:.1f} "
                f"held_out(before/after)={held_before:.1f}/{held_after:.1f} "
                f"corrB_log10=[{corr_b_log.min():.5f},{corr_b_log.max():.5f}] "
                f"max|corr|={max_abs_log:.5f} step/pitch={max_step_log:.5f} "
                f"edges L={row['corr_b_edge_left_log10']:.5f} "
                f"R={row['corr_b_edge_right_log10']:.5f} "
                f"table={row['table_kb']}KB fit={t_fit:.2f}s apply={t_apply:.2f}s"
            )

            before_val = normalization.decode_normalized(
                image[yi : yi + hi, xi : xi + wi]
            ).astype(np.float32)
            after_val = normalization.decode_normalized(
                healed[yi : yi + hi, xi : xi + wi]
            ).astype(np.float32)
            sheet = _contact_sheet(
                before_val,
                after_val,
                f"{negative.output['name']}:{name}",
                spans,
                args.axis,
            )
            sheet_path = out_dir / f"{negative.negative_id}-{name}.png"
            cv2.imwrite(str(sheet_path), sheet)
            print(f"  wrote {sheet_path}")
        except ValueError as exc:
            row.update({"error": str(exc)})
            print(f"  ValueError: {exc}")
        rows.append(row)

    csv_path = out_dir / "measurements.csv"
    write_header = not csv_path.exists()
    fieldnames = [
        "negative",
        "region",
        "window",
        "axis",
        "before_p99_x1e3_log10",
        "after_p99_x1e3_log10",
        "before_full_window_x1e3_log10",
        "after_full_window_x1e3_log10",
        "held_out_before_x1e3_log10",
        "held_out_after_x1e3_log10",
        "corr_b_min_log10",
        "corr_b_max_log10",
        "corr_max_abs_log10",
        "corr_max_step_per_pitch_log10",
        "corr_b_edge_left_log10",
        "corr_b_edge_right_log10",
        "corr_b_min_val",
        "corr_b_max_val",
        "corr_b_edge_left_val",
        "corr_b_edge_right_val",
        "table_bytes",
        "table_kb",
        "blocks",
        "fit_seconds",
        "apply_seconds",
        "held_out_seconds",
        "error",
    ]
    with csv_path.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})
    print(f"appended {csv_path}")


if __name__ == "__main__":
    main()
