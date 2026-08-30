"""Orchestrates `probe`'s levels of detail: whole-catalogue canonical
ordering, (with `--files`) selection, grouping, and setup-consistency
validation, (with `--files` and `--out`) output-folder validation, disk
estimate, and overwrite-conflict preview, and (with `--roll`) the roll
folder's invariant validation plus the selection's overlap with prior runs
(Phase 3 section 3.5). See `docs/IMPLEMENTATION_PLAN.md` section 4.1 and
`docs/PHASE3_IMPLEMENTATION_PLAN.md` section 3.5.

Every problem this module detects is reported through `ProbeFailure`,
carrying one of the stable `CONTRACT.md` codes. Some structural problems —
a duplicate or unresolvable entry in the input folder, or a `--files` entry
that isn't a real, distinct catalogue member — have no dedicated code of
their own. `NO_FILES` ("No .nef files, or none selected") is the closest
fit: none of these leave a valid selection to work with.

Warnings are reported through `on_warning` as soon as each is found, not
batched until the end: a warning discovered early (for example, the whole
catalogue falling back to filename order) must still reach the caller even
if a later step raises `ProbeFailure`, matching the live, line-at-a-time
event stream `CONTRACT.md` describes.
"""

from __future__ import annotations

import dataclasses
import shutil
from collections.abc import Callable
from pathlib import Path

from scanny_boy.catalogue import (
    CatalogueError,
    compute_canonical_order,
    discover_catalogue,
)
from scanny_boy.consistency import ConsistencyError, check_consistency
from scanny_boy.disk_check import required_free_bytes
from scanny_boy.events import Code, RollOverlapEntry
from scanny_boy.icc_profile import (
    PROFILE_FILENAME,
    PROFILE_SHA256,
    IccProfileError,
    load_icc_profile,
)
from scanny_boy.manifest import (
    BadManifestError,
    Manifest,
    ManifestMismatchError,
    current_scanny_boy_version,
    estimate_manifest_size,
)
from scanny_boy.metadata import (
    SourceSettings,
    UnreadableRawError,
    UnsupportedRawError,
    read_source_settings,
)
from scanny_boy.output_folder import (
    ROLL_RULES,
    OutputFolderError,
    plan_rerun,
    plan_rerun_preview,
    validate_not_same_as_input,
    validate_writable,
)
from scanny_boy.pipeline import build_curated_metadata, build_groups, hash_sources
from scanny_boy.raw_decode import jsonable_raw_params, read_active_size
from scanny_boy.roll_manifest import (
    ROLL_MANIFEST_FILENAME,
    RollInvariantMismatchError,
    RollInvariants,
    RollManifestUnsupportedError,
)
from scanny_boy.selection import (
    SelectionUsageError,
    group,
    is_contiguous,
    nearest_valid_counts,
    order_selection,
)

# The candidate's stitch params must be exactly what `run --roll` will
# present (section 3.4), so they come from stitch_pipeline itself — one
# source of truth, not a copy that can drift.
from scanny_boy.stitch_pipeline import _stitch_params

OnWarning = Callable[[Code, str], None]

# `probe --out` never writes a manifest and does not yet know the film date
# (section 4.1: the preview happens before `convert`, which is what supplies
# it). These placeholders only affect the byte length `estimate_manifest_size`
# measures, not any field a rerun's `MANIFEST_MISMATCH` check compares.
_PREVIEW_RUN_ID = "preview"
_PREVIEW_FILM_DATE = "0001-01-01"
_PREVIEW_TIMESTAMP = "0001-01-01T00:00:00+00:00"


