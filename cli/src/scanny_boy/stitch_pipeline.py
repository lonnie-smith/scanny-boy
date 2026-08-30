"""The `stitch` command: read a work directory's Phase 1 intermediates and
publish one stitched TIFF per negative into the output folder.

See `docs/PHASE2_IMPLEMENTATION_PLAN.md` section 5's Chunk P2-6 entry for the
order of operations, which is not negotiable because each step protects the
next, and section 3.5 for the failure, cancellation, and cleanup rules a
negative inherits from Phase 1's group-failure rule.

One deviation from that entry's numbering, decided with the user: every
negative's layout is solved *before* the disk check, because section 3.8's
free-space formula needs `canvas_width x canvas_height` and a canvas does not
exist until its layout is solved. Solving is cheap next to compositing and
allocates nothing canvas-sized, so the guard still runs before anything large
is written or allocated.
"""

from __future__ import annotations

import dataclasses
import datetime
import math
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np
import tifffile
import tifftools
from tifftools.constants import Tag

from scanny_boy import composite as composite_module
from scanny_boy import concurrency, disk_check, hashing, registration, tiff_exif
from scanny_boy.cancellation import CancellationToken, CancelledError
from scanny_boy.composite import (
    FILL_COLOR,
    MAX_OVERLAP_MAD,
    check_memory_budget,
    check_output_size,
    composite,
    estimate_peak_bytes,
)
from scanny_boy.detection import (
    DETECTION_LONG_EDGE,
    USE_CLAHE,
    build_detection_image,
)
from scanny_boy.events import (
    Code,
    NegativeDone,
    NegativeFailed,
    PipelineStep,
    Progress,
    Stage,
    WarningEvent,
)
from scanny_boy.icc_profile import load_icc_profile
from scanny_boy.layout import (
    MAX_GLOBAL_RMS_PX,
    STRIP_SPREAD_RATIO,
    Layout,
    largest_valid_rect,
    solve_layout,
)
from scanny_boy.manifest import (
    BadManifestError,
    GroupRecord,
    Manifest,
    load_manifest,
)
from scanny_boy.output_folder import (
    ROLL_RULES,
    OutputFolderError,
    apply_recovery_cleanup,
    plan_rerun,
    staging_dir_path,
    validate_writable,
)
from scanny_boy.registration import (
    DETECTOR,
    MAX_PAIR_RMS_PX,
    MIN_PAIR_INLIER_RATIO,
    MIN_PAIR_INLIERS,
    RANSAC_REPROJ_PX,
    RATIO_TEST,
    SCALE_DRIFT_FAIL,
    SCALE_DRIFT_WARN,
    PairResult,
    StitchError,
    detect_features,
    register_pair,
)
from scanny_boy.roll_manifest import (
    ROLL_MANIFEST_FILENAME,
    CaptureTime,
    FrameRecord,
    NegativeRecord,
    PairRecord,
    RollInvariantMismatchError,
    RollInvariants,
    RollManifest,
    RollManifestUnsupportedError,
    RunRecord,
    allocate_output_name,
    append_run,
    current_roll_manifest_path,
    estimate_roll_manifest_size,
    format_negative_id,
    merge_sources,
    write_roll_manifest,
)
from scanny_boy.stitched_tiff import stitched_image_description, write_stitched_tiff
from scanny_boy.tiff_writer import BaseTiffTags, software_tag_value

EmitFn = Any

# Section 3.8's stitch-stage free-space formula.
_DISK_HEADROOM = 1.05
_DISK_SAFETY_MARGIN = 1.20

# Progress steps this stage emits, per section 3.9's `PipelineStep`
# additions. Per frame: load, detect (solving) and warp (compositing).
# Per negative: match, solve (solving) and blend, write_stitched
# (compositing). `run`'s combined span is Chunk P2-7's business; these are
# the concrete step boundaries that actually occur here.
_STEPS_PER_FRAME = 3
_STEPS_PER_NEGATIVE = 4


