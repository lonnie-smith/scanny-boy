"""Writes the base TIFF: RGB16 pixels, Deflate compression with horizontal
prediction, the ordinary (non-nested) IFD0 tags of section 3.5's table, and
an embedded ICC profile.

Chunk 4 rewrites this file with `tifftools` to add the nested EXIF
directory (`DateTimeOriginal`, `ExposureTime`, and the rest); this module
only ever produces the `<name>.base.tif` half of that two-pass write.

See `docs/IMPLEMENTATION_PLAN.md` section 3.4, "Writing the TIFF with
tifffile": four rules are not obvious and are each followed exactly here —
`metadata=None` (avoids a duplicate `ImageDescription`), `description=`/
`software=` keyword arguments (`extratags` silently drops both),
`iccprofile=` (not `extratags`), and Adobe Deflate's TIFF compression code
`32946` (not `8`).
"""

from __future__ import annotations

import dataclasses
import datetime
import importlib.metadata
from pathlib import Path

import numpy as np
import tifffile
from tifftools.constants import Tag

# tifffile writes Deflate as Adobe Deflate; assert this in tests so a future
# tifffile change is caught rather than silently producing a different code.
DEFLATE_COMPRESSION_CODE = 32946
HORIZONTAL_PREDICTOR = 2

# TIFF Orientation is always 1: pixels are already upright by the time they
# reach this writer (section 3.4). Never copy the source Orientation value.
OUTPUT_ORIENTATION = 1


def software_tag_value() -> str:
    """The `Software` tag value: "Scanny Boy <version>", per section 3.5."""
    return f"Scanny Boy {importlib.metadata.version('scanny-boy')}"


def image_description(source_filename: str) -> str:
    """The `ImageDescription` tag value: the source filename and
    "unstitched scan frame", per section 3.5."""
    return f"{source_filename}: unstitched scan frame"


@dataclasses.dataclass(frozen=True)
class BaseTiffTags:
    description: str
    software: str
    conversion_time: datetime.datetime
    icc_profile: bytes
    make: str | None = None
    model: str | None = None


def write_base_tiff(path: Path, pixels: np.ndarray, tags: BaseTiffTags) -> None:
    """Write `pixels` (`(height, width, 3)` `uint16`) to `path`."""
    if pixels.dtype != np.uint16 or pixels.ndim != 3 or pixels.shape[2] != 3:
        raise ValueError(
            f"expected (height, width, 3) uint16 pixels, got shape "
            f"{pixels.shape} dtype {pixels.dtype}"
        )
    if not tags.icc_profile:
        # Never silently write untagged linear data (see icc_profile.py).
        raise ValueError("refusing to write a TIFF without an embedded ICC profile")

    extratags: list[tuple] = [
        (Tag.Orientation.value, tifffile.DATATYPE.SHORT, 1, (OUTPUT_ORIENTATION,), True),
    ]
    if tags.make is not None:
        extratags.append((Tag.Make.value, tifffile.DATATYPE.ASCII, 0, tags.make, True))
    if tags.model is not None:
        extratags.append((Tag.Model.value, tifffile.DATATYPE.ASCII, 0, tags.model, True))

    tifffile.imwrite(
        path,
        pixels,
        photometric="rgb",
        compression="deflate",
        predictor=True,
        maxworkers=1,
        metadata=None,
        description=tags.description,
        software=tags.software,
        datetime=tags.conversion_time,
        iccprofile=tags.icc_profile,
        extratags=extratags,
    )
