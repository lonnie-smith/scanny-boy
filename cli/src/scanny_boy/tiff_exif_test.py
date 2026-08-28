import datetime
import hashlib
from fractions import Fraction
from pathlib import Path

import exifread
import numpy as np
import pytest
import tifffile
import tifftools
from tifftools.constants import Tag

from scanny_boy.icc_profile import load_icc_profile
from scanny_boy.metadata import read_exif_settings
from scanny_boy.sample_nef_support import FIXTURES_DIR, requires_real_samples
from scanny_boy.tiff_exif import (
    COLOR_SPACE,
    DATE_TIME_DIGITIZED,
    DATE_TIME_ORIGINAL,
    EXPOSURE_TIME,
    F_NUMBER,
    FOCAL_LENGTH,
    LENS_MODEL,
    OFFSET_TIME_DIGITIZED,
    PHOTOGRAPHIC_SENSITIVITY,
    SUBSEC_TIME_DIGITIZED,
    SUBSEC_TIME_ORIGINAL,
    UNCALIBRATED_COLOR_SPACE,
    NestedExifFields,
    TiffFinalizeError,
    build_exif_tags,
    finalize_tiff,
    write_nested_exif,
)
from scanny_boy.tiff_writer import (
    DEFLATE_COMPRESSION_CODE,
    HORIZONTAL_PREDICTOR,
    OUTPUT_ORIENTATION,
    BaseTiffTags,
    image_description,
    software_tag_value,
    write_base_tiff,
)

# Deliberately naive: only used as the base TIFF's "conversion happened at"
# value, which carries no timezone.
CONVERSION_TIME = datetime.datetime(2026, 8, 28, 10, 0, 0)  # noqa: DTZ001

SYNTHETIC_TIME_WITH_SUBSEC = datetime.datetime(2026, 8, 2, 12, 33, 41, 450000)  # noqa: DTZ001
SYNTHETIC_TIME_ON_THE_SECOND = datetime.datetime(2026, 8, 2, 12, 0, 0)  # noqa: DTZ001


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


def _pixel_hash(pixels: np.ndarray) -> str:
    return hashlib.sha256(pixels.tobytes()).hexdigest()


def _base_tags(**overrides) -> BaseTiffTags:
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


def _exif_fields(**overrides) -> NestedExifFields:
    defaults = {
        "date_time_original": SYNTHETIC_TIME_WITH_SUBSEC,
        "exposure_time": Fraction(1, 30),
        "f_number": Fraction(8, 1),
        "iso": 100,
        "focal_length": Fraction(55, 1),
        "lens_model": "55mm f/2.8",
        "date_time_digitized": "2026:08:02 12:33:41",
        "subsec_time_digitized": "45",
        "offset_time_digitized": "-05:00",
    }
    defaults.update(overrides)
    return NestedExifFields(**defaults)


def _write_base(tmp_path: Path, pixels: np.ndarray | None = None, **tag_overrides) -> Path:
    base_path = tmp_path / "a.base.tif"
    if pixels is None:
        pixels = _gradient_pixels()
    write_base_tiff(base_path, pixels, _base_tags(**tag_overrides))
    return base_path


# --- build_exif_tags: pure, no files -----------------------------------


def test_build_exif_tags_required_fields():
    tags = build_exif_tags(_exif_fields())

    assert tags[EXPOSURE_TIME] == {"data": [1, 30], "datatype": tifftools.Datatype.RATIONAL}
    assert tags[F_NUMBER] == {"data": [8, 1], "datatype": tifftools.Datatype.RATIONAL}
    assert tags[PHOTOGRAPHIC_SENSITIVITY] == {"data": [100], "datatype": tifftools.Datatype.SHORT}
    assert tags[FOCAL_LENGTH] == {"data": [55, 1], "datatype": tifftools.Datatype.RATIONAL}


