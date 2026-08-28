"""Per-file capture settings needed for setup-consistency validation.

See `docs/IMPLEMENTATION_PLAN.md` section 3.2 (what to compare) and section
3.5 (the required/optional tag mapping, which this module dumps in full for
the Chunk 2 pull request).

Reading is split in two, for testability per section 7 ("Do not mock
rawpy's decoding"):

- `read_exif_settings` reads ordinary TIFF/EXIF tags with `exifread`. It can
  be exercised against small crafted TIFF fixtures (see
  `fake_nef_support.py`), because `exifread` only cares about TIFF
  structure, not whether the file is a real NEF.
- `read_camera_whitebalance` opens the file with `rawpy`, which only a real
  RAW file satisfies. It is tested against the real sample NEFs.

`read_source_settings` combines both for `probe.py`'s orchestration.
"""

from __future__ import annotations

import dataclasses
from fractions import Fraction
from pathlib import Path
from typing import Any

import exifread
import rawpy


class UnsupportedRawError(Exception):
    """LibRaw cannot read this file at all (typically HE/HE*) — maps to
    `UNSUPPORTED_RAW`."""


class UnreadableRawError(Exception):
    """The file could not be opened for another reason — maps to
    `UNREADABLE_RAW`."""


@dataclasses.dataclass(frozen=True)
class ExifSettings:
    exposure_time: Fraction | None
    f_number: Fraction | None
    iso: int | None
    focal_length: Fraction | None
    lens_model: str | None
    orientation: int | None


@dataclasses.dataclass(frozen=True)
class SourceSettings:
    filename: str
    exposure_time: Fraction | None
    f_number: Fraction | None
    iso: int | None
    focal_length: Fraction | None
    lens_model: str | None
    orientation: int | None
    camera_whitebalance: tuple[float, float, float, float] | None


def _ratio_to_fraction(tag: Any) -> Fraction | None:
    if tag is None or not tag.values:
        return None
    ratio = tag.values[0]
    if ratio.den == 0:
        return None
    return Fraction(ratio.num, ratio.den)


def _short(tag: Any) -> int | None:
    if tag is None or not tag.values:
        return None
    return int(tag.values[0])


def _ascii(tag: Any) -> str | None:
    if tag is None:
        return None
    text = str(tag).strip()
    return text or None


def read_exif_settings(path: Path) -> ExifSettings:
    """Read the section-3.5 comparison fields with `exifread`."""
    with path.open("rb") as f:
        tags = exifread.process_file(f, details=False)

    return ExifSettings(
        exposure_time=_ratio_to_fraction(tags.get("EXIF ExposureTime")),
        f_number=_ratio_to_fraction(tags.get("EXIF FNumber")),
        iso=_short(tags.get("EXIF ISOSpeedRatings")),
        focal_length=_ratio_to_fraction(tags.get("EXIF FocalLength")),
        lens_model=_ascii(tags.get("EXIF LensModel")),
        orientation=_short(tags.get("Image Orientation")),
    )


def read_camera_whitebalance(path: Path) -> tuple[float, float, float, float] | None:
    """Read `raw.camera_whitebalance`, per section 3.2. Returns `None` when
    LibRaw reports fewer than four multipliers."""
    try:
        with rawpy.imread(str(path)) as raw:
            wb = raw.camera_whitebalance
    except rawpy.LibRawFileUnsupportedError as exc:
        raise UnsupportedRawError(str(path)) from exc
    except rawpy.LibRawError as exc:
        raise UnreadableRawError(str(path)) from exc
    except OSError as exc:
        raise UnreadableRawError(str(path)) from exc

    if wb is None or len(wb) < 4:
        return None
    return (float(wb[0]), float(wb[1]), float(wb[2]), float(wb[3]))


def read_source_settings(path: Path) -> SourceSettings:
    exif = read_exif_settings(path)
    wb = read_camera_whitebalance(path)
    return SourceSettings(
        filename=path.name,
        exposure_time=exif.exposure_time,
        f_number=exif.f_number,
        iso=exif.iso,
        focal_length=exif.focal_length,
        lens_model=exif.lens_model,
        orientation=exif.orientation,
        camera_whitebalance=wb,
    )
