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
from scanny_boy import (
    concurrency,
    disk_check,
    flatfield,
    hashing,
    previews,
    registration,
    tiff_exif,
)
from scanny_boy.apply_metadata import ApplyMetadataFailure, rewrite_date_time_original
from scanny_boy.cancellation import CancellationToken, CancelledError
from scanny_boy.composite import (
    FILL_COLOR,
    GAIN_DRIFT_WARN,
    MAX_OVERLAP_MAD,
    MIN_GAIN_OVERLAP_PX,
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
    MetadataApplied,
    MetadataSkipped,
    NegativeDone,
    NegativeFailed,
    PipelineStep,
    Progress,
    Stage,
    WarningEvent,
)
from scanny_boy.icc_profile import ProfileKind, load_icc_profile, profile_record
from scanny_boy.layout import (
    MAX_GLOBAL_RMS_PX,
    RMS_WEIGHT_FLOOR_PX,
    STRIP_SPREAD_RATIO,
    Layout,
    largest_valid_rect,
    solve_layout,
)
from scanny_boy.library import repo
from scanny_boy.manifest import (
    BadManifestError,
    GroupRecord,
    Manifest,
    load_manifest,
)
from scanny_boy.normalization import (
    HEADROOM_CLIP_WARN_FRACTION,
    NORMALIZED_FILL,
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
    CaptureTime,
    FrameRecord,
    NegativeRecord,
    PairRecord,
    RollInvariantMismatchError,
    RollInvariants,
    RollManifest,
    RunRecord,
    allocate_output_name,
    append_run,
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
# Per negative: match, solve (solving), blend, normalize (the analysis pass
# fused into the encode, docs/DECISIONS.md, "Normalization decisions") and
# write_stitched (compositing). `run`'s combined span is Chunk P2-7's
# business; these are the concrete step boundaries that actually occur here.
_STEPS_PER_FRAME = 3
_STEPS_PER_NEGATIVE = 5


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
    # Covered negatives this run removes when this group publishes: records
    # dropped from the manifest, TIFFs unlinked best-effort.
    covered_to_remove: list[NegativeRecord] = dataclasses.field(default_factory=list)
    layout: Layout | None = None
    frame_size: tuple[int, int] | None = None  # (height, width)
    ca_maps: dict | None = None  # profile CA object, "maps" mode only
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


def _stitch_params(profile=None) -> dict[str, Any]:
    """Section 3.7: the roll manifest records "the stitch parameters and
    every threshold in force", so a file can be interpreted, and the
    section 3.12.2 thresholds revisited, without knowing which build wrote
    it.

    With a calibrated profile (docs/GEOMETRIC_PLAN.md section 3.6), the
    `geometry` bucket carries the profile id, the geometry object, and —
    only in "maps" mode — the chromatic aberration object. It is absent,
    not null, when the profile carries no geometry, so a geometry-free
    profile compares equal to a pre-geometry roll."""
    params: dict[str, Any] = {
        "detection_long_edge": DETECTION_LONG_EDGE,
        "use_clahe": USE_CLAHE,
        # `_CLAHE_RETRY_CODES`: whether to fall back to CLAHE for a negative
        # a same-parameters run would otherwise fail (a fixed policy, so it
        # belongs beside `use_clahe` here; which negatives actually needed it
        # is recorded per-negative as `used_clahe_fallback`, not here).
        "clahe_fallback_enabled": not USE_CLAHE,
        "detector": DETECTOR,
        "ratio_test": RATIO_TEST,
        "ransac_reproj_px": RANSAC_REPROJ_PX,
        "min_pair_inliers": MIN_PAIR_INLIERS,
        "min_pair_inlier_ratio": MIN_PAIR_INLIER_RATIO,
        "max_pair_rms_px": MAX_PAIR_RMS_PX,
        "scale_drift_warn": SCALE_DRIFT_WARN,
        "scale_drift_fail": SCALE_DRIFT_FAIL,
        "max_overlap_mad": MAX_OVERLAP_MAD,
        # Measured against uncorrected overlaps; now gates the post-gain
        # residual and is pending re-measurement at a user gate
        # (composite.py's module docstring, docs/DECISIONS.md).
        "max_overlap_mad_semantics": "post-gain-residual",
        "min_gain_overlap_px": MIN_GAIN_OVERLAP_PX,
        "gain_drift_warn": GAIN_DRIFT_WARN,
        "max_global_rms_px": MAX_GLOBAL_RMS_PX,
        "strip_spread_ratio": STRIP_SPREAD_RATIO,
        # docs/STITCH_QUALITY_PLAN.md section 2.4: distinguishes a manifest
        # written before this change (implicitly rigid, scale forced to 1)
        # from one written after, without consulting the build.
        "layout_model": "similarity",
        # docs/STITCH_QUALITY_PLAN.md section 3: how the layout's three
        # solves weight each pairwise row.
        "layout_row_weight": "sqrt(inliers)/rms",
        "rms_weight_floor_px": RMS_WEIGHT_FLOOR_PX,
        "interpolation": "INTER_LANCZOS4",
        "mask_erode_px": composite_module.MASK_ERODE_PX,
        "memory_safety_factor": composite_module.MEMORY_SAFETY_FACTOR,
        "fill_color": list(FILL_COLOR),
        "feather": composite_module.FEATHER,
    }
    if profile is not None and profile.geometry is not None:
        bucket: dict[str, Any] = {
            "profile_id": profile.profile_id,
            "geometry": profile.geometry,
        }
        ca = profile.chromatic_aberration
        if ca is not None and ca.get("mode") == "maps":
            bucket["chromatic_aberration"] = ca
        params["geometry"] = bucket
    return params


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


def _read_curated_exif(
    path: Path,
) -> tuple[tiff_exif.NestedExifFields, str | None, str | None]:
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
    paths: list[Path], workers: int, cancel: CancellationToken, *, use_clahe: bool
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
            pixels, long_edge=DETECTION_LONG_EDGE, clahe=use_clahe
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


# Failures caused by too few or too poorly-distributed feature matches —
# exactly what a second pass with more local contrast can fix. Canvas-size
# and memory failures (`STITCH_OUTPUT_TOO_LARGE`, `INSUFFICIENT_MEMORY`) are
# not in this set: a sharper detection image does not change a canvas that
# is already too big to write.
_CLAHE_RETRY_CODES = frozenset(
    {Code.STITCH_UNDERCONSTRAINED, Code.STITCH_RESIDUAL_TOO_HIGH}
)


def _solve_negative(
    work_dir: Path,
    entry: _SolvedNegative,
    *,
    workers: int,
    cancel: CancellationToken,
    progress: _StitchProgress,
    source_index: int,
    on_warning,
    profile=None,
) -> tuple[Layout, tuple[int, int], dict | None]:
    """Detection, registration, and the global solve for one negative,
    retried once with CLAHE if the plain pass leaves the pair graph
    disconnected or too far off (`_CLAHE_RETRY_CODES`). Raises `StitchError`
    for anything that still fails after that.

    Every progress step this negative is budgeted for
    (`_STEPS_PER_FRAME`/`_STEPS_PER_NEGATIVE`) is spent on the first attempt;
    a retry solves again silently, with no further `Progress` events, so a
    negative needing it does not overrun the run's declared step total.
    """
    group = entry.group
    paths = _intermediate_paths(work_dir, group)
    frame_size = _read_intermediate_size(paths[0])

    try:
        return _attempt_solve(
            group,
            entry,
            paths,
            frame_size,
            use_clahe=USE_CLAHE,
            workers=workers,
            cancel=cancel,
            progress=progress,
            source_index=source_index,
            on_warning=on_warning,
            profile=profile,
        )
    except StitchError as exc:
        if exc.code not in _CLAHE_RETRY_CODES or USE_CLAHE:
            raise
        entry.record.used_clahe_fallback = True
        on_warning(
            Code.STITCH_CLAHE_FALLBACK_USED,
            f"{group.group_id}: {exc.code.value} on the first pass; retrying "
            f"registration with CLAHE contrast enhancement",
        )
        return _attempt_solve(
            group,
            entry,
            paths,
            frame_size,
            use_clahe=True,
            workers=workers,
            cancel=cancel,
            progress=None,
            source_index=source_index,
            on_warning=on_warning,
            profile=profile,
        )


def _attempt_solve(
    group: GroupRecord,
    entry: _SolvedNegative,
    paths: list[Path],
    frame_size: tuple[int, int],
    *,
    use_clahe: bool,
    workers: int,
    cancel: CancellationToken,
    progress: _StitchProgress | None,
    source_index: int,
    on_warning,
    profile=None,
) -> tuple[Layout, tuple[int, int], dict | None]:
    """One pass of detection, registration, and the global solve, at a fixed
    `use_clahe`. Raises `StitchError` for anything that fails the negative.

    Writes the pairs it computes onto `entry` before any gate can raise, so
    a negative that fails still records its per-pair section 3.4 metrics:
    those numbers are exactly what a reader needs to see *why* it failed.
    `progress` is `None` on a CLAHE retry, which spends no further budget.

    With a calibrated profile, matched points are undistorted before RANSAC
    (docs/GEOMETRIC_PLAN.md section 5.3) and the memory estimate includes
    the band-map terms."""
    if progress is not None:
        for _ in paths:
            progress.advance(source_index, PipelineStep.LOAD)

    undistorter = None
    geometry = None
    ca_maps = None
    if profile is not None and profile.geometry is not None:
        geometry = profile.geometry
        undistorter = registration.undistorter_from_geometry(geometry)
        ca = profile.chromatic_aberration
        if ca is not None and ca.get("mode") == "maps":
            ca_maps = ca

    features = _detect_all(paths, workers, cancel, use_clahe=use_clahe)
    if progress is not None:
        for _ in paths:
            progress.advance(source_index, PipelineStep.DETECT)
    cancel.raise_if_cancelled()

    pairs: list[PairResult] = []
    for i in range(len(features)):
        for j in range(i + 1, len(features)):
            cancel.raise_if_cancelled()
            pairs.append(register_pair(features[i], features[j], undistorter))
    entry.pairs = pairs
    entry.record.pairs = _pair_records(pairs)
    if progress is not None:
        progress.advance(source_index, PipelineStep.MATCH)

    for pair in pairs:
        # docs/STITCH_QUALITY_PLAN.md section 2.5: with a per-frame scale in
        # the layout solve, `scale_drift` no longer means "this pair should
        # have been scale 1" — it reports how much magnification the pair
        # carries, still gated at SCALE_DRIFT_WARN/FAIL because film cannot
        # plausibly carry more than that between two frames of one strip.
        if pair.accepted and pair.scale_drift > SCALE_DRIFT_WARN:
            on_warning(
                Code.STITCH_SCALE_DRIFT,
                f"{group.group_id}: pair {pair.a}-{pair.b} carries "
                f"{pair.scale_drift:.5f} magnification, exceeding the "
                f"plausible-fit bound {SCALE_DRIFT_WARN}",
            )

    names = [path.name for path in paths]
    layout = solve_layout(names, frame_size, pairs)
    if progress is not None:
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
            len(paths),
            geometry=geometry is not None,
            ca_maps=ca_maps is not None,
        )
    )

    return layout, frame_size, ca_maps


