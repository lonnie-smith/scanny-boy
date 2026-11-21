"""A roll's display order and applied timestamps, both pure functions of
the manifest.

Nothing else recomputes either of these: `roll_manifest.stamp_derived_state`
calls `sequence_negatives` to refresh every negative's `sequence` field on
every write, and the metadata stage will call `intended_times` the same
way. Neither function reads or writes anything itself.
"""

from __future__ import annotations

import datetime

from scanny_boy.roll_manifest import RollManifest

NOON = datetime.time(12, 0, 0)


def _rank_time(negative) -> str | None:
    """The time a negative ranks at: its sequence anchor when a re-scan took
    over an earlier slot (`capture_time.sequence_datetime_original`),
    otherwise the real capture time of its first frame."""
    capture_time = negative.capture_time
    return (
        capture_time.sequence_datetime_original
        or capture_time.source_datetime_original
    )


def _sequenceable(manifest: RollManifest) -> list:
    """Every negative that can hold a position in the roll: actually
    published -- a `pending` or `failed` negative has no real capture time to
    rank by and nothing to display."""
    return [
        n
        for n in manifest.negatives
        if n.status == "completed" and _rank_time(n) is not None
    ]


def _rank_key(run_index: dict[str, int], negative) -> tuple:
    rank_time = datetime.datetime.fromisoformat(_rank_time(negative))
    return (rank_time, run_index[negative.run_id], negative.members[0])


def sequence_negatives(manifest: RollManifest) -> list[str]:
    """Every published negative's `negative_id`, ordered by the real
    capture time of its first member across every run, ascending -- or by
    its sequence anchor, when a re-scan took over an earlier slot. Ties
    break by run index (the order runs were appended in, i.e.
    `manifest.runs`' own order), then by first member's filename."""
    run_index = {run.run_id: i for i, run in enumerate(manifest.runs)}
    ordered = sorted(_sequenceable(manifest), key=lambda n: _rank_key(run_index, n))
    return [n.negative_id for n in ordered]


def intended_times(manifest: RollManifest) -> dict[str, datetime.datetime]:
    """The applied-timestamp formula.

    A negative carrying a logged time (`capture_time.logged_datetime_original`,
    imported from the Frame log) uses it as is. It does not consume a rank:
    it is returned even when the roll has no `roll_capture_date`, and the
    rank-based negatives never count it.

    Every other negative gets noon plus `(rank - 1)` seconds on its effective
    date -- the roll's `roll_capture_date`, or its own `date_override` when
    it has one. `rank` is the negative's 1-based position among the
    rank-based negatives sharing that same effective date, counted in the
    roll's overall sequence order; a roll with no overrides or logged times
    reduces to noon plus `(sequence - 1)` seconds on `roll_capture_date` for
    everyone.

    Rank-based negatives are omitted when the roll has no
    `roll_capture_date` -- there is no date to apply until one is set.
    """
    roll_date_text = manifest.metadata.roll_capture_date
    roll_date = (
        datetime.date.fromisoformat(roll_date_text) if roll_date_text else None
    )

    by_id = {n.negative_id: n for n in manifest.negatives}
    times: dict[str, datetime.datetime] = {}
    rank_by_date: dict[datetime.date, int] = {}
    for negative_id in sequence_negatives(manifest):
        negative = by_id[negative_id]
        logged = negative.capture_time.logged_datetime_original
        if logged:
            times[negative_id] = datetime.datetime.fromisoformat(logged)
            continue
        if roll_date is None:
            # Rank-based negatives need the roll's date, as before.
            continue
        override = negative.capture_time.date_override
        date = datetime.date.fromisoformat(override) if override else roll_date
        rank_by_date[date] = rank_by_date.get(date, 0) + 1
        rank = rank_by_date[date]
        times[negative_id] = datetime.datetime.combine(date, NOON) + datetime.timedelta(
            seconds=rank - 1
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