def test_build_exif_tags_color_space_is_always_uncalibrated():
    tags = build_exif_tags(_exif_fields())
    assert tags[COLOR_SPACE] == {
        "data": [UNCALIBRATED_COLOR_SPACE],
        "datatype": tifftools.Datatype.SHORT,
    }
    assert UNCALIBRATED_COLOR_SPACE == 65535


def test_build_exif_tags_date_time_original_and_subsec():
    tags = build_exif_tags(_exif_fields(date_time_original=SYNTHETIC_TIME_WITH_SUBSEC))
    assert tags[DATE_TIME_ORIGINAL] == {
        "data": "2026:08:02 12:33:41",
        "datatype": tifftools.Datatype.ASCII,
    }
    assert tags[SUBSEC_TIME_ORIGINAL] == {"data": "45", "datatype": tifftools.Datatype.ASCII}


def test_build_exif_tags_omits_subsec_on_the_second():
    tags = build_exif_tags(_exif_fields(date_time_original=SYNTHETIC_TIME_ON_THE_SECOND))
    assert SUBSEC_TIME_ORIGINAL not in tags


@pytest.mark.parametrize(
    ("field", "code"),
    [
        ("lens_model", LENS_MODEL),
        ("date_time_digitized", DATE_TIME_DIGITIZED),
        ("subsec_time_digitized", SUBSEC_TIME_DIGITIZED),
        ("offset_time_digitized", OFFSET_TIME_DIGITIZED),
    ],
)
def test_build_exif_tags_omits_optional_fields_when_absent(field, code):
    tags = build_exif_tags(_exif_fields(**{field: None}))
    assert code not in tags


def test_build_exif_tags_includes_optional_fields_when_present():
    tags = build_exif_tags(_exif_fields())
    assert tags[LENS_MODEL] == {"data": "55mm f/2.8", "datatype": tifftools.Datatype.ASCII}
    assert tags[DATE_TIME_DIGITIZED] == {
        "data": "2026:08:02 12:33:41",
        "datatype": tifftools.Datatype.ASCII,
    }
    assert tags[SUBSEC_TIME_DIGITIZED] == {"data": "45", "datatype": tifftools.Datatype.ASCII}
    assert tags[OFFSET_TIME_DIGITIZED] == {"data": "-05:00", "datatype": tifftools.Datatype.ASCII}


# --- write_nested_exif / finalize_tiff: real TIFFs ----------------------


def test_nested_exif_fields_round_trip(tmp_path):
    base_path = _write_base(tmp_path)
    final_path = tmp_path / "a.final.tif"
    fields = _exif_fields()

    write_nested_exif(base_path, final_path, fields)

    info = tifftools.read_tiff(str(final_path))
    exif_ifd = info["ifds"][0]["tags"][Tag.ExifIFD.value]["ifds"][0][0]["tags"]
    assert exif_ifd[DATE_TIME_ORIGINAL]["data"] == "2026:08:02 12:33:41"
    assert exif_ifd[SUBSEC_TIME_ORIGINAL]["data"] == "45"
    assert exif_ifd[EXPOSURE_TIME]["data"] == [1, 30]
    assert exif_ifd[F_NUMBER]["data"] == [8, 1]
    assert exif_ifd[PHOTOGRAPHIC_SENSITIVITY]["data"] == [100]
    assert exif_ifd[FOCAL_LENGTH]["data"] == [55, 1]
    assert exif_ifd[LENS_MODEL]["data"] == "55mm f/2.8"
    assert exif_ifd[COLOR_SPACE]["data"] == [UNCALIBRATED_COLOR_SPACE]
    assert exif_ifd[DATE_TIME_DIGITIZED]["data"] == "2026:08:02 12:33:41"
    assert exif_ifd[SUBSEC_TIME_DIGITIZED]["data"] == "45"
    assert exif_ifd[OFFSET_TIME_DIGITIZED]["data"] == "-05:00"

    # Independent reader (section 7's "development tests" requirement),
    # separate from the tifftools-based checks above.
    with final_path.open("rb") as f:
        exif_tags = exifread.process_file(f, details=False)
    assert str(exif_tags["EXIF DateTimeOriginal"]) == "2026:08:02 12:33:41"
    assert str(exif_tags["EXIF SubSecTimeOriginal"]) == "45"
    assert str(exif_tags["EXIF LensModel"]) == "55mm f/2.8"
    assert str(exif_tags["EXIF FocalLength"]) == "55"