@dataclasses.dataclass(frozen=True)
class StitchOutcome:
    status: str  # "complete" | "partial" | "cancelled"
    published: list[str]
    failed: list[str]


@dataclasses.dataclass
class _SolvedNegative:
    """One negative after the solving phase: either a layout ready to
    composite, or the failure that stopped it. Held for every negative so
    the disk check can see every canvas before anything is written."""

    group: GroupRecord
    record: NegativeRecord
    pairs: list[PairResult]
    layout: Layout | None = None
    frame_size: tuple[int, int] | None = None  # (height, width)
    failure: tuple[Code, str] | None = None


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None).isoformat() + "Z"


def _intermediate_name(member: str) -> str:
    """Phase 1 names each intermediate after its source frame, so this maps a
    group member back to the file `convert` wrote for it.

    Not the published name: section 3.4 moved that to
    `roll_manifest.allocate_output_name`, which is now the only place a
    published name is chosen."""
    return f"{Path(member).stem}.tif"


def _stitch_params() -> dict[str, Any]:
    """Section 3.7: the roll manifest records "the stitch parameters and
    every threshold in force", so a file can be interpreted, and the
    section 3.12.2 thresholds revisited, without knowing which build wrote
    it."""
    return {
        "detection_long_edge": DETECTION_LONG_EDGE,
        "use_clahe": USE_CLAHE,
        "detector": DETECTOR,
        "ratio_test": RATIO_TEST,
        "ransac_reproj_px": RANSAC_REPROJ_PX,
        "min_pair_inliers": MIN_PAIR_INLIERS,
        "min_pair_inlier_ratio": MIN_PAIR_INLIER_RATIO,
        "max_pair_rms_px": MAX_PAIR_RMS_PX,
        "scale_drift_warn": SCALE_DRIFT_WARN,
        "scale_drift_fail": SCALE_DRIFT_FAIL,
        "max_overlap_mad": MAX_OVERLAP_MAD,
        "max_global_rms_px": MAX_GLOBAL_RMS_PX,
        "strip_spread_ratio": STRIP_SPREAD_RATIO,
        "interpolation": "INTER_LANCZOS4",
        "mask_erode_px": composite_module.MASK_ERODE_PX,
        "memory_safety_factor": composite_module.MEMORY_SAFETY_FACTOR,
        "fill_color": list(FILL_COLOR),
    }


class _StitchProgress:
    """Counts emitted stitch steps. Single-threaded: feature detection runs
    on a pool, but only the parent thread advances progress, so unlike
    Phase 1's `_ProgressReporter` this needs no lock."""

    def __init__(self, *, total: int, emit: EmitFn, run_id: str) -> None:
        self._total = total
        self._emit = emit
        self._run_id = run_id
        self._completed = 0

    def advance(self, source_index: int, step: PipelineStep) -> None:
        self._completed += 1
        self._emit(
            Progress(
                run_id=self._run_id,
                source_index=source_index,
                step=step,
                completed=self._completed,
                total=self._total,
                stage=Stage.STITCH,
            )
        )


def _read_intermediate(path: Path) -> np.ndarray:
    return tifffile.imread(path)


def _read_intermediate_size(path: Path) -> tuple[int, int]:
    """`(height, width)` from the TIFF header alone, without decoding any
    pixels — the disk and memory guards need dimensions before they can
    afford to load a frame."""
    with tifffile.TiffFile(path) as tf:
        page = tf.pages[0]
        return int(page.imagelength), int(page.imagewidth)


def _rational(value: Any) -> Fraction:
    return Fraction(int(value[0]), int(value[1]))


