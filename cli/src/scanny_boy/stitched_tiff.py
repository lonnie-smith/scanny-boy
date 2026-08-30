"""Writes a stitched negative's TIFF: a thin wrapper around Phase 1's
two-pass writer (`tiff_writer`, `tiff_exif`), which established the
extratags rules, the ICC handling, and the two-pass write itself
(`docs/IMPLEMENTATION_PLAN.md` section 3.4) and must not be reimplemented
here.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import numpy as np

from scanny_boy.tiff_exif import NestedExifFields, finalize_tiff
from scanny_boy.tiff_writer import BaseTiffTags, write_base_tiff


def stitched_image_description(first_source: str, frame_count: int) -> str:
    """f"{first_source}+{frame_count - 1}: stitched scan" — e.g.
    "_DSC4638.NEF+2: stitched scan"."""
    return f"{first_source}+{frame_count - 1}: stitched scan"


def write_stitched_tiff(
    path: Path,
    image: np.ndarray,
    *,
    tags: BaseTiffTags,
    exif: NestedExifFields,
    icc_bytes: bytes,
) -> None:
    """Thin wrapper. Calls tiff_writer.write_base_tiff and
    tiff_exif.finalize_tiff. Do NOT reimplement the two-pass write, the
    extratags rules, or the ICC handling — Phase 1 section 3.4 established
    all four of those and each was independently verified to matter.
    """
    full_tags = dataclasses.replace(tags, icc_profile=icc_bytes)
    base_path = path.with_name(f"{path.stem}.base.tif")
    write_base_tiff(base_path, image, full_tags)
    finalize_tiff(base_path, path, exif)
