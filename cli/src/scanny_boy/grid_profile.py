"""Named grid configuration presets for the app.

Each preset is a label for an `across` x `down` grid shape the user picks
when adding scans — the same dimensions `probe`/`run` take via `--grid AxD`
or `--per-negative N`.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime

from scanny_boy.events import Code, GridProfileSummary
from scanny_boy.selection import GridSpec, validate_grid


class GridProfileError(Exception):
    def __init__(self, code: Code, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclasses.dataclass(frozen=True)
class GridProfile:
    profile_id: str
    name: str
    across: int
    down: int
    created_at: str


def grid_profile_summary(profile: GridProfile) -> GridProfileSummary:
    return GridProfileSummary(
        profile_id=profile.profile_id,
        name=profile.name,
        across=profile.across,
        down=profile.down,
        created_at=profile.created_at,
    )


def new_grid_profile(name: str, across: int, down: int) -> GridProfile:
    """Build one preset after validating its grid shape."""
    try:
        validate_grid(GridSpec(across=across, down=down))
    except ValueError as exc:
        raise GridProfileError(Code.INVALID_GRID, str(exc)) from exc
    return GridProfile(
        profile_id=str(uuid.uuid4()),
        name=name,
        across=across,
        down=down,
        created_at=datetime.now(UTC).replace(microsecond=0).isoformat().replace(
            "+00:00", "Z"
        ),
    )
