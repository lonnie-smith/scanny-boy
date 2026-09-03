"""RAW decoding: linear sensor channels, matching NegPy's decode exactly.

NegPy's pipeline (its `docs/PIPELINE.md`, "Color handling") works on
**linear RGB straight from the raw decode** — `output_color=raw`,
`gamma=(1, 1)`, unity white balance — because film density is a
radiometric measurement of the sensor's own channels, never converted
through camera primaries into a colorimetric space. `RAW_PARAMS` matches
that decode value for value; channel balance is downstream business, not
the decoder's. `adjust_maximum_thr=0.0` pins the scale to the camera's
white level (not LibRaw's per-frame maximum), so pixel scaling stays fixed
across a whole negative. Every name and value here is locked and was
checked against the installed rawpy 0.27.0 (`user_wb` maps to LibRaw's
`user_mul`; `ColorSpace.raw` is 0).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import numpy as np
import rawpy

from scanny_boy.metadata import UnreadableRawError, UnsupportedRawError

RAW_PARAMS = {
    "output_bps": 16,
    "gamma": (1, 1),
    "no_auto_bright": True,
    "adjust_maximum_thr": 0.0,
    "use_camera_wb": False,
    "use_auto_wb": False,
    "user_wb": [1, 1, 1, 1],
    "output_color": rawpy.ColorSpace.raw,
    "demosaic_algorithm": rawpy.DemosaicAlgorithm.AHD,
    "four_color_rgb": False,
    "median_filter_passes": 0,
    "highlight_mode": rawpy.HighlightMode.Clip,
}


@dataclasses.dataclass(frozen=True)
class DecodedFrame:
    pixels: np.ndarray  # (height, width, 3) uint16
    width: int
    height: int


def jsonable_raw_params(
    chromatic_aberration: tuple[float, float] | None = None,
) -> dict:
    """`RAW_PARAMS` as a JSON-serialisable dict, for the manifest's
    `processing_params` (section 3.7: "all pixel-processing parameters").
    Enum values become their names (`"raw"`, `"AHD"`, `"Clip"`) and the
    `gamma` tuple becomes a list; everything else passes through unchanged.

    `chromatic_aberration`, when given, is the CA scale pair this decode
    actually ran with (docs/GEOMETRIC_PLAN.md section 5.2) — `processing_params`
    must describe the decode that happened, not the default one."""
    params: dict = RAW_PARAMS
    if chromatic_aberration is not None:
        params = {**RAW_PARAMS, "chromatic_aberration": tuple(chromatic_aberration)}
    result: dict = {}
    for key, value in params.items():
        if hasattr(value, "name"):
            result[key] = value.name
        elif isinstance(value, tuple):
            result[key] = list(value)
        else:
            result[key] = value
    return result


def read_active_size(path: Path) -> tuple[int, int]:
    """`(width, height)` of the processed active area, from `raw.sizes`
    alone — no `postprocess()` call needed. Section 3.9: disk-check pixel
    counts must come from this, not raw sensor dimensions or a full decode.
    """
    try:
        with rawpy.imread(str(path)) as raw:
            sizes = raw.sizes
    except rawpy.LibRawFileUnsupportedError as exc:
        raise UnsupportedRawError(str(path)) from exc
    except rawpy.LibRawError as exc:
        raise UnreadableRawError(str(path)) from exc
    except OSError as exc:
        raise UnreadableRawError(str(path)) from exc

    return sizes.width, sizes.height


def decode_raw(
    path: Path,
    *,
    chromatic_aberration: tuple[float, float] | None = None,
    params: dict | None = None,
) -> DecodedFrame:
    """Decode `path` with `RAW_PARAMS`.

    `chromatic_aberration`, when given, is the profile's CA scale pair
    (docs/GEOMETRIC_PLAN.md section 5.2), merged into the params for this
    call alone — the one rawpy knob that changes pixel geometry at decode
    time, applied before flat-field touches the pixels.

    `params`, when given, replaces the base parameter set outright; the CA
    fit's `RAW_PARAMS_HALF_SIZE` derivation is the one intended use
    (ca_fit.py documents why it deviates).

    Dimensions are derived from the returned array's shape, per section 3.4
    ("Record output dimensions from the final postprocess array shape."),
    not from rawpy's reported sensor sizes.
    """
    merged = params if params is not None else RAW_PARAMS
    if chromatic_aberration is not None:
        merged = {**merged, "chromatic_aberration": tuple(chromatic_aberration)}
    try:
        with rawpy.imread(str(path)) as raw:
            pixels = raw.postprocess(**merged)
    except rawpy.LibRawFileUnsupportedError as exc:
        raise UnsupportedRawError(str(path)) from exc
    except rawpy.LibRawError as exc:
        raise UnreadableRawError(str(path)) from exc
    except OSError as exc:
        raise UnreadableRawError(str(path)) from exc

    height, width, _ = pixels.shape
    return DecodedFrame(pixels=pixels, width=width, height=height)
