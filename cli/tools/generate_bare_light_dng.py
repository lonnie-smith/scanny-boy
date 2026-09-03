#!/usr/bin/env python3
"""Deterministic generator for tests/fixtures/flatfield/bare-light.dng.

The Swift integration scenarios need a flat-field profile to run against —
the app requires one on Add Scans — but the only local references are the
sample NEFs, which are film frames: their scene content survives the gain
map's smoothing and corrupts the correction (the packaged run then fails
registration with STITCH_RESIDUAL_TOO_HIGH). A bare light source these
fixtures do not have, and a NEF cannot be authored anyway — the container
is proprietary.

What *can* be authored is a DNG: an open, TIFF-based RAW container that
LibRaw decodes through the locked `raw_decode.RAW_PARAMS` like any NEF.
This writes one holding a plausible bare light — a smooth 10% radial
falloff, the order a copy stand's light panel actually shows, plus mild
sensor noise so the gain map's blur has something honest to average over —
as a 768x512 (3:2, matching the sample scans' aspect) uncompressed RGGB
CFA frame.

Run from the repository root:

    uv run --project cli python cli/tools/generate_bare_light_dng.py

The output is byte-identical across runs (fixed seed, fixed layout), so the
committed file can always be regenerated and compared.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import tifffile

OUTPUT = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "flatfield" / "bare-light.dng"

WIDTH, HEIGHT = 768, 512
FALLOFF = 0.10  # centre-to-corner irradiance drop of the modelled light
NOISE_SIGMA = 60.0
MEAN_LEVEL = 0.55  # of full scale: well above black, well below clip
SEED = 7


def main() -> None:
    yy, xx = np.mgrid[0:HEIGHT, 0:WIDTH]
    cx, cy = (WIDTH - 1) / 2, (HEIGHT - 1) / 2
    radius = np.sqrt(((xx - cx) / cx) ** 2 + ((yy - cy) / cy) ** 2)
    vignette = 1.0 - FALLOFF * radius**2
    rng = np.random.default_rng(SEED)
    gray = np.clip(
        MEAN_LEVEL * 65535 * vignette + rng.normal(0, NOISE_SIGMA, (HEIGHT, WIDTH)),
        0,
        65535,
    ).astype(np.uint16)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    tifffile.imwrite(
        OUTPUT,
        gray,
        photometric=32803,  # PhotometricInterpretation: CFA
        extratags=[
            (33421, 3, 2, (2, 2), False),  # CFARepeatPatternDim
            (33422, 1, 4, bytes([0, 1, 1, 2]), False),  # CFAPattern: RGGB
            (50706, 1, 4, bytes([1, 4, 0, 0]), False),  # DNGVersion 1.4.0.0
            (50717, 4, 1, 65535, False),  # WhiteLevel
        ],
    )
    print(f"wrote {OUTPUT} ({OUTPUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
