"""Group-by-group conversion pipeline: decode -> base TIFF -> nested
EXIF -> staging -> publish -> manifest.

See `docs/IMPLEMENTATION_PLAN.md` section 3.6 (staging, overwriting, and
grouping), section 3.7 (manifest), section 3.8 (concurrency and
cancellation), and section 3.9 (disk checks).

Per section 3.6, "never publish only part of a group": every frame in a
group is decoded and written into that group's staging directory first
(`_stage_group`, wrapped in a single try/except — an ordinary per-frame
failure there deletes the whole staging directory and fails the group).
Publishing — moving each finished file from staging into the output folder
(`_publish_group`) — happens only after every frame in the group has staged
successfully, and is deliberately *not* wrapped in that same handler:
section 3.6 frames this move as the one place a real crash can leave a
group half-published, and recovery for that is a rerun's job
(`output_folder.plan_rerun` + `apply_recovery_cleanup`), not this run's.

Concurrency (section 3.8) lives entirely inside one group. `_stage_group`
either runs the frames serially — `--jobs 1`, which never constructs an
executor at all — or submits them to a `ThreadPoolExecutor`. Workers return
`_StagedFrame`, which carries a name, an index, and a path: "Do not return
full image arrays to the parent." Publishing stays on the main thread and
always walks the group in member order, so `item_done` order is stable even
though frames finish out of order.

Cancellation is cooperative. Workers check `CancellationToken` at the three
step boundaries only (never mid-decode), and `_stage_group`'s `finally`
shuts the pool down with `wait=True, cancel_futures=True` — queued frames
are dropped, running frames are allowed to finish their current call, and
only once every worker has stopped does the caller delete the staging
directory. That ordering is section 3.8's "Never delete a directory while a
worker may still write to it."
"""

from __future__ import annotations

import dataclasses
import datetime
import os
import shutil
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

from scanny_boy import (
    concurrency,
    disk_check,
    flatfield,
    hashing,
    normalization,
    raw_decode,
)
from scanny_boy.cancellation import CancellationToken, CancelledError
from scanny_boy.catalogue import (
    CaptureTimestamp,
    CatalogueError,
    compute_canonical_order,
    discover_catalogue,
    read_capture_timestamp,
)
from scanny_boy.consistency import ConsistencyError, check_consistency
from scanny_boy.events import (
    Code,
    Event,
    GroupDone,
    GroupFailed,
    ItemDone,
    PipelineStep,
    Progress,
    WarningEvent,
)
from scanny_boy.icc_profile import (
    IccProfileError,
    ProfileKind,
    load_icc_profile,
    profile_record,
)
from scanny_boy.manifest import (
    BadManifestError,
    CuratedMetadata,
    GroupRecord,
    Manifest,
    ManifestMismatchError,
    OutputRecord,
    SourceRecord,
    current_scanny_boy_version,
    estimate_manifest_size,
    write_manifest,
)
from scanny_boy.metadata import (
    DigitizedFields,
    SourceSettings,
    UnreadableRawError,
    UnsupportedRawError,
    choose_digitized_fields,
    read_digitization_fields,
    read_source_settings,
)
from scanny_boy.output_folder import (
    OutputFolderError,
    apply_recovery_cleanup,
    plan_rerun,
    staging_dir_path,
    validate_not_same_as_input,
    validate_writable,
)
from scanny_boy.selection import (
    SelectionUsageError,
    is_contiguous,
    nearest_valid_counts,
    order_selection,
)
from scanny_boy.selection import group as group_names
from scanny_boy.tiff_exif import NestedExifFields, TiffFinalizeError, finalize_tiff
from scanny_boy.tiff_writer import (
    BaseTiffTags,
    image_description,
    software_tag_value,
    write_base_tiff,
)

EmitFn = Callable[[Event], None]

STEPS_PER_FRAME = 3  # decode, write_tiff, add_metadata

