"""The `run` command: one process, one event stream, one cancellation, from
a selection of NEFs all the way to finished, stitched negatives.

Calls Phase 1's `run_convert` in-process to build a work directory, then
`stitch_pipeline.run_stitch` to publish it, exactly as section 3.6
describes. Both stages' section 3.8 disk checks fire for free by simply
calling each function in turn — `run_convert` already refuses to write
anything if the work volume is short, and `run_stitch` already refuses to
publish anything if the output volume is short, so there is no separate
disk-check code here to add or to accidentally add together.
"""

from __future__ import annotations

import dataclasses
import shutil
from pathlib import Path

from scanny_boy.cancellation import CancellationToken
from scanny_boy.events import Code, Event, NegativeSuperseded, Progress, WarningEvent
from scanny_boy.pipeline import (
    STEPS_PER_FRAME,
    ConvertFailure,
    ConvertOutcome,
    run_convert,
)
from scanny_boy.registration import StitchError
from scanny_boy.roll_manifest import (
    load_roll_manifest,
    mark_superseded,
    write_roll_manifest,
)
from scanny_boy.stitch_pipeline import EmitFn, StitchOutcome, run_stitch

# Section 3.12.1's table 7: 0.57s detect + 0.50s warp is 1.07s of per-frame
# stitch work, and 0.5s match + 0.0s solve + 2.9s blend + 0.8s write is 4.2s
# of per-negative stitch work. One conversion unit is ~0.48s (15 frames in
# 21.7s at --jobs 4, 3 units per frame), so 1.07/0.48 ~= 2 and 4.2/0.48 ~= 9.
STITCH_UNITS_PER_FRAME = 2
STITCH_UNITS_PER_NEGATIVE = 9


