"""Shared helper for building small TIFF files with crafted EXIF tags.

Chunk 2's catalogue, sorting, and metadata-reading logic reads its input
through `exifread`, which only cares about TIFF/EXIF structure, not whether
the file is a real NEF. This writes a minimal TIFF with a nested EXIF IFD
(the same `tifftools` technique Chunk 4 uses for real output) so tests can
control exactly which tags are present, without needing rawpy or real RAW
pixel data.

These fixtures are not openable by rawpy as RAW files, so anything that goes
through `rawpy.imread` (camera_whitebalance, and the full `probe --files`
pipeline) must still be tested against the real sample NEFs. See
`docs/IMPLEMENTATION_PLAN.md` section 7: "Do not mock rawpy's decoding."
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import tifffile
import tifftools
from tifftools.constants import EXIFTag, Tag


def write_fake_nef(
    path: Path,
    *,
    date_time_original: str | None = "2026:08:02 12:33:27",
    subsec_time_original: str | None = "77",
    exposure_time: tuple[int, int] | None = (1, 30),
    f_number: tuple[int, int] | None = (8, 1),
    iso: int | None = 100,
    focal_length: tuple[int, int] | None = (55, 1),
    lens_model: str | None = "55mm f/2.8",
    orientation: int | None = 1,
    make: str | None = "NIKON CORPORATION",
    model: str | None = "NIKON Z f",
) -> Path:
    """Write a tiny TIFF at `path` carrying the given tag values.

    Pass `None` for any field to omit that tag entirely, for testing
    missing-tag behaviour.
    """
    base = path.with_suffix(".base.tif")
    tifffile.imwrite(base, np.zeros((2, 2, 3), dtype=np.uint16), photometric="rgb")

    info = tifftools.read_tiff(str(base))
    ifd0 = info["ifds"][0]

    exif_tags: dict[int, dict] = {}
    if date_time_original is not None:
        exif_tags[EXIFTag.DateTimeOriginal.value] = {
            "data": date_time_original,
            "datatype": tifftools.Datatype.ASCII,
        }
    if subsec_time_original is not None:
        exif_tags[EXIFTag.SubSecTimeOriginal.value] = {
            "data": subsec_time_original,
            "datatype": tifftools.Datatype.ASCII,
        }
    if exposure_time is not None:
        exif_tags[EXIFTag.ExposureTime.value] = {
            "data": list(exposure_time),
            "datatype": tifftools.Datatype.RATIONAL,
        }
    if f_number is not None:
        exif_tags[EXIFTag.FNumber.value] = {
            "data": list(f_number),
            "datatype": tifftools.Datatype.RATIONAL,
        }
    if iso is not None:
        exif_tags[EXIFTag.ISOSpeedRatings.value] = {
            "data": [iso],
            "datatype": tifftools.Datatype.SHORT,
        }
    if focal_length is not None:
        exif_tags[EXIFTag.FocalLength.value] = {
            "data": list(focal_length),
            "datatype": tifftools.Datatype.RATIONAL,
        }
    if lens_model is not None:
        exif_tags[EXIFTag.LensModel.value] = {
            "data": lens_model,
            "datatype": tifftools.Datatype.ASCII,
        }

    if exif_tags:
        exif_ifd = {"tags": exif_tags, "ifds": []}
        ifd0["tags"][Tag.ExifIFD.value] = {
            "ifds": [[exif_ifd]],
            "datatype": tifftools.Datatype.LONG,
        }

    if orientation is not None:
        ifd0["tags"][Tag.Orientation.value] = {
            "data": [orientation],
            "datatype": tifftools.Datatype.SHORT,
        }
    else:
        ifd0["tags"].pop(Tag.Orientation.value, None)
    if make is not None:
        ifd0["tags"][Tag.Make.value] = {"data": make, "datatype": tifftools.Datatype.ASCII}
    if model is not None:
        ifd0["tags"][Tag.Model.value] = {"data": model, "datatype": tifftools.Datatype.ASCII}

    tifftools.write_tiff(info, str(path))
    base.unlink()
    return path