# Ordinary per-frame failures: these fail one group and let later groups
# run. `CancelledError` is deliberately absent — a cancelled group is
# abandoned, not recorded as `failed`.
GROUP_FAILURE_EXCEPTIONS = (
    UnsupportedRawError,
    UnreadableRawError,
    TiffFinalizeError,
    OSError,
)


class ConvertFailure(Exception):
    def __init__(self, code: Code, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _SourceChangedError(Exception):
    """A selected source's size or modification time no longer matches the
    value recorded during hashing (section 3.7). There is no dedicated
    CONTRACT.md code for this; `UNREADABLE_RAW` ("File exists but could not
    be decoded") is the closest fit among the locked codes, since the file
    can no longer be trusted to decode to the hashed content."""

    def __init__(self, filename: str, reason: str) -> None:
        super().__init__(f"{filename} changed since it was hashed: {reason}")
        self.code = Code.UNREADABLE_RAW


def _output_name_for(source_filename: str) -> str:
    """`docs/IMPLEMENTATION_PLAN.md` section 3.4: `DSC_0042.NEF` becomes
    `DSC_0042.tif`."""
    return f"{Path(source_filename).stem}.tif"


@dataclasses.dataclass(frozen=True)
class ConvertOutcome:
    run_id: str
    status: str  # "complete" | "partial" | "cancelled"
    manifest: Manifest
    workers: int = 1


@dataclasses.dataclass(frozen=True)
class _ValidatedSelection:
    names: list[str]
    used_filename_fallback: bool


@dataclasses.dataclass(frozen=True)
class _StagedFrame:
    """Everything a thread worker hands back to the parent.

    Section 3.8: "Each worker opens one RAW, writes its staged TIFF, adds
    metadata, and returns only status and paths. Do not return full image
    arrays to the parent." A name, an index, and a path — the decoded
    array is dropped when `_stage_one_frame` returns. The per-channel
    sensor-clip fractions measured at decode ride along (they are three
    floats, not pixels).
    """

    member: str
    source_index: int
    final_path: Path
    scan_clip_fractions: tuple[float, float, float]


class _ProgressReporter:
    """Counts completed pipeline steps and emits `progress` events.

    Both jobs need a lock once frames run in parallel: the count must not
    be lost to a read-modify-write race, and `EventWriter.write` must not
    interleave two JSON lines on stdout. Holding one lock across the
    increment and the emit gives monotonically increasing `completed`
    values as a side effect.
    """

    def __init__(
        self,
        *,
        total_steps: int,
        emit: EmitFn,
        run_id: str,
        completed_offset: int = 0,
        total_override: int | None = None,
    ) -> None:
        # `completed_offset`/`total_override` let `run` (Chunk P2-7) report
        # one progress span across both the convert and stitch stages,
        # without changing a single number `convert` emits on its own:
        # both default to a no-op (offset 0, no override), which is what
        # `pipeline_test.py` passing unmodified proves.
        self._total = total_steps if total_override is None else total_override
        self._emit = emit
        self._run_id = run_id
        self._lock = threading.Lock()
        self._completed = completed_offset

    def advance(self, source_index: int, step: PipelineStep) -> None:
        with self._lock:
            self._completed += 1
            self._emit(
                Progress(
                    run_id=self._run_id,
                    source_index=source_index,
                    step=step,
                    completed=self._completed,
                    total=self._total,
                )
            )

    def warn(self, code: Code, message: str) -> None:
        """Emits a `warning` under the same lock as `advance`, so a worker
        thread's warning can never interleave with its progress line or
        another worker's event."""
        with self._lock:
            self._emit(WarningEvent(run_id=self._run_id, code=code, message=message))

    @property
    def completed(self) -> int:
        with self._lock:
            return self._completed


@dataclasses.dataclass(frozen=True)
class _GroupContext:
    """Read-only per-run state a worker needs to stage one frame.

    Frozen and shared by every worker: nothing here is mutated during a
    group, so no worker needs a lock to read it. The one mutable
    collaborator, `progress`, does its own locking.
    """

    input_dir: Path
    staging_dir: Path
    source_records_by_name: dict[str, SourceRecord]
    source_index_by_name: dict[str, int]
    real_time_by_name: dict[str, datetime.datetime]
    digitized_fields_by_name: dict[str, DigitizedFields]
    settings_by_name: dict[str, SourceSettings]
    icc_profile: bytes
    # The full-resolution gain map, resized once per run and shared
    # read-only across workers (docs/FLATFIELD_PLAN.md section 2.7). `None`
    # for a run without `--flatfield`.
    full_res_gain: np.ndarray | None
    # The profile's CA scales in "scale" mode, merged into every decode
    # (docs/GEOMETRIC_PLAN.md section 5.2). None otherwise.
    ca_scales: tuple[float, float] | None
    progress: _ProgressReporter
    cancel: CancellationToken


def _validate_selection(
    input_dir: Path, files: list[str], per_negative: int, emit: EmitFn, run_id: str
) -> _ValidatedSelection:
    try:
        names = discover_catalogue(input_dir)
    except CatalogueError as exc:
        raise ConvertFailure(Code.NO_FILES, str(exc)) from exc
    except OSError as exc:
        raise ConvertFailure(
            Code.NO_FILES, f"input folder does not exist or is not readable: {exc}"
        ) from exc

    if not names:
        raise ConvertFailure(Code.NO_FILES, f"no .nef files found in {input_dir}")

    order = compute_canonical_order(input_dir, names)
    if order.used_filename_fallback:
        emit(
            WarningEvent(
                run_id=run_id,
                code=Code.FILENAME_SORT_USED,
                message=(
                    "a catalogue file has no usable capture timestamp; sorted "
                    "the whole catalogue by filename instead"
                ),
            )
        )

    if not files:
        raise ConvertFailure(Code.NO_FILES, "no files were selected")

    try:
        selection = order_selection(order.order, files)
    except SelectionUsageError as exc:
        raise ConvertFailure(Code.NO_FILES, str(exc)) from exc

    if not is_contiguous(selection):
        raise ConvertFailure(
            Code.NON_CONTIGUOUS_SELECTION, "the selection has a gap in canonical order"
        )

    count = len(selection.names)
    if count % per_negative != 0:
        lower, upper = nearest_valid_counts(count, per_negative)
        raise ConvertFailure(
            Code.NOT_DIVISIBLE,
            f"{count} files is not divisible by {per_negative} per negative; "
            f"nearest valid counts are {lower} and {upper}",
        )

    return _ValidatedSelection(
        names=selection.names, used_filename_fallback=order.used_filename_fallback
    )


def _read_settings_and_check_consistency(
    input_dir: Path, selected: list[str]
) -> list[SourceSettings]:
    settings_list: list[SourceSettings] = []
    for name in selected:
        try:
            settings_list.append(read_source_settings(input_dir / name))
        except UnsupportedRawError as exc:
            raise ConvertFailure(
                Code.UNSUPPORTED_RAW,
                f"{name} cannot be read by LibRaw; Z f HE/HE* files must be "
                "recaptured as lossless-compressed NEFs",
            ) from exc
        except UnreadableRawError as exc:
            raise ConvertFailure(
                Code.UNREADABLE_RAW, f"{name} could not be decoded"
            ) from exc

    try:
        check_consistency(settings_list)
    except ConsistencyError as exc:
        raise ConvertFailure(exc.code, exc.message) from exc

    return settings_list


def hash_sources(input_dir: Path, selected: list[str]) -> list[SourceRecord]:
    records = []
    for name in selected:
        path = input_dir / name
        stat = path.stat()
        records.append(
            SourceRecord(
                filename=name,
                absolute_path=str(path.resolve()),
                size=stat.st_size,
                mtime=stat.st_mtime,
                sha256=hashing.sha256_file(path),
            )
        )
    return records


def build_groups(selected: list[str], per_negative: int) -> list[GroupRecord]:
    groups = []
    for i, members in enumerate(group_names(selected, per_negative)):
        groups.append(
            GroupRecord(
                group_id=f"negative-{i + 1:02d}",
                members=members,
                expected_outputs=[_output_name_for(m) for m in members],
            )
        )
    return groups


def build_curated_metadata(settings_list: list[SourceSettings]) -> CuratedMetadata:
    first = settings_list[0]
    return CuratedMetadata(
        exposure_time=str(first.exposure_time),
        f_number=str(first.f_number),
        iso=first.iso,
        focal_length=str(first.focal_length),
        lens_model=first.lens_model,
        orientation=first.orientation,
        camera_whitebalance=first.camera_whitebalance,
    )


def _as_datetime(ts: CaptureTimestamp) -> datetime.datetime:
    """`ts.when` plus its subsecond fraction folded into `microsecond`."""
    microsecond = min(round(ts.subsec_fraction * 1_000_000), 999_999)
    return ts.when.replace(microsecond=microsecond)


def _read_real_times(input_dir: Path, selected: list[str]) -> list[datetime.datetime]:
    """Section 0/§2: Phase 3 has no film date, so every intermediate carries
    the real `DateTimeOriginal` its own source frame already has — read the
    same way canonical ordering already trusts (`catalogue.read_capture_timestamp`).
    A selected file with no usable capture timestamp fails the run with
    `MISSING_CAPTURE_TIME`: Phase 1's noon-plus-elapsed fallback for this
    case no longer exists, so it can no longer be papered over."""
    times: list[datetime.datetime] = []
    for name in selected:
        ts = read_capture_timestamp(input_dir / name)
        if ts is None:
            raise ConvertFailure(
                Code.MISSING_CAPTURE_TIME,
                f"{name} has no usable capture timestamp",
            )
        times.append(_as_datetime(ts))
    return times


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _stage_one_frame(member: str, ctx: _GroupContext) -> _StagedFrame:
    """Decode one RAW and write its finished TIFF into the staging
    directory. Runs on a worker thread when `--jobs` is above 1, and on
    the calling thread when it is 1.

    Cancellation is checked at the three step boundaries of section 3.8 —
    before the decode, and after each of the decode, TIFF-writing, and
    metadata steps — never inside a LibRaw or tifffile call, which cannot
    be interrupted safely.
    """
    ctx.cancel.raise_if_cancelled()

    source_index = ctx.source_index_by_name[member]
    record = ctx.source_records_by_name[member]
    path = ctx.input_dir / member

    _verify_source_unchanged(path, record)
    pixels = raw_decode.decode_raw(path, chromatic_aberration=ctx.ca_scales).pixels
    # Sensor clipping is measured here, before anything touches the pixels:
    # it is a property of the capture, and the flat-field gain would move
    # the level it is measured against (docs/DECISIONS.md, "Normalization
    # decisions"). The pipeline attempts no reconstruction, exactly as NegPy says.
    clip_fractions = normalization.measure_clip_fractions(pixels)
    for channel, fraction in enumerate(clip_fractions):
        if fraction > normalization.SCAN_CLIP_WARN:
            ctx.progress.warn(
                Code.SCAN_CLIPPED,
                f"{member}: {fraction * 100:.2f}% of the channel-{channel} "
                f"pixels are at or above sensor white "
                f"({normalization.SCAN_CLIP_LEVEL:.2f}); their highlights "
                "are clipped and no reconstruction is attempted",
            )
    # Flat-field correction sits inside the DECODE step boundary: after the
    # RAW decode, before the base TIFF, so the stitch stage's per-frame
    # photometric gain solve is asked to explain real exposure mismatch, not
    # spatial falloff (docs/FLATFIELD_PLAN.md section 1).
    if ctx.full_res_gain is not None:
        clipped = flatfield.apply_in_place(pixels, ctx.full_res_gain)
        height, width = pixels.shape[:2]
        if clipped / (height * width) > flatfield.CLIPPED_PIXEL_WARN_FRACTION:
            ctx.progress.warn(
                Code.FLATFIELD_HIGHLIGHT_CLIPPED,
                f"{member}: the correction pushed {clipped} pixels past full "
                "scale; their highlights were clipped",
            )
    _verify_source_unchanged(path, record)
    ctx.progress.advance(source_index, PipelineStep.DECODE)
    ctx.cancel.raise_if_cancelled()

    settings = ctx.settings_by_name[member]
    base_path = ctx.staging_dir / f"{Path(member).stem}.base.tif"
    write_base_tiff(
        base_path,
        pixels,
        BaseTiffTags(
            description=image_description(member),
            software=software_tag_value(),
            conversion_time=datetime.datetime.now(datetime.UTC).replace(tzinfo=None),
            icc_profile=ctx.icc_profile,
            make=settings.make,
            model=settings.model,
        ),
    )
    del pixels  # the array never leaves this frame's scope (section 3.8)
    ctx.progress.advance(source_index, PipelineStep.WRITE_TIFF)
    ctx.cancel.raise_if_cancelled()

    digitized = ctx.digitized_fields_by_name[member]
    final_path = ctx.staging_dir / _output_name_for(member)
    finalize_tiff(
        base_path,
        final_path,
        NestedExifFields(
            date_time_original=ctx.real_time_by_name[member],
            exposure_time=settings.exposure_time,
            f_number=settings.f_number,
            iso=settings.iso,
            focal_length=settings.focal_length,
            lens_model=settings.lens_model,
            date_time_digitized=digitized.date_time_digitized,
            subsec_time_digitized=digitized.subsec_time_digitized,
            offset_time_digitized=digitized.offset_time_digitized,
        ),
    )
    ctx.progress.advance(source_index, PipelineStep.ADD_METADATA)

    return _StagedFrame(
        member=member,
        source_index=source_index,
        final_path=final_path,
        scan_clip_fractions=clip_fractions,
    )


def _stage_group(
    members: list[str], ctx: _GroupContext, workers: int
) -> list[_StagedFrame]:
    """Stage every frame of one group, serially or across threads.

    Raises on the first frame that fails or on cancellation; either way
    every worker has stopped by the time this returns, so the caller can
    safely delete the staging directory.
    """
    if workers <= 1:
        # Section 3.8: "`--jobs 1` uses the serial path." No executor is
        # constructed at all, so a serial run has no thread-pool
        # behaviour to go wrong.
        return [_stage_one_frame(member, ctx) for member in members]

    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="scanny-frame")
    futures: dict[Future[_StagedFrame], str] = {}
    try:
        for member in members:
            futures[pool.submit(_stage_one_frame, member, ctx)] = member
        staged: list[_StagedFrame] = []
        for future in as_completed(futures):
            staged.append(future.result())
        return staged
    finally:
        # `cancel_futures=True` drops frames that never started;
        # `wait=True` blocks until frames that did start have finished
        # their current step. Both halves of section 3.8's "Queued work
        # is cancelled, running workers stop, and only then is the
        # staging directory deleted" — the deletion is the caller's, and
        # happens after this `finally`.
        pool.shutdown(wait=True, cancel_futures=True)


