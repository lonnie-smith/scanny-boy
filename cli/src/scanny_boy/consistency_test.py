from fractions import Fraction

import pytest

from scanny_boy.consistency import ConsistencyError, check_consistency
from scanny_boy.events import Code
from scanny_boy.metadata import SourceSettings

GOOD_WB = (1.691406, 1.0, 1.378906, 1.0)


def _settings(filename: str, **overrides) -> SourceSettings:
    defaults = {
        "filename": filename,
        "exposure_time": Fraction(1, 30),
        "f_number": Fraction(8, 1),
        "iso": 100,
        "focal_length": Fraction(55, 1),
        "lens_model": "55mm f/2.8",
        "orientation": 1,
        "camera_whitebalance": GOOD_WB,
        "make": "NIKON CORPORATION",
        "model": "NIKON Z f",
    }
    defaults.update(overrides)
    return SourceSettings(**defaults)


def test_uniform_settings_pass_with_no_warnings():
    settings_list = [_settings("a.NEF"), _settings("b.NEF"), _settings("c.NEF")]

    result = check_consistency(settings_list)

    assert result.warnings == []


def test_mismatched_exposure_time_passes_with_no_warnings():
    # Exposure time is required per file but deliberately not compared
    # across the selection; exposure may differ across a roll.
    settings_list = [
        _settings("a.NEF"),
        _settings("b.NEF", exposure_time=Fraction(1, 60)),
    ]

    result = check_consistency(settings_list)

    assert result.warnings == []


@pytest.mark.parametrize(
    ("field", "differing_value", "field_label"),
    [
        ("f_number", Fraction(4, 1), "aperture"),
        ("iso", 200, "ISO"),
        ("focal_length", Fraction(85, 1), "focal length"),
        ("orientation", 3, "source orientation"),
    ],
)
def test_required_field_mismatch_names_the_differing_file(field, differing_value, field_label):
    settings_list = [
        _settings("a.NEF"),
        _settings("b.NEF", **{field: differing_value}),
    ]

    with pytest.raises(ConsistencyError) as excinfo:
        check_consistency(settings_list)

    assert excinfo.value.code == Code.CAPTURE_SETTINGS_DIFFER
    assert "b.NEF" in excinfo.value.message
    assert field_label in excinfo.value.message


@pytest.mark.parametrize(
    "field",
    ["exposure_time", "f_number", "iso", "focal_length", "orientation"],
)
def test_required_field_missing_stops_with_capture_metadata_missing(field):
    settings_list = [_settings("a.NEF"), _settings("b.NEF", **{field: None})]

    with pytest.raises(ConsistencyError) as excinfo:
        check_consistency(settings_list)

    assert excinfo.value.code == Code.CAPTURE_METADATA_MISSING
    assert "b.NEF" in excinfo.value.message


def test_lens_model_mismatch_among_present_values_stops():
    settings_list = [
        _settings("a.NEF", lens_model="55mm f/2.8"),
        _settings("b.NEF", lens_model="85mm f/1.8"),
    ]

    with pytest.raises(ConsistencyError) as excinfo:
        check_consistency(settings_list)

    assert excinfo.value.code == Code.CAPTURE_SETTINGS_DIFFER
    assert "a.NEF" in excinfo.value.message
    assert "b.NEF" in excinfo.value.message


def test_lens_model_missing_from_one_file_warns_and_continues():
    settings_list = [
        _settings("a.NEF", lens_model="55mm f/2.8"),
        _settings("b.NEF", lens_model=None),
    ]

    result = check_consistency(settings_list)

    assert len(result.warnings) == 1
    assert result.warnings[0].code == Code.CAPTURE_METADATA_MISSING
    assert "b.NEF" in result.warnings[0].message


def test_lens_model_missing_from_every_file_warns_for_each_and_does_not_stop():
    settings_list = [
        _settings("a.NEF", lens_model=None),
        _settings("b.NEF", lens_model=None),
    ]

    result = check_consistency(settings_list)

    assert {w.message for w in result.warnings} == {
        "lens model is not available for a.NEF",
        "lens model is not available for b.NEF",
    }


@pytest.mark.parametrize(
    "bad_wb",
    [
        None,
        (0.0, 1.0, 1.0, 1.0),
        (-1.0, 1.0, 1.0, 1.0),
        (float("nan"), 1.0, 1.0, 1.0),
        (float("inf"), 1.0, 1.0, 1.0),
    ],
)
def test_invalid_white_balance_multipliers_are_rejected(bad_wb):
    settings_list = [_settings("a.NEF"), _settings("b.NEF", camera_whitebalance=bad_wb)]

    with pytest.raises(ConsistencyError) as excinfo:
        check_consistency(settings_list)

    assert excinfo.value.code == Code.CAPTURE_METADATA_MISSING
    assert "b.NEF" in excinfo.value.message


def test_white_balance_within_tolerance_passes():
    close_wb = (1.691406 + 5e-7, 1.0, 1.378906 - 5e-7, 1.0)
    settings_list = [_settings("a.NEF"), _settings("b.NEF", camera_whitebalance=close_wb)]

    result = check_consistency(settings_list)

    assert result.warnings == []


def test_white_balance_beyond_tolerance_names_differing_file():
    different_wb = (2.0, 1.0, 1.0, 1.0)
    settings_list = [_settings("a.NEF"), _settings("b.NEF", camera_whitebalance=different_wb)]

    with pytest.raises(ConsistencyError) as excinfo:
        check_consistency(settings_list)

    assert excinfo.value.code == Code.CAPTURE_SETTINGS_DIFFER
    assert "b.NEF" in excinfo.value.message


def test_white_balance_is_normalised_by_first_green_multiplier():
    # Same ratios, scaled by an arbitrary constant; should still match after
    # normalising by the first green multiplier (g1).
    scaled_wb = tuple(v * 2.0 for v in GOOD_WB)
    settings_list = [_settings("a.NEF"), _settings("b.NEF", camera_whitebalance=scaled_wb)]

    result = check_consistency(settings_list)

    assert result.warnings == []
