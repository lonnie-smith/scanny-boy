"""`edit` subcommands: the nondestructive editing entry points.

**Edits live in the ops log; the TIFF is the artefact.** `edit rotate`,
`edit flip`, `edit tone`, and `edit color` append ops to the negatives'
ordered log in the library database, regenerate the CLI-rendered previews
so the app can show the results, and emit `edit_recorded` per negative —
they never touch the published TIFFs. The pixels are transformed only at
export time, when the exporter replays each negative's ops log over the
published TIFF (`tone` and `color` are preview-only judgement aids, so the
exporter ignores them).

Every subcommand accepts a *selection* of negatives: the whole selection is
validated before anything is written, so a batch either records or fails
without partial effects.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from scanny_boy import color, previews
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

_COLOR_REGION_KEYS = {
    "global": ("wb_magenta", "wb_yellow"),
    "shadows": ("shadow_magenta", "shadow_yellow"),
    "highlights": ("highlight_magenta", "highlight_yellow"),
}


def roll_is_monochrome(roll: RollManifest) -> bool:
    """The roll's frozen film kind (MONOCHROME_PLAN §2). Colour has no
    meaning on a single-density roll: there are no layers to balance and
    no dyes to separate."""
    film = roll.film
    if not film:
        return False
    return film.get("kind") == "monochrome"


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
        state = repo.net_edit_state(roll_dir, negative.negative_id)
        results.append(
            {
                "negative_id": negative.negative_id,
                "edit": edit,
                "rotation_quarter_turns": state.quarter_turns,
                "flipped_horizontally": state.flipped,
                "fine_rotation_deg": state.fine_angle_deg,
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


def run_edit_tone(
    roll_dir: Path,
    negative_ids: str | Sequence[str],
    params: dict[str, float | None] | None,
    *,
    auto_density: bool = False,
    auto_grade: bool = False,
    emit: EmitFn,
) -> list[dict]:
    """Records each selected negative's preview tone adjustment — the full
    nine-key tone state, or all `None` for the reset to the flat linear
    look (see `tone.py`). Auto flags solve density and/or grade from each
    negative's recorded normalization before validation.

    The op is a state, not a transform: the latest one wins and a trailing
    `tone` op is coalesced in place. The preview is regenerated from the
    published TIFF; the TIFFs themselves are untouched, and export ignores
    the tone op."""
    from scanny_boy import auto_tone

    roll, negatives = _validated_negatives(roll_dir, _as_selection(negative_ids))

    results: list[dict] = []
    for negative in negatives:
        solved = dict(params or {key: None for key in repo.validated_tone_params(None)})
        if auto_density or auto_grade:
            record = negative.normalization
            if auto_density:
                value = auto_tone.solve_density(record)
                if value is None:
                    emit(
                        WarningEvent(
                            code=Code.TONE_METERING_UNAVAILABLE,
                            message=(
                                f"{negative.negative_id}: normalization metering "
                                "unavailable; density left unchanged"
                            ),
                        )
                    )
                else:
                    solved["density"] = value
            if auto_grade:
                value = auto_tone.solve_grade(record)
                if value is None:
                    emit(
                        WarningEvent(
                            code=Code.TONE_METERING_UNAVAILABLE,
                            message=(
                                f"{negative.negative_id}: normalization metering "
                                "unavailable; grade left unchanged"
                            ),
                        )
                    )
                else:
                    solved["grade_r"] = value
        try:
            validated = repo.validated_tone_params(solved)
        except ValueError as exc:
            raise EditFailure(Code.INVALID_EDIT, str(exc)) from exc

        edit = repo.append_tone_edit(roll_dir, negative.negative_id, validated)
        _refresh_preview(roll_dir, roll, negative, repo.TONE_OP, what="tone", emit=emit)
        state = repo.net_edit_state(roll_dir, negative.negative_id)
        results.append(
            {
                "negative_id": negative.negative_id,
                "edit": edit,
                "rotation_quarter_turns": state.quarter_turns,
                "flipped_horizontally": state.flipped,
                "fine_rotation_deg": state.fine_angle_deg,
                "preview_path": negative.preview_path,
            }
        )
    return results


def _merge_color_params(
    recorded: dict[str, float] | None,
    updates: dict[str, float | None],
) -> dict[str, float | None]:
    import dataclasses

    from scanny_boy import color
    from scanny_boy.library.repo import _color_neutral_defaults

    # Build the base from the neutral defaults and overlay the recorded
    # dict, instead of indexing the recorded dict directly
    # (docs/CAST_REMOVAL_PLAN.md R-2 §6.2): a twelve-key recorded state —
    # an op written before the thirteenth key existed — must not raise,
    # and a missing newer key keeps its neutral default.
    base = _color_neutral_defaults()
    if recorded is not None:
        for key, value in recorded.items():
            if key in base:
                base[key] = value
    else:
        base = dataclasses.asdict(color.NEUTRAL_COLOR)
    merged = {key: base[key] for key in color.COLOR_PARAM_KEYS}
    for key, value in updates.items():
        if value is not None:
            merged[key] = value
    return merged


def run_edit_color(
    roll_dir: Path,
    negative_ids: str | Sequence[str],
    params: dict[str, float | None] | None,
    *,
    reset: bool = False,
    temperature: float | None = None,
    region: str = "global",
    emit: EmitFn,
) -> list[dict]:
    """Records each selected negative's preview colour adjustment — the full
    twelve-key colour state, or all `None` for the reset."""
    from scanny_boy import color

    roll, negatives = _validated_negatives(roll_dir, _as_selection(negative_ids))
    if not reset and roll_is_monochrome(roll):
        raise EditFailure(
            Code.INVALID_EDIT,
            "this roll is monochrome — colour adjustment has no meaning on a "
            "single-density roll",
        )

    mag_key, yellow_key = _COLOR_REGION_KEYS[region]

    results: list[dict] = []
    for negative in negatives:
        if reset:
            solved = {key: None for key in color.COLOR_PARAM_KEYS}
        else:
            state = repo.net_edit_state(roll_dir, negative.negative_id)
            updates = dict(params or {})
            if temperature is not None:
                import dataclasses

                base = (
                    dict(state.color)
                    if state.color is not None
                    else dataclasses.asdict(color.NEUTRAL_COLOR)
                )
                m, y = color.kelvin_to_wb(temperature, base[mag_key], base[yellow_key])
                updates[mag_key] = m
                updates[yellow_key] = y
            solved = _merge_color_params(state.color, updates)
            cast = solved.get("cast_removal", 0.0)
            if cast and float(cast) != 0.0:
                meter = color.read_metering(negative.normalization)
                if meter.shadow_refs_norm is None:
                    emit(
                        WarningEvent(
                            code=Code.TONE_METERING_UNAVAILABLE,
                            message=(
                                f"{negative.negative_id}: normalization metering "
                                "unavailable; cast removal recorded anyway"
                            ),
                        )
                    )
        try:
            validated = repo.validated_color_params(solved)
        except ValueError as exc:
            raise EditFailure(Code.INVALID_EDIT, str(exc)) from exc

        edit = repo.append_color_edit(roll_dir, negative.negative_id, validated)
        _refresh_preview(roll_dir, roll, negative, repo.COLOR_OP, what="color", emit=emit)
        state = repo.net_edit_state(roll_dir, negative.negative_id)
        results.append(
            {
                "negative_id": negative.negative_id,
                "edit": edit,
                "rotation_quarter_turns": state.quarter_turns,
                "flipped_horizontally": state.flipped,
                "fine_rotation_deg": state.fine_angle_deg,
                "preview_path": negative.preview_path,
            }
        )
    return results


def _validated_negative(
    roll_dir: Path, negative_id: str
) -> tuple[RollManifest, NegativeRecord]:
    """The roll manifest plus the one named negative, verified to belong to
    the roll and to have been stitched — the single-negative form of
    `_validated_negatives`, shared by the pure-query rendering subcommands
    (which take exactly one negative, not a selection)."""
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
    return roll, negative


def _validated_display_mode(mode: str) -> str:
    """The display encode `render-region`/`render-preview` should use."""
    if mode not in previews.DISPLAY_MODES:
        raise EditFailure(
            Code.INVALID_EDIT,
            f"--mode must be one of {list(previews.DISPLAY_MODES)}, got {mode!r}",
        )
    return mode


def run_edit_render_region(
    roll_dir: Path,
    negative_id: str,
    x: int,
    y: int,
    width: int,
    height: int,
    output_path: Path,
    *,
    mode: str = "positive",
    emit: EmitFn,
) -> dict:
    """Render one display-space region of a negative's published TIFF at
    1:1 — the ops log's net rotation and flip folded in, the display encode
    `mode` names (`"positive"`: the same inverted encode as
    `generate_preview`, with the net tone composed in; `"negative"`: the
    un-inverted density view, which no tone reaches) — into `output_path`
    as a lossless PNG. A pure rendering query: nothing is recorded, the
    published TIFF and the ops log are untouched. Returns the
    `RegionRendered` event's field values (the rect actually rendered,
    post-clamp). Raises `EditFailure` when the roll, negative, mode, or
    region is no good."""
    _validated_display_mode(mode)
    if width <= 0 or height <= 0:
        raise EditFailure(
            Code.INVALID_EDIT,
            f"--width and --height must be positive, got {width}x{height}",
        )

    _roll, negative = _validated_negative(roll_dir, negative_id)

    tiff_path = roll_dir / negative.output["name"]
    state = repo.net_edit_state(roll_dir, negative_id)
    meter = color.read_metering(negative.normalization)
    try:
        rendered = previews.render_region(
            tiff_path,
            x,
            y,
            width,
            height,
            quarter_turns=state.quarter_turns,
            flipped_horizontally=state.flipped,
            fine_angle_deg=state.fine_angle_deg,
            tone_params=state.tone,
            color_params=state.color,
            metering=meter,
            destination=output_path,
            mode=mode,
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


def run_edit_render_preview(
    roll_dir: Path,
    negative_id: str,
    output_path: Path,
    *,
    mode: str = "positive",
    emit: EmitFn,
) -> dict:
    """Render the whole display image — the ops log's net transform folded
    in, downscaled to the managed preview's own `PREVIEW_MAX_EDGE` — in the
    display encode `mode` names into `output_path` as a lossless PNG. The
    pure-query backing of the app's positive/negative toggle: like
    `run_edit_render_region` it is a pure rendering query — nothing is
    recorded, the published TIFF and the ops log are untouched — and the
    `"negative"` mode is the un-inverted density view, which no tone
    reaches. Returns the `PreviewRendered` event's field values (the
    written PNG's pixel dimensions). Raises `EditFailure` when the roll,
    negative, or mode is no good."""
    _validated_display_mode(mode)

    _roll, negative = _validated_negative(roll_dir, negative_id)

    tiff_path = roll_dir / negative.output["name"]
    state = repo.net_edit_state(roll_dir, negative_id)
    try:
        width, height = previews.render_preview(
            tiff_path,
            output_path,
            quarter_turns=state.quarter_turns,
            flipped_horizontally=state.flipped,
            fine_angle_deg=state.fine_angle_deg,
            mode=mode,
            tone_params=state.tone,
        )
    except ValueError as exc:
        raise EditFailure(Code.INVALID_EDIT, str(exc)) from exc
    return {
        "negative_id": negative_id,
        "path": str(output_path),
        "width": width,
        "height": height,
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