def _read_curated_exif(path: Path) -> tuple[tiff_exif.NestedExifFields, str | None, str | None]:
    """Read back the curated EXIF Phase 1 wrote into one intermediate,
    plus its `Make`/`Model`.

    Section 3.11: curated EXIF comes from the negative's first frame in
    canonical order, and the synthetic `DateTimeOriginal` is the one
    Phase 1 computed for that frame. That value lives in the intermediate
    itself and nowhere else — the work manifest's `curated_metadata`
    carries the exposure settings but no per-frame timestamp — so the
    frame's own file is the authoritative source.
    """
    info = tifftools.read_tiff(str(path))
    ifd0 = info["ifds"][0]["tags"]
    exif = info["ifds"][0]["tags"][Tag.ExifIFD.value]["ifds"][0][0]["tags"]

    def ascii_tag(tags: dict, code: int) -> str | None:
        entry = tags.get(code)
        return None if entry is None else str(entry["data"])

    date_text = ascii_tag(exif, tiff_exif.DATE_TIME_ORIGINAL)
    if date_text is None:
        raise StitchError(
            Code.INTERMEDIATE_CHANGED,
            f"{path.name} has no DateTimeOriginal; it was not written by this program",
        )
    date_time_original = datetime.datetime.strptime(  # noqa: DTZ007
        date_text, "%Y:%m:%d %H:%M:%S"
    )
    subsec = ascii_tag(exif, tiff_exif.SUBSEC_TIME_ORIGINAL)
    if subsec:
        # `film_date.format_subsec` writes the fractional digits with
        # trailing zeros stripped, so "45" means 450000 microseconds.
        date_time_original = date_time_original.replace(
            microsecond=int(subsec.ljust(6, "0"))
        )

    fields = tiff_exif.NestedExifFields(
        date_time_original=date_time_original,
        exposure_time=_rational(exif[tiff_exif.EXPOSURE_TIME]["data"]),
        f_number=_rational(exif[tiff_exif.F_NUMBER]["data"]),
        iso=int(exif[tiff_exif.PHOTOGRAPHIC_SENSITIVITY]["data"][0]),
        focal_length=_rational(exif[tiff_exif.FOCAL_LENGTH]["data"]),
        lens_model=ascii_tag(exif, tiff_exif.LENS_MODEL),
        date_time_digitized=ascii_tag(exif, tiff_exif.DATE_TIME_DIGITIZED),
        subsec_time_digitized=ascii_tag(exif, tiff_exif.SUBSEC_TIME_DIGITIZED),
        offset_time_digitized=ascii_tag(exif, tiff_exif.OFFSET_TIME_DIGITIZED),
    )
    return (
        fields,
        ascii_tag(ifd0, Tag.Make.value),
        ascii_tag(ifd0, Tag.Model.value),
    )


def _verify_intermediates(work_dir: Path, group: GroupRecord) -> None:
    """Step 4: every intermediate exists and still matches the work
    manifest's size and SHA-256. Phase 1's section 3.7 requires exactly
    this, and it is the one guarantee section 3.6's `--allow-partial`
    amendment does not relax."""
    for output in group.outputs:
        path = work_dir / output.name
        if not path.exists():
            raise StitchError(
                Code.INTERMEDIATE_MISSING,
                f"{output.name} is named by the work manifest but is not in {work_dir}",
            )
        size = path.stat().st_size
        if size != output.size:
            raise StitchError(
                Code.INTERMEDIATE_CHANGED,
                f"{output.name} is {size} bytes, but the work manifest records "
                f"{output.size}",
            )
        digest = hashing.sha256_file(path)
        if digest != output.sha256:
            raise StitchError(
                Code.INTERMEDIATE_CHANGED,
                f"{output.name} does not match the SHA-256 recorded in the work manifest",
            )


def _intermediate_paths(work_dir: Path, group: GroupRecord) -> list[Path]:
    """The group's intermediates in canonical member order. Phase 1 names
    each output after its source frame, so this is `group.members` mapped
    through the same rule."""
    by_name = {output.name: work_dir / output.name for output in group.outputs}
    return [by_name[_intermediate_name(member)] for member in group.members]


