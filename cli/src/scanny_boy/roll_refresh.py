"""``roll refresh``: recompute the highlight lock and regenerate stale previews.

See docs/TETHER_PLAN.md §4.4. §4.4 predates automatic tone-split neutral
balance: the deferred `stitch --defer-roll-refresh` path skips
`auto_neutral.recompute_roll_auto_neutral` entirely (see
`stitch_pipeline.py`'s `defer_roll_refresh` branch), so a tether-captured
roll's negatives never get an `auto_neutral` block until something measures
it. This refresh is the only catch-up point, so it measures every completed
colour negative rather than mirroring the non-deferred path's
lock-changed-only shortcut (`stitch_pipeline.py` recomputes the whole roll
when the lock moved and only the newly published negatives otherwise) — in
refresh there is no "newly published" set to fall back to, and on the first
refresh after a deferred session none of the roll's negatives have been
measured at all.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from scanny_boy import auto_neutral, highlight_lock, previews
from scanny_boy.events import Code, Event, WarningEvent
from scanny_boy.roll_manifest import load_roll_manifest, write_roll_manifest

if TYPE_CHECKING:
    from scanny_boy.roll_manifest import RollManifest

EmitFn = Callable[[Event], None]


@dataclasses.dataclass(frozen=True)
class RollRefreshFailure(Exception):
    code: Code
    message: str

    def __init__(self, code: Code, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclasses.dataclass(frozen=True)
class RollRefreshOutcome:
    lock_changed: bool
    previews_regenerated: bool


def _recompute_auto_neutral_tolerant(
    roll: RollManifest, roll_dir: Path, *, emit: EmitFn
) -> bool:
    """Measure `auto_neutral` for every completed colour negative.

    Mirrors `auto_neutral.recompute_roll_auto_neutral`'s per-negative work
    (`recompute_negative_auto_neutral`, which already skips mono negatives
    and negatives without a published TIFF — not duplicated here) but adds
    a per-negative try/except: one negative's measurement failure — a
    corrupt or since-removed TIFF, say — must not abort the rest of the
    roll's catch-up, the same tolerance `sync_previews` below gets for a
    preview failure.
    """
    changed = False
    for negative in roll.negatives:
        if negative.status != "completed" or negative.output is None:
            continue
        try:
            if auto_neutral.recompute_negative_auto_neutral(
                negative, roll_dir, highlight_lock=roll.highlight_lock
            ):
                changed = True
        except Exception as exc:  # noqa: BLE001 — one bad negative must not abort the refresh
            emit(
                WarningEvent(
                    code=Code.INTERNAL_ERROR,
                    message=(
                        "could not measure auto-neutral for "
                        f"{negative.negative_id} during refresh: {exc}"
                    ),
                )
            )
    return changed


def run_roll_refresh(roll_dir: Path, *, emit: EmitFn) -> RollRefreshOutcome:
    """Recompute ``highlight_lock``, measure ``auto_neutral`` for every
    completed colour negative, sync previews, and clear ``refresh_pending``."""
    roll = load_roll_manifest(roll_dir)
    previous_lock = roll.highlight_lock
    new_lock = highlight_lock.compute_roll_highlight_lock(roll)
    roll.highlight_lock = None if new_lock is None else new_lock.to_dict()
    lock_changed = roll.highlight_lock != previous_lock

    # docs/ROLL_HIGHLIGHT_LOCK.md §4 / auto_neutral.py: auto-neutral is
    # measured against the published TIFF using the (possibly just-updated)
    # highlight lock, so it runs after the lock above, not before.
    auto_neutral_changed = _recompute_auto_neutral_tolerant(roll, roll_dir, emit=emit)

    roll.refresh_pending = False
    write_roll_manifest(roll_dir, roll)

    # `sync_previews`'s third argument names the output files *this call*
    # just published (`list[str]`, per `previews.py`) — refresh never
    # publishes anything, so it is always empty. A negative that has no
    # preview yet still gets one built regardless (`sync_previews` does
    # that unconditionally); `force_previews` below is what makes an
    # already-previewed negative regenerate.
    #
    # docs/ROLL_HIGHLIGHT_LOCK.md §5: force every completed negative's
    # cached preview to regenerate when either the lock or any negative's
    # auto-neutral correction moved — mirrors the `force=` decision in
    # `stitch_pipeline.py`'s non-deferred path, so a deferred roll's
    # previews never go stale relative to what `roll info` now reports.
    force_previews = lock_changed or auto_neutral_changed
    previews_regenerated = force_previews
    try:
        previews.sync_previews(roll_dir, roll, [], force=force_previews)
    except Exception as exc:  # noqa: BLE001
        emit(
            WarningEvent(
                code=Code.PREVIEW_FAILED,
                message=f"could not regenerate previews during refresh: {exc}",
            )
        )
        previews_regenerated = False

    return RollRefreshOutcome(
        lock_changed=lock_changed,
        previews_regenerated=previews_regenerated,
    )
