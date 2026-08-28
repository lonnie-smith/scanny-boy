from fractions import Fraction

import pytest

from scanny_boy.fake_nef_support import write_fake_nef
from scanny_boy.metadata import (
    DigitizationSourceFields,
    UnreadableRawError,
    UnsupportedRawError,
    choose_digitized_fields,
    read_camera_whitebalance,
    read_digitization_fields,
    read_exif_settings,
)
from scanny_boy.sample_nef_support import (
    FIXTURES_DIR,
    REAL_SAMPLE_FILES,
    requires_real_samples,
)


def test_read_exif_settings_reads_expected_values(tmp_path):
    path = write_fake_nef(
        tmp_path / "a.NEF",
        exposure_time=(1, 30),
        f_number=(8, 1),
        iso=100,
        focal_length=(55, 1),
        lens_model="55mm f/2.8",
        orientation=1,
    )

    settings = read_exif_settings(path)

    assert settings.exposure_time == Fraction(1, 30)
    assert settings.f_number == Fraction(8, 1)
    assert settings.iso == 100
    assert settings.focal_length == Fraction(55, 1)
    assert settings.lens_model == "55mm f/2.8"
    assert settings.orientation == 1


@pytest.mark.parametrize(
    "field",
    ["exposure_time", "f_number", "iso", "focal_length", "orientation", "lens_model"],
)
def test_read_exif_settings_returns_none_for_missing_tag(tmp_path, field):
    path = write_fake_nef(tmp_path / "a.NEF", **{field: None})

    settings = read_exif_settings(path)

    assert getattr(settings, field) is None


def test_read_exif_settings_ignores_missing_tags_independently(tmp_path):
    path = write_fake_nef(tmp_path / "a.NEF", lens_model=None)

    settings = read_exif_settings(path)

    assert settings.lens_model is None
    assert settings.exposure_time == Fraction(1, 30)


def test_read_camera_whitebalance_maps_garbage_file_to_unreadable_raw(tmp_path):
    # A file rawpy simply cannot parse at all, exercising the real error
    # path rather than mocking rawpy's decoding — see
    # IMPLEMENTATION_PLAN.md section 7.
    path = tmp_path / "garbage.NEF"
    path.write_bytes(b"not a raw file at all")

    with pytest.raises(UnreadableRawError):
        read_camera_whitebalance(path)


def test_read_camera_whitebalance_maps_non_raw_tiff_to_unsupported_raw(tmp_path):
    # A well-formed TIFF that LibRaw recognises as a file format but not a
    # RAW file — the real `fake_nef_support` fixture LibRaw actually
    # rejects this way. A genuine HE/HE* NEF (the real-world trigger for
    # UNSUPPORTED_RAW) has no sample file to test against, but this
    # exercises the same rawpy exception and the same mapping.
    path = write_fake_nef(tmp_path / "a.NEF")

    with pytest.raises(UnsupportedRawError):
        read_camera_whitebalance(path)


def test_read_digitization_fields_reads_all_six_raw_strings(tmp_path):
    path = write_fake_nef(
        tmp_path / "a.NEF",
        date_time_original="2026:08:02 12:33:27",
        subsec_time_original="77",
        offset_time_original="-05:00",
        date_time_digitized="2026:08:02 12:33:26",
        subsec_time_digitized="50",
        offset_time_digitized="-06:00",
    )

    fields = read_digitization_fields(path)

    assert fields.date_time_original == "2026:08:02 12:33:27"
    assert fields.subsec_time_original == "77"
    assert fields.offset_time_original == "-05:00"
    assert fields.date_time_digitized == "2026:08:02 12:33:26"
    assert fields.subsec_time_digitized == "50"
    assert fields.offset_time_digitized == "-06:00"


def test_read_digitization_fields_returns_none_for_missing_tags(tmp_path):
    path = write_fake_nef(tmp_path / "a.NEF", date_time_original=None, subsec_time_original=None)

    fields = read_digitization_fields(path)

    assert fields.date_time_original is None
    assert fields.subsec_time_original is None
    assert fields.offset_time_original is None
    assert fields.date_time_digitized is None
    assert fields.subsec_time_digitized is None
    assert fields.offset_time_digitized is None


