"""Serial group-by-group conversion pipeline: decode -> base TIFF -> nested
EXIF -> staging -> publish -> manifest.

See `docs/IMPLEMENTATION_PLAN.md` section 3.6 (staging, overwriting, and
grouping), section 3.7 (manifest), section 3.9 (disk checks), and section
3.8's "Implement and verify a serial path first" — the threaded executor,
memory budget, and cancellation are Chunk 6's job; `--jobs` is accepted by
the argument parser but unused here.

Per section 3.6, "never publish only part of a group": every frame in a
group is decoded and written into that group's staging directory first
(`_process_group_to_staging`, wrapped in a single try/except — an ordinary
per-frame failure there deletes the whole staging directory and fails the
group). Publishing — moving each finished file from staging into the
output folder (`_publish_group`) — happens only after every frame in the
group has staged successfully, and is deliberately *not* wrapped in that
same handler: section 3.6 frames this move as the one place a real crash
can leave a group half-published, and recovery for that is a rerun's job
(`output_folder.plan_rerun` + `apply_recovery_cleanup`), not this run's.
"""

from __future__ import annotations

import dataclasses
import datetime
import os
import shutil
from collections.abc import Callable
from pathlib import Path

from scanny_boy import disk_check, hashing, raw_decode
from scanny_boy.catalogue import (
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
from scanny_boy.film_date import (
    FilmDateError,
    synthetic_times_from_capture,
    synthetic_times_from_filename_fallback,
)
from scanny_boy.icc_profile import (
    PROFILE_FILENAME,
    PROFILE_SHA256,
    IccProfileError,
    load_icc_profile,
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
    status: str  # "complete" | "partial"
    manifest: Manifest


@dataclasses.dataclass(frozen=True)
class _ValidatedSelection:
    names: list[str]
    used_filename_fallback: bool


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

    return _ValidatedSelection(names=selection.names, used_filename_fallback=order.used_filename_fallback)


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
            raise ConvertFailure(Code.UNREADABLE_RAW, f"{name} could not be decoded") from exc

    try:
        check_consistency(settings_list)
    except ConsistencyError as exc:
        raise ConvertFailure(exc.code, exc.message) from exc

    return settings_list


def _hash_sources(input_dir: Path, selected: list[str]) -> list[SourceRecord]:
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


def _build_groups(selected: list[str], per_negative: int) -> list[GroupRecord]:
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


def _build_curated_metadata(settings_list: list[SourceSettings]) -> CuratedMetadata:
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


def _compute_synthetic_times(
    input_dir: Path,
    selected: list[str],
    film_date: datetime.date,
    used_filename_fallback: bool,
) -> list[datetime.datetime]:
    try:
        if used_filename_fallback:
            return synthetic_times_from_filename_fallback(film_date, len(selected))
        timestamps = [read_capture_timestamp(input_dir / name) for name in selected]
        return synthetic_times_from_capture(film_date, timestamps)
    except FilmDateError as exc:
        raise ConvertFailure(exc.code, exc.message) from exc


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _process_group_to_staging(
    *,
    input_dir: Path,
    staging_dir: Path,
    group_record: GroupRecord,
    source_records_by_name: dict[str, SourceRecord],
    source_index_by_name: dict[str, int],
    synthetic_time_by_name: dict[str, datetime.datetime],
    digitized_fields_by_name: dict[str, DigitizedFields],
    settings_by_name: dict[str, SourceSettings],
    icc_profile: bytes,
    total_steps: int,
    steps_done: int,
    emit: EmitFn,
    run_id: str,
) -> tuple[dict[str, Path], int]:
    """Decode and write every frame of one group into `staging_dir`. Raises
    on the first frame that fails; the caller deletes `staging_dir` and
    marks the group failed. Returns `{member: final_tiff_path}` and the
    updated `steps_done` count on success."""
    final_paths: dict[str, Path] = {}

    for member in group_record.members:
        source_index = source_index_by_name[member]
        record = source_records_by_name[member]
        path = input_dir / member

        _verify_source_unchanged(path, record)
        pixels = raw_decode.decode_raw(path).pixels
        _verify_source_unchanged(path, record)

        steps_done += 1
        emit(
            Progress(
                run_id=run_id,
                source_index=source_index,
                step=PipelineStep.DECODE,
                completed=steps_done,
                total=total_steps,
            )
        )

        settings = settings_by_name[member]
        base_path = staging_dir / f"{Path(member).stem}.base.tif"
        write_base_tiff(
            base_path,
            pixels,
            BaseTiffTags(
                description=image_description(member),
                software=software_tag_value(),
                conversion_time=datetime.datetime.now(datetime.UTC).replace(tzinfo=None),
                icc_profile=icc_profile,
                make=settings.make,
                model=settings.model,
            ),
        )
        steps_done += 1
        emit(
            Progress(
                run_id=run_id,
                source_index=source_index,
                step=PipelineStep.WRITE_TIFF,
                completed=steps_done,
                total=total_steps,
            )
        )

        digitized = digitized_fields_by_name[member]
        final_path = staging_dir / _output_name_for(member)
        finalize_tiff(
            base_path,
            final_path,
            NestedExifFields(
                date_time_original=synthetic_time_by_name[member],
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
        steps_done += 1
        emit(
            Progress(
                run_id=run_id,
                source_index=source_index,
                step=PipelineStep.ADD_METADATA,
                completed=steps_done,
                total=total_steps,
            )
        )

        final_paths[member] = final_path

    return final_paths, steps_done


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
    docstring."""
    for member in group_record.members:
        source_path = final_paths[member]
        output_name = _output_name_for(member)
        dest = output_dir / output_name
        os.replace(source_path, dest)
        size = dest.stat().st_size
        sha256 = hashing.sha256_file(dest)
        group_record.outputs.append(OutputRecord(name=output_name, size=size, sha256=sha256))
        emit(
            ItemDone(
                run_id=run_id, source_index=source_index_by_name[member], output=output_name
            )
        )


def run_convert(
    input_dir: Path,
    files: list[str],
    output_dir: Path,
    film_date: datetime.date,
    per_negative: int,
    *,
    run_id: str,
    overwrite: bool = False,
    emit: EmitFn = lambda event: None,
) -> ConvertOutcome:
    """Validate the selection and output folder exactly as `probe` does,
    then convert every selected frame group by group. Raises
    `ConvertFailure` for any validation problem; a group-level failure
    during conversion does not raise — it is recorded in the manifest and
    reported through `GroupFailed`, and processing continues with the next
    group.
    """
    validated = _validate_selection(input_dir, files, per_negative, emit, run_id)
    selected = validated.names

    try:
        validate_not_same_as_input(input_dir, output_dir)
        validate_writable(output_dir)
    except OutputFolderError as exc:
        raise ConvertFailure(exc.code, exc.message) from exc

    settings_list = _read_settings_and_check_consistency(input_dir, selected)
    source_records = _hash_sources(input_dir, selected)
    width, height = raw_decode.read_active_size(input_dir / selected[0])
    synthetic_times = _compute_synthetic_times(
        input_dir, selected, film_date, validated.used_filename_fallback
    )
    digitized_fields = [choose_digitized_fields(read_digitization_fields(input_dir / n)) for n in selected]
    try:
        icc_profile = load_icc_profile()
    except IccProfileError as exc:
        raise ConvertFailure(exc.code, exc.message) from exc

    groups = _build_groups(selected, per_negative)
    candidate = Manifest(
        scanny_boy_version=current_scanny_boy_version(),
        run_id=run_id,
        status="running",
        input_folder=str(input_dir.resolve()),
        film_date=film_date.isoformat(),
        shots_per_negative=per_negative,
        processing_params=raw_decode.jsonable_raw_params(),
        icc_profile={"name": PROFILE_FILENAME, "sha256": PROFILE_SHA256},
        source_order=selected,
        sources=source_records,
        curated_metadata=_build_curated_metadata(settings_list),
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
            "--overwrite to confirm: " + ", ".join(sorted(set(plan.conflicting_outputs))),
        )

    apply_recovery_cleanup(output_dir, plan)

    missing_output_count = sum(
        1 for name in candidate.all_expected_outputs() if not (output_dir / name).exists()
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
    source_records_by_name = {r.filename: r for r in source_records}
    settings_by_name = dict(zip(selected, settings_list, strict=True))
    synthetic_time_by_name = dict(zip(selected, synthetic_times, strict=True))
    digitized_fields_by_name = dict(zip(selected, digitized_fields, strict=True))
    total_steps = len(selected) * STEPS_PER_FRAME
    steps_done = 0

    for group_record in candidate.groups:
        staging_dir = staging_dir_path(output_dir, run_id, group_record.group_id)
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        staging_dir.mkdir(parents=True)

        try:
            final_paths, steps_done = _process_group_to_staging(
                input_dir=input_dir,
                staging_dir=staging_dir,
                group_record=group_record,
                source_records_by_name=source_records_by_name,
                source_index_by_name=source_index_by_name,
                synthetic_time_by_name=synthetic_time_by_name,
                digitized_fields_by_name=digitized_fields_by_name,
                settings_by_name=settings_by_name,
                icc_profile=icc_profile,
                total_steps=total_steps,
                steps_done=steps_done,
                emit=emit,
                run_id=run_id,
            )
        except (
            UnsupportedRawError,
            UnreadableRawError,
            TiffFinalizeError,
            _SourceChangedError,
            OSError,
        ) as exc:
            shutil.rmtree(staging_dir, ignore_errors=True)
            code = _code_for_group_failure(exc)
            group_record.status = "failed"
            group_record.error_code = code.value
            group_record.error_message = str(exc)
            write_manifest(output_dir, candidate)
            emit(
                GroupFailed(
                    run_id=run_id, group_id=group_record.group_id, code=code, message=str(exc)
                )
            )
            continue

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

    final_status = "complete" if all(g.status == "completed" for g in candidate.groups) else "partial"
    candidate.status = final_status
    candidate.finished_at = _now_iso()
    write_manifest(output_dir, candidate)

    return ConvertOutcome(run_id=run_id, status=final_status, manifest=candidate)
