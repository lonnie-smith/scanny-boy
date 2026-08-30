"""Synthetic film-date ordering times.

The user supplies a film date but not an original capture time (section
3.5). This computes one synthetic `DateTimeOriginal` per output frame: noon
on the film date, then each frame's elapsed scan time relative to the first
frame — or, when canonical sorting fell back to filename order (no
catalogue-wide capture timestamps to trust), noon plus one second per
frame. Either way, each computed time is nudged to be at least one second
after the previous one, so tied or reversed source timestamps come out
strictly ordered; and if any computed time would leave the film date (pass
midnight), the whole computation fails with `CAPTURE_SPAN_TOO_LONG` rather
than silently rolling onto the next calendar day.

Noon is deliberate (section 3.5): it leaves twelve hours of headroom, far
more than any realistic copy-stand session, and keeps the timestamp away
from a day boundary so a viewer in another time zone still shows the
correct film date.
"""

from __future__ import annotations

import datetime

from scanny_boy.catalogue import CaptureTimestamp

NOON = datetime.time(12, 0, 0)
MIN_GAP = datetime.timedelta(seconds=1)

# Retired with Phase 3; kept as a string until `film_date.py` is deleted in P3-5.
CAPTURE_SPAN_TOO_LONG = "CAPTURE_SPAN_TOO_LONG"


class FilmDateError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _precise(ts: CaptureTimestamp) -> datetime.datetime:
    """`ts.when` plus its subsecond fraction, as one comparable instant."""
    return ts.when + datetime.timedelta(seconds=ts.subsec_fraction)


def _finalize(
    candidates: list[datetime.datetime], film_date: datetime.date
) -> list[datetime.datetime]:
    times: list[datetime.datetime] = []
    for candidate in candidates:
        if times and candidate < times[-1] + MIN_GAP:
            candidate = times[-1] + MIN_GAP
        times.append(candidate)

    for t in times:
        if t.date() != film_date:
            raise FilmDateError(
                CAPTURE_SPAN_TOO_LONG,
                f"a synthetic ordering time ({t.isoformat()}) would leave "
                f"the film date {film_date.isoformat()}; split the run",
            )

    return times


def synthetic_times_from_capture(
    film_date: datetime.date, capture_timestamps: list[CaptureTimestamp]
) -> list[datetime.datetime]:
    """Noon on `film_date`, then each frame's elapsed scan time relative to
    the first frame. `capture_timestamps` must already be in canonical
    (chronological) order — the order the selection was made in."""
    if not capture_timestamps:
        return []
    start = datetime.datetime.combine(film_date, NOON)
    first = _precise(capture_timestamps[0])
    candidates = [start + (_precise(ts) - first) for ts in capture_timestamps]
    return _finalize(candidates, film_date)


def synthetic_times_from_filename_fallback(
    film_date: datetime.date, count: int
) -> list[datetime.datetime]:
    """Noon on `film_date`, plus one second per frame — used when canonical
    sorting fell back to filename order and per-frame capture timestamps
    can't be trusted for ordering."""
    start = datetime.datetime.combine(film_date, NOON)
    candidates = [start + i * MIN_GAP for i in range(count)]
    return _finalize(candidates, film_date)


def format_date_time(dt: datetime.datetime) -> str:
    """EXIF ASCII date/time format: `YYYY:MM:DD HH:MM:SS`."""
    return dt.strftime("%Y:%m:%d %H:%M:%S")


def format_subsec(dt: datetime.datetime) -> str | None:
    """The fractional part of `dt`, formatted like source `SubSecTime*`
    tags (section 3.5: "fractional synthetic time when present"). `None`
    when `dt` falls exactly on the second, matching "omit when absent"."""
    if dt.microsecond == 0:
        return None
    digits = f"{dt.microsecond:06d}".rstrip("0")
    return digits or None
