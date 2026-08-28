"""Orchestrates `probe`'s two levels of detail: whole-catalogue canonical
ordering, and (with `--files`) selection, grouping, and setup-consistency
validation. See `docs/IMPLEMENTATION_PLAN.md` section 4.1.

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
from collections.abc import Callable
from pathlib import Path

from scanny_boy.catalogue import (
    CatalogueError,
    compute_canonical_order,
    discover_catalogue,
)
from scanny_boy.consistency import ConsistencyError, check_consistency
from scanny_boy.events import Code
from scanny_boy.metadata import (
    UnreadableRawError,
    UnsupportedRawError,
    read_source_settings,
)
from scanny_boy.selection import (
    SelectionUsageError,
    group,
    is_contiguous,
    nearest_valid_counts,
    order_selection,
)

OnWarning = Callable[[Code, str], None]


class ProbeFailure(Exception):
    def __init__(self, code: Code, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclasses.dataclass(frozen=True)
class ProbeOutcome:
    catalogue: list[str]
    groups: list[list[str]]


def run_probe(
    input_dir: Path,
    files: list[str] | None,
    per_negative: int,
    *,
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

    return ProbeOutcome(catalogue=order.order, groups=groups)
