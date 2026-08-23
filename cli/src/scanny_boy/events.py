"""Typed event protocol for scanny-boy's stdout stream.

See `shared/contract/CONTRACT.md` and `shared/contract/schema.json`, which
this module must stay consistent with.
"""

from __future__ import annotations

import dataclasses
import enum
import json
from typing import IO, Any, ClassVar

PROTOCOL_VERSION = 3


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
    METADATA_APPLIED = "metadata_applied"
    METADATA_SKIPPED = "metadata_skipped"


class Stage(enum.StrEnum):
    CONVERT = "convert"
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
    WRITE_STITCHED = "write_stitched"


class Code(enum.StrEnum):
    """Stable error and warning codes from CONTRACT.md."""

    NO_FILES = "NO_FILES"
    NON_CONTIGUOUS_SELECTION = "NON_CONTIGUOUS_SELECTION"
    NOT_DIVISIBLE = "NOT_DIVISIBLE"
    INVALID_PER_NEGATIVE = "INVALID_PER_NEGATIVE"
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
    STITCH_LAYOUT_UNEXPECTED = "STITCH_LAYOUT_UNEXPECTED"
    STITCH_REBATE_CHECK_FAILED = "STITCH_REBATE_CHECK_FAILED"
    OUTPUT_DIMENSIONS_LARGE = "OUTPUT_DIMENSIONS_LARGE"
    ROLL_NOT_FOUND = "ROLL_NOT_FOUND"
    ROLL_MANIFEST_UNSUPPORTED = "ROLL_MANIFEST_UNSUPPORTED"
    ROLL_EXISTS = "ROLL_EXISTS"
    ROLL_RENAME_FAILED = "ROLL_RENAME_FAILED"
    ROLL_INVARIANT_MISMATCH = "ROLL_INVARIANT_MISMATCH"
    PER_NEGATIVE_LOCKED = "PER_NEGATIVE_LOCKED"
    OUTPUT_MODIFIED_EXTERNALLY = "OUTPUT_MODIFIED_EXTERNALLY"
    METADATA_WRITE_FAILED = "METADATA_WRITE_FAILED"


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


@dataclasses.dataclass(frozen=True, kw_only=True)
class Progress(Event):
    event_type: ClassVar[EventType] = EventType.PROGRESS

    source_index: int
    step: PipelineStep
    completed: int
    total: int
    stage: Stage = Stage.CONVERT


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


class EventWriter:
    """Writes events to a stream as one flushed JSON line each."""

    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream

    def write(self, event: Event) -> None:
        line = json.dumps(event.to_dict(), separators=(",", ":"), sort_keys=True)
        self._stream.write(line + "\n")
        self._stream.flush()
