"""`edit` subcommands: the nondestructive editing entry points.

**Edits live in the ops log; the TIFF is the artefact.** `edit rotate`
appends a rotation op to the negative's ordered log in the library database,
regenerates the CLI-rendered preview so the app can show the result, and
emits `edit_recorded` — it never touches the published TIFF. The pixels are
transformed only at export time, when the exporter replays each negative's
ops log over the published TIFF.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scanny_boy import previews
from scanny_boy.events import Code, WarningEvent
from scanny_boy.library import repo
from scanny_boy.library.repo import RollNotRegisteredError
from scanny_boy.manifest import BadManifestError
from scanny_boy.roll_manifest import (
    load_roll_manifest,
    write_roll_manifest,
)

EmitFn = Any

DIRECTIONS = {"cw", "ccw"}


class EditFailure(Exception):
    def __init__(self, code: Code, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def run_edit_rotate(
    roll_dir: Path,
    negative_id: str,
    direction: str,
    *,
    emit: EmitFn,
) -> dict:
    """Append a quarter-turn op and refresh the preview. Returns the
    `EditRecorded` event's field values; the published TIFF is untouched.
    Raises `EditFailure` when the roll, negative, or direction is no good —
    one problem per invocation, since there is exactly one."""
    if direction not in DIRECTIONS:
        raise EditFailure(
            Code.INVALID_EDIT,
            f"--direction must be one of {sorted(DIRECTIONS)}, got {direction!r}",
        )

    if not repo.roll_registered(roll_dir):
        raise EditFailure(
            Code.ROLL_NOT_FOUND,
            f"{roll_dir} is not a registered roll; create the roll first",
        )

    try:
        roll = load_roll_manifest(roll_dir)
    except (BadManifestError, RollNotRegisteredError) as exc:
        raise EditFailure(exc.code, exc.message) from exc

    try:
        negative = roll.negative(negative_id)
    except KeyError:
        raise EditFailure(
            Code.NEGATIVE_NOT_FOUND,
            f"{roll_dir} has no negative {negative_id!r}",
        ) from None

    if negative.output is None:
        raise EditFailure(
            Code.NEGATIVE_NOT_FOUND,
            f"{negative_id} has not been stitched yet; nothing to rotate",
        )

    edit = repo.append_edit(
        roll_dir, negative_id, repo.ROTATE_OP, {"direction": direction}
    )

    try:
        preview = previews.ensure_preview(roll_dir, roll.roll_id, negative, direction)
    except Exception as exc:  # noqa: BLE001 — a preview failure must not lose the edit
        emit(
            WarningEvent(
                code=Code.PREVIEW_FAILED,
                message=f"recorded the rotation but could not refresh the preview: {exc}",
            )
        )
        preview = None

    if preview is not None and negative.preview_path != str(preview):
        negative.preview_path = str(preview)
        write_roll_manifest(roll_dir, roll)

    quarter_turns = repo.net_rotation_quarter_turns(roll_dir, negative_id)
    return {
        "negative_id": negative_id,
        "edit": edit,
        "rotation_quarter_turns": quarter_turns,
        "preview_path": negative.preview_path,
    }
