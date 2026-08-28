"""Adds the nested EXIF directory to a base TIFF, with `tifftools`.

`tiff_writer.write_base_tiff` (Chunk 3) writes `<name>.base.tif`: pixels,
the ordinary IFD0 tags, and the ICC profile. This module performs the
second pass of section 3.4's two-pass write — reading that file, adding
the nested EXIF IFD from section 3.5's tag table, and writing
`<name>.final.tif` — because `tifffile` cannot create a nested EXIF
directory at all (see `tiff_writer`'s module docstring).

Every EXIF tag below is addressed by its numeric code, not by
`tifftools.constants.EXIFTag`'s names: two of them are spelled differently
there than in `CONTRACT.md`/exiftool's convention (36868 is `CreateDate`,
and 34855 is `ISOSpeedRatings`), so numeric codes avoid the whole problem
(section 3.5, "Writing the nested EXIF directory with tifftools").

Do not copy Nikon MakerNotes, serial numbers, thumbnails, or arbitrary
unknown tags: only the fields built here are ever written.
"""

from __future__ import annotations

import dataclasses
import datetime
from fractions import Fraction
from pathlib import Path

import tifffile
import tifftools
from tifftools.constants import Tag

from scanny_boy import tiff_writer
from scanny_boy.events import Code
from scanny_boy.film_date import format_date_time, format_subsec

# EXIF tag codes for the nested IFD (section 3.5's mapping table).
DATE_TIME_ORIGINAL = 36867
SUBSEC_TIME_ORIGINAL = 37521
DATE_TIME_DIGITIZED = 36868
SUBSEC_TIME_DIGITIZED = 37522
OFFSET_TIME_DIGITIZED = 36882
LENS_MODEL = 42036
EXPOSURE_TIME = 33434
F_NUMBER = 33437
PHOTOGRAPHIC_SENSITIVITY = 34855
FOCAL_LENGTH = 37386
COLOR_SPACE = 40961

# ColorSpace value 65535 ("uncalibrated"): the embedded ICC profile
# identifies ROMM, not one of the two calibrated EXIF ColorSpace values.
UNCALIBRATED_COLOR_SPACE = 65535

# ImageDescription (270): asserted present exactly once, both here and by
# tiff_writer's own base-TIFF tests.
IMAGE_DESCRIPTION_TAG_CODE = 270


