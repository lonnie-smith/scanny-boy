"""Typed event protocol for scanny-boy's stdout stream.

See `shared/contract/CONTRACT.md` and `shared/contract/schema.json`, which
this module must stay consistent with.
"""

from __future__ import annotations

import dataclasses
import enum
import json
from typing import IO, Any, ClassVar

# Protocol 9 added two features: the extended-metadata editing feature (the
# `metadata` command family — `metadata_updated`, `metadata_values` events,
# the `INVALID_METADATA` code — and the roll/negative extended-metadata
# fields in the roll manifest), and the `edit render-region` command with
# its `region_rendered` event: a 1:1 PNG of one display-space region of a
# published TIFF, with the net rotation folded in, for the app's 100% zoom.
#
# Protocol 10 (2D grid stitching) adds `--grid AxD` on `probe`, `prepare`,
# and `run` (mutually exclusive with `--per-negative`; a strip is the
# down=1 case) and the `INVALID_GRID` error code.
# Protocol 11 is the colour-managed export (docs/EXPORT_PLAN.md): the
# export becomes a rendered positive in Adobe RGB (1998)-compatible colour
# (grey for a mono roll), with the negative's recorded tone op baked in,
# written as a 16-bit lossless JPEG XL with the ICC profile embedded and
# Exif/XMP boxes at encode time. New: the `JXL_ENCODER_UNAVAILABLE` error
# (libjxl could not be reached — a packaging failure), the
# `CAMERA_MATRIX_MISSING` error (a colour roll predating the roll
# manifest's `camera_color` block), and the `CAMERA_MATRIX_CONFLICT`
# warning (a later run's source reports a different matrix than the roll's
# frozen one). The roll manifest gains the optional `camera_color` block
# and the work manifest's curated block gains `rgb_xyz_matrix`/
# `camera_model`. No `export_done` payload change.
#
# Protocol 11 also adds the app's positive/negative display toggle: a `--mode
# positive|negative` flag on `edit render-region` and the new `edit
# render-preview` command (with its `preview_rendered` event) — a
# pure-query render of a negative's whole display image, downscaled like
# the cached preview. The negative mode is the un-inverted density view;
# no tone ever reaches it.
#
# Protocol 15 retires the film-kind auto-detector: `--film-kind` moves to
# `roll init` (required, `colour` or `monochrome` only); `run`/`stitch`
# read the roll manifest's `film.kind` instead. The `MONO_DETECT_AMBIGUOUS`
# and `MONO_DECISION_CONFLICT` warning codes are removed; `FILM_KIND_REQUIRED`
# is added for unseeded rolls with no `film` block.
#
# Protocol 11 (MONOCHROME_PLAN) added monochrome film support: single-channel
# published TIFFs on silver B&W rolls, the top-level `film` block, and the
# `ScannyBoy-Density-Grey-v1.icc` profile.
#
# Protocol 11 also extends the preview tone adjustment: seven curve
# controls and two auto flags on `edit tone`, matching `tone_*` fields on
# `roll info`, and the `TONE_METERING_UNAVAILABLE` code.
# Protocol 12 adds the preview colour adjustment: the `edit color`
# subcommand, thirteen derived `color_*` fields (twelve stored params plus
# `color_temperature`) and `film_kind` on `roll info`, and the `color` op
# in the ops log.
#
# Protocol 13 is the film-base reference (docs/REBATE_ANCHORING.md): the
# new `roll set-base-frame` command (with its `base_frame_set` event)
# attaches a measured per-roll film-base reference — the thin-end colour
# anchor for every negative on the roll — through a new top-level
# `film_base` block on the roll manifest (`roll info` reports it verbatim).
# The reference is replaceable until the roll's first negative is
# published, then locked (`FILM_BASE_LOCKED`); a run/stitch on a roll
# without one fails `FILM_BASE_REQUIRED`; a roll whose manifest predates
# the feature cannot take new negatives or a base frame
# (`ROLL_PREDATES_FILM_BASE`); `run`/`stitch`/`set-base-frame` refuse old
# manifests. Seven gate/diagnostic codes (`FILM_BASE_NOT_FOUND`,
# `_TOO_SMALL`, `_CLIPPED`, `_TOO_DARK`, `_AMBIGUOUS`) shape the attach
# path, and two warnings (`FILM_BASE_CAMERA_CONFLICT`,
# `FILM_BASE_FLATFIELD_CONFLICT`) record rig disagreements. `probe --roll`
# reports `film_base` so the app can gate Convert without starting a run.
#
# The same protocol 13 also carries spotting (SPOTTING_PLAN, merged from
# origin/main): `edit detect-spots` (the detector's proposals, one `spots`
# op per negative), `edit spots` (review — reject/accept ids by id, the
# whole-negative repair switch, clear), and `edit list-spots` (a pure
# query). One new event, `spots_reported` — carrying the spots as
# **display-space** rects, ids unchanged, never the RLE masks (Swift
# converts no coordinates) — plus `SPOT_LIMIT_REACHED` (the detector
# capped its proposals) and `SPOTS_STALE` (a re-stitch changed the canvas;
# the set needs re-detecting). `roll info` gains a per-negative `spots`
# summary block. No new error codes of its own: every failure there is
# `INVALID_EDIT`, `ROLL_NOT_FOUND` or `NEGATIVE_NOT_FOUND`.
#
# Protocol 14 is cast removal's second tie and auto solve
# (docs/CAST_REMOVAL_PLAN.md): `edit color` gains `--cast-removal-highlights`
# (the highlight-end tie strength, 0..1) and `--auto-cast` (solve the global
# filtration from the negative's recorded neutral estimate — exclusive with
# `--reset` and with an explicit `--cyan`/`--magenta`/`--yellow`), the
# `color_cast_removal_highlights` derived field joins the other `color_*`
# fields on `roll info`, and the per-negative `normalization` block gains
# two recorded meters — `highlight_refs` (the dense end's same-pixel
# neutral set, null when the band held no trustworthy neutrals) and
# `neutral_residual` (the `(R-G, B-G)` offset the auto solve reads). The
# auto reads a stitch-time meter, so it is unavailable on rolls stitched by
# an older build — that absence warns `TONE_METERING_UNAVAILABLE`, reused
# for the colour-only condition rather than renamed (it shipped in protocol
# 11; renaming a live contract code costs more than the wart). Global and
# regional CMY are now mean-removed, which changes how already-recorded
# colour ops render — accepted, the op being preview-only (§0.3). No new
# codes.
# Protocol 16 adds named grid configuration presets: the `grid create` /
# `grid list` / `grid delete` command family and the `grid_created`,
# `grid_list`, and `grid_deleted` events. Each preset is a user label for
# an `across` x `down` shape the app picks when adding scans.
# Protocol 17 retires `STITCH_GRID_ORDER_UNEXPECTED`: cell assignment is
# geometry-only and capture order is not checked.
#
# Protocol 18 (docs/OPTIMIZATION.md §2.1) adds `scanny-boy serve`: the
# resident process that reads newline-delimited JSON *requests* on stdin
# and answers on this same stdout stream. Every event emitted while a
# served request is in flight gains an optional `request_id` field, and
# each request ends with a `finished` carrying its `request_id` and the
# exit status the one-shot CLI would have returned. One-shot invocations
# continue to emit events without `request_id`; the app's decoder treats
# it as optional for exactly that reason.
PROTOCOL_VERSION = 18