def _finite(value: float) -> float:
    """A rejected pair can carry an infinite residual or scale drift, and
    JSON has no way to spell infinity. The roll manifest is a record of what
    happened, so those become the largest finite float rather than being
    dropped — the pair's `accepted: false` is what actually matters, and a
    reader comparing against a threshold still sees "far too large"."""
    return sys.float_info.max if math.isinf(value) else value


def _normalization_record(
    result: composite_module.CompositeResult,
    analysis_rect: tuple[int, int, int, int],
) -> dict[str, Any]:
    """The per-negative `normalization` block (docs/DECISIONS.md, "Normalization
    decisions"): the bounds the published pixels were stretched with, the
    recorded-not-acted-on metering, the observed pre-clip extrema and
    headroom clipping (section 3.6), the analysis region, and the rebate
    finding (section 3.13, recorded but not yet consumed). `source` names
    D-4's per-negative policy; a future run-median mode attaches there."""
    bounds = result.bounds
    return {
        "floors": list(bounds.floors),
        "ceils": list(bounds.ceils),
        "shadow_refs": list(result.shadow_refs),
        "anchor": result.anchor,
        "textural_range": result.textural_range,
        "analysis_rect": list(analysis_rect),
        "observed_min": list(result.observed_min),
        "observed_max": list(result.observed_max),
        "headroom_clipped": list(result.headroom_clipped),
        "rebate": {
            "detected": result.rebate.detected,
            "mask_fraction": result.rebate.mask_fraction,
            "base_density": (
                None
                if result.rebate.base_density is None
                else list(result.rebate.base_density)
            ),
            "clipped": result.rebate.clipped,
        },
        "source": "per-negative",
    }