class ProbeFailure(Exception):
    def __init__(self, code: Code, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclasses.dataclass(frozen=True)
class ProbeOutcome:
    catalogue: list[str]
    groups: list[list[str]]
    output_conflicts: list[str] = dataclasses.field(default_factory=list)
    estimated_required_bytes: int | None = None
    available_bytes: int | None = None
    roll_overlap: list[RollOverlapEntry] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(frozen=True)
class _OutputPreview:
    output_conflicts: list[str]
    estimated_required_bytes: int
    available_bytes: int


def _preview_output_folder(
    input_dir: Path,
    selected: list[str],
    per_negative: int,
    settings_list: list[SourceSettings],
    out_dir: Path,
) -> _OutputPreview:
    """`probe --out`'s output-folder validation and overwrite-conflict
    preview (section 4.1). Raises `ProbeFailure` for anything that would
    also stop `convert`: a bad output folder, a manifest that does not match
    this selection, an invalid ICC profile, or insufficient disk space.
    Existing outputs that a matching rerun would replace are not a failure
    here — they are reported in the returned conflict list, exactly like
    section 3.6's "show the exact files that will be replaced and require
    confirmation.\""""
    try:
        validate_not_same_as_input(input_dir, out_dir)
        validate_writable(out_dir)
    except OutputFolderError as exc:
        raise ProbeFailure(exc.code, exc.message) from exc

    source_records = hash_sources(input_dir, selected)
    group_records = build_groups(selected, per_negative)

    try:
        load_icc_profile()
    except IccProfileError as exc:
        raise ProbeFailure(exc.code, exc.message) from exc

    try:
        plan = plan_rerun_preview(
            out_dir,
            source_order=selected,
            source_hashes={r.filename: r.sha256 for r in source_records},
            shots_per_negative=per_negative,
            groups=[(g.group_id, g.members) for g in group_records],
            icc_sha256=PROFILE_SHA256,
        )
    except (OutputFolderError, BadManifestError, ManifestMismatchError) as exc:
        raise ProbeFailure(exc.code, exc.message) from exc

    width, height = read_active_size(input_dir / selected[0])
    placeholder = Manifest(
        scanny_boy_version=current_scanny_boy_version(),
        run_id=_PREVIEW_RUN_ID,
        status="running",
        input_folder=str(input_dir.resolve()),
        film_date=_PREVIEW_FILM_DATE,
        shots_per_negative=per_negative,
        processing_params=jsonable_raw_params(),
        icc_profile={"name": PROFILE_FILENAME, "sha256": PROFILE_SHA256},
        source_order=selected,
        sources=source_records,
        curated_metadata=build_curated_metadata(settings_list),
        groups=group_records,
        started_at=_PREVIEW_TIMESTAMP,
        finished_at=None,
    )

    missing_output_count = sum(
        1 for name in placeholder.all_expected_outputs() if not (out_dir / name).exists()
    )
    required_bytes = required_free_bytes(
        width=width,
        height=height,
        missing_output_count=missing_output_count,
        largest_group_size=per_negative,
        manifest_size_estimate=estimate_manifest_size(placeholder),
    )
    available_bytes = shutil.disk_usage(out_dir).free
    if available_bytes < required_bytes:
        raise ProbeFailure(
            Code.INSUFFICIENT_DISK,
            f"required {required_bytes} bytes free, but only "
            f"{available_bytes} bytes are available on the output volume",
        )

    return _OutputPreview(
        output_conflicts=sorted(set(plan.conflicting_outputs)),
        estimated_required_bytes=required_bytes,
        available_bytes=available_bytes,
    )


def _preview_roll(
    input_dir: Path,
    selected: list[str],
    per_negative: int,
    groups: list[list[str]],
    roll_dir: Path,
) -> list[RollOverlapEntry]:
    """`probe --roll`'s roll-folder validation and overlap report (section
    3.5). Raises `ProbeFailure` for anything that would also stop `run
    --roll`: no roll manifest in the folder, an unreadable or unsupported
    one, content unrelated to the roll, or invariants that differ from this
    run's parameters.

    Overlap is reported, not rejected: section 3.4's supersession decides at
    `run` time which overlapped negatives are replaced, so the report names
    what each prospective group shares with the roll and lets the caller
    decide."""
    if not (roll_dir / ROLL_MANIFEST_FILENAME).exists():
        raise ProbeFailure(
            Code.ROLL_NOT_FOUND,
            f"{roll_dir} has no {ROLL_MANIFEST_FILENAME}; create the roll first",
        )

    # The same invariants `run --roll` will present (section 3.4), so a
    # probe that passes here cannot fail there on parameters.
    candidate = RollInvariants(
        shots_per_negative=per_negative,
        processing_params=jsonable_raw_params(),
        icc_profile_sha256=PROFILE_SHA256,
        stitch_params=_stitch_params(),
    )
    try:
        plan = plan_rerun(roll_dir, candidate, rules=ROLL_RULES)
    except (
        OutputFolderError,
        BadManifestError,
        RollManifestUnsupportedError,
        RollInvariantMismatchError,
    ) as exc:
        raise ProbeFailure(exc.code, exc.message) from exc
    roll = plan.existing_manifest
    assert roll is not None

    if not groups:
        return []

    # Section 3.5: overlap detection "hashes the selection, compares against
    # `manifest.sources` by `sha256`, and reports per prospective group". A
    # prospective group collides with a negative when the two share sources
    # by content, so a renamed rescan still matches — whether that overlap
    # would supersede the negative (section 3.4's subset rule) is decided at
    # run time, not here.
    selected_hashes = {r.filename: r.sha256 for r in hash_sources(input_dir, selected)}
    roll_hashes = {s.filename: s.sha256 for s in roll.sources}
    entries: list[RollOverlapEntry] = []
    for group_index, members in enumerate(groups):
        for negative in roll.live_negatives():
            negative_hashes = {
                roll_hashes[member]
                for member in negative.members
                if member in roll_hashes
            }
            overlapping = [
                name for name in members if selected_hashes[name] in negative_hashes
            ]
            if overlapping:
                entries.append(
                    RollOverlapEntry(
                        negative_id=negative.negative_id,
                        expected_output=negative.expected_output,
                        run_id=negative.run_id,
                        overlapping_sources=overlapping,
                        group_index=group_index,
                    )
                )
    return entries


def run_probe(
    input_dir: Path,
    files: list[str] | None,
    per_negative: int,
    *,
    out_dir: Path | None = None,
    roll_dir: Path | None = None,
    on_warning: OnWarning = lambda code, message: None,
) -> ProbeOutcome:
    try:
        names = discover_catalogue(input_dir)
    except CatalogueError as exc:
        raise ProbeFailure(Code.NO_FILES, str(exc)) from exc
    except OSError as exc:
        raise ProbeFailure(
            Code.NO_FILES, f"input folder does not exist or is not readable: {exc}"
        ) from exc

    if not names:
        raise ProbeFailure(Code.NO_FILES, f"no .nef files found in {input_dir}")

    order = compute_canonical_order(input_dir, names)
    if order.used_filename_fallback:
        on_warning(
            Code.FILENAME_SORT_USED,
            (
                "a catalogue file has no usable capture timestamp; sorted "
                "the whole catalogue by filename instead"
            ),
        )

    if files is None:
        # Section 3.5: `--roll` without `--files` still validates the roll
        # folder and its invariants; without a selection there is no overlap
        # to report.
        if roll_dir is not None:
            _preview_roll(input_dir, [], per_negative, [], roll_dir)
        return ProbeOutcome(catalogue=order.order, groups=[])

    if not files:
        raise ProbeFailure(Code.NO_FILES, "no files were selected")

    try:
        selection = order_selection(order.order, files)
    except SelectionUsageError as exc:
        raise ProbeFailure(Code.NO_FILES, str(exc)) from exc

    if not is_contiguous(selection):
        raise ProbeFailure(
            Code.NON_CONTIGUOUS_SELECTION,
            "the selection has a gap in canonical order",
        )

    count = len(selection.names)
    if count % per_negative != 0:
        lower, upper = nearest_valid_counts(count, per_negative)
        raise ProbeFailure(
            Code.NOT_DIVISIBLE,
            f"{count} files is not divisible by {per_negative} per negative; "
            f"nearest valid counts are {lower} and {upper}",
        )

    groups = group(selection.names, per_negative)

    settings_list = []
    for name in selection.names:
        try:
            settings_list.append(read_source_settings(input_dir / name))
        except UnsupportedRawError as exc:
            raise ProbeFailure(
                Code.UNSUPPORTED_RAW,
                f"{name} cannot be read by LibRaw; Z f HE/HE* files must be "
                "recaptured as lossless-compressed NEFs",
            ) from exc
        except UnreadableRawError as exc:
            raise ProbeFailure(
                Code.UNREADABLE_RAW, f"{name} could not be decoded"
            ) from exc

    try:
        result = check_consistency(settings_list)
    except ConsistencyError as exc:
        raise ProbeFailure(exc.code, exc.message) from exc

    for warning in result.warnings:
        on_warning(warning.code, warning.message)

    preview = None
    if out_dir is not None:
        preview = _preview_output_folder(
            input_dir, selection.names, per_negative, settings_list, out_dir
        )

    roll_overlap: list[RollOverlapEntry] = []
    if roll_dir is not None:
        roll_overlap = _preview_roll(
            input_dir, selection.names, per_negative, groups, roll_dir
        )

    return ProbeOutcome(
        catalogue=order.order,
        groups=groups,
        output_conflicts=preview.output_conflicts if preview else [],
        estimated_required_bytes=preview.estimated_required_bytes if preview else None,
        available_bytes=preview.available_bytes if preview else None,
        roll_overlap=roll_overlap,
    )