class RunFailure(Exception):
    """A validation-level failure of the whole run, before any negative
    could be attempted — the same role `ConvertFailure` plays for `convert`
    and `StitchError` for `stitch`, unified here because `run` is one
    command backed by two calls that can each fail this way."""

    def __init__(self, code: Code, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclasses.dataclass(frozen=True)
class RunOutcome:
    run_id: str
    status: str  # "complete" | "partial" | "cancelled"
    convert: ConvertOutcome | None
    stitch: StitchOutcome | None
    work_dir: Path
    work_dir_kept: bool


def _wrap_emit_for_stitch(base_emit: EmitFn, *, completed_offset: int, weighted_total: int, combined_total: int) -> EmitFn:
    """Rescales `stitch`'s own step-counted progress into its share of the
    combined span. `run_stitch` counts real step boundaries for its own
    sake (correct, but not weighted by wall-clock cost); this rescales that
    count proportionally into `weighted_total`, the gate-C time-weighted
    share, so the combined total is exactly the formula section 3.9 asks
    for while `completed` still advances monotonically through it. The raw
    total is read from `run_stitch`'s own first Progress event rather than
    recomputed here, so this never has to know `stitch_pipeline`'s internal
    step-counting constants.
    """
    state: dict[str, int | None] = {"raw_total": None}

    def wrapped(event: Event) -> None:
        if isinstance(event, Progress):
            if state["raw_total"] is None:
                state["raw_total"] = event.total
            raw_total = state["raw_total"]
            scaled = (
                round(event.completed / raw_total * weighted_total) if raw_total else weighted_total
            )
            event = dataclasses.replace(
                event, completed=completed_offset + scaled, total=combined_total
            )
        base_emit(event)

    return wrapped


def _supersede_this_run(out_dir: Path, *, run_id: str, emit: EmitFn) -> None:
    """Section 3.4's replacement rule, executed: for every negative this run
    just published, mark whatever it covers as superseded, then delete each
    superseded negative's file and report it. Manifest first, file second —
    every supersession `mark_superseded` finds is written once, before any
    file is touched, so a crash between the two leaves an orphan file rather
    than a dangling record (section 3.4).

    A failed delete never fails the run: it emits
    `SUPERSEDED_FILE_NOT_REMOVED` and moves on (section 3.4).
    """
    roll = load_roll_manifest(out_dir)
    published_this_run = [
        n for n in roll.negatives if n.run_id == run_id and n.status == "completed"
    ]

    superseded = []
    for negative in published_this_run:
        superseded.extend(mark_superseded(roll, negative))
    if not superseded:
        return

    write_roll_manifest(out_dir, roll)

    for old in superseded:
        emit(
            NegativeSuperseded(
                run_id=run_id,
                old_negative_id=old.negative_id,
                new_negative_id=old.superseded_by,
            )
        )
        # A superseded record that never published (still `pending` or
        # `failed` from an earlier, crashed run — section 3.4's subset test
        # does not require `completed`) has no file to remove.
        if old.output is None:
            continue
        old_path = out_dir / old.output["name"]
        try:
            old_path.unlink()
        except OSError as exc:
            emit(
                WarningEvent(
                    run_id=run_id,
                    code=Code.SUPERSEDED_FILE_NOT_REMOVED,
                    message=f"{old_path} could not be removed: {exc}",
                )
            )


def run_full(
    input_dir: Path,
    files: list[str],
    out_dir: Path,
    per_negative: int,
    *,
    run_id: str,
    work_dir: Path | None,
    keep_intermediates: bool,
    skip_sources: list[str],
    jobs: int | None,
    cancel: CancellationToken,
    emit: EmitFn,
) -> RunOutcome:
    """Convert `files` into a work directory, then stitch it into `out_dir`
    (a roll).

    Raises `RunFailure` for any validation-level problem from either stage
    (mirroring `ConvertFailure`/`StitchError`). A group or negative failure
    does not raise: it is recorded by the stage that hit it, and `run`
    continues and ends `partial`. Cancellation during the convert stage
    skips the stitch stage entirely (section 3.5's "the group being
    processed is not published" extends naturally to "no stitching starts
    on a run that never finished converting").

    `skip_sources` (section 3.5) names filenames, relative to `input_dir`,
    to exclude from `files` before validation and grouping — so excluding
    anything but a whole group at a selection edge fails
    `NON_CONTIGUOUS_SELECTION` exactly as it would otherwise, with no
    special-cased check needed here.

    Section 3.6: the default work directory is `<roll>/.work/<run_id>/`,
    created here rather than a scattered temp directory, so a kept one
    (failure, cancellation, or `keep_intermediates`) is discoverable from
    inside the roll.
    """
    created_work_dir = work_dir is None
    if created_work_dir:
        resolved_work_dir = out_dir / ".work" / run_id
        resolved_work_dir.mkdir(parents=True)
    else:
        resolved_work_dir = Path(work_dir)

    files = [f for f in files if f not in skip_sources]

    frame_count = len(files)
    negative_count = frame_count // per_negative if per_negative else 0
    convert_total = frame_count * STEPS_PER_FRAME
    stitch_weighted_total = (
        STITCH_UNITS_PER_FRAME * frame_count + STITCH_UNITS_PER_NEGATIVE * negative_count
    )
    combined_total = convert_total + stitch_weighted_total

    try:
        convert_outcome = run_convert(
            input_dir,
            files,
            resolved_work_dir,
            per_negative,
            run_id=run_id,
            overwrite=False,
            jobs=jobs,
            cancel=cancel,
            emit=emit,
            completed_offset=0,
            total_override=combined_total,
        )
    except ConvertFailure as exc:
        raise RunFailure(exc.code, exc.message) from exc

    stitch_outcome: StitchOutcome | None = None
    if convert_outcome.status != "cancelled":
        stitch_emit = _wrap_emit_for_stitch(
            emit,
            completed_offset=convert_total,
            weighted_total=stitch_weighted_total,
            combined_total=combined_total,
        )
        try:
            stitch_outcome = run_stitch(
                resolved_work_dir,
                out_dir,
                run_id=run_id,
                overwrite=False,
                allow_partial=True,
                jobs=jobs,
                cancel=cancel,
                emit=stitch_emit,
            )
        except StitchError as exc:
            raise RunFailure(exc.code, exc.message) from exc

        _supersede_this_run(out_dir, run_id=run_id, emit=emit)

    if convert_outcome.status == "cancelled" or (
        stitch_outcome is not None and stitch_outcome.status == "cancelled"
    ):
        status = "cancelled"
    elif convert_outcome.status == "partial" or (
        stitch_outcome is not None and stitch_outcome.status == "partial"
    ):
        status = "partial"
    else:
        status = "complete"

    # Deleting a folder the user pointed at is never this program's
    # decision (section 3.6): only a work dir this run created is ever
    # removed, and only on complete success with no explicit request to
    # keep it.
    keep = status != "complete" or keep_intermediates or not created_work_dir
    warn = status != "complete" or keep_intermediates
    if keep:
        if warn:
            emit(
                WarningEvent(
                    run_id=run_id,
                    code=Code.INTERMEDIATES_KEPT,
                    message=f"intermediates kept at {resolved_work_dir}",
                )
            )
    else:
        shutil.rmtree(resolved_work_dir, ignore_errors=True)

    return RunOutcome(
        run_id=run_id,
        status=status,
        convert=convert_outcome,
        stitch=stitch_outcome,
        work_dir=resolved_work_dir,
        work_dir_kept=keep,
    )
