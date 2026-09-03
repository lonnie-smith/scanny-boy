"""The library: slugged roll folders, and the two ways `roll list` and
`roll info` see them. See `docs/PHASE3_IMPLEMENTATION_PLAN.md` section 3.1
for the library's rules and section 3.2 for slugging and renaming.

Rolls are registered in the library database (`scanny_boy.library`), which
is what `scan_library` reports from; the filesystem supplies the folder the
stitched TIFFs live in.
"""

from __future__ import annotations

import dataclasses
import os
import re
import unicodedata
import uuid
from pathlib import Path
from typing import Any

from scanny_boy.events import Code, WarningEvent
from scanny_boy.library import repo
from scanny_boy.library.repo import RollNotRegisteredError
from scanny_boy.roll_manifest import (
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


def create_roll(library: Path, name: str) -> Path:
    """Create a new roll folder under `library` (slug + collision rule) and
    write an empty v3 manifest into it via `new_roll_manifest`. Returns the
    roll's directory.

    A roll records no grouping of its own: `shots_per_negative` is each
    stitch batch's choice (`run`/`convert --per-negative`, stored in the
    work manifest), so a roll can hold negatives stitched from different
    scan counts."""
    library.mkdir(parents=True, exist_ok=True)
    folder_name = unique_folder_name(library, slugify(name))
    roll_dir = library / folder_name
    roll_dir.mkdir()
    manifest = new_roll_manifest(
        roll_id=str(uuid.uuid4()),
        roll_name=name,
    )
    write_roll_manifest(roll_dir, manifest)
    return roll_dir


def rename_roll(roll_dir: Path, new_name: str) -> Path:
    """Section 3.2: move the folder first, then write `roll_name` — so a
    failed move leaves both the folder and the record untouched. Renaming
    to a name that slugs to the folder's current name (case-insensitively)
    is a no-op move.

    The record now lives in the library database, so the order is: load it
    (the row still names the old folder), move the folder, then save — the
    save updates the row's `folder_path` to the new location.

    Section 5.5: a failed move raises `RollFolderError(ROLL_RENAME_FAILED)`
    rather than a raw `OSError`, so `roll rename` (the CLI command Chunk
    P3-10 added) has one exception type to catch, matching every other
    subcommand's pattern.
    """
    manifest = repo.load_roll(roll_dir)

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
                Code.ROLL_RENAME_FAILED,
                f"could not rename {roll_dir} to {new_path}: {exc}",
            ) from exc

    manifest.roll_name = new_name
    write_roll_manifest(new_path, manifest)
    return new_path


def delete_roll(roll_dir: Path, *, emit: Any) -> dict:
    """`roll delete`: unregister the roll — its runs, sources, negatives,
    and edits rows cascade away with it — and unlink the negatives'
    rendered previews. The folder itself is deliberately not touched: the
    app trashes it first with `NSWorkspace.recycle`, and `roll list` stops
    reporting the roll because the registration is gone.

    Like `run_edit_delete`, the record goes first: a crash then leaves an
    orphan preview file, never a dangling registration. A failed unlink is
    a warning (`ORPHAN_FILE_NOT_REMOVED`), not a failure — the registration
    is already gone, so re-deleting cannot help. Returns the `RollDeleted`
    event's field values."""
    try:
        manifest = repo.load_roll(roll_dir)
    except RollNotRegisteredError as exc:
        raise RollFolderError(exc.code, exc.message) from exc

    previews = [
        Path(negative.preview_path)
        for negative in manifest.negatives
        if negative.preview_path
    ]
    repo.delete_roll(roll_dir)

    for path in previews:
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            emit(
                WarningEvent(
                    code=Code.ORPHAN_FILE_NOT_REMOVED,
                    message=f"{path} could not be removed: {exc}",
                )
            )

    return {"roll_id": manifest.roll_id, "path": str(roll_dir)}


def scan_library(library: Path) -> list[RollListing]:
    """What `roll list` reports from: every roll registered under `library`,
    straight from the database. A roll whose folder has vanished (an
    unmounted external drive, a manual delete) becomes an `"unreadable"`
    listing rather than silently disappearing."""
    listings: list[RollListing] = []
    for folder_path, roll_id, roll_name, negative_count in repo.registered_rolls_under(
        library
    ):
        if not Path(folder_path).is_dir():
            listings.append(
                RollListing(
                    path=Path(folder_path),
                    status="unreadable",
                    reason=(Code.ROLL_NOT_FOUND.value, f"{folder_path} does not exist"),
                    roll_id=None,
                    roll_name=roll_name,
                    negative_count=None,
                )
            )
            continue
        listings.append(
            RollListing(
                path=Path(folder_path),
                status="ok",
                reason=None,
                roll_id=roll_id,
                roll_name=roll_name,
                negative_count=negative_count,
            )
        )
    return listings
