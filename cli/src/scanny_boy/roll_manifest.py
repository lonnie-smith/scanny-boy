"""One roll's durable record: format version 6, persisted in the library
database instead of `scanny-boy-roll.json`.

A *roll* is a named folder the user returns to, holding the stitched TIFFs of
one roll of film across many runs. That is the whole reason this record is a
break rather than a patch: Phase 2's version 1 carried one `run_id`, one
`input_folder`, and one `film_date`, and refused any rerun that changed them,
version 2's supersession tombstones are gone in version 3 — a rerun
adopts the covered negative in place instead of publishing a replacement —
version 4 adds per-frame solved photometric gains and per-pair pre-gain
overlap MAD, version 5 drops the roll-level `shots_per_negative`: each
stitch batch's grouping lives in its own work manifest, so one roll can hold
negatives stitched from different scan counts, and version 6 adds a
per-frame solved `scale` (docs/STITCH_QUALITY_PLAN.md section 2: the global
layout is now a similarity, not a rigid transform). See
`docs/PHASE3_IMPLEMENTATION_PLAN.md` section 3.3
for the shape and section 3.4 for the invariants and naming rules this
module enforces. The record's shape is unchanged from the JSON-manifest era
otherwise — `to_dict()` still emits exactly the fields
`roll-manifest.schema.json` and the `roll info` event describe — but the
file is gone: `load_roll_manifest` and `write_roll_manifest` read and write
rows through `scanny_boy.library.repo`. A roll "exists" when it is
registered in the library database (which `roll init` and every write do);
there is no migration from JSON files, because the app never shipped.

The structural-validation layer that guarded against corrupt or foreign JSON
files is gone with the file: the database is written only by this program.
What survives is the output-path containment check on load, which is cheap
and protects the pipelines from a tampered row.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
from pathlib import Path
from typing import Any

from scanny_boy.events import Code
from scanny_boy.icc_profile import ProfileKind, profile_record
from scanny_boy.library import repo
from scanny_boy.manifest import (
    BadManifestError,
    SourceRecord,
    resolve_within,
)

ROLL_MANIFEST_FORMAT_VERSION = 6
ROLL_MANIFEST_KIND = "roll"

# The extended metadata fields, in display order. Every one lives on both
# `RollMetadata` (the roll-level fallback) and `NegativeMetadata` (the
# explicit per-image value); `effective_metadata` resolves the pair.
METADATA_FIELDS = ("city", "state", "camera", "lens", "caption")

# Section 3.4: `short_id` starts at six characters of the run's UUID and
# lengthens until it is free within the roll.
SHORT_ID_LENGTHS = (6, 8, 10)


class RollInvariantMismatchError(Exception):
    """Maps to `ROLL_INVARIANT_MISMATCH` (section 3.12): this run's
    parameters differ from the ones the roll already established. Section 3.4
    keeps `MANIFEST_MISMATCH` for the Phase 1 work manifest, so the two never
    share a code."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = Code.ROLL_INVARIANT_MISMATCH
        self.message = message


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None).isoformat() + "Z"


@dataclasses.dataclass(frozen=True)
class PairRecord:
    a: str
    b: str
    inliers: int
    good_matches: int
    inlier_ratio: float
    rms_residual_px: float
    scale_drift: float
    overlap_fraction: float | None
    # Post-gain residual — the value the MAX_OVERLAP_MAD gate checks.
    overlap_mad: float | None
    # The same measurement taken before per-frame gain compensation; the
    # diagnostic that explains why a gain was applied.
    overlap_mad_pregain: float | None
    accepted: bool

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class FrameRecord:
    name: str
    rotation_deg: float
    translation: tuple[float, float]
    # Solved per-channel (R, G, B) photometric gain; the frame's warped
    # linear values were multiplied by this before the blend. Geometric
    # mean of the gains across a negative's frames is 1 by construction.
    gain: tuple[float, float, float]
    # Solved per-frame isotropic scale (docs/STITCH_QUALITY_PLAN.md section
    # 2): the global layout is a similarity, not a rigid transform. Geometric
    # mean of the scales across a negative's frames is 1 by construction,
    # the same gauge convention as `gain`.
    scale: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "rotation_deg": self.rotation_deg,
            "translation": list(self.translation),
            "gain": list(self.gain),
            "scale": self.scale,
        }


