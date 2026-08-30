import datetime
from fractions import Fraction
from pathlib import Path

import numpy as np
import tifffile
import tifftools
from tifftools.constants import Tag

from scanny_boy.icc_profile import load_icc_profile
from scanny_boy.stitched_tiff import stitched_image_description, write_stitched_tiff
from scanny_boy.tiff_exif import NestedExifFields
from scanny_boy.tiff_writer import (
    DEFLATE_COMPRESSION_CODE,
    HORIZONTAL_PREDICTOR,
    OUTPUT_ORIENTATION,
    BaseTiffTags,
    software_tag_value,
)

_CONVERSION_TIME = datetime.datetime(2026, 8, 2, 12, 0, 0)  # noqa: DTZ001
_IMAGE_DESCRIPTION_TAG_CODE = 270


def _canvas_pixels(height: int = 30, width: int = 40, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    y = np.linspace(0, 65535, height)[:, None]
    x = np.linspace(0, 65535, width)[None, :]
    base = (y + x) / 2
    noise = rng.normal(scale=200, size=(height, width))
    r = np.clip(base + noise, 0, 65535)
    g = np.clip(base * 0.8 + noise, 0, 65535)
    b = np.clip(base * 0.6 + noise, 0, 65535)
    return np.stack([r, g, b], axis=-1).astype(np.uint16)


def _tags(**overrides) -> BaseTiffTags:
    defaults = {
        "description": stitched_image_description("_DSC4638.NEF", 3),
        "software": software_tag_value(),
        "conversion_time": _CONVERSION_TIME,
        "icc_profile": b"",  # write_stitched_tiff overrides this with icc_bytes
        "make": "NIKON CORPORATION",
        "model": "NIKON Z f",
    }
    defaults.update(overrides)
    return BaseTiffTags(**defaults)


def _exif() -> NestedExifFields:
    return NestedExifFields(
        date_time_original=datetime.datetime(2026, 8, 2, 12, 33, 41),  # noqa: DTZ001
        exposure_time=Fraction(1, 30),
        f_number=Fraction(8, 1),
        iso=100,
        focal_length=Fraction(55, 1),
        lens_model="55mm f/2.8",
        date_time_digitized="2026:08:02 12:33:41",
        subsec_time_digitized="45",
        offset_time_digitized="-05:00",
    )


def _write(tmp_path: Path, **tag_overrides) -> Path:
    path = tmp_path / "_DSC4638.tif"
    write_stitched_tiff(
        path,
        _canvas_pixels(),
        tags=_tags(**tag_overrides),
        exif=_exif(),
        icc_bytes=load_icc_profile(),
    )
    return path


def test_matches_every_phase_one_tiff_rule(tmp_path):
    path = _write(tmp_path)

    with tifffile.TiffFile(path) as tf:
        page = tf.pages[0]
        assert page.shape[-1] == 3
        assert page.dtype == np.uint16
        assert page.tags["Compression"].value == DEFLATE_COMPRESSION_CODE
        assert page.tags["Predictor"].value == HORIZONTAL_PREDICTOR
        assert int(page.tags["Orientation"].value) == OUTPUT_ORIENTATION
        codes = [tag.code for tag in page.tags]
        assert codes.count(_IMAGE_DESCRIPTION_TAG_CODE) == 1

    info = tifftools.read_tiff(str(path))
    icc_tag = info["ifds"][0]["tags"][Tag.ICCProfile.value]
    assert bytes(icc_tag["data"]) == load_icc_profile()

    exif_ifd = info["ifds"][0]["tags"][Tag.ExifIFD.value]["ifds"][0][0]
    exif_tags = exif_ifd["tags"]
    assert exif_tags[36867]["data"] == "2026:08:02 12:33:41"  # DateTimeOriginal
    assert exif_tags[33434]["data"] == [1, 30]  # ExposureTime
    assert exif_tags[33437]["data"] == [8, 1]  # FNumber
    assert exif_tags[34855]["data"] == [100]  # PhotographicSensitivity
    assert exif_tags[37386]["data"] == [55, 1]  # FocalLength
    assert exif_tags[42036]["data"] == "55mm f/2.8"  # LensModel


def test_image_description_names_source_and_count(tmp_path):
    assert stitched_image_description("_DSC4638.NEF", 3) == "_DSC4638.NEF+2: stitched scan"
    assert stitched_image_description("_DSC4644.NEF", 2) == "_DSC4644.NEF+1: stitched scan"

    path = _write(tmp_path, description=stitched_image_description("_DSC4638.NEF", 3))
    with tifffile.TiffFile(path) as tf:
        description = tf.pages[0].tags["ImageDescription"].value
    assert description == "_DSC4638.NEF+2: stitched scan"
