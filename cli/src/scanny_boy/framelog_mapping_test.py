"""Pure mapping: frames to `metadata set` payloads and display entries."""

from __future__ import annotations

import pytest

from scanny_boy.framelog_mapping import (
    display_frame,
    map_frame,
    map_roll,
    parse_us_address,
)
from scanny_boy.framelog_support import load_fixture


@pytest.fixture(scope="module")
def roll_42():
    data = load_fixture("roll_42")
    return data["roll"], {f["frameNumber"]: f for f in data["frames"]}


def _frame(**overrides):
    frame = {
        "uid": "zzzzzzzzzz",
        "frameNumber": 1,
        "capturedAt": None,
        "lens": None,
        "position": None,
        "place": {
            "locationId": None,
            "locationName": None,
            "locationTag": None,
            "placeName": None,
            "address": None,
        },
        "notes": None,
    }
    frame.update(overrides)
    return frame


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("735 N State St, Chicago, IL 60654", ("Chicago", "IL")),
        ("233 S Wacker Dr, Chicago, IL 60606-4611", ("Chicago", "IL")),
        ("111 S Michigan Ave, Chicago, IL 60603, USA", ("Chicago", "IL")),
        ("1 Main St, Portland, OR 97201, United States", ("Portland", "OR")),
        ("1 Main St, Apt 4, Portland, OR 97201", ("Portland", "OR")),
        ("12 Rue de Rivoli, 75004 Paris, France", None),
        ("Chicago, IL 60654", None),
        ("1 Main St, Chicago, IL", None),
        ("1 Main St, Chicago, Il 60654", None),
        ("1 Main St, Chicago, IL 6065", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_us_address(address, expected):
    assert parse_us_address(address) == expected


def test_map_frame_instant_with_gps_and_us_address(roll_42):
    assert map_frame(roll_42[1][1]) == {
        "framelog_frame_uid": "a8f2m4q9zz",
        "logged_time": "2026-10-01T10:10:00",
        "logged_offset": "-05:00",
        "lens": "Nikkor 50mm f/1.8",
        "caption": "f/8 1/125",
        "latitude": 41.8781,
        "longitude": -87.6298,
        "gps_accuracy_m": 12,
        "location_name": "Holy Name Cathedral",
        "address": "735 N State St, Chicago, IL 60654",
        "city": "Chicago",
        "state": "IL",
    }


def test_map_frame_date_precision_sets_capture_date_only(roll_42):
    payload = map_frame(roll_42[1][2])
    assert payload["capture_date"] == "2026-10-02"
    assert "logged_time" not in payload and "logged_offset" not in payload
    assert "latitude" not in payload


def test_map_frame_all_null_is_only_the_uid(roll_42):
    assert map_frame(roll_42[1][3]) == {"framelog_frame_uid": "c4d9e1f6gh"}


def test_map_frame_non_us_address_has_no_city_or_state(roll_42):
    payload = map_frame(roll_42[1][5])
    assert payload["address"] == "12 Rue de Rivoli, 75004 Paris, France"
    assert "city" not in payload and "state" not in payload
    # locationName is null, so the place name is used.
    assert payload["location_name"] == "Rue de Rivoli"
    assert payload["logged_offset"] == "+02:00"


def test_map_frame_location_source_position_and_usa_suffix(roll_42):
    payload = map_frame(roll_42[1][6])
    assert payload["latitude"] == 41.8796
    assert payload["gps_accuracy_m"] == 150
    assert (payload["city"], payload["state"]) == ("Chicago", "IL")


def test_map_frame_blank_notes_and_zip4(roll_42):
    payload = map_frame(roll_42[1][7])
    assert "caption" not in payload
    assert "lens" not in payload
    assert "location_name" not in payload
    assert (payload["city"], payload["state"]) == ("Chicago", "IL")


def test_map_frame_instant_with_null_offset_omits_the_offset():
    frame = _frame(
        capturedAt={
            "utc": None,
            "local": "2026-10-01T10:10:00",
            "offset": None,
            "precision": "instant",
        }
    )
    assert map_frame(frame) == {
        "framelog_frame_uid": "zzzzzzzzzz",
        "logged_time": "2026-10-01T10:10:00",
    }


def test_map_frame_falls_back_to_place_name_and_strips():
    frame = _frame(
        place={"locationName": "  ", "placeName": " Cloud Gate ", "address": None},
        notes="  hello  ",
    )
    payload = map_frame(frame)
    assert payload["location_name"] == "Cloud Gate"
    assert payload["caption"] == "hello"


def test_map_frame_prefers_location_name_over_place_name():
    frame = _frame(place={"locationName": "A", "placeName": "B", "address": None})
    assert map_frame(frame)["location_name"] == "A"


def test_map_frame_drops_unusable_position_and_accuracy():
    frame = _frame(
        position={"latitude": 95, "longitude": 10, "accuracyM": 3, "source": "gps"}
    )
    assert "latitude" not in map_frame(frame)
    frame = _frame(
        position={"latitude": 10, "longitude": 10, "accuracyM": None, "source": "gps"}
    )
    payload = map_frame(frame)
    assert payload["latitude"] == 10 and "gps_accuracy_m" not in payload


def test_map_roll(roll_42):
    assert map_roll(roll_42[0], list(roll_42[1].values())) == {
        "film": "Kodak Portra 400",
        "camera": "Nikon FM2",
        "iso": "400",
        "framelog_roll_uid": "k3j9x0a1bc",
        "framelog_roll_number": 42,
        "capture_date": "2026-10-01",
    }


def test_map_roll_omits_null_values_and_skips_frames_without_a_time():
    roll = {
        "uid": "abcdefghij",
        "rollNumber": 3,
        "film": None,
        "camera": None,
        "iso": None,
    }
    frames = [
        _frame(frameNumber=1),
        _frame(
            frameNumber=2,
            capturedAt={"local": "2026-05-06", "offset": None, "precision": "date"},
        ),
    ]
    assert map_roll(roll, frames) == {
        "framelog_roll_uid": "abcdefghij",
        "framelog_roll_number": 3,
        "capture_date": "2026-05-06",
    }
    assert "capture_date" not in map_roll(roll, [_frame()])


def test_display_frame(roll_42):
    shown = display_frame(roll_42[1][1])
    assert shown == {
        "uid": "a8f2m4q9zz",
        "frame_number": 1,
        "captured_local": "2026-10-01T10:10:00",
        "precision": "instant",
        "offset": "-05:00",
        "lens": "Nikkor 50mm f/1.8",
        "caption": "f/8 1/125",
        "location_name": "Holy Name Cathedral",
        "address": "735 N State St, Chicago, IL 60654",
        "city": "Chicago",
        "state": "IL",
        "has_position": True,
    }
    empty = display_frame(roll_42[1][3])
    assert empty["captured_local"] is None and empty["precision"] is None
    assert empty["has_position"] is False and empty["caption"] is None
    assert display_frame(roll_42[1][2])["precision"] == "date"
    assert display_frame(roll_42[1][7])["caption"] is None
