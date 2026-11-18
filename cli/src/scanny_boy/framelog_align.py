"""Default alignment of a roll's negatives to Frame log frames
(docs/METADATA_IMPORT_PLAN.md 5.3)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from scanny_boy.roll_sequence import sequence_negatives

if TYPE_CHECKING:
    from scanny_boy.roll_manifest import RollManifest


def last_frame(api_roll: dict[str, Any]) -> int:
    value = api_roll.get("lastFrame")
    return value if type(value) is int else 0


def default_alignment(
    manifest: RollManifest,
    api_roll: dict[str, Any],
    frames: list[dict[str, Any]],
) -> list[tuple[str, int | None]]:
    """`(negative_id, frame_number | None)` per sequenced negative, in
    sequence order."""
    order = sequence_negatives(manifest)
    by_id = {n.negative_id: n for n in manifest.negatives}
    number_by_uid = {frame["uid"]: frame["frameNumber"] for frame in frames}

    linked = manifest.metadata.framelog_roll_uid == api_roll["uid"]
    stored = {
        negative_id: number_by_uid.get(by_id[negative_id].framelog_frame_uid)
        for negative_id in order
    }
    if linked and any(number is not None for number in stored.values()):
        result: list[tuple[str, int | None]] = []
        previous = 0
        for negative_id in order:
            number = stored[negative_id]
            if number is not None and number > previous:
                previous = number
            else:
                number = None
            result.append((negative_id, number))
        return result

    limit = last_frame(api_roll)
    return [
        (negative_id, rank if 1 <= rank <= limit else None)
        for rank, negative_id in enumerate(order, start=1)
    ]