class EventType(enum.StrEnum):
    STARTED = "started"
    PROBE_RESULT = "probe_result"
    PROGRESS = "progress"
    ITEM_DONE = "item_done"
    GROUP_DONE = "group_done"
    GROUP_FAILED = "group_failed"
    WARNING = "warning"
    ERROR = "error"
    FINISHED = "finished"
    NEGATIVE_DONE = "negative_done"
    NEGATIVE_FAILED = "negative_failed"
    ROLL_CREATED = "roll_created"
    ROLL_LIST = "roll_list"
    ROLL_INFO = "roll_info"
    ROLL_RENAMED = "roll_renamed"
    ROLL_DELETED = "roll_deleted"
    METADATA_APPLIED = "metadata_applied"
    METADATA_SKIPPED = "metadata_skipped"
    METADATA_UPDATED = "metadata_updated"
    METADATA_VALUES = "metadata_values"
    EDIT_RECORDED = "edit_recorded"
    NEGATIVE_DELETED = "negative_deleted"
    REGION_RENDERED = "region_rendered"
    PREVIEW_RENDERED = "preview_rendered"
    EXPORT_DONE = "export_done"
    FLATFIELD_CREATED = "flatfield_created"
    FLATFIELD_LIST = "flatfield_list"
    FLATFIELD_DELETED = "flatfield_deleted"
    FLATFIELD_PROGRESS = "flatfield_progress"
    GRID_CREATED = "grid_created"
    GRID_LIST = "grid_list"
    GRID_DELETED = "grid_deleted"
    BASE_FRAME_SET = "base_frame_set"
    SPOTS_REPORTED = "spots_reported"


