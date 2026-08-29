"""Typed event protocol for scanny-boy's stdout stream.

See `shared/contract/CONTRACT.md` and `shared/contract/schema.json`, which
this module must stay consistent with.
"""

from __future__ import annotations

import dataclasses
import enum
import json
from typing import IO, Any, ClassVar

PROTOCOL_VERSION = 1


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


class PipelineStep(enum.StrEnum):
    DECODE = "decode"
    WRITE_TIFF = "write_tiff"
    ADD_METADATA = "add_metadata"


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
    CAPTURE_SPAN_TOO_LONG = "CAPTURE_SPAN_TOO_LONG"
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
    return value


@dataclasses.dataclass(frozen=True, kw_only=True)
class Started(Event):
    event_type: ClassVar[EventType] = EventType.STARTED

    command: str


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


@dataclasses.dataclass(frozen=True, kw_only=True)
class Progress(Event):
    event_type: ClassVar[EventType] = EventType.PROGRESS

    source_index: int
    step: PipelineStep
    completed: int
    total: int


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


class EventWriter:
    """Writes events to a stream as one flushed JSON line each."""

    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream

    def write(self, event: Event) -> None:
        line = json.dumps(event.to_dict(), separators=(",", ":"), sort_keys=True)
        self._stream.write(line + "\n")
        self._stream.flush()
