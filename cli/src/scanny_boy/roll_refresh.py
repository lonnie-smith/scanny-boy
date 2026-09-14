"""``roll refresh``: recompute the highlight lock and regenerate stale previews.

See docs/TETHER_PLAN.md §4.4.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path

from scanny_boy import highlight_lock, previews
from scanny_boy.events import Code, Event
from scanny_boy.roll_manifest import load_roll_manifest, write_roll_manifest

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


def run_roll_refresh(roll_dir: Path, *, emit: EmitFn) -> RollRefreshOutcome:
    """Recompute ``highlight_lock``, sync every completed negative's preview,
    and clear ``refresh_pending``."""
    roll = load_roll_manifest(roll_dir)
    previous_lock = roll.highlight_lock
    new_lock = highlight_lock.compute_roll_highlight_lock(roll)
    roll.highlight_lock = None if new_lock is None else new_lock.to_dict()
    lock_changed = roll.highlight_lock != previous_lock
    roll.refresh_pending = False
    write_roll_manifest(roll_dir, roll)

    published = [n for n in roll.negatives if n.status == "completed"]
    previews_regenerated = lock_changed
    try:
        previews.sync_previews(roll_dir, roll, published, force=lock_changed)
    except Exception as exc:  # noqa: BLE001
        from scanny_boy.events import WarningEvent

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