class Stage(enum.StrEnum):
    # Renamed `convert` -> `prepare` (docs/DECISIONS.md, "Normalization
    # decisions"): "Convert" is reserved, unambiguously, for the whole `run`, so
    # stage 1 — decode + flat-field + write intermediates — is `prepare`.
    PREPARE = "prepare"
    STITCH = "stitch"


class PipelineStep(enum.StrEnum):
    DECODE = "decode"
    WRITE_TIFF = "write_tiff"
    ADD_METADATA = "add_metadata"
    LOAD = "load"
    DETECT = "detect"
    MATCH = "match"
    SOLVE = "solve"
    WARP = "warp"
    BLEND = "blend"
    # Emitted per negative in the stitch stage between BLEND and
    # WRITE_STITCHED (section 3.10).
    NORMALIZE = "normalize"
    WRITE_STITCHED = "write_stitched"


class Code(enum.StrEnum):
    """Stable error and warning codes from CONTRACT.md."""

    NO_FILES = "NO_FILES"
    NON_CONTIGUOUS_SELECTION = "NON_CONTIGUOUS_SELECTION"
    NOT_DIVISIBLE = "NOT_DIVISIBLE"
    INVALID_PER_NEGATIVE = "INVALID_PER_NEGATIVE"
    INVALID_GRID = "INVALID_GRID"
    MISSING_CAPTURE_TIME = "MISSING_CAPTURE_TIME"
    FILENAME_SORT_USED = "FILENAME_SORT_USED"
    UNSUPPORTED_RAW = "UNSUPPORTED_RAW"
    CAPTURE_METADATA_MISSING = "CAPTURE_METADATA_MISSING"
    CAPTURE_SETTINGS_DIFFER = "CAPTURE_SETTINGS_DIFFER"
    UNREADABLE_RAW = "UNREADABLE_RAW"
    OUTPUT_SAME_AS_INPUT = "OUTPUT_SAME_AS_INPUT"
    OUTPUT_NOT_WRITABLE = "OUTPUT_NOT_WRITABLE"
    OUTPUT_NOT_EMPTY = "OUTPUT_NOT_EMPTY"
    OUTPUT_CONFLICT = "OUTPUT_CONFLICT"
    INSUFFICIENT_DISK = "INSUFFICIENT_DISK"
    INSUFFICIENT_MEMORY = "INSUFFICIENT_MEMORY"
    BAD_MANIFEST = "BAD_MANIFEST"
    MANIFEST_MISMATCH = "MANIFEST_MISMATCH"
    ICC_PROFILE_INVALID = "ICC_PROFILE_INVALID"
    TIFF_WRITE_FAILED = "TIFF_WRITE_FAILED"
    CANCELLED = "CANCELLED"
    WORK_SAME_AS_OUTPUT = "WORK_SAME_AS_OUTPUT"
    WORK_MANIFEST_UNUSABLE = "WORK_MANIFEST_UNUSABLE"
    INTERMEDIATE_MISSING = "INTERMEDIATE_MISSING"
    INTERMEDIATE_CHANGED = "INTERMEDIATE_CHANGED"
    STITCH_INSUFFICIENT_MATCHES = "STITCH_INSUFFICIENT_MATCHES"
    STITCH_UNDERCONSTRAINED = "STITCH_UNDERCONSTRAINED"
    STITCH_RESIDUAL_TOO_HIGH = "STITCH_RESIDUAL_TOO_HIGH"
    STITCH_OUTPUT_TOO_LARGE = "STITCH_OUTPUT_TOO_LARGE"
    STITCH_FAILED = "STITCH_FAILED"
    STITCH_SCALE_DRIFT = "STITCH_SCALE_DRIFT"
    STITCH_GAIN_DRIFT = "STITCH_GAIN_DRIFT"
    STITCH_LAYOUT_UNEXPECTED = "STITCH_LAYOUT_UNEXPECTED"
    STITCH_REBATE_CHECK_FAILED = "STITCH_REBATE_CHECK_FAILED"
    STITCH_CLAHE_FALLBACK_USED = "STITCH_CLAHE_FALLBACK_USED"
    OUTPUT_DIMENSIONS_LARGE = "OUTPUT_DIMENSIONS_LARGE"
    ROLL_NOT_FOUND = "ROLL_NOT_FOUND"
    ROLL_MANIFEST_UNSUPPORTED = "ROLL_MANIFEST_UNSUPPORTED"
    ROLL_EXISTS = "ROLL_EXISTS"
    ROLL_RENAME_FAILED = "ROLL_RENAME_FAILED"
    ROLL_INVARIANT_MISMATCH = "ROLL_INVARIANT_MISMATCH"
    OUTPUT_MODIFIED_EXTERNALLY = "OUTPUT_MODIFIED_EXTERNALLY"
    METADATA_WRITE_FAILED = "METADATA_WRITE_FAILED"
    ORPHAN_FILE_NOT_REMOVED = "ORPHAN_FILE_NOT_REMOVED"
    NEGATIVE_NOT_FOUND = "NEGATIVE_NOT_FOUND"
    INVALID_EDIT = "INVALID_EDIT"
    INVALID_METADATA = "INVALID_METADATA"
    EXPORT_FAILED = "EXPORT_FAILED"
    # EXPORT_PLAN §1.2: libjxl could not be reached through the process's
    # symbol namespace. A packaging failure, not a user error.
    JXL_ENCODER_UNAVAILABLE = "JXL_ENCODER_UNAVAILABLE"
    # EXPORT_PLAN §3: the camera colour matrix recorded in the roll manifest.
    CAMERA_MATRIX_MISSING = "CAMERA_MATRIX_MISSING"
    CAMERA_MATRIX_CONFLICT = "CAMERA_MATRIX_CONFLICT"
    PREVIEW_FAILED = "PREVIEW_FAILED"
    FLATFIELD_PROFILE_NOT_FOUND = "FLATFIELD_PROFILE_NOT_FOUND"
    FLATFIELD_PROFILE_EXISTS = "FLATFIELD_PROFILE_EXISTS"
    FLATFIELD_PROFILE_IN_USE = "FLATFIELD_PROFILE_IN_USE"
    FLATFIELD_GAIN_MAP_MISSING = "FLATFIELD_GAIN_MAP_MISSING"
    FLATFIELD_ASPECT_MISMATCH = "FLATFIELD_ASPECT_MISMATCH"
    FLATFIELD_HIGHLIGHT_CLIPPED = "FLATFIELD_HIGHLIGHT_CLIPPED"
    GRID_PROFILE_NOT_FOUND = "GRID_PROFILE_NOT_FOUND"
    GRID_PROFILE_EXISTS = "GRID_PROFILE_EXISTS"
    GEOMETRY_INSUFFICIENT_FRAMES = "GEOMETRY_INSUFFICIENT_FRAMES"
    GEOMETRY_BOARD_NOT_DETECTED = "GEOMETRY_BOARD_NOT_DETECTED"
    GEOMETRY_FRAME_SIZE_MISMATCH = "GEOMETRY_FRAME_SIZE_MISMATCH"
    GEOMETRY_FIT_REJECTED = "GEOMETRY_FIT_REJECTED"
    GEOMETRY_MAGNITUDE_SUSPECT = "GEOMETRY_MAGNITUDE_SUSPECT"
    GEOMETRY_FEW_FRAMES = "GEOMETRY_FEW_FRAMES"
    CHROMATIC_FIT_REJECTED = "CHROMATIC_FIT_REJECTED"
    SCAN_CLIPPED = "SCAN_CLIPPED"
    NORMALIZE_DEGENERATE_BOUNDS = "NORMALIZE_DEGENERATE_BOUNDS"
    NORMALIZE_HEADROOM_CLIPPED = "NORMALIZE_HEADROOM_CLIPPED"
    TONE_METERING_UNAVAILABLE = "TONE_METERING_UNAVAILABLE"
    FILM_KIND_REQUIRED = "FILM_KIND_REQUIRED"
    FILM_KIND_LOCKED = "FILM_KIND_LOCKED"
    # REBATE_ANCHORING §7.2: the film-base reference. Codes may exist before
    # anything raises them (chunk B-1); the consumers arrive with B-2/B-3.
    FILM_BASE_REQUIRED = "FILM_BASE_REQUIRED"
    FILM_BASE_LOCKED = "FILM_BASE_LOCKED"
    FILM_BASE_NOT_FOUND = "FILM_BASE_NOT_FOUND"
    FILM_BASE_TOO_SMALL = "FILM_BASE_TOO_SMALL"
    FILM_BASE_CLIPPED = "FILM_BASE_CLIPPED"
    FILM_BASE_TOO_DARK = "FILM_BASE_TOO_DARK"
    FILM_BASE_AMBIGUOUS = "FILM_BASE_AMBIGUOUS"
    ROLL_PREDATES_FILM_BASE = "ROLL_PREDATES_FILM_BASE"
    FILM_BASE_CAMERA_CONFLICT = "FILM_BASE_CAMERA_CONFLICT"
    FILM_BASE_FLATFIELD_CONFLICT = "FILM_BASE_FLATFIELD_CONFLICT"
    # SPOTTING_PLAN §1.4: the detector found more spots than it may
    # propose; the highest-scoring ones were kept. The remedy is a lower
    # --sensitivity.
    SPOT_LIMIT_REACHED = "SPOT_LIMIT_REACHED"
    # SPOTTING_PLAN §1.5: the negative's spot set was recorded against a
    # canvas a re-stitch has replaced; it repairs nothing and needs
    # re-detecting.
    SPOTS_STALE = "SPOTS_STALE"
    LIBRARY_DB_UNSUPPORTED = "LIBRARY_DB_UNSUPPORTED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclasses.dataclass(frozen=True, kw_only=True)
