"""`edit` subcommands: the nondestructive editing entry points.

**Edits live in the ops log; the TIFF is the artefact.** `edit rotate` and
`edit flip` append ops to the negatives' ordered log in the library
database, regenerate the CLI-rendered previews so the app can show the
results, and emit `edit_recorded` per negative — they never touch the
published TIFFs. The pixels are transformed only at export time, when the
exporter replays each negative's ops log over the published TIFF.

Every subcommand accepts a *selection* of negatives: the whole selection is
validated before anything is written, so a batch either records or fails
without partial effects.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from scanny_boy import previews
from scanny_boy.events import Code, WarningEvent
from scanny_boy.library import repo
from scanny_boy.library.repo import RollNotRegisteredError
from scanny_boy.manifest import BadManifestError
from scanny_boy.roll_manifest import (
    load_roll_manifest,
    write_roll_manifest,
)

if TYPE_CHECKING:
    from scanny_boy.roll_manifest import NegativeRecord, RollManifest

EmitFn = Any

DIRECTIONS = {"cw", "ccw"}


def _as_selection(negative_ids: str | Sequence[str]) -> list[str]:
    """A single negative id is accepted for convenience — the CLI's
    `--negative` is repeatable, so internally every selection is a list."""
    return [negative_ids] if isinstance(negative_ids, str) else list(negative_ids)


class EditFailure(Exception):
    def __init__(self, code: Code, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _validated_negatives(
    roll_dir: Path, negative_ids: Sequence[str]
) -> tuple[RollManifest, list[NegativeRecord]]:
    """The roll manifest plus the named negatives, each verified to belong
    to the roll and to have been stitched. One shared check for every edit
    subcommand, so a batch never applies half a selection."""
    if not repo.roll_registered(roll_dir):
        raise EditFailure(
            Code.ROLL_NOT_FOUND,
            f"{roll_dir} is not a registered roll; create the roll first",
        )

    try:
        roll = load_roll_manifest(roll_dir)
    except (BadManifestError, RollNotRegisteredError) as exc:
        raise EditFailure(exc.code, exc.message) from exc

    negatives: list[NegativeRecord] = []
    for negative_id in negative_ids:
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
                f"{negative_id} has not been stitched yet; nothing to edit",
            )
        negatives.append(negative)
    return roll, negatives


def _refresh_preview(
    roll_dir: Path, roll: RollManifest, negative: NegativeRecord, op: str, *,
    what: str, emit: EmitFn,
) -> None:
    """Refreshes the negative's cached preview after one appended op, and
    records the path on the manifest. A preview failure must not lose the
    edit, so it downgrades to a warning (the next full regeneration from
    the net transform will catch up)."""
    try:
        preview = previews.ensure_preview(roll_dir, roll.roll_id, negative, op)
    except Exception as exc:  # noqa: BLE001 — a preview failure must not lose the edit
        emit(
            WarningEvent(
                code=Code.PREVIEW_FAILED,
                message=f"recorded the {what} but could not refresh the preview: {exc}",
            )
        )
        return

    if negative.preview_path != str(preview):
        negative.preview_path = str(preview)
        write_roll_manifest(roll_dir, roll)


def _append_transform_op(
    roll_dir: Path,
    negative_ids: Sequence[str],
    op: str,
    params: dict[str, Any],
    *,
    preview_op: str,
    what: str,
    emit: EmitFn,
) -> list[dict]:
    """The shared body of `edit rotate` and `edit flip`: validate the whole
    selection, append one op per negative, refresh each preview, and return
    one `EditRecorded` field set per negative in selection order."""
    roll, negatives = _validated_negatives(roll_dir, negative_ids)

    results: list[dict] = []
    for negative in negatives:
        edit = repo.append_edit(roll_dir, negative.negative_id, op, params)
        _refresh_preview(roll_dir, roll, negative, preview_op, what=what, emit=emit)
        quarter_turns, flipped, fine_angle = repo.net_edit_state(
            roll_dir, negative.negative_id
        )
        results.append(
            {
                "negative_id": negative.negative_id,
                "edit": edit,
                "rotation_quarter_turns": quarter_turns,
                "flipped_horizontally": flipped,
                "fine_rotation_deg": fine_angle,
                "preview_path": negative.preview_path,
            }
        )
    return results


def run_edit_rotate(
    roll_dir: Path,
    negative_ids: str | Sequence[str],
    direction: str,
    *,
    emit: EmitFn,
) -> list[dict]:
    """Append a quarter-turn op to each selected negative and refresh its
    preview. Returns one `EditRecorded` event's field values per negative;
    the published TIFFs are untouched. Raises `EditFailure` when the roll,
    any negative, or the direction is no good — the selection is validated
    up front, so a failure leaves nothing recorded."""
    if direction not in DIRECTIONS:
        raise EditFailure(
            Code.INVALID_EDIT,
            f"--direction must be one of {sorted(DIRECTIONS)}, got {direction!r}",
        )
    return _append_transform_op(
        roll_dir,
        _as_selection(negative_ids),
        repo.ROTATE_OP,
        {"direction": direction},
        preview_op=direction,
        what="rotation",
        emit=emit,
    )


def run_edit_flip(
    roll_dir: Path,
    negative_ids: str | Sequence[str],
    *,
    emit: EmitFn,
) -> list[dict]:
    """Append a horizontal-mirror op to each selected negative and refresh
    its preview — the flip applies to the pixels as they currently render,
    *after* any recorded rotations, which is why the ops log is replayed in
    order rather than collapsed to a rotation count. Same contract as
    `run_edit_rotate`."""
    return _append_transform_op(
        roll_dir,
        _as_selection(negative_ids),
        repo.FLIP_OP,
        {},
        preview_op="flip",
        what="flip",
        emit=emit,
    )


def run_edit_render_region(
    roll_dir: Path,
    negative_id: str,
    x: int,
    y: int,
    width: int,
    height: int,
    output_path: Path,
    *,
    emit: EmitFn,
) -> dict:
    """Render one display-space region of a negative's published TIFF at
    1:1 — the ops log's net rotation and flip folded in, the same display
    encode as `generate_preview` — into `output_path` as a lossless PNG. A
    pure rendering query: nothing is recorded, the published TIFF and the
    ops log are untouched. Returns the `RegionRendered` event's field
    values (the rect actually rendered, post-clamp). Raises `EditFailure`
    when the roll, negative, or region is no good."""
    if width <= 0 or height <= 0:
        raise EditFailure(
            Code.INVALID_EDIT,
            f"--width and --height must be positive, got {width}x{height}",
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
            f"{negative_id} has not been stitched yet; nothing to render",
        )

    tiff_path = roll_dir / negative.output["name"]
    quarter_turns, flipped, fine_angle = repo.net_edit_state(roll_dir, negative_id)
    try:
        rendered = previews.render_region(
            tiff_path,
            x,
            y,
            width,
            height,
            quarter_turns=quarter_turns,
            flipped_horizontally=flipped,
            fine_angle_deg=fine_angle,
            destination=output_path,
        )
    except ValueError as exc:
        raise EditFailure(Code.INVALID_EDIT, str(exc)) from exc
    return {
        "negative_id": negative_id,
        "path": str(output_path),
        "x": rendered[0],
        "y": rendered[1],
        "width": rendered[2],
        "height": rendered[3],
    }


def run_edit_delete(
    roll_dir: Path, negative_ids: str | Sequence[str], *, emit: EmitFn
) -> list[dict]:
    """Remove each selected negative outright: its record (and its edits
    ops log, by cascade) from the library database, its published TIFF from
    the roll folder, and its rendered preview from Application Support. Any
    negative is deletable, whatever its status — a pending or failed one
    simply has no file to unlink.

    The whole selection is validated first, then every record goes, exactly
    as `_remove_covered_negatives` does: a crash then leaves an orphan file,
    never a dangling record. A failed unlink is a warning
    (`ORPHAN_FILE_NOT_REMOVED`), not a failure — the record is already gone,
    so re-deleting cannot help and the user should not be stuck. Raises
    `EditFailure` when the roll or any negative is no good. Returns one
    `NegativeDeleted` event's field values per negative."""
    if not repo.roll_registered(roll_dir):
        raise EditFailure(
            Code.ROLL_NOT_FOUND,
            f"{roll_dir} is not a registered roll; create the roll first",
        )

    try:
        roll = load_roll_manifest(roll_dir)
    except (BadManifestError, RollNotRegisteredError) as exc:
        raise EditFailure(exc.code, exc.message) from exc

    negative_ids = _as_selection(negative_ids)
    removals: list[tuple[NegativeRecord, str | None]] = []
    for negative_id in negative_ids:
        try:
            negative = roll.negative(negative_id)
        except KeyError:
            raise EditFailure(
                Code.NEGATIVE_NOT_FOUND,
                f"{roll_dir} has no negative {negative_id!r}",
            ) from None
        output_name = negative.output["name"] if negative.output is not None else None
        removals.append((negative, output_name))

    for negative, _ in removals:
        roll.negatives.remove(negative)
    # One write for the whole batch: `write_roll_manifest` renumbers the
    # survivors' sequences and saves; the removed negatives' rows (and their
    # edits) are deleted by the save's diff.
    write_roll_manifest(roll_dir, roll)

    results: list[dict] = []
    for negative, output_name in removals:
        targets = [Path(negative.preview_path)] if negative.preview_path else []
        if output_name is not None:
            targets.insert(0, roll_dir / output_name)
        for path in targets:
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                emit(
                    WarningEvent(
                        code=Code.ORPHAN_FILE_NOT_REMOVED,
                        message=f"{path} could not be removed: {exc}",
                    )
                )
        results.append(
            {"negative_id": negative.negative_id, "output": output_name}
        )
    return results