def _normalization_aggregate(
    records: list[NegativeRecord],
) -> dict[str, Any] | None:
    """D-4's run-level aggregate: the per-channel median of the run's
    negatives' bounds. Nothing reads it yet; the data for a
    roll-consistency feature exists from day one."""
    normalization_blocks = [r.normalization for r in records if r.normalization]
    if not normalization_blocks:
        return None

    def channel_medians(key: str) -> list[float]:
        columns = [
            [block[key][ch] for block in normalization_blocks] for ch in range(3)
        ]
        return [float(np.median(column)) for column in columns]

    return {
        "negative_count": len(normalization_blocks),
        "floors": channel_medians("floors"),
        "ceils": channel_medians("ceils"),
        "source": "run-median",
    }


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
            overlap_mad_pregain=pair.overlap_mad_pregain,
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
    negatives: list[str] | None = None,
    flatfield_profile_id: str | None = None,
) -> StitchOutcome:
    """Read the Phase 1 manifest in `work_dir`, verify every intermediate,
    and publish one stitched TIFF per negative into `out_dir`.

    Raises `StitchError` for any run-level validation problem. A negative
    that cannot be stitched fails alone: its failure is recorded in the
    roll manifest, reported through `NegativeFailed`, and the run continues
    and ends `partial` (section 3.5). A cancelled negative is abandoned,
    not failed, and emits no `NegativeFailed`.

    `overwrite` is accepted and unused: a stitch replaces a published file
    only by adopting the covered negative in place, which needs no flag.

    `negatives`, when given, restricts this stitch to the work manifest
    groups whose members exactly match one of the roll's existing negatives
    named by `negatives` (section 3.5's `--negatives` re-stitch path). Each
    match adopts the existing negative in place — same `negative_id`, same
    output name — per the replacement rule.

    `flatfield_profile_id` names the calibration profile whose geometry
    (and, in "maps" mode, CA maps) reach the stitch warp
    (docs/GEOMETRIC_PLAN.md section 5.4). Omitting it on a roll whose
    `stitch_params` carry geometry fails `ROLL_INVARIANT_MISMATCH` through
    the existing check, with no new code.
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

    # The calibration profile, if any: its geometry reaches the stitch warp
    # (docs/GEOMETRIC_PLAN.md sections 3.6 and 5.4). Loaded before the
    # invariants are built, because the geometry bucket is part of them.
    profile = None
    if flatfield_profile_id is not None:
        try:
            profile = repo.load_flatfield_profile(flatfield_profile_id)
        except flatfield.FlatFieldError as exc:
            raise StitchError(exc.code, exc.message) from exc
        if profile.geometry is not None:
            width, height = _read_intermediate_size(
                _intermediate_paths(work_dir, groups[0])[0]
            )
            try:
                flatfield.check_geometry_frame_size(profile, width, height)
            except flatfield.FlatFieldError as exc:
                raise StitchError(exc.code, exc.message) from exc

    # 5. The roll must already exist (section 5.4 decision 1: `stitch` never
    #    creates one) and this run's parameters must match its invariants.
    if not repo.roll_registered(out_dir):
        raise StitchError(
            Code.ROLL_NOT_FOUND,
            f"{out_dir} is not a registered roll; create the roll first",
        )
    invariants = RollInvariants(
        processing_params=work_manifest.processing_params,
        icc_profile_sha256=work_manifest.icc_profile.get("sha256", ""),
        # The density profile the published TIFFs are tagged with is a
        # second invariant (section 3.12's split), sourced from
        # `icc_profile.PROFILES` — not from the work manifest, which only
        # knows the intermediates'.
        published_icc_profile_sha256=profile_record(ProfileKind.DENSITY)["sha256"],
        stitch_params=_stitch_params(profile),
    )
    try:
        plan = plan_rerun(out_dir, invariants, rules=ROLL_RULES)
    except (
        OutputFolderError,
        BadManifestError,
        repo.RollNotRegisteredError,
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

    if negatives:
        wanted_members = {
            frozenset(n.members) for n in roll.negatives if n.negative_id in negatives
        }
        groups = [g for g in groups if frozenset(g.members) in wanted_members]
        if not groups:
            raise StitchError(
                Code.WORK_MANIFEST_UNUSABLE,
                "none of the requested --negatives match a group in this work manifest",
            )

    run_record, records_by_group, removals_by_group = _append_this_run(
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
        entry = _SolvedNegative(
            group=group,
            record=record,
            pairs=[],
            covered_to_remove=removals_by_group.get(group.group_id, []),
        )
        try:
            layout, frame_size, ca_maps = _solve_negative(
                work_dir,
                entry,
                workers=workers,
                cancel=cancel,
                progress=progress,
                source_index=source_index_by_group[group.group_id],
                on_warning=on_warning,
                profile=profile,
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
        entry.ca_maps = ca_maps
        solved.append(entry)

    if not cancelled:
        # 6. Disk check on the output volume, now that canvases are known.
        canvases = [e.layout.canvas_size for e in solved if e.layout is not None]
        required = _required_free_bytes(canvases, estimate_roll_manifest_size(roll))
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
                profile=profile,
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
    # D-4: record the run's aggregate bounds. Nothing reads it yet.
    run_record.normalization_aggregate = _normalization_aggregate(
        list(records_by_group.values())
    )
    write_roll_manifest(out_dir, roll)

    # Previews for the newly published negatives: the app's Edit tab shows
    # the CLI's rendering, never its own (Python owns every decision).
    try:
        previews.sync_previews(out_dir, roll, published)
    except Exception as exc:  # noqa: BLE001 — a preview failure must not fail the stitch
        emit(
            WarningEvent(
                code=Code.PREVIEW_FAILED,
                message=f"could not generate previews: {exc}",
            )
        )

    return StitchOutcome(status=status, published=published, failed=failed)


def _covered_negatives(roll: RollManifest, members: list[str]) -> list[NegativeRecord]:
    """Every existing negative whose members are all covered by `members` —
    the replacement rule's subset test. The status is irrelevant: a covered
    negative that never published (`pending`/`failed`, no file) is still
    adopted or removed."""
    covering = set(members)
    return [n for n in roll.negatives if set(n.members) <= covering]


def _pick_adopted(covered: list[NegativeRecord], first_member: str) -> NegativeRecord:
    """Which covered negative a group adopts, deterministically: the one
    whose `expected_output` is the group's natural stem name when one does,
    otherwise the first in manifest order."""
    stem = f"{Path(first_member).stem}.tif"
    for negative in covered:
        if negative.expected_output == stem:
            return negative
    return covered[0]


def _append_this_run(
    roll: RollManifest,
    work_manifest: Manifest,
    groups: list[GroupRecord],
    run_id: str,
    invariants: RollInvariants,
    work_dir: Path,
) -> tuple[RunRecord, dict[str, NegativeRecord], dict[str, list[NegativeRecord]]]:
    """Add this stitch to the roll: its run record, its sources, and one
    negative per group, all per sections 3.3 and 3.4.

    The replacement rule: a group that covers existing negatives adopts one
    of them — its `negative_id` and `expected_output` are kept, and the
    record is updated in place with this run's identity (`run_id`, members;
    frames, pairs, output, and status follow at publish). Any other covered
    negative is marked for removal, executed when this group publishes. A
    group that covers nothing gets a fresh id and name exactly as before.

    The adopted record's existing output, capture time, and rank data stay
    in place until publish replaces them, so a crash before the staged
    `os.replace` leaves the roll describing exactly what was there before.

    Section 5.4 decision 1: the first run establishes the three invariants an
    empty roll cannot know. `check_roll_invariants` has already passed, so
    assigning them here is a seeding, never an overwrite.

    Returns the run record, this run's negatives keyed by work-manifest
    group id (because the group id is what the solving loop carries), and
    per group the covered records to remove at publish.
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
    removals: dict[str, list[NegativeRecord]] = {}
    next_index = 1
    for group in groups:
        covered = _covered_negatives(roll, group.members)
        if covered:
            adopted = _pick_adopted(covered, group.members[0])
            adopted.run_id = run_id
            adopted.members = list(group.members)
            adopted.status = "pending"
            adopted.used_clahe_fallback = False
            adopted.error_code = None
            adopted.error_message = None
            records[group.group_id] = adopted
            removals[group.group_id] = [n for n in covered if n is not adopted]
        else:
            negative_id = format_negative_id(run_record.short_id, next_index)
            next_index += 1
            record = NegativeRecord(
                negative_id=negative_id,
                run_id=run_id,
                members=list(group.members),
                expected_output=allocate_output_name(
                    roll, group.members[0], negative_id
                ),
                fill_color=FILL_COLOR,
            )
            roll.negatives.append(record)
            records[group.group_id] = record
    return run_record, records, removals