class Event:
    """Base class for one line of the stdout event protocol.

    Subclasses fix `event_type` and add their own fields. `to_dict` always
    puts `protocol_version`, `event`, and (when present) `run_id` first, then
    the subclass's own fields, matching schema.json's required base
    properties.
    """

    event_type: ClassVar[EventType]

    run_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "protocol_version": PROTOCOL_VERSION,
            "event": self.event_type.value,
        }
        if self.run_id is not None:
            data["run_id"] = self.run_id
        for field in dataclasses.fields(self):
            if field.name == "run_id":
                continue
            value = getattr(self, field.name)
            data[field.name] = _jsonable(value)
        return data


def _jsonable(value: Any) -> Any:
    if isinstance(value, enum.Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _jsonable(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


@dataclasses.dataclass(frozen=True, kw_only=True)
class Started(Event):
    event_type: ClassVar[EventType] = EventType.STARTED

    command: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class RollOverlapEntry:
    negative_id: str
    expected_output: str
    run_id: str
    overlapping_sources: list[str]
    group_index: int


@dataclasses.dataclass(frozen=True, kw_only=True)
class ProbeResult(Event):
    event_type: ClassVar[EventType] = EventType.PROBE_RESULT

    catalogue: list[str]
    warnings: list[str] = dataclasses.field(default_factory=list)
    groups: list[list[str]] = dataclasses.field(default_factory=list)
    # Present (non-empty/non-null) only when `--out` was given alongside
    # `--files` and validation reached the disk estimate (section 4.1).
    output_conflicts: list[str] = dataclasses.field(default_factory=list)
    estimated_required_bytes: int | None = None
    available_bytes: int | None = None
    # Present only when `--roll` was given alongside a validated `--files`
    # selection (Phase 3 section 3.5).
    roll_overlap: list[RollOverlapEntry] = dataclasses.field(default_factory=list)
    # The roll's film-base reference block, verbatim from the roll manifest,
    # when `--roll` was given (docs/REBATE_ANCHORING.md §7.1) — how the app
    # gates Convert without starting a run.
    film_base: dict[str, Any] | None = None


@dataclasses.dataclass(frozen=True, kw_only=True)
class Progress(Event):
    event_type: ClassVar[EventType] = EventType.PROGRESS

    source_index: int
    step: PipelineStep
    completed: int
    total: int
    stage: Stage = Stage.PREPARE


@dataclasses.dataclass(frozen=True, kw_only=True)
class ItemDone(Event):
    event_type: ClassVar[EventType] = EventType.ITEM_DONE

    source_index: int
    output: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class GroupDone(Event):
    event_type: ClassVar[EventType] = EventType.GROUP_DONE

    group_id: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class GroupFailed(Event):
    event_type: ClassVar[EventType] = EventType.GROUP_FAILED

    group_id: str
    code: Code
    message: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class NegativeDone(Event):
    event_type: ClassVar[EventType] = EventType.NEGATIVE_DONE

    negative_id: str
    output: str
    width: int
    height: int
    global_rms_px: float
    max_overlap_mad: float


@dataclasses.dataclass(frozen=True, kw_only=True)
class NegativeFailed(Event):
    event_type: ClassVar[EventType] = EventType.NEGATIVE_FAILED

    negative_id: str
    code: Code
    message: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class WarningEvent(Event):
    event_type: ClassVar[EventType] = EventType.WARNING

    code: Code
    message: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class ErrorEvent(Event):
    event_type: ClassVar[EventType] = EventType.ERROR

    code: Code
    message: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class Finished(Event):
    event_type: ClassVar[EventType] = EventType.FINISHED

    status: str
    exit_status: int


@dataclasses.dataclass(frozen=True, kw_only=True)
class RollCreated(Event):
    event_type: ClassVar[EventType] = EventType.ROLL_CREATED

    roll_id: str
    roll_name: str
    path: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class RollRenamed(Event):
    """Section 5.5: `roll rename`'s counterpart to `RollCreated`, carrying
    the roll's new location."""

    event_type: ClassVar[EventType] = EventType.ROLL_RENAMED

    roll_id: str
    roll_name: str
    path: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class RollDeleted(Event):
    """`roll delete`'s counterpart to `RollCreated`, carrying the deleted
    roll's id and the (now unregistered) folder path."""

    event_type: ClassVar[EventType] = EventType.ROLL_DELETED

    roll_id: str
    path: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class RollListingReason:
    code: str
    message: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class RollListingEntry:
    path: str
    status: str
    reason: RollListingReason | None = None
    roll_id: str | None = None
    roll_name: str | None = None
    negative_count: int | None = None


@dataclasses.dataclass(frozen=True, kw_only=True)
class RollList(Event):
    event_type: ClassVar[EventType] = EventType.ROLL_LIST

    rolls: list[RollListingEntry]


@dataclasses.dataclass(frozen=True, kw_only=True)
class RollInfo(Event):
    event_type: ClassVar[EventType] = EventType.ROLL_INFO

    manifest: dict[str, Any]


@dataclasses.dataclass(frozen=True, kw_only=True)
class MetadataApplied(Event):
    event_type: ClassVar[EventType] = EventType.METADATA_APPLIED

    negative_id: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class MetadataSkipped(Event):
    event_type: ClassVar[EventType] = EventType.METADATA_SKIPPED

    negative_id: str
    code: Code
    message: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class MetadataUpdated(Event):
    """`metadata set`'s confirmation: the updated roll manifest, whole, in
    the same shape a `roll_info` event carries — so the app can swap its
    in-memory roll without a `roll info` round trip, exactly as an
    `edit_recorded` preview-path update lands in place."""

    event_type: ClassVar[EventType] = EventType.METADATA_UPDATED

    manifest: dict[str, Any]


@dataclasses.dataclass(frozen=True, kw_only=True)
class MetadataValues(Event):
    """`metadata values`' answer: the catalog of previously-entered values
    for one extended-metadata field, most-recently-used first — the list the
    app's typeahead offers."""

    event_type: ClassVar[EventType] = EventType.METADATA_VALUES

    field: str
    values: list[str]


@dataclasses.dataclass(frozen=True, kw_only=True)
class EditRecorded(Event):
    """`edit rotate`/`edit flip`'s confirmation: the ops log entry as
    appended, the negative's net transform after it (quarter turns plus the
    horizontal-mirror flag plus the net fine angle — a flip and a rotation
    do not commute, so one number cannot carry them), and the regenerated
    preview the app should now display. No pixel data of the published TIFF
    changes."""

    event_type: ClassVar[EventType] = EventType.EDIT_RECORDED

    negative_id: str
    edit: dict[str, Any]
    rotation_quarter_turns: int
    flipped_horizontally: bool
    preview_path: str | None
    fine_rotation_deg: float = 0.0


@dataclasses.dataclass(frozen=True, kw_only=True)
class NegativeDeleted(Event):
    """`edit delete`'s confirmation: the negative's record is gone from the
    library database (its edits log cascaded away) and its published TIFF
    was unlinked from the roll folder. `output` is the deleted TIFF's name,
    or None when the negative had never been stitched."""

    event_type: ClassVar[EventType] = EventType.NEGATIVE_DELETED

    negative_id: str
    output: str | None


@dataclasses.dataclass(frozen=True, kw_only=True)
class RegionRendered(Event):
    """`edit render-region`'s confirmation: one display-space region of a
    negative's published TIFF rendered at 1:1 — net rotation folded in,
    display encode identical to `generate_preview` — as a lossless PNG at
    `path`. `x`/`y`/`width`/`height` are the rect actually rendered,
    post-clamp against the image bounds; no pixel data of the published
    TIFF changes and nothing is recorded anywhere."""

    event_type: ClassVar[EventType] = EventType.REGION_RENDERED

    negative_id: str
    path: str
    x: int
    y: int
    width: int
    height: int


@dataclasses.dataclass(frozen=True, kw_only=True)
class PreviewRendered(Event):
    """`edit render-preview`'s confirmation: a negative's whole display
    image — net transform folded in, downscaled like the cached preview —
    rendered in the display encode the command's `--mode` named, as a
    lossless PNG at `path`; `width`/`height` are the written PNG's pixel
    dimensions. No tone ever reaches the negative mode's un-inverted
    density view. No pixel data of the published TIFF changes and nothing
    is recorded anywhere."""

    event_type: ClassVar[EventType] = EventType.PREVIEW_RENDERED

    negative_id: str
    path: str
    width: int
    height: int


@dataclasses.dataclass(frozen=True, kw_only=True)
class ExportDone(Event):
    """One negative's edits applied and written into the export folder."""

    event_type: ClassVar[EventType] = EventType.EXPORT_DONE

    negative_id: str
    output: str
    width: int
    height: int


@dataclasses.dataclass(frozen=True, kw_only=True)
class BaseFrameSet(Event):
    """`roll set-base-frame`'s confirmation (docs/REBATE_ANCHORING.md
    §7.1): the attached (or replaced) film-base reference's identity and
    measurement summary. `density` is the per-channel median log10 density
    of the chosen population; `area_fraction` is its share of the frame;
    `population_count` is every population the detector found; `locked` is
    always false — a locked roll refuses the command outright."""

    event_type: ClassVar[EventType] = EventType.BASE_FRAME_SET

    roll_id: str
    source_name: str
    density: list[float]
    area_fraction: float
    population_count: int
    locked: bool


@dataclasses.dataclass(frozen=True, kw_only=True)
class SpotsReported(Event):
    """The three spotting commands' shared event: a negative's spot set as
    the app draws it. Every rect is **display space**, already transformed
    by the net rotation/flip/fine angle — the app never converts
    coordinates, and rejection is by `id`, never by position. The RLE masks
    stay in the ops log; they are an implementation detail of the repair
    and would multiply the payload for nothing. `found` is the detector's
    count before the `MAX_SPOTS` cap. `preview_path` is null for
    `list-spots`, which is a pure query."""

    event_type: ClassVar[EventType] = EventType.SPOTS_REPORTED

    negative_id: str
    detector_version: int
    sensitivity: float
    repair: bool
    spots: list[dict[str, Any]]  # display space, no rle
    found: int
    preview_path: str | None


@dataclasses.dataclass(frozen=True, kw_only=True)
class FlatFieldProfileSummary:
    """The profile fields a `flatfield` event carries. The gain map's path
    and SHA-256 are deliberately absent: the path is app-private storage the
    UI has no use for, and the hash is roll-invariant bookkeeping the CLI
    owns. The calibration fields (docs/GEOMETRIC_PLAN.md section 6) are a
    straight decode of what the profile record holds — no computation in
    Swift."""

    profile_id: str
    name: str
    reference_width: int
    reference_height: int
    source_path: str | None
    created_at: str
    board_key: str | None = None
    has_geometry: bool = False
    chromatic_aberration_mode: str | None = None
    calibration_report: dict | None = None


@dataclasses.dataclass(frozen=True, kw_only=True)
class FlatFieldCreated(Event):
    event_type: ClassVar[EventType] = EventType.FLATFIELD_CREATED

    profile: FlatFieldProfileSummary


@dataclasses.dataclass(frozen=True, kw_only=True)
class FlatFieldList(Event):
    event_type: ClassVar[EventType] = EventType.FLATFIELD_LIST

    profiles: list[FlatFieldProfileSummary]


@dataclasses.dataclass(frozen=True, kw_only=True)
class FlatFieldDeleted(Event):
    event_type: ClassVar[EventType] = EventType.FLATFIELD_DELETED

    profile_id: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class FlatFieldProgress(Event):
    """Progress of a long `flatfield create` (docs/GEOMETRIC_PLAN.md section
    4.8). Deliberately carries no `run_id`: the `flatfield` family is not a
    pipeline run, and this keeps that rule."""

    event_type: ClassVar[EventType] = EventType.FLATFIELD_PROGRESS

    phase: str  # "detect" | "fit" | "chromatic" | "reference"
    completed: int
    total: int


@dataclasses.dataclass(frozen=True, kw_only=True)
class GridProfileSummary:
    """The fields a `grid` event carries — a label and its grid shape."""

    profile_id: str
    name: str
    across: int
    down: int
    created_at: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class GridCreated(Event):
    event_type: ClassVar[EventType] = EventType.GRID_CREATED

    profile: GridProfileSummary


@dataclasses.dataclass(frozen=True, kw_only=True)
class GridList(Event):
    event_type: ClassVar[EventType] = EventType.GRID_LIST

    profiles: list[GridProfileSummary]


@dataclasses.dataclass(frozen=True, kw_only=True)
class GridDeleted(Event):
    event_type: ClassVar[EventType] = EventType.GRID_DELETED

    profile_id: str


class EventWriter:
    """Writes events to a stream as one flushed JSON line each.

    `request_id` is `scanny-boy serve`'s addition (docs/OPTIMIZATION.md
    §2.1): when set, every event written through this writer carries it,
    which is how one shared stdout stream is partitioned among the
    requests the resident process answers. One-shot invocations leave it
    None and emit events without the field, exactly as before.
    """

    def __init__(self, stream: IO[str], request_id: str | None = None) -> None:
        self._stream = stream
        self._request_id = request_id

    def write(self, event: Event) -> None:
        data = event.to_dict()
        if self._request_id is not None:
            data["request_id"] = self._request_id
        line = json.dumps(data, separators=(",", ":"), sort_keys=True)
        self._stream.write(line + "\n")
        self._stream.flush()