def _verify_source_unchanged(path: Path, record: SourceRecord) -> None:
    try:
        stat = path.stat()
    except OSError as exc:
        raise _SourceChangedError(record.filename, str(exc)) from exc
    if stat.st_size != record.size or stat.st_mtime != record.mtime:
        raise _SourceChangedError(
            record.filename, "size or modification time changed since hashing"
        )


def _code_for_group_failure(exc: Exception) -> Code:
    if isinstance(exc, UnsupportedRawError):
        return Code.UNSUPPORTED_RAW
    if isinstance(exc, (UnreadableRawError, _SourceChangedError)):
        return Code.UNREADABLE_RAW
    if isinstance(exc, TiffFinalizeError):
        return exc.code
    return Code.TIFF_WRITE_FAILED


def _publish_group(
    *,
    output_dir: Path,
    group_record: GroupRecord,
    final_paths: dict[str, Path],
    source_index_by_name: dict[str, int],
    emit: EmitFn,
    run_id: str,
) -> None:
    """Move every staged final TIFF into `output_dir`. Deliberately not
    wrapped by the group's ordinary-failure handler — see the module
    docstring.

    Always walks `group_record.members`, never the order the frames
    finished in, so `item_done` events stay in canonical source order
    even when threads completed out of order.
    """
    for member in group_record.members:
        source_path = final_paths[member]
        output_name = _output_name_for(member)
        dest = output_dir / output_name
        os.replace(source_path, dest)
        size = dest.stat().st_size
        sha256 = hashing.sha256_file(dest)
        group_record.outputs.append(
            OutputRecord(name=output_name, size=size, sha256=sha256)
        )
        emit(
            ItemDone(
                run_id=run_id,
                source_index=source_index_by_name[member],
                output=output_name,
            )
        )