def _friendly_failure_message(
    code: Code, negative_id: str, members: list[str], detail: str
) -> str:
    """Turns a technical `StitchError`/exception message into a sentence a
    user scanning film can act on, naming the negative and its source files
    rather than the intermediate names or internal metrics a developer would
    want. `CONTRACT.md` is explicit that message text isn't the app's
    machine interface, so rewording it here changes nothing about `code`,
    which is what the app actually keys behavior off of."""
    files = ", ".join(members)
    if code is Code.STITCH_UNDERCONSTRAINED:
        return f"Could not find a stitching solution for {negative_id} ({files})"
    if code is Code.STITCH_RESIDUAL_TOO_HIGH:
        return f"The images for {negative_id} did not align closely enough ({files})"
    if code is Code.STITCH_OUTPUT_TOO_LARGE:
        return f"The stitched result for {negative_id} would be too large to save ({files})"
    if code in (Code.INTERMEDIATE_MISSING, Code.INTERMEDIATE_CHANGED):
        return f"{negative_id}'s saved intermediates are missing or changed ({files})"
    return f"Could not stitch {negative_id} ({files}): {detail}"


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
    message = _friendly_failure_message(
        code, record.negative_id, record.members, message
    )
    record.status = "failed"
    record.error_code = code.value
    record.error_message = message
    write_roll_manifest(out_dir, roll)
    emit(
        NegativeFailed(
            run_id=run_id, negative_id=record.negative_id, code=code, message=message
        )
    )