@dataclasses.dataclass(frozen=True)
class RollSourceRecord:
    """Section 3.3: "as Phase 1, plus `run_id` naming the run that first
    contributed it". Phase 1's `SourceRecord` is shared with the work
    manifest and must not grow a field, so the roll keeps its own record
    (section 5.4)."""

    filename: str
    absolute_path: str
    size: int
    mtime: float
    sha256: str
    run_id: str
    # Per-channel fraction of pixels at or above sensor white, measured in
    # the prepare stage at decode (docs/DECISIONS.md, "Normalization decisions").
    # Null when the contributing run predates the measurement.
    scan_clip_fractions: tuple[float, float, float] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["scan_clip_fractions"] = (
            None if self.scan_clip_fractions is None else list(self.scan_clip_fractions)
        )
        return data


@dataclasses.dataclass
class CaptureTime:
    """Section 3.3. `source_datetime_original` is what the negative's first
    frame actually carries; `intended_` is what the metadata stage wants;
    `applied_` is what was last written into the published TIFF. A negative
    is *dirty* when the last two differ (section 3.8)."""

    source_datetime_original: str | None = None
    intended_datetime_original: str | None = None
    applied_datetime_original: str | None = None
    date_override: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class RunRecord:
    """Section 3.3. One invocation of `run` or `stitch` that added negatives
    to this roll. `short_id` is assigned by `append_run` and never
    recomputed, so `negative_id`s are stable for the life of the roll."""

    run_id: str
    kind: str
    status: str
    started_at: str
    short_id: str = ""
    convert_run_id: str | None = None
    input_folder: str | None = None
    source_order: list[str] = dataclasses.field(default_factory=list)
    work_dir: str | None = None
    finished_at: str | None = None
    # D-4: the per-channel median of the run's negatives' bounds, recorded
    # so the data for a roll-consistency feature exists from day one.
    # Nothing reads it yet.
    normalization_aggregate: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "short_id": self.short_id,
            "kind": self.kind,
            "status": self.status,
            "convert_run_id": self.convert_run_id,
            "input_folder": self.input_folder,
            "source_order": self.source_order,
            "work_dir": self.work_dir,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "normalization_aggregate": self.normalization_aggregate,
        }


