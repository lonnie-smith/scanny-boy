"""Pure mapping from Frame log API shapes to `metadata set` payloads and to
the `framelog_roll` display entries (docs/METADATA_IMPORT_PLAN.md 5.2).

No I/O. A source value that is null, missing or blank means the payload key
is *absent*: an import never clears a field.
"""

from __future__ import annotations

import datetime
import re
from typing import Any

MAX_TEXT = 500

_LOGGED_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
_OFFSET = re.compile(r"[+-]\d{2}:\d{2}")
_US_ADDRESS = re.compile(
    r"^.+,\s*(?P<city>[^,]+?),\s*(?P<state>[A-Z]{2})\s+\d{5}(?:-\d{4})?"
    r"(?:,\s*(?:USA|United States))?$"
)


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped[:MAX_TEXT] or None


def _name(obj: Any) -> str | None:
    return _text(obj.get("name")) if isinstance(obj, dict) else None


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def parse_us_address(address: str | None) -> tuple[str, str] | None:
    """`(city, ST)` from `…, <City>, <ST> <ZIP5>[-<ZIP4>][, USA]`, else
    `None` (no street, non-US, or a state without a ZIP)."""
    if not isinstance(address, str):
        return None
    match = _US_ADDRESS.match(address.strip())
    if match is None:
        return None
    return match["city"].strip(), match["state"]


def _captured(api_frame: dict[str, Any]) -> dict[str, Any] | None:
    captured = api_frame.get("capturedAt")
    return captured if isinstance(captured, dict) else None


def _date_part(local: Any) -> str | None:
    if not isinstance(local, str):
        return None
    try:
        datetime.date.fromisoformat(local[:10])
    except ValueError:
        return None
    return local[:10]


def _place(api_frame: dict[str, Any]) -> dict[str, Any]:
    place = api_frame.get("place")
    return place if isinstance(place, dict) else {}


def _position(api_frame: dict[str, Any]) -> tuple[Any, Any, Any] | None:
    """`(latitude, longitude, accuracy)` when both coordinates are usable."""
    position = api_frame.get("position")
    if not isinstance(position, dict):
        return None
    latitude = _number(position.get("latitude"))
    longitude = _number(position.get("longitude"))
    if latitude is None or longitude is None:
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    accuracy = _number(position.get("accuracyM"))
    return (
        latitude,
        longitude,
        accuracy if accuracy is not None and accuracy >= 0 else None,
    )


def map_frame(api_frame: dict[str, Any]) -> dict[str, Any]:
    """The `metadata set` negative payload for one frame."""
    payload: dict[str, Any] = {"framelog_frame_uid": api_frame["uid"]}
    captured = _captured(api_frame)
    if captured is not None:
        local = captured.get("local")
        if captured.get("precision") == "instant":
            if isinstance(local, str) and _LOGGED_TIME.fullmatch(local[:19]):
                payload["logged_time"] = local[:19]
                offset = captured.get("offset")
                if isinstance(offset, str) and _OFFSET.fullmatch(offset):
                    payload["logged_offset"] = offset
        elif captured.get("precision") == "date":
            date = _date_part(local)
            if date is not None:
                payload["capture_date"] = date
    lens = _name(api_frame.get("lens"))
    if lens:
        payload["lens"] = lens
    caption = _text(api_frame.get("notes"))
    if caption:
        payload["caption"] = caption
    position = _position(api_frame)
    if position is not None:
        payload["latitude"], payload["longitude"] = position[0], position[1]
        if position[2] is not None:
            payload["gps_accuracy_m"] = position[2]
    place = _place(api_frame)
    location = _text(place.get("locationName")) or _text(place.get("placeName"))
    if location:
        payload["location_name"] = location
    address = _text(place.get("address"))
    if address:
        payload["address"] = address
        parsed = parse_us_address(address)
        if parsed is not None:
            payload["city"], payload["state"] = parsed
    return payload


def map_roll(api_roll: dict[str, Any], frames: list[dict[str, Any]]) -> dict[str, Any]:
    """The `metadata set` roll payload."""
    payload: dict[str, Any] = {}
    film = _name(api_roll.get("film"))
    if film:
        payload["film"] = film
    camera = _name(api_roll.get("camera"))
    if camera:
        payload["camera"] = camera
    iso = _number(api_roll.get("iso"))
    if iso is not None:
        payload["iso"] = str(int(iso) if float(iso).is_integer() else iso)
    payload["framelog_roll_uid"] = api_roll["uid"]
    roll_number = api_roll.get("rollNumber")
    if type(roll_number) is int:
        payload["framelog_roll_number"] = roll_number
    for frame in sorted(frames, key=lambda f: f["frameNumber"]):
        captured = _captured(frame)
        date = _date_part(captured.get("local")) if captured else None
        if date is not None:
            payload["capture_date"] = date
            break
    return payload


def display_frame(api_frame: dict[str, Any]) -> dict[str, Any]:
    """One `framelog_roll` frame entry."""
    captured = _captured(api_frame) or {}
    place = _place(api_frame)
    address = _text(place.get("address"))
    parsed = parse_us_address(address)
    precision = captured.get("precision")
    local = captured.get("local")
    return {
        "uid": api_frame["uid"],
        "frame_number": api_frame["frameNumber"],
        "captured_local": local if isinstance(local, str) else None,
        "precision": precision if precision in ("instant", "date") else None,
        "offset": captured.get("offset")
        if isinstance(captured.get("offset"), str)
        else None,
        "lens": _name(api_frame.get("lens")),
        "caption": _text(api_frame.get("notes")),
        "location_name": _text(place.get("locationName"))
        or _text(place.get("placeName")),
        "address": address,
        "city": parsed[0] if parsed else None,
        "state": parsed[1] if parsed else None,
        "has_position": _position(api_frame) is not None,
    }
