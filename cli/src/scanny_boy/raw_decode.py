"""RAW decoding with the exact rawpy parameters of section 3.4.

`RAW_PARAMS` fixes bit depth, colour space, demosaicing, and — critically —
disables histogram-based brightening and content-dependent maximum
adjustment, so pixel scaling stays fixed across a whole negative. See
`docs/IMPLEMENTATION_PLAN.md` section 3.4; every name and value here is
locked and was checked against the installed rawpy 0.27.0.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import numpy as np
import rawpy

from scanny_boy.metadata import UnreadableRawError, UnsupportedRawError

RAW_PARAMS = {
    "output_bps": 16,
    "gamma": (1.8, 16),
    "no_auto_bright": True,
    "adjust_maximum_thr": 0.0,
    "use_camera_wb": True,
    "use_auto_wb": False,
    "output_color": rawpy.ColorSpace.ProPhoto,
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


def jsonable_raw_params() -> dict:
    """`RAW_PARAMS` as a JSON-serialisable dict, for the manifest's
    `processing_params` (section 3.7: "all pixel-processing parameters").
    Enum values become their names (`"ProPhoto"`, `"AHD"`, `"Clip"`) and the
    `gamma` tuple becomes a list; everything else passes through unchanged.
    """
    result: dict = {}
    for key, value in RAW_PARAMS.items():
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


def decode_raw(path: Path) -> DecodedFrame:
    """Decode `path` with `RAW_PARAMS`.

    Dimensions are derived from the returned array's shape, per section 3.4
    ("Record output dimensions from the final postprocess array shape."),
    not from rawpy's reported sensor sizes.
    """
    try:
        with rawpy.imread(str(path)) as raw:
            pixels = raw.postprocess(**RAW_PARAMS)
    except rawpy.LibRawFileUnsupportedError as exc:
        raise UnsupportedRawError(str(path)) from exc
    except rawpy.LibRawError as exc:
        raise UnreadableRawError(str(path)) from exc
    except OSError as exc:
        raise UnreadableRawError(str(path)) from exc

    height, width, _ = pixels.shape
    return DecodedFrame(pixels=pixels, width=width, height=height)