def test_rewrite_preserves_pixels_icc_compression_predictor_dimensions_and_orientation(
    tmp_path,
):
    pixels = _gradient_pixels()
    icc = load_icc_profile()
    base_path = _write_base(tmp_path, pixels=pixels, icc_profile=icc)
    final_path = tmp_path / "a.final.tif"

    write_nested_exif(base_path, final_path, _exif_fields())

    read_back = tifffile.imread(final_path)
    assert read_back.dtype == np.uint16
    assert read_back.shape == pixels.shape
    assert _pixel_hash(read_back) == _pixel_hash(pixels)

    with tifffile.TiffFile(final_path) as tf:
        page = tf.pages[0]
        assert page.tags["Compression"].value == DEFLATE_COMPRESSION_CODE
        assert page.tags["Predictor"].value == HORIZONTAL_PREDICTOR
        assert int(page.tags["Orientation"].value) == OUTPUT_ORIENTATION == 1
        assert page.tags["InterColorProfile"].value == icc


def test_exactly_one_image_description_survives_the_rewrite(tmp_path):
    base_path = _write_base(tmp_path)
    final_path = tmp_path / "a.final.tif"

    write_nested_exif(base_path, final_path, _exif_fields())

    with tifffile.TiffFile(final_path) as tf:
        codes = [tag.code for tag in tf.pages[0].tags]
        assert codes.count(270) == 1  # ImageDescription
        assert tf.pages[0].tags["ImageDescription"].value == image_description(
            "_DSC4638.NEF"
        )


def test_makernotes_and_serial_number_tags_are_absent(tmp_path):
    # Only the fields build_exif_tags constructs are ever written — nothing
    # is copied wholesale from a source file, so there is nothing to check
    # against a real MakerNote here; this proves the nested IFD carries
    # only the documented tag codes.
    base_path = _write_base(tmp_path)
    final_path = tmp_path / "a.final.tif"

    write_nested_exif(base_path, final_path, _exif_fields())

    info = tifftools.read_tiff(str(final_path))
    exif_ifd = info["ifds"][0]["tags"][Tag.ExifIFD.value]["ifds"][0][0]["tags"]
    expected_codes = {
        DATE_TIME_ORIGINAL,
        SUBSEC_TIME_ORIGINAL,
        DATE_TIME_DIGITIZED,
        SUBSEC_TIME_DIGITIZED,
        OFFSET_TIME_DIGITIZED,
        LENS_MODEL,
        EXPOSURE_TIME,
        F_NUMBER,
        PHOTOGRAPHIC_SENSITIVITY,
        FOCAL_LENGTH,
        COLOR_SPACE,
    }
    assert set(exif_ifd.keys()) == expected_codes
    # 37500 == MakerNote; 41483/33421/... are irrelevant here, but this is
    # the specific tag exiftool uses for a body serial number.
    assert 37500 not in exif_ifd
    assert 42033 not in exif_ifd  # BodySerialNumber


def test_finalize_tiff_removes_base_only_after_writing_final(tmp_path):
    base_path = _write_base(tmp_path)
    final_path = tmp_path / "a.final.tif"
    assert base_path.exists()
    assert not final_path.exists()

    finalize_tiff(base_path, final_path, _exif_fields())

    assert not base_path.exists()
    assert final_path.exists()