class TiffFinalizeError(Exception):
    """Maps to `TIFF_WRITE_FAILED`: the nested-EXIF rewrite could not be
    verified. The base file is left in place so nothing is lost."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = Code.TIFF_WRITE_FAILED
        self.message = message


@dataclasses.dataclass(frozen=True)
class NestedExifFields:
    """Everything needed to build the nested EXIF IFD for one frame.

    `exposure_time`, `f_number`, `iso`, and `focal_length` are all
    **required** per section 3.5's tag table — by the time this is built,
    `consistency.check_consistency` has already confirmed every selected
    file carries them. `lens_model` and the three `*_digitized` fields are
    **optional**: `None` omits the corresponding tag.
    """

    date_time_original: datetime.datetime  # synthetic; see film_date.py
    exposure_time: Fraction
    f_number: Fraction
    iso: int
    focal_length: Fraction
    lens_model: str | None
    date_time_digitized: str | None
    subsec_time_digitized: str | None
    offset_time_digitized: str | None


def _ascii_tag(value: str) -> dict:
    return {"data": value, "datatype": tifftools.Datatype.ASCII}


def _rational_tag(value: Fraction) -> dict:
    return {
        "data": [value.numerator, value.denominator],
        "datatype": tifftools.Datatype.RATIONAL,
    }


def _short_tag(value: int) -> dict:
    return {"data": [value], "datatype": tifftools.Datatype.SHORT}


def build_exif_tags(fields: NestedExifFields) -> dict[int, dict]:
    """The nested EXIF IFD's tag dict, in `tifftools`' `{code: {data,
    datatype}}` shape. Pure and file-free, so section 3.5's curation rules
    are testable without writing any TIFF."""
    tags: dict[int, dict] = {
        DATE_TIME_ORIGINAL: _ascii_tag(format_date_time(fields.date_time_original)),
        EXPOSURE_TIME: _rational_tag(fields.exposure_time),
        F_NUMBER: _rational_tag(fields.f_number),
        PHOTOGRAPHIC_SENSITIVITY: _short_tag(fields.iso),
        FOCAL_LENGTH: _rational_tag(fields.focal_length),
        COLOR_SPACE: _short_tag(UNCALIBRATED_COLOR_SPACE),
    }

    subsec = format_subsec(fields.date_time_original)
    if subsec is not None:
        tags[SUBSEC_TIME_ORIGINAL] = _ascii_tag(subsec)
    if fields.lens_model is not None:
        tags[LENS_MODEL] = _ascii_tag(fields.lens_model)
    if fields.date_time_digitized is not None:
        tags[DATE_TIME_DIGITIZED] = _ascii_tag(fields.date_time_digitized)
    if fields.subsec_time_digitized is not None:
        tags[SUBSEC_TIME_DIGITIZED] = _ascii_tag(fields.subsec_time_digitized)
    if fields.offset_time_digitized is not None:
        tags[OFFSET_TIME_DIGITIZED] = _ascii_tag(fields.offset_time_digitized)

    return tags


def write_nested_exif(base_path: Path, final_path: Path, fields: NestedExifFields) -> None:
    """Read `base_path`, add the nested EXIF IFD, and write `final_path`.
    Leaves `base_path` untouched — callers decide when it is safe to
    remove (see `finalize_tiff`)."""
    info = tifftools.read_tiff(str(base_path))
    ifd0 = info["ifds"][0]
    exif_ifd = {"tags": build_exif_tags(fields), "ifds": []}
    ifd0["tags"][Tag.ExifIFD.value] = {
        "ifds": [[exif_ifd]],
        "datatype": tifftools.Datatype.LONG,
    }
    tifftools.write_tiff(info, str(final_path))


def _verify_final_tiff(final_path: Path, fields: NestedExifFields) -> None:
    """Structural sanity checks the two-pass write must satisfy before its
    base file is removed: the ordinary tags `tifftools` copied through are
    unchanged, exactly one `ImageDescription` survives, and the nested EXIF
    IFD round-trips the value just written."""
    try:
        with tifffile.TiffFile(final_path) as tf:
            page = tf.pages[0]
            codes = [tag.code for tag in page.tags]
            if codes.count(IMAGE_DESCRIPTION_TAG_CODE) != 1:
                raise ValueError("expected exactly one ImageDescription tag")
            if page.tags["Compression"].value != tiff_writer.DEFLATE_COMPRESSION_CODE:
                raise ValueError("compression code changed during the EXIF rewrite")
            if page.tags["Predictor"].value != tiff_writer.HORIZONTAL_PREDICTOR:
                raise ValueError("predictor changed during the EXIF rewrite")
            if int(page.tags["Orientation"].value) != tiff_writer.OUTPUT_ORIENTATION:
                raise ValueError("orientation changed during the EXIF rewrite")

        info = tifftools.read_tiff(str(final_path))
        exif_ifd = info["ifds"][0]["tags"][Tag.ExifIFD.value]["ifds"][0][0]
        actual = exif_ifd["tags"][DATE_TIME_ORIGINAL]["data"]
        expected = format_date_time(fields.date_time_original)
        if actual != expected:
            raise ValueError(
                f"DateTimeOriginal did not round-trip: wrote {expected!r}, read {actual!r}"
            )
    except Exception as exc:
        raise TiffFinalizeError(
            f"could not verify rewritten TIFF at {final_path}: {exc}"
        ) from exc


def finalize_tiff(base_path: Path, final_path: Path, fields: NestedExifFields) -> None:
    """The full second pass (section 3.6): write the nested-EXIF
    `final_path` from `base_path`, verify it, and only then remove
    `base_path`. A failed verification leaves both files in place — the
    base file is never removed until the final file is proven usable."""
    write_nested_exif(base_path, final_path, fields)
    _verify_final_tiff(final_path, fields)
    base_path.unlink()
