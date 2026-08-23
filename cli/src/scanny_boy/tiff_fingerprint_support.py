"""A comparable summary of one output TIFF, shared by the tests that compare
two runs of the converter against each other.

Section 7: "Compare pixel hashes and metadata after documented changing
fields are ignored, not entire TIFF bytes, which contain conversion
timestamps." The only such field is IFD0 `DateTime` (306), the moment of
conversion. `DateTimeOriginal` is synthetic and derived from the film date,
so it must match exactly between runs.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import tifffile
import tifftools
from tifftools.constants import Tag as ExifIFDTag

CONVERSION_TIME_TAG = 306  # IFD0 DateTime: the one documented changing field


def tiff_fingerprint(path: Path) -> tuple[str, dict]:
    """The SHA-256 of the TIFF's decoded pixels, plus every IFD0 and
    nested-EXIF tag value except the conversion time."""
    with tifffile.TiffFile(path) as handle:
        pixels = handle.asarray()
    pixel_sha256 = hashlib.sha256(pixels.tobytes()).hexdigest()

    ifd0 = tifftools.read_tiff(str(path))["ifds"][0]
    tags: dict = {}
    for code, entry in ifd0["tags"].items():
        if code == CONVERSION_TIME_TAG:
            continue
        if code == ExifIFDTag.ExifIFD.value:
            nested = entry["ifds"][0][0]["tags"]
            tags[code] = {c: e.get("data") for c, e in nested.items()}
        else:
            tags[code] = entry.get("data")
    return pixel_sha256, tags