def test_finalize_tiff_keeps_base_when_verification_fails(tmp_path, monkeypatch):
    base_path = _write_base(tmp_path)
    final_path = tmp_path / "a.final.tif"

    def _write_garbage(base: Path, final: Path, fields: NestedExifFields) -> None:
        final.write_bytes(b"not a tiff at all")

    monkeypatch.setattr("scanny_boy.tiff_exif.write_nested_exif", _write_garbage)

    with pytest.raises(TiffFinalizeError):
        finalize_tiff(base_path, final_path, _exif_fields())

    assert base_path.exists()


def test_repeated_runs_are_equal_excluding_conversion_time(tmp_path):
    # Two independent runs of the same two-pass write for the same source
    # frame, differing only in the base TIFF's `DateTime` (306) conversion
    # timestamp (section 7: "documented changing fields", e.g. the
    # baseline conversion time) — everything else must be identical.
    pixels = _gradient_pixels()
    fields = _exif_fields()

    base_1 = tmp_path / "run1.base.tif"
    final_1 = tmp_path / "run1.final.tif"
    write_base_tiff(base_1, pixels, _base_tags(conversion_time=CONVERSION_TIME))
    write_nested_exif(base_1, final_1, fields)

    base_2 = tmp_path / "run2.base.tif"
    final_2 = tmp_path / "run2.final.tif"
    later = CONVERSION_TIME + datetime.timedelta(hours=1)
    write_base_tiff(base_2, pixels, _base_tags(conversion_time=later))
    write_nested_exif(base_2, final_2, fields)

    read_1 = tifffile.imread(final_1)
    read_2 = tifffile.imread(final_2)
    assert _pixel_hash(read_1) == _pixel_hash(read_2)

    info_1 = tifftools.read_tiff(str(final_1))
    info_2 = tifftools.read_tiff(str(final_2))
    exif_1 = info_1["ifds"][0]["tags"][Tag.ExifIFD.value]["ifds"][0][0]["tags"]
    exif_2 = info_2["ifds"][0]["tags"][Tag.ExifIFD.value]["ifds"][0][0]["tags"]
    assert exif_1 == exif_2

    with tifffile.TiffFile(final_1) as tf1, tifffile.TiffFile(final_2) as tf2:
        page_1, page_2 = tf1.pages[0], tf2.pages[0]
        assert page_1.tags["ImageDescription"].value == page_2.tags["ImageDescription"].value
        assert page_1.tags["Compression"].value == page_2.tags["Compression"].value
        assert page_1.tags["Predictor"].value == page_2.tags["Predictor"].value
        assert page_1.tags["Orientation"].value == page_2.tags["Orientation"].value
        assert page_1.tags["InterColorProfile"].value == page_2.tags["InterColorProfile"].value
        # DateTime (306) is the one documented volatile field.
        assert page_1.tags["DateTime"].value != page_2.tags["DateTime"].value


@requires_real_samples
def test_lens_and_exposure_fields_from_real_sample_are_present_in_final_tiff(tmp_path):
    sample = FIXTURES_DIR / "_DSC4638.NEF"
    settings = read_exif_settings(sample)

    base_path = _write_base(tmp_path)
    final_path = tmp_path / "a.final.tif"
    fields = _exif_fields(
        exposure_time=settings.exposure_time,
        f_number=settings.f_number,
        iso=settings.iso,
        focal_length=settings.focal_length,
        lens_model=settings.lens_model,
    )

    write_nested_exif(base_path, final_path, fields)

    with final_path.open("rb") as f:
        exif_tags = exifread.process_file(f, details=False)
    exposure_ratio = exif_tags["EXIF ExposureTime"].values[0]
    f_number_ratio = exif_tags["EXIF FNumber"].values[0]
    assert str(exif_tags["EXIF LensModel"]) == settings.lens_model
    assert Fraction(exposure_ratio.num, exposure_ratio.den) == settings.exposure_time
    assert Fraction(f_number_ratio.num, f_number_ratio.den) == settings.f_number
