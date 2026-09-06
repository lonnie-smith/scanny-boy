#!/usr/bin/env python3
"""Deterministic generator for tests/fixtures/base-frame/base-frame.dng.

Chunk B-7 of docs/REBATE_ANCHORING.md. The fast tier needs a realistic
base frame — a film-base reference the detector measures and gates — and
a NEF cannot be authored (the container is proprietary). What can be
authored is a DNG, which LibRaw decodes through the locked
`raw_decode.RAW_PARAMS` like any NEF.

The default layout is the §0.4 common case: **two rebate bands** (the
top and bottom quarters, at a plausible colour-negative film base — an
orange mask, dense in blue) with a **picture strip between them** (a
smooth horizontal density gradient, so the peel's flatness gate correctly
declines it), plus mild sensor noise. `--layout flat` writes an
all-rebate frame (§0.3's easy case, the one detect_rebate cannot
handle); `--layout sliver` adds a bare-light band along the top edge
(thinner than base, small enough that gate 6's ambiguity rule must NOT
fire). A gentle 3% radial falloff is modelled — well inside the
detector's flatness gate even without a flat-field gain map applied, and
what a copy stand's panel actually shows.

Run from the repository root:

    uv run --project cli python cli/tools/generate_base_frame_dng.py \
        [--layout {rebate-bands,flat,sliver}] [--out PATH]

The output is byte-identical across runs (fixed seed, fixed layout), so
the committed file can always be regenerated and compared.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import tifffile

DEFAULT_OUTPUT = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "base-frame"
    / "base-frame.dng"
)

WIDTH, HEIGHT = 768, 512
# Log10 densities. Base: an orange mask — dense in blue, exactly §1.2's
# channel that runs out first. Picture: denser scene content, gradient.
BASE_DENSITY = (-0.42, -0.12, -0.99)
PICTURE_DENSITY = (-1.00, -0.85, -1.40)
PICTURE_GRADIENT = 0.9  # decades of smooth change across the picture strip
BARE_DENSITY = (0.0, 0.0, 0.0)  # bare light: no film in the path
BAND_FRACTION = 0.25  # each rebate band's share of the height
SLIVER_ROWS = 6
FALLOFF = 0.03  # centre-to-corner irradiance drop of the modelled light
NOISE_SIGMA = 30.0
SEED = 7


def _channel_levels(density: tuple[float, float, float]) -> np.ndarray:
    return np.power(10.0, np.asarray(density, dtype=np.float64))


def build_frame(layout: str) -> np.ndarray:
    """The modelled frame as per-channel linear levels, (HEIGHT, WIDTH, 3)
    in [0, 1]."""
    yy, xx = np.mgrid[0:HEIGHT, 0:WIDTH]
    cx, cy = (WIDTH - 1) / 2, (HEIGHT - 1) / 2
    radius = np.sqrt(((xx - cx) / cx) ** 2 + ((yy - cy) / cy) ** 2)
    vignette = (1.0 - FALLOFF * radius**2)[:, :, np.newaxis]

    base = np.broadcast_to(
        _channel_levels(BASE_DENSITY)[np.newaxis, np.newaxis, :], (HEIGHT, WIDTH, 3)
    )
    if layout == "flat":
        field = base
    else:
        band = int(HEIGHT * BAND_FRACTION)
        # The picture strip is scene content: a smooth density gradient
        # around PICTURE_DENSITY, spanning PICTURE_GRADIENT decades down
        # the strip — never featureless, so the peel's flatness gate
        # correctly declines it.
        rows = np.linspace(0.0, PICTURE_GRADIENT, HEIGHT, dtype=np.float64)
        gradient_density = (
            np.asarray(PICTURE_DENSITY, dtype=np.float64)[np.newaxis, :]
            - rows[:, np.newaxis]
        )[:, np.newaxis, :]
        picture = np.power(10.0, gradient_density)
        field = np.array(np.broadcast_to(picture, (HEIGHT, WIDTH, 3)), copy=True)
        field[:band] = base[0]
        field[HEIGHT - band :] = base[0]
        if layout == "sliver":
            field[:SLIVER_ROWS] = _channel_levels(BARE_DENSITY)

    rng = np.random.default_rng(SEED)
    return np.clip(
        field * vignette + rng.normal(0, NOISE_SIGMA / 65535.0, (HEIGHT, WIDTH, 3)),
        0.0,
        1.0,
    )


def _to_cfa(frame: np.ndarray) -> np.ndarray:
    """Mosaic the (HEIGHT, WIDTH, 3) linear levels onto an RGGB CFA: R at
    (0, 0), G at (0, 1) and (1, 0), B at (1, 1)."""
    red, green, blue = frame[..., 0], frame[..., 1], frame[..., 2]
    cfa = np.zeros((HEIGHT, WIDTH), dtype=np.float64)
    cfa[0::2, 0::2] = red[0::2, 0::2]
    cfa[0::2, 1::2] = green[0::2, 1::2]
    cfa[1::2, 0::2] = green[1::2, 0::2]
    cfa[1::2, 1::2] = blue[1::2, 1::2]
    return cfa


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--layout",
        choices=("rebate-bands", "flat", "sliver"),
        default="rebate-bands",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    cfa = _to_cfa(build_frame(args.layout))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    tifffile.imwrite(
        args.out,
        np.rint(cfa * 65535.0).astype(np.uint16),
        photometric=32803,  # PhotometricInterpretation: CFA
        extratags=[
            (33421, 3, 2, (2, 2), False),  # CFARepeatPatternDim
            (33422, 1, 4, bytes([0, 1, 1, 2]), False),  # CFAPattern: RGGB
            (50706, 1, 4, bytes([1, 4, 0, 0]), False),  # DNGVersion 1.4.0.0
            (50717, 4, 1, 65535, False),  # WhiteLevel
        ],
    )
    print(f"wrote {args.out} ({args.out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