def _detect_all(
    paths: list[Path], workers: int, cancel: CancellationToken
) -> list[registration.FrameFeatures]:
    """Build every frame's detection image and detect its features.

    Each frame's full-resolution pixels are released as soon as its
    detection image exists, so peak residency here is one intermediate,
    not the whole negative — `jobs` bounds this step and nothing else
    (section 3.6).
    """

    def one(path: Path) -> registration.FrameFeatures:
        cancel.raise_if_cancelled()
        pixels = _read_intermediate(path)
        detection = build_detection_image(
            pixels, long_edge=DETECTION_LONG_EDGE, clahe=USE_CLAHE
        )
        del pixels
        return detect_features(detection, name=path.name)

    if workers <= 1:
        return [one(path) for path in paths]

    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="scanny-detect")
    try:
        return list(pool.map(one, paths))
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def _solve_negative(
    work_dir: Path,
    entry: _SolvedNegative,
    *,
    workers: int,
    cancel: CancellationToken,
    progress: _StitchProgress,
    source_index: int,
    on_warning,
) -> tuple[Layout, tuple[int, int]]:
    """Detection, registration, and the global solve for one negative.
    Raises `StitchError` for anything that fails the negative.

    Writes the pairs it computes onto `entry` before any gate can raise, so
    a negative that fails still records its per-pair section 3.4 metrics:
    those numbers are exactly what a reader needs to see *why* it failed.
    """
    group = entry.group
    paths = _intermediate_paths(work_dir, group)
    frame_size = _read_intermediate_size(paths[0])

    for _ in paths:
        progress.advance(source_index, PipelineStep.LOAD)

    features = _detect_all(paths, workers, cancel)
    for _ in paths:
        progress.advance(source_index, PipelineStep.DETECT)
    cancel.raise_if_cancelled()

    pairs: list[PairResult] = []
    for i in range(len(features)):
        for j in range(i + 1, len(features)):
            cancel.raise_if_cancelled()
            pairs.append(register_pair(features[i], features[j]))
    entry.pairs = pairs
    entry.record.pairs = _pair_records(pairs)
    progress.advance(source_index, PipelineStep.MATCH)

    for pair in pairs:
        if pair.accepted and pair.scale_drift > SCALE_DRIFT_WARN:
            on_warning(
                Code.STITCH_SCALE_DRIFT,
                f"{group.group_id}: pair {pair.a}-{pair.b} scale drift "
                f"{pair.scale_drift:.5f} exceeds {SCALE_DRIFT_WARN}",
            )

    names = [path.name for path in paths]
    layout = solve_layout(names, frame_size, pairs)
    progress.advance(source_index, PipelineStep.SOLVE)
    cancel.raise_if_cancelled()

    if layout.global_rms_px > MAX_GLOBAL_RMS_PX:
        raise StitchError(
            Code.STITCH_RESIDUAL_TOO_HIGH,
            f"{group.group_id}: global RMS {layout.global_rms_px:.2f}px exceeds "
            f"{MAX_GLOBAL_RMS_PX}px",
        )

    if layout.strip_spread_ratio > STRIP_SPREAD_RATIO:
        on_warning(
            Code.STITCH_LAYOUT_UNEXPECTED,
            f"{group.group_id}: solved layout is not strip-shaped "
            f"(spread ratio {layout.strip_spread_ratio:.4f} exceeds "
            f"{STRIP_SPREAD_RATIO})",
        )

    check_output_size(layout.canvas_size, on_warning=on_warning)
    check_memory_budget(
        estimate_peak_bytes(
            layout.canvas_size,
            frame_size,
            (layout.canvas_size[1], layout.canvas_size[0]),
        )
    )

    return layout, frame_size


def _finite(value: float) -> float:
    """A rejected pair can carry an infinite residual or scale drift, and
    JSON has no way to spell infinity. The roll manifest is a record of what
    happened, so those become the largest finite float rather than being
    dropped — the pair's `accepted: false` is what actually matters, and a
    reader comparing against a threshold still sees "far too large"."""
    return sys.float_info.max if math.isinf(value) else value