@dataclasses.dataclass
class NegativeMetadata:
    """The extended metadata fields a negative carries *explicitly* (the
    extended-metadata editing feature). A `None` means "inherit the roll's
    fallback" — the effective value is the negative's own, else the roll's.
    Serialized as the negative's nested `metadata` object; every key is
    required-nullable in `roll-manifest.schema.json`, so rows written before
    the feature existed (which carry five NULLs) read back unchanged."""

    city: str | None = None
    state: str | None = None
    camera: str | None = None
    lens: str | None = None
    caption: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class NegativeRecord:
    negative_id: str
    run_id: str
    members: list[str]
    expected_output: str
    fill_color: tuple[int, int, int]
    status: str = "pending"
    # Section 3.7: the 1-based position in the roll, recomputed by
    # `roll_sequence.py` (Chunk P3-6). Null while unranked (pending/failed).
    sequence: int | None = None
    capture_time: CaptureTime = dataclasses.field(default_factory=CaptureTime)
    # The negative's explicit extended-metadata values (None = inherit the
    # roll's fallback).
    metadata: NegativeMetadata = dataclasses.field(default_factory=NegativeMetadata)
    # `{name, size, sha256, width, height}`; a plain dict rather than a
    # dataclass because Phase 1's `OutputRecord` has no dimensions and this
    # module's dataclass list is fixed by the plan.
    output: dict[str, Any] | None = None
    frames: list[FrameRecord] = dataclasses.field(default_factory=list)
    pairs: list[PairRecord] = dataclasses.field(default_factory=list)
    global_rms_px: float | None = None
    canvas: tuple[int, int] | None = None  # (width, height)
    valid_rect: tuple[int, int, int, int] | None = None
    # The normalization record (docs/DECISIONS.md, "Normalization decisions"):
    # per-negative bounds, metering, observed extrema, headroom clipping,
    # and the rebate finding. Null when this build predates normalization
    # or the negative never published.
    normalization: dict[str, Any] | None = None
    # Section 3.14's fill value, recorded beside `fill_color` for the same
    # reason: a file is interpretable without knowing which build wrote it.
    normalized_fill: float | None = None
    # Phase 2 section 3.12.2: never set, because Chunk P2-1 found the rebate
    # is not cleanly detectable with a generic straight-edge finder. The
    # field stays in the contract; its value is always null.
    rebate_deviation_px: float | None = None
    # Whether registration needed the CLAHE retry (stitch_pipeline.py's
    # `_solve_negative`) to solve this negative's layout — section 3.7's
    # "every threshold in force" promise extended to a per-negative choice,
    # since the roll-level `stitch_params` records the fallback as a fixed
    # policy, not which negatives actually used it.
    used_clahe_fallback: bool = False
    error_code: str | None = None
    error_message: str | None = None
    # CLI-generated small preview of the published TIFF, as rendered with
    # the negative's edits applied so far; set by `previews.py`, consumed
    # by the app's Edit tab. Null until first generated.
    preview_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "negative_id": self.negative_id,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "members": self.members,
            "expected_output": self.expected_output,
            "status": self.status,
            "output": self.output,
            "frames": [f.to_dict() for f in self.frames],
            "pairs": [p.to_dict() for p in self.pairs],
            "global_rms_px": self.global_rms_px,
            "canvas": (
                None
                if self.canvas is None
                else {"width": self.canvas[0], "height": self.canvas[1]}
            ),
            "valid_rect": None if self.valid_rect is None else list(self.valid_rect),
            "fill_color": list(self.fill_color),
            "normalized_fill": self.normalized_fill,
            "normalization": self.normalization,
            "rebate_deviation_px": self.rebate_deviation_px,
            "used_clahe_fallback": self.used_clahe_fallback,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "capture_time": self.capture_time.to_dict(),
            "metadata": self.metadata.to_dict(),
            "preview_path": self.preview_path,
        }


@dataclasses.dataclass
class RollMetadata:
    """The roll's metadata stage record. `roll_capture_date` is the
    section 3.7 fallback date every negative without a `date_override`
    ranks on; the five extended-metadata fields are the roll-level
    fallbacks each negative without its own value inherits."""

    roll_capture_date: str | None = None
    last_applied_at: str | None = None
    city: str | None = None
    state: str | None = None
    camera: str | None = None
    lens: str | None = None
    caption: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def effective_metadata(
    roll_metadata: RollMetadata, negative_metadata: NegativeMetadata
) -> dict[str, str | None]:
    """The extended-metadata live fallback: per field, the negative's own
    explicit value when it has one, else the roll's. Nothing is copied
    between rows — changing a roll-level value instantly covers every
    negative without an explicit one, and a negative added later inherits
    the roll's values without a write."""
    return {
        field: getattr(negative_metadata, field) or getattr(roll_metadata, field)
        for field in METADATA_FIELDS
    }


@dataclasses.dataclass(frozen=True)
class RollInvariants:
    """Section 3.4's roll-invariant set, and section 5.4's name for it.
    Everything else — input folder, source list, order, grouping, and the
    batch's `shots_per_negative` — is expected to differ between runs and is
    never compared.

    `icc_profile_sha256` is the *intermediates'* linear profile, sourced
    from the work manifest; `published_icc_profile_sha256` is the density
    profile the published TIFF is tagged with (section 3.12's split)."""

    processing_params: dict[str, Any]
    icc_profile_sha256: str
    stitch_params: dict[str, Any]
    published_icc_profile_sha256: str = ""