def build_processing_params(profile) -> dict:
    """The processing params a run presents as its roll invariant (section
    3.4): the raw decode params, the normalization constants (section 3.8 —
    the key is always present, normalization is not optional), and, when a
    flat-field profile applies, its token and any CA decode scales (the
    second invariant bucket of docs/GEOMETRIC_PLAN.md section 3.6).

    `run_convert` and `probe --roll` must both present exactly this shape,
    so both go through this one function rather than keeping copies that
    can drift apart."""
    ca_scales = (
        None if profile is None else flatfield.chromatic_aberration_scales(profile)
    )
    processing_params = raw_decode.jsonable_raw_params(chromatic_aberration=ca_scales)
    processing_params["normalize"] = normalization.build_params()
    if profile is not None:
        # Absent, not null, when no profile was given, so a no-profile run
        # still compares equal to a pre-flat-field roll (section 2.4).
        processing_params["flat_field"] = flatfield.profile_token(profile)
        if ca_scales is not None:
            processing_params["chromatic_aberration"] = {
                "profile_id": profile.profile_id,
                "mode": "scale",
                "red_scale": ca_scales[0],
                "blue_scale": ca_scales[1],
            }
    return processing_params


def run_convert(
    input_dir: Path,
    files: list[str],
    output_dir: Path,
    per_negative: int,
    *,
    run_id: str,
    overwrite: bool = False,
    jobs: int | None = None,
    cancel: CancellationToken | None = None,
    emit: EmitFn = lambda event: None,
    completed_offset: int = 0,
    total_override: int | None = None,
    flatfield_profile_id: str | None = None,
) -> ConvertOutcome:
    """Validate the selection and output folder exactly as `probe` does,
    then convert every selected frame group by group. Raises
    `ConvertFailure` for any validation problem; a group-level failure
    during conversion does not raise — it is recorded in the manifest and
    reported through `GroupFailed`, and processing continues with the next
    group.

    `jobs` is `None` for the section 3.8 default worker count, or an
    explicit 1-12. Cancelling `cancel` abandons the group in flight,
    leaves already-published groups alone, records the manifest as
    `cancelled`, and returns an outcome whose status is `"cancelled"`.

    `flatfield_profile_id` applies one flat-field profile to every frame of
    the run; it is folded into `processing_params` under `flat_field` as the
    profile token, so a roll locks to one profile with its first run. A
    missing profile or unreadable gain map fails here, having touched
    nothing.
    """
    cancel = cancel if cancel is not None else CancellationToken()

    # Before anything touches the filesystem: an explicit --jobs that
    # exceeds the memory budget is rejected outright (section 3.8), while
    # the default is silently reduced to fit. The flat-field profile is
    # next for the same reason: loading it touches nothing the run writes.
    try:
        workers = concurrency.resolve_worker_count(per_negative, jobs)
    except concurrency.MemoryBudgetError as exc:
        raise ConvertFailure(exc.code, exc.message) from exc

    profile = None
    gain_map = None
    ca_scales: tuple[float, float] | None = None
    if flatfield_profile_id is not None:
        from scanny_boy.library import repo

        try:
            profile = repo.load_flatfield_profile(flatfield_profile_id)
            gain_map = flatfield.load_gain_map(profile)
            ca_scales = flatfield.chromatic_aberration_scales(profile)
        except flatfield.FlatFieldError as exc:
            raise ConvertFailure(exc.code, exc.message) from exc

    validated = _validate_selection(input_dir, files, per_negative, emit, run_id)
    selected = validated.names

    try:
        validate_not_same_as_input(input_dir, output_dir)
        validate_writable(output_dir)
    except OutputFolderError as exc:
        raise ConvertFailure(exc.code, exc.message) from exc

    settings_list = _read_settings_and_check_consistency(input_dir, selected)
    source_records = hash_sources(input_dir, selected)
    width, height = raw_decode.read_active_size(input_dir / selected[0])
    if profile is not None:
        # Section 1.2: a profile's geometry is only valid for the frame
        # dimensions it was fitted at; fail before anything is written.
        try:
            flatfield.check_geometry_frame_size(profile, width, height)
        except flatfield.FlatFieldError as exc:
            raise ConvertFailure(exc.code, exc.message) from exc
    real_times = _read_real_times(input_dir, selected)
    digitized_fields = [
        choose_digitized_fields(read_digitization_fields(input_dir / n))
        for n in selected
    ]
    try:
        icc_profile = load_icc_profile(ProfileKind.LINEAR)
    except IccProfileError as exc:
        raise ConvertFailure(exc.code, exc.message) from exc

    if profile is not None:
        # A portrait reference against landscape scans would stretch the
        # correction silently; past 1% aspect difference, say so and let the
        # user decide.
        reference_ratio = profile.reference_width / profile.reference_height
        frame_ratio = width / height
        if abs(reference_ratio - frame_ratio) / reference_ratio > 0.01:
            emit(
                WarningEvent(
                    run_id=run_id,
                    code=Code.FLATFIELD_ASPECT_MISMATCH,
                    message=(
                        f"the reference is {profile.reference_width}x"
                        f"{profile.reference_height} but the frames decode at "
                        f"{width}x{height}; the gain map will be stretched "
                        "to fit"
                    ),
                )
            )

    processing_params = build_processing_params(profile)

    groups = build_groups(selected, per_negative)
    candidate = Manifest(
        scanny_boy_version=current_scanny_boy_version(),
        run_id=run_id,
        status="running",
        input_folder=str(input_dir.resolve()),
        # No film date at convert (Phase 3 section 0): `film_date` is now
        # just the calendar date of the selection's first real capture
        # time, kept only because the work manifest's schema still has the
        # field and a rerun still compares it.
        film_date=real_times[0].date().isoformat(),
        shots_per_negative=per_negative,
        processing_params=processing_params,
        icc_profile=profile_record(ProfileKind.LINEAR),
        source_order=selected,
        sources=source_records,
        curated_metadata=build_curated_metadata(settings_list),
        groups=groups,
        started_at=_now_iso(),
        finished_at=None,
    )

    try:
        plan = plan_rerun(output_dir, candidate)
    except (OutputFolderError, BadManifestError, ManifestMismatchError) as exc:
        raise ConvertFailure(exc.code, exc.message) from exc

    if plan.conflicting_outputs and not overwrite:
        raise ConvertFailure(
            Code.OUTPUT_CONFLICT,
            "these outputs already exist and would be replaced; pass "
            "--overwrite to confirm: "
            + ", ".join(sorted(set(plan.conflicting_outputs))),
        )

    apply_recovery_cleanup(output_dir, plan)

    missing_output_count = sum(
        1
        for name in candidate.all_expected_outputs()
        if not (output_dir / name).exists()
    )
    required = disk_check.required_free_bytes(
        width=width,
        height=height,
        missing_output_count=missing_output_count,
        largest_group_size=per_negative,
        manifest_size_estimate=estimate_manifest_size(candidate),
    )
    try:
        disk_check.check_disk_space(output_dir, required)
    except disk_check.DiskCheckError as exc:
        raise ConvertFailure(exc.code, exc.message) from exc

    write_manifest(output_dir, candidate)

    source_index_by_name = {name: i for i, name in enumerate(selected)}
    progress = _ProgressReporter(
        total_steps=len(selected) * STEPS_PER_FRAME,
        emit=emit,
        run_id=run_id,
        completed_offset=completed_offset,
        total_override=total_override,
    )
    base_context = {
        "input_dir": input_dir,
        "source_records_by_name": {r.filename: r for r in source_records},
        "source_index_by_name": source_index_by_name,
        "real_time_by_name": dict(zip(selected, real_times, strict=True)),
        "digitized_fields_by_name": dict(zip(selected, digitized_fields, strict=True)),
        "settings_by_name": dict(zip(selected, settings_list, strict=True)),
        "icc_profile": icc_profile,
        # Resized once per run and shared read-only across workers: every
        # frame of a run has the same dimensions (section 2.7).
        "full_res_gain": (
            flatfield.resize_gain_map(gain_map, width, height)
            if gain_map is not None
            else None
        ),
        "ca_scales": ca_scales,
        "progress": progress,
        "cancel": cancel,
    }

    cancelled = False
    for group_record in candidate.groups:
        if cancel.cancelled:
            cancelled = True
            break

        staging_dir = staging_dir_path(output_dir, run_id, group_record.group_id)
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        staging_dir.mkdir(parents=True)

        try:
            staged_frames = _stage_group(
                group_record.members,
                _GroupContext(staging_dir=staging_dir, **base_context),
                workers,
            )
        except CancelledError:
            # Every worker has stopped by now (`_stage_group`'s finally),
            # so deleting the directory cannot race a write.
            shutil.rmtree(staging_dir, ignore_errors=True)
            cancelled = True
            break
        except GROUP_FAILURE_EXCEPTIONS + (_SourceChangedError,) as exc:
            shutil.rmtree(staging_dir, ignore_errors=True)
            code = _code_for_group_failure(exc)
            group_record.status = "failed"
            group_record.error_code = code.value
            group_record.error_message = str(exc)
            write_manifest(output_dir, candidate)
            emit(
                GroupFailed(
                    run_id=run_id,
                    group_id=group_record.group_id,
                    code=code,
                    message=str(exc),
                )
            )
            continue

        # Record each source's measured sensor-clip fractions on the
        # manifest's source records, so they ride through `merge_sources`
        # into the roll (docs/DECISIONS.md, "Normalization decisions").
        clips_by_member = {f.member: f.scan_clip_fractions for f in staged_frames}
        candidate.sources = [
            (
                dataclasses.replace(s, scan_clip_fractions=clips_by_member[s.filename])
                if s.filename in clips_by_member
                else s
            )
            for s in candidate.sources
        ]
        final_paths = {f.member: f.final_path for f in staged_frames}

        if cancel.cancelled:
            # Cancelled in the window between the last frame staging and
            # the first publish. Section 3.6: "The group being processed
            # is not published." A staged-but-unpublished group is still
            # the group being processed.
            shutil.rmtree(staging_dir, ignore_errors=True)
            cancelled = True
            break

        _publish_group(
            output_dir=output_dir,
            group_record=group_record,
            final_paths=final_paths,
            source_index_by_name=source_index_by_name,
            emit=emit,
            run_id=run_id,
        )
        shutil.rmtree(staging_dir, ignore_errors=True)
        group_record.status = "completed"
        write_manifest(output_dir, candidate)
        emit(GroupDone(run_id=run_id, group_id=group_record.group_id))

    if cancelled:
        final_status = "cancelled"
    elif all(g.status == "completed" for g in candidate.groups):
        final_status = "complete"
    else:
        final_status = "partial"
    candidate.status = final_status
    candidate.finished_at = _now_iso()
    write_manifest(output_dir, candidate)

    return ConvertOutcome(
        run_id=run_id, status=final_status, manifest=candidate, workers=workers
    )
