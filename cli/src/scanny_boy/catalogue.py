"""Catalogue discovery and canonical ordering.

See `docs/IMPLEMENTATION_PLAN.md` section 1.1 (vocabulary) and section 3.3
(sorting).
"""

from __future__ import annotations

import dataclasses
import datetime
import re
from pathlib import Path

import exifread

_NATURAL_CHUNK = re.compile(r"(\d+)")


class CatalogueError(Exception):
    """The input folder itself is structurally invalid: a duplicate real
    file under two directory entries, or an entry that resolves outside the
    folder (for example through a symlink). Neither has a dedicated
    CONTRACT.md code; `probe.py` reports both as `NO_FILES`.
    """


def natural_sort_key(name: str) -> tuple:
    """A comparison key that orders embedded numbers numerically, so
    `DSC_9` sorts before `DSC_10`."""
    return tuple(
        int(chunk) if chunk.isdigit() else chunk.lower()
        for chunk in _NATURAL_CHUNK.split(name)
    )


def discover_catalogue(input_dir: Path) -> list[str]:
    """List `.nef` files directly inside `input_dir`, case-insensitively and
    without recursion, resolving paths per section 3.2."""
    resolved_input = input_dir.resolve()
    names: list[str] = []
    seen_resolved: set[Path] = set()
    for entry in sorted(input_dir.iterdir(), key=lambda e: e.name):
        if not entry.is_file() or entry.suffix.lower() != ".nef":
            continue
        resolved = entry.resolve()
        if resolved.parent != resolved_input:
            raise CatalogueError(
                f"{entry.name!r} resolves outside the input folder {input_dir}"
            )
        if resolved in seen_resolved:
            raise CatalogueError(f"duplicate file in input folder: {entry.name!r}")
        seen_resolved.add(resolved)
        names.append(entry.name)
    return names


@dataclasses.dataclass(frozen=True)
class CaptureTimestamp:
    when: datetime.datetime
    subsec_fraction: float = 0.0

    def sort_key(self) -> tuple[datetime.datetime, float]:
        return (self.when, self.subsec_fraction)


def read_capture_timestamp(path: Path) -> CaptureTimestamp | None:
    """Read `DateTimeOriginal` (with `SubSecTimeOriginal` when present).
    Returns `None` when there is no usable capture timestamp."""
    with path.open("rb") as f:
        tags = exifread.process_file(f, details=False)

    raw = tags.get("EXIF DateTimeOriginal")
    if raw is None:
        return None
    try:
        # Deliberately naive: EXIF DateTimeOriginal carries no timezone, and
        # only relative ordering within one shoot matters here.
        when = datetime.datetime.strptime(  # noqa: DTZ007
            str(raw), "%Y:%m:%d %H:%M:%S"
        )
    except ValueError:
        return None

    subsec_fraction = 0.0
    subsec = tags.get("EXIF SubSecTimeOriginal")
    if subsec is not None:
        digits = str(subsec).strip()
        if digits.isdigit():
            subsec_fraction = float(f"0.{digits}")

    return CaptureTimestamp(when=when, subsec_fraction=subsec_fraction)


@dataclasses.dataclass(frozen=True)
class CanonicalOrder:
    order: list[str]
    used_filename_fallback: bool


def compute_canonical_order(input_dir: Path, names: list[str]) -> CanonicalOrder:
    """Sort the whole catalogue by capture timestamp, breaking ties with
    natural filename order. If any file lacks a usable timestamp, the whole
    catalogue falls back to natural filename order instead — including a
    file outside any particular selection, because this function always
    considers every name it is given, independent of any later selection.
    """
    timestamps = {name: read_capture_timestamp(input_dir / name) for name in names}

    if any(ts is None for ts in timestamps.values()):
        return CanonicalOrder(
            order=sorted(names, key=natural_sort_key), used_filename_fallback=True
        )

    order = sorted(
        names,
        key=lambda name: (timestamps[name].sort_key(), natural_sort_key(name)),
    )
    return CanonicalOrder(order=order, used_filename_fallback=False)
