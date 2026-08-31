"""The library: slugged roll folders, and the two ways `roll list` and
`roll info` see them. See `docs/PHASE3_IMPLEMENTATION_PLAN.md` section 3.1
for the library's rules and section 3.2 for slugging and renaming.

**The filesystem is the source of truth** (section 3.1): there is no index or
registry anywhere. `list_rolls` and `scan_library` perform the one-level scan
of the library that `roll list` reports from; nothing here caches it.
"""

from __future__ import annotations

import dataclasses
import os
import re
import unicodedata
import uuid
from pathlib import Path

from scanny_boy.events import Code
from scanny_boy.manifest import BadManifestError
from scanny_boy.roll_manifest import (
    ROLL_MANIFEST_FILENAME,
    RollManifestUnsupportedError,
    load_roll_manifest,
    new_roll_manifest,
    write_roll_manifest,
)

SLUG_MAX_LENGTH = 60
FALLBACK_SLUG = "roll"

# Section 3.5: `roll init` fails `ROLL_EXISTS` only if the computed folder
# exists and is not creatable after 99 suffix attempts.
MAX_SUFFIX_ATTEMPTS = 99

_INVALID_SLUG_RUN = re.compile(r"[^A-Za-z0-9._-]+")


class RollFolderError(Exception):
    def __init__(self, code: Code, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclasses.dataclass(frozen=True)
class RollListing:
    path: Path
    status: str  # "ok" | "unreadable"
    reason: tuple[str, str] | None  # (code, message) when unreadable
    roll_id: str | None
    roll_name: str | None
    negative_count: int | None


def slugify(name: str) -> str:
    """Section 3.2's slug rule: NFC-normalise, replace runs of characters
    outside `[A-Za-z0-9._-]` (whitespace included) with a single `-`, strip
    leading/trailing `-` and `.`, truncate to `SLUG_MAX_LENGTH`. An empty
    result becomes `FALLBACK_SLUG`. Case is preserved — only collision
    comparison is case-insensitive, in `unique_folder_name`."""
    normalized = unicodedata.normalize("NFC", name)
    replaced = _INVALID_SLUG_RUN.sub("-", normalized)
    stripped = replaced.strip("-.")
    truncated = stripped[:SLUG_MAX_LENGTH]
    return truncated or FALLBACK_SLUG


def _sibling_names(library: Path) -> set[str]:
    if not library.is_dir():
        return set()
    return {p.name.lower() for p in library.iterdir() if p.is_dir()}


def unique_folder_name(library: Path, slug: str) -> str:
    """`slug`, or `slug`-2, -3, ... until a name free of `library`'s existing
    child directories (compared lowercased) is found. Raises
    `RollFolderError(ROLL_EXISTS)` after `MAX_SUFFIX_ATTEMPTS` suffixed
    attempts all collide."""
    existing = _sibling_names(library)
    if slug.lower() not in existing:
        return slug
    for suffix in range(2, MAX_SUFFIX_ATTEMPTS + 2):
        candidate = f"{slug}-{suffix}"
        if candidate.lower() not in existing:
            return candidate
    raise RollFolderError(
        Code.ROLL_EXISTS,
        f"could not find a free folder name for {slug!r} under {library}",
    )


def create_roll(library: Path, name: str, shots_per_negative: int) -> Path:
    """Create a new roll folder under `library` (slug + collision rule) and
    write an empty v2 manifest into it via `new_roll_manifest`. Returns the
    roll's directory."""
    library.mkdir(parents=True, exist_ok=True)
    folder_name = unique_folder_name(library, slugify(name))
    roll_dir = library / folder_name
    roll_dir.mkdir()
    manifest = new_roll_manifest(
        roll_id=str(uuid.uuid4()),
        roll_name=name,
        shots_per_negative=shots_per_negative,
    )
    write_roll_manifest(roll_dir, manifest)
    return roll_dir


def rename_roll(roll_dir: Path, new_name: str) -> Path:
    """Section 3.2: move the folder first, then write `roll_name` — so a
    failed move leaves both the folder and the manifest untouched. Renaming
    to a name that slugs to the folder's current name (case-insensitively)
    is a no-op move.

    Section 5.5: a failed move raises `RollFolderError(ROLL_RENAME_FAILED)`
    rather than a raw `OSError`, so `roll rename` (the CLI command Chunk
    P3-10 added) has one exception type to catch, matching every other
    subcommand's pattern.
    """
    library = roll_dir.parent
    slug = slugify(new_name)
    if slug.lower() == roll_dir.name.lower():
        new_path = roll_dir
    else:
        new_path = library / unique_folder_name(library, slug)
        try:
            os.rename(roll_dir, new_path)
        except OSError as exc:
            raise RollFolderError(
                Code.ROLL_RENAME_FAILED, f"could not rename {roll_dir} to {new_path}: {exc}"
            ) from exc

    manifest = load_roll_manifest(new_path)
    manifest.roll_name = new_name
    write_roll_manifest(new_path, manifest)
    return new_path


def list_rolls(library: Path) -> list[Path]:
    """One level deep, unsorted: every child directory of `library` holding
    a `scanny-boy-roll.json`. Does not load any of them."""
    if not library.is_dir():
        return []
    return [
        p
        for p in library.iterdir()
        if p.is_dir() and (p / ROLL_MANIFEST_FILENAME).exists()
    ]


def scan_library(library: Path) -> list[RollListing]:
    """What `roll list` reports from: every roll `list_rolls` finds, loaded
    and validated. A manifest that fails to load or is not format version 2
    becomes an `"unreadable"` listing carrying its section 3.12 code, rather
    than raising. `negative_count` excludes superseded negatives."""
    listings: list[RollListing] = []
    for roll_dir in list_rolls(library):
        try:
            manifest = load_roll_manifest(roll_dir)
        except (BadManifestError, RollManifestUnsupportedError) as exc:
            listings.append(
                RollListing(
                    path=roll_dir,
                    status="unreadable",
                    reason=(exc.code.value, exc.message),
                    roll_id=None,
                    roll_name=None,
                    negative_count=None,
                )
            )
            continue
        listings.append(
            RollListing(
                path=roll_dir,
                status="ok",
                reason=None,
                roll_id=manifest.roll_id,
                roll_name=manifest.roll_name,
                negative_count=len(manifest.live_negatives()),
            )
        )
    return listings