@dataclasses.dataclass
class RollManifest:
    scanny_boy_version: str
    roll_id: str
    roll_name: str
    created_at: str
    updated_at: str
    processing_params: dict[str, Any] = dataclasses.field(default_factory=dict)
    icc_profile: dict[str, str] = dataclasses.field(default_factory=dict)
    # The density profile the published TIFFs carry (section 3.12); the
    # other two profile facts are `icc_profile` (the intermediates', from
    # the work manifest) and `stitch_params`.
    published_icc_profile: dict[str, str] = dataclasses.field(default_factory=dict)
    stitch_params: dict[str, Any] = dataclasses.field(default_factory=dict)
    runs: list[RunRecord] = dataclasses.field(default_factory=list)
    sources: list[RollSourceRecord] = dataclasses.field(default_factory=list)
    negatives: list[NegativeRecord] = dataclasses.field(default_factory=list)
    metadata: RollMetadata = dataclasses.field(default_factory=RollMetadata)
    manifest_format_version: int = ROLL_MANIFEST_FORMAT_VERSION
    manifest_kind: str = ROLL_MANIFEST_KIND

    def negative(self, negative_id: str) -> NegativeRecord:
        for n in self.negatives:
            if n.negative_id == negative_id:
                return n
        raise KeyError(negative_id)

    def run(self, run_id: str) -> RunRecord:
        for r in self.runs:
            if r.run_id == run_id:
                return r
        raise KeyError(run_id)

    def all_expected_outputs(self) -> list[str]:
        return [n.expected_output for n in self.negatives]

    def invariants(self) -> RollInvariants:
        return RollInvariants(
            processing_params=self.processing_params,
            icc_profile_sha256=self.icc_profile.get("sha256", ""),
            stitch_params=self.stitch_params,
            published_icc_profile_sha256=self.published_icc_profile.get("sha256", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_format_version": self.manifest_format_version,
            "manifest_kind": self.manifest_kind,
            "scanny_boy_version": self.scanny_boy_version,
            "roll_id": self.roll_id,
            "roll_name": self.roll_name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "processing_params": self.processing_params,
            "icc_profile": self.icc_profile,
            "published_icc_profile": self.published_icc_profile,
            "stitch_params": self.stitch_params,
            "runs": [r.to_dict() for r in self.runs],
            "sources": [s.to_dict() for s in self.sources],
            "negatives": [n.to_dict() for n in self.negatives],
            "metadata": self.metadata.to_dict(),
        }


def new_roll_manifest(*, roll_id: str, roll_name: str) -> RollManifest:
    """Section 5.4 decision 1: the one constructor of an empty roll. No runs,
    no sources, no negatives — and no grouping of its own, since
    `shots_per_negative` is each stitch batch's choice, not the roll's.

    `icc_profile` is seeded from the bundled linear profile's compile-time
    constants and `published_icc_profile` from the density profile's,
    because section 3.4 makes both hashes roll invariants.
    `processing_params` and `stitch_params` stay empty — they are
    established by the first run, and `check_roll_invariants` knows not to
    compare them until then.
    """
    from scanny_boy.manifest import current_scanny_boy_version

    now = _now_iso()
    return RollManifest(
        scanny_boy_version=current_scanny_boy_version(),
        roll_id=roll_id,
        roll_name=roll_name,
        created_at=now,
        updated_at=now,
        processing_params={},
        icc_profile=profile_record(ProfileKind.LINEAR),
        published_icc_profile=profile_record(ProfileKind.DENSITY),
        stitch_params={},
    )


def write_roll_manifest(output_dir: Path, manifest: RollManifest) -> None:
    """Persist the manifest to the library database, registering (or moving)
    the roll row for `output_dir` as a side effect.

    Section 3.3/3.7: `updated_at` is rewritten and every negative's
    `sequence` is recomputed on every write, so this mutates the manifest
    it is given. The import is local to avoid a circular import: this
    module builds `RollManifest`, and `roll_sequence` reads it."""
    from scanny_boy.roll_sequence import sequence_negatives

    manifest.updated_at = _now_iso()
    rank_by_id = {
        negative_id: rank
        for rank, negative_id in enumerate(sequence_negatives(manifest), start=1)
    }
    for negative in manifest.negatives:
        negative.sequence = rank_by_id.get(negative.negative_id)

    repo.save_roll(output_dir, manifest)


def estimate_roll_manifest_size(manifest: RollManifest) -> int:
    """A stand-in for the roll record's storage cost in the disk-space
    check: the JSON encoding of the manifest is a fine proxy for the rows it
    becomes in the database."""
    return len(json.dumps(manifest.to_dict()).encode("utf-8"))


def _validate_output_paths_within(output_dir: Path, manifest: RollManifest) -> None:
    for negative in manifest.negatives:
        names = [negative.expected_output]
        if negative.output is not None:
            names.append(negative.output["name"])
        for name in names:
            try:
                resolve_within(output_dir, name)
            except ValueError as exc:
                raise BadManifestError(str(exc)) from exc


def load_roll_manifest(output_dir: Path) -> RollManifest:
    """Load the roll registered at `output_dir` from the library database.
    Raises `repo.RollNotRegisteredError` (code `ROLL_NOT_FOUND`) when no roll
    is registered there, and `BadManifestError` when a loaded negative names
    an output that escapes `output_dir`."""
    manifest = repo.load_roll(output_dir)
    _validate_output_paths_within(output_dir, manifest)
    return manifest


# --- Section 3.4: invariants, additive runs, naming -----------------------

# `flat_field`/`chromatic_aberration` name the flat-field profile a run
# used. A roll does not lock to one profile: different runs into the same
# roll may each choose a different profile, so these two keys are excluded
# from the processing-params comparison below. Everything else in
# `processing_params` (the raw decode settings, the normalization
# constants) is still established by the first run and held invariant.
ROLL_PROFILE_PROCESSING_PARAMS_KEYS = ("flat_field", "chromatic_aberration")

# `stitch_params["geometry"]` (`_stitch_params` in stitch_pipeline.py) is
# the same profile's optional geometric calibration bucket — excluded here
# for the same reason.
ROLL_PROFILE_STITCH_PARAMS_KEYS = ("geometry",)


def _processing_params_for_invariant_check(params: dict[str, Any]) -> dict[str, Any]:
    """MONOCHROME_PLAN section 5.1: the stored `normalize` block is upgraded
    through `normalization.upgrade_normalize_params` before comparison, so
    a v1 roll compares equal to a v2 build whose new constants sit at their
    defaults. Idempotent; a no-op for v2+ blocks."""
    from scanny_boy.normalization import upgrade_normalize_params

    compared = dict(params)
    if "normalize" in compared:
        compared["normalize"] = upgrade_normalize_params(compared["normalize"])
    return {
        key: value
        for key, value in compared.items()
        if key not in ROLL_PROFILE_PROCESSING_PARAMS_KEYS
    }


def _stitch_params_for_invariant_check(params: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in params.items()
        if key not in ROLL_PROFILE_STITCH_PARAMS_KEYS
    }


def check_roll_invariants(
    manifest: RollManifest, candidate_params: RollInvariants
) -> None:
    """Section 3.4's roll-invariant check, replacing Phase 2's
    `check_roll_rerun_matches` entirely. Input folder, source list, order,
    grouping, each batch's `shots_per_negative`, and the flat-field profile
    (including its optional geometric calibration) are *expected* to differ
    between runs and are never compared.

    The other three invariants are established by the first run, so a roll
    with no runs yet is unseeded and passes; the caller then assigns them.
    This function never mutates. Raises `RollInvariantMismatchError`.
    """
    if not manifest.runs:
        return

    if _processing_params_for_invariant_check(
        manifest.processing_params
    ) != _processing_params_for_invariant_check(candidate_params.processing_params):
        raise RollInvariantMismatchError(
            "this run's processing settings differ from the roll's"
        )
    if manifest.icc_profile.get("sha256") != candidate_params.icc_profile_sha256:
        raise RollInvariantMismatchError(
            "this run's ICC profile differs from the roll's"
        )
    if (
        manifest.published_icc_profile.get("sha256", "")
        != candidate_params.published_icc_profile_sha256
    ):
        raise RollInvariantMismatchError(
            "this run's published ICC profile differs from the roll's"
        )
    if _stitch_params_for_invariant_check(
        manifest.stitch_params
    ) != _stitch_params_for_invariant_check(candidate_params.stitch_params):
        raise RollInvariantMismatchError(
            "this run's stitch settings differ from the roll's"
        )


def append_run(manifest: RollManifest, run: RunRecord) -> None:
    """Append `run` to the roll, assigning its `short_id` per section 3.4.

    `run_id` is a UUID, so six hex characters can collide between two runs
    of one roll. Lengthen to eight, then ten, then the whole `run_id`, until
    the value is free. The chosen value is stored on the record and never
    recomputed, so `negative_id`s are stable for the life of the roll.
    """
    taken = {r.short_id for r in manifest.runs}
    for length in SHORT_ID_LENGTHS:
        candidate = run.run_id[:length]
        if candidate not in taken:
            run.short_id = candidate
            break
    else:
        run.short_id = run.run_id
    manifest.runs.append(run)


def merge_sources(
    manifest: RollManifest, sources: list[SourceRecord], run_id: str
) -> None:
    """Section 3.3: `sources` is keyed by `sha256`. A file already present is
    never appended twice, even from a different folder or under a different
    name, and keeps the `run_id` of the run that *first* contributed it."""
    known = {s.sha256 for s in manifest.sources}
    for source in sources:
        if source.sha256 in known:
            continue
        known.add(source.sha256)
        manifest.sources.append(
            RollSourceRecord(
                filename=source.filename,
                absolute_path=source.absolute_path,
                size=source.size,
                mtime=source.mtime,
                sha256=source.sha256,
                run_id=run_id,
                scan_clip_fractions=source.scan_clip_fractions,
            )
        )


def format_negative_id(short_id: str, index: int) -> str:
    """Section 3.4: `<run.short_id>-negative-NN`, `NN` being the existing
    per-run two-digit index."""
    return f"{short_id}-negative-{index:02d}"


def _claimed_output_names(
    manifest: RollManifest, negative_id: str, adoptable: set[str] | None = None
) -> set[str]:
    """Every name held by some *other* negative, minus `adoptable` — names
    the current group is about to adopt or free, so they are available."""
    claimed: set[str] = set()
    for n in manifest.negatives:
        if n.negative_id == negative_id:
            continue
        claimed.add(n.expected_output)
        if n.output is not None:
            claimed.add(n.output["name"])
    return claimed - (adoptable or set())


def allocate_output_name(
    manifest: RollManifest,
    first_member: str,
    negative_id: str,
    adoptable: set[str] | None = None,
) -> str:
    """Section 3.4's output-naming rule, and the **only** place a published
    name is chosen.

    Phase 2's rule unchanged — the stem of the group's first member in
    canonical order, plus `.tif` — with one addition: if that name is already
    claimed by a *different* `negative_id`, append `-2`, `-3`, … until free.
    `adoptable` names names of negatives the current group covers, which this
    run is about to adopt or remove, so they count as free. Re-stitching an
    adopted negative keeps its existing name, which the pipeline reuses
    rather than re-allocating.
    """
    claimed = _claimed_output_names(manifest, negative_id, adoptable)
    stem = Path(first_member).stem
    candidate = f"{stem}.tif"
    suffix = 1
    while candidate in claimed:
        suffix += 1
        candidate = f"{stem}-{suffix}.tif"
    return candidate