def _covered_applied_source(
    roll: RollManifest, record: NegativeRecord, previous: CaptureTime
) -> CaptureTime | None:
    """Section 3.9: the capture time to re-apply when `record` republishes
    an existing negative's result — the adopted record's own previous
    applied time when it had one, otherwise an applied capture time held by
    another negative `record` covers (the replacement rule's subset test).

    Returns the `CaptureTime` whose `applied_datetime_original` should be
    carried forward, or `None` when there is nothing to re-apply."""
    if previous.applied_datetime_original is not None:
        return previous
    covering = set(record.members)
    for other in roll.negatives:
        if other.negative_id == record.negative_id:
            continue
        if not set(other.members) <= covering:
            continue
        if other.capture_time.applied_datetime_original is not None:
            return other.capture_time
    return None


def _maybe_reapply_metadata(
    out_dir: Path,
    roll: RollManifest,
    record: NegativeRecord,
    previous: CaptureTime,
    run_id: str,
    emit: EmitFn,
) -> None:
    """Section 3.9: a negative that republishes one whose capture time was
    already applied re-applies it automatically, as the final step before
    the manifest is written — the user is not asked, and nothing is left
    dirty. A failed re-apply leaves `record` `completed` with
    `applied_datetime_original` cleared (dirty, recoverable with Apply) and
    never fails the stitch."""
    source = _covered_applied_source(roll, record, previous)
    if source is None:
        return

    assert record.output is not None
    record.capture_time.intended_datetime_original = source.applied_datetime_original
    intended = datetime.datetime.fromisoformat(
        record.capture_time.intended_datetime_original
    )
    tiff_path = out_dir / record.output["name"]

    try:
        rewrite_date_time_original(tiff_path, intended)
    except ApplyMetadataFailure as exc:
        record.capture_time.applied_datetime_original = None
        emit(
            MetadataSkipped(
                run_id=run_id,
                negative_id=record.negative_id,
                code=exc.code,
                message=exc.message,
            )
        )
        return

    record.output["size"] = tiff_path.stat().st_size
    record.output["sha256"] = hashing.sha256_file(tiff_path)
    record.capture_time.applied_datetime_original = (
        record.capture_time.intended_datetime_original
    )
    emit(MetadataApplied(run_id=run_id, negative_id=record.negative_id))