def _pair_records(pairs: list[PairResult]) -> list[PairRecord]:
    return [
        PairRecord(
            a=pair.a,
            b=pair.b,
            inliers=pair.inliers,
            good_matches=pair.good_matches,
            inlier_ratio=pair.inlier_ratio,
            rms_residual_px=_finite(pair.rms_residual_px),
            scale_drift=_finite(pair.scale_drift),
            overlap_fraction=pair.overlap_fraction,
            overlap_mad=pair.overlap_mad,
            accepted=pair.accepted,
        )
        for pair in pairs
    ]


def _required_free_bytes(canvases: list[tuple[int, int]], manifest_size: int) -> int:
    """Section 3.8's stitch-stage formula. `S` is computed per negative
    rather than once, because a roll's canvases differ; the lone extra `S`
    covers the one staged file held alongside the finished ones and uses
    the largest canvas, which is the one that could actually be in flight."""
    per_negative = [
        math.ceil(width * height * 3 * 2 * _DISK_HEADROOM) for width, height in canvases
    ]
    largest = max(per_negative, default=0)
    d = max(disk_check.MIB, manifest_size)
    return math.ceil((sum(per_negative) + largest + d) * _DISK_SAFETY_MARGIN)


def run_stitch(
    work_dir: Path,
    out_dir: Path,
    *,
    run_id: str,
    overwrite: bool,
    allow_partial: bool,
    jobs: int | None,
    cancel: CancellationToken,
    emit: EmitFn,
) -> StitchOutcome:
    """Read the Phase 1 manifest in `work_dir`, verify every intermediate,
    and publish one stitched TIFF per negative into `out_dir`.

    Raises `StitchError` for any run-level validation problem. A negative
    that cannot be stitched fails alone: its failure is recorded in the
    roll manifest, reported through `NegativeFailed`, and the run continues
    and ends `partial` (section 3.5). A cancelled negative is abandoned,
    not failed, and emits no `NegativeFailed`.

    `overwrite` is accepted and unused: section 3.4 makes a roll additive, so
    a stitch never replaces a published file. Section 3.5 reserves the flag
    for the `--negatives` re-stitch path, which arrives in Chunk P3-5.
    """
    work_dir = Path(work_dir)
    out_dir = Path(out_dir)

    def on_warning(code: Code, message: str) -> None:
        emit(WarningEvent(run_id=run_id, code=code, message=message))

    # 1. --work and --out must be different directories (section 3.6).
    if work_dir.resolve() == out_dir.resolve():
        raise StitchError(
            Code.WORK_SAME_AS_OUTPUT,
            "the work directory resolves to the same folder as the output folder",
        )

    # 2. The output folder must exist and be writable.
    try:
        validate_writable(out_dir)
    except OutputFolderError as exc:
        raise StitchError(exc.code, exc.message) from exc

    # 3. The work manifest must be usable (section 3.6's amendment to
    #    Phase 1's section 3.7).
    try:
        work_manifest = load_manifest(work_dir)
    except BadManifestError as exc:
        raise StitchError(exc.code, exc.message) from exc

    if work_manifest.status in ("running", "cancelled"):
        raise StitchError(
            Code.WORK_MANIFEST_UNUSABLE,
            f"the work manifest's status is {work_manifest.status!r}; only a "
            "complete or partial conversion can be stitched",
        )
    if work_manifest.status == "partial" and not allow_partial:
        raise StitchError(
            Code.WORK_MANIFEST_UNUSABLE,
            "the work manifest is 'partial'; pass --allow-partial to stitch only "
            "the negatives that converted successfully",
        )

    groups = [g for g in work_manifest.groups if g.status == "completed"]
    if not groups:
        raise StitchError(
            Code.WORK_MANIFEST_UNUSABLE,
            "the work manifest records no completed negatives to stitch",
        )

    # 4. Every intermediate must be present and unchanged.
    for group in groups:
        _verify_intermediates(work_dir, group)

    # 5. The roll must already exist (section 5.4 decision 1: `stitch` never
    #    creates one) and this run's parameters must match its invariants.
    if not current_roll_manifest_path(out_dir).exists():
        raise StitchError(
            Code.ROLL_NOT_FOUND,
            f"{out_dir} has no {ROLL_MANIFEST_FILENAME}; create the roll first",
        )
    invariants = RollInvariants(
        shots_per_negative=work_manifest.shots_per_negative,
        processing_params=work_manifest.processing_params,
        icc_profile_sha256=work_manifest.icc_profile.get("sha256", ""),
        stitch_params=_stitch_params(),
    )
    try:
        plan = plan_rerun(out_dir, invariants, rules=ROLL_RULES)
    except (
        OutputFolderError,
        BadManifestError,
        RollManifestUnsupportedError,
        RollInvariantMismatchError,
    ) as exc:
        raise StitchError(exc.code, exc.message) from exc

    # Section 5.4 decision 3: there is no `OUTPUT_CONFLICT` here. Section
    # 3.4's naming rule makes one impossible — `allocate_output_name` cannot
    # return a name another negative already claims — so the only outputs
    # `plan.conflicting_outputs` can name are earlier runs' files this run
    # does not touch. Recovery cleanup of never-finished negatives stays.
    apply_recovery_cleanup(out_dir, plan)

    roll = plan.existing_manifest
    assert roll is not None
    run_record, records_by_group = _append_this_run(
        roll, work_manifest, groups, run_id, invariants, work_dir
    )

    try:
        workers = concurrency.resolve_worker_count(
            work_manifest.shots_per_negative, jobs
        )
    except concurrency.MemoryBudgetError as exc:
        raise StitchError(exc.code, exc.message) from exc

    frame_count = sum(len(g.members) for g in groups)
    progress = _StitchProgress(
        total=frame_count * _STEPS_PER_FRAME + len(groups) * _STEPS_PER_NEGATIVE,
        emit=emit,
        run_id=run_id,
    )
    source_index_by_group = {g.group_id: i for i, g in enumerate(groups)}

    # Solve every layout before the disk check, so section 3.8's formula
    # has real canvas sizes (see the module docstring).
    solved: list[_SolvedNegative] = []
    cancelled = False
    for group in groups:
        record = records_by_group[group.group_id]
        if cancel.cancelled:
            cancelled = True
            break
        entry = _SolvedNegative(group=group, record=record, pairs=[])
        try:
            layout, frame_size = _solve_negative(
                work_dir,
                entry,
                workers=workers,
                cancel=cancel,
                progress=progress,
                source_index=source_index_by_group[group.group_id],
                on_warning=on_warning,
            )
        except CancelledError:
            cancelled = True
            break
        except StitchError as exc:
            entry.failure = (exc.code, exc.message)
            solved.append(entry)
            continue
        except Exception as exc:  # noqa: BLE001
            entry.failure = (Code.STITCH_FAILED, str(exc))
            solved.append(entry)
            continue
        entry.layout = layout
        entry.frame_size = frame_size
        solved.append(entry)

    if not cancelled:
        # 6. Disk check on the output volume, now that canvases are known.
        canvases = [e.layout.canvas_size for e in solved if e.layout is not None]
        required = _required_free_bytes(
            canvases, estimate_roll_manifest_size(roll)
        )
        try:
            disk_check.check_disk_space(out_dir, required)
        except disk_check.DiskCheckError as exc:
            raise StitchError(exc.code, exc.message) from exc

    # 7. Write the `running` roll manifest before publishing anything.
    write_roll_manifest(out_dir, roll)

    published: list[str] = []
    failed: list[str] = []

    # 8. Composite and publish, negative by negative, in canonical order.
    for entry in solved:
        if cancelled or cancel.cancelled:
            cancelled = True
            break
        if entry.failure is not None:
            code, message = entry.failure
            _record_failure(out_dir, roll, entry.record, code, message, emit, run_id)
            failed.append(entry.group.group_id)
            continue

        try:
            _composite_and_publish(
                work_dir=work_dir,
                out_dir=out_dir,
                entry=entry,
                roll=roll,
                run_id=run_id,
                cancel=cancel,
                emit=emit,
                progress=progress,
                source_index=source_index_by_group[entry.group.group_id],
            )
        except CancelledError:
            cancelled = True
            break
        except StitchError as exc:
            _record_failure(
                out_dir, roll, entry.record, exc.code, exc.message, emit, run_id
            )
            failed.append(entry.group.group_id)
            continue
        except Exception as exc:  # noqa: BLE001
            _record_failure(
                out_dir, roll, entry.record, Code.STITCH_FAILED, str(exc), emit, run_id
            )
            failed.append(entry.group.group_id)
            continue

        published.append(entry.record.expected_output)

    if cancelled:
        status = "cancelled"
    elif all(r.status == "completed" for r in records_by_group.values()):
        status = "complete"
    else:
        status = "partial"

    # The status belongs to *this run*, not to the roll: a roll is additive
    # and has no single status (section 3.3).
    run_record.status = status
    run_record.finished_at = _now_iso()
    write_roll_manifest(out_dir, roll)

    return StitchOutcome(status=status, published=published, failed=failed)