def test_choose_digitized_fields_prefers_date_time_original(tmp_path):
    path = write_fake_nef(
        tmp_path / "a.NEF",
        date_time_original="2026:08:02 12:33:27",
        subsec_time_original="77",
        offset_time_original="-05:00",
        date_time_digitized="2026:08:02 12:33:26",
        subsec_time_digitized="50",
        offset_time_digitized="-06:00",
    )

    chosen = choose_digitized_fields(read_digitization_fields(path))

    assert chosen.date_time_digitized == "2026:08:02 12:33:27"
    assert chosen.subsec_time_digitized == "77"
    assert chosen.offset_time_digitized == "-05:00"


def test_choose_digitized_fields_falls_back_to_source_digitized(tmp_path):
    path = write_fake_nef(
        tmp_path / "a.NEF",
        date_time_original=None,
        subsec_time_original=None,
        date_time_digitized="2026:08:02 12:33:26",
        subsec_time_digitized="50",
        offset_time_digitized="-06:00",
    )

    chosen = choose_digitized_fields(read_digitization_fields(path))

    assert chosen.date_time_digitized == "2026:08:02 12:33:26"
    assert chosen.subsec_time_digitized == "50"
    assert chosen.offset_time_digitized == "-06:00"


def test_choose_digitized_fields_never_invents_an_offset():
    # DateTimeOriginal is present, so that branch is chosen, but its own
    # offset is absent — must stay absent, never borrow the Digitized
    # branch's offset (section 3.5: "Never invent an offset for the
    # synthetic film time").
    source = DigitizationSourceFields(
        date_time_original="2026:08:02 12:33:27",
        subsec_time_original="77",
        offset_time_original=None,
        date_time_digitized="2026:08:02 12:33:26",
        subsec_time_digitized="50",
        offset_time_digitized="-06:00",
    )

    chosen = choose_digitized_fields(source)

    assert chosen.date_time_digitized == "2026:08:02 12:33:27"
    assert chosen.offset_time_digitized is None


@requires_real_samples
def test_real_sample_files_digitization_fields_match_chunk_2_dump():
    # Values recorded in the Chunk 2 pull-request body's tag dump: every
    # sample file's DateTimeDigitized/SubSecTimeDigitized/OffsetTime*
    # tags mirror its DateTimeOriginal/SubSecTimeOriginal/OffsetTimeOriginal
    # exactly, so `choose_digitized_fields` picks the DateTimeOriginal
    # branch and reproduces the same values either way.
    for name in REAL_SAMPLE_FILES:
        fields = read_digitization_fields(FIXTURES_DIR / name)
        assert fields.offset_time_original == "-05:00"
        assert fields.date_time_digitized == fields.date_time_original
        assert fields.subsec_time_digitized == fields.subsec_time_original
        assert fields.offset_time_digitized == fields.offset_time_original

        chosen = choose_digitized_fields(fields)
        assert chosen.date_time_digitized == fields.date_time_original
        assert chosen.subsec_time_digitized == fields.subsec_time_original
        assert chosen.offset_time_digitized == "-05:00"


@requires_real_samples
def test_real_sample_files_have_every_section_3_5_tag():
    for name in REAL_SAMPLE_FILES:
        settings = read_exif_settings(FIXTURES_DIR / name)
        assert settings.exposure_time == Fraction(1, 30)
        assert settings.f_number == Fraction(8, 1)
        assert settings.iso == 100
        assert settings.focal_length == Fraction(55, 1)
        assert settings.lens_model == "55mm f/2.8"
        assert settings.orientation == 1


@requires_real_samples
def test_real_sample_files_camera_whitebalance_matches_appendix_a():
    for name in REAL_SAMPLE_FILES:
        wb = read_camera_whitebalance(FIXTURES_DIR / name)
        assert wb is not None
        assert wb == pytest.approx((1.691406, 1.0, 1.378906, 1.0), abs=1e-6)
