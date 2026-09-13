"""``capture check``: run the stitch solve phase without compositing."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path

from scanny_boy.library import repo
from scanny_boy.cancellation import CancellationToken
from scanny_boy.events import Code, Event
from scanny_boy.manifest import BadManifestError, load_manifest
from scanny_boy.registration import StitchError
from scanny_boy.roll_manifest import NegativeRecord

EmitFn = Callable[[Event], None]


@dataclasses.dataclass(frozen=True)
class CaptureCheckFailure(Exception):
    code: Code
    message: str

    def __init__(self, code: Code, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclasses.dataclass(frozen=True)
class CaptureCheckOutcome:
    passed: bool
    code: str | None
    message: str | None
    global_rms_px: float | None
    used_clahe_fallback: bool


def run_capture_check(
    work_dir: Path,
    *,
    rig_profile_id: str | None = None,
) -> CaptureCheckOutcome:
    """Run detection, matching, and the layout solve for one work folder."""
    from scanny_boy import concurrency
    from scanny_boy.stitch_pipeline import (
        _solve_negative,
        _SolvedNegative,
        _StitchProgress,
        _verify_intermediates,
    )

    work_dir = Path(work_dir)
    try:
        work_manifest = load_manifest(work_dir)
    except BadManifestError as exc:
        raise CaptureCheckFailure(exc.code, exc.message) from exc

    groups = [g for g in work_manifest.groups if g.status == "completed"]
    if not groups:
        raise CaptureCheckFailure(
            Code.WORK_MANIFEST_UNUSABLE,
            "the work manifest records no completed negatives to check",
        )
    group = groups[0]
    _verify_intermediates(work_dir, group)

    from scanny_boy import calibration

    profile = None
    if rig_profile_id is not None:
        try:
            profile = repo.load_rig_profile(rig_profile_id)
        except calibration.RigError as exc:
            raise CaptureCheckFailure(exc.code, exc.message) from exc

    record = NegativeRecord(
        negative_id="capture-check",
        run_id="capture-check",
        members=list(group.members),
        expected_output=f"{Path(group.members[0]).stem}.tif",
        fill_color=(0, 0, 0),
    )
    entry = _SolvedNegative(group=group, record=record, pairs=[])
    progress = _StitchProgress(total=1, emit=lambda _event: None, run_id="capture-check")
    try:
        workers = concurrency.resolve_worker_count(work_manifest.shots_per_negative, None)
    except concurrency.MemoryBudgetError as exc:
        raise CaptureCheckFailure(exc.code, exc.message) from exc

    try:
        layout, _frame_size, _ca_maps = _solve_negative(
            work_dir,
            entry,
            grid=work_manifest.grid_spec,
            workers=workers,
            cancel=CancellationToken(),
            progress=progress,
            source_index=0,
            on_warning=lambda _code, _message: None,
            profile=profile,
        )
    except StitchError as exc:
        return CaptureCheckOutcome(
            passed=False,
            code=exc.code.value,
            message=exc.message,
            global_rms_px=None,
            used_clahe_fallback=entry.record.used_clahe_fallback,
        )

    global_rms = entry.record.global_rms_px
    if global_rms is None and layout is not None:
        global_rms = layout.global_rms_px
    return CaptureCheckOutcome(
        passed=True,
        code=None,
        message=None,
        global_rms_px=global_rms,
        used_clahe_fallback=entry.record.used_clahe_fallback,
    )