def _append_this_run(
    roll: RollManifest,
    work_manifest: Manifest,
    groups: list[GroupRecord],
    run_id: str,
    invariants: RollInvariants,
    work_dir: Path,
) -> tuple[RunRecord, dict[str, NegativeRecord]]:
    """Add this stitch to the roll: its run record, its sources, and one
    `pending` negative per group, all per sections 3.3 and 3.4.

    Section 5.4 decision 1: the first run establishes the three invariants an
    empty roll cannot know. `check_roll_invariants` has already passed, so
    assigning them here is a seeding, never an overwrite.

    Returns the run record and this run's negatives keyed by work-manifest
    group id, because the group id is what the solving loop carries and
    `negative_id` is now the roll's name for the same thing, not Phase 1's.
    """
    if not roll.runs:
        roll.processing_params = invariants.processing_params
        roll.stitch_params = invariants.stitch_params
        roll.icc_profile = work_manifest.icc_profile

    run_record = RunRecord(
        run_id=run_id,
        kind="stitch",
        status="running",
        started_at=_now_iso(),
        convert_run_id=work_manifest.run_id,
        # Section 3.3: a `stitch` has no input folder of its own — it reads a
        # work directory someone else's `convert` produced.
        input_folder=None,
        source_order=list(work_manifest.source_order),
        # Section 3.6: `stitch` never deletes the work directory it was
        # given, so the intermediates are kept by definition and the roll
        # records where, which is what makes a re-stitch target discoverable.
        work_dir=str(work_dir),
        finished_at=None,
    )
    append_run(roll, run_record)
    merge_sources(roll, work_manifest.sources, run_id)

    records: dict[str, NegativeRecord] = {}
    for index, group in enumerate(groups, start=1):
        negative_id = format_negative_id(run_record.short_id, index)
        record = NegativeRecord(
            negative_id=negative_id,
            run_id=run_id,
            members=list(group.members),
            expected_output=allocate_output_name(roll, group.members[0], negative_id),
            fill_color=FILL_COLOR,
        )
        roll.negatives.append(record)
        records[group.group_id] = record
    return run_record, records