def _remove_covered_negatives(
    out_dir: Path,
    roll: RollManifest,
    covered: list[NegativeRecord],
    run_id: str,
    emit: EmitFn,
) -> None:
    """The replacement rule's removal half: the covered negatives a publish
    did not adopt are dropped from the manifest outright, and their TIFFs
    are unlinked best-effort — a failed delete emits
    `ORPHAN_FILE_NOT_REMOVED` and never fails the run. Records go first and
    the manifest is written after, so a crash here leaves an orphan file
    rather than a dangling record."""
    for old in covered:
        roll.negatives.remove(old)
        if old.output is None:
            continue
        old_path = out_dir / old.output["name"]
        try:
            old_path.unlink()
        except OSError as exc:
            emit(
                WarningEvent(
                    run_id=run_id,
                    code=Code.ORPHAN_FILE_NOT_REMOVED,
                    message=f"{old_path} could not be removed: {exc}",
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
    profile=None,
) -> None:
    """Composite one negative, apply the remaining section 3.4 gates, and
    stage-then-publish it atomically, exactly as Phase 1 publishes a group.

    With a calibrated profile, the warp folds in the profile's geometry and
    — in "maps" mode only — its chromatic aberration maps
    (docs/GEOMETRIC_PLAN.md section 5.3)."""
    layout = entry.layout
    assert layout is not None
    record = entry.record
    # The adopted record's previous capture time, kept aside before this
    # publish overwrites it: its applied time is what section 3.9 carries
    # forward onto the new file.
    previous_capture_time = record.capture_time
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

        # The analysis region must be computed *before* compositing (section
        # 1.5): it takes only `layout` and `entry.frame_size`, both of which
        # exist at solve time. It restricts the meters only — it never crops
        # the output.
        valid_rect = largest_valid_rect(layout, entry.frame_size)

        geometry = profile.geometry if profile is not None else None
        ca = entry.ca_maps if profile is not None else None
        result = composite(
            layout,
            load_frame,
            cancel=cancel,
            on_progress=on_frame_warped,
            geometry=geometry,
            ca=ca,
            region=valid_rect,
        )
        progress.advance(source_index, PipelineStep.BLEND)
        progress.advance(source_index, PipelineStep.NORMALIZE)
        cancel.raise_if_cancelled()

        # Section 3.6: the observed extrema are recorded so the two
        # headroom constants can be tuned from real scans; the warning is
        # the signal that they are too tight for this film and scanner.
        worst_headroom = max(result.headroom_clipped)
        if worst_headroom > HEADROOM_CLIP_WARN_FRACTION:
            emit(
                WarningEvent(
                    run_id=run_id,
                    code=Code.NORMALIZE_HEADROOM_CLIPPED,
                    message=(
                        f"{record.negative_id}: the encode's headroom clipped "
                        f"{worst_headroom * 100:.2f}% of one channel's pixels; "
                        "the headroom constants are likely too tight"
                    ),
                )
            )

        # Fold the measured photometric numbers back into the pairs and the
        # frames, warn on solved gains far from unity, then apply the honest
        # gate (section 3.4). `overlap_mad` is now the post-gain residual —
        # the gate is a registration check, not a lamp-drift check — while
        # `overlap_mad_pregain` records why a gain was applied. The gate's
        # threshold was measured against uncorrected overlaps and is pending
        # re-measurement (composite.py's module docstring).
        merged_pairs = [
            dataclasses.replace(
                pair,
                overlap_fraction=result.overlap_fraction.get((pair.a, pair.b)),
                overlap_mad=result.overlap_mad.get((pair.a, pair.b)),
                overlap_mad_pregain=result.overlap_mad_pregain.get((pair.a, pair.b)),
            )
            for pair in entry.pairs
        ]
        record.pairs = _pair_records(merged_pairs)
        record.frames = [
            FrameRecord(
                name=placement.name,
                rotation_deg=placement.rotation_deg,
                translation=(placement.translation[0], placement.translation[1]),
                gain=result.gains[placement.name],
                scale=placement.scale,
            )
            for placement in layout.placements
        ]
        for placement in layout.placements:
            gain = result.gains[placement.name]
            drift = max(abs(c - 1.0) for c in gain)
            if drift > GAIN_DRIFT_WARN:
                emit(
                    WarningEvent(
                        run_id=run_id,
                        code=Code.STITCH_GAIN_DRIFT,
                        message=(
                            f"{record.negative_id}: frame {placement.name} solved "
                            f"gain ({gain[0]:.4f}, {gain[1]:.4f}, {gain[2]:.4f}) "
                            f"deviates more than {GAIN_DRIFT_WARN} from unity"
                        ),
                    )
                )
        record.global_rms_px = layout.global_rms_px
        record.canvas = layout.canvas_size

        measured = [
            p.overlap_mad
            for p in merged_pairs
            if p.accepted and p.overlap_mad is not None
        ]
        worst = max(measured, default=0.0)
        if worst > MAX_OVERLAP_MAD:
            raise StitchError(
                Code.STITCH_RESIDUAL_TOO_HIGH,
                f"{record.negative_id}: overlap MAD {worst:.4f} exceeds "
                f"{MAX_OVERLAP_MAD}",
            )

        record.valid_rect = valid_rect
        record.normalization = _normalization_record(result, valid_rect)
        record.normalized_fill = NORMALIZED_FILL

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
                conversion_time=datetime.datetime.now(datetime.UTC).replace(
                    tzinfo=None
                ),
                icc_profile=b"",  # replaced by write_stitched_tiff's icc_bytes
                make=make,
                model=model,
            ),
            exif=exif,
            icc_bytes=load_icc_profile(ProfileKind.DENSITY),  # section 3.12
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
        _maybe_reapply_metadata(
            out_dir, roll, record, previous_capture_time, run_id, emit
        )
        _remove_covered_negatives(out_dir, roll, entry.covered_to_remove, run_id, emit)
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
