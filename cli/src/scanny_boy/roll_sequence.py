"""A roll's display order and applied timestamps, both pure functions of
the manifest. See `docs/PHASE3_IMPLEMENTATION_PLAN.md` section 3.7.

Nothing else recomputes either of these: `roll_manifest.write_roll_manifest`
calls `sequence_negatives` to refresh every negative's `sequence` field on
every write, and the metadata stage (Chunk P3-7) will call `intended_times`
the same way. Neither function reads or writes anything itself.
"""

from __future__ import annotations

import datetime

from scanny_boy.roll_manifest import RollManifest

NOON = datetime.time(12, 0, 0)


def _sequenceable(manifest: RollManifest) -> list:
    """Every negative that can hold a position in the roll: actually
    published -- a `pending` or `failed` negative has no real capture time to
    rank by and nothing to display."""
    return [
        n
        for n in manifest.negatives
        if n.status == "completed" and n.capture_time.source_datetime_original is not None
    ]


def _rank_key(run_index: dict[str, int], negative) -> tuple:
    source_time = datetime.datetime.fromisoformat(negative.capture_time.source_datetime_original)
    return (source_time, run_index[negative.run_id], negative.members[0])


def sequence_negatives(manifest: RollManifest) -> list[str]:
    """Section 3.7: every published negative's `negative_id`, ordered by the
    real capture time of its first member across every run, ascending. Ties
    break by run index (the order runs were appended in, i.e.
    `manifest.runs`' own order), then by first member's filename."""
    run_index = {run.run_id: i for i, run in enumerate(manifest.runs)}
    ordered = sorted(_sequenceable(manifest), key=lambda n: _rank_key(run_index, n))
    return [n.negative_id for n in ordered]


def intended_times(manifest: RollManifest) -> dict[str, datetime.datetime]:
    """Section 3.7's rank-based applied-timestamp formula: noon plus
    `(rank - 1)` seconds on a negative's effective date -- the roll's
    `roll_capture_date`, or its own `date_override` when it has one. `rank`
    is the negative's 1-based position among every negative sharing that
    same effective date, counted in the roll's overall sequence order; a
    roll with no overrides at all reduces to noon plus `(sequence - 1)`
    seconds on `roll_capture_date` for everyone, exactly as section 3.7
    states it for the non-override case.

    Returns `{}` when the roll has no `roll_capture_date` yet -- there is
    no date to apply until one is set.
    """
    roll_date_text = manifest.metadata.roll_capture_date
    if roll_date_text is None:
        return {}
    roll_date = datetime.date.fromisoformat(roll_date_text)

    by_id = {n.negative_id: n for n in manifest.negatives}
    times: dict[str, datetime.datetime] = {}
    rank_by_date: dict[datetime.date, int] = {}
    for negative_id in sequence_negatives(manifest):
        negative = by_id[negative_id]
        override = negative.capture_time.date_override
        date = datetime.date.fromisoformat(override) if override else roll_date
        rank_by_date[date] = rank_by_date.get(date, 0) + 1
        rank = rank_by_date[date]
        times[negative_id] = (
            datetime.datetime.combine(date, NOON) + datetime.timedelta(seconds=rank - 1)
        )
    return times


def apply_intended_times(manifest: RollManifest) -> None:
    """Writes `intended_times`' result back into the manifest, stamping each
    negative's `intended_datetime_original` in `datetime.isoformat()` form
    and clearing it when the roll has no `roll_capture_date` (there is no
    date to intend). The metadata-editing stage calls this after every
    change to `roll_capture_date` or a negative's `date_override`, so the
    intended timestamps in the database are always the rank-based formula's
    current answer — roll order preserved, noon + (rank − 1) seconds."""
    times = intended_times(manifest)
    for negative in manifest.negatives:
        intended = times.get(negative.negative_id)
        negative.capture_time.intended_datetime_original = (
            intended.isoformat() if intended is not None else None
        )