def _record_failure(
    out_dir: Path,
    roll: RollManifest,
    record: NegativeRecord,
    code: Code,
    message: str,
    emit: EmitFn,
    run_id: str,
) -> None:
    """Section 3.5: a negative that cannot be stitched fails alone. Its
    failure is recorded, the run continues, and the run ends `partial`."""
    record.status = "failed"
    record.error_code = code.value
    record.error_message = message
    write_roll_manifest(out_dir, roll)
    emit(
        NegativeFailed(
            run_id=run_id, negative_id=record.negative_id, code=code, message=message
        )
    )


def _composite_and_publish(
    *,
    work_dir: Path,
    out_dir: Path,
    entry: _SolvedNegative,
    roll: RollManifest,
    run_id: str,
    cancel: CancellationToken,
    emit: EmitFn,
    progress: _StitchProgress,
    source_index: int,
) -> None:
    """Composite one negative, apply the remaining section 3.4 gates, and
    stage-then-publish it atomically, exactly as Phase 1 publishes a group."""
    layout = entry.layout
    assert layout is not None
    record = entry.record
    paths = _intermediate_paths(work_dir, entry.group)
    by_name = {path.name: path for path in paths}

    staging_dir = staging_dir_path(out_dir, run_id, record.negative_id)
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True)

    try:
        cancel.raise_if_cancelled()

        def load_frame(name: str) -> np.ndarray:
            cancel.raise_if_cancelled()
            return _read_intermediate(by_name[name])

        def on_frame_warped() -> None:
            progress.advance(source_index, PipelineStep.WARP)

        result = composite(
            layout, load_frame, cancel=cancel, on_progress=on_frame_warped
        )
        progress.advance(source_index, PipelineStep.BLEND)
        cancel.raise_if_cancelled()

        # Fold the measured photometric numbers back into the pairs, then
        # apply the honest gate (section 3.4).
        merged_pairs = [
            dataclasses.replace(
                pair,
                overlap_fraction=result.overlap_fraction.get((pair.a, pair.b)),
                overlap_mad=result.overlap_mad.get((pair.a, pair.b)),
            )
            for pair in entry.pairs
        ]
        record.pairs = _pair_records(merged_pairs)
        record.frames = [
            FrameRecord(
                name=placement.name,
                rotation_deg=placement.rotation_deg,
                translation=(placement.translation[0], placement.translation[1]),
            )
            for placement in layout.placements
        ]
        record.global_rms_px = layout.global_rms_px
        record.canvas = layout.canvas_size

        measured = [p.overlap_mad for p in merged_pairs if p.accepted and p.overlap_mad is not None]
        worst = max(measured, default=0.0)
        if worst > MAX_OVERLAP_MAD:
            raise StitchError(
                Code.STITCH_RESIDUAL_TOO_HIGH,
                f"{record.negative_id}: overlap MAD {worst:.4f} exceeds "
                f"{MAX_OVERLAP_MAD}",
            )

        record.valid_rect = largest_valid_rect(layout, entry.frame_size)

        exif, make, model = _read_curated_exif(paths[0])

        # Section 5.4 decision 4: the roll records the capture time the
        # negative's first frame actually carries, which is exactly the value
        # just read. `intended_`, `applied_`, and `date_override` stay null —
        # they are the metadata stage's, not the stitch stage's (section 3.8).
        record.capture_time = CaptureTime(
            source_datetime_original=exif.date_time_original.isoformat()
        )

        staged_path = staging_dir / record.expected_output
        write_stitched_tiff(
            staged_path,
            result.image,
            tags=BaseTiffTags(
                description=stitched_image_description(
                    entry.group.members[0], len(entry.group.members)
                ),
                software=software_tag_value(),
                conversion_time=datetime.datetime.now(datetime.UTC).replace(tzinfo=None),
                icc_profile=b"",  # replaced by write_stitched_tiff's icc_bytes
                make=make,
                model=model,
            ),
            exif=exif,
            icc_bytes=load_icc_profile(),
        )
        progress.advance(source_index, PipelineStep.WRITE_STITCHED)

        height, width = result.image.shape[0], result.image.shape[1]
        del result

        cancel.raise_if_cancelled()

        dest = out_dir / record.expected_output
        staged_path.replace(dest)
        size = dest.stat().st_size
        digest = hashing.sha256_file(dest)
        record.output = {
            "name": record.expected_output,
            "size": size,
            "sha256": digest,
            "width": width,
            "height": height,
        }
        record.status = "completed"
        write_roll_manifest(out_dir, roll)
        emit(
            NegativeDone(
                run_id=run_id,
                negative_id=record.negative_id,
                output=record.expected_output,
                width=width,
                height=height,
                global_rms_px=layout.global_rms_px,
                max_overlap_mad=worst,
            )
        )
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
