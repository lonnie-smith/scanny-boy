import datetime

import numpy as np
import pytest
import tifffile

from scanny_boy.icc_profile import load_icc_profile
from scanny_boy.tiff_writer import (
    DEFLATE_COMPRESSION_CODE,
    HORIZONTAL_PREDICTOR,
    OUTPUT_ORIENTATION,
    BaseTiffTags,
    image_description,
    software_tag_value,
    write_base_tiff,
)

# Deliberately naive: the TIFF DateTime tag has no timezone component, and
# this is only the fixed "conversion happened at" value used in these tests.
CONVERSION_TIME = datetime.datetime(2026, 8, 28, 10, 0, 0)  # noqa: DTZ001


def _gradient_pixels(height: int = 8, width: int = 12, seed: int = 0) -> np.ndarray:
    # Per section 7: synthetic test images must not be pure random noise —
    # Deflate cannot compress it and a full frame takes minutes. A gradient
    # with light noise compresses normally and stays fast.
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
        "description": image_description("_DSC4638.NEF"),
        "software": software_tag_value(),
        "conversion_time": CONVERSION_TIME,
        "icc_profile": load_icc_profile(),
        "make": "NIKON CORPORATION",
        "model": "NIKON Z f",
    }
    defaults.update(overrides)
    return BaseTiffTags(**defaults)


def test_round_trip_preserves_pixels_shape_dtype_and_channels(tmp_path):
    pixels = _gradient_pixels()
    path = tmp_path / "a.tif"

    write_base_tiff(path, pixels, _tags())

    read_back = tifffile.imread(path)
    assert read_back.dtype == np.uint16
    assert read_back.shape == pixels.shape
    np.testing.assert_array_equal(read_back, pixels)


def test_compression_code_and_predictor(tmp_path):
    path = tmp_path / "a.tif"
    write_base_tiff(path, _gradient_pixels(), _tags())

    with tifffile.TiffFile(path) as tf:
        page = tf.pages[0]
        assert page.tags["Compression"].value == DEFLATE_COMPRESSION_CODE
        assert page.tags["Predictor"].value == HORIZONTAL_PREDICTOR


def test_orientation_is_always_one(tmp_path):
    path = tmp_path / "a.tif"
    write_base_tiff(path, _gradient_pixels(), _tags())

    with tifffile.TiffFile(path) as tf:
        assert int(tf.pages[0].tags["Orientation"].value) == OUTPUT_ORIENTATION == 1


def test_image_description_and_software_written_exactly_once(tmp_path):
    path = tmp_path / "a.tif"
    tags = _tags(description=image_description("_DSC4640.NEF"))
    write_base_tiff(path, _gradient_pixels(), tags)

    with tifffile.TiffFile(path) as tf:
        codes = [tag.code for tag in tf.pages[0].tags]
        assert codes.count(270) == 1  # ImageDescription
        assert codes.count(305) == 1  # Software
        page = tf.pages[0]
        assert page.tags["ImageDescription"].value == "_DSC4640.NEF: unstitched scan frame"
        assert page.tags["Software"].value == tags.software


def test_make_and_model_written_when_provided(tmp_path):
    path = tmp_path / "a.tif"
    write_base_tiff(path, _gradient_pixels(), _tags())

    with tifffile.TiffFile(path) as tf:
        page = tf.pages[0]
        assert page.tags["Make"].value == "NIKON CORPORATION"
        assert page.tags["Model"].value == "NIKON Z f"


def test_make_and_model_omitted_when_absent(tmp_path):
    path = tmp_path / "a.tif"
    write_base_tiff(path, _gradient_pixels(), _tags(make=None, model=None))

    with tifffile.TiffFile(path) as tf:
        page = tf.pages[0]
        assert "Make" not in page.tags
        assert "Model" not in page.tags


def test_icc_profile_embedded_verbatim(tmp_path):
    path = tmp_path / "a.tif"
    profile = load_icc_profile()
    write_base_tiff(path, _gradient_pixels(), _tags(icc_profile=profile))

    with tifffile.TiffFile(path) as tf:
        assert tf.pages[0].tags["InterColorProfile"].value == profile


@pytest.mark.parametrize("empty", [b"", None])
def test_refuses_to_write_without_icc_profile(tmp_path, empty):
    path = tmp_path / "a.tif"
    with pytest.raises(ValueError, match="ICC profile"):
        write_base_tiff(path, _gradient_pixels(), _tags(icc_profile=empty))


def test_refuses_wrong_shape_or_dtype(tmp_path):
    path = tmp_path / "a.tif"
    wrong_dtype = _gradient_pixels().astype(np.uint8)
    with pytest.raises(ValueError, match="uint16"):
        write_base_tiff(path, wrong_dtype, _tags())

    wrong_channels = _gradient_pixels()[:, :, :2]
    with pytest.raises(ValueError, match="uint16"):
        write_base_tiff(path, wrong_channels, _tags())


def test_software_tag_value_reads_package_version():
    assert software_tag_value().startswith("Scanny Boy ")


def test_image_description_names_source_and_marks_unstitched():
    text = image_description("_DSC4638.NEF")
    assert "_DSC4638.NEF" in text
    assert "unstitched scan frame" in text
