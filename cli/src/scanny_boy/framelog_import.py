"""The three `framelog` command runners (docs/METADATA_IMPORT_PLAN.md 5.4).

Each raises `FrameLogError` (transport/API failures) or
`MetadataEditFailure` (roll and assignment validation) and never writes
partial state: `apply` builds one payload and hands it to
`metadata_edit.run_metadata_set`, which is atomic.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from scanny_boy.events import Code
from scanny_boy.framelog_align import default_alignment, last_frame
from scanny_boy.framelog_client import (
    FrameLogClient,
    token_from_environment,
)
from scanny_boy.framelog_mapping import display_frame, map_frame, map_roll
from scanny_boy.metadata_edit import (
    MetadataEditFailure,
    _validated_roll,
    run_metadata_set,
)
from scanny_boy.roll_sequence import sequence_negatives

Opener = Callable[..., Any] | None

_ROLL_LIST_FIELDS = (
    ("uid", "uid"),
    ("roll_number", "rollNumber"),
    ("film", "film"),
    ("camera", "camera"),
    ("iso", "iso"),
    ("created_at", "createdAt"),
    ("finished_at", "finishedAt"),
    ("frame_count", "frameCount"),
    ("last_frame", "lastFrame"),
    ("first_captured_at", "firstCapturedAt"),
    ("last_captured_at", "lastCapturedAt"),
)


def _client(base_url: str, environ: Mapping[str, str] | None, opener: Opener):
    # The token check comes first: no request is built without one.
    return FrameLogClient(base_url, token_from_environment(environ), opener=opener)


def _flat_name(value: Any) -> Any:
    return value.get("name") if isinstance(value, dict) else value


def run_framelog_rolls(
    status: str,
    base_url: str,
    *,
    environ: Mapping[str, str] | None = None,
    opener: Opener = None,
) -> list[dict[str, Any]]:
    client = _client(base_url, environ, opener)
    return [
        {name: api.get(key) for name, key in _ROLL_LIST_FIELDS}
        for api in client.list_rolls(status)
    ]


def run_framelog_roll(
    roll_dir: Path,
    ref: str,
    base_url: str,
    *,
    environ: Mapping[str, str] | None = None,
    opener: Opener = None,
) -> dict[str, Any]:
    """The `framelog_roll` event's fields: roll, frames, alignment, linked."""
    client = _client(base_url, environ, opener)
    manifest = _validated_roll(roll_dir)
    api_roll, frames = client.get_roll(ref)
    missing = api_roll.get("missingFrameNumbers")
    return {
        "roll": {
            "uid": api_roll["uid"],
            "roll_number": api_roll.get("rollNumber"),
            "film": _flat_name(api_roll.get("film")),
            "camera": _flat_name(api_roll.get("camera")),
            "iso": api_roll.get("iso"),
            "created_at": api_roll.get("createdAt"),
            "finished_at": api_roll.get("finishedAt"),
            "frame_count": api_roll.get("frameCount"),
            "last_frame": last_frame(api_roll),
            "missing_frame_numbers": missing if isinstance(missing, list) else [],
        },
        "frames": [display_frame(frame) for frame in frames],
        "alignment": [
            {"negative_id": negative_id, "frame_number": number}
            for negative_id, number in default_alignment(manifest, api_roll, frames)
        ],
        "linked": manifest.metadata.framelog_roll_uid == api_roll["uid"],
    }


def _invalid(message: str) -> MetadataEditFailure:
    return MetadataEditFailure(Code.INVALID_METADATA, message)


def _parse_assignments(raw: str) -> dict[str, Any]:
    try:
        assignments = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _invalid(f"--assignments is not valid JSON: {exc}") from None
    if not isinstance(assignments, dict):
        raise _invalid("--assignments must be a JSON object")
    return assignments


def run_framelog_apply(
    roll_dir: Path,
    ref: str,
    assignments_json: str,
    base_url: str,
    *,
    environ: Mapping[str, str] | None = None,
    opener: Opener = None,
    emit: Any = None,
) -> dict[str, Any]:
    """Validates `{negative_id: frame_number | null}`, re-fetches the roll,
    and writes one atomic `metadata set` payload. Returns the manifest dict
    (the `metadata_updated` event's `manifest`)."""
    client = _client(base_url, environ, opener)
    assignments = _parse_assignments(assignments_json)
    manifest = _validated_roll(roll_dir)
    by_id = {n.negative_id: n for n in manifest.negatives}

    # Checks that need no fetch run first. A negative the roll lacks, or one
    # that is not sequenced, cannot be aligned.
    order = [nid for nid in sequence_negatives(manifest) if nid in assignments]
    for negative_id in assignments:
        if negative_id not in by_id:
            raise MetadataEditFailure(
                Code.NEGATIVE_NOT_FOUND,
                f"{negative_id} is not a negative of this roll",
            )
        if negative_id not in order:
            raise _invalid(f"negative {negative_id!r} is not part of the roll sequence")
    for negative_id, number in assignments.items():
        if number is not None and (isinstance(number, bool) or type(number) is not int):
            raise _invalid(f"the frame for {negative_id!r} must be an integer or null")
    previous = 0
    for negative_id in order:
        number = assignments[negative_id]
        if number is None:
            continue
        if number <= previous:
            raise _invalid(
                "frame numbers must strictly increase in sequence order "
                f"({negative_id!r} has {number} after {previous})"
            )
        previous = number

    api_roll, frames = client.get_roll(ref)
    limit = last_frame(api_roll)
    for negative_id, number in assignments.items():
        if number is not None and not 1 <= number <= limit:
            raise _invalid(f"frame {number} for {negative_id!r} is outside 1..{limit}")

    frame_by_number = {frame["frameNumber"]: frame for frame in frames}
    negatives: dict[str, dict[str, Any]] = {}
    for negative_id, number in assignments.items():
        frame = frame_by_number.get(number) if number is not None else None
        if frame is not None:
            negatives[negative_id] = map_frame(frame)
        elif by_id[negative_id].framelog_frame_uid is not None:
            negatives[negative_id] = {"framelog_frame_uid": None}

    payload = {"roll": map_roll(api_roll, frames), "negatives": negatives}
    return run_metadata_set(roll_dir, payload, emit=emit)
