import datetime

import pytest

from scanny_boy.catalogue import CaptureTimestamp
from scanny_boy.film_date import (
    CAPTURE_SPAN_TOO_LONG,
    FilmDateError,
    format_date_time,
    format_subsec,
    synthetic_times_from_capture,
    synthetic_times_from_filename_fallback,
)

FILM_DATE = datetime.date(2026, 8, 2)


def _ts(when: datetime.datetime, subsec: str | None = None) -> CaptureTimestamp:
    fraction = float(f"0.{subsec}") if subsec is not None else 0.0
    return CaptureTimestamp(when=when, subsec_fraction=fraction)


def test_noon_plus_elapsed_preserves_order_across_midnight():
    # The *real* capture session crosses midnight, but the elapsed gap
    # (5 minutes) stays far inside noon's twelve hours of headroom, so the
    # synthetic times stay on the one film date.
    captures = [
        _ts(datetime.datetime(2026, 8, 2, 23, 58, 0)),  # noqa: DTZ001
        _ts(datetime.datetime(2026, 8, 3, 0, 3, 0)),  # noqa: DTZ001
    ]

    times = synthetic_times_from_capture(FILM_DATE, captures)

    assert times[0] == datetime.datetime(2026, 8, 2, 12, 0, 0)  # noqa: DTZ001
    assert times[1] == datetime.datetime(2026, 8, 2, 12, 5, 0)  # noqa: DTZ001
    assert all(t.date() == FILM_DATE for t in times)


def test_elapsed_scan_time_includes_subsecond_precision():
    captures = [
        _ts(datetime.datetime(2026, 8, 2, 12, 33, 27), "77"),  # noqa: DTZ001
        _ts(datetime.datetime(2026, 8, 2, 12, 33, 41), "45"),  # noqa: DTZ001
    ]

    times = synthetic_times_from_capture(FILM_DATE, captures)

    # Elapsed = 13.68s (14s - 0.32s... i.e. (41.45 - 27.77) = 13.68s).
    expected_elapsed = datetime.timedelta(seconds=13, microseconds=680000)
    assert times[1] - times[0] == expected_elapsed


def test_tied_or_reversed_source_timestamps_are_nudged_one_second_apart():
    same_instant = datetime.datetime(2026, 8, 2, 12, 0, 0)  # noqa: DTZ001
    captures = [_ts(same_instant), _ts(same_instant), _ts(same_instant)]

    times = synthetic_times_from_capture(FILM_DATE, captures)

    assert times[0] == datetime.datetime(2026, 8, 2, 12, 0, 0)  # noqa: DTZ001
    assert times[1] == datetime.datetime(2026, 8, 2, 12, 0, 1)  # noqa: DTZ001
    assert times[2] == datetime.datetime(2026, 8, 2, 12, 0, 2)  # noqa: DTZ001


def test_empty_selection_returns_no_times():
    assert synthetic_times_from_capture(FILM_DATE, []) == []


def test_filename_fallback_assigns_noon_plus_one_second_per_frame():
    times = synthetic_times_from_filename_fallback(FILM_DATE, 4)

    assert times == [
        datetime.datetime(2026, 8, 2, 12, 0, 0),  # noqa: DTZ001
        datetime.datetime(2026, 8, 2, 12, 0, 1),  # noqa: DTZ001
        datetime.datetime(2026, 8, 2, 12, 0, 2),  # noqa: DTZ001
        datetime.datetime(2026, 8, 2, 12, 0, 3),  # noqa: DTZ001
    ]


def test_span_too_long_from_capture_fails_with_capture_span_too_long():
    # 13 hours of elapsed scan time pushes the second frame past midnight,
    # off the film date. This can't happen with the real sample files (see
    # appendix A); it needs synthetic timestamps.
    captures = [
        _ts(datetime.datetime(2026, 8, 2, 0, 0, 0)),  # noqa: DTZ001
        _ts(datetime.datetime(2026, 8, 2, 13, 0, 0)),  # noqa: DTZ001
    ]

    with pytest.raises(FilmDateError) as exc_info:
        synthetic_times_from_capture(FILM_DATE, captures)
    assert exc_info.value.code == CAPTURE_SPAN_TOO_LONG


def test_span_too_long_from_filename_fallback_fails_with_capture_span_too_long():
    with pytest.raises(FilmDateError) as exc_info:
        synthetic_times_from_filename_fallback(FILM_DATE, count=13 * 60 * 60)
    assert exc_info.value.code == CAPTURE_SPAN_TOO_LONG


def test_format_date_time_matches_exif_ascii_format():
    dt = datetime.datetime(2026, 8, 2, 12, 33, 27)  # noqa: DTZ001
    assert format_date_time(dt) == "2026:08:02 12:33:27"


@pytest.mark.parametrize(
    ("microsecond", "expected"),
    [
        (0, None),
        (770000, "77"),
        (450000, "45"),
        (1, "000001"),
        (500000, "5"),
    ],
)
def test_format_subsec(microsecond, expected):
    dt = datetime.datetime(2026, 8, 2, 12, 0, 0, microsecond)  # noqa: DTZ001
    assert format_subsec(dt) == expected
